# SPDX-License-Identifier: Apache-2.0
"""tan-cli#309 end to end (upstream tan-cli #97): `execute_slices` must
refuse an `os: zephyr` slice whose CMake configure never loaded Zephyr's
boilerplate, whatever the tool's own exit code says -- reproduces the
reported defect (`tan init --template minimal-app` -> `tan build` reporting
`[+] ok` for a plain host binary) at the `execute_slices` layer. The guard
stays regardless of whether any one template's own CMake is correct today --
it is the safety net for every `os: zephyr` slice, not a substitute for a
correct scaffold. `minimal-app`'s own CMake shape (root `find_package(Zephyr
...)` + `project()`, `src/CMakeLists.txt` contributing via `target_sources(app
...)`, and `board.yaml`'s `app:` pointed at the directory that actually holds
`find_package`) is fixed at the source in `tan/core/scaffold.py`; see
`python/tan/templates/vendored/MANIFEST.md`'s "`minimal-app`" section for the
two-part defect (CMake shape, then `app:` target) and why this guard is still
worth keeping regardless."""
import json
import sys

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices

PYTHON = json.dumps(sys.executable)
_TRUE_CMD = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'


def _plan(command: str, backend: str = "zephyr", args_extra: str = "") -> str:
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


def test_a_zephyr_slice_that_exits_0_with_no_zephyr_evidence_is_reported_failed(tmp_path):
    """The tan-cli#309 defect itself: exit code 0 alone must not be reported
    `succeeded` for a declared `os: zephyr` core whose build dir shows no
    sign Zephyr's CMake boilerplate ever ran."""
    out = execute_slices(
        parse_build_plan(_plan(_TRUE_CMD)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "failed"
    # The tool really did exit 0 -- the guard refuses the RESULT, not the
    # exit, so `exit_code` must stay the real, honest value.
    assert out[0].exit_code == 0
    assert out[0].output_artefact is None
    assert out[0].build_dir is None


def test_the_refusal_message_matches_the_oracle_verbatim(tmp_path):
    """Confirmed against the compiled oracle's own runtime string (`cargo
    test -p alp-tan-cli --bin tan
    native_execute_refuses_a_zephyr_slice_whose_configure_never_loaded_zephyr
    -- --nocapture`), not transcribed from source alone.

    tan-cli#510 review (MAJOR 1): a round-1 attempt at this fix appended a
    trailing `` (`<tool>` resolved to `<resolved>`) `` note to `message`
    itself, which put the resolved path into `data.slices[].reason` AND the
    persisted `system-manifest.yaml` `slices[].reason` -- corrupting both
    contracts. The resolved path now lives in its own field
    (`SliceOutcome.resolved_tool` / `data.slices[].resolvedTool`), never in
    `message`, so this string is unconditionally the oracle's, byte for
    byte, with nothing appended."""
    out = execute_slices(
        parse_build_plan(_plan(_TRUE_CMD)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].message == (
        "core `c1` is declared `os: zephyr`, but the build in `build/c1` never loaded "
        "Zephyr (no ZEPHYR_BASE in its CMakeCache.txt and no zephyr/ output) — its "
        "CMakeLists.txt must call `find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})` "
        "before `project()`; without it CMake builds a plain host binary, not firmware. "
        "Scaffold a working app with `tan init --template zephyr-app`, or point the "
        "core's `app:` at one that does."
    )
    # `sys.executable` is already absolute, so identity and resolution are
    # the same string -- `resolved_tool` stays `None` (suppressed, not just
    # unappended): showing it here would be information-free by
    # construction, the same reasoning `SliceOutcome.resolved_tool`'s own
    # docstring gives.
    assert out[0].resolved_tool is None


def test_a_non_zephyr_backend_is_never_subject_to_the_guard(tmp_path):
    out = execute_slices(
        parse_build_plan(_plan(_TRUE_CMD, backend="baremetal")),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "succeeded"


def test_a_genuine_build_failure_is_reported_on_its_own_terms_not_the_guard(tmp_path):
    """A real nonzero exit must not be swallowed into the guard's own
    message -- the guard only ever fires on an otherwise-`succeeded` slice."""
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "raise SystemExit(3)"], "cwd": "build/c1"}}'
    out = execute_slices(
        parse_build_plan(_plan(cmd)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "failed"
    assert out[0].exit_code == 3
    assert "os: zephyr" not in (out[0].message or "")


def test_a_real_zephyr_build_dir_is_accepted(tmp_path):
    """The other half of the guard: it must not fail a REAL Zephyr build.
    `ZEPHYR_BASE:` in the build dir's own `CMakeCache.txt` is the primary
    signal, pinned here WITHOUT the directory fallback -- a verified real
    Zephyr slice carries `ZEPHYR_BASE:PATH=...` and has no `zephyr/` dir."""
    nested = tmp_path / "build" / "c1" / "build"
    nested.mkdir(parents=True)
    (nested / "CMakeCache.txt").write_text(
        "CMAKE_PROJECT_NAME:STATIC=zephyr\nZEPHYR_BASE:PATH=/work/zephyr\n", encoding="utf-8"
    )
    out = execute_slices(
        parse_build_plan(_plan(_TRUE_CMD)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "succeeded", out[0].message


def test_a_sysbuild_slice_evidenced_one_level_down_is_accepted(tmp_path):
    """`--sysbuild` nests the real per-image Zephyr build one directory
    deeper than its own superbuild top level -- without the one-level-down
    look this would fail a correct V2N sysbuild build."""
    nested = tmp_path / "build" / "c1" / "build" / "alp_app"
    (nested / "zephyr").mkdir(parents=True)
    out = execute_slices(
        parse_build_plan(_plan(_TRUE_CMD)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "succeeded", out[0].message


def test_the_guard_stands_down_when_the_build_dir_is_overridden(tmp_path):
    """Same refusal `resolve_zephyr_artefact` and the sdk-switch wipe already
    make: with `-d`/`--build-dir` west wrote somewhere this cannot see, so
    there is no evidence to judge -- the guard must not fail a build it
    cannot inspect."""
    cmd = json.dumps(
        {"tool": sys.executable, "args": ["-c", "pass", "-d", "../elsewhere"], "cwd": "build/c1"}
    )
    out = execute_slices(
        parse_build_plan(_plan(cmd)),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
    )
    assert out[0].status == "succeeded", out[0].message
