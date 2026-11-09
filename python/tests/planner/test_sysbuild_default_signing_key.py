# SPDX-License-Identifier: Apache-2.0
"""An AEN MCUboot sysbuild plan passes the SDK-anchored dev signing key
(tan-cli#1569, mirrors alp-sdk#3033 / #2950).

The curated `zephyr/sysbuild/aen/sysbuild.conf` carries no key, so without
an absolute `-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE` Zephyr silently signs with
MCUboot's bundled `root-ec-p256.pem`, which the factory MCUboot rejects.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set -- tan.planner needs a bound alp-sdk "
           "checkout carrying the curated AEN sysbuild base and dev key.",
)

_FLAG = "-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="

_AEN = """
    som: {sku: E1M-AEN801, hw_rev: r2}
    preset: e1m-evk
    cores:
      m55_hp: {os: zephyr, app: ./src}
    boot:
      method: mcuboot
"""
_AEN_KEY = _AEN + """      signing:
        algorithm: ecdsa_p256
        key_file: keys/dev.pem
"""


def _cmds(tmp_path, body):
    from tan.planner import emit_build_plan, load_board_yaml

    (tmp_path / "src").mkdir(exist_ok=True)
    path = tmp_path / "board.yaml"
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    plan = json.loads(emit_build_plan(
        load_board_yaml(path), board_yaml=path, build_root=tmp_path / "build"))
    return json.dumps(plan["slices"]).replace('\\"', '"')


def test_default_dev_key_is_passed_sdk_anchored(tmp_path, _bound_sdk):  # noqa: F811
    assert (_FLAG + '"${SDK_ROOT}/keys/mcuboot_shared_dev_ecdsa_p256.pem"'
            in _cmds(tmp_path, _AEN))


def test_explicit_key_file_wins_and_is_project_anchored(tmp_path, _bound_sdk):  # noqa: F811
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "dev.pem").write_text("pem", encoding="utf-8")
    cmds = _cmds(tmp_path, _AEN_KEY)
    assert _FLAG + '"${PROJECT_ROOT}/keys/dev.pem"' in cmds
    assert "mcuboot_shared_dev_ecdsa_p256" not in cmds


def test_missing_explicit_key_file_is_refused(tmp_path, _bound_sdk):  # noqa: F811
    from tan.planner import OrchestratorError

    with pytest.raises(OrchestratorError, match="not a file"):
        _cmds(tmp_path, _AEN_KEY)
