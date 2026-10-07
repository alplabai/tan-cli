# SPDX-License-Identifier: Apache-2.0
"""``tan build --project <dir> --board <target>`` (tan-cli#1359).

A board.yaml-less Zephyr example builds as ONE synthesised Zephyr slice through
the ordinary build pipeline. A fake ``west`` on PATH records its argv/env, so
nothing real is compiled.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tan.core.plain_zephyr_plan import (
    board_target_problem,
    core_id_for,
    normalise_defines,
    plain_zephyr_plan,
)
from tests.commands.test_build_command import envelope_of, run_tan

BOARD = "alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake west is a POSIX script")


@pytest.fixture
def world(tmp_path):
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("# marker\n", encoding="utf-8")
    app = sdk / "examples" / "aen" / "demo"
    app.mkdir(parents=True)
    (app / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    # The SDK's canonical `<sdk-parent>/.venv`: tan prefers its `west` and bakes
    # its `python` into `${PYTHON}`, the same resolution a planned build gets.
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(sys.executable) if sys.platform != "win32" else None
    log = tmp_path / "west.json"
    west = bindir / "west"
    west.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}, "
        f"open({str(log)!r}, 'w'))\n"
        "os.makedirs('build', exist_ok=True)\n"
        "open('build/CMakeCache.txt', 'w').write('ZEPHYR_BASE:PATH=/z\\n')\n"
        "if '--fail' in os.environ.get('FAKE_WEST', ''): sys.exit(3)\n",
        encoding="utf-8",
    )
    west.chmod(west.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "out"
    out.mkdir()
    return {"sdk": sdk, "app": app, "bin": bindir, "log": log, "out": out}


def _run(world, *extra, env=None):
    path = f"{world['bin']}{os.pathsep}{os.environ.get('PATH', '')}"
    return run_tan(
        "build", "--project", str(world["app"]), "--sdk-root", str(world["sdk"]),
        "--build-root", str(world["out"]), "--format", "json", *extra,
        cwd=world["out"], env_overrides={"PATH": path, **(env or {})},
    )


def test_plan_shape_is_one_zephyr_slice():
    plan = json.loads(plain_zephyr_plan("/x/app", BOARD, ["-DA=1"]))
    (sl,) = plan["slices"]
    assert (sl["coreId"], sl["backend"], sl["buildDir"]) == ("rtss_he", "zephyr", "build/rtss_he-zephyr")
    assert sl["command"]["args"][:5] == ["build", "-b", BOARD, "/x/app", "--"]
    assert sl["command"]["args"][-1] == "-DA=1" and sl["command"]["cwd"] == "build/rtss_he-zephyr"
    assert sl["artifacts"]["elf"] == "build/rtss_he-zephyr/build/zephyr/zephyr.elf"


@pytest.mark.parametrize("board,ok", [(BOARD, True), ("native_sim", True), ("a b", False), ("", False), ("x;rm", False)])
def test_board_target_validation(board, ok):
    assert (board_target_problem(board) is None) is ok


def test_define_with_plan_token_is_refused(world):
    assert normalise_defines(["A=${SDK_ROOT}"])[1] is not None
    env = envelope_of(_run(world, "--board", BOARD, "-D", "A=${PYTHON}"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.invalid-argument"


def test_defines_normalised_and_validated():
    assert normalise_defines(["A=1", "-DB:STRING=x"]) == (["-DA=1", "-DB:STRING=x"], None)
    assert normalise_defines(["not valid"])[1] is not None
    assert core_id_for("native_sim") == "native_sim"


@posix_only
def test_builds_through_west_with_normal_envelope(world):
    proc = _run(world, "--board", BOARD, "-D", "AEN_NPU_MODEL=ethos-u55-128")
    env = envelope_of(proc)
    assert proc.returncode == 0 and env["ok"] is True and env["exitCode"] == 0, env
    (sl,) = env["data"]["slices"]
    assert sl["coreId"] == "rtss_he" and sl["backend"] == "zephyr" and sl["status"] == "ok"
    rec = json.loads(world["log"].read_text())
    args = rec["argv"]
    assert args[:4] == ["build", "-b", BOARD, str(world["app"])]
    assert args[4] == "--" and "-DAEN_NPU_MODEL=ethos-u55-128" in args
    assert any(a.startswith("-DPython3_EXECUTABLE=") and "${" not in a for a in args)
    assert "build.manifest-write-failed" not in [i["code"] for i in env["issues"]]
    assert "-d" not in args  # an explicit -d would disable the pristine guards
    assert rec["cwd"].endswith("build/rtss_he-zephyr")
    assert rec["env"]["ALP_SDK_ROOT"] == str(world["sdk"])
    assert str(world["sdk"]) in rec["env"]["EXTRA_ZEPHYR_MODULES"]


@posix_only
def test_failing_west_reports_slice_failed(world):
    env = envelope_of(_run(world, "--board", BOARD, env={"FAKE_WEST": "--fail"}))
    assert env["ok"] is False and env["exitCode"] == 1
    assert "build.slice-failed" in [i["code"] for i in env["issues"]]


@posix_only
def test_pristine_is_accepted_on_the_plain_route(world):
    env = envelope_of(_run(world, "--board", BOARD, "--pristine"))
    assert env["exitCode"] == 0, env


def test_no_board_yaml_no_board_names_both_routes(world):
    env = envelope_of(_run(world))
    (issue,) = [i for i in env["issues"] if i["code"] == "build.plan-unavailable"]
    assert "--board-yaml <PATH>" in issue["message"] and "--board <zephyr-board-target>" in issue["message"]


def test_board_with_board_yaml_flag_conflicts(world):
    env = envelope_of(_run(world, "--board", BOARD, "--board-yaml", "x.yaml"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.conflicting-flags"


def test_board_with_existing_board_yaml_conflicts(world):
    (world["app"] / "board.yaml").write_text("x: 1\n", encoding="utf-8")
    env = envelope_of(_run(world, "--board", BOARD))
    assert env["issues"][0]["code"] == "build.conflicting-flags"


def test_define_without_board_and_bad_board_refused(world):
    env = envelope_of(_run(world, "--board", "a b"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.invalid-argument"


@posix_only
def test_plain_route_stamps_and_records_defines(world):
    env = envelope_of(_run(world, "--board", BOARD, "-D", "SHIELD=a"))
    assert env["exitCode"] == 0, env["issues"]
    assert env["data"]["defines"] == {"args": ["-DSHIELD=a"], "slices": ["rtss_he"]}
    stamp = world["out"] / "build" / "rtss_he-zephyr" / "build" / ".tan-user-defines"
    assert stamp.read_text() == "SHIELD=a\n"
    env = envelope_of(_run(world, "--board", BOARD, "-D", "SHIELD=b"))
    assert "build.configure-cache-reset" in [i["code"] for i in env["issues"]]
