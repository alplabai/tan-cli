# SPDX-License-Identifier: Apache-2.0
import json
import sys
import time

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices

# A JSON string literal (quotes included) for the running interpreter --
# `sys.executable` is `C:\Python311\python.exe` on Windows, and interpolating
# it into an f-string JSON literal directly (`"{sys.executable}"`) produces
# an invalid `\P`/`\p` escape. json.dumps() escapes it correctly on every
# platform.
PYTHON = json.dumps(sys.executable)


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
