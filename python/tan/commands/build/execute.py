# SPDX-License-Identifier: Apache-2.0
"""Dispatch each slice: assemble its env, apply the execution policy's
skip-vs-fail dispositions, resolve `west` from the workspace venv, spawn the
tool, stream its output, and report a per-slice outcome -- never an escaping
exception. Mirrors the dispatch loop in `tan-cli/src/commands/build/execute/
mod.rs`, trimmed to this port's current scope: no `tan build --pristine`
manual override (`force_pristine` in the Rust oracle -- this port's `build`
command has no `--pristine` flag yet, so the automatic stamp comparison is
the only path that can ever fire), and no Zephyr-boilerplate-loaded guard.
What IS ported: the unknown-backend / null-command / unsafe-cwd / missing-tool
skip-vs-fail policy and dispatch order, the build-dir-must-exist-before-the-
tool-runs precondition, the `tool == "west"` rewrite to the workspace venv's
own `west` plus the matching PATH prepend (tan-cli#289/#106: `west` normally
lives ONLY inside the bootstrapped venv, and nothing activates it for a
GUI-launched editor -- without this every Zephyr slice skipped with `tool
'west' not found` on exactly the host the VS Code extension runs on; see
`tan.core.venv`), the sdk-switch-pristine guard (issue #52: wipe a slice's
build dir before dispatch when it was configured against a different SDK
root than this run resolved, then re-stamp it -- see
[_maybe_pristine_stale_sdk_build_dir]), and (see [`last_manifest_write`]) the
post-build `system-manifest.yaml` write.

**tan-cli#307, a DELIBERATE divergence from the frozen Rust oracle.**
`crates/` is frozen (`docs/ROADMAP.md`'s standing rule), and the oracle's own
`execute/mod.rs` has the identical bug: `Command::new(&tool).current_dir(&cwd)`
lets `west` resolve its topdir from `cwd` alone, so an unrelated `.west`
ABOVE the project wins over the workspace `tan bootstrap` actually created,
purely because west never consults the workspace tan itself resolved. Fixed
Python-side only -- see [`_pin_west_workspace`].

**How the post-build write reaches `tan run` without a `build_cmd.py` change.**
The Rust oracle's executor (`execute/mod.rs::execute_slices_outcome`) returns
a `NativeBuildOutcome` bundling the dispatch result with the write's two
signals, and `commands::run` consumes that value directly. This port's
`execute_slices` is reached only through `tan.commands.build_cmd._dispatch` /
`_build`, both out of THIS unit's scope (a parallel, disjoint-files
work-split, not an architecture choice) -- so widening `execute_slices`'s
return type would need `_dispatch`'s own unpacking (`iter(execute_slices(...))`
/ `next(dispatched)`) to change too. Instead the write still happens as a
side effect of every `execute_slices` call (so `tan build`'s own CLI
invocation gets the file on disk exactly like `tan run`'s does), and the two
signals are exposed through [`last_manifest_write`] -- a same-process
recorder, not a return value. See that function's docstring for why reading
it is still safe against the R1 staleness defect the Rust oracle's own module
doc records (`decide_run_action` never consults it unless `build_ok`, and
`build_ok` cannot be `True` without `execute_slices` having just run to
completion in the SAME synchronous call)."""
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build.manifest import (
    build_dir_overridden,
    cmake_cache_configured,
    read_sdk_stamp,
    resolve_zephyr_artefact,
    write_post_build_manifest,
    write_sdk_stamp,
)
from tan.commands.build.materialise import MaterialiseError, confine_to_build_root
from tan.core.plan_exec import (
    PolicyAction,
    SdkStampAction,
    assemble_slice_env,
    resolve_action,
    sdk_stamp_action,
    sdk_stamp_key,
)
from tan.core.system_manifest import SliceRunResult
from tan.core.venv import west_program, west_workspace_dir, with_venv_on_path
from tan.envelope import Issue

if os.name != "nt":
    import signal

# metadata/schemas/build-plan-v1.schema.json: slices[].backend enum. Rust's
# `Backend` (tan-core/src/build_plan.rs) matches this exactly, plus a catch-all
# `Unknown(String)` for anything else -- "native" is NOT a legal backend; a
# slice naming it must be refused by `executionPolicy.unknownBackend`
# (default fail), never dispatched.
KNOWN_BACKENDS = frozenset({"zephyr", "yocto", "baremetal"})

#: `tan_core::build_plan::CONSUMER_BUILD_ROOT` -- the top-level dir name a
#: slice's plan-supplied (relative, pre-`confine_to_build_root`) `cwd` must
#: start with before the sdk-switch-pristine guard will touch it. Duplicated
#: rather than imported to avoid a `tan.core.build_plan` <-> here cycle risk;
#: it is the SDK's own build-plan schema constant, not derived from anything
#: in this module.
_CONSUMER_BUILD_ROOT = "build"

# How often the output-draining loop checks `cancelled()` -- bounds
# cancellation latency without spinning the CPU.
_POLL_INTERVAL_S = 0.05
# How long to give a terminated process to exit on its own before escalating
# to a forceful kill.
_TERMINATE_GRACE_S = 1.0

#: Envelope-wire status vocabulary, from `SliceOutcome.status` -- verbatim
#: `tan.commands.build_cmd._WIRE_STATUS` (duplicated rather than imported:
#: `build_cmd.py` already imports this module, so the reverse import would be
#: circular). Also the manifest schema's own `slices[].status` vocabulary
#: (`ok`/`failed`/`skipped`), which is why [`_write_manifest_after_dispatch`]
#: reuses it for the post-build overlay too.
_WIRE_STATUS = {"succeeded": "ok", "skipped": "skipped"}


@dataclass(frozen=True)
class SliceOutcome:
    core_id: str
    status: str  # "succeeded" | "skipped" | "failed" | "cancelled"
    exit_code: int | None
    message: str | None
    #: Real on-disk `zephyr.elf` path after a successful build (absolute),
    #: fed into the post-build manifest so `size`/`renode`/`flash`/`run` find
    #: the artefact west actually produced. `None` for every other status.
    output_artefact: str | None = None
    #: Real on-disk build directory (west's nested `<cwd>/build`, absolute).
    #: `None` for every other status.
    build_dir: str | None = None


def _skip_or_fail(core_id: str, action: PolicyAction, message: str) -> SliceOutcome:
    status = "skipped" if action is PolicyAction.SKIP else "failed"
    return SliceOutcome(core_id, status, None, message)


@dataclass
class _ManifestWriteSignal:
    manifest_written: bool = False
    native_sim_target: bool | None = None
    #: Why the write failed, when it did (`None` on success). A THIRD signal,
    #: kept OUT of [`last_manifest_write`]'s tuple deliberately: that
    #: function's 2-tuple shape is `tan.core.run.decide_run_action`'s own
    #: contract (via `tan.commands.run_cmd`, out of this unit's scope), and
    #: widening it would need that caller's own unpacking to change too.
    #: [`last_manifest_write_failure`] exposes this one separately for
    #: `tan.commands.build_cmd._build`, which folds it into the JSON
    #: envelope's `issues` as the `build.manifest-write-failed` warning --
    #: see that module's own call site.
    write_failed_reason: str | None = None


_last_manifest_write = _ManifestWriteSignal()


def reset_last_manifest_write() -> None:
    """Clear the recorded signal from the most recent [`execute_slices`] call
    in this process. `tan run`'s `_run` calls this immediately before
    invoking the build engine, so a build that never reaches dispatch (an
    early coded refusal, or a monkeypatched stub in a test) reads the honest
    default `(False, None)` below instead of a previous invocation's
    leftover -- see [`last_manifest_write`] for why that default is always
    safe regardless."""
    global _last_manifest_write
    _last_manifest_write = _ManifestWriteSignal()


def last_manifest_write() -> tuple[bool, bool | None]:
    """`(manifest_written, native_sim_target)` from the most recent
    [`execute_slices`] call in this process -- the in-memory signal
    `tan.core.run.decide_run_action` needs, exposed as a same-process
    recorder rather than a return value (see this module's docstring for
    why). Safe against staleness: `decide_run_action` only ever consults
    these two values when `build_ok` is `True`
    (`tan.commands.build_cmd._build`'s own exit code), and `build_ok` can
    only be `True` after `_build` has run [`execute_slices`] to completion in
    THIS SAME synchronous call -- there is no path to a successful build that
    skips dispatch. Pair with [`reset_last_manifest_write`] before a build
    that might not reach dispatch at all."""
    return _last_manifest_write.manifest_written, _last_manifest_write.native_sim_target


def last_manifest_write_failure() -> str | None:
    """Why the most recent [`execute_slices`] call's post-build manifest
    write failed, or `None` on success -- see
    [`_ManifestWriteSignal.write_failed_reason`] for why this is a separate
    accessor rather than a third element of [`last_manifest_write`]'s
    tuple."""
    return _last_manifest_write.write_failed_reason


#: Coded envelope issues from the sdk-switch-pristine guard, one call's
#: worth -- verbatim `crates/tan-cli/.../execute/mod.rs`'s `sdk_switch_issues`,
#: which the Rust oracle folds into the JSON envelope (`build.sdk-switch-
#: pristine` / `build.sdk-switch-pristine-failed`) instead of leaving the
#: wipe stderr-only: the VS Code extension only ever sees the envelope, not
#: `on_output`'s stream. Same same-process-recorder pattern as
#: `_last_manifest_write`, for the same reason (see [`last_sdk_switch_issues`]
#: and this module's own docstring).
_last_sdk_switch_issues: list[Issue] = []


def last_sdk_switch_issues() -> list[Issue]:
    """The `build.sdk-switch-pristine`/`build.sdk-switch-pristine-failed`
    issues recorded by the most recent [`execute_slices`] call in this
    process -- always freshly OVERWRITTEN (never appended-to) on every call,
    so a build with nothing to wipe reads back the honest empty list rather
    than a previous invocation's leftover."""
    return list(_last_sdk_switch_issues)


def _drain_output(
    proc: subprocess.Popen, on_output: Callable[[str], None], cancelled: Callable[[], bool]
) -> bool:
    """Stream `proc`'s stdout to `on_output` line by line, polling
    `cancelled()` every iteration -- BEFORE waiting on the next line, not
    only when none is pending. Returns True iff cancellation was observed.

    Two separate problems, both live here:

    1. A plain `for line in proc.stdout:` blocks on the next `readline()`
       with no way to interleave a check -- a slice that produces no output
       at all (e.g. `time.sleep(60)`) would never yield control back. Fixed
       by reading on a background thread and polling a queue with a timeout
       (Windows anonymous pipes don't support `select`).
    2. Polling `cancelled()` only in the `except queue.Empty:` arm (the
       first cut of this fix) is not enough: a CHATTY child -- `west build`
       driving cmake/ninja is chatty by construction -- keeps the queue
       non-empty forever, so that arm never runs and cancellation is never
       observed. `cancelled()` must be checked unconditionally at the top of
       every iteration, before the queue is touched at all.
    """
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)  # sentinel: stream closed

    threading.Thread(target=reader, daemon=True).start()

    while True:
        if cancelled():
            return True
        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            continue
        if line is None:
            return False
        on_output(line.rstrip("\n"))


def _terminate(proc: subprocess.Popen) -> None:
    """Kill the whole process TREE, not just the direct child.

    `west build` spawns cmake, which spawns ninja/make -- each inherits the
    parent's stdout handle (Python's own `close_fds` default only closes fds
    above 2). Killing just `proc` leaves a grandchild holding the pipe's
    write end open: the reader thread's `for line in proc.stdout` never sees
    EOF and blocks forever, and `Popen.__exit__` (which closes stdout) then
    deadlocks on that same block. `start_new_session=True` at spawn (see
    `execute_slices`) puts the child in its own POSIX process group so
    `killpg` reaches every descendant; Windows has no process groups, so
    `taskkill /T` walks the OS-tracked parent-PID tree instead."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=_TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited between the cancellation check and here
    proc.wait()


def _command_on_path(tool: str) -> bool:
    """PATH-only lookup for a bare/relative tool name -- never the current
    directory. `shutil.which` is correct for this on POSIX (no CWD
    special-case there), but on Windows its stdlib implementation inserts
    `os.curdir` ahead of every PATH entry ("the current directory takes
    precedence on Windows" -- its own source comment), reproducing
    `CreateProcess`'s native search order. Rust's `command_on_path`
    (crates/tan-cli/src/util.rs) deliberately walks `%PATH%` by hand instead,
    precisely so a project checked out with its own `west.exe`/`openocd.exe`
    at its root can never get spawned in place of the real tool -- mirrored
    here rather than left as a Windows-only hole `shutil.which` doesn't
    close."""
    if os.name != "nt":
        return shutil.which(tool) is not None
    path = os.environ.get("PATH")
    if not path:
        return False
    pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    names = [tool] if Path(tool).suffix else [tool + ext for ext in pathext]
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        if any((Path(directory) / name).is_file() for name in names):
            return True
    return False


def _tool_is_available(tool: str) -> bool:
    """Rust's `tool_available` (crates/tan-cli/src/util.rs): an absolute
    path must exist; anything else must resolve on PATH -- never merely
    exist relative to the process's current working directory."""
    if Path(tool).is_absolute():
        return Path(tool).exists()
    return _command_on_path(tool)


def _cwd_under_build_root(raw_cwd: str | None) -> bool:
    """`Path::new(&cmd.cwd).components().next() == CONSUMER_BUILD_ROOT` (Rust
    oracle): checked against the slice's PLAN-supplied relative `cwd` string,
    not the resolved absolute path -- a plan cwd of `src/` (still a legal
    relative path) must not let the wipe target land at
    `<project>/src/build`, which may hold files the build never created."""
    if not raw_cwd:
        return False
    parts = Path(raw_cwd).parts
    return bool(parts) and parts[0] == _CONSUMER_BUILD_ROOT


def _maybe_pristine_stale_sdk_build_dir(
    core_id: str,
    cwd: Path,
    raw_cwd: str | None,
    cmd_args: Sequence[str],
    sdk_stamp_key_str: str | None,
    on_output: Callable[[str], None],
) -> list[Issue]:
    """Sdk-switch-pristine guard (issue #52): a build dir west configured
    against a PREVIOUS `--sdk-root` makes it FATAL ERROR on this one ("please
    clean it, use --pristine, or use --build-dir") -- a real failure that
    otherwise reaches the user as a bare "terminated with exit code: 1".
    Detect it here and wipe west's own nested build dir before the tool runs,
    so the configure that follows is fresh instead of fatal, then re-stamp
    the dir with this run's SDK identity BEFORE the tool is spawned (a
    mid-configure failure still stamped correctly, since the dir really was
    configured against it regardless of whether the build finishes).

    Two guards (mirroring `resolve_zephyr_artefact`'s own refusal to trust a
    build dir it cannot resolve) gate the whole function -- detection, wipe,
    AND the stamp write -- so a `-d`/`--build-dir` override or a cwd outside
    `CONSUMER_BUILD_ROOT` never gets touched. Best-effort throughout: a wipe
    or write failure is reported via `on_output` but never raises -- it
    fails toward a spurious future rebuild, never toward trusting a stale
    build dir or crashing the build. Port of `execute/mod.rs`'s inline guard
    (~line 505) plus `manifest.rs::write_sdk_stamp`'s call site.

    Returns the coded `build.sdk-switch-pristine`/`build.sdk-switch-pristine-
    failed` [`Issue`]s for this slice (verbatim `execute/mod.rs`'s
    `sdk_switch_issues.push`, at `warning` severity like the oracle's) --
    empty when nothing was wiped. `on_output` still gets the same "note: ..."
    text regardless (this port's stderr stream is always-on, unlike the
    oracle's text-mode-only `eprintln!`); the caller folds the returned
    issues into the JSON envelope so the wipe is not stderr-only there."""
    overridden = build_dir_overridden(cmd_args)
    under_build_root = _cwd_under_build_root(raw_cwd)
    if overridden or not under_build_root:
        return []

    issues: list[Issue] = []
    cached = read_sdk_stamp(cwd)
    action = sdk_stamp_action(
        cached, sdk_stamp_key_str, cmake_cache_configured(cwd), overridden, under_build_root
    )
    if action is SdkStampAction.PRISTINE:
        new_root = sdk_stamp_key_str or "?"
        if cached is not None:
            message = (
                f"{core_id}: build dir was configured against SDK root `{cached}`; "
                f"active SDK is `{new_root}` — running pristine"
            )
        else:
            message = (
                f"{core_id}: build dir predates the SDK-switch stamp (no recorded "
                f"SDK root); running pristine against the active SDK `{new_root}`"
            )
        on_output(f"note: {message}")
        issues.append(Issue("build.sdk-switch-pristine", "warning", message))
        try:
            shutil.rmtree(cwd / "build")
        except OSError as err:
            # Best-effort: west's own FATAL ERROR below (if the wipe didn't
            # fully land) at least now ships with a note explaining why.
            failed = f"{core_id}: could not fully wipe the stale build dir: {err}"
            on_output(f"note: {failed}")
            issues.append(Issue("build.sdk-switch-pristine-failed", "warning", failed))

    if sdk_stamp_key_str is not None:
        try:
            write_sdk_stamp(cwd, sdk_stamp_key_str)
        except OSError:
            pass  # best-effort -- see this function's docstring
    return issues


def _pin_west_workspace(
    cwd: Path, args: Sequence[str], workspace_dir: Path | None
) -> tuple[Path, list[str]]:
    """tan-cli#307: `west` resolves its own topdir purely from the SPAWNED
    PROCESS's cwd (`west.util.west_topdir`/`west.app.main.WestApp.run`,
    verified against the real package: no env var and no CLI flag override
    it -- `$ZEPHYR_BASE` is consulted only as a last resort, when NOTHING is
    found walking up from cwd at all) -- entirely independent of the binary
    tan already resolved (`west_program`). An ancestor `.west` above the
    project (a second Zephyr checkout, a vendor SDK, an old workspace) wins
    over the workspace `tan bootstrap` actually created purely because west
    never consults anything tan resolved; tan picking the right binary and
    then letting that binary pick the wrong workspace is exactly tan-cli#307.

    Fixed by making the CHILD's own cwd land inside `workspace_dir` -- but
    that collides with `west build`'s OWN cwd dependency: with no
    `-d`/`--build-dir` in `args` (the plan's normal shape), `west build`
    defaults its build dir to `<cwd>/build`, exactly the location
    [`resolve_zephyr_artefact`] expects afterward. Moving `cwd` to the
    workspace topdir would silently move that output too. Decoupled by
    adding an explicit `-d <original cwd>/build` ourselves before handing
    `cwd` off to the topdir: the effective build location stays identical to
    what `west` would have defaulted to, and `cwd`'s new value only ever
    answers "which workspace is this", never "where do the artefacts land".

    No-op (returns `cwd`/`args` verbatim) when: no workspace resolved
    (`workspace_dir` is `None` -- the caller then keeps behaving exactly as
    before this fix, matching every other `west_workspace_dir` consumer's
    `None` fallback); the command is not `west build` (`args[0] != "build"`
    -- this pin only knows how to preserve `west build`'s own
    default-build-dir rule, not any other verb); or the plan itself already
    redirects the build dir (`build_dir_overridden`) -- that value may
    already be an absolute path west will honour regardless of `cwd`, or a
    relative one this function has no safe way to re-anchor; either way a
    caller-supplied `-d` is left exactly as given, never doubled up."""
    if workspace_dir is None or not args or args[0] != "build" or build_dir_overridden(args):
        return cwd, list(args)
    return workspace_dir, [args[0], "-d", str(cwd / "build"), *args[1:]]


def execute_slices(
    plan,
    *,
    build_root: Path,
    env_lookup: Callable[[str], str | None],
    gap_fillers: Sequence[tuple[str, str]],
    on_output: Callable[[str], None],
    cancelled: Callable[[], bool] = lambda: False,
    sdk_root: str | None = None,
    sdk_root_for_stamp: str | None = None,
    held_outcomes: Sequence[SliceOutcome] = (),
) -> list[SliceOutcome]:
    """Dispatch every slice of `plan` and return one [`SliceOutcome`] per
    slice, in plan order.

    `sdk_root_for_stamp` -- the identity the sdk-switch-pristine guard keys
    its stamp comparison on, when it differs from `sdk_root` (the value the
    post-build manifest write still uses). Defaults to `sdk_root` itself.
    The caller (`tan.commands.build_cmd.build`) passes a NORMALIZED,
    workspace-root-anchored form here (`tan.core.plan_exec.normalize_path`)
    while leaving `sdk_root` raw -- mirroring the oracle's own split between
    `normalized_sdk_root_str` (stamp-only) and the unnormalized
    `resolve_sdk_root` result (`${SDK_ROOT}` token substitution, `sdk`
    envelope reporting, the manifest emit's checkout lookup): a relative
    `--sdk-root ../alp-sdk` must key the SAME as the absolute pointer `tan
    sdk switch` already pinned for the identical checkout (tan-cli#163),
    but must NOT change what `${SDK_ROOT}` substitutes to or what the
    manifest emit resolves.

    `held_outcomes` -- outcomes for slices the CALLER already decided not to
    dispatch (`tan.commands.build_cmd._dispatch` holds back a
    token-substitution-demoted slice before this function ever sees the
    plan, tan-cli #89). They are folded into the post-build manifest overlay
    ([`_write_manifest_after_dispatch`]) alongside this call's own outcomes,
    but are NOT part of the returned list -- the caller already accounts for
    them in its own outcome list and return-value shape must not double them
    up. Omitting a held slice from the overlay would leave its
    `system-manifest.yaml` entry at a previous run's `status`/
    `output_artefact` forever, indistinguishable from a slice that actually
    built (oracle `execute/mod.rs:379-400`)."""
    policy = plan.execution_policy
    outcomes: list[SliceOutcome] = []
    sdk_switch_issues: list[Issue] = []
    # Computed once per build (not per slice): the plan's `sdkCommit` reflects
    # THIS run's freshly emitted plan, not a per-slice fact. `None` when
    # `sdk_root` itself is unresolved -- see `sdk_stamp_key`'s docstring.
    stamp_root = sdk_root_for_stamp if sdk_root_for_stamp is not None else sdk_root
    sdk_stamp_key_str = sdk_stamp_key(stamp_root, plan.sdk_commit)
    # tan-cli#307: resolved ONCE for the whole run (same reasoning as
    # `flash_cmd.py`'s own `workspace_dir` -- see that module's docstring),
    # keyed on `build_root` like every other `west_workspace_dir`/
    # `west_program` call in this function already is. `None` when nothing
    # resolves (CI, an activated venv, the contract harness) -- every west
    # slice below then keeps its old cwd, matching the pre-fix behaviour
    # exactly (see [`_pin_west_workspace`]).
    workspace_dir = west_workspace_dir(
        str(build_root), Path(sdk_root) if sdk_root is not None else None
    )

    for sl in plan.slices:
        if sl.backend not in KNOWN_BACKENDS:
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "unknown_backend", PolicyAction.FAIL),
                    f"unknown backend `{sl.backend}`",
                )
            )
            continue

        if sl.command is None:
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "null_command", PolicyAction.SKIP),
                    f"slice `{sl.core_id}` has no command",
                )
            )
            continue

        # Dispatch order below matches the Rust oracle (execute/mod.rs):
        # unsafe-cwd, then create_dir_all, THEN the missing-tool skip/fail --
        # an escaping cwd is a plan defect and must stay loud even when the
        # tool also happens to be missing, not get silently absorbed into
        # missingTool's default-skip.

        # Plans are trusted input, but writes (and here, the working
        # directory the tool runs in) stay confined under `build_root` --
        # same guard `materialise_plan` applies to artefact paths.
        try:
            cwd = (
                confine_to_build_root(build_root, sl.command.cwd)
                if sl.command.cwd
                else build_root
            )
        except MaterialiseError as err:
            outcomes.append(SliceOutcome(sl.core_id, "failed", None, err.message))
            continue

        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            outcomes.append(
                SliceOutcome(
                    sl.core_id, "failed", None, f"cannot create build dir `{cwd}`: {err}"
                )
            )
            continue

        tool = sl.command.tool
        is_west = tool == "west"
        if is_west:
            # tan-cli#289/#106: rewrite a bare `"west"` to the west-capable
            # workspace venv's own binary -- an explicit different tool the
            # plan named (an absolute path, or anything not literally
            # `"west"`) is never touched, matching the Rust oracle's own
            # `cmd.tool == "west"` exact-match precedence.
            tool = west_program(str(build_root), sdk_root)
        if not _tool_is_available(tool):
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "missing_tool", PolicyAction.SKIP),
                    f"tool `{tool}` not found",
                )
            )
            continue

        # Deliberately AFTER the missing-tool skip above (not right after
        # `cwd.mkdir`): the wipe is destructive, and the tool-availability
        # check is the only thing standing between "this slice is about to
        # be rebuilt" and "this slice is about to be skipped" -- running the
        # wipe first would delete the last good `zephyr.elf` for a rebuild
        # that then never happens on a host missing `west`.
        sdk_switch_issues.extend(
            _maybe_pristine_stale_sdk_build_dir(
                sl.core_id, cwd, sl.command.cwd, sl.command.args, sdk_stamp_key_str, on_output
            )
        )

        env = dict(os.environ)
        env.update(dict(assemble_slice_env(sl.env, sl.env_append_path, env_lookup, gap_fillers)))
        # tan-cli#289/#106: the venv `west` spawns nested `west`/`bitbake`
        # (via `alp_orchestrate`) that resolve purely via PATH -- without
        # this they fail to find `west` exactly like the parent process
        # would have, unless the user activated the venv. A no-op when
        # `tool` did not resolve to an absolute venv path above.
        env = with_venv_on_path(env, tool)

        # tan-cli#307: pin `west build` to the workspace tan resolved rather
        # than letting west infer one from `cwd` -- see [`_pin_west_workspace`].
        # Every OTHER use of `cwd` in this function (the pristine guard just
        # above, `resolve_zephyr_artefact` below) deliberately still reads
        # the ORIGINAL `cwd`/`sl.command.args`, not these: this pin is a
        # spawn-time-only concern, not a change to where the build dir
        # itself lives on disk.
        spawn_cwd, spawn_args = (
            _pin_west_workspace(cwd, sl.command.args, workspace_dir)
            if is_west
            else (cwd, list(sl.command.args))
        )

        try:
            # A context manager: closes stdout/stderr/stdin on every exit
            # path (success, cancellation, exception) -- a bare `Popen(...)`
            # here leaked the pipe's file object (a `ResourceWarning` under
            # `-W error::ResourceWarning`, and a real fd leak across a long
            # multi-slice run).
            #
            # start_new_session=True (POSIX: setsid) puts the child in its
            # own process group so `_terminate` can `killpg` the whole tree
            # instead of only the direct child; a documented no-op on
            # Windows (subprocess.py's Windows `_execute_child` takes and
            # ignores it), where `_terminate` uses `taskkill /T` instead.
            with subprocess.Popen(
                [tool, *spawn_args],
                cwd=str(spawn_cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            ) as proc:
                if _drain_output(proc, on_output, cancelled):
                    _terminate(proc)
                    outcomes.append(
                        SliceOutcome(
                            sl.core_id, "cancelled", None, f"slice `{sl.core_id}` cancelled"
                        )
                    )
                    continue
                code = proc.wait()
        except OSError as err:
            # The tool vanished between the availability check above and
            # here, is a directory, lacks the executable bit, or is not a
            # valid executable format -- any of these raise here rather than
            # at the `_tool_is_available` precheck. A failed slice, never a
            # crash.
            outcomes.append(
                SliceOutcome(sl.core_id, "failed", None, f"failed to launch `{tool}`: {err}")
            )
            continue

        # On success, resolve the real on-disk artefact west produced so the
        # post-build manifest points downstream consumers (`run`/`size`/
        # `flash`/`image`) at the elf that exists, not a plan-time guess.
        output_artefact, slice_build_dir = (
            resolve_zephyr_artefact(cwd, sl.command.args) if code == 0 else (None, None)
        )
        outcomes.append(
            SliceOutcome(
                sl.core_id,
                "succeeded" if code == 0 else "failed",
                # A negative POSIX return code means the process died from a
                # signal -- Rust's `ExitStatus::code()` returns `None` for
                # that case (it has no single-integer exit code), so the
                # envelope's `rc` must be null too, not the raw `-N`.
                None if code < 0 else code,
                None if code == 0 else f"slice `{sl.core_id}` terminated with exit code: {code}",
                output_artefact,
                slice_build_dir,
            )
        )

    global _last_sdk_switch_issues
    _last_sdk_switch_issues = sdk_switch_issues

    _write_manifest_after_dispatch(plan, build_root, [*outcomes, *held_outcomes], on_output, sdk_root)
    return outcomes


def _write_manifest_after_dispatch(
    plan,
    build_root: Path,
    outcomes: list[SliceOutcome],
    on_output: Callable[[str], None],
    sdk_root: str | None,
) -> None:
    """The output seam: write the post-build `system-manifest.yaml` (the
    contract `tan run`/`flash`/`size`/`image` read) reflecting this run's
    per-slice status, and record the in-memory signals `tan run`/
    `tan build` decide from -- see [`last_manifest_write`] and
    [`last_manifest_write_failure`]. Always attempted, even for zero slices,
    mirroring the Rust oracle's `execute_slices_outcome` (which calls
    `write_post_build_manifest` unconditionally after its dispatch loop, not
    only when at least one slice ran). `outcomes` here is the FULL set
    overlaid onto the manifest -- dispatched slices plus any the caller
    held back (see [`execute_slices`]'s `held_outcomes` parameter) -- not
    necessarily what [`execute_slices`] itself returns."""
    results = [
        SliceRunResult(
            core_id=o.core_id,
            status=_WIRE_STATUS.get(o.status, "failed"),
            output_artefact=o.output_artefact,
            build_dir=o.build_dir,
            reason=o.message,
        )
        for o in outcomes
    ]
    outcome = write_post_build_manifest(
        sdk_root=sdk_root,
        board_yaml=plan.board_yaml,
        base=str(build_root),
        plan_build_root=plan.build_root,
        results=results,
    )
    global _last_manifest_write
    _last_manifest_write = _ManifestWriteSignal(
        manifest_written=outcome.write_failed_reason is None,
        native_sim_target=outcome.native_sim_target,
        write_failed_reason=outcome.write_failed_reason,
    )
    if outcome.write_failed_reason is not None:
        # Not silent -- mirrors the Rust oracle's identical `eprintln!` (via
        # `on_output`, this port's stderr-streaming callback: this function
        # has no envelope/issues list of its own to fold a warning `Issue`
        # into). `tan.commands.build_cmd._build` folds the SAME reason into
        # the JSON envelope's `issues` as `build.manifest-write-failed`
        # (`last_manifest_write_failure()`, see that module's own call
        # site) -- so the envelope-side half this docstring used to call
        # "out of this unit's scope" is covered too; this line is the
        # text-mode/stderr half only. Unlike the oracle (`execute/mod.rs`
        # gates its `eprintln!` on `text_mode`), this call fires in BOTH
        # `--format text` and `--format json` -- the same deliberate,
        # port-wide "stderr is always-on, never mode-gated" convention
        # `_maybe_pristine_stale_sdk_build_dir`'s docstring documents for
        # the sibling sdk-switch-pristine note; harmless since stdout, not
        # stderr, is the JSON envelope's only channel.
        on_output(f"note: skipped writing system-manifest.yaml — {outcome.write_failed_reason}")
