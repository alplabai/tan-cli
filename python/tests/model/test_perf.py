# SPDX-License-Identifier: Apache-2.0
"""`tan.model.perf` -- the reader for alp-sdk's bench-measured perf points
(tier 2 of `docs/superpowers/plans/2026-08-16-model-perf-tier2.md`, Task 4).

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

from tan.model.perf import (
    coverage_from_placement,
    find_perf_point,
    find_perf_points,
    has_perf_points,
    read_perf_point,
    target_dir_name,
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
    """A minimal, schema-shaped, REAL-looking perf point. Every test that needs
    a variation overrides one key of it, so a refusal test can never pass
    because its document was malformed in some second, unintended way."""
    doc = {
        "stance": "bench-measured",
        "measured_on": {"sku": "E1M-AEN801", "hw_rev": "r2", "core": "m55_hp",
                        "backend": "ethos_u", "accel_config": "ethos-u85-256"},
        "model": {"slug": "person-detect-int8", "sha256": _SHA, "size_bytes": 300568,
                   "source": "tests/fixtures/models/person_detect_int8.tflite"},
        # REQUIRED on an `ethos_u` point by alp-sdk's own validator: a flagless
        # vela picks a DRAM-backed default profile, and the arena figures then
        # describe THAT rather than the module.
        "toolchain": {"name": "vela", "version": "5.1.0",
                       "system_config": "Ethos_U85_SRAM_Only",
                       "memory_mode": "Sram_Only"},
        "measured": {"npu_ops": 44, "cpu_ops": 0, "arena_bytes": 74480,
                      "req_sram_kib": 73, "latency_ms_mean": 12.4,
                      "latency_ms_p95": 12.9, "runs": 100},
        "capture": {"date": "2026-08-16", "operator": "alpCaner",
                     "reference": "alp-sdk-internal:bench/captures/x.log"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key] = {**doc[key], **value}
        else:
            doc[key] = value
    return doc


def _write_point(meta: Path, doc: dict, *, sku: str | None = None,
                 target: str | None = None, filename: str = "p.json") -> Path:
    """Write @doc where its own body says it belongs, unless a test explicitly
    misfiles it by passing @sku/@target."""
    on = doc.get("measured_on", {})
    sku = sku if sku is not None else on.get("sku", "E1M-AEN801")
    target = target if target is not None else target_dir_name(
        on.get("backend", ""), on.get("accel_config", ""))
    d = meta / "model_perf" / sku / target
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
# The match: the eight identity fields (alp-sdk `f724d3e4`), exact
# ---------------------------------------------------------------------------

def test_an_exact_eight_field_match_returns_the_point(tmp_path):
    path = _write_point(tmp_path, _point_doc())
    point = _find(tmp_path)
    assert point is not None
    assert point.path == path
    assert (point.sku, point.hw_rev, point.core) == ("E1M-AEN801", "r2", "m55_hp")
    assert (point.backend, point.accel_config) == ("ethos_u", "ethos-u85-256")
    assert point.model_source == "tests/fixtures/models/person_detect_int8.tflite"
    assert point.model_sha256 == _SHA
    assert (point.toolchain, point.toolchain_version) == ("vela", "5.1.0")
    assert point.toolchain_system_config == "Ethos_U85_SRAM_Only"
    assert point.toolchain_memory_mode == "Sram_Only"
    assert (point.npu_ops, point.cpu_ops) == (44, 0)
    assert (point.arena_bytes, point.req_sram_kib) == (74480, 73)
    assert (point.latency_ms_mean, point.latency_ms_p95, point.runs) == (12.4, 12.9, 100)
    assert point.capture_reference == "alp-sdk-internal:bench/captures/x.log"


@pytest.mark.parametrize("field,value", [
    ("sku", "E1M-AEN701"),
    ("hw_rev", "r1"),                        # a different module revision
    ("core", "m55_he"),                      # a different core on the same die
    ("backend", "drpai"),
    ("accel_config", "ethos-u85-128"),      # same family, different config
    ("toolchain", "dxcom"),
    ("toolchain_version", "5.2.0"),
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


def test_an_unstated_core_or_version_narrows_nothing_and_invents_nothing(tmp_path):
    # The other two optional query fields WIDEN rather than refuse: there is no
    # local toolchain to read a version off in the tier-2 premise, and nothing
    # ties a declared model to a core on a multi-core module. An unstated field
    # is never guessed -- it just leaves more points standing.
    _write_point(tmp_path, _point_doc())
    point = _find(tmp_path, core=None, toolchain_version=None)
    assert point is not None
    assert (point.core, point.toolchain_version) == ("m55_hp", "5.1.0")


def test_a_different_model_is_not_a_match_however_close_the_slug(tmp_path):
    # The slug is IDENTICAL and only the bytes differ -- exactly the case the
    # sha256-as-match-key rule exists for. Re-quantizing a model must retire
    # its old point, not silently keep applying it.
    _write_point(tmp_path, _point_doc())
    other = "f" * 64
    assert _find(tmp_path, model_sha256=other) is None


def test_a_truncated_digest_never_matches_by_prefix(tmp_path):
    # The 12-character prefix in the FILENAME is a readability aid, never a
    # match key: two different models can share it.
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, model_sha256=_SHA[:12]) is None


def test_an_uppercase_digest_is_the_same_value_not_a_near_miss(tmp_path):
    # Hex case is a SPELLING of one value. Refusing here would be a false
    # negative, not a strictness.
    _write_point(tmp_path, _point_doc())
    assert _find(tmp_path, model_sha256=_SHA.upper()) is not None


def test_a_point_measured_with_another_toolchain_version_still_matches(tmp_path):
    """DECISION 1, the tier-2 premise half: the toolchain VERSION is not a
    query field.

    The customer this tier exists for has no toolchain installed at all, so
    there is no local version for a point to differ from -- narrowing on one
    would leave the tier serving nobody. What the point WAS measured with is
    reported instead, so the customer can see it. The other half of the
    decision (what happens when a customer DOES hold the toolchain and their
    compile disagrees) is pinned in `test_check.py`, where a real compile
    exists to compare against."""
    _write_point(tmp_path, _point_doc(toolchain={"version": "4.0.0"}))
    point = _find(tmp_path)
    assert point is not None
    assert point.toolchain_version == "4.0.0"


# ---------------------------------------------------------------------------
# Ambiguity, fixtures, and malformed documents -- all silent None
# ---------------------------------------------------------------------------

def test_two_profiles_of_one_identity_are_surfaced_never_ranked(tmp_path):
    """The multi-match case the profile's exclusion from the key creates.

    One model, one module revision, one core, one toolchain version -- captured
    under `Sram_Only` and under `Dedicated_Sram_384KB`. Those are DIFFERENT
    MACHINES, and the collision that forced this design left the DRAM-backed
    one as the arbitrary survivor on a part with no DRAM. `find_perf_points`
    hands back BOTH and ranks nothing; `find_perf_point` refuses. A caller
    holding a silicon fact (the SoC spec's own profile) may choose -- that is
    `test_check.py`'s `_profile_matches_the_part` case."""
    _write_point(tmp_path, _point_doc(), filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_point(tmp_path, _point_doc(toolchain={"memory_mode": "Dedicated_Sram_384KB"}),
                 filename="b@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    points = find_perf_points(sku="E1M-AEN801", backend="ethos_u", hw_rev="r2",
                               accel_config="ethos-u85-256", model_sha256=_SHA,
                               toolchain="vela", metadata_root=tmp_path)
    assert len(points) == 2
    assert {p.toolchain_memory_mode for p in points} == {"Sram_Only", "Dedicated_Sram_384KB"}
    assert _find(tmp_path) is None                # ... and nothing was picked


def test_two_toolchain_versions_left_unstated_are_refused_not_ranked(tmp_path):
    # The same model re-benched under a second vela version, with the caller
    # unable to state which (no local toolchain). Both are real measurements;
    # picking one would be a preference the data does not express, and a
    # coin-flip behind `confidence: "certain"` is the failure the contract
    # exists to prevent.
    _write_point(tmp_path, _point_doc(), filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_point(tmp_path, _point_doc(toolchain={"version": "5.2.0"}),
                 filename="b@vela-5.2.0+r2+m55_hp+aaaaaaaaaaaa.json")
    assert _find(tmp_path) is None
    # ... but a caller that CAN state the version gets an unambiguous answer.
    assert _find(tmp_path, toolchain_version="5.2.0") is not None


def test_a_fixture_bannered_point_is_never_consumed(tmp_path):
    # alp-sdk stamps `_fixture` on synthetic documents whose `measured`
    # figures are PLACEHOLDERS. Reporting one at `confidence: "certain"` is
    # the single worst output this tier can produce, so it is refused
    # outright rather than consumed with a caveat.
    path = _write_point(tmp_path, _point_doc(_fixture="SYNTHETIC -- not a measurement"))
    assert read_perf_point(path) is None
    assert _find(tmp_path) is None


def test_a_point_that_is_not_bench_measured_is_refused(tmp_path):
    # The `stance` enum has one member so that a future CALIBRATED ESTIMATE
    # has to arrive as a distinguishable kind. An estimate this reader took
    # for a measurement would ship at `confidence: "certain"`.
    _write_point(tmp_path, _point_doc(stance="calibrated-estimate"))
    assert _find(tmp_path) is None


@pytest.mark.parametrize("body", ["{not json", '["a list"]', '"a string"', "null"])
def test_a_malformed_document_is_not_a_match_and_never_raises(tmp_path, body):
    d = tmp_path / "model_perf" / "E1M-AEN801" / "ethos-u85-256"
    d.mkdir(parents=True)
    path = d / "p.json"
    path.write_text(body, encoding="utf-8")
    assert read_perf_point(path) is None
    assert _find(tmp_path) is None


def test_an_unreadable_file_is_not_a_match_and_never_raises(tmp_path):
    # A directory where a file should be: `read_text` raises OSError, and a
    # consumer must stay quiet where the PRODUCING repository's gate is loud.
    d = tmp_path / "model_perf" / "E1M-AEN801" / "ethos-u85-256" / "p.json"
    d.mkdir(parents=True)
    assert read_perf_point(d) is None
    assert _find(tmp_path) is None


@pytest.mark.parametrize("block,key", [
    ("measured_on", "sku"), ("measured_on", "hw_rev"), ("measured_on", "core"),
    ("measured_on", "backend"), ("measured_on", "accel_config"),
    ("model", "sha256"), ("model", "slug"), ("model", "size_bytes"),
    ("model", "source"), ("toolchain", "name"), ("toolchain", "version"),
    ("capture", "date"), ("capture", "operator"), ("capture", "reference"),
])
def test_a_point_missing_any_identity_field_is_refused(tmp_path, block, key):
    doc = _point_doc()
    doc[block] = {k: v for k, v in doc[block].items() if k != key}
    _write_point(tmp_path, doc, sku="E1M-AEN801", target="ethos-u85-256")
    assert _find(tmp_path) is None


def test_a_point_whose_body_disagrees_with_its_path_is_not_found(tmp_path):
    # The path is an INDEX; the body is the truth. alp-sdk's validator refuses
    # to publish a misfiled point, and this reader does not have to trust it.
    _write_point(tmp_path, _point_doc(measured_on={"sku": "E1M-AEN701"}),
                 sku="E1M-AEN801", target="ethos-u85-256")
    assert _find(tmp_path) is None


def test_a_measured_block_that_is_not_an_object_is_refused(tmp_path):
    _write_point(tmp_path, _point_doc(measured=[]))
    assert _find(tmp_path) is None


# ---------------------------------------------------------------------------
# `None` means "not measured", never zero
# ---------------------------------------------------------------------------

def _only_measured(**measured) -> dict:
    """A point whose `measured` block is EXACTLY @measured -- `_point_doc`'s
    per-block merge is the wrong tool here, since these cases are about what
    happens when a figure is genuinely absent."""
    doc = _point_doc()
    doc["measured"] = dict(measured)
    return doc


def test_an_omitted_figure_reads_as_none_not_zero(tmp_path):
    # The schema OMITS a figure nobody measured rather than zero-filling it,
    # so a zero here is a MEASURED zero and the two must not collapse.
    _write_point(tmp_path, _only_measured(npu_ops=44, cpu_ops=0))
    point = _find(tmp_path)
    assert point is not None
    assert point.arena_bytes is None and point.req_sram_kib is None
    assert point.latency_ms_mean is None and point.runs is None


def test_a_measured_zero_survives_as_a_zero(tmp_path):
    _write_point(tmp_path, _only_measured(npu_ops=0, cpu_ops=44, arena_bytes=0))
    point = _find(tmp_path)
    assert point is not None
    assert point.arena_bytes == 0            # measured, not missing
    assert point.npu_ops == 0


def test_a_boolean_is_not_a_count(tmp_path):
    # `bool` is an `int` subclass in Python; `True` is not 1 operator.
    _write_point(tmp_path, _only_measured(npu_ops=True, cpu_ops=0))
    point = _find(tmp_path)
    assert point is not None
    assert point.npu_ops is None


# ---------------------------------------------------------------------------
# A backend with no accel config keys its points by backend name
# ---------------------------------------------------------------------------

def test_a_backend_with_no_accel_config_is_keyed_by_backend_name(tmp_path):
    assert target_dir_name("drpai", "") == "drpai"
    assert target_dir_name("ethos_u", "ethos-u85-256") == "ethos-u85-256"
    doc = _point_doc(measured_on={"backend": "drpai", "accel_config": ""})
    # A DRP-AI point carries no vela memory profile at all, and `None` there
    # must stay `None` rather than becoming an empty string.
    doc["toolchain"] = {"name": "translator", "version": "1.12"}
    _write_point(tmp_path, doc)
    point = _find(tmp_path, backend="drpai", accel_config="", toolchain="translator")
    assert point is not None
    assert point.toolchain_system_config is None
    assert point.toolchain_memory_mode is None


def test_an_absent_accel_config_key_is_not_the_same_as_an_empty_one(tmp_path):
    # REQUIRED-AND-MAY-BE-EMPTY: `""` is a backend that HAS none, an absent
    # key is a point that failed to record one, and the whole value of a perf
    # point is that nothing about its identity is left to inference.
    doc = _point_doc(measured_on={"backend": "drpai"},
                     toolchain={"name": "translator", "version": "1.12"})
    del doc["measured_on"]["accel_config"]
    _write_point(tmp_path, doc, target="drpai")
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
def test_alp_sdks_own_synthetic_fixture_point_is_refused_by_this_reader():
    """Not tan's IDEA of a fixture -- the actual `_fixture`-bannered document
    alp-sdk ships. Its identity fields are real (a real sha256 of a real
    model) while every figure under `measured` is a placeholder, which is
    exactly the shape that would be catastrophic to report as `bench`."""
    fixtures = sorted((SDK / "tests" / "fixtures" / "model_perf").rglob("*.json"))
    assert fixtures, "the capability mark should have skipped this"
    for path in fixtures:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert "_fixture" in doc, f"{path} is not a fixture point"
        assert read_perf_point(path) is None, (
            f"{path}: a placeholder document must never be readable as a "
            f"measurement"
        )


@pytestmark_real_sdk
@needs_sdk_model_perf_points
def test_every_published_point_round_trips_through_this_reader():
    """Whatever the first bench campaign publishes, this reader must be able
    to read ALL of it -- and each point must be findable by its own identity,
    which is what proves the path convention and the match rule agree with
    what alp-sdk actually writes."""
    published = sorted((_META / "model_perf").rglob("*.json"))
    assert published, "the capability mark should have skipped this"
    for path in published:
        point = read_perf_point(path)
        assert point is not None, f"{path} is published but unreadable"
        found = find_perf_point(
            sku=point.sku, backend=point.backend,
            accel_config=point.accel_config, model_sha256=point.model_sha256,
            toolchain=point.toolchain, metadata_root=_META)
        assert found is not None and found.path == path, (
            f"{path}: published but not findable by its own identity -- the "
            f"path convention and the match rule disagree"
        )
