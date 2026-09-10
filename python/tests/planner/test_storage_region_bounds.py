# SPDX-License-Identifier: Apache-2.0
"""Two alp-sdk#2010 gaps in `tan/planner/partition.py`'s alp-sdk#1365
split B logic, hand-ported from alp-sdk's
`tests/scripts/test_orchestrate_storage_region_bounds.py`:

  - `TestLegacyCarveoutFallback` (P1) -- `_is_flash_sub_partition()`'s
    legacy `carveout: false` fallback (`if inside is None: return
    region.get("carveout") is False`) had no test at all; it is what the
    non-Alif no-op argument rests on.
  - `TestDerivedVerdictLoadBearingEndToEnd` (P4) --
    `resolve_storage_partitions()` hoists ONE real aperture per call and
    threads it through every helper it calls; mutating that hoisted
    value to `None` (silently bypassing split B in the actual production
    entry point) survived every gate, because the P2/P3 fixes were only
    proven via a direct `_resolve_flash_device()` /
    `_is_flash_sub_partition()` call, never through
    `resolve_storage_partitions()` itself.

Only these two additions are ported -- the rest of alp-sdk's
`test_orchestrate_storage_region_bounds.py` (auto-allocation / explicit-
offset / reserved-bytes / no-false-positives coverage) predates the
alp-sdk#2010 delta this resync is for and has no tan file to land in yet;
porting it is a separate unit of work.

Real-SDK-gated: `TestDerivedVerdictLoadBearingEndToEnd` needs the real
E1M-AEN801 SoM preset (`metadata/e1m_modules/E1M-AEN801.yaml`) and its
declared aperture (`metadata/socs/alif/ensemble/e8.json`'s
`soc_flash_base`) from a bound alp-sdk checkout -- same requirement as
`test_carveout_aperture_ordering.py`. `TestLegacyCarveoutFallback` is a
direct call needing only SOME bound root to import `tan.planner.partition`
at all (`tan/planner_root.py` raises otherwise).

Run locally:

    python -m pytest python/tests/planner/test_storage_region_bounds.py -v
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`
# (tan-cli#1081: every real-SDK-gated module reuses this one definition
# rather than redefining it locally).
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.partition requires SOME "
           "bound root (tan/planner_root.py), even though "
           "TestLegacyCarveoutFallback reads only its own direct-call "
           "arguments, never the bound checkout's content.",
)


def _write_board(tmp_path: Path, body: str, name: str = "board.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


def _by_name(parts):
    return {p.name: p for p in parts}


class TestLegacyCarveoutFallback:
    """P1 (alp-sdk#2010): `_is_flash_sub_partition()`'s legacy `carveout:
    false` fallback (`if inside is None: return region.get("carveout")
    is False`) has no test at all -- it is what the docstring's "MAJOR 3"
    and the non-Alif no-op argument both rest on. No non-Alif preset in
    `metadata/e1m_modules/*.yaml` authors `carveout: false` today (`git
    grep -rn "carveout:" metadata/e1m_modules/` is AEN-only), so this is
    a direct-call pin with an explicit `aperture=None` -- the exact value
    `resolve_aperture()` returns for every non-Alif SoM, bypassing the
    `_APERTURE_UNSET` sentinel's own re-resolution."""

    def test_carveout_false_still_excludes_when_aperture_is_none(self):
        from tan.planner.partition import _is_flash_sub_partition

        region = {"name": "legacy_subpart", "base": 0x1000, "size_kib": 64,
                   "carveout": False}
        assert _is_flash_sub_partition(
            region, {}, SDK / "metadata", aperture=None) is True, (
            "a carveout: false region was NOT treated as a flash "
            "sub-partition when no aperture is declared -- the legacy "
            "fallback every non-Alif SoM depends on did not fire")


class TestDerivedVerdictLoadBearingEndToEnd:
    """P4 (alp-sdk#2010): `resolve_storage_partitions()` hoists ONE real
    aperture per call and threads it through every helper it calls --
    mutating that hoisted value to `None` (silently bypassing split B in
    the actual production entry point) survived every gate, because the
    P2/P3 fixes were proven only via a direct `_resolve_flash_device()` /
    `_is_flash_sub_partition()` call, never through
    `resolve_storage_partitions()` itself. No SHIPPED preset makes the
    derived and legacy (`carveout:`-only) verdicts disagree -- every AEN
    flash-class sub-region already authors an explicit, agreeing
    `carveout: false` -- so this fixture adds ONE synthetic `memory_map:`
    row, in-memory only (never touching tracked YAML), that the two
    verdicts read differently: strictly INSIDE the declared aperture
    (derived: flash-class, by containment) but carrying NO `carveout:`
    key at all (legacy-only fallback: not `False`, so "not a
    sub-partition" -- treated as a real device). This is exactly the
    alp-sdk#1365 hazard shape (an unflagged row silently becoming a
    customer-writable target) with the roles of `mram_main` played by a
    plain synthetic row instead."""

    def test_undeclared_row_inside_the_aperture_is_still_refused(
            self, tmp_path):
        from tan.planner import load_board_yaml
        from tan.planner.models import StorageEntry
        from tan.planner.partition import resolve_storage_partitions

        path = _write_board(tmp_path, """
        name: test-aen801-p4-fixture
        som:
          sku: E1M-AEN801
          hw_rev: r2

        cores:
          m55_hp:
            os: zephyr
            app: ./m55_hp
        """)
        project = load_board_yaml(path)
        project.som_preset = copy.deepcopy(project.som_preset)
        project.som_preset["memory_map"].append({
            "name": "undeclared_sub_region",
            "base": 0x80000000 + 0x100000,   # strictly inside the aperture
            "size_kib": 64,
            "accessible_from": ["m55_hp", "m55_he"],
        })
        project.storage = [StorageEntry(
            name="leak_test", size_kib=32, fs="littlefs",
            flash_device="undeclared_sub_region")]

        parts = resolve_storage_partitions(project)
        entry = _by_name(parts)["leak_test"]

        assert entry.status == "blocked", (
            f"a memory_map row strictly inside the declared aperture, "
            f"with no carveout: key authored, resolved status "
            f"{entry.status!r} -- the legacy-only fallback silently "
            f"treated it as a real, customer-writable device")
        assert "partition inside a flash-class region" in (entry.reason or ""), (
            f"blocked for the wrong reason -- the DERIVED (aperture-"
            f"containment) verdict must be the one that decided this, "
            f"not some other check: {entry.reason!r}")
