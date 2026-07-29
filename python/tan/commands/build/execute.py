# SPDX-License-Identifier: Apache-2.0
"""Dispatch each slice: assemble its env, apply the execution policy's
skip-vs-fail dispositions, spawn the tool, stream its output, and report a
per-slice outcome -- never an escaping exception. Mirrors the dispatch loop
in `tan-cli/src/commands/build/execute/mod.rs`, trimmed to this port's
current scope: no post-build system-manifest.yaml write, no
sdk-switch-pristine wipe (genuinely unreachable here -- tracked separately,
not a gap in this file), no Zephyr-boilerplate-loaded guard, and no
`tool == "west"` rewrite to the venv's west (`with_venv_on_path` in the Rust
oracle -- no Python venv-resolution module exists yet). What IS ported: the
unknown-backend / null-command / unsafe-cwd / missing-tool skip-vs-fail
policy and dispatch order, and the build-dir-must-exist-before-the-tool-runs
precondition."""
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build.materialise import MaterialiseError, confine_to_build_root
from tan.core.plan_exec import PolicyAction, assemble_slice_env, resolve_action

if os.name != "nt":
    import signal

# metadata/schemas/build-plan-v1.schema.json: slices[].backend enum. Rust's
# `Backend` (tan-core/src/build_plan.rs) matches this exactly, plus a catch-all
# `Unknown(String)` for anything else -- "native" is NOT a legal backend; a
# slice naming it must be refused by `executionPolicy.unknownBackend`
# (default fail), never dispatched.
KNOWN_BACKENDS = frozenset({"zephyr", "yocto", "baremetal"})

# How often the output-draining loop checks `cancelled()` -- bounds
# cancellation latency without spinning the CPU.
_POLL_INTERVAL_S = 0.05
# How long to give a terminated process to exit on its own before escalating
# to a forceful kill.
_TERMINATE_GRACE_S = 1.0


@dataclass(frozen=True)
class SliceOutcome:
    core_id: str
    status: str  # "succeeded" | "skipped" | "failed" | "cancelled"
    exit_code: int | None
    message: str | None


def _skip_or_fail(core_id: str, action: PolicyAction, message: str) -> SliceOutcome:
    status = "skipped" if action is PolicyAction.SKIP else "failed"
    return SliceOutcome(core_id, status, None, message)


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


def execute_slices(
    plan,
    *,
    build_root: Path,
    env_lookup: Callable[[str], str | None],
    gap_fillers: Sequence[tuple[str, str]],
    on_output: Callable[[str], None],
    cancelled: Callable[[], bool] = lambda: False,
) -> list[SliceOutcome]:
    policy = plan.execution_policy
    outcomes: list[SliceOutcome] = []

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
        if not _tool_is_available(tool):
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "missing_tool", PolicyAction.SKIP),
                    f"tool `{tool}` not found",
                )
            )
            continue

        env = dict(os.environ)
        env.update(dict(assemble_slice_env(sl.env, sl.env_append_path, env_lookup, gap_fillers)))

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
                [tool, *sl.command.args],
                cwd=str(cwd),
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
            )
        )

    return outcomes
