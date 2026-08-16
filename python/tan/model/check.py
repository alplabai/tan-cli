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
  * The tier-2 upgrade: [`_apply_perf_point`] swaps in a bench-measured
    `metadata/model_perf/` point (`tan.model.perf`) when alp-sdk publishes one
    for this EXACT identity -- the `basis: "bench"` half of the resolution
    order below.

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
(`_perf_disagreements` defines "disagrees" concretely, on the three figures
both sides actually report).

Does its own IO (reads `metadata/**` through `resolve_targets`/
`resolve_ethos_u_variant`, and `--exact` shells `vela`) -- the same stance
`tan.model.build`/`tan.model.targets` already take: IO lives in `tan.model`,
never in `tan.core` (see `tan.core.model_check`, which is the pure
serialisation/rendering half `tan.commands.model_cmd` calls alongside this).
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from .adapters import Blob
from .adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from .analyze import BackendReport, OpVerdict, analyze_backend, resolve_ethos_u_variant
from .perf import PerfPoint, coverage_from_placement, find_perf_points, has_perf_points
from .targets import TargetSpec, resolve_targets
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


# The perf-point step below is gated on `has_perf_points` and its digest is
# computed LAZILY behind that gate, once for all backends: no alp-sdk has
# published a `metadata/model_perf/` tree yet, and hashing every model source on
# every `check` to look inside a directory that does not exist would be pure cost
# for every customer alive today.
#
# `hw_rev` is the customer's own `board.yaml` `som.hw_rev`, or `None` when they
# declared none -- in which case `_resolve_hw_rev` falls back to the SKU preset's
# `default_hw_rev`, which is what the SDK itself does for an omitted key. It
# reaches ONLY the perf-point step: a bench measurement is pinned to the module
# revision it ran on, and nothing else in `check` is.
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
            report = _apply_perf_point(report, backend=backend, sku=sku,
                                        model_sha256=model_sha256,
                                        hw_rev=resolved_hw_rev,
                                        metadata_root=metadata_root)
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


def _placement_outcome(report: BackendReport, npu_ops: int | None,
                        cpu_ops: int | None) -> tuple[str, list[OpVerdict], int] | None:
    """(npu_coverage, ops, uncosted_cpu_op_count) for a MEASURED placement, or
    `None` when there is no readable placement at all.

    100% NPU is the only case that reports `"fits"`, and it drops `ops`: no
    per-op verdict can add anything to "every operator ran on the NPU".
    Partial or 0% KEEPS @report's static per-op verdicts AND its
    uncosted-cpu-op count -- a real partial/0% result must erase neither
    (MINOR review).

    Takes the two COUNTS rather than a `Blob`, so the identical keep/drop
    policy serves both measured surfaces: a real vela compile
    (`_report_from_vela_compile`) and a bench perf point
    (`_perf_point_report`). The coverage word itself comes from
    `tan.model.perf.coverage_from_placement` -- the one site in tan allowed to
    return `"fits"`."""
    coverage = coverage_from_placement(npu_ops, cpu_ops)
    if coverage is None:
        return None
    if coverage == "fits":
        return coverage, [], 0
    return coverage, report.ops, report.uncosted_cpu_op_count


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


# ---------------------------------------------------------------------------
# TIER 2: the manufacturer's own bench measurement (`metadata/model_perf/`).
#
# Everything below answers ONE question -- "did Alp Lab already measure THIS
# model, on THIS module, at THIS accelerator target?" -- and does nothing at
# all when the answer is no. That is not a courtesy, it is the contract: the
# same absence rule `npu-ops-v1` states for a missing op table applies here, so
# a customer whose model has never been benched (which is most of them) must
# get exactly the report they get today, note for note. `_apply_perf_point`
# therefore returns @report ITSELF on every non-match path rather than a
# rebuilt copy, so "unchanged" is structural rather than a promise.
# ---------------------------------------------------------------------------

#: backend -> the toolchain name whose measurements that backend's perf points
#: carry. These are alp-sdk's OWN established tool names -- the same ones its
#: op-support tables are keyed by (`npu_ops/ethos_u/u85@vela-5.1.0.json`,
#: `npu_ops/drpai/onnx-i8@translator-1.12.json`) and the three the perf-point
#: schema names -- not names tan invents. `cpu` is deliberately ABSENT rather
#: than mapped to a made-up name: a backend with no entry here is simply never
#: asked for a point, which is the right behaviour for one nobody benches.
_PERF_TOOLCHAIN = {"ethos_u": "vela", "drpai": "translator", "deepx_dxm1": "dxcom"}


def _model_sha256(source: Path) -> str:
    """Lowercase sha256 of @source's exact bytes -- the perf-point match key.

    Chunked, because a real model is not a small file (a production ONNX runs
    to tens of MB), and computed ONCE per `check` rather than once per backend.
    Any `OSError` propagates exactly as `extract_ops`' own read of the same file
    does: an unreadable source is `model.check-failed`, never a quietly degraded
    report."""
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_hw_rev(sku: str, metadata_root: Path, declared: str | None) -> str | None:
    """The module revision a perf point must have been measured on: the
    customer's own `board.yaml` `som.hw_rev` when they declared one, else the
    SKU preset's `default_hw_rev`.

    That fallback is the SDK's own rule, not an invention -- `board.schema.json`
    says of `som.hw_rev`: "Optional; the SDK falls back to the SKU preset's
    `default_hw_rev` when omitted" -- and both spellings use the same `rN` keys
    the perf-point schema requires, so no translation is involved.

    `None` when neither answers, and `find_perf_points` matches NOTHING on a
    `None`: an r2 measurement handed to a customer holding an r1 module is
    exactly the "exactly measured, describes a different machine" failure that
    put `hw_rev` in a point's identity in the first place (alp-sdk `f724d3e4`).
    Soft-fail on a preset that does not exist or does not parse, matching
    `resolve_ethos_u_variant`'s stance next door."""
    if declared:
        return declared
    preset_path = Path(metadata_root) / "e1m_modules" / f"{sku}.yaml"
    if not preset_path.is_file():
        return None
    try:
        preset = yaml.safe_load(preset_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    value = preset.get("default_hw_rev") if isinstance(preset, dict) else None
    return str(value) if value else None


def _profile_matches_the_part(point: PerfPoint, target: TargetSpec) -> bool:
    """Was @point captured under the memory profile THIS part declares?

    The tiebreak, and the only one applied: the SoC spec's own
    `npu_toolchain.vela` block (`TargetSpec.vela_memory_mode` /
    `vela_system_config` / `vela_vendor_system_config`) is what `tan model
    build` compiles this part under, so a point captured under the same profile
    is the one describing THIS module. Comparing on a silicon fact rather than
    on recency or on file order is the whole difference between choosing and
    guessing -- the collision that put the profile into a point's identity left
    a DRAM-backed capture as the arbitrary survivor on a part with no DRAM.

    A field the SoC spec does not declare is not compared: `system_config` is
    withheld from `TargetSpec` for every part shipped today (the Alif names live
    only in a proprietary `.ini`), so on those parts this narrows on the memory
    mode alone. `False` whenever there is nothing to compare at all -- an
    unnarrowable ambiguity must fall through, not resolve to "everything
    matches"."""
    system_config = target.vela_system_config or target.vela_vendor_system_config
    checks = [(target.vela_memory_mode, point.toolchain_memory_mode),
              (system_config, point.toolchain_system_config)]
    stated = [(declared, measured) for declared, measured in checks if declared]
    return bool(stated) and all(declared == measured for declared, measured in stated)


def _one_line(text: str) -> str:
    """@text with every run of whitespace collapsed to a single space.

    Applied to every FOREIGN string a perf-point note quotes (the capture
    citation, the toolchain version, the hardware revision): a note is one line
    in both the text report and the JSON envelope by contract, and these come
    out of a document in ANOTHER repository that this reader deliberately does
    not schema-validate."""
    return " ".join(text.split())


def _perf_measured_clauses(point: PerfPoint) -> list[str]:
    """The `measured` figures @point actually carries, as note clauses.

    A figure the point omits produces NO clause -- it was not measured, and
    the note must not imply a zero. `latency_runs` travels with the latency
    for the same reason the schema requires it: a latency without a run count
    is a single shot wearing a measurement's clothes."""
    total = (point.npu_ops or 0) + (point.cpu_ops or 0)
    clauses = [f"{point.npu_ops}/{total} operators on the NPU"]
    if point.arena_bytes is not None:
        clauses.append(f"arena {point.arena_bytes} bytes")
    if point.req_sram_kib is not None:
        clauses.append(f"{point.req_sram_kib} KiB resident SRAM")
    if point.latency_ms_mean is not None and point.runs is not None:
        latency = f"{point.latency_ms_mean} ms mean"
        if point.latency_ms_p95 is not None:
            latency += f", {point.latency_ms_p95} ms p95"
        clauses.append(f"{latency} over {point.runs} runs")
    return clauses


def _perf_identity(point: PerfPoint) -> str:
    """The measurement identity, as one readable clause -- module, hardware
    revision, driving core, accelerator target, toolchain AND its version, and
    the memory profile the figures describe. Every one of them is in the note
    because every one of them CHANGES THE NUMBER (alp-sdk `f724d3e4`), and a
    customer sizing hardware has to be able to see which machine was measured.

    The CORE and the VERSION are the two the match rule may leave unnarrowed --
    where the SoC spec pairs no core to this accelerator, and always for the
    version, since the tier-2 customer has no local toolchain to state one --
    so stating them here is the only way the customer learns which one this
    point actually describes. The PROFILE is there for the reason the schema
    makes it required on an Ethos-U point at all: an arena captured under a
    DRAM-backed profile is exactly measured and describes the wrong machine,
    and hiding it would re-open on the reading side the ambiguity the schema
    closed on the writing side."""
    target = point.accel_config or point.backend
    profile = ", ".join(_one_line(p) for p in
                        (point.toolchain_system_config, point.toolchain_memory_mode) if p)
    return (f"{point.sku} {_one_line(point.hw_rev)} (core {_one_line(point.core)}) "
            f"at {_one_line(target)} with {_one_line(point.toolchain)} "
            f"{_one_line(point.toolchain_version)}"
            + (f" ({profile})" if profile else ""))


def _perf_point_note(point: PerfPoint, *, corroborated: bool) -> str:
    """The one-line note a `basis: "bench"` report leads with.

    Carries `capture.reference` verbatim (whitespace-collapsed) because a
    number nobody can trace back to a run is not reproducible, and a perf point
    is worth exactly what its reproducibility is. Closes by saying that a local
    compile can differ -- true for every backend, unlike an `--exact`
    suggestion, which would be wrong advice on the two license-gated ones."""
    tail = ("This host's own compile agrees with it." if corroborated else
            "A compile on your own host can differ if its toolchain profile "
            "differs from the one recorded here.")
    return (f"bench: Alp Lab measured this exact model on {_perf_identity(point)} "
            f"-- {'; '.join(_perf_measured_clauses(point))}. Captured "
            f"{_one_line(point.capture_date)}, raw capture "
            f"{_one_line(point.capture_reference)}. {tail}")


def _perf_disagreements(point: PerfPoint, coverage: str,
                         report: BackendReport) -> list[str]:
    """Every concrete way @point and the customer's OWN `basis: "compiled"`
    @report differ -- empty when they agree on everything both measured.

    "Disagrees" is defined on the three figures BOTH sides really produce: the
    placement verdict, the tensor arena, and the resident SRAM. Nothing else is
    compared -- latency has no compiled counterpart to disagree with -- and a
    figure EITHER side did not report is never a disagreement: absence is not
    evidence here any more than anywhere else in this tier."""
    diffs: list[str] = []
    if coverage != report.npu_coverage:
        diffs.append(f"placement (bench {coverage}, local compile {report.npu_coverage})")
    for label, unit, measured, compiled in (
            ("arena", "bytes", point.arena_bytes, report.arena_bytes),
            ("SRAM", "KiB", point.req_sram_kib, report.req_sram_kib)):
        if measured is not None and compiled is not None and measured != compiled:
            diffs.append(f"{label} (bench {measured} {unit}, local compile {compiled} {unit})")
    return diffs


def _perf_disagreement_note(point: PerfPoint, diffs: list[str]) -> str:
    """Why the customer's own compile is being reported and OUR measurement is
    not -- with the measurement still named, so nothing is hidden."""
    return (f"--exact wins here: Alp Lab's bench point for this exact model on "
            f"{_perf_identity(point)} ({_one_line(point.capture_reference)}) "
            f"disagrees with this host's own compile -- {'; '.join(diffs)}. The "
            f"LOCAL compile is what is reported above: it ran this host's own "
            f"toolchain and profile, which is what a build here would produce.")


def _perf_point_report(report: BackendReport, point: PerfPoint,
                        outcome: tuple[str, list[OpVerdict], int]) -> BackendReport:
    """@report re-based on @point: `basis: "bench"`, `confidence: "certain"`.

    Drops @report's own notes and leads with the bench note, exactly as
    `_report_from_vela_compile` does for a real compile -- those notes describe
    a basis this report no longer has. `compute_on_npu_pct_max` stays `None`
    (it is MAC-weighted by contract and a placement carries no MAC breakdown);
    the real op-count ratio goes to `npu_placement_pct_real`, the same split
    the compiled path makes."""
    coverage, ops, uncosted = outcome
    total = (point.npu_ops or 0) + (point.cpu_ops or 0)
    return BackendReport(
        backend=report.backend, variant=report.variant, table=report.table,
        npu_coverage=coverage, compute_on_npu_pct_max=None,
        npu_placement_pct_real=100.0 * (point.npu_ops or 0) / total,
        uncosted_cpu_op_count=uncosted, ops=ops,
        basis="bench", confidence="certain",
        arena_bytes=point.arena_bytes, req_sram_kib=point.req_sram_kib,
        latency_ms_mean=point.latency_ms_mean, latency_ms_p95=point.latency_ms_p95,
        latency_runs=point.runs, perf_ref=point.capture_reference,
        notes=[_perf_point_note(point, corroborated=report.basis == "compiled")],
    )


def _resolve_perf_point(*, backend: str, sku: str, model_sha256: str,
                         hw_rev: str | None, metadata_root: Path) -> PerfPoint | None:
    """The ONE point that describes this customer's module, or `None`.

    Narrows on everything tan can SOURCE: the SKU, the module revision, the
    accel config `--exact` would compile for, the model's own bytes, and the
    toolchain name this backend's points are keyed by. It does NOT narrow on
    the toolchain VERSION -- there is no local toolchain to read one off in the
    tier-2 premise, and a point measured at another version is still OUR
    measurement (decision 1) -- and it narrows on the CORE only where the SoC
    spec pairs one to this accelerator (`npus[].paired_core`, `m55_hp` for the
    E8's high-perf U55). Where the spec declares no pairing (the E8's Ethos-U85
    today) nothing is inferred: the metadata does not know, so neither does tan.

    THE MULTI-MATCH RULE. What is left may legitimately be more than one point
    -- one model, one module, one core, one toolchain, captured under two
    different vela profiles, which are two different machines. Exactly ONE
    explicit, silicon-sourced tiebreak is applied: keep the points captured
    under the profile the SoC spec's own `npu_toolchain` block declares for this
    part (`_profile_matches_the_part`), i.e. the profile `tan model build`
    compiles it under. If that leaves exactly one, it answers. Anything else --
    still several, or none once narrowed -- falls through to the next fidelity
    tier, silently, rather than picking."""
    toolchain = _PERF_TOOLCHAIN.get(backend)
    if toolchain is None:
        return None
    target = _headline_ethos_u_target(sku, metadata_root) if backend == "ethos_u" else None
    if backend == "ethos_u" and target is None:
        return None
    points = find_perf_points(
        sku=sku, backend=backend, accel_config=target.accel_config if target else "",
        model_sha256=model_sha256, toolchain=toolchain, hw_rev=hw_rev,
        core=target.paired_core if target else None, metadata_root=metadata_root)
    if len(points) == 1:
        return points[0]
    if len(points) < 2 or target is None:
        return None
    narrowed = [p for p in points if _profile_matches_the_part(p, target)]
    return narrowed[0] if len(narrowed) == 1 else None


def _apply_perf_point(report: BackendReport, *, backend: str, sku: str,
                       model_sha256: str, hw_rev: str | None,
                       metadata_root: Path) -> BackendReport:
    """Tier 2, or @report untouched.

    Every early return hands back the SAME object, so an absent, ambiguous,
    fixture-marked or placement-less point cannot degrade a report by even a
    note. The last of those is the subtle one and is deliberate: a point that
    measured latency but recorded no operator placement cannot answer what
    `npu_coverage` asks, and re-basing on it would have to either invent a
    coverage or downgrade a real static verdict to `undetermined` -- both worse
    than the report the customer already had.

    DECISION 1 (tier-2 plan, Task 4): a `basis: "compiled"` report -- the
    customer really does hold the toolchain and really did compile -- WINS over
    a disagreeing point, and the point is named in a note instead. Their
    profile may not be ours, and the number they can act on is the one their own
    machine produced. When the two AGREE the point wins, because it adds
    measured wall-clock latency and a traceable capture that no compile can
    produce, and the local compile corroborating it is said out loud."""
    point = _resolve_perf_point(backend=backend, sku=sku, model_sha256=model_sha256,
                                 hw_rev=hw_rev, metadata_root=metadata_root)
    if point is None:
        return report
    outcome = _placement_outcome(report, point.npu_ops, point.cpu_ops)
    if outcome is None:
        return report
    if report.basis == "compiled":
        diffs = _perf_disagreements(point, outcome[0], report)
        if diffs:
            return replace(report, notes=[*report.notes,
                                           _perf_disagreement_note(point, diffs)])
    return _perf_point_report(report, point, outcome)
