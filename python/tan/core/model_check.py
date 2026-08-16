# SPDX-License-Identifier: Apache-2.0
"""Pure serialisation + text rendering for `tan model check` (tan-cli#782).

Mirrors `tan.core.model_doctor`'s split: the engine (`tan.model.check`,
`tan.model.analyze`) returns `BackendReport`s; this module turns THOSE into
the wire dict `--format json` emits (camelCase, the house convention every
other command's `data` payload already follows -- `schemaVersion`,
`boardYaml`, ...) and the lines `--format text` prints. No IO, no vela/dxcom
probing, nothing that was not already computed by the caller.

`model_cmd.py`'s `finish()` renders `check`'s text mode from `data` -- the
SAME serialised dict `--format json` emits, not a second dataclass-shaped
code path -- exactly the pattern `doctor`'s own text branch already uses
(`data["backends"]`): one source of truth for the values, two renderers for
the format.
"""
from __future__ import annotations

from tan.model.analyze import BackendReport, OpVerdict

#: Cosmetic backend -> text-mode label. `ethos_u` is special-cased in
#: `backend_label` below to fold in the resolved variant ("Ethos-U55") --
#: the other two backends carry no per-instance variant to show.
_BACKEND_LABELS = {"ethos_u": "Ethos-U", "drpai": "DRP-AI", "deepx_dxm1": "DEEPX DX-M1"}


def backend_label(backend: str, variant: str | None) -> str:
    if backend == "ethos_u" and variant and variant.startswith("u"):
        return f"Ethos-U{variant[1:]}"
    return _BACKEND_LABELS.get(backend, backend)


def op_verdict_as_dict(v: OpVerdict) -> dict:
    return {"op": v.op, "status": v.status, "reason": v.reason, "macs": v.macs}


def backend_report_as_dict(report: BackendReport) -> dict:
    """Every `BackendReport` field, camelCase -- including `basis`,
    `confidence` and `uncostedCpuOpCount` (a structured caveat on
    `computeOnNpuPctMax`, not just prose -- see `analyze.BackendReport`'s own
    comment), so a consumer never needs the text rendering to recover a
    number this dict already carries."""
    return {
        "backend": report.backend,
        "variant": report.variant,
        "table": report.table,
        "npuCoverage": report.npu_coverage,
        "computeOnNpuPctMax": report.compute_on_npu_pct_max,
        "uncostedCpuOpCount": report.uncosted_cpu_op_count,
        "basis": report.basis,
        "confidence": report.confidence,
        "notes": list(report.notes),
        "ops": [op_verdict_as_dict(o) for o in report.ops],
    }


def _coverage_line(report: dict) -> str | None:
    """`None` on BOTH of `npu_coverage`'s "we don't actually know" spellings
    -- `"undetermined"` itself, and the more subtle case where every `op`
    entry carries `status: "unknown"` (`format-not-accepted`/`no-table-for-
    backend`: `analyze.py` still emits one placeholder verdict per input op
    so a consumer can see WHICH ops were skipped, but none of them was
    actually screened). Counting `status == "unknown"` entries against
    `len(ops)` reads as "0/N ops are NPU-eligible" -- the exact `cpu-only`
    misreading non-negotiables 1 and 2 forbid, reintroduced by the renderer
    (MAJOR 3 review) even though the JSON underneath was already correct.
    Only ever counts verdicts a real screen actually determined
    (`npu-eligible`/`cpu-certain`). Also `None` for a real (`basis:
    "compiled"`) report: `tan.model.check._report_from_vela_compile` keeps
    the STATIC per-op verdicts alongside a real vela placement result
    (`ops=report.ops`, so the caller can still see which ops the table
    thought were eligible), and those can legitimately disagree with what
    vela actually placed (table membership doesn't check dtype/shape) --
    recomputing an "E/T ops are NPU-eligible" figure from them here would
    read alongside the REAL placement percentage as self-contradictory. The
    real split is already `notes`' whole point for that report
    (`"N/Total operators placed on the NPU (P%)"`)."""
    if report["npuCoverage"] == "undetermined" or report["basis"] != "static-screen":
        return None
    determined = [o for o in report["ops"] if o["status"] in ("npu-eligible", "cpu-certain")]
    if not determined:
        return None
    eligible = sum(1 for o in determined if o["status"] == "npu-eligible")
    total = len(determined)
    pct = report["computeOnNpuPctMax"]
    if pct is not None:
        return (f"  {pct:.0f}% of compute ({eligible}/{total} ops) is NPU-eligible"
                f"   [upper bound, static screen]")
    return f"  {eligible}/{total} ops are NPU-eligible by name (no MAC-weighted figure)"


def _cpu_fallback_line(report: dict) -> str | None:
    names = [o["op"] for o in report["ops"] if o["status"] == "cpu-certain"]
    if not names:
        return None
    shown = ", ".join(names[:8])
    if len(names) > 8:
        shown += f", +{len(names) - 8} more"
    noun = "op is" if len(names) == 1 else "ops are"
    return f"  {len(names)} {noun} certain CPU fallback: {shown}"


def _exact_hint_line(report: dict) -> str | None:
    """The upgrade-path advertisement -- ethos_u only (the sole backend with
    a free, un-gated compiler), and only while the report is STILL a static
    screen. Suppressed when a note already explains an `--exact` attempt
    (`tan.model.check._maybe_exact_ethos_u`'s own notes all open with
    "--exact ..."); printing both would repeat the same install command
    twice in one report. Deliberately NOT a substring check on "vela" --
    every ethos_u table is itself named `<variant>@vela-<ver>.json`
    (`tan.model.analyze._resolve_table`), so the static-screen note ALWAYS
    mentions "vela" via the table filename it cites, which would suppress
    this hint on every ordinary run rather than only after a real attempt."""
    if report["backend"] != "ethos_u" or report["basis"] != "static-screen":
        return None
    if any(n.startswith("--exact") for n in report["notes"]):
        return None
    return "  Exact:  pip install alp-tan[model-compile]  &&  tan model check --exact"


def render_backend_report(report: dict, *, sku: str | None, prefix: str = "") -> list[str]:
    """One backend's block of text-mode lines: the header (`{label} ({sku})
    {coverage}`), the coverage + CPU-fallback summary lines, every note
    (this is where the engine's own silent-CPU-fallback sentence and the
    uncosted-MAC caveat surface), and the exact-upgrade hint."""
    label = backend_label(report["backend"], report["variant"])
    lines = [f"{prefix}{label} ({sku})  {report['npuCoverage']}"]
    for build_line in (_coverage_line, _cpu_fallback_line):
        line = build_line(report)
        if line:
            lines.append(line)
    lines.extend(f"  {note}" for note in report["notes"])
    hint = _exact_hint_line(report)
    if hint:
        lines.append(hint)
    return lines


def render_check_text(data: dict) -> list[str]:
    """Every backend block for every declared model, in order. `[]` for no
    declared models -- the caller (`model_cmd.finish`) is the one that
    decides whether an empty result needs a "nothing to check" filler line,
    the same split `build`'s own text branch already makes for `built`."""
    models = data.get("models") or []
    sku = data.get("sku")
    show_name = len(models) > 1
    lines: list[str] = []
    for m in models:
        prefix = f"{m['name']}: " if show_name else ""
        for backend in m.get("backends", []):
            lines.extend(render_backend_report(backend, sku=sku, prefix=prefix))
    return lines
