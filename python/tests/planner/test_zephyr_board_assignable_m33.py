# SPDX-License-Identifier: Apache-2.0
"""V2N CM33 board tree: an `assignable:` instance that carries an `m33:` block
in `core-ownership.yaml` is declared `disabled` with a metadata-derived
pinctrl group (hand-ported from the two `emit_zephyr_board` cases of alp-sdk's
`tests/scripts/test_core_ownership.py`, #2673).

Real-SDK-gated: copies the bound checkout's `metadata/` to edit one fact.
"""

from __future__ import annotations

import shutil

import pytest
import yaml

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound metadata/ tree. A SKIP about "
           "the missing root, not a pass.",
)


def _emit(root):
    from tan.planner.zephyr_board import emit_zephyr_board

    return emit_zephyr_board("E1M-V2N101", "m33_sm", root)


def _meta_with_m33_uart0(tmp_path):
    """A metadata tree whose e1m_uart0 DEFAULTS to the M33 (the real one is
    a55-only until the P51 pull-up is bench-proven).  Its PFC functions come
    from the real SoC linux_dt.UART0.pinmux."""
    from tan.planner.paths import METADATA_ROOT

    root = tmp_path / "metadata"
    shutil.copytree(METADATA_ROOT, root)
    f = root / "e1m_modules" / "v2n" / "core-ownership.yaml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    e = doc["assignable"]["e1m_uart0"]
    e["candidates"] = ["a55", "m33"]
    e["default"] = "m33"
    e["m33"] = {"dt_label": "sci0", "alias": "alp-uart9", "kconfig": ["CONFIG_SERIAL=y"],
                "pinctrl": {"group_label": "sci0_asg_pins", "node": "sci0_asg",
                            "child_node": "sci0-asg-pinmux"}}
    f.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root


def test_board_tree_declares_assignable_node_disabled_with_metadata_pinctrl(tmp_path):
    files = _emit(_meta_with_m33_uart0(tmp_path))
    pin = next(v for k, v in files.items() if k.endswith("-pinctrl.dtsi"))
    dts = next(v for k, v in files.items() if k.endswith(".dts"))
    assert "sci0_asg_pins: sci0_asg {" in pin
    assert "RZV_PINMUX(PORT_05, 0, 1)" in pin and "RZV_PINMUX(PORT_05, 1, 1)" in pin
    assert ("&sci0 {\n\tpinctrl-0 = <&sci0_asg_pins>;\n\tpinctrl-names = \"default\";"
            "\n\tstatus = \"disabled\";") in dts


def test_board_tree_unchanged_without_m33_blocks():
    from tan.planner.paths import METADATA_ROOT

    assert "sci0_asg" not in "".join(_emit(METADATA_ROOT).values())
