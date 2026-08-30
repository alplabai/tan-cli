# SPDX-License-Identifier: Apache-2.0
"""tan-cli#983: does `resolve_targets()` survive a document that carries
*only* what `soc-spec-v1.schema.json`/`som-preset-v1.schema.json` actually
REQUIRE?

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

`_discrete_socs` NEEDS A SECOND SoC JSON TO BE LIVE AT ALL. `_metadata_root`
below writes exactly one SoC JSON -- the host's own -- so every entry
`_discrete_socs` enumerates under `socs/**/*.json` hits its own
`ref == host_ref` guard and `continue`s before either `soc.get("variants",
[])` or `v.get("alp_module_skus", [])` is ever reached; a test built only on
`_metadata_root` cannot regress that function no matter what it asserts.
`_metadata_root_with_discrete_accelerator` below adds the real
DEEPX-DX-M1-on-V2M shape (`test_resolve_targets_for_v2m101_folds_in_on_
module_deepx`, `test_targets.py`, which needs a bound `ALP_SDK_ROOT` this
file does not) -- a SECOND SoC JSON, a different `ref`, found only through
`variants[].alp_module_skus` -- so both guards run for real, and so does
`resolve_targets`'s second `_soc_targets()` call (on a non-host SoC,
`targets.py:353-355`), which no other test in this file reaches either.

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
import yaml

from tan.model.targets import resolve_targets

_SKU = "E1M-FAKE983"
_SILICON = "fakevendor:fakefamily:fakepart"
# The on-module discrete accelerator's own SoC ref -- distinct from the host,
# found only through `variants[].alp_module_skus`, mirroring the real
# `deepx:dx:m1` shape E1M-V2M101 uses (`test_targets.py`).
_DISCRETE_SILICON = "deepx:dx:m1"
# A THIRD, unrelated SoC JSON living in the same `socs/` tree -- most real
# parts (anything with no on-module discrete-accelerator concept) never
# declare `variants` at all. `_discrete_socs` globs EVERY file under `socs/`,
# not just the host and the one accelerator that matches this SKU, so this is
# the file that actually exercises `soc.get("variants", [])`'s DEFAULT: a
# bare `soc["variants"]` raises on THIS file (which legitimately carries no
# such key), not on the accelerator's own.
_SIBLING_SILICON = "fakevendor:fakefamily:solo"


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


def _minimal_som_preset() -> dict:
    """The smallest `som-preset-v1.schema.json`-valid document naming
    `_SKU`/`_SILICON` -- every required top-level key (`schema_version`/
    `sku`/`family`/`silicon`/`display_name`/`on_module`/`memory`/
    `inference`/`topology`/`default_hw_rev`/`default_board`/`status`), each
    filled with the smallest value its own sub-schema allows. Genuinely
    schema-minimal on purpose: the earlier `sku:`/`silicon:`-only fixture
    left the `som_preset` half of #983's premise ("carries only what the
    schema actually REQUIRES") with no executable coverage at all, since 10
    of these 12 keys were simply absent rather than minimal.

    Two fields need a specific value, not just "smallest legal", to avoid
    pulling in a THIRD required block this fixture has nothing to say about:
    `topology`'s one entry sets `os: "off"` -- the schema's own `allOf` makes
    `mailbox:` required whenever every `topology` entry's `os` is
    `zephyr`/`baremetal`, and `off` is the one legal value that sidesteps
    that conditional rather than fabricating a mailbox controller this
    fixture doesn't need. `inference.preferred_backend` avoids `ethos_u` for
    the same reason: that value conditionally requires `ethos_u_variant`
    too."""
    return {
        "schema_version": 1,
        "sku": _SKU,
        "family": "fakefamily",
        "silicon": _SILICON,
        "display_name": "Fake 983 SoM",
        "on_module": {"silicon": _SILICON},
        "memory": {"dram_mbit": 1024, "flash_mbit": 1024},
        "inference": {"preferred_backend": "drpai"},
        "topology": {"cpu0": {"os": "off"}},
        "default_hw_rev": "a",
        "default_board": "fake-983-board",
        "status": {"preliminary": False, "partial_hw_config": False},
    }


def _metadata_root(tmp_path, npus: list[dict]) -> object:
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        yaml.safe_dump(_minimal_som_preset()), encoding="utf-8")
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
# already produces. That is a DIFFERENT assertion, already pinned by
# `test_an_ethos_u_npu_missing_the_optional_mac_per_cycle_does_not_crash`
# (`test_targets_nonstring_npu_type.py`) against the exact same
# `{"type": "ethos-u55", "gops": 100}` document -- folding it into this map
# would assert the wrong thing (that it resolves) for the one backend #983
# is actually about, and pinning it a second time here would only restate
# that existing test under a different document wrapper.
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


def _discrete_accelerator_soc() -> dict:
    """The real on-module DEEPX DX-M1 shape E1M-V2M101 uses
    (`test_resolve_targets_for_v2m101_folds_in_on_module_deepx`,
    `test_targets.py`): a SECOND, schema-valid SoC JSON under `socs/`, a
    different `ref` from the host, found only through
    `variants[].alp_module_skus` -- never through `sku:` or a hardcoded
    backend->silicon map (`_discrete_socs`'s own module docstring).

    Two variants on purpose, not one: `other-die` carries no
    `alp_module_skus` key at all (a sibling die option this SKU does not
    populate -- a real SoC spec can and does list more than one variant);
    only `m1` names `_SKU`. That exercises `v.get("alp_module_skus", [])`'s
    DEFAULT, not merely its presence -- a bare `v["alp_module_skus"]` raises
    on the FIRST variant, before ever reaching the one that actually matches
    this SKU."""
    return {
        "soc_spec_version": 1,
        "ref": _DISCRETE_SILICON,
        "vendor": "deepx",
        "family": "dx",
        "part": "m1",
        "cores": [{"id": "npu0", "type": "npu", "count": 1}],
        "npus": [_MINIMAL_NPU_BY_BACKEND["deepx_dxm1"]],
        "peripherals": {},
        "variants": [
            {"variant_id": "other-die", "memory_mb": 2},
            {"variant_id": "m1", "alp_module_skus": [_SKU]},
        ],
    }


def _sibling_soc_without_variants() -> dict:
    """A plain, unrelated SoC JSON in the same `socs/` tree that declares no
    `variants` block at all -- the common case (most real SoC specs have no
    on-module discrete-accelerator concept to declare). `_discrete_socs`
    enumerates every file under `socs/**/*.json`, this one included; it must
    fall through `soc.get("variants", [])`'s default and be skipped (this SKU
    is not in its, nonexistent, variant list), not raise."""
    return {
        "soc_spec_version": 1,
        "ref": _SIBLING_SILICON,
        "vendor": "fakevendor",
        "family": "fakefamily",
        "part": "solo",
        "cores": [{"id": "cpu0", "type": "cortex-a", "count": 1}],
        "npus": [],
        "peripherals": {},
    }


def _metadata_root_with_discrete_accelerator(tmp_path) -> object:
    """Same tree `_metadata_root` writes (host SoC carries a minimal `drpai`
    NPU, matching E1M-V2M101's own host silicon), PLUS the on-module DEEPX
    accelerator's SoC JSON and an unrelated sibling SoC JSON with no
    `variants` block -- the only fixture in this file where `_discrete_socs`
    (`targets.py:288-299`) does anything at all. Every other test above
    writes exactly one SoC JSON, the host's own, always skipped by
    `ref == host_ref` before either `.get()` call inside `_discrete_socs`
    runs."""
    root = _metadata_root(tmp_path, [_MINIMAL_NPU_BY_BACKEND["drpai"]])
    discrete_dir = root / "socs" / "deepx" / "dx"
    discrete_dir.mkdir(parents=True)
    (discrete_dir / "m1.json").write_text(
        json.dumps(_discrete_accelerator_soc()), encoding="utf-8")
    # Same vendor/family dir `_metadata_root` already created for the host
    # (both `fakevendor`/`fakefamily`) -- no mkdir needed, just the file.
    (root / "socs" / "fakevendor" / "fakefamily" / "solo.json").write_text(
        json.dumps(_sibling_soc_without_variants()), encoding="utf-8")
    return root


def test_a_discrete_accelerator_soc_resolves_through_the_public_entry_point(tmp_path):
    """The real E1M-V2M101 shape (host `drpai` + on-module discrete DEEPX),
    rebuilt schema-minimal and SDK-free so it runs with no `ALP_SDK_ROOT`
    bound. Proves `_discrete_socs` -- unexercised by every test above it,
    each of which writes exactly one SoC JSON -- resolves the accelerator
    through `soc.get("variants", [])` and `v.get("alp_module_skus", [])`
    exactly as `_soc_targets` resolves the host's own `npus[]`, and that
    `resolve_targets`'s second `_soc_targets()` call, on a non-host SoC
    (`targets.py:353-355`), runs at all."""
    specs = resolve_targets(
        _SKU, metadata_root=_metadata_root_with_discrete_accelerator(tmp_path))
    by = {(s.backend, s.silicon_ref) for s in specs}
    assert ("drpai", _SILICON) in by
    assert ("deepx_dxm1", _DISCRETE_SILICON) in by, (
        "the on-module DEEPX accelerator, found only through "
        "variants[].alp_module_skus on a SECOND SoC JSON, must resolve "
        "through the public entry point, not silently disappear")
    assert ("cpu", "*") in by
