# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1084: `tan/core/example_catalog.py` decoded `metadata/templates/
catalog-v1.json` through an unguarded `json.loads` and then `.get`/
subscripted the result -- the SAME shape tan-cli#1077 closed on
`tan/planner/template.py`, on the SAME document, in a DELIBERATE second
implementation of the same read.

The second implementation is not a mistake: `tan.planner.paths` binds
`REPO = sdk_root()` at MODULE scope, so importing anything under
`tan.planner` before `bind_sdk_root` has run raises `PlannerRootError`, and
`tan init`'s SDK-free path (invariant I-32) must keep working with no
checkout bound at all. What WAS a mistake is that the two stopped agreeing:
after #1077 the planner refused a malformed catalog with a curated error
while this reader crashed raw or silently mis-read -- and the gate that
asserts the two agree covered only WELL-FORMED input.

RE-DERIVED ON `dev@be3a44b6` BEFORE FIXING, not taken on trust. One edit per
row of a synthetic catalog, both readers driven on the same tree
(`{"m33_sm": "zephyr"}` requested), verbatim:

    row                            planner (after #1077)  example_catalog (dev)
    not valid JSON                 TemplateError          JSONDecodeError
    a JSON list                    TemplateError          AttributeError
    a JSON string                  TemplateError          AttributeError
    templates: 3                   TemplateError          TypeError
    templates: 'abc'               TemplateError          *silent* not-found
    a record that is not a mapping TemplateError          *silent* skip
    cores: 3                       TemplateError          TypeError
    cores: 'abc'                   TemplateError          TypeError
    a cores entry not a mapping    TemplateError          TypeError
    a cores entry with no id       TemplateError          KeyError
    a cores entry with no os       TemplateError          KeyError
    a cores entry, id: 3           TemplateError          *silent* not-found
    the matched record has no id   TemplateError          *silent* OK
    the matched record id: 3       TemplateError          *silent* OK
    the matched record example: 3  OK (record)            *silent* OK, src '3'
    catalog file absent            TemplateError          FileNotFoundError

Nine raw crashes, six silent mis-reads. The worst is the last: a record
carrying `example: 3` resolved to the literal src `'3'` and `tan init` went
looking for a directory of that name.

`unsupported_som`'s own written contract -- "Never raises: a scaffold must
not fail because a catalog could not be read" -- was BROKEN by two of those
rows, `templates: 3` (`TypeError: 'int' object is not iterable`) and a record
with `supported: 3` (`AttributeError: 'int' object has no attribute 'get'`).

THE GUARD IS THE PLANNER'S OWN REGISTER, MOVED, NOT COPIED. `_require_field`
/ `_require_mapping_doc` / `_require_key` and the catalog's two readers moved
to `tan/core/document_guards.py` (stdlib-only, no `tan.planner` in its import
closure), and `template.py` binds the same objects -- so its ~40 call sites
are byte-identical to what #1073/#1077 landed, and neutering one method there
reds tests on BOTH sides. The register-sharing half of that proof needs a
bound alp-sdk checkout to import the planner at all, so it lives in
`tests/gates/test_example_catalog_cores_selector_agrees_with_planner.py`
alongside the byte-identical-message agreement; everything in THIS file runs
with no checkout bound, which is the point -- `example_catalog.py` is the
SDK-free reader.

NOT STRICTER THAN `metadata/schemas/template-catalog-v1.schema.json`. Every
key required here is `required` there (`id`, `example`, `supported`,
`supported.som_skus`, `cores[].id`, `cores[].os`), every type matches its
declared type, and an ABSENT `templates:`/`cores:` still degrades exactly as
before -- asserted directly below, not assumed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tan.core.example_catalog as ec

_SKU = "E1M-FAKE1084"
_EXAMPLE = "examples/peripheral-io/fake1084"
_SRC = "peripheral-io/fake1084"
_CORES = {"m33_sm": "zephyr"}


def _record(**overrides):
    """A catalog record that resolves, with @overrides applied. A key whose
    value is the sentinel `...` is DELETED rather than replaced -- the
    "required key absent entirely" cases."""
    rec = {
        "id": "fake1084",
        "example": _EXAMPLE,
        "description": "a synthetic record",
        "supported": {"som_skus": [_SKU]},
        "files": {"user_owned": ["board.yaml"]},
        "cores": [{"id": "m33_sm", "os": "zephyr"}],
        "parameters": [],
    }
    for key, value in overrides.items():
        if value is ...:
            rec.pop(key, None)
        else:
            rec[key] = value
    return rec


def _tree(tmp_path: Path, catalog: str | dict | None) -> Path:
    """A fake SDK root holding @catalog. `None` writes no catalog file at
    all; a `dict` is dumped as JSON; a `str` is written verbatim, so a case
    can be illegal JSON."""
    root = tmp_path / "sdk"
    (root / "metadata" / "templates").mkdir(parents=True, exist_ok=True)
    if catalog is not None:
        text = catalog if isinstance(catalog, str) else json.dumps(catalog)
        (root / "metadata" / "templates" / "catalog-v1.json").write_text(
            text, encoding="utf-8")
    return root


def _one(tmp_path: Path, **overrides) -> Path:
    """A tree whose catalog holds exactly one record, with @overrides."""
    return _tree(tmp_path, {"schemaVersion": 1, "description": "d",
                            "templates": [_record(**overrides)]})


def _catalog_path(root: Path) -> Path:
    return root / "metadata" / "templates" / "catalog-v1.json"


# ---------------------------------------------------------------------------
# the control: nothing that resolved before stops resolving
# ---------------------------------------------------------------------------


def test_a_wellformed_catalog_still_resolves(tmp_path):
    assert ec.find_example_by_cores(_one(tmp_path), _CORES) == _SRC


def test_a_wellformed_catalog_still_reports_an_unsupported_sku(tmp_path):
    assert ec.unsupported_som(_one(tmp_path), _SRC, "E1M-OTHER") == (_SKU,)


def test_a_supported_sku_still_reports_nothing(tmp_path):
    assert ec.unsupported_som(_one(tmp_path), _SRC, _SKU) is None


# ---------------------------------------------------------------------------
# read_catalog_document: the file, the decode, the outer shape
# ---------------------------------------------------------------------------


def test_a_missing_catalog_file_is_curated_not_a_filenotfounderror(tmp_path):
    root = _tree(tmp_path, None)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value).startswith(
        f"cannot read template catalog at {_catalog_path(root)}: ")


def test_an_unreadable_catalog_path_is_curated_too(tmp_path):
    """A DIRECTORY where the catalog should be -- present, but unreadable.
    `except OSError`, not a pre-flight `is_file()`, is what names this one."""
    root = tmp_path / "sdk"
    _catalog_path(root).mkdir(parents=True)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert "cannot read template catalog at" in str(exc.value)


def test_a_catalog_that_is_not_valid_json_is_curated_not_a_decodeerror(tmp_path):
    root = _tree(tmp_path, "{")
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value).startswith(
        f"malformed template catalog at {_catalog_path(root)}: not valid JSON (")


def test_a_non_utf8_catalog_is_curated_not_a_unicodedecodeerror(tmp_path):
    """tan-cli#1096 review (BLOCKER): `read_catalog_document` caught
    `except OSError` only, and `UnicodeDecodeError` is a `ValueError`, NOT an
    `OSError` -- a byte a catalog author never intended (a stray `0xff`, a
    Windows-editor mis-save) escaped `find_example_by_cores` as a raw
    traceback instead of the same curated `MalformedCatalogError` every other
    unreadable-document row on this page gets."""
    root = _tree(tmp_path, None)
    _catalog_path(root).write_bytes(b"\xff\xfe not valid utf-8")
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value).startswith(
        f"cannot read template catalog at {_catalog_path(root)}: ")
    assert "codec can't decode" in str(exc.value)


@pytest.mark.parametrize("text,kind", [("[1, 2]", "list"), ('"nope"', "str"),
                                       ("3", "int"), ("null", "NoneType")])
def test_a_nonobject_catalog_raises_a_curated_error(tmp_path, text, kind):
    root = _tree(tmp_path, text)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"malformed template catalog at {_catalog_path(root)}: expected "
        f"a JSON object, got {kind}")


def test_read_catalog_document_itself_refuses_a_nonobject_document(tmp_path):
    """The REDUNDANT-GUARD half, pinned at its own call site.

    `catalog_templates` re-checks the outer shape one call later, so
    reverting the guard inside `read_catalog_document` alone changes nothing
    observable through `find_example_by_cores` -- exactly the pair that
    stayed green twice in PR #1082's review. The redundancy is worth keeping
    (each takes the value as a parameter and is reachable on its own), but an
    unpinned guard is not a guard."""
    root = _tree(tmp_path, "[1, 2]")
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec._GUARDS.read_catalog_document(_catalog_path(root))
    assert str(exc.value) == (
        f"malformed template catalog at {_catalog_path(root)}: expected "
        f"a JSON object, got list")


def test_catalog_templates_itself_refuses_a_nonobject_document(tmp_path):
    """The OTHER half of the same pair, for the same reason."""
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec._GUARDS.catalog_templates([1, 2], path="catalog")
    assert str(exc.value) == (
        "malformed template catalog at catalog: expected a JSON object, "
        "got list")


# ---------------------------------------------------------------------------
# catalog_templates: `templates:` itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,kind", [(3, "int"), ("abc", "str"),
                                        ({}, "dict"), (None, "NoneType")])
def test_a_nonlist_templates_raises_a_curated_error(tmp_path, value, kind):
    """`templates: 'abc'` is the quiet half: a string IS iterable, so dev
    walked it CHARACTER by character, found no mapping, and answered
    "no catalog template with that topology (known topologies: [])" about a
    catalog it had not read."""
    root = _tree(tmp_path, {"templates": value})
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates must be a list, got {kind}")


def test_an_absent_templates_key_still_degrades_to_not_found(tmp_path):
    """ABSENT is not malformed -- the pre-existing `.get` default, kept."""
    root = _tree(tmp_path, {"schemaVersion": 1})
    with pytest.raises(ec.CoresTopologyNotFoundError):
        ec.find_example_by_cores(root, _CORES)


@pytest.mark.parametrize("entry,kind", [(3, "int"), ("abc", "str"),
                                        ([], "list"), (None, "NoneType")])
def test_a_templates_entry_that_is_not_a_mapping_raises(tmp_path, entry, kind):
    """dev SKIPPED it (`if isinstance(r, dict)`), so a corrupted record was
    invisible and the answer came back as a confident not-found."""
    root = _tree(tmp_path, {"templates": [entry]})
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0] must be a mapping, got {kind}")


# ---------------------------------------------------------------------------
# _topology: `cores:` and its entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,kind", [(3, "int"), ("abc", "str"),
                                        ({}, "dict"), (None, "NoneType")])
def test_a_nonlist_cores_raises_a_curated_error(tmp_path, value, kind):
    root = _one(tmp_path, cores=value)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].cores must be a list, got {kind}")


def test_an_absent_cores_key_still_degrades_to_an_empty_topology(tmp_path):
    """`cores: null` is refused above (`got NoneType`); an ABSENT `cores:`
    still means `{}`, so a record without one is simply not a match. That is
    dev's behaviour and the planner's, and the schema requires `cores` with
    `minItems: 1` either way."""
    root = _one(tmp_path, cores=...)
    with pytest.raises(ec.CoresTopologyNotFoundError):
        ec.find_example_by_cores(root, _CORES)


@pytest.mark.parametrize("entry,kind", [(3, "int"), ("abc", "str"),
                                        ([], "list"), (None, "NoneType")])
def test_a_cores_entry_that_is_not_a_mapping_raises(tmp_path, entry, kind):
    root = _one(tmp_path, cores=[entry])
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].cores[0] must be a mapping, "
        f"got {kind}")


@pytest.mark.parametrize("missing,present", [("id", {"os": "zephyr"}),
                                             ("os", {"id": "m33_sm"})])
def test_a_cores_entry_missing_id_or_os_raises_a_curated_error(
        tmp_path, missing, present):
    root = _one(tmp_path, cores=[present])
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].cores[0] is missing required "
        f"key {missing!r}")


@pytest.mark.parametrize("core,field,kind", [
    ({"id": 3, "os": "zephyr"}, "id", "int"),
    ({"id": "m33_sm", "os": 3}, "os", "int"),
    ({"id": None, "os": "zephyr"}, "id", "NoneType"),
])
def test_a_nonstring_core_id_or_os_raises_before_it_mis_reads_the_topology(
        tmp_path, core, field, kind):
    """The SILENT half. dev built `{3: 'zephyr'}` -- a topology no caller can
    ever request -- and answered not-found about a record that was really
    corrupt."""
    root = _one(tmp_path, cores=[core])
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].cores[0].{field} must be a "
        f"string, got {kind}")


# ---------------------------------------------------------------------------
# the matched record: `id:` and `example:`
# ---------------------------------------------------------------------------


def test_the_single_match_records_id_is_checked_too(tmp_path):
    """The NEW refusal, and the redundant-guard half worth its own case: dev
    read `id` only in the AMBIGUOUS branch, so a UNIQUE match with no `id:`
    came back happily. The planner has resolved every match's id before the
    `>1` test since tan-cli#1077; this is that same fix, and it is what makes
    the two agree on this row."""
    root = _one(tmp_path, id=...)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0] is missing required key 'id'")


@pytest.mark.parametrize("value,kind", [(3, "int"), (None, "NoneType"),
                                        ([], "list")])
def test_a_nonstring_record_id_raises_a_curated_error(tmp_path, value, kind):
    root = _one(tmp_path, id=value)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].id must be a string, got {kind}")


def test_the_ambiguous_branch_still_names_every_candidate_id(tmp_path):
    """Two records, one topology -- the ids are resolved through the register
    now, and the message is unchanged."""
    root = _tree(tmp_path, {"templates": [
        _record(id="bbb"), _record(id="aaa", example="examples/x/y")]})
    with pytest.raises(ec.AmbiguousCoresTopologyError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert exc.value.candidate_ids == ["aaa", "bbb"]
    assert str(exc.value) == (
        "cores topology {'m33_sm': 'zephyr'} matches multiple catalog "
        "templates ['aaa', 'bbb'] -- use --template or --from-example to "
        "disambiguate")


def test_an_ambiguous_candidates_missing_id_raises_rather_than_printing_it(
        tmp_path):
    """dev formatted a missing id as the literal `'?'` into the candidate
    list -- a message telling the customer to choose between `?` and a real
    template. Every candidate id goes through the register now."""
    root = _tree(tmp_path, {"templates": [
        _record(id=...), _record(id="aaa", example="examples/x/y")]})
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0] is missing required key 'id'")


@pytest.mark.parametrize("value,kind", [(3, "int"), (True, "bool"),
                                        (["a"], "list"), ({"a": 1}, "dict")])
def test_a_nonstring_example_no_longer_resolves_to_a_literal_str_of_it(
        tmp_path, value, kind):
    """The WORST row of the re-derived table: dev returned
    `str(matches[0]["example"])`, so `example: 3` resolved to the src `'3'`
    and `tan init --topology` went looking for a directory of that name."""
    root = _one(tmp_path, example=value)
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert str(exc.value) == (
        f"{_catalog_path(root)} templates[0].example must be a string, "
        f"got {kind}")


def test_a_record_with_no_example_is_still_excluded_not_refused(tmp_path):
    """dev's documented posture, KEPT: "cannot tell means silent", scoped to
    the one record. This is the single place the two selectors deliberately
    still differ (the planner returns the record, so it never reads
    `example` here at all) -- pinned as a named divergence in
    `tests/gates/test_example_catalog_cores_selector_agrees_with_planner.py`
    rather than left undocumented as it was."""
    root = _one(tmp_path, example=...)
    with pytest.raises(ec.CoresTopologyNotFoundError):
        ec.find_example_by_cores(root, _CORES)


# ---------------------------------------------------------------------------
# unsupported_som: the "Never raises" contract, now actually held
# ---------------------------------------------------------------------------


def test_unsupported_som_no_longer_raises_on_a_nonlist_templates(tmp_path):
    """dev: `TypeError: 'int' object is not iterable`, straight out of a
    function whose docstring says it never raises."""
    root = _tree(tmp_path, {"templates": 3})
    assert ec.unsupported_som(root, _SRC, "E1M-OTHER") is None


def test_unsupported_som_no_longer_raises_on_a_nonmapping_supported(tmp_path):
    """dev: `AttributeError: 'int' object has no attribute 'get'`, from
    `(record.get("supported") or {}).get("som_skus")`."""
    root = _one(tmp_path, supported=3)
    assert ec.unsupported_som(root, _SRC, "E1M-OTHER") is None


def test_unsupported_som_no_longer_raises_on_a_non_utf8_catalog(tmp_path):
    """tan-cli#1096 review (BLOCKER), the exact case measured end to end
    against `tan init --from-example`: a catalog with one non-UTF-8 byte
    raised `UnicodeDecodeError` straight through `read_catalog_document`
    (only `OSError` was caught there), past this function's own
    `except MalformedCatalogError`, out of a function whose docstring
    promises "a scaffold must not fail because a catalog could not be
    read" -- `init_cmd.py` called it bare, so the raw traceback surfaced as
    `init.internal-failure` instead of the silent `None` every other
    unreadable-catalog row in this section already gets."""
    root = _tree(tmp_path, None)
    _catalog_path(root).write_bytes(b"\xff\xfe not valid utf-8")
    assert ec.unsupported_som(root, _SRC, "E1M-OTHER") is None


@pytest.mark.parametrize("catalog", [
    None,                                             # no catalog at all
    "{",                                              # not valid JSON
    "[1, 2]",                                         # not a JSON object
    {"templates": []},                                # no record for it
    {"templates": [{"example": "examples/other/x"}]},  # a different example
])
def test_unsupported_som_still_returns_none_for_every_documented_reason(
        tmp_path, catalog):
    assert ec.unsupported_som(_tree(tmp_path, catalog), _SRC, "E1M-OTHER") is None


@pytest.mark.parametrize("supported", [..., {}, {"som_skus": []},
                                       {"som_skus": 3}, {"som_skus": None}])
def test_unsupported_som_still_returns_none_without_a_usable_som_skus(
        tmp_path, supported):
    root = _one(tmp_path, supported=supported)
    assert ec.unsupported_som(root, _SRC, "E1M-OTHER") is None


def test_a_malformed_record_does_not_silence_a_later_wellformed_one(tmp_path):
    """The per-record SKIP, kept deliberately and pinned on its own. A
    catalog-level shape problem degrades the whole call to None; a record
    whose own `example:` cannot be read is simply not the record the scan is
    looking for, so the warning a LATER well-formed record would have
    produced still arrives. Collapsing the two would make one corrupt record
    silence every warning behind it."""
    root = _tree(tmp_path, {"templates": [
        {"no": "example"}, 3, {"example": 7}, _record()]})
    assert ec.unsupported_som(root, _SRC, "E1M-OTHER") == (_SKU,)


# ---------------------------------------------------------------------------
# catalog_unreadable: tan-cli#1101's "check was skipped, not passed" signal
# ---------------------------------------------------------------------------


def test_catalog_unreadable_is_none_for_a_wellformed_catalog(tmp_path):
    assert ec.catalog_unreadable(_one(tmp_path), _SRC) is None


def test_catalog_unreadable_is_none_for_an_absent_catalog(tmp_path):
    """`unsupported_som`'s own long-standing precedent: no catalog at all is
    an older SDK, not a new failure to report -- must not warn."""
    root = _tree(tmp_path, None)
    assert ec.catalog_unreadable(root, _SRC) is None


def test_catalog_unreadable_names_a_non_utf8_catalog(tmp_path):
    root = _tree(tmp_path, None)
    _catalog_path(root).write_bytes(b"\xff\xfe not valid utf-8")
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert reason.startswith(f"cannot read template catalog at {_catalog_path(root)}: ")


def test_catalog_unreadable_names_a_directory_where_the_file_should_be(tmp_path):
    root = tmp_path / "sdk"
    _catalog_path(root).mkdir(parents=True)
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert "cannot read template catalog at" in reason


def test_catalog_unreadable_names_invalid_json(tmp_path):
    root = _tree(tmp_path, "{")
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert "not valid JSON" in reason


def test_catalog_unreadable_names_a_nonobject_document(tmp_path):
    root = _tree(tmp_path, "[1, 2]")
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert "expected a JSON object, got list" in reason


def test_catalog_unreadable_names_a_nonlist_templates(tmp_path):
    root = _tree(tmp_path, {"templates": 3})
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert "templates must be a list, got int" in reason


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root: chmod 0o000 has no effect for root, and "
           "Windows ACLs don't honour POSIX mode bits the same way.",
)
def test_catalog_unreadable_names_a_permission_denied_parent_directory(tmp_path):
    """tan-cli#1101 review BLOCKER, re-derived. `Path.exists()` swallows only
    `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` (`pathlib.py`'s `_IGNORED_ERRNOS`) --
    NOT `EACCES`. A pre-flight `if not catalog.exists(): return None` (the
    function's own first draft) therefore let a `PermissionError` on a
    catalog whose PARENT directory the caller cannot traverse escape RAW,
    straight past `except MalformedCatalogError` -- undoing the exact
    crash-safety PR #1096 landed, on the exact "a permissions failure" shape
    this function's own docstring claims to cover. There must be no
    pre-flight stat at all: `catalog_unreadable` reads straight through
    `_declared_som_skus` -> `read_catalog_document`, whose own
    `except (OSError, UnicodeDecodeError)` already covers this without ever
    calling `.exists()` first."""
    root = _one(tmp_path)
    templates_dir = root / "metadata" / "templates"
    templates_dir.chmod(0o000)
    try:
        reason = ec.catalog_unreadable(root, _SRC)
    finally:
        templates_dir.chmod(0o755)
    assert reason is not None
    assert "cannot read template catalog at" in reason
    assert "Permission denied" in reason


def test_catalog_unreadable_names_a_malformed_matching_records_supported_field(tmp_path):
    """tan-cli#1101 review MAJOR: a catalog that is well-formed at the top
    level, whose record's `example:` DOES match, but whose `supported:` (or
    `supported.som_skus:`) cannot be read as the shape its schema declares,
    used to report `None` -- indistinguishable from "checked, sku is fine"
    or "no record at all". The SoM-support check for THIS example could not
    run either way; both must be named."""
    root = _one(tmp_path, supported=3)
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert f"{_catalog_path(root)} templates[0].supported must be a mapping" in reason


def test_catalog_unreadable_names_a_malformed_matching_records_som_skus(tmp_path):
    root = _one(tmp_path, supported={"som_skus": "not-a-list"})
    reason = ec.catalog_unreadable(root, _SRC)
    assert reason is not None
    assert "supported.som_skus must be a list" in reason


def test_catalog_unreadable_is_none_when_a_different_records_example_is_malformed(tmp_path):
    """The per-record SKIP still holds through `catalog_unreadable`: a
    record for a DIFFERENT example whose own `example:` cannot be read must
    not spuriously warn about `_SRC`, which a later, well-formed record
    still resolves cleanly."""
    root = _tree(tmp_path, {"templates": [{"example": 7}, _record()]})
    assert ec.catalog_unreadable(root, _SRC) is None


# ---------------------------------------------------------------------------
# the refusal type itself
# ---------------------------------------------------------------------------


def test_every_refusal_is_curated_and_reaches_both_handler_shapes(tmp_path):
    """`MalformedCatalogError` is a `ValueError` -- the family
    `tan/model/targets.py:312-323` established -- AND a `CoresTopologyError`,
    so a caller that catches only this module's base still cannot be handed a
    raw traceback."""
    root = _tree(tmp_path, "[1, 2]")
    with pytest.raises(ec.MalformedCatalogError) as exc:
        ec.find_example_by_cores(root, _CORES)
    assert isinstance(exc.value, ValueError)
    assert isinstance(exc.value, ec.CoresTopologyError)
    assert not isinstance(exc.value, ec.CoresTopologyNotFoundError)
    assert not isinstance(exc.value, ec.AmbiguousCoresTopologyError)


def test_a_malformed_catalog_is_not_swallowed_by_an_unrelated_handler():
    """The measured claim behind the multiple inheritance: nothing between
    `find_example_by_cores` and `init()` catches a bare `ValueError`.
    `init_cmd.py`'s two `except ... ValueError` blocks belong to `_pin_sdk`
    and a board.yaml read; neither encloses `_plan_from_topology`'s call, and
    `_plan_from_topology`'s own `except ValueError` guards
    `parse_topology_arg` in a DIFFERENT try block, three statements earlier.
    A structural check, so a future edit that widens one of them reds here."""
    import ast
    import inspect

    from tan.commands import init_cmd

    tree = ast.parse(inspect.getsource(init_cmd._plan_from_topology))
    handlers = [h for node in ast.walk(tree) if isinstance(node, ast.Try)
                for h in node.handlers
                if any(find_example.id == "find_example_by_cores"
                       for stmt in node.body
                       for find_example in ast.walk(stmt)
                       if isinstance(find_example, ast.Name))]
    caught = {name.id for h in handlers for name in ast.walk(h.type or h)
              if isinstance(name, ast.Name)}
    assert "MalformedCatalogError" in caught, caught
    assert "ValueError" not in caught, caught


def test_the_malformed_catalog_handler_emits_its_own_issue_code(tmp_path, monkeypatch):
    """End to end through the command layer: the curated message reaches the
    envelope as `init.catalog-malformed`, not as a traceback and not as
    `init.topology-not-found`."""
    from tan.commands import init_cmd

    root = _tree(tmp_path, "[1, 2]")
    monkeypatch.setattr(init_cmd, "_is_sdk_checkout", lambda _p: True)
    sdk = init_cmd._Sdk(path=root, display=str(root))
    with pytest.raises(init_cmd.InitError) as exc:
        init_cmd._plan_from_topology("m33_sm:zephyr", None, sdk)
    assert exc.value.code == "init.catalog-malformed"
    assert "expected a JSON object, got list" in exc.value.message


# ---------------------------------------------------------------------------
# not stricter than the schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {},
    {"cores": [{"id": "m33_sm", "os": "zephyr", "dir": "./src"}]},
    {"parameters": ...},
    {"files": ...},
    {"description": ...},
    {"supported": {"som_skus": [_SKU], "families": ["renesas-rzv2n"]}},
])
def test_a_schema_valid_shape_still_resolves(tmp_path, overrides):
    """Only keys this module actually reads are required, and only with the
    type the schema already declares. `description`/`files`/`parameters` are
    `required` by the schema and deliberately NOT checked here -- this reader
    never touches them, and guarding a key nobody reads is exactly the
    stricter-than-the-schema the issue rules out."""
    assert ec.find_example_by_cores(_one(tmp_path, **overrides), _CORES) == _SRC
