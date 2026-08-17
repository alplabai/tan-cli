# SPDX-License-Identifier: Apache-2.0
"""Manufacturer-precomputed model perf points -- the READER half of tier 2.

alp-sdk publishes bench-measured perf points under `metadata/model_perf/`
(`metadata/schemas/model-perf-v1.schema.json`), exactly as it already publishes
op-support tables under `metadata/npu_ops/`. This module finds the ONE point
that describes a given measurement identity, so `tan model check` can answer
"does this model run on this module, and how fast" for a customer holding
NEITHER the NPU toolchain NOR the silicon -- the middle fidelity tier between
the always-offline static screen (`tan.model.analyze`, `basis:
"static-screen"`) and the customer's own `--exact` compile (`tan.model.check`,
`basis: "compiled"`). A match is what finally produces the `basis: "bench"`
that `analyze.py`'s vocabulary has reserved from the beginning and that nothing
has ever emitted.

WHAT THIS READER REFUSES TO MATCH IS ITS WHOLE VALUE. A perf point is an exact
number a customer sizes hardware from precisely because it says "bench", so a
point describing a NEARBY identity is worse than no point at all:

  * **No "closest model".** Part of the match key is `model.sha256` -- the
    exact bytes -- never `model.slug`, which is a human label two different
    byte-sequences can share. Re-quantize a model and its old point stops
    applying.
  * **No "same accelerator family".** `ethos-u85-256` is not `ethos-u85-128`
    and not `ethos-u55-256`; `accel_config` is compared as an exact string.
  * **No "same module, different revision".** An r1 and an r2 measurement of
    one model on one target are different measurements (alp-sdk `f724d3e4`),
    so `hw_rev` is a REQUIRED query field: a caller that cannot say which
    revision it holds gets nothing rather than the other revision's number.
  * **No "same die, different core".** An A-cluster and an M-class inference
    of one model differ by orders of magnitude, and on Ethos-U the U55 paired
    to the HP core is not the one paired to the HE core.
  * **No fixture data, ever.** A point carrying alp-sdk's `_fixture` banner is
    a synthetic document whose `measured` figures are placeholders; it is
    refused outright, never consumed with a caveat. `confidence: "certain"` on
    a placeholder is the single worst output this tier can produce.
  * **No ambiguity resolved by preference.** More than one point left standing
    by the match key is genuine ambiguity: the toolchain PROFILE is part of a
    point's file identity but deliberately NOT part of the key, because a
    customer with no toolchain cannot state a profile. Picking one would author
    a preference the data does not express -- and the collision that forced
    this design left the DRAM-backed profile as the survivor, which is exactly
    measured and describes the wrong machine. `find_perf_points` therefore
    hands the caller ALL of them and lets a caller with a silicon fact decide
    (`tan.model.check` uses the SoC spec's own `npu_toolchain` profile);
    `find_perf_point` hands back `None`.

Every refusal above returns `None`, SILENTLY. Absence of a point means "we have
not benched this", never "it does not fit" -- the same rule `npu-ops-v1` states
for a missing op table, and the reason a missing `metadata/model_perf/`
directory is handled here as the ORDINARY case rather than an error: no alp-sdk
published one before the first bench campaign, and a consumer that treated its
absence as exceptional would break `tan model check` on every SDK checkout in
existence.

Nothing in this module raises. A malformed, truncated or schema-violating point
is simply not a match: the producing side (alp-sdk's
`scripts/validate_metadata.py` + `tests/scripts/test_model_perf_metadata.py`)
is where a bad point is made LOUD, and a consumer that also failed loudly would
turn a metadata defect in another repository into a broken `tan model check`
for a customer who cannot fix it.

`coverage_from_placement` lives here rather than in `tan.model.check` because
it is THE one function in tan that may return the word `"fits"`, and both
surfaces permitted to say it -- a real compile and a bench point -- answer the
same question from the same kind of evidence (a real per-operator placement,
measured, not screened). One site is what makes the guard on that word
(`tests/model/test_check.py`) bind to the live rule for both.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The published tree's directory name under a `metadata/` root. Named once
#: here so a consumer never has to spell it, and so `has_perf_points` and
#: `find_perf_point` cannot drift apart on it.
_PERF_DIRNAME = "model_perf"

#: The banner alp-sdk stamps on the synthetic points under
#: `tests/fixtures/model_perf/`, which exist so its own perf-point checks run
#: against a real document instead of passing vacuously over an empty
#: `metadata/model_perf/`. alp-sdk's `validate_metadata.py` REFUSES this key on
#: anything under `metadata/model_perf/`; this reader refuses it wherever it
#: appears, so a fixture that ever escaped into a published tree still could
#: not be read as bench data by a customer's `tan`.
_FIXTURE_KEY = "_fixture"

#: The only `stance` a readable point may declare. The schema's enum has
#: exactly this one member today; the enum exists so that a future DERIVED
#: asset (a per-NPU estimate calibrated to these points, which the roadmap
#: plans) has to arrive as a distinguishable kind rather than silently
#: redefining this one. Checked here for the same reason -- an estimate this
#: reader mistook for a measurement would ship at `confidence: "certain"`.
_MEASURED_STANCE = "bench-measured"


@dataclass(frozen=True)
class PerfPoint:
    """One bench-measured perf point, as published.

    Identity fields are all required and always populated -- a document
    missing any of them is not a point and never becomes one of these. Every
    `measured` field is OPTIONAL and `None` means "not measured", never zero:
    the schema omits a figure nobody measured rather than zero-filling it, so
    a zero here is a MEASURED zero.
    """

    path: Path                    # where it was read from, for traceability
    sku: str                       # measured_on.sku
    hw_rev: str                    # measured_on.hw_rev, e.g. "r2"
    core: str                      # measured_on.core, e.g. "m55_hp"
    backend: str                   # measured_on.backend
    accel_config: str              # measured_on.accel_config; "" when N/A
    model_slug: str                # model.slug -- a LABEL, never a match key
    model_sha256: str              # model.sha256 -- part of the match key
    model_size_bytes: int          # model.size_bytes
    model_source: str              # model.source -- provenance for those bytes
    toolchain: str                 # toolchain.name, e.g. "vela"
    toolchain_version: str         # toolchain.version, e.g. "5.1.0"
    capture_date: str              # capture.date
    capture_operator: str          # capture.operator
    capture_reference: str         # capture.reference -- the raw capture's citation
    # The MEMORY PROFILE the figures describe. `None` for a backend that has
    # none, but never for an `ethos_u` point: the schema makes both REQUIRED
    # there, because invoked flagless, `ethos-u-vela` 5.1.0 silently picks a
    # DRAM-backed default per accelerator and the arena figures then describe
    # THAT profile rather than the module. A point that did not record the
    # profile is exactly measured and describes an unknown machine -- so a
    # consumer that HAS them must show them, or it re-creates on the reading
    # side the ambiguity the schema closed on the writing side.
    toolchain_system_config: str | None = None
    toolchain_memory_mode: str | None = None
    npu_ops: int | None = None
    cpu_ops: int | None = None
    arena_bytes: int | None = None
    req_sram_kib: int | None = None
    latency_ms_mean: float | None = None
    latency_ms_p95: float | None = None
    runs: int | None = None
    notes: str | None = None


def coverage_from_placement(npu_ops: int | None, cpu_ops: int | None) -> str | None:
    """The `npu_coverage` a REAL per-operator placement supports -- and the one
    function in tan that may return `"fits"`.

    `"fits"` only at 100% NPU placement (no CPU operator, and at least one
    operator placed); `"cpu-only"` at 0%; `"partial"` in between. `None` when
    there is no placement to read at all -- either count absent, or both zero
    (an unreadable summary, not an all-NPU one). A caller handed `None` must
    NOT manufacture a verdict from it: "nothing said it fell back to the CPU"
    is not evidence that it did not.

    Both callers measure the same thing and share this rule verbatim:
    `tan.model.check._placement_outcome` (a real vela compile's own "CPU/NPU
    operators = N" summary) and the bench path next to it (a point's
    `measured.npu_ops`/`cpu_ops`, i.e. what the compiler that produced the
    MEASURED artefact placed). One site is what makes the `"fits"` guard bind
    to the live rule for both surfaces rather than to a copy of it -- that
    guard was already found decorative once for exactly that reason.
    """
    if npu_ops is None or cpu_ops is None:
        return None
    if npu_ops + cpu_ops == 0:
        return None
    if cpu_ops == 0:
        return "fits"
    return "cpu-only" if npu_ops == 0 else "partial"


def perf_points_root(metadata_root: Path) -> Path:
    """`metadata/model_perf/` under @metadata_root. May not exist."""
    return Path(metadata_root) / _PERF_DIRNAME


def has_perf_points(metadata_root: Path) -> bool:
    """Does this SDK checkout publish a perf-point tree at all?

    A cheap `is_dir()` a caller uses to skip work it would otherwise do for
    nothing -- notably hashing the model source, which `find_perf_point` needs
    and which is pure waste on the (currently universal) checkout publishing no
    points. NOT a precondition: `find_perf_point` makes the same check itself
    and answers `None`, so a caller that skips this is correct, just slower.
    """
    return perf_points_root(metadata_root).is_dir()


def target_dir_name(backend: str, accel_config: str) -> str:
    """The `<target>` path segment for a (backend, accel_config) pair: the
    accel config when the backend has one, the backend name when it does not
    (`drpai`, `deepx_dxm1`, `cpu`) -- the schema's own rule, spelled once."""
    return accel_config or backend


def _text(doc: dict, key: str) -> str | None:
    value = doc.get(key)
    return value if isinstance(value, str) else None


def _count(doc: dict, key: str) -> int | None:
    value = doc.get(key)
    # `bool` is an `int` subclass and `True` is not a measurement.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _millis(doc: dict, key: str) -> float | None:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # `>= 0`, not `> 0`: a `0` here is a MEASURED zero (`PerfPoint`'s own
    # docstring), the same reading `_count` above already gives a `0` op
    # count. `> 0` silently turned a real "0.0 ms" latency into "not
    # measured" -- indistinguishable from an omitted key on the wire
    # (tan-cli#791 review NIT (a)).
    return float(value) if value >= 0 else None


def _identity(doc: dict) -> dict | None:
    """@doc's identity fields as plain values, or `None` when any is missing
    or of the wrong type.

    `accel_config` is REQUIRED-AND-MAY-BE-EMPTY and the distinction is
    load-bearing: `""` is a backend that HAS no accel config, while an absent
    key is a point that failed to record one. `_text` separates them -- `""`
    is a `str` and survives the emptiness test below, `None` is the
    missing-key answer and refuses.
    """
    measured_on, model, toolchain, capture = (
        doc.get(k) for k in ("measured_on", "model", "toolchain", "capture"))
    if not all(isinstance(block, dict)
               for block in (measured_on, model, toolchain, capture)):
        return None
    out = {
        "sku": _text(measured_on, "sku"),
        "hw_rev": _text(measured_on, "hw_rev"),
        "core": _text(measured_on, "core"),
        "backend": _text(measured_on, "backend"),
        "model_slug": _text(model, "slug"),
        "model_sha256": _text(model, "sha256"),
        "model_source": _text(model, "source"),
        "toolchain": _text(toolchain, "name"),
        "toolchain_version": _text(toolchain, "version"),
        "capture_date": _text(capture, "date"),
        "capture_operator": _text(capture, "operator"),
        "capture_reference": _text(capture, "reference"),
    }
    accel_config = _text(measured_on, "accel_config")
    size_bytes = _count(model, "size_bytes")
    if not all(out.values()) or accel_config is None or not size_bytes:
        return None
    return {**out, "accel_config": accel_config, "model_size_bytes": size_bytes}


def read_perf_point(path: Path) -> PerfPoint | None:
    """Parse ONE perf-point file, or `None` for anything this reader will not
    treat as a measurement.

    `None` covers: unreadable/unparseable bytes, a document that is not a JSON
    object, a `_fixture`-bannered synthetic, a `stance` that is not
    `bench-measured`, and any document missing an identity field. Never raises
    -- see the module docstring on why a consumer stays quiet where the
    producing repository's own gate is loud.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or _FIXTURE_KEY in doc:
        return None
    if doc.get("stance") != _MEASURED_STANCE:
        return None
    identity = _identity(doc)
    measured = doc.get("measured")
    if identity is None or not isinstance(measured, dict):
        return None
    toolchain = doc["toolchain"]
    return PerfPoint(
        path=path, **identity,
        toolchain_system_config=_text(toolchain, "system_config"),
        toolchain_memory_mode=_text(toolchain, "memory_mode"),
        npu_ops=_count(measured, "npu_ops"), cpu_ops=_count(measured, "cpu_ops"),
        arena_bytes=_count(measured, "arena_bytes"),
        req_sram_kib=_count(measured, "req_sram_kib"),
        latency_ms_mean=_millis(measured, "latency_ms_mean"),
        latency_ms_p95=_millis(measured, "latency_ms_p95"),
        runs=_count(measured, "runs"),
        notes=_text(doc, "notes"),
    )


# ---------------------------------------------------------------------------
# THE MATCH RULE (alp-sdk `f724d3e4`), and the three things about it that are
# DECISIONS rather than mechanics.
#
# THE KEY IS EIGHT FIELDS: sku + hw_rev + core + backend + accel_config +
# model.sha256 + toolchain.name + toolchain.version. Every one of them changes
# the number, which is why every one of them is also in the filename -- a
# segment left out of the path is a segment on which a second measurement
# silently overwrites the first, and the collision that forced this shape made
# an r1 and an r2 point of one model resolve to a single file.
#
# THREE OF THOSE EIGHT ARE OPTIONAL *QUERY* FIELDS, because a caller does not
# always hold the fact. `hw_rev` is not one of them: a caller that cannot say
# which module revision it has gets NOTHING, since serving an r2 measurement to
# an r1 customer is precisely the "describes a different machine" failure. But
# `core` and `toolchain_version` may be left unstated -- there is no local
# toolchain to read a version off in the tier-2 premise, and nothing ties a
# declared model to a core on a multi-core module -- and an unstated field is
# simply not narrowed on BY THIS FUNCTION: within `find_perf_points` alone, it
# is never guessed, and it never widens a match, it only leaves MORE points
# standing here.
#
# THAT IS NOT THE WHOLE PIPELINE'S GUARANTEE, though, and stating it as one
# used to overclaim (tan-cli#791 review item 2 -- the eight-identity-fields
# match was six fields exact plus two that narrowed only sometimes, not
# eight). A CALLER holding a sourced silicon fact this function's own query
# shape has no field for -- "which cores does this die pair to ANY npu of
# this backend", not just to the one specific accelerator being screened --
# may narrow FURTHER, by filtering what this function hands back
# (`tan.model.perf_apply._paired_cores_for_backend`). `core` can therefore
# still end up narrowed in practice even when the QUERY passed here left it
# `None`; what never happens, in this function or the layer above it, is
# WIDENING -- inferring a match a sourced fact does not support.
#
# THE TOOLCHAIN PROFILE IS IDENTITY BUT NOT KEY. `system_config`/`memory_mode`/
# `pins` are digested into the filename's `+<profile12>` segment, but a customer
# with no toolchain cannot state a profile, so requiring one would make tier 2
# unusable for exactly the person it exists for. The consequence is that a
# lookup can legitimately leave MORE THAN ONE point standing -- one model, one
# module, one core, one toolchain, captured under two different vela profiles --
# and those describe DIFFERENT MACHINES. `find_perf_points` returns all of them
# and picks nothing; `find_perf_point` returns `None`. Picking arbitrarily is
# what the collision test already demonstrated the cost of: its survivor was the
# DRAM-backed profile, exactly measured and describing a part with no DRAM. A
# caller holding a silicon fact may of course choose -- `tan.model.perf_apply`
# narrows on the profile the SoC spec's own `npu_toolchain` block declares for
# the part, and it does so BEFORE trusting even a single standing point, not
# only as a tiebreak once two or more survive (tan-cli#791 review item 1 -- a
# lone point captured under a profile the part does not declare used to be
# handed back unfiltered at `confidence: "certain"`) -- but that decision
# belongs where the silicon facts are, not here.
#
# THE PATH IS AN INDEX, THE BODY IS THE TRUTH. `<root>/<sku>/<target>/` narrows
# the search cheaply, but every field is then re-checked against the document
# itself, so a file whose body disagrees with where it sits is not found here.
# alp-sdk's own validator refuses to publish one, and this reader does not have
# to trust that it did. Nothing here parses the FILENAME -- the `+<hw_rev>+
# <core>+<profile12>` segments are alp-sdk's collision-avoidance mechanism, and
# a consumer that re-derived identity from them would be reading the index
# instead of the record.
# ---------------------------------------------------------------------------


def find_perf_points(*, sku: str, backend: str, accel_config: str,
                      model_sha256: str, toolchain: str, hw_rev: str | None,
                      metadata_root: Path, core: str | None = None,
                      toolchain_version: str | None = None) -> list[PerfPoint]:
    """EVERY published point this identity leaves standing, in path order.

    `[]` for no match, ONE entry for the ordinary case, and MORE THAN ONE when
    the caller could not narrow far enough to separate two real measurements --
    see the block comment above. A caller that cannot choose between them must
    fall through to the next fidelity tier, never take `[0]`.

    @sku, @backend, @accel_config (exact string; `""` for a backend that has
    none), @model_sha256 and @toolchain (the toolchain NAME: `vela`, `dxcom`,
    `translator`) are always compared. The digest is compared case-folded --
    hex case is a spelling of one value, not a different one -- and a
    @model_sha256 that is not a full 64-hex digest is refused outright, because
    a truncated or prefix digest is the near-miss that would let two models
    share one measurement. The toolchain NAME comes from the caller rather than
    being derived from @backend, so this function never invents one for a
    backend nobody has benched.

    @hw_rev is required-but-nullable and `None` matches NOTHING: see above.
    @core and @toolchain_version narrow only when given.
    """
    root = perf_points_root(metadata_root)
    if not root.is_dir():
        return []                       # the ordinary case, not an error
    # No separate `hw_rev is None: return []` early return here (tan-cli#791
    # review NIT (b)): `point.hw_rev == hw_rev` below already refuses on its
    # own -- `PerfPoint.hw_rev` is a REQUIRED, always-non-empty field
    # (`_identity`'s own `all(out.values())` gate), so it can never equal a
    # `None` query value, and the dedicated early return was provably dead
    # (measured: deleting it alone left `tests/model` fully green; only the
    # comparison operator's own mutants went red).
    wanted = model_sha256.strip().lower()
    if len(wanted) != 64 or any(c not in "0123456789abcdef" for c in wanted):
        return []
    target_dir = root / sku / target_dir_name(backend, accel_config)
    if not target_dir.is_dir():
        return []
    return [
        point for point in (read_perf_point(p) for p in sorted(target_dir.glob("*.json")))
        if point is not None
        and point.sku == sku
        and point.hw_rev == hw_rev
        and point.backend == backend
        and point.accel_config == accel_config
        and point.model_sha256.lower() == wanted
        and point.toolchain == toolchain
        and (core is None or point.core == core)
        and (toolchain_version is None or point.toolchain_version == toolchain_version)
    ]


def find_perf_point(**query) -> PerfPoint | None:
    """`find_perf_points`' answer when it is UNAMBIGUOUS -- the sole standing
    point, or `None`.

    `None` folds "no match" and "more than one point standing" into one silent
    answer on purpose: a caller with no way to choose between two measurements
    of two different machines must do the same thing in both cases, which is
    fall through. A caller that CAN choose calls `find_perf_points` and applies
    its own, explicit, sourced rule."""
    matches = find_perf_points(**query)
    return matches[0] if len(matches) == 1 else None
