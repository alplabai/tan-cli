# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys
import threading
import time

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices

# A JSON string literal (quotes included) for the running interpreter --
# `sys.executable` is `C:\Python311\python.exe` on Windows, and interpolating
# it into an f-string JSON literal directly (`"{sys.executable}"`) produces
# an invalid `\P`/`\p` escape. json.dumps() escapes it correctly on every
# platform.
PYTHON = json.dumps(sys.executable)


def _run_with_watchdog(fn, timeout: float):
    """Run `fn` on a background thread and return `(result, elapsed)` --
    never let a regression that makes `fn` hang block the whole suite.
    Fails the assertion instead: the thread is a daemon, so a genuinely
    hung call is abandoned (and reaped when the process exits) rather than
    stalling pytest indefinitely."""
    result: list = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread below
            error.append(exc)

    t = threading.Thread(target=target, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(timeout=timeout)
    elapsed = time.monotonic() - start
    assert not t.is_alive(), f"did not complete within {timeout}s (still running at {elapsed:.1f}s) -- looks hung"
    if error:
        raise error[0]
    return result[0], elapsed


def _plan(command: str, backend: str = "zephyr") -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"}},
      "slices": [{{
        "coreId": "c1", "backend": "{backend}", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
        "command": {command}, "env": {{}}, "envAppendPath": {{}}
      }}]
    }}"""


def test_successful_slice_reports_succeeded(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "print(1)"], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "succeeded"
    assert out[0].exit_code == 0


def test_failing_slice_reports_failed_with_exit_code(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "raise SystemExit(3)"], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"
    assert out[0].exit_code == 3


def test_null_command_is_skipped_per_policy(tmp_path):
    out = execute_slices(parse_build_plan(_plan("null")), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "skipped"


def test_missing_tool_is_skipped_per_policy(tmp_path):
    cmd = '{"tool": "definitely-not-a-real-tool-xyz", "args": [], "cwd": null}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "skipped"


def test_unknown_backend_fails_per_policy(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd, backend="martian")),
                         build_root=tmp_path, env_lookup=lambda k: None,
                         gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"


def test_cancellation_terminates_a_running_slice(tmp_path):
    """Cancellation is a spec acceptance criterion -- a long slice must be
    stopped, not waited out."""
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "import time; time.sleep(60)"], "cwd": null}}'
    start = time.monotonic()
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[],
                         on_output=lambda s: None, cancelled=lambda: True)
    elapsed = time.monotonic() - start
    assert out[0].status == "cancelled"
    assert elapsed < 10, f"cancellation took {elapsed:.1f}s -- must not wait the slice out"


def test_build_dir_is_created_before_the_tool_runs(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                    env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert (tmp_path / "build/c1").is_dir()


def test_refuses_a_cwd_that_escapes_the_build_root(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "../../escape"}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"
    assert "escape" in out[0].message.lower()
    assert not (tmp_path.parent.parent / "escape").exists()


def test_tool_that_is_a_directory_fails_instead_of_crashing(tmp_path):
    """The tool path exists (so the missing-tool skip does not trigger) but
    is a directory, not an executable -- the spawn itself must fail cleanly,
    never raise out of execute_slices."""
    tool_dir = tmp_path / "not_a_tool"
    tool_dir.mkdir()
    cmd = f'{{"tool": {json.dumps(str(tool_dir))}, "args": [], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"
    assert out[0].message is not None


def test_undecodable_stdout_bytes_are_replaced_not_fatal(tmp_path):
    """A child that writes invalid UTF-8 must not blow up the reader --
    errors='replace' swaps the bad byte for U+FFFD and the slice still
    reports a real outcome."""
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'before-\\xff\\xfe-after\\n'); "
        "sys.stdout.buffer.flush()"
    )
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", {json.dumps(script)}], "cwd": null}}'
    lines = []
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append)
    assert out[0].status == "succeeded"
    assert lines, "expected at least one output line"
    assert "before-" in lines[0] and "after" in lines[0]


def test_unknown_backend_skipped_when_policy_says_skip(tmp_path):
    plan_json = f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"unknownBackend": "skip"}},
      "slices": [{{
        "coreId": "c1", "backend": "martian", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
        "command": {{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}},
        "env": {{}}, "envAppendPath": {{}}
      }}]
    }}"""
    out = execute_slices(parse_build_plan(plan_json), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "skipped"


def test_native_is_not_a_legal_backend(tmp_path):
    """C1 regression guard: metadata/schemas/build-plan-v1.schema.json's
    `slices[].backend` enum is zephyr/yocto/baremetal only -- "native" must
    be treated as unknown (fails by default), never dispatched and run."""
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd, backend="native")),
                         build_root=tmp_path, env_lookup=lambda k: None,
                         gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"


def test_cancellation_is_observed_despite_continuous_chatty_output(tmp_path):
    """C2 regression guard: a chatty child (west -> cmake -> ninja is chatty
    by construction) that keeps producing output faster than the poll
    interval must not starve the cancelled() check. Guarded by a watchdog --
    on the old code (cancelled() polled only in the queue.Empty arm) this
    would hang for as long as the child keeps talking, not forever, but
    still far longer than a real cancellation should ever take."""
    chatty_script = (
        "import sys, time\n"
        "end = time.monotonic() + 3\n"
        "while time.monotonic() < end:\n"
        "    sys.stdout.write('x\\n'); sys.stdout.flush()\n"
    )
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", {json.dumps(chatty_script)}], "cwd": null}}'

    out, elapsed = _run_with_watchdog(
        lambda: execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                               env_lookup=lambda k: None, gap_fillers=[],
                               on_output=lambda s: None, cancelled=lambda: True),
        timeout=10,
    )
    assert out[0].status == "cancelled"
    assert elapsed < 5, f"cancellation took {elapsed:.1f}s -- a chatty child starved cancelled()"


def test_cancellation_kills_the_whole_process_tree(tmp_path):
    """C3 regression guard: a grandchild that inherited the stdout pipe (the
    west -> cmake -> ninja shape -- each spawns the next, inheriting stdout)
    must not survive `_terminate`. If it does, the reader thread blocks
    forever waiting for EOF on the pipe and `Popen.__exit__` deadlocks
    closing it -- guarded by a watchdog so a regression fails this test
    instead of hanging the suite.

    Cancellation must not fire until the grandchild genuinely exists --
    `cancelled=lambda: True` alone races the child's own startup (interpreter
    boot + `import subprocess` + spawn easily loses to `cancelled()` being
    polled on the very first drain iteration), which would kill the direct
    child before any grandchild is spawned at all and pass even on the buggy
    code. The child prints a marker line right after spawning the grandchild;
    `cancelled` only trips once that marker has actually been observed."""
    tree_script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('grandchild-spawned', flush=True)\n"
        "time.sleep(60)\n"
    )
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", {json.dumps(tree_script)}], "cwd": null}}'

    spawned = threading.Event()

    def on_output(line: str) -> None:
        if "grandchild-spawned" in line:
            spawned.set()

    out, elapsed = _run_with_watchdog(
        lambda: execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                               env_lookup=lambda k: None, gap_fillers=[],
                               on_output=on_output, cancelled=spawned.is_set),
        timeout=15,
    )
    assert spawned.is_set(), "grandchild was never observed as spawned -- test did not exercise C3"
    assert out[0].status == "cancelled"
    assert elapsed < 10, f"took {elapsed:.1f}s -- a surviving grandchild kept the pipe open"


def test_relative_tool_not_on_path_is_skipped_not_run(tmp_path):
    """M1 regression guard: Rust's tool_available (crates/tan-cli/src/util.rs)
    treats a RELATIVE tool as available only via PATH -- a same-named file
    that merely exists in the process's current working directory (but isn't
    on PATH, and isn't referenced by an absolute path) must still be treated
    as missing, not spawned. On Windows this is the CWD-precedence hole
    `shutil.which` alone doesn't close (its own source: "the current
    directory takes precedence on Windows") -- a decoy planted in CWD must
    NOT be picked up over (or in place of) a genuine PATH lookup."""
    decoy = "definitely-not-on-path-decoy-tool.exe"
    (tmp_path / decoy).write_bytes(b"not really a launchable interpreter")
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        cmd = f'{{"tool": {json.dumps(decoy)}, "args": [], "cwd": null}}'
        out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                             env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    finally:
        os.chdir(old_cwd)
    assert out[0].status == "skipped"


def test_unsafe_cwd_fails_even_when_the_tool_is_also_missing(tmp_path):
    """M2 regression guard: dispatch order must match the Rust oracle --
    unsafe-cwd is checked BEFORE missing-tool, so a slice with both stays
    'failed' (loud plan defect), never silently absorbed into
    missingTool's default-skip."""
    cmd = '{"tool": "definitely-not-a-real-tool-xyz", "args": [], "cwd": "../../escape"}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"
    assert "escape" in out[0].message.lower()
