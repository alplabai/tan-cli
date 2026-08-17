# SPDX-License-Identifier: Apache-2.0
"""Tier 2's MATCHING half: turn a customer's identity into the ONE
`metadata/model_perf/` point that describes their machine, and turn that point
into a `BackendReport`. `tan.model.perf` is deliberately only the READER (it
finds points and refuses to rank them, by its own module docstring); this is
the DECIDER, and `tan.model.check.check_model_backends` is its only caller.

Split out of `tan.model.check` (tan-cli#791 review NIT (c)): this is pure
string/decision logic with no subprocess in it -- `check.py` was 789 lines
against the house `module_cap: 800`, and `MODULE_SIZE_BUDGET_LOG.md`'s own
`ethos_u.py` entry already named exactly this shape of growth for a
neighbouring module ("the extraction to make when this file next grows").
This module takes the already-resolved Ethos-U target as a PARAMETER
(`target`) rather than resolving it itself (`_headline_ethos_u_target` stays
in `check.py`, which also needs it for `--exact`) -- passing it down avoids a
`check.py` <-> `perf_apply.py` import cycle, and it costs nothing: the caller
already has it in hand before either half of `check_model_backends`' per-model
work runs.

Everything below answers ONE question -- "did Alp Lab already measure THIS
model, on THIS module, at THIS accelerator target?" -- and does nothing at all
when the answer is no. That is not a courtesy, it is the contract: the same
absence rule `npu-ops-v1` states for a missing op table applies here, so a
customer whose model has never been benched (which is most of them) must get
exactly the report they get today, note for note. `apply_perf_point`
therefore returns @report ITSELF on every non-match path rather than a rebuilt
copy, so "unchanged" is structural rather than a promise.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import yaml

from .analyze import BackendReport, OpVerdict
from .perf import PerfPoint, coverage_from_placement, find_perf_points
from .targets import TargetSpec, resolve_targets


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
    (`tan.model.check._report_from_vela_compile`) and a bench perf point
    (`_perf_point_report` below). The coverage word itself comes from
    `tan.model.perf.coverage_from_placement` -- the one site in tan allowed to
    return `"fits"`."""
    coverage = coverage_from_placement(npu_ops, cpu_ops)
    if coverage is None:
        return None
    if coverage == "fits":
        return coverage, [], 0
    return coverage, report.ops, report.uncosted_cpu_op_count


#: backend -> the toolchain name whose measurements that backend's perf points
#: carry. These are alp-sdk's OWN established tool names -- the same ones its
#: op-support tables are keyed by (`npu_ops/ethos_u/u85@vela-5.1.0.json`,
#: `npu_ops/drpai/onnx-i8@translator-1.12.json`) and the three the perf-point
#: schema names -- not names tan invents. `cpu` is deliberately ABSENT rather
#: than mapped to a made-up name: a backend with no entry here is simply never
#: asked for a point, which is the right behaviour for one nobody benches.
#:
#: `cpu` STAYS absent on purpose (tan-cli#791 review, item 6): the schema's
#: `measured_on.backend` enum includes it, so alp-sdk COULD publish a
#: CPU-baseline campaign, but `resolve_check_backends` (`tan.model.check`)
#: already excludes `cpu` from what `tan model check` screens at all --
#: "how much of this model can target the NPU" has nothing to say about the
#: fallback CPU path itself, and that decision predates tier 2. Wiring `cpu`
#: in here without also revisiting that exclusion would publish points this
#: reader can never be asked for, which is worse than not wiring it: a
#: campaign planner who greps this dict for "which backends does tier 2 read"
#: gets a true, complete answer as of THIS decision, made here rather than
#: rediscovered from a silent no-op.
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
    `resolve_ethos_u_variant`'s stance next door.

    `declared` reaching here as `None` is itself a FAIL-CLOSED answer from the
    caller, not a "customer said nothing" default: `tan.commands.model_cmd.
    _declared_hw_rev` refuses the whole run outright (a `model.board-yaml-
    invalid` issue) when `som.hw_rev` is present but not a usable string --
    e.g. an unquoted YAML int (`hw_rev: 2`) -- rather than letting it fall
    through to here and silently pick up the preset's `default_hw_rev`
    instead (tan-cli#791 review MINOR 5: measured `{'som': {'hw_rev': 2}} ->
    None` serving an r2 preset measurement to a customer who wrote r2 but
    would have been served r1's if the preset defaulted there)."""
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


def _part_declares_a_profile(target: TargetSpec) -> bool:
    """Does the SoC spec that produced @target name a vela memory profile at
    all -- ANY of `vela_memory_mode` / `vela_system_config` /
    `vela_vendor_system_config`?

    Mirrors `_profile_matches_the_part`'s own `stated` computation exactly,
    because the two must agree: a part this says `True` about is one
    `_resolve_perf_point` (below) now narrows on BEFORE the single-point
    shortcut, and a part this says `False` about must not be narrowed on at
    all -- narrowing unconditionally would turn "the part is silent" into
    "reject every point", which is the opposite failure from the BLOCKER this
    function exists to close (tan-cli#791 review item 1): a part that states
    nothing has nothing for `_profile_matches_the_part` to ever agree with, so
    an unconditional narrow would empty out even the correctly-profiled
    ordinary case."""
    system_config = target.vela_system_config or target.vela_vendor_system_config
    return bool(target.vela_memory_mode) or bool(system_config)


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
    matches". CALLERS must gate this on `_part_declares_a_profile` first
    (above) when using it to REJECT rather than to TIEBREAK: this function
    alone cannot distinguish "nothing declared" from "declared and disagrees",
    and only the caller knows which failure mode it is currently guarding."""
    system_config = target.vela_system_config or target.vela_vendor_system_config
    checks = [(target.vela_memory_mode, point.toolchain_memory_mode),
              (system_config, point.toolchain_system_config)]
    stated = [(declared, measured) for declared, measured in checks if declared]
    return bool(stated) and all(declared == measured for declared, measured in stated)


def _paired_cores_for_backend(sku: str, backend: str, metadata_root: Path) -> set[str]:
    """Every core the SoC spec pairs to *some* NPU of @backend on this SKU's
    die -- not just the one specific accelerator `target` (below) names.

    Exists for the shipping case a single target's own `paired_core` cannot
    answer (tan-cli#791 review item 2): the E8 pairs each Ethos-U55 to an M55
    (`m55_hp`/`m55_he`) but pairs its Ethos-U85 to NOTHING -- yet nothing on
    that die ever pairs an Ethos-U NPU of ANY variant to the a32_cluster
    application core, so a point naming one describes a core that cannot have
    driven this backend on this SoC at all, whether or not the SPECIFIC
    accelerator being screened names a pairing of its own. Measured: a point
    with `measured_on.core: "a32_cluster"` used to match the E8's unpaired
    Ethos-U85 target outright and report `basis: "bench", confidence:
    "certain"` -- the exact MAJOR the review flagged, because the claim that
    "an unstated field ... never widens a match" is false the moment exactly
    one point is published, which is the realistic first-campaign state.

    `{}` when the spec pairs NO npu of this backend to any core at all --
    every drpai/deepx_dxm1 SoM published today (no SoC spec of either kind
    names `paired_core` anywhere), and any Ethos-U SoC spec that predates the
    field entirely -- and an empty set narrows on nothing, exactly like every
    other unstated field in this match."""
    return {t.paired_core for t in resolve_targets(sku, metadata_root=metadata_root)
            if t.backend == backend and t.paired_core}


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
    where NOTHING on this die pairs any NPU of this backend to a core
    (`_paired_cores_for_backend` empty: every drpai/deepx_dxm1 SoM, and an
    Ethos-U SoC predating `paired_core`), and always for the version, since the
    tier-2 customer has no local toolchain to state one -- so stating them here
    is the only way the customer learns which one this point actually
    describes. The PROFILE is there for the reason the schema makes it required
    on an Ethos-U point at all: an arena captured under a DRAM-backed profile
    is exactly measured and describes the wrong machine, and hiding it would
    re-open on the reading side the ambiguity the schema closed on the writing
    side."""
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


#: A note whose text starts with `"--exact"` is a diagnostic about THIS RUN's
#: HOST/toolchain environment for the customer's `--exact` request (vela
#: absent, license-gated, footprint refused, ...) -- see every note-authoring
#: site in `tan.model.check`'s exact-compile path, which all share this
#: prefix by construction. `_perf_point_report` (below) uses it to tell that
#: kind of note apart from a BASIS-describing one (the static-screen prose, a
#: real vela placement summary): the former survives a re-base onto a bench
#: point because it is still true of this run; the latter does not, because it
#: describes a basis the report no longer has (tan-cli#791 review item 4:
#: measured the "--exact was requested, but vela is not on PATH" note present
#: with no perf point and GONE the moment one matched).
_EXACT_HOST_NOTE_PREFIX = "--exact"


def _perf_point_report(report: BackendReport, point: PerfPoint,
                        outcome: tuple[str, list[OpVerdict], int]) -> BackendReport:
    """@report re-based on @point: `basis: "bench"`, `confidence: "certain"`.

    Drops @report's BASIS-describing notes and leads with the bench note,
    exactly as `tan.model.check._report_from_vela_compile` does for a real
    compile -- those notes describe a basis this report no longer has. Notes
    that instead describe the HOST/toolchain environment for a `--exact`
    request (`_EXACT_HOST_NOTE_PREFIX`) are still true of this run and are
    KEPT, appended after the bench note (tan-cli#791 review item 4) --
    dropping them silently un-answers a customer's own `--exact` ask the
    moment a bench point happens to exist.

    `compute_on_npu_pct_max` stays `None` (it is MAC-weighted by contract and
    a placement carries no MAC breakdown); the real op-count ratio goes to
    `npu_placement_pct_real`, the same split the compiled path makes."""
    coverage, ops, uncosted = outcome
    total = (point.npu_ops or 0) + (point.cpu_ops or 0)
    host_notes = [n for n in report.notes if n.startswith(_EXACT_HOST_NOTE_PREFIX)]
    return BackendReport(
        backend=report.backend, variant=report.variant, table=report.table,
        npu_coverage=coverage, compute_on_npu_pct_max=None,
        npu_placement_pct_real=100.0 * (point.npu_ops or 0) / total,
        uncosted_cpu_op_count=uncosted, ops=ops,
        basis="bench", confidence="certain",
        arena_bytes=point.arena_bytes, req_sram_kib=point.req_sram_kib,
        latency_ms_mean=point.latency_ms_mean, latency_ms_p95=point.latency_ms_p95,
        latency_runs=point.runs, perf_ref=point.capture_reference,
        notes=[_perf_point_note(point, corroborated=report.basis == "compiled"), *host_notes],
    )


def _resolve_perf_point(*, backend: str, sku: str, model_sha256: str,
                         hw_rev: str | None, metadata_root: Path,
                         target: TargetSpec | None) -> PerfPoint | None:
    """The ONE point that describes this customer's module, or `None`.

    @target is the ALREADY-RESOLVED Ethos-U accelerator
    (`tan.model.check._headline_ethos_u_target`'s answer for @backend ==
    `"ethos_u"`, `None` for every other backend) -- passed in rather than
    resolved here so this module never has to import `check.py` back.

    Narrows on everything tan can SOURCE: the SKU, the module revision, the
    accel config `--exact` would compile for, the model's own bytes, and the
    toolchain name this backend's points are keyed by. It does NOT narrow on
    the toolchain VERSION -- there is no local toolchain to read one off in the
    tier-2 premise, and a point measured at another version is still OUR
    measurement (decision 1).

    CORE narrows in TWO STEPS, not one. The exact-match query below narrows to
    @target's own `paired_core` when @target names one (the E8's Ethos-U55s).
    When it does not (the E8's Ethos-U85 -- unpaired on this die, but sharing
    it with two Ethos-U55s that ARE paired), `_paired_cores_for_backend`
    additionally refuses any point whose core is not one this die pairs to
    SOME npu of this backend, which still rules out a core -- an A-cluster --
    that drives no Ethos-U NPU on this die at all (tan-cli#791 review item 2).
    Only a backend/SoC that pairs NO npu at all to a core (every drpai/
    deepx_dxm1 SoM today) leaves core fully unnarrowed, exactly as before.

    THE PROFILE TIEBREAK NARROWS BEFORE THE SINGLE-POINT SHORTCUT, not after
    (tan-cli#791 review item 1, the BLOCKER). A part that declares a vela
    memory profile (`_part_declares_a_profile`) only ever matches a point
    captured under that SAME profile -- a lone point captured under a profile
    the part does NOT declare (a DRAM-backed default measured against a
    Sram_Only part) must be refused exactly as decisively as a second,
    wrongly-profiled point in a multi-point tie already was: the old code
    applied this ONLY once two or more points survived the sourced filters
    above, so a single wrong-profile point was returned unfiltered at
    `basis: "bench", confidence: "certain"` -- overstating resident SRAM by
    5.3x in the measured case (a DRAM-backed default capture against
    E8-class e8.json's `Sram_Only`-declaring part). Applied only where the
    part actually states a profile: narrowing on a part that states nothing
    would reject every point outright, the opposite failure.

    THE MULTI-MATCH RULE, for what is left. More than one point can
    legitimately still stand -- one model, one module, one core, one
    toolchain, captured under two different vela profiles the PART itself
    leaves unconstrained (`_part_declares_a_profile` false for it). Nothing
    further is picked: still several, or none once narrowed, falls through to
    the next fidelity tier, silently, rather than guessing."""
    toolchain = _PERF_TOOLCHAIN.get(backend)
    if toolchain is None:
        return None
    if backend == "ethos_u" and target is None:
        return None
    points = find_perf_points(
        sku=sku, backend=backend, accel_config=target.accel_config if target else "",
        model_sha256=model_sha256, toolchain=toolchain, hw_rev=hw_rev,
        core=target.paired_core if target else None, metadata_root=metadata_root)
    allowed_cores = _paired_cores_for_backend(sku, backend, metadata_root)
    if allowed_cores:
        points = [p for p in points if p.core in allowed_cores]
    if target is not None and _part_declares_a_profile(target):
        points = [p for p in points if _profile_matches_the_part(p, target)]
    return points[0] if len(points) == 1 else None


def apply_perf_point(report: BackendReport, *, backend: str, sku: str,
                      model_sha256: str, hw_rev: str | None,
                      metadata_root: Path, target: TargetSpec | None) -> BackendReport:
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
    machine produced. THE POINT'S OWN LATENCY AND CAPTURE REFERENCE ARE STILL
    CARRIED AS FIELDS on that disagreement report (tan-cli#791 review item 3):
    a compile has no wall-clock or raw-capture counterpart to disagree WITH, so
    `perf_ref`/`latency_ms_mean`/`latency_ms_p95`/`latency_runs` are never part
    of `_perf_disagreements`' comparison -- they are Alp Lab's own measured
    figures, offered alongside the LOCAL compile's coverage/arena/SRAM numbers
    that won on those three, not competing with them. Without this the
    `perfRef` the note's own prose quotes was `null` in the very same envelope,
    forcing a consumer to parse tan's own sentence back to recover it --
    precisely the second, silently-drifting spelling the comment right above
    `tan.model.check._report_from_vela_compile` already refuses to create for
    `arena_bytes`/`req_sram_kib`.

    When the two AGREE the point wins, because it adds measured wall-clock
    latency and a traceable capture that no compile can produce, and the local
    compile corroborating it is said out loud."""
    point = _resolve_perf_point(backend=backend, sku=sku, model_sha256=model_sha256,
                                 hw_rev=hw_rev, metadata_root=metadata_root, target=target)
    if point is None:
        return report
    outcome = _placement_outcome(report, point.npu_ops, point.cpu_ops)
    if outcome is None:
        return report
    if report.basis == "compiled":
        diffs = _perf_disagreements(point, outcome[0], report)
        if diffs:
            return replace(report, notes=[*report.notes,
                                           _perf_disagreement_note(point, diffs)],
                           perf_ref=point.capture_reference,
                           latency_ms_mean=point.latency_ms_mean,
                           latency_ms_p95=point.latency_ms_p95,
                           latency_runs=point.runs)
    return _perf_point_report(report, point, outcome)
