# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `tan/planner/aperture.py` -- SoC on-die flash aperture
resolution + region classification (alp-sdk#1365 split B, hand-ported for
tan-cli).

`aperture.py`'s own docstring states the governing doctrine an alp-sdk#1365
split B review found two verdict-returning functions violating: containment
is ONE-SIDED -- inside the aperture proves flash, outside proves NOTHING.

Covers:
  - `resolve_aperture()`'s success case and every None case (no `silicon:`,
    a malformed `silicon:`, a SoC JSON missing `soc_flash_base`, a variant
    with no usable `mram_mb`).
  - `region_extent()`, including the TBD-masking case (`{size_mib: "TBD",
    size_kib: 64}` must resolve via `size_kib`).
  - `classify_region()`'s four verdicts and every boundary: extent exactly
    equal to the aperture, straddling the low edge, straddling the high
    edge, fully outside, unresolved base, unresolved size -- and the case a
    split B review found: a DERIVED (non-preset-authored) row whose extent
    is contained in the aperture must classify `"flash"`, not `"ram"`.
  - `is_partition_inside_aperture()` returning `None` (not `False`) for a
    region outside the aperture -- the fact `partition.py`'s
    `_is_flash_sub_partition()` depends on to fall back to the legacy
    `carveout:` flag instead of reading "not proven inside" as "is a flash
    device".

Hermetic except for the import requirement: `tan.planner.aperture` lives
inside the `tan.planner` package, so merely importing it needs SOME bound
alp-sdk root (`tan/planner_root.py` raises otherwise) -- same shape as
`tests/planner/test_storage_dt_label_verification.py`. Every
`resolve_aperture()` fixture below is a synthetic on-disk SoC-JSON tree
built fresh under `tmp_path`, never the bound checkout's own metadata, so
re-pinning `ALP_SDK_ROOT` cannot change what these assertions measure.

Run locally:

    python -m pytest python/tests/planner/test_aperture.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.aperture requires SOME bound "
           "root (tan/planner_root.py), even though every assertion below "
           "reads only a synthetic on-disk fixture, never the bound "
           "checkout's content. A SKIP about the missing root, not a pass.",
)

# A synthetic aperture: base 0x80000000, 5.5 MiB -> top 0x80580000. Mirrors
# the real E8 SoC spec's shape (metadata/socs/alif/ensemble/e8.json:
# soc_flash_base 0x80000000, variant AE822FA0E5597LS0 mram_mb 5.5) without
# depending on the bound checkout actually declaring it.
APERTURE: tuple[int, int] = (0x80000000, 0x80580000)

_SILICON = "test:soc:x1"
_ORDER_CODE = "TESTVAR1"
_PRESET: dict[str, Any] = {
    "sku": "E1M-TESTXX",
    "silicon": _SILICON,
    "silicon_variant": _ORDER_CODE,
}


def _write_soc_json(
    root: Path,
    *,
    soc_flash_base: Any = None,
    variant: Optional[dict[str, Any]] = None,
) -> None:
    """Write a minimal `metadata/socs/test/soc/x1.json` under `root`,
    matching `resolve_soc_path`'s `<vendor>/<family>/<part>.json` layout for
    `_SILICON` ("test:soc:x1")."""
    doc: dict[str, Any] = {"variants": [variant] if variant else []}
    if soc_flash_base is not None:
        doc["soc_flash_base"] = soc_flash_base
    path = root / "socs" / "test" / "soc" / "x1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _region(base: Any, size_kib: Optional[int] = None,
            size_mib: Any = None, name: str = "r",
            **extra: Any) -> dict[str, Any]:
    region: dict[str, Any] = {"name": name, "base": base}
    if size_kib is not None:
        region["size_kib"] = size_kib
    if size_mib is not None:
        region["size_mib"] = size_mib
    region.update(extra)
    return region


class TestResolveAperture:
    def test_resolves_a_declared_aperture(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        _write_soc_json(
            tmp_path, soc_flash_base=APERTURE[0],
            variant={"order_code": _ORDER_CODE, "mram_mb": 5.5})
        assert resolve_aperture(_PRESET, tmp_path) == APERTURE

    def test_none_when_preset_names_no_silicon(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        assert resolve_aperture({"sku": "no-silicon"}, tmp_path) is None

    def test_none_when_silicon_key_is_malformed(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        preset = {"silicon": "not-a-vendor-family-part"}
        assert resolve_aperture(preset, tmp_path) is None

    def test_none_when_soc_json_omits_soc_flash_base(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        _write_soc_json(
            tmp_path,
            variant={"order_code": _ORDER_CODE, "mram_mb": 5.5})
        assert resolve_aperture(_PRESET, tmp_path) is None

    def test_none_when_variant_has_no_usable_mram_mb(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        _write_soc_json(
            tmp_path, soc_flash_base=APERTURE[0],
            variant={"order_code": _ORDER_CODE})
        assert resolve_aperture(_PRESET, tmp_path) is None

    def test_none_when_soc_json_is_missing(self, tmp_path):
        from tan.planner.aperture import resolve_aperture

        assert resolve_aperture(_PRESET, tmp_path) is None


class TestRegionExtent:
    def test_size_kib_field(self):
        from tan.planner.aperture import region_extent

        r = _region(0x1000, size_kib=4)
        assert region_extent(r) == (0x1000, 0x1000 + 4 * 1024)

    def test_size_mib_field(self):
        from tan.planner.aperture import region_extent

        r = _region(0x1000, size_mib=1)
        assert region_extent(r) == (0x1000, 0x1000 + 1024 * 1024)

    def test_unresolved_when_base_is_the_tbd_string(self):
        from tan.planner.aperture import region_extent

        r = _region("TBD", size_kib=4)
        assert region_extent(r) is None

    def test_unresolved_when_base_is_absent(self):
        from tan.planner.aperture import region_extent

        assert region_extent({"name": "r", "size_kib": 4}) is None

    def test_unresolved_when_neither_size_field_is_set(self):
        from tan.planner.aperture import region_extent

        assert region_extent({"name": "r", "base": 0x1000}) is None

    def test_tbd_size_mib_does_not_mask_a_usable_size_kib(self):
        """alp-sdk#1365 split B fix: the `memory_region` schema lets
        `size_kib` and `size_mib` each independently be `"TBD"`; a `"TBD"`
        in one field must never mask a usable integer sitting in the
        other."""
        from tan.planner.aperture import region_extent

        r = _region(0x1000, size_kib=64, size_mib="TBD")
        assert region_extent(r) == (0x1000, 0x1000 + 64 * 1024)

    def test_tbd_size_kib_does_not_mask_a_usable_size_mib(self):
        from tan.planner.aperture import region_extent

        r = _region(0x1000, size_mib=1, size_kib="TBD")
        assert region_extent(r) == (0x1000, 0x1000 + 1024 * 1024)


class TestClassifyRegion:
    def test_unresolved_when_region_base_is_unresolved(self):
        from tan.planner.aperture import classify_region

        r = _region("TBD", size_kib=64)
        assert classify_region(r, APERTURE, True) == "unresolved"
        assert classify_region(r, APERTURE, False) == "unresolved"

    def test_unresolved_when_region_size_is_unresolved(self):
        from tan.planner.aperture import classify_region

        r = {"name": "r", "base": APERTURE[0]}
        assert classify_region(r, APERTURE, True) == "unresolved"

    def test_unresolved_when_aperture_is_none(self):
        from tan.planner.aperture import classify_region

        r = _region(APERTURE[0], size_kib=64)
        assert classify_region(r, None, True) == "unresolved"
        assert classify_region(r, None, False) == "unresolved"

    def test_flash_whole_device_alias_regardless_of_authorship(self):
        """Extent exactly equal to the aperture -- flash no matter who
        (nominally) authored the row."""
        from tan.planner.aperture import classify_region, region_extent

        r = _region(APERTURE[0], size_kib=5632)  # == [0x80000000, 0x80580000)
        assert region_extent(r) == APERTURE
        assert classify_region(r, APERTURE, True) == "flash"
        assert classify_region(r, APERTURE, False) == "flash"

    def test_flash_strictly_contained_even_when_not_preset_authored(self):
        """Containment must be tested BEFORE `is_preset_authored` is
        consulted. A DERIVED row (is_preset_authored=False) whose extent
        sits inside the aperture is flash -- discarding that proof in
        favour of a blanket `"ram"` default is the exact inverse of the
        one-sidedness this module defends."""
        from tan.planner.aperture import classify_region

        r = _region(APERTURE[0] + 0x10000, size_kib=64)  # strictly inside
        assert classify_region(r, APERTURE, False) == "flash"
        assert classify_region(r, APERTURE, True) == "flash"

    def test_ram_outside_the_aperture_and_not_preset_authored(self):
        from tan.planner.aperture import classify_region

        r = _region(0xA0000000, size_kib=64)  # fully outside, e.g. OSPI XIP
        assert classify_region(r, APERTURE, False) == "ram"

    def test_unclassified_outside_the_aperture_and_preset_authored(self):
        from tan.planner.aperture import classify_region

        r = _region(0xA0000000, size_kib=64)  # fully outside, e.g. OSPI XIP
        assert classify_region(r, APERTURE, True) == "unclassified"

    def test_straddling_the_low_edge_is_not_contained(self):
        # lo just below the floor, hi just above it: crosses the boundary,
        # neither fully inside nor fully outside.
        from tan.planner.aperture import classify_region

        r = _region(APERTURE[0] - 0x1000, size_kib=8)
        assert classify_region(r, APERTURE, True) == "unclassified"
        assert classify_region(r, APERTURE, False) == "ram"

    def test_straddling_the_high_edge_is_not_contained(self):
        # lo just below the ceiling, hi just above it: crosses the boundary.
        from tan.planner.aperture import classify_region

        r = _region(APERTURE[1] - 0x1000, size_kib=8)
        assert classify_region(r, APERTURE, True) == "unclassified"
        assert classify_region(r, APERTURE, False) == "ram"

    def test_tbd_size_mib_does_not_mask_size_kib_in_classification(self):
        """The `region_extent()` TBD-masking fix, threaded through
        `classify_region`."""
        from tan.planner.aperture import classify_region

        r = _region(APERTURE[0] + 0x10000, size_kib=64, size_mib="TBD")
        assert classify_region(r, APERTURE, True) == "flash"


class TestIsPartitionInsideAperture:
    def test_true_for_a_proper_subset(self):
        from tan.planner.aperture import is_partition_inside_aperture

        r = _region(APERTURE[0] + 0x10000, size_kib=64)
        assert is_partition_inside_aperture(r, APERTURE) is True

    def test_false_only_when_extent_equals_the_aperture_exactly(self):
        from tan.planner.aperture import (
            is_partition_inside_aperture, region_extent)

        r = _region(APERTURE[0], size_kib=5632)
        assert region_extent(r) == APERTURE
        assert is_partition_inside_aperture(r, APERTURE) is False

    def test_none_outside_the_aperture(self):
        """Outside the aperture proves NOTHING, so the verdict is `None`,
        not `False`. A definite `False` here would let `partition.py`'s
        `_is_flash_sub_partition()` read it as "not a sub-partition", i.e.
        "this is a flash DEVICE" -- exactly backwards for a row (e.g. an
        OSPI XIP window) that merely resolves outside the SoC's declared
        on-die MRAM aperture."""
        from tan.planner.aperture import is_partition_inside_aperture

        r = _region(0xA0000000, size_kib=64)
        assert is_partition_inside_aperture(r, APERTURE) is None

    def test_none_when_aperture_is_none(self):
        from tan.planner.aperture import is_partition_inside_aperture

        r = _region(APERTURE[0] + 0x10000, size_kib=64)
        assert is_partition_inside_aperture(r, None) is None

    def test_none_when_region_extent_is_unresolved(self):
        from tan.planner.aperture import is_partition_inside_aperture

        r = _region("TBD", size_kib=64)
        assert is_partition_inside_aperture(r, APERTURE) is None
