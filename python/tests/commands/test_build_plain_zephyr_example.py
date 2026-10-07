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
        "if '--fail' in os.environ.get('FAKE_WEST', ''): sys.exit(3)\n"
        "elf = os.environ.get('FAKE_WEST_ELF_LMA')\n"
        "if elf:\n"
        "    import struct\n"
        "    os.makedirs('build/zephyr', exist_ok=True)\n"
        "    hdr = bytearray(52); hdr[:7] = b'\\x7fELF\\x01\\x01\\x01'\n"
        "    struct.pack_into('<I', hdr, 28, 52); struct.pack_into('<HH', hdr, 42, 32, 1)\n"
        "    ph = struct.pack('<8I', 1, 0, int(elf, 0), int(elf, 0), 16, 16, 5, 4)\n"
        "    open('build/zephyr/zephyr.elf', 'wb').write(bytes(hdr) + ph)\n",
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
    assert envelope_of(_run(world, "-D", "A=1"))["issues"][0]["code"] == "build.conflicting-flags"
    env = envelope_of(_run(world, "--board", "a b"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.invalid-argument"


# --- tan-cli#1370: the plain route writes system-manifest.yaml -----------------

SOC = {
    "ref": "alif:ensemble:e8",
    "soc_flash_base": 0x80000000,
    "cores": [{"id": "m55_he", "type": "cortex-m55", "zephyr_cpucluster": "rtss_he"},
              {"id": "m55_hp", "type": "cortex-m55", "zephyr_cpucluster": "rtss_hp"}],
    "variants": [{
        "order_code": "AE822FA0E5597LS0", "alp_module_skus": ["E1M-AEN803"],
        "debug": {"jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"},
                  "jlink_flash_device": "AE822FA0E5597LS0_M55_HE", "expect_dpidr": "0x4C013477"},
    }],
}
PRESET = """\
sku: E1M-AEN803
silicon: alif:ensemble:e8
silicon_variant: AE822FA0E5597LS0
topology:
  m55_hp:
    board: alp_e1m_aen803_m55_hp/ae822fa0e5597ls0/rtss_hp
    toolchain: arm-zephyr-eabi
  m55_he:
    board: alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he
    toolchain: arm-zephyr-eabi
"""


@pytest.fixture
def aen_world(world):
    md = world["sdk"] / "metadata"
    (md / "e1m_modules").mkdir(parents=True)
    (md / "e1m_modules" / "E1M-AEN803.yaml").write_text(PRESET, encoding="utf-8")
    soc = md / "socs" / "alif" / "ensemble"
    soc.mkdir(parents=True)
    (soc / "e8.json").write_text(json.dumps(SOC), encoding="utf-8")
    return world


def _manifest(world):
    import yaml

    return yaml.safe_load((world["out"] / "build" / "system-manifest.yaml").read_text())


@posix_only
def test_plain_route_writes_a_manifest_with_the_planner_core_id(aen_world):
    env = envelope_of(_run(aen_world, "--board", BOARD, env={"FAKE_WEST_ELF_LMA": "0x80012000"}))
    assert env["exitCode"] == 0 and env["issues"] == [], env
    assert [s["coreId"] for s in env["data"]["slices"]] == ["m55_he"]
    m = _manifest(aen_world)
    assert m["schema_version"] == 1 and m["hw_info"]["sku"] == "E1M-AEN803"
    (sl,) = m["slices"]
    assert (sl["core_id"], sl["os"], sl["board"], sl["status"]) == ("m55_he", "zephyr", BOARD, "ok")
    assert sl["output_artefact"].endswith("m55_he-zephyr/build/zephyr/zephyr.elf")
    assert sl["flash_method"] == "zephyr_west_flash"
    assert sl["flash_args"]["expect_dpidr"] == "0x4C013477"
    assert sl["flash_args"]["jlink_device"] == "Cortex-M55"
    assert sl["flash_args"]["jlink_flash_device"] == "AE822FA0E5597LS0_M55_HE"
    assert "slot0_load_address" in sl["flash_args"]


@posix_only
def test_plain_route_hp_target_maps_to_m55_hp(aen_world):
    env = envelope_of(_run(aen_world, "--board", BOARD.replace("_he", "_hp")))
    assert [s["coreId"] for s in env["data"]["slices"]] == ["m55_hp"]
    assert _manifest(aen_world)["slices"][0]["core_id"] == "m55_hp"


@posix_only
def test_itcm_linked_elf_is_ram_run_only(aen_world):
    env = envelope_of(_run(aen_world, "--board", BOARD, env={"FAKE_WEST_ELF_LMA": "0x0"}))
    assert env["exitCode"] == 0, env
    (sl,) = _manifest(aen_world)["slices"]
    assert sl["flash_method"] == "ram_run_only"
    assert sl["flash_args"] == {"expect_dpidr": "0x4C013477", "jlink_device": "Cortex-M55"}


@posix_only
def test_mram_linked_elf_keeps_the_flash_recipe(aen_world):
    _run(aen_world, "--board", BOARD, env={"FAKE_WEST_ELF_LMA": "0x80012000"})
    assert _manifest(aen_world)["slices"][0]["flash_method"] == "zephyr_west_flash"


@posix_only
def test_unknown_target_still_gets_a_one_slice_manifest(world):
    env = envelope_of(_run(world, "--board", "native_sim"))
    assert env["exitCode"] == 0 and env["issues"] == [], env
    (sl,) = _manifest(world)["slices"]
    assert (sl["core_id"], sl["flash_method"]) == ("native_sim", "zephyr_west_flash")


@posix_only
def test_tan_flash_dry_run_reads_the_plain_manifest(aen_world):
    _run(aen_world, "--board", BOARD, env={"FAKE_WEST_ELF_LMA": "0x80012000"})
    path = f"{aen_world['bin']}{os.pathsep}{os.environ.get('PATH', '')}"
    proc = run_tan(
        "flash", "--build-root", str(aen_world["out"] / "build"), "--dry-run", "--probe-serial", "123456789",
        "--format", "json", cwd=aen_world["out"], env_overrides={"PATH": path},
    )
    out = json.loads(proc.stdout)
    assert "flash.manifest-not-found" not in [i["code"] for i in out["issues"]], out
    (entry,) = out["data"]["entries"]
    assert (entry["id"], entry["method"]) == ("m55_he", "alif_mram_jlink"), entry
