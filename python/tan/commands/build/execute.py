# SPDX-License-Identifier: Apache-2.0
"""Dispatch each slice: assemble its env, apply the execution policy's
skip-vs-fail dispositions, resolve `west` from the workspace venv, spawn the
tool, stream its output, and report a per-slice outcome -- never an escaping
exception. Mirrors the dispatch loop in `tan-cli/src/commands/build/execute/
mod.rs`. `force_pristine` (`tan build --pristine`, tan-cli#427) is the manual
counterpart to the automatic stamp comparison below: it forces the SAME
[SdkStampAction.PRISTINE] decision the stamp comparison would make on its
own, inside the identical two structural safety guards, and reports
`build.pristine-skipped` (never silent) when one of those guards -- or a
build dir that was never configured at all -- declines the wipe.
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
real exit code alone is not evidence the build produced firmware -- (see
[`last_manifest_write`]) the post-build `system-manifest.yaml` write, and
(tan-cli#550) the plan's per-slice `postCommands` plus the matching
baremetal-evidence guard: an `os: baremetal` slice's `command` is a `cmake -S
... -B .` that only CONFIGURES, so its `cmake --build .` arrives as a post
command ([`_run_post_commands`], with skip-vs-fail taken from the plan's own
`executionPolicy` per the SDK schema and ADR-0001 -- see
[`_missing_post_tool`]) and its linked output is checked against the plan's own
`artifacts.outputDir` ([`_baremetal_artefact_refusal`], whose three
NON-coverages are enumerated there) before the slice may be called built.

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

**tan-cli#567, a third DELIBERATE divergence from the frozen Rust oracle** --
the one above, widened, and recorded here so the register stays complete.
Two things in this file still handed the platform a bare identity after #510.
(1) `_terminate`'s Windows `taskkill`: `CreateProcess` reaches
`%SystemRoot%\\System32` only AFTER the current directory, so a cancelled build
in a project carrying its own `taskkill.exe` ran that one -- now resolved
first, see [`_taskkill_program`]. (2) The lookup itself, on POSIX: an EMPTY
`PATH` entry (`PATH="$PATH:"`, `PATH=":$PATH"` -- both routine) means "the
current directory" to every POSIX consumer, and `shutil.which` duly joins `""`
with the name and probes it relative to cwd. Measured on `dev`,
`_resolve_tool("cwdprobe", {"PATH": ":/nope:"})` answered the RELATIVE string
`'cwdprobe'` for a file that existed only in the process's own working
directory, which `execute_slices` then spawned -- live on this path since #510,
because the Windows branch already filtered empty entries and the POSIX branch
did not. Empty entries are dropped before the walk; the same tree now answers
`None`. See `tan.core.tool_lookup.resolve_tool`, which is where that walk now
lives (this module keeps `_ToolResolution`/`_resolve_tool` as re-export
aliases). Not back-ported to `crates/`; fixed Python-side only.

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

from tan.commands.build.configure_inputs import (
    discover_configure_inputs,
    read_configure_inputs_stamp,
    resolve_zephyr_discovery_dir,
    write_configure_inputs_stamp,
)
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
from tan.commands.build.toolchain import host_scan_has_toolchain, verified_store_dir
from tan.core.plan_exec import (
    CROSS_DRIVE_MSG,
    ExecutionPolicy,
    PolicyAction,
    SdkStampAction,
    assemble_slice_env,
    cross_drive_source_refusal,
    missing_tool_message,
    pristine_suppression,
    resolve_action,
    sdk_stamp_action,
    sdk_stamp_key,
)
from tan.core.subprocess_env import spawn_env
from tan.core.system_manifest import SliceRunResult
from tan.core.tool_lookup import ToolResolution, resolve_tool
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
    #: fed into the post-build manifest so `size`/`flash`/`run` find
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


#: Coded envelope issues from the stale-configure-cache guard (tan-cli#655),
#: one call's worth -- same same-process-recorder pattern as
#: `_last_sdk_switch_issues`, for the same reason: the VS Code extension only
#: ever sees the envelope, not `on_output`'s stream, and a `-U` reset that
#: only ever showed up in stderr would be invisible there.
_last_configure_cache_issues: list[Issue] = []


def last_configure_cache_issues() -> list[Issue]:
    """The `build.configure-cache-reset` issues recorded by the most recent
    [`execute_slices`] call in this process -- always freshly OVERWRITTEN
    (never appended-to), so a build with nothing to reset reads back the
    honest empty list rather than a previous invocation's leftover."""
    return list(_last_configure_cache_issues)


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
    `taskkill /T` walks the OS-tracked parent-PID tree instead.

    **tan-cli#567.** That `taskkill` used to be spawned as a BARE `argv[0]`,
    which is the one place in this file #510 did not reach. `CreateProcess`
    with `lpApplicationName=NULL` searches the parent process's CURRENT
    DIRECTORY *before* the system directories, and the current directory here
    is the customer's project -- so a project carrying its own `taskkill.exe`
    got that binary run, with whatever privileges tan has, the moment a build
    was cancelled. `taskkill` really does live in `%SystemRoot%\\System32`,
    which is on every sane `%PATH%`, so the hardened lookup finds the real one;
    the `%SystemRoot%` fallback covers a stripped `%PATH%`, and a host where
    NEITHER answers falls back to killing the direct child rather than spawning
    a name the current directory could supply. That last case loses the
    grandchildren, which is a worse cleanup but not a worse *hazard* -- and it
    is unreachable on any Windows install that still has System32."""
    if os.name == "nt":
        taskkill = _taskkill_program()
        if taskkill is None:
            proc.kill()
        else:
            subprocess.run(
                [taskkill, "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                env=spawn_env(),
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


def _taskkill_program() -> str | None:
    """The absolute path to Windows' own `taskkill`, or `None`.

    `%PATH%` first, through the same hardened walk every other spawn in this
    port now uses (`os.curdir` is never consulted); then `%SystemRoot%\\System32`
    directly, because a `%PATH%` stripped of the system directories is a real
    (if unusual) CI shape and losing the process-tree kill there would be a
    regression this fix does not need to cause. `None` only when both miss --
    see [`_terminate`] for why that answers with `proc.kill()` rather than a
    bare name."""
    resolved = resolve_tool("taskkill", os.environ).resolved
    if resolved is not None:
        return resolved
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    return str(system32) if system32.is_file() else None


#: tan-cli#567/#532: the ONE hardened lookup, now in `tan.core.tool_lookup`
#: so the flash write path and the size path spawn the SAME resolved path
#: instead of keeping a third and fourth opinion about it. Imported under the
#: private names this module's call sites (and `tests/commands/test_execute.py`)
#: already use -- the move is a relocation, not a rename. See that module's
#: docstring for `_ToolResolution`'s reasoning, which travelled with it.
_ToolResolution = ToolResolution
_resolve_tool = resolve_tool


@dataclass(frozen=True)
class _StepResult:
    """One spawned process's outcome, with its three shapes kept apart.

    Exactly one is ever populated: `exit_code` (the process ran to completion and
    this is its real exit status, negative on a POSIX signal death),
    `cancelled` (the caller's `cancelled()` went true mid-stream and the
    process tree was terminated), or `launch_error` (the spawn itself raised
    `OSError` -- the tool vanished between the availability check and here,
    is a directory, lacks the executable bit, or is not a valid executable
    format). A bare `int | None` return could not tell the second from the
    third."""

    exit_code: int | None = None
    cancelled: bool = False
    launch_error: str | None = None


def _spawn_step(
    program: str, args: Sequence[str], cwd: Path, env: dict[str, str],
    on_line: Callable[[str], None], cancelled: Callable[[], bool],
) -> _StepResult:
    """Spawn ONE already-resolved program, stream its output, and report how
    it ended -- the single spawn implementation shared by a slice's own
    `command` and by each of its `postCommands` steps (tan-cli#550).

    Extracted rather than duplicated for the post-build steps: the fd hygiene,
    the process-group setup and the cancellation handshake below are all
    load-bearing and subtly easy to get wrong a second time -- a post-build
    `cmake --build` that ignored `cancelled()` would keep compiling after the
    user pressed Ctrl-C, with tan already reporting the slice cancelled.

    `program` is always the RESOLVED absolute path (tan-cli#510), never a
    bare identity -- resolution happens in the caller, which is also the only
    place that can report a miss against the right execution policy.

    A context manager, not a bare `Popen(...)`: it closes stdout/stderr/stdin
    on every exit path (success, cancellation, exception) -- a bare `Popen`
    here leaked the pipe's file object (a `ResourceWarning` under
    `-W error::ResourceWarning`, and a real fd leak across a long run).

    `start_new_session=True` (POSIX: setsid) puts the child in its own process
    group so `_terminate` can `killpg` the whole tree instead of only the
    direct child; a documented no-op on Windows (subprocess.py's Windows
    `_execute_child` takes and ignores it), where `_terminate` uses
    `taskkill /T` instead."""
    try:
        with subprocess.Popen(
            [program, *args],
            cwd=str(cwd),
            # tan-cli#992: re-applied at the literal spawn, not trusted from
            # the caller alone -- `spawn_env(base=env)` is idempotent when
            # `env` already went through it (as `execute_slices`'s does), so
            # this is defense-in-depth, not a behaviour change, for the one
            # caller that already gets it right, and a real fix for any
            # future caller that doesn't.
            env=spawn_env(base=env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        ) as proc:
            if _drain_output(proc, on_line, cancelled):
                _terminate(proc)
                return _StepResult(cancelled=True)
            return _StepResult(exit_code=proc.wait())
    except OSError as err:
        return _StepResult(launch_error=str(err))


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
    *,
    force_pristine: bool = False,
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

    `force_pristine` is `tan build --pristine` (tan-cli#163, wired tan-cli#427):
    unconditionally treats the sdk-switch-pristine decision as
    [`SdkStampAction.PRISTINE`] instead of consulting the stamp, for the
    manual case the automatic stamp comparison doesn't (or can't yet) cover.
    It is NOT a second wipe path -- it forces the SAME decision the automatic
    check already makes, still inside the same two structural safety guards
    below, so it still never touches a build dir tan cannot vouch for. Every
    suppression (either guard, or a build dir that was never configured at
    all) is reported as `build.pristine-skipped` rather than silently doing
    nothing (tan-cli#183) -- "pristine" must never silently mean
    "incremental".

    Two guards (mirroring `resolve_zephyr_artefact`'s own refusal to trust a
    build dir it cannot resolve) gate the whole function -- detection, wipe,
    AND the stamp write -- so a `-d`/`--build-dir` override or a cwd outside
    `CONSUMER_BUILD_ROOT` never gets touched. Best-effort throughout: a wipe
    or write failure is reported via `on_output` but never raises -- it
    fails toward a spurious future rebuild, never toward trusting a stale
    build dir or crashing the build. Port of `execute/mod.rs`'s inline guard
    (~line 505) plus `manifest.rs::write_sdk_stamp`'s call site.

    Returns the coded `build.sdk-switch-pristine`/`build.sdk-switch-pristine-
    failed`/`build.pristine-skipped` [`Issue`]s for this slice (verbatim
    `execute/mod.rs`'s `sdk_switch_issues.push`, at `warning` severity like
    the oracle's) -- empty when nothing was wiped and no `--pristine` was
    asked for. `on_output` still gets the same "note: ..." text regardless
    (this port's stderr stream is always-on, unlike the oracle's
    text-mode-only `eprintln!`); the caller folds the returned issues into
    the JSON envelope so the wipe -- or its suppression -- is not
    stderr-only there."""
    overridden = build_dir_overridden(cmd_args)
    under_build_root = _cwd_under_build_root(raw_cwd)
    issues: list[Issue] = []

    # Probed only when `--pristine` was actually passed, so the non-pristine
    # path keeps its existing IO exactly (one `cmake_cache_configured` call
    # below, not two) -- mirrors the oracle's own `pristine_cache_configured`
    # split.
    cache_configured = cmake_cache_configured(cwd) if force_pristine else False
    skipped = pristine_suppression(force_pristine, overridden, under_build_root, cache_configured)
    if skipped is not None:
        message = f"{core_id}: --pristine did NOT wipe the build dir — {skipped.reason()}"
        on_output(f"note: {message}")
        issues.append(Issue("build.pristine-skipped", "warning", message))

    if overridden or not under_build_root:
        return issues

    cached = read_sdk_stamp(cwd)
    if force_pristine:
        # Reached only when the dir WAS configured -- a never-configured one
        # was already reported by `pristine_suppression` above, so this arm
        # needs no message of its own for that case.
        action = SdkStampAction.PRISTINE if cache_configured else SdkStampAction.KEEP
    else:
        action = sdk_stamp_action(
            cached, sdk_stamp_key_str, cmake_cache_configured(cwd), overridden, under_build_root
        )
    if action is SdkStampAction.PRISTINE:
        new_root = sdk_stamp_key_str or "?"
        if force_pristine:
            message = f"{core_id}: --pristine passed; wiping build dir before dispatch"
        elif cached is not None:
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


#: cmake cache-var reset applied by [`_maybe_reset_stale_configure_cache`] --
#: see that function's docstring for why exactly these two and no others.
_CONFIGURE_CACHE_RESET_ARGS = ["-UDTC_OVERLAY_FILE", "-UCONF_FILE"]


def _maybe_reset_stale_configure_cache(
    core_id: str,
    cwd: Path,
    app_dir: str | None,
    backend: str,
    build_root: Path,
    on_output: Callable[[str], None],
) -> tuple[list[str], list[Issue]]:
    """tan-cli#655: a newly-added (or removed) devicetree overlay or Kconfig
    fragment at one of Zephyr's own auto-discovery locations (`app.overlay`,
    `boards/*.overlay`, `socs/*.overlay`, `prj*.conf`, `boards/*.conf`,
    `socs/*.conf`) was silently IGNORED by an already-configured build dir --
    `tan build` reported `ok`, but the generated `zephyr.dts`/`.config` still
    reflected the PREVIOUS overlay/fragment set, and only `tan clean` (a full
    wipe) picked the change up.

    This was NOT a "CMake never reconfigures" bug -- every zephyr slice's
    command already carries at least one `-D` after `--` (`_slice_command`'s
    `-DPython3_EXECUTABLE=${PYTHON}`, always present), so `west build`'s own
    `cmake_opts`-non-empty check (`scripts/west_commands/build.py::do_run`)
    already sets `run_cmake = True` and reconfigures on every `tan build`,
    measured on a real Zephyr 4.4.1 tree, not inferred. The reconfigure was
    ALREADY happening; it just produced a stale result.

    Root cause, also measured against that same tree: Zephyr's own
    `cmake/modules/configuration_files.cmake` resolves `DTC_OVERLAY_FILE`/
    `CONF_FILE` by auto-discovery ONLY `if(NOT DEFINED ...)`, then
    unconditionally CACHES whatever it found -- including "found nothing" --
    as an ordinary CMake cache entry. Every later configure of the SAME
    build dir sees the var already `DEFINED` from the cache and skips
    discovery entirely, so a file that did not exist at the FIRST configure
    stays invisible to every configure after, no matter how many times
    reconfigure runs, until that one cache entry is cleared. An EDIT to an
    already-discovered file is a different, already-solved case: Zephyr ties
    every file it DID discover into `CMAKE_CONFIGURE_DEPENDS`
    (`dts.cmake`/`kconfig.cmake`), so its own mtime bump alone triggers a
    correct reconfigure -- only the SET of discoverable files changing
    defeats the cache.

    The reset is `-UDTC_OVERLAY_FILE -UCONF_FILE` on the configure that
    follows a set change -- deliberately NOT `-UEXTRA_DTC_OVERLAY_FILE`/
    `-UEXTRA_CONF_FILE`: those two are re-resolved via `zephyr_get(...
    MERGE REVERSE)` on every configure regardless of the cache (no `NOT
    DEFINED` guard gates them), and this slice's own command already ends
    with `-DEXTRA_CONF_FILE=<build_dir>/alp.conf` (`_slice_command`'s
    per-core Kconfig wiring) -- appending `-UEXTRA_CONF_FILE` AFTER that in
    the same argv would UNSET it instead (measured: `-D`/`-U` on the same
    cache key apply in argv order), silently dropping the per-core fragment
    tan itself wires in on every reset. Verified this narrower pair alone is
    sufficient AND leaves `-DEXTRA_CONF_FILE` intact: reproduced the #655
    bug against a real Zephyr 4.4.1 `west build` (new `app.overlay`, new
    `boards/<board>.conf`, and a removed overlay all silently/fatally
    ignored pre-fix), then confirmed `-UDTC_OVERLAY_FILE -UCONF_FILE`
    appended after an existing `-DEXTRA_CONF_FILE=...` recovers all three
    while the `-DEXTRA_CONF_FILE` fragment's own content still lands.

    Scoped to `os: zephyr` slices (`DTC_OVERLAY_FILE`/`CONF_FILE` are
    Zephyr-CMake-specific; a yocto/baremetal slice has neither -- and
    `_slice_command`'s zephyr branch is the ONLY place that ever emits one,
    always with `tool: "west"`, so checking `backend` alone is exactly as
    precise as also checking the tool identity, mirroring
    `_maybe_pristine_stale_sdk_build_dir`'s own choice not to gate on tool
    either), and to a build dir CMake has already configured at least once
    (`cmake_cache_configured` -- nothing is cached yet on a first build, so
    there is nothing to reset; this call only lays down the baseline stamp).
    Comparison uses [`tan.commands.build.configure_inputs`]'s existence-only
    fingerprint of the discovery dir's auto-discovery locations, stamped
    inside west's own nested build dir (mirrors the SDK-identity stamp
    above) -- NOT the plan's `command.args`, which name every EXTRA_/-D flag
    tan itself controls but say nothing about a file the CUSTOMER added by
    hand.

    tan-cli#798: `app_dir` is the plan's `appDir` -- the CONFIGURED path,
    NOT the directory `west build` is actually pointed at (see
    `tan.commands.build_cmd`'s "Note `appDir` is NOT the substituted value"
    docstring). For the scaffolded `app: ./src` layout (96 of 105 alp-sdk
    example core entries, and every `tan init` template but `minimal-app`),
    Zephyr auto-discovers overlays/fragments at the PARENT of `app_dir`, so
    globbing `app_dir` itself found nothing on every build and this guard
    never fired. [`resolve_zephyr_discovery_dir`] anchors `app_dir` on
    `build_root` (a relative `appDir` must not resolve against the `tan`
    process's own CWD -- the same reasoning `_missing_app_dirs` and
    `_substituted_app_dirs` already carry) and applies the identical
    CMakeLists.txt-parent fallback `_zephyr_app_dir`
    (`tan.planner.orchestrator`) uses for the real `west build` positional,
    so this guard's glob lands on the same directory.

    Returns `(extra_cmake_args, issues)`, mirroring
    [`_maybe_pristine_stale_sdk_build_dir`]'s own return shape: append
    `extra_cmake_args` to the spawn argv (empty when nothing changed);
    `issues` carries the coded `build.configure-cache-reset` note at `info`
    severity so a JSON-mode caller (the VS Code extension, envelope-only)
    can see a reset happened, not just a `text`-mode `on_output` line. Best-
    effort throughout: a stamp read/write failure is "no signal" (read) or
    silently skipped (write), never raised -- fails toward one spurious
    extra reset, never toward trusting a cache this function cannot
    actually verify."""
    if backend != "zephyr" or not app_dir:
        return [], []
    app_dir_path = resolve_zephyr_discovery_dir(app_dir, build_root)
    if not cmake_cache_configured(cwd):
        # Nothing cached yet to poison -- lay down this configure's own
        # baseline so the NEXT build has something to compare against.
        try:
            write_configure_inputs_stamp(cwd, discover_configure_inputs(app_dir_path))
        except OSError:
            pass
        return [], []

    current = discover_configure_inputs(app_dir_path)
    previous = read_configure_inputs_stamp(cwd)
    try:
        write_configure_inputs_stamp(cwd, current)
    except OSError:
        pass  # best-effort -- see this function's docstring
    if previous is not None and previous == current:
        return [], []

    added = sorted(current - (previous or frozenset()))
    removed = sorted((previous or frozenset()) - current)
    detail = "; ".join(
        part
        for part in (f"added {added}" if added else "", f"removed {removed}" if removed else "")
        if part
    ) or "no prior record"
    message = (
        f"{core_id}: devicetree overlay/Kconfig fragment set changed ({detail}) -- "
        f"resetting Zephyr's cached DTC_OVERLAY_FILE/CONF_FILE so the configure that "
        f"follows re-discovers it (tan-cli#655)"
    )
    on_output(f"note: {message}")
    return list(_CONFIGURE_CACHE_RESET_ARGS), [
        Issue("build.configure-cache-reset", "info", message)
    ]


# tan-cli#697. The refusal itself -- `cross_drive_source_refusal` -- is pure
# argv/path logic with no IO, so it lives in `tan.core.plan_exec` (imported
# above) rather than here, matching this repo's own convention of keeping
# pure decisions out of the command/IO module. `CROSS_DRIVE_MSG` (imported
# above as well) is matched, not imported, by `build_cmd._cross_drive_issues`
# to promote a refused slice's `reason` into a coded top-level `issues[]`
# entry -- same idiom as `_missing_tool_issues`' `` tool `...` not found ``
# text match, immediately below in this same file's `_ZEPHYR_SDK_MISSING_MSG`
# /`_WEST_NO_WORKSPACE_MSG` pattern. See `cross_drive_source_refusal`'s own
# docstring (`tan/core/plan_exec.py`) for the no-op conditions (mirroring
# `_pin_west_workspace`'s own guard, below) and why `args[-1]` -- this
# function's first, wrong, implementation -- never located the real source
# dir on a plan `orchestrator.py` actually emits.


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


@dataclass(frozen=True)
class _PostOutcome:
    """How a slice's `postCommands` ended, when they did not all exit 0.

    `manifest_message` is the short form persisted as `system-manifest.yaml`
    `slices[].reason` when it must differ from `message` -- the same split, for
    the same reason, as [`SliceOutcome.manifest_message`]: the missing-tool
    refusal's `-- searched PATH: <every entry>` belongs in this run's terminal
    and envelope, never in a build ARTEFACT that outlives the run and gets
    forwarded with a support ticket (tan-cli#615 review, MAJOR 1)."""

    status: str
    exit_code: int | None
    message: str
    manifest_message: str | None = None


def _missing_post_tool(
    where: str, tool: str, searched: str, policy: ExecutionPolicy | None
) -> _PostOutcome:
    """The refusal for a post-build step whose tool is not on this host.

    Routed through `executionPolicy.missingTool`, exactly like the slice's own
    `command` (tan-cli#615 review, MAJOR 3). An earlier round hardcoded
    `failed` here, arguing that a slice whose command has already run and
    rewritten the build tree cannot honestly be called "skipped". That
    argument loses to two documents this port is bound by, neither of which it
    cited: alp-sdk's `metadata/schemas/build-plan-v1.schema.json` AT THE PINNED
    COMMIT says, in `postCommands`' own description, "`executionPolicy` applies
    to each step exactly as it does to `command`"; and this repo's
    `docs/adr/0001-pmt-contract-decoupling.md` says "`tan` applies the policy
    the plan declares; it does not hardcode a copy of the planner's skip
    rules". A hardcoded disposition here is the exact coupling ADR-0001 exists
    to prevent.

    tan-cli#550 is not reopened by honouring it: `skipped` is not `built`
    either -- the slice is absent from the `N of M slice(s) built` count and
    carries a reason naming the missing tool. What the safety argument DOES
    buy is kept: the message says the configure already ran, so a `skipped`
    here cannot be misread as "never attempted".

    Only `missingTool` is reachable per step. `nullCommand` cannot be (a null
    step is refused at parse time, `tan.core.build_plan._post_commands`) and
    `unknownBackend` is a per-slice fact decided before any step runs."""
    action = resolve_action(policy, "missing_tool", PolicyAction.SKIP)
    short = f"{where} cannot run: {missing_tool_message(tool)}"
    return _PostOutcome(
        "skipped" if action is PolicyAction.SKIP else "failed",
        None,
        f"{short} -- searched {searched}. The slice's own configure already ran.",
        manifest_message=short,
    )


def _run_post_commands(
    core_id: str, post_commands: Sequence, build_root: Path, env: dict[str, str],
    on_output: Callable[[str], None], cancelled: Callable[[], bool],
    policy: ExecutionPolicy | None,
) -> _PostOutcome | None:
    """Run a slice's `postCommands` in order; `None` when every one of them
    exited 0, else the [`_PostOutcome`] the slice must report.

    The CONSUMER half of alp-sdk #1344 (tan-cli#550). The planner half landed
    in `tan/planner/` with the #608 re-sync and already emits the step --
    `{"tool": "cmake", "args": ["--build", "."]}` for every `os: baremetal`
    slice -- but nothing here read the key, so such a slice ran only its
    `cmake -S ... -B .` CONFIGURE and reported `ok` over a build tree holding
    `CMakeCache.txt` and no object file, archive or executable.

    Skip-vs-fail for a missing step tool is the plan's call, not this
    function's -- see [`_missing_post_tool`]. A `cwd` that escapes the build
    root and a spawn that raises stay hard failures, matching what the slice's
    own `command` does with the same two conditions."""
    total = len(post_commands)
    for n, step in enumerate(post_commands, start=1):
        label = " ".join([step.tool, *step.args])
        where = f"slice `{core_id}` post-build step {n} of {total} (`{label}`)"
        try:
            # Same confinement the slice's own `command.cwd` gets: a plan is
            # trusted input, never trusted enough to run a process outside
            # the build root.
            cwd = confine_to_build_root(build_root, step.cwd) if step.cwd else build_root
        except MaterialiseError as err:
            return _PostOutcome("failed", None, f"{where} was refused: {err.message}")
        resolution = _resolve_tool(step.tool, env)
        if resolution.resolved is None:
            return _missing_post_tool(where, step.tool, resolution.searched, policy)
        result = _spawn_step(resolution.resolved, step.args, cwd, env, on_output, cancelled)
        if result.cancelled:
            # Deliberately the same message the slice's own command produces:
            # one Ctrl-C cancelled one slice, and which step was in flight is
            # not what the caller asked.
            return _PostOutcome("cancelled", None, f"slice `{core_id}` cancelled")
        if result.launch_error is not None:
            # Both "the tool vanished since the check above" and "this step's
            # `cwd` does not exist"; the OSError text names which.
            return _PostOutcome("failed", None, f"{where} could not run: {result.launch_error}")
        if result.exit_code != 0:
            return _PostOutcome(
                "failed", result.exit_code,
                f"{where} terminated with exit code: {result.exit_code}",
            )
    return None


#: MEASURED, and the reason the guard below is PRESENCE-based rather than
#: freshness-based (tan-cli#615 review, MAJOR 2 -- answered by disclosure, with
#: the measurement, rather than by the requested mtime check).
#:
#: A "the artefact must be newer than this run started" rule was implemented
#: and then withdrawn, because it cannot tell the two cases apart:
#:
#:   * ORPHANED -- the app's CMakeLists.txt stopped defining the executable
#:     target, so a previous run's binary lingers in `outputDir` and nothing
#:     rewrites it. This is the case worth catching.
#:   * UP TO DATE -- nothing changed, so `cmake --build .` correctly relinks
#:     nothing and the (still valid, still current) binary keeps its old mtime.
#:     This is the ordinary incremental rebuild, and it is the common case.
#:
#: In BOTH the artefact is untouched by this run, so any "newer than the run"
#: rule refuses both. Measured end to end: with the freshness check in place, a
#: second `tan build` with NO source change at all reported
#: `failed: m55_hp [baremetal]` -- a false failure on the commonest workflow
#: there is, strictly worse than the defect it was closing.
#:
#: The other candidate discriminators were measured too, and none separates
#: the cases: `CMakeCache.txt`'s mtime is PRESERVED across all three runs
#: (fresh / no-op / orphaned) and `Makefile`'s is REWRITTEN in all three, so
#: neither is a signal. Comparing the artefact against the app's own source
#: tree (make's own out-of-date rule) does separate them, but false-refuses
#: whenever any file under `appDir` is touched without triggering a relink --
#: a `git checkout`, a README edit, an editor swap file -- and re-deriving
#: when an artefact is current is the build system's job, not tan's
#: (`docs/adr/0001-pmt-contract-decoupling.md`).
#:
#: So `cmake --build .` exiting 0 is taken as the build system's own statement
#: that the artefact is current, and STALENESS ACROSS A REMOVED TARGET IS A
#: DISCLOSED LIMIT of this guard -- see [`_baremetal_artefact_refusal`], where
#: it is listed beside the other two.


def _output_dir_has_a_file(out_dir: Path) -> bool:
    """Whether a slice's output directory holds at least one regular FILE.

    Files only: `rglob` walks directories too, and an empty `CMakeFiles/`
    left behind by the configure is not an artefact.

    An `OSError` on a single entry (a broken symlink, a file deleted between
    the walk and the `stat`, a permission hole) skips that entry rather than
    taking the guard down -- one unreadable file is not evidence either way.

    Presence, not freshness -- see the block comment above this function for
    the measurement that ruled freshness out."""
    if not out_dir.is_dir():
        return False
    for path in out_dir.rglob("*"):
        try:
            if path.is_file():
                return True
        except OSError:
            continue
    return False


def _baremetal_artefact_refusal(sl, build_root: Path) -> str | None:
    """Why an `os: baremetal` slice whose tools all exited 0 must still be
    reported `failed` -- `None` when there is nothing to refuse.

    The second half of tan-cli#550. Running `postCommands` makes a COMPILE or
    LINK error fail the slice honestly, but it does not close the issue's own
    headline: an exit code is still not evidence that firmware exists. An app
    defining no executable target configures, "builds", and exits 0 with an
    empty output directory -- the issue's "green build and an empty output
    directory", verbatim.

    The evidence is the plan's own `artifacts.outputDir`, which alp-sdk #1344
    added alongside `postCommands` and which the baremetal configure pins as
    `CMAKE_RUNTIME_OUTPUT_DIRECTORY` -- where the SDK itself says the slice's
    linked executables land, not a convention invented here.

    THREE KNOWN LIMITS, all deliberate, none worked around:

    1. Silent (`None`) when the plan names no `outputDir`. A plan from an
       alp-sdk predating #1344 gives this guard nothing to judge by, and
       inventing a location would fail slices that build fine. Those plans get
       the `postCommands` half of the fix and not this half.
    2. A baremetal slice whose app intentionally links no executable (a pure
       `add_library` tree) is refused. Same trade the `os: zephyr` boilerplate
       guard takes -- a core in `cores:` is firmware for that core, and a slice
       producing nothing flashable has nothing for `tan flash`/`size`/`image`
       to consume.
    3. **A previous run's binary satisfies this guard.** Edit an app's
       CMakeLists.txt to stop defining its executable target and rebuild IN
       PLACE, and the orphaned binary from the earlier run is still sitting in
       `outputDir`; the slice is reported `ok` and `tan flash` would write that
       stale image as though it were current. Deleting the build directory (or
       `tan clean`) is the only thing that surfaces it today. This one is a
       DISCLOSURE, not a design preference: a freshness check was implemented
       and withdrawn after measurement -- see the block comment above
       [`_output_dir_has_a_file`] for what was tried and why every candidate
       either false-failed the ordinary incremental rebuild or duplicated the
       build system's own out-of-date rule."""
    rel = sl.artifacts.get("outputDir") if isinstance(sl.artifacts, dict) else None
    if not isinstance(rel, str) or not rel:
        return None
    try:
        out_dir = confine_to_build_root(build_root, rel)
    except MaterialiseError as err:
        return (
            f"core `{sl.core_id}` is declared `os: baremetal`, but the output directory "
            f"its plan names cannot be checked: {err.message}"
        )
    if _output_dir_has_a_file(out_dir):
        return None
    return (
        f"core `{sl.core_id}` is declared `os: baremetal`, but its build produced no "
        f"artefact in `{rel}` -- the configure and every post-build step exited 0, yet "
        f"nothing was linked there. tan pins `CMAKE_RUNTIME_OUTPUT_DIRECTORY` at that "
        f"directory so `tan flash`/`size`/`image` can find the firmware, so an app whose "
        f"CMakeLists.txt defines no executable target (`add_executable(...)`) builds "
        f"nothing this core can run."
    )


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
    force_pristine: bool = False,
) -> list[SliceOutcome]:
    """Dispatch every slice of `plan` and return one [`SliceOutcome`] per
    slice, in plan order.

    `force_pristine` is `tan build --pristine` (tan-cli#427): threaded
    straight through to [`_maybe_pristine_stale_sdk_build_dir`] per slice --
    see that function's own docstring for the wipe/suppression decision it
    makes.

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
    configure_cache_issues: list[Issue] = []
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
    # tan-cli#1209: resolved ONCE for the whole run, same reasoning as
    # `zephyr_base` just above -- it depends only on `sdk_root` (this run's
    # checkout), never on a per-slice plan field, so recomputing it inside
    # the loop below would just repeat the same read+stamp-check on every
    # slice for no different answer. `None` when `sdk_root` is unresolved,
    # the manifest is missing/malformed, or the stamped store does not
    # match this checkout's pin -- [`zephyr_env_overrides`] then fills
    # nothing, matching today's behaviour exactly.
    #
    # tan-cli#1209 review MINOR: also `None` when `host_scan_has_toolchain()`
    # -- a `zephyr-sdk*` install CMake's own prefix scan (or the user package
    # registry) can already find on this host, independent of tan's store.
    # tan's own store is `-t arm-zephyr-eabi` ONLY; forcing it ahead of a
    # fuller, scan-visible host SDK could fail a non-ARM slice that
    # configured fine unaided, and would make `tan build` trust a different
    # toolchain than `tan doctor` reports (`doctor_cmd._zephyr_sdk_scan_roots`
    # ranks that same store LAST, never first). Checked here, not inside
    # `verified_store_dir` itself: that function answers "is the pin
    # satisfied", a fact independent of what else is on the host.
    verified_store = (
        verified_store_dir(sdk_root) if not host_scan_has_toolchain() else None
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

        # tan-cli#697: checked here -- right after `is_west` is known and
        # BEFORE tool resolution -- rather than down at the `_pin_west_
        # workspace` call site it mirrors: like the unsafe-cwd escape and
        # `cwd.mkdir` failure above, a cross-drive layout is a plan/host
        # defect that will crash regardless of which `west` binary this host
        # happens to have, so it must not be silently absorbed into
        # `executionPolicy.missingTool`'s default-skip on a host where
        # `west` is ALSO not on PATH.
        if is_west:
            cross_drive_refusal = cross_drive_source_refusal(workspace_dir, sl.command.args)
            if cross_drive_refusal is not None:
                outcomes.append(
                    SliceOutcome(
                        sl.core_id,
                        "failed",
                        None,
                        f"slice `{sl.core_id}` refused before build: "
                        f"{cross_drive_refusal.message}",
                        # tan-cli#697 review: a short form for
                        # `system-manifest.yaml` `slices[].reason` -- see
                        # [`SliceOutcome.manifest_message`]'s own docstring;
                        # same split the missing-tool refusal below already
                        # makes.
                        manifest_message=cross_drive_refusal.manifest_message,
                    )
                )
                continue

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
                zephyr_base,
                sdk_root_path,
                sl.env,
                sl.env_append_path,
                env_lookup,
                toolchain_store=verified_store,
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
        # tan-cli#992: `spawn_env()` restores tan's own bundled
        # `LD_LIBRARY_PATH` override before this slice's own `env`/
        # `envAppendPath` overlay -- a build slice spawns `west`/`cmake`/the
        # toolchain, all system programs, and this is the build path (a
        # `--flash` runs straight out of a build that just took this env).
        env = spawn_env()
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
                    f"{missing_tool_message(tool)} -- searched {resolution.searched}",
                    # tan-cli#510 review round 3, MAJOR: the full searched-PATH
                    # text stays in `message` (this run's stdout + envelope
                    # `reason`) but must NOT reach the persisted
                    # `system-manifest.yaml` -- see [`SliceOutcome.
                    # manifest_message`]'s own docstring.
                    manifest_message=missing_tool_message(tool),
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
                sl.core_id,
                cwd,
                sl.command.cwd,
                sl.command.args,
                sdk_stamp_key_str,
                on_output,
                force_pristine=force_pristine,
            )
        )

        # tan-cli#655: AFTER the sdk-switch-pristine guard, not before -- a
        # wipe there removes `cwd/build` wholesale (this stamp lives inside
        # it, same as the SDK stamp), so this call must see the POST-wipe
        # state to correctly treat a just-wiped dir as "nothing cached yet"
        # rather than comparing against a stamp that no longer describes
        # anything on disk.
        configure_cache_reset_args, new_configure_cache_issues = (
            _maybe_reset_stale_configure_cache(
                sl.core_id, cwd, sl.app_dir, sl.backend, build_root, on_output
            )
        )
        configure_cache_issues.extend(new_configure_cache_issues)

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
        # Appended to the SPAWN argv only -- `sl.command.args` (read again
        # below by `resolve_zephyr_artefact`/`build_dir_overridden`) stays
        # exactly what the plan named, so those checks never see a flag tan
        # itself injected. Order matters: this must land AFTER the plan's
        # own `-DEXTRA_CONF_FILE=...` (already inside `spawn_args`), never
        # before -- see `_maybe_reset_stale_configure_cache`'s docstring for
        # why an `-U`/`-D` pair on the same key is order-sensitive and why
        # `EXTRA_CONF_FILE` itself is deliberately excluded from the reset.
        spawn_args = spawn_args + configure_cache_reset_args

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
        # `None` means "persist `message` verbatim" -- only a post-build
        # refusal that carries host detail sets it (tan-cli#615 review,
        # MAJOR 1). Re-bound per iteration so one slice's redaction can never
        # be persisted as the next slice's reason.
        manifest_message: str | None = None

        def _watch_for_no_workspace(line: str) -> None:
            nonlocal saw_no_workspace, saw_no_zephyr_sdk
            if _WEST_NO_WORKSPACE_MSG in line:
                saw_no_workspace = True
            if _ZEPHYR_SDK_MISSING_MSG in line:
                saw_no_zephyr_sdk = True
            on_output(line)

        # tan-cli#550: the spawn itself lives in [`_spawn_step`] so the
        # post-build steps below run through the SAME Popen/drain/terminate
        # path as the slice's own command -- one spawn implementation, not
        # two that can drift on cancellation handling or fd hygiene.
        step = _spawn_step(
            resolved_tool, spawn_args, spawn_cwd, env, _watch_for_no_workspace, cancelled
        )
        if step.cancelled:
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
        if step.launch_error is not None:
            # The tool vanished between the availability check above and
            # here, is a directory, lacks the executable bit, or is not a
            # valid executable format -- any of these raise inside
            # `_spawn_step` rather than at the `_resolve_tool` precheck. A
            # failed slice, never a crash. The base message already names
            # both `tool` and `resolved_tool` explicitly (it IS the substance
            # of what failed, not a redundant echo of it) -- `resolved_tool=`
            # below still carries the SAME fact in the dedicated structured
            # field.
            outcomes.append(
                SliceOutcome(
                    sl.core_id,
                    "failed",
                    None,
                    f"failed to launch `{tool}` resolved to `{resolved_tool}`: "
                    f"{step.launch_error}",
                    resolved_tool=outcome_resolved_tool,
                )
            )
            continue
        # Neither cancelled nor a launch failure, so `_spawn_step` reached
        # `proc.wait()` and `code` is a real integer -- the only two shapes
        # that leave it `None` are the two `continue`d above.
        code = step.exit_code

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

        # tan-cli#550, half 1: run the plan's `postCommands`. `west build` and
        # `bitbake` configure AND build in one invocation and carry none; a
        # baremetal slice's `cmake -S ... -B .` only CONFIGURES, so its
        # `cmake --build .` arrives here and MUST run before the slice can be
        # called built. Gated on the slice's own command having succeeded --
        # there is nothing to build on top of a failed configure -- and on the
        # zephyr-evidence guard above, for the same reason.
        if status == "succeeded" and sl.post_commands:
            post = _run_post_commands(
                sl.core_id, sl.post_commands, build_root, env, on_output, cancelled, policy
            )
            if post is not None:
                # The failing step's own exit code becomes the slice's `rc`:
                # unlike the evidence guards, this is not tan refusing a
                # result, it is a real process that really failed. A step that
                # never reached a process at all carries `None`, which the
                # single `SliceOutcome` construction below already renders as
                # a null `rc`.
                #
                # No early `continue` for the `cancelled`/`skipped` shapes:
                # every path from here to that construction is gated on
                # `status == "succeeded"`, so a non-succeeded post outcome
                # already reaches it untouched. A second append site would be
                # an untestable duplicate of the first (proven: a mutant that
                # removed the `skipped` case from such a branch changed no
                # observable field).
                status, code, message = post.status, post.exit_code, post.message
                # tan-cli#615 review, MAJOR 1: the missing-tool refusal's
                # searched-PATH text must not reach the persisted
                # `system-manifest.yaml` -- see [`SliceOutcome.manifest_message`].
                manifest_message = post.manifest_message

        # tan-cli#550, half 2: an exit code is not evidence that firmware
        # exists. See [`_baremetal_artefact_refusal`] -- the same shape as the
        # zephyr guard above, keyed on the plan's own `artifacts.outputDir`.
        if status == "succeeded" and sl.backend == "baremetal":
            refusal = _baremetal_artefact_refusal(sl, build_root)
            if refusal is not None:
                status = "failed"
                message = refusal

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
                #
                # `code` is also `None` when a post-build step (tan-cli#550)
                # never reached a process at all -- its tool was missing, its
                # `cwd` escaped the build root, or the spawn raised -- which
                # is the same "no single-integer exit code" case the signal
                # death above is, and reaches the envelope's `rc` as null the
                # same way.
                None if code is None or code < 0 else code,
                message,
                output_artefact,
                slice_build_dir,
                resolved_tool=outcome_resolved_tool,
                manifest_message=manifest_message,
            )
        )

    global _last_sdk_switch_issues, _last_configure_cache_issues
    _last_sdk_switch_issues = sdk_switch_issues
    _last_configure_cache_issues = configure_cache_issues

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
