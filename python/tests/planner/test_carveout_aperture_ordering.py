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
    """The ordering guard above has zero coverage on the leg that stops a
    future authored OSPI XIP row from silently becoming an IPC candidate
    just because it resolves outside the aperture -- dropping ONLY the
    `write_authority == "customer_runtime"` check on the
    `cls == "unclassified"` branch (`carveout.py`'s
    `_region_ipc_eligibility()`) is caught by NOTHING else.

    This test is the positive mirror of `TestMramMainOrderingGuard`: it
    asserts a preset-authored row OUTSIDE the aperture with
    `write_authority: customer_runtime` DOES resolve `status: ok` --
    losing that leg (mutated to never grant eligibility) flips this entry
    to `blocked` and turns this test red.
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
