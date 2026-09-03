# SPDX-License-Identifier: Apache-2.0
"""tan.model.analyze -- static NPU-eligibility screen (ADR-0028 amendment).

Supersedes the retired `fits | cpu-fallback | no-fit` vocabulary. Two test
tiers:

  * Format-gate + MAC-weighting + the vocabulary guard need no real alp-sdk
    checkout -- they build their own throwaway `metadata/npu_ops/**` tables
    under `tmp_path`.
  * Table-RESOLUTION-by-SKU tests (u85 vs u55/u65) need the real, committed
    `metadata/npu_ops/**` + `metadata/e1m_modules/*.yaml` content, so they are
    gated on a bound `ALP_SDK_ROOT` the same way `tests/model/test_targets.py`
    is -- skip LOUDLY, naming the missing var, never a silent pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tan.model.analyze import (
    COVERAGE_WITHHELD,
    LEGITIMATE_COVERAGE_BY_BASIS,
    BackendReport,
    OpVerdict,
    analyze_backend,
    resolve_ethos_u_variant,
)
from tan.model import analyze as analyze_mod
from tan.model.perf import PerfPoint
from tan.model.perf_apply import _perf_point_report
from tan.model.tensorio import OpDesc
from tests.conftest import needs_sdk_npu_op_tables, sdk_root

SDK = sdk_root()


def _op(name: str, macs: int = 0, *, op_namespace: str = "tflite") -> OpDesc:
    return OpDesc(op=name, macs=macs, op_namespace=op_namespace)


def _write_table(root: Path, backend: str, filename: str, *, variant: str,
                  op_namespace: str = "tflite", supported: list[str],
                  stance: str = "screening") -> Path:
    d = root / "npu_ops" / backend
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text(json.dumps({
        "applies_to": {"variant": variant, "products": [], "toolchain": "x", "toolchain_version": "1"},
        "op_namespace": op_namespace,
        "authority": "tool-generated",
        "stance": stance,
        "provenance": {},
        "supported_ops": supported,
    }))
    return path


# ---------------------------------------------------------------------------
# Task 1: the format gate, first -- a category error, not a low-confidence answer
# ---------------------------------------------------------------------------

def test_a_tflite_model_is_not_scored_against_an_onnx_backend(tmp_path):
    rep = analyze_backend(backend="drpai", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path)
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "format-not-accepted"
    assert rep.compute_on_npu_pct_max is None
    # Must NOT claim cpu-only, which a customer reads as "won't run".
    assert rep.npu_coverage != "cpu-only"
    assert rep.basis == "static-screen"


def test_an_onnx_model_is_not_scored_against_a_tflite_backend(tmp_path):
    rep = analyze_backend(backend="ethos_u", src_format="onnx", ops=[_op("Conv")],
                           metadata_root=tmp_path)
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "format-not-accepted"
    assert rep.compute_on_npu_pct_max is None


def test_format_match_proceeds_past_the_gate(tmp_path):
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["CONV_2D"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.ops[0].reason != "format-not-accepted"


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        analyze_backend(backend="not-a-real-backend", src_format="tflite", ops=[],
                        metadata_root=Path("."))


# ---------------------------------------------------------------------------
# Task 2: table resolution by (backend, variant) -- synthetic tables
# ---------------------------------------------------------------------------

def test_missing_table_directory_is_undetermined_never_cpu_only(tmp_path):
    # deepx_dxm1 ships no table by decision -- 'no data' must never collapse
    # into 'no support'.
    rep = analyze_backend(backend="deepx_dxm1", src_format="onnx", ops=[_op("Conv")],
                           metadata_root=tmp_path)
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"
    assert rep.npu_coverage != "cpu-only"
    assert rep.table is None


def test_the_deepx_answer_would_change_if_a_table_existed(tmp_path):
    """Proves the absence checked above is load-bearing, not incidental: point
    metadata_root at a tree that DOES carry a deepx_dxm1 table and the verdict
    changes from undetermined to a real per-op score."""
    # op_namespace="onnx": these ops stand in for a (not-yet-built) ONNX
    # extractor's output -- the table's own op_namespace must match THIS,
    # not just the src_format= the caller happens to pass.
    without_table = analyze_backend(backend="deepx_dxm1", src_format="onnx",
                                     ops=[_op("Conv", op_namespace="onnx")], metadata_root=tmp_path)
    assert without_table.npu_coverage == "undetermined"

    with_table_root = tmp_path / "with-table"
    _write_table(with_table_root, "deepx_dxm1", "onnx-i8@dxcom-2.3.0.json",
                 variant="onnx-i8", op_namespace="onnx", supported=["Conv"])
    with_table = analyze_backend(backend="deepx_dxm1", src_format="onnx",
                                  ops=[_op("Conv", op_namespace="onnx")], metadata_root=with_table_root)
    assert with_table.npu_coverage == "full-eligible"
    assert with_table.ops[0].status == "npu-eligible"
    assert with_table.ops[0].reason != "no-table-for-backend"


def test_variant_selects_among_multiple_tables(tmp_path):
    _write_table(tmp_path, "ethos_u", "u85@vela-5.1.0.json", variant="u85",
                 supported=["GATHER", "CONV_2D"])
    _write_table(tmp_path, "ethos_u", "u55-u65@vela-5.1.0.json", variant="u55-u65",
                 supported=["CONV_2D"])          # no GATHER -- the u85-only op

    rep_u85 = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("GATHER")],
                               metadata_root=tmp_path, variant="u85")
    rep_u55 = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("GATHER")],
                               metadata_root=tmp_path, variant="u55")
    rep_u65 = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("GATHER")],
                               metadata_root=tmp_path, variant="u65")
    assert rep_u85.ops[0].status == "npu-eligible"
    assert rep_u55.ops[0].status == "cpu-certain"
    assert rep_u65.ops[0].status == "cpu-certain"
    assert rep_u85.table != rep_u55.table


def test_op_namespace_mismatch_refuses_the_comparison_rather_than_guessing(tmp_path):
    # A table exists and its applies_to.variant matches, but it speaks ONNX
    # while the walked ops are TFLite names -- refuse, don't compare across
    # incomparable vocabularies.
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 op_namespace="onnx", supported=["CONV_2D"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_namespace_guard_compares_against_the_ops_actual_vocabulary_not_src_format(tmp_path):
    # The table's op_namespace ("onnx") equals @src_format ("onnx") here, so
    # a guard that compared table-vs-src_format (the pre-fix behaviour) would
    # let this through -- but the ops actually carry TFLite-spelled names
    # (OpDesc.op_namespace defaults to "tflite"), e.g. a caller whose
    # src_format claim doesn't match what was really extracted. Scoring
    # "CONV_2D" against an onnx-vocabulary supported_ops=["Conv"] table
    # fabricates a cpu-only negative -- the guard must compare against the
    # ops' OWN vocabulary and refuse.
    _write_table(tmp_path, "drpai", "onnx-i8@translator-1.12.json", variant="onnx-i8",
                 op_namespace="onnx", supported=["Conv"])
    ops = [OpDesc(op="CONV_2D", op_namespace="tflite")]
    rep = analyze_backend(backend="drpai", src_format="onnx", ops=ops, metadata_root=tmp_path)
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"
    assert rep.npu_coverage != "cpu-only"


def test_no_table_covers_the_requested_variant(tmp_path):
    _write_table(tmp_path, "ethos_u", "u85@vela-5.1.0.json", variant="u85",
                 supported=["CONV_2D"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")     # no u55 table present
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_empty_ops_is_undetermined_even_with_a_resolvable_table(tmp_path):
    # ONNX operator extraction is a follow-on (Task 3): a real .onnx source
    # yields ops=[] today even against a backend/table that COULD score it.
    _write_table(tmp_path, "drpai", "onnx-i8@translator-1.12.json", variant="onnx-i8",
                 op_namespace="onnx", supported=["Conv"])
    rep = analyze_backend(backend="drpai", src_format="onnx", ops=[], metadata_root=tmp_path)
    assert rep.npu_coverage == "undetermined"
    assert rep.table is not None          # the table WAS found; there was just nothing to score
    assert rep.ops == []


def test_empty_ops_does_not_cite_a_table_whose_namespace_disagrees_with_src_format(tmp_path):
    # ops=[] means there is no extracted vocabulary to check the table
    # against -- but the table's own op_namespace ("onnx") still visibly
    # disagrees with the caller's src_format claim ("tflite"), the only
    # vocabulary evidence available in this case. Citing that table as "the"
    # table found would be misleading even though nothing gets scored either
    # way -- must fall back to the no-table report (table=None), not the
    # empty-ops report.
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 op_namespace="onnx", supported=["CONV_2D"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.table is None
    assert rep.ops == []


# ---------------------------------------------------------------------------
# Table resolution against the REAL alp-sdk metadata (ALP_SDK_ROOT-gated)
# ---------------------------------------------------------------------------

pytestmark_real_sdk = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- table resolution by SKU needs the real, committed "
           "metadata/npu_ops/** and metadata/e1m_modules/*.yaml content.",
)
_META = SDK / "metadata" if SDK is not None else None


@pytestmark_real_sdk
@pytest.mark.parametrize("sku", ["E1M-AEN401", "E1M-AEN601", "E1M-AEN801"])
def test_u85_skus_resolve_the_u85_variant(sku):
    assert resolve_ethos_u_variant(sku, metadata_root=_META) == "u85"


@pytestmark_real_sdk
@pytest.mark.parametrize("sku", ["E1M-AEN301", "E1M-AEN501", "E1M-AEN701"])
def test_u55_skus_resolve_the_u55_variant(sku):
    assert resolve_ethos_u_variant(sku, metadata_root=_META) == "u55"


@pytestmark_real_sdk
def test_nx9101_resolves_the_u65_variant():
    assert resolve_ethos_u_variant("E1M-NX9101", metadata_root=_META) == "u65"


@pytestmark_real_sdk
def test_drpai_som_has_no_ethos_u_variant():
    assert resolve_ethos_u_variant("E1M-V2N101", metadata_root=_META) is None


@pytestmark_real_sdk
@needs_sdk_npu_op_tables
def test_u85_and_u55_resolve_different_real_tables_and_the_17_op_delta_is_visible():
    variant_u85 = resolve_ethos_u_variant("E1M-AEN801", metadata_root=_META)
    variant_u55 = resolve_ethos_u_variant("E1M-AEN301", metadata_root=_META)
    assert variant_u85 == "u85" and variant_u55 == "u55"

    # GATHER: present in the u85 (70-op) table, absent from the u55/u65
    # (53-op) table -- part of the 17-op delta the u55-u65 table's own
    # provenance note calls out.
    rep_u85 = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("GATHER")],
                               metadata_root=_META, variant=variant_u85)
    rep_u55 = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("GATHER")],
                               metadata_root=_META, variant=variant_u55)
    assert rep_u85.table != rep_u55.table
    assert rep_u85.ops[0].status == "npu-eligible"
    assert rep_u55.ops[0].status == "cpu-certain"


@pytestmark_real_sdk
def test_real_deepx_dxm1_has_no_table_and_is_undetermined():
    variant = resolve_ethos_u_variant("E1M-V2M101", metadata_root=_META)
    assert variant is None            # deepx_dxm1 carries no ethos_u_variant
    rep = analyze_backend(backend="deepx_dxm1", src_format="onnx",
                           ops=[_op("Conv", op_namespace="onnx")], metadata_root=_META)
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"
    assert rep.npu_coverage != "cpu-only"


@pytestmark_real_sdk
@needs_sdk_npu_op_tables
def test_real_drpai_table_surfaces_uncosted_cpu_op_count_alongside_the_headline_pct():
    # Live on real data, not synthetic (the real drpai table lists "Conv" but
    # not "NonMaxSuppression"): a headline compute_on_npu_pct_max of 100.0
    # alongside npu_coverage="partial" is a caveat that must survive as a
    # STRUCTURED field, not just prose in `notes`, for a consumer that reads
    # the float alone.
    ops = [_op("Conv", macs=100, op_namespace="onnx"),
           _op("NonMaxSuppression", macs=0, op_namespace="onnx")]
    rep = analyze_backend(backend="drpai", src_format="onnx", ops=ops, metadata_root=_META)
    assert rep.npu_coverage == "partial"
    assert rep.compute_on_npu_pct_max == pytest.approx(100.0)
    assert rep.uncosted_cpu_op_count == 1
    assert any("compute_on_npu_pct_max" in n and "excluded" in n for n in rep.notes)


# ---------------------------------------------------------------------------
# Task 3: MAC weighting -- op-count coverage is misleading, MAC coverage isn't
# ---------------------------------------------------------------------------

def test_mac_weighted_coverage_diverges_sharply_from_op_count_coverage(tmp_path):
    # One CONV_2D op that is CPU-certain (huge MACs) plus twenty ADD ops that
    # are NPU-eligible (0 MACs apiece, e.g. bias-adds the extractor couldn't
    # cost). Op-count coverage reads ~95% eligible; MAC-weighted coverage must
    # not, because the conv dominates the real compute budget.
    _write_table(tmp_path, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["ADD"])              # CONV_2D deliberately NOT listed
    ops = [_op("CONV_2D", macs=1_000_000)] + [_op("ADD", macs=1) for _ in range(20)]
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=ops,
                           metadata_root=tmp_path, variant="dummy")

    op_count_pct = 100.0 * 20 / 21                # what a naive op-count ratio would report
    assert rep.compute_on_npu_pct_max is not None
    assert rep.compute_on_npu_pct_max < 1.0        # nowhere near the ~95% op-count figure
    assert rep.compute_on_npu_pct_max != pytest.approx(op_count_pct)
    assert rep.npu_coverage == "partial"


def test_compute_pct_is_none_when_no_macs_are_computable(tmp_path):
    _write_table(tmp_path, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["RESHAPE"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("RESHAPE", macs=0)],
                           metadata_root=tmp_path, variant="dummy")
    assert rep.compute_on_npu_pct_max is None


def test_compute_pct_100_with_partial_coverage_surfaces_uncosted_ops_in_notes(tmp_path):
    # Every cpu-certain op costs 0 MACs (SOFTMAX/TOPK_V2 aren't conv/dense,
    # so the estimator never prices them) -- the MAC-weighted denominator
    # excludes them entirely, so the ONE priced op (the eligible CONV_2D)
    # reads as 100% even though npu_coverage is "partial" and real, uncosted
    # CPU compute exists. The number must NOT be silently clamped away from
    # 100.0 -- that would hide the gap -- but `notes` must say so plainly, AND
    # `uncosted_cpu_op_count` (a structured field, not just prose) must carry
    # the same caveat for a consumer that doesn't render `notes`.
    _write_table(tmp_path, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["CONV_2D"])              # SOFTMAX/TOPK_V2 NOT listed
    ops = [_op("CONV_2D", macs=1_000_000), _op("SOFTMAX", macs=0), _op("TOPK_V2", macs=0)]
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=ops,
                           metadata_root=tmp_path, variant="dummy")
    assert rep.npu_coverage == "partial"
    assert rep.compute_on_npu_pct_max == pytest.approx(100.0)     # not silently clamped
    assert rep.uncosted_cpu_op_count == 2                          # SOFTMAX + TOPK_V2
    assert any("compute_on_npu_pct_max" in n and "excluded" in n for n in rep.notes)


def test_uncosted_cpu_op_count_is_zero_when_nothing_is_uncosted(tmp_path):
    _write_table(tmp_path, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["CONV_2D"])
    rep = analyze_backend(backend="ethos_u", src_format="tflite",
                           ops=[_op("CONV_2D", macs=10)], metadata_root=tmp_path,
                           variant="dummy")
    assert rep.uncosted_cpu_op_count == 0


def test_full_eligible_and_cpu_only_coverage_labels(tmp_path):
    _write_table(tmp_path, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["CONV_2D"])
    all_in = analyze_backend(backend="ethos_u", src_format="tflite",
                              ops=[_op("CONV_2D", macs=10)], metadata_root=tmp_path,
                              variant="dummy")
    assert all_in.npu_coverage == "full-eligible"
    assert all_in.compute_on_npu_pct_max == pytest.approx(100.0)

    none_in = analyze_backend(backend="ethos_u", src_format="tflite",
                               ops=[_op("RESHAPE", macs=10)], metadata_root=tmp_path,
                               variant="dummy")
    assert none_in.npu_coverage == "cpu-only"
    assert none_in.compute_on_npu_pct_max == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The cheapest possible regression guard: the retired vocabulary never resurfaces
# ---------------------------------------------------------------------------

def _all_representative_reports(metadata_root: Path) -> list[BackendReport]:
    _write_table(metadata_root, "ethos_u", "dummy@vela-1.0.0.json", variant="dummy",
                 supported=["CONV_2D"])
    return [
        analyze_backend(backend="drpai", src_format="tflite", ops=[_op("CONV_2D")],
                        metadata_root=metadata_root),                               # format gate
        analyze_backend(backend="deepx_dxm1", src_format="onnx", ops=[_op("Conv")],
                        metadata_root=metadata_root),                               # no table
        analyze_backend(backend="ethos_u", src_format="tflite",
                        ops=[_op("CONV_2D", macs=10), _op("RESHAPE", macs=5),
                             _op("SOFTMAX", macs=0)],
                        metadata_root=metadata_root, variant="dummy"),               # partial,
                                                                                      # carries _uncosted_macs_note
        analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D", macs=10)],
                        metadata_root=metadata_root, variant="dummy"),               # full-eligible
    ]


def test_the_word_fits_never_appears_in_a_static_screen_report(tmp_path):
    metadata_root = tmp_path
    for rep in _all_representative_reports(metadata_root):
        assert rep.basis == "static-screen"
        # Scan the report CONTENT (coverage label, per-op status/reason,
        # notes) for the retired vocabulary -- not the absolute `table` path,
        # which is a filesystem artifact under pytest's tmp_path and would
        # spuriously contain "fits" here purely because THIS test's own name
        # does (pytest derives the tmp dir name from the test id). The table
        # FILENAME (real ones come from scripts/gen_npu_ops.py) is still
        # checked, just not its ancestor directories.
        blob = json.dumps({
            "backend": rep.backend, "variant": rep.variant,
            "table": Path(rep.table).name if rep.table else None,
            "npu_coverage": rep.npu_coverage,
            "compute_on_npu_pct_max": rep.compute_on_npu_pct_max,
            "basis": rep.basis, "confidence": rep.confidence, "notes": rep.notes,
            "ops": [{"op": o.op, "status": o.status, "reason": o.reason, "macs": o.macs}
                    for o in rep.ops],
        }).lower()
        assert "fits" not in blob, f"retired 'fits' vocabulary leaked into: {blob}"


# ---------------------------------------------------------------------------
# The two adapter registries (this module's own + build.py's) must agree
# ---------------------------------------------------------------------------

def test_analyze_and_build_adapter_registries_agree():
    """`analyze.py`'s `_ADAPTERS` is a deliberate second instantiation of
    `build.py`'s own registry (see the module-level comment above `_ADAPTERS`
    in analyze.py for why it isn't a shared import). Nothing else pins them
    equal -- an adapter added, or an `accepts()` changed, on one side alone
    would make `analyze_backend` either raise `unknown backend` or report
    `format-not-accepted` for a format `build_model()` accepts just fine.
    Compared by (class, backend, which of the three known formats it
    accepts), not by identity -- the two lists are legitimately different
    objects."""
    from tan.model import analyze as analyze_mod
    from tan.model import build as build_mod

    def _fingerprint(adapters):
        return sorted(
            (type(a).__name__, a.backend,
             tuple(fmt for fmt in ("tflite", "onnx", "pte") if a.accepts(fmt)))
            for a in adapters
        )

    assert _fingerprint(analyze_mod._ADAPTERS) == _fingerprint(build_mod._ADAPTERS)


# ---------------------------------------------------------------------------
# Soft-fail promises that must not raise (an explicit-but-null YAML/JSON
# block, or a table document that parses but isn't an object)
# ---------------------------------------------------------------------------

def test_resolve_ethos_u_variant_tolerates_an_explicit_null_inference_block(tmp_path):
    modules_dir = tmp_path / "e1m_modules"
    modules_dir.mkdir()
    # `inference:` with no value parses to {"inference": None}, not a missing
    # key -- resolve_ethos_u_variant's own docstring promises soft-fail (None),
    # not an AttributeError from `.get()` on that None.
    (modules_dir / "E1M-FAKE999.yaml").write_text("inference:\nsom: {}\n", encoding="utf-8")
    assert resolve_ethos_u_variant("E1M-FAKE999", metadata_root=tmp_path) is None


def test_resolve_table_tolerates_an_explicit_null_applies_to_block(tmp_path):
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": None, "op_namespace": "tflite", "supported_ops": ["CONV_2D"],
    }))
    # A null applies_to can never match a requested variant -- it must read
    # as "no table covers u55" (undetermined), not raise.
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_tolerates_a_non_dict_applies_to_block(tmp_path):
    # tan-cli#969: `metadata/npu_ops/**` has no schema at all, so
    # `applies_to: 7` (a scalar where the population's own convention wants
    # an object) is only ruled out by this isinstance guard, not by a schema
    # gate elsewhere. Before the fix this raised
    # `AttributeError: 'int' object has no attribute 'get'` out of
    # `_resolve_table`; it must instead read exactly like the null-block
    # case above -- "no table covers u55" (undetermined), not a crash.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": 7, "op_namespace": "tflite", "supported_ops": ["CONV_2D"],
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_tolerates_a_non_string_variant_field(tmp_path):
    # tan-cli#969, sibling site: `applies_to.variant: 7` used to reach
    # `table_variant.split("-")` and raise
    # `AttributeError: 'int' object has no attribute 'split'`. Same
    # undetermined outcome as an absent/null variant, no fourth behaviour.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": 7}, "op_namespace": "tflite", "supported_ops": ["CONV_2D"],
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_load_table_rejects_a_parseable_non_dict_json_document(tmp_path):
    from tan.model.analyze import _load_table
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    # json.loads("[1, 2, 3]") raises neither OSError nor ValueError -- the
    # OLD except-only guard let a non-dict document straight through to the
    # first caller's .get(), an uncaught AttributeError rather than the
    # clean "no table" _load_table promises.
    assert _load_table(path) is None


# ---------------------------------------------------------------------------
# tan-cli#979 review finding 1: `_score_ops` reads `supported_ops` off the
# same schema-free table document `_table_variant` sanitises, unguarded.
# ---------------------------------------------------------------------------

def test_resolve_table_tolerates_a_non_list_supported_ops_field(tmp_path):
    # Before the fix: `set(doc.get("supported_ops", []))` on `supported_ops:
    # 7` raised `TypeError: 'int' object is not iterable`. A table this
    # malformed is unscoreable -- the whole resolve must read as
    # "undetermined", not crash, and never reach _score_ops at all.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": "u55"}, "op_namespace": "tflite", "supported_ops": 7,
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_tolerates_a_null_supported_ops_field(tmp_path):
    # `supported_ops: null` raised the same TypeError as the int case, off
    # `set(None)` -- `'NoneType' object is not iterable`.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": "u55"}, "op_namespace": "tflite", "supported_ops": None,
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_tolerates_a_string_supported_ops_field_without_fabricating_cpu_only(tmp_path):
    # The dangerous shape: `supported_ops: "CONV_2D"` does NOT raise --
    # `set("CONV_2D")` silently builds a set of seven characters no real
    # operator name ever matches, so the pre-fix behaviour reported EVERY op
    # cpu-only/op-not-in-table: a fabricated negative manufactured from
    # malformed data, exactly the outcome this module's own docstring names
    # as the worst one it exists to prevent. Must read as undetermined, not
    # a confident negative.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": "u55"}, "op_namespace": "tflite", "supported_ops": "CONV_2D",
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.npu_coverage != "cpu-only"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_tolerates_a_supported_ops_list_with_a_non_string_entry(tmp_path):
    # A list is the right container type but a non-str element inside it is
    # still not something a `str in supported` membership test can trust
    # blindly -- reject the whole table rather than silently coercing.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": "u55"}, "op_namespace": "tflite",
        "supported_ops": ["CONV_2D", 7],
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.npu_coverage == "undetermined"
    assert rep.ops[0].reason == "no-table-for-backend"


def test_resolve_table_scores_normally_when_supported_ops_is_absent(tmp_path):
    # A genuinely-absent `supported_ops` key defaults to `[]` (a table that
    # legitimately supports no operators) and must NOT be treated as
    # malformed -- only a PRESENT but wrong-shaped field routes to
    # "no-table-for-backend". The table still resolves and scores; the op
    # simply has no match.
    d = tmp_path / "npu_ops" / "ethos_u"
    d.mkdir(parents=True)
    (d / "u55@vela-1.0.0.json").write_text(json.dumps({
        "applies_to": {"variant": "u55"}, "op_namespace": "tflite",
    }))
    rep = analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                           metadata_root=tmp_path, variant="u55")
    assert rep.table is not None
    assert rep.ops[0].reason == "op-not-in-table"
    assert rep.npu_coverage == "cpu-only"


# ---------------------------------------------------------------------------
# tan-cli#979 review finding 2: resolve_ethos_u_variant's sibling `or {}`
# gap, and the scalar-preset-document case at the same site.
# ---------------------------------------------------------------------------

def test_resolve_ethos_u_variant_tolerates_a_non_dict_inference_block(tmp_path):
    # `inference: 7` used to reach `(7).get("ethos_u_variant")` and raise
    # `AttributeError: 'int' object has no attribute 'get'`.
    modules_dir = tmp_path / "e1m_modules"
    modules_dir.mkdir()
    (modules_dir / "E1M-FAKE999.yaml").write_text("inference: 7\nsom: {}\n", encoding="utf-8")
    assert resolve_ethos_u_variant("E1M-FAKE999", metadata_root=tmp_path) is None


def test_resolve_ethos_u_variant_tolerates_a_scalar_top_level_preset_document(tmp_path):
    # A preset YAML file whose top-level document is a bare scalar (not even
    # a mapping) used to reach `preset.get("inference")` on an int and raise
    # the same AttributeError.
    modules_dir = tmp_path / "e1m_modules"
    modules_dir.mkdir()
    (modules_dir / "E1M-FAKE999.yaml").write_text("7\n", encoding="utf-8")
    assert resolve_ethos_u_variant("E1M-FAKE999", metadata_root=tmp_path) is None


def test_resolve_ethos_u_variant_tolerates_a_non_string_ethos_u_variant_field(tmp_path):
    # `ethos_u_variant: 7` used to return the bare int uncaught, straight
    # into analyze_backend(variant: str | None).
    modules_dir = tmp_path / "e1m_modules"
    modules_dir.mkdir()
    (modules_dir / "E1M-FAKE999.yaml").write_text(
        "inference:\n  ethos_u_variant: 7\nsom: {}\n", encoding="utf-8")
    assert resolve_ethos_u_variant("E1M-FAKE999", metadata_root=tmp_path) is None


# --------------------------------------------------------------------------
# tan-cli#1135 -- LEGITIMATE_COVERAGE_BY_BASIS and the construction-time guard
# --------------------------------------------------------------------------


def _report(coverage: str, basis: str) -> BackendReport:
    return BackendReport(backend="ethos_u", variant="u85", table=None,
                         npu_coverage=coverage, compute_on_npu_pct_max=None,
                         basis=basis)


@pytest.mark.parametrize("basis,coverage", sorted(
    (b, c) for b, cs in LEGITIMATE_COVERAGE_BY_BASIS.items() for c in cs))
def test_every_declared_legitimate_pair_constructs(basis, coverage):
    assert _report(coverage, basis).npu_coverage == coverage


def test_the_static_screen_may_not_claim_fits():
    # The word is reserved for a REAL placement -- `tan.model.perf.
    # coverage_from_placement` is the only function in tan that returns it,
    # and this module's module docstring says so in its last paragraph.
    with pytest.raises(ValueError, match=r"\('fits', 'static-screen'\)"):
        _report("fits", "static-screen")


def test_a_compiled_report_may_not_claim_the_static_screens_capped_positive():
    # `full-eligible` is a NAME-level estimate; a compile measured a real
    # placement and reports `fits`/`partial`/`cpu-only` instead. Promoting the
    # estimate under `confidence: "certain"` is the exact failure tan-cli#1115
    # closed on the bench path.
    with pytest.raises(ValueError, match=r"\('full-eligible', 'compiled'\)"):
        _report("full-eligible", "compiled")


def test_an_unknown_basis_is_refused_by_name():
    with pytest.raises(ValueError, match=r"unknown basis 'measured'"):
        _report("partial", "measured")


def test_the_bench_vocabulary_is_the_compiled_one_plus_the_withheld_word():
    # Not an independently maintained list: `_perf_point_report` CARRIES a
    # compiled report's own verdict or withholds, so a word newly reachable at
    # `"compiled"` is reachable at `"bench"` by construction.
    assert (LEGITIMATE_COVERAGE_BY_BASIS["bench"]
            == LEGITIMATE_COVERAGE_BY_BASIS["compiled"] | {COVERAGE_WITHHELD})


def test_the_bench_entry_is_still_DERIVED_from_the_compiled_one_in_the_source():
    """The value check above cannot see the property this design rests on.

    Re-typing `"bench"` as a literal `frozenset({...})` with today's four words
    leaves every value assertion in this file green (measured on the tree
    before this test existed: `51 passed, 11 skipped`, rc=0) -- it only goes
    red once the two lists have ALREADY drifted,
    which is precisely the second-hand-written-list failure tan-cli#1135 exists
    to prevent. So read the SOURCE: the `"bench"` value must still be an
    expression built from `_COVERAGE_AT_COMPILED`, not a set literal.

    Structural (`ast`) rather than a substring match, so reformatting the dict
    cannot red this and cannot green a literal either."""
    import ast  # noqa: PLC0415 -- test-local, one call site

    tree = ast.parse(Path(analyze_mod.__file__).read_text(encoding="utf-8"))
    value = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(t.id == "LEGITIMATE_COVERAGE_BY_BASIS"
                for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
                if isinstance(t, ast.Name)))
    assert isinstance(value, ast.Dict)
    bench = next(v for k, v in zip(value.keys, value.values, strict=True)
                 if isinstance(k, ast.Constant) and k.value == "bench")
    names = {n.id for n in ast.walk(bench) if isinstance(n, ast.Name)}
    assert "_COVERAGE_AT_COMPILED" in names, (
        f"LEGITIMATE_COVERAGE_BY_BASIS['bench'] is spelled "
        f"{ast.unparse(bench)!r} -- a self-contained list. It must be DERIVED "
        f"from `_COVERAGE_AT_COMPILED`, because `_perf_point_report` carries a "
        f"compiled report's own verdict or withholds: a word reachable at "
        f"`compiled` is reachable at `bench` for free, and writing the bench "
        f"words out again re-creates the duplication tan-cli#1135 removed.")


def test_the_no_verdict_reports_all_state_the_withheld_word_at_static_screen():
    # All THREE `analyze_backend` short-circuits that never reach `_score_ops`.
    # The first two need no table on disk; `_empty_ops_report` is only reached
    # once a table RESOLVES, so it gets one written for it.
    reports = [
        analyze_backend(backend="drpai", src_format="tflite", ops=[_op("Conv")],
                        metadata_root=Path("/nonexistent")),          # format-not-accepted
        analyze_backend(backend="ethos_u", src_format="tflite", ops=[_op("CONV_2D")],
                        metadata_root=Path("/nonexistent")),          # no-table-for-backend
    ]
    assert [r.ops[0].reason for r in reports] == ["format-not-accepted", "no-table-for-backend"]
    for report in reports:
        assert (report.npu_coverage, report.basis) == (COVERAGE_WITHHELD, "static-screen")


def test_the_empty_ops_report_also_states_the_withheld_word_at_static_screen(tmp_path):
    # The third short-circuit -- a table that RESOLVED, with nothing to score.
    _write_table(tmp_path, "drpai", "onnx-i8@translator-1.12.json", variant="onnx-i8",
                 op_namespace="onnx", supported=["Conv"])
    report = analyze_backend(backend="drpai", src_format="onnx", ops=[], metadata_root=tmp_path)
    assert report.table is not None and report.ops == []
    assert (report.npu_coverage, report.basis) == (COVERAGE_WITHHELD, "static-screen")


def test_partial_at_bench_is_really_reachable_through_production_code():
    """The one declared pair no other test reaches through a real code path.

    `_perf_point_report`'s carry branch is coverage-AGNOSTIC (`report.
    npu_coverage if real_placement else COVERAGE_WITHHELD`), so this adds no
    new branch -- but without it the gate protects a documented row nothing in
    CI ever produces, and "reachable" would rest on the reviewer's word rather
    than on a run."""
    point = PerfPoint(
        path=Path("/x/p.yaml"), sku="E1M-AEN801", hw_rev="A", core="m55",
        backend="ethos_u", accel_config="ethos-u85-256",
        compiler_version="vela 5.1.0", model_name="m", model_src_sha="ab" * 32,
        model_format="tflite", capture_date="2026-01-01", capture_operator="x",
        capture_bench_id="e1m-aen-evk-01", vela_system_config=None,
        vela_memory_mode=None, req_sram_kib=73, arena_bytes=74480,
        latency_ms_mean=1.0, latency_ms_p50=None, latency_ms_p95=1.2,
        latency_ms_stdev=None, latency_runs=100, capture_notes=None)
    compiled = BackendReport(
        backend="ethos_u", variant="u85", table=None, npu_coverage="partial",
        compute_on_npu_pct_max=None, npu_placement_pct_real=66.67,
        basis="compiled", confidence="certain")

    rebased = _perf_point_report(compiled, point)

    assert (rebased.npu_coverage, rebased.basis) == ("partial", "bench")
    assert rebased.confidence == "certain"
    # And the withholding half of the same branch, off a static screen.
    static = BackendReport(backend="ethos_u", variant="u85", table=None,
                           npu_coverage="partial", compute_on_npu_pct_max=66.67)
    assert _perf_point_report(static, point).npu_coverage == COVERAGE_WITHHELD
