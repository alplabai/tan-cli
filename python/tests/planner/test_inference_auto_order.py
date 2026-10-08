# SPDX-License-Identifier: Apache-2.0
"""SoM-declared AUTO accelerator order (alp-sdk#2677): a preset's
`inference.auto_order` reaches a Zephyr `alp.conf` as
`CONFIG_ALP_SDK_INFERENCE_AUTO_ORDER` and a Yocto `local.conf` as
`ALP_SDK_INFERENCE_AUTO_ORDER ?=`.

Real-SDK-gated: reads the bound checkout's SoM presets.
"""
from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound SoM presets. "
           "A SKIP about the missing root, not a pass.",
)


def _load(path, **kw):
    from tan.planner import load_board_yaml

    return load_board_yaml(path, **kw)


def _write(tmp_path, body):
    path = tmp_path / "board.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize("sku,order", [("E1M-V2M103", "deepx_dxm1,drpai,cpu"),
                                       ("E1M-V2N101", "drpai,cpu")])
def test_auto_order_reaches_yocto_local_conf(tmp_path, sku, order):
    from tan.planner import _slice_local_conf

    proj = _load(_write(tmp_path, f"som:\n  sku: {sku}\ncores:\n  a55_cluster:\n"
                                  "    os: yocto\n    image: alp-image-edge\n"))
    conf = _slice_local_conf(proj, proj.cores["a55_cluster"])
    assert f'ALP_SDK_INFERENCE_AUTO_ORDER ?= "{order}"' in conf.splitlines()


def test_auto_order_reaches_zephyr_kconfig_for_an_inference_slice(tmp_path):
    from tan.planner import _slice_alp_conf

    proj = _load(_write(tmp_path, "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n"
                                  "    os: zephyr\n    inference:\n"
                                  "      default_arena_kib: 512\n"))
    conf = _slice_alp_conf(proj, proj.cores["m55_hp"])
    assert 'CONFIG_ALP_SDK_INFERENCE_AUTO_ORDER="ethos_u,cpu"' in conf.splitlines()
