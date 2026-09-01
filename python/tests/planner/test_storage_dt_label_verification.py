# SPDX-License-Identifier: Apache-2.0
"""A `flash_device:` must be a real device carrying a real Devicetree label
(alp-sdk#1484 + alp-sdk#1556, ported into `tan/planner/partition.py` for
tan-cli#868).

Two upstream defects, one root cause -- the resolver trusted a name it had
invented:

  * a `memory_map:` region marked `carveout: false` is a partition INSIDE a
    flash-class node (on AEN: `mcuboot` / `he_slot0` / `hp_slot0` /
    `reserved` / `storage` / `atoc` inside `mram_storage`), not a flash device
    of its own. It has no Devicetree label, so a `partitions { }` child
    decorating it targets a node the generated board tree never defines. It
    was both advertised by `_known_flash_devices()` and accepted by
    `_resolve_flash_device()` (alp-sdk#1484).
  * `_resolve_flash_device()`'s `dt_label` DEFAULTS to the device name when
    the SoM preset declares no explicit `dt_label:` override, and that default
    was never checked against the generated tree. `flash_device: mram_main`
    reached `status: ok` with a fabricated `dt_label` of `mram_main` even
    though the tree only ever defines `mram_storage` (alp-sdk#1556).

Both were invisible to tan until this port: the freshness gate
(`tests/gates/test_planner_relocation_freshness.py`) hashes the UPSTREAM
files, so it says a delta EXISTS but never that tan's own emit is wrong, and
the byte-parity suite (`tests/parity/test_planner_emit_parity.py`) only fails
while the bound alp-sdk checkout is NEWER than the pin -- which is exactly the
window tan-cli#868 was filed in, and exactly the window that closes the
moment the pin moves. These assertions do not close: they pin the BEHAVIOUR
in tan, against tan's own resolver, so re-pinning cannot quietly re-freeze the
old shape.

Hermetic: every fixture below is a synthetic `BoardProject` whose SoM preset
declares `memory_map:` EXPLICITLY, which `som_metadata.resolve_memory_map()`
returns verbatim without reading any metadata tree. The bound-root skip is an
import requirement only (`tan/planner_root.py` raises when `tan.planner` is
imported with no root bound), not a content dependency -- same shape as
`tests/planner/test_secure_single_slot_swap_default.py`.
"""
from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401 -- `_bound_sdk` imported for its side effect (fixture registration)

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.partition requires SOME bound "
           "root (tan/planner_root.py), even though every assertion below "
           "reads only its own synthetic preset, never the bound checkout's "
           "content. A SKIP about the missing root, not a pass.",
)

# The SoM under test. `mram_main` is the whole-window alias with NO
# `dt_label:` override -- the alp-sdk#1556 case. `mcuboot` is the
# `carveout: false` sub-region tiling its first 128 KiB -- the alp-sdk#1484
# case, and also the obstacle the ordering test below needs. `ext_flash` is
# the one device that carries an EXPLICIT `dt_label:`, i.e. the only kind
# this resolver may now recommend or resolve to `status: ok`. `ospi0` is the
# `on_module.ospi_memories:` device, deliberately exempt from the label gate
# (see `_has_real_dt_label()`'s docstring: that key's own verification gap is
# tracked separately, and gating it here would block every AEN storage build).
_SOM_PRESET = {
    "sku": "E1M-TEST01",
    "memory_map": [
        {"name": "mram_main", "base": 0x8000_0000, "size_mib": 4},
        {"name": "mcuboot", "base": 0x8000_0000, "size_kib": 128,
         "carveout": False},
        {"name": "ext_flash", "base": 0x9000_0000, "size_mib": 8,
         "dt_label": "ospi_flash0"},
    ],
    "on_module": {"ospi_memories": {"ospi0": {"capacity_mbit": 512}}},
}


def _project(*entries):
    from tan.planner.models import BoardProject

    return BoardProject(
        sku="E1M-TEST01",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={},
        ipc=[],
        soc_spec={},
        som_preset=_SOM_PRESET,
        board_preset=None,
        storage=list(entries),
    )


def _entry(**kw):
    from tan.planner.models import StorageEntry

    base = {"name": "data", "size_kib": 64, "fs": "littlefs"}
    base.update(kw)
    return StorageEntry(**base)


def _resolved(*entries):
    from tan.planner.partition import resolve_storage_partitions

    return resolve_storage_partitions(_project(*entries))


def test_a_carveout_false_region_is_not_an_advertised_flash_device():
    """`_known_flash_devices()` is what the loader's "did you mean..." list is
    built from, so a name in it is a name tan RECOMMENDS. A `carveout: false`
    sub-region must not appear there (alp-sdk#1484)."""
    from tan.planner.partition import _known_flash_devices

    known = set(_known_flash_devices(_SOM_PRESET, SDK / "metadata"))
    assert "mcuboot" not in known
    assert {"mram_main", "ext_flash", "ospi0"} <= known


def test_naming_a_carveout_false_region_directly_is_refused():
    """Defense in depth for a hand-built project that never went through the
    loader's check: the resolver refuses rather than fabricating a `dt_label`
    from the region name."""
    from tan.planner.partition import _resolve_flash_device

    descriptor, reason = _resolve_flash_device(
        "mcuboot", _SOM_PRESET, SDK / "metadata")
    assert descriptor is None
    assert "is a partition inside a flash-class region" in reason
    assert "cannot take a `partitions { }` child" in reason


def test_only_an_explicit_dt_label_override_counts_as_verified():
    """`_has_real_dt_label()` trusts an EXPLICIT `dt_label:` and nothing else
    -- not a name that happens to look like a node label."""
    from tan.planner.partition import _has_real_dt_label

    root = SDK / "metadata"
    assert _has_real_dt_label("ext_flash", _SOM_PRESET, root) is True
    assert _has_real_dt_label("mram_main", _SOM_PRESET, root) is False
    # An `ospi_memories:` key is not a `memory_map:` region at all, so the
    # predicate itself answers False; the resolver exempts it separately.
    assert _has_real_dt_label("ospi0", _SOM_PRESET, root) is False


def test_an_unverified_default_dt_label_blocks_the_entry():
    """alp-sdk#1556: the entry clears page alignment, capacity and every
    overlap check, and still must not reach `status: ok` -- its `dt_label`
    was defaulted from the device name and never checked against the tree."""
    [part] = _resolved(_entry(flash_device="mram_main", offset_kib=128))
    assert part.status == "blocked"
    assert "its Devicetree label defaults to 'mram_main'" in part.reason
    assert "may decorate a `partitions { }` child under a node the board " \
           "`.dts` never defines" in part.reason
    # The remedy names the one device that both resolves and is verified --
    # never `mram_main` itself, and never the unverified `ospi0`.
    assert "use a different flash_device: (ext_flash)" in part.reason


def test_an_explicit_dt_label_resolves_to_ok():
    """The verified device is the one shape that still reaches `status: ok`,
    and it emits the OVERRIDE, not the device name."""
    [part] = _resolved(_entry(flash_device="ext_flash"))
    assert part.status == "ok", part.reason
    assert part.dt_label == "ospi_flash0"


def test_an_ospi_memories_device_is_exempt_from_the_label_gate():
    """`on_module.ospi_memories:` keys are deliberately NOT gated -- gating
    them would block every AEN storage build on `ospi0` and the storage
    suite's own device-independent allocator fixture. Their own verification
    gap is tracked upstream, not silently widened here."""
    [part] = _resolved(_entry(flash_device="ospi0"))
    assert part.status == "ok", part.reason


def test_an_overlap_keeps_the_overlap_reason_not_the_label_reason():
    """ORDER IS BEHAVIOUR: the #1556 check sits AFTER the overlap/overflow
    checks, so an entry that would ALSO collide with one of the SoM's own
    regions reports that collision -- the more actionable fact -- instead of
    the label complaint."""
    [part] = _resolved(_entry(flash_device="mram_main", offset_kib=0))
    assert part.status == "blocked"
    assert "overlaps SoM region 'mcuboot'" in part.reason
    assert "Devicetree label defaults to" not in part.reason
