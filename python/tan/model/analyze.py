# SPDX-License-Identifier: Apache-2.0
"""Static NPU-eligibility screen for a packaged model (ADR-0028 amendment).

Answers, offline and with no NPU toolchain installed, "how much of this model
can target the NPU on this SoM, and what definitely cannot?" -- with claims
the evidence actually supports. Supersedes the retired `fits | cpu-fallback |
no-fit` vocabulary: no backend can deliver `fits` statically (Vela attaches
Generic constraints -- quantization, per-axis quant, dtype, zero-point, shape
-- to every operator and Specific ones to 30 of 70; DRP-AI gates acceptance on
enumerated kernel x stride x padding x dilation x groups, so the same operator
name is accepted or rejected on tensor shape alone). So:

  * **Negatives are sound.** An operator absent from the resolved table is
    certainly CPU (`cpu-certain`).
  * **Positives are capped at "eligible", never "will run".** An operator
    present in the table is `npu-eligible` -- a static screen cannot see the
    shape/quantization decision a real compile makes.
  * **A missing source-format match, or a missing support table, is
    `undetermined`, never `cpu-only`.** Both read to a customer as "won't
    run", which is false: every backend degrades to silent CPU fallback
    rather than refusing. `deepx_dxm1` ships no table by decision -- DEEPX is
    the headline feature of V2M, so a fabricated negative there is the worst
    outcome this module exists to prevent.

The word "fits" is reserved for `basis: compiled` or `basis: bench`; this
module's `basis` is always `"static-screen"` and must never emit it.

Pure engine: no click/typer, no envelope construction, no file writes. Every
public function takes `metadata_root` explicitly rather than resolving an
ambient SDK checkout -- `tan.model` stays self-contained
(`tests/model/test_model_package_imports.py`)."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import CompilerAdapter
from .adapters.cpu import CpuAdapter
from .adapters.deepx import DeepxAdapter
from .adapters.drpai import DrpaiAdapter
from .adapters.ethos_u import VelaAdapter
from .adapters.executorch import ExecutorchAdapter
from .tensorio import OpDesc

# One adapter instance per source format a backend can ingest -- mirrors
# `build.py`'s own `_ADAPTERS` registry (deliberately a second, independent
# instantiation: these are stateless, side-effect-free objects, and importing
# `build.py`'s registry directly would reach into another module's private
# (`_`-prefixed) name for no benefit). A `backend` string maps to >= 1
# adapter -- e.g. "cpu" carries both `CpuAdapter` (.tflite) and
# `ExecutorchAdapter` (.pte) -- so the format gate below asks "does ANY
# adapter registered for this backend accept this format", the same question
# `build_model()` asks per model.
_ADAPTERS: list[CompilerAdapter] = [
    CpuAdapter(), VelaAdapter(), DrpaiAdapter(), DeepxAdapter(), ExecutorchAdapter(),
]


def _adapters_by_backend() -> dict[str, list[CompilerAdapter]]:
    by_backend: dict[str, list[CompilerAdapter]] = {}
    for a in _ADAPTERS:
        by_backend.setdefault(a.backend, []).append(a)
    return by_backend


_ADAPTERS_BY_BACKEND = _adapters_by_backend()


def _known_backend(backend: str) -> bool:
    return backend in _ADAPTERS_BY_BACKEND


def _accepts(backend: str, src_format: str) -> bool:
    return any(a.accepts(src_format) for a in _ADAPTERS_BY_BACKEND.get(backend, []))


@dataclass(frozen=True)
class OpVerdict:
    """One operator's static verdict against a resolved support table."""

    op: str                  # as spelled in the model's own vocabulary
    status: str               # "npu-eligible" | "cpu-certain" | "unknown"
    reason: str               # "op-not-in-table" | "constraint-unchecked"
                               # | "no-table-for-backend" | "format-not-accepted"
    macs: int = 0             # 0 when not computable


@dataclass(frozen=True)
class BackendReport:
    """One backend's static-screen partition report for a model."""

    backend: str              # cpu | ethos_u | drpai | deepx_dxm1
    variant: str | None       # u85 | u55 | u65 | None
    table: str | None         # the table file that answered, or None
    npu_coverage: str          # "full-eligible" | "partial" | "cpu-only" | "undetermined" --
                               # plus "fits", but ONLY at basis == "compiled" (tan.model.check's
                               # `--exact` path, tan-cli#782 Task 6) or basis == "bench" (a
                               # matched `metadata/model_perf/` point, the tier-2 path in the
                               # same module). Those are the only two surfaces ever permitted
                               # to say it, and both derive it from ONE function
                               # (`tan.model.perf.coverage_from_placement`) so the guard on the
                               # word binds to a live rule rather than to a copy.
                               # analyze_backend() itself never emits "fits" -- basis stays
                               # "static-screen" here always.
    # MAC-weighted UPPER bound, 0-100 -- ONLY at `basis: "static-screen"`
    # (this module's own `_score_ops`/`eligible_macs / total_macs`). Always
    # `None` at `basis: "compiled"` (tan.model.check's `--exact` path):
    # vela's own placement summary is an aggregate CPU/NPU *op count*, not a
    # per-op MAC breakdown, so there is no real MAC-weighted figure to put
    # here for a compile -- `npu_placement_pct_real` below carries the real
    # (but op-count, not MAC-weighted) placement instead (MAJOR 4 review: a
    # `basis: "compiled"` report used to overload THIS field with that
    # op-count ratio, under the exact MAC-weighted-upper-bound contract this
    # comment documents -- measured a static report with one 1000-MAC
    # eligible op and two 0-MAC CPU ops emitting `66.666...` here on the
    # compiled path, precisely the op-count distortion MAC weighting exists
    # to eliminate).
    compute_on_npu_pct_max: float | None
    # The REAL NPU-vs-CPU op-count placement ratio, 0-100 -- set only where a
    # real placement was MEASURED: `basis: "compiled"` (`tan.model.check.
    # _report_from_vela_compile`, from vela's own "CPU/NPU operators = N (P%)"
    # summary counts) and `basis: "bench"` (a `metadata/model_perf/` point's
    # `measured.npu_ops`/`cpu_ops`, i.e. what the compiler that produced the
    # measured artefact placed). `None` at `basis: "static-screen"`, where
    # there is no real compile to report a placement for. Deliberately a
    # SEPARATE field from `compute_on_npu_pct_max` above, not a reuse of it
    # under a different meaning: an op-count ratio and a MAC-weighted ratio
    # answer different questions and must never share one key a consumer could
    # read as either.
    npu_placement_pct_real: float | None = None
    # cpu-certain ops with an unpriced (macs=0) MAC estimate -- excluded from
    # compute_on_npu_pct_max's denominator, so a nonzero count here is a
    # machine-readable caveat on that percentage: it can read 100.0 while real,
    # uncosted CPU compute exists. Structured so the caveat survives into an
    # envelope consumer that reads compute_on_npu_pct_max but doesn't render
    # `notes` (prose). 0 whenever no such op exists, including every report
    # variant that never reached scoring (format-not-accepted, no-table,
    # empty-ops) -- there, no cpu-certain verdict was determined at all.
    uncosted_cpu_op_count: int = 0
    ops: list[OpVerdict] = field(default_factory=list)
    basis: str = "static-screen"          # the only basis this module ever emits
    confidence: str = "screening"          # "certain" | "screening"
    # THE FOOTPRINT AND LATENCY FIELDS, and what each `None` means.
    #
    # `None` is "not measured", never zero: a zero here is a measured zero, the
    # same reading `metadata/model_perf/`'s own schema gives an omitted key.
    # All six stay `None` at `basis: "static-screen"` -- a screen that walks
    # operator NAMES against a table has no footprint to report and inventing
    # one from it would be the estimate this whole vocabulary exists to keep
    # separate from a measurement.
    #
    # `arena_bytes` / `req_sram_kib` are set at `basis: "compiled"` (from the
    # `Blob` a real vela compile returned) and at `basis: "bench"` (from a
    # matched perf point). Carrying them as FIELDS rather than only inside the
    # prose note is what lets `tan.model.check` compare a customer's own
    # compile against a bench point figure-by-figure instead of by parsing its
    # own sentence back.
    #
    # The three latency fields and `perf_ref` are set at `basis: "bench"` --
    # a compile reports no wall-clock, and only a bench point cites a raw
    # capture (`capture.reference`, e.g. `alp-sdk-internal:bench/captures/
    # ...`) -- OR, informationally, on a `basis: "compiled"` report that names
    # a disagreeing bench point in its notes
    # (`tan.model.perf_apply.apply_perf_point`'s Decision 1: the customer's
    # own compile still wins on coverage/arena/SRAM, but Alp Lab's own
    # measured latency and traceable capture have no compiled counterpart to
    # disagree WITH, so they ride alongside rather than being discarded --
    # tan-cli#791 review item 3). A latency without `latency_runs` is a single
    # shot rather than a measurement, which is why the count travels with the
    # figures.
    arena_bytes: int | None = None
    req_sram_kib: int | None = None
    latency_ms_mean: float | None = None
    latency_ms_p95: float | None = None
    latency_runs: int | None = None
    perf_ref: str | None = None
    notes: list[str] = field(default_factory=list)


def resolve_ethos_u_variant(sku: str, *, metadata_root: Path) -> str | None:
    """Read `inference.ethos_u_variant` off the SoM preset for @sku. None for
    a SoM preset with no such field (non-Ethos-U SoMs -- drpai/deepx_dxm1
    presets carry no per-instance Ethos-U variant), or for a preset that
    doesn't exist on disk (callers that need a hard failure for a bad SKU
    should resolve targets first; this function stays soft-fail, matching
    `tan.soc_ref.resolve_soc_path`'s stance). Also soft-fails (None, never a
    raise) for a preset document, `inference` block, or `ethos_u_variant`
    value that parses to the wrong shape -- live on `tan model check`
    (`check.py:155`, `check.py:537`), same #957/#965/#969 defect class,
    tan-cli#979 review finding 2: `inference: 7` and a scalar top-level
    preset document (e.g. a YAML file containing only `7`) both raised
    `AttributeError: 'int' object has no attribute 'get'` (measured), and
    `ethos_u_variant: 7` returned the int straight into
    `analyze_backend(variant: str | None)` uncaught."""
    preset_path = Path(metadata_root) / "e1m_modules" / f"{sku}.yaml"
    if not preset_path.is_file():
        return None
    import yaml  # noqa: PLC0415 -- deferred (tan-cli#810); see sdk_cmd's `_releases_opener`

    preset = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    # A preset document that doesn't even parse to a dict (a bare scalar or
    # list at the YAML top level) can't be walked by the `.get()`s below --
    # treat it exactly like a missing preset rather than let it reach
    # `.get()` and raise.
    if not isinstance(preset, dict):
        return None
    # `isinstance` check, not a `.get(..., {})` default: a preset carrying an
    # explicit but EMPTY `inference:` block parses to `{"inference": None}`,
    # and the dict-literal default only fires when the key is absent
    # entirely -- `.get("inference", {})` still returns None there, and
    # `.get()` on None raises AttributeError, contradicting this function's
    # own soft-fail docstring. A non-dict, non-null `inference:` (e.g. `7`)
    # needs the same treatment, which a bare `or {}` alone does not give it.
    inference = preset.get("inference")
    if not isinstance(inference, dict):
        return None
    ethos_u_variant = inference.get("ethos_u_variant")
    return ethos_u_variant if isinstance(ethos_u_variant, str) else None


def _load_table(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A table file that parses but is not a JSON object (e.g. a bare array)
    # must be rejected HERE -- every caller treats a non-None return as
    # something it can `.get()` off, and letting a list or scalar through
    # turns a downstream `.get()` into an uncaught AttributeError instead of
    # the clean "no table" this function promises.
    return doc if isinstance(doc, dict) else None


def _table_variant(doc: dict) -> str:
    """@doc's `applies_to.variant`, normalised to `""` for any shape that
    isn't a plain string reachable through a dict `applies_to`.

    `metadata/npu_ops/**` has NO schema at all (tan-cli#969) -- unlike the
    `soc-spec-v1.schema.json`/`som-preset-v1.schema.json` family #964 covers,
    there is nothing to eventually enforce on the read path here today, so
    this isinstance guard is the only defence this population gets FOR NOW,
    pending an `npu-ops-v1.schema.json` (tracked: alp-sdk#1801) -- not a
    permanent substitute for one; do not read this comment as a decision
    that `npu_ops/**` should stay schema-free. `.get()` off a non-dict
    `applies_to` (e.g.
    `applies_to: 7`) raises `AttributeError: 'int' object has no attribute
    'get'`, and `.split()` on the caller's side off a non-string `variant`
    (e.g. `variant: 7`) raises `AttributeError: 'int' object has no
    attribute 'split'` -- same defect class as the #957/#965 family, same
    `_load_table` precedent immediately above (`analyze.py:204-214`) for why
    a malformed table is skipped rather than crashing the whole resolve.
    `or {}` alone (the pre-#969 guard) only covered an explicit-but-null
    `applies_to:`; it did not cover a non-dict scalar, which still reached
    `.get()`.

    `""` is the identical sentinel a genuinely-absent field already
    produces: the caller's `variant in _table_variant(doc).split("-")`
    then simply fails to match, the same "undetermined, not a fabricated
    negative" outcome `_resolve_table`'s own docstring already promises for
    "no table covers @variant" -- no fourth behaviour invented."""
    applies_to = doc.get("applies_to")
    if not isinstance(applies_to, dict):
        applies_to = {}
    table_variant = applies_to.get("variant", "")
    return table_variant if isinstance(table_variant, str) else ""


def _table_supported_ops(doc: dict) -> list[str] | None:
    """@doc's `supported_ops`, validated to a list of `str`. `None` for any
    other shape -- the caller (`analyze_backend`) treats that identically to
    a table that failed to resolve at all, never as evidence to score
    against (tan-cli#979 review finding 1, on top of #969's `applies_to`/
    `variant` guards above).

    `_score_ops` used to read `set(doc.get("supported_ops", []))` straight
    off the same schema-free `metadata/npu_ops/**` document `_table_variant`
    sanitises above, a few lines below on the same `analyze_backend` call
    path. `supported_ops: 7` raised `TypeError: 'int' object is not
    iterable`; `supported_ops: null` raised the same as `'NoneType' object
    is not iterable` -- but `supported_ops: "CONV_2D"` raised NOTHING:
    `set("CONV_2D")` silently built a set of seven characters no real
    operator name ever matches, so every op came back `cpu-only`/
    `cpu-certain` -- a fabricated negative manufactured from malformed data,
    exactly the outcome this module's own docstring (`analyze.py:18-23`)
    names as the worst one it exists to prevent. A crash at least announces
    itself; that one would have shipped a wrong verdict silently.

    A genuinely-absent key defaults to `[]` (a table that legitimately
    supports no operators) and passes straight through -- only a PRESENT but
    wrong-shaped field is rejected."""
    supported_ops = doc.get("supported_ops", [])
    if not isinstance(supported_ops, list):
        return None
    if not all(isinstance(op, str) for op in supported_ops):
        return None
    return supported_ops


def _resolve_table(metadata_root: Path, backend: str, variant: str | None) -> tuple[Path, dict] | None:
    """Pick the `metadata/npu_ops/<backend>/<variant>@<toolchain>-<ver>.json`
    whose `applies_to.variant` covers @variant. None when the backend has no
    table directory at all (e.g. `deepx_dxm1`, by decision) or no table in it
    covers @variant -- both cases are the caller's `undetermined`, never a
    negative verdict manufactured from absent data."""
    table_dir = Path(metadata_root) / "npu_ops" / backend
    if not table_dir.is_dir():
        return None
    candidates = sorted(table_dir.glob("*.json"))
    if not candidates:
        return None
    if variant is None:
        # No per-instance variant to resolve (e.g. drpai, which ships exactly
        # one table today): unambiguous only when there is exactly one table.
        if len(candidates) == 1:
            doc = _load_table(candidates[0])
            return (candidates[0], doc) if doc is not None else None
        return None
    for path in candidates:
        doc = _load_table(path)
        if doc is None:
            continue
        if variant in _table_variant(doc).split("-"):
            return path, doc
    return None


def _format_not_accepted_report(backend: str, src_format: str, ops: Sequence[OpDesc],
                                 variant: str | None) -> BackendReport:
    verdicts = [OpVerdict(op=o.op, status="unknown", reason="format-not-accepted")
                for o in ops]
    return BackendReport(
        backend=backend, variant=variant, table=None,
        npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=verdicts,
        notes=[f"{backend} does not ingest {src_format!r} source models; "
               f"no score computed. This is not a verdict on the model, only "
               f"on the format/backend pairing."],
    )


def _no_table_report(backend: str, ops: Sequence[OpDesc], variant: str | None) -> BackendReport:
    verdicts = [OpVerdict(op=o.op, status="unknown", reason="no-table-for-backend",
                           macs=o.macs) for o in ops]
    return BackendReport(
        backend=backend, variant=variant, table=None,
        npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=verdicts,
        notes=["no NPU-ops support table for this backend/variant -- absence "
               "of data, not evidence of no support."],
    )


def _empty_ops_report(backend: str, table_path: Path, variant: str | None) -> BackendReport:
    return BackendReport(
        backend=backend, variant=variant, table=str(table_path),
        npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=[],
        notes=["no operators were extracted for this source; nothing to "
               "score, so no coverage verdict is reported."],
    )


def _score_ops(ops: Sequence[OpDesc], doc: dict) -> tuple[list[OpVerdict], int, int]:
    """Per-op npu-eligible/cpu-certain verdicts against @doc's `supported_ops`,
    plus the running total/eligible MAC totals MAC-weighting needs.

    @doc is assumed pre-validated by `analyze_backend` via
    `_table_supported_ops` before this is ever called -- a malformed
    `supported_ops` is routed to `_no_table_report` instead of reaching
    here (tan-cli#979 review finding 1: `set()` of a non-list, or of a list
    containing a non-str, is exactly the crash/fabricated-negative class
    this module exists to keep out, so this function must never be called
    with an unvalidated @doc)."""
    supported = set(doc.get("supported_ops", []))
    verdicts: list[OpVerdict] = []
    total_macs = 0
    eligible_macs = 0
    for o in ops:
        total_macs += o.macs
        if o.op in supported:
            verdicts.append(OpVerdict(op=o.op, status="npu-eligible",
                                       reason="constraint-unchecked", macs=o.macs))
            eligible_macs += o.macs
        else:
            verdicts.append(OpVerdict(op=o.op, status="cpu-certain",
                                       reason="op-not-in-table", macs=o.macs))
    return verdicts, total_macs, eligible_macs


def _coverage_label(verdicts: list[OpVerdict]) -> str:
    all_eligible = all(v.status == "npu-eligible" for v in verdicts)
    any_eligible = any(v.status == "npu-eligible" for v in verdicts)
    if all_eligible:
        return "full-eligible"
    return "partial" if any_eligible else "cpu-only"


def _uncosted_op_count(verdicts: list[OpVerdict]) -> int:
    """Count of cpu-certain ops `_estimate_macs` could not price (macs=0) --
    also the source of the `uncosted_cpu_op_count` report field, so the note
    below and the structured field always agree."""
    return sum(1 for v in verdicts if v.status == "cpu-certain" and v.macs == 0)


def _uncosted_macs_note(uncosted: int, pct: float | None) -> str | None:
    """compute_on_npu_pct_max's denominator only counts ops `_estimate_macs`
    could price (conv/dense); a cpu-certain op outside that set (macs=0)
    leaves the denominator entirely, so the percentage can read 100.0 while
    real, un-priced CPU compute exists and `npu_coverage` still says
    "partial". Never clamp for this -- clamping hides the exact gap this
    module exists to surface -- return a note instead, or None when there is
    nothing uncosted (or no MAC-weighted number was computed at all)."""
    if pct is None or not uncosted:
        return None
    return (f"{uncosted} cpu-certain op(s) carry no static MAC estimate (outside "
            f"the conv/dense-only estimator) and are excluded from "
            f"compute_on_npu_pct_max's denominator -- the reported percentage "
            f"can overstate NPU-bound compute share when those ops are "
            f"significant in practice.")


def analyze_backend(*, backend: str, src_format: str, ops: Sequence[OpDesc],
                     metadata_root: Path, variant: str | None = None) -> BackendReport:
    """Static-screen @ops (already extracted from the model source) against
    @backend's resolved support table.

    Order of operations, deliberately: (1) the format gate -- does @backend
    even ingest @src_format -- before (2) table resolution, before (3) the
    per-op walk. Scoring a format a backend cannot ingest is a category error,
    not a low-confidence answer, so it short-circuits before any table load."""
    if not _known_backend(backend):
        raise ValueError(f"unknown backend {backend!r}")

    if not _accepts(backend, src_format):
        return _format_not_accepted_report(backend, src_format, ops, variant)

    resolved = _resolve_table(metadata_root, backend, variant)
    if resolved is not None:
        # The table must speak the SAME operator vocabulary as what will be
        # compared against it -- @ops' own op_namespace when there are ops to
        # compare (never @src_format, which is caller-supplied and
        # independent of what extraction really produced), or, when @ops is
        # empty and there is no extracted vocabulary to check, @src_format as
        # the best available surrogate. Always run this -- even for the
        # empty-ops case, where nothing gets scored either way -- because
        # citing a table whose op_namespace demonstrably disagrees with the
        # only vocabulary evidence available is misleading regardless of
        # whether anything was scored against it.
        actual_namespace = ops[0].op_namespace if ops else src_format
        if resolved[1].get("op_namespace") != actual_namespace:
            resolved = None
        elif _table_supported_ops(resolved[1]) is None:
            # tan-cli#979 review finding 1: a table with a well-formed
            # `applies_to`/`op_namespace` (i.e. one that survived every
            # check above -- exactly the table `_score_ops` would otherwise
            # be handed) but a malformed `supported_ops` is unscoreable, not
            # a table that legitimately supports nothing. Route it through
            # the same "no usable table" outcome as a namespace mismatch --
            # `undetermined`, never a verdict manufactured from the bad
            # field -- rather than reaching `_score_ops` at all.
            resolved = None

    if resolved is None:
        return _no_table_report(backend, ops, variant)

    table_path, doc = resolved
    if not ops:
        return _empty_ops_report(backend, table_path, variant)

    verdicts, total_macs, eligible_macs = _score_ops(ops, doc)
    pct = (100.0 * eligible_macs / total_macs) if total_macs > 0 else None
    notes = [f"static screen ({doc.get('stance', 'screening')}): operator-name "
             f"membership against {table_path.name} only. Eligible ops still "
             f"carry unchecked quantization/shape/dtype constraints this check "
             f"cannot verify -- the model will run either way, an unsupported "
             f"op falls back to the CPU silently rather than failing. Only a "
             f"real compile proves NPU execution."]
    uncosted = _uncosted_op_count(verdicts)
    uncosted_note = _uncosted_macs_note(uncosted, pct)
    if uncosted_note:
        notes.append(uncosted_note)

    return BackendReport(
        backend=backend, variant=variant, table=str(table_path),
        npu_coverage=_coverage_label(verdicts), compute_on_npu_pct_max=pct,
        uncosted_cpu_op_count=uncosted, ops=verdicts, notes=notes,
    )
