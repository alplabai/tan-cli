# SPDX-License-Identifier: Apache-2.0
"""som-preset v2 (alp-sdk#2024): every region `som_metadata.resolve_memory_map`
DERIVES states its `write_authority` explicitly.

Hand-ported from alp-sdk's `tests/scripts/test_som_preset_v2_schema.py::
test_derived_regions_carry_write_authority` (alp-sdk `29df99f9`), tightened
from "the key is present" to the exact value each derivation branch writes:

* SoC-level `memory_regions` (RZ/V2N) -- all RAM, so `customer_runtime`;
* the Alif variant derivation -- `mram_main`, the whole-device alias spanning
  rows of differing authority, is `composite`; every SRAM/TCM bank is
  `customer_runtime`.

Before alp-sdk#2024 a derived row carried no authority key at all (ADR-0034
clause 4: absent meant unresolved). v2 makes the loader state it, so a
consumer never has to default one.

Real-SDK-gated: reads the real E1M-AEN301 / E1M-V2N101 presets and their SoC
JSON from a bound alp-sdk checkout.
"""

from __future__ import annotations

import pytest
import yaml

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these tests derive memory maps from the real "
           "E1M-AEN301 / E1M-V2N101 presets and their SoC JSON.",
)


def _derived(preset_name: str) -> list[dict]:
    from tan.planner import som_metadata

    assert SDK is not None
    metadata = SDK / "metadata"
    doc = yaml.safe_load(
        (metadata / "e1m_modules" / f"{preset_name}.yaml").read_text(encoding="utf-8"))
    doc.pop("memory_map", None)  # force the derived path
    return som_metadata.resolve_memory_map(doc, metadata)


@pytest.mark.parametrize("preset_name", ["E1M-AEN301", "E1M-V2N101"])
def test_every_derived_region_carries_write_authority(
        _bound_sdk, preset_name: str) -> None:  # noqa: F811
    regions = _derived(preset_name)

    assert regions, f"{preset_name}: no derived regions -- the test lost its subject"
    missing = [r["name"] for r in regions if "write_authority" not in r]
    assert not missing, f"{preset_name}: derived rows with no write_authority: {missing}"


def test_soc_level_regions_are_customer_runtime(_bound_sdk) -> None:  # noqa: F811
    """RZ/V2N takes the SoC-JSON `memory_regions` branch: every row is RAM."""
    by_name = {r["name"]: r["write_authority"] for r in _derived("E1M-V2N101")}

    assert by_name == {
        "ddr_main": "customer_runtime",
        "ocram_low": "customer_runtime",
        "m33_tcm": "customer_runtime",
    }


def test_variant_derived_mram_is_composite_and_sram_is_runtime(
        _bound_sdk) -> None:  # noqa: F811
    by_name = {r["name"]: r["write_authority"] for r in _derived("E1M-AEN301")}

    assert by_name.pop("mram_main") == "composite"
    assert by_name, "E1M-AEN301 derived no SRAM banks -- the test lost its subject"
    assert set(by_name.values()) == {"customer_runtime"}, by_name

