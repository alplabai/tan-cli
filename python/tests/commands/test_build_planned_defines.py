# SPDX-License-Identifier: Apache-2.0
"""`tan build -D NAME=VALUE` on a PLANNED build (tan-cli#1382)."""
import json
import os
import stat
import sys

import pytest

from tan.core.plain_zephyr_plan import plain_zephyr_plan
from tan.core.user_defines import (
    UserDefineError,
    apply_user_defines,
    user_defines_problem,
)
from tests.commands.test_build_command import envelope_of, run_tan

BOARD = "alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake west is a POSIX script")


def _plan(*extra_args, second=False):
    plan = json.loads(plain_zephyr_plan("/x/app", BOARD, []))
    plan["slices"][0]["command"]["args"] += ["-DEXTRA_CONF_FILE=/b/alp.conf", *extra_args]
    if second:
        other = json.loads(json.dumps(plan["slices"][0]))
        other["coreId"] = "rtss_hp"
        other["buildDir"] = "build/rtss_hp-zephyr"
        other["command"]["cwd"] = "build/rtss_hp-zephyr"
        plan["slices"].append(other)
    return json.dumps(plan)


def _args(text, idx=0):
    return json.loads(text)["slices"][idx]["command"]["args"]


def test_user_define_goes_after_plan_args():
    text, cores = apply_user_defines(_plan(), ["-DSHIELD=imx335", "-DCONFIG_X=y"])
    args = _args(text)
    assert cores == ["rtss_he"]
    assert args[-2:] == ["-DSHIELD=imx335", "-DCONFIG_X=y"]
    assert args.index("-DEXTRA_CONF_FILE=/b/alp.conf") < args.index("-DSHIELD=imx335")


def test_extra_conf_and_overlay_append_not_replace():
    text, _ = apply_user_defines(
        _plan("-DEXTRA_DTC_OVERLAY_FILE=/b/a.overlay"),
        ["-DEXTRA_CONF_FILE=u.conf", "-DEXTRA_CONF_FILE=v.conf", "-DEXTRA_DTC_OVERLAY_FILE=u.overlay"],
    )
    args = _args(text)
    assert "-DEXTRA_CONF_FILE=/b/alp.conf;u.conf;v.conf" in args
    assert "-DEXTRA_DTC_OVERLAY_FILE=/b/a.overlay;u.overlay" in args
    assert not any(a in ("-DEXTRA_CONF_FILE=u.conf", "-DEXTRA_DTC_OVERLAY_FILE=u.overlay") for a in args)


def test_append_key_absent_from_plan_is_added():
    text, _ = apply_user_defines(_plan(), ["-DEXTRA_DTC_OVERLAY_FILE=u.overlay"])
    assert _args(text)[-1] == "-DEXTRA_DTC_OVERLAY_FILE=u.overlay"


def test_sysbuild_prefixed_key_appends():
    text, _ = apply_user_defines(_plan("-Dapp_EXTRA_CONF_FILE=/b/x"), ["-Dapp_EXTRA_CONF_FILE=u"])
    assert "-Dapp_EXTRA_CONF_FILE=/b/x;u" in _args(text)


def test_core_scopes_to_one_slice_and_default_is_all():
    both, cores = apply_user_defines(_plan(second=True), ["-DSHIELD=s"])
    assert cores == ["rtss_he", "rtss_hp"] and "-DSHIELD=s" in _args(both, 1)
    one, cores = apply_user_defines(_plan(second=True), ["-DSHIELD=s"], ["rtss_hp"])
    assert cores == ["rtss_hp"] and "-DSHIELD=s" not in _args(one, 0) and "-DSHIELD=s" in _args(one, 1)


def test_unknown_core_and_no_zephyr_slice_refused():
    with pytest.raises(UserDefineError) as e:
        apply_user_defines(_plan(), ["-DA=1"], ["nope"])
    assert e.value.code == "build.invalid-argument"
    plan = json.loads(_plan())
    plan["slices"][0]["backend"] = "yocto"
    with pytest.raises(UserDefineError) as e:
        apply_user_defines(json.dumps(plan), ["-DA=1"])
    assert e.value.code == "build.define-no-target"


@pytest.mark.parametrize("d", ["-DBOARD=x", "-DPython3_EXECUTABLE=/usr/bin/python", "-DBOARD:STRING=x"])
def test_reserved_keys_refused(d):
    assert user_defines_problem([d])[0] == "build.define-reserved"


def test_append_key_needs_a_value_and_shield_passes():
    assert user_defines_problem(["-DEXTRA_CONF_FILE"])[0] == "build.invalid-argument"
    assert user_defines_problem(["-DSHIELD=imx335", "-DCONFIG_A=y"]) is None


@pytest.fixture
def world(tmp_path):
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("# marker\n", encoding="utf-8")
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    if sys.platform != "win32":
        (bindir / "python").symlink_to(sys.executable)
    log = tmp_path / "west.json"
    west = bindir / "west"
    west.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:]}}, open({str(log)!r}, 'w'))\n"
        "os.makedirs('build', exist_ok=True)\n"
        "open('build/CMakeCache.txt', 'w').write('ZEPHYR_BASE:PATH=/z\\n')\n",
        encoding="utf-8",
    )
    west.chmod(west.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "out"
    out.mkdir()
    (out / "board.yaml").write_text("version: 1\n", encoding="utf-8")
    plan = out / "plan.json"
    app = tmp_path / "app"
    app.mkdir()
    plan.write_text(_plan().replace("/x/app", str(app)), encoding="utf-8")
    return {"sdk": sdk, "bin": bindir, "log": log, "out": out, "plan": plan}


def _run(world, *extra):
    path = f"{world['bin']}{os.pathsep}{os.environ.get('PATH', '')}"
    return run_tan(
        "build", "--plan-from", str(world["plan"]),
        "--board-yaml", str(world["out"] / "board.yaml"), "--sdk-root", str(world["sdk"]),
        "--build-root", str(world["out"]), "--execute", "--format", "json", *extra,
        cwd=world["out"], env_overrides={"PATH": path},
    )


@posix_only
def test_planned_build_passes_defines_to_west_and_records_them(world):
    env = envelope_of(_run(world, "-D", "SHIELD=imx335", "-D", "EXTRA_CONF_FILE=u.conf"))
    assert env["exitCode"] == 0, env["issues"]
    assert world["log"].exists(), json.dumps(env["data"])[:1500]
    argv = json.loads(world["log"].read_text())["argv"]
    assert argv[-1] == "-DSHIELD=imx335"
    assert "-DEXTRA_CONF_FILE=/b/alp.conf;u.conf" in argv
    assert env["data"]["defines"] == {
        "args": ["-DSHIELD=imx335", "-DEXTRA_CONF_FILE=u.conf"],
        "slices": ["rtss_he"],
    }


@posix_only
def test_no_defines_leaves_envelope_without_defines(world):
    env = envelope_of(_run(world))
    assert "defines" not in env["data"]


@posix_only
@pytest.mark.parametrize("flag", ["BOARD=x", "Python3_EXECUTABLE=/p", "A=${SDK_ROOT}"])
def test_cli_refuses_reserved_and_token_defines(world, flag):
    env = envelope_of(_run(world, "-D", flag))
    assert env["exitCode"] == 2 and not world["log"].exists()
    assert env["issues"][0]["code"] in ("build.define-reserved", "build.invalid-argument")


@posix_only
def test_cli_unknown_core_and_core_without_define(world):
    env = envelope_of(_run(world, "-D", "A=1", "--core", "nope"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.invalid-argument"
    env = envelope_of(_run(world, "--core", "rtss_he"))
    assert env["exitCode"] == 2 and env["issues"][0]["code"] == "build.invalid-argument"
