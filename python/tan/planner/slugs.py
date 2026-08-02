#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Kconfig / board-define slug derivation -- a shared leaf.

The slug + symbol helpers the per-slice config emitters (alp.conf, cmake-args)
share: the board-define slug, the on-module / helper-firmware Kconfig slug
derivation (+ the non-chip field set they skip), and the two Kconfig-mapping
tables (peripheral -> symbol bundle, chip -> subsystem dependency). A leaf that
reaches only for `paths`, so every per-slice emitter pulls them from one place
instead of duplicating. Extracted as a #285 leaf seam (the paths.py /
memregion.py move) ahead of the kconfig emitter.

`peripheral_kconfig` is RELOCATED from alp-sdk's `scripts/alp_registries.py` and
`_CHIP_SUBSYSTEMS` from `scripts/alp_project_emit/__init__.py` -- the last two
edges this leaf and `kconfig.py` made into the SDK's Python. The peripheral
table is still READ from the SDK's `metadata/registries/peripheral-kconfig.json`
(the fact stays there, ADR-0017); `_CHIP_SUBSYSTEMS` has no registry file to
read from today, so it moves with its one and only consumer -- see the note
above the table.
"""

from __future__ import annotations

import functools
import json

from .paths import METADATA_ROOT

PERIPHERAL_KCONFIG_REGISTRY = METADATA_ROOT / "registries" / "peripheral-kconfig.json"


@functools.lru_cache(maxsize=1)
def peripheral_kconfig() -> dict[str, tuple[str, ...]]:
    """Return board.yaml peripheral tokens -> Zephyr Kconfig symbol bundles."""
    data = json.loads(PERIPHERAL_KCONFIG_REGISTRY.read_text(encoding="utf-8"))
    return {
        token: tuple(symbols)
        for token, symbols in data["peripherals"].items()
    }


def _is_tbd(value: object) -> bool:
    """Case/whitespace-insensitive `TBD` placeholder match ("tbd", "Tbd",
    " TBD " all match, alp-sdk #1048) -- the two chip-slug extractors below
    both used a bare `== "TBD"` before, so an all-lowercase board.yaml
    value silently fell through as a real chip slug instead of being
    dropped as the placeholder it was hand-typed to mean."""
    return isinstance(value, str) and value.strip().upper() == "TBD"


def _board_define_slug(name: str) -> str:
    """'E1M-X-EVK' -> 'E1M_X_EVK': the ALP_BOARD_* compile-define suffix.

    Mirrors gen_board_header._board_slug (lower + '-'->'_') then uppercases
    for the C macro. Used by <alp/board.h>'s board-selection facade.
    """
    return name.lower().replace("-", "_").upper()


def _som_define_slug(sku: str) -> str:
    """'E1M-AEN801' -> 'E1M_AEN801': the ALP_SOM_* compile-define suffix.

    Same transform as _board_define_slug; kept as its own name so the
    SoM-selection define (per-SKU capability restrictions in
    <alp/soc_caps.h>, gen_soc_caps.som_token) reads distinctly from the
    board facade define at call sites.
    """
    return _board_define_slug(sku)


# on_module fields that carry non-chip-slug values — skip them when
# walking the block for chip-driver enables.  Numeric fields, silicon
# identifiers, and structured sub-blocks are excluded by name rather
# than by type so the logic stays explicit and easy to audit.
_ON_MODULE_NON_CHIP_FIELDS: frozenset[str] = frozenset({
    "silicon",             # e.g. "renesas:rzv2n:n44" — SoC identifier, not a driver
    "ethernet_phy_count",  # integer count, not a chip slug
    "i2c_devices",         # sub-block: handled by extracting chip: entries below
    "ospi_memories",       # sub-block: storage parts (flash/HyperRAM); MPNs have no chips/ driver -- excluded like nor_flash/emmc below
    # Storage-class fields encode the SoC controller / peripheral name
    # that reaches the on-module storage (e.g. `nor_flash: xspi` -> the
    # NOR flash is wired to the xSPI controller; `emmc: sd0` -> eMMC on
    # SD/MMC controller 0).  They are routing annotations, not chip
    # slugs, and have no `chips/<part>/` driver behind them; emitting
    # them as CHIP_<NAME> trips the Zephyr build with an undefined-symbol
    # warning (no CONFIG_ALP_SDK_CHIP_XSPI / SD0 declaration exists).
    "nor_flash",
    "emmc",
})


def _slugs_from_on_module(on_module: dict) -> list[str]:
    """Extract unique, non-TBD chip slugs from an ``on_module:`` block.

    Walks every scalar field that is NOT in ``_ON_MODULE_NON_CHIP_FIELDS``,
    then recurses into the ``i2c_devices`` sub-block (extracting the
    ``chip:`` field from each device entry).  ``ospi_memories`` and the
    ``hyperram`` block are storage parts (NOR flash / HyperRAM) with no
    ``chips/<part>/`` driver, so their MPNs are NOT extracted as chip
    slugs (emitting them as ``CONFIG_ALP_SDK_CHIP_<X>`` would trip
    Zephyr's undefined-symbol guard).  Duplicate slugs and values of
    ``TBD`` / ``null`` are silently dropped.

    Returns a sorted, deduplicated list of slug strings.
    """
    seen: set[str] = set()

    def _add(val: object) -> None:
        if not val or _is_tbd(val):
            return
        if not isinstance(val, str):
            return
        seen.add(val)

    # 1. Scalar fields — every key whose value is a plain string and
    #    is not in the exclusion list.
    for key, val in on_module.items():
        if key in _ON_MODULE_NON_CHIP_FIELDS:
            continue
        if isinstance(val, str):
            _add(val)

    # 2. i2c_devices sub-block — each bus entry contains a `devices:`
    #    list; extract the `chip:` field from each device.
    #    Devices marked `assembled: optional` are DNI (do-not-install)
    #    on some builds and must NOT be auto-enabled as chip drivers —
    #    the customer explicitly enables them via `board.populated:`.
    i2c_buses = on_module.get("i2c_devices")
    if isinstance(i2c_buses, dict):
        for _bus, bus_entry in i2c_buses.items():
            if not isinstance(bus_entry, dict):
                continue
            for dev in bus_entry.get("devices") or []:
                if isinstance(dev, dict):
                    if dev.get("assembled") == "optional":
                        continue
                    _add(dev.get("chip"))

    return sorted(seen)


def _slugs_from_helper_firmware(helper_firmware: list) -> list[str]:
    """Extract unique, non-TBD chip slugs from a ``helper_firmware:`` list.

    Each entry is a dict; we pull the ``chip:`` field.  TBD values and
    missing fields are skipped.  Returns a sorted, deduplicated list.
    """
    seen: set[str] = set()
    for entry in helper_firmware or []:
        if not isinstance(entry, dict):
            continue
        chip = entry.get("chip")
        if chip and not _is_tbd(chip):
            seen.add(chip)
    return sorted(seen)


_PERIPHERAL_KCONFIG: dict[str, tuple[str, ...]] = peripheral_kconfig()


# Chip name -> Zephyr subsystem CONFIG_* keys the chip driver
# depends on.  Mirrors the `depends on ...` line in each
# `config ALP_SDK_CHIP_<NAME>` entry in zephyr/Kconfig: enabling
# a chip driver doesn't auto-select its subsystem, so the loader
# emits the matching `CONFIG_<SUBSYS>=y` here.
#
# RELOCATED from alp-sdk's `scripts/alp_project_emit/__init__.py`, whose only
# consumer was ever `_emit_chips` in this package's `kconfig.py`.
#
# DEBT, stated plainly: this IS a hardware fact and it now lives in `tan`
# (ADR-0017 / I-26 says facts stay in `metadata/**`). It moved because no
# metadata file carries it -- a chip manifest's `bus: i2c` cannot distinguish
# `lsm6dso` -> `("I2C",)` from `tas2563` -> `("I2C", "GPIO")`, so deriving it
# from existing metadata would change the emitted Kconfig. Retires when a
# registry (`metadata/registries/chip-subsystems.json`, alongside
# peripheral-kconfig.json) declares it and this table becomes a loader like
# `peripheral_kconfig` above.
_CHIP_SUBSYSTEMS: dict[str, tuple[str, ...]] = {
    # GPIO-only
    "button_led":         ("GPIO",),
    "cam_mux_pi3wvr626":  ("GPIO",),
    # SPI + GPIO
    "ssd1331":            ("SPI", "GPIO"),
    "cc3501e":            ("SPI", "GPIO"),
    # I2C + GPIO
    "tas2563":            ("I2C", "GPIO"),
    # I2C-only
    "lsm6dso":            ("I2C",),
    "ssd1306":            ("I2C",),
    "bme280":             ("I2C",),
    "lis2dw12":           ("I2C",),
    "ov5640":             ("I2C",),
    "icm42670":           ("I2C",),
    "bmi323":             ("I2C",),
    "bmp581":             ("I2C",),
    "tmp112":             ("I2C",),
    "rv3028c7":           ("I2C",),
    "optiga_trust_m":     ("I2C",),
    "eeprom_24c128":      ("I2C",),
    "tcal9538":           ("I2C",),
    "ina236":             ("I2C",),
    # pdm_mic helper has no subsystem dep declared in Kconfig
    # (uses <alp/i2s.h> when enabled at v0.2+).
    # v0.5 §D.AI batch -- 18 vision / display / accelerator chips.
    "ov2640":             ("I2C",),
    "ov5645":             ("I2C",),
    "ov7670":             ("I2C",),
    "ov9281":             ("I2C",),
    "ar0234":             ("I2C",),
    "imx219":             ("I2C",),
    "imx477":             ("I2C",),
    "gc2145":             ("I2C",),
    "ti_ds90ub953_954":   ("I2C",),
    "maxim_max9295_9296": ("I2C",),
    "st7789":             ("SPI", "GPIO"),
    "ili9341":            ("SPI", "GPIO"),
    "ili9488":            ("SPI", "GPIO"),
    "ra8875":             ("SPI",),
    "sh1106":             ("I2C",),
    "il3820":             ("SPI", "GPIO"),
    "gdew0154t8":         ("SPI", "GPIO"),
    "hailo_8l":           ("GPIO",),
    # v0.5 §D.industrial batch -- 18 industrial sensing / control chips.
    "bmp390":             ("I2C",),
    "ms5611":             ("I2C",),
    "lps22hb":            ("I2C",),
    "vl53l1x":            ("I2C",),
    "vl53l5cx":           ("I2C",),
    "a02yyuw":            ("SERIAL",),
    "drv8833":            ("PWM",),
    "drv8825":            ("PWM", "GPIO"),
    "tmc2209":            ("SERIAL",),
    "a4988":              ("PWM", "GPIO"),
    "as5048a_b":          ("I2C",),
    "mt6701":             ("I2C",),
    "hx711":              ("GPIO",),
    "max31855":           ("SPI",),
    "max31865":           ("SPI",),
    "tsl2591":            ("I2C",),
    "qmc5883l":           ("I2C",),
    "veml7700":           ("I2C",),
    # v0.5 §D.iot batch -- 9 IoT / connectivity chips.
    "quectel_bg95":       ("SERIAL",),
    "quectel_bg77":       ("SERIAL",),
    "ublox_sara_r5":      ("SERIAL",),
    "semtech_sx1262":     ("SPI", "GPIO"),
    "semtech_sx1276":     ("SPI", "GPIO"),
    "ublox_neo_m9n":      ("SERIAL",),
    "ublox_max_m10s":     ("SERIAL",),
    "atgm336h":           ("SERIAL",),
    "atecc608b":          ("I2C",),
    # v0.5 §D.audio batch -- 6 audio chips.
    "ics_43434":          (),                 # no Zephyr subsystem dep; sample flow via <alp/i2s.h>
    "inmp441":            (),
    "wm8960":             ("I2C",),
    "tlv320aic3204":      ("I2C",),
    "max98357a":          ("GPIO",),
    "es8388":             ("I2C",),
}
