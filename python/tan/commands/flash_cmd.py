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
`TBD` in `flash_args` / a `flash_policy` this run may not invoke, **#611**),
`>0` failed -- including an `output_artefact`/
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

**No spawn in this file ever gets a bare `argv[0]` (tan-cli#567).** The
required-tool gate ([`_tool_available`]) walks `$PATH` by hand specifically so
the current directory cannot supply a probe/programmer -- and then every spawn
below used to hand `subprocess` the bare identity, which on Windows is resolved
by `CreateProcess` (`lpApplicationName=NULL`), whose documented search order
puts *the current directory for the parent process* ahead of `%PATH%`. tan's
cwd for a flash is the customer's project. [`_execute`] now resolves every
program position -- `argv[0]` AND the token after a `"|"` -- through the same
`tan.core.tool_lookup` the build path has used since tan-cli#510, hands the
result to `subprocess` as **`executable=`** (`lpApplicationName` on Windows,
where a non-NULL value means no search happens at all; `execv` on POSIX, which
leaves the child's own `argv[0]` alone so a tool's diagnostics still read
`dd: ...` and not `/usr/bin/dd: ...`), and refuses the entry outright rather
than spawning a name the current directory could satisfy
([`_unresolved_program_outcome`]). The read-only DPIDR preflight gets the same
treatment, for the stronger reason that it is what decides which board the
write is about to go to.
"""
from __future__ import annotations

import codecs
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

#: POSIX-only, and the ONE reason this file has a conditional import
#: (tan-cli#541). `pty`/`termios`/`fcntl` do not exist on Windows at all --
#: `import pty` there raises `ModuleNotFoundError: termios` -- and CPython
#: ships no equivalent: the Windows console-pty primitive is ConPTY
#: (`CreatePseudoConsole`), which the standard library does not expose. So the
#: live-console tee runs a real pty on POSIX (the child keeps its `isatty()`
#: and therefore its progress bar) and falls back to the plain pipes on
#: Windows. That fallback is a STATED difference, not a silent one -- see
#: [`_open_console_pty`] and `_spawn`'s docstring, and tan-cli#541's own
#: acceptance list, which asks for exactly that.
try:  # pragma: no cover -- the except arm is Windows-only
    import fcntl
    import pty
    import struct
    import termios
except ImportError:  # pragma: no cover -- Windows has none of the four
    fcntl = pty = struct = termios = None  # type: ignore[assignment]

import typer

from tan.commands.build_cmd import resolve_sdk_root_ladder
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.core.flash_plan import (
    DPIDR_GUARD_COVERAGE,
    FAIL,
    FLASH_POLICY_RECOVERY_ONLY,
    FLOW_D_METHOD,
    PIPE,
    SKIP,
    SWD_PROBE_METHOD,
    FlashInputs,
    FlashPlan,
    FlashPlanError,
    FlashTarget,
    FlowDShape,
    ManifestError,
    YOCTO_WIC_METHODS,
    _DEV_ROOT,
    CONFIRM_REMEDY,
    backend_for,
    confirm_gate_note,
    display_argv,
    dpidr_preflight_possible,
    dpidr_preflight_unarmed,
    fa_bool_checked,
    fa_str,
    fa_str_checked,
    flash_args_has_tbd,
    flow_d_preflight_script,
    helper_flash_gate,
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
from tan.core.tool_lookup import resolve_program_positions, resolve_tool
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
#:
#: tan-cli#519/#522 review, MINOR 1: NOT the whole wall-clock bound on a
#: live-console `_spawn` call. After the kill (or the child's own exit),
#: `_spawn` still joins its two `_Tee` drain threads -- see `_DRAIN_JOIN_S`'s
#: own comment -- so the real ceiling on a single flash entry is this value
#: PLUS up to `2 * _DRAIN_JOIN_S`, not this value alone.
_FLASH_TIMEOUT_S = 900.0

#: The read-only DPIDR preflight is a connect-and-quit; it must not inherit the
#: write timeout.
_PREFLIGHT_TIMEOUT_S = 60.0

#: Seconds to wait for the pipeline's stderr reader after both children are gone
#: (`_Drain`). Bounded, not indefinite: the reader is what carries the
#: decompressor's diagnosis into the outcome (tan-cli#401), but a `tan` that
#: hangs on a thread rather than reporting a flash result would be the worse
#: trade -- both children have already exited by every path that joins it.
#:
#: `_Tee` (tan-cli#519/#522) reuses this SAME constant for the identical
#: reason, but `_spawn` joins TWO of them -- `out_tee.text()` and
#: `err_tee.text()`, one per stream -- so the real overrun past a `_spawn`
#: caller's own `timeout` is bounded by UP TO `2 * _DRAIN_JOIN_S`, not one
#: `_DRAIN_JOIN_S`: worst case both tees straggle behind a lingering
#: grandchild in sequence. Measured: `timeout=3` -> 4.00s; `timeout=2` ->
#: 6.01s. Still bounded and still the better trade than an unbounded join,
#: but a reader of `_FLASH_TIMEOUT_S` alone would not learn a `tan flash`
#: timeout is really `timeout + up to 2 * _DRAIN_JOIN_S` from this constant
#: on its own -- see `_Tee.join`'s own docstring for where that bound is
#: actually spent.
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
    #: tan-cli#520 REVIEW (design point): set on a CONFIRMED, real write whose
    #: `flash_args.expect_dpidr` was absent -- i.e. the wrong-board preflight
    #: genuinely did not run for this write, not merely that it ran and
    #: passed. `_run` reads this to append its own
    #: `flash.dpidr-preflight-unarmed` warning `Issue` alongside this entry's
    #: `ok` one. NOT part of the envelope contract -- `as_dict()` never emits
    #: it, only `_run`'s own `issues` list reads it.
    #:
    #: tan-cli#609: which methods can set it is `DPIDR_GUARD_COVERAGE`'s
    #: answer (`swd_probe`'s J-Link arm AND Flow D), not `swd_probe`'s alone
    #: as #520 shipped it -- the AEN dispatches Flow D, so this stayed False
    #: for every real MRAM write and the envelope carried no signal at all.
    preflight_unarmed: bool = False
    #: tan-cli#540: set on a CONFIRMED, real, `swd_probe` J-Link write whose
    #: own transcript said the core never halted AND whose Commander script
    #: could carry no `verifybin` -- i.e. the ELF/HEX (`loadfile`) arm, where
    #: the load went into a running core and nothing observed the bytes land.
    #: NOT set on the raw-`.bin` arm, which since tan-cli#540 defect 2 reads
    #: its own write back (`jlink_commander_script`) and is therefore
    #: confirmed even when the halt failed -- only its post-write reset is in
    #: doubt there, and the entry's message says so. `_run` reads this to
    #: append its own `flash.swd-probe-write-unconfirmed` warning `Issue` (and
    #: the matching text line) alongside this entry's `ok` one; the entry's
    #: own `message` is already qualified by `_swd_probe_qualified_message`,
    #: which is what decides this flag. NOT part of the envelope contract --
    #: `as_dict()` never emits it, exactly like `preflight_unarmed` above.
    write_unconfirmed: bool = False
    #: tan-cli#611: set when a `flash_policy: recovery_only` helper was let
    #: through by `--helper <id> --recover`. The write itself is legitimate --
    #: it is the bricked-device path the policy exists to keep reachable -- but
    #: it is also the one write a customer performs on a device that is already
    #: broken, so `_run` appends a `flash.recovery-flash-armed` warning `Issue`
    #: (and the matching text line) saying what was armed. NOT part of the
    #: envelope contract -- `as_dict()` never emits it, like the two above.
    recovery_armed: bool = False

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
    GUI-launched editor's PATH never has it. `tool_lookup.resolve_tool` walks
    `$PATH` by hand rather than using `shutil.which`, which on Windows probes
    the CURRENT DIRECTORY first -- a project checked out with its own
    `openocd.exe` at its root would otherwise be reported as this host's
    tooling and then SPAWNED against attached silicon.

    **This is the GO/NO-GO gate only, and it answers a bool** -- which is
    exactly why tan-cli#567 was possible: the resolved path this walk produces
    was computed and thrown away, and `_execute` then handed the platform the
    bare identity the gate had just protected. The gate stays a bool (its
    consumer, `flash_plan.tool_gate`, asks a yes/no question about a whole
    `requires` list); the SPAWN now does its own resolution through the SAME
    `tool_lookup` module, so the two can no longer disagree about what runs.
    See [`_execute`], which does that resolution.

    tan-cli#532: the walk itself is no longer `doctor_cmd.on_path`'s private
    copy but the shared one -- three of the five hand-rolled implementations
    that issue enumerates now share a single definition."""
    try:
        if resolve_tool(tool, os.environ).resolved is not None:
            return True
    except (OSError, ValueError):
        pass
    return venv_bin is not None and tool_in_venv(venv_bin, tool) is not None


# ── spawning ────────────────────────────────────────────────────────────────


def _open_console_pty(sink):
    """A pty for the live-console tee, as `(master_reader, slave_fd)` -- or
    `None` to keep the plain `subprocess.PIPE` pair (tan-cli#541).

    **Why a pty at all.** `_Tee` gave the child `subprocess.PIPE` for both
    streams so the transcript could be read (tan-cli#522 needs it in DEFAULT
    text mode). A pipe is not a terminal, so the CHILD's own
    `stdout.isatty()`/`stderr.isatty()` flipped `True` -> `False`. Measured on
    a real pty, same invocation: `origin/dev  child sees stdout_isatty=True
    stderr_isatty=True`; with the pipe tee, `stdout_isatty=False
    stderr_isatty=False`. `pyocd flash`, `west flash` and `openocd` all gate
    their `\\r`-redrawn progress bar and their colour on `isatty()`, so a
    bench operator watching a multi-minute GD32G553 or Alif MRAM write lost
    the live progress indicator the previous release showed -- and a write
    that shows no progress reads as hung, which is the operator-perception
    problem tan-cli#388/#522 are about, arriving from a third direction. A
    pty is a terminal AND readable, so it buys the transcript back without
    costing the child its tty.

    `None` -- i.e. keep today's pipes -- in three cases, each deliberate:

    * `sink` is not a terminal. tan piped to a file, or under CI, is the case
      where the pipe path is already RIGHT: the operator has no terminal to
      redraw on, the tool's non-interactive rendering is what should land in
      the log, and handing the child a pty there would inject escape codes
      and `\\r` runs into a file nobody watches. tan-cli#541's own acceptance
      asks for this explicitly ("behaviour when tan is not attached to a
      terminal is unchanged from today").
    * Windows, where `pty`/`termios`/`fcntl` do not exist -- see the guarded
      import at the top of this module for why there is no equivalent to
      reach for, and note that this is a STATED platform difference: a
      Windows operator keeps the pipe behaviour, progress bar included in
      whatever form the tool picks for a pipe.
    * `pty.openpty()` itself failing (a container with no `/dev/ptmx`, the
      host out of pty slaves). A flash must still run there; falling back to
      the pipes costs the progress bar, not the write.

    The caller owns both fds: it must `os.close(slave_fd)` once the child has
    inherited it (otherwise the parent's own copy holds the write end open and
    the reader never sees EOF), and the reader is closed by [`_Tee`] reaching
    EOF plus process teardown, exactly as for a pipe."""
    if pty is None:
        return None
    try:
        if not sink.isatty():
            return None
    except (OSError, ValueError, AttributeError):
        return None
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return None
    _shape_console_pty(master_fd, slave_fd, sink)
    return os.fdopen(master_fd, "rb"), slave_fd


def _shape_console_pty(master_fd: int, slave_fd: int, sink) -> None:
    """Make the new pty behave like the terminal it is standing in for: no
    `\\n` -> `\\r\\n` translation on the way out, no echo, and tan's OWN
    window size (tan-cli#541).

    `ONLCR` off because this pty's output is not going to a terminal directly
    -- it is read by [`_Tee`] and written through `sys.stderr` to the REAL
    terminal, whose own line discipline supplies the carriage return. Left on,
    every `\\n` the child wrote would reach the captured transcript as
    `\\r\\n`, and `str.splitlines()` (`_capture_tail`, and every consumer of
    `outcome.stdout`) would then be reading a transcript that differs from the
    pipe path's for no reason a caller can see.

    `TIOCSWINSZ` because a tool that renders a progress bar asks the terminal
    how wide it is; a fresh pty answers 0x0, and a bar sized to zero columns
    is worse than no bar. Copied from `sink`'s own size so the child draws to
    the width the operator is actually looking at.

    Every step is best-effort: none of them is worth failing a flash over, and
    a pty that could not be shaped still delivers the `isatty()` this function
    exists for."""
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[1] &= ~termios.ONLCR  # oflag
        attrs[3] &= ~termios.ECHO  # lflag
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except (OSError, ValueError, termios.error):
        pass
    try:
        size = os.get_terminal_size(sink.fileno())
        fcntl.ioctl(
            master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0)
        )
    except (OSError, ValueError, AttributeError):
        pass


def _close_console_pty(reader, slave_fd: int | None) -> None:
    """Drop both ends of a pty the spawn never got to use (the `Popen` raised).
    Best-effort on both, so a failure closing one still closes the other."""
    if reader is not None:
        try:
            reader.close()
        except (OSError, ValueError):
            pass
    if slave_fd is not None:
        try:
            os.close(slave_fd)
        except OSError:
            pass


def _tee_text(tee) -> str:
    """A tee's transcript, or `""` for the stream a pty run does not have.

    A pty is ONE device: the child's stdout and stderr are the same fd, so the
    live-console branch builds a single [`_Tee`] there and leaves the stderr
    one `None` (tan-cli#541). Everything the child said lands in
    `_Outcome.stdout`; `.stderr` stays empty. Both downstream consumers
    already read it that way -- `_capture_tail` falls back to `stdout` when
    `stderr` is blank, and `_flow_d_reset_qualified_message` concatenates the
    two before searching -- which is why the merge needs no consumer change,
    only this note saying which field a pty run fills."""
    return tee.text() if tee is not None else ""


def _spawn(
    argv,
    capture: bool,
    timeout: float,
    venv_bin: Path | None = None,
    workspace: str | None = None,
    executable: str | None = None,
) -> _Outcome:
    """One process. Captured in JSON mode (the output is kept for the failure
    message and never re-spawned), TEED to stderr in text mode -- streamed live
    to the console AND collected, so a transcript-dependent qualification
    (`_flow_d_reset_qualified_message`, tan-cli#522) has the same evidence to
    read regardless of `--format`.

    In text mode the child's stdout is redirected to **stderr**, not inherited:
    stdout is the envelope channel for this process even when this run is not
    using it, and a flash tool that prints to stdout would otherwise put
    non-envelope bytes there. Rust can inherit safely because its text path
    never writes an envelope at all; here the same process object owns both.

    tan-cli#522 review, MAJOR 1: `outcome.stdout`/`.stderr` used to be empty
    in EVERY text-mode branch of this function -- the wrapped-console branch
    (`sink is None`) captured the full transcript via `capture_output=True`
    and then only ever `print()`-replayed it, throwing the strings away
    before they reached the `_Outcome`; the live-console branch (`sink` real)
    handed the child's stdout fd straight to `subprocess.run(stdout=sink)`,
    an OS-level redirect this process never saw a byte of. Both are fixed
    below: the wrapped-console branch now threads its already-captured
    `proc.stdout`/`.stderr` through; the live-console branch replaces the OS-
    level redirect with [`_Tee`] on a `Popen`, which forwards each chunk to
    the console as it arrives (the same "streams live" guarantee this
    function always promised) while also accumulating it. (tan-cli#519/#522
    review round 3: the FIRST version of `_Tee`, shipped in the round this
    docstring was written, did not actually keep that promise -- it read the
    child's TEXT-mode stream in fixed 4096-*character* chunks, which blocks
    until that many characters have been decoded or EOF, so a slowly
    dribbling child produced no console output for over a second at a time,
    measured. `_Tee` now reads the raw binary pipe with `read1`, which
    returns as soon as the OS has ANY bytes ready, and decodes them itself
    -- see [`_Tee`]'s own docstring.)

    tan-cli#541: on POSIX, when `sink` is a REAL terminal, that live-console
    branch now tees through a **pty** rather than a pipe. The pipe tee cost
    the child its own `isatty()`, and `pyocd`/`west`/`openocd` all gate their
    `\\r`-redrawn progress bar on exactly that -- so a bench operator watching
    a multi-minute write lost the progress indicator the previous release
    showed. Three consequences a caller should know: the child's stdout and
    stderr are the SAME device there, so the whole transcript lands in
    `_Outcome.stdout` and `.stderr` stays `""` (see [`_tee_text`] -- both
    downstream consumers already read it that way); the transcript now
    contains whatever `\\r` and ANSI the tool chose to draw for a terminal
    (harmless -- `--format json` never reaches this branch, since
    `capture=json_mode`); and on Windows, on a non-terminal `sink` (tan
    piped to a file, or CI), or on a host that cannot allocate a pty, the
    pipes are kept UNCHANGED. See [`_open_console_pty`] for each case.

    `venv_bin` (tan-cli#289/#59), when given, is prepended onto the child's
    PATH -- `env=None` (the default, passed through unchanged) means
    "inherit this process's own environment", exactly the pre-#59 behaviour.
    That inheritance is also what carries `TERM` (and `COLUMNS`/`LINES`, when
    the operator exports them) to a child now spawned onto a pty -- tan never
    synthesises a `TERM`, so a child sees the operator's own or none at all,
    the same value it saw under fd inheritance before the tee existed.
    `workspace` (tan-cli#289/#61), when given, becomes the child's cwd, so
    `west flash` can see alp-sdk's out-of-tree runners.
    """
    env = _child_env(venv_bin)
    try:
        if capture:
            proc = subprocess.run(
                list(argv),
                executable=executable,
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
                executable=executable,
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
            return _Outcome(
                success=proc.returncode == 0,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                returncode=proc.returncode,
            )
        # tan-cli#541: a PTY when `sink` is a real terminal, so the child
        # keeps its own `isatty()` (and therefore its progress bar and its
        # colour) while this process still reads the transcript tan-cli#522
        # needs. `None` -- Windows, a non-terminal `sink` (piped/CI), or a
        # host that could not allocate one -- keeps the pipes below exactly
        # as they are today. See `_open_console_pty` for each case.
        console_pty = _open_console_pty(sink)
        reader, slave_fd = console_pty if console_pty is not None else (None, None)
        try:
            # BINARY, not `text=True` (tan-cli#519/#522 review round 3,
            # MAJOR 1): `_Tee` reads and decodes the raw pipe itself, in
            # chunks bounded by BYTES ready, not characters decoded -- see
            # its docstring for why a `text=True` stream defeated the whole
            # "live" point of this branch. Unchanged by the pty: `slave_fd`
            # is a raw fd, which is binary by construction.
            #
            # stdin is NOT redirected either way. The child inherits tan's
            # own, exactly as before this branch existed -- a pty for stdin
            # would be a second behaviour change (a tool prompting on it
            # would read EOF from a pty nobody writes to) and tan-cli#541
            # asks only about the OUTPUT streams.
            proc = subprocess.Popen(  # noqa: S603 -- argv comes from the pure planner
                list(argv),
                executable=executable,
                stdout=slave_fd if slave_fd is not None else subprocess.PIPE,
                stderr=slave_fd if slave_fd is not None else subprocess.PIPE,
                env=env,
                cwd=workspace,
            )
        except OSError as err:
            _close_console_pty(reader, slave_fd)
            return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)
        if slave_fd is not None:
            # The child has its own dup now. This copy MUST go, or the pty's
            # write end never closes, the reader never sees EOF, and the tee
            # spends its full `_DRAIN_JOIN_S` on every single spawn.
            os.close(slave_fd)
        out_tee = _Tee(reader if reader is not None else proc.stdout, sink)
        # One device, one tee (see `_tee_text`): a pty conflates the child's
        # two streams, so there is no second stream to read here.
        err_tee = None if reader is not None else _Tee(proc.stderr, sink)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            # `Popen.wait`'s own `TimeoutExpired` carries no output (unlike
            # `subprocess.run(capture_output=True)`'s) -- built by hand here so
            # the shared handler below sees the same shape either way.
            raise subprocess.TimeoutExpired(
                list(argv), timeout, output=_tee_text(out_tee), stderr=_tee_text(err_tee)
            ) from None
        return _Outcome(
            success=proc.returncode == 0,
            stdout=_tee_text(out_tee),
            stderr=_tee_text(err_tee),
            returncode=proc.returncode,
        )
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
    card. The third spawn variant (live console, [`_Tee`]-backed) used to have
    no output here either -- `Popen.wait`'s own `TimeoutExpired` carries none
    -- but `_spawn` now builds one BY HAND from what the tees had already
    collected before the kill (tan-cli#522 review, MAJOR 1), so this handler
    sees the same shape on all three spawn variants and needs no branch of
    its own to tell them apart.

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

    NOT the only divergence this file carried from the Rust oracle: before
    the oracle was retired (`crates/` deleted in 2883cdf), `plan_flash_targets`
    (`tan.core.flash_plan.TargetPlan.refused_skipped`) already treated a
    `status: skipped` slice/helper, or one declared `os: "off"` in
    `board.yaml` (tan-cli#699), as a warning that alone never fails the run,
    where the Rust `plan_flash_targets` had no such bucket and refused (and
    failed) either shape exactly like any other non-`ok` status. See
    `TargetPlan.refused_skipped` for the
    reasoning; there is no longer a second implementation to diff against.
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
    with nothing following. (tan-cli#519/#522 review, MAJOR 1: this pipeline
    case is the ONE ordinary text-mode failure shape that still, genuinely,
    leaves `outcome.stderr` empty -- `_spawn`'s single-tool branches no
    longer do, now that they [`_Tee`]/capture-and-replay the child's
    transcript in every mode; see `_execute_message`'s own docstring, which
    used to make that claim unconditionally.)"""
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


class _Tee:
    """One BINARY `Popen` child stream (stdout or stderr), read to EOF on a
    background thread -- forwarding every chunk to `sink` AS it arrives (so a
    live console still sees the child's output as it happens, unchanged from
    before this class existed) while also accumulating it, so the caller has
    the same transcript a `--format json` capture would have produced
    (tan-cli#522 review, MAJOR 1: `_flow_d_reset_qualified_message` needs a
    transcript to qualify Flow D's reset claim against, and text mode's own
    `_spawn` branch had none at all).

    `stream.read1(n)` on the raw pipe, not `TextIOWrapper.read(n)` (this
    class's OWN first version, and not [`_Drain`]'s single blocking `.read()`
    either): `read1` returns as soon as the OS has ANY bytes ready, up to `n`.
    A bounded read on a *text*-mode stream does NOT have that property -- it
    blocks until it has decoded `n` CHARACTERS or hit EOF, silently reading
    and buffering as many underlying chunks as that takes (tan-cli#519/#522
    review round 3, MAJOR 1: measured against a child dribbling one line
    every 20ms, `TextIOWrapper.read(4096)` delivered nothing to the console
    for over a second at a time -- the exact "capture, then replay after the
    child exits" shape this class exists to avoid on the live-console path
    (the `sink is None` branch of `_spawn` already does that replay
    deliberately; this is the OTHER branch, where a real console is present
    and can show output as it is produced)). `Popen` is spawned WITHOUT
    `text=True` for this branch specifically so this class can read the raw
    bytes and decode them itself, incrementally
    (`codecs.getincrementaldecoder`), so a multi-byte UTF-8 sequence split
    across two `read1` calls is never corrupted or double-counted.

    Two independent threads (one per stream, both writing to the same `sink`)
    means the console's stdout/stderr interleaving is no longer OS-arbitrated
    the way direct fd inheritance was -- each thread's own chunks stay in
    order with themselves, but the two streams may interleave differently
    than before. Both still land on the SAME console, visibly, in real time.

    That is the ONE console-visible change this class was believed to make.
    There was a SECOND, tan-cli#519/#522 review round 3 found only after
    measuring on a real pty: `subprocess.PIPE` means the CHILD's own
    `stdout.isatty()`/`stderr.isatty()` reported `False`, where direct fd
    inheritance (the pre-`_Tee` behaviour) let them report `True` on a real
    terminal. Measured: same invocation, real pty --
    `origin/dev  child sees  stdout_isatty=True   stderr_isatty=True`;
    `pipe tee   child sees  stdout_isatty=False  stderr_isatty=False`.
    `pyocd flash` / `west flash` / `openocd` gate their own `\r`-updated
    progress bar and colour output on `isatty()`, so on a real terminal an
    operator lost the live progress indicator for a multi-minute GD32/Alif
    write -- console output was still complete and still live line-by-line,
    but the CHILD renders it differently once it can no longer see a tty of
    its own, and a write that shows no progress reads as hung.

    **FIXED, tan-cli#541.** This class is unchanged -- it still reads one
    binary stream -- but `_spawn` no longer always hands it a pipe: on POSIX,
    when the console `sink` is a real terminal, it hands it the MASTER end of
    a `pty.openpty()` pair whose SLAVE is the child's stdout and stderr, so
    the child sees a tty again and keeps its own rendering. See
    [`_open_console_pty`] for the three cases that still get a pipe (Windows,
    a non-terminal `sink`, a host that cannot allocate a pty) and [`_tee_text`]
    for the one shape change that follows: a pty is one device, so a pty run
    builds ONE tee, not two, and the whole transcript lands in
    `_Outcome.stdout`.

    `join`'s default timeout is `_DRAIN_JOIN_S`, the SAME bound [`_Drain`]
    uses and for the identical reason (tan-cli#519/#522 review round 3,
    BLOCKER): a grandchild the killed child leaves behind (a backgrounded
    `sleep 20 &` inside a shell script `_spawn` ran) still holds this pipe's
    write end open after `proc.kill()`/`proc.wait()` return -- `kill()` reaps
    only the direct child -- so the read loop below never sees EOF until that
    OTHER process exits too. An unbounded `join()` here is what made
    `_FLASH_TIMEOUT_S` toothless: `proc.wait(timeout=...)` returned on
    schedule but the subsequent `.text()` call blocked past it, measured
    hanging past an outer kill entirely on the TimeoutExpired path. A `tan`
    that hangs on a thread rather than reporting a flash result would be the
    worse trade -- the exact sentence `_Drain.join`'s own docstring already
    uses for this same class of problem.

    tan-cli#519/#522 review, MINOR 1: `_DRAIN_JOIN_S` alone documents ONE
    stream's bound. `_spawn` calls [`text`] on TWO `_Tee`s (stdout's and
    stderr's), one after the other, so the real overrun past `_spawn`'s own
    `timeout` is up to `2 * _DRAIN_JOIN_S`, not one -- a straggling
    grandchild can make BOTH joins spend their full bound in sequence.
    Measured: `_spawn(..., timeout=3)` -> 4.00s; `timeout=2` -> 6.01s.
    Bounded and still the right trade, but see `_DRAIN_JOIN_S`'s own comment
    for why that bound alone understates it by 2x."""

    def __init__(self, stream, sink) -> None:
        self._chunks: list[str] = []
        self._stream = stream
        self._sink = sink
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            for raw in iter(lambda: self._stream.read1(65536), b""):
                chunk = self._decoder.decode(raw)
                if chunk:
                    self._chunks.append(chunk)
                    self._write(chunk)
        except (OSError, ValueError):
            # A PIPE signals EOF with `b""` and never lands here; a PTY
            # MASTER raises `OSError(EIO)` instead, once the last holder of
            # the slave side closes it (tan-cli#541). That is this loop's
            # normal, expected end on the pty path, not an error -- which is
            # why the final decoder flush moved into the `finally` below: left
            # inside the `try`, an EIO would have skipped it and silently
            # dropped a multi-byte character straddling the very last read.
            pass
        finally:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._chunks.append(tail)
                self._write(tail)

    def _write(self, chunk: str) -> None:
        try:
            self._sink.write(chunk)
            self._sink.flush()
        except UnicodeEncodeError:
            # The DECODED chunk is a well-formed `str` (the incremental UTF-8
            # decoder above already replaced anything malformed); this is the
            # SINK's own narrower encoding rejecting a character it can
            # represent, not a decode failure. Re-encoded with a lossy
            # fallback instead of losing the whole chunk (tan-cli#519/#522
            # review round 3, MINOR): the bare `except (OSError, ValueError):
            # pass` below used to catch this too -- `UnicodeEncodeError` is a
            # `ValueError` subclass -- discarding the WHOLE 4KiB chunk from
            # the console, including whatever clean ASCII lines shared it.
            try:
                encoding = getattr(self._sink, "encoding", None) or "utf-8"
                self._sink.write(chunk.encode(encoding, "backslashreplace").decode(encoding))
                self._sink.flush()
            except (OSError, ValueError):
                pass
        except (OSError, ValueError):
            # The console went away mid-write (a closed pipe, a torn-
            # down pytest capture). Keep reading and accumulating --
            # the transcript this thread is building still matters
            # even when nobody can watch it live any more.
            pass

    def join(self, timeout: float | None = _DRAIN_JOIN_S) -> None:
        self._thread.join(timeout=timeout)

    def text(self) -> str:
        """Everything this stream said, joined. Joins first ([`_Drain.text`]'s
        own reasoning): reading `_chunks` while the thread may still be
        appending is exactly the read that could observe a partial list.
        Bounded (see `join`'s own docstring): a straggling grandchild still
        holding this pipe open means `_chunks` may be incomplete when this
        returns -- the same tradeoff `_Drain.text` already makes, for the
        same reason."""
        self.join()
        return "".join(self._chunks)


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
    left_executable: str | None = None,
    right_executable: str | None = None,
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
    env = _child_env(venv_bin)
    deadline = time.monotonic() + timeout
    try:
        first = subprocess.Popen(  # noqa: S603 -- argv comes from the pure planner
            list(left),
            executable=left_executable,
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
                executable=right_executable,
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
    executable: str | None = None,
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
        return _spawn([*argv, path], capture, timeout, venv_bin, workspace, executable)
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


def _child_env(venv_bin: Path | None) -> dict[str, str] | None:
    """The `env=` every spawn in this module hands its child: the venv's bin dir
    prepended onto PATH when one is in play, else `None` -- which `subprocess`
    documents as "inherit this process's environment", and which is exactly the
    pre-tan-cli#59 behaviour.

    One definition, called by [`_spawn`], [`_spawn_pipeline`] and
    [`_resolution_env`], rather than the same expression written out three
    times: tan-cli#567's whole shape is a lookup and a spawn that drifted into
    disagreeing, and the env is the other half of what they must agree on."""
    if venv_bin is None:
        return None
    return prepend_path(dict(os.environ), venv_bin)


def _resolution_env(venv_bin: Path | None) -> dict[str, str]:
    """The environment a child spawned with this `venv_bin` will actually see
    -- the ONE thing `argv[0]` may be resolved against (tan-cli#567/#510).

    [`_child_env`], with its "inherit" `None` spelled out as the environment
    that inheriting actually yields, because a lookup needs a real mapping.
    Resolving against a different PATH than the child gets is precisely how
    tan-cli#510's MAJOR 2 let an approved tool and a spawned tool be two
    different files."""
    return _child_env(venv_bin) or dict(os.environ)


def _unresolved_program_outcome(program: str, venv_bin: Path | None, capture: bool) -> _Outcome:
    """The refusal for a plan whose program is on neither PATH nor the venv.

    **Refused, never spawned bare (tan-cli#567).** "Not on PATH and not in the
    workspace venv" leaves exactly one place `CreateProcess` could still find
    something by that name: the current directory -- the customer's project.
    Handing the OS the bare name at that point is not "letting the OS report a
    clean not-found", it is asking the project directory to supply the binary
    that writes to the board. So this becomes a failed entry instead, with the
    searched PATH named the same way `tool_lookup.resolve_tool`'s `missingTool`
    refusal names it (tan-cli#510's acceptance: a refusal a customer can act on
    without a support ticket).

    On POSIX this replaces a message, not an outcome: `execvp` never searched
    the current directory, so the same plan already failed here -- with
    `could not spawn: [Errno 2] No such file or directory: 'dd'`. The `could
    not spawn:` prefix is kept so both read the same way in
    `data.entries[].message`.

    **Defence in depth, not a hole being plugged** -- correcting this fix's own
    first telling, that the `gunzip`/`xz`/`dd` halves of a `.wic.gz` pipeline
    reach the spawn "with no tool gate in front of them at all". False in both
    halves, measured: `dd` IS declared (`_REGISTRY["yocto_wic"].requires ==
    ('bmaptool', 'dd')`, same for `yocto_wic_to_sd_or_emmc`) so `tool_gate`
    covers it, and `plan_yocto_wic` `which`-checks `gunzip`/`gzip`/`xz` itself
    through the callable this module hands it ([`_tool_available`], the same
    walk) -- `plan_yocto_wic(inp, which=lambda t: t in ("dd",))` raises
    `FlashPlanError` and never returns a plan for this function to see.

    What is left is the window between plan time (gate, planner) and spawn time
    (here): a tool removed, a `PATH` rewritten or a venv torn down in between --
    as would a backend naming a program its `requires` never declares and its
    planner never `which`-es (`xspi_flashwriter` already: `requires=()`, argv[0]
    `flash-writer-scif`, unreachable only because its confirmed arm raises).
    `--skip-missing-tools` is about the declared list, not about this."""
    searched = resolve_tool(program, _resolution_env(venv_bin)).searched
    venv_note = (
        " and the workspace venv does not provide it"
        if venv_bin is not None
        else " (no workspace venv resolved for this run)"
    )
    return _Outcome(
        success=False,
        stderr=(
            f"could not spawn: `{program}` was not found -- searched {searched}"
            f"{venv_note}. Refusing to hand the bare name to the OS: on Windows "
            "that lets the current directory supply the binary (tan-cli#567)."
        ),
        captured=capture,
    )


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

    **tan-cli#567: every remaining PROGRAM position is then pinned to an
    absolute path via `executable=`, and a plan whose program does not resolve
    is REFUSED rather than spawned.** The venv rewrite above only ever covered
    tools the venv itself provides, so `west`, a host-PATH
    `openocd`/`pyocd`/`JLinkExe`, and both halves of the `gunzip | dd` image
    pipeline all reached `subprocess.run`/`Popen` as bare names -- and a bare
    `argv[0]` on Windows is resolved by `CreateProcess`
    (`lpApplicationName=NULL`), whose documented search order consults *the
    current directory for the parent process* BEFORE `%PATH%`. tan's cwd for a
    flash is the customer's project. So a project carrying its own
    `openocd.exe`/`dd.exe` at its root got that binary spawned against attached
    silicon, having passed a tool gate that had carefully looked at `%PATH%`
    and nowhere else. #510 closed exactly this on the build path; this is the
    write path, where the consequence is a wrong image on a customer's board.

    **`executable=`, NOT an `argv[0]` rewrite -- and the oracle is why.** #510
    fixed the build spawn by replacing `argv[0]` with the resolved path
    (`[resolved_tool, *spawn_args]`). Doing the same here is measurably wrong:
    a spawned tool prints ITS OWN `argv[0]` in its diagnostics, so
    `test_a_real_spawn_diffs_including_the_captured_failure_tail` went red with
    `data.entries[].message` reading `yocto_wic[c1]: /usr/bin/dd: failed to
    open ...` where the frozen oracle envelope says `dd: failed to open ...`.
    That is a customer-visible envelope regression AND an absolute-host-path
    leak into a message the VS Code extension renders. `executable=` is the
    mechanism that was wanted all along: on POSIX it is `execv(resolved,
    args)`, so the child keeps the `argv[0]` the plan named; on Windows it
    fills `lpApplicationName`, and a non-NULL `lpApplicationName` means
    `CreateProcess` performs NO SEARCH AT ALL -- strictly stronger than handing
    it a resolved name it would still have parsed. `args` therefore stays
    byte-identical to what the oracle spawns, and `_program_label`'s basename
    for a pipeline half is unchanged.

    The ORDER matters and is not interchangeable: `on_path_bin` is decided by
    the VENV rewrite alone, before the PATH resolution runs. Deciding it after
    would prepend the venv to the child's PATH whenever ANY program resolved to
    an absolute path -- i.e. almost always -- which is the "must not have its
    PATH silently rewritten for no reason" rule above, inverted. And the PATH
    resolution is done against the env the child will ACTUALLY get
    ([`_resolution_env`]), never `os.environ` when those differ: resolving
    against one environment and spawning into another is tan-cli#510's own
    MAJOR 2, and it would reintroduce the same check/spawn disagreement one
    call up.
    """
    argv = list(plan.argv)
    # `spawned` is what the child's own `argv` will be -- the oracle's argv,
    # venv rewrite included. `resolved` is the same list with each PROGRAM
    # position replaced by its absolute location; only its program entries are
    # ever read, and only as `executable=`.
    spawned = _programs_resolved_in_venv(argv, venv_bin)
    on_path_bin = venv_bin if spawned != argv else None
    resolved, unresolved = resolve_program_positions(
        spawned, _resolution_env(on_path_bin), PIPE
    )
    if unresolved is not None:
        return _unresolved_program_outcome(unresolved, on_path_bin, capture)
    if PIPE in spawned:
        cut = spawned.index(PIPE)
        return _spawn_pipeline(
            spawned[:cut],
            spawned[cut + 1 :],
            capture,
            _FLASH_TIMEOUT_S,
            on_path_bin,
            workspace,
            resolved[0],
            resolved[cut + 1],
        )
    if plan.jlink_script is not None:
        return _spawn_jlink(
            spawned, plan.jlink_script, capture, _FLASH_TIMEOUT_S, on_path_bin, workspace,
            resolved[0],
        )
    return _spawn(spawned, capture, _FLASH_TIMEOUT_S, on_path_bin, workspace, resolved[0])


#: Terminal control sequences a tool emits ONLY because it can see a tty:
#: CSI (`\x1b[31m` colour, `\x1b[K` erase-line, `\x1b[1G` cursor-move,
#: `\x1b[?25l` hide-cursor), OSC (`\x1b]0;title\x07`, terminated by BEL or
#: ST), and the two-character escapes (`\x1bM`, `\x1b7`). Stripped from the
#: message only -- never from what is streamed live to the console, where they
#: are exactly what the operator should see (tan-cli#541 review, MAJOR 2).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)


def _console_lines(text: str) -> list[str]:
    """The lines a TERMINAL would be showing after `text` was drawn on it --
    the input `_capture_tail` actually wants (tan-cli#541 review, MAJOR 2).
    Pure.

    Two things `str.splitlines()` gets wrong for a transcript that came off a
    pty, and got wrong the moment tan-cli#541 gave the child a tty back:

    * **It splits on `\\r`.** A `\\r`-redrawn progress bar -- which is what
      `pyocd`/`west`/`openocd` draw once they can see a terminal, and the
      whole reason #541 exists -- is ONE line on the screen and N lines to
      `splitlines()`. Measured on the pty path before this fix, three of
      `_capture_tail`'s four slots went to redraws of the same bar
      (`'[40%] writing image | [70%] writing image | [100%] writing image |
      Error: could not connect to target'`) and pushed the tool's actual
      diagnosis out. Split on `\\n` alone and keep only the segment after the
      LAST `\\r`, and the bar costs one slot showing its final state, which is
      what the operator was looking at. (`splitlines()` also splits on `\\v`,
      `\\f`, `\\x1c`-`\\x1e`, `\\x85`, `\\u2028`/`\\u2029` -- none of which
      end a line on a terminal either, and any of which can appear verbatim in
      a binary-ish tool dump.)
    * **It leaves the escapes in.** `data.entries[].message` and the text-mode
      `FAIL:` line are strings a customer reads and a `--format json` consumer
      may store or re-render; measured, a raw `\\x1b[31m` shipped inside one.

    **`\\r\\n` is a LINE ENDING; a bare `\\r` is a redraw.** The two have to be
    told apart BEFORE the collapse above, or the collapse eats the whole
    transcript on Windows (tan-cli#575 review). A Windows child writes `\\r\\n`,
    and the live-console tee reads the raw pipe and decodes it itself
    (`_Tee`) rather than letting `text=True` translate -- so every `\\r\\n` the
    tool wrote reaches here intact. Split on `\\n`, and each row then ENDS in
    `\\r`; the segment after that last `\\r` is the empty string, every row is
    dropped by the blank test below, and `_capture_tail` returns nothing but
    the bare `exited rc=N`. Measured on the shipped source before this fix:
    `_console_lines('Error: could not connect to target\\r\\n')` -> `[]`. That
    is not a test artefact -- it blanks the flash failure diagnosis for every
    Windows operator, which is the exact surface tan-cli#541's MAJOR 2 exists
    to protect. So the carriage return that belongs to the TERMINATOR is
    stripped first, and only what is left is read for redraws.

    `rstrip("\\r")` rather than removing exactly one, because a redraw is only
    ever observable when content FOLLOWS the `\\r`: trailing carriage returns
    drew nothing, so they can erase nothing. That also settles the last
    segment, which has no `\\n` after it and so no terminator to strip --
    a transcript that ends mid-row (`'Error: ...\\r'`, a tool killed on
    `_FLASH_TIMEOUT_S` just after returning the cursor, or a bar's final
    `\\r` with the newline still unwritten) keeps the row the terminal is
    still showing, where reading it as an erasure would throw away the
    diagnosis of the very run that failed.

    Deliberately NOT a terminal emulator: no cursor-position model, no
    scrollback. It collapses `\\r` runs and drops escape sequences, which is
    the whole of the damage a progress bar does to a captured transcript."""
    lines = []
    for raw in text.split("\n"):
        # The `\r` of a `\r\n` terminator (and any trailing one that drew
        # nothing) is not a redraw -- strip it before looking for redraws.
        row = raw.rstrip("\r")
        # After the last `\r`: what the redraws finally left on that row.
        drawn = _ANSI_ESCAPE_RE.sub("", row.rsplit("\r", 1)[-1]).rstrip()
        if drawn.strip():
            lines.append(drawn)
    return lines


def _capture_tail(outcome: _Outcome) -> str | None:
    """The failure tail from the ALREADY-captured output -- a pure read, no
    second spawn. The last 4 non-empty lines joined by " | ", or `None` when the
    process actually succeeded.

    Lines as a TERMINAL would show them ([`_console_lines`]), not as
    `str.splitlines()` finds them: since tan-cli#541 handed the child a pty,
    the transcript can carry the `\\r` redraws and the colour a tool only
    emits for a tty, and both were reaching the customer-visible string.

    **stderr-first is preserved where the streams still exist, and is
    deliberately not reconstructed where they do not.** A pty is ONE device by
    construction -- that is what makes it a terminal -- so a pty run's whole
    transcript arrives in `.stdout` with `.stderr` empty (see [`_tee_text`])
    and this preference has nothing to choose between. tan-cli#541 review,
    MAJOR 2 asked whether that loss is acceptable; the answer, measured rather
    than asserted:

    * The reported harm was a bar displacing the diagnosis, and that was the
      `\\r` split above, not the merge -- with it fixed the bar costs ONE slot
      and the measured 3-line diagnosis arrives whole (`'[100%] writing image
      | Error: flash algo timed out |   target reported SWD fault at
      0x08000000 |   the image on the device may be partial'`, against dev's
      `'Error: flash algo timed out | ...'`). What is left is one line of TRUE
      context -- how far the write got before it failed.
    * The residual loss is bounded and one-sided: only a diagnosis longer than
      three lines loses its OLDEST line, and only on a terminal.
    * Splitting it back apart is not free. Giving the child two ptys would
      restore the preference but would hand it two DIFFERENT terminals, would
      re-interleave the two streams through two reader threads instead of the
      child's own write order, and -- the load-bearing one -- would cost
      `_swd_probe_halt_markers` the chronological ordering its positional
      reading depends on (tan-cli#540 review, MAJOR 1: whether a halt failure
      came before or after the load is the entire distinction between a failed
      write and a benign busy core). Trading a correct flash verdict for a
      fourth line of tail is the wrong way round.
    * And the preference is not uniformly a win even where it survives: it
      picks stderr over stdout whenever stderr is non-blank, so a tool whose
      real diagnosis goes to stdout and whose stderr carries only a
      deprecation notice is reported by the notice. The merged transcript
      shows the diagnosis.

    The PIPE path is untouched: two streams, `.stderr` preferred exactly as
    before."""
    if outcome.success:
        return None
    text = outcome.stderr
    if not text.strip():
        text = outcome.stdout
    tail = _console_lines(text)[-4:]
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
    every one of them populates `outcome.stderr` regardless of mode.

    tan-cli#519/#522 review, MAJOR 1: an ORDINARY text-mode failure -- the
    child streamed straight to the console and then exited non-zero, with no
    tan-authored diagnosis at all -- used to leave BOTH `stderr`/`stdout`
    empty unconditionally, so this always fell through to the bare `flash
    command failed` sentence. That is no longer true for `_spawn`'s two
    single-tool branches (the wrapped-console capture-and-replay branch, and
    the live-console branch now [`_Tee`]s instead of only streaming): both
    populate `outcome.stdout`/`.stderr` with the child's own transcript in
    EVERY mode now, so an ordinary text-mode failure whose child printed a
    diagnosis on stderr/stdout (`Error: could not connect to target`, say)
    now surfaces that diagnosis here too, in place of the old generic
    sentence -- a CUSTOMER-VISIBLE change to the exact string
    `data.entries[].message` (and the text-mode `FAIL:` line) reports for
    every `_spawn`-backed method (`swd_probe`, `alif_setools`, `west_flash`,
    ...), declared in this change's own CHANGELOG entry. The bare fallback
    now survives only for a child that truly prints nothing (both streams
    genuinely empty) and for `_spawn_pipeline`'s UNCAPTURED text-mode path,
    which pipes neither half's stderr at all and so still reaches
    `_Outcome.stderr == ""` unconditionally -- see [`_half_lines`]'s own
    docstring for that narrower, still-true case."""
    if outcome.captured or outcome.stderr.strip() or outcome.stdout.strip():
        tail = _capture_tail(outcome)
        if tail:
            return f"{method}[{entry_id}]: {tail}"
    return f"{method}[{entry_id}]: flash command failed"


#: The two J-Link Commander phrases that mean the post-write PIN-reset
#: (`RSetType 2` / `r` / `g`, `plan_alif_mram_jlink`'s own script) asked the
#: core to halt and it refused -- the documented busy-resident case: an image
#: that never idles keeps the core running, so `VC_CORERESET` cannot halt it
#: (tan-cli#522). Matched against whatever JLinkExe printed, not derived from
#: the exit code -- JLinkExe still exits 0 here (`outcome.success` is `True`;
#: the WRITE and its `verifybin` genuinely succeeded), so the exit code alone
#: cannot tell this run apart from one whose reset actually landed.
_FLOW_D_HALT_FAILURE_MARKERS = ("Failed to halt CPU", "CPU is not halted")

#: The exact tail `plan_alif_mram_jlink` always appends to `ok_message` --
#: see its own `f"...; verified and PIN-reset"` lines. Matched verbatim so the
#: qualification below is a targeted substring swap, not a re-derivation of
#: the message shape.
_FLOW_D_VERIFIED_AND_RESET = "; verified and PIN-reset"
_FLOW_D_VERIFIED_ONLY = "; verified; reset requested, core was busy and did not halt"


def _flow_d_reset_qualified_message(ok_message: str, outcome: _Outcome) -> str:
    """Downgrade Flow D's claimed `PIN-reset` to what the transcript actually
    shows (tan-cli#522).

    `plan_alif_mram_jlink` composes `ok_message` at PLAN time, before the
    write has run -- it has no transcript to consult, so it reports the
    INTENDED outcome ("verified and PIN-reset") unconditionally. The write
    itself is genuinely fine here (`outcome.success` is `True`, `verifybin`
    passed), but a busy-resident image can keep the core running straight
    through `VC_CORERESET`, so the reset half did not take even though
    JLinkExe still exits 0 -- the identical message otherwise reports every
    run alike, whether the reset landed or the transcript ended in three
    lines of J-Link errors. `data.entries[].message` is what a `--format
    json` consumer renders as the outcome of the flash (the same surface
    tan-cli#402/#487 fixed for the device/address halves of this message),
    so an operator reading it cannot tell the two apart -- and on this board
    the reset is what would have started the freshly-written image.

    Scoped to a substring match on the CAPTURED transcript
    (`outcome.stdout`/`.stderr` -- see `_Outcome`/`_spawn`). tan-cli#522
    review, MAJOR 1: this used to be populated ONLY under `--format json`'s
    single-spawn capture, so in text mode -- the default human invocation --
    this was a silent no-op and the qualification never reached the operator
    reading the console, even though JLinkExe's own transcript streamed
    straight past them there too. `_spawn`'s live-console branch now TEES
    that same transcript (streamed live to the console AND collected, via
    [`_Tee`]) instead of only streaming it, so `outcome.stdout`/`.stderr` are
    populated in every mode and this function needs no mode split of its
    own. Not a general transcript-scraping layer: this reads only the one
    tail `plan_alif_mram_jlink` always appends, and only for
    `FLOW_D_METHOD`.

    tan-cli#519/#522 review, NIT (residual risk, not fixed): the transcript
    this reads is [`_Tee.text`]'s bounded join, which can be INCOMPLETE if a
    grandchild the killed child leaves behind still holds the pipe open past
    `_DRAIN_JOIN_S` -- see that method's own docstring. Should that bound
    ever truncate a real JLinkExe transcript mid-halt-failure, the marker
    substring search above finds nothing and this function falls through to
    `return ok_message` UNCHANGED -- the OPTIMISTIC `"; verified and
    PIN-reset"` claim, the exact wrong claim tan-cli#522 exists to stop. No
    plausible JLinkExe repro for this was constructible during this review
    (a 24 MB transcript drains through the tee in ~0.13s, far under the
    2s/4s bound), so this is documented risk, not a reproduced defect."""
    if _FLOW_D_VERIFIED_AND_RESET not in ok_message:
        return ok_message
    transcript = outcome.stdout + outcome.stderr
    if not any(marker in transcript for marker in _FLOW_D_HALT_FAILURE_MARKERS):
        return ok_message
    return ok_message.replace(_FLOW_D_VERIFIED_AND_RESET, _FLOW_D_VERIFIED_ONLY)


#: The exact claim `plan_swd_probe`'s J-Link arm composes at PLAN time for an
#: ELF/HEX artefact (`f"...{device} flashed via J-Link"`) -- the arm whose
#: Commander script takes `loadfile` and therefore carries NO `verifybin`
#: (there is no address to give one; see `jlink_commander_script`). Matched
#: verbatim so the qualification below is a targeted substring swap of the one
#: asserted word, not a re-derivation of the message shape -- the same
#: discipline `_FLOW_D_VERIFIED_AND_RESET` applies one backend over.
_SWD_PROBE_FLASHED_CLAIM = "flashed via J-Link"
_SWD_PROBE_ATTEMPTED_CLAIM = "write attempted via J-Link"

#: The claim the SAME arm composes for a raw `.bin` (tan-cli#540 defect 2),
#: whose script now ends its load with a `verifybin` read-back. Deliberately
#: NOT a superstring-safe variant of the above by accident: `_SWD_PROBE_
#: FLASHED_CLAIM` is not a substring of this one, so the two never both match
#: and the unverifiable arm's wording can never land on a verified write.
_SWD_PROBE_VERIFIED_CLAIM = "flashed and verified via J-Link"

#: The POST-LOAD qualification (tan-cli#590), appended when a halt failure
#: landed after the load completed. Deliberately NOT a fourth phrasing: the
#: clause `core was busy and did not halt` is lifted from Flow D's own
#: `_FLOW_D_VERIFIED_ONLY`, which draws this distinction already, and the
#: remediation sentence is the one the pre-load case in
#: [`_swd_probe_qualified_message`] already ends with. What is added is the
#: quoted marker -- the tan-cli#540 discipline of reporting what the tool said
#: rather than paraphrasing it.
#:
#: **It says "after the load", not "the post-write reset", and that limit is
#: deliberate** (tan-cli#590 REVIEW, MINOR 1). Flow D CAN name its reset,
#: because `plan_alif_mram_jlink` always emits one. This backend cannot, for
#: two measured reasons:
#:
#:   * `jlink_commander_script` emits `verifybin` BETWEEN the load and the
#:     `r`/`g` on the `.bin` arm, so a marker in this region may belong to the
#:     VERIFY stage rather than any reset.
#:   * `do_reset = _default(fa_bool_checked(fa, "reset"), True)` is a local in
#:     `plan_swd_probe`, consumed by `jlink_commander_script` and never carried
#:     on `FlashPlan` -- so a manifest with `reset: false` emits no `r`/`g` at
#:     all and this code cannot tell. Claiming "reset requested" there would be
#:     flatly false, not merely imprecise.
#:
#: What the partition DOES prove is that the core was busy after the load, and
#: therefore that nothing took the part through a halted reset into the image
#: just written -- which is exactly what the remediation turns on. The claim is
#: narrowed to that; the advice is unchanged.
_SWD_PROBE_BUSY_AFTER_LOAD = (
    "; after the load the core was busy and did not halt (J-Link reported "
    "{quoted}) -- the target may not have been taken through a halted reset "
    "into the firmware just written, and may still be running the firmware it "
    "had. Power-cycle it and confirm the new firmware answers."
)


def _swd_probe_halt_markers(outcome: _Outcome) -> list[str]:
    """Which of the J-Link halt-failure phrases this `swd_probe` write's own
    transcript actually contains (tan-cli#540). Empty when none did.

    Same two phrases Flow D matches (`_FLOW_D_HALT_FAILURE_MARKERS`) and for
    the same measured reason: #522 established on real E1M-AEN801 silicon
    that a halt failure does NOT make JLinkExe exit non-zero even with
    `-ExitOnError 1` on the argv -- this arm carries that flag too, and the
    bench still saw exit 0 alongside `VC_CORERESET did not halt CPU` /
    `WARNING: CPU could not be halted` / `****** Error: Failed to halt CPU` /
    `CPU is not halted`. So the exit code cannot tell a load into a halted
    core apart from a load into one that kept running.

    Returned as a LIST rather than a bool so the message below can quote what
    the tool actually said instead of paraphrasing it -- the difference
    between "tan thinks the core did not halt" and "J-Link said `Failed to
    halt CPU`" is the whole point of tan-cli#540.

    **POSITIONAL, not a whole-transcript substring search** (tan-cli#540
    review, MAJOR 1). `jlink_commander_script` emits TWO halt-capable stages,
    not one: the pre-load `r`/`halt`, and then -- after the load -- `r`/`g`,
    which `do_reset = _default(fa_bool_checked(fa, "reset"), True)` turns ON
    BY DEFAULT, so a shipped `E1M-V2N101` manifest carrying no `reset:` key
    gets it. Established by RUNNING the script through a capturing Commander
    stub on `PATH` rather than assuming an order: the script really is `r,
    halt, loadbin <art>, <base>, r, g, qc`, and the transcript really does
    report the load finishing (`Downloading file [...]` then `O.K.`) BEFORE
    the reset chatter that follows it.

    That ordering is the whole distinction. A resident image -- the GD32
    bridge firmware is exactly one -- starts running the instant `loadbin`
    finishes, so the post-load `r` cannot halt it and JLinkExe prints the same
    two phrases a never-halted core would, on a flash that landed perfectly.
    Searched positionlessly, a COMPLETELY SUCCESSFUL write was reported as
    unconfirmed and the operator was told to re-flash hardware. Markers after
    the load speak to the RESET (which is all Flow D ever claims them for --
    `_flow_d_reset_qualified_message` scopes them to `reset requested, core
    was busy and did not halt`); only markers BEFORE it can speak to whether
    the write happened.

    Conservative in the direction that matters: when the transcript never
    reports a completed load -- because the pre-load halt failed and the write
    never had a halted target, because the tool worded it differently, or
    because [`_Tee`]'s bounded join truncated it -- there is no boundary, every
    marker counts, and the unconfirmed verdict stands. Only a load the tool
    itself reported FINISHING moves a marker to the reset side.

    A marker landing between the two -- i.e. during the download -- cannot
    reach here at all: this arm carries `-ExitOnError 1` (measured on the real
    argv), so a `loadbin` that fails moves the exit code, `outcome.success` is
    `False`, and `_flash_entry` reports a failure rather than qualifying a
    success.

    The search runs over `stdout + stderr`, the same concatenation
    `_flow_d_reset_qualified_message` reads, so a marker on `stderr` sorts
    after all of `stdout`. That is the real ordering in both transports that
    exist here: JLinkExe writes this transcript to one stream, and a pty run
    is ONE device whose entire transcript lands in `.stdout` with `.stderr`
    empty (see [`_tee_text`]). Pure."""
    transcript = outcome.stdout + outcome.stderr
    loaded_at = _jlink_load_completed_at(transcript)
    markers = []
    for marker in _FLOW_D_HALT_FAILURE_MARKERS:
        at = transcript.find(marker)
        if at < 0:
            continue
        if loaded_at is not None and at >= loaded_at:
            continue  # after the load: the reset stage's, not the write's
        markers.append(marker)
    return markers


def _swd_probe_post_load_halt_markers(outcome: _Outcome) -> list[str]:
    """The other half of the same partition (tan-cli#590): which halt-failure
    phrases this write's transcript contains AT OR AFTER the point JLinkExe
    said the load finished. Empty when none did, and empty whenever the
    transcript never reported a load COMPLETING at all. Pure.

    tan-cli#575 made [`_swd_probe_halt_markers`] positional and dropped every
    post-load marker from the WRITE verdict, which was right: a marker printed
    after `loadbin`/`loadfile` finished says nothing about whether the bytes
    landed, and treating it as write-doubt was the false alarm #575 removed.
    But it does not follow that such a marker says NOTHING -- it says the core
    was still running after the bytes landed, so nothing took the part through
    a halted reset INTO the image just written. `jlink_commander_script`'s
    post-load `r`/`g` is ON BY DEFAULT (`do_reset = _default(fa_bool_checked(
    fa, "reset"), True)`, so a shipped `E1M-V2N101` manifest carrying no
    `reset:` key gets it), and it is exactly the stage a freshly-written
    resident image refuses -- the GD32 bridge firmware starts running the
    instant the load completes. So this is the COMMON post-write shape, not an
    edge one, and #575 left it reported as a bare `flashed and verified` with
    no qualification at all.

    This function does NOT claim the marker came from that `r`/`g`, and the
    message it feeds does not either -- see [`_SWD_PROBE_BUSY_AFTER_LOAD`] for
    the two measured reasons the region is wider than the reset stage
    (`verifybin` sits inside it on the `.bin` arm, and `reset: false` removes
    the reset entirely without this code being able to tell).

    The boundary is [`_jlink_load_completed_at`]'s, unchanged and shared, so
    the two halves can never disagree about where the load ended. A marker
    present on BOTH sides lands in both lists; [`_swd_probe_qualified_message`]
    checks the write-scoped half FIRST, so the pre-load wording wins there and
    the two qualifications are never both appended.

    `loaded_at is None` yields `[]` deliberately, and that asymmetry is the
    same conservatism [`_swd_probe_halt_markers`] documents from the other
    side: when the tool never reported a completed load, every marker counts
    as the WRITE's (the unconfirmed verdict stands) and none as the post-load
    stage's. Splitting one ambiguous marker across both verdicts would qualify
    the write AND the post-load stage off a single phrase.

    Searched over `stdout + stderr`, the same concatenation every other reader
    here uses -- see [`_swd_probe_halt_markers`] for why that ordering holds
    in both transports."""
    transcript = outcome.stdout + outcome.stderr
    loaded_at = _jlink_load_completed_at(transcript)
    if loaded_at is None:
        return []
    return [m for m in _FLOW_D_HALT_FAILURE_MARKERS if transcript.find(m, loaded_at) >= 0]


#: The two phrases JLinkExe's own transcript uses to open and to close a load
#: -- `loadbin` (raw `.bin`) and `loadfile` (ELF/HEX) print the same pair, so
#: the positional reading below covers BOTH of `jlink_commander_script`'s
#: arms. Matched verbatim, as substrings, exactly as the halt markers above
#: are: the path inside the brackets varies per run and the completion token
#: stands alone on its own line.
_JLINK_LOAD_OPENED = "Downloading file"
_JLINK_LOAD_COMPLETED = "O.K."


def _jlink_load_completed_at(transcript: str) -> int | None:
    """The index just past the point JLinkExe said the load FINISHED, or `None`
    when it never said so (tan-cli#540 review, MAJOR 1). Pure.

    Completion, not commencement: the opening `Downloading file [...]` alone
    proves only that a write was attempted, which is the very thing already in
    doubt. The completion token is required to come AFTER the opening one so a
    stray `O.K.` from an earlier stage (a connect, a `halt`) cannot be read as
    this load's, and so a download that opens and then goes quiet -- the
    truncated-transcript case -- yields `None` and keeps every marker
    counting."""
    opened = transcript.find(_JLINK_LOAD_OPENED)
    if opened < 0:
        return None
    completed = transcript.find(_JLINK_LOAD_COMPLETED, opened)
    if completed < 0:
        return None
    return completed + len(_JLINK_LOAD_COMPLETED)


def _swd_probe_qualified_message(
    ok_message: str, markers: list[str], post_load_markers: list[str]
) -> tuple[str, bool]:
    """Qualify `swd_probe`'s claim by what the transcript shows, and say
    whether the WRITE itself is left unconfirmed (tan-cli#540). Returns
    `(message, write_unconfirmed)`; `(ok_message, False)` unchanged when the
    transcript named no halt failure on EITHER side of the load, or when the
    message is not one this backend's J-Link arm composed. Pure.

    `markers` are the PRE-load (write-scoped) halt failures and
    `post_load_markers` the ones after the load completed -- see
    [`_swd_probe_halt_markers`] and [`_swd_probe_post_load_halt_markers`] for
    the partition. `markers` is checked first and returns on its own, so a
    marker present on both sides gets the write-scoped wording alone; the two
    are never both appended.

    `plan_swd_probe` composes its claim at PLAN time, before anything has run,
    and `_flash_entry` asserts it on the exit code -- which tan-cli#522
    measured on real E1M-AEN801 silicon does NOT go non-zero when the core
    fails to halt, even with `-ExitOnError 1` (this arm carries it). So the
    exit code alone can never tell a halted write from a non-halted one; only
    the transcript (see [`_swd_probe_halt_markers`]) and the script's own
    read-back can.

    **Two arms, two different truths, because only one can verify.**

    * A raw `.bin` (`_SWD_PROBE_VERIFIED_CLAIM`) -- `jlink_commander_script`
      now emits `verifybin` after the load (tan-cli#540 defect 2), so a run
      that exits 0 has had its bytes COMPARED against the artefact. The write
      is confirmed; `write_unconfirmed` is `False` and the
      `flash.swd-probe-write-unconfirmed` advisory (whose own text says "this
      backend runs no verifybin") would be false here and must not fire. What
      a halt failure still costs is the RESET half: nothing took the part
      through a halted reset into the image just written, so it may still be
      executing the one it had. That is Flow D's exact residual and it gets
      Flow D's exact treatment -- the message is qualified, the claim of a
      verified write is not. The sentence deliberately does NOT name the
      post-write `r`/`g` as the stage that failed, because after the
      positional fix it cannot have been: [`_swd_probe_halt_markers`] drops
      every marker printed after the load completes, so a marker that reaches
      here is a PRE-load one (or one from a transcript that never reported a
      load completing at all, where the stage is unknowable). Naming the
      post-write reset would be the one wording that is wrong in every case
      that still gets here.
    * An ELF/HEX (`_SWD_PROBE_FLASHED_CLAIM`) -- `loadfile` takes no address,
      `verifybin` requires one, and no `verifyfile` is emitted because nothing
      in this repo has measured that the shipped Commander accepts it (see
      `jlink_commander_script`). This arm therefore still has NEITHER of the
      two things that could make `flashed` an observation, so the word becomes
      `write attempted`, `write_unconfirmed` is `True`, and the advisory
      fires. Narrowed, not deleted: a path that cannot check its own write
      still owes the operator that sentence.

    Blast radius, for a reader wondering whether this matters: `swd_probe` is
    declared in alp-sdk metadata solely under `helper_firmware:` (`name:
    gd32_bridge`, `chip: gd32g553`) on `E1M-V2N101`, `E1M-V2M101` and
    `E1M-V2M102`, so the failure this qualifies is "tan says the GD32 bridge
    firmware is flashed when it may not be", which then presents downstream as
    the bridge not answering with the flash step apparently clean.

    Status stays `ok` and rc stays 0 in BOTH arms (`_flash_entry`),
    deliberately. On the `.bin` arm a genuinely failed write no longer reaches
    this function at all -- `verifybin` + `-ExitOnError 1` makes it a non-zero
    exit and a `failed` entry. On the ELF/HEX arm the write may well have
    landed (a busy-resident core is the documented benign shape on the Flow D
    side) and there is no bench evidence that a GD32 halt failure means a
    failed write, so failing the run there would trade an overstatement for a
    false negative -- worse, on a command that writes hardware.

    **The POST-load case (tan-cli#590), one qualification for both arms.**
    #575's positional rule dropped a post-load marker from the write verdict
    and then dropped it entirely, so the commonest real outcome -- a
    resident image that starts running the instant `loadbin` finishes and
    refuses the default post-write `r`/`g` -- reported the bare `flashed and
    verified via J-Link @ <base>` with nothing qualifying it. The operator was
    told the write succeeded (true) and told nothing about the target never
    having been taken through a halted reset.

    What changes is the POST-LOAD half only, and identically on both arms:
    [`_SWD_PROBE_BUSY_AFTER_LOAD`] is appended and NOTHING else moves. The write
    claim is untouched in both spellings -- `flashed and verified` stays
    verified (that is what `verifybin` measured), and the ELF/HEX arm's plain
    `flashed` is left exactly as unqualified as it is today, because a
    post-load marker is not evidence against a write and the thing that arm
    genuinely cannot do (verify) is the pre-load branch's separate sentence.
    `write_unconfirmed` stays `False`, so `flash.swd-probe-write-unconfirmed`
    does NOT fire: that advisory's own text says "this backend runs no
    verifybin", which would be false on the `.bin` arm, and firing it here
    would undo exactly what #575 fixed.

    Status stays `ok` and rc stays 0 here too. #590 records the exit-code
    question rather than deciding it, and the reasoning it gives for leaning
    that way is the one already applied one paragraph up: the write succeeded
    and was verified, so failing the run would be a worse false alarm than the
    one #575 removed."""
    if not markers:
        return _swd_probe_post_load_qualified(ok_message, post_load_markers)
    quoted = " / ".join(f'"{marker}"' for marker in markers)
    if _SWD_PROBE_VERIFIED_CLAIM in ok_message:
        return (
            ok_message
            + f"; the core did not halt (J-Link reported {quoted}), so the bytes "
            "are verified but the target was never taken through a halted reset "
            "-- it may still be running the firmware it had. Power-cycle it and "
            "confirm the new firmware answers.",
            False,
        )
    if _SWD_PROBE_FLASHED_CLAIM not in ok_message:
        return ok_message, False
    return (
        ok_message.replace(_SWD_PROBE_FLASHED_CLAIM, _SWD_PROBE_ATTEMPTED_CLAIM)
        + f"; the core did not halt (J-Link reported {quoted}) and this backend "
        "runs no verifybin for an ELF/HEX load, so nothing confirms the bytes "
        "landed -- re-run the write with the target held in reset and confirm the "
        "firmware answers before trusting it.",
        True,
    )


def _swd_probe_post_load_qualified(
    ok_message: str, post_load_markers: list[str]
) -> tuple[str, bool]:
    """The post-load half of [`_swd_probe_qualified_message`] (tan-cli#590),
    split out so that function's own pre-load branches stay untouched. Pure.

    Returns `(ok_message, False)` unchanged when the transcript named no
    post-load halt failure, or when the message is not one this backend's
    J-Link arm composed -- the openocd/pyocd arm makes neither claim and never
    emits these phrases, and `_flash_entry` already gates the whole
    qualification on the J-Link arm having run.

    Both claims are matched, and both keep their write wording verbatim: the
    append is the ONLY edit. The two claim constants are lexically disjoint by
    construction (`_SWD_PROBE_VERIFIED_CLAIM` is not a superstring of
    `_SWD_PROBE_FLASHED_CLAIM`), so this cannot append twice."""
    if not post_load_markers:
        return ok_message, False
    if not (
        _SWD_PROBE_VERIFIED_CLAIM in ok_message or _SWD_PROBE_FLASHED_CLAIM in ok_message
    ):
        return ok_message, False
    quoted = " / ".join(f'"{marker}"' for marker in post_load_markers)
    return ok_message + _SWD_PROBE_BUSY_AFTER_LOAD.format(quoted=quoted), False


#: `ALP_FLASH_REQUIRE_DPIDR=1` (tan-cli#589) -- the env spelling, matching the
#: `ALP_FLASH_FORCE=1` idiom this same command already reads for its confirm
#: gate (`_run`), so a bench/factory harness arms both the same way.
REQUIRE_DPIDR_ENV = "ALP_FLASH_REQUIRE_DPIDR"


def _dpidr_unarmed_advisory(entry_id: str, method: str | None) -> str:
    """The `flash.dpidr-preflight-unarmed` warning text for one entry (tan-cli
    #520, generalised off the method by tan-cli#609). Pure.

    The method NAMES ITSELF here rather than the string saying `swd_probe`
    unconditionally: #609 measured an AEN Flow D write emitting no advisory at
    all, and the fix that makes it emit one must not then mislabel it as a
    `swd_probe` write. For `swd_probe` this renders byte-for-byte what it
    rendered before -- the only difference is which entries reach it.

    The remedy clause differs because the two backends' ARMED shapes differ.
    `swd_probe` arms off `expect_dpidr` alone (its read device is the already-
    resolved write device, `FlashPlan.preflight_device`). Flow D pairs
    `expect_dpidr` with `flash_args.jlink_device`, the LIVE-CORE attach
    profile, which is a different field from `jlink_flash_device` (the
    write-time MRAM-loader profile that arms Flow D itself) -- telling an AEN
    operator to set only `expect_dpidr` would earn them a half-armed refusal
    from `validate_flow_d_preflight_args` on the next run.
    """
    remedy = (
        "Set flash_args.expect_dpidr AND flash_args.jlink_device (the live-core "
        "attach profile, not jlink_flash_device) to arm it."
        if method == FLOW_D_METHOD
        else "Set flash_args.expect_dpidr to arm it."
    )
    return (
        f"{entry_id}: {method} wrote with no flash_args.expect_dpidr set -- "
        "the read-only SW-DP ID preflight did not run, so a cloned/shared "
        f"probe serial could still have reached the wrong board. {remedy}"
    )


def _require_dpidr_gate(
    method: str, entry_id: str, flash_args: Any, preflight_device: str | None
) -> str | None:
    """The `ALP_FLASH_REQUIRE_DPIDR=1` decision for one entry: a refusal
    message, or `None` to proceed. Pure. The CALLER checks
    `ctx.require_dpidr` -- this answers only "is this write guarded".

    Scoped by `DPIDR_GUARD_COVERAGE`, not by a `method ==` literal (tan-cli
    #609), so the strict switch and the default advisory cover exactly the
    same methods and cannot drift apart the way they had before #609: the
    advisory was wired to `swd_probe` and #607 wired the switch to `swd_probe`
    after it, leaving the AEN's customer MRAM path outside both.

    `armed` is written as `possible and not <the advisory's own predicate>`
    rather than as a second reading of `flash_args`, for the same reason the
    #520 call site hoisted its arm test to a single local: two spellings of
    "is the guard up" silently disagreeing is how a refusal fires on a guarded
    write, or stays silent on an unguarded one. An entry where a preflight is
    IMPOSSIBLE (`swd_probe`'s openocd/pyocd arm) is never `armed`, so it
    refuses too -- with different remediation, see `_require_dpidr_refusal`.
    """
    if not DPIDR_GUARD_COVERAGE.get(method, False):
        return None
    possible = dpidr_preflight_possible(method, preflight_device)
    armed = possible and not dpidr_preflight_unarmed(method, flash_args, preflight_device)
    if armed:
        return None
    return _require_dpidr_refusal(method, entry_id, possible)


def _require_dpidr_refusal(method: str, entry_id: str, preflight_possible: bool) -> str:
    """The refusal text for a write that `ALP_FLASH_REQUIRE_DPIDR=1` demands a
    wrong-board guard for and that has none (tan-cli#589; generalised past
    `swd_probe` by tan-cli#609). Pure.

    **Why the conditional #589 asks for cannot be implemented as written.**
    #589 asks for `flash.dpidr-preflight-unarmed` to become a refusal "for
    boards whose metadata declares a SW-DP ID". There is no such second
    declaration to read: `flash_args.expect_dpidr` IS the declaration, so
    "declares an ID" and "is armed" are the same predicate and the conditional
    collapses. What remains is a choice of DEFAULT, and there are three
    options, not two (tan-cli#590 REVIEW, MAJOR 3 -- an earlier version of
    this docstring listed only the first two and read as though opt-in were
    the only workable design; it is not):

    * **Advisory by default** (what ships). An `issues[]` warning does not
      stop a write, so on its own it did not answer #589's near-miss -- which
      happened on an UNATTENDED bench run, where nobody reads warnings between
      the plan and the write. Hence the switch below.
    * **Refuse always, no override.** No shipped alp-sdk preset carries a
      SW-DP ID today (measured: `expect_dpidr` appears nowhere under
      `metadata/**`) and tan is forbidden from deriving one
      (`_resolve_jlink_device`'s I-26 reasoning), so this refuses 100% of real
      `swd_probe` writes with no way out. That is not defensible.
    * **Refuse by default WITH a documented override** (e.g. an
      `ALP_FLASH_ALLOW_UNGUARDED=1`, or reusing the existing
      `ALP_FLASH_FORCE`). This one refuses NOBODY: the refusal text is itself
      the discovery mechanism, delivered at the one moment the operator is
      looking. It is strictly stronger than the shipped default for the
      customer path -- a bricked-bridge recovery is a real, maintainer-
      confirmed scenario (a customer CAN flash, to recover a bricked device,
      with Alp Lab-supplied binaries), and such a customer will never have
      read a bench doc to know an env var exists.

    The shipped default is the FIRST, and the reason is scope rather than
    design: #589's actual incident is a bench host, where the switch is set
    once and is sufficient, and tan-cli#610 endorses exactly this shape while
    the GD32's SW-DP ID is unverified -- arming anything by default before
    that value is settled would refuse writes against an ID nobody can source.
    Refuse-with-override is the better end state for the customer path and is
    recorded as a follow-up, not dismissed.

    An env var rather than a flag deliberately -- a per-invocation flag is
    forgotten precisely on the run that needed it, and this is a property of
    the host, not of the command.

    **Scope: every method tan can run the preflight for** -- today
    `swd_probe` AND Flow D (`alif_mram_jlink`), read off
    `DPIDR_GUARD_COVERAGE` rather than a `method ==` literal (tan-cli#609).
    #589 shipped this switch scoped to `swd_probe`, matching the advisory's
    own scope at the time; #609 then measured that the advisory never reached
    the AEN at all, so the Alif MRAM write -- the one genuinely
    CUSTOMER-facing flash path of the two, the GD32 bridge being
    factory-programmed by Alp Lab -- sat outside both halves of the guard.
    Widening the switch with the advisory, off one shared table, is what stops
    the next backend inheriting the same silence. It changes nothing for
    anyone who has not set the env var: the switch is opt-in and defaults off.

    **Where the Flow D refusal fires matters.** `_flash_entry` calls this for
    Flow D from the same early point tan-cli#512 hoisted Flow D's preflight
    to -- ahead of the SETOOLS auto-sign, which is itself a real write into
    the customer's SETOOLS install (`app-gen-toc` REWRITES
    `build/app-package-map.txt`). Refusing at the later, shared `swd_probe`
    site instead would let the sign run first, which is exactly the ordering
    defect #512 fixed.

    **Both `swd_probe` arms refuse, with different remediation.** On the
    openocd/pyocd arm
    an armed preflight is not merely absent but IMPOSSIBLE: the DPIDR read is
    a JLinkExe-only primitive here, so `plan_swd_probe` refuses an
    `expect_dpidr` that lands there at plan time. That arm therefore has no
    wrong-board guard AND (unlike the J-Link arm) no advisory either, because
    `preflight_device is None` makes `dpidr_preflight_possible` False -- the
    quietest case of the two. `openocd_usb_location` does not close it:
    a USB path SELECTS a probe, it never confirms what is on the other end of
    the SWD cable, which is the exact failure #589 measured (the probe
    resolved to a device and the device was a different SoC on a different,
    unreserved board).

    `preflight_possible` is `dpidr_preflight_possible`'s answer for this
    entry, threaded in rather than recomputed, so the branch a customer READS
    is the same one the gate DECIDED on. `method` prefixes the message -- for
    `swd_probe` both texts render byte-for-byte as they did before #609; the
    only new wording is Flow D's paired remedy, since `expect_dpidr` alone
    would earn an AEN manifest a half-armed refusal on its next run."""
    if preflight_possible:
        remedy = (
            "flash_args.expect_dpidr to this board's SW-DP IDR AND "
            "flash_args.jlink_device to its live-core attach profile (Flow D pairs "
            "the two; jlink_flash_device is the write-time loader, not this)"
            if method == FLOW_D_METHOD
            else "flash_args.expect_dpidr to this board's SW-DP IDR"
        )
        return (
            f"{method}[{entry_id}]: {REQUIRE_DPIDR_ENV}=1 is set and "
            "flash_args.expect_dpidr is not -- refusing to write with no wrong-board "
            "guard. The read-only SW-DP ID preflight is the only check that the probe "
            "reached the intended board: JLinkExe selects a probe by serial alone, and "
            f"a cloned or shared serial cannot be told apart without it. Set {remedy}, "
            f"or unset {REQUIRE_DPIDR_ENV} to accept an unguarded write."
        )
    return (
        f"{method}[{entry_id}]: {REQUIRE_DPIDR_ENV}=1 is set, but this run is taking "
        "the openocd/pyocd path, which has no SW-DP ID preflight of its own -- "
        "refusing to write with no wrong-board guard. OpenOCD's `adapter usb location` "
        "selects a probe but never confirms which board is on the other end of the SWD "
        "cable. Ensure a SEGGER J-Link is on PATH (and flash_args.use_openocd/use_pyocd "
        "are not forcing this path), add flash_args.jlink_device and set "
        f"flash_args.expect_dpidr, or unset {REQUIRE_DPIDR_ENV} to accept an unguarded "
        "write."
    )


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
    #: `ALP_FLASH_REQUIRE_DPIDR=1` (tan-cli#589) -- the OPT-IN strict half of
    #: the wrong-board guard. Default `False`, i.e. every existing invocation
    #: behaves byte-for-byte as before and an unarmed write keeps the
    #: `flash.dpidr-preflight-unarmed` advisory. When set, an unarmed write on
    #: any method the guard covers (`DPIDR_GUARD_COVERAGE` -- `swd_probe` AND
    #: Flow D since tan-cli#609, not `swd_probe` alone as #589 shipped it)
    #: REFUSES before anything spawns, and before Flow D's SETOOLS auto-sign
    #: mutates anything -- see [`_require_dpidr_refusal`] for the whole
    #: argument and [`_require_dpidr_gate`] for the decision itself.
    require_dpidr: bool = False
    #: `--recover` (tan-cli#611) -- the operator's half of the deliberate action
    #: that reaches a `flash_policy: recovery_only` helper. Inert on its own:
    #: `helper_flash_gate` also requires `helper_filter` to name the entry, so a
    #: whole-manifest run cannot sweep a recovery helper in.
    recover: bool = False
    #: The run's `--helper NAME`, or `None` (tan-cli#611). Already applied by
    #: `plan_flash_targets` as a target FILTER; carried here as well because the
    #: recovery gate needs to know the run was narrowed to ONE named entry, which
    #: a filtered target list alone cannot say (a manifest with a single helper
    #: yields the same list unfiltered).
    helper_filter: str | None = None


def _recovery_armed_for(target: FlashTarget, ctx: _Context) -> bool:
    """Did this target reach dispatch as an armed recovery flash?

    The same three-part condition `helper_flash_gate` lets through, stated once
    here so the `_Entry` flag and the gate cannot drift apart: the preset must
    declare `recovery_only`, the operator must have passed `--recover`, and the
    run must be narrowed to this entry by name."""
    return (
        (target.flash_policy or "").strip() == FLASH_POLICY_RECOVERY_ONLY
        and ctx.recover
        and ctx.helper_filter == target.id
    )


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
            f"app-gen-toc -- not run ({confirm_gate_note(why)})"
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

    # A `recovery_only` entry reaches dispatch ONLY when the operator both named
    # it and armed `--recover` -- exactly the condition `helper_flash_gate` lets
    # through, computed here so every `_Entry` this function builds carries the
    # flag. Set from here rather than at the write itself so the advisory rides
    # along even when the entry later skips or fails for an unrelated reason:
    # the fact worth reporting is that a recovery write was AUTHORISED, not that
    # one completed. False on every policy-declined path by construction.
    recovery = _recovery_armed_for(target, ctx)

    def entry(
        method: str | None,
        status: str,
        rc: int,
        message: str,
        *,
        preflight_unarmed: bool = False,
        write_unconfirmed: bool = False,
    ) -> _Entry:
        return _Entry(
            kind=kind, id=entry_id, method=method, status=status, rc=rc, message=message,
            preflight_unarmed=preflight_unarmed, write_unconfirmed=write_unconfirmed,
            recovery_armed=recovery,
        )

    # tan-cli#611, THE HOIST. WHO may flash this entry is decided BEFORE
    # anything about HOW, so a helper the SoM preset declares non-customer is
    # declined whether or not it also carries a `flash_method`. Previously the
    # only such declaration (`update_channel`) was consulted exclusively inside
    # the `if not raw_method:` guard below -- so an entry declaring both had its
    # declaration silently dropped and was flashed like any other target, which
    # is worse than carrying no declaration at all.
    #
    # `entry(None, ...)`: the method is deliberately NOT reported for a policy
    # decline. `as_dict()` omits a `None` method entirely, matching the shape
    # the `update_channel` skip below has always emitted -- an entry nobody was
    # allowed to flash should not advertise the transport it would have used.
    policy_skip = helper_flash_gate(
        target, recovery_armed=ctx.recover, helper_filter=ctx.helper_filter
    )
    if policy_skip is not None:
        lines.append(policy_skip)
        return -1, entry(None, "skipped", -1, policy_skip), lines

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
            # tan-cli#512: the read-only DPIDR preflight must run BEFORE the
            # SETOOLS auto-sign, not after -- `_resolve_flow_d_atoc_via_
            # setools` is itself a real write into the customer's SETOOLS
            # install (`app-gen-toc` REWRITES `build/app-package-map.txt`
            # rather than appending, destroying any prior accumulated sign
            # record), and the wrong-board case this preflight exists to
            # catch is exactly the one where nothing should have been
            # written yet. Measured on real E1M-AEN801 silicon: a wrong-
            # board `expect_dpidr` correctly aborted the MRAM write with
            # slot0 byte-identical, but the SETOOLS install had already been
            # mutated by the sign that ran first.
            #
            # Gated on the SAME `not (ctx.dry_run or not confirm)` condition
            # `_resolve_flow_d_atoc_via_setools`'s own real-sign branch uses
            # (identically, `plan_alif_mram_jlink` computes this as
            # `planning_only` for the OLD call site below) -- a preview or an
            # unconfirmed run gets no preflight probe either, matching the
            # previous call site's behaviour exactly. The preflight reads
            # only `expect_dpidr`/`jlink_device`/`jlink_serial`/`jlink_speed`
            # off `flash_args` (`flow_d_preflight_script`) -- none of which
            # `_resolve_flow_d_atoc_via_setools` touches (it only ever adds
            # `atoc`/`atoc_address`) -- so running it against `flash_args` as
            # it stands here, before the sign, is behaviourally identical to
            # running it after for every case except the one this fixes.
            if not ctx.dry_run and confirm:
                # tan-cli#609: the strict switch's Flow D site. It has to be
                # HERE, not at the shared one further down, for exactly the
                # reason #512 hoisted the preflight to this point: the
                # SETOOLS auto-sign a few lines below is itself a real write
                # into the customer's SETOOLS install, and a refusal that
                # fires after it has already run is a refusal that did not
                # prevent the mutation. `preflight_device=None` is Flow D's
                # shape -- it has no openocd/pyocd arm to be on, so its read
                # device comes from `flash_args.jlink_device` (paired) rather
                # than from an already-resolved write device.
                if ctx.require_dpidr:
                    refusal = _require_dpidr_gate(method, entry_id, flash_args, None)
                    if refusal is not None:
                        lines.append(f"flash: {kind} '{entry_id}' -> {method}")
                        lines.append(f"  FAIL: {refusal}")
                        return 1, entry(method, "failed", 1, refusal), lines
                preflight_inputs = FlashInputs(
                    artefact=artefact_path,
                    flash_args=flash_args,
                    core_id=entry_id,
                    sku=ctx.sku,
                    dry_run=ctx.dry_run,
                    force_confirm=ctx.force_confirm,
                )
                refusal = _flow_d_preflight(preflight_inputs, ctx.venv_bin, ctx.workspace)
                if refusal is not None:
                    lines.append(f"flash: {kind} '{entry_id}' -> {method}")
                    lines.append(f"  FAIL: {refusal}")
                    return 1, entry(method, "failed", 1, refusal), lines
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
            f"would run {shown} -- NOT written: "
            f"{confirm_gate_note('flash_args.confirm is false')}"
        )
        lines.append(f"  {msg}")
        return 0, entry(method, "planned", 0, msg), lines

    # tan-cli#512: Flow D's read-only DPIDR preflight used to run HERE --
    # after `meta.build`, immediately before the real write. It now runs
    # earlier, above, ahead of the SETOOLS auto-sign that may precede this
    # point: see the `if not ctx.dry_run and confirm:` block in the
    # `FLOW_D_METHOD` branch near the top of this function for the fix and
    # why the sign could not be allowed to run first.

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

    # tan-cli#520: `swd_probe`'s J-Link arm gets the identical read-only DPIDR
    # preflight Flow D has via #512 -- reusing `_flow_d_preflight` (the same
    # runner, `method="swd_probe"` so a refusal names the right backend)
    # rather than growing a second implementation. Placed HERE, immediately
    # before the one spawn (`_execute`, just below) that can ever write for
    # this entry: unlike Flow D there is no earlier mutating step (no SETOOLS
    # auto-sign) to hoist ahead of on this path, so this position already
    # satisfies #512's "before anything that writes or mutates" rule -- it is
    # not the ordering bug #512 fixed, because that bug was specifically about
    # a mutating step that does not exist here. Reached only on a genuine,
    # non-dry-run write: the `plan.planning_only or ctx.dry_run` return above
    # already exits for a preview (`plan_swd_probe` never sets
    # `planning_only`, so a real run always reaches this line unconfirmed --
    # `swd_probe` targets an external helper MCU's own flash, not persistent
    # SoC MRAM, and has no `flash_args.confirm` gate to hide behind).
    #
    # Gated on `plan.preflight_device is not None` -- NOT merely
    # `method == "swd_probe"` (tan-cli#520 REVIEW, BLOCKER 2). `preflight_
    # device` is set ONLY by `plan_swd_probe`'s J-Link branch; it is `None`
    # for the openocd/pyocd arm. Calling `_flow_d_preflight` unconditionally
    # for every `swd_probe` entry passed `read_device=None` on THAT arm,
    # which `flow_d_preflight_script` reads as "derive `require_device_key`
    # from `read_device is None`" -- i.e. `True`, Flow D's PAIRED shape -- so
    # a manifest that legitimately sets `flash_args.jlink_device` (e.g. as a
    # J-Link fallback profile) while `flash_args.use_openocd: true` forces
    # this arm for real got refused at WRITE time for an absent
    # `expect_dpidr` it was never asked to pair, even though `--dry-run` on
    # the exact same manifest stayed green (the plan-time guard just below
    # `plan_swd_probe`'s arm split checks `expect_dpidr` on that arm, and
    # only that -- `jlink_device` has no preflight-only meaning on the
    # openocd/pyocd arm, since it is not paired with `expect_dpidr` there.
    # This no longer means "never checked" though: the round-four hoist
    # (`flash_plan.py:1225`, ahead of this same split) added a `jlink_device`
    # charset-and-type guard that runs unconditionally on BOTH arms -- a
    # shape check, not a preflight pairing, so it does not change the
    # conclusion here). Gating on `preflight_device`
    # instead means this call never runs at all for that arm, matching
    # `plan_swd_probe`'s own plan-time guard exactly: `expect_dpidr` set on
    # the openocd/pyocd arm still refuses (that check, unaffected by this
    # fix); `expect_dpidr` absent there now stays a true no-op, both under
    # `--dry-run` and for real.
    # tan-cli#520 REVIEW, design point: `expect_dpidr` stays OPTIONAL (making
    # it required would refuse every shipped preset -- none carries a SW-DP
    # ID today, and tan is forbidden from deriving one, the same I-26
    # reasoning `_resolve_jlink_device` already documents), but that leaves a
    # SILENT gap: a confirmed J-Link `swd_probe` write with no `expect_dpidr`
    # runs with no wrong-board guard and no signal that it did. Recorded
    # here, before the preflight call, and surfaced as a non-fatal warning
    # `Issue` on the eventual SUCCESS return below (never on a refusal --
    # armed-and-mismatched already gets its own `flash.entry-failed`) so a
    # `--format json` consumer can tell "this write had no wrong-board check"
    # from an ordinary clean flash.
    # tan-cli#520 REVIEW round 2, nit: whether the preflight call below runs
    # and whether an unarmed write can warn must not drift apart -- two copies
    # of "did the J-Link arm actually run" silently going out of sync would
    # let the warning fire on an entry whose preflight DID run, or stay silent
    # on one where it did not.
    #
    # tan-cli#609 keeps that property while widening the warning past this
    # method: both are `plan.preflight_device is not None` for `swd_probe`,
    # this local directly and `preflight_unarmed` via
    # `dpidr_preflight_possible`, which is handed the SAME value. The local
    # stays `swd_probe`-shaped because its OTHER readers -- the preflight call
    # and #540/#590's halt-marker partition -- are about JLinkExe's transcript
    # on this backend specifically, not about the guard's coverage.
    swd_probe_took_jlink_arm = method == SWD_PROBE_METHOD and plan.preflight_device is not None
    # tan-cli#609: METHOD-INDEPENDENT, decided by `DPIDR_GUARD_COVERAGE` inside
    # `dpidr_preflight_unarmed` rather than by `method == SWD_PROBE_METHOD`
    # here. The literal this replaces is the whole defect #609 measured: the
    # AEN dispatches Flow D, so a real MRAM write emitted `ISSUES = []` -- no
    # wrong-board guard AND no signal that there was none -- while the warning
    # about an unarmed guard was wired only to the other backend. Reached only
    # on a real, confirmed write for BOTH methods (every preview path has
    # already returned above), which is the precondition
    # `dpidr_preflight_unarmed` documents.
    preflight_unarmed = dpidr_preflight_unarmed(method, flash_args, plan.preflight_device)
    # tan-cli#589: the OPT-IN strict half. Placed HERE -- after the
    # `planning_only or ctx.dry_run` return above and BEFORE both the
    # preflight probe and `_execute` -- so it is the last word before
    # anything can write, and so `--dry-run` stays the pure, policy-free
    # preview it is documented to be (a preview writes nothing, so there is
    # nothing for a wrong-board guard to protect there, and making a preview
    # depend on a bench env var would make the same manifest preview
    # differently on two machines for no reason a preview cares about).
    #
    # The ARMED predicate lives in `_require_dpidr_gate`, written as the
    # negation of the advisory's own condition rather than as a second reading
    # of `flash_args`, so "armed" and "unarmed" can never disagree. On the
    # openocd/pyocd arm it is False unconditionally -- an armed preflight is
    # impossible there (`plan_swd_probe` refuses `expect_dpidr` on that arm at
    # plan time), which is why the refusal text branches on the arm rather
    # than pretending a manifest edit alone would fix it.
    #
    # tan-cli#609: scoped by coverage, not by `method == SWD_PROBE_METHOD`.
    # For Flow D this is a NO-OP by the time control reaches here -- its own
    # call site above (ahead of the SETOOLS auto-sign) fires on the identical
    # condition and returns -- and it is kept unconditional anyway so that a
    # future covered method reaching only this site is still gated.
    if ctx.require_dpidr:
        refusal = _require_dpidr_gate(method, entry_id, flash_args, plan.preflight_device)
        if refusal is not None:
            lines.append(f"  FAIL: {refusal}")
            return 1, entry(method, "failed", 1, refusal), lines
    if swd_probe_took_jlink_arm:
        refusal = _flow_d_preflight(
            inputs, ctx.venv_bin, ctx.workspace, method=method,
            read_device=plan.preflight_device,
        )
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
        if method == FLOW_D_METHOD:
            ok_message = _flow_d_reset_qualified_message(ok_message, outcome)
        # tan-cli#540: the same intent-vs-observed shape #522 closed for Flow
        # D. Gated on `swd_probe_took_jlink_arm` (already computed above for
        # the DPIDR preflight, so the two can never drift): the halt phrases
        # are JLinkExe's, and the openocd/pyocd arm neither emits them nor
        # makes either of the two claims this qualifies.
        #
        # WHICH qualification, and whether the write is left unconfirmed at
        # all, is decided by the pure `_swd_probe_qualified_message` off the
        # claim the plan composed -- a `.bin` write now carries a `verifybin`
        # read-back and is confirmed even when the core refused to halt (only
        # its reset is in doubt); an ELF/HEX load still cannot be verified at
        # all. Neither decision is re-derived here.
        #
        # tan-cli#590: the SAME transcript is now partitioned BOTH ways, on
        # the one boundary `_jlink_load_completed_at` finds -- pre-load
        # markers speak to the write (unchanged), post-load ones to the
        # default `r`/`g` that #575 correctly removed from the write verdict
        # and then dropped entirely. Both lists are computed under the same
        # `swd_probe_took_jlink_arm` gate, off the same `outcome`, so they
        # cannot disagree about which stage a marker belongs to.
        halt_markers = _swd_probe_halt_markers(outcome) if swd_probe_took_jlink_arm else []
        post_load_markers = (
            _swd_probe_post_load_halt_markers(outcome) if swd_probe_took_jlink_arm else []
        )
        ok_message, write_unconfirmed = _swd_probe_qualified_message(
            ok_message, halt_markers, post_load_markers
        )
        lines.append(f"  ok: {ok_message}")
        return (
            0,
            entry(
                method,
                "ok",
                0,
                ok_message,
                preflight_unarmed=preflight_unarmed,
                write_unconfirmed=write_unconfirmed,
            ),
            lines,
        )
    msg = _execute_message(outcome, method, entry_id)
    lines.append(f"  FAIL: {msg}")
    return 1, entry(method, "failed", 1, msg), lines


def _flow_d_preflight(
    inputs: FlashInputs,
    venv_bin: Path | None = None,
    workspace: str | None = None,
    method: str = FLOW_D_METHOD,
    *,
    read_device: str | None = None,
) -> str | None:
    """Connect read-only with the manifest's ATTACH device profile and confirm
    the SW-DP IDR before any write. Returns a refusal message, or `None`
    to proceed.

    Shared by Flow D (`alif_mram_jlink`, an MRAM write) and `swd_probe`'s
    J-Link arm (a GD32 bridge write; tan-cli#520) -- `method` selects which
    backend a refusal names, and both `method` and `read_device` are threaded
    straight through to `flow_d_preflight_script`/`validate_flow_d_
    preflight_args`, the ONE definition of this preflight's shape. `method`
    defaults to `FLOW_D_METHOD` and `read_device` to `None` so every existing
    Flow D call site is unaffected. `read_device` (`swd_probe` only): the
    ALREADY-RESOLVED write device (`FlashPlan.preflight_device`) -- passing it
    arms the preflight off `flash_args.expect_dpidr` ALONE, since `swd_probe`
    has no separate preflight-only device field the way Flow D's `jlink_
    device` is (see `validate_flow_d_preflight_args`'s `require_device_key`).

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
        prepared = flow_d_preflight_script(inputs, method, read_device=read_device)
    except FlashPlanError as err:
        return str(err)
    if prepared is None:
        return None
    script, expected = prepared
    # tan-cli#520 REVIEW, minor 3: Flow D's refusal text named its write
    # target explicitly ("write MRAM") before this function served a second
    # backend; a bare generalisation to backend-neutral "write" would have
    # SILENTLY changed wording a bench operator reads, with nothing pinning
    # the string to catch it. Branched on `method` instead, so Flow D's exact
    # original wording is byte-for-byte unchanged and `swd_probe` gets its
    # own correct noun (it writes the GD32 bridge's own flash, not MRAM).
    verb = "write MRAM" if method == FLOW_D_METHOD else "write"
    binary = next((n for n in ("JLinkExe", "JLink") if _tool_available(n, venv_bin)), None)
    if binary is None:
        # Unreachable via `_flash_entry`: the tool gate already required
        # JLinkExe/JLink to be available PATH-or-venv (`_tool_available`,
        # same as the probe above), and kept because the alternative to a
        # refusal here would be proceeding to the WRITE with the identity
        # unconfirmed.
        return f"{method}: no J-Link binary on PATH or in the workspace venv for the DPIDR preflight."
    spawned = _programs_resolved_in_venv([binary], venv_bin)
    on_path_bin = venv_bin if spawned != [binary] else None
    # tan-cli#567: and the program is then PINNED to an absolute location via
    # `executable=`, exactly as `_execute` does for the write itself -- this
    # probe is the step that decides WHICH BOARD is about to be written, so a
    # project-supplied `JLinkExe.exe` here would answer the identity question
    # with a value of its own choosing and then hand tan a green light. The
    # child's own `argv[0]` is left as `spawned[0]` for the same reason as in
    # `_execute`: it is what the tool prints in its own banner, which this
    # function then READS. `unresolved` is not reachable via `_flash_entry`
    # (the `binary is None` gate above already required PATH-or-venv
    # availability through the same lookup) but is answered rather than
    # asserted: an `assert` is stripped by `-O`, and the fallback must never be
    # "spawn it bare anyway".
    resolved, unresolved = resolve_program_positions(spawned, _resolution_env(on_path_bin))
    if unresolved is not None:
        return (
            f"{method}: the J-Link binary `{unresolved}` for the DPIDR preflight could "
            "not be resolved to a real location on PATH or in the workspace venv; "
            f"refusing to {verb} without confirming which board is attached."
        )
    # No `-ExitOnError`: a failed connect is the SIGNAL being read here, not an
    # error to abort the probe on.
    outcome = _spawn_jlink([spawned[0], "-NoGui", "1", "-CommanderScript"], script, True,
                           _PREFLIGHT_TIMEOUT_S, on_path_bin, workspace, resolved[0])
    banner = f"{outcome.stdout}\n{outcome.stderr}"
    if _hex_in(expected, banner):
        return None
    if not banner.strip():
        return (
            f"{method}: the read-only DPIDR preflight produced no output "
            f"({outcome.stderr.strip() or 'probe silent'}); refusing to {verb} "
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
            f"{method}: the read-only DPIDR preflight's connect reported no "
            f"SW-DP ID at all (expected {expected}) -- refusing to {verb} to an "
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
        # tan-cli#512, secondary: name the ACTUAL SW-DP ID this connect just read,
        # not only the expected one -- on a bench where a USB serial is CLONED
        # across two physical probes (measured: `603000869` answers both a real
        # AEN E8 at `0x4C013477` and a GD32 bridge at `0x0BE12477`), the actual ID
        # is the single most useful datum for working out which board actually
        # answered. `_dp_id_value` reuses the exact regex `_dp_id_reported` just
        # matched on this same banner, so it cannot fail to find a value here.
        actual = _dp_id_value(banner) or "an ID this preflight could not parse back out"
        return (
            f"{method}: expected SW-DP IDR {expected} was not reported on connect "
            f"-- the probe reported {actual} instead. Refusing to {verb} to an "
            "unidentified board. Check the wiring and "
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
        f"{method}: expected SW-DP IDR {expected} was not reported on connect "
        f"-- refusing to {verb} to an unidentified board. Check the probe "
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
#: firmware/DLL version's phrasing. The hex value is its own capture group
#: (tan-cli#512) so `_dp_id_value` can report what was ACTUALLY read, not only
#: whether something was -- `_dp_id_reported` still just asks whether this
#: matches at all.
_DP_ID_RE = re.compile(r"(?:with\s+ID|DPIDR)\s*:?\s*(0x[0-9A-Fa-f]+)", re.IGNORECASE)

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


def _dp_id_value(banner: str) -> str | None:
    """The actual SW-DP ID text the banner reported, verbatim (whatever hex
    casing/`0x` spelling SEGGER printed), or `None` if `_DP_ID_RE` does not
    match at all. tan-cli#512: a caller that already knows `_dp_id_reported(
    banner)` is `True` gets a non-`None` value here by construction -- both
    read the same regex against the same banner."""
    match = _DP_ID_RE.search(banner)
    return match.group(1) if match else None


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
    recover: bool = False,
    confirm_flag: bool = False,
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
    sdk = (
        SdkInfo.from_resolution(resolved_sdk, sdk_resolution) if resolved_sdk is not None else None
    )
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

    # tan-cli#719: `--confirm` is the documented CLI half of the same gate the
    # env var already armed. OR-ed, not layered: the three spellings are
    # alternatives, and `confirm_gate_note` lists them in this order.
    force_confirm = confirm_flag or os.environ.get("ALP_FLASH_FORCE") == "1"
    # tan-cli#589. Read exactly like `ALP_FLASH_FORCE` one line up -- `== "1"`,
    # not truthiness, so an empty or `0` value is off and the two gates cannot
    # be armed by different spellings of the same intent.
    require_dpidr = os.environ.get(REQUIRE_DPIDR_ENV) == "1"
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
        # warning, not error, and NOT counted into `failed` below. Two shapes
        # land here: `tan build` already decided (via `executionPolicy`) not
        # to build this slice on this host -- e.g. no `bitbake` for a Yocto
        # slice on an MCU-only checkout -- and reported that decision; or the
        # slice is declared `os: "off"` in `board.yaml` (tan-cli#699), for
        # which `executionPolicy` never ran at all -- `tan build` never
        # considers an off core a candidate to build in the first place.
        # Either way, no customer who never asked for that slice to build
        # must see a red `tan flash` over it; the skip stays visible in the
        # envelope instead of being swallowed.
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
        require_dpidr=require_dpidr,
        recover=recover,
        # The RAW `--helper` argument, not a value derived from `plan.targets`:
        # `helper_flash_gate` needs to know the operator NAMED this entry, and a
        # manifest carrying exactly one helper produces the same target list
        # whether or not `--helper` was given (tan-cli#611).
        helper_filter=helper,
    )
    for target in plan.targets:
        rc, entry, lines = _flash_entry(target, ctx)
        text_lines.extend(lines)
        if entry.recovery_armed:
            # tan-cli#611. Emitted BEFORE the entry's own outcome lines are
            # counted, and to both channels for the same reason `flash.dpidr-
            # preflight-unarmed` is: the operator running this does not pass
            # `--format json`, and the extension cannot key off prose. This is
            # not a warning that something went wrong -- the write is the
            # sanctioned bricked-device path -- it is the run stating, in the
            # transcript and in the envelope, that a normally-declined target
            # was authorised, on the one command that writes hardware.
            message = (
                f"{entry.id}: RECOVERY FLASH ARMED (--recover). This helper is "
                "programmed by Alp Lab in production; this path exists only to "
                "recover a bricked device, with Alp Lab-supplied binaries. A "
                "working device is updated over its OTA channel instead."
            )
            text_lines.append(message)
            issues.append(Issue("flash.recovery-flash-armed", "warning", message))
        # A failed entry used to land only in `data.entries[].message`; `issues`
        # is the channel `--format json` consumers key error rendering off, so
        # `ok:false` must never ship with an empty issues list.
        if rc > 0:
            issues.append(Issue("flash.entry-failed", "error", entry.message))
        if entry.status == "planned":
            # `status` alone is prose no automated consumer parses.
            issues.append(Issue("flash.confirm-required", "warning", entry.message))
        if entry.preflight_unarmed:
            # tan-cli#520 REVIEW, design point: `expect_dpidr` stays optional
            # (no shipped preset carries a SW-DP ID for tan to require), but a
            # confirmed `swd_probe` J-Link write that ran with none armed had
            # NO signal at all that its wrong-board guard never ran -- this
            # closes that without making the field mandatory.
            #
            # tan-cli#520 REVIEW round 3, finding 2: appended to `text_lines`
            # too, not only `issues` -- `_run`'s caller prints only
            # `text_lines` in the DEFAULT, non-JSON mode (`--format json` is
            # opt-in), so this warning used to be invisible on a plain
            # `tan flash`: the exact bench operator this preflight exists to
            # protect (a cloned probe serial reaching the wrong board) saw a
            # bare `ok: swd_probe[...] flashed via J-Link` with no hint the
            # wrong-board guard never armed. Every other `flash.*` issue is
            # already text-visible this way -- `flash.confirm-required` rides
            # the entry's own message into `lines` above,
            # `flash.entries-skipped`/`flash.nothing-matched` append to
            # `text_lines` explicitly below -- this one was the outlier.
            #
            # tan-cli#609: the text is composed off `entry.method`, because
            # this warning is no longer `swd_probe`'s alone. Which entries set
            # `preflight_unarmed` is decided in `_flash_entry` off
            # `DPIDR_GUARD_COVERAGE`; nothing about the METHOD is re-decided
            # here.
            message = _dpidr_unarmed_advisory(entry.id, entry.method)
            text_lines.append(message)
            issues.append(Issue("flash.dpidr-preflight-unarmed", "warning", message))
        if entry.write_unconfirmed:
            # tan-cli#540. The entry's own `message` is already qualified
            # (`_swd_probe_unconfirmed_message`), and that message reaches
            # DEFAULT text output via its `ok:` line -- so this is not about
            # visibility, it is about MACHINE-detectability: an `ok` entry
            # whose prose happens to contain a caveat is not something a
            # `--format json` consumer can key off, and `issues` is the
            # channel alp-sdk-vscode renders warnings from. Appended to
            # `text_lines` too, for the same reason `flash.dpidr-preflight-
            # unarmed` is just above: a bench operator does not pass
            # `--format json`, and this is the one line that says what to do
            # about it rather than only what happened.
            message = (
                f"{entry.id}: swd_probe's J-Link write is UNCONFIRMED -- the core did "
                "not halt, and an ELF/HEX load takes `loadfile`, which this backend "
                "has no verifybin for, so JLinkExe's exit code 0 does not mean the "
                "firmware landed. Re-run with the target held in reset, then confirm "
                "the flashed firmware answers. A raw .bin artefact takes the verified "
                "path instead."
            )
            text_lines.append(message)
            issues.append(Issue("flash.swd-probe-write-unconfirmed", "warning", message))
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
        # `TargetPlan.refused_skipped`): the skip was already a decision made
        # before `tan flash` ran, and `flashed_anything` being True there
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

    # tan-cli#719: the confirm gate's own silent-success hole. Every OTHER
    # "nothing was written" shape above already bumps `failed`; a run whose
    # slices all came back `planned` because the gate was not armed did not,
    # so `tan flash && echo flashed` printed `flashed` over an untouched
    # device. `--dry-run` is excluded: that IS an explicit preview request and
    # keeps exiting 0. `flash.confirm-required` is already appended per entry
    # above; this is the run-level verdict those warnings could not reach,
    # because a caller checking `$?` or `ok` never sees `issues[]`.
    #
    # NOT keyed on `flashed_anything`: that flag is set for every entry that
    # reached a backend, INCLUDING a `planned` one (see its `continue` above --
    # only a silent skip leaves it False), so a condition using it could never
    # fire here. Keyed on the absence of any `ok` entry instead: at least one
    # target was planned and none was written. A MIXED run -- one slice written,
    # one planned -- keeps today's exit code and its per-entry
    # `flash.confirm-required` warning; the hole this closes is the run that
    # wrote nothing at all.
    unconfirmed = [e for e in entries if e.get("status") == "planned"]
    wrote_something = any(e.get("status") == "ok" for e in entries)
    if unconfirmed and not wrote_something and not dry_run:
        failed += len(unconfirmed)
        message = (
            f"flash: {len(unconfirmed)} target(s) were planned but not written -- "
            f"the confirm gate is not armed; nothing reached the device. "
            f"{CONFIRM_REMEDY}."
        )
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
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Arm the confirm gate and actually write the device. Without it (and "
        "without ALP_FLASH_FORCE=1 or flash_args.confirm: true) every slice is "
        "previewed, nothing is written, and the run exits non-zero (tan-cli#719).",
    ),
    skip_missing_tools: bool = typer.Option(
        False,
        "--skip-missing-tools",
        help="When a backend's required tools are all absent from PATH, warn + skip "
        "the entry instead of failing it. No effect under --dry-run.",
    ),
    recover: bool = typer.Option(
        False,
        "--recover",
        help="Authorise a recovery flash of a helper MCU that Alp Lab programs in "
        "production (flash_policy: recovery_only). For a BRICKED device only, with "
        "Alp Lab-supplied binaries. Must be combined with --helper NAME: this flag "
        "alone flashes nothing.",
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
            recover=recover,
            capture=json_mode,
            cwd=cwd,
            setools_dir_arg=setools_dir,
            confirm_flag=confirm,
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
