# SPDX-License-Identifier: Apache-2.0
"""V2N/V2M CM33 board tree: the alp-sdk#2747 / #2685 / #2710 content, pinned
with NO alp-sdk checkout (tan-cli#1216).

The sibling `test_zephyr_board_v2n_gd32_pads_and_wdt.py` is real-SDK-gated, so
on the unbound `gates` job these values were measured by nothing. These tests
emit from a small committed copy of the metadata the emitter reads
(`tests/fixtures/v2n_board_dt_metadata/`: the E1M-V2N101 preset, the v2n
`supervisor-links.yaml` + `core-ownership.yaml`, and the RZ/V2N n44 SoC spec,
all as of alp-sdk `2d2a85333`), so the literals below are the upstream ones,
verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "v2n_board_dt_metadata"
SKU = "E1M-V2N101"


def _files(root: Path = ROOT) -> dict[str, str]:
    from tan.planner.zephyr_board import emit_zephyr_board

    return emit_zephyr_board(SKU, "m33_sm", root)


def _one(files: dict[str, str], suffix: str) -> str:
    return next(c for r, c in files.items() if r.endswith(suffix))


def test_sci0_rxd_carries_a_pull_up():
    pinctrl = _one(_files(), "-pinctrl.dtsi")
    assert "bias-pull-up;" in pinctrl
    # inside the console pin group, right after the RXD pinmux entry
    group = pinctrl.split("pinmux = ", 1)[1].split("};", 1)[0]
    assert "/* RXD */" in group and "bias-pull-up;" in group


def test_ram_console_node_and_kconfig():
    files = _files()
    dts = _one(files, ".dts")
    assert "ram_console: memory@9f710000 {" in dts
    assert 'compatible = "zephyr,memory-region";' in dts
    assert 'zephyr,memory-region = "RAM_CONSOLE";' in dts
    assert "zephyr,ram-console = &ram_console;" in dts
    defconfig = _one(files, "_defconfig")
    assert "CONFIG_RAM_CONSOLE=y\n" in defconfig
    assert "CONFIG_RAM_CONSOLE_BUFFER_SIZE=16384\n" in defconfig
    assert "CONFIG_LOG_PRINTK=n\n" in defconfig


def test_attn_is_routed_to_icu_tint_slot_31():
    dts = _one(_files(), ".dts")
    assert "&tint31" in dts
    assert "irqs = <&tint31 1>" in dts


def test_cm33_ns_to_a55_offset_is_carried_from_the_soc_spec():
    dts = _one(_files(), ".dts")
    assert "zephyr,user {" in dts
    assert "alp,cm33-ns-to-a55-offset = <" in dts


def test_twister_supported_lists_only_what_the_dts_enables():
    twister = _one(_files(), ".yaml")
    assert "supported:\n  - gpio\n  - spi\n" in twister
    assert "  - i2c" not in twister and "  - uart" not in twister


def _root_without(tmp_path: Path, mutate) -> Path:
    import shutil

    root = tmp_path / "meta"
    shutil.copytree(ROOT, root)
    soc = root / "socs" / "renesas" / "rzv2n" / "n44.json"
    spec = json.loads(soc.read_text(encoding="utf-8"))
    mutate(spec)
    soc.write_text(json.dumps(spec), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda s: s.pop("openamp_carveout"), "`openamp_carveout` is missing"),
        (lambda s: s["openamp_carveout"].pop("cm33_ns_base"), "`cm33_ns_base`"),
        (lambda s: s["openamp_carveout"].pop("regions"), "`regions`"),
        (lambda s: s["openamp_carveout"].pop("ram_console"), "`ram_console`"),
        (lambda s: s["openamp_carveout"]["ram_console"].pop("size"),
         "`ram_console.size`"),
    ],
)
def test_a_soc_spec_without_the_carveout_is_a_coded_refusal(tmp_path, mutate, field):
    from tan.planner.zephyr_board import ZephyrBoardEmitError

    root = _root_without(tmp_path, mutate)
    with pytest.raises(ZephyrBoardEmitError) as err:
        _files(root)
    assert field in str(err.value)
