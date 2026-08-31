# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1025: `tan/planner/template.py::_load_som_doc` had no
`isinstance(doc, dict)` guard between `yaml.safe_load(...)` and the bare
`.get(...)` every one of its three callers (`_default_preset_for_sku`,
`_derive_core_renames`, `_core_board`) performs on the result.

Measured on the unguarded code, verbatim (the issue's own repro):

    _load_som_doc(<bare-list preset>)  -> ['one', 'two']
    _default_preset_for_sku(...)       -> AttributeError: 'list' object
                                           has no attribute 'get'

Mirrors `tests/model/test_targets_malformed_preset.py` (tan-cli#1010's fix
to the sibling `preset["silicon"]` read in `tan/model/targets.py`) -- same
fixture shape, same assertion style, applied to `_load_som_doc`'s shared
read instead.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same
requirement as `test_find_template_by_cores.py` -- even though these cases
never touch that checkout's own metadata content (a synthetic
`metadata_root` is passed explicitly to every call under test).
"""
from __future__ import annotations

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


_SKU = "E1M-FAKE1025"


def _metadata_root_with_raw_preset(tmp_path, preset_yaml_text: str):
    """A metadata/ tree whose SoM preset is @preset_yaml_text VERBATIM --
    for exercising a preset document that doesn't even parse to a mapping."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        preset_yaml_text, encoding="utf-8")
    return root


def test_a_bare_list_som_doc_raises_a_curated_error_not_an_attributeerror(tmp_path):
    """The issue's own repro shape: a SoM preset YAML that parses to a bare
    list (legal YAML, illegal `e1m-preset-*.schema.json`) must raise
    `TemplateError`, not an uncaught `AttributeError: 'list' object has no
    attribute 'get'` from `_default_preset_for_sku`'s bare
    `_load_som_doc(...).get("default_board")`."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._default_preset_for_sku(_SKU, root)


def test_a_bare_scalar_som_doc_raises_a_curated_error_not_an_attributeerror(tmp_path):
    """Same guard, the other bare shape: a preset YAML that parses to a
    bare scalar string raises `AttributeError: 'str' object has no
    attribute 'get'` on the unguarded code."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(tmp_path, "just a scalar string\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._default_preset_for_sku(_SKU, root)


def test_the_error_names_the_offending_path_and_the_actual_type(tmp_path):
    """Diagnostic, not generic -- names the preset path (so a caller
    juggling many SKUs knows which file) and the real Python type,
    mirroring `resolve_targets`'s `host_soc`/`preset` guard message."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    preset_path = root / "e1m_modules" / f"{_SKU}.yaml"
    with pytest.raises(tmpl.TemplateError) as exc_info:
        tmpl._default_preset_for_sku(_SKU, root)
    assert "got list" in str(exc_info.value)
    assert str(preset_path) in str(exc_info.value)


def test_derive_core_renames_also_raises_not_a_bare_attributeerror(tmp_path):
    """`_derive_core_renames` is the second of `_load_som_doc`'s three
    callers (`topology = _load_som_doc(...).get("topology") or {}`) -- the
    guard lives in the shared helper, so it must cover this call site too,
    not just the one the issue names."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._derive_core_renames(["m55_hp"], _SKU, root)


def test_core_board_also_raises_not_a_bare_attributeerror(tmp_path):
    """`_core_board` is the third caller (`_load_som_doc(...).get("topology")
    or {}`, same shape as `_derive_core_renames`) -- also covered by the
    shared guard."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._core_board(_SKU, "m55_hp", root)


def test_a_well_formed_mapping_preset_still_resolves_normally(tmp_path):
    """Vacuity check: the guard must not fire on a legitimate mapping --
    confirms the new `isinstance` check is not accidentally over-broad."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_preset(
        tmp_path, "default_board: E1M-EVK\ntopology:\n  m55_hp: {}\n")
    assert tmpl._default_preset_for_sku(_SKU, root) == "e1m-evk"
