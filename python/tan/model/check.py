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
  * The tier-2 upgrade: [`tan.model.perf_apply.apply_perf_point`] swaps in a
    bench-measured `metadata/model_perf/` point (`tan.model.perf` reads it,
    `tan.model.perf_apply` matches + applies it) when alp-sdk publishes one
    for this EXACT identity -- the `basis: "bench"` half of the resolution
    order below. Split into its own module (tan-cli#791 review NIT (c)): it is
    pure string/decision logic with no subprocess in it, and this file was
    against the house module-size cap.

RESOLUTION ORDER IS **precomputed -> exact-if-toolchain -> static**, and it is
not the order the code RUNS in. `check_model_backends` computes the static
screen first because everything else is built on top of it, then runs
`--exact` (only when the customer asked, and only when vela is really there),
then consults the perf point LAST -- but the point is what WINS, because it is
a measurement off real silicon rather than a screen or a host compile. The one
exception is decision 1 of the tier-2 plan, and it is deliberate: when the
customer really does hold the toolchain and their own compile DISAGREES with
our bench point, `--exact` wins, because their profile may not be ours and the
number they can act on is the one their machine produces
(`tan.model.perf_apply._perf_disagreements` defines "disagrees" concretely, on
the three figures both sides actually report).

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
from .adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from .analyze import BackendReport, analyze_backend, resolve_ethos_u_variant
from .perf import has_perf_points
from .perf_apply import (
    _PERF_TOOLCHAIN,  # noqa: F401  (re-export: tests/model/test_check.py's check_mod._PERF_TOOLCHAIN)
    _model_sha256,
    _placement_outcome,
    _resolve_hw_rev,
    apply_perf_point,
)
from .targets import TargetSpec, resolve_targets
from .tensorio import extract_ops, tflite_reader_available


def resolve_check_backends(sku: str, *, metadata_root: Path) -> list[str]:
    """Distinct non-`cpu` backend names @sku's SoM actually declares, in
    `resolve_targets`' own first-seen order -- the exact set `check` screens
    a model against. `cpu` is excluded: "how much of this model can target
    the NPU" has nothing to say about the fallback CPU path itself.

    This exclusion is also why tier 2 (`tan.model.perf_apply._PERF_TOOLCHAIN`)
    never asks for a `cpu` perf point either, even though alp-sdk's schema
    allows publishing one (tan-cli#791 review item 6): `check_model_backends`
    only ever iterates the backends THIS function returns, so a `cpu` point
    would have no caller to be handed to regardless of what
    `_PERF_TOOLCHAIN` said. See that dict's own comment for the fuller
    campaign-planner-facing statement of the decision.

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


# The perf-point step below is gated on `has_perf_points` and its digest is
# computed LAZILY behind that gate, once for all backends: no alp-sdk has
# published a `metadata/model_perf/` tree yet, and hashing every model source on
# every `check` to look inside a directory that does not exist would be pure cost
# for every customer alive today.
#
# `hw_rev` is the customer's own `board.yaml` `som.hw_rev`, or `None` when they
# declared none -- in which case `tan.model.perf_apply._resolve_hw_rev` falls
# back to the SKU preset's `default_hw_rev`, which is what the SDK itself does
# for an omitted key. It reaches ONLY the perf-point step: a bench measurement
# is pinned to the module revision it ran on, and nothing else in `check` is.
def check_model_backends(*, backends: list[str], sku: str, source: Path,
                          metadata_root: Path, exact: bool,
                          hw_rev: str | None = None) -> list[BackendReport]:
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
    (MAJOR 2 review).

    See the block comment above for the perf-point step's two properties: the
    lazy digest behind `has_perf_points`, and what @hw_rev is."""
    ops = extract_ops(source)
    src_format = source.suffix.lstrip(".").lower()
    reader_missing = src_format == "tflite" and not ops and not tflite_reader_available()
    perf_published = has_perf_points(metadata_root)
    resolved_hw_rev = (_resolve_hw_rev(sku, metadata_root, hw_rev)
                       if perf_published else None)
    model_sha256: str | None = None
    reports: list[BackendReport] = []
    for backend in backends:
        variant = (resolve_ethos_u_variant(sku, metadata_root=metadata_root)
                   if backend == "ethos_u" else None)
        report = analyze_backend(backend=backend, src_format=src_format, ops=ops,
                                  metadata_root=metadata_root, variant=variant)
        if reader_missing and report.table is not None and not report.ops:
            report = _reader_missing_report(report)
        if exact:
            report = _apply_exact(report, backend, source, sku, metadata_root)
        if perf_published:
            if model_sha256 is None:
                model_sha256 = _model_sha256(source)
            # `target` is resolved here, once per backend, and handed to
            # `apply_perf_point` rather than let it re-resolve internally --
            # `tan.model.perf_apply` must never import `_headline_ethos_u_
            # target` back out of this module (that would be the cycle this
            # split exists to avoid), so the caller that already has this
            # function is the one that calls it.
            target = (_headline_ethos_u_target(sku, metadata_root)
                      if backend == "ethos_u" else None)
            report = apply_perf_point(report, backend=backend, sku=sku,
                                       model_sha256=model_sha256,
                                       hw_rev=resolved_hw_rev,
                                       metadata_root=metadata_root,
                                       target=target)
        reports.append(report)
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


#: A note is one line in the JSON envelope and the text render; a failed
#: `vela` subprocess can raise `RuntimeError` wrapping its own raw stderr,
#: which can be a multi-hundred-character, multi-newline Python traceback
#: (measured: 750 characters, 9 newlines) that must never land verbatim in
#: either (MAJOR 2 review). This budget governs FOREIGN text -- whatever
#: arbitrary thing vela's stderr happened to contain.
_VELA_ERR_NOTE_BUDGET = 200

#: `VelaFootprintRefused` gets a wider one, because it is a different kind of
#: string: tan AUTHORS it, one line by contract, from a fixed template over a
#: handful of bounded fields (accel config, two op counts, vela's own memory
#: areas + resolved profile names). At 200 it was cut at "... Refusing to
#: report a zero…" -- measured inner length 197 -- so the customer-facing note
#: in the text report AND the JSON envelope carried the diagnosis with none of
#: the remediation, which is the whole reason the refusal was reworded
#: (tan-cli#789 review BLOCKER 2 / MINOR 5). The two numbers below are RE-RUN
#: against the live template every time it is reworded, never carried forward
#: with a delta applied: a real `ethos-u85-256` refusal is 623 characters
#: (measured by compiling `tests/fixtures/models/tiny_int8.tflite` at
#: `ethos-u85-256` with real `ethos-u-vela` 5.1.0, carrying e8.json's own
#: `vendor_config_filename` and its no-DRAM answer, then taking
#: `len(str(exc))`), and a deliberately maximal one (three memory areas at
#: five significant figures, the longest profile name vela emits, four-digit
#: op counts, both metadata clauses) is 691 --
#: `test_the_refusal_note_budget_covers_a_maximal_refusal` builds exactly that
#: and fails if a future message outgrows this. Both were 3 and 2 characters
#: over-stated when last edited, because the reword's delta was estimated
#: rather than the message re-measured (tan-cli#789 review MINOR 1); no gate
#: reads either figure, so an assumed one rots silently. Still bounded and still
#: word-boundary-truncated with a trailing marker, so the one-line guarantee
#: is unchanged; the 750-character, 9-newline traceback stays governed by
#: `_VELA_ERR_NOTE_BUDGET` above, not by this.
_VELA_REFUSAL_NOTE_BUDGET = 700


#: Python's own traceback banner. When @err's message wraps a raw traceback (a
#: failed `vela` subprocess re-raising its stderr verbatim), the FIRST line is
#: the only one with no diagnostic content in it -- measured on a real run the
#: note read "--exact compile with vela failed (vela failed for ethos-u85-256:
#: Traceback (most recent call last):); reporting the static screen instead.",
#: discarding the actual cause ("RuntimeError: Compilation failed: No networks
#: defined via GraphAPI") that sat one line further down.
_TRACEBACK_BANNER = "Traceback (most recent call last):"


def _short_vela_error(err: Exception, *, budget: int = _VELA_ERR_NOTE_BUDGET) -> str:
    """ONE line of @err's message, cut to @budget characters on a word
    boundary with a trailing `…` when it doesn't already fit -- mirrors
    `tan.core.doctor_render._truncate_fix`'s word-boundary-plus-ellipsis
    convention. Never a mid-word cut, never a bare truncation with no marker,
    and never a multi-line traceback.

    WHICH line: for a wrapped traceback, the LAST non-empty one -- the
    exception line, the only one that says what actually went wrong -- kept
    behind whatever @err's own first line said BEFORE the banner (the accel
    config, in `VelaAdapter.compile`'s "vela failed for ethos-u85-256:"), so
    the note names both the target and the cause. Otherwise the first line,
    unchanged.

    @budget is a parameter rather than the bare constant because the two
    callers pass genuinely different kinds of string: arbitrary foreign vela
    stderr (`_VELA_ERR_NOTE_BUDGET`) versus tan's own single-line
    `VelaFootprintRefused` template (`_VELA_REFUSAL_NOTE_BUDGET`). Both are
    bounded; only the number differs."""
    text = str(err).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return text
    if _TRACEBACK_BANNER in text and len(lines) > 1:
        prefix = lines[0].split(_TRACEBACK_BANNER, 1)[0].strip()
        line = f"{prefix} {lines[-1]}".strip() if prefix else lines[-1]
    else:
        line = lines[0]
    if len(line) <= budget:
        return line
    cut = line[:budget]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return f"{cut}…"


def _footprint_refused_note(report: BackendReport, err: Exception) -> BackendReport:
    """`--exact` degrades to the static screen when tan refuses the footprint
    of an otherwise CLEAN vela run -- and says exactly that.

    NOT "compile with vela failed" (tan-cli#789 review NIT 8): vela exited 0,
    wrote its output and printed a real placement summary. What failed was
    tan's acceptance of the footprint it reported. Wording the two the same
    would send a customer hunting a vela bug that isn't there.

    The refusal text passes through at `_VELA_REFUSAL_NOTE_BUDGET`, so the
    remediation clause at its tail survives into the text report and the JSON
    envelope instead of being cut off mid-diagnosis."""
    detail = _short_vela_error(err, budget=_VELA_REFUSAL_NOTE_BUDGET)
    return replace(report, notes=[*report.notes,
                                  f"--exact: {detail} Reporting the static screen instead."])


def _maybe_exact_ethos_u(report: BackendReport, source: Path, sku: str,
                          metadata_root: Path) -> BackendReport:
    """Runs the real `vela` compile when it is on PATH; degrades cleanly --
    and SAYS SO, in a note -- back to @report (the static screen) for every
    other case: no vela, no resolvable accelerator config, a vela failure, or
    a footprint tan refused. Never raises. The compile succeeding is handed to
    `_report_from_vela_compile`, which is what actually decides `npu_coverage`
    -- NOT this function, and NOT vela's exit code (0 on a full CPU fallback
    by design).

    The refusal gets its OWN except-branch ahead of the general one
    (`_footprint_refused_note`): "compile with vela failed" is the wrong
    sentence for a run that exited 0 and printed a real placement summary, and
    the refusal's remediation tail needs the wider note budget to survive.

    Gated on `VelaAdapter().accepts(...)` directly, NOT on
    `report.ops[0].reason == "format-not-accepted"`: `extract_ops` (tensorio)
    only ever extracts operators from a `.tflite` source, so `report.ops` is
    `[]` for every OTHER format too -- meaning the reason-based check was
    dead code for exactly the case it existed to guard (ethos_u only ingests
    "tflite", so it only ever GETS "format-not-accepted" for a non-.tflite
    source, which is precisely when `ops` is always empty). Left ungated,
    `VelaAdapter().compile()` was invoked on e.g. a `.onnx` source under
    `--exact` on an Ethos-U SKU, and a `vela` failure on unparseable input
    can raise with its own raw, multi-line traceback as the message (MAJOR 2
    review: measured a 750-character, 9-newline traceback landing in
    `notes[1]` of the JSON envelope this way)."""
    if not VelaAdapter().accepts(source.suffix.lstrip(".").lower()):
        return report
    if shutil.which("vela") is None:
        note = ("--exact was requested, but vela is not on PATH (pip install "
                 "alp-tan[model-compile]); reporting the static screen instead.")
        return replace(report, notes=[*report.notes, note])
    target = _headline_ethos_u_target(sku, metadata_root)
    if target is None:
        note = ("--exact could not resolve an Ethos-U accelerator config for "
                 "this SKU; reporting the static screen instead.")
        return replace(report, notes=[*report.notes, note])
    accel_config = target.accel_config
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Accel-config, memory profile AND the part's diagnostic facts all
            # come off the SAME resolved target (`_headline_ethos_u_target`), so
            # `--exact` reports what `tan model build` would actually compile
            # for this SKU -- a screen run against a different memory model than
            # the build uses would be a figure the customer cannot act on, and a
            # refusal here has to read exactly as the build's would.
            blob = VelaAdapter().compile(
                source, accel_config=accel_config, out_dir=Path(tmp),
                vela_memory_mode=target.vela_memory_mode,
                vela_system_config=target.vela_system_config,
                vela_vendor_system_config=target.vela_vendor_system_config,
                vela_vendor_config_filename=target.vela_vendor_config_filename,
                soc_declares_dram=target.soc_declares_dram)
    except VelaFootprintRefused as err:
        return _footprint_refused_note(report, err)
    except Exception as err:  # noqa: BLE001 -- a failed exact compile degrades, it never crashes
        note = (f"--exact compile with vela failed ({_short_vela_error(err)}); "
                 f"reporting the static screen instead.")
        return replace(report, notes=[*report.notes, note])
    return _report_from_vela_compile(report, blob, accel_config)


def _vela_placement_note(blob: Blob, accel_config: str, total: int, pct: float,
                          sizing: str) -> str:
    """The one-line placement summary a `basis: "compiled"` report leads with.

    `blob.compiler_version` is ALREADY the full string to show -- "vela"
    (degraded) or "vela X.Y.Z" (real); wrapping it in a second "vela (...)"
    would read "vela (vela)" on the degraded path."""
    return (f"{blob.compiler_version} compiled for {accel_config}: "
            f"{blob.npu_op_count}/{total} operators placed on the NPU "
            f"({pct:.0f}%); {sizing}.")


# `arena_bytes`/`req_sram_kib` are carried as REPORT FIELDS as well as inside
# `sizing`'s prose below: `_perf_disagreements` compares a bench point against
# a compiled report figure-by-figure, and parsing tan's own sentence back to do
# that would be a second, silently-drifting spelling of the same two numbers.
def _report_from_vela_compile(report: BackendReport, blob: Blob, accel_config: str) -> BackendReport:
    """Turn a REAL, successful vela compile into a `BackendReport` driven by
    what vela actually placed on the NPU -- never by the fact that compile()
    didn't raise. **Vela exits 0 on a full CPU fallback by design** (measured:
    a float32 FULLY_CONNECTED model compiled for `ethos-u85-256` prints "NPU
    operators = 0 (0.0%)" and still exits 0), so "it didn't crash" is not
    proof of NPU placement -- that overclaim is exactly what this function
    replaces (the BLOCKER this fix addresses).

    Reads `blob.cpu_op_count`/`npu_op_count` (vela's own placement verdict):
    100% NPU (no CPU ops, at least one op ran) is the only case that reports
    `"fits"`, the only `npu_coverage` this module ever emits at `basis:
    "compiled"` -- `ops` stays `[]`. Partial or 0% NPU reports the REAL split
    (`npu_placement_pct_real`) and KEEPS @report's own per-op verdicts
    (`ops=report.ops`) rather than discarding them -- a 0%-NPU compile must
    never read as a bare, contextless success. An unparseable placement
    summary degrades to `_vela_placement_unreadable`, same as every other
    `_maybe_exact_ethos_u` failure path.

    `pct` -- vela's aggregate CPU/NPU op *count* split -- goes into
    `npu_placement_pct_real`, NEVER into `compute_on_npu_pct_max` (left
    `None` here): the latter's contract is a MAC-weighted figure
    (`analyze.BackendReport`'s own field comment), and vela's placement
    summary carries no per-op MAC data to weight `pct` by -- reusing that
    field for an op-count ratio was the unit mismatch MAJOR 4 review flagged.

    `blob.caveats` follow the placement note verbatim and are never dropped
    -- today, vela's own "may be invalid or non-optimal" verdict on the
    BUILT-IN default profile it fell back to. A `basis: "compiled"` report
    off a default-profile compile must say so; the figures above it describe
    vela's default memory model, not the module's (see
    `tan.model.adapters.ethos_u`'s module docstring)."""
    sizing = f"arena {blob.arena_bytes} bytes, SRAM {blob.req_sram_kib} KiB"
    outcome = _placement_outcome(report, blob.npu_op_count, blob.cpu_op_count)
    if outcome is None:
        return _vela_placement_unreadable(report, blob, accel_config, sizing)
    total = blob.npu_op_count + blob.cpu_op_count
    pct = 100.0 * blob.npu_op_count / total
    coverage, ops, uncosted = outcome
    note = _vela_placement_note(blob, accel_config, total, pct, sizing)
    return BackendReport(
        backend="ethos_u", variant=report.variant, table=report.table,
        npu_coverage=coverage, compute_on_npu_pct_max=None,
        npu_placement_pct_real=pct, uncosted_cpu_op_count=uncosted, ops=ops,
        basis="compiled", confidence="certain",
        arena_bytes=blob.arena_bytes, req_sram_kib=blob.req_sram_kib,
        notes=[note, *blob.caveats],
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
    return replace(report, notes=[*report.notes, note, *blob.caveats])


def _headline_ethos_u_target(sku: str, metadata_root: Path) -> TargetSpec | None:
    """The target `--exact` hands `vela`: `resolve_ethos_u_variant`'s own
    PRIMARY variant, at the highest `mac_per_cycle` config resolved for it --
    an arbitrary but deterministic tiebreak for a SoM shipping more than one
    NPU at the same variant (E1M-AEN701's `alif:ensemble:e7` lists a
    high-perf U55 at 256 MAC/cycle alongside a high-efficiency U55 at 128,
    `metadata/socs/alif/ensemble/e7.json`'s own `npus[]`). There is no
    per-SKU "preferred accel_config" field in the metadata this could read
    instead of guessing; None when the variant itself doesn't resolve
    (drpai/deepx_dxm1 SoMs) or no target matches it.

    Returns the whole `TargetSpec` rather than just its `accel_config` so the
    caller can hand the adapter this part's memory profile and its diagnostic
    facts alongside it (tan-cli#789 review (g)): all of them come off the SAME
    resolved target, so `--exact` can never describe one target's accelerator
    with another's memory model or another part's vendor `.ini`."""
    variant = resolve_ethos_u_variant(sku, metadata_root=metadata_root)
    if variant is None:
        return None
    prefix = f"ethos-{variant}-"
    candidates = [s for s in resolve_targets(sku, metadata_root=metadata_root)
                  if s.backend == "ethos_u" and s.accel_config.startswith(prefix)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: int(s.accel_config.rsplit("-", 1)[-1]))

