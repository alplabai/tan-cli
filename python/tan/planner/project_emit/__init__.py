# SPDX-License-Identifier: Apache-2.0
"""board.yaml emitters whose `--emit` front door is `scripts/alp_project.py`.

RELOCATED (was alp-sdk `scripts/alp_project_emit/`). The package keeps its shape
so the move stays reviewable as a move: `dts.py`, `native_sim.py`, `hw_info.py`,
`west_libs.py` and `bom_netlist.py` are the same files with their imports
repointed at `tan.planner`'s own leaves.

Two things alp-sdk's package root carried did NOT come across, because nothing
these five modules do reaches them and a second copy would be a second source of
truth:

* `_PERIPHERAL_KCONFIG` -- `slugs.peripheral_kconfig()` already reads
  `metadata/registries/peripheral-kconfig.json` here.
* `_CHIP_SUBSYSTEMS` -- already in `slugs.py`, with its I-26 justification.

`composed-route-table` is likewise absent: it is a debug surface `tan generate`
has no target for, so `bom_netlist.py` came across with the shared
`_composed_route_rows` helper and the `carrier-netlist` contract only.

What DOES live here is the one fact genuinely shared by two leaves: the canonical
E1M / E1M-X GPIO pad order, which `dts.py` (`--emit dts-overlay`) and
`native_sim.py` (`--emit native-sim-overlay`) must agree on byte for byte -- the
"Devicetree / overlay invariant" documented in the SDK's `e1m_pinout.h` /
`e1m_x_pinout.h`. Kept here, not duplicated in either leaf, so the two overlays
cannot drift.
"""

from __future__ import annotations

__all__ = ["_e1m_gpio_canonical", "_e1m_x_gpio_canonical"]


def _e1m_gpio_canonical() -> list[str]:
    """Return the 52 ALP_E1M_GPIO_<suffix> names in canonical index order."""
    names: list[str] = [f"IO{n}" for n in range(26)]      # 0..25
    names += [f"PWM{n}" for n in range(8)]                 # 26..33
    for e in range(4):                                     # 34..41
        names += [f"ENC{e}_X", f"ENC{e}_Y"]
    names += [f"ADC{n}" for n in range(8)]                 # 42..49
    names += ["DAC0", "DAC1"]                              # 50..51
    return names


def _e1m_x_gpio_canonical() -> list[str]:
    """Return the 99 ALP_E1M_X_GPIO_<suffix> names in canonical index order."""
    names: list[str] = [f"IO{n}" for n in range(36)]       # 0..35
    names += [f"PWM{n}" for n in range(8)]                 # 36..43
    for e in range(4):                                     # 44..51
        names += [f"ENC{e}_X", f"ENC{e}_Y"]
    names += [f"ADC{n}" for n in range(8)]                 # 52..59
    names += ["DAC0", "DAC1"]                              # 60..61
    names += ["I2C2_SDA", "I2C2_SCL", "I2C3_SDA", "I2C3_SCL"]
    names += ["SPI2_MISO", "SPI2_MOSI", "SPI2_SCLK", "SPI2_CS0", "SPI2_CS1"]
    names += ["CAN1_H", "CAN1_L"]
    names += [f"LCD_B{n}" for n in range(24)]
    names += ["LCD_HSYNC", "LCD_VSYNC"]                    # 97..98
    return names
