# SPDX-License-Identifier: Apache-2.0
"""`tan.model.perf` -- the reader for alp-sdk's bench-measured perf points
(tier 2 of `docs/superpowers/plans/2026-08-16-model-perf-tier2.md`, Task 4).

RECONCILED AGAINST ALP-SDK'S REAL SCHEMA (tan-cli#1115). Every document this
file writes uses the REAL `model-perf-v1` field model -- top-level `sku`/
`hw_rev`, `model`/`target`/`vela`/`perf`/`capture` blocks -- and the REAL
published path, `metadata/model_perf/<SKU>/<hash>.yaml`, exactly two path
segments below root. See `tan.model.perf`'s own module docstring for where
that schema lives and what changed; see the bottom of this file for the
before/after case count and the retirement reason for each case tan-cli#1115
did not carry forward unchanged.

Almost every case here is a REFUSAL, and that is the point: a perf point is an
exact number a customer sizes hardware from precisely because it says "bench",
so what this reader declines to match matters more than what it matches. The
tier-1 shape (`test_analyze.py`'s own split): synthetic point trees under
`tmp_path` need no alp-sdk at all; the two cases that read alp-sdk's REAL
documents carry the capability marks from `tests/conftest.py` and skip -- with
a reason -- against a pin that does not carry them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tan.model.perf import (
    _toolchain_name,
    coverage_from_placement,
    find_perf_point,
    find_perf_points,
    has_perf_points,
    read_perf_point,
)
from tests.conftest import (
    needs_sdk_model_perf_fixture,
    needs_sdk_model_perf_points,
    sdk_root,
)

SDK = sdk_root()

#: COMPUTED here, never transcribed: a hand-typed 64-hex constant in a test
#: file is indistinguishable from a real model's digest to anyone reading it
#: later, and this one is not one. It is the digest of four literal bytes.
_SHA = hashlib.sha256(b"perf").hexdigest()


def _point_doc(**overrides) -> dict:
    """A minimal, schema-shaped, REAL-looking perf point
    (`metadata/schemas/model-perf-v1.schema.json`). Every test that needs a
    variation overrides one key of it, so a refusal test can never pass
    because its document was malformed in some second, unintended way."""
    doc = {
        "schema_version": 1,
        "sku": "E1M-AEN801",
        "hw_rev": "r2",
        "model": {"name": "person-detect-int8", "src_sha": _SHA, "format": "tflite"},
        "target": {"backend": "ethos_u", "accel_config": "ethos-u85-256",
                    "core": "m55_hp", "compiler_version": "vela 5.1.0"},
        # REQUIRED on an `ethos_u` point by alp-sdk's own validator: a flagless
        # vela picks a DRAM-backed default profile, and the arena figures then
        # describe THAT rather than the module.
        "vela": {"system_config": "Ethos_U85_SRAM_Only", "memory_mode": "Sram_Only"},
        "perf": {"arena_bytes": 74480, "req_sram_kib": 73,
                  "latency_ms": {"mean": 12.4, "p50": 12.0, "p95": 12.9, "runs": 100}},
        "capture": {"date": "2026-08-16", "operator": "alpCaner",
                     "bench_id": "alp-sdk-internal:bench/captures/x.log"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key] = {**doc[key], **value}
        else:
            doc[key] = value
    return doc


def _write_point(meta: Path, doc: dict, *, sku: str | None = None,
                 filename: str = "p.json") -> Path:
    """Write @doc where its own body says it belongs (`model_perf/<sku>/`,
    exactly one segment below the `model_perf/` root -- alp-sdk's real
    `<SKU>/<hash>.yaml` shape), unless a test explicitly misfiles it by
    passing @sku."""
    sku = sku if sku is not None else doc.get("sku", "E1M-AEN801")
    d = meta / "model_perf" / sku
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _find(meta: Path, **overrides):
    query = {"sku": "E1M-AEN801", "backend": "ethos_u", "hw_rev": "r2",
             "accel_config": "ethos-u85-256", "model_sha256": _SHA,
             "toolchain": "vela", "metadata_root": meta}
    query.update(overrides)
    return find_perf_point(**query)


# ---------------------------------------------------------------------------
# Absence is the ORDINARY case, not an error
# ---------------------------------------------------------------------------

def test_a_metadata_root_with_no_model_perf_directory_is_not_an_error(tmp_path):
    # `metadata/model_perf/` exists in NO alp-sdk today -- a reader that
    # treated its absence as exceptional would break `tan model check` on
    # every checkout in existence.
    assert has_perf_points(tmp_path) is False
    assert _find(tmp_path) is None


def test_a_metadata_root_that_does_not_exist_at_all_is_not_an_error(tmp_path):
    missing = tmp_path / "nope"
    assert has_perf_points(missing) is False
    assert _find(missing) is None


def test_has_perf_points_is_true_once_the_tree_exists(tmp_path):
    _write_point(tmp_path, _point_doc())
    assert has_perf_points(tmp_path) is True


# ---------------------------------------------------------------------------
# THE PATH: exactly two segments below root, non-recursive, PINNED
# ---------------------------------------------------------------------------

def test_a_point_at_root_slash_sku_slash_hash_is_found(tmp_path):
    # alp-sdk's REAL layout (tan-cli#1115's second, initially-missed half):
    # `metadata/model_perf/<SKU>/<hash>.yaml` -- no `<target>/` subdirectory.
    path = _write_point(tmp_path, _point_doc(), filename="aaaaaaaaaaaaaaaa.yaml")
    point = _find(tmp_path)
    assert point is not None
    assert point.path == path


def test_a_point_nested_one_level_too_deep_is_never_found(tmp_path):
    """PINS the segment count: `find_perf_points` globs NON-recursively
    (`Path.glob`, never `Path.rglob`) directly under `root/<sku>/`, so a file
    one level deeper -- exactly the `<SKU>/_fixture/<hash>.yaml` evasion
    alp-sdk's own `_collect_model_perf_files` calls out, or any other stray
    subdirectory -- is invisible by construction, not by a count that could
    silently drift back to matching the wrong depth."""
    nested = tmp_path / "model_perf" / "E1M-AEN801" / "extra" / "p.json"
    nested.parent.mkdir(parents=True)
    nested.write_text(json.dumps(_point_doc()), encoding="utf-8")
    assert _find(tmp_path) is None


def test_a_point_directly_under_the_model_perf_root_is_never_found(tmp_path):
    # One segment, not two -- the shape alp-sdk's REAL `tests/fixtures/
    # model_perf/e1m_aen801_ethos_u55_hp.yaml` itself sits at (see the
    # capability-gated section at the bottom of this file): a fixture living
    # one directory shallower than the published convention is exactly as
    # invisible to discovery as one nested too deep.
    shallow = tmp_path / "model_perf" / "p.json"
    shallow.parent.mkdir(parents=True)
    shallow.write_text(json.dumps(_point_doc()), encoding="utf-8")
    assert _find(tmp_path) is None


# ---------------------------------------------------------------------------
# The match: the identity fields (alp-sdk `f724d3e4`), exact
# ---------------------------------------------------------------------------

def test_an_exact_identity_match_returns_the_point(tmp_path):
    path = _write_point(tmp_path, _point_doc())
    point = _find(tmp_path)
    assert point is not None
    assert point.path == path
    assert (point.sku, point.hw_rev, point.core) == ("E1M-AEN801", "r2", "m55_hp")
    assert (point.backend, point.accel_config) == ("ethos_u", "ethos-u85-256")
    assert point.model_name == "person-detect-int8"
    assert point.model_src_sha == _SHA
    assert point.model_format == "tflite"
    assert point.compiler_version == "vela 5.1.0"
    assert point.vela_system_config == "Ethos_U85_SRAM_Only"
    assert point.vela_memory_mode == "Sram_Only"
    assert (point.arena_bytes, point.req_sram_kib) == (74480, 73)
    assert (point.latency_ms_mean, point.latency_ms_p95, point.latency_runs) == (12.4, 12.9, 100)
    assert point.latency_ms_p50 == 12.0
    assert point.capture_bench_id == "alp-sdk-internal:bench/captures/x.log"
    assert point.capture_date == "2026-08-16" and point.capture_operator == "alpCaner"


def test_a_yaml_suffixed_point_is_discovered_not_just_a_json_one(tmp_path):
    # tan-cli#1114 review major: `find_perf_points`'s OWN discovery glob was
    # `*.json`-only, the same never-fires shape tan-cli#1105 fixed for the
    # two test-side predicates -- but on the PRODUCTION lookup path, which is
    # what actually has to find a real alp-sdk-published point
    # (`metadata/model_perf/<sku>/<hash>.yaml`, per
    # `scripts/validate_metadata.py::_collect_model_perf_files`). The content
    # here is still `json.dumps` output (this reader parses that fine too --
    # the module docstring's "ONE PARSER, BOTH FORMATS") -- only the
    # FILENAME suffix is the thing under test.
    path = _write_point(tmp_path, _point_doc(), filename="p.yaml")
    assert path.name == "p.yaml"
    point = _find(tmp_path)
    assert point is not None
    assert point.path == path


@pytest.mark.parametrize("field,value", [
    ("sku", "E1M-AEN701"),
    ("hw_rev", "r1"),                        # a different module revision
    ("core", "m55_he"),                      # a different core on the same die
    ("backend", "drpai"),
    ("accel_config", "ethos-u85-128"),      # same family, different config
    ("toolchain", "dxcom"),
    ("compiler_version", "vela 5.2.0"),
])
def test_a_near_miss_on_any_identity_field_is_not_a_match(tmp_path, field, value):
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, **{field: value}) is None


def test_an_unknown_module_revision_matches_nothing(tmp_path):
    """`hw_rev=None` is the one query field whose absence REFUSES rather than
    widens. An r1 and an r2 measurement of one model on one target are
    different measurements (alp-sdk `f724d3e4`), so a caller who cannot say
    which module it holds is handed nothing -- serving the other revision's
    number is exactly the "exactly measured, describes a different machine"
    failure that put `hw_rev` in the identity."""
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, hw_rev=None) is None
    assert find_perf_points(sku="E1M-AEN801", backend="ethos_u", hw_rev=None,
                             accel_config="ethos-u85-256", model_sha256=_SHA,
                             toolchain="vela", metadata_root=tmp_path) == []


def test_an_unstated_core_or_compiler_version_narrows_nothing_and_invents_nothing(tmp_path):
    # The other two optional query fields WIDEN rather than refuse: there is no
    # local toolchain to read a version off in the tier-2 premise, and nothing
    # ties a declared model to a core on a multi-core module. An unstated field
    # is never guessed -- it just leaves more points standing.
    _write_point(tmp_path, _point_doc())
    point = _find(tmp_path, core=None, compiler_version=None)
    assert point is not None
    assert (point.core, point.compiler_version) == ("m55_hp", "vela 5.1.0")


def test_a_different_model_is_not_a_match_however_close_the_name(tmp_path):
    # `model.name` is IDENTICAL and only the bytes (`model.src_sha`) differ --
    # exactly the case the sha256-as-match-key rule exists for. Re-quantizing
    # a model must retire its old point, not silently keep applying it.
    _write_point(tmp_path, _point_doc())
    other = "f" * 64
    assert _find(tmp_path, model_sha256=other) is None


def test_a_truncated_digest_never_matches_by_prefix(tmp_path):
    # alp-sdk's own identity-hash filename is a readability aid, never a
    # match key: two different models can share a filename prefix.
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, model_sha256=_SHA[:12]) is None


def test_an_uppercase_digest_is_the_same_value_not_a_near_miss(tmp_path):
    # Hex case is a SPELLING of one value. Refusing here would be a false
    # negative, not a strictness.
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, model_sha256=_SHA.upper()) is not None


def test_a_point_measured_with_another_compiler_version_still_matches(tmp_path):
    """DECISION 1, the tier-2 premise half: the exact `compiler_version` is
    not a query field UNLESS a caller states one.

    The customer this tier exists for has no toolchain installed at all, so
    there is no local version for a point to differ from -- narrowing on one
    unconditionally would leave the tier serving nobody. What the point WAS
    measured with is reported instead (`_toolchain_name` only ever narrows on
    the LEADING word, `vela`, not the full string), so the customer can see
    it. The other half of the decision (what happens when a customer DOES
    hold the toolchain and their compile disagrees) is pinned in
    `test_check.py`, where a real compile exists to compare against."""
    _write_point(tmp_path, _point_doc(target={"compiler_version": "vela 4.0.0"}))
    point = _find(tmp_path)
    assert point is not None
    assert point.compiler_version == "vela 4.0.0"
    assert _toolchain_name(point.compiler_version) == "vela"


# ---------------------------------------------------------------------------
# Ambiguity, fixtures, and malformed documents -- all silent None
# ---------------------------------------------------------------------------

def test_two_profiles_of_one_identity_are_surfaced_never_ranked(tmp_path):
    """The multi-match case the profile's exclusion from the key creates.

    One model, one module revision, one core, one toolchain -- captured under
    `Sram_Only` and under `Dedicated_Sram_384KB`. Those are DIFFERENT
    MACHINES, and the collision that forced this design left the DRAM-backed
    one as the arbitrary survivor on a part with no DRAM. `find_perf_points`
    hands back BOTH and ranks nothing; `find_perf_point` refuses. A caller
    holding a silicon fact (the SoC spec's own profile) may choose -- that is
    `test_check.py`'s `_profile_matches_the_part` case."""
    _write_point(tmp_path, _point_doc(), filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_point(tmp_path, _point_doc(vela={"memory_mode": "Dedicated_Sram_384KB"}),
                 filename="b@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    points = find_perf_points(sku="E1M-AEN801", backend="ethos_u", hw_rev="r2",
                               accel_config="ethos-u85-256", model_sha256=_SHA,
                               toolchain="vela", metadata_root=tmp_path)
    assert len(points) == 2
    assert {p.vela_memory_mode for p in points} == {"Sram_Only", "Dedicated_Sram_384KB"}
    assert _find(tmp_path) is None                # ... and nothing was picked


def test_two_compiler_versions_left_unstated_are_refused_not_ranked(tmp_path):
    # The same model re-benched under a second vela version, with the caller
    # unable to state which (no local toolchain). Both are real measurements;
    # picking one would be a preference the data does not express, and a
    # coin-flip behind `confidence: "certain"` is the failure the contract
    # exists to prevent.
    _write_point(tmp_path, _point_doc(), filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_point(tmp_path, _point_doc(target={"compiler_version": "vela 5.2.0"}),
                 filename="b@vela-5.2.0+r2+m55_hp+aaaaaaaaaaaa.json")
    assert _find(tmp_path) is None
    # ... but a caller that CAN state the exact version gets an unambiguous answer.
    assert _find(tmp_path, compiler_version="vela 5.2.0") is not None


def test_a_fixture_marked_path_is_never_consumed(tmp_path):
    """alp-sdk's OWN convention, adopted verbatim -- `_MODEL_PERF_FIXTURE_
    MARKER` is a PATH check on the *published* tree (`validate_metadata.py`),
    checked against the SKU-directory segment and the filename, never a
    document key (tan-cli#1115 -- an earlier, unreconciled version of this
    reader had its OWN separate document-key convention here, `_FIXTURE_KEY`,
    that nothing upstream ever wrote; it is gone, not renamed). The document
    below is otherwise perfectly schema-valid -- refused solely because of
    where it sits."""
    path = _write_point(tmp_path, _point_doc(), filename="aaaaaaaaaaaa._fixture.json")
    assert read_perf_point(path) is None
    assert _find(tmp_path) is None

    # The SKU-directory segment is the other place the marker is checked.
    marked_dir = tmp_path / "model_perf" / "E1M-AEN801_fixture"
    marked_dir.mkdir(parents=True)
    marked_path = marked_dir / "p.json"
    marked_path.write_text(json.dumps(_point_doc(sku="E1M-AEN801_fixture")), encoding="utf-8")
    assert read_perf_point(marked_path) is None


@pytest.mark.parametrize("body", [
    b"{not json", b'["a list"]', b'"a string"', b"null",
    # tan-cli#1114 review blocker 1: `path.read_text(encoding="utf-8")`
    # raises `UnicodeDecodeError` on this BEFORE `yaml.safe_load` ever runs
    # -- a `ValueError` subclass, not a `yaml.YAMLError`, so the reader's
    # except clause must catch both, not just the latter.
    b"schema_version: 1\nnote: caf\xe9 -- invalid utf-8 mid-value\n",
])
def test_a_malformed_document_is_not_a_match_and_never_raises(tmp_path, body):
    d = tmp_path / "model_perf" / "E1M-AEN801"
    d.mkdir(parents=True)
    path = d / "p.json"
    path.write_bytes(body)
    assert read_perf_point(path) is None
    assert _find(tmp_path) is None


def test_an_unrecognised_schema_version_is_refused(tmp_path):
    # A future alp-sdk v2 might publish under new field names this reader has
    # never seen; refused exactly like any other unrecognised document rather
    # than half-parsed under v1's field mapping.
    _write_point(tmp_path, _point_doc(schema_version=2))
    assert _find(tmp_path) is None


def test_an_unreadable_file_is_not_a_match_and_never_raises(tmp_path):
    # A directory where a file should be: `read_text` raises OSError, and a
    # consumer must stay quiet where the PRODUCING repository's gate is loud.
    d = tmp_path / "model_perf" / "E1M-AEN801" / "p.json"
    d.mkdir(parents=True)
    assert read_perf_point(d) is None
    assert _find(tmp_path) is None


def _real_yaml_point(quote_scalars: bool = True) -> str:
    """The `_point_doc()` field model written as REAL YAML source -- block
    mappings, a comment, no JSON braces -- not `json.dumps` output that
    merely happens to parse as YAML too. Distinguishes a reader that actually
    parses YAML from one that only accidentally tolerates JSON's syntax
    overlap with it (tan-cli#1114 review blocker 2).

    @quote_scalars controls `capture.date` / `target.compiler_version`:
    quoted (the default, and what alp-sdk's own fixture does) stays a `str`;
    left unquoted, PyYAML's YAML-1.1 implicit typing turns them into a
    `datetime.date` / `float` -- see the module docstring's "YAML 1.1 TYPING
    IS ALREADY GUARDED" note.
    """
    date = '"2026-08-16"' if quote_scalars else "2026-08-16"
    version = '"vela 5.1.0"' if quote_scalars else "vela 5.1"
    return f"""\
# A real bench-measured point, hand-authored (this is what tan's reader must
# actually parse -- not the `json.dumps` shape every OTHER test in this file
# writes).
schema_version: 1
sku: E1M-AEN801
hw_rev: r2
model:
  name: person-detect-int8
  src_sha: {_SHA}
  format: tflite
target:
  backend: ethos_u
  accel_config: ethos-u85-256
  core: m55_hp
  compiler_version: {version}
vela:
  system_config: Ethos_U85_SRAM_Only
  memory_mode: Sram_Only
perf:
  arena_bytes: 74480
  req_sram_kib: 73
  latency_ms:
    mean: 12.4
    p50: 12.0
    p95: 12.9
    runs: 100
capture:
  date: {date}
  operator: alpCaner
  bench_id: "alp-sdk-internal:bench/captures/x.log"
"""


def test_a_real_yaml_syntax_document_is_read_not_just_json(tmp_path):
    # tan-cli#1114 review blocker 2: the fix that issue was FOR is "the reader
    # can parse YAML", and no test asserted that against real YAML syntax --
    # every OTHER document in this file is `json.dumps` output, which a
    # reader that still only accepted JSON would pass just as well. This one
    # is real YAML source (block mappings, a `#` comment): unparseable by a
    # `json.loads`-based reader (a `JSONDecodeError`, caught, `None`) and read
    # correctly by this one.
    d = tmp_path / "model_perf" / "E1M-AEN801"
    d.mkdir(parents=True)
    path = d / "p.yaml"
    path.write_text(_real_yaml_point(), encoding="utf-8")
    point = read_perf_point(path)
    assert point is not None, "a real YAML document must be readable"
    assert point.sku == "E1M-AEN801"
    assert point.model_src_sha == _SHA
    assert point.latency_ms_mean == 12.4
    assert _find(tmp_path) is not None


def test_an_unquoted_scalar_silently_drops_the_whole_point(tmp_path):
    # THE ASYMMETRY THAT COMES BACK THE OTHER WAY (module docstring): YAML
    # 1.1 implicit-types an unquoted `date`/`compiler_version` scalar away
    # from `str` (`datetime.date` / `float`), and `_text()` is `str`-strict,
    # so the WHOLE point is refused -- the same `None` as a missing field,
    # not a crash and not a coercion. Pinned deliberately, not merely
    # "known": this reader's whole design is refuse-over-guess (module
    # docstring, throughout), and alp-sdk's own schema validator already
    # requires `"type": "string"` on both fields, so a document that reaches
    # this reader unquoted was never going to be a valid published point
    # either.
    d = tmp_path / "model_perf" / "E1M-AEN801"
    d.mkdir(parents=True)
    path = d / "p.yaml"
    path.write_text(_real_yaml_point(quote_scalars=False), encoding="utf-8")
    assert read_perf_point(path) is None
    assert _find(tmp_path) is None


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("sku"),
    lambda d: d.pop("hw_rev"),
    lambda d: d["target"].pop("core"),
    lambda d: d["target"].pop("backend"),
    lambda d: d["target"].pop("accel_config"),
    lambda d: d["target"].pop("compiler_version"),
    lambda d: d["model"].pop("src_sha"),
    lambda d: d["model"].pop("name"),
    lambda d: d["model"].pop("format"),
    lambda d: d["capture"].pop("date"),
    lambda d: d["capture"].pop("operator"),
    lambda d: d["capture"].pop("bench_id"),
], ids=["sku", "hw_rev", "target.core", "target.backend", "target.accel_config",
        "target.compiler_version", "model.src_sha", "model.name", "model.format",
        "capture.date", "capture.operator", "capture.bench_id"])
def test_a_point_missing_any_identity_field_is_refused(tmp_path, mutate):
    doc = _point_doc()
    mutate(doc)
    _write_point(tmp_path, doc, sku="E1M-AEN801")
    assert _find(tmp_path) is None


def test_a_point_whose_body_disagrees_with_its_path_is_not_found(tmp_path):
    # The path is an INDEX; the body is the truth. alp-sdk's validator refuses
    # to publish a misfiled point, and this reader does not have to trust it.
    _write_point(tmp_path, _point_doc(sku="E1M-AEN701"), sku="E1M-AEN801")
    assert _find(tmp_path) is None


def test_a_missing_or_malformed_perf_block_still_reads_the_identity(tmp_path):
    """UNLIKE the identity blocks (`model`/`target`/`capture`, all required
    for a document to become a `PerfPoint` at all), a missing or wrong-typed
    `perf` block does NOT refuse the whole point -- it is not part of the
    match key, and this reader stays defensive rather than re-implementing
    alp-sdk's own schema-level `perf` requirement. The point is still found;
    every `perf.*`-derived field just reads `None`, the same "not measured"
    answer an omitted figure inside a present `perf` block gives.

    RETIRED PREMISE, tan-cli#1115: a pre-reconciliation version of this test
    (`test_a_measured_block_that_is_not_an_object_is_refused`) asserted the
    OPPOSITE -- that a malformed `measured` block refused the WHOLE point --
    against the old, fictional `measured` block. `perf` is not treated the
    same way; see this test's own docstring for why."""
    doc = _point_doc()
    doc["perf"] = []
    _write_point(tmp_path, doc)
    point = _find(tmp_path)
    assert point is not None
    assert (point.arena_bytes, point.req_sram_kib) == (None, None)
    assert point.latency_ms_mean is None


# ---------------------------------------------------------------------------
# `None` means "not measured", never zero
# ---------------------------------------------------------------------------

def _only_perf(**perf) -> dict:
    """A point whose `perf` block is EXACTLY @perf -- `_point_doc`'s
    per-block merge is the wrong tool here, since these cases are about what
    happens when a figure is genuinely absent."""
    doc = _point_doc()
    doc["perf"] = dict(perf)
    return doc


def test_an_omitted_figure_reads_as_none_not_zero(tmp_path):
    # The schema OMITS a figure nobody measured rather than zero-filling it,
    # so a zero here is a MEASURED zero and the two must not collapse.
    _write_point(tmp_path, _only_perf(arena_bytes=32))
    point = _find(tmp_path)
    assert point is not None
    assert point.req_sram_kib is None
    assert point.latency_ms_mean is None and point.latency_runs is None


def test_a_measured_zero_survives_as_a_zero(tmp_path):
    _write_point(tmp_path, _only_perf(arena_bytes=0, req_sram_kib=0))
    point = _find(tmp_path)
    assert point is not None
    assert point.arena_bytes == 0            # measured, not missing
    assert point.req_sram_kib == 0


def test_a_measured_zero_latency_survives_as_a_zero_too(tmp_path):
    # tan-cli#791 review NIT (a): `_millis` used to map a measured `0` to
    # `None`, contradicting `PerfPoint`'s own docstring ("a zero here is a
    # MEASURED zero") -- the same rule `_count` (the sibling used for
    # `arena_bytes` above) already honoured.
    _write_point(tmp_path, _only_perf(
        arena_bytes=32, latency_ms={"mean": 0.0, "p95": 0.0, "runs": 1}))
    point = _find(tmp_path)
    assert point is not None
    assert point.latency_ms_mean == 0.0      # measured, not missing
    assert point.latency_ms_p95 == 0.0


def test_a_boolean_is_not_a_count(tmp_path):
    # `bool` is an `int` subclass in Python; `True` is not one byte.
    _write_point(tmp_path, _only_perf(arena_bytes=True, req_sram_kib=1))
    point = _find(tmp_path)
    assert point is not None
    assert point.arena_bytes is None


# ---------------------------------------------------------------------------
# A backend with no accel config, and no vela profile
# ---------------------------------------------------------------------------

def test_a_backend_with_no_memory_profile_reads_none_not_an_empty_string(tmp_path):
    doc = _point_doc(target={"backend": "drpai", "accel_config": "",
                               "compiler_version": "translator 1.12"})
    del doc["vela"]                # a DRP-AI point carries no vela block at all
    _write_point(tmp_path, doc)
    point = _find(tmp_path, backend="drpai", accel_config="", toolchain="translator")
    assert point is not None
    assert point.vela_system_config is None
    assert point.vela_memory_mode is None


def test_an_absent_accel_config_key_is_not_the_same_as_an_empty_one(tmp_path):
    # REQUIRED-AND-MAY-BE-EMPTY: `""` is a backend that HAS none, an absent
    # key is a point that failed to record one, and the whole value of a perf
    # point is that nothing about its identity is left to inference.
    doc = _point_doc(target={"backend": "drpai", "compiler_version": "translator 1.12"})
    del doc["target"]["accel_config"]
    del doc["vela"]
    _write_point(tmp_path, doc)
    assert _find(tmp_path, backend="drpai", accel_config="",
                 toolchain="translator") is None


# ---------------------------------------------------------------------------
# coverage_from_placement -- the ONE function in tan that may say "fits"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("npu,cpu,expected", [
    (44, 0, "fits"),          # 100% NPU -- the only shape that earns the word
    (43, 1, "partial"),
    (1, 43, "partial"),
    (0, 44, "cpu-only"),
])
def test_coverage_from_a_real_placement(npu, cpu, expected):
    assert coverage_from_placement(npu, cpu) == expected


@pytest.mark.parametrize("npu,cpu", [(None, 0), (44, None), (None, None), (0, 0)])
def test_an_unreadable_placement_yields_no_coverage_at_all(npu, cpu):
    # `(0, 0)` is an unreadable summary, NOT an all-NPU one: "nothing said it
    # fell back to the CPU" is not evidence that it did not.
    assert coverage_from_placement(npu, cpu) is None


# ---------------------------------------------------------------------------
# alp-sdk's REAL documents (capability-gated -- see tests/conftest.py)
# ---------------------------------------------------------------------------

_META = SDK / "metadata" if SDK is not None else None

pytestmark_real_sdk = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read alp-sdk's own committed perf-point "
           "documents.",
)


@pytestmark_real_sdk
@needs_sdk_model_perf_fixture
def test_alp_sdks_own_synthetic_fixture_point_is_read_by_its_real_fields():
    """THE tan-cli#1115 PROOF: the actual illustrative document alp-sdk ships
    under `tests/fixtures/model_perf/` (alp-sdk `9b466018`, "feat(metadata):
    tier-2 model-perf perf-point contract (Refs #1520) (#1884)") -- a
    schema-valid, semantic-check-clean point whose figures are explicitly NOT
    a real bench capture (its own header comment says so), keyed to E1M-AEN801's
    REAL ethos-u55-256 target and its REAL paired m55_hp core (not a synthetic
    SoM). Published as YAML, per `model-perf-v1.schema.json`'s own `$id`/title.

    BEFORE this reconciliation, this same test (named `..._is_refused_by_
    this_reader`) proved the reader refused this document -- but not through
    alp-sdk's real fixture-refusal mechanism (a PATH convention on the
    *published* tree that this file, sitting under `tests/fixtures/`, never
    triggers at all: neither `path.parent.name` ("model_perf") nor
    `path.name` ("e1m_aen801_ethos_u55_hp.yaml") contains alp-sdk's
    `_fixture` marker). It was refused because the reader's field model
    (`stance`/`measured_on`/`measured`) did not match this document's real
    shape (`sku`/`hw_rev`/`target`/`vela`/`perf`/`capture`) AT ALL -- a green
    assertion that proved less than it appeared to, exactly the failure mode
    tan-cli#1115 exists to fix. Now the reader understands the real shape, so
    this file asserts on the fixture's ACTUAL, real field VALUES -- not
    merely that parsing did not raise."""
    fixtures = sorted((SDK / "tests" / "fixtures" / "model_perf").rglob("*.yaml"))
    assert fixtures, "the capability mark should have skipped this"
    assert len(fixtures) == 1
    path = fixtures[0]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} did not parse to a mapping"

    point = read_perf_point(path)
    assert point is not None, (
        f"{path}: alp-sdk's own real, schema-valid fixture must be readable "
        f"by its actual fields"
    )
    assert point.sku == "E1M-AEN801"
    assert point.hw_rev == "r1"
    assert point.backend == "ethos_u"
    assert point.accel_config == "ethos-u55-256"
    assert point.core == "m55_hp"
    assert point.compiler_version == "vela 4.1.0"
    assert point.model_name == "person_detect_int8_v1"
    assert point.model_src_sha == doc["model"]["src_sha"]
    assert point.model_format == "tflite"
    assert point.vela_system_config == "Ethos_U55_High_End_Embedded"
    assert point.vela_memory_mode == "Sram_Only"
    assert point.arena_bytes == 300000
    assert point.req_sram_kib == 384
    assert point.latency_ms_mean == 12.4
    assert point.latency_ms_p50 == 12.1
    assert point.latency_ms_p95 == 15.8
    assert point.latency_ms_stdev == 1.9
    assert point.latency_runs == 200
    assert point.capture_date == "2026-08-20"
    assert point.capture_operator == "alpCaner"
    assert point.capture_bench_id == "e1m-aen-evk-01"
    assert point.capture_notes == "illustrative fixture body -- not a real bench capture"

    # This SAME fixture sits ONE segment below `tests/fixtures/model_perf/`
    # (no `<SKU>/` subdirectory) -- unlike the published-tree convention,
    # so it is READABLE directly but not DISCOVERABLE by `find_perf_points`
    # against that root (see `test_a_point_directly_under_the_model_perf_
    # root_is_never_found` above for the synthetic proof of the same shape).
    assert path.parent.name == "model_perf"


@pytestmark_real_sdk
@needs_sdk_model_perf_points
def test_every_published_point_round_trips_through_this_reader():
    """Whatever the first bench campaign publishes, this reader must be able
    to read ALL of it -- and each point must be findable by its own identity,
    which is what proves the path convention and the match rule agree with
    what alp-sdk actually writes."""
    published = sorted((_META / "model_perf").rglob("*.yaml"))
    assert published, "the capability mark should have skipped this"
    for path in published:
        point = read_perf_point(path)
        assert point is not None, f"{path} is published but unreadable"
        found = find_perf_point(
            sku=point.sku, backend=point.backend,
            accel_config=point.accel_config, model_sha256=point.model_src_sha,
            toolchain=_toolchain_name(point.compiler_version),
            hw_rev=point.hw_rev, metadata_root=_META)
        assert found is not None and found.path == path, (
            f"{path}: published but not findable by its own identity -- the "
            f"path convention and the match rule disagree"
        )


# ---------------------------------------------------------------------------
# BEFORE/AFTER CASE COUNT (tan-cli#1115 report)
#
# 33 `def test_*` before this reconciliation; 36 after. Every one of the 33 is
# accounted for below -- none silently dropped:
#
#   * 30 MIGRATED 1:1 -- same assertion, updated onto the real field names/
#     blocks and the real two-segment path (most of this file: identity
#     matching, near-miss/ambiguity/malformed-document refusals,
#     omitted-vs-zero, YAML parsing, `coverage_from_placement`). This
#     includes `test_a_fixture_bannered_point_is_never_consumed` ->
#     `test_a_fixture_marked_path_is_never_consumed` (the MECHANISM changed
#     from a document key to alp-sdk's real path convention, but the
#     assertion -- "a fixture is never consumed" -- did not), and
#     `test_alp_sdks_own_synthetic_fixture_point_is_refused_by_this_reader`
#     -> `..._is_read_by_its_real_fields` (the assertion direction flipped
#     from refused to accepted -- see that test's own docstring for why that
#     is the tan-cli#1115 fix working as intended, not a weakened proof).
#
#   * 2 RETIRED and replaced by a new test with a DIFFERENT invariant, not a
#     like-for-like migration:
#       - `test_a_measured_block_that_is_not_an_object_is_refused` (asserted
#         a malformed `measured` block refused the WHOLE point) ->
#         `test_a_missing_or_malformed_perf_block_still_reads_the_identity`
#         (asserts the OPPOSITE for the real `perf` block -- see that test's
#         own docstring: `perf` is not part of the identity/match key the
#         way `model`/`target`/`capture` are, so this reader stays defensive
#         rather than re-implementing alp-sdk's schema-level requirement).
#       - `test_a_backend_with_no_accel_config_is_keyed_by_backend_name`
#         (asserted `target_dir_name(...)`, a helper tan-cli#1115 retired --
#         the real path has no `<target>/` segment for it to name) ->
#         `test_a_backend_with_no_memory_profile_reads_none_not_an_empty_
#         string` (keeps only the still-true half of the old assertion: a
#         backend with no vela profile reads `None`, not `""`).
#
#   * 1 RETIRED OUTRIGHT, no replacement of any kind:
#       - `test_a_point_that_is_not_bench_measured_is_refused` -- `stance`
#         has no real-schema counterpart at all (see `tan.model.perf`'s
#         module docstring, "DEAD FIELDS"); publication under
#         `metadata/model_perf/` (vs. `tests/fixtures/model_perf/`) IS the
#         bench-measured signal, so there is no in-document flag left to
#         test refusing.
#
#   * 4 GENUINELY NEW, no prior counterpart: the two-segment-path pins
#     (`test_a_point_at_root_slash_sku_slash_hash_is_found`,
#     `test_a_point_nested_one_level_too_deep_is_never_found`,
#     `test_a_point_directly_under_the_model_perf_root_is_never_found` -- the
#     path-reconciliation half of tan-cli#1115, absent from the pre-widened
#     issue text entirely) and `test_an_unrecognised_schema_version_is_
#     refused` (the new `schema_version` gate this reconciliation added).
#
# 30 migrated + 2 replaced + 1 retired-outright = 33 old cases, all
# accounted for. 30 migrated + 2 replacements + 4 new = 36 new cases.
# ---------------------------------------------------------------------------
