# SPDX-License-Identifier: Apache-2.0
"""Regression tests for tan-cli#655: `tan build` reported success while
silently ignoring a newly-added `app.overlay` -- CMake's own `DTC_OVERLAY_FILE`/
`CONF_FILE` cache (set the first time Zephyr's `configuration_files.cmake`
auto-discovery ran, `if(NOT DEFINED ...)`) never re-discovers a file that did
not exist yet at that FIRST configure, no matter how many times `tan build`
reconfigures afterward -- and it already reconfigures every time (every
zephyr slice's command carries at least one `-D`, so `west build`'s own
`cmake_opts`-non-empty check always sets `run_cmake = True`). Verified against
a real Zephyr 4.4.1 tree, not inferred -- see `tan.commands.build.execute.
_maybe_reset_stale_configure_cache`'s docstring for the full measurement.

Two layers of test here, mirroring how the sibling sdk-switch-pristine guard
(issue #52) is covered in `test_execute.py`:

- Direct unit tests of `_maybe_reset_stale_configure_cache` /
  `tan.commands.build.configure_inputs.discover_configure_inputs` -- fast,
  deterministic, no spawnable stand-in needed.
- One `execute_slices`-level end-to-end test with a real spawned process
  (a stand-in `west`) that captures its OWN argv, proving the `-U` flags
  actually reach the spawn -- not just that the helper function returns the
  right thing in isolation.
"""
import json
import os
import shutil
import sys
from pathlib import Path

from tan.core.build_plan import parse_build_plan
from tan.commands.build.configure_inputs import (
    discover_configure_inputs,
    read_configure_inputs_stamp,
    write_configure_inputs_stamp,
)
import tan.commands.build.execute as execute_module
from tan.commands.build.execute import execute_slices
from tan.core.bootstrap import venv_layout

PYTHON = json.dumps(sys.executable)


def _plan(command: str, app_dir: str, backend: str = "zephyr") -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"}},
      "slices": [{{
        "coreId": "c1", "backend": "{backend}", "buildDir": "build/c1", "appDir": {json.dumps(app_dir)},
        "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
        "command": {command}, "env": {{}}, "envAppendPath": {{}}
      }}]
    }}"""


def _configure(slice_dir: Path) -> None:
    """Fake what a prior successful `west build` left behind: a configured
    nested build dir (mirrors `test_execute.py`'s own `_configure` for the
    sdk-switch-pristine guard)."""
    build = slice_dir / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "CMakeCache.txt").write_text("# fake cmake cache\n", encoding="utf-8")


# ------------------------------------------------------ discover_configure_inputs


def test_discovers_app_overlay_at_the_app_root(tmp_path):
    (tmp_path / "app.overlay").write_text("/{ };\n", encoding="utf-8")
    (tmp_path / "prj.conf").write_text("", encoding="utf-8")
    assert discover_configure_inputs(tmp_path) == frozenset({"app.overlay", "prj.conf"})


def test_discovers_nested_boards_and_socs_fragments(tmp_path):
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "native_sim.overlay").write_text("", encoding="utf-8")
    (tmp_path / "boards" / "native_sim.conf").write_text("", encoding="utf-8")
    (tmp_path / "socs").mkdir()
    (tmp_path / "socs" / "posix.conf").write_text("", encoding="utf-8")
    found = discover_configure_inputs(tmp_path)
    assert found == frozenset({
        "boards/native_sim.overlay", "boards/native_sim.conf", "socs/posix.conf",
    })


def test_missing_app_dir_is_the_empty_set_not_an_error(tmp_path):
    assert discover_configure_inputs(tmp_path / "does-not-exist") == frozenset()


def test_stamp_round_trips_including_the_empty_set(tmp_path):
    write_configure_inputs_stamp(tmp_path, frozenset())
    assert read_configure_inputs_stamp(tmp_path) == frozenset()
    write_configure_inputs_stamp(tmp_path, frozenset({"app.overlay", "boards/x.conf"}))
    assert read_configure_inputs_stamp(tmp_path) == frozenset({"app.overlay", "boards/x.conf"})


def test_unstamped_reads_as_no_signal(tmp_path):
    assert read_configure_inputs_stamp(tmp_path) is None


# ---------------------------------------- _maybe_reset_stale_configure_cache


def test_first_configure_lays_down_a_baseline_without_resetting(tmp_path):
    """No `CMakeCache.txt` yet -- nothing cached to poison, so this call must
    never emit a reset; it only stamps this configure's own discoverable set."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lambda s: None
    )
    assert args == []
    assert issues == []
    assert read_configure_inputs_stamp(cwd) == frozenset({"prj.conf"})


def test_new_overlay_triggers_the_narrow_cache_reset(tmp_path):
    """The reported case: `app.overlay` appears after the first configure."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    write_configure_inputs_stamp(cwd, discover_configure_inputs(app_dir))

    (app_dir / "app.overlay").write_text("/{ };\n", encoding="utf-8")
    lines = []
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lines.append
    )
    # Exactly the narrow pair -- see the function's own docstring for why
    # EXTRA_DTC_OVERLAY_FILE/EXTRA_CONF_FILE must NOT be in this list.
    assert args == ["-UDTC_OVERLAY_FILE", "-UCONF_FILE"]
    assert len(issues) == 1
    assert issues[0].code == "build.configure-cache-reset"
    assert "app.overlay" in issues[0].message
    assert any("app.overlay" in line for line in lines)
    assert read_configure_inputs_stamp(cwd) == frozenset({"prj.conf", "app.overlay"})


def test_removed_overlay_also_triggers_the_reset(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    (app_dir / "app.overlay").write_text("/{ };\n", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    write_configure_inputs_stamp(cwd, discover_configure_inputs(app_dir))

    (app_dir / "app.overlay").unlink()
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lambda s: None
    )
    assert args == ["-UDTC_OVERLAY_FILE", "-UCONF_FILE"]
    assert "removed" in issues[0].message


def test_unchanged_set_resets_nothing(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    write_configure_inputs_stamp(cwd, discover_configure_inputs(app_dir))

    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lambda s: None
    )
    assert args == []
    assert issues == []


def test_edited_content_alone_does_not_trigger_the_reset(tmp_path):
    """A same-path content edit is Zephyr's OWN `CMAKE_CONFIGURE_DEPENDS`
    territory (the tracked file's mtime bump alone triggers a correct
    reconfigure) -- this guard only tracks set MEMBERSHIP, so an edit with no
    path added/removed must not spuriously reset."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "app.overlay").write_text("/{ };\n", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    write_configure_inputs_stamp(cwd, discover_configure_inputs(app_dir))

    (app_dir / "app.overlay").write_text("/{ /delete-node/ &foo; };\n", encoding="utf-8")
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lambda s: None
    )
    assert args == []
    assert issues == []


def test_non_zephyr_backend_is_never_reset(tmp_path):
    """`DTC_OVERLAY_FILE`/`CONF_FILE` are Zephyr-CMake-specific -- a
    yocto/baremetal slice must never get these flags."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    write_configure_inputs_stamp(cwd, frozenset())
    (app_dir / "app.overlay").write_text("", encoding="utf-8")
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "baremetal", lambda s: None
    )
    assert args == []
    assert issues == []


def test_no_app_dir_is_never_reset(tmp_path):
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)
    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, None, "zephyr", lambda s: None
    )
    assert args == []
    assert issues == []


def test_never_stamped_but_already_configured_self_heals_with_one_reset(tmp_path):
    """A build dir that predates this feature (`CMakeCache.txt` exists, no
    `.tan-configure-inputs` stamp) reads as changed DELIBERATELY -- the
    conservative default, mirroring the sdk-switch-pristine guard's own
    `test_missing_stamp_on_an_already_configured_dir_self_heals_to_pristine`."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    cwd = tmp_path / "build" / "c1"
    _configure(cwd)  # no stamp written

    args, issues = execute_module._maybe_reset_stale_configure_cache(
        "c1", cwd, str(app_dir), "zephyr", lambda s: None
    )
    assert args == ["-UDTC_OVERLAY_FILE", "-UCONF_FILE"]
    assert len(issues) == 1


# --------------------------------------------------------- end-to-end (spawn)


def _plant_spawnable_west(west_path: Path) -> None:
    """Same recipe `test_execute.py::_plant_spawnable_west` uses -- duplicated
    rather than imported, matching this test suite's own convention of
    self-contained test files (see e.g. `test_execute_zephyr_guard.py`,
    `test_execute_post_commands.py`, each defining their own `PYTHON`/`_plan`
    rather than importing another test module's private helpers)."""
    west_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        interpreter_dir = Path(sys.executable).parent
        for dll in interpreter_dir.glob("*.dll"):
            shutil.copy(dll, west_path.parent / dll.name)
        shutil.copy(sys.executable, west_path)
    else:
        west_path.write_text(
            f'#!/bin/sh\nexec {json.dumps(sys.executable)} "$@"\n', encoding="utf-8"
        )
        os.chmod(west_path, 0o755)


def _argv_capturing_build_script(argv_probe: Path) -> str:
    """A minimal stand-in for zephyr's `build` west extension: appends its
    OWN argv (one JSON line per invocation, so two `execute_slices` calls in
    the same test can be told apart) to `argv_probe`, then leaves behind the
    `CMakeCache.txt` `ZEPHYR_BASE:` entry the tan-cli#309
    `zephyr_boilerplate_loaded` guard needs to accept the slice as `ok` --
    without it every call here would report `failed` for an unrelated
    reason, masking what THIS test is about. Not a real cmake/west: the
    point under test is what argv `execute_slices` hands to the spawned
    process, not Zephyr's own configure behaviour (that half is measured
    directly against a real Zephyr tree -- see this module's own docstring)."""
    return (
        "import os, sys, json\n"
        f"probe = {json.dumps(str(argv_probe))}\n"
        "with open(probe, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + chr(10))\n"
        "d = sys.argv[sys.argv.index('-d') + 1] if '-d' in sys.argv else os.path.join(os.getcwd(), 'build')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "with open(os.path.join(d, 'CMakeCache.txt'), 'w') as fh:\n"
        "    fh.write('ZEPHYR_BASE:PATH=/fake/zephyr\\n')\n"
        "sys.exit(0)\n"
    )


def test_e2e_new_overlay_appearing_forces_the_cache_reset_on_the_next_spawn(
    tmp_path, monkeypatch
):
    """The full tan-cli#655 regression, at the `execute_slices` layer with a
    REAL spawned process (not a call to the helper function directly): build
    once (baseline), add `app.overlay`, build again -- the SECOND spawn's
    OWN argv must carry `-UDTC_OVERLAY_FILE -UCONF_FILE`; the first spawn's
    must not, and a third build with nothing changed must not either.

    FAILS BEFORE THE FIX (verified): with `_maybe_reset_stale_configure_cache`
    and its call site removed, the second capture is byte-identical to the
    first -- no `-U` flags ever appended -- which is exactly the reported
    defect's mechanism (a real Zephyr `west build` given that same
    unchanged argv keeps discovering nothing, per this module's own
    docstring)."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)

    real_ws = tmp_path / "ws"
    sdk_root_dir = real_ws / "alp-sdk"
    sdk_root_dir.mkdir(parents=True)
    (real_ws / ".west").mkdir()
    (real_ws / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")

    build_root = tmp_path / "proj"
    app_dir = build_root / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "prj.conf").write_text("", encoding="utf-8")

    layout = venv_layout(os.name == "nt")
    _plant_spawnable_west(build_root / ".venv" / layout.bin_dir / layout.west)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    argv_probe = real_ws / "argv.txt"
    (real_ws / "build").write_text(_argv_capturing_build_script(argv_probe), encoding="utf-8")

    cmd = json.dumps(
        {"tool": "west", "args": ["build", "-b", "board", str(app_dir)], "cwd": "build/c1"}
    )
    plan = parse_build_plan(_plan(cmd, str(app_dir)))

    def _build():
        return execute_slices(
            plan, build_root=build_root, env_lookup=lambda k: None, gap_fillers=[],
            on_output=lambda s: None, sdk_root=str(sdk_root_dir),
        )

    # 1) First build: no CMakeCache.txt yet -- baseline only, no reset.
    out1 = _build()
    assert out1[0].status == "succeeded", out1[0].message
    first_argv = json.loads(argv_probe.read_text(encoding="utf-8").splitlines()[0])
    assert "board" in first_argv and str(app_dir) in first_argv
    assert "-UDTC_OVERLAY_FILE" not in first_argv
    assert "-UCONF_FILE" not in first_argv

    # 2) Customer adds app.overlay by hand (the reported repro step) and
    # re-runs `tan build` -- the build dir is ALREADY configured now (the
    # first spawn's own CMakeCache.txt write above), so this is exactly the
    # "already-configured project" shape the issue names.
    (app_dir / "app.overlay").write_text("/{ };\n", encoding="utf-8")
    out2 = _build()
    assert out2[0].status == "succeeded", out2[0].message
    second_argv = json.loads(argv_probe.read_text(encoding="utf-8").splitlines()[1])
    assert "-UDTC_OVERLAY_FILE" in second_argv
    assert "-UCONF_FILE" in second_argv
    # The reset flags are APPENDED, never clobbering the rest of the
    # command -- west's own `-b board <app_dir>` positional shape is still
    # there too, strictly BEFORE the appended `-U` pair.
    assert "board" in second_argv and str(app_dir) in second_argv
    assert second_argv.index("-UDTC_OVERLAY_FILE") > second_argv.index("board")
    assert second_argv[-2:] == ["-UDTC_OVERLAY_FILE", "-UCONF_FILE"]

    # The JSON-envelope path (VS Code extension, envelope-only) must also
    # see build #2's reset as a coded issue, not stderr-only -- checked
    # right after build #2, before build #3 freshly overwrites it.
    issues = execute_module.last_configure_cache_issues()
    assert any(i.code == "build.configure-cache-reset" for i in issues)

    # 3) A third build with nothing changed since #2 must NOT reset again.
    out3 = _build()
    assert out3[0].status == "succeeded", out3[0].message
    third_argv = json.loads(argv_probe.read_text(encoding="utf-8").splitlines()[2])
    assert "-UDTC_OVERLAY_FILE" not in third_argv
    assert execute_module.last_configure_cache_issues() == []
