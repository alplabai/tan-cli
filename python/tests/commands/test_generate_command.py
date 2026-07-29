# SPDX-License-Identifier: Apache-2.0
"""`tan generate`: target resolution, output paths, and every refusal path.

The one committed conformance fixture pins only `board-yaml-missing`. These
cover what it cannot: the argument-shape refusals, the two guards that protect a
user's tree, and -- the whole point -- that no failure path escapes as a
traceback instead of an envelope.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tan.commands import generate_cmd
from tan.commands.generate_cmd import (
    ALL_EMIT_MODES,
    CORE_SCOPABLE_TARGETS,
    ZEPHYR_BOARD,
    GenerateError,
    _output_path,
    _overlay_would_overwrite,
    _scan_som_sku,
    generate,
    resolve_targets,
    zephyr_board_dir_name,
)
from tan.exit_codes import ExitCode

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_cli(argv, cwd):
    """Spawn the real CLI -- the only way to prove stdout carries exactly one
    JSON envelope and stderr stays empty under `--format json`."""
    return subprocess.run(
        [sys.executable, "-m", "tan", "generate", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)},
        check=False,
    )


def envelope_of(proc):
    assert proc.stderr.strip() == "", f"stderr must be empty under --format json:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def make_sdk(root: Path) -> Path:
    """A directory `_resolve_sdk_root` accepts. The refusal tests all fail out
    before the script is ever spawned, so its contents do not matter."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------


def test_bare_invocation_resolves_every_default_target():
    assert resolve_targets(None, False, None) == ALL_EMIT_MODES
    assert resolve_targets(None, True, None) == ALL_EMIT_MODES
    # zephyr-board is never folded into the default set: it hard-requires
    # --core and writes a directory, so a bare run has no path for it.
    assert ZEPHYR_BOARD not in ALL_EMIT_MODES


def test_single_target_resolves_to_itself():
    assert resolve_targets("cmake-args", False, None) == ("cmake-args",)
    # The Studio netlist handoff and the native_sim overlay must reach the SDK
    # spawn, not be rejected at the allowlist.
    assert resolve_targets("carrier-netlist", False, None) == ("carrier-netlist",)
    assert resolve_targets("native-sim-overlay", False, None) == ("native-sim-overlay",)


def test_unknown_target_is_a_validation_failure_not_internal():
    with pytest.raises(GenerateError) as err:
        resolve_targets("bogus", False, None)
    assert err.value.code == "generate.invalid-target"
    assert err.value.exit_code == ExitCode.VALIDATION_FAILURE
    assert "Unsupported generate target" in err.value.message


def test_zephyr_board_requires_core():
    # The FAILING case: no --core must be refused, not silently defaulted --
    # alp_project.py itself hard-requires it. And it must be exit 2, not the
    # exit 5 an ordinary usage mistake used to get.
    with pytest.raises(GenerateError) as err:
        resolve_targets(ZEPHYR_BOARD, False, None)
    assert err.value.exit_code == ExitCode.VALIDATION_FAILURE
    assert "--core" in err.value.message
    assert resolve_targets(ZEPHYR_BOARD, False, "m55_hp") == (ZEPHYR_BOARD,)


@pytest.mark.parametrize("target", CORE_SCOPABLE_TARGETS)
def test_core_is_accepted_for_the_scopable_targets(target):
    assert resolve_targets(target, False, "m55_hp") == (target,)


@pytest.mark.parametrize(
    "target,all_targets",
    [
        # Targets the SDK never reads --core for at all...
        ("carrier-netlist", False),
        ("native-sim-overlay", False),
        # ...one that accepts and then ignores it...
        ("os-topology", False),
        # ...and the default/--all sets, which mix core-scoped and core-blind
        # targets in one run.
        (None, False),
        ("cmake-args", True),
    ],
)
def test_core_is_refused_where_it_would_do_nothing(target, all_targets):
    with pytest.raises(GenerateError) as err:
        resolve_targets(target, all_targets, "m55_hp")
    assert "--core" in err.value.message


# --------------------------------------------------------------------------
# Output paths
# --------------------------------------------------------------------------


def test_output_paths_match_the_documented_conventions():
    ws = Path("/ws")
    assert _output_path(ws, "zephyr-conf", None).as_posix().endswith(
        "build/generated/alp.conf"
    )
    assert _output_path(ws, "west-libraries", None).as_posix().endswith(
        "build/generated/alp-west-libs.yml"
    )
    assert _output_path(ws, "hw-info-h", None).as_posix().endswith(
        "build/generated/alp_hw_info_build.h"
    )
    # Zephyr auto-discovers boards/<board>.overlay in the app SOURCE tree, NOT
    # build/generated -- put it in the wrong place and native_sim GPIO never
    # resolves.
    assert _output_path(ws, "native-sim-overlay", None).as_posix().endswith(
        "boards/native_sim_native_64.overlay"
    )


def test_output_path_uses_the_native_separator_throughout():
    """The FAILING case an `os.path.join` of the whole `/`-separated literal
    produces: one envelope's `written[]` mixing `build/generated/alp.conf` with
    `build\\boards\\<dir>`. Asserts the RENDERED string, because Windows path
    parsing treats `/` as a separator regardless of the raw bytes."""
    import os

    if os.sep == "/":
        pytest.skip("nothing to prove where '/' is already native")
    for mode in ALL_EMIT_MODES:
        rendered = str(_output_path(Path("."), mode, None))
        assert "/" not in rendered, f"{mode}: {rendered!r} carries a non-native '/'"


def test_zephyr_board_dir_name_folds_in_the_sku():
    assert zephyr_board_dir_name("E1M-AEN801", "m55_hp") == "alp_e1m_aen801_m55_hp"
    # The FAILING case a bare core id produced: two projects on the SAME core
    # but DIFFERENT SoMs collided in one build/boards/m55_hp/.
    assert zephyr_board_dir_name("E1M-AEN901", "m55_hp") != zephyr_board_dir_name(
        "E1M-AEN801", "m55_hp"
    )
    assert zephyr_board_dir_name("BOGUS-SKU", "m55_hp") is None


def test_scan_som_sku_reads_the_nested_key_without_pyyaml():
    assert _scan_som_sku("som:\n  sku: E1M-AEN801\n") == "E1M-AEN801"
    assert _scan_som_sku("som:\n  sku: 'E1M-V2N101'\n") == "E1M-V2N101"
    # A `sku:` under some OTHER top-level block is not the SoM's.
    assert _scan_som_sku("cores:\n  sku: nope\n") is None
    assert _scan_som_sku("som:\n") is None


def test_overlay_guard_fires_only_on_an_existing_unforced_overlay(tmp_path):
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "native_sim_native_64.overlay").write_text("x", encoding="utf-8")
    assert _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), False)
    assert not _overlay_would_overwrite(tmp_path, ("native-sim-overlay",), True)
    assert not _overlay_would_overwrite(tmp_path, ("cmake-args",), False)


# --------------------------------------------------------------------------
# End-to-end refusals: an envelope with a code, never a traceback
# --------------------------------------------------------------------------


def test_missing_board_yaml_is_exit_2_with_the_frozen_message(tmp_path):
    proc = run_cli(["--format", "json"], cwd=tmp_path)
    assert proc.returncode == 2
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.board-yaml-missing"
    assert env["data"] == {
        "schemaVersion": "1",
        "targets": [],
        "written": [],
        "failed": [],
    }
    # `sdk` is absent, never null.
    assert "sdk" not in env


def test_missing_board_yaml_is_checked_before_a_bad_target(tmp_path):
    """An asymmetry worth pinning: the oracle checks the board FIRST, so a bad
    `--target` in a directory with no board.yaml reports the missing board."""
    proc = run_cli(["--target", "bogus", "--format", "json"], cwd=tmp_path)
    env = envelope_of(proc)
    assert env["issues"][0]["code"] == "generate.board-yaml-missing"


def test_unresolved_sdk_is_a_refusal_not_a_crash(tmp_path):
    project = tmp_path / "deep" / "project"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    proc = run_cli(["--sdk-root", str(tmp_path / "nope"), "--format", "json"], cwd=project)
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.sdk-root-unresolved"


def test_bad_target_is_a_refusal_once_the_board_and_sdk_resolve(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(["--sdk-root", str(sdk), "--target", "bogus", "--format", "json"], cwd=project)
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.invalid-target"


def test_non_e1m_sku_is_refused_before_naming_a_board_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: BOGUS-SKU\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        [
            "--sdk-root", str(sdk),
            "--target", ZEPHYR_BOARD,
            "--core", "m55_hp",
            "--format", "json",
        ],
        cwd=project,
    )
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.board-sku-unresolved"


def test_unreadable_board_yaml_is_board_sku_unresolved_not_a_traceback(tmp_path):
    """Non-UTF-8 bytes where a SKU should be. The oracle swallows the read
    failure into "no SKU"; what must never happen is a traceback on stdout."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_bytes(b"som:\n  sku: \xff\xfe\x00bad\n")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        [
            "--sdk-root", str(sdk),
            "--target", ZEPHYR_BOARD,
            "--core", "m55_hp",
            "--format", "json",
        ],
        cwd=project,
    )
    assert proc.returncode == 2
    assert envelope_of(proc)["issues"][0]["code"] == "generate.board-sku-unresolved"


def test_overlay_overwrite_is_exit_3(tmp_path):
    project = tmp_path / "project"
    (project / "boards").mkdir(parents=True)
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    (project / "boards" / "native_sim_native_64.overlay").write_text("tuned", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    proc = run_cli(
        ["--sdk-root", str(sdk), "--target", "native-sim-overlay", "--format", "json"],
        cwd=project,
    )
    assert proc.returncode == 3
    assert envelope_of(proc)["issues"][0]["code"] == "generate.would-overwrite"
    # And the hand-tuned file is still there.
    assert (project / "boards" / "native_sim_native_64.overlay").read_text() == "tuned"


def _call_generate(**kwargs) -> int:
    """Invoke the command in-process, returning its exit code. In-process
    because the emitter stand-ins below must run under THIS interpreter, and
    `_planner_python` deliberately names a PATH interpreter -- monkeypatching it
    is the seam, not a test-only env var in production code."""
    defaults = dict(
        target=None,
        all_targets=False,
        core=None,
        force=False,
        project=None,
        board_yaml=None,
        sdk_root=None,
        output_format="text",
        verbose=False,
    )
    with pytest.raises(typer.Exit) as exit_info:
        generate(**{**defaults, **kwargs})
    return int(exit_info.value.exit_code)


def test_a_failing_emit_reports_the_sdk_stderr_and_exit_3(tmp_path, monkeypatch, capsys):
    """The delegation path, with a stand-in for the SDK script: tan reports
    WHAT the SDK said, it does not paraphrase or swallow it."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys\nsys.stderr.write('alp_project: kaboom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)

    code = _call_generate(
        target="cmake-args", sdk_root=str(sdk), output_format="json"
    )
    assert code == 3
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["failed"] == ["cmake-args"]
    assert env["data"]["written"] == []
    assert env["issues"][0]["code"] == "generate.emit-failed"
    assert "kaboom" in env["issues"][0]["message"]


def test_a_successful_emit_reports_the_relative_written_path(tmp_path, monkeypatch, capsys):
    """The happy path, exercised against a stand-in emitter: tan reports the
    workspace-RELATIVE output path, and creates nothing itself -- the SDK's own
    `_write_or_print` makes the parent directories."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    sdk = make_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "alp_project.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text('generated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(generate_cmd, "_planner_python", lambda: sys.executable)
    assert _call_generate(target="cmake-args", sdk_root=str(sdk), output_format="json") == 0
    env = json.loads(capsys.readouterr().out.strip())
    # Workspace-RELATIVE, never the absolute host path the envelope would
    # otherwise leak into a golden.
    assert env["data"]["written"] == [os.path.join("build", "generated", "alp-cmake-args.txt")]
    assert env["data"]["failed"] == []
    written = project / "build" / "generated" / "alp-cmake-args.txt"
    assert written.read_text(encoding="utf-8") == "generated\n"
