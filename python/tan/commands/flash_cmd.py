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
`TBD` in `flash_args`), `>0` failed. `failed` counts only `rc > 0`; skipped
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

**Known port gaps, both inherited and both deliberate.** The Rust resolves a
workspace venv (`venv_bin_dir`, so a GUI-launched editor's PATH-less `west` is
still found) and the west workspace topdir (`west_workspace_dir`, which becomes
each child's cwd so `west flash` can see alp-sdk's out-of-tree runners). This
port has no venv-resolution module -- the same gap
`tan.commands.build.execute` documents for `with_venv_on_path` -- so a
`zephyr_west_flash` entry on such a host fails the tool gate here where Rust
succeeds. Fixing it is one shared module for both commands, not a second copy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import typer

from tan.commands.build_cmd import discover_sdk_root
from tan.commands.doctor_cmd import on_path
from tan.core.flash_plan import (
    FAIL,
    FLOW_D_METHOD,
    PIPE,
    SKIP,
    FlashInputs,
    FlashPlan,
    FlashPlanError,
    FlashTarget,
    ManifestError,
    backend_for,
    display_argv,
    fa_str,
    fa_str_checked,
    flash_args_has_tbd,
    flow_d_preflight_script,
    parse_atoc_start_address,
    parse_system_manifest,
    plan_flash_targets,
    registry_keys_debug,
    resolve_artefact_path,
    select_flash_method,
    tool_gate,
)
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

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

    `board.yaml`'s existence is NOT checked, matching
    `project.rs::resolve_board_yaml_path`, which joins the configured relative
    path onto the workspace root unconditionally -- the field names where one
    WOULD live. Verified: the oracle reports `<cwd>/board.yaml` from a scratch
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
    return Project(
        root=resolved_root.replace("\\", "/"), board_yaml=resolved.replace("\\", "/")
    )


def _resolve_sdk(sdk_root: str | None, workspace_root: str) -> tuple[str | None, str | None]:
    """`(sdk_root, sourceTier)` -- `util.rs::resolve_sdk_root`, trimmed to the two
    tiers this port can honour.

    `--sdk-root` is TERMINAL and returned AS GIVEN when it holds the loader
    marker, else the whole command fails (I-31): a bad path must fail loudly
    rather than silently fall through to a lower tier and build/flash against a
    different SDK. The `tan sdk switch` workspace pin and the machine-global
    default are skipped for the reason `build_cmd.discover_sdk_root` records --
    this port has no writer for either, and half a precedence chain that quietly
    picks the wrong checkout is worse than an honest `--sdk-root`."""
    if sdk_root is not None:
        return (sdk_root if _is_sdk_root(sdk_root) else None), "sdkRootFlag"
    found = discover_sdk_root_safe(workspace_root)
    return found, "discovery"


def _is_sdk_root(path: str) -> bool:
    """`util.rs::has_loader_script`. `os.path.isfile` swallows its own
    `OSError`/`ValueError`, so a path with an embedded NUL or a permission-denied
    parent reads as "not an SDK root" rather than raising out of the guard."""
    try:
        return os.path.isfile(os.path.join(path, "scripts", "alp_project.py"))
    except (OSError, ValueError):
        return False


def discover_sdk_root_safe(workspace_root: str) -> str | None:
    """`build_cmd.discover_sdk_root`, with its filesystem walk made incapable of
    raising -- an unreadable ancestor on the walk must not become a traceback in
    a command whose whole job is to report an envelope."""
    try:
        found = discover_sdk_root(_as_path(workspace_root))
    except (OSError, ValueError):
        return None
    return str(found) if found is not None else None


def _as_path(text: str):
    from pathlib import Path  # noqa: PLC0415 -- one call site, keeps the import local

    return Path(text)


def _tool_available(tool: str) -> bool:
    """A tool counts as available when it is on PATH. `doctor_cmd.on_path` walks
    `$PATH` by hand rather than using `shutil.which`, which on Windows probes the
    CURRENT DIRECTORY first -- a project checked out with its own `openocd.exe`
    at its root would otherwise be reported as this host's tooling and then
    SPAWNED against attached silicon."""
    try:
        return on_path(tool) is not None
    except (OSError, ValueError):
        return False


# ── spawning ────────────────────────────────────────────────────────────────


def _spawn(argv, capture: bool, timeout: float) -> _Outcome:
    """One process. Captured in JSON mode (the output is kept for the failure
    message and never re-spawned), inherited-to-stderr in text mode so a long
    write streams live.

    In text mode the child's stdout is redirected to **stderr**, not inherited:
    stdout is the envelope channel for this process even when this run is not
    using it, and a flash tool that prints to stdout would otherwise put
    non-envelope bytes there. Rust can inherit safely because its text path
    never writes an envelope at all; here the same process object owns both.
    """
    try:
        if capture:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
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
            )
            if proc.stdout:
                print(proc.stdout, end="", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr)
            return _Outcome(success=proc.returncode == 0, returncode=proc.returncode)
        proc = subprocess.run(list(argv), stdout=sink, timeout=timeout)
        return _Outcome(success=proc.returncode == 0, returncode=proc.returncode)
    except subprocess.TimeoutExpired:
        return _Outcome(
            success=False,
            stderr=f"timed out after {timeout:.0f}s and was killed",
            captured=capture,
        )
    except OSError as err:
        # The tool vanished between the gate and the spawn, is a DIRECTORY, or
        # is not executable. All three are ordinary host states, not tan bugs,
        # so they become a failed entry rather than reaching the outer guard.
        return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)


def _stderr_sink():
    """`sys.stderr` when it has a real OS handle a child can inherit, else `None`.

    **A DELIBERATE divergence from the oracle**, and the only one in this file.
    Rust's text path calls `cmd.status()`, which INHERITS stdio, so a flash tool's
    stdout lands on tan's stdout. Here a child's stdout is routed to STDERR
    instead. Both are safe today -- Rust's text mode writes nothing to stdout
    either (`main.rs::emit` uses `eprintln!`) -- but in this process stdout is the
    envelope channel and the redirect makes that unconditional rather than true
    only as long as nobody adds a stdout write to the text path. Visible only to a
    caller doing `tan flash > log` in TEXT mode; `--format json` captures on both
    sides and is byte-identical (43 diffed cases).
    """
    try:
        sys.stderr.fileno()
    except (OSError, ValueError, AttributeError):
        return None
    return sys.stderr


def _spawn_pipeline(left, right, capture: bool, timeout: float) -> _Outcome:
    """A decompress -> dd pipeline: wire the decompressor's stdout into dd's
    stdin. Fails when EITHER process fails, matching the Python rc folding.

    The decompressor's stderr is drained on a background thread for the
    pipeline's lifetime. Creating the pipe without reading it is a silent hang
    mid-write to a real block device: once the decompressor writes more than the
    OS pipe buffer its `write()` blocks forever, it never reaches EOF on stdout,
    dd's `read()` blocks too, and the `wait()` never returns.
    """
    deadline = time.monotonic() + timeout
    try:
        first = subprocess.Popen(  # noqa: S603 -- argv comes from the pure planner
            list(left),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture else None,
        )
    except OSError as err:
        return _Outcome(success=False, stderr=f"could not spawn: {err}", captured=capture)

    drained: list[bytes] = []
    drain: threading.Thread | None = None
    if first.stderr is not None:
        stream = first.stderr

        def _drain() -> None:
            try:
                drained.append(stream.read() or b"")
            except (OSError, ValueError):
                pass

        drain = threading.Thread(target=_drain, daemon=True)
        drain.start()

    try:
        try:
            second = subprocess.Popen(  # noqa: S603 -- as above
                list(right),
                stdin=first.stdout,
                stdout=subprocess.PIPE if capture else _stderr_sink(),
                stderr=subprocess.PIPE if capture else None,
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
            return _Outcome(
                success=False,
                stderr=f"timed out after {timeout:.0f}s and was killed",
                captured=capture,
            )
        try:
            left_ok = first.wait(timeout=max(1.0, deadline - time.monotonic())) == 0
        except subprocess.TimeoutExpired:
            _terminate(first)
            left_ok = False
        return _Outcome(
            success=(second.returncode == 0) and left_ok,
            stdout=_text(out),
            stderr=_text(err_text),
            returncode=second.returncode if second.returncode is not None else -1,
            captured=capture,
        )
    finally:
        _terminate(first)
        if drain is not None:
            drain.join(timeout=2.0)


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


def _spawn_jlink(argv, script: str, capture: bool, timeout: float) -> _Outcome:
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
        return _spawn([*argv, path], capture, timeout)
    finally:
        _unlink(path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _execute(plan: FlashPlan, capture: bool) -> _Outcome:
    """Spawn the plan: a pipeline (a `"|"` token), a J-Link plan (temp Commander
    script), or a plain single process."""
    argv = list(plan.argv)
    if PIPE in argv:
        cut = argv.index(PIPE)
        return _spawn_pipeline(argv[:cut], argv[cut + 1 :], capture, _FLASH_TIMEOUT_S)
    if plan.jlink_script is not None:
        return _spawn_jlink(argv, plan.jlink_script, capture, _FLASH_TIMEOUT_S)
    return _spawn(argv, capture, _FLASH_TIMEOUT_S)


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
    rc-style summary."""
    if outcome.captured:
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


def _flash_entry(target: FlashTarget, ctx: _Context) -> tuple[int, _Entry, list[str]]:
    """Dispatch + run one target. Returns `(rc, entry, text-lines)`."""
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

    artefact = target.output_artefact or target.firmware_path or ""
    if not artefact:
        if not ctx.dry_run:
            msg = f"flash: {kind} '{entry_id}' has no output_artefact / firmware_path; can't flash."
            lines.append(msg)
            return 1, entry(method, "failed", 1, msg), lines
        artefact = f"<missing-artefact-for-{entry_id}>"
    artefact_path = resolve_artefact_path(artefact, ctx.build_root, ctx.sdk_root, _is_file)

    gate = tool_gate(
        meta.requires, ctx.dry_run, ctx.skip_missing_tools, kind, entry_id, method,
        _tool_available,
    )
    if gate.outcome == SKIP:
        lines.append(gate.message)
        return -1, entry(method, "skipped", -1, gate.message), lines
    if gate.outcome == FAIL:
        lines.append(gate.message)
        return 1, entry(method, "failed", 1, gate.message), lines

    flash_args = target.flash_args
    if method == FLOW_D_METHOD:
        # The one place `flash_args` is augmented before dispatch: the ATOC
        # address is a build-time output, so it may need resolving from a
        # build artefact rather than arriving on the manifest already. See
        # `_resolve_flow_d_atoc_address`. A supplied-but-unusable `atoc_map`
        # raises there rather than silently deferring to `plan_alif_mram_jlink`'s
        # generic refusal -- caught here the same way `meta.build`'s is below.
        try:
            flash_args = _resolve_flow_d_atoc_address(flash_args, ctx.build_root, ctx.sdk_root)
        except FlashPlanError as err:
            msg = str(err)
            lines.append(f"flash: {kind} '{entry_id}' -> {method}")
            lines.append(f"  FAIL: {msg}")
            return 1, entry(method, "failed", 1, msg), lines

    inputs = FlashInputs(
        artefact=artefact_path,
        flash_args=flash_args,
        core_id=entry_id,
        sku=ctx.sku,
        dry_run=ctx.dry_run,
        force_confirm=ctx.force_confirm,
    )
    try:
        plan = meta.build(inputs, _tool_available)
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
        refusal = _flow_d_preflight(inputs)
        if refusal is not None:
            lines.append(f"  FAIL: {refusal}")
            return 1, entry(method, "failed", 1, refusal), lines

    outcome = _execute(plan, ctx.capture)
    if outcome.success:
        lines.append(f"  ok: {plan.ok_message}")
        return 0, entry(method, "ok", 0, plan.ok_message), lines
    msg = _execute_message(outcome, method, entry_id)
    lines.append(f"  FAIL: {msg}")
    return 1, entry(method, "failed", 1, msg), lines


def _flow_d_preflight(inputs: FlashInputs) -> str | None:
    """Connect read-only with the manifest's ATTACH device profile and confirm
    the SW-DP IDR before any MRAM write. Returns a refusal message, or `None`
    to proceed.

    ABSENT-BY-DEFAULT, on purpose: a manifest that declares no `expect_dpidr`
    (or no attach-profile `jlink_device`) gets no preflight, because tan has no
    hardware knowledge to supply either value and a wrong expected ID would
    refuse every good board. Both come from `flash_args`.

    Capture is forced on regardless of output mode: the whole point is to READ
    the connect banner, and letting it stream would both lose the value and put
    probe output in the transcript ahead of the decision it drives.
    """
    try:
        prepared = flow_d_preflight_script(inputs)
    except FlashPlanError as err:
        return str(err)
    if prepared is None:
        return None
    script, expected = prepared
    binary = next((n for n in ("JLinkExe", "JLink") if _tool_available(n)), None)
    if binary is None:
        # Unreachable via `_flash_entry` (the tool gate already required one),
        # kept because the alternative to a refusal here would be proceeding to
        # the WRITE with the identity unconfirmed.
        return f"{FLOW_D_METHOD}: no J-Link binary on PATH for the DPIDR preflight."
    # No `-ExitOnError`: a failed connect is the SIGNAL being read here, not an
    # error to abort the probe on.
    outcome = _spawn_jlink([binary, "-NoGui", "1", "-CommanderScript"], script, True,
                           _PREFLIGHT_TIMEOUT_S)
    banner = f"{outcome.stdout}\n{outcome.stderr}"
    if _hex_in(expected, banner):
        return None
    if not banner.strip():
        return (
            f"{FLOW_D_METHOD}: the read-only DPIDR preflight produced no output "
            f"({outcome.stderr.strip() or 'probe silent'}); refusing to write MRAM "
            "without confirming which board is attached."
        )
    return (
        f"{FLOW_D_METHOD}: expected SW-DP IDR {expected} was not reported on connect "
        "-- refusing to write MRAM to an unidentified board. Check the probe "
        "selection (flash_args.jlink_serial) and the wiring."
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


def _is_file(path: str) -> bool:
    """`Path::is_file`, incapable of raising -- it is called on manifest-supplied
    strings, which may hold a NUL byte or overlong component."""
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


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
    resolved_sdk, tier = _resolve_sdk(sdk_root_arg, cwd)
    sdk = SdkInfo(resolved_sdk, tier) if resolved_sdk is not None else None
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

    text_lines: list[str] = []
    issues: list[Issue] = []
    entries: list[dict[str, Any]] = []
    # Seeded with the status-refused slices: they never become a target, so they
    # cannot increment `failed` in the loop -- but a slice `tan build` reports
    # non-`ok` must still fail the overall run, not disappear into a clean exit.
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

    ctx = _Context(
        sku=manifest.sku,
        build_root=build_root,
        sdk_root=resolved_sdk,
        dry_run=dry_run,
        skip_missing_tools=skip_missing_tools,
        force_confirm=force_confirm,
        capture=capture,
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

    if not flashed_anything and not plan.refused:
        # A refused slice DID match the requested filters -- it was refused, not
        # absent -- so "nothing matched" would be a misleading second message on
        # top of the flash.slice-not-built issue already pushed above.
        message = "flash: nothing matched the requested filters."
        text_lines.append(message)
        # A `--core`/`--helper` filter matching nothing used to warn only in text
        # mode, so `--format json` reported `ok:true` with empty
        # `entries`/`issues` for a flash that never touched a device.
        issues.append(Issue("flash.nothing-matched", "warning", message))
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
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Program every slice + helper MCU in the project's system manifest."""
    # `--format` is accepted BEFORE the subcommand too (clap makes it
    # `global = true`, so the Rust takes it on either side); the root callback
    # records it and this option overrides it when repeated after the command
    # name. `flash` honours the pre-subcommand position -- and is therefore in
    # `cli._HONOURS_ROOT_FORMAT` -- because refusing it here means a customer's
    # FLASH does not run, on the one command where the fallback (a text-mode run
    # with an empty stdout) would be indistinguishable from a broken device.
    resolved_format = output_format or (ctx.obj or {}).get("format") or "text"
    if resolved_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{resolved_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = resolved_format == "json"

    # Resolved OUTSIDE the guard: `project_obj` is reported on every path
    # including the internal-failure one, and `_resolve_project` is pure string
    # work that cannot raise. The port's most-repeated defect was a helper that
    # throws being called from the exception guard's own recovery path -- so
    # nothing below the guard may compute a field the guard itself needs.
    cwd = workspace_root(project)
    project_obj = _resolve_project(cwd, board_yaml)

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
        )
    except Exception as err:  # noqa: BLE001 -- the whole point of this guard
        # Anything reaching here is a tan bug, and it is reported AS ONE, with an
        # envelope. A raw traceback means an empty stdout and an extension that
        # renders nothing, with no error visible on either side.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = _data("")
        issues = [Issue("flash.internal-failure", "error", f"{type(err).__name__}: {err}")]
        text_lines = ["flash: internal failure"]

    if json_mode:
        emit(Envelope("flash", project_obj, data, issues, exit_code, sdk=sdk))
    else:
        for line in text_lines:
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))
