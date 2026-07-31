# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys
import threading
import time
from pathlib import Path

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices
from tan.commands.build.manifest import PostBuildManifest
from tan.core.system_manifest import parse_system_manifest
from tests.conftest import sdk_root

# A JSON string literal (quotes included) for the running interpreter --
# `sys.executable` is `C:\Python311\python.exe` on Windows, and interpolating
# it into an f-string JSON literal directly (`"{sys.executable}"`) produces
# an invalid `\P`/`\p` escape. json.dumps() escapes it correctly on every
# platform.
PYTHON = json.dumps(sys.executable)

#: Captured at MODULE IMPORT time -- i.e. at collection, before pytest ever
#: runs a test function. `tests/conftest.py`'s autouse `_scrub_sdk_discovery_env`
#: deletes `ALP_SDK_ROOT` from the process environment ahead of every test
#: function (hermeticity for the tests that assert "nothing resolves"), so a
#: call to `sdk_root()` made from inside a test body -- i.e. AFTER that
#: fixture has already run -- always sees it gone, even on a host where it
#: was exported for real. `test_native_sim_e2e.py` and `test_kconfig_symbols.py`
#: hit the same fixture and use the identical module-level-capture pattern.
SDK = sdk_root()


def _stub_manifest_write(monkeypatch) -> None:
    """The sdk-switch-pristine stamp-guard tests below pass a throwaway
    `sdk_root=` to exercise the stamp comparison -- but `execute_slices`
    feeds that SAME value into the post-build manifest write's real planner
    emit too (a different, already-covered concern -- see the "post-build
    manifest write + signal" section). `tan.planner_root.bind_sdk_root`
    freezes `tan.planner`'s module-level constants to the FIRST root it ever
    sees IN THIS PROCESS (by design -- rebinding to a different root once
    imported is a loud error, not a no-op, see that module's docstring), so a
    throwaway root here would permanently wedge every later real-SDK-emit
    test in the same pytest run. Stub the write out instead of touching that
    global."""
    import tan.commands.build.execute as execute_module

    monkeypatch.setattr(
        execute_module,
        "write_post_build_manifest",
        lambda **_: PostBuildManifest(write_failed_reason=None, native_sim_target=None),
    )


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


# ------------------------------------------- sdk-switch-pristine guard (issue #52)


def _configure(slice_dir: Path, stamp: str | None) -> None:
    """Fake what a prior `west build` (and, when `stamp` is given, a prior
    `tan build`) left behind: a configured nested build dir, optionally
    already stamped with an SDK identity."""
    build = slice_dir / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "CMakeCache.txt").write_text("# fake cmake cache\n", encoding="utf-8")
    if stamp is not None:
        (build / ".tan-sdk-root").write_text(stamp, encoding="utf-8")


def test_matching_sdk_stamp_keeps_the_build_dir(tmp_path, monkeypatch):
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v1")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    lines = []
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append,
                   sdk_root="/sdk/v1")
    assert (slice_dir / "build" / "CMakeCache.txt").is_file(), "matching stamp must not wipe"
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v1"
    assert not any("pristine" in line for line in lines), lines


def test_mismatched_sdk_stamp_wipes_and_restamps_the_build_dir(tmp_path, monkeypatch):
    """The reported case: v0.11.0 -> v0.13.0."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    lines = []
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append,
                         sdk_root="/sdk/v0.13.0")
    assert out[0].status == "succeeded"
    assert not (slice_dir / "build" / "CMakeCache.txt").exists(), "mismatched stamp must wipe"
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.13.0"
    assert any("running pristine" in line for line in lines), lines


def test_missing_stamp_on_an_already_configured_dir_self_heals_to_pristine(tmp_path, monkeypatch):
    """A build dir that predates this feature (CMakeCache.txt exists, no
    stamp) reads as stale DELIBERATELY -- the only way it ever self-heals."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, stamp=None)
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert not (slice_dir / "build" / "CMakeCache.txt").exists()
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.13.0"


def test_corrupt_non_utf8_stamp_reads_as_a_mismatch_not_a_crash(tmp_path, monkeypatch):
    """`read_sdk_stamp` must fold `UnicodeDecodeError` into "no signal" the
    same as a missing file, not let it escape -- `execute_slices`'s own
    module docstring promises no escaping exception. Under a
    `read_sdk_stamp` regression that catches only `OSError` this test raises
    `UnicodeDecodeError` instead of completing (verified: the whole stamp
    suite otherwise stays green under that mutation)."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, stamp=None)
    (slice_dir / "build" / ".tan-sdk-root").write_bytes(b"\xff\xfe\x00garbage")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert not (slice_dir / "build" / "CMakeCache.txt").exists(), "unreadable stamp must read as stale"
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.13.0"


def test_never_configured_dir_is_never_wiped_but_still_gets_stamped(tmp_path, monkeypatch):
    """No `CMakeCache.txt` at all -- nothing to go stale, so `sdk_stamp_action`
    is always `Keep` here regardless of any prior stamp. The dir still gets
    (re-)stamped before the tool runs, unconditionally, so THIS build's own
    `west build` leaves an honest stamp for the NEXT run to compare against."""
    _stub_manifest_write(monkeypatch)
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    lines = []
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append,
                   sdk_root="/sdk/v0.13.0")
    assert not any("pristine" in line for line in lines), lines
    assert (
        tmp_path / "build" / "c1" / "build" / ".tan-sdk-root"
    ).read_text(encoding="utf-8") == "/sdk/v0.13.0"


def test_build_dir_override_is_never_touched_by_the_stamp_guard(tmp_path, monkeypatch):
    """A `-d`/`--build-dir` in the slice's argv means west wrote somewhere
    this guard cannot know -- it must never wipe or stamp `<cwd>/build`."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass", "-d", "elsewhere"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert (slice_dir / "build" / "CMakeCache.txt").is_file()
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.11.0"


def test_cwd_outside_build_root_is_never_touched_by_the_stamp_guard(tmp_path, monkeypatch):
    """The wipe target must stay confined under `CONSUMER_BUILD_ROOT`
    (`build`) -- a plan cwd of `src/c1` (still plain-relative) must not put
    the wipe target at `<project>/src/c1/build`, which may hold files the
    build never created."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "src" / "c1"
    _configure(slice_dir, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "src/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert (slice_dir / "build" / "CMakeCache.txt").is_file()
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.11.0"


def test_unresolved_sdk_root_never_wipes(tmp_path):
    """`sdk_root=None` (unresolved -- the default, and what every OTHER test
    in this file passes) means nothing to compare against: the guard must
    never wipe on that, and never write a stamp with no key."""
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert (slice_dir / "build" / "CMakeCache.txt").is_file()
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v0.11.0"


def test_mismatched_sdk_stamp_records_a_coded_sdk_switch_issue(tmp_path, monkeypatch):
    """The pristine wipe must not be stderr-only: `tan.commands.build_cmd`
    folds `execute_module.last_sdk_switch_issues()` into the JSON envelope,
    the same way the post-build manifest signal is threaded, since the VS
    Code extension only ever sees the envelope, not `on_output`'s stream."""
    import tan.commands.build.execute as execute_module

    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    issues = execute_module.last_sdk_switch_issues()
    assert [i.code for i in issues] == ["build.sdk-switch-pristine"]
    assert issues[0].severity == "warning"
    assert "running pristine" in issues[0].message


def test_matching_sdk_stamp_records_no_sdk_switch_issues_and_overwrites_a_prior_calls(
    tmp_path, monkeypatch
):
    """A call with nothing to wipe must read back an EMPTY list, even right
    after a call that recorded one -- always freshly overwritten, never
    appended-to across calls."""
    import tan.commands.build.execute as execute_module

    _stub_manifest_write(monkeypatch)
    mismatched = tmp_path / "build" / "c1"
    _configure(mismatched, "/sdk/v0.11.0")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert execute_module.last_sdk_switch_issues()  # sanity: the previous call recorded one

    matching_root = tmp_path.parent / "matching-root"
    slice_dir = matching_root / "build" / "c1"
    _configure(slice_dir, "/sdk/v0.13.0")
    execute_slices(parse_build_plan(_plan(cmd)), build_root=matching_root,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/sdk/v0.13.0")
    assert execute_module.last_sdk_switch_issues() == []


def test_sdk_root_for_stamp_is_preferred_over_sdk_root_for_the_stamp_comparison(
    tmp_path, monkeypatch
):
    """`tan.commands.build_cmd.build` threads a NORMALIZED, workspace-root-
    anchored form here (`tan.core.plan_exec.normalize_path`) while `sdk_root`
    itself stays raw (still consumed for the manifest emit and `${SDK_ROOT}`
    token substitution) -- the stamp comparison must key on the normalized
    form, not the raw one, even when the two spell the same checkout
    differently (tan-cli#163)."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v1")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    lines = []
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append,
                   sdk_root="../alp-sdk", sdk_root_for_stamp="/sdk/v1")
    assert not any("pristine" in line for line in lines), lines
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v1"


def test_matching_root_but_different_plan_commit_still_wipes(tmp_path, monkeypatch):
    """Guards `execute_slices` actually threading `plan.sdk_commit` into the
    stamp key -- every OTHER guard test's plan (via `_plan()`) carries no
    `sdkCommit` at all, so a mutation reading `None` regardless of the plan
    (e.g. `sdk_stamp_key(stamp_root, None)`) would leave every other case in
    this file green."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v1@deadbee")
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    plan_doc = json.loads(_plan(cmd))
    plan_doc["sdkCommit"] = "3ffd8774"
    lines = []
    execute_slices(parse_build_plan(json.dumps(plan_doc)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append,
                   sdk_root="/sdk/v1")
    assert not (slice_dir / "build" / "CMakeCache.txt").exists(), "different commit at same root must wipe"
    # Under a `plan.sdk_commit -> None` regression this would instead read
    # back the bare root "/sdk/v1" (no "@3ffd8774"), the one assertion in
    # this file that catches it.
    assert (slice_dir / "build" / ".tan-sdk-root").read_text(encoding="utf-8") == "/sdk/v1@3ffd8774"
    assert any("running pristine" in line for line in lines), lines


# ------------------------------------------- post-build manifest write + signal


def test_manifest_overlay_writes_ok_and_failed_status_for_the_right_slices(
    tmp_path, monkeypatch
):
    """`SliceOutcome.status` -> `system-manifest.yaml`'s `status` (via
    `_WIRE_STATUS`) had no test at all: a mutation reversing the map, or
    defaulting an unmapped status to "ok", left the whole hermetic suite
    green -- dangerous in the direction that ships a flash of a slice that
    failed to build (`status` is the only gate `flash` consults). Exercise
    `execute_slices`'s manifest-write side effect over one succeeding and one
    failing slice and read the actual written YAML back, with
    `tan.planner_root.emit` monkeypatched so no real SDK is needed."""
    import tan.planner_root as planner_root

    fixture_manifest = (
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n"
        "- core_id: ok_core\n  os: zephyr\n  status: pending\n"
        "- core_id: bad_core\n  os: zephyr\n  status: pending\n"
        "ipc: []\nhelper_mcus: []\nboot_order: []\n"
    )
    monkeypatch.setattr(planner_root, "emit", lambda *a, **k: fixture_manifest)

    plan_json = f"""{{
      "schemaVersion": 1, "generatedBy": "g",
      "boardYaml": {json.dumps(str(tmp_path / "board.yaml"))},
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"}},
      "slices": [
        {{"coreId": "ok_core", "backend": "zephyr", "buildDir": "build/ok_core", "appDir": "app",
          "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
          "command": {{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}},
          "env": {{}}, "envAppendPath": {{}}}},
        {{"coreId": "bad_core", "backend": "zephyr", "buildDir": "build/bad_core", "appDir": "app",
          "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
          "command": {{"tool": {PYTHON}, "args": ["-c", "raise SystemExit(1)"], "cwd": null}},
          "env": {{}}, "envAppendPath": {{}}}}
      ]
    }}"""

    execute_slices(parse_build_plan(plan_json), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
                   sdk_root="/fake/sdk")

    written = (tmp_path / "build" / "system-manifest.yaml").read_text(encoding="utf-8")
    manifest = parse_system_manifest(written)
    ok = next(s for s in manifest.slices if s["core_id"] == "ok_core")
    bad = next(s for s in manifest.slices if s["core_id"] == "bad_core")
    assert ok["status"] == "ok"
    assert bad["status"] == "failed"


def test_held_outcomes_reach_the_manifest_overlay_with_explicit_empty_artefact(
    tmp_path, monkeypatch
):
    """tan-cli #89 MINOR 4 (oracle `execute/mod.rs:379-400`): a slice the
    CALLER already decided not to dispatch (`build_cmd._dispatch` holding
    back a token-substitution-demoted slice) must still land in the written
    `system-manifest.yaml` -- not be left at its plan-time `status`/
    `output_artefact` forever. `held_outcomes` is folded into the overlay
    alongside the dispatched slice's own outcome, and a held slice's
    `output_artefact` overlays as the explicit empty string (never `None`,
    which `overlay_run_results_raw` treats as "leave the plan-time value
    alone")."""
    import tan.planner_root as planner_root

    from tan.commands.build.execute import SliceOutcome

    fixture_manifest = (
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n"
        "- core_id: ok_core\n  os: zephyr\n  status: pending\n"
        "- core_id: held_core\n  os: zephyr\n  status: pending\n"
        "  output_artefact: build/held_core/build/zephyr/zephyr.elf\n"
        "ipc: []\nhelper_mcus: []\nboot_order: []\n"
    )
    monkeypatch.setattr(planner_root, "emit", lambda *a, **k: fixture_manifest)

    # Mirrors `build_cmd._dispatch`: `execute_slices` only ever sees the
    # RUNNABLE subset of the plan (the held slice is not one of its slices),
    # so the only way its manifest overlay learns `held_core` existed is via
    # `held_outcomes`.
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}}'
    plan_doc = json.loads(_plan(cmd, backend="zephyr"))
    plan_doc["slices"][0]["coreId"] = "ok_core"

    execute_slices(
        parse_build_plan(json.dumps(plan_doc)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root="/fake/sdk",
        held_outcomes=[
            SliceOutcome(
                "held_core", "skipped", None, "toolchain root unresolved",
                output_artefact="",
            )
        ],
    )

    written = (tmp_path / "build" / "system-manifest.yaml").read_text(encoding="utf-8")
    manifest = parse_system_manifest(written)
    held = next(s for s in manifest.slices if s["core_id"] == "held_core")
    assert held["status"] == "skipped"
    # The stale plan-time artefact must be CLEARED, not left in place --
    # `output_artefact=""` is the explicit-clear convention, distinct from
    # `None` ("preserve whatever is already there").
    assert held.get("output_artefact", "") == ""


def test_successful_slice_resolves_the_real_on_disk_elf(tmp_path):
    """A slice that actually leaves `<cwd>/build/zephyr/zephyr.elf` behind
    (what west's default nested output looks like) must resolve it into
    `SliceOutcome.output_artefact`/`build_dir` -- fed into the post-build
    manifest so `run`/`size`/`flash` find the real artefact, not a guess."""
    elf_dir = tmp_path / "build" / "c1" / "build" / "zephyr"
    script = (
        f"import os; os.makedirs({json.dumps(str(elf_dir))}, exist_ok=True); "
        f"open(os.path.join({json.dumps(str(elf_dir))}, 'zephyr.elf'), 'w').close()"
    )
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", {json.dumps(script)}], "cwd": "build/c1"}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "succeeded"
    assert out[0].output_artefact == os.path.abspath(str(elf_dir / "zephyr.elf"))
    assert out[0].build_dir == os.path.abspath(str(tmp_path / "build" / "c1" / "build"))


def test_a_failed_slice_reports_no_artefact(tmp_path):
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "raise SystemExit(1)"], "cwd": null}}'
    out = execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                         env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert out[0].status == "failed"
    assert out[0].output_artefact is None
    assert out[0].build_dir is None


def test_execute_slices_writes_the_manifest_as_a_side_effect_and_records_the_signal(tmp_path):
    """No discoverable SDK near a synthetic `boardYaml` -- the write must
    fail gracefully (never raise), report itself via `on_output` (never
    silent), and leave the recorder at the honest `(False, None)`."""
    from tan.commands.build import execute as execute_module

    execute_module.reset_last_manifest_write()
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": null}}'
    lines = []
    execute_slices(parse_build_plan(_plan(cmd)), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lines.append)

    assert execute_module.last_manifest_write() == (False, None)
    assert any("system-manifest.yaml" in line for line in lines), lines
    assert not (tmp_path / "build" / "system-manifest.yaml").exists()


def test_reset_last_manifest_write_clears_a_previous_calls_leftover(tmp_path):
    from tan.commands.build import execute as execute_module

    execute_module._last_manifest_write = execute_module._ManifestWriteSignal(
        manifest_written=True, native_sim_target=True
    )
    execute_module.reset_last_manifest_write()
    assert execute_module.last_manifest_write() == (False, None)


def test_execute_slices_writes_a_real_manifest_when_the_sdk_is_discoverable(tmp_path):
    """Calling `execute_slices` directly (as every other test in this file
    does) passes no `sdk_root=`, so this exercises the same fallback a real
    `tan build` invocation hits only when `--sdk-root` is unresolved: falling
    back to discovering one near the plan's own `boardYaml`. Requires
    `ALP_SDK_ROOT`, same gate as `tests/commands/test_build_manifest.py`'s own
    real-SDK case.

    `build_root` is `tmp_path`, NOT `board_yaml.parent` -- the two are
    independent (SDK discovery walks up from `board_yaml`; the manifest is
    written under `build_root`), so pointing `build_root` at the real
    checkout bought no extra coverage and instead wrote a `build/` directory
    into it (and the real example's own `board.yaml.parent` is a read-only
    checkout in this project's own dev setup)."""
    if SDK is None:
        import pytest

        pytest.skip("set ALP_SDK_ROOT to an alp-sdk checkout to exercise the real write")

    board_yaml = SDK / "examples" / "aen" / "aen-analog-validate" / "board.yaml"
    assert board_yaml.is_file(), "fixture example moved -- update the path"

    from tan.commands.build import execute as execute_module

    manifest_path = tmp_path / "build" / "system-manifest.yaml"
    execute_module.reset_last_manifest_write()
    plan_json = f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": {json.dumps(str(board_yaml))},
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": []
    }}"""
    execute_slices(parse_build_plan(plan_json), build_root=tmp_path,
                   env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None)
    assert manifest_path.is_file()
    manifest_written, native_sim_target = execute_module.last_manifest_write()
    assert manifest_written is True
    assert native_sim_target is False  # AEN801 has no native_sim slice
