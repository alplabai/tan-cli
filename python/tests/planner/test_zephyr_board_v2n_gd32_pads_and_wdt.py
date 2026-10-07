# SPDX-License-Identifier: Apache-2.0
"""V2N-family CM33 board `.dts`: the GD32 control pads and the CM33 watchdog
(hand-ported from alp-sdk `gen_zephyr_board.py`, #2692 and #2679, plus the
test that sits beside each upstream).

* The GD32 SWD/NRST/ATTN pads are a DEDICATED `alp,gd32-pads` node -- never
  entries of the positional `alp,pin-array`, whose index 0 is the GD32 SPI
  chip-select.
* `wdt0` is emitted DISABLED (an expiry resets the whole SoM) with alias
  `alp-wdt0`, taking base/size/clock from the SoC spec's `m33_sm`
  `watchdog` block rather than from a generator literal.

Real-SDK-gated: reads the bound checkout's `metadata/`.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound metadata/ tree. A SKIP about "
           "the missing root, not a pass.",
)

_SKUS = ("E1M-V2N101", "E1M-V2M101")


def _dts(sku: str) -> str:
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.zephyr_board import emit_zephyr_board

    files = emit_zephyr_board(sku, "m33_sm", METADATA_ROOT)
    return next(c for r, c in files.items() if r.endswith(".dts"))


@pytest.mark.parametrize("sku", _SKUS)
def test_gd32_pads_are_a_dedicated_node_not_alp_pins_entries(sku):
    dts = _dts(sku)
    pins = re.search(r"alp_pins: alp-pins \{(.*?)\n\t\};", dts, re.S)
    assert pins is not None
    assert re.findall(r"<&gpio\d+ \d+ [A-Z_]+>", pins.group(1)) == [
        "<&gpio9 7 GPIO_ACTIVE_LOW>"]
    node = re.search(r"gd32_pads: gd32-pads \{(.*?)\n\t\};", dts, re.S)
    assert node is not None
    assert 'compatible = "alp,gd32-pads"' in node.group(1)
    for prop, spec in (("swdio", "<&gpio7 0 GPIO_ACTIVE_HIGH>"),
                       ("swclk", "<&gpio7 1 GPIO_ACTIVE_HIGH>"),
                       ("nrst", "<&gpio7 4 GPIO_ACTIVE_HIGH>"),
                       ("attn", "<&gpio7 1 GPIO_ACTIVE_HIGH>")):
        assert f"{prop}-gpios = {spec};" in node.group(1)
    assert "&gpio7 {" in dts


@pytest.mark.parametrize("sku", _SKUS)
def test_v2n_family_dts_declares_the_cm33_watchdog(sku):
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.zephyr_board import _find_core

    soc = json.loads((METADATA_ROOT / "socs/renesas/rzv2n/n44.json")
                     .read_text(encoding="utf-8"))
    wdt = _find_core(soc, "m33_sm")["watchdog"]
    dts = _dts(sku)
    assert "alp-wdt0 = &wdt0;" in dts
    node = dts.split("wdt0: watchdog@", 1)[1].split("};", 1)[0]
    assert node.startswith(wdt["base"][2:] + " {")
    assert 'compatible = "renesas,rzv-wdt";' in node
    assert f"reg = <{wdt['base']} {wdt['size']}>;" in node
    assert f"clock-freq = <{wdt['counting_clock_hz']}>;" in node
    assert 'status = "disabled";' in node
