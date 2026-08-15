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

import yaml

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
    npu_coverage: str          # "full-eligible" | "partial" | "cpu-only" | "undetermined"
    compute_on_npu_pct_max: float | None   # MAC-weighted UPPER bound, 0-100
    ops: list[OpVerdict] = field(default_factory=list)
    basis: str = "static-screen"          # the only basis this module ever emits
    confidence: str = "screening"          # "certain" | "screening"
    notes: list[str] = field(default_factory=list)


def resolve_ethos_u_variant(sku: str, *, metadata_root: Path) -> str | None:
    """Read `inference.ethos_u_variant` off the SoM preset for @sku. None for
    a SoM preset with no such field (non-Ethos-U SoMs -- drpai/deepx_dxm1
    presets carry no per-instance Ethos-U variant), or for a preset that
    doesn't exist on disk (callers that need a hard failure for a bad SKU
    should resolve targets first; this function stays soft-fail, matching
    `tan.soc_ref.resolve_soc_path`'s stance)."""
    preset_path = Path(metadata_root) / "e1m_modules" / f"{sku}.yaml"
    if not preset_path.is_file():
        return None
    preset = yaml.safe_load(preset_path.read_text(encoding="utf-8")) or {}
    return preset.get("inference", {}).get("ethos_u_variant")


def _load_table(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
        table_variant = doc.get("applies_to", {}).get("variant", "")
        if variant in table_variant.split("-"):
            return path, doc
    return None


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
        verdicts = [OpVerdict(op=o.op, status="unknown", reason="format-not-accepted")
                    for o in ops]
        return BackendReport(
            backend=backend, variant=variant, table=None,
            npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=verdicts,
            notes=[f"{backend} does not ingest {src_format!r} source models; "
                   f"no score computed. This is not a verdict on the model, only "
                   f"on the format/backend pairing."],
        )

    resolved = _resolve_table(metadata_root, backend, variant)
    if resolved is not None:
        table_path, doc = resolved
        if doc.get("op_namespace") != src_format:
            # A table exists but speaks a different operator vocabulary than
            # the one @ops was walked in -- refuse the comparison rather than
            # silently matching names across two incomparable namespaces.
            resolved = None

    if resolved is None:
        verdicts = [OpVerdict(op=o.op, status="unknown", reason="no-table-for-backend",
                               macs=o.macs) for o in ops]
        return BackendReport(
            backend=backend, variant=variant, table=None,
            npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=verdicts,
            notes=["no NPU-ops support table for this backend/variant -- absence "
                   "of data, not evidence of no support."],
        )

    table_path, doc = resolved
    supported = set(doc.get("supported_ops", []))
    verdicts = []
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

    stance = doc.get("stance", "screening")
    base_notes = [
        f"static screen ({stance}): operator-name membership against "
        f"{table_path.name} only. Eligible ops still carry unchecked "
        f"quantization/shape/dtype constraints this check cannot verify -- "
        f"the model will run either way, an unsupported op falls back to the "
        f"CPU silently rather than failing. Only a real compile proves NPU "
        f"execution.",
    ]

    if not ops:
        return BackendReport(
            backend=backend, variant=variant, table=str(table_path),
            npu_coverage="undetermined", compute_on_npu_pct_max=None, ops=verdicts,
            notes=["no operators were extracted for this source; nothing to "
                   "score, so no coverage verdict is reported."],
        )

    any_eligible = any(v.status == "npu-eligible" for v in verdicts)
    all_eligible = all(v.status == "npu-eligible" for v in verdicts)
    if all_eligible:
        coverage = "full-eligible"
    elif any_eligible:
        coverage = "partial"
    else:
        coverage = "cpu-only"

    pct = (100.0 * eligible_macs / total_macs) if total_macs > 0 else None

    return BackendReport(
        backend=backend, variant=variant, table=str(table_path),
        npu_coverage=coverage, compute_on_npu_pct_max=pct, ops=verdicts,
        notes=base_notes,
    )
