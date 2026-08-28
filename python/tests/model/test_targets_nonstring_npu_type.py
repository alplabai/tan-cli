# SPDX-License-Identifier: Apache-2.0
"""tan-cli#965: `soc.get("npus", [])[i]["type"]` (and `["subtype"]`) reach
`_npu_backend`'s `.startswith`/`in` calls with no `isinstance` check, on a
value read straight out of an unvalidated SoC JSON (the real fix is
tan-cli#964 -- schema validation on the READ path, not just on `new-som`'s
WRITE path; this guard is the last line of defence, not a substitute).

Unlike the #957 `cores[].type` family, `.get("type", "")`'s default fires
only when the key is ABSENT, never when it is present with the wrong type --
so there was no falsy-safe half here at all: every one of the seven
non-string JSON shapes below raised `AttributeError` on unguarded `dev`,
not just the truthy ones. Reproduced end-to-end through the same public
`resolve_targets()` entry point `tan model build` (`model/build.py:120`) and
`tan model check` (`model/check.py:106,541`) call, on a SYNTHETIC metadata
tree (same convention as `test_targets_vela_profile.py`) so these run in the
bare CI install with no `ALP_SDK_ROOT` bound.

Chosen fallback: an unrecognised/unresolvable `type` degrades to the SAME
outcome a genuinely-absent `npus` key (or a string matching none of the three
known prefixes) already produces -- the NPU is skipped and only the
always-present `cpu` target survives. `test_a_string_that_matches_no_known_prefix_is_the_reference_fallback`
and `test_an_absent_npus_key_is_the_same_fallback` pin that reference
behaviour so the seven non-string shapes can be asserted EQUAL to it, not
just "doesn't crash" (a crash and a silent wrong answer are both a fourth
behaviour; this file rules out both).

PR #967 review follow-ups (same file, same audit, not filed separately):
the two-line `type`/`subtype` guard above protected `npu.get("type")`'s
RETURN value but not the `npu.get` CALL itself, one line above -- the exact
#957 round-4-to-round-5 recurrence #964 exists to stop. The tests below cover
the sites the review measured through this SAME public `resolve_targets()`
entry point: `npus[]` elements/container (`targets.py:203`, folded with
`_discrete_socs`'s identical `variants[]` gap at `:227`), a schema-VALID SoC
doc missing the optional `mac_per_cycle` (`:211` -- #964 cannot fix this one,
the document already passes the schema), a parsed SoC JSON that isn't an
object (`:235`, `:267`), and a non-string `silicon:` in the SoM preset
(`soc_ref.py:41`, a shared leaf also reached through `resolve_targets`)."""
import json

import pytest

from tan.model.targets import resolve_targets
from tan.soc_ref import resolve_soc_path

_SKU = "E1M-FAKE965"
_SILICON = "fakevendor:fakefamily:fakepart"


def _metadata_root(tmp_path, npus: list) -> object:
    """A metadata/ tree with exactly what `resolve_targets` reads: one SoM
    preset naming one SoC, and that SoC's spec carrying @npus verbatim."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        f"sku: {_SKU}\nsilicon: {_SILICON}\n", encoding="utf-8")
    vendor, family, part = _SILICON.split(":")
    soc_dir = root / "socs" / vendor / family
    soc_dir.mkdir(parents=True)
    soc = {"ref": _SILICON, "npus": npus}
    (soc_dir / f"{part}.json").write_text(json.dumps(soc), encoding="utf-8")
    return root


def _backends(tmp_path, npus: list) -> set[str]:
    specs = resolve_targets(_SKU, metadata_root=_metadata_root(tmp_path, npus))
    return {s.backend for s in specs}


# The seven shapes from tan-cli#965's own reproduction table, plus a valid
# control. `id=` names match the issue's table verbatim so a failure names
# the exact shape without decoding a parametrize index.
_NON_STRING_TYPE_SHAPES = [
    pytest.param(7, id="int-7"),
    pytest.param(["ethos-u55"], id="list"),
    pytest.param({"a": 1}, id="dict"),
    pytest.param(True, id="bool-True"),
    pytest.param(None, id="None"),
    pytest.param(0, id="int-0"),
    pytest.param([], id="empty-list"),
]


@pytest.mark.parametrize("bad_type", _NON_STRING_TYPE_SHAPES)
def test_a_nonstring_npu_type_does_not_crash_and_resolves_the_no_match_fallback(
        tmp_path, bad_type):
    """Every one of the seven shapes must resolve `resolve_targets()` to
    exactly the `cpu`-only fallback -- not raise, and not silently invent a
    backend. Driving `resolve_targets` (not the private `_soc_targets`
    directly) proves the guard holds all the way out to the public entry
    point `tan model build`/`tan model check` actually call."""
    backends = _backends(tmp_path, [{"type": bad_type, "mac_per_cycle": 256}])
    assert backends == {"cpu"}, (
        f"type={bad_type!r} must degrade to the unresolved-NPU fallback "
        f"(cpu only), not {backends!r}")


def test_a_nonstring_npu_subtype_does_not_crash_and_resolves_the_no_match_fallback(tmp_path):
    """`subtype` feeds the same `_npu_backend` `"drp" in subtype` check and has
    the identical exposure -- tan-cli#965 names it explicitly as needing the
    same pass, not a follow-up. `type=""` (a well-typed but unrecognised
    string) forces evaluation past the short-circuit into the `subtype`
    check; `subtype=7` is chosen because `"drp" in 7` raises `TypeError`
    (ints, unlike dicts, support no `in` protocol at all) -- a dict subtype
    would happen to work by accident (`"drp" in {"a": 1}` just checks keys)
    and so would not prove this guard is doing anything."""
    backends = _backends(
        tmp_path, [{"type": "", "subtype": 7, "mac_per_cycle": 256}])
    assert backends == {"cpu"}


def test_a_string_that_matches_no_known_prefix_is_the_reference_fallback(tmp_path):
    """The pre-existing, already-correct behaviour for a well-typed but
    unrecognised string -- pinned here as the REFERENCE the seven non-string
    shapes above are required to match, per tan-cli#965's own instruction not
    to invent a fourth behaviour."""
    backends = _backends(tmp_path, [{"type": "some-future-npu", "mac_per_cycle": 256}])
    assert backends == {"cpu"}


def test_an_absent_npus_key_is_the_same_fallback(tmp_path):
    """A SoC spec that declares no `npus` at all -- the OTHER pre-existing
    reference point the fix must not diverge from."""
    backends = _backends(tmp_path, [])
    assert backends == {"cpu"}


def test_a_valid_ethos_u_type_still_resolves_the_real_backend(tmp_path):
    """Control: the guard must not turn a VALID string into the fallback too.
    Resolves the actual backend + accel_config, not just "no crash"."""
    specs = resolve_targets(
        _SKU, metadata_root=_metadata_root(
            tmp_path, [{"type": "ethos-u55", "mac_per_cycle": 256}]))
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("ethos_u", "ethos-u55-256") in by
    assert ("cpu", "") in by


# ---------------------------------------------------------------------------
# PR #967 review follow-ups
# ---------------------------------------------------------------------------

# `npus[]` container/element shapes from the review's own reproduction table
# (`targets.py:203`): a whole-`npus` scalar that isn't iterable, a whole-`npus`
# dict (iterating it yields STRING keys, not NPU objects), and single elements
# that are a bare int or a bare str. Every one must degrade to the identical
# `cpu`-only fallback the seven non-string `type` shapes already do -- same
# outcome, one guard earlier in the same loop.
_MALFORMED_NPUS_SHAPES = [
    pytest.param(7, id="npus-int-not-iterable"),
    pytest.param({"a": 1}, id="npus-dict-not-a-list"),
    pytest.param([7], id="element-int"),
    pytest.param(["ethos-u55"], id="element-str"),
]


@pytest.mark.parametrize("bad_npus", _MALFORMED_NPUS_SHAPES)
def test_a_malformed_npus_container_or_element_does_not_crash_and_resolves_the_no_match_fallback(
        tmp_path, bad_npus):
    """`targets.py:203` -- the guard the PR shipped protects `npu.get("type")`'s
    return value but not the `npu.get` CALL, one line above where the review
    found it. Driving `resolve_targets()` (not `_soc_targets` directly)
    proves the guard holds all the way out to the public entry point."""
    backends = _backends(tmp_path, bad_npus)
    assert backends == {"cpu"}, (
        f"npus={bad_npus!r} must degrade to the unresolved-NPU fallback "
        f"(cpu only), not {backends!r}")


def test_an_ethos_u_npu_missing_the_optional_mac_per_cycle_does_not_crash(tmp_path):
    """`targets.py:211` -- `mac_per_cycle` is OPTIONAL in `$defs/npu` (only
    `type` plus `anyOf(gops, tops)` are required), so this doc is
    SCHEMA-VALID -- #964 (read-path schema validation) cannot fix this one,
    the document already passes the schema. Chosen fallback: unmappable, the
    same "no mappable backend for this entry" outcome an unrecognised `type`
    already produces (see module docstring) -- not a partial accel_config
    like "ethos-u55-" that would be a new, undegradeable shape reaching
    `check.py`'s `accel_config.startswith(...)` reader."""
    backends = _backends(tmp_path, [{"type": "ethos-u55", "gops": 100}])
    assert backends == {"cpu"}


def test_an_ethos_u_npu_with_a_nonint_mac_per_cycle_does_not_crash(tmp_path):
    """Same guard, the unvalidated-on-read half: a `mac_per_cycle` present
    but the wrong type (schema says `"type": "integer"`) must not build a
    garbled `accel_config` like "ethos-u55-256px" either."""
    backends = _backends(tmp_path, [{"type": "ethos-u55", "mac_per_cycle": "256px"}])
    assert backends == {"cpu"}


def test_a_valid_mac_per_cycle_still_resolves_the_real_accel_config(tmp_path):
    """Control for the `mac_per_cycle` guard: a well-typed value must still
    resolve the real accel_config, not just avoid crashing."""
    specs = resolve_targets(
        _SKU, metadata_root=_metadata_root(
            tmp_path, [{"type": "ethos-u85", "mac_per_cycle": 512}]))
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("ethos_u", "ethos-u85-512") in by


def _metadata_root_with_raw_host_soc(tmp_path, soc_content) -> object:
    """Like `_metadata_root`, but writes @soc_content VERBATIM as the host
    SoC JSON instead of wrapping it in `{"ref": ..., "npus": ...}` -- for
    exercising a host SoC JSON that doesn't even parse to an object."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        f"sku: {_SKU}\nsilicon: {_SILICON}\n", encoding="utf-8")
    vendor, family, part = _SILICON.split(":")
    soc_dir = root / "socs" / vendor / family
    soc_dir.mkdir(parents=True)
    (soc_dir / f"{part}.json").write_text(json.dumps(soc_content), encoding="utf-8")
    return root


def test_a_bare_array_host_soc_json_raises_a_clean_valueerror_not_an_attributeerror(tmp_path):
    """`targets.py:267` -- the host SoC IS `silicon:`'s one named spec, so
    (unlike the discrete sweep below) there is no "skip it and try the next
    one"; malformed content at a path that exists must raise the same kind
    of clean, named error `resolve_targets` already raises for a malformed
    ref or a missing path, not an uncaught AttributeError two frames into
    `_soc_targets`."""
    root = _metadata_root_with_raw_host_soc(tmp_path, [1, 2, 3])
    with pytest.raises(ValueError, match="expected a JSON object"):
        resolve_targets(_SKU, metadata_root=root)


def test_a_bare_array_soc_json_in_the_discrete_sweep_is_skipped_not_crashed(tmp_path):
    """`targets.py:235` -- unlike the host SoC above, `_discrete_socs`
    ENUMERATES every file under `socs/**`; one malformed file among many must
    be skipped (the `analyze.py` `_load_table` stance), not abort the whole
    sweep. The valid host NPU must still resolve alongside the skip."""
    root = _metadata_root(tmp_path, [{"type": "ethos-u55", "mac_per_cycle": 256}])
    bogus_dir = root / "socs" / "bogus" / "bogus"
    bogus_dir.mkdir(parents=True)
    (bogus_dir / "part.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    specs = resolve_targets(_SKU, metadata_root=root)
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("ethos_u", "ethos-u55-256") in by
    assert ("cpu", "") in by


def test_a_nondict_variants_element_in_the_discrete_sweep_is_skipped_not_crashed(tmp_path):
    """`targets.py:227` (`_discrete_socs`'s `v.get`) -- folded into this PR
    per the review rather than filed separately: same element-type gap as
    the `npus[]` guard above, in the same file. A non-dict `variants[]`
    element must not blank out the WHOLE discrete-SoC match; the valid
    sibling element in the same list must still be found."""
    root = _metadata_root(tmp_path, [{"type": "ethos-u55", "mac_per_cycle": 256}])
    other_dir = root / "socs" / "deepx" / "dx"
    other_dir.mkdir(parents=True)
    (other_dir / "m1.json").write_text(json.dumps({
        "ref": "deepx:dx:m1",
        "variants": [7, {"alp_module_skus": [_SKU]}],
        "npus": [{"type": "dx-m1"}],
    }), encoding="utf-8")
    specs = resolve_targets(_SKU, metadata_root=root)
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("deepx_dxm1", "") in by
    assert ("ethos_u", "ethos-u55-256") in by


def test_a_nonlist_variants_in_the_discrete_sweep_is_skipped_not_crashed(tmp_path):
    """The container half of the same `_discrete_socs` guard: `variants` can
    itself be a non-list scalar, which `for v in variants` would raise
    `TypeError: ... not iterable` on -- the same "npus" vs "npus[] element"
    split the host-side guard makes, one level up in this file's sibling
    function. This SoC contributes no SKUs and is simply not matched, same
    as the host NPU resolving on its own."""
    root = _metadata_root(tmp_path, [{"type": "ethos-u55", "mac_per_cycle": 256}])
    other_dir = root / "socs" / "deepx" / "dx"
    other_dir.mkdir(parents=True)
    (other_dir / "m1.json").write_text(json.dumps({
        "ref": "deepx:dx:m1",
        "variants": 7,
        "npus": [{"type": "dx-m1"}],
    }), encoding="utf-8")
    specs = resolve_targets(_SKU, metadata_root=root)
    by = {(s.backend, s.accel_config) for s in specs}
    assert by == {("cpu", ""), ("ethos_u", "ethos-u55-256")}


def test_a_nonstring_silicon_in_the_som_preset_raises_a_clean_valueerror_not_an_attributeerror(
        tmp_path):
    """`soc_ref.py:41`, reached from `targets.py:259` -- `resolve_soc_path`'s
    own docstring promises None for a `silicon` that is "falsy or not exactly
    3 colon-separated parts"; a non-string is neither, and raised instead.
    `resolve_targets` already turns a None resolution into this same
    ValueError for a malformed-but-string ref (see the ValueError immediately
    below `resolve_soc_path`'s call site) -- proving the fix here, not a new
    behaviour at the caller."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        f"sku: {_SKU}\nsilicon: 7\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed silicon ref"):
        resolve_targets(_SKU, metadata_root=root)


@pytest.mark.parametrize("bad_silicon", [
    pytest.param(7, id="int"),
    pytest.param(["a", "b", "c"], id="list"),
    pytest.param(True, id="bool"),
])
def test_resolve_soc_path_returns_none_for_a_nonstring_silicon_per_its_own_docstring(
        tmp_path, bad_silicon):
    """Direct unit coverage of the shared leaf itself (`tan.soc_ref`, also
    re-exported by `tan.planner.som_metadata` -- every existing caller
    already treats a None return as "unresolved" through its own error
    message, so fixing the leaf once fixes every caller identically)."""
    assert resolve_soc_path(bad_silicon, tmp_path) is None
