# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1077: `tan/planner/template.py` decoded `metadata/templates/
catalog-v1.json` through an unguarded `json.loads` and then bare-subscripted
the result at sixteen sites.

The SIXTH round of one family (tan-cli#1025 -> #1034 -> #1037/#1048 -> #1052
-> this), and the first on a document that is not YAML. That is what put it
in its own issue rather than folded into #1052: the three documents PR #1073
guarded are `yaml.safe_load`-ed and read with `.get(...)`, so a wrong-SHAPE
document fails as `AttributeError`/`TypeError`; the catalog is `json.loads`-ed
and read by BARE SUBSCRIPT, so a wrong-KEYED document fails as `KeyError`.
Same escape route either way -- `cli._emit_scaffold` catches `TemplateError`
and lets everything else out as a traceback -- but a different check, which
neither `_require_mapping_doc` (whole document) nor `_require_field` (value
already in hand) could express.

Re-derived on `dev@0ca54fbf` before fixing, not taken on trust. Verbatim, with
the frame `traceback` reports (harness: the synthetic catalog + example tree
below, one edit per row):

    catalog = "[1, 2]"          AttributeError: 'list' object has no
                                  attribute 'get'              template.py:123
    catalog = "{"               json.decoder.JSONDecodeError: Expecting
                                  property name ...            template.py:119
    templates: 3                TypeError: 'int' object is not
                                  iterable                     template.py:123
    a record with no `id`       KeyError: 'id'                 template.py:124
    a record that is not a
      mapping                   TypeError: 'int' object is not
                                  subscriptable                template.py:124
    cores: [{os: zephyr}]       KeyError: 'id'                 template.py:166
    no `supported` key          KeyError: 'supported'          template.py:1527
    no `files` key              KeyError: 'files'              template.py:80
    files.user_owned: [3, 'a']  TypeError: '<' not supported between
                                  instances of 'str' and 'int'  template.py:80
    no `example` key            KeyError: 'example'            template.py:1539
    parameters: [{name: n}]     KeyError: 'default'            template.py:239
    substitute: {literal: x}    KeyError: 'file'               template.py:266
    cores: [{... dir: 3}]       TypeError: argument should be a str or an
                                  os.PathLike object          template.py:1155

THE GUARD IS ONE NEW HELPER ON THE EXISTING REGISTER, not a third register.
`_require_key(mapping, key, kind=None, *, doc, field)` adds exactly the check
that was missing -- the key is present -- and DELEGATES both type checks (the
container is a mapping; the value is @kind) to `_require_field`, whose message
tan-cli#1034/#1048/#1052 already share across the module's three YAML
documents. `_require_mapping_doc` gained one keyword, `noun`, so the catalog's
outer message can say "a JSON object" (true of a JSON file) while the three
YAML callers keep "a YAML mapping" byte for byte.

The proof the extension is genuinely SHARED and not a fourth hand-rolled copy
is that neutering `_require_field` reds THIS file alongside
`test_load_som_doc_malformed_preset.py`,
`test_board_route_entries_malformed_board.py` and
`test_render_to_envelope_malformed_example_board.py` -- four files, one
mutant. The register-consistency tests at the bottom assert that directly.

NOT STRICTER THAN `metadata/schemas/template-catalog-v1.schema.json`. Every
key required here is `required` there: `id`, `example`, `supported`,
`supported.som_skus`, `files`, `files.user_owned`, `parameters`, `cores`, and
`cores[].id`/`cores[].os`, plus `$defs/parameter`'s `name`/`type`/`default`.
`default` is required by PRESENCE only -- the schema declares no type for it.
`description` is `required` by the schema and deliberately NOT checked here:
this module never subscripts it, and guarding a key nobody reads would be the
stricter-than-the-schema the issue rules out. An ABSENT `templates:`/`cores:`/
`parameters:` still degrades to `[]` exactly as the pre-existing
`.get(..., [])` did, so no document that rendered before stops rendering --
proved directly against the real shipped catalog below.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same requirement
as the three sibling files above -- even though every synthetic case here
passes its own `catalog_path`/`base_dir`/`metadata_root` and never reads that
checkout's content.
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


_TEMPLATE = "fake1077"
_SKU = "E1M-FAKE1077"
_EXAMPLE = "examples/peripheral-io/fake1077"

_BOARD_YAML = """\
som:
  sku: E1M-FAKE1077

preset: fake-board

cores:
  m33_sm:
    app: ./src
"""


def _record(**overrides):
    """A catalog record that renders, with @overrides applied. A key whose
    value is the sentinel `...` is DELETED instead of replaced -- the
    "required key absent entirely" cases."""
    rec = {
        "id": _TEMPLATE,
        "example": _EXAMPLE,
        "supported": {"som_skus": [_SKU]},
        "files": {"user_owned": ["board.yaml"]},
        "cores": [],
    }
    for key, value in overrides.items():
        if value is ...:
            rec.pop(key, None)
        else:
            rec[key] = value
    return rec


def _tree(tmp_path, doc):
    """A synthetic (catalog, base_dir, metadata_root) triple whose catalog
    file contains @doc -- serialised with `json.dumps` when it is a Python
    object, or written VERBATIM when it is a `str`, so a catalog that is not
    even valid JSON can be exercised, not only one with a bad key."""
    tmp_path = tmp_path / "render"
    base = tmp_path / "sdk"
    example = base / _EXAMPLE
    example.mkdir(parents=True)
    (example / "board.yaml").write_text(_BOARD_YAML, encoding="utf-8")

    catalog = tmp_path / "catalog-v1.json"
    catalog.write_text(
        doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    metadata = tmp_path / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "e1m_modules" / f"{_SKU}.yaml").write_text(
        "default_board: FAKE-BOARD\n"
        "topology:\n"
        "  m33_sm:\n"
        "    board: fake/soc/m33\n",
        encoding="utf-8")
    return catalog, base, metadata


def _render(tmp_path, doc, template_id=_TEMPLATE, params=None):
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path, doc)
    return m.render_to_envelope(
        template_id, _SKU, params,
        catalog_path=catalog, base_dir=base, metadata_root=metadata)


def _raises(tmp_path, doc, template_id=_TEMPLATE, params=None):
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        _render(tmp_path, doc, template_id, params)
    return str(excinfo.value)


def _catalog(*records, **top):
    out = {"templates": list(records)}
    out.update(top)
    return out


# ---------------------------------------------------------------------
# The control: the well-formed catalog still renders.
# ---------------------------------------------------------------------

def test_the_wellformed_catalog_still_renders(tmp_path):
    """Every refusal below has to be a refusal of a MALFORMED catalog, not
    of the shape the shipped one actually has."""
    out = _render(tmp_path, _catalog(_record()))
    assert [rel for rel, _ in out] == ["board.yaml"]


# ---------------------------------------------------------------------
# Site :119 -- the outer `json.loads`.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("doc", "typename"),
    [("[1, 2]", "list"), ('"just-a-string"', "str"), ("3", "int"),
     ("null", "NoneType")],
)
def test_a_nonobject_catalog_raises_a_curated_error(tmp_path, doc, typename):
    """Pre-fix this reached `doc.get("templates", [])` bare:
    `AttributeError: 'list' object has no attribute 'get'` at
    `template.py:123`. Now a `TemplateError` naming the FILE, what was
    expected, and the ACTUAL type."""
    msg = _raises(tmp_path, doc)
    assert "catalog-v1.json" in msg
    assert "expected a JSON object" in msg
    assert f"got {typename}" in msg


def test_the_outer_catalog_error_says_json_object_not_yaml_mapping(tmp_path):
    """`_require_mapping_doc`'s `noun` keyword, and the only reason it
    exists: this document is JSON, and the register's default wording
    ("a YAML mapping") would be a curated message that is not true. The
    three YAML documents keep the default -- asserted at the bottom."""
    msg = _raises(tmp_path, "[1, 2]")
    assert "expected a JSON object, got list" in msg
    assert "YAML" not in msg


@pytest.mark.parametrize("doc", ["{", "{'templates': []}", ""])
def test_a_catalog_that_is_not_valid_json_is_curated_not_a_decodeerror(
        tmp_path, doc):
    """The other half of `:119`. `json.JSONDecodeError` is a `ValueError`,
    not a `TemplateError`, so `cli._emit_scaffold`'s `except TemplateError`
    never caught it and a half-written catalog in a developer's checkout
    reached the user as a traceback."""
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        _render(tmp_path, doc)
    msg = str(excinfo.value)
    assert "catalog-v1.json" in msg
    assert "not valid JSON" in msg


def test_load_catalog_itself_refuses_a_nonobject_document(tmp_path):
    """M15 in this PR's mutant table stayed GREEN, and this is the answer:
    reverting ONLY `load_catalog`'s `_require_mapping_doc` left every
    render-path case passing, because `_catalog_templates` re-guards the
    same document one call later, inside both selectors.

    That redundancy is deliberate -- `load_catalog` is PUBLIC, and two
    callers `.get("templates", ...)` its return value WITHOUT going through
    a selector (`tan/planner/cli.py:182` hands it straight to
    `find_template_by_cores`, and `tests/gates/test_example_catalog_cores_
    selector_agrees_with_planner.py::_known_topologies` walks it itself) --
    but an unpinned guard is not a guard. This asserts it at its own call
    site, so M15 reds like every other mutant."""
    m = _tmpl()
    catalog, _base, _metadata = _tree(tmp_path, "[1, 2]")
    with pytest.raises(m.TemplateError) as excinfo:
        m.load_catalog(catalog)
    assert "catalog-v1.json" in str(excinfo.value)
    assert "expected a JSON object, got list" in str(excinfo.value)


def test_the_json_decode_error_is_not_a_valueerror_subclass_escape(tmp_path):
    """`json.JSONDecodeError` IS a `ValueError`; the point is that what
    escapes is a `TemplateError`, the one class the CLI catches."""
    m = _tmpl()
    with pytest.raises(m.TemplateError):
        _render(tmp_path, "{")


# ---------------------------------------------------------------------
# Sites :123 :126 :168 :173 -- `doc.get("templates", [])`.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ("x", "str"), ({"a": 1}, "dict")])
def test_a_nonlist_templates_raises_a_curated_error(tmp_path, value, typename):
    """Pre-fix: `TypeError: 'int' object is not iterable` from iterating
    `doc.get("templates", [])` at `template.py:123`."""
    msg = _raises(tmp_path, {"templates": value})
    assert "catalog-v1.json" in msg
    assert f"templates must be a list, got {typename}" in msg


def test_an_absent_templates_key_still_degrades_to_not_found(tmp_path):
    """NOT STRICTER THAN THE SCHEMA, and not a behaviour change either:
    `templates:` absent used to hit the `.get(..., [])` default and produce
    `TemplateNotFoundError (known: )`. It still does -- this guard refuses
    a PRESENT non-list only."""
    m = _tmpl()
    with pytest.raises(m.TemplateNotFoundError) as excinfo:
        _render(tmp_path, {})
    assert str(excinfo.value) == (
        "no template 'fake1077' in catalog (known: )")


# ---------------------------------------------------------------------
# Sites :123-:126 :173 -- `rec["id"]` in both selectors.
# ---------------------------------------------------------------------

def test_a_record_with_no_id_raises_a_curated_error(tmp_path):
    """Pre-fix: `KeyError: 'id'` at `template.py:124` -- the shape that
    put this issue in its own round. `id` is `required` on every record in
    `template-catalog-v1.schema.json`."""
    msg = _raises(tmp_path, _catalog(_record(id=...)))
    assert "catalog-v1.json" in msg
    assert "templates[0] is missing required key 'id'" in msg


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), (["a"], "list"), ({"a": 1}, "dict")])
def test_a_nonstring_record_id_raises_a_curated_error(
        tmp_path, value, typename):
    """A record whose `id:` is present but not a string compared unequal to
    every requested id and fell out as a MISLEADING `TemplateNotFoundError`
    listing it among the "known" ids, or blew up in `sorted()` on a mixed
    list. The schema gives `id` `"type": "string"` with a pattern."""
    msg = _raises(tmp_path, _catalog(_record(id=value)))
    assert f"templates[0].id must be a string, got {typename}" in msg


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), (["a"], "list"), ("x", "str")])
def test_a_templates_entry_that_is_not_a_mapping_raises(
        tmp_path, value, typename):
    """Pre-fix: `TypeError: 'int' object is not subscriptable` from
    `rec["id"]`. The container check `_require_key` delegates to
    `_require_field` before `key in mapping` can mean anything."""
    msg = _raises(tmp_path, _catalog(value))
    assert f"templates[0] must be a mapping, got {typename}" in msg


def test_the_not_found_message_is_byte_identical_to_before(tmp_path):
    """The guard de-duplicated the two `rec["id"]` walks into one; the
    message it feeds must not have moved. Measured on `dev@0ca54fbf` with
    this exact catalog."""
    m = _tmpl()
    doc = _catalog(_record(id="alpha"), _record(id="zulu"), _record(id="mike"))
    with pytest.raises(m.TemplateNotFoundError) as excinfo:
        _render(tmp_path, doc, template_id="nope")
    assert str(excinfo.value) == (
        "no template 'nope' in catalog (known: alpha, mike, zulu)")


def test_a_record_malformed_after_the_match_is_a_new_refusal(tmp_path):
    """THE HONEST NEW REFUSAL. `find_template` used to stop at the match, so
    a junk record AFTER it was never touched and the render succeeded. Every
    id is resolved up front now -- as the not-found path always did -- so
    this document reds where it used to render.

    It was never schema-valid (`id` is `required` on every record), and the
    alternative is a selector whose strictness depends on catalog ORDER,
    which is exactly the unpinned asymmetry every round of this family was
    filed about. Asserted as its own case rather than folded in, because it
    is a different claim from "a crash became a curated error"."""
    msg = _raises(tmp_path, _catalog(_record(), {"no": "id"}))
    assert "templates[1] is missing required key 'id'" in msg


# ---------------------------------------------------------------------
# Sites :166 :168 :173 -- `find_template_by_cores`.
# ---------------------------------------------------------------------

def _by_cores(tmp_path, doc, cores):
    m = _tmpl()
    catalog, _base, _metadata = _tree(tmp_path, doc)
    with pytest.raises(m.TemplateError) as excinfo:
        m.find_template_by_cores(
            m.load_catalog(catalog), cores, path=catalog)
    return str(excinfo.value)


@pytest.mark.parametrize("missing", ["id", "os"])
def test_a_cores_entry_missing_id_or_os_raises_a_curated_error(
        tmp_path, missing):
    """Pre-fix: `KeyError: 'id'` / `KeyError: 'os'` from `_topology`'s
    `{c["id"]: c["os"] for c in rec.get("cores", [])}` at
    `template.py:166`. Both are `required` on every `cores[]` entry."""
    entry = {"id": "m33_sm", "os": "zephyr"}
    entry.pop(missing)
    msg = _by_cores(
        tmp_path, _catalog(_record(cores=[entry])), {"m33_sm": "zephyr"})
    assert "catalog-v1.json" in msg
    assert f"templates[0].cores[0] is missing required key {missing!r}" in msg


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ("m33_sm", "str"), ({"a": 1}, "dict")])
def test_a_nonlist_cores_in_the_selector_raises(tmp_path, value, typename):
    """`rec.get("cores", [])` was iterated unguarded: `cores: 3` was
    `TypeError: 'int' object is not iterable`."""
    msg = _by_cores(
        tmp_path, _catalog(_record(cores=value)), {"m33_sm": "zephyr"})
    assert f"templates[0].cores must be a list, got {typename}" in msg


def test_the_single_match_records_id_is_checked_too(tmp_path):
    """This is what makes `cli._emit_scaffold`'s OWN `record["id"]`
    (`tan/planner/cli.py:186`) safe -- the one site of this defect outside
    this module. Pre-fix, `rec["id"]` was only subscripted in the
    AMBIGUOUS branch, so a UNIQUE match with no `id:` was returned happily
    and the CLI raised `KeyError: 'id'` one line later, outside every
    `except TemplateError` on the path."""
    msg = _by_cores(
        tmp_path,
        _catalog(_record(id=..., cores=[{"id": "m33_sm", "os": "zephyr"}])),
        {"m33_sm": "zephyr"})
    assert "templates[0] is missing required key 'id'" in msg


def test_the_ambiguous_message_is_byte_identical_to_before(tmp_path):
    """The ids are resolved before the >1 test now instead of inside it;
    the message must not have moved."""
    m = _tmpl()
    cores = [{"id": "m33_sm", "os": "zephyr"}]
    catalog, _base, _metadata = _tree(
        tmp_path,
        _catalog(_record(id="beta", cores=cores),
                 _record(id="alpha", cores=cores)))
    with pytest.raises(m.AmbiguousCoresError) as excinfo:
        m.find_template_by_cores(
            m.load_catalog(catalog), {"m33_sm": "zephyr"}, path=catalog)
    assert str(excinfo.value) == (
        "cores topology {'m33_sm': 'zephyr'} matches multiple templates "
        "['alpha', 'beta'] -- use --template to disambiguate")


# ---------------------------------------------------------------------
# Site :80 -- `record["files"]["user_owned"]`.
# ---------------------------------------------------------------------

def test_a_record_with_no_files_key_raises_a_curated_error(tmp_path):
    """Pre-fix: `KeyError: 'files'` at `template.py:80`, the bare DOUBLE
    subscript. Labelled by template id, not by list index, because
    `find_template` has already required `record["id"] == template_id`."""
    msg = _raises(tmp_path, _catalog(_record(files=...)))
    assert "catalog-v1.json" in msg
    assert "templates['fake1077'] is missing required key 'files'" in msg


def test_a_record_whose_files_has_no_user_owned_raises(tmp_path):
    """The inner half of the same double subscript: `KeyError:
    'user_owned'`."""
    msg = _raises(tmp_path, _catalog(_record(files={"generated": []})))
    assert ("templates['fake1077'].files is missing required key "
            "'user_owned'" in msg)


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ("board.yaml", "str")])
def test_a_nonlist_user_owned_raises_a_curated_error(
        tmp_path, value, typename):
    """`sorted("board.yaml")` is the quieter half: a string IS iterable, so
    it sorted into single CHARACTERS and every one of them became a
    "missing" template file -- no exception, just garbage."""
    msg = _raises(tmp_path, _catalog(_record(files={"user_owned": value})))
    assert (f"templates['fake1077'].files.user_owned must be a list, "
            f"got {typename}" in msg)


def test_a_nonstring_user_owned_entry_raises_before_sorted_does(tmp_path):
    """`sorted(["board.yaml", 3])` raised `TypeError: '<' not supported
    between instances of 'int' and 'str'` -- a raw traceback from a
    comparison, naming neither the file nor the field. The schema gives the
    entries `"type": "string"`."""
    msg = _raises(
        tmp_path, _catalog(_record(files={"user_owned": ["board.yaml", 3]})))
    assert ("templates['fake1077'].files.user_owned[1] must be a string, "
            "got int" in msg)


# ---------------------------------------------------------------------
# Site :1527 -- `record["supported"]["som_skus"]`.
# ---------------------------------------------------------------------

def test_a_record_with_no_supported_key_raises(tmp_path):
    """Pre-fix: `KeyError: 'supported'` at `template.py:1527`, the other
    bare double subscript."""
    msg = _raises(tmp_path, _catalog(_record(supported=...)))
    assert "templates['fake1077'] is missing required key 'supported'" in msg


def test_a_record_whose_supported_has_no_som_skus_raises(tmp_path):
    msg = _raises(tmp_path, _catalog(_record(supported={"families": []})))
    assert ("templates['fake1077'].supported is missing required key "
            "'som_skus'" in msg)


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ({"a": 1}, "dict")])
def test_a_nonlist_som_skus_raises(tmp_path, value, typename):
    msg = _raises(tmp_path, _catalog(_record(supported={"som_skus": value})))
    assert (f"templates['fake1077'].supported.som_skus must be a list, "
            f"got {typename}" in msg)


def test_a_nonstring_som_sku_entry_raises_before_sorted_does(tmp_path):
    """`sorted(supported)` runs in the `SkuNotSupportedError` message, so a
    mixed list turned a curated "sku not supported" into a raw
    `TypeError`."""
    msg = _raises(
        tmp_path, _catalog(_record(supported={"som_skus": [_SKU, 3]})))
    assert ("templates['fake1077'].supported.som_skus[1] must be a string, "
            "got int" in msg)


# ---------------------------------------------------------------------
# Site :301/:1539/:1751 -- `record["example"]`.
# ---------------------------------------------------------------------

def test_a_record_with_no_example_key_raises(tmp_path):
    """Pre-fix: `KeyError: 'example'`. Read at three sites, resolved once
    now."""
    msg = _raises(tmp_path, _catalog(_record(example=...)))
    assert "templates['fake1077'] is missing required key 'example'" in msg


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ([_EXAMPLE], "list")])
def test_a_nonstring_example_raises_a_curated_error(
        tmp_path, value, typename):
    """`_safe_join(root, rel)`'s `root / rel` on a non-string raised
    `TypeError: argument should be a str or an os.PathLike object`, from
    inside a containment guard, naming neither the catalog nor the
    field."""
    msg = _raises(tmp_path, _catalog(_record(example=value)))
    assert f"templates['fake1077'].example must be a string, got {typename}" \
        in msg


# ---------------------------------------------------------------------
# Sites :191 :229 :239 :258-266 -- `parameters:` and `substitute:`.
# ---------------------------------------------------------------------

def _params(tmp_path, parameters):
    return _raises(tmp_path, _catalog(_record(parameters=parameters)))


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ({"a": 1}, "dict"), ("x", "str")])
def test_a_nonlist_parameters_raises(tmp_path, value, typename):
    """`record.get("parameters", [])` was iterated unguarded at TWO sites
    (`_resolve_params` `:229`, `_substitutions_for` `:258`)."""
    msg = _params(tmp_path, value)
    assert (f"templates['fake1077'].parameters must be a list, "
            f"got {typename}" in msg)


@pytest.mark.parametrize("missing", ["name", "type", "default"])
def test_a_parameter_spec_missing_a_required_key_raises(tmp_path, missing):
    """`p["name"]` (`:229`), `spec["type"]` (`:191`, in `_coerce`) and
    `spec["default"]` (`:239`) were all bare. All three are `required` in
    the schema's `$defs/parameter`."""
    spec = {"name": "knob", "type": "string", "default": "x"}
    spec.pop(missing)
    msg = _params(tmp_path, [spec])
    assert (f"templates['fake1077'].parameters[0] is missing required key "
            f"{missing!r}" in msg)


@pytest.mark.parametrize("field", ["name", "type"])
def test_a_nonstring_parameter_name_or_type_raises(tmp_path, field):
    spec = {"name": "knob", "type": "string", "default": "x"}
    spec[field] = 3
    msg = _params(tmp_path, [spec])
    assert (f"templates['fake1077'].parameters[0].{field} must be a string, "
            f"got int" in msg)


def test_a_parameter_spec_that_is_not_a_mapping_raises(tmp_path):
    msg = _params(tmp_path, ["knob"])
    assert ("templates['fake1077'].parameters[0] must be a mapping, got str"
            in msg)


def test_a_parameter_default_may_be_any_json_value(tmp_path):
    """NOT STRICTER THAN THE SCHEMA. `$defs/parameter`'s `default:` declares
    NO type -- "the value the source example ships today" -- so `_require_key`
    is called with `kind=None` there and requires presence only. A `default:`
    of `null`, `0`, `false`, a list or a mapping must all still render, and
    `default: null` in particular would be refused by any `kind` at all."""
    for default in (None, 0, False, "", [], {}):
        out = _render(tmp_path / f"d{default!r}", _catalog(_record(
            parameters=[{"name": "knob", "type": "string",
                         "default": default}])))
        assert [rel for rel, _ in out] == ["board.yaml"], repr(default)


def test_a_substitute_block_with_no_file_key_raises(tmp_path):
    """`sub["file"]` (`:266`) was bare. The schema FORBIDS `substitute:`
    outright (`additionalProperties: false` on `$defs/parameter`), so no
    shipped catalog reaches this -- but the code path is live and a
    hand-edited catalog reaches it, which is the whole premise of this
    issue.

    The `knob=y` override is LOAD-BEARING: `_substitutions_for` returns
    early when the effective value equals the spec's `default:`, so a
    catalog whose `substitute:` block is never consulted still renders (the
    case below asserts exactly that). This is the document that reaches
    the site."""
    msg = _raises(tmp_path, _catalog(_record(
        parameters=[{"name": "knob", "type": "string", "default": "x",
                     "substitute": {"literal": "x"}}])),
        params={"knob": "y"})
    assert ("templates['fake1077'].parameters[0].substitute is missing "
            "required key 'file'" in msg)


def test_a_nonmapping_substitute_block_raises(tmp_path):
    """`sub.get("literal", ...)` on a non-mapping raised `AttributeError`
    one line before `sub["file"]` could raise `KeyError`."""
    msg = _raises(tmp_path, _catalog(_record(
        parameters=[{"name": "knob", "type": "string", "default": "x",
                     "substitute": "main.c"}])),
        params={"knob": "y"})
    assert ("templates['fake1077'].parameters[0].substitute must be a "
            "mapping, got str" in msg)


def test_a_substitute_whose_override_equals_its_default_is_still_skipped(
        tmp_path):
    """The guards went in at the ORIGINAL positions, not at the top of the
    loop: a spec whose effective value equals its `default:` returns early
    and its `substitute:` block is never validated, exactly as before. A
    catalog with an unreachable-but-malformed `substitute:` therefore still
    renders -- this fix adds no refusal to a document that used to work.

    No `params` override here, deliberately: the effective value IS the
    default, which is the early-return the two cases above have to defeat
    with `knob=y` to reach the guard at all."""
    out = _render(tmp_path, _catalog(_record(
        parameters=[{"name": "knob", "type": "string", "default": "x",
                     "substitute": {"literal": "x"}}])))
    assert [rel for rel, _ in out] == ["board.yaml"]


# ---------------------------------------------------------------------
# Sites :1149-1161 -- `_cmake_core_map`'s `core["dir"]` / `core["id"]`.
# ---------------------------------------------------------------------

def _cmake_map(tmp_path, cores):
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        m._cmake_core_map(
            {"cores": cores}, tmp_path,
            doc="metadata/templates/catalog-v1.json",
            field="templates['fake1077']")
    return str(excinfo.value)


@pytest.mark.parametrize(
    ("value", "typename"), [(3, "int"), ("./src", "str")])
def test_a_nonlist_cores_in_the_cmake_map_raises(tmp_path, value, typename):
    msg = _cmake_map(tmp_path, value)
    assert (f"templates['fake1077'].cores must be a list, got {typename}"
            in msg)


def test_a_cmake_core_entry_that_is_not_a_mapping_raises(tmp_path):
    """`core.get("os")` on a non-mapping raised `AttributeError` before
    either bare subscript could raise `KeyError`."""
    msg = _cmake_map(tmp_path, ["m33_sm"])
    assert ("templates['fake1077'].cores[0] must be a mapping, got str"
            in msg)


def test_a_nonstring_core_dir_raises_before_safe_join_does(tmp_path):
    """Pre-fix: `TypeError: argument should be a str or an os.PathLike
    object` out of `_safe_join`'s `root / rel`. `dir` is only read after
    the pre-existing `core.get("dir")` truthiness test, so this is a TYPE
    check, never a new requirement -- a core with no `dir:` is still
    skipped, asserted below."""
    msg = _cmake_map(tmp_path, [{"id": "m33_sm", "os": "zephyr", "dir": 3}])
    assert ("templates['fake1077'].cores[0].dir must be a string, got int"
            in msg)


def test_a_cmake_core_missing_id_raises(tmp_path):
    """`out[rel] = core["id"]` was bare: `KeyError: 'id'`. Reached only
    after `dir` resolves, so the fixture carries a real one."""
    example = tmp_path / "ex"
    (example / "src").mkdir(parents=True)
    m = _tmpl()
    with pytest.raises(m.TemplateError) as excinfo:
        m._cmake_core_map(
            {"cores": [{"os": "zephyr", "dir": "./src"}]}, example,
            doc="metadata/templates/catalog-v1.json",
            field="templates['fake1077']")
    assert ("templates['fake1077'].cores[0] is missing required key 'id'"
            in str(excinfo.value))


@pytest.mark.parametrize(
    ("label", "cores"),
    [
        ("no cores: key at all", None),
        ("cores: []", []),
        ("a core with no dir: (an `os: off` slice)",
         [{"id": "m33_sm", "os": "off"}]),
        ("a non-zephyr core", [{"id": "a55", "os": "yocto", "dir": "./linux"}]),
    ],
)
def test_the_cmake_map_still_skips_what_it_always_skipped(
        tmp_path, label, cores):
    """NOT STRICTER THAN THE SCHEMA, and not stricter than the pre-existing
    `os != "zephyr" or not core.get("dir")` skip either: every shape that
    used to be skipped is still skipped, and returns an empty map rather
    than raising."""
    m = _tmpl()
    record = {} if cores is None else {"cores": cores}
    assert m._cmake_core_map(
        record, tmp_path, doc="c", field="f") == {}, label


# ---------------------------------------------------------------------
# Not stricter than the schema -- proved against the REAL catalog.
# ---------------------------------------------------------------------

def test_every_record_in_the_real_shipped_catalog_still_resolves():
    """The acceptance bar this issue set: every document
    `template-catalog-v1.schema.json` accepts must still load. The strongest
    available evidence is the real one -- the bound checkout's own
    `metadata/templates/catalog-v1.json`, which alp-sdk's
    `scripts/check_template_catalog.py` validates against that schema on
    every run. Each record is resolved by id, by cores topology, and through
    every catalog-reading helper this fix touched."""
    m = _tmpl()
    doc = m.load_catalog()
    assert doc["templates"], "the bound catalog declares no templates"
    for record in doc["templates"]:
        found = m.find_template(doc, record["id"])
        assert found is record
        assert m._ordered_files(
            found, doc="catalog", field="rec") == tuple(
                sorted(record["files"]["user_owned"]))
        assert m._resolve_params(found, None, doc="catalog", field="rec") == {
            spec["name"]: spec["default"]
            for spec in record.get("parameters", [])}
        assert m._record_parameters(
            found, doc="catalog", field="rec") == record.get("parameters", [])
        assert m._catalog_templates(doc, path="catalog") is doc["templates"]


def test_an_absent_optional_shaped_list_still_degrades_to_empty(tmp_path):
    """`templates:`, `cores:` and `parameters:` are all `required` by the
    schema, and all three were read with a `.get(..., [])` default that
    tolerated their absence. That tolerance is PRESERVED -- tightening it
    would refuse documents the pre-fix code rendered, which is a behaviour
    change no issue asks for and the opposite of the acceptance bar."""
    out = _render(tmp_path, _catalog(_record(cores=..., parameters=...)))
    assert [rel for rel, _ in out] == ["board.yaml"]


# ---------------------------------------------------------------------
# The register is genuinely shared -- one rule, now four documents.
# ---------------------------------------------------------------------

def test_the_catalog_missing_key_message_is_the_registers_only_new_line():
    """`_require_key` adds exactly ONE message -- the missing-key line --
    and delegates both type checks to `_require_field`, so the "must be a
    <noun>, got <type>" half is the same string the three YAML documents
    already produce. If a future round hand-rolls a `KeyError` guard of its
    own instead of calling this, the two halves drift and this reds."""
    m = _tmpl()
    with pytest.raises(m.TemplateError) as missing:
        m._require_key({}, "id", str, doc="cat.json", field="templates[0]")
    with pytest.raises(m.TemplateError) as wrong_type:
        m._require_key({"id": 3}, "id", str, doc="cat.json",
                       field="templates[0]")
    with pytest.raises(m.TemplateError) as not_a_mapping:
        m._require_key(3, "id", str, doc="cat.json", field="templates[0]")

    assert str(missing.value) == (
        "cat.json templates[0] is missing required key 'id'")
    assert str(wrong_type.value) == str(m_field_message(m, 3, str, "templates[0].id"))
    assert str(not_a_mapping.value) == str(
        m_field_message(m, 3, dict, "templates[0]"))


def m_field_message(m, value, kind, field):
    """`_require_field`'s own message for @value, produced by calling it --
    so the assertion above compares `_require_key`'s output against the
    LIVE register rather than against a copy of it typed here."""
    try:
        m._require_field(value, kind, doc="cat.json", field=field)
    except m.TemplateError as exc:
        return exc
    raise AssertionError("expected _require_field to raise")


def test_the_three_yaml_documents_keep_the_yaml_wording(tmp_path):
    """`_require_mapping_doc`'s new `noun` keyword must not have moved the
    message tan-cli#1034/#1048/#1052 landed. The catalog says "a JSON
    object"; the three YAML documents still say "a YAML mapping", byte for
    byte, and `test_render_to_envelope_malformed_example_board.py::
    test_all_three_documents_share_one_outer_mapping_message` asserts the
    same thing from the other side."""
    m = _tmpl()
    metadata = tmp_path / "docs" / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "boards").mkdir(parents=True)
    (metadata / "e1m_modules" / "E1M-BARE.yaml").write_text(
        "- one\n- two\n", encoding="utf-8")
    (metadata / "boards" / "bare.yaml").write_text(
        "- one\n- two\n", encoding="utf-8")

    for call in (lambda: m._load_som_doc("E1M-BARE", metadata),
                 lambda: m._board_route_entries("bare", metadata)):
        with pytest.raises(m.TemplateError) as excinfo:
            call()
        assert "expected a YAML mapping, got list" in str(excinfo.value)

    assert "expected a JSON object, got list" in _raises(tmp_path, "[1, 2]")


def test_require_key_accepts_no_kind_for_an_untyped_schema_field():
    """`kind=None` is the `default:` case and nothing else: presence is
    required, the value is returned untouched whatever it is."""
    m = _tmpl()
    for value in (None, 0, False, "", [], {}, 3.5):
        assert m._require_key(
            {"default": value}, "default", doc="c", field="f") is value
