# SPDX-License-Identifier: Apache-2.0
"""`tan model check`'s engine glue -- which backends a SKU screens against,
and the opportunistic `--exact` upgrade (ADR-0028 amendment, tan-cli#782
Tasks 4/6).

`tan.model.analyze` is the pure static-eligibility screen and deliberately
knows nothing about a SKU's *actual* NPU backends or about a real compiler --
its own docstring says so ("no click/typer, no envelope construction").
This module is the layer above it that a `check` run actually calls:

  * [`resolve_check_backends`] -- which non-cpu backends @sku's SoM declares
    at all (`tan.model.targets.resolve_targets`, the SAME resolver
    `build_model` uses to decide what to compile), so `check` never screens
    a model against hardware the board doesn't have -- an Ethos-U row on a
    V2N board would misreport hardware that isn't there.
  * [`check_model_backends`] -- one `BackendReport` per resolved backend for
    one model source, walking its ops once (`tensorio.extract_ops`) and
    reusing them across every backend.
  * The `--exact` upgrade: Vela is a free, un-gated `pip install`
    (`ethos-u-vela`), so for `ethos_u` the "offline, no toolchain" constraint
    the static screen exists for is soft. When `--exact` is set and `vela` is
    on PATH, [`_maybe_exact_ethos_u`] runs the REAL compiler; [`_report_from_
    vela_compile`] then reads what vela ACTUALLY placed on the NPU (`Blob.
    npu_op_count`/`cpu_op_count`, sourced from vela's own "NPU operators = N
    (P%)" summary line) and only returns `basis: "compiled"` / `npu_coverage:
    "fits"` -- the only basis allowed to say `"fits"`
    (`analyze.BackendReport.npu_coverage`'s own comment) -- at 100% NPU
    placement. A clean vela exit is NOT proof of that: vela exits 0 on a full
    CPU fallback by design (measured: a float32 FULLY_CONNECTED model prints
    "NPU operators = 0 (0.0%)" and still exits 0), so partial/zero placement
    reports the real split (`npu_coverage: "partial"`/`"cpu-only"`) and keeps
    the static per-op verdicts (`report.ops`) rather than discarding them.
    `drpai`/`deepx_dxm1` stay opportunistic-only: both toolchains are
    license-gated (`dxcom`, the DRP-AI TVM checkout), so `--exact` never
    attempts to spawn either here -- [`_license_gated_exact_note`] reports
    that as a reason, not a crash, exactly like `model doctor`'s own narrower
    probes do for the same two backends.

Does its own IO (reads `metadata/**` through `resolve_targets`/
`resolve_ethos_u_variant`, and `--exact` shells `vela`) -- the same stance
`tan.model.build`/`tan.model.targets` already take: IO lives in `tan.model`,
never in `tan.core` (see `tan.core.model_check`, which is the pure
serialisation/rendering half `tan.commands.model_cmd` calls alongside this).
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from .adapters import Blob
from .adapters.ethos_u import VelaAdapter
from .analyze import BackendReport, analyze_backend, resolve_ethos_u_variant
from .targets import resolve_targets
from .tensorio import extract_ops, tflite_reader_available


def resolve_check_backends(sku: str, *, metadata_root: Path) -> list[str]:
    """Distinct non-`cpu` backend names @sku's SoM actually declares, in
    `resolve_targets`' own first-seen order -- the exact set `check` screens
    a model against. `cpu` is excluded: "how much of this model can target
    the NPU" has nothing to say about the fallback CPU path itself.

    Raises exactly what `resolve_targets` raises (`FileNotFoundError` for no
    SoM preset or no SoC spec, `ValueError` for a malformed `silicon:` ref) --
    a board-level fact about the SKU, not a per-model one, so the caller
    reports it once for the whole run rather than once per declared model.
    """
    seen: list[str] = []
    for spec in resolve_targets(sku, metadata_root=metadata_root):
        if spec.backend != "cpu" and spec.backend not in seen:
            seen.append(spec.backend)
    return seen


def check_model_backends(*, backends: list[str], sku: str, source: Path,
                          metadata_root: Path, exact: bool) -> list[BackendReport]:
    """One `BackendReport` per @backends entry for @source -- the ops are
    walked ONCE (`extract_ops`, best-effort: `[]` for a non-.tflite source,
    same contract every ONNX-ingesting backend already treats as
    `undetermined` via `analyze_backend`'s own format gate) and reused across
    every backend, mirroring `build_model`'s own single-read-many-adapters
    shape.

    A `.tflite` @source that extracts to `[]` is ambiguous on its own -- a
    genuinely op-free/malformed model and a missing `tflite` reader (the
    `model-io` extra) both look identical to `analyze_backend`. Checked here,
    once, rather than inside `analyze_backend` (which stays format-agnostic
    and knows nothing about `model-io`): when the reader is the actual cause,
    the resulting `undetermined` report's note is swapped for one that names
    the fix (`_reader_missing_report`), never left as the generic "nothing to
    score" text that gives a `pip install alp-tan` customer -- exactly the
    shape `ci.yml` itself installs, no `model-io` extra -- no hint at all
    (MAJOR 2 review)."""
    ops = extract_ops(source)
    src_format = source.suffix.lstrip(".").lower()
    reader_missing = src_format == "tflite" and not ops and not tflite_reader_available()
    reports: list[BackendReport] = []
    for backend in backends:
        variant = (resolve_ethos_u_variant(sku, metadata_root=metadata_root)
                   if backend == "ethos_u" else None)
        report = analyze_backend(backend=backend, src_format=src_format, ops=ops,
                                  metadata_root=metadata_root, variant=variant)
        if reader_missing and report.table is not None and not report.ops:
            report = _reader_missing_report(report)
        reports.append(_apply_exact(report, backend, source, sku, metadata_root) if exact else report)
    return reports


def _reader_missing_report(report: BackendReport) -> BackendReport:
    """Swap `_empty_ops_report`'s generic "nothing was extracted" note for one
    that names the actual, fixable cause -- the `tflite` reader (the
    `model-io` extra) is not installed on this host -- without touching
    `npu_coverage`: this is still `undetermined`, never a verdict on the
    model itself. Only called when a resolved table (`report.table is not
    None`) proves the format/backend pairing was fine and ops really did come
    back empty for a reader reason, never for `format-not-accepted` or
    `no-table-for-backend` (both already carry their own honest note and
    leave `report.table` `None`)."""
    return replace(report, notes=[
        "no operators could be extracted: the `tflite` reader is not "
        "installed on this host (pip install alp-tan[model-io]); this is "
        "not a statement about the model itself.",
    ])


def _apply_exact(report: BackendReport, backend: str, source: Path, sku: str,
                  metadata_root: Path) -> BackendReport:
    if backend != "ethos_u":
        return _license_gated_exact_note(report, backend)
    return _maybe_exact_ethos_u(report, source, sku, metadata_root)


def _license_gated_exact_note(report: BackendReport, backend: str) -> BackendReport:
    """`drpai`/`deepx_dxm1` never attempt a real compile under `--exact` in
    this release -- both toolchains are license-gated (`dxcom`, the DRP-AI
    TVM checkout), so there is no un-gated path the way `vela` is for
    ethos_u. Reported as a note, never a crash -- mirrors `model doctor`'s
    own `_deepx_dxm1_status`/`_drpai_status` treatment of the same two
    backends."""
    if report.ops and report.ops[0].reason == "format-not-accepted":
        return report          # wrong ingest format either way; nothing to caveat
    tool = "dxcom" if backend == "deepx_dxm1" else "the DRP-AI TVM toolchain"
    note = (f"--exact is not available for {backend} in this release: "
            f"{tool} is license-gated, so this stays the static screen.")
    return replace(report, notes=[*report.notes, note])


def _maybe_exact_ethos_u(report: BackendReport, source: Path, sku: str,
                          metadata_root: Path) -> BackendReport:
    """Runs the real `vela` compile when it is on PATH; degrades cleanly --
    and SAYS SO, in a note -- back to @report (the static screen) for every
    other case: no vela, no resolvable accelerator config, or a vela failure.
    Never raises. The compile succeeding is handed to `_report_from_vela_
    compile`, which is what actually decides `npu_coverage` -- NOT this
    function, and NOT vela's exit code (0 on a full CPU fallback by
    design)."""
    if report.ops and report.ops[0].reason == "format-not-accepted":
        return report
    if shutil.which("vela") is None:
        note = ("--exact was requested, but vela is not on PATH (pip install "
                 "alp-tan[model-compile]); reporting the static screen instead.")
        return replace(report, notes=[*report.notes, note])
    accel_config = _headline_ethos_u_accel_config(sku, metadata_root)
    if accel_config is None:
        note = ("--exact could not resolve an Ethos-U accelerator config for "
                 "this SKU; reporting the static screen instead.")
        return replace(report, notes=[*report.notes, note])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            blob = VelaAdapter().compile(source, accel_config=accel_config, out_dir=Path(tmp))
    except Exception as err:  # noqa: BLE001 -- a failed exact compile degrades, it never crashes
        note = f"--exact compile with vela failed ({err}); reporting the static screen instead."
        return replace(report, notes=[*report.notes, note])
    return _report_from_vela_compile(report, blob, accel_config)


def _report_from_vela_compile(report: BackendReport, blob: Blob, accel_config: str) -> BackendReport:
    """Turn a REAL, successful vela compile into a `BackendReport` driven by
    what vela actually placed on the NPU -- never by the fact that compile()
    didn't raise. **Vela exits 0 on a full CPU fallback by design** (measured:
    a float32 FULLY_CONNECTED model compiled for `ethos-u85-256` prints "NPU
    operators = 0 (0.0%)" and still exits 0), so "it didn't crash" is not
    proof of NPU placement -- that overclaim is exactly what this function
    replaces (the BLOCKER this fix addresses).

    Reads `blob.cpu_op_count`/`npu_op_count` (vela's own placement verdict,
    `ethos_u._parse_vela_placement`): 100% NPU (no CPU ops, at least one op
    ran) is the only case that reports `"fits"`, the only `npu_coverage` this
    module ever emits at `basis: "compiled"` -- `ops` stays `[]`, since
    nothing fell back. Partial or 0% NPU reports the REAL split
    (`"partial"`/`"cpu-only"`, a real not upper-bound `compute_on_npu_pct_
    max`) and KEEPS @report's own per-op verdicts (`ops=report.ops`) rather
    than discarding them -- a 0%-NPU compile must never read as a bare,
    contextless success. An unparseable placement summary degrades to
    `_vela_placement_unreadable`, same as every other `_maybe_exact_ethos_u`
    failure path: this function cannot verify a placement it can't read."""
    sizing = f"arena {blob.arena_bytes} bytes, SRAM {blob.req_sram_kib} KiB"
    total = (blob.npu_op_count or 0) + (blob.cpu_op_count or 0)
    if blob.npu_op_count is None or blob.cpu_op_count is None or total == 0:
        return _vela_placement_unreadable(report, blob, accel_config, sizing)
    pct = 100.0 * blob.npu_op_count / total
    if blob.cpu_op_count == 0:
        coverage, ops = "fits", []
    else:
        coverage = "cpu-only" if blob.npu_op_count == 0 else "partial"
        ops = report.ops          # keep the static per-op verdicts; a real
                                   # partial/0% compile must not erase them
    # `blob.compiler_version` is ALREADY the full string to show -- "vela"
    # (degraded: the `ethos-u-vela` distribution's metadata is absent,
    # `_vela_version()` in `adapters/ethos_u.py`) or "vela X.Y.Z" (real).
    # Wrapping it in a second "vela (...)" would read "vela (vela)" on the
    # degraded path -- a typo, not a version string.
    note = (f"{blob.compiler_version} compiled for {accel_config}: "
            f"{blob.npu_op_count}/{total} operators placed on the NPU "
            f"({pct:.0f}%); {sizing}.")
    return BackendReport(
        backend="ethos_u", variant=report.variant, table=report.table,
        npu_coverage=coverage, compute_on_npu_pct_max=pct, ops=ops,
        basis="compiled", confidence="certain", notes=[note],
    )


def _vela_placement_unreadable(report: BackendReport, blob: Blob, accel_config: str,
                                sizing: str) -> BackendReport:
    """vela ran and produced output, but `_parse_vela_placement` found no
    "CPU/NPU operators = N" lines in it (an unexpected output shape, e.g. a
    future vela version that changes this text) -- degrade to @report (the
    static screen) exactly like every other `_maybe_exact_ethos_u` failure
    path. This function cannot verify a placement it can't read, so it does
    not claim one -- never "0 CPU ops, so it must be all-NPU"."""
    note = (f"--exact compiled with {blob.compiler_version} for {accel_config} "
            f"({sizing}), but its operator-placement summary could not be read, "
            f"so NPU placement cannot be verified; reporting the static screen "
            f"instead.")
    return replace(report, notes=[*report.notes, note])


def _headline_ethos_u_accel_config(sku: str, metadata_root: Path) -> str | None:
    """The accel_config `--exact` hands `vela`: `resolve_ethos_u_variant`'s
    own PRIMARY variant, at the highest `mac_per_cycle` config resolved for
    it -- an arbitrary but deterministic tiebreak for a SoM shipping more
    than one NPU at the same variant (E1M-AEN701's `alif:ensemble:e7` lists a
    high-perf U55 at 256 MAC/cycle alongside a high-efficiency U55 at 128,
    `metadata/socs/alif/ensemble/e7.json`'s own `npus[]`). There is no
    per-SKU "preferred accel_config" field in the metadata this could read
    instead of guessing; None when the variant itself doesn't resolve
    (drpai/deepx_dxm1 SoMs) or no target matches it."""
    variant = resolve_ethos_u_variant(sku, metadata_root=metadata_root)
    if variant is None:
        return None
    prefix = f"ethos-{variant}-"
    candidates = [s.accel_config for s in resolve_targets(sku, metadata_root=metadata_root)
                  if s.backend == "ethos_u" and s.accel_config.startswith(prefix)]
    if not candidates:
        return None
    return max(candidates, key=lambda c: int(c.rsplit("-", 1)[-1]))
