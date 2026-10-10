# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1298: `tan model check` validates the per-NPU op-support tables it
reads against `npu-ops-v1.schema.json`, skip-but-disclose.

A table the schema rejects is skipped like a malformed file (the report reads
`undetermined`, never a negative scored against it) and the violation is a
note on the report -- the channel `tan model check` already renders. An SDK
without the schema keeps working, with a disclosure note instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tan.core.metadata_schema import npu_ops_schema_path, validate_document
from tan.model.analyze import COVERAGE_WITHHELD, analyze_backend
from tan.model.tensorio import OpDesc
from tests.conftest import sdk_root

SDK = sdk_root()

# A stand-in for the real schema, small enough to read but with the same
# top-level shape: the real `required` list, `additionalProperties: false`,
# and the two constraints these tests flip (`stance` is the single allowed
# word, `supported_ops` is a list of identifier strings). `_table()` is
# built to be valid against the REAL schema too -- the SDK-bound test below
# asserts that, so the fixture cannot drift into a shape the real one rejects.
_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["applies_to", "op_namespace", "authority", "stance", "provenance", "supported_ops"],
    "properties": {
        "applies_to": {"type": "object"},
        "op_namespace": {"enum": ["tflite", "onnx"]},
        "authority": {"enum": ["tool-generated", "vendor-manual"]},
        "provenance": {"type": "object"},
        "stance": {"type": "string", "enum": ["screening"]},
        "supported_ops": {"type": "array", "minItems": 1,
                          "items": {"type": "string", "pattern": "^[A-Za-z0-9_]+$"}},
    },
}
_OPS = [OpDesc(op="CONV_2D", macs=10, op_namespace="tflite")]
_SCHEMA_NOTE = "npu-ops-v1.schema.json"


def _table(variant: str = "u85", stance: str = "screening", **overrides) -> dict:
    doc = {
        "applies_to": {"variant": variant, "products": ["ethos-u85"],
                       "toolchain": "vela", "toolchain_version": "5.2.0"},
        "op_namespace": "tflite",
        "authority": "tool-generated",
        "stance": stance,
        "provenance": {"source": "vela --supported-ops-report", "tool_version": "vela 5.2.0",
                       "content_hash": "md5:5f3168f3b3edb8b9dd6005088d4c5b3"},
        "supported_ops": ["CONV_2D"],
    }
    doc.update(overrides)
    return doc


def _root(tmp_path: Path, tables: dict[str, dict], *, schema: str | None = "valid") -> Path:
    """A metadata root with @tables (file name -> document) under
    `npu_ops/ethos_u/`. @schema: "valid" writes the stand-in, None writes no
    schema file, any other string is written verbatim as the schema file."""
    table_dir = tmp_path / "npu_ops" / "ethos_u"
    table_dir.mkdir(parents=True)
    for name, doc in tables.items():
        (table_dir / name).write_text(json.dumps(doc), encoding="utf-8")
    if schema is not None:
        path = npu_ops_schema_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_SCHEMA) if schema == "valid" else schema, encoding="utf-8")
    return tmp_path


def _single(tmp_path: Path, doc: dict, **kw) -> Path:
    return _root(tmp_path, {"u85@vela-5.2.0.json": doc}, **kw)


def _analyze(root: Path, variant: str | None = "u85"):
    return analyze_backend(backend="ethos_u", src_format="tflite", ops=_OPS,
                           metadata_root=root, variant=variant)


def _schema_notes(rep) -> list[str]:
    return [n for n in rep.notes if _SCHEMA_NOTE in n or "not validated" in n or "skipped" in n]


def _assert_no_absolute_path(rep, root: Path) -> None:
    for note in rep.notes:
        assert str(root) not in note and root.as_posix() not in note, note


def test_npu_ops_schema_path_joins_the_metadata_root(tmp_path):
    assert npu_ops_schema_path(tmp_path) == tmp_path / "schemas" / "npu-ops-v1.schema.json"


def test_a_schema_valid_table_is_scored_and_carries_no_schema_note(tmp_path):
    rep = _analyze(_single(tmp_path, _table()))
    assert rep.table is not None
    assert rep.npu_coverage == "full-eligible"
    assert _schema_notes(rep) == []


def test_a_schema_invalid_table_is_skipped_and_the_violation_is_reported(tmp_path):
    rep = _analyze(_single(tmp_path, _table(stance="certifying")))
    # Skipped exactly like a malformed file: no table, withheld, never cpu-only.
    assert rep.table is None
    assert rep.npu_coverage == COVERAGE_WITHHELD
    assert all(v.status == "unknown" for v in rep.ops)
    # ...and the finding names the file, the JSON pointer and what was found.
    (note,) = _schema_notes(rep)
    assert "skipped" in note
    assert "u85@vela-5.2.0.json" in note
    assert "stance" in note
    assert "'certifying' is not one of ['screening']" in note
    assert note.endswith("This table is not used.")
    assert note.startswith(
        "npu-ops table skipped, it fails npu-ops-v1.schema.json: npu_ops/ethos_u/u85@vela-5.2.0.json: stance:")
    _assert_no_absolute_path(rep, tmp_path)


def test_a_wrong_typed_supported_ops_is_reported_by_the_schema(tmp_path):
    rep = _analyze(_single(tmp_path, _table(supported_ops="CONV_2D")))
    assert rep.table is None and rep.npu_coverage == COVERAGE_WITHHELD
    assert any("is not of type 'array'" in n for n in rep.notes)


def test_a_long_violation_list_is_capped_with_a_count(tmp_path):
    rep = _analyze(_single(tmp_path, _table(supported_ops=[1, 2, 3, 4, 5, 6, 7, 8])))
    (note,) = _schema_notes(rep)
    assert "(+3 more)" in note


def test_an_invalid_table_sorted_before_a_valid_one_with_the_same_variant_is_passed_over(tmp_path):
    root = _root(tmp_path, {
        "a-u85@vela-5.1.0.json": _table(stance="certifying"),
        "u85@vela-5.2.0.json": _table(),
    })
    rep = _analyze(root)
    assert rep.table is not None and Path(rep.table).name == "u85@vela-5.2.0.json"
    assert rep.npu_coverage == "full-eligible"
    (note,) = _schema_notes(rep)
    assert "a-u85@vela-5.1.0.json" in note and "skipped" in note


@pytest.mark.parametrize("checking, broken", [("u85", "u55-u65@vela-5.2.0.json"),
                                              ("u55", "u85@vela-5.2.0.json")])
def test_an_invalid_table_for_a_different_variant_is_neither_validated_nor_mentioned(
        tmp_path, checking, broken):
    good = "u85@vela-5.2.0.json" if checking == "u85" else "u55-u65@vela-5.2.0.json"
    good_variant = "u85" if checking == "u85" else "u55-u65"
    broken_variant = "u55-u65" if checking == "u85" else "u85"
    root = _root(tmp_path, {good: _table(variant=good_variant),
                            broken: _table(variant=broken_variant, stance="certifying")})
    rep = _analyze(root, variant=checking)
    assert Path(rep.table).name == good
    assert rep.npu_coverage == "full-eligible"
    assert _schema_notes(rep) == []


def test_the_single_table_path_without_a_variant_validates_too(tmp_path):
    rep = _analyze(_root(tmp_path, {"onnx-i8@translator-1.12.json": _table(variant="onnx-i8", stance="certifying")}),
                   variant=None)
    assert rep.table is None and rep.npu_coverage == COVERAGE_WITHHELD
    (note,) = _schema_notes(rep)
    assert "onnx-i8@translator-1.12.json" in note


def test_several_tables_and_no_variant_is_undetermined_and_silent_about_the_schema(tmp_path):
    root = _root(tmp_path, {"u85@vela-5.2.0.json": _table(),
                            "u55-u65@vela-5.2.0.json": _table(variant="u55-u65")}, schema=None)
    rep = _analyze(root, variant=None)
    assert rep.table is None
    assert _schema_notes(rep) == []


def test_no_table_covering_the_variant_is_silent_about_a_missing_schema(tmp_path):
    rep = _analyze(_single(tmp_path, _table(variant="u55-u65"), schema=None), variant="u85")
    assert rep.table is None
    assert _schema_notes(rep) == []


def test_an_sdk_without_the_schema_scores_as_before_and_discloses_it_once(tmp_path):
    rep = _analyze(_single(tmp_path, _table(), schema=None))
    assert rep.table is not None and rep.npu_coverage == "full-eligible"
    (note,) = _schema_notes(rep)
    assert note == ("npu_ops/ethos_u: not validated -- no schema at "
                    "schemas/npu-ops-v1.schema.json in this checkout")
    _assert_no_absolute_path(rep, tmp_path)


def test_an_sdk_without_the_schema_keeps_the_isinstance_guards(tmp_path):
    rep = _analyze(_single(tmp_path, _table(supported_ops="CONV_2D"), schema=None))
    assert rep.table is None and rep.npu_coverage == COVERAGE_WITHHELD
    assert len([n for n in rep.notes if "not validated" in n]) == 1


def test_a_corrupt_schema_file_is_not_blamed_on_the_table(tmp_path):
    rep = _analyze(_single(tmp_path, _table(), schema="{ not json"))
    assert rep.table is not None and rep.npu_coverage == "full-eligible"
    (note,) = _schema_notes(rep)
    assert note.startswith("npu_ops/ethos_u: not validated -- the schema at "
                           "schemas/npu-ops-v1.schema.json could not be loaded (")
    assert note.endswith("); the tables are used under the built-in shape checks only")
    assert "skipped" not in note
    _assert_no_absolute_path(rep, tmp_path)


def test_an_oserror_reason_does_not_carry_the_schemas_absolute_path(tmp_path):
    # A directory where the schema file belongs: the OSError's own text names
    # the path, the note must keep only the OS wording.
    root = _single(tmp_path, _table(), schema=None)
    npu_ops_schema_path(root).mkdir(parents=True)
    rep = _analyze(root)
    assert rep.table is not None
    (note,) = _schema_notes(rep)
    assert "could not be loaded (" in note
    _assert_no_absolute_path(rep, tmp_path)


def test_a_schema_that_is_json_but_not_a_json_schema_degrades_without_raising(tmp_path):
    rep = _analyze(_single(tmp_path, _table(), schema='{"type": 5}'))
    assert rep.table is not None and rep.npu_coverage == "full-eligible"
    (note,) = _schema_notes(rep)
    assert "could not be loaded" in note and "skipped" not in note
    _assert_no_absolute_path(rep, tmp_path)


def test_a_broken_schema_is_disclosed_once_even_with_several_matching_candidates(tmp_path):
    root = _root(tmp_path, {"a-u85@vela-5.1.0.json": _table(supported_ops="CONV_2D"),
                            "u85@vela-5.2.0.json": _table()}, schema="{ not json")
    rep = _analyze(root)
    assert len([n for n in rep.notes if "not validated" in n]) == 1


_SDK_SCHEMA = npu_ops_schema_path(SDK / "metadata") if SDK is not None else None
_needs_schema = [
    pytest.mark.skipif(SDK is None, reason="ALP_SDK_ROOT is not set -- no bound SDK to read tables from"),
    pytest.mark.skipif(
        _SDK_SCHEMA is not None and not _SDK_SCHEMA.is_file(),
        reason="the bound alp-sdk ships no metadata/schemas/npu-ops-v1.schema.json "
               "(alp-sdk#1470) -- nothing to validate against"),
]


def _needs(fn):
    for mark in _needs_schema:
        fn = mark(fn)
    return fn


def _real_tables() -> list[Path]:
    tables = sorted((SDK / "metadata" / "npu_ops").glob("*/*.json"))
    if not tables:
        pytest.skip("the bound alp-sdk ships no metadata/npu_ops/ tables")
    return tables


@_needs
def test_every_table_the_bound_sdk_ships_validates_against_its_own_schema():
    for table in _real_tables():
        doc = json.loads(table.read_text(encoding="utf-8"))
        assert validate_document(doc, _SDK_SCHEMA, table) == [], table.name


@_needs
def test_the_fixture_table_is_valid_against_the_real_schema():
    assert validate_document(_table(), _SDK_SCHEMA, "fixture") == []
    # ...and the invalid shapes above are invalid there too, not just here.
    assert validate_document(_table(stance="certifying"), _SDK_SCHEMA, "fixture") != []


@_needs
@pytest.mark.parametrize("backend, src_format, namespace, variant", [
    ("ethos_u", "tflite", "tflite", "u85"),
    ("ethos_u", "tflite", "tflite", "u55"),
    ("drpai", "onnx", "onnx", None),
])
def test_analyze_backend_resolves_the_bound_sdks_real_table_without_a_schema_note(
        backend, src_format, namespace, variant):
    if not (SDK / "metadata" / "npu_ops" / backend).is_dir():
        pytest.skip(f"the bound alp-sdk ships no npu_ops/{backend} tables")
    rep = analyze_backend(
        backend=backend, src_format=src_format,
        ops=[OpDesc(op="Conv" if namespace == "onnx" else "CONV_2D", macs=1, op_namespace=namespace)],
        metadata_root=SDK / "metadata", variant=variant)
    assert rep.table is not None, rep.notes
    assert _schema_notes(rep) == [], rep.notes
