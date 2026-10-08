# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2791 (Refs): board.yaml `cameras:` -> Zephyr `-DSHIELD` / Yocto
`ALP_CAMERA_CAM<n>`, mirrored from `tan.planner.cameras` / `camera_owner`.

Real-SDK-gated: resolves against the bound checkout's real board presets and
`metadata/camera_modules/`. Ports a representative slice of alp-sdk's
`tests/scripts/test_orchestrate_cameras.py` (ownership, SHIELD, sysbuild
scoping, ALP_CAMERA_CAM0, and the `camera-select-failed` warning).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- cameras resolve against real presets.",
)

OV9281 = "innomaker_cam_ov9281"
GS = "raspberry_pi_global_shutter_camera"
SHIELD = f"e1m_evk_rpi_csi {OV9281}"

AEN = """
som: {{sku: E1M-AEN803}}
preset: e1m-evk
cores:
  a32_cluster: {{os: "off"}}
  m55_he: {{app: ./src}}
cameras:
  - {{connector: CAM0, module: {module}}}
"""
V2M = """
som: {{sku: E1M-V2M101}}
preset: e1m-x-evk
cores:
  a55_cluster: {{image: alp-image-edge}}
  m33_sm: {{app: ./cm33}}
cameras:
  - {{connector: CAM0, module: {module}}}
"""
BOOT = """
boot:
  method: mcuboot
  signing:
    algorithm: ecdsa_p256
    key_file: keys/mcuboot_shared_dev_ecdsa_p256.pem
"""


def _plan(tmp_path: Path, body: str, module: str, extra: str = "") -> dict:
    from tan.planner.buildplan import emit_build_plan
    from tan.planner.loader import load_board_yaml

    path = tmp_path / "board.yaml"
    path.write_text(
        textwrap.dedent(body).lstrip("\n").format(module=module) + extra,
        encoding="utf-8")
    return json.loads(emit_build_plan(
        load_board_yaml(path), board_yaml=path, build_root=Path("build")))


def _slice(plan: dict, core: str) -> dict:
    return next(s for s in plan["slices"] if s["coreId"] == core)


def _args(plan: dict, core: str) -> list[str]:
    return (_slice(plan, core)["command"] or {"args": []})["args"]


def _artefact(sl: dict, name: str) -> str:
    return next(c for c in sl["configArtefacts"]
                if c["path"].endswith("/" + name))["contents"]


def test_zephyr_owner_gets_one_shield_define_and_cmake_args(tmp_path) -> None:
    plan = _plan(tmp_path, AEN, OV9281)
    assert f"-DSHIELD={SHIELD}" in _args(plan, "m55_he")
    assert f"-DSHIELD={SHIELD}" in _artefact(
        _slice(plan, "m55_he"), "cmake-args.txt")
    assert not plan["warnings"]


def test_module_without_zephyr_shield_blocks_the_command(tmp_path) -> None:
    plan = _plan(tmp_path, AEN, "raspberry_pi_camera_module_2")
    assert _slice(plan, "m55_he")["command"] is None
    w = next(w for w in plan["warnings"] if w["code"] == "camera-select-failed")
    assert w["coreId"] == "m55_he" and "zephyr_shield" in w["message"]


def test_linux_owner_gets_alp_camera_cam0_and_cm33_no_shield(tmp_path) -> None:
    plan = _plan(tmp_path, V2M, GS)
    assert not plan["warnings"]
    conf = _artefact(_slice(plan, "a55_cluster"), "local.conf")
    assert f'ALP_CAMERA_CAM0 = "{GS}"' in conf
    cm33 = _args(plan, "m33_sm")
    assert cm33 and not any("SHIELD" in a for a in cm33)


def test_sysbuild_scopes_the_shield_to_the_app_image(tmp_path) -> None:
    # No CMakeLists.txt under ./src: the app dir resolves to the project dir.
    (tmp_path / "src").mkdir()
    (tmp_path / "CMakeLists.txt").write_text("", encoding="utf-8")
    plan = _plan(tmp_path, AEN, OV9281, extra=BOOT)
    args = _args(plan, "m55_he")
    assert "--sysbuild" in args
    assert f"-D{tmp_path.name}_SHIELD={SHIELD}" in args
    assert not any(a.startswith("-DSHIELD=") for a in args)
