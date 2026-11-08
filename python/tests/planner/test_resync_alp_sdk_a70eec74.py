# SPDX-License-Identifier: Apache-2.0
"""Hand-ported alp-sdk deltas 65583f78 -> a70eec74 (tan-cli#1585, #1546).

Each test fails against the unported code: #2984 (AEN storage ownership,
per-app lptimer0, HE-only peripherals dtsi), #2951 (V2N zephyr,entropy) and
#3082 (per-HW-rev route guards in the board header).
"""

from __future__ import annotations

import pytest

# `bound_sdk_root` binds the real checkout when ALP_SDK_ROOT is set and a
# synthetic root otherwise, so the two pure tests run on every CI leg.
from tests.planner._baremetal_support import SDK, bound_sdk_root  # noqa: F401

needs_sdk = pytest.mark.skipif(
    SDK is None, reason="ALP_SDK_ROOT is not set -- emit tests need a real alp-sdk checkout"
)


def _dts(sku: str, role: str) -> str:
    from tan.planner.zephyr_board import emit_zephyr_board

    files = emit_zephyr_board(sku, role, SDK / "metadata")
    return next(v for k, v in files.items() if k.endswith(".dts"))


def test_storage_partition_is_disabled_for_a_core_outside_accessible_from():
    from tan.planner.zephyr_board import _aen_unowned_partitions

    mm = [{"name": "storage", "accessible_from": ["m55_he"]}]
    assert _aen_unowned_partitions(mm, "hp") == {"storage"}
    assert _aen_unowned_partitions(mm, "he") == set()
    assert _aen_unowned_partitions(None, "hp") == set()
    assert _aen_unowned_partitions([{"name": "storage"}], "hp") == set()


@needs_sdk
def test_e8_he_dts_includes_he_peripherals_and_disables_lptimer0():
    he = _dts("E1M-AEN801", "m55_he")
    assert "#include <alif/ensemble_e8_he_peripherals.dtsi>" in he
    assert "&lptimer0 {\n\tstatus = \"disabled\";\n};" in he


@needs_sdk
def test_e8_hp_dts_has_no_he_peripherals_include():
    hp = _dts("E1M-AEN801", "m55_hp")
    assert "ensemble_e8_he_peripherals.dtsi" not in hp
    assert "&lptimer0 {" in hp


@needs_sdk
def test_v2n_dts_chooses_the_gd32_trng_entropy_node():
    dts = _dts("E1M-V2N101", "m33_sm")
    assert "zephyr,entropy = &gd32_trng;" in dts
    assert 'gd32_trng: gd32-trng {\n\t\tcompatible = "alp,gd32-trng";' in dts


def test_board_header_hw_rev_blocks_are_not_parsed_as_base_routes(tmp_path):
    from tan.planner.project_emit.dts import _parse_board_macros

    hdr = tmp_path / "b.h"
    hdr.write_text(
        "#define EVK_A ALP_E1M_I2C0\n"
        "#ifdef ALP_FOO_HW_REV_R3\n"
        "#define EVK_B ALP_E1M_I2C5\n"
        "#endif\n"
        "#define EVK_C ALP_E1M_I2C1\n"
    )
    got = _parse_board_macros(hdr)["I2C"]
    assert got == [("EVK_A", 0), ("EVK_C", 1)]
