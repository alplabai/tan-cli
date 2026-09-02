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
`basis: "compiled"`).

THE AUTHORITATIVE SCHEMA (tan-cli#1115). Read from alp-sdk directly, not
inferred from one fixture: `metadata/schemas/model-perf-v1.schema.json`
(`additionalProperties: false` at every level -- a published point can carry
NO field this schema doesn't name), cross-checked by `scripts/
validate_metadata.py`'s `_check_model_perf_semantics()`, and demonstrated by
`tests/fixtures/model_perf/e1m_aen801_ethos_u55_hp.yaml`. A valid point is:

    schema_version: 1
    sku: E1M-...                       # top level, not nested
    hw_rev: rN                         # top level, not nested
    model: {name, src_sha, format}      # src_sha is the 64-hex match key
    target: {backend, accel_config, core, compiler_version}
    vela: {system_config, memory_mode}  # required only when target.backend
                                         # == "ethos_u"; absent otherwise
    perf: {req_sram_kib, arena_bytes, latency_ms?: {mean, p50, p95, stdev?, runs}}
    capture: {date, operator, bench_id, notes?}

BEFORE THIS RECONCILIATION, `PerfPoint`/`_identity()`/`read_perf_point()`
recognised a DIFFERENT, never-published shape -- a top-level `stance` field,
a `measured_on` block, and a `measured` block carrying `npu_ops`/`cpu_ops`
op-placement counts. None of that exists in alp-sdk's real schema, has never
existed in its git history (`git log -- metadata/schemas/model-perf-v1.schema.json`
is one commit, `9b466018` (alp-sdk #1884, Refs #1520), and it always looked
like the shape above), and `additionalProperties: false` means it never can
without a new schema version alp-sdk would have to author and this reader
would have to re-reconcile against. So the old fields are DEAD, not
renamed-with-a-gap, and are deleted rather than kept unpopulated:

  * `stance` had no real counterpart at all -- deleted outright. Publication
    under `metadata/model_perf/` (vs. `tests/fixtures/model_perf/`, see the
    fixture rule below) is alp-sdk's own way of saying "this is a real
    measurement"; there is no in-document flag for it.
  * `measured_on.{sku,hw_rev,core,backend,accel_config}` map onto top-level
    `sku`/`hw_rev` and `target.{core,backend,accel_config}`.
  * `measured.{npu_ops,cpu_ops}` are GENUINELY DEAD: no field anywhere in the
    real schema records a per-operator NPU/CPU placement split. A bench point
    can state that a model fits in a given SRAM/arena budget and (once a
    timing harness exists) how fast it ran -- the schema's own `perf`
    description says the first capture campaign "can record fit/SRAM alone
    until a timing harness exists" -- but not what fraction of its operators
    the compiler placed on the NPU. `tan.model.perf_apply` no longer asks a
    bench point for a placement percentage; see that module's docstring for
    what a matched point can and cannot claim as a result.
  * `measured.{arena_bytes,req_sram_kib,runs}` map onto `perf.arena_bytes`,
    `perf.req_sram_kib`, `perf.latency_ms.runs`.
  * `measured.{latency_ms_mean,latency_ms_p95}` map onto
    `perf.latency_ms.{mean,p95}` (which also carries `p50`/`stdev`, not
    represented in the old shape at all).
  * `toolchain.{name,version}` map onto ONE field, `target.compiler_version`
    (a single string, e.g. `"vela 4.1.0"` or `"passthrough"` for a backend
    with no real compile step) -- there is no separate name/version pair on
    the wire. `PerfPoint.compiler_version` carries it verbatim; a caller that
    needs just the leading tool name (to narrow by which toolchain a backend
    is benched with, the way `find_perf_points`' own `toolchain` query field
    always has) gets it via `_toolchain_name()` below, not a second stored
    field -- there is only one string to lie about.
  * `toolchain.{system_config,memory_mode}` map onto `vela.{system_config,
    memory_mode}` -- renamed `PerfPoint.vela_system_config`/
    `vela_memory_mode` to match the wire block's own name (`vela`, not
    `toolchain`: only ever populated for an `ethos_u` point, because only
    vela has this profile concept at all).
  * `capture.reference` doesn't exist; the real citation field is
    `capture.bench_id` ("which physical bench/rig captured this point").
    `PerfPoint.capture_reference` is renamed `capture_bench_id` to match.
  * `notes` (top-level in the old shape) is `capture.notes` in the real one --
    renamed `PerfPoint.notes` to `capture_notes`.
  * `model.{slug,size_bytes,source}` don't exist; the real `model` block is
    `{name, src_sha, format}` -- `PerfPoint.model_slug`/`model_sha256` are
    renamed `model_name`/`model_src_sha`, and `model_size_bytes`/
    `model_source` are deleted outright (no wire field backs them, and
    nothing in tan ever read them).

WHAT THIS READER REFUSES TO MATCH IS ITS WHOLE VALUE. A perf point is an exact
number a customer sizes hardware from precisely because it says "bench", so a
point describing a NEARBY identity is worse than no point at all:

  * **No "closest model".** Part of the match key is `model.src_sha` -- the
    exact bytes -- never `model.name`, which is a human label two different
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
  * **No fixture data, ever.** alp-sdk's own validator draws the fixture-vs-
    published line by PATH, not by a document key
    (`scripts/validate_metadata.py`'s `_MODEL_PERF_FIXTURE_MARKER = "_fixture"`,
    checked against `path.parent.name`/`path.name` -- i.e. the SKU-directory
    segment and the filename, the two components that are actually PART of
    the published-tree naming): "a fixture belongs under
    `tests/fixtures/model_perf/`, never in the published `metadata/
    model_perf/` tree." This reader adopts that exact rule rather than
    inventing a document-level one, for the reason alp-sdk's own comment
    gives -- a real point's BODY has no reason to ever say "I am a fixture";
    only where it SITS says so.
  * **No ambiguity resolved by preference.** More than one point left standing
    by the match key is genuine ambiguity: the toolchain PROFILE
    (`vela_system_config`/`vela_memory_mode`) is part of a point's identity
    but deliberately NOT part of the key, because a customer with no
    toolchain cannot state a profile. Picking one would author a preference
    the data does not express -- and the collision that put the profile into
    a point's identity hash (alp-sdk `f724d3e4`) left the DRAM-backed profile
    as the survivor, which is exactly measured and describes the wrong
    machine. `find_perf_points` therefore hands the caller ALL of them and
    lets a caller with a silicon fact decide (`tan.model.check` uses the SoC
    spec's own `npu_toolchain` profile); `find_perf_point` hands back `None`.

Every refusal above returns `None`, SILENTLY. Absence of a point means "we have
not benched this", never "it does not fit" -- the same rule `npu-ops-v1` states
for a missing op table, and the reason a missing `metadata/model_perf/`
directory is handled here as the ORDINARY case rather than an error: alp-sdk
ships that tree EMPTY (`metadata/model_perf/README.md`: "This directory is
intentionally empty... a perf point comes off real Alp Lab bench silicon or it
does not exist, and none has been captured yet"), and a consumer that treated
its absence as exceptional would break `tan model check` on every SDK checkout
in existence today.

Nothing in this module raises. A malformed, truncated or schema-violating point
is simply not a match: the producing side (alp-sdk's
`scripts/validate_metadata.py` + `tests/scripts/test_model_perf_metadata.py`)
is where a bad point is made LOUD, and a consumer that also failed loudly would
turn a metadata defect in another repository into a broken `tan model check`
for a customer who cannot fix it.

`coverage_from_placement` lives here rather than in `tan.model.check` because
it is THE one function in tan that may return the word `"fits"` off a real,
measured per-operator placement -- today that is only a real compile's own
summary (`tan.model.check._report_from_vela_compile`'s vela "CPU/NPU
operators = N" line); a bench perf point carries no such summary (see the
DEAD FIELDS note above) and no longer calls this function at all.

THE PATH: `metadata/model_perf/<SKU>/<hash>.yaml`, EXACTLY TWO PATH SEGMENTS
BELOW ROOT (tan-cli#1115's second, initially-missed half). alp-sdk's own
`scripts/validate_metadata.py::_collect_model_perf_files()` enforces this
shape on the PUBLISHING side ("a real point is exactly <SKU>/<hash>.yaml (2
segments); a file this shallow or this deep is never opened by the schema/
semantic passes and validates nothing") and refuses anything else outright --
there is no `<sku>/<target>/` subdirectory the way an earlier, unreconciled
version of this reader searched. `find_perf_points` below globs
NON-RECURSIVELY (`Path.glob`, never `Path.rglob`) directly under
`root/<sku>/`, which is what PINS the segment count: a point nested one
level deeper (a stray `<sku>/_fixture/<hash>.yaml`, or any other
subdirectory) is invisible to this reader by construction, not by a count
that could quietly drift back to matching a wrong depth --
`tests/model/test_perf.py::test_a_point_nested_one_level_too_deep_is_never_found`
proves it.

ONE PARSER, BOTH FORMATS. `read_perf_point` parses with `yaml.safe_load`, not
`json.loads`: alp-sdk publishes its tier-2 perf points as YAML, while every
document `tan`'s own test suite writes for this reader is JSON. Valid JSON is
valid YAML, so ONE parser reads both, with no format-sniff or extension check
of its own to drift from what either side actually writes -- a reader pinned
to `json.loads` in one place while the real documents are YAML is exactly how
tan-cli#1105 happened (the fixture discovery predicate in `tests/conftest.py`
globbed for `*.json` against a `*.yaml` file and permanently skipped, hiding
that this reader could not have parsed it either). `yaml` is imported inside
`read_perf_point`, not at module scope, because `tan.model.perf` sits on
`tan.cli`'s eager import graph (via `tan/commands/model_cmd.py`) and PyYAML is
single-use here (tan-cli#810, enforced by `tests/gates/test_cli_import_is_lean.py`).

YAML 1.1 TYPING IS ALREADY GUARDED. `yaml.safe_load` gives an unquoted scalar
an implicit type -- `2026-08-16` becomes a `datetime.date`, `5.1` becomes a
`float` -- and `_text()` below is deliberately `str`-strict, so an identity
field a hand-authored point left unquoted does not raise, it silently drops
the WHOLE point (the same `None` as a missing field). Deliberately not
"fixed" by coercing non-`str` scalars back to text: this reader's entire
design is to refuse rather than guess, and a value YAML re-typed out from
under the schema's own `"type": "string"` is exactly the ambiguity that
design refuses on. alp-sdk's own `validate_metadata.py` schema pass already
rejects an unquoted date/version at the SOURCE, so every point that actually
reaches a customer's `metadata_root` -- published or the `tests/fixtures/`
synthetic -- was quoted correctly before it got there; the residual risk is a
document nobody has validated yet, which is refused, not misread.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The published tree's directory name under a `metadata/` root. Named once
#: here so a consumer never has to spell it, and so `has_perf_points` and
#: `find_perf_point` cannot drift apart on it.
_PERF_DIRNAME = "model_perf"

#: alp-sdk's OWN fixture convention, adopted verbatim (see the module
#: docstring's fixture-rule note): `scripts/validate_metadata.py`'s
#: `_MODEL_PERF_FIXTURE_MARKER`. Checked against the SKU-directory segment
#: and the filename -- the two path components that are actually part of the
#: published-tree naming -- never the full absolute path, which could contain
#: this substring by coincidence (a developer's own checkout directory name)
#: with nothing to do with the tree's own content.
_FIXTURE_MARKER = "_fixture"

#: The only `schema_version` this reader understands. A future v2 alp-sdk
#: might publish under new field names this reader has never seen -- refused
#: exactly like any other unrecognised document, rather than half-parsed
#: under v1's field mapping.
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PerfPoint:
    """One bench-measured perf point, as published.

    Identity fields are all required and always populated -- a document
    missing any of them is not a point and never becomes one of these. Every
    OTHER field is OPTIONAL and `None` means "not measured" (or, for
    `vela_system_config`/`vela_memory_mode`, "not applicable to this
    backend"), never zero: the schema omits a figure nobody measured rather
    than zero-filling it, so a zero here is a MEASURED zero.
    """

    path: Path                    # where it was read from, for traceability
    sku: str                       # top-level `sku`
    hw_rev: str                    # top-level `hw_rev`, e.g. "r2"
    core: str                      # target.core, e.g. "m55_hp"
    backend: str                   # target.backend
    accel_config: str              # target.accel_config; "" when N/A
    compiler_version: str          # target.compiler_version, e.g. "vela 4.1.0"
    model_name: str                # model.name -- a LABEL, never a match key
    model_src_sha: str             # model.src_sha -- part of the match key
    model_format: str              # model.format -- "tflite" | "onnx"
    capture_date: str              # capture.date
    capture_operator: str          # capture.operator
    capture_bench_id: str          # capture.bench_id -- the raw capture's citation
    # The MEMORY PROFILE the figures describe. `None` for a backend that has
    # none (only `ethos_u` points ever carry a `vela:` block at all), never
    # for the reason vela itself matters: invoked flagless, `ethos-u-vela`
    # silently picks a DRAM-backed default per accelerator and the arena
    # figures then describe THAT profile rather than the module. A point that
    # did not record the profile is exactly measured and describes an
    # unknown machine -- so a consumer that HAS them must show them, or it
    # re-creates on the reading side the ambiguity the schema closed on the
    # writing side.
    vela_system_config: str | None = None
    vela_memory_mode: str | None = None
    req_sram_kib: int | None = None
    arena_bytes: int | None = None
    latency_ms_mean: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    latency_ms_stdev: float | None = None
    latency_runs: int | None = None
    capture_notes: str | None = None


def _toolchain_name(compiler_version: str) -> str:
    """The leading whitespace-delimited token of @compiler_version -- `"vela"`
    out of `"vela 4.1.0"`, or the whole string when it carries no space (e.g.
    `"passthrough"`, the schema's own example for a backend with no real
    compile step).

    `compiler_version` is the ONE wire field; there is no separate stored
    toolchain-name attribute to drift from it. Used only to narrow
    `find_perf_points`' own `toolchain` query field (a bare name a caller
    already knows, e.g. `tan.model.perf_apply._PERF_TOOLCHAIN`'s values)
    against a point actually captured by that tool, without inventing a
    second identity field the schema doesn't have."""
    return compiler_version.split(" ", 1)[0]


def coverage_from_placement(npu_ops: int | None, cpu_ops: int | None) -> str | None:
    """The `npu_coverage` a REAL per-operator placement supports -- and the
    ONLY function in tan that may return `"fits"`.

    `"fits"` only at 100% NPU placement (no CPU operator, and at least one
    operator placed); `"cpu-only"` at 0%; `"partial"` in between. `None` when
    there is no placement to read at all -- either count absent, or both zero
    (an unreadable summary, not an all-NPU one). A caller handed `None` must
    NOT manufacture a verdict from it: "nothing said it fell back to the CPU"
    is not evidence that it did not.

    TODAY'S ONLY CALLER is `tan.model.check._report_from_vela_compile`, off a
    real vela compile's own "CPU/NPU operators = N" summary
    (`blob.npu_op_count`/`cpu_op_count`). It is deliberately NOT called from
    `tan.model.perf_apply` any more (tan-cli#1115): alp-sdk's real
    `model-perf-v1` schema carries no per-operator placement counts on a bench
    point at all (see `tan.model.perf`'s module docstring, "DEAD FIELDS"), so
    there is no bench-side npu_ops/cpu_ops pair to feed this with. A matched
    bench point instead inherits whatever `npu_coverage` the report already
    had -- see `tan.model.perf_apply.apply_perf_point`."""
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


#: `.yaml` is what alp-sdk actually publishes; `.json` is additionally
#: discovered because tan's own test suite writes JSON synthetic points.
#: `.yml` is deliberately NOT globbed -- alp-sdk's own `_collect_model_perf_
#: files` rejects it too ("extension `.yml` is not `.yaml`"), so a point
#: spelled that way is invisible on the publishing side already; matching it
#: here would let tan accept a shape alp-sdk's own gate would reject.
_PERF_POINT_GLOBS = ("*.yaml", "*.json")


def _perf_point_files(sku_dir: Path) -> list[Path]:
    """Every file directly under @sku_dir this reader will attempt, in path
    order. Non-recursive (`Path.glob`, never `Path.rglob`) BY CONSTRUCTION --
    see the module docstring's "THE PATH" note for why that is what pins the
    two-segments-below-root shape rather than merely documenting it."""
    return sorted({p for pattern in _PERF_POINT_GLOBS for p in sku_dir.glob(pattern)})


def _as_dict(value: object) -> dict:
    """@value if it is a dict, else `{}` -- the safe "read a nested block
    that might be missing or wrong-typed" idiom this module reuses for every
    OPTIONAL block (`vela`, `perf`, `perf.latency_ms`)."""
    return value if isinstance(value, dict) else {}


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
    # measured" -- indistinguishable from an omitted key on the wire.
    return float(value) if value >= 0 else None


def _identity(doc: dict) -> dict | None:
    """@doc's identity fields as plain values, or `None` when any is missing
    or of the wrong type.

    `accel_config` is REQUIRED-AND-MAY-BE-EMPTY and the distinction is
    load-bearing: `""` is a backend that HAS no accel config, while an absent
    key is a point that failed to record one. `_text` separates them -- `""`
    is a `str` and survives the emptiness test below, `None` is the
    missing-key answer and refuses.

    `capture.{date,operator,bench_id}` are folded in here, not left to
    `read_perf_point` as optional extras: a number nobody can trace back to a
    run is not reproducible, and a perf point is worth exactly what its
    reproducibility is (mirrors the pre-reconciliation reader's own stance,
    carried forward under the real field names)."""
    model, target, capture = (doc.get(k) for k in ("model", "target", "capture"))
    if not all(isinstance(block, dict) for block in (model, target, capture)):
        return None
    out = {
        "sku": _text(doc, "sku"),
        "hw_rev": _text(doc, "hw_rev"),
        "core": _text(target, "core"),
        "backend": _text(target, "backend"),
        "compiler_version": _text(target, "compiler_version"),
        "model_name": _text(model, "name"),
        "model_src_sha": _text(model, "src_sha"),
        "model_format": _text(model, "format"),
        "capture_date": _text(capture, "date"),
        "capture_operator": _text(capture, "operator"),
        "capture_bench_id": _text(capture, "bench_id"),
    }
    accel_config = _text(target, "accel_config")
    if not all(out.values()) or accel_config is None:
        return None
    return {**out, "accel_config": accel_config}


def read_perf_point(path: Path) -> PerfPoint | None:
    """Parse ONE perf-point file, or `None` for anything this reader will not
    treat as a measurement.

    Parsed with `yaml.safe_load` -- see the module docstring's "ONE PARSER,
    BOTH FORMATS" note for why, and why the import is deferred to here rather
    than module scope.

    `None` covers: unreadable bytes, a document this parser cannot make sense
    of at all, a document that is not a mapping, a path naming alp-sdk's own
    `_fixture` marker, an unrecognised `schema_version`, and any document
    missing an identity field. Never raises -- see the module docstring on why
    a consumer stays quiet where the producing repository's own gate is loud.
    """
    import yaml  # noqa: PLC0415 (tan-cli#810: single-use, kept off `tan --version`)

    # `ValueError` is required alongside `yaml.YAMLError`, not redundant with
    # it: `path.read_text(encoding="utf-8")` raises `UnicodeDecodeError` (a
    # `ValueError` subclass) on non-UTF-8 bytes, BEFORE the parser ever runs,
    # and `yaml.YAMLError` does not cover it.
    if _FIXTURE_MARKER in path.parent.name or _FIXTURE_MARKER in path.name:
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict) or doc.get("schema_version") != _SCHEMA_VERSION:
        return None
    identity = _identity(doc)
    if identity is None:
        return None
    vela = _as_dict(doc.get("vela"))
    perf = _as_dict(doc.get("perf"))
    latency = _as_dict(perf.get("latency_ms"))
    capture = doc["capture"]  # already proven a dict by `_identity`
    return PerfPoint(
        path=path, **identity,
        vela_system_config=_text(vela, "system_config"),
        vela_memory_mode=_text(vela, "memory_mode"),
        req_sram_kib=_count(perf, "req_sram_kib"),
        arena_bytes=_count(perf, "arena_bytes"),
        latency_ms_mean=_millis(latency, "mean"),
        latency_ms_p50=_millis(latency, "p50"),
        latency_ms_p95=_millis(latency, "p95"),
        latency_ms_stdev=_millis(latency, "stdev"),
        latency_runs=_count(latency, "runs"),
        capture_notes=_text(capture, "notes"),
    )


# ---------------------------------------------------------------------------
# THE MATCH RULE (alp-sdk `f724d3e4`), and the three things about it that are
# DECISIONS rather than mechanics.
#
# THE KEY IS EIGHT FIELDS: sku + hw_rev + core + backend + accel_config +
# model.src_sha + toolchain name (derived from target.compiler_version) +
# target.compiler_version itself. Every one of them changes the number, which
# is why every one of them is also part of alp-sdk's own identity hash
# (`_model_perf_identity_hash()`) -- a component left out of that hash is one
# on which a second measurement silently overwrites the first.
#
# THREE OF THOSE EIGHT ARE OPTIONAL *QUERY* FIELDS, because a caller does not
# always hold the fact. `hw_rev` is not one of them: a caller that cannot say
# which module revision it has gets NOTHING, since serving an r2 measurement to
# an r1 customer is precisely the "describes a different machine" failure. But
# `core` and `compiler_version` (the exact string, not just the toolchain name)
# may be left unstated -- there is no local toolchain to read a version off in
# the tier-2 premise, and nothing ties a declared model to a core on a
# multi-core module -- and an unstated field is simply not narrowed on BY THIS
# FUNCTION: within `find_perf_points` alone, it is never guessed, and it never
# widens a match, it only leaves MORE points standing here.
#
# THAT IS NOT THE WHOLE PIPELINE'S GUARANTEE. ONE caller-side check narrows
# `core` further than this function's own query shape can
# (`tan.model.perf_apply._topology_core_ids`): a point's core must exist in
# @sku's OWN `topology:` map at all, UNCONDITIONALLY, for EVERY backend on
# EVERY SKU, whether or not the accelerator being screened pairs to a core --
# this is what refuses `m55_hp` on E1M-NX9101/imx93 (a real Ensemble core, not
# an imx93 one) or `cortex_potato` anywhere. `core` narrows to @target's own
# `paired_core` ONLY through this function's QUERY parameter (above) -- a
# declared fact about the SAME accelerator being screened -- and nothing
# wider; where @target declares no pairing of its own, `core` stays unnarrowed
# past the topology check.
#
# THE TOOLCHAIN PROFILE IS IDENTITY BUT NOT KEY. `vela_system_config`/
# `vela_memory_mode` are part of a point's real identity (both required by
# alp-sdk whenever `target.backend == "ethos_u"`), but a customer with no
# toolchain cannot state a profile, so requiring one would make tier 2
# unusable for exactly the person it exists for. The consequence is that a
# lookup can legitimately leave MORE THAN ONE point standing -- one model, one
# module, one core, one toolchain, captured under two different vela profiles
# -- and those describe DIFFERENT MACHINES. `find_perf_points` returns all of
# them and picks nothing; `find_perf_point` returns `None`. A caller holding a
# silicon fact may of course choose (`tan.model.perf_apply` narrows on the
# profile the SoC spec's own `npu_toolchain` block declares for the part,
# BEFORE trusting even a single standing point, not only as a tiebreak once
# two or more survive) -- but that decision belongs where the silicon facts
# are, not here.
#
# THE PATH IS AN INDEX, THE BODY IS THE TRUTH. `<root>/<sku>/` narrows the
# search cheaply, but every field is then re-checked against the document
# itself, so a file whose body disagrees with where it sits is not found
# here. alp-sdk's own validator refuses to publish one, and this reader does
# not have to trust that it did. Nothing here parses the FILENAME -- alp-sdk's
# own filename is an opaque content hash of the identity fields
# (`_model_perf_identity_hash()`), and a consumer that re-derived identity
# from it would be reading the index instead of the record.
# ---------------------------------------------------------------------------


def find_perf_points(*, sku: str, backend: str, accel_config: str,
                      model_sha256: str, toolchain: str, hw_rev: str | None,
                      metadata_root: Path, core: str | None = None,
                      compiler_version: str | None = None) -> list[PerfPoint]:
    """EVERY published point this identity leaves standing, in path order.

    `[]` for no match, ONE entry for the ordinary case, and MORE THAN ONE when
    the caller could not narrow far enough to separate two real measurements --
    see the block comment above. A caller that cannot choose between them must
    fall through to the next fidelity tier, never take `[0]`.

    @sku, @backend, @accel_config (exact string; `""` for a backend that has
    none) and @model_sha256 are always compared. The digest is compared
    case-folded -- hex case is a spelling of one value, not a different one --
    and a @model_sha256 that is not a full 64-hex digest is refused outright,
    because a truncated or prefix digest is the near-miss that would let two
    models share one measurement.

    @toolchain is the toolchain NAME (`vela`, `dxcom`, `translator`), compared
    against the LEADING token of the point's own `compiler_version` (there is
    no separate name field on the wire -- see `_toolchain_name`) -- it comes
    from the caller rather than being derived from @backend, so this function
    never invents one for a backend nobody has benched.

    @hw_rev is required-but-nullable and `None` matches NOTHING: see above.
    @core narrows only when given. @compiler_version, when given, narrows on
    the EXACT wire string (e.g. `"vela 4.1.0"`), not just the tool name --
    today's only real caller (`tan.model.perf_apply`) never passes it, because
    there is no local toolchain in the tier-2 premise to read a version off.
    """
    root = perf_points_root(metadata_root)
    if not root.is_dir():
        return []                       # the ordinary case, not an error
    # No separate `hw_rev is None: return []` early return here:
    # `point.hw_rev == hw_rev` below already refuses on its own --
    # `PerfPoint.hw_rev` is a REQUIRED, always-non-empty field (`_identity`'s
    # own `all(out.values())` gate), so it can never equal a `None` query
    # value.
    wanted = model_sha256.strip().lower()
    if len(wanted) != 64 or any(c not in "0123456789abcdef" for c in wanted):
        return []
    sku_dir = root / sku
    if not sku_dir.is_dir():
        return []
    return [
        point for point in (read_perf_point(p) for p in _perf_point_files(sku_dir))
        if point is not None
        and point.sku == sku
        and point.hw_rev == hw_rev
        and point.backend == backend
        and point.accel_config == accel_config
        and point.model_src_sha.lower() == wanted
        and _toolchain_name(point.compiler_version) == toolchain
        and (core is None or point.core == core)
        and (compiler_version is None or point.compiler_version == compiler_version)
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
