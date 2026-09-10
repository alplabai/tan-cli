# SPDX-License-Identifier: Apache-2.0
"""The ordering guard for alp-sdk#1365 split B (hand-ported into
`tan/planner/carveout.py` + `tan/planner/aperture.py`) -- P1's flash-class
exclusion must hold EVEN IF `mram_main`'s `base: "TBD"`
(`metadata/e1m_modules/E1M-AEN801.yaml`) were filled in tomorrow. Split B
deliberately does NOT fill that field (a separate, later step); this test
proves the field staying `"TBD"` is not secretly load-bearing for safety.

Also covers two gaps an alp-sdk#1365 split B review found in
`_region_ipc_eligibility()` (`tan/planner/carveout.py`):

  - `TestUnclassifiedWriteAuthorityLegCoverage` -- the positive mirror of
    the ordering guard: a preset-authored row OUTSIDE the aperture with
    `write_authority: customer_runtime` must resolve `status: ok`.
  - `TestCarveoutAgreementBlocker` -- a present `carveout:` that DISAGREES
    with the derived class must refuse, naming both the derived class
    (with addresses) and the authored flag, instead of letting
    `write_authority: customer_runtime` alone silently drop an authored
    `carveout: false`.

The hazard this closes: `mram_main` is the only region on E1M-AEN801 (and
its AEN siblings) that lists an `a32_cluster`/`m55_*` endpoint AND carries
no `carveout` key at all -- pre-port `tan/planner/carveout.py` read
`if region.get("carveout") is False`, so an absent key meant ELIGIBLE. The
allocator is top-down and seeds `region_top` from `base + size` alone with
no knowledge that mcuboot/he_slot0/hp_slot0/reserved/storage/atoc tile the
same window. Before this port the ONLY thing keeping an `a32_cluster` IPC
entry out of the live `atoc` band (0x80578000..0x80580000) was
`mram_main`'s unresolved `base: "TBD"`.

This test synthetically resolves that `base` (0x80000000, matching the
declared aperture floor -- `metadata/socs/alif/ensemble/e8.json`'s
`soc_flash_base`) in an in-memory copy of the loaded project, WITHOUT
touching the tracked YAML, and asserts the `ipc:` entry still blocks --
naming the DERIVED flash class, not an address inside `atoc`. A green run
here against PRE-PORT `tan/planner/carveout.py` (`if region.get("carveout")
is False`) goes RED -- verified by mutation (temporarily reverting
`aperture.py`/`carveout.py`/`partition.py` and re-running this module) at
port time, reported alongside the port rather than re-derived on every run.

Real-SDK-gated: needs the actual `examples/multicore/rpmsg-aen/board.yaml`
+ `examples/multicore/mproc-mailbox/board.yaml` and E1M-AEN801's real SoM
preset from a bound alp-sdk checkout -- same requirement as
`tests/parity/test_planner_emit_parity.py`.

Split A is NOT merged upstream. Checked 2026-09-06: alp-sdk `origin/dev`
(e296881ff12d50e347eec623cfb31798499f8d7d) and `origin/main`
(eb96112b) carry neither `scripts/alp_orchestrate/aperture.py` nor
`metadata/socs/alif/ensemble/e8.json`'s `soc_flash_base`, so NO bound
checkout declares the aperture every assertion here turns on -- against
one that doesn't, `resolve_aperture()` returns None, `carveout.py`
honours the legacy `carveout:` flag verbatim, and all three exclusion
tests below resolve `ok`.

Skipping on that would be the wrong answer: the one CI job that binds a
real checkout and runs this directory (`unsharded-python-canary.yml` --
`pytest tests/commands tests/planner` with `ALP_SDK_ROOT` bound) clones
alp-sdk's published tree, so a presence-gated skip would make every test
in this module vacuous exactly where it is supposed to bite, and stay
vacuous silently after split A lands somewhere unnoticed. Instead the
`split_a_metadata` fixture COPIES the bound checkout's `metadata/` tree
and injects `soc_flash_base` INTO THE COPY -- the same synthetic-what-if
spirit as `_with_mram_main_resolved()` below, which fills in
`mram_main.base` in an in-memory preset copy without touching the tracked
YAML. Nothing in the bound checkout is written. When split A does land
and the checkout already declares the field, the fixture ASSERTS the
declared value rather than overwriting it, so a value that disagrees with
`_E8_APERTURE_BASE` surfaces as fixture drift instead of a green run
against a stale assumption.

Run locally:

    python -m pytest python/tests/planner/test_carveout_aperture_ordering.py -v
"""

from __future__ import annotations

import copy
import json
import shutil

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
# tan-cli#1081 requires every real-SDK-gated module reuse this ONE
# definition rather than redefining it locally.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

_RPMSG_AEN_REL = ("examples", "multicore", "rpmsg-aen", "board.yaml")
_MPROC_MAILBOX_REL = ("examples", "multicore", "mproc-mailbox", "board.yaml")

HAS_EXAMPLES = SDK is not None and all(
    SDK.joinpath(*rel).is_file() for rel in (_RPMSG_AEN_REL, _MPROC_MAILBOX_REL)
)

pytestmark = pytest.mark.skipif(
    not HAS_EXAMPLES,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout that ships "
           "examples/multicore/{rpmsg-aen,mproc-mailbox}/board.yaml to run "
           "the alp-sdk#1365 split B ordering guard",
)

# metadata/socs/alif/ensemble/e8.json's `soc_flash_base` (alp-sdk#1365
# split A).
_E8_APERTURE_BASE = 0x80000000
# E8's declared aperture top (base + variant AE822FA0E5597LS0's 5.5 MiB
# mram_mb): [0x80000000, 0x80580000). A row resolving here is OUTSIDE it.
_OUTSIDE_APERTURE_BASE = 0xA0000000


def _rpmsg_aen_board():
    return SDK.joinpath(*_RPMSG_AEN_REL)


def _mproc_mailbox_board():
    return SDK.joinpath(*_MPROC_MAILBOX_REL)


@pytest.fixture(scope="module")
def split_a_metadata(tmp_path_factory):
    """A COPY of the bound checkout's `metadata/` tree that declares
    `soc_flash_base` (alp-sdk#1365 split A), whatever the checkout itself
    declares.

    The copy is what every test here binds as the project's
    `metadata_root`; the bound checkout is only ever READ. See the module
    docstring for why this is an injection and not a skip.
    """
    src = SDK / "metadata"
    assert src.is_dir(), (
        f"fixture drift: the bound alp-sdk checkout has no metadata/ tree "
        f"at {src} -- HAS_EXAMPLES found its board.yaml files, so this is "
        f"a half-populated checkout, not an absent root")
    root = tmp_path_factory.mktemp("split-a-metadata") / "metadata"
    shutil.copytree(src, root)

    # `soc-spec-v1.schema.json` closes `additionalProperties`, and
    # `loader.py`'s `_refuse_on_schema_errors()` (tan-cli#964) validates
    # every SoC spec it reads -- so injecting the key into e8.json alone
    # makes `load_board_yaml()` REFUSE the board outright. Split A amends
    # the schema in the same commit; the copy has to as well, in the same
    # shape (`"type": "integer"`, `"minimum": 0`).
    schema = root / "schemas" / "soc-spec-v1.schema.json"
    assert schema.is_file(), (
        f"fixture drift: no {schema.relative_to(root)} in the copy")
    schema_doc = json.loads(schema.read_text(encoding="utf-8"))
    props = schema_doc.get("properties")
    assert isinstance(props, dict), (
        "fixture drift: soc-spec-v1.schema.json declares no `properties` "
        "object to inject soc_flash_base into")
    if "soc_flash_base" not in props:
        props["soc_flash_base"] = {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Injected by tests/planner/test_carveout_aperture_ordering"
                ".py against a pre-split-A checkout; alp-sdk#1365 split A "
                "declares the real one."),
        }
        schema.write_text(
            json.dumps(schema_doc, indent=2) + "\n", encoding="utf-8")

    e8 = root / "socs" / "alif" / "ensemble" / "e8.json"
    assert e8.is_file(), f"fixture drift: no {e8.relative_to(root)} in the copy"
    spec = json.loads(e8.read_text(encoding="utf-8"))
    declared = spec.get("soc_flash_base")
    if declared is None:
        # Pre-split-A checkout (every published alp-sdk ref as of
        # 2026-09-06). Inject, in the copy only.
        spec["soc_flash_base"] = _E8_APERTURE_BASE
        e8.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    else:
        assert declared == _E8_APERTURE_BASE, (
            f"fixture drift: the bound checkout declares e8.json "
            f"soc_flash_base=0x{declared:x}, not the 0x{_E8_APERTURE_BASE:x} "
            f"this module's _OUTSIDE_APERTURE_BASE "
            f"(0x{_OUTSIDE_APERTURE_BASE:x}) and ATOC-band constants are "
            f"written against -- re-derive them before silencing this")
    return root


def _load(board_path, metadata_root):
    """Load `board_path` reading SoM/SoC facts from `metadata_root` (the
    `split_a_metadata` copy), via `load_board_yaml`'s own supported seam --
    `BoardProject.metadata_root` is never reassigned after the fact."""
    from tan.planner import load_board_yaml
    return load_board_yaml(board_path, metadata_root=metadata_root)


def _with_mram_main_resolved(project):
    """Return `project` with an in-memory-only `mram_main.base` fill-in.

    Deep-copies `som_preset` first so this never mutates the tracked
    `metadata/e1m_modules/E1M-AEN801.yaml` -- split B leaves that file's
    `base: "TBD"` untouched; this is purely a synthetic what-if.
    """
    project.som_preset = copy.deepcopy(project.som_preset)
    found = False
    for region in project.som_preset["memory_map"]:
        if region.get("name") == "mram_main":
            region["base"] = _E8_APERTURE_BASE
            found = True
    assert found, "fixture drift: E1M-AEN801.yaml no longer declares mram_main"
    return project


def _with_outside_aperture_row_added(project, *, carveout=None):
    """Return `project` with an extra preset-authored `memory_map:` row
    appended in-memory, resolving OUTSIDE the declared aperture
    `[0x80000000, 0x80580000)` (e.g. an OSPI XIP window) and carrying
    `write_authority: customer_runtime`.

    Deep-copies `som_preset` first -- never mutates the tracked
    `metadata/e1m_modules/E1M-AEN801.yaml`.
    """
    project.som_preset = copy.deepcopy(project.som_preset)
    row = {
        "name": "ospi_xip_test" if carveout is None else "ospi_xip",
        "base": _OUTSIDE_APERTURE_BASE,
        "size_kib": 1024,
        "accessible_from": ["a32_cluster", "m55_hp"],
        "write_authority": "customer_runtime",
    }
    if carveout is not None:
        row["carveout"] = carveout
    project.som_preset["memory_map"].append(row)
    return project


def _by_name(carve_outs):
    return {c.name: c for c in carve_outs}


class TestMramMainOrderingGuard:
    """alp-sdk#1365 split B: resolving `mram_main`'s base must NOT
    resurrect the ATOC-overwrite hazard -- the flash-class exclusion has to
    hold on its own, independent of the TBD placeholder."""

    def test_a32_cluster_rpmsg_entry_stays_blocked_once_mram_main_resolves(
            self, split_a_metadata):
        from tan.planner import resolve_carve_outs

        project = _with_mram_main_resolved(_load(_rpmsg_aen_board(), split_a_metadata))
        resolved = _by_name(resolve_carve_outs(project))
        entry = resolved["alp_default_rpmsg"]

        assert entry.status == "blocked", (
            f"a32_cluster ipc entry resolved {entry.status!r} once "
            f"mram_main's base was filled in -- the flash-class exclusion "
            f"did not hold on its own; base={entry.base:#x}")
        # The whole point: it must not merely happen to land somewhere
        # harmless -- it must be refused with a reason naming the DERIVED
        # flash class, not silently re-blocked for an unrelated cause (e.g.
        # a stale mailbox-metadata check tripping first).
        assert "flash-class" in entry.reason, (
            f"blocked for the wrong reason: {entry.reason!r}")
        assert "mram_main" in entry.reason

    def test_never_allocates_inside_the_atoc_band(self, split_a_metadata):
        """Even if some future change loosened the exclusion, the base must
        never land in [0x8057_8000, 0x8058_0000) -- the live ATOC band --
        while still reporting `status: ok`."""
        from tan.planner import resolve_carve_outs

        project = _with_mram_main_resolved(_load(_rpmsg_aen_board(), split_a_metadata))
        entry = _by_name(resolve_carve_outs(project))["alp_default_rpmsg"]
        if entry.status == "ok":
            assert not (0x80578000 <= entry.base < 0x80580000), (
                f"carve-out placed at 0x{entry.base:x}, inside the live "
                f"ATOC band -- exactly the alp-sdk#1365 hazard")

    def test_raw_shmem_entry_on_aen801_also_stays_blocked(
            self, split_a_metadata):
        """`mproc-mailbox`'s raw_shmem entry (m55_hp/m55_he, not
        a32_cluster) exercises the same SoM/aperture with a different
        `ipc.kind` -- the exclusion must not be accidentally scoped to
        `rpmsg` alone."""
        from tan.planner import resolve_carve_outs

        project = _with_mram_main_resolved(_load(_mproc_mailbox_board(), split_a_metadata))
        entry = _by_name(resolve_carve_outs(project))["alp_shmem0"]
        assert entry.status == "blocked"
        assert "flash-class" in entry.reason


class TestUnclassifiedWriteAuthorityLegCoverage:
    """MAJOR 4 / alp-sdk#2010: the ordering guard above had zero coverage on
    the leg that stops a future authored OSPI XIP row from silently
    becoming an IPC candidate just because it resolves outside the
    aperture -- dropping ONLY the `write_authority == "customer_runtime"`
    check on the `cls == "unclassified"` branch (`carveout.py`'s
    `_region_ipc_eligibility()`) is caught by NOTHING else.

    The first test below is the positive mirror of
    `TestMramMainOrderingGuard`: it asserts a preset-authored row OUTSIDE
    the aperture with `write_authority: customer_runtime` DOES resolve
    `status: ok` -- losing that leg (mutated to never grant eligibility)
    flips this entry to `blocked` and turns this test red.

    That test is positive-only, though, so it is blind to the OPPOSITE
    mutation -- `derived_eligible = wa == "customer_runtime"` replaced
    with `derived_eligible = True` unconditionally, dropping the
    requirement rather than inverting it (alp-sdk#2010, mutant C4). The
    second and third tests below close that: an outside-aperture,
    preset-authored row whose `write_authority` is anything OTHER than
    `customer_runtime` (or absent) must still be refused.
    """

    def test_outside_aperture_authored_row_with_customer_runtime_resolves_ok(
            self, split_a_metadata):
        from tan.planner import resolve_carve_outs

        project = _with_outside_aperture_row_added(_load(_rpmsg_aen_board(), split_a_metadata))
        entry = _by_name(resolve_carve_outs(project))["alp_default_rpmsg"]

        assert entry.status == "ok", (
            f"a32_cluster ipc entry resolved {entry.status!r} against an "
            f"outside-aperture authored row carrying "
            f"write_authority: customer_runtime; reason={entry.reason!r}")
        assert entry.region == "ospi_xip_test"
        assert _OUTSIDE_APERTURE_BASE <= entry.base < (
            _OUTSIDE_APERTURE_BASE + 1024 * 1024)

    def test_outside_aperture_authored_row_with_wrong_write_authority_blocks(self):
        """alp-sdk#2010 mutant C4: `derived_eligible = wa ==
        "customer_runtime"` mutated to `derived_eligible = True`
        unconditionally survived every gate because nothing exercised the
        refusal direction on this branch. A preset-authored row resolving
        OUTSIDE the aperture with `write_authority: vendor_image` (any
        value other than `customer_runtime`) must be refused -- this is
        exactly the hazard `_region_ipc_eligibility()`'s own docstring
        names: "a future authored OSPI XIP row must NOT silently become an
        IPC candidate just because it resolves outside the aperture."."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": _OUTSIDE_APERTURE_BASE, "size_kib": 1024,
             "write_authority": "vendor_image"},
            (_E8_APERTURE_BASE, 0x80580000),
            True)
        assert eligible is False, (
            "an outside-aperture, preset-authored row with "
            "write_authority: vendor_image (not customer_runtime) "
            "became IPC-eligible -- mutant C4 (derived_eligible -> True "
            "unconditionally) is back")
        assert "vendor_image" in reason
        assert "customer_runtime" in reason

    def test_outside_aperture_authored_row_with_no_write_authority_blocks(self):
        """Same hazard, absent `write_authority:` rather than a wrong
        value -- `ABSENT MEANS UNRESOLVED, NEVER customer_runtime`
        (`som-preset-v1.schema.json`'s `write_authority` description)."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": _OUTSIDE_APERTURE_BASE, "size_kib": 1024},
            (_E8_APERTURE_BASE, 0x80580000),
            True)
        assert eligible is False
        assert "None" in reason


class TestCarveoutAgreementBlocker:
    """A present `carveout:` that DISAGREES with the derived class must
    refuse, naming BOTH facts -- the derived class (with the addresses
    that produced it) and the authored flag. Before the fix,
    `_region_ipc_eligibility()` decided eligibility from `write_authority`
    alone on both the `cls == "ram"` and `cls == "unclassified"` branches,
    silently dropping a contradicting `carveout:` value.

    Reproduces the exact probe run against the real `rpmsg-aen` project:
    appending `{name: ospi_xip, base: 0xA0000000, size_kib: 1024,
    carveout: false, write_authority: customer_runtime}` to
    E1M-AEN801.yaml's `memory_map:`.
    """

    def test_carveout_false_disagreeing_with_write_authority_refuses(
            self, split_a_metadata):
        from tan.planner import resolve_carve_outs

        project = _with_outside_aperture_row_added(
            _load(_rpmsg_aen_board(), split_a_metadata), carveout=False)
        entry = _by_name(resolve_carve_outs(project))["alp_default_rpmsg"]

        assert entry.status == "blocked", (
            f"a32_cluster ipc entry resolved {entry.status!r} onto a "
            f"region carrying `carveout: false` -- the AGREE contract did "
            f"not hold; base={entry.base:#x} region={entry.region!r}")
        assert "ospi_xip" not in entry.region
        # Both facts named: the derived class (with the addresses that
        # produced it) AND the authored flag it disagrees with.
        assert "unclassified" in entry.reason
        assert "0xa0000000" in entry.reason and "0xa0100000" in entry.reason
        assert "carveout: False" in entry.reason
        assert "disagrees" in entry.reason


def _with_mram_main_resolved_and_customer_runtime(project):
    """Like `_with_mram_main_resolved` above, but ALSO overwrites
    `mram_main`'s authored `write_authority` to `customer_runtime` --
    the exact edit issue alp-sdk#2009's refused-remedy text used to
    instruct a reader to make (`carveout.py`'s old wording: "...outside
    the ... aperture (or, if inside it, one that resolves
    `write_authority: customer_runtime`)"). `mram_main`'s real authored
    value is `composite` (`metadata/e1m_modules/E1M-AEN801.yaml`); this
    overwrite simulates a customer who followed that remedy verbatim.

    Deep-copies `som_preset` first -- never mutates the tracked YAML."""
    project.som_preset = copy.deepcopy(project.som_preset)
    found = False
    for region in project.som_preset["memory_map"]:
        if region.get("name") == "mram_main":
            region["base"] = _E8_APERTURE_BASE
            region["write_authority"] = "customer_runtime"
            found = True
    assert found, "fixture drift: E1M-AEN801.yaml no longer declares mram_main"
    return project


class TestContainedRegionWriteAuthorityNeverRescues:
    """alp-sdk#2009: `carveout.py`'s refused-remedy message (and
    `docs/board-config-features.md`'s prose) used to tell the reader that
    a region CONTAINED in the declared MRAM aperture could be made
    IPC-eligible by adding `write_authority: customer_runtime`. False --
    `cls == "flash"` refuses UNCONDITIONALLY
    (`_region_ipc_eligibility()`); `write_authority` is consulted ONLY on
    the `cls == "unclassified"` (outside-aperture) branch. These tests
    pin the BEHAVIOUR the corrected remedy now describes truthfully, not
    the wording -- a future change that made `write_authority` actually
    override flash-class containment (implementing the OLD, false
    remedy) would turn this red even though it never touches this file's
    string literals.
    """

    def test_issue_repro_contained_region_with_customer_runtime_still_refuses(self):
        """Direct repro from alp-sdk#2009: a region whose resolved extent
        sits INSIDE the declared aperture, carrying `write_authority:
        customer_runtime`, must still refuse as flash-class -- exactly
        the call the issue used to prove the old remedy false."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": 0x80500000, "size_kib": 64,
             "write_authority": "customer_runtime"},
            (0x80000000, 0x80580000),
            True)
        assert eligible is False, (
            "a region CONTAINED in the aperture became IPC-eligible via "
            "write_authority: customer_runtime -- the alp-sdk#2009 "
            "inversion is back")
        assert "flash-class" in reason

    def test_mram_main_resolved_with_customer_runtime_stays_blocked(
            self, split_a_metadata):
        """End-to-end mirror of the unit repro above, through the same
        `rpmsg-aen` project `TestMramMainOrderingGuard` uses: a customer
        who followed the OLD (false) remedy on `mram_main` -- filling in
        its `base` AND setting `write_authority: customer_runtime` --
        must still see the ipc entry blocked, not resolved `status: ok`
        inside the live ATOC-tiled aperture."""
        from tan.planner import resolve_carve_outs

        project = _with_mram_main_resolved_and_customer_runtime(
            _load(_rpmsg_aen_board(), split_a_metadata))
        entry = _by_name(resolve_carve_outs(project))["alp_default_rpmsg"]

        assert entry.status == "blocked", (
            f"a32_cluster ipc entry resolved {entry.status!r} after "
            f"mram_main was given write_authority: customer_runtime -- "
            f"the old (false) remedy would now silently work; "
            f"reason={entry.reason!r}")
        assert "flash-class" in entry.reason

    def test_outside_aperture_authored_customer_runtime_is_actually_eligible(self):
        """The positive half of the CORRECTED remedy: a region resolving
        OUTSIDE the declared aperture, authored by the SoM preset itself,
        with `write_authority: customer_runtime`, IS eligible -- proving
        the new wording ("...sits outside the declared MRAM aperture
        (and, if the SoM preset authors the row itself, carries
        `write_authority: customer_runtime`)") describes a real, working
        fix, not just different false wording."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": _OUTSIDE_APERTURE_BASE, "size_kib": 64,
             "write_authority": "customer_runtime"},
            (_E8_APERTURE_BASE, 0x80580000),
            True)
        assert eligible is True, (
            f"a region OUTSIDE the aperture, preset-authored, carrying "
            f"write_authority: customer_runtime was refused: {reason!r} "
            f"-- the corrected remedy no longer describes a working fix")


class TestUnresolvedLegOrdering:
    """alp-sdk#2010: the `cls == "unresolved"` tail of
    `_region_ipc_eligibility()` (this region's OWN `base` doesn't
    resolve) had zero direct coverage of its own precedence and terminal
    cases -- every existing end-to-end test that reaches this tail
    (`mram_main`) carries `write_authority: composite` and no
    `carveout:` key at all, so it only ever exercises the "neither field
    customer_runtime" refusal, never the ordering between the two fields
    or the true no-authored-flag terminal case."""

    def test_unresolved_base_honours_carveout_false_over_customer_runtime(self):
        """C6 (alp-sdk#1365 split B review, MAJOR 2): `carveout:` must be
        checked BEFORE `write_authority` on this tail -- this used to
        check `write_authority` first, silently dropping an authored
        `carveout: false` whenever `write_authority: customer_runtime`
        was also present. No shipped preset authors both fields on an
        unresolved-base row (defensive-only), so this is a direct-call
        pin, not an end-to-end one."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": "TBD", "carveout": False,
             "write_authority": "customer_runtime"},
            (0x80000000, 0x80580000),
            True)
        assert eligible is False, (
            "an unresolved-base region with carveout: False became "
            "IPC-eligible because write_authority: customer_runtime was "
            "checked first -- MAJOR 2 is back")
        assert "carveout: false" in reason.lower()

    def test_unresolved_base_with_neither_flag_refuses_terminal(self):
        """C12 (ADR-0034 clause 4): with base unresolved and NEITHER
        `carveout:` nor `write_authority:` authored, the region must
        refuse -- never guess. This is the function's terminal fallback,
        reached by nothing else in the existing suite.

        Deviation from upstream: the reason string this repo's
        `_region_ipc_eligibility()` returns on this leg does not carry
        the literal `"(ADR-0034 clause 4)"` citation upstream's does --
        a pre-existing divergence in `tan/planner/carveout.py` unrelated
        to this alp-sdk#2010 port (confirmed: `git diff 15b2f32c 20fec7a7
        -- scripts/alp_orchestrate/carveout.py` touches neither this
        message nor the ADR-0034 citation), so this test pins the
        BEHAVIOUR (never-guess refusal, "neither" named) rather than that
        substring."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": "TBD"},
            (0x80000000, 0x80580000),
            True)
        assert eligible is False, (
            "an unresolved-base region with neither `carveout` nor "
            "`write_authority` authored became IPC-eligible -- ADR-0034 "
            "clause 4 (never guess) is broken")
        assert "neither" in reason
        assert "never guessed" in reason


class TestMaxExcludedDetailCap:
    def test_cap_is_six(self):
        """C9 (alp-sdk#2010): the value is unpinned by any behavioural
        test -- pinned directly here. `board.schema.json`'s
        `/$defs/ipc_entry` sets `endpoints.minItems: 2`, and every AEN
        SKU's `memory_map:` has exactly 7 rows, so the true maximum
        simultaneous exclusion count on any SKU shipped today is 5 --
        this cap (6) sits one above that proven ceiling deliberately
        (see `changelog.d/1365-split-b.md` in alp-sdk); it is not itself
        derived from anything that would break if it drifted by one,
        hence the direct pin rather than a behavioural fixture with 7
        excluded regions."""
        from tan.planner.carveout import _MAX_EXCLUDED_DETAIL

        assert _MAX_EXCLUDED_DETAIL == 6


class TestRamLegReachability:
    """C3 / C11 (alp-sdk#2010): the `cls == "ram"` leg of
    `_region_ipc_eligibility()` -- a region OUTSIDE the aperture that the
    SoM preset did NOT author -- is likely UNREACHABLE through any real
    project today: `classify_region()` only returns `"ram"` when
    `is_preset_authored` is False, but every Alif SoM preset in
    `metadata/e1m_modules/*.yaml` that declares an aperture (i.e.
    resolves to an Ensemble SoC with `soc_flash_base`) ALSO authors an
    explicit `memory_map:` block, making `is_preset_authored` True for
    every row `_region_ipc_eligibility()` is ever called with on a live
    preset. `git grep -n "carveout:" metadata/socs/` returns zero hits,
    confirming a SoC-derived (non-preset-authored) row never carries the
    flag either.

    Kept (not deleted): a future minimal Alif SoM port that relies
    entirely on SoC-level `memory_regions:` (no preset `memory_map:`
    override) would make `is_preset_authored` False while an aperture
    still resolves, reaching this leg for real -- the same shape every
    non-Alif SoM already uses, just with an aperture declared. Pinned by
    direct call, the only way to exercise it today; NOT reachable via
    `load_board_yaml()` + `resolve_carve_outs()` against any board.yaml
    in this tree."""

    def test_ram_class_outside_aperture_not_preset_authored_is_eligible(self):
        """C11: the leg's own unconditional `return True, ""` once
        `_agree_or_refuse` finds no disagreement (`carveout:` absent)."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": _OUTSIDE_APERTURE_BASE, "size_kib": 64},
            (_E8_APERTURE_BASE, 0x80580000),
            False)
        assert eligible is True, (
            f"a ram-class region (outside the aperture, not preset-"
            f"authored) with no `carveout:` override was refused: "
            f"{reason!r}")
        assert reason == ""

    def test_ram_class_disagreeing_carveout_false_refuses(self):
        """C3: an authored `carveout: false` disagreeing with the
        derived ram-eligible verdict must refuse, naming both facts --
        the same AGREE contract `TestCarveoutAgreementBlocker` proves on
        the `unclassified` branch, exercised here on the `ram` branch
        instead."""
        from tan.planner.carveout import _region_ipc_eligibility

        eligible, reason = _region_ipc_eligibility(
            {"base": _OUTSIDE_APERTURE_BASE, "size_kib": 64,
             "carveout": False},
            (_E8_APERTURE_BASE, 0x80580000),
            False)
        assert eligible is False, (
            "a ram-class region with an authored carveout: False became "
            "eligible anyway -- the AGREE contract did not hold on the "
            "ram leg")
        assert "ram-class" in reason
        assert "disagrees" in reason
