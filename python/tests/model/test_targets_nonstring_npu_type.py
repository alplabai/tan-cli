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
behaviour; this file rules out both)."""
import json

import pytest

from tan.model.targets import resolve_targets

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
