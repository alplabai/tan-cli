# SPDX-License-Identifier: Apache-2.0
"""Dispatch each slice: assemble its env, apply the execution policy's
skip-vs-fail dispositions, resolve `west` from the workspace venv, spawn the
tool, stream its output, and report a per-slice outcome -- never an escaping
exception. Mirrors the dispatch loop in `tan-cli/src/commands/build/execute/
mod.rs`, trimmed to this port's current scope: no `tan build --pristine`
manual override (`force_pristine` in the Rust oracle -- this port's `build`
command has no `--pristine` flag yet, so the automatic stamp comparison is
the only path that can ever fire).
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
[_maybe_pristine_stale_sdk_build_dir]), (tan-cli#309, upstream tan-cli #97)
the Zephyr-boilerplate-loaded guard -- an `os: zephyr` slice that exits 0
without ever loading Zephyr's CMake boilerplate (`tan.commands.build.
manifest.zephyr_boilerplate_loaded`) is reported `failed`, not `ok`, since a
real exit code alone is not evidence the build produced firmware -- and (see
[`last_manifest_write`]) the post-build `system-manifest.yaml` write.

**tan-cli#307, a DELIBERATE divergence from the frozen Rust oracle.**
`crates/` is frozen (`docs/ROADMAP.md`'s standing rule), and the oracle's own
`execute/mod.rs` has the identical bug: `Command::new(&tool).current_dir(&cwd)`
lets `west` resolve its topdir from `cwd` alone, so an unrelated `.west`
ABOVE the project wins over the workspace `tan bootstrap` actually created,
purely because west never consults the workspace tan itself resolved. Fixed
Python-side only -- see [`_pin_west_workspace`].

**tan-cli#510, a second DELIBERATE divergence from the frozen Rust oracle.**
`execute/mod.rs:696`'s spawn is still `Command::new(&tool)` -- the bare
identity, handed straight to the platform's own resolver, whose Windows
search order includes a current-directory step ADR-0020 says an executor
must not consult. This port instead resolves `tool` to an absolute path with
a hardened lookup FIRST ([`_resolve_tool`]) and spawns that -- see this
module's own `_ToolResolution` docstring for why one resolver, not two. Not
back-ported to `crates/`, which ships to nobody (`docs/ROADMAP.md`); fixed
Python-side only.

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
    zephyr_boilerplate_loaded,
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
from tan.core.zephyr_env import zephyr_env_overrides
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

#: tan-cli#336: west's own literal message (`west/util.py::west_topdir`) when
#: neither an ancestor `.west` nor `$ZEPHYR_BASE` resolves a workspace --
#: watched for on a west slice's stdout so a failure carrying it can be
#: re-worded with what tan itself resolved (see [`execute_slices`]'s dispatch
#: loop). Substring match, not the whole line: west prefixes it with
#: "FATAL ERROR: ".
_WEST_NO_WORKSPACE_MSG = "Could not find a west workspace"

#: The Zephyr SDK cross toolchain is absent -- watched for on the same stdout,
#: for the same reason (tan-cli#419). Zephyr's own CMake says
#:
#:     CMake Error at <zephyr>/cmake/modules/FindZephyr-sdk.cmake:160
#:       (find_package): Could not find a package configuration file provided
#:       by "Zephyr-sdk" (requested version 1.0)
#:
#: and the slice then exits 1, so without this the whole diagnosis is a bare
#: `terminated with exit code: 1` plus 4 KB of pass-through CMake text the
#: envelope never names. tan ALREADY HOLDS the answer -- `doctor`'s `zephyrSdk`
#: check reports it with the exact remedy -- so this is not a missing
#: capability, it is one the failing command does not surface. Measured on the
#: same host at the same moment: `doctor --build` said `zephyrSdk -> fail` with
#: the install command, while `build`'s envelope contained neither
#: "zephyr sdk" nor "sdk install".
#:
#: Matched on the package name rather than the `FindZephyr-sdk.cmake` path,
#: which carries the workspace's absolute location and a line number that
#: moves with upstream Zephyr.
_ZEPHYR_SDK_MISSING_MSG = 'provided by "Zephyr-sdk"'


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
    #: The absolute path `_resolve_tool` actually resolved `command.tool` to
    #: -- MAJOR 1 of the tan-cli#510 review. `None` when no tool was ever
    #: resolved (unknown-backend/null-command/missing-tool/cwd/mkdir
    #: refusals, all decided before resolution runs) OR when resolution
    #: succeeded but landed on the SAME string the plan already named
    #: (`tool == resolved`) -- information-free by construction, and the
    #: dominant Zephyr shape once `west_program` has already rewritten
    #: `tool` to the workspace venv's own absolute `west`. Carried as its own
    #: field -- `data.slices[].resolvedTool` -- rather than folded into
    #: `message`: `message` is the source of BOTH `data.slices[].reason`
    #: (`build_cmd.py`) and the persisted `system-manifest.yaml`
    #: `slices[].reason` (alp-sdk `metadata/schemas/system-manifest-v1.
    #: schema.json`, "why a slice was skipped/failed") -- a resolved path is
    #: neither, and stuffing it into `reason` silently added a key to every
    #: green build's envelope and on-disk manifest. See `_text_recap` in
    #: `build_cmd.py` for where this is surfaced in default text (failed and
    #: cancelled slices only -- a success confirms the right tool already,
    #: with nothing left to explain).
    #:
    #: tan-cli#510 review round 3, NIT: `None` here is genuinely AMBIGUOUS for
    #: an already-absolute `command.tool` that then FAILS to launch -- both
    #: "never resolved" (an earlier refusal) and "resolved, identical to the
    #: plan's own `tool`" collapse to the same `null`, and `data.slices[]`
    #: carries no separate `tool` key to disambiguate from the envelope
    #: alone. Deliberate, not an oversight: the motivating case this field
    #: exists for is a MISMATCH (a bare identity landing on a different
    #: binary than the plan named) -- an absolute `tool` that fails is
    #: already fully explained by `message`'s `failed to launch \`<tool>\`:
    #: ...`, which names the identity verbatim, so the suppression rule
    #: (`tool == resolved_tool`) is doing its job rather than hiding
    #: something. A future consumer that needs "which identity did the plan
    #: name" independent of "did it resolve to something different" should
    #: get it as its own additive field (mirroring `resolvedTool`'s own
    #: addition), not by overloading this one's `None` further.
    resolved_tool: str | None = None
    #: The text persisted as `system-manifest.yaml` `slices[].reason`, when it
    #: must differ from `message` -- `None` (the common case) means "use
    #: `message` verbatim". Exists ONLY for the missing-tool refusal
    #: (tan-cli#510 review round 3, MAJOR): `message` there carries `-- searched
    #: PATH: <every entry>` so the CUSTOMER'S OWN TERMINAL shows a fix they can
    #: apply themselves (tan-cli#510's own acceptance bar) -- but that same
    #: string, unredacted, is also what a customer pastes into a support
    #: ticket, and `system-manifest.yaml` is a build ARTEFACT that outlives the
    #: run and gets forwarded. Persisting the full `PATH` there leaks machine
    #: layout (private directory names, sometimes credentials-in-paths) to
    #: whoever the ticket reaches. The short form still answers "why" per the
    #: alp-sdk schema's own field description; the searched detail stays
    #: transient (this run's stdout/envelope only) -- see
    #: [`_write_manifest_after_dispatch`]'s use of this field.
    manifest_message: str | None = None


def _skip_or_fail(
    core_id: str, action: PolicyAction, message: str, *, manifest_message: str | None = None
) -> SliceOutcome:
    status = "skipped" if action is PolicyAction.SKIP else "failed"
    return SliceOutcome(core_id, status, None, message, manifest_message=manifest_message)


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


@dataclass(frozen=True)
class _ToolResolution:
    """One answer to both "is `tool` available" and "where is it" --
    `resolved` is the absolute path to actually spawn (`None` when nothing
    was found), `searched` is always populated, found or not, and describes
    where this looked.

    Replaces the old `_command_on_path`/`_tool_is_available` pair
    (tan-cli#510): that pair answered a bare bool from one hardened lookup,
    and the spawn below then repeated a SEPARATE, unhardened one -- handing
    the bare identity straight to the platform's own resolver, whose Windows
    search order includes a current-directory step the availability check
    was written specifically to exclude. One resolver now; the check and the
    spawn can never again disagree about what ran."""

    resolved: str | None
    searched: str


def _resolve_tool(tool: str, env: dict[str, str]) -> _ToolResolution:
    """Resolve `command.tool` -- an identity per ADR-0020 (never a path in a
    well-formed plan), or an absolute path a caller already resolved
    (`west_program`'s workspace-venv rewrite, `execute_slices` below) -- to
    the absolute path to spawn.

    An absolute `tool` is answered by existence alone: whoever produced it
    already did the searching, and re-walking PATH for something that is
    already a path would be wrong.

    A bare/relative name is walked exactly the way the old
    `_command_on_path` did -- this IS that lookup, now doubling as the
    resolution the spawn itself uses instead of a second, divergent one:
    POSIX via `shutil.which` (no CWD special-case there); Windows via a
    hand-rolled `%PATH%` walk, deliberately NOT `shutil.which`, whose
    stdlib implementation inserts `os.curdir` ahead of every PATH entry
    ("the current directory takes precedence on Windows" -- its own source
    comment), reproducing `CreateProcess`'s native search order -- mirrors
    Rust's `command_on_path` (`crates/tan-cli/src/util.rs`), precisely so a
    project checked out with its own `west.exe`/`openocd.exe` at its root
    can never get spawned in place of the real tool.

    `env` -- MAJOR 2 of the tan-cli#510 review: this used to read
    `os.environ["PATH"]` directly, 46 lines before `execute_slices`
    assembled the slice's OWN `env` (the one the spawn below actually gets),
    so a plan that pinned a different `PATH` in `command.env` resolved
    against the PARENT's PATH and then spawned a DIFFERENT binary than the
    one the check just approved -- the pre-fix `Command::new(&tool)` case
    this whole issue exists to close, reintroduced one call up. Callers pass
    the FULLY ASSEMBLED slice env (post `assemble_slice_env`/
    `with_venv_on_path`), never `os.environ` -- a plan that pins a PATH is
    asking for that PATH to be used, matching pre-fix POSIX `Popen`, which
    always selected via `os.get_exec_path(env)` (the spawn's OWN env), never
    the calling process's.

    `searched` is populated on every return, including a hit: a missingTool
    refusal that can only say "not found" is a support ticket; one that
    names the literal PATH entries this walked is a fix the customer
    applies themselves (tan-cli#510 acceptance)."""
    if Path(tool).is_absolute():
        if Path(tool).exists():
            return _ToolResolution(tool, f"`{tool}` (given as an absolute path)")
        # tan-cli#510 review, minor: the old wording ("searched `<path>`
        # (given as an absolute path)") echoed the same path back as if a
        # search had run one -- nothing was searched, the path was just
        # checked. Naming the miss plainly avoids the tautology.
        return _ToolResolution(None, f"`{tool}` -- that path does not exist")

    if os.name != "nt":
        # `os.get_exec_path(env)` mirrors what a POSIX `Popen(..., env=env)`
        # itself consults to resolve a bare argv[0] (`os.defpath` when `env`
        # carries no `PATH` at all) -- the same fallback `shutil.which`'s own
        # `path=None` default would use from `os.environ`, reproduced here
        # against `env` instead.
        search_dirs = os.get_exec_path(env)
        path = os.pathsep.join(search_dirs)
        return _ToolResolution(shutil.which(tool, path=path), f"PATH: {path}")

    path = env.get("PATH")
    if not path:
        return _ToolResolution(None, "PATH is unset")
    pathext = [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    names = [tool] if Path(tool).suffix else [tool + ext for ext in pathext]
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return _ToolResolution(str(candidate), f"PATH: {path}")
    return _ToolResolution(None, f"PATH: {path}")


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
    sdk_root_path = Path(sdk_root) if sdk_root is not None else None
    workspace_dir = west_workspace_dir(str(build_root), sdk_root_path)
    # tan-cli#308: port of the oracle's `resolve_zephyr_base` -- the
    # workspace's own `zephyr/` checkout, filtered to a real directory so a
    # `workspace_dir` that resolved but was never `west update`d (no
    # `zephyr/` yet) does not hand `west` a `ZEPHYR_BASE` that does not
    # exist. `None` propagates through [`zephyr_env_overrides`] as "nothing
    # to fill", matching every other `workspace_dir` consumer's fallback.
    zephyr_base = workspace_dir / "zephyr" if workspace_dir is not None else None
    if zephyr_base is not None and not zephyr_base.is_dir():
        zephyr_base = None

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

        # tan-cli#308: the zephyr gap-fillers are computed PER SLICE (not
        # once for the whole run, unlike `workspace_dir`/`zephyr_base`
        # themselves) because "plan wins" depends on THIS slice's own
        # `env`/`env_append_path` -- a heterogeneous plan can have one slice
        # that already pins `EXTRA_ZEPHYR_MODULES` (an SDK-emitted plan's
        # `envAppendPath`) alongside one that doesn't, and the caller's
        # `gap_fillers` merge (`assemble_slice_env`) OVERWRITES a key
        # unconditionally -- computing this once, outside the loop, would
        # silently clobber the plan's own richer module list on every slice
        # that DOES pin it.
        #
        # Computed HERE, BEFORE tool resolution -- MAJOR 2 of the tan-cli#510
        # review: `_resolve_tool` used to run on `os.environ` directly, 46
        # lines before this assembly ever built the env the spawn below
        # actually gets, so a plan pinning its own `PATH` in `command.env`
        # was checked against the PARENT's PATH and then spawned against a
        # DIFFERENT one -- an identical plan, run with `env: {"PATH":
        # "<planbin>:..."}` and a different `dualtool` on the parent's own
        # PATH, resolved and reported the PARENT's copy while the CHILD (had
        # it been spawned unresolved, pre-fix) would have run the plan's. A
        # plan that pins `PATH` is asking for that PATH to be used --
        # matching pre-fix POSIX `Popen`, which always selected the spawned
        # binary via `os.get_exec_path(env)` (the SPAWN's own env), never
        # the calling process's.
        slice_gap_fillers = [
            *gap_fillers,
            *zephyr_env_overrides(
                zephyr_base, sdk_root_path, sl.env, sl.env_append_path, env_lookup
            ),
        ]
        # Bound to a name rather than inlined into the `update` call: the
        # tan-cli#336 pop below needs to distinguish a `ZEPHYR_BASE` that
        # something AUTHORITATIVE put here (the plan's own `env`, or
        # tan-cli#308's gap filler) from one merely inherited off
        # `os.environ` -- and after the `update` the merged `env` can no
        # longer tell those apart.
        slice_env = dict(
            assemble_slice_env(sl.env, sl.env_append_path, env_lookup, slice_gap_fillers)
        )
        env = dict(os.environ)
        env.update(slice_env)
        # tan-cli#289/#106: the venv `west` spawns nested `west`/`bitbake`
        # (via `alp_orchestrate`) that resolve purely via PATH -- without
        # this they fail to find `west` exactly like the parent process
        # would have, unless the user activated the venv. A no-op when
        # `tool` did not resolve to an absolute venv path above.
        env = with_venv_on_path(env, tool)

        # tan-cli#510: resolve the identity to an absolute path with the
        # SAME hardened lookup the availability check used to only report a
        # bool for, and spawn THAT -- never the bare `tool` -- below. `tool`
        # itself stays exactly what it was (the plan's identity, or west's
        # venv rewrite just above): `with_venv_on_path` and every message
        # that follows still needs to say what the PLAN named, not what it
        # resolved to, and [`_ToolResolution`]'s own docstring is why a
        # second, divergent resolution must never be written here again.
        # Resolved against THIS slice's own fully assembled `env`
        # (MAJOR 2), not `os.environ` -- see the env-assembly comment above.
        resolution = _resolve_tool(tool, env)
        if resolution.resolved is None:
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "missing_tool", PolicyAction.SKIP),
                    f"tool `{tool}` not found -- searched {resolution.searched}",
                    # tan-cli#510 review round 3, MAJOR: the full searched-PATH
                    # text stays in `message` (this run's stdout + envelope
                    # `reason`) but must NOT reach the persisted
                    # `system-manifest.yaml` -- see [`SliceOutcome.
                    # manifest_message`]'s own docstring.
                    manifest_message=f"tool `{tool}` not found",
                )
            )
            continue
        resolved_tool = resolution.resolved
        # MAJOR 1 of the tan-cli#510 review: `None` (never surfaced) whenever
        # resolution landed on the exact string the plan already named --
        # see [`SliceOutcome.resolved_tool`]'s own docstring.
        outcome_resolved_tool = resolved_tool if resolved_tool != tool else None

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

        if is_west and workspace_dir is not None and "ZEPHYR_BASE" not in slice_env:
            # tan-cli#336: a dangling `$ZEPHYR_BASE` inherited from the
            # ambient shell (seeded above by `dict(os.environ)`) OUTRANKS the
            # workspace tan just resolved -- west's own `set_zephyr_base`
            # (`west/app/main.py`) trusts an already-set `ZEPHYR_BASE`
            # UNCONDITIONALLY over the manifest-derived one, with no
            # existence check, so a stale value is never self-corrected.
            # This is a DIFFERENT hazard than tan-cli#307's, and the cwd pin
            # above does not cover it: the `build` extension's OWN internal
            # `west_topdir(self.source_dir)` call (zephyr's
            # `scripts/west_commands/build.py`) walks from the SLICE'S OWN
            # app directory, not from cwd. An app inside the workspace tree
            # (an SDK-bundled shim) still resolves fine via that ancestor
            # walk regardless of `ZEPHYR_BASE`; the user's own project -- the
            # normal shape `tan init` scaffolds, a SIBLING of the workspace,
            # never nested inside it -- has no ancestor `.west` at all, so
            # that walk falls through to `$ZEPHYR_BASE` and inherits
            # whatever this process saw. Verified against a real dual-core
            # Zephyr SDK slice pair: the SDK-shim core builds either way,
            # the user's-project core fails only when `ZEPHYR_BASE` is
            # stale-but-set.
            #
            # Popping the key (rather than pinning it to a value computed
            # here) lets west's OWN manifest-project lookup re-derive it --
            # exactly what already happens when `ZEPHYR_BASE` is unset
            # (verified: an unset `ZEPHYR_BASE` self-heals via the
            # manifest's "zephyr"-named project, `zephyr.base-prefer`
            # unset).
            #
            # Guarded on `slice_env`, NOT on `sl.env`: tan-cli#308's
            # `zephyr_env_overrides` fills this key as a gap filler for
            # precisely the slices that DON'T pin it themselves, so keying
            # off the plan alone would pop #308's freshly-computed value on
            # every slice #308 exists to serve and leave only the pop's
            # weaker self-heal behind. `slice_env` holds the plan's own env
            # AND the gap fillers merged, so "present in `slice_env`" is
            # exactly "something authoritative decided this" -- and only an
            # ambient, inherited `ZEPHYR_BASE` is dropped. The two fixes
            # compose in that order: #308 supplies the right value whenever
            # the workspace has a real `zephyr/`; #336 removes a stale
            # ambient one for the cases #308 cannot fill (no
            # `workspace_dir`, or a workspace not yet `west update`d).
            env.pop("ZEPHYR_BASE", None)

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

        # tan-cli#336: watch the slice's own stdout for west's literal
        # "could not find a workspace" message so a failure carrying it can
        # be re-worded below with what tan itself resolved -- the bare
        # `terminated with exit code: N` a plain west failure gets otherwise
        # is "the least informative true statement available" (issue #336).
        # `nonlocal` into a fresh binding scoped to THIS iteration (declared
        # inside the loop, not hoisted above it), so a later slice's closure
        # never reads a previous one's leftover.
        saw_no_workspace = False
        saw_no_zephyr_sdk = False

        def _watch_for_no_workspace(line: str) -> None:
            nonlocal saw_no_workspace, saw_no_zephyr_sdk
            if _WEST_NO_WORKSPACE_MSG in line:
                saw_no_workspace = True
            if _ZEPHYR_SDK_MISSING_MSG in line:
                saw_no_zephyr_sdk = True
            on_output(line)

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
                # tan-cli#510: the RESOLVED absolute path, never the bare
                # `tool` identity -- see [`_resolve_tool`]'s docstring for
                # why the availability check above and this spawn must
                # share one resolution rather than two.
                [resolved_tool, *spawn_args],
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
                if _drain_output(proc, _watch_for_no_workspace, cancelled):
                    _terminate(proc)
                    outcomes.append(
                        SliceOutcome(
                            sl.core_id,
                            "cancelled",
                            None,
                            f"slice `{sl.core_id}` cancelled",
                            resolved_tool=outcome_resolved_tool,
                        )
                    )
                    continue
                code = proc.wait()
        except OSError as err:
            # The tool vanished between the availability check above and
            # here, is a directory, lacks the executable bit, or is not a
            # valid executable format -- any of these raise here rather than
            # at the `_resolve_tool` precheck. A failed slice, never a
            # crash. The base message already names both `tool` and
            # `resolved_tool` explicitly (it IS the substance of what failed,
            # not a redundant echo of it) -- `resolved_tool=` below still
            # carries the SAME fact in the dedicated structured field.
            outcomes.append(
                SliceOutcome(
                    sl.core_id,
                    "failed",
                    None,
                    f"failed to launch `{tool}` resolved to `{resolved_tool}`: {err}",
                    resolved_tool=outcome_resolved_tool,
                )
            )
            continue

        status = "succeeded" if code == 0 else "failed"
        if code == 0:
            message = None
        elif is_west and saw_no_workspace and workspace_dir is not None:
            # tan-cli#336: west named no cause beyond its own exit code even
            # though tan was holding a resolved workspace path the whole
            # time -- name it, and the `ZEPHYR_BASE` this spawn actually saw.
            # After tan-cli#308 that value is usually the workspace's own
            # `zephyr/`; "unset" means #308 had nothing to fill and the #336
            # pop above ran. Either way it is the fact that distinguishes
            # "tan pointed west somewhere wrong" from "west never saw what
            # tan resolved". Plain string interpolation, not `!r`: a Windows
            # path's backslashes survive unescaped this way, matching every
            # other path already embedded in this module's messages.
            seen_zephyr_base = env.get("ZEPHYR_BASE") or "unset"
            message = (
                f"slice `{sl.core_id}` terminated with exit code: {code} -- west could not "
                f"find a workspace; tan resolved `{workspace_dir}`, but the spawned process "
                f"saw ZEPHYR_BASE={seen_zephyr_base}"
            )
        elif saw_no_zephyr_sdk:
            # tan-cli#419: name the cause and the remedy tan already knows.
            # `doctor`'s `zephyrSdk` check assembles the same command from the
            # same pin, so the two cannot drift into naming different SDK
            # versions -- imported rather than re-spelled here for exactly the
            # reason `zephyr_sdk_install_command`'s own docstring gives.
            from tan.commands.doctor_cmd import zephyr_sdk_install_command

            message = (
                f"slice `{sl.core_id}` terminated with exit code: {code} -- the Zephyr "
                f"SDK cross toolchain is not installed (CMake could not find the "
                f"`Zephyr-sdk` package). From an initialised west workspace, run "
                f"`{zephyr_sdk_install_command()}`, then re-run the build; "
                f"`tan doctor --build` reports this as `zephyrSdk` with the same fix."
            )
        else:
            message = f"slice `{sl.core_id}` terminated with exit code: {code}"

        # tan-cli#309 (upstream tan-cli #97): a core declared `os: zephyr`
        # whose CMakeLists.txt never calls `find_package(Zephyr ...)` still
        # configures and links fine under `west build -b <board>` (CMake
        # only emits a *dev* warning about the missing `project()` call), so
        # a real exit code 0 is NOT sufficient evidence -- without this the
        # out-of-the-box scaffold was reported `[+] ok` for a plain host
        # binary with no Zephyr in it at all. Checked only on an otherwise-
        # successful slice (a genuine build failure already speaks for
        # itself) and skipped when the slice redirects west's own build dir
        # (`-d`/`--build-dir`), where the evidence lives somewhere this
        # cannot see -- the same refusal `resolve_zephyr_artefact` below
        # already makes.
        if (
            status == "succeeded"
            and sl.backend == "zephyr"
            and not build_dir_overridden(sl.command.args)
            and not zephyr_boilerplate_loaded(cwd)
        ):
            status = "failed"
            message = (
                f"core `{sl.core_id}` is declared `os: zephyr`, but the build in "
                f"`{sl.command.cwd or '.'}` never loaded Zephyr (no ZEPHYR_BASE in its "
                f"CMakeCache.txt and no zephyr/ output) — its CMakeLists.txt must call "
                f"`find_package(Zephyr REQUIRED HINTS $ENV{{ZEPHYR_BASE}})` before `project()`; "
                f"without it CMake builds a plain host binary, not firmware. Scaffold a working "
                f"app with `tan init --template zephyr-app`, or point the core's `app:` at one "
                f"that does."
            )

        # On success, resolve the real on-disk artefact west produced so the
        # post-build manifest points downstream consumers (`run`/`size`/
        # `flash`/`image`) at the elf that exists, not a plan-time guess.
        # Gated on the FINAL `status` (after the guard above), not the raw
        # exit code: a guard-failed slice has no real Zephyr artefact to
        # report even though the tool itself exited 0.
        output_artefact, slice_build_dir = (
            resolve_zephyr_artefact(cwd, sl.command.args) if status == "succeeded" else (None, None)
        )
        outcomes.append(
            SliceOutcome(
                sl.core_id,
                status,
                # A negative POSIX return code means the process died from a
                # signal -- Rust's `ExitStatus::code()` returns `None` for
                # that case (it has no single-integer exit code), so the
                # envelope's `rc` must be null too, not the raw `-N`. Stays
                # the tool's REAL exit code even when the guard above
                # overrode `status` to "failed" -- `west build` really did
                # exit 0, the guard is refusing the RESULT, not the exit.
                None if code < 0 else code,
                message,
                output_artefact,
                slice_build_dir,
                resolved_tool=outcome_resolved_tool,
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
            # tan-cli#510 review round 3, MAJOR: `manifest_message`, when set,
            # is the short form that must land on disk instead of `message`
            # (the missing-tool refusal's `-- searched PATH: <every entry>`
            # leaks the customer's machine layout into a persisted, forwarded
            # artefact -- see [`SliceOutcome.manifest_message`]).
            reason=o.manifest_message if o.manifest_message is not None else o.message,
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
