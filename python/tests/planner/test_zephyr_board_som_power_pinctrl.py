# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2795 (Refs): the AEN `alp,som-power` node carries a
`pinctrl_som_power` group and a `pinctrl-0` state.

Real-SDK-gated: the pads come from `power_domains:` in the bound checkout's
`on-module-links.yaml` and the SKU's `on_module:` block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- the power pads resolve against real metadata.",
)


def _dtsi() -> str:
    from tan.planner.zephyr_board import emit_zephyr_board

    files = emit_zephyr_board("E1M-AEN803", "m55_he", Path(SDK) / "metadata")
    return "\n".join(files.values())


def test_pinctrl_group_precedes_the_node_and_is_referenced():
    text = _dtsi()
    group = text.index("pinctrl_som_power: pinctrl_som_power {")
    node = text.index("som_power: som-power {")
    assert group < node
    assert "\t\tpinctrl-0 = <&pinctrl_som_power>;" in text
    assert '\t\tpinctrl-names = "default";' in text
    assert "input-enable;" in text and "input-schmitt-enable;" in text


def test_pinmux_lists_sorted_unique_output_pads():
    import re

    text = _dtsi()
    line = next(l for l in text.splitlines()
                if l.strip().startswith("pinmux = <PIN_P")
                and "__LPGPIO" in l)
    pads = re.findall(r"PIN_P(\d+)_(\d)__(LPGPIO|GPIO)", line)
    keys = [(int(a), int(b)) for a, b, _ in pads]
    assert keys == sorted(set(keys))
    assert all((kind == "LPGPIO") == (port == "15") for port, _, kind in pads)
