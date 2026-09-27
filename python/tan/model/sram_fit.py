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
`no-fit` is certain: both checks are plain ceil'd-KiB arithmetic against a
real, sourced number, so a failure is a hard physical fact, not a screen.

Pure logic (`evaluate_sram_fit`, `FitVerdict`, `SramFit`) is IO-free; the two
resolvers it calls (`resolve_arena_kib`, `resolve_sram0_kib`) do real
filesystem reads and are the only IO in this module -- the same split every
other `tan.model` file makes (`tan.model.check`'s own module doc: "IO lives
in tan.model, never in tan.core")."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build_output import read_sdk_som_and_soc
from tan.core.size import resolve_variant, sram_banks

#: `metadata/schemas/board.schema.json` (alp-sdk, verified against the pinned
#: checkout at commit 79c834e6): `cores.<id>.inference.default_arena_kib` is
#: `{"type": "integer", "minimum": 16, "default": 128}`. Applied whenever
#: `board.yaml` declares no override for the core a target pairs to -- never
#: a tan-invented number.
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


@dataclass(frozen=True)
class FitVerdict:
    """One axis's verdict (arena, or SRAM0 residency): a KiB comparison plus
    an optional human note. `reason` carries the SKIP explanation (`SKIPPED`)
    or the "not verified" caveat (`FIT_UNVERIFIED`); it is always `None` at
    `NO_FIT` -- the numbers already say everything there is to say there."""

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
        never contributes: an unresolvable SRAM0 total is an unanswered
        question, never treated as a failure."""
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


def resolve_arena_kib(board_doc: dict | None, core_id: str | None) -> int:
    """`cores.<core_id>.inference.default_arena_kib`, or `DEFAULT_ARENA_KIB`
    when `board.yaml` declares no override, when @core_id is unresolved (the
    target's own `paired_core` is `None` -- a real, sourced answer for a
    shared NPU, not a gap: `tan.model.targets.TargetSpec.paired_core`'s own
    comment), or when @board_doc itself is unavailable (a direct
    `build_model()`/`check_model_backends()` call with no board.yaml in
    hand, e.g. from a test)."""
    if isinstance(board_doc, dict) and core_id:
        cores = board_doc.get("cores")
        slice_ = cores.get(core_id) if isinstance(cores, dict) else None
        inference = slice_.get("inference") if isinstance(slice_, dict) else None
        if isinstance(inference, dict):
            value = inference.get("default_arena_kib")
            # bool is an int subclass in Python; a malformed `true`/`false`
            # value is not a KiB count.
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return DEFAULT_ARENA_KIB


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
    is the `sramFit` envelope block `tan model check --exact` attaches."""
    if memory_mode != SRAM_ONLY_MODE:
        return _skipped_not_sram_only(memory_mode)

    arena_kib = resolve_arena_kib(board_doc, paired_core)
    arena = FitVerdict(
        verdict=NO_FIT if req_sram_kib > arena_kib else FIT_UNVERIFIED,
        needed_kib=req_sram_kib, limit_kib=arena_kib,
    )

    blob_kib = math.ceil(blob_len_bytes / 1024) if blob_len_bytes > 0 else 0
    sram0_total, reason = resolve_sram0_kib(sku, metadata_root)
    if sram0_total is None:
        sram0 = FitVerdict(verdict=SKIPPED, reason=reason)
    else:
        needed = blob_kib + arena_kib
        limit_kib = int(sram0_total)
        if needed > sram0_total:
            sram0 = FitVerdict(verdict=NO_FIT, needed_kib=needed, limit_kib=limit_kib)
        else:
            sram0 = FitVerdict(
                verdict=FIT_UNVERIFIED, needed_kib=needed, limit_kib=limit_kib,
                reason=(f"needs {needed} KiB of SRAM0 for the model blob; "
                        f"firmware's own SRAM0 use not checked"),
            )
    return SramFit(arena=arena, sram0=sram0, blob_kib=blob_kib)
