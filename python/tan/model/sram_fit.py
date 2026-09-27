# SPDX-License-Identifier: Apache-2.0
"""SRAM0 residency + arena fit for a `Sram_Only` ethos_u compile (tan-cli#1288).

**Premise correction, recorded here since the issue that opened this file got
it wrong.** tan-cli#1288 claimed tan already compares the vela ARENA
(`req_sram_kib`) against the board's own budget "per tan-cli#1011". It does
not: #1011 was only the scope decision that `req_sram_kib` stays arena-only
(alp-sdk#2312 confirms weights are never summed into it) -- no host-side
comparison against `board.yaml` existed anywhere before this file.
`tan.model.targets.TargetSpec` carried no arena limit, and `_vela_profile`
never resolved a SoC-JSON `variants[]` entry, so `sram_banks_kb.SRAM0`
(nested under `variants[]`, `tan.core.size.sram_banks`) was unreachable from
model code. This module adds BOTH halves: the arena check the issue thought
already existed, and the SRAM0 residency check it asked for.

alp-sdk's `src/backends/inference/ethos_u_aen.cpp` pins EVERY NPU access to
the SRAM AXI port under `Sram_Only` -- not just the tensor arena
(`tan.model.adapters.ethos_u._footprint`'s own docstring has the full vela
bookkeeping story for why the arena figure is arena-only), but the compiled
BLOB too: the AEN examples copy the model's weights into
`section("SRAM0")` right beside the arena. Applies ONLY under that one vela
memory mode (`SRAM_ONLY_MODE` below) -- every other mode may place the blob
off SRAM0, so both checks report `"skipped"`, with a reason, everywhere else.

**Never "fits", only "fits-unverified".** Every SoC ships
`inference_arena_sram_kib: 0` today, so there is no VERIFIED inference-arena
SRAM budget in metadata to compare against -- a pass here means "the
declared/default budget is not exceeded", never "measured to run". Only a
`no-fit` is certain: it is plain ceil'd-KiB arithmetic against a real,
sourced number, so a failure is a hard physical fact, not a screen -- BUT see
`resolve_arena_budget`'s own docstring for exactly which arena resolutions
are sourced enough to certify one (tan-cli#1288 review: the E8/E6/E4 SoC
specs declare NO `paired_core` for their own Ethos-U85, so treating the
schema default as certain there would have refused a build the board's own
`cores.m55_hp.inference.default_arena_kib` override should have decided).

Pure logic (`evaluate_sram_fit`, `resolve_arena_budget`, `FitVerdict`,
`SramFit`, `ArenaBudget`) is IO-free; the two resolvers that do real
filesystem reads (`resolve_sram0_kib`, and `read_sdk_som_and_soc`/
`resolve_variant`/`sram_banks` it reuses) are the only IO in this module --
the same split every other `tan.model` file makes (`tan.model.check`'s own
module doc: "IO lives in tan.model, never in tan.core")."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build_output import read_sdk_som_and_soc
from tan.core.size import resolve_variant, sram_banks

#: `metadata/schemas/board.schema.json` (alp-sdk, verified against the pinned
#: checkout at commit 79c834e6): `cores.<id>.inference.default_arena_kib` is
#: `{"type": "integer", "minimum": 16, "default": 128}`. Applied only to a
#: core `board.yaml` actually declares, when THAT core's own `inference:`
#: block omits an override -- never invented for a core the board never
#: declares at all (`resolve_arena_budget` below).
DEFAULT_ARENA_KIB = 128

#: The one vela memory mode that pins BOTH the tensor arena and the compiled
#: blob (weights) to the same SRAM AXI port
#: (`src/backends/inference/ethos_u_aen.cpp`) -- every other mode
#: (`Shared_Sram` / `Dedicated_Sram*`) may place the blob off SRAM0, so this
#: whole module is a no-op there.
SRAM_ONLY_MODE = "Sram_Only"

#: The three verdict words this module ever emits -- never a bare "fits" (see
#: the module docstring's "Never fits" paragraph).
FIT_UNVERIFIED = "fits-unverified"
NO_FIT = "no-fit"
SKIPPED = "skipped"

#: The caveat every `FIT_UNVERIFIED` arena verdict carries -- the board's OWN
#: declared/default budget is a real, sourced number, but nothing in
#: metadata publishes a firmware-verified one to compare it against (the
#: identical reason the word is "unverified" rather than "fits" at all).
_ARENA_UNVERIFIED_REASON = "board arena budget not verified against firmware"


@dataclass(frozen=True)
class FitVerdict:
    """One axis's verdict (arena, or SRAM0 residency): a KiB comparison plus
    an optional human note. `reason` carries the SKIP explanation
    (`SKIPPED`), the "not verified" caveat (`FIT_UNVERIFIED`), or -- ONLY for
    the ambiguous multi-core `NO_FIT` shape `resolve_arena_budget`'s
    `"range"` kind can produce -- which bound the certain failure was proven
    against. Every OTHER `NO_FIT` carries no `reason`: a single sourced
    number already says everything there is to say."""

    verdict: str
    needed_kib: int | None = None
    limit_kib: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "neededKib": self.needed_kib,
                "limitKib": self.limit_kib, "reason": self.reason}


@dataclass(frozen=True)
class SramFit:
    """Both axes of a `Sram_Only` ethos_u compile's SRAM fit, plus the
    compiled blob's own size in KiB -- the one shape `tan model build` (the
    refusal decision only) and `tan model check --exact` (the `sramFit`
    envelope block, `tan.core.model_check.backend_report_as_dict`) both
    read."""

    arena: FitVerdict
    sram0: FitVerdict
    blob_kib: int | None = None

    @property
    def no_fit(self) -> bool:
        """True the moment EITHER axis is a certain `NO_FIT` -- what `tan
        model build` refuses the whole per-model build on. A `SKIPPED` axis
        never contributes: an unresolvable SRAM0 total, or an arena budget
        this project's board.yaml never actually declared, is an unanswered
        question, never treated as a failure (`resolve_arena_budget`)."""
        return self.arena.verdict == NO_FIT or self.sram0.verdict == NO_FIT

    def as_dict(self) -> dict:
        return {"arena": self.arena.as_dict(), "sram0": self.sram0.as_dict(),
                "blobKib": self.blob_kib}


def _skipped_not_sram_only(memory_mode: str | None) -> SramFit:
    reason = (f"the SRAM0 residency and arena fit checks apply only under "
              f"the Sram_Only vela memory mode; this target resolved "
              f"{memory_mode!r}.")
    skip = FitVerdict(verdict=SKIPPED, reason=reason)
    return SramFit(arena=skip, sram0=skip, blob_kib=None)


@dataclass(frozen=True)
class ArenaBudget:
    """How `resolve_arena_budget` resolved a target's arena budget -- exactly
    one of three shapes, keyed by `kind`:

    * `"single"` -- ONE board.yaml core's own arena figure is the answer,
      CERTAIN enough for `NO_FIT` (`single_kib`, `cores` names it).
    * `"range"` -- the target's own NPU pairs to no core
      (`TargetSpec.paired_core is None`, a real answer for a shared NPU, e.g.
      the E8/E6/E4's own Ethos-U85 -- not a gap), and board.yaml declares an
      `inference:` block on MORE THAN ONE core, so nothing sourced picks
      between them (`min_kib`/`max_kib`/`cores`, the declared budgets and the
      core names that produced them).
    * `"unresolved"` -- no board.yaml in hand, the target's paired core is
      not declared in board.yaml AT ALL, or (paired_core is `None` too)
      board.yaml declares no core running an inference workload at all
      (`reason` says which). Never treated as a `NO_FIT` input."""

    kind: str
    single_kib: int | None = None
    min_kib: int | None = None
    max_kib: int | None = None
    cores: tuple[str, ...] = ()
    reason: str | None = None


def _core_arena_kib_or_default(core_slice: dict) -> int:
    """`inference.default_arena_kib` off one already-declared board.yaml core
    slice, or `DEFAULT_ARENA_KIB` when that core's own `inference:` block
    omits it (or declares none at all) -- the core EXISTING in board.yaml is
    what makes this a sourced fact about THIS project, not the presence of
    the tuning key itself."""
    inference = core_slice.get("inference")
    if isinstance(inference, dict):
        value = inference.get("default_arena_kib")
        # bool is an int subclass in Python; a malformed `true`/`false` value
        # is not a KiB count.
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return DEFAULT_ARENA_KIB


def resolve_arena_budget(board_doc: dict | None, core_id: str | None) -> ArenaBudget:
    """The arena budget for one ethos_u target, scoped to exactly what
    board.yaml actually says (tan-cli#1288 review, HIGH/MEDIUM findings):

    1. @core_id resolved (`TargetSpec.paired_core`) AND that core is declared
       under board.yaml's `cores:` at all (whether or not it tunes
       `inference.default_arena_kib` itself) -- `"single"`, CERTAIN: THIS
       core is what runs this NPU, full stop.
    2. @core_id resolved but NOT declared under `cores:` -- `"unresolved"`:
       nothing in this project runs on that core, so a board arena figure has
       no core to attach to. Never a `DEFAULT_ARENA_KIB`-based guess.
    3. @core_id is `None` (a shared NPU with no pairing of its own, e.g. the
       E8/E6/E4's own Ethos-U85) -- collect every board.yaml core that
       declares an `inference:` block at all (the only honest way to guess
       "which core(s) might run this", absent a real pairing): exactly one
       -- `"single"`, CERTAIN, use it; two or more -- `"range"` (the caller
       decides what a `NO_FIT`/`FIT_UNVERIFIED` bound can honestly claim
       across them); none at all -- `"unresolved"` (a bare
       `DEFAULT_ARENA_KIB` here would be a number about nothing declared,
       never certain enough to refuse a build on).
    4. @board_doc itself absent -- `"unresolved"`, same reasoning as 3's
       empty case: nothing to inspect at all."""
    if not isinstance(board_doc, dict):
        return ArenaBudget(kind="unresolved",
                           reason="no board.yaml in hand; arena/SRAM0 residency not checked")
    raw_cores = board_doc.get("cores")
    cores = raw_cores if isinstance(raw_cores, dict) else {}

    if core_id:
        slice_ = cores.get(core_id)
        if not isinstance(slice_, dict):
            return ArenaBudget(kind="unresolved", reason=(
                f"core {core_id!r} not declared in board.yaml; nothing in "
                f"this project runs on it"))
        return ArenaBudget(kind="single", single_kib=_core_arena_kib_or_default(slice_),
                           cores=(core_id,))

    inference_cores = sorted(
        name for name, slice_ in cores.items()
        if isinstance(slice_, dict) and isinstance(slice_.get("inference"), dict)
    )
    if not inference_cores:
        return ArenaBudget(kind="unresolved", reason=(
            "board.yaml declares no core running an inference workload; "
            "arena/SRAM0 residency not checked"))
    budgets = {name: _core_arena_kib_or_default(cores[name]) for name in inference_cores}
    if len(inference_cores) == 1:
        only = inference_cores[0]
        return ArenaBudget(kind="single", single_kib=budgets[only], cores=(only,))
    return ArenaBudget(kind="range", min_kib=min(budgets.values()),
                       max_kib=max(budgets.values()), cores=tuple(inference_cores))


def resolve_sram0_kib(sku: str, metadata_root: Path) -> tuple[float | None, str | None]:
    """`(sram0_kib, None)` off the SoC-JSON variant @sku resolves to, or
    `(None, reason)` when any leg of that resolution fails.

    Reused, not re-walked: `tan.commands.build_output.read_sdk_som_and_soc`
    is the SAME metadata-layout walk `tan size`/`tan debug-config` already
    use (its own docstring: "must call ONE reader, not two that could
    disagree"), and `tan.core.size.resolve_variant`/`sram_banks` are the SAME
    pure variant/bank resolution `tan size`'s own budget uses -- this module
    adds no second, potentially-drifting reader."""
    walked = read_sdk_som_and_soc(str(metadata_root), sku)
    if walked is None:
        return None, f"unreadable SoM preset for {sku}; SRAM0 residency not checked"
    _silicon, silicon_variant, variants, _soc_flash_mb, _soc_cores = walked
    variant = resolve_variant(silicon_variant, sku, variants)
    if variant is None:
        return None, f"no SoC variant resolved for {sku}; SRAM0 residency not checked"
    for name, kib in sram_banks(variant):
        if name == "SRAM0":
            return kib, None
    return None, (f"the SoC variant resolved for {sku} declares no SRAM0 bank; "
                  f"SRAM0 residency not checked")


def _evaluate_sram0(blob_kib: int, arena_kib: int, sram0_total: float | None,
                    sram0_reason: str | None, *, note_suffix: str = "") -> FitVerdict:
    """`blob_kib + arena_kib` against @sram0_total -- @arena_kib is whatever
    the caller decided is the right SINGLE number for this comparison (the
    resolved core's own budget, or -- in the `"range"` shape -- the smallest
    of several equally-plausible ones, which is what makes a `NO_FIT` there a
    certain LOWER BOUND: every other candidate core only needs MORE SRAM0,
    never less)."""
    if sram0_total is None:
        return FitVerdict(verdict=SKIPPED, reason=sram0_reason)
    needed = blob_kib + arena_kib
    limit_kib = int(sram0_total)
    if needed > sram0_total:
        return FitVerdict(verdict=NO_FIT, needed_kib=needed, limit_kib=limit_kib)
    return FitVerdict(
        verdict=FIT_UNVERIFIED, needed_kib=needed, limit_kib=limit_kib,
        reason=(f"blob {blob_kib} + arena {arena_kib} = {needed} KiB of SRAM0; "
                f"firmware's own SRAM0 use not checked{note_suffix}"),
    )


def _evaluate_arena_single(req_sram_kib: int, arena_kib: int) -> FitVerdict:
    if req_sram_kib > arena_kib:
        return FitVerdict(verdict=NO_FIT, needed_kib=req_sram_kib, limit_kib=arena_kib)
    return FitVerdict(verdict=FIT_UNVERIFIED, needed_kib=req_sram_kib, limit_kib=arena_kib,
                      reason=_ARENA_UNVERIFIED_REASON)


def _evaluate_arena_range(req_sram_kib: int, budget: ArenaBudget) -> FitVerdict:
    lo, hi, names = budget.min_kib, budget.max_kib, ", ".join(budget.cores)
    if req_sram_kib > hi:
        return FitVerdict(
            verdict=NO_FIT, needed_kib=req_sram_kib, limit_kib=hi,
            reason=(f"exceeds even the largest declared core budget ({hi} KiB, "
                    f"across cores {names} with no paired_core to narrow which "
                    f"one actually runs this NPU)"),
        )
    if req_sram_kib <= lo:
        return FitVerdict(
            verdict=FIT_UNVERIFIED, needed_kib=req_sram_kib, limit_kib=lo,
            reason=(f"{_ARENA_UNVERIFIED_REASON} (fits every declared core's budget, "
                    f"{lo}-{hi} KiB across cores {names})"),
        )
    return FitVerdict(verdict=SKIPPED, reason=(
        f"ambiguous across cores {names} (declared budgets {lo}-{hi} KiB) with no "
        f"paired_core to narrow which one actually runs this NPU; needs {req_sram_kib} KiB"
    ))


def evaluate_sram_fit(*, memory_mode: str | None, req_sram_kib: int, blob_len_bytes: int,
                      board_doc: dict | None, paired_core: str | None,
                      sku: str, metadata_root: Path) -> SramFit:
    """The whole check for one compiled ethos_u target.

    `req_sram_kib` is the arena, already ceil'd
    (`tan.model.adapters.ethos_u._footprint`, tan-cli#1011's own arena-only
    scope -- never re-derived here). `blob_len_bytes` is `len(Blob.payload)`,
    the compiled weights -- the SECOND half of `Sram_Only` residency #1288
    exists to check, which nothing read before this module.

    `SramFit.no_fit` is what `tan model build` refuses on; `SramFit.as_dict()`
    is the `sramFit` envelope block `tan model check --exact` attaches.
    `resolve_arena_budget`'s own docstring is the source of truth for exactly
    which of its three shapes may ever produce a `NO_FIT` here."""
    if memory_mode != SRAM_ONLY_MODE:
        return _skipped_not_sram_only(memory_mode)

    budget = resolve_arena_budget(board_doc, paired_core)
    blob_kib = math.ceil(blob_len_bytes / 1024) if blob_len_bytes > 0 else 0
    sram0_total, sram0_reason = resolve_sram0_kib(sku, metadata_root)

    if budget.kind == "unresolved":
        skip = FitVerdict(verdict=SKIPPED, reason=budget.reason)
        return SramFit(arena=skip, sram0=skip, blob_kib=blob_kib)

    if budget.kind == "single":
        arena = _evaluate_arena_single(req_sram_kib, budget.single_kib)
        sram0 = _evaluate_sram0(blob_kib, budget.single_kib, sram0_total, sram0_reason)
        return SramFit(arena=arena, sram0=sram0, blob_kib=blob_kib)

    # budget.kind == "range": paired_core is None and MORE THAN ONE board core
    # declares an inference: block -- see `_evaluate_sram0`'s own docstring
    # for why the SRAM0 axis uses the MIN of them unconditionally, whatever
    # the arena axis itself concludes.
    arena = _evaluate_arena_range(req_sram_kib, budget)
    sram0 = _evaluate_sram0(
        blob_kib, budget.min_kib, sram0_total, sram0_reason,
        note_suffix=(f" (using the smallest of cores {', '.join(budget.cores)}'s "
                     f"declared budgets, {budget.min_kib} KiB, as a lower bound -- no "
                     f"paired_core narrows which one actually runs this NPU)"),
    )
    return SramFit(arena=arena, sram0=sram0, blob_kib=blob_kib)
