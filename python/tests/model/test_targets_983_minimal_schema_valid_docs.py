# SPDX-License-Identifier: Apache-2.0
"""tan-cli#983: does `resolve_targets()` survive a document that carries
*only* what `soc-spec-v1.schema.json` actually REQUIRES?

tan-cli#964 landed read-path schema validation; the natural (wrong) reading
of that is "a schema-valid document is now safe for `model/targets.py` to
read". It is not, and tan-cli#983 is the follow-up that says so explicitly:
`$defs/npu` requires only `type` plus `anyOf(gops, tops)`, so
`{"type": "ethos-u55", "gops": 100}` is fully schema-valid and still carries
none of `mac_per_cycle`, `subtype`, `paired_core` -- fields `_soc_targets()`
reads. tan-cli#965 already closed the one live crash this produced
(`mac_per_cycle` reaching an unguarded f-string), and that fix is pinned by
`test_targets_nonstring_npu_type.py::test_an_ethos_u_npu_missing_the_optional_
mac_per_cycle_does_not_crash`.

THIS FILE IS THE SWEEP'S RECORD, MADE EXECUTABLE. #983 asked whether
`mac_per_cycle` was "the only instance" of a schema-optional-but-effectively-
required field in `targets.py`/`build.py`/`check.py` and their neighbours
(`analyze.py`, `perf.py`, `perf_apply.py`, `soc_ref.py`,
`commands/size_cmd.py`) -- the sweep (see the PR this file shipped with)
found no other LIVE instance: every other schema-optional field those readers
touch is already behind a `.get()` + `isinstance` guard and degrades on
absence rather than assuming presence. Rather than leave that conclusion only
in a PR description, this builds the ACTUAL minimal-required-fields-only
shape for all three NPU backend families `_npu_backend()` recognises --
exactly the "declares nothing but what the schema demands" input #983 is
about -- and proves the whole `resolve_targets()` pipeline (not a private
helper) survives it. A future field added to `_soc_targets`/`_vela_profile`/
`_discrete_socs` with a bare subscript instead of a guarded read fails HERE,
against this same minimal shape, the same way the original `mac_per_cycle`
defect would have.

Durable fix filed separately: alp-sdk#1849 proposes tightening
`soc-spec-v1.schema.json`'s `$defs/npu` (an `if/then` requiring
`mac_per_cycle` when `type` matches `ethos-u*`, mirroring the sibling
`vela_profile` block's own `system_config_requires_vendor_config` ->
`vendor_config_filename` if/then) -- schema validation and this guard are
complementary, per #964/#965's own decided rule, not substitutes: tightening
the schema does not make this test (or the guard it exercises) redundant, it
only moves the equivalent authoring-time mistake from "silently skipped at
read" to "refused at `new-som` write".
"""
import json

import pytest

from tan.model.targets import resolve_targets

_SKU = "E1M-FAKE983"
_SILICON = "fakevendor:fakefamily:fakepart"


def _minimal_soc(npus: list[dict]) -> dict:
    """The smallest `soc-spec-v1.schema.json`-valid document that carries
    @npus verbatim: every OTHER top-level required key
    (`soc_spec_version`/`vendor`/`family`/`part`/`cores`/`peripherals`) filled
    with the smallest value its own sub-schema allows, none of them read by
    `model/targets.py` at all -- this fixture's only variable is `npus`."""
    return {
        "soc_spec_version": 1,
        "ref": _SILICON,
        "vendor": "fakevendor",
        "family": "fakefamily",
        "part": "fakepart",
        "cores": [{"id": "cpu0", "type": "cortex-a", "count": 1}],
        "npus": npus,
        "peripherals": {},
    }


def _metadata_root(tmp_path, npus: list[dict]) -> object:
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        f"sku: {_SKU}\nsilicon: {_SILICON}\n", encoding="utf-8")
    vendor, family, part = _SILICON.split(":")
    soc_dir = root / "socs" / vendor / family
    soc_dir.mkdir(parents=True)
    (soc_dir / f"{part}.json").write_text(
        json.dumps(_minimal_soc(npus)), encoding="utf-8")
    return root


# One minimal (required-fields-only) NPU entry per `_npu_backend()` prefix
# family -- `gops`/`tops` alternated across the three so the `anyOf` branch
# that resolves each is exercised at least once, not just the `gops` half
# #983's own worked example used.
#
# `ethos_u` is DELIBERATELY EXCLUDED from this map: unlike `drpai`/
# `deepx_dxm1`, a minimal `ethos_u` entry does NOT resolve a real target --
# there is no honest partial `accel_config` to build without `mac_per_cycle`
# (`_soc_targets`'s own comment), so #965's guard correctly SKIPS it, the
# same "no mappable backend for this entry" outcome an unrecognised `type`
# already produces. That is a DIFFERENT, already-more-thoroughly-pinned
# assertion (`test_a_minimal_ethos_u_npu_is_unmappable_without_mac_per_cycle_
# not_crashed`, below) -- folding it into this map would assert the wrong
# thing (that it resolves) for the one backend #983 is actually about.
_MINIMAL_NPU_BY_BACKEND = {
    "drpai": {"type": "drp-ai3", "gops": 50},
    "deepx_dxm1": {"type": "dx-m1", "tops": 8},
}


@pytest.mark.parametrize("backend, npu", sorted(_MINIMAL_NPU_BY_BACKEND.items()))
def test_a_minimal_schema_valid_npu_resolves_through_the_public_entry_point(
        tmp_path, backend, npu):
    """@npu carries NOTHING beyond what `$defs/npu` requires (`type` plus
    exactly one of `gops`/`tops`) -- no `subtype`, no `paired_core`, and (for
    these two backends, unlike `ethos_u`) that is enough: neither `drpai` nor
    `deepx_dxm1` reads `mac_per_cycle` to build anything. `resolve_targets()`
    (the same public entry point `tan model build`/`tan model check` call,
    not a private helper) must resolve this backend without raising, alongside
    the always-present `cpu` target."""
    specs = resolve_targets(_SKU, metadata_root=_metadata_root(tmp_path, [npu]))
    backends = {s.backend for s in specs}
    assert backend in backends, (
        f"a minimal, schema-valid {npu['type']!r} entry must still resolve "
        f"a {backend!r} target, not silently disappear")
    assert "cpu" in backends


def test_a_soc_declaring_every_minimal_npu_family_at_once_resolves_what_can_resolve(tmp_path):
    """The combined case: one SoC spec, all three backend families, each NPU
    entry carrying only what the schema requires. Proves the guards compose --
    one entry `_soc_targets` cannot map to a real target (`ethos_u`, minimal)
    must not blank out its siblings that CAN (`drpai`/`deepx_dxm1`), matching
    every other "skip this one, keep the rest" guard the module already
    documents for genuinely malformed entries."""
    npus = [*_MINIMAL_NPU_BY_BACKEND.values(),
            {"type": "ethos-u55", "gops": 100}]  # #983's own worked example
    specs = resolve_targets(_SKU, metadata_root=_metadata_root(tmp_path, npus))
    backends = {s.backend for s in specs}
    assert backends == {"drpai", "deepx_dxm1", "cpu"}, (
        "the minimal ethos_u entry must be skipped (no mac_per_cycle to build "
        "an accel_config from), not silently present and not crashed past")


def test_a_minimal_ethos_u_npu_is_unmappable_without_mac_per_cycle_not_crashed(tmp_path):
    """#983's own worked example, reproduced against the FULL minimal
    document shape (not just the `{"type": ..., "gops": ...}` fragment
    `test_targets_nonstring_npu_type.py` already pins) -- named explicitly
    for `mac_per_cycle` because it is the one instance #965 already guards:
    unlike `drpai`/`deepx_dxm1` above, `ethos_u` cannot resolve a real
    `accel_config` from a minimal entry (there is no honest partial value --
    see `_soc_targets`'s own comment), so it degrades to the same
    "no mappable backend for this entry" outcome an unrecognised `type`
    already produces, not a crash and not a garbled accel_config."""
    specs = resolve_targets(
        _SKU, metadata_root=_metadata_root(
            tmp_path, [{"type": "ethos-u55", "gops": 100}]))
    assert {s.backend for s in specs} == {"cpu"}
