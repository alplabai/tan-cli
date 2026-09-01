# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1052: `tan/planner/template.py::render_to_envelope` read the
catalog template's OWN `examples/<...>/board.yaml` through four bare
`.get(...)`/`list(...)` calls with no `isinstance` guard at any of them --
the FIFTH sibling of the malformed-YAML family tan-cli#1025 -> #1034 ->
#1037/#1048 had already swept twice, one document over each time.

All four are reachable from ONE command. Re-derived on the pre-fix tree
(`--emit scaffold --template peripheral --sku E1M-V2N101`, against a
`metadata/` + `examples/peripheral-io/gpio-button-led/` copy whose only
edit is the field named):

    <the whole doc> = "- one\\n- two\\n"
        -> AttributeError: 'list' object has no attribute 'get'   template.py:1542
    <the whole doc> = "just-a-string\\n"
        -> AttributeError: 'str' object has no attribute 'get'    template.py:1542
    cores: 3
        -> AttributeError: 'int' object has no attribute 'keys'   template.py:1542
    cores: [m55_hp]
        -> AttributeError: 'list' object has no attribute 'keys'  template.py:1542
    som: 3
        -> AttributeError: 'int' object has no attribute 'get'    template.py:1543
    pins: 3
        -> TypeError: 'int' object is not iterable                template.py:1551
    pins: true
        -> TypeError: 'bool' object is not iterable               template.py:1551

`pins:` (`:1551`) is the site the issue names -- `list(example_doc.get("pins")
or [])`, the same `or []`-then-iterate shape PR #1048 guarded for
`e1m_routes.<section>`. The other three are its siblings in the same
function, found by sweeping it rather than fixing only the named line: every
previous round of this family was filed because the round before it guarded
one site and left a sibling.

`preset:` is the fifth read. It never raised a RAW exception -- a non-string
reaches `metadata/boards/<preset>.yaml` as a path component -- but the
curated message it produced was untrue: measured pre-fix, `preset: [a]` gave
`no metadata/boards/['a'].yaml for board ['a']`, which reads as a missing
file rather than a malformed field. Guarded here too, so the function has no
unguarded `board.yaml` read left.

NOT STRICTER THAN THE SCHEMA. `metadata/schemas/board.schema.json` gives
`pins:` `"type": "array"` with NO `minItems`, so `pins: []` is legal and must
still render; so must an absent or explicitly-`null` `pins:`, an empty
`cores: {}`/`som: {}`, and an absent `preset:` (optional -- an inline board
definition, or a template with no pins to re-derive). Asserted below.

THE SHARED HELPERS. The guards are not a sixth hand-written `isinstance`
block: `_require_mapping_doc` (outer document) and `_require_field` (one
level in) now serve ALL THREE documents this module reads -- the SoM preset
(`metadata/e1m_modules/<sku>.yaml`, tan-cli#1025/#1034), the board metadata
(`metadata/boards/<board>.yaml`, tan-cli#1037/#1048) and the template example
`board.yaml` (this issue) -- across nine call sites, with the message
register of the first two preserved byte for byte. The proof they are
genuinely shared is that neutering EITHER helper reds this file AND
`test_load_som_doc_malformed_preset.py` AND
`test_board_route_entries_malformed_board.py`; the register-consistency
tests at the bottom of this file assert that sharing directly, so a future
hand-rolled copy of the block is a red rather than a silent divergence.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same requirement
as `test_load_som_doc_malformed_preset.py` / `test_board_route_entries_
malformed_board.py` -- even though every case here passes a synthetic
`base_dir`/`metadata_root`/`catalog_path` explicitly and never reads that
checkout's own content.
"""
from __future__ import annotations

import json

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


_TEMPLATE = "fake1052"
_SKU = "E1M-FAKE1052"
_EXAMPLE = "examples/peripheral-io/fake1052"

_GOOD_BOARD_YAML = """\
som:
  sku: E1M-FAKE1052

preset: fake-board

cores:
  m33_sm:
    app: ./src
"""


def _tree(tmp_path, board_yaml_text: str):
    """A synthetic (catalog, base_dir, metadata_root) triple whose example
    `board.yaml` is @board_yaml_text VERBATIM -- so a document that does not
    even parse to a mapping can be exercised, not just a mapping with a bad
    field.

    Rooted in its own `render/` subdirectory so a test may ALSO build a
    second, unrelated `metadata/` tree under the same `tmp_path` (the
    register-consistency cases at the bottom do exactly that)."""
    tmp_path = tmp_path / "render"
    base = tmp_path / "sdk"
    example = base / _EXAMPLE
    example.mkdir(parents=True)
    (example / "board.yaml").write_text(board_yaml_text, encoding="utf-8")

    catalog = tmp_path / "catalog-v1.json"
    catalog.write_text(json.dumps({"templates": [{
        "id": _TEMPLATE,
        "example": _EXAMPLE,
        "supported": {"som_skus": [_SKU]},
        "files": {"user_owned": ["board.yaml"]},
        "cores": [],
    }]}), encoding="utf-8")

    metadata = tmp_path / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "e1m_modules" / f"{_SKU}.yaml").write_text(
        "default_board: FAKE-BOARD\n"
        "topology:\n"
        "  m33_sm:\n"
        "    board: fake/soc/m33\n",
        encoding="utf-8")
    return catalog, base, metadata


def _render(tmp_path, board_yaml_text: str):
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path, board_yaml_text)
    return m.render_to_envelope(
        _TEMPLATE, _SKU,
        catalog_path=catalog, base_dir=base, metadata_root=metadata)


def _raises(tmp_path, board_yaml_text: str):
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        _render(tmp_path, board_yaml_text)
    return str(excinfo.value)


def _swap(field: str, value: str) -> str:
    """`_GOOD_BOARD_YAML` with @field's whole top-level block dropped, and
    `<field>: <value>` appended -- or just dropped, when @value is `None`
    (the "field absent entirely" cases)."""
    out, skipping = [], False
    for line in _GOOD_BOARD_YAML.splitlines(keepends=True):
        if line.startswith(f"{field}:"):
            skipping = True
            continue
        if skipping:
            if line.strip() and not line[0].isspace():
                skipping = False
            else:
                continue
        out.append(line)
    text = "".join(out)
    return text if value is None else text + f"{field}: {value}\n"


# ---------------------------------------------------------------------
# The control: the well-formed document still renders.
# ---------------------------------------------------------------------

def test_the_wellformed_example_board_yaml_still_renders(tmp_path):
    """Every refusal below has to be a refusal of a MALFORMED document,
    not of the shape every catalog template actually ships."""
    out = _render(tmp_path, _GOOD_BOARD_YAML)
    assert [rel for rel, _ in out] == ["board.yaml"]


# ---------------------------------------------------------------------
# Level 1: the outer example board.yaml document.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("doc", "typename"),
    [("- one\n- two\n", "list"), ("just-a-string\n", "str"), ("3\n", "int")],
)
def test_a_nonmapping_example_board_yaml_raises_a_curated_error(
        tmp_path, doc, typename):
    """Pre-fix this reached `example_doc.get("cores")` bare:
    `AttributeError: 'list' object has no attribute 'get'`. It must now be
    a `TemplateError` naming the FILE, what was expected, and the ACTUAL
    type -- the register `tan/model/targets.py:312-323` set."""
    msg = _raises(tmp_path, doc)
    assert "board.yaml" in msg
    assert "expected a YAML mapping" in msg
    assert f"got {typename}" in msg


def test_the_nonmapping_example_doc_error_is_not_an_attributeerror(tmp_path):
    """The defect was the RAW exception class escaping to a CLI user, not
    just the wording: `_emit_scaffold` catches `TemplateError` and prints
    one line, and lets anything else escape as a traceback."""
    m = _tmpl()
    with pytest.raises(m.TemplateError):
        _render(tmp_path, "- one\n- two\n")


# ---------------------------------------------------------------------
# Level 2: `cores:` -- a mapping whose `cores:` is not one.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "typename"), [("3", "int"), ("[m33_sm]", "list"), ("'x'", "str")])
def test_a_nonmapping_cores_raises_a_curated_error(tmp_path, value, typename):
    """Pre-fix: `AttributeError: 'int' object has no attribute 'keys'` from
    `list((example_doc.get("cores") or {}).keys())`."""
    msg = _raises(tmp_path, _swap("cores", value))
    assert "board.yaml" in msg
    assert f"cores must be a mapping, got {typename}" in msg


# ---------------------------------------------------------------------
# Level 3: `som:` -- a mapping whose `som:` is not one.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "typename"), [("3", "int"), ("[E1M-FAKE1052]", "list")])
def test_a_nonmapping_som_raises_a_curated_error(tmp_path, value, typename):
    """Pre-fix: `AttributeError: 'int' object has no attribute 'get'` from
    `(example_doc.get("som") or {}).get("sku", "")`."""
    msg = _raises(tmp_path, _swap("som", value))
    assert "board.yaml" in msg
    assert f"som must be a mapping, got {typename}" in msg


# ---------------------------------------------------------------------
# Level 4: `pins:` -- THE site tan-cli#1052 names (`template.py:1551`).
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "typename"),
    [("3", "int"), ("true", "bool"), ("'E1M_GPIO_IO4'", "str"),
     ("{e1m: E1M_GPIO_IO4}", "dict")],
)
def test_a_nonlist_pins_raises_a_curated_error_not_a_typeerror(
        tmp_path, value, typename):
    """The issue's own measurement: `pins: 3` reached
    `list(example_doc.get("pins") or [])` and raised
    `TypeError: 'int' object is not iterable` at `template.py:1551`.

    `str`/`dict` are the quieter half of the same defect: both ARE
    iterable, so they never raised at all -- `pins: E1M_GPIO_IO4` iterated
    the string CHARACTER BY CHARACTER and `pins: {e1m: ...}` iterated its
    KEYS, feeding `_derive_pin_renames` garbage instead of refusing. The
    guard closes both halves with one rule."""
    msg = _raises(tmp_path, _swap("pins", value))
    assert "board.yaml" in msg
    assert f"pins must be a list, got {typename}" in msg


# ---------------------------------------------------------------------
# Level 5: `preset:` -- curated pre-fix, but untrue.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "typename"), [("[a]", "list"), ("3", "int"), ("{a: b}", "dict")])
def test_a_nonstring_preset_names_the_field_not_a_missing_file(
        tmp_path, value, typename):
    """Measured pre-fix, `preset: [a]` produced `no metadata/boards/
    ['a'].yaml for board ['a']` -- curated, but describing a missing file
    instead of the malformed field that produced the path. Same shape
    `default_board:` already had one document over
    (`_default_preset_for_sku`)."""
    msg = _raises(tmp_path, _swap("preset", value))
    assert "board.yaml" in msg
    assert f"preset must be a string, got {typename}" in msg
    assert "no metadata/boards/" not in msg


# ---------------------------------------------------------------------
# Not stricter than the schema.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "doc"),
    [
        # `pins:` is `"type": "array"` with NO `minItems` -- an empty list
        # is a legal board.yaml, and this guard must not invent a refusal.
        ("pins: []", _swap("pins", "[]")),
        # An explicit YAML null, and the field simply absent: both are the
        # falsy-but-legal shapes the `or []` normalises, and both predate
        # this fix as working renders.
        ("pins: null", _swap("pins", "")),
        ("no pins: key at all", _swap("pins", None)),
        # `cores: {}` is rejected by the schema's `minProperties: 1`, not by
        # this guard -- an empty MAPPING is still a mapping, and inventing a
        # second refusal here would be exactly the "stricter than the
        # schema" the issue rules out.
        ("cores: {}", _swap("cores", "{}")),
    ],
)
def test_a_falsy_but_legal_value_still_renders(tmp_path, label, doc):
    """The acceptance bar tan-cli#1052 set in as many words: the guard must
    not be stricter than `metadata/schemas/board.schema.json`. Each case
    here renders, it does not raise."""
    out = _render(tmp_path, doc)
    assert [rel for rel, _ in out] == ["board.yaml"], label


@pytest.mark.parametrize(
    ("label", "doc", "older_rule"),
    [
        # `som: {}` IS a mapping, so this guard passes it; the render then
        # fails one field in, on `som.sku:` being absent -- a pre-existing
        # refusal, measured verbatim.
        ("som: {}", _swap("som", "{}"),
         "no metadata/e1m_modules/.yaml for sku ''"),
        # An absent `preset:` likewise passes this guard (the field is
        # optional) and is refused by `_substitute_board_yaml_sku`'s
        # pre-existing "exactly one top-level `preset:` line" rule.
        ("no preset: key at all", _swap("preset", None),
         "to substitute (found 0)"),
    ],
)
def test_an_empty_container_is_refused_by_the_older_rule_not_this_guard(
        tmp_path, label, doc, older_rule):
    """Two empty-but-mapping-shaped documents DO still fail the render --
    but on rules that predate this fix and are not type checks. Asserted
    explicitly rather than folded into the case above, because "it raises,
    therefore the guard is fine" would hide a guard that had quietly become
    stricter than the schema: what matters is that the refusal is NOT this
    fix's register."""
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        _render(tmp_path, doc)
    msg = str(excinfo.value)
    assert older_rule in msg, label
    assert "must be a mapping" not in msg
    assert "must be a string" not in msg
    assert "must be a list" not in msg


# ---------------------------------------------------------------------
# The helpers are genuinely shared -- one register, three documents.
# ---------------------------------------------------------------------

def test_all_three_documents_share_one_outer_mapping_message(tmp_path):
    """`_require_mapping_doc` serves the SoM preset, the board metadata AND
    the template example board.yaml. If a future round hand-rolls a fourth
    `isinstance` block instead of calling it, this reds -- which is the
    whole point of extracting it (tan-cli#1052: the four rounds before this
    one each re-wrote the same block)."""
    m = _tmpl()
    metadata = tmp_path / "docs" / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "boards").mkdir(parents=True)
    (metadata / "e1m_modules" / "E1M-BARE.yaml").write_text(
        "- one\n- two\n", encoding="utf-8")
    (metadata / "boards" / "bare.yaml").write_text(
        "- one\n- two\n", encoding="utf-8")

    messages = []
    for call in (
        lambda: m._load_som_doc("E1M-BARE", metadata),
        lambda: m._board_route_entries("bare", metadata),
        lambda: _render(tmp_path, "- one\n- two\n"),
    ):
        with pytest.raises(m.TemplateError) as excinfo:
            call()
        messages.append(str(excinfo.value))

    for msg in messages:
        assert msg.startswith("malformed ")
        assert "expected a YAML mapping, got list" in msg


def test_all_three_documents_share_one_nested_field_message(tmp_path):
    """`_require_field` likewise: `topology:` (SoM preset),
    `e1m_routes:` (board metadata) and `cores:` (template example
    board.yaml) all render `<file> <field> must be a mapping, got <type>`.
    Byte-identical to the register tan-cli#1034/#1048 landed -- this fix
    de-duplicated it, it did not re-word it."""
    m = _tmpl()
    metadata = tmp_path / "docs" / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "boards").mkdir(parents=True)
    (metadata / "e1m_modules" / "E1M-BADTOPO.yaml").write_text(
        "default_board: FAKE-BOARD\ntopology: [m33_sm]\n", encoding="utf-8")
    (metadata / "boards" / "badroutes.yaml").write_text(
        "e1m_routes: [gpio]\n", encoding="utf-8")

    with pytest.raises(m.TemplateError) as topo:
        m._topology_for_sku("E1M-BADTOPO", metadata)
    with pytest.raises(m.TemplateError) as routes:
        m._board_route_entries("badroutes", metadata)
    with pytest.raises(m.TemplateError) as cores:
        _render(tmp_path, _swap("cores", "[m33_sm]"))

    assert str(topo.value).endswith("topology must be a mapping, got list")
    assert str(routes.value).endswith("e1m_routes must be a mapping, got list")
    assert str(cores.value).endswith("cores must be a mapping, got list")


def test_the_shape_noun_table_covers_every_kind_the_module_guards(tmp_path):
    """`_SHAPE_NOUN` is the helper's only lookup: a kind missing from it
    would raise `KeyError` from inside the guard -- a raw exception, the
    exact defect class this family exists to close. All three kinds the
    module passes today are present."""
    m = _tmpl()
    assert m._SHAPE_NOUN[dict] == "a mapping"
    assert m._SHAPE_NOUN[list] == "a list"
    assert m._SHAPE_NOUN[str] == "a string"
