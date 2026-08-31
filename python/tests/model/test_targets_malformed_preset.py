# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1010: `resolve_targets()`'s `preset = yaml.safe_load(...)` read had
no `isinstance(preset, dict)` guard before the very next line bare-subscripted
it (`preset["silicon"]`) -- twenty lines before the SAME function applies
exactly that guard to `host_soc` (`targets.py:339-350`) and raises a curated
`ValueError` instead of letting a raw `TypeError` escape.

Measured on the unguarded code, verbatim (the issue's own repro table):

    bare list preset   -> TypeError: list indices must be integers or
                           slices, not str
    bare scalar preset -> TypeError: string indices must be integers,
                           not 'str'

Mirrors `test_targets_nonstring_npu_type.py`'s own
`test_a_bare_array_host_soc_json_raises_a_clean_valueerror_not_an_attributeerror`
for the `host_soc` half of this exact function -- same fixture shape, same
assertion style, applied to the sibling `preset` read the #965/#964 sweep
left open (the issue's own scope note: `silicon:` IS schema-required, so this
is #964's read-path-validation remit, not #983's optional-field remit)."""
import pytest

from tan.model.targets import resolve_targets

_SKU = "E1M-FAKE1010"


def _metadata_root_with_raw_preset(tmp_path, preset_yaml_text: str) -> object:
    """A metadata/ tree whose SoM preset is @preset_yaml_text VERBATIM --
    for exercising a preset document that doesn't even parse to a mapping."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        preset_yaml_text, encoding="utf-8")
    return root


def test_a_bare_list_preset_yaml_raises_a_clean_valueerror_not_a_typeerror(tmp_path):
    """`targets.py:311` -- a preset YAML that parses to a bare list (legal
    YAML, illegal `som-preset-v1.schema.json`) must raise the same kind of
    clean, named error `resolve_targets` already raises for a malformed
    silicon ref or a malformed host SoC spec, not an uncaught
    `TypeError: list indices must be integers or slices, not str` from the
    bare `preset["silicon"]` subscript at `targets.py:325` (the guard sits
    between the two lines at this head)."""
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        resolve_targets(_SKU, metadata_root=root)


def test_a_bare_scalar_preset_yaml_raises_a_clean_valueerror_not_a_typeerror(tmp_path):
    """Same guard, the other shape from the issue's own repro table: a
    preset YAML that parses to a bare scalar string raised
    `TypeError: string indices must be integers, not 'str'` on the
    unguarded bare subscript."""
    root = _metadata_root_with_raw_preset(tmp_path, "just a scalar string\n")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        resolve_targets(_SKU, metadata_root=root)


def test_the_valueerror_names_the_offending_path_and_the_actual_type(tmp_path):
    """The message must be diagnostic, not generic -- it names the preset
    path (so a caller juggling many SKUs knows which file) and the real
    Python type (mirroring `host_soc`'s own `type(host_soc).__name__}`), not
    just "malformed"."""
    root = _metadata_root_with_raw_preset(tmp_path, "- one\n- two\n")
    preset_path = root / "e1m_modules" / f"{_SKU}.yaml"
    with pytest.raises(ValueError, match=r"malformed SoM preset.*got list"):
        resolve_targets(_SKU, metadata_root=root)
    # Confirm the exact path named is the preset that was actually read --
    # not a stale/hardcoded string a copy-paste of the host_soc guard could
    # have left behind.
    with pytest.raises(ValueError) as exc_info:
        resolve_targets(_SKU, metadata_root=root)
    assert str(preset_path) in str(exc_info.value)


def test_a_mapping_preset_missing_silicon_raises_the_curated_ref_error_not_a_keyerror(tmp_path):
    """tan-cli#1018 review MAJOR: the guard above only fixes the container
    shape -- `preset["silicon"]` was still a bare subscript, so a preset
    that IS a mapping but simply omits the schema-required `silicon:` key
    raised raw `KeyError: 'silicon'` at `targets.py:325`, thirteen lines
    below the guard, on the very expression the issue's title names. This
    must instead fall into the *existing* curated `resolve_soc_path()` path
    that already handles a malformed silicon ref -- a missing key is exactly
    as malformed as a wrong-shaped one."""
    root = _metadata_root_with_raw_preset(tmp_path, "not_silicon: whatever\n")
    with pytest.raises(ValueError, match="malformed silicon ref None"):
        resolve_targets(_SKU, metadata_root=root)
