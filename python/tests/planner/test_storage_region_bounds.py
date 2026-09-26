# SPDX-License-Identifier: Apache-2.0
"""`tan/planner/partition.py`'s storage-offset bounds checking, hand-ported
from alp-sdk's `tests/scripts/test_orchestrate_storage_region_bounds.py`
(14 tests at alp-sdk c81cb5db) -- tan-cli#1255.

PR #1251 landed only the two alp-sdk#2010 classes below
(`TestLegacyCarveoutFallback`, `TestDerivedVerdictLoadBearingEndToEnd`),
deferring the other 12 -- predating #2010, with no tan-side counterpart
anywhere -- as "a separate unit of work" (this port):

  - `TestAutoAllocation` (2) -- the DEFAULT auto-allocation path must never
    land a mount on MCUboot, and a fully-tiled device must block with an
    actionable reason.
  - `TestExplicitOffset` (3) -- an explicit `offset_kib: 0` on a
    whole-window device is MCUboot and must be refused; the refusal must
    name a remedy that itself round-trips; an offset inside the #1289 ATOC
    band must be refused by name.
  - `TestReservedBytesLessThanCapacity` (4) -- the `reserved_bytes <
    capacity_bytes` branch (E1M-V2N101's `ddr_main`, a single reserved
    span with room free elsewhere) -- distinct from AEN's fully-tiled
    `mram_main`; the remedy must never re-offer a refused region or an
    unverified Devicetree label as an alternative.
  - `TestTargetingARegionDirectly` (2) -- naming a `carveout: false`
    sub-region (e.g. `storage`) as a `flash_device:` directly must be
    refused, both at the loader boundary and in `_resolve_flash_device()`
    itself (defense in depth).
  - `TestNoFalsePositives` (1) -- a genuinely free flash device must still
    allocate; the bounds check must not false-positive on it.

  - `TestLegacyCarveoutFallback` (P1, alp-sdk#2010) --
    `_is_flash_sub_partition()`'s legacy `carveout: false` fallback
    (`if inside is None: return region.get("carveout") is False`) had no
    test at all; it is what the non-Alif no-op argument rests on.
  - `TestDerivedVerdictLoadBearingEndToEnd` (P4, alp-sdk#2010) --
    `resolve_storage_partitions()` hoists ONE real aperture per call and
    threads it through every helper it calls; mutating that hoisted
    value to `None` (silently bypassing split B in the actual production
    entry point) survived every gate, because the P2/P3 fixes were only
    proven via a direct `_resolve_flash_device()` /
    `_is_flash_sub_partition()` call, never through
    `resolve_storage_partitions()` itself.

Real-SDK-gated: every class below reads real SoM presets
(E1M-AEN801/AEN301/AEN401/V2N101) from a bound alp-sdk checkout --
`metadata/e1m_modules/*.yaml` and, for the aperture-derived classes, the
SoC's `soc_flash_base` (`metadata/socs/alif/ensemble/e8.json`, alp-sdk#1365
split A, merged upstream as of the pin above) -- same requirement as
`test_carveout_aperture_ordering.py`. `TestLegacyCarveoutFallback` is the
one exception: a direct call needing only SOME bound root to import
`tan.planner.partition` at all (`tan/planner_root.py` raises otherwise).

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


def _aen801(storage_body: str) -> str:
    return f"""
    name: test-aen801-bounds
    som:
      sku: E1M-AEN801
      hw_rev: r2

    cores:
      m55_hp:
        os: zephyr
        app: ./m55_hp

    storage:
    {storage_body}
    """


def _resolve(tmp_path, storage_body):
    from tan.planner import load_board_yaml, resolve_storage_partitions

    path = _write_board(tmp_path, _aen801(storage_body))
    return resolve_storage_partitions(load_board_yaml(path))


def _by_name(parts):
    return {p.name: p for p in parts}


class TestAutoAllocation:
    def test_auto_allocation_does_not_land_on_mcuboot(self, tmp_path):
        """The DEFAULT path -- no offsets declared anywhere.

        Pre-fix this resolved to `offset_kib: 0` of `mram_main`, i.e.
        absolute 0x80000000, which is the MCUboot partition: a littlefs
        mount on the bootloader, reported as success. It must never
        resolve to offset 0 of a whole-window device whose low bytes are
        MCUboot.
        """
        parts = _resolve(tmp_path, """
      - { name: app_data, size_kib: 64, fs: littlefs, flash_device: mram_main, mount: /lfs/app }
    """)
        app = _by_name(parts)["app_data"]
        if getattr(app, "status", None) == "blocked":
            # AEN801 is fully tiled, so "no room" is the correct answer.
            assert "mram_main" in (app.reason or "")
            return
        assert app.base_kib != 0, (
            "app_data resolved to offset 0 of mram_main -- that is MCUboot")

    def test_fully_tiled_device_blocks_with_an_actionable_reason(self, tmp_path):
        """AEN801's memory_map tiles all 5632 KiB, so mram_main has no room.

        Blocking is the truthful outcome; the reason must name the
        occupying regions rather than just saying the device is full.
        """
        parts = _resolve(tmp_path, """
      - { name: app_data, size_kib: 256, fs: littlefs, flash_device: mram_main, mount: /lfs/app }
    """)
        app = _by_name(parts)["app_data"]
        assert getattr(app, "status", None) == "blocked", app
        reason = app.reason or ""
        assert "mcuboot" in reason and "atoc" in reason, reason


class TestExplicitOffset:
    def test_explicit_offset_zero_is_refused(self, tmp_path):
        """`offset_kib: 0` on a whole-window device is MCUboot.

        Pre-fix: page-aligned, inside capacity, no siblings -> accepted.
        """
        parts = _resolve(tmp_path, """
      - { name: logs, size_kib: 64, fs: littlefs, flash_device: mram_main, offset_kib: 0, mount: /lfs/logs }
    """)
        logs = _by_name(parts)["logs"]
        assert getattr(logs, "status", None) == "blocked", logs
        assert "mcuboot" in (logs.reason or ""), logs.reason

    def test_the_refusal_names_the_remedy(self, tmp_path):
        """A block that doesn't say what to do instead is a dead end.

        The remedy must name a flash_device: that actually resolves
        (alp-sdk#1484) -- naming the reserved region itself back at the
        caller is a dead end, since that region is refused by
        `_resolve_flash_device()`. Prove it round-trips rather than just
        asserting the literal "flash_device:" substring, which the
        dead-end text also contained.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions
        from tan.planner.partition import _known_flash_devices

        board_path = _write_board(tmp_path, _aen801("""
      - { name: logs, size_kib: 32, fs: littlefs, flash_device: mram_main, offset_kib: 0, mount: /lfs/logs }
    """))
        project = load_board_yaml(board_path)
        parts = resolve_storage_partitions(project)
        reason = _by_name(parts)["logs"].reason or ""
        assert "not customer-writable" in reason, reason

        # `mram_main` is fully tiled by its own carveout:false sub-regions
        # on every AEN preset (0 KiB free) -- the remedy must NOT lead
        # with "pick an offset on 'mram_main' outside the SoM's declared
        # regions", since that advice is unfollowable on the fully-tiled
        # case this test pins (AEN801 mram_main, 0 KiB free).
        assert "pick an offset on 'mram_main'" not in reason, reason
        assert "fully tiled" in reason, reason

        # #1484 re-review: AEN801's `ospi0` is a KNOWN, RESOLVING device
        # (`_known_flash_devices()` / `_resolve_flash_device()`) but it
        # has no verified Devicetree label. Recommending it back to the
        # customer would decorate a node that isn't a working flash area
        # -- the exact defect #1484 is titled after -- so the remedy must
        # name NO alternative device at all until a real per-instance DT
        # label is verified.
        known = _known_flash_devices(project.som_preset, project.effective_metadata_root())
        alt = [d for d in known if d != "mram_main" and d in reason]
        assert not alt, (
            f"remedy names unverified alternative device(s) {alt}: {reason}")
        assert "use a different flash_device:" not in reason, reason
        assert "ospi0" not in reason, reason

        # A dead end must at least name where to follow up (#1556 re-review).
        assert "alp-sdk#1556" in reason, reason

    def test_offset_inside_the_atoc_band_is_refused(self, tmp_path):
        """The #1289 band, reached the customer-facing way.

        `storage` ends at 0x578000 = 5600 KiB; the atoc band is the 32 KiB
        above it. An explicit offset there must be refused by name.
        """
        parts = _resolve(tmp_path, """
      - { name: logs, size_kib: 16, fs: raw, flash_device: mram_main, offset_kib: 5600 }
    """)
        logs = _by_name(parts)["logs"]
        assert getattr(logs, "status", None) == "blocked", logs
        assert "atoc" in (logs.reason or ""), logs.reason


class TestReservedBytesLessThanCapacity:
    """The `reserved_bytes < capacity_bytes` branch (partition.py) -- the
    OTHER shape a reserved-region overlap can take, distinct from AEN's
    fully-tiled `mram_main`. E1M-V2N101's `ddr_main` has a single reserved
    span (`m33_tcm`) and 4194176 KiB of free room, so this is the
    `pick an offset on '<device>' outside the SoM's declared regions`
    remedy, not the `fully tiled` one -- and per #1484 review it has NO
    prior coverage in this file (both TestExplicitOffset cases pin
    AEN801, which is fully tiled and never reaches this branch).
    """

    def test_v2n101_ddr_main_overlap_names_no_undefined_alternative(
            self, tmp_path):
        """Regression for the #1484 review major finding: naming
        `m33_tcm` (the very region just refused) or `ocram_low` as a
        "different flash_device:" alternative -- neither has a
        Devicetree label -- so following that remedy would decorate an
        undefined node. E1M-V2N101 has no `on_module.ospi_memories:` and
        no `memory_map:` region carries an explicit `dt_label:`
        override, so the correct remedy names NO alternative device at
        all.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions

        path = _write_board(tmp_path, """
        name: test-v2n101-ddr-main-overlap
        som:
          sku: E1M-V2N101
          hw_rev: r1

        cores:
          m33_sm:
            app: ./m33_sm

        storage:
          - { name: blob, size_kib: 4096, fs: raw, flash_device: ddr_main, offset_kib: 917504 }
        """)
        project = load_board_yaml(path)
        parts = resolve_storage_partitions(project)
        blob = _by_name(parts)["blob"]
        assert getattr(blob, "status", None) == "blocked", blob
        reason = blob.reason or ""
        assert "m33_tcm" in reason, reason
        assert "not customer-writable" in reason, reason

        # The branch under test: room is free outside the reserved span,
        # so the remedy must lead with "pick an offset", not "fully tiled".
        assert "pick an offset on 'ddr_main'" in reason, reason
        assert "fully tiled" not in reason, reason

        # The defect: neither reserved region may be offered back as a
        # "different flash_device:" -- both are refused sub-regions with
        # no verified Devicetree label.
        assert "m33_tcm" not in reason.split(
            "not customer-writable")[1], (
            f"remedy re-offers the refused region: {reason}")
        assert "ocram_low" not in reason, reason
        assert "use a different flash_device:" not in reason, reason

    def test_a_named_alternative_always_round_trips(self, tmp_path):
        """Whenever the remedy DOES name an alternative (any SoM, any
        overlap), every name in the parenthesised list must itself
        resolve AND carry a verified Devicetree label -- guards against a
        future SoM/region reintroducing the #1484 defect shape.

        Kept general (not hardcoded to "no alternative is ever named")
        because `_has_real_dt_label()` legitimately starts returning
        `True` again once a `memory_map:` region grows an explicit
        `dt_label:`; this is deliberately NOT a tautology against that
        future -- it asserts the referent (`_has_real_dt_label()` +
        `_resolve_flash_device()` are exactly the two predicates
        `alt_devices` is filtered on, see partition.py), which is exactly
        what would need to move in lockstep if either predicate loosened
        incorrectly.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions
        from tan.planner.partition import _has_real_dt_label, _resolve_flash_device

        path = _write_board(tmp_path, _aen801("""
      - { name: logs, size_kib: 32, fs: littlefs, flash_device: mram_main, offset_kib: 0, mount: /lfs/logs }
    """))
        project = load_board_yaml(path)
        parts = resolve_storage_partitions(project)
        reason = _by_name(parts)["logs"].reason or ""
        if "use a different flash_device:" not in reason:
            return
        listed = reason.split(
            "use a different flash_device: (")[1].split(")")[0]
        for device in (d.strip() for d in listed.split(",")):
            assert _has_real_dt_label(
                device, project.som_preset, project.effective_metadata_root()), (
                f"remedy named '{device}' with no verified DT label")
            descriptor, err = _resolve_flash_device(
                device, project.som_preset, project.effective_metadata_root())
            assert descriptor is not None, (
                f"remedy named '{device}' but it does not resolve: {err}")

    def test_a_named_alternative_with_a_real_dt_label_round_trips(
            self, tmp_path):
        """Synthetic drive for the round-trip guarantee above.

        `_has_real_dt_label()` returns False for every device on every
        SoM today (alp-sdk#1556), so
        `test_a_named_alternative_always_round_trips` never executes its
        own body -- its `if "use a different flash_device:" not in
        reason: return` guard fires every run. This pins the SAME
        property against a hand-built `som_preset` carrying an explicit
        `dt_label:` override, so the guarantee is actually exercised at
        least once rather than only ever agreeing with itself.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions
        from tan.planner.partition import _reserved_spans

        path = _write_board(tmp_path, _aen801("""
      - { name: logs, size_kib: 32, fs: littlefs, flash_device: mram_main, offset_kib: 0, mount: /lfs/logs }
    """))
        project = load_board_yaml(path)
        # `mram_main` is given its OWN resolved base (0x80000000, same
        # as `mcuboot`'s -- legal: a SoM CAN author one) so
        # `_reserved_spans()` takes the `self_region has a base` branch
        # directly and reserves every sibling with a resolved base by
        # name (6 spans: mcuboot/he_slot0/hp_slot0/reserved/storage/
        # atoc) WITHOUT computing the fragile `origin + capacity ==
        # window_top` identity a synthetic out-of-window sibling would
        # otherwise poison (alp-sdk#2088 review round 2, Major 3).
        # `test_alt_device` keeps its ORIGINAL `base: "TBD"`, which is
        # what keeps it OUT of `_reserved_spans()`'s `sized` list (only
        # integer bases enter that computation) while still resolving
        # via `_resolve_flash_device()` since `size_kib` is an int;
        # `write_authority: customer_runtime` is required for that
        # resolve now that the aperture resolves for this SoM (alp-
        # sdk#2088 round 1).
        project.som_preset["memory_map"] = [
            dict(r, base=0x80000000) if r["name"] == "mram_main" else r
            for r in project.som_preset["memory_map"]] + [{
                "name": "test_alt_device",
                "base": "TBD",
                "size_kib": 64,
                "accessible_from": ["m55_he", "m55_hp"],
                "cacheable": True,
                "write_authority": "customer_runtime",
                "dt_label": "test_alt_device",
            }]
        # Regression pin for review round 2, Major 3: `_reserved_spans()`
        # must actually reserve `mram_main`'s 6 real siblings here, not
        # degrade to `[]` -- the overlap-with-a-verified-alternative
        # branch this test targets has zero coverage if nothing is
        # reserved for `offset_kib: 0` to collide with.
        spans, spans_reason = _reserved_spans(
            "mram_main", 5632 * 1024, project.som_preset,
            project.effective_metadata_root())
        assert spans_reason is None, spans_reason
        assert len(spans) == 6, spans
        parts = resolve_storage_partitions(project)
        reason = _by_name(parts)["logs"].reason or ""
        assert "mcuboot" in reason, reason
        assert "use a different flash_device:" in reason, reason
        assert "test_alt_device" in reason, reason

    def test_aen401_fully_tiled_names_no_undefined_alternative(
            self, tmp_path):
        """Regression for the #1484 re-review major finding: E1M-AEN401's
        `mram_main` is fully tiled (64 + 2688 + 2688 + 64 + 96 + 32 = 5632
        KiB, 0 KiB free), and its `on_module.ospi_memories.ospi0` resolves
        (`_resolve_flash_device()`) but has NO verified Devicetree label:
        E1M-AEN401's board tree never includes the peripherals DTSI that
        defines an `ospi0:` node and never declares any `ospi` node of
        its own. Recommending `ospi0` here -- as the pre-re-review code
        did -- decorated a Devicetree label the board tree never defines:
        the mirror of the E1M-V2N101 case above, on the SKU the original
        finding measured against.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions

        path = _write_board(tmp_path, """
        name: test-aen401-fully-tiled
        som:
          sku: E1M-AEN401
          hw_rev: r1

        cores:
          m55_hp:
            os: zephyr
            app: ./m55_hp

        storage:
          - { name: logs, size_kib: 32, fs: littlefs, flash_device: mram_main, offset_kib: 0, mount: /lfs/logs }
        """)
        project = load_board_yaml(path)
        parts = resolve_storage_partitions(project)
        logs = _by_name(parts)["logs"]
        assert getattr(logs, "status", None) == "blocked", logs
        reason = logs.reason or ""
        assert "not customer-writable" in reason, reason
        assert "fully tiled" in reason, reason
        assert "use a different flash_device:" not in reason, reason
        assert "ospi0" not in reason, reason
        assert "alp-sdk#1556" in reason, reason


class TestTargetingARegionDirectly:
    def test_naming_the_mram_storage_subregion_directly_is_refused(
            self, tmp_path):
        """Naming `storage` directly used to be the documented remedy for
        the block above -- but `storage` is itself a `carveout: false`
        region, a partition label *inside* the `mram_storage` flash node,
        not a Devicetree label of its own (alp-sdk#1484). The loader must
        refuse it at load time with the same "Known devices" message a
        typo gets, not resolve it and decorate a label the board tree
        never defines.
        """
        from tan.planner import OrchestratorError

        with pytest.raises(
                OrchestratorError,
                match="does not resolve to any flash device"):
            _resolve(tmp_path, """
      - { name: settings, size_kib: 32, fs: littlefs, flash_device: storage, mount: /lfs/settings }
    """)

    def test_resolve_flash_device_refuses_the_subregion_directly(
            self, tmp_path):
        """Defense in depth: `_resolve_flash_device()` itself must refuse
        a `carveout: false` sub-region even for a caller that bypasses
        the loader's eager `_known_flash_devices()` cross-check.
        Exercised directly since nothing else in this file reaches that
        branch -- the loader check above always fires first for a
        board.yaml-driven call.

        `mram_main`, not `ospi0`, is the settings partition's device here:
        E1M-AEN801 declares `ospi0` `assembled: false` (alp-sdk#2311), so
        it is no longer a `_known_flash_devices()` member this board.yaml
        could name at all -- unrelated to the sub-region refusal this
        test exercises.
        """
        from tan.planner import load_board_yaml
        from tan.planner.partition import _resolve_flash_device

        path = _write_board(tmp_path, _aen801("""
      - { name: settings, size_kib: 32, fs: littlefs, flash_device: mram_main, mount: /lfs/settings }
    """))
        project = load_board_yaml(path)
        descriptor, reason = _resolve_flash_device(
            "storage", project.som_preset, project.effective_metadata_root())
        assert descriptor is None, descriptor
        assert "is a partition inside a" in (reason or ""), reason
        assert "flash-class region" in (reason or ""), reason


class TestNoFalsePositives:
    def test_a_genuinely_free_flash_device_is_unaffected(self, tmp_path):
        """A partition aimed at a flash device the map DOES leave free
        must still allocate -- the bounds check must not false-positive
        on it.

        alp-sdk#1484 removed `storage` as a legal `flash_device:` target
        -- it is a `carveout: false` region, a partition label *inside*
        the `mram_storage` flash node, not a Devicetree label of its own
        (see `test_naming_the_mram_storage_subregion_directly_is_refused`
        above). E1M-AEN301's `mram_main` is fully tiled by its own
        sub-regions, so no `memory_map:` device on this SoM is free; this
        exercises the same "must not false-positive" property against
        `ospi0` (`on_module.ospi_memories`, 32 MiB via
        `capacity_mbit: 256`), which `_resolve_flash_device()` still
        resolves and has room to spare -- unaffected by this fix, since
        the allocator bounds logic under test is device-independent. Not
        a claim that `ospi0` is a verified, board-tree-backed flash
        device on E1M-AEN301.
        """
        from tan.planner import load_board_yaml, resolve_storage_partitions

        path = _write_board(tmp_path, """
        name: test-aen301-region-target
        som:
          sku: E1M-AEN301
          hw_rev: r1

        cores:
          m55_hp:
            os: zephyr
            app: ./m55_hp

        storage:
          - { name: settings, size_kib: 64, fs: littlefs, flash_device: ospi0, mount: /lfs/settings }
        """)
        parts = resolve_storage_partitions(load_board_yaml(path))
        settings = _by_name(parts)["settings"]
        assert getattr(settings, "status", None) != "blocked", settings.reason
        assert settings.base_kib == 0, settings


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
