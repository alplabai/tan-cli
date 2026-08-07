# SPDX-License-Identifier: Apache-2.0
"""`tan flash` -- walk `build/system-manifest.yaml` and program every slice +
helper MCU onto attached hardware in `boot_order`.

Port of `crates/tan-cli/src/commands/flash/mod.rs`: the IO half only. Every
argv, decision and message is pure in `tan.core.flash_plan`; this module
resolves paths, probes PATH, spawns subprocesses and materialises the J-Link
Commander temp file.

**Per-entry rc convention**, mirroring `alp_flash._flash_entry` exactly:
`0` success / clean-dry-run / clean-skip-via-flag, `-1` silently skipped (no
`flash_method` / tools missing under `--skip-missing-tools` / an unresolved
`TBD` in `flash_args`), `>0` failed -- including an `output_artefact`/
`firmware_path` that is the unresolved `TBD` sentinel rather than a path
(**#222**: a `TBD` in `flash_args` skips, a `TBD` artefact fails).
`failed` counts only `rc > 0`; skipped
entries never count. Within rc 0, `status` further distinguishes a real/dry-run
`ok` from a `planned` entry -- the confirm gate declining a REAL write, nothing
programmed -- so a `--format json` consumer cannot mistake a no-op for a
completed flash (**I-30**: this used to report byte-identical to a real write).

**This command writes to hardware.** Two rules follow, and neither is style:

* Nothing but the single JSON envelope may reach stdout under `--format json`.
  Every spawned tool's output is CAPTURED in JSON mode (never inherited), and
  the human transcript goes to stderr.
* No exception may escape. A raw traceback is an empty stdout, and the
  extension then renders nothing at all with no error on either side. The guard
  in `flash` catches everything and reports `flash.internal-failure`; every
  helper it calls on its recovery path is chosen to be incapable of raising.

**Workspace venv + west topdir (tan-cli#289/#59/#61).** Rust resolves a
workspace venv (`venv_bin_dir`, so a GUI-launched editor's PATH-less `west` is
still found) and the west workspace topdir (`west_workspace_dir`, which
becomes each child's cwd so `west flash` can see alp-sdk's out-of-tree
runners). Both are resolved once per run in [`_run`] and threaded through
[`_Context`]: `venv_bin` widens the required-tool gate ([`_tool_available`])
and rewrites the spawned program to the venv's own copy
([`_programs_resolved_in_venv`]), and `workspace` becomes every spawned
child's cwd. The search itself is shared, not duplicated, with
`tan.commands.build.execute` -- both consume `tan.core.venv`.
"""
from __future__ import annotations

import functools
import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import typer

from tan.commands.build_cmd import resolve_sdk_root_ladder
from tan.commands.doctor_cmd import on_path
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.core.flash_plan import (
    FAIL,
    FLOW_D_METHOD,
    PIPE,
    SKIP,
    FlashInputs,
    FlashPlan,
    FlashPlanError,
    FlashTarget,
    FlowDShape,
    ManifestError,
    YOCTO_WIC_METHODS,
    _DEV_ROOT,
    backend_for,
    display_argv,
    fa_bool_checked,
    fa_str,
    fa_str_checked,
    flash_args_has_tbd,
    flow_d_preflight_script,
    is_pending,
    is_rust_absolute,
    parse_atoc_start_address,
    parse_system_manifest,
    plan_flash_targets,
    registry_keys_debug,
    resolve_artefact_path,
    select_flash_method,
    tool_gate,
    validate_flow_d_preflight_args,
    validate_flow_d_shape,
)
from tan.core.global_flags import accept_global_flags
from tan.core.setools import (
    find_app_gen_toc,
    missing_tool_message,
    resolve_setools_dir,
    sign_slot0,
    unresolved_message,
)
from tan.core.venv import prepend_path, tool_in_venv, venv_bin_dir, west_workspace_dir
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format
from tan.core.shapes import is_sdk_root

#: `data.schemaVersion` -- the STRING "1", not the integer. Rust serializes it
#: as `&'static str` and the extension compares it as one.
_DATA_SCHEMA_VERSION = "1"

#: Seconds any single spawned flash tool may run before it is killed. A flash
#: tool that hangs (a probe mid-handshake, `dd` on a device that stopped
#: answering, `west flash` waiting on a serial prompt that will never come) must
#: not hang `tan` forever: I-23's scar is a CI job that runs to the runner's own
#: timeout with no output at all. Generous -- a real MRAM/eMMC write is seconds
#: to minutes, and a wrongly-short timeout would abort a write MID-FLIGHT, which
#: on a bootloader partition is worse than waiting.
_FLASH_TIMEOUT_S = 900.0

#: The read-only DPIDR preflight is a connect-and-quit; it must not inherit the
#: write timeout.
_PREFLIGHT_TIMEOUT_S = 60.0

#: Seconds to wait for the pipeline's stderr reader after both children are gone
#: (`_Drain`). Bounded, not indefinite: the reader is what carries the
#: decompressor's diagnosis into the outcome (tan-cli#401), but a `tan` that
#: hangs on a thread rather than reporting a flash result would be the worse
#: trade -- both children have already exited by every path that joins it.
_DRAIN_JOIN_S = 2.0


@dataclass
class _Entry:
    """One entry's result in the envelope `data.entries[]`."""

    kind: str
    id: str
    method: str | None
    status: str
    rc: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "id": self.id}
        # ABSENT, not null, when the entry never resolved a method -- Rust's
        # `skip_serializing_if = "Option::is_none"`. Verified against the oracle
        # on the `update_channel` helper, whose entry carries no `method` key.
        if self.method is not None:
            out["method"] = self.method
        out["status"] = self.status
        out["rc"] = self.rc
        out["message"] = self.message
        return out


@dataclass
class _Outcome:
    """What a spawn produced: success, plus -- in capture mode only -- the output
    the SINGLE spawn collected, so the failure message reuses it instead of
    re-running the hardware-programming tool (which would re-flash the device on
    a first-attempt failure)."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    captured: bool = False


def _abs_join(*parts: str) -> str:
    """`Path::join` on a native string, WITHOUT normalisation.

    `os.path.join`, never `pathlib`: Rust's `cwd.join(".")` keeps the `.`
    component and the envelope's `data.buildRoot` ships it (verified against the
    shipped binary: `...\\app\\.\\build` for the default `app_path` of `.`).
    `Path.cwd() / "."` silently drops it, so the two implementations would
    disagree on the default invocation -- the most common one there is."""
    return os.path.join(*parts)


def workspace_root(project: str | None = None) -> str:
    """`util.rs::cli_workspace_root` -- the CWD, joined with the GLOBAL
    `--project` flag.

    **Not `app_path`.** Rust anchors both `project.*` and SDK discovery on
    `cli_workspace_root(g)`, which is the cwd joined with the GLOBAL `--project`
    flag; `app_path` is the flash-local positional and feeds ONLY `build_root`.
    They coincide on the default `tan flash .` and diverge the moment anyone runs
    `tan flash app`: the oracle then reports `project.root` = cwd and looks for
    the SDK beside the CWD, while an app_path-anchored port reports `cwd/app` and
    hunts for the SDK a level too deep -- verified on both, and invisible to any
    test that only ever passes `.`.

    `project` is joined via `os.path.join`, mirroring `build_cmd.build`'s
    `Path(os.path.join(str(cwd), project))` -- an absolute `--project` value
    replaces the cwd outright, same as `os.path.join`'s own rule.

    **Cannot raise.** `os.getcwd()` throws `FileNotFoundError` when the working
    directory has been deleted underneath the process -- entirely reachable, since
    a flash normally follows a build and a cleanup script can remove the tree in
    between. This function is called from OUTSIDE the exception guard (the guard's
    own recovery path reports `project`), so a throw here would be the port's
    recurring double fault: the guard cannot report an envelope because building
    the envelope is what failed. `"."` is the honest fallback -- a relative root
    in the envelope is a visibly odd value, which is strictly better than an empty
    stdout.
    """
    try:
        cwd = os.getcwd()
    except OSError:
        return "."
    return os.path.join(cwd, project) if project else cwd


def _resolve_project(root: str, board_yaml: str | None) -> Project:
    """`(project.root, project.boardYaml)`, both posix.

    `board.yaml`'s existence is NOT checked by the join below, matching
    `project.rs::resolve_board_yaml_path` -- it names where one WOULD live. The
    `Project.resolved` call at the end is the seam that checks (tan-cli#236):
    `project.boardYaml` is `null`, not this joined path, from a scratch
    directory holding no `board.yaml` at all.

    Every step is wrapped: `os.path.abspath` calls `getcwd()` for a relative
    input and therefore inherits `workspace_root`'s deleted-cwd failure mode, and
    this runs OUTSIDE the exception guard. See `workspace_root` for why a throw
    here is unrecoverable rather than merely wrong.
    """
    try:
        resolved_root = os.path.abspath(root)
        configured = board_yaml or "board.yaml"
        resolved = (
            configured if os.path.isabs(configured) else os.path.join(resolved_root, configured)
        )
    except (OSError, ValueError):
        return Project(root=None, board_yaml=None)
    return Project.resolved(
        resolved_root.replace("\\", "/"), resolved.replace("\\", "/")
    )


@dataclass(frozen=True)
class _SdkResolution:
    """The local shape both `_resolve_sdk` and [`resolve_sdk_root_ladder_safe`]
    return -- a named result, not a growing tuple (the same reasoning
    `build_cmd.SdkRootResolution` documents): a fact one caller needs
    (`foreign_global_default_for`, tan-cli#464) must not force every field
    before it to be re-counted by position.

    `tier` is always a real tier string, never Python `None` -- `"none"` is
    the oracle's own spelling for "nothing resolved" (`project_pin_issue`
    interpolates it into its message; a bare `None` would have read
    "falling through to the None tier instead")."""

    path: str | None
    tier: str
    broken_project_pin: str | None = None
    foreign_global_default_for: str | None = None


def _resolve_sdk(sdk_root: str | None, workspace_root: str) -> _SdkResolution:
    """`util.rs::resolve_sdk_root`:
    `--sdk-root` (terminal) > the project's own `.alp/sdk-path` pin > the
    machine-global default (`~/.alp/sdk-default`) > the wide positional walk --
    the oracle's closed five-value `SdkSourceTier` (`SdkRootFlag`, `ProjectPin`,
    `GlobalDefault`, `Discovery`, `None`); no `ALP_SDK_ROOT` tier (tried and
    reverted -- the oracle only ever WRITES that variable into a build
    slice's env, never reads it back for discovery; the project-pin tier
    already makes `tan init && tan build` compose without it).

    `--sdk-root` is TERMINAL and returned AS GIVEN when it holds the loader
    marker, else the whole command fails (I-31): a bad path must fail loudly
    rather than silently fall through to a lower tier and build/flash against a
    different SDK. The pin/global-default/positional-walk tiers are
    best-effort -- previously skipped here entirely (this port had no writer
    for the pointer files when this comment was written; `tan init` writes
    `.alp/sdk-path`, so skipping them silently ignored it).

    `.broken_project_pin` (tan-cli#263 review): `None` on the `--sdk-root`
    branch (nothing to fall through from), else whatever
    [`resolve_sdk_root_ladder_safe`] carried through. Same for
    `.foreign_global_default_for` (tan-cli#464)."""
    if sdk_root is not None:
        return _SdkResolution(sdk_root if _is_sdk_root(sdk_root) else None, "sdkRootFlag")
    return resolve_sdk_root_ladder_safe(workspace_root)


#: tan-cli#408: one implementation, in `tan.core.shapes`, imported under the
#: private name this module's call site already uses. The guard against
#: `OSError`/`ValueError` that lived here moved with it, deliberately -- see
#: `is_sdk_root`'s own docstring for why a pre-flight probe must not raise.
_is_sdk_root = is_sdk_root


def resolve_sdk_root_ladder_safe(workspace_root: str) -> _SdkResolution:
    """`build_cmd.resolve_sdk_root_ladder(None, ...)`, made incapable of
    raising -- an unreadable `.alp/sdk-path` pin, an unreadable global-default
    pointer (`~/.alp/sdk-default`), or an unreadable ancestor on the
    positional walk must not become a traceback in a command whose whole job
    is to report an envelope."""
    try:
        resolution = resolve_sdk_root_ladder(None, Path(workspace_root))
    except (OSError, ValueError):
        return _SdkResolution(None, "none")
    found = resolution.path
    if found is None:
        # `resolution.tier` is already "none" here -- carried through, not
        # dropped, so a broken pin with no working fallback still names a
        # real tier in `project_pin_issue`'s message.
        return _SdkResolution(None, resolution.tier, resolution.broken_project_pin)
    return _SdkResolution(
        str(found),
        resolution.tier,
        resolution.broken_project_pin,
        resolution.foreign_global_default_for,
    )


def _tool_available(tool: str, venv_bin: Path | None = None) -> bool:
    """A tool counts as available when it is on PATH **or** provided by the
    west-capable workspace venv (`venv_bin`, when one resolved), mirroring
    Rust's `tool_available` (tan-cli#289/#59): `west` is the case that
    matters -- `tan bootstrap` installs it INSIDE the venv, and a
    GUI-launched editor's PATH never has it. `doctor_cmd.on_path` walks
    `$PATH` by hand rather than using `shutil.which`, which on Windows probes
    the CURRENT DIRECTORY first -- a project checked out with its own
    `openocd.exe` at its root would otherwise be reported as this host's
    tooling and then SPAWNED against attached silicon."""
    try:
        if on_path(tool) is not None:
            return True
    except (OSError, ValueError):
        pass
    return venv_bin is not None and tool_in_venv(venv_bin, tool) is not None


# ── spawning ────────────────────────────────────────────────────────────────


def _spawn(
    argv,
    capture: bool,
    timeout: float,
    venv_bin: Path | None = None,
    workspace: str | None = None,
) -> _Outcome:
    """One process. Captured in JSON mode (the output is kept for the failure
    message and never re-spawned), inherited-to-stderr in text mode so a long
    write streams live.

    In text mode the child's stdout is redirected to **stderr**, not inherited:
    stdout is the envelope channel for this process even when this run is not
    using it, and a flash tool that prints to stdout would otherwise put
    non-envelope bytes there. Rust can inherit safely because its text path
    never writes an envelope at all; here the same process object owns both.

    `venv_bin` (tan-cli#289/#59), when given, is prepended onto the child's
    PATH -- `env=None` (the default, passed through unchanged) means
    "inherit this process's own environment", exactly the pre-#59 behaviour.
    `workspace` (tan-cli#289/#61), when given, becomes the child's cwd, so
    `west flash` can see alp-sdk's out-of-tree runners.
    """
    env = prepend_path(dict(os.environ), venv_bin) if venv_bin is not None else None
    try:
        if capture:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                cwd=workspace,
            )
            return _Outcome(
                success=proc.returncode == 0,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                returncode=proc.returncode,
                captured=True,
            )
        sink = _stderr_sink()
        if sink is None:
            # stderr has no OS-level handle to hand a child (a pytest/embedded
            # capture object). Capture and REPLAY instead of failing the spawn:
            # a flash must still run when the console is wrapped, it just cannot
            # stream live.
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                cwd=workspace,
            )
            if proc.stdout:
                print(proc.stdout, end="", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr)
            return _Outcome(success=proc.returncode == 0, returncode=proc.returncode)
        proc = subprocess.run(list(argv), stdout=sink, timeout=timeout, env=env, cwd=workspace)
        return _Outcome(success=proc.returncode == 0, returncode=proc.returncode)
    except subprocess.TimeoutExpired as exc:
        return _Outcome(
            success=False,
            stderr=_timeout_stderr(exc, timeout),
            captured=capture,
        )
    except OSError as err:
        # The tool vanished between the gate and the spawn, is a DIRECTORY, or
        # is not executable. All three are ordinary host states, not tan bugs,
        # so they become a failed entry rather than reaching the outer guard.
        return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)


def _timeout_stderr(exc: subprocess.TimeoutExpired, timeout: float) -> str:
    """The timeout report, with whatever the child managed to print BEFORE the
    kill folded in ahead of the sentence that names the failure.

    `subprocess.TimeoutExpired` carries `.stdout`/`.stderr` from a
    `capture_output=True` spawn -- both the JSON-mode branch and the
    wrapped-console text-mode branch of `_spawn` use one -- and this used to
    be discarded outright, keeping only the generic sentence (tan-cli#487,
    defect 4's wrapped-console half): the two `print()` replay lines in that
    branch exist to show the operator what the tool said before a kill, and a
    `TimeoutExpired` raises PAST them, straight to this handler, so on a
    wrapped console (a pytest/embedded capture object with no OS-level
    stderr handle) a multi-GB `.wic` write killed mid-transfer used to report
    only `flash command failed` with no hint a truncated image is now on the
    card. The third spawn variant (`stdout=sink`, direct-to-console
    streaming) never sets `capture_output`, so `exc.stdout`/`.stderr` are
    `None` there and this falls back to the bare sentence, unchanged --
    nothing was lost that this function could recover, since that variant's
    own output already reached the console directly.

    `_text`, not a bare `isinstance(chunk, str)` check: measured,
    `subprocess.TimeoutExpired.stdout`/`.stderr` are `bytes` even when the
    spawn itself passed `text=True` -- the decode `subprocess.run` normally
    applies never runs on the exception's own partial-output attributes
    (a `Popen.communicate()`-timeout quirk, not a tan bug), so a naive `str`
    check silently dropped every real capture here.

    The sentence goes LAST, same reasoning as `_spawn_pipeline`'s sibling
    `_timed_out_stderr`: `_capture_tail` keeps the final four non-empty
    lines, and the sentence is the one that names this failure mode.
    """
    lines: list[str] = []
    for chunk in (exc.stdout, exc.stderr):
        lines.extend(line for line in _text(chunk).splitlines() if line.strip())
    lines.append(f"timed out after {timeout:.0f}s and was killed")
    return "\n".join(lines)


def _stderr_sink():
    """`sys.stderr` when it has a real OS handle a child can inherit, else `None`.

    **A DELIBERATE divergence from the oracle.** Rust's text path calls
    `cmd.status()`, which INHERITS stdio, so a flash tool's stdout lands on
    tan's stdout. Here a child's stdout is routed to STDERR instead. Both are
    safe today -- Rust's text mode writes nothing to stdout either
    (`main.rs::emit` uses `eprintln!`) -- but in this process stdout is the
    envelope channel and the redirect makes that unconditional rather than true
    only as long as nobody adds a stdout write to the text path. Visible only to a
    caller doing `tan flash > log` in TEXT mode; `--format json` captures on both
    sides and is byte-identical (43 diffed cases).

    NOT the only divergence in this file any more: `plan_flash_targets`
    (`tan.core.flash_plan.TargetPlan.refused_skipped`) treats a `status:
    skipped` slice/helper as a warning that alone never fails the run, where
    the shipped Rust `plan_flash_targets` has no such bucket and refuses (and
    fails) a `status: skipped` slice exactly like any other non-`ok` status.
    See `TargetPlan.refused_skipped` for the reasoning and
    `tests/parity/test_flash_oracle_parity.py` for why that case is not diffed
    against the oracle.
    """
    try:
        sys.stderr.fileno()
    except (OSError, ValueError, AttributeError):
        return None
    return sys.stderr


@dataclass(frozen=True)
class _Half:
    """One process of a two-process pipeline, once it is over: the program a
    reader knows it by, the code it exited with (`None` when it was killed before
    it reported one), and everything it said on stderr."""

    program: str
    returncode: int | None
    stderr: str


def _program_label(argv) -> str:
    """The half's program as the reader knows it -- `gunzip`, not the absolute
    path `_programs_resolved_in_venv` may have rewritten `argv[0]` into
    (tan-cli#401/#289). A bare basename because this label goes into a failure
    message that already has four lines to spend."""
    argv = list(argv)
    return os.path.basename(argv[0]) if argv else "?"


def _half_lines(half: _Half, *, captured: bool) -> list[str]:
    """One attributed section: an `<program> exited rc=<n>:` header over the
    half's own non-empty stderr lines.

    A half that both SUCCEEDED and said nothing contributes no section at all --
    a header alone for a clean `dd` is noise in a four-line window. A half that
    FAILED always gets its header even when it printed nothing, because "it
    exited 9 and said nothing" is then the entire diagnosis available, and
    dropping it would leave the message reading as if only the other half spoke
    (tan-cli#401) -- PROVIDED `captured` is true, i.e. this half's stderr was
    actually piped and read (`_spawn_pipeline` only does that when
    `capture=True`). tan-cli#487 review finding 5: in an UNCAPTURED
    (text-mode) pipeline NEITHER half's stderr is ever piped -- both
    processes inherit stdio instead -- so "it exited 9 and said nothing" is
    not a diagnosis there, it is an artifact of never having listened, and a
    body-less header for it produced a dangling `"<program> exited rc=1:"`
    with nothing following, contradicting `_execute_message`'s own docstring
    claim that an ordinary text-mode failure leaves `outcome.stderr` empty."""
    body = [line for line in half.stderr.splitlines() if line.strip()]
    if not body and (half.returncode == 0 or not captured):
        return []
    rc = "unknown" if half.returncode is None else half.returncode
    return [f"{half.program} exited rc={rc}:", *body]


def _pipeline_stderr(left: _Half, right: _Half, *, captured: bool) -> str:
    """Both halves' stderr, attributed, with the failing halves LAST.

    Ordering is the whole point (tan-cli#401). `_capture_tail` keeps only the
    last four non-empty lines of this text, and `dd status=progress` fills three
    of them on every single run whether it wrote a good image or a truncated one
    -- so folding the decompressor's stderr in anywhere but the end would still
    leave `data.entries[].message` reading `0+61 records in | 0+1 records out |
    3997696 bytes transferred`, which is what #401 measured. Among two failing
    halves the LEFT one goes last: it is upstream, so it is the cause and the
    right half's failure is its consequence. A healthy pipeline keeps the
    natural left-to-right reading order.

    `captured` (tan-cli#487 review finding 5): threaded straight through to
    [`_half_lines`] -- see its docstring."""
    ok = [half for half in (left, right) if half.returncode == 0]
    failed = [half for half in (right, left) if half.returncode != 0]
    lines = [line for half in (*ok, *failed) for line in _half_lines(half, captured=captured)]
    return "\n".join(lines) + "\n" if lines else ""


def _pipeline_returncode(left_rc: int | None, right_rc: int | None) -> int:
    """The code the entry reports for the pipeline: the FIRST non-zero of the two
    halves, left first.

    Reporting only the right half's code -- what this did until tan-cli#401 --
    means a `.wic.gz` that died in `gunzip` is reported as `returncode=0`,
    because `dd` happily exits 0 after writing whatever short stream it was fed.
    `None` (a half killed before it reported a code) is not a zero and never
    becomes one: with no honest code to report the pipeline reports `-1`, the
    same sentinel `_Outcome` defaults to. Pure."""
    for rc in (left_rc, right_rc):
        if rc:
            return rc
    return 0 if left_rc is not None and right_rc is not None else -1


class _Drain:
    """The left half's stderr, read to EOF on a background thread for the
    pipeline's lifetime.

    Creating that pipe without reading it is a silent hang mid-write to a real
    block device: once the decompressor writes more than the OS pipe buffer its
    `write()` blocks forever, it never reaches EOF on stdout, dd's `read()`
    blocks too, and the `wait()` never returns. So the read has to be concurrent
    with the write, which is why it is a thread and not a `.read()` at the end.

    `stream` is `None` in text mode, where the child's stderr is inherited and
    streams straight to the terminal -- there is nothing here to drain, and
    [`text`] answers `""`."""

    def __init__(self, stream) -> None:
        self._chunks: list[bytes] = []
        self._stream = stream
        started = threading.Thread(target=self._read, daemon=True) if stream is not None else None
        self._thread = started
        if started is not None:
            started.start()

    def _read(self) -> None:
        try:
            self._chunks.append(self._stream.read() or b"")
        except (OSError, ValueError):
            # A stream closed underneath the reader. Whatever it had already
            # collected still gets reported; raising here would only kill a
            # daemon thread nobody is watching.
            pass

    def join(self) -> None:
        """Wait for the reader, briefly. Bounded because this also runs on the
        pipeline's cleanup path, where the child may be unkillable (a `dd` in
        uninterruptible IO) and `tan` must still return an outcome."""
        if self._thread is not None:
            self._thread.join(timeout=_DRAIN_JOIN_S)

    def text(self) -> str:
        """Everything the half said, decoded. Joins first: reading `_chunks`
        while the thread may still be appending is exactly the read that made
        the drained bytes look empty and invited dropping them (tan-cli#401)."""
        self.join()
        return _text(b"".join(self._chunks))


def _spawn_pipeline(
    left,
    right,
    capture: bool,
    timeout: float,
    venv_bin: Path | None = None,
    workspace: str | None = None,
) -> _Outcome:
    """A decompress -> dd pipeline: wire the decompressor's stdout into dd's
    stdin. Fails when EITHER process fails, and reports the first non-zero of
    their two codes ([`_pipeline_returncode`]).

    BOTH halves' stderr reaches the outcome, each attributed to the program that
    said it ([`_pipeline_stderr`]). Until tan-cli#401 the decompressor's stderr
    was drained (it has to be, see [`_Drain`]) and then thrown away, and the
    outcome carried `dd`'s stderr and `dd`'s rc alone: a `.wic.gz` truncated in
    transit was reported to the extension as `0+61 records in | 0+1 records out |
    3997696 bytes transferred in 0.003888 secs` with `returncode=0`, while
    `gunzip: <image>.wic.gz: unexpected end of file` -- the one line that ends
    the investigation -- had been read by `tan` and dropped. The entry status was
    right and the cause was wrong, which is worse than useless here: the reader
    blames the card or the target and re-runs the flash, writing a partial image
    onto a real block device a second time.

    `venv_bin`/`workspace`: see [`_spawn`] -- the same PATH-prepend/cwd
    threading, applied to BOTH halves of the pipeline (tan-cli#289/#59/#61).
    """
    env = prepend_path(dict(os.environ), venv_bin) if venv_bin is not None else None
    deadline = time.monotonic() + timeout
    try:
        first = subprocess.Popen(  # noqa: S603 -- argv comes from the pure planner
            list(left),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture else None,
            env=env,
            cwd=workspace,
        )
    except OSError as err:
        return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)

    drain = _Drain(first.stderr)
    left_label, right_label = _program_label(left), _program_label(right)
    try:
        try:
            second = subprocess.Popen(  # noqa: S603 -- as above
                list(right),
                stdin=first.stdout,
                stdout=subprocess.PIPE if capture else _stderr_sink(),
                stderr=subprocess.PIPE if capture else None,
                env=env,
                cwd=workspace,
            )
        except OSError as err:
            return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)
        # Close OUR handle on the pipe so the decompressor sees a real EOF when
        # dd exits; otherwise this process keeps the read end open and `first`
        # can block forever on a full buffer.
        if first.stdout is not None:
            first.stdout.close()
        try:
            out, err_text = second.communicate(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate(second)
            _terminate(first)
            killed = _Half(left_label, first.returncode, drain.text())
            return _Outcome(
                success=False,
                stderr=_timed_out_stderr(killed, timeout, captured=capture),
                captured=capture,
            )
        try:
            left_rc: int | None = first.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate(first)
            left_rc = None
        # The drain is joined HERE, before the outcome is built -- not only in
        # the `finally` below, which runs too late to contribute anything to it.
        left_half = _Half(left_label, left_rc, drain.text())
        right_half = _Half(right_label, second.returncode, _text(err_text))
        rc = _pipeline_returncode(left_rc, second.returncode)
        return _Outcome(
            success=rc == 0,
            stdout=_text(out),
            stderr=_pipeline_stderr(left_half, right_half, captured=capture),
            returncode=rc,
            captured=capture,
        )
    finally:
        _terminate(first)
        drain.join()


def _timed_out_stderr(left: _Half, timeout: float, *, captured: bool) -> str:
    """The timeout report, with whatever the decompressor managed to say BEFORE
    it and the sentence itself LAST.

    Last because `_capture_tail` keeps the final four non-empty lines and the
    sentence is the one that names this failure mode; first-half output because
    a `gunzip` that printed a diagnosis and then wedged is a different bench
    problem from one that went quiet, and #401's lesson is that captured stderr
    is never thrown away.

    `captured`: threaded through to [`_half_lines`] for the same reason
    [`_pipeline_stderr`] does -- in text mode `left`'s stderr was never piped
    (it inherited stdio), so a body-less `rc=unknown` header ahead of the
    sentence would claim a diagnosis this process never actually captured."""
    return "\n".join(
        [*_half_lines(left, captured=captured), f"timed out after {timeout:.0f}s and was killed"]
    ) + "\n"


def _terminate(proc) -> None:
    """Best-effort kill of a still-running child. Never raises: it runs on the
    pipeline's cleanup path, and a `finally` that throws would replace a real
    outcome with a traceback."""
    try:
        if proc.poll() is None:
            proc.kill()
    except (OSError, ValueError):
        pass


def _text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _spawn_jlink(
    argv,
    script: str,
    capture: bool,
    timeout: float,
    venv_bin: Path | None = None,
    workspace: str | None = None,
) -> _Outcome:
    """Materialise the Commander script to a temp file, append its path as the
    final `-CommanderScript` argument, spawn, and remove the temp file.

    `newline=""` on the write: `Path.write_text`/a text-mode handle translates
    every `\\n` to `os.linesep`, so on Windows this file would silently become
    CRLF (**I-27**). A J-Link Commander script is line-oriented and a stray `\\r`
    lands inside the `loadbin <path>, <addr>` argument.

    The temp file is removed in a `finally` even on a timeout or a spawn error --
    it carries the flash addresses, and a leaked one in the system temp dir is
    both a mess and a small information leak.
    """
    handle, path = tempfile.mkstemp(prefix="tan-flash-", suffix=".jlink")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as fh:
            fh.write(script)
    except OSError as err:
        _unlink(path)
        return _Outcome(
            success=False,
            stderr=f"could not write the J-Link Commander script: {err}",
            captured=capture,
        )
    try:
        return _spawn([*argv, path], capture, timeout, venv_bin, workspace)
    finally:
        _unlink(path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _programs_resolved_in_venv(argv: list[str], venv_bin: Path | None) -> list[str]:
    """Rewrite every PROGRAM position in `argv` -- `argv[0]`, plus the token
    right after a `"|"` pipeline separator -- to its absolute venv path when
    the venv provides that program, mirroring Rust's
    `programs_resolved_in_venv` (tan-cli#289/#59). Arguments are never
    touched, an already-absolute program is left alone, and a tool the venv
    does not provide keeps its bare name so PATH resolution stays in charge.
    Pure.

    `is_rust_absolute`, not `os.path.isabs`: `flash_plan.py`'s own convention
    (see its docstring) exists precisely because `os.path.isabs` answers
    differently for a rooted-but-driveless Windows path across supported
    Python versions (3.13 changed it) -- this argv-rewrite must not disagree
    with the oracle, or with itself between interpreters on the same host.
    """
    if venv_bin is None:
        return list(argv)
    out: list[str] = []
    is_program = True
    for arg in argv:
        if is_program and not is_rust_absolute(arg):
            out.append(tool_in_venv(venv_bin, arg) or arg)
        else:
            out.append(arg)
        is_program = arg == PIPE
    return out


def _execute(
    plan: FlashPlan, capture: bool, venv_bin: Path | None = None, workspace: str | None = None
) -> _Outcome:
    """Spawn the plan: a pipeline (a `"|"` token), a J-Link plan (temp Commander
    script), or a plain single process.

    `venv_bin`/`workspace` (tan-cli#289/#59/#61): the run-wide west-capable
    workspace venv bin dir and west workspace topdir, resolved once in
    [`_run`]. `argv[0]` (and the post-`"|"` token) is rewritten to the venv's
    own copy when it provides one ([`_programs_resolved_in_venv`]); the venv
    only joins the child's PATH when a program was ACTUALLY resolved there
    (mirroring the oracle's `on_path = if argv == plan.argv { None } else {
    venv_bin }`) -- a plan naming only absolute/non-venv tools must not have
    its PATH silently rewritten for no reason.
    """
    argv = list(plan.argv)
    resolved = _programs_resolved_in_venv(argv, venv_bin)
    on_path_bin = venv_bin if resolved != argv else None
    if PIPE in resolved:
        cut = resolved.index(PIPE)
        return _spawn_pipeline(
            resolved[:cut], resolved[cut + 1 :], capture, _FLASH_TIMEOUT_S, on_path_bin, workspace
        )
    if plan.jlink_script is not None:
        return _spawn_jlink(
            resolved, plan.jlink_script, capture, _FLASH_TIMEOUT_S, on_path_bin, workspace
        )
    return _spawn(resolved, capture, _FLASH_TIMEOUT_S, on_path_bin, workspace)


def _capture_tail(outcome: _Outcome) -> str | None:
    """The failure tail from the ALREADY-captured output -- a pure read, no
    second spawn. The last 4 non-empty lines joined by " | ", or `None` when the
    process actually succeeded."""
    if outcome.success:
        return None
    text = outcome.stderr
    if not text.strip():
        text = outcome.stdout
    tail = [line for line in text.splitlines() if line.strip()][-4:]
    if not tail:
        return f"exited rc={outcome.returncode}"
    return " | ".join(tail)


def _execute_message(outcome: _Outcome, method: str, entry_id: str) -> str:
    """In JSON mode reuse the output already captured by the single spawn (never
    re-run the flash); in text mode the child already streamed, so report the
    rc-style summary -- UNLESS `outcome` itself carries a TAN-AUTHORED
    diagnosis (tan-cli#487, defect 4).

    `outcome.captured` alone used to gate this: true only in JSON mode, so in
    text mode -- the default human invocation of the one command that writes
    hardware -- `_spawn`'s `timed out ... and was killed`/`could not spawn:
    <err>`, `_spawn_pipeline`'s `_timed_out_stderr`, and `_spawn_jlink`'s
    `could not write the J-Link Commander script: <err>` were all composed
    and then discarded, reported as a bare `flash command failed` even though
    every one of them populates `outcome.stderr` regardless of mode. The
    ORDINARY text-mode failure (the child streamed straight to the console
    and then exited non-zero) leaves BOTH `stderr`/`stdout` empty -- there is
    nothing tan itself has to add there, so that case is unaffected: `outcome.
    stderr.strip() or outcome.stdout.strip()` is false and this still falls
    through to the generic sentence, exactly as before."""
    if outcome.captured or outcome.stderr.strip() or outcome.stdout.strip():
        tail = _capture_tail(outcome)
        if tail:
            return f"{method}[{entry_id}]: {tail}"
    return f"{method}[{entry_id}]: flash command failed"


# ── per-entry dispatch ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Context:
    sku: str
    build_root: str
    sdk_root: str
    dry_run: bool
    skip_missing_tools: bool
    force_confirm: bool
    capture: bool
    #: The west-capable workspace venv's bin dir, when one resolves
    #: (tan-cli#289/#59). `None` on CI, an activated venv, or the contract
    #: harness -- every spawn/gate below then behaves exactly as before.
    venv_bin: Path | None = None
    #: The west workspace topdir (holding `.west/`), when one resolves
    #: (tan-cli#289/#61) -- becomes every spawned child's cwd so `west
    #: flash` can see alp-sdk's out-of-tree runners. `None` keeps the old
    #: app-dir cwd, matching the oracle exactly.
    workspace: str | None = None
    #: `--setools-dir` (tan-cli#368) -- the HIGHEST-precedence SETOOLS
    #: source, ahead of `SETOOLS_DIR` and `flash_args.setools_dir`; see
    #: `tan.core.setools.resolve_setools_dir`. `None` when the flag was not
    #: given, in which case resolution falls through to the other two exactly
    #: as before.
    setools_dir: str | None = None


def _resolve_flow_d_atoc_address(flash_args: Any, build_root: str, sdk_root: str) -> Any:
    """Fill in `flash_args.atoc_address` from the `app-gen-toc` build report
    (`flash_args.atoc_map`, an `app-package-map.txt` path) when the manifest
    does not already carry one.

    **The ATOC address is a BUILD-TIME output, not a plan-time metadata fact.**
    `app-gen-toc` writes it fresh at signing time and the runbook says outright
    it shifts per build/config, so nothing under `metadata/**` can express it
    -- an earlier design here assumed it lived in metadata, which was wrong.
    Every bench script reads it the same way
    (`awk '/APP Package Start Address:/{print $NF}' app-package-map.txt |
    tail -1`); see `flash_plan.parse_atoc_start_address` for the byte-identical
    parse. This is the ONE place in `tan flash` that reads a file `plan_*`
    itself never touches -- kept here, not in `flash_plan`, because the module
    docstring is explicit that plan-building stays pure/no-IO.

    Leaves `flash_args` UNCHANGED -- and therefore lets `plan_alif_mram_jlink`
    raise its own, single required-field refusal -- whenever: `atoc_address` is
    already present (an explicit manifest value always wins over a parsed one),
    `atoc_map` is absent, or the map path does not resolve to a real file yet
    (the ordinary "signing has not run" case -- there is nothing to read, so
    `plan_alif_mram_jlink`'s own refusal is the right one).

    Raises `FlashPlanError`, naming the resolved path, when `atoc_map` WAS
    supplied and resolves to a real file but the file itself cannot be used --
    unreadable, or missing the `APP Package Start Address:` marker. Those are
    not "no map yet"; they are "found your map and could not get an address out
    of it", and falling through to `plan_alif_mram_jlink`'s generic
    "flash_args.atoc_address / flash_args.atoc are both required" refusal there
    would tell the user to redo a step they already did.
    """
    try:
        if fa_str_checked(flash_args, "atoc_address", True) is not None:
            return flash_args
    except FlashPlanError:
        return flash_args  # let plan_alif_mram_jlink raise the real refusal
    atoc_map = fa_str(flash_args, "atoc_map")
    if atoc_map is None:
        return flash_args
    map_path = resolve_artefact_path(atoc_map, build_root, sdk_root, _is_file)
    if not _is_file(map_path):
        return flash_args
    try:
        text = _read(map_path)
    except OSError as err:
        raise FlashPlanError(
            f"flash_args.atoc_map resolved to {map_path} but it could not be read "
            f"({err}) -- pass a readable app-package-map.txt, or set "
            "flash_args.atoc_address explicitly."
        ) from err
    address = parse_atoc_start_address(text)
    if address is None:
        raise FlashPlanError(
            f"flash_args.atoc_map resolved to {map_path}, but no 'APP Package "
            "Start Address:' line was found in it -- re-run the SETOOLS "
            "app-gen-toc step so the report is current, or set "
            "flash_args.atoc_address explicitly."
        )
    merged = dict(flash_args)
    merged["atoc_address"] = address
    return merged


def _resolve_flow_d_atoc_path(flash_args: Any, build_root: str, sdk_root: str) -> Any:
    """Resolve `flash_args.atoc` to an absolute path before it reaches
    `plan_alif_mram_jlink`, the same way `atoc_map` (above) and the entry's
    own `output_artefact` (`_flash_entry`, before `FlashInputs` is built)
    already are.

    **tan-cli#289 follow-up.** `atoc` was the one MRAM-write input
    `plan_alif_mram_jlink` read straight off `flash_args` with no resolution
    at all (`fa_str(fa, "atoc")`) -- it goes verbatim into the J-Link
    Commander script's `loadbin`/`verifybin` lines. #289 set the flash
    child's `cwd` to the west workspace topdir (`_run` -> `west_workspace_dir`
    -> `_Context.workspace`), which silently moved every OTHER relative
    input's resolution base off the tan process's own cwd; `atoc` alone kept
    resolving (at the OS level, at spawn time) against whatever that topdir
    happens to be, not `build_root`. This repo's own fixtures spell it as a
    relative `atoc: atoc.bin` in several places, and nothing in `docs/`
    tells an author it must be absolute -- so a relative `atoc` now risks
    writing a stale/foreign file to MRAM, or failing with a confusing
    not-found, purely because the west topdir differs from the build root.
    Resolving it here, at plan time and anchored on `build_root`/`sdk_root`
    exactly like `atoc_map`, removes the ambiguity outright.

    A missing/non-string `atoc` is left untouched: `fa_str` already reads
    that as `None`, and `plan_alif_mram_jlink` raises its own, clearer
    "flash_args.atoc ... required" refusal for it -- this must not turn that
    into a resolved `<build_root>/None` string.
    """
    atoc = fa_str(flash_args, "atoc")
    if atoc is None:
        return flash_args
    merged = dict(flash_args)
    merged["atoc"] = resolve_artefact_path(atoc, build_root, sdk_root, _is_file)
    return merged


def _resolve_flow_d_atoc_via_setools(
    flash_args: Any, shape: FlowDShape, ctx: _Context, entry_id: str, confirm: bool
) -> tuple[Any, str | None]:
    """tan-cli#353's remaining half: when Flow D still has no `atoc`/
    `atoc_address` after the explicit-value and `atoc_map` resolutions above
    (`_resolve_flow_d_atoc_address`/`_resolve_flow_d_atoc_path`), sign one via
    SETOOLS instead of handing `plan_alif_mram_jlink`'s bare "both required"
    refusal to a customer who has never heard of `app-gen-toc`. Measured on
    real silicon (e1m-aen-evk-01, E8 AE822): that refusal is exactly what a
    fresh AEN801 manifest hits today, since alp-sdk's own emit carries only
    `flash_args.jlink_flash_device`.

    `shape` is `validate_flow_d_shape`'s result -- the caller (`_flash_entry`,
    tan-cli#366/#367) validates + resolves it BEFORE this function ever runs,
    so `shape.artefact` is ALREADY the raw `.bin` to hand `app-gen-toc` (the
    same ELF-only sibling resolution `plan_alif_mram_jlink` uses for the
    eventual `loadbin`, from the ONE shared definition,
    `flash_plan.resolve_slot0_binary`) and a manifest that would fail that
    check has already failed BEFORE reaching this function, real run or
    `--dry-run` alike -- this function no longer re-derives or re-checks
    either.

    `confirm` -- the SAME confirm gate `plan_alif_mram_jlink` itself applies
    to the real MRAM write (`ctx.force_confirm` OR `flash_args.confirm`),
    computed by the caller BEFORE this function runs. tan-cli#487, defect 5:
    this function's own real-sign branch used to be gated on `ctx.dry_run`
    ALONE, so a plain `tan flash` on a fresh manifest -- confirm gate not
    armed, but also not `--dry-run` -- spawned `app-gen-toc` for REAL: wrote
    `build/images/<id>.bin` / `build/config/<id>-slot0.json` into the
    customer's SETOOLS install, APPENDED to the install-wide, hand-run-
    inclusive `build/app-package-map.txt`, and overwrote the shared `build/
    AppTocPackage.bin` -- on a run that goes on to hit the confirm gate and
    refuse the MRAM write it was signing FOR. The SETOOLS auto-sign is
    itself a real write and must not run just because the run is not ALSO a
    preview.

    Returns `(flash_args, note)`. `note` is `None` on the one path that never
    touched SETOOLS at all -- an already-resolved no-op (an explicit `atoc`/
    `atoc_map`, or `atoc_address` already present). Every path that DOES touch
    SETOOLS returns a non-`None` `note` naming `setools.path`/`setools.source`
    (tan-cli#373): under `--dry-run` OR an unconfirmed real run it describes
    what WOULD be signed and `flash_args` is left with `atoc`/`atoc_address`
    still absent (signing writes real files into the customer's SETOOLS
    install and spawns a real tool, which neither a preview nor an unarmed
    confirm gate may do); a CONFIRMED real run instead describes what WAS
    just signed, with `flash_args` fully resolved for `plan_alif_mram_jlink`
    to consume. The caller (`_flash_entry`) tells the two apart by `ctx.
    dry_run or not confirm`, which it already has: the preview note is the
    entry's own terminal message, the real-sign note is an EXTRA line ahead
    of the real write's own ok/fail message -- previously `setools.source`
    reached a customer only via a FAILURE (`missing_tool_message`/
    `unresolved_message`), never on a run that succeeded.

    Raises `FlashPlanError` for: SETOOLS unresolved, resolved but not a real
    install, no `flash_args.slot0_load_address` to give `app-gen-toc` as its
    `mramAddress`, or the sign step itself failing -- the caller's existing
    `except FlashPlanError` arm (mirroring `_resolve_flow_d_atoc_address`/
    `_resolve_flow_d_atoc_path` above) reports it as the entry's
    `flash.entry-failed` message.

    **Only when the manifest points at NOTHING signing-related at all.** A
    customer who already supplied an explicit `atoc` (a blob they signed
    themselves) or `atoc_map` (pointing at their own `app-gen-toc` run) gets
    NONE of this -- even if that path did not fully resolve (e.g. the map has
    not materialised yet) `plan_alif_mram_jlink`'s own precise refusal is the
    right one, not a fresh SETOOLS sign silently overriding what they already
    pointed tan at.
    """
    if fa_str(flash_args, "atoc") is not None or fa_str(flash_args, "atoc_map") is not None:
        return flash_args, None
    if fa_str_checked(flash_args, "atoc_address", True) is not None:
        return flash_args, None

    setools = resolve_setools_dir(flash_args, os.environ, ctx.setools_dir)
    if setools is None:
        raise FlashPlanError(unresolved_message())
    app_gen_toc = find_app_gen_toc(setools.path)
    if app_gen_toc is None:
        raise FlashPlanError(missing_tool_message(setools))

    # `mramAddress` -- app-gen-toc's own placement for the app itself, distinct
    # from `atoc_address` (the SIGNED PACKAGE's placement, derived below from
    # its own build report). tan has no source for it besides this already-
    # documented Flow D key (`plan_alif_mram_jlink`'s optional
    # `slot0_load_address`, already extracted into `shape.app_address`) --
    # there is nothing to guess it from, so a manifest that omits it refuses
    # here rather than falling through to a confusing generic message.
    if shape.app_address is None:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: flash_args.slot0_load_address is required to auto-sign "
            "via SETOOLS (it becomes app-gen-toc's mramAddress) -- supply the app's "
            "real MRAM slot0 address, or sign by hand and set flash_args.atoc / "
            "flash_args.atoc_address yourself."
        )

    if ctx.dry_run or not confirm:
        # Planning only -- report what WOULD be signed without touching the
        # customer's SETOOLS install or spawning a real tool. tan-cli#487:
        # `not confirm` closes defect 5 -- a real (non-`--dry-run`) run whose
        # confirm gate is not armed previews exactly like a dry run instead
        # of signing for real and then refusing the write it signed for.
        why = "dry-run" if ctx.dry_run else "flash_args.confirm is false"
        return flash_args, (
            f"would sign {shape.artefact} with SETOOLS at {setools.path} (via "
            f"{setools.source}) -> build/config/{entry_id}-slot0.json, then run "
            f"app-gen-toc -- not run ({why})"
        )

    atoc_path, address = sign_slot0(
        setools.path, app_gen_toc, shape.artefact, entry_id, shape.app_address
    )
    merged = dict(flash_args)
    merged["atoc"] = atoc_path
    merged["atoc_address"] = address
    return merged, (
        f"signed {shape.artefact} with SETOOLS at {setools.path} (via "
        f"{setools.source}) -> {atoc_path} @ {address}"
    )


def _flash_entry(
    target: FlashTarget,
    ctx: _Context,
    *,
    yocto_wic_stat: Callable[[str], os.stat_result] = os.stat,
) -> tuple[int, _Entry, list[str]]:
    """Dispatch + run one target. Returns `(rc, entry, text-lines)`.

    `yocto_wic_stat` is the write-time block-device gate's `stat_fn`
    (`_yocto_wic_block_device_refusal`'s own parameter), threaded through
    here -- not called from anywhere else in this function -- purely so a
    test can reach the REAL dispatch path with an injected mode, exactly the
    way that helper's own direct-call tests already fake `st_mode`, without
    needing a literal `/dev/`-rooted regular file to exist on disk (`/dev/shm`
    is Linux-only tmpfs; neither macOS nor Windows have anywhere writable
    under a path lexically starting with `/dev/`, and `plan_yocto_wic`'s own
    `must start with /dev/` refusal -- oracle-pinned wording, see that
    function -- means the target STRING has to start with `/dev/` regardless
    of host, even though nothing here actually touches a real device). The
    real dispatch default (`os.stat`) is unchanged for every existing caller."""
    kind, entry_id = target.kind, target.id
    lines: list[str] = []

    def entry(method: str | None, status: str, rc: int, message: str) -> _Entry:
        return _Entry(kind=kind, id=entry_id, method=method, status=status, rc=rc, message=message)

    # No flash_method -> silent skip. A helper carrying `update_channel` instead
    # (the AEN cc3501e_otp, programmed over the bridge SPI) gets a clearer reason
    # than the generic one: it was never meant to be a customer flash target at
    # all, not just one whose wiring is unfinished.
    raw_method = target.flash_method or ""
    if not raw_method:
        channel = target.update_channel or ""
        if channel:
            msg = (
                f"flash: {kind} '{entry_id}' is Alp-OTA-updated (update_channel: "
                f"{channel}), not a customer flash target; skipping"
            )
        else:
            msg = f"flash: {kind} '{entry_id}' has no flash_method; skipping"
        lines.append(msg)
        return -1, entry(None, "skipped", -1, msg), lines

    # Flow D by default where the manifest armed it; Flow A otherwise. `method`
    # is what dispatches AND what the envelope reports, so a consumer can see
    # which transport actually ran. See `select_flash_method`.
    # The `or raw_method` tail is unreachable by construction (`raw_method` is
    # non-empty here, so `select_flash_method` cannot answer `None`) and is kept
    # only to keep the type honest without an `assert`, which `-O` strips.
    method = select_flash_method(target) or raw_method
    meta = backend_for(method)
    if meta is None:
        msg = (
            f"flash: {kind} '{entry_id}' uses flash_method '{method}' which has no "
            f"registered backend. Available: {registry_keys_debug()}"
        )
        lines.append(msg)
        return 1, entry(method, "failed", 1, msg), lines

    # A resolved backend with unresolved `flash_args` (the AEN801 cc3501e
    # helper's `mode: TBD, device: TBD`) is the SDK's documented pending
    # sentinel, not a flash failure: one helper whose args are not finalised must
    # never fail the whole run and block the resolved slices. Checked BEFORE
    # artefact resolution and dispatch so it skips cleanly under both `--dry-run`
    # and a real run.
    if flash_args_has_tbd(target.flash_args):
        msg = (
            f"flash: {kind} '{entry_id}' has an unresolved 'TBD' flash_arg (e.g. "
            "mode/device not finalised); skipping"
        )
        lines.append(msg)
        return -1, entry(method, "skipped", -1, msg), lines

    # The SIBLING of the check above, and the one #222 actually reports: an
    # `output_artefact`/`firmware_path` of `TBD` is not `flash_args`, so the
    # guard above never sees it -- and the emptiness guard below never fires,
    # because a `TBD` placeholder is the one thing that is not empty. It
    # therefore used to resolve to `<build_root>/TBD` and reach a real flasher:
    # a J-Link Commander script whose `loadfile` names it, `dd if=` it, `west
    # flash` a build dir derived from it. That is byte-for-byte the alp-sdk
    # `flash/mod.rs:307` sighting (`.filter(|s| !s.is_empty())`), one field over.
    #
    # FAILED, not skipped, and unlike the `flash_args` case above it fails under
    # `--dry-run` too. Three reasons, in order:
    #   * A dry run is the preview a bench trusts before arming a real write --
    #     reporting `ok` for a manifest that cannot possibly flash is the exact
    #     silent-success class this file guards everywhere else.
    #   * `flash_args: TBD` is a helper whose WIRING is unfinished, which must
    #     not block the resolved slices (hence its skip). An artefact of `TBD`
    #     is a target with no image at all -- there is nothing to program, and
    #     `""` in that same field already fails below.
    #   * `skipped` pushes no `issues[]` entry, so the extension would render a
    #     clean flash for a target that was never going to be written.
    # Ordered AFTER the `flash_args` check on purpose: the AEN801 `cc3501e_otp`
    # helper the issue reports carries BOTH, and it must keep skipping cleanly.
    pending = next(
        (v for v in (target.output_artefact, target.firmware_path) if is_pending(v)), None
    )
    if pending is not None:
        field = "output_artefact" if is_pending(target.output_artefact) else "firmware_path"
        msg = (
            f"flash: {kind} '{entry_id}' has {field}: '{pending}' -- the SDK's "
            "unresolved-placeholder sentinel, not a path. Refusing to resolve it "
            f"under the build root and flash '<build_root>/{pending.strip()}'. "
            "Build this target (or fill the field in) first."
        )
        lines.append(msg)
        return 1, entry(method, "failed", 1, msg), lines

    artefact = target.output_artefact or target.firmware_path or ""
    if not artefact:
        if not ctx.dry_run:
            msg = f"flash: {kind} '{entry_id}' has no output_artefact / firmware_path; can't flash."
            lines.append(msg)
            return 1, entry(method, "failed", 1, msg), lines
        artefact = f"<missing-artefact-for-{entry_id}>"
    artefact_path = resolve_artefact_path(artefact, ctx.build_root, ctx.sdk_root, _is_file)

    # tan-cli#289/#59: widen the required-tool gate (and every plan-builder's
    # own tool probe, below) with the resolved workspace venv -- a tool
    # counts as AVAILABLE when it is on PATH **or** provided by the venv,
    # never venv-only, so an explicit different tool the user put on PATH is
    # never treated as MISSING just because this widening exists.
    #
    # This governs only the go/no-go GATE. Which binary actually SPAWNS is a
    # separate, venv-preferring decision made later by
    # `_programs_resolved_in_venv`: a PATH tool IS rewritten to the venv's own
    # copy there whenever the venv provides one, PATH or no PATH -- matching
    # Rust's split between `tool_available` (PATH-or-venv) and
    # `programs_resolved_in_venv` (venv-preferring) at
    # `crates/tan-cli/src/commands/flash/mod.rs:521-546`. The port matches the
    # oracle; do not read the gate's PATH-or-venv rule as also governing argv[0].
    available = functools.partial(_tool_available, venv_bin=ctx.venv_bin)
    gate = tool_gate(
        meta.requires, ctx.dry_run, ctx.skip_missing_tools, kind, entry_id, method,
        available,
    )
    if gate.outcome == SKIP:
        lines.append(gate.message)
        return -1, entry(method, "skipped", -1, gate.message), lines
    if gate.outcome == FAIL:
        lines.append(gate.message)
        return 1, entry(method, "failed", 1, gate.message), lines

    flash_args = target.flash_args
    # Set only on the Flow D SETOOLS-auto-sign path below, and only when THIS
    # run's own sign actually ran -- carried past the `if` block so the
    # eventual success message (tan-cli#373) can name which SETOOLS install
    # signed it, the same way `setools_note is not None` decides the
    # `--dry-run` early return just below.
    setools_note: str | None = None
    if method == FLOW_D_METHOD:
        # FOUR things happen to `flash_args`/its shape before dispatch, in an
        # order tan-cli#366/#367's review fixed: the ATOC address may need
        # resolving from a build artefact rather than arriving on the
        # manifest already (`_resolve_flow_d_atoc_address`; a
        # supplied-but-unusable `atoc_map` raises there rather than silently
        # deferring to `plan_alif_mram_jlink`'s generic refusal, caught here
        # the same way); the ATOC blob path itself is anchored on
        # `build_root`/`sdk_root` (`_resolve_flow_d_atoc_path`) before it can
        # reach the Commander script unresolved; EVERYTHING ELSE about the
        # entry's shape that does NOT depend on `atoc`/`atoc_address` --
        # `jlink_flash_device`, and (when armed) the slot0 artefact's own
        # ELF-sibling-`.bin` resolution -- is validated NOW, via
        # `validate_flow_d_shape`, the SAME function `plan_alif_mram_jlink`
        # itself calls, so there is exactly one definition of "does this
        # artefact resolve" (#367); the DPIDR preflight args are validated
        # now too, for the same plan-time-not-real-write-time reason `tan
        # flash --dry-run` needs any of this validated at all; and only THEN,
        # tan-cli#353's remaining half, does SETOOLS sign one from scratch
        # (`_resolve_flow_d_atoc_via_setools`) when the first two leave
        # `atoc`/`atoc_address` still absent.
        #
        # **#366: this order is the fix.** A malformed/half-armed manifest
        # used to be caught only by `meta.build`/`validate_flow_d_preflight_
        # args` FURTHER DOWN -- both unreachable from the `setools_note`
        # early-return below, so a `--dry-run` whose `SETOOLS_DIR` happened to
        # resolve reported `ok:true` for a manifest that would refuse a real
        # (or SETOOLS-less) run outright. Validating first means that early
        # return can only ever fire for an entry that has already passed
        # everything checkable without `atoc`/`atoc_address`.
        try:
            flash_args = _resolve_flow_d_atoc_address(flash_args, ctx.build_root, ctx.sdk_root)
            flash_args = _resolve_flow_d_atoc_path(flash_args, ctx.build_root, ctx.sdk_root)
            shape = validate_flow_d_shape(flash_args, artefact_path, _is_file)
            validate_flow_d_preflight_args(flash_args)
            # tan-cli#487, defect 5: the SAME confirm gate `plan_alif_mram_
            # jlink` itself applies to the real MRAM write must ALSO cover
            # the SETOOLS auto-sign below -- see `_resolve_flow_d_atoc_via_
            # setools`'s own docstring for why an unconfirmed real run must
            # not sign for real just because it is not ALSO `--dry-run`.
            confirm = ctx.force_confirm or bool(fa_bool_checked(flash_args, "confirm"))
            flash_args, setools_note = _resolve_flow_d_atoc_via_setools(
                flash_args, shape, ctx, entry_id, confirm
            )
        except FlashPlanError as err:
            msg = str(err)
            lines.append(f"flash: {kind} '{entry_id}' -> {method}")
            lines.append(f"  FAIL: {msg}")
            return 1, entry(method, "failed", 1, msg), lines
        if setools_note is not None and (ctx.dry_run or not confirm):
            # Preview only (see the helper's own docstring): nothing was
            # signed, so there is no `atoc`/`atoc_address` to hand
            # `plan_alif_mram_jlink` -- report the preview directly rather
            # than reaching its "both required" refusal over a field this
            # entry was never asked to fill in by hand. A REAL, CONFIRMED
            # sign (the `else` this condition now excludes -- tan-cli#373)
            # leaves `setools_note` set too, but must NOT return here: it
            # falls through to `meta.build` like every other Flow D entry,
            # carrying the note to the eventual ok message below instead.
            #
            # `status`: a clean `--dry-run` preview reports "ok" like every
            # other preview in this file; an unconfirmed REAL run reports
            # "planned" (tan-cli#487) so `flash.confirm-required` fires --
            # I-30's contract that a JSON consumer must be able to tell
            # "nothing was written" from "programmed the device" applies to
            # the SETOOLS half of Flow D exactly as it does to the MRAM
            # write it feeds.
            lines.append(f"flash: {kind} '{entry_id}' -> {method}")
            lines.append(f"  {setools_note}")
            status = "ok" if ctx.dry_run else "planned"
            return 0, entry(method, status, 0, setools_note), lines

    inputs = FlashInputs(
        artefact=artefact_path,
        flash_args=flash_args,
        core_id=entry_id,
        sku=ctx.sku,
        dry_run=ctx.dry_run,
        force_confirm=ctx.force_confirm,
    )
    try:
        plan = meta.build(inputs, available)
    except FlashPlanError as err:
        msg = str(err)
        lines.append(f"flash: {kind} '{entry_id}' -> {method}")
        lines.append(f"  FAIL: {msg}")
        return 1, entry(method, "failed", 1, msg), lines

    lines.append(f"flash: {kind} '{entry_id}' -> {method}")

    if plan.planning_only or ctx.dry_run:
        shown = display_argv(plan)
        if ctx.dry_run:
            # The user explicitly asked for a preview -- nothing was ever going
            # to run. rc 0 / status "ok" (alp_flash's "clean-dry-run").
            msg = f"would run {shown}"
            lines.append(f"  {msg}")
            return 0, entry(method, "ok", 0, msg), lines
        # The BACKEND declined a real write because the confirm gate is not
        # armed. Keep rc 0 -- this IS a clean, non-error outcome -- but give it a
        # distinct status, and `flash` turns it into a warning Issue. Collapsing
        # it back into "ok" is I-30's exact regression: a JSON consumer then
        # cannot tell "nothing was written" from "programmed the device".
        msg = (
            f"would run {shown} -- NOT written: flash_args.confirm is false (set "
            "ALP_FLASH_FORCE=1 or flash_args.confirm: true to actually flash)"
        )
        lines.append(f"  {msg}")
        return 0, entry(method, "planned", 0, msg), lines

    # A real write. Flow D gets its read-only DPIDR preflight FIRST: flashing the
    # wrong attached board is the one unrecoverable mistake here, so the identity
    # is confirmed while the session is still read-only, and a mismatch aborts.
    if method == FLOW_D_METHOD:
        refusal = _flow_d_preflight(inputs, ctx.venv_bin, ctx.workspace)
        if refusal is not None:
            lines.append(f"  FAIL: {refusal}")
            return 1, entry(method, "failed", 1, refusal), lines

    # tan-cli#487, defect 1's second tier -- see `_yocto_wic_block_device_
    # refusal`'s own docstring. Scoped to `yocto_wic`/`yocto_wic_to_sd_or_
    # emmc` only, and to a REAL write only (never a preview): a regular file
    # lexically living under a genuine `/dev/` subtree is not caught by
    # `flash_plan._resolve_dev_root`'s pure lexical check alone.
    #
    # This `stat` and the spawn below it are two separate syscalls, so there
    # is a TOCTOU window between them in principle -- but it is bounded: the
    # only way to turn a refused target into an accepted one inside that
    # window is to replace it with an actual block device (`mknod`), which
    # needs root, the same privilege that already owns everything under
    # `/dev/` in the first place. This gate cannot be raced by an unprivileged
    # process any more than the directory it inspects can be tampered with by
    # one.
    if method in YOCTO_WIC_METHODS:
        wic_target = fa_str(flash_args, "target")
        if wic_target is not None:
            refusal = _yocto_wic_block_device_refusal(wic_target, stat_fn=yocto_wic_stat)
            if refusal is not None:
                lines.append(f"  FAIL: {refusal}")
                return 1, entry(method, "failed", 1, refusal), lines

    outcome = _execute(plan, ctx.capture, ctx.venv_bin, ctx.workspace)
    if outcome.success:
        # tan-cli#373: `setools_note` is set here only when THIS run's own
        # SETOOLS auto-sign actually ran (the dry-run preview above always
        # returns before reaching this line) -- prefixed onto the real
        # write's own `ok_message` so `setools.source` reaches a customer on
        # a SUCCESSFUL sign too, not only via `missing_tool_message`/
        # `unresolved_message` on a failure.
        ok_message = f"{setools_note}; {plan.ok_message}" if setools_note else plan.ok_message
        lines.append(f"  ok: {ok_message}")
        return 0, entry(method, "ok", 0, ok_message), lines
    msg = _execute_message(outcome, method, entry_id)
    lines.append(f"  FAIL: {msg}")
    return 1, entry(method, "failed", 1, msg), lines


def _flow_d_preflight(
    inputs: FlashInputs, venv_bin: Path | None = None, workspace: str | None = None
) -> str | None:
    """Connect read-only with the manifest's ATTACH device profile and confirm
    the SW-DP IDR before any MRAM write. Returns a refusal message, or `None`
    to proceed.

    ABSENT-BY-DEFAULT, on purpose: a manifest that declares BOTH no `expect_dpidr`
    AND no attach-profile `jlink_device` gets no preflight, because tan has no
    hardware knowledge to supply either value and a wrong expected ID would
    refuse every good board. Any other combination -- one present without the
    other, or either present but null/empty -- refuses instead of silently
    dropping the check (see `validate_flow_d_preflight_args`). Both come from
    `flash_args`.

    Capture is forced on regardless of output mode: the whole point is to READ
    the connect banner, and letting it stream would both lose the value and put
    probe output in the transcript ahead of the decision it drives.

    `venv_bin`/`workspace` (tan-cli#289 review): the same run-wide
    venv-bin-dir / west-topdir `_flash_entry` threads into `_execute` for the
    real write. Without these this probe was PATH-only while the tool gate at
    its call site is PATH-or-venv, so a venv-only J-Link host passed the gate
    and then refused HERE with a confusing "no J-Link binary on PATH" -- the
    "Unreachable via `_flash_entry`" comment below is the invariant this
    restores, not just documents.
    """
    try:
        prepared = flow_d_preflight_script(inputs)
    except FlashPlanError as err:
        return str(err)
    if prepared is None:
        return None
    script, expected = prepared
    binary = next((n for n in ("JLinkExe", "JLink") if _tool_available(n, venv_bin)), None)
    if binary is None:
        # Unreachable via `_flash_entry`: the tool gate already required
        # JLinkExe/JLink to be available PATH-or-venv (`_tool_available`,
        # same as the probe above), and kept because the alternative to a
        # refusal here would be proceeding to the WRITE with the identity
        # unconfirmed.
        return f"{FLOW_D_METHOD}: no J-Link binary on PATH or in the workspace venv for the DPIDR preflight."
    resolved = _programs_resolved_in_venv([binary], venv_bin)
    on_path_bin = venv_bin if resolved != [binary] else None
    # No `-ExitOnError`: a failed connect is the SIGNAL being read here, not an
    # error to abort the probe on.
    outcome = _spawn_jlink([resolved[0], "-NoGui", "1", "-CommanderScript"], script, True,
                           _PREFLIGHT_TIMEOUT_S, on_path_bin, workspace)
    banner = f"{outcome.stdout}\n{outcome.stderr}"
    if _hex_in(expected, banner):
        return None
    if not banner.strip():
        return (
            f"{FLOW_D_METHOD}: the read-only DPIDR preflight produced no output "
            f"({outcome.stderr.strip() or 'probe silent'}); refusing to write MRAM "
            "without confirming which board is attached."
        )
    # `expected` is confirmed absent (checked above) -- but "absent" covers two
    # measurably different banners (tan-cli#312): a connect that DID reach a
    # board and reported some OTHER SW-DP ID (a real wrong-board / wiring /
    # probe-selection problem), and a connect that reported no ID at all
    # (measured on the rc3 bench: the probe still re-enumerating a few seconds
    # after a prior `JLinkExe` close -- same probe, same cable, same
    # `jlink_serial`, and nothing wrong with either). Both used to get the
    # SAME wiring-and-jlink_serial sentence, which sent a user re-checking
    # cables that were never the problem.
    #
    # Conservative on purpose: the "no ID at all" message below asserts the
    # wiring is FINE, so it is only used when BOTH signals agree -- no
    # DP-ID-shaped token anywhere in the banner, AND the banner carries
    # SEGGER's own "the probe itself refused" wording. Anything the detector
    # cannot place that confidently keeps the original sentence rather than
    # guessing the wiring is innocent.
    if not _dp_id_reported(banner) and _connect_failed_outright(banner):
        return (
            f"{FLOW_D_METHOD}: the read-only DPIDR preflight's connect reported no "
            f"SW-DP ID at all (expected {expected}) -- refusing to write MRAM to an "
            "unidentified board. This looks like the J-Link probe still "
            "re-enumerating after a previous JLinkExe session closed, not a wiring "
            "or probe-selection problem -- wait a couple of seconds and retry."
        )
    # tan-cli#373 (regression in #369): this point is reached by FOUR distinct
    # banners, and only ONE of them is the cloned-serial mismatch #369 was
    # filed over -- a board DID answer, with a DIFFERENT SW-DP ID than
    # expected. #369's rewrite gave all four this one sentence, silently
    # deleting the original wiring/`jlink_serial` advice for the other three
    # (an unrecognised banner shape, and the two `_CONNECT_FAILED_TARGET_RE`
    # shapes -- an unplugged ribbon's "Cannot connect to target." and a
    # refused `jlink_serial`'s "Cannot connect to J-Link.") even though #369
    # scoped itself explicitly to the wrong-DP-ID case alone.
    if _dp_id_reported(banner):
        # A real board answered but with a DIFFERENT SW-DP ID than expected --
        # the bench mismatch #369 actually measured. A USB serial can be
        # CLONED across two separate physical probes (a real OEM J-Link clone
        # measured sharing one with a GD32 bridge probe on a different board
        # entirely), so `jlink_serial` alone cannot disambiguate them even
        # when set -- `JLinkExe` selects by serial only, with no USB-port
        # selector. The SW-DP ID this preflight already reads IS the true
        # per-silicon discriminator; this remediation says so instead of
        # pointing at the one field that cannot fix this.
        return (
            f"{FLOW_D_METHOD}: expected SW-DP IDR {expected} was not reported on connect "
            "-- refusing to write MRAM to an unidentified board. Check the wiring and "
            "which board is physically attached -- do NOT treat pinning "
            "flash_args.jlink_serial alone as the fix: some OEM J-Link probes share a "
            "CLONED serial across more than one physical unit, so jlink_serial cannot "
            "disambiguate them even when set. The SW-DP ID is the real per-silicon "
            "discriminator -- this preflight already checks it via "
            "flash_args.expect_dpidr; if more than one board is reachable, confirm "
            "which one answered by its own reported ID, not by serial alone."
        )
    # An unrecognised banner, or a genuine TARGET-level connect refusal (no DP
    # ID was even read to compare against the cloned-serial case above) --
    # `jlink_serial` pinning a probe IS the actual fix here when it is unset
    # on a multi-probe host (tan-cli#353), so the original sentence survives.
    return (
        f"{FLOW_D_METHOD}: expected SW-DP IDR {expected} was not reported on connect "
        "-- refusing to write MRAM to an unidentified board. Check the probe "
        "selection (flash_args.jlink_serial) and the wiring. If jlink_serial is "
        "unset the script selects NO probe, which on a host carrying more than "
        "one J-Link cannot connect at all (tan-cli#353)."
    )


def _hex_in(expected: str, haystack: str) -> bool:
    """Whether `expected` appears in `haystack` as a hex value, ignoring case and
    an optional `0x` on EITHER side -- probes print the ID both ways."""
    needle = expected.lower()
    for prefix in ("0x", "0X"):
        if expected.startswith(prefix):
            needle = expected[len(prefix) :].lower()
            break
    return needle in haystack.lower().replace("0x", "")


#: SEGGER's own wording for a successful SWD connect that read AN id, whatever
#: it turned out to be -- "Found SW-DP with ID 0x........" / "DPIDR: 0x........".
#: Matched loosely on purpose: what this distinguishes is "a real board
#: answered with a different identity" from "nothing answered", not the exact
#: firmware/DLL version's phrasing.
_DP_ID_RE = re.compile(r"(?:with\s+ID|DPIDR)\s*:?\s*0x[0-9A-Fa-f]+", re.IGNORECASE)

#: SEGGER's own wording for the PROBE itself refusing the connection outright
#: -- measured verbatim on the rc3 bench run: "Connecting to J-Link ...FAILED:
#: Cannot connect to the probe/programmer." (tan-cli#312). Deliberately NOT a
#: bare `FAILED`/`Cannot connect` match (that was the tan-cli#312 review
#: finding): JLinkExe prints "FAILED" in many contexts, and "Cannot connect"
#: alone also fires on a TARGET-level refusal -- see `_CONNECT_FAILED_TARGET_RE`
#: below -- which is a real wiring/probe-selection problem, not a re-enumerating
#: probe.
_CONNECT_FAILED_RE = re.compile(r"Cannot connect to the probe/programmer", re.IGNORECASE)

#: SEGGER's own wording for a TARGET-level connect refusal -- "Cannot connect
#: to target." (unplugged SWD ribbon, unpowered board) or "Cannot connect to
#: J-Link." (a probe that IS reachable via USB but refuses the requested
#: `jlink_serial`). Both are genuine wiring/probe-selection problems, so their
#: presence forces `_connect_failed_outright` to False even alongside the
#: probe-level phrase above -- asserting "wiring is fine" here would be the
#: false negative tan-cli#312's review flagged (measured against a real
#: unplugged-ribbon and a real unpowered-target banner).
_CONNECT_FAILED_TARGET_RE = re.compile(r"Cannot connect to (?:target|J-Link)\b", re.IGNORECASE)


def _dp_id_reported(banner: str) -> bool:
    """Whether the banner names ANY SW-DP ID -- not whether it matches
    `expected` (the caller already ruled that out via `_hex_in`), only whether
    a connect got far enough to read one at all."""
    return _DP_ID_RE.search(banner) is not None


def _connect_failed_outright(banner: str) -> bool:
    """Whether the banner carries SEGGER's own wording for the PROBE itself
    refusing the connection (still re-enumerating, no board reachable at all),
    as opposed to a TARGET-level refusal -- a real wiring/probe-selection
    problem that must keep the original remediation, not the re-enumeration
    one."""
    if _CONNECT_FAILED_TARGET_RE.search(banner) is not None:
        return False
    return _CONNECT_FAILED_RE.search(banner) is not None


def _is_file(path: str) -> bool:
    """`Path::is_file`, incapable of raising -- it is called on manifest-supplied
    strings, which may hold a NUL byte or overlong component."""
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _yocto_wic_block_device_refusal(
    target: str, stat_fn: Callable[[str], os.stat_result] = os.stat
) -> str | None:
    """tan-cli#487, defect 1's second tier: `flash_plan._resolve_dev_root` is
    PURE (lexical `..`-collapse only), so a real, EXISTING regular file that
    merely lives under a genuine `/dev/` subtree (`/dev/shm/<name>`) passes
    it clean. This runs here, in the IO half, right before the real
    (non-`planning_only`) spawn: `os.stat` (follows a symlink to its real
    target, so `/dev/disk/by-id/...` still passes) must show `stat.S_ISBLK`,
    or refuse.

    Ported from alp-sdk's `_require_block_device` (`scripts/flash_backends/
    yocto_wic.py`, security fix 3aa65cd7 / #1112) with ONE divergence:
    alp-sdk refuses when the target cannot be stat'd at all, including
    "does not exist"; this fails OPEN there instead -- NARROWLY, since
    tan-cli#487's review (finding 1): a blanket fail-open on ENOENT let a
    typo'd `flash_args.target` (`/dev/shm/sdb` for `/dev/sdb`) `dd` a
    multi-GB image into a BRAND-NEW file under a real `/dev/` subtree at
    `ok:true`, and the same hole reached a dangling symlink under `/dev/`
    (its target may resolve outside `/dev/` entirely) and `/dev/x/../sdb`
    through a symlinked `/dev/x` (the kernel resolves that `..` against
    `/dev/x`'s REAL target, not `/dev`, so a lexical-only guard cannot see
    where it actually lands). The fail-open now applies ONLY when `target`'s
    own parent directory is `_DEV_ROOT` itself (`posixpath.dirname`, on the
    ORIGINAL string -- deliberately not lexically `..`-collapsed first, since
    collapsing is exactly what lets the `/dev/x/../sdb` shape masquerade as
    "parent is /dev"): tan's own
    `test_a_real_spawn_diffs_including_the_captured_failure_tail` (a FROZEN
    oracle-parity fixture under `python/tests/parity/`, which may not move)
    spawns a real `dd` against `flash_args.target: /dev/sdb` specifically
    BECAUSE that device does not exist on any host the suite runs on -- its
    subject is `dd`'s own captured failure opening the missing SOURCE
    artefact, not hardware presence -- and `/dev/sdb`'s parent IS `/dev`
    itself, the devtmpfs root, where "does not exist" genuinely means "not
    plugged in". Every other shape now refuses instead: a target that does
    not exist cannot be an EXISTING file this write clobbers either way,
    which is the data-loss case this gate exists for, but a deeper `/dev/`
    subtree or a traversal through a symlink cannot tell "not plugged in
    yet" from a typo or an escape -- and that ambiguity is refused, not
    guessed. A target that DOES exist still `stat`s successfully and still
    refuses on `not stat.S_ISBLK(mode)` below regardless of where it lives.

    `stat_fn` defaults to the real `os.stat`, injected only so a test can
    simulate a block device without monkeypatching the process-wide
    `os.stat` -- which pytest's own internals also call.
    """
    try:
        mode = stat_fn(target).st_mode
    except FileNotFoundError:
        if posixpath.dirname(target) == _DEV_ROOT:
            return None
        return (
            f"yocto_wic: refusing target '{target}' -- does not exist, and its "
            "parent is not /dev itself, so this cannot tell 'device not "
            "plugged in yet' from a typo, a dangling symlink, or a traversal "
            "that resolves outside /dev/ -- refusing rather than letting the "
            "write create a new file there."
        )
    except OSError as err:
        return f"yocto_wic: refusing target '{target}' -- cannot stat it: {err}."
    if not stat.S_ISBLK(mode):
        return (
            f"yocto_wic: refusing target '{target}' -- not a block device. Regular "
            "files are not a supported flash target for this backend."
        )
    return None


# ── the command ─────────────────────────────────────────────────────────────


def _run(
    app_path: str,
    build_root_arg: str | None,
    sdk_root_arg: str | None,
    board_yaml: str | None,
    core: str | None,
    helper: str | None,
    dry_run: bool,
    skip_missing_tools: bool,
    capture: bool,
    cwd: str,
    setools_dir_arg: str | None = None,
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str], SdkInfo | None]:
    """Everything between argument parsing and the envelope. Returns
    `(exit_code, data, issues, text_lines, sdk)`."""
    app_dir = _abs_join(cwd, app_path)
    if build_root_arg is not None:
        build_root = (
            build_root_arg if os.path.isabs(build_root_arg) else _abs_join(cwd, build_root_arg)
        )
    else:
        build_root = _abs_join(app_dir, "build")

    # Anchored on the WORKSPACE root, never on `app_dir` -- see `workspace_root`.
    # `broken_project_pin`/`foreign_global_default_for` no longer travel past
    # `sdk` here -- `flash` resolves the SAME pair again and turns it into
    # issues ONCE, after every return this function can take (tan-cli#464).
    sdk_resolution = _resolve_sdk(sdk_root_arg, cwd)
    resolved_sdk = sdk_resolution.path
    tier = sdk_resolution.tier
    sdk = SdkInfo(resolved_sdk, tier) if resolved_sdk is not None else None
    # tan-cli#487, defect 3 (review finding 4): absolutised ONCE, right after
    # `resolved_sdk` reaches the envelope's `sdk.root` above -- oracle-
    # parity-pinned to report a relative `--sdk-root` LITERALLY (e.g.
    # `"./sdk"`), so `resolved_sdk` itself must stay untouched; `sdk_root_abs`
    # is the absolutised value every OTHER consumer uses instead. The
    # original tan-cli#487 fix absolutised only the artefact-resolution half
    # (`ctx.sdk_root`, further below) and missed `venv_bin_dir`/
    # `west_workspace_dir` just below THIS comment: both take a `sdk_root`
    # and walk `Path(sdk_root).parent` to find the workspace-wide `.venv`
    # (`find_workspace_venv`/`_zephyr_base_venv`), so a relative `--sdk-root
    # ../alp-sdk` produced a RELATIVE `../.venv/bin` -- `prepend_path` then
    # put that literal relative string on the spawned child's PATH, while
    # `_execute` spawns with `cwd=ctx.workspace` (the west topdir), not this
    # process's own cwd. The exact "validate one base, execute against
    # another" split defect 3 reports, one layer up from the artefact-path
    # half already fixed below -- so `sdk_root_abs` is computed HERE, before
    # every consumer that resolves anything under the SDK root, and reused
    # (not recomputed) for `ctx.sdk_root` at the bottom of this function.
    sdk_root_abs = (
        None
        if resolved_sdk is None
        else resolved_sdk
        if os.path.isabs(resolved_sdk)
        else _abs_join(cwd, resolved_sdk)
    )
    if resolved_sdk is None:
        # Faithful to the Python `find_sdk_root() is None` die: `buildRoot` is
        # reported EMPTY on this path, not the value computed above (verified
        # against the oracle).
        return (
            ExitCode.RUNTIME_FAILURE,
            _data(""),
            [Issue("flash.sdk-root-not-found", "error", "Cannot locate alp-sdk root.")],
            ["flash: Cannot locate alp-sdk root."],
            None,
        )

    manifest_path = _abs_join(build_root, "system-manifest.yaml")
    if not _is_file(manifest_path):
        message = (
            f"system-manifest.yaml not found at {manifest_path}; run "
            f"`tan build --project {app_path}` first."
        )
        return _error(build_root, "flash.manifest-not-found", message, sdk)
    try:
        text = _read(manifest_path)
    except OSError as err:
        # Unreadable, a DIRECTORY where a file was expected, a permission
        # denial. Same issue code the oracle uses for a read failure.
        return _error(build_root, "flash.manifest-not-found", f"{manifest_path}: {err}", sdk)
    try:
        manifest = parse_system_manifest(text)
    except ManifestError as err:
        return _error(build_root, "flash.manifest-invalid", f"{manifest_path}: {err}", sdk)

    force_confirm = os.environ.get("ALP_FLASH_FORCE") == "1"
    plan = plan_flash_targets(manifest, core, helper)

    # tan-cli#289/#59/#61: resolved ONCE for the whole run, keyed on the SAME
    # `app_dir` the oracle uses (`venv_bin_dir`/`west_workspace_dir` both walk
    # the filesystem, so doing this per-target would repeat that walk for
    # every slice/helper for no reason). `sdk_root_abs`, not `resolved_sdk`
    # (tan-cli#487 review finding 4) -- see that computation's own comment
    # above for why the relative form breaks this specific pair of callers.
    venv_bin = venv_bin_dir(app_dir, sdk_root_abs)
    workspace_dir = west_workspace_dir(app_dir, Path(sdk_root_abs))
    workspace = str(workspace_dir) if workspace_dir is not None else None

    text_lines: list[str] = []
    # No longer `sdk_resolution_issues(...)` here -- see the note above and
    # `flash`'s own call, the one place that pair is built now.
    issues: list[Issue] = []
    entries: list[dict[str, Any]] = []
    # Seeded with the status-refused slices: they never become a target, so they
    # cannot increment `failed` in the loop -- but a slice `tan build` reports
    # non-`ok` must still fail the overall run, not disappear into a clean exit.
    # `refused_skipped` is deliberately NOT folded in here -- see the loop
    # below and `TargetPlan.refused_skipped`.
    failed = len(plan.refused)
    flashed_anything = False

    for warning in plan.warnings:
        text_lines.append(warning)
        issues.append(Issue("flash.boot-order-unknown-core", "warning", warning))
    for refusal in plan.refused:
        text_lines.append(refusal)
        # error, not warning: the planner refused to select this slice's
        # (possibly stale) artefact for flashing at all, so `ok` must disagree
        # with a green exit code here exactly as a spawned flash failure does.
        issues.append(Issue("flash.slice-not-built", "error", refusal))
    for refusal in plan.refused_skipped:
        text_lines.append(refusal)
        # warning, not error, and NOT counted into `failed` below: `tan build`
        # already decided (via `executionPolicy`) not to build this slice on
        # this host -- e.g. no `bitbake` for a Yocto slice on an MCU-only
        # checkout -- and reported that decision. An MCU customer who never
        # asked for that slice must not see a red `tan flash` over it; the
        # skip stays visible in the envelope instead of being swallowed.
        issues.append(Issue("flash.slice-skipped", "warning", refusal))

    # tan-cli#487, defect 3: `resolved_sdk` itself stays untouched -- it
    # already reached the envelope's `sdk.root` above (`sdk = SdkInfo(
    # resolved_sdk, tier)`), which is oracle-parity-pinned to report a
    # relative `--sdk-root` LITERALLY (e.g. `"./sdk"`), so it must not be
    # absolutised there. `ctx.sdk_root` is a DIFFERENT consumer: its only
    # three readers (`resolve_artefact_path`, `_resolve_flow_d_atoc_address`,
    # `_resolve_flow_d_atoc_path`) join it onto a manifest-relative artefact
    # string and then check/read that path from DISK -- while `_execute`
    # spawns the flasher with `cwd=ctx.workspace` (the west topdir), not this
    # process's own cwd. A relative `sdk_root` there is validated with
    # `os.path.isfile` against ONE base (this process's cwd) and then read by
    # the spawned tool against ANOTHER (the topdir) -- the "validate one file,
    # write another" split the issue reports, common case an ordinary-looking
    # `--sdk-root ../alp-sdk` layout, dangerous case a stale same-named file
    # under the topdir gets flashed instead. `sdk_root_abs` -- computed ONCE,
    # up where `resolved_sdk` is resolved, per review finding 4 -- covers
    # every `_resolve_sdk` tier that can carry a relative path, not just
    # `--sdk-root` (`resolve_sdk_root_ladder_safe`'s `.alp/sdk-path`/
    # `~/.alp/sdk-default` pins are read verbatim too; `sdk_cmd._pointer_
    # target` does not absolutise either), and is reused below for
    # `venv_bin_dir`/`west_workspace_dir` too -- not recomputed here.

    ctx = _Context(
        sku=manifest.sku,
        build_root=build_root,
        sdk_root=sdk_root_abs,
        dry_run=dry_run,
        skip_missing_tools=skip_missing_tools,
        force_confirm=force_confirm,
        capture=capture,
        venv_bin=venv_bin,
        workspace=workspace,
        setools_dir=setools_dir_arg,
    )
    for target in plan.targets:
        rc, entry, lines = _flash_entry(target, ctx)
        text_lines.extend(lines)
        # A failed entry used to land only in `data.entries[].message`; `issues`
        # is the channel `--format json` consumers key error rendering off, so
        # `ok:false` must never ship with an empty issues list.
        if rc > 0:
            issues.append(Issue("flash.entry-failed", "error", entry.message))
        if entry.status == "planned":
            # `status` alone is prose no automated consumer parses.
            issues.append(Issue("flash.confirm-required", "warning", entry.message))
        entries.append(entry.as_dict())
        if rc < 0:
            continue  # silently skipped -- not counted, does not set flashed_anything
        flashed_anything = True
        if rc > 0:
            failed += 1

    if not flashed_anything and not plan.refused and not plan.refused_skipped and plan.targets:
        # tan-cli#487 defect 7: every target this run matched DID reach
        # `_flash_entry`, which correctly skipped each one there (an
        # unresolved `TBD` flash_arg, no flash_method, or a missing tool
        # under `--skip-missing-tools`) -- a real, per-entry diagnosis
        # already sitting in `entries[]` above. `plan.refused`/`plan.
        # refused_skipped` are the planner's OWN skip buckets and an
        # entry-level skip never populates either of them, so this branch
        # used to fall through to `flash.nothing-matched` below -- which
        # contradicts that branch's own reasoning (see its comment): the run
        # had no `--core`/`--helper` filter at all, and something DID match.
        # `ok`/exit code are UNCHANGED here (still SUCCESS, per `test_
        # tbd_sentinel_never_reaches_a_flasher` and its siblings) -- this is
        # a diagnosis fix, not a behaviour change; see each entry's own
        # `message` for why THAT target didn't run.
        message = (
            "flash: every matched target was skipped before dispatch; nothing "
            "was flashed. See entries[] for why each one was skipped."
        )
        text_lines.append(message)
        issues.append(Issue("flash.entries-skipped", "warning", message))
    elif not flashed_anything and not plan.refused and not plan.refused_skipped:
        # `plan.targets` is empty too here (the `if` above already claimed
        # the non-empty case).
        message = "flash: nothing matched the requested filters."
        text_lines.append(message)
        # A `--core`/`--helper` filter matching nothing used to warn only in text
        # mode, so `--format json` reported `ok:true` with empty
        # `entries`/`issues` for a flash that never touched a device.
        issues.append(Issue("flash.nothing-matched", "warning", message))
    elif not flashed_anything and not plan.refused and plan.refused_skipped:
        # `refused_skipped` alone is fine ALONGSIDE at least one real flash (see
        # `TargetPlan.refused_skipped`): the skip was already a policy decision
        # `tan build` made and reported, and `flashed_anything` being True there
        # means the run did something real. But when NOTHING flashed and every
        # match was a skip, exiting 0 here would be the same silent-success bug
        # `refused` fixes above, just inverted: a manifest whose only slice is
        # `status: skipped` (or a `--core`/`--helper` filter naming exactly one)
        # used to report `ok:true`/exit 0 with an empty `entries[]` -- a bench
        # reads that as a completed flash over an unchanged board. `failed` is
        # bumped the same way `refused` seeds it above (one per skipped match)
        # so the count and the exit code both reflect that nothing was
        # programmed; the individual `flash.slice-skipped` warnings above still
        # say WHY each one didn't run.
        failed += len(plan.refused_skipped)
        message = "flash: every matched slice/helper was build-skipped; nothing was flashed."
        text_lines.append(message)
        issues.append(Issue("flash.nothing-flashed", "error", message))
    text_lines.append(f"flash: {failed} failure(s).")

    exit_code = ExitCode.RUNTIME_FAILURE if failed > 0 else ExitCode.SUCCESS
    return exit_code, _data(build_root, entries), issues, text_lines, sdk


def _read(path: str) -> str:
    """`encoding="utf-8"` explicitly (**I-27**): a bare read uses the host's
    locale encoding, so a manifest carrying any non-ASCII byte -- a SoM name, a
    reason string, a `⚠️` -- raises `UnicodeDecodeError` on a cp1252 Windows host
    and parses fine on ubuntu CI. `errors="replace"` on top: a TRUNCATED or
    binary file must become a YAML shape error with a real issue code, never a
    decode traceback."""
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _data(build_root: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": _DATA_SCHEMA_VERSION,
        "buildRoot": build_root,
        "entries": entries if entries is not None else [],
    }


def _error(
    build_root: str, code: str, message: str, sdk: SdkInfo | None
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str], SdkInfo | None]:
    """A manifest-gate refusal: exit 1, an empty `data`, one issue.

    tan-cli#464 review: no longer also takes `sdk_broken_pin`/`tier`/
    `sdk_foreign_default` to rebuild the pin/foreign-default pair itself --
    three same-typed `str | None` positionals a caller could transpose
    unnoticed. `flash` computes that pair once now, after every return."""
    return (
        ExitCode.RUNTIME_FAILURE,
        _data(build_root),
        [Issue(code, "error", message)],
        [f"flash: {message}"],
        sdk,
    )


def flash(
    ctx: typer.Context,
    app_path: str = typer.Argument(
        ".",
        metavar="APP_PATH",
        help="Application source directory (default: the current directory). "
        "`build_root` defaults to <APP_PATH>/build.",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="PATH",
        help="Override the build root holding system-manifest.yaml "
        "(default: <APP_PATH>/build).",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="Flash only the slice with this core_id (skips every other slice AND "
        "all helpers).",
    ),
    helper: str = typer.Option(
        None,
        "--helper",
        metavar="NAME",
        help="Flash only the helper MCU with this name (skips ALL slices and every "
        "other helper).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the flash command each backend WOULD run and return ok without "
        "spawning; also bypasses the required-tool PATH gate.",
    ),
    skip_missing_tools: bool = typer.Option(
        False,
        "--skip-missing-tools",
        help="When a backend's required tools are all absent from PATH, warn + skip "
        "the entry instead of failing it. No effect under --dry-run.",
    ),
    setools_dir: str = typer.Option(
        None,
        "--setools-dir",
        metavar="PATH",
        help="Alif SETOOLS install used to auto-sign a Flow D slot0 ATOC "
        "(license-gated; obtained from Alif, never redistributed by tan). "
        "Precedence: this flag, then the SETOOLS_DIR environment variable, "
        "then flash_args.setools_dir in the manifest (lowest -- and rebuilt "
        "over by the next `tan build`, see docs/setools.md).",
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
) -> None:
    """Program every slice + helper MCU in the project's system manifest."""
    # `--format` is accepted BEFORE the subcommand too (clap makes it
    # `global = true`, so the Rust takes it on either side); the root callback
    # records it and this option overrides it when repeated after the command
    # name. `flash` honours the pre-subcommand position -- and is therefore in
    # `cli._HONOURS_ROOT_FORMAT` -- because refusing it here means a customer's
    # FLASH does not run, on the one command where the fallback (a text-mode run
    # with an empty stdout) would be indistinguishable from a broken device.
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    # Resolved OUTSIDE the guard: `project_obj` is reported on every path
    # including the internal-failure one, and `_resolve_project` is pure string
    # work that cannot raise. The port's most-repeated defect was a helper that
    # throws being called from the exception guard's own recovery path -- so
    # nothing below the guard may compute a field the guard itself needs.
    cwd = workspace_root(project)
    project_obj = _resolve_project(cwd, board_yaml)

    # Resolved OUTSIDE the guard, like `project_obj` above: the same
    # `(sdk_root, cwd)` `_run` resolves for itself, so the pair below covers
    # every one of `_run`'s returns including `flash.internal-failure`
    # (`build_cmd.build`'s own placement, tan-cli#464 review).
    sdk_resolution = _resolve_sdk(sdk_root, cwd)

    sdk: SdkInfo | None = None
    try:
        exit_code, data, issues, text_lines, sdk = _run(
            app_path=app_path,
            build_root_arg=build_root,
            sdk_root_arg=sdk_root,
            board_yaml=board_yaml,
            core=core,
            helper=helper,
            dry_run=dry_run,
            skip_missing_tools=skip_missing_tools,
            capture=json_mode,
            cwd=cwd,
            setools_dir_arg=setools_dir,
        )
    except Exception as err:  # noqa: BLE001 -- the whole point of this guard
        # Anything reaching here is a tan bug, and it is reported AS ONE, with an
        # envelope. A raw traceback means an empty stdout and an extension that
        # renders nothing, with no error visible on either side.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = _data("")
        issues = [Issue("flash.internal-failure", "error", f"{type(err).__name__}: {err}")]
        text_lines = ["flash: internal failure"]

    # The ONE `sdk_resolution_issues` call left in this file (tan-cli#464
    # review) -- covers every return above, guard included.
    resolution_issues = sdk_resolution_issues(
        sdk_resolution.broken_project_pin,
        sdk_resolution.tier,
        sdk_resolution.foreign_global_default_for,
    )
    if resolution_issues:
        issues = [*resolution_issues, *issues]

    if json_mode:
        emit(Envelope("flash", project_obj, data, issues, exit_code, sdk=sdk))
    else:
        for line in text_lines:
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`/`--verbose`) on top of `--board-yaml`, already declared and read
# above; see `tan.core.global_flags`.
flash = accept_global_flags(flash)
