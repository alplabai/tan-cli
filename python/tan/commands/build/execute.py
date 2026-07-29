# SPDX-License-Identifier: Apache-2.0
"""Dispatch each slice: assemble its env, apply the execution policy's
skip-vs-fail dispositions, spawn the tool, stream its output, and report a
per-slice outcome -- never an escaping exception. Mirrors the dispatch loop
in `tan-cli/src/commands/build/execute/mod.rs`, trimmed to this port's
current scope: no post-build system-manifest.yaml write, no
sdk-switch-pristine wipe (see the task report for why), no
Zephyr-boilerplate-loaded guard. What IS ported: the unknown-backend /
null-command / missing-tool skip-vs-fail policy, the unsafe-cwd refusal, and
the build-dir-must-exist-before-the-tool-runs precondition."""
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

KNOWN_BACKENDS = frozenset({"zephyr", "baremetal", "yocto", "native"})

# How often the output-draining loop checks `cancelled()` while no output
# line is pending -- bounds cancellation latency without spinning the CPU.
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
    `cancelled()` at least every `_POLL_INTERVAL_S` while waiting for the
    next line. Returns True iff cancellation was observed.

    A plain `for line in proc.stdout:` blocks on the next `readline()` with
    no way to interleave a check -- a slice that produces no output at all
    (e.g. `time.sleep(60)`) would never yield control back, so `cancelled()`
    would never be polled until the process exits on its own. Reading on a
    background thread and polling a queue with a timeout is the portable fix
    (Windows anonymous pipes don't support `select`)."""
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
        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            if cancelled():
                return True
            continue
        if line is None:
            return False
        on_output(line.rstrip("\n"))


def _terminate(proc: subprocess.Popen) -> None:
    """Actually stop a running process -- not wait it out. `terminate()`
    first (SIGTERM on POSIX; Windows has no distinct signal so this already
    calls TerminateProcess), a short grace period, then `kill()` for a
    process that ignored (or, on POSIX, blocked) the polite request."""
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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

        tool = sl.command.tool
        if shutil.which(tool) is None and not Path(tool).exists():
            outcomes.append(
                _skip_or_fail(
                    sl.core_id,
                    resolve_action(policy, "missing_tool", PolicyAction.SKIP),
                    f"tool `{tool}` not found",
                )
            )
            continue

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

        env = dict(os.environ)
        env.update(dict(assemble_slice_env(sl.env, sl.env_append_path, env_lookup, gap_fillers)))

        try:
            # A context manager: closes stdout/stderr/stdin on every exit
            # path (success, cancellation, exception) -- a bare `Popen(...)`
            # here leaked the pipe's file object (a `ResourceWarning` under
            # `-W error::ResourceWarning`, and a real fd leak across a long
            # multi-slice run).
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
            # at the `shutil.which` precheck. A failed slice, never a crash.
            outcomes.append(
                SliceOutcome(sl.core_id, "failed", None, f"failed to launch `{tool}`: {err}")
            )
            continue

        outcomes.append(
            SliceOutcome(
                sl.core_id,
                "succeeded" if code == 0 else "failed",
                code,
                None if code == 0 else f"slice `{sl.core_id}` terminated with exit code: {code}",
            )
        )

    return outcomes
