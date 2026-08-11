# SPDX-License-Identifier: Apache-2.0
"""`tan.core.error_catalog` -- the alp-sdk error-catalogue reader.

Unit level: the lookup, the suggestion shortlist, the rendered field order and
the four ways a catalogue can fail to read. The command-level surface (flags,
envelope, refusal codes) is `tests/commands/test_explain_code_command.py`.
"""

from __future__ import annotations

import json

import pytest

from tan.core.error_catalog import (
    CatalogUnreadable,
    catalog_path,
    detail_lines,
    load_codes,
    lookup,
    normalise,
    summary_line,
)

_ENTRY = {
    "cause": "a cause",
    "code": "ALP-B003",
    "doc": "docs/diagnostics/ALP-B003.md",
    "fix": "a fix",
    "kind": "runtime-diagnostic",
    "summary": "value violates an enum or pattern constraint",
}


def _write(tmp_path, payload, *, raw=None):
    (tmp_path / "metadata").mkdir(exist_ok=True)
    text = raw if raw is not None else json.dumps(payload)
    (tmp_path / "metadata" / "error-catalog.json").write_text(text, encoding="utf-8")
    return tmp_path


def test_catalog_path_is_the_generated_metadata_file(tmp_path):
    assert catalog_path(tmp_path) == tmp_path / "metadata" / "error-catalog.json"


def test_load_codes_returns_the_codes_map(tmp_path):
    _write(tmp_path, {"_banner": "AUTO-GENERATED", "codes": {"ALP-B003": _ENTRY}})
    assert load_codes(tmp_path) == {"ALP-B003": _ENTRY}


def test_a_catalogue_with_no_codes_key_is_empty_not_an_error(tmp_path):
    """`explain.py` reads `data.get("codes", {})`. An empty catalogue is one in
    which every code is unknown -- which the miss path already reports
    honestly, with a shortlist of nothing."""
    _write(tmp_path, {"_banner": "AUTO-GENERATED"})
    assert load_codes(tmp_path) == {}


def test_a_missing_catalogue_names_the_generator(tmp_path):
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail == (
        "error catalog not found -- run `python3 scripts/gen_error_catalog.py` in "
        "the alp-sdk checkout"
    )
    assert err.value.path == tmp_path / "metadata" / "error-catalog.json"


def test_malformed_json_is_a_catalog_error(tmp_path):
    _write(tmp_path, None, raw="{not json")
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail.startswith("error catalog is not valid JSON:")


def test_a_non_object_document_is_a_catalog_error(tmp_path):
    _write(tmp_path, ["ALP-B003"])
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail == "error catalog is not a JSON object"


def test_a_non_object_codes_value_is_a_catalog_error(tmp_path):
    """A `codes:` that parses but is a LIST would otherwise reach `lookup`,
    whose `{normalise(k): k for k in codes}` iterates the list happily -- so a
    query naming one of its elements MATCHES, and `codes[key]` then indexes a
    list by string. Measured with this guard removed: `TypeError: list indices
    must be integers or slices, not str`, surfacing as `explain.internal-
    failure` at exit 5. `test_a_wrong_shaped_catalogue_refuses_at_exit_1_not_
    exit_5` (tests/commands) is the pin on that difference; this one only
    asserts the guard fires."""
    _write(tmp_path, {"codes": ["ALP-B003"]})
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail == "error catalog's `codes` is not a JSON object"


def test_a_non_object_entry_is_a_catalog_error(tmp_path):
    """A `codes:` value that parses fine but whose OWN entry is not an object
    (a string, a list, `null`) would otherwise reach `resolve_code`'s `entry =
    codes[key]`, which hands it straight to `summary_line`/`detail_lines` --
    both `entry.get(...)`. Measured with this guard removed: `AttributeError:
    'str' object has no attribute 'get'`, the same exit-1-not-exit-5 concern
    `test_a_non_object_codes_value_is_a_catalog_error` pins one level up."""
    _write(tmp_path, {"codes": {"ALP-B003": "not an object"}})
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail == "error catalog entry 'ALP-B003' is not a JSON object"


def test_an_unreadable_catalogue_path_is_a_catalog_error(tmp_path):
    """The `except (OSError, UnicodeDecodeError)` arm, hit by a PATH that
    exists but cannot be read as a file -- a directory sitting where the
    catalogue should be. Distinct from `test_a_missing_catalogue_names_the_
    generator` (`FileNotFoundError`, caught by its own earlier arm) and from
    every JSON-shape test above (those all get past `read_text` and fail
    later): this is the one case that fails ON THE READ ITSELF for a reason
    other than "does not exist". Deleting this except arm is GREEN under the
    default `-k "explain or error_catalog"` sweep with no other change --
    that gap is what this test (plus the sibling non-UTF-8 one) closes."""
    (tmp_path / "metadata" / "error-catalog.json").mkdir(parents=True)
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail.startswith("error catalog is unreadable:")


def test_a_non_utf8_catalogue_is_a_catalog_error(tmp_path):
    """The same `except (OSError, UnicodeDecodeError)` arm, hit by a readable
    file whose bytes are not valid UTF-8 -- a `UnicodeDecodeError` rather than
    an `OSError`, so both members of the tuple get their own test."""
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "error-catalog.json").write_bytes(b"\xff\xfe")
    with pytest.raises(CatalogUnreadable) as err:
        load_codes(tmp_path)
    assert err.value.detail.startswith("error catalog is unreadable:")
    assert "codec can't decode" in err.value.detail


@pytest.mark.parametrize(
    "typed,expected",
    [("alp-b003", "ALP-B003"), ("  ALP-B003 ", "ALP-B003"), ("\talp_err_x\n", "ALP_ERR_X")],
)
def test_normalise_upcases_and_trims(typed, expected):
    assert normalise(typed) == expected


def test_lookup_reports_the_catalogues_own_spelling(tmp_path):
    key, suggestions = lookup({"ALP-B003": _ENTRY}, "alp-b003")
    assert key == "ALP-B003"
    assert suggestions == []


def test_a_miss_returns_no_key_and_the_shortlist(tmp_path):
    codes = {"ALP_ERR_NO_BACKEND": {}, "ALP_ERR_NOT_READY": {}, "ALP-B003": {}}
    key, suggestions = lookup(codes, "ALP_ERR_NO_BACKENDD")
    assert key is None
    assert suggestions == ["ALP_ERR_NO_BACKEND", "ALP_ERR_NOT_READY"]


def test_a_miss_with_nothing_close_suggests_nothing(tmp_path):
    key, suggestions = lookup({"ALP-B003": _ENTRY}, "ZZZ")
    assert key is None
    assert suggestions == []


def test_summary_line_is_code_then_kind():
    assert summary_line(_ENTRY) == "ALP-B003 (runtime-diagnostic)"


def test_detail_lines_hold_the_field_order():
    assert detail_lines(_ENTRY) == [
        "summary: value violates an enum or pattern constraint",
        "cause: a cause",
        "fix: a fix",
        "doc: docs/diagnostics/ALP-B003.md",
    ]


def test_a_field_the_catalogue_omits_is_skipped_not_rendered_empty():
    """The api-error shape: no `cause`, no `fix`. The generator omits a field
    with no source rather than fabricating one; rendering `cause: ` here would
    put an empty claim back on screen that alp-sdk deliberately never made."""
    entry = {"code": "ALP_ERR_X", "kind": "api-error", "summary": "s", "doc": "d"}
    assert detail_lines(entry) == ["summary: s", "doc: d"]
