# SPDX-License-Identifier: Apache-2.0
"""`tan.core.metadata_schema` -- the ONE shared entry point tan-cli#964 adds
for `soc-spec-v1.schema.json`/`som-preset-v1.schema.json` validation on the
READ path. Unit-level coverage of the module itself, independent of any one
caller (`planner/loader.py`, `presets_cmd.py`, `size_cmd.py` each have their
own behavioural tests for how THEY use it).
"""

from __future__ import annotations

import json

import pytest

from tan.core.metadata_schema import (
    schema_errors,
    soc_spec_schema_path,
    som_preset_schema_path,
    validate_document,
)

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["type"],
    "properties": {"type": {"type": "string"}},
}


def _write(path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_soc_spec_and_som_preset_schema_path_join_metadata_root(tmp_path):
    assert soc_spec_schema_path(tmp_path) == tmp_path / "schemas" / "soc-spec-v1.schema.json"
    assert som_preset_schema_path(tmp_path) == tmp_path / "schemas" / "som-preset-v1.schema.json"


def test_schema_errors_with_no_source_matches_new_som_cmds_original_shape(tmp_path):
    """`source=None` (the default) is what `new_som_cmd`'s generated-skeleton
    self-check relies on -- the exact `pointer: message` shape it always had,
    with no file name prefixed (there is no file yet at that point)."""
    schema_path = tmp_path / "schema.json"
    _write(schema_path, _SCHEMA)

    errors = schema_errors({"type": 7}, schema_path)

    assert errors == ["type: 7 is not of type 'string'"]


def test_schema_errors_with_a_source_prefixes_every_message(tmp_path):
    schema_path = tmp_path / "schema.json"
    _write(schema_path, _SCHEMA)
    doc_path = tmp_path / "socs" / "part.json"

    errors = schema_errors({"type": 7}, schema_path, source=doc_path)

    assert errors == [f"{doc_path}: type: 7 is not of type 'string'"]


def test_schema_errors_sorted_and_empty_on_a_clean_document(tmp_path):
    schema_path = tmp_path / "schema.json"
    _write(schema_path, _SCHEMA)

    assert schema_errors({"type": "cortex-m33"}, schema_path) == []


def test_schema_errors_raises_when_the_schema_file_is_unreadable(tmp_path):
    """The RAISING half -- what `new_som_cmd`'s two call sites still catch
    (`except (OSError, UnicodeDecodeError)`) to report `could not read
    <schema_path>` distinctly from a document violation. `validate_document`
    below is the non-raising variant read-path callers use instead."""
    missing = tmp_path / "does-not-exist.json"

    with pytest.raises(OSError):
        schema_errors({}, missing)


def test_validate_document_reports_a_violation_with_the_source_prefixed(tmp_path):
    schema_path = tmp_path / "schema.json"
    _write(schema_path, _SCHEMA)
    doc_path = tmp_path / "socs" / "part.json"

    errors = validate_document({"type": 7}, schema_path, doc_path)

    assert errors == [f"{doc_path}: type: 7 is not of type 'string'"]


def test_validate_document_is_empty_on_a_clean_document(tmp_path):
    schema_path = tmp_path / "schema.json"
    _write(schema_path, _SCHEMA)

    assert validate_document({"type": "cortex-m33"}, schema_path, tmp_path / "d.json") == []


def test_validate_document_silently_skips_a_missing_schema_file(tmp_path):
    """tan-cli#964's own verification requirement: must not fire on a
    checkout that is simply missing this schema (an SDK predating it, or a
    synthetic/partial metadata root) -- matches `presets_cmd._os_choices`'s
    existing precedent for a missing `board.schema.json`. `[]`, not a
    synthetic 'could not validate' message: there is nothing anomalous about
    a checkout that never shipped this file.

    Mutation-proven: removing the `if not Path(schema_path).is_file(): return
    []` guard (byte copy restored after, never `git checkout`) turns this RED
    -- but NOT because `FileNotFoundError` escapes uncaught (tan-cli#964
    review, minor 10, correcting this docstring's own earlier claim):
    `FileNotFoundError` IS an `OSError`, so `schema_errors`'s call falls
    through to the SAME `except (OSError, UnicodeDecodeError, ValueError)`
    the corrupt-schema case below already relies on, and the mutant
    `validate_document` returns ONE synthetic message (`<doc>: could not
    validate against <schema>: [Errno 2] No such file or directory: ...`)
    rather than raising. The test still goes RED -- `[]` (silent skip,
    correct) vs. one message (a false 'could not validate' report on a
    checkout that never shipped this schema) -- which is exactly the
    distinction this test exists to pin; only the STATED mechanism (an
    uncaught exception) was wrong, not the mutation result. Restoring the
    guard turns it GREEN.
    """
    missing = tmp_path / "schemas" / "soc-spec-v1.schema.json"

    assert validate_document({"type": 7}, missing, tmp_path / "d.json") == []


def test_validate_document_reports_one_message_for_a_corrupt_schema_file(tmp_path):
    """The OTHER half of the same requirement: a schema file that EXISTS but
    cannot be parsed (truncated, not JSON) is a genuine anomaly on an
    otherwise-real checkout, not a legitimate absence -- one synthetic
    message, never a silent `[]`.
    """
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{not valid json", encoding="utf-8")
    doc_path = tmp_path / "d.json"

    errors = validate_document({"type": 7}, schema_path, doc_path)

    assert len(errors) == 1
    assert errors[0].startswith(f"{doc_path}: could not validate against {schema_path}: ")


# ---------------------------------------------------------------------------
# Caching (tan-cli#964 review, minor 8)
# ---------------------------------------------------------------------------


def test_schema_errors_reuses_a_cached_validator_across_calls(tmp_path):
    """The review's perf finding: `schema_errors` used to re-read + re-parse
    + re-construct a `Draft202012Validator` on EVERY call, scaling linearly
    with the number of documents validated against one schema (measured:
    +0.08s at 11 SoMs, +0.46s at 111). `_cached_validator_by_stat` is keyed
    on `(path, mtime_ns, size)`, so two calls against the SAME unchanged
    schema file must be a cache HIT the second time, not a second
    read+parse+construct.

    Mutation-proven: removing the `@functools.lru_cache(...)` decorator on
    `_cached_validator_by_stat` (byte copy restored after, never `git
    checkout`) turns this test's `hits` assertion red (stays 0); restoring
    turns it green.
    """
    from tan.core import metadata_schema as ms

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "properties": {"x": {"type": "string"}}}),
        encoding="utf-8",
    )
    ms._cached_validator_by_stat.cache_clear()

    schema_errors({"x": "ok"}, schema_path)
    before = ms._cached_validator_by_stat.cache_info()
    schema_errors({"x": "ok"}, schema_path)
    after = ms._cached_validator_by_stat.cache_info()

    assert before.hits == 0  # the first call is always a miss (nothing cached yet)
    assert after.hits == before.hits + 1


def test_an_edited_schema_file_invalidates_the_cache(tmp_path):
    """The correctness half of the same cache: a schema file EDITED between
    two calls (its `mtime`/`size` both change) must not serve the STALE
    compiled validator -- a `tan new-som` run followed by a `tan presets` in
    the same interpreter must see the new schema, not the old one."""
    from tan.core import metadata_schema as ms

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "properties": {"x": {"type": "string"}}}),
        encoding="utf-8",
    )
    ms._cached_validator_by_stat.cache_clear()

    # `x: 7` is schema-invalid under the FIRST schema (x must be a string).
    first = schema_errors({"x": 7}, schema_path)
    assert first != []

    # Rewrite the schema so `x` may be ANYTHING -- a real edit, not a no-op
    # write, so `mtime_ns`/`size` both actually change.
    schema_path.write_text(
        json.dumps({"type": "object", "properties": {"x": {}}, "extra": "widen the size"}),
        encoding="utf-8",
    )
    second = schema_errors({"x": 7}, schema_path)

    assert second == []
