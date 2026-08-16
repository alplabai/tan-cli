# SPDX-License-Identifier: Apache-2.0
"""Arm Ethos-U (Vela) compiler adapter.

Wraps the `vela` CLI from `ethos-u-vela` (the `model-compile` optional
dependency). is_available() is True when `vela` is on PATH; compile() shells out
for the given accelerator-config and reads back `<stem>_vela.tflite`. The
arena/peak-SRAM footprint is parsed from vela's summary CSV for THAT run, and a
successful compile that placed operators on the NPU but reports no SRAM working
set is a hard error, never a silent zero (see `_refuse_zero_sram_footprint`).

compile() NEVER raises on a clean vela exit *for placement reasons*, including a
full CPU fallback -- vela's own exit code is 0 whether it placed every operator
on the NPU or none of them (measured: `vela float32_fc.tflite
--accelerator-config ethos-u85-256` on a float32 FULLY_CONNECTED model prints
"NPU operators = 0 (0.0%)" and still exits 0). `_parse_vela_placement` reads
vela's own per-run placement summary (always printed to stdout,
`ethosu.vela.stats_writer.print_performance_metrics_common`'s "CPU/NPU
operators = N (P%)" lines) so a caller never has to infer placement from the
mere absence of an exception -- see `tan.model.check`'s
`_report_from_vela_compile`, the actual consumer of `Blob.npu_op_count`/
`cpu_op_count`.

THE MEMORY PROFILE (alp-sdk #1470): this adapter passes `--memory-mode` when
its caller resolved one, and today every caller does -- it is a SILICON fact,
published per part in the SoC spec's `npu_toolchain.vela.memory_mode`
(`Sram_Only` on the Alif Ensemble parts, `Shared_Sram` on the NXP i.MX 93) and
carried here as `TargetSpec.vela_memory_mode`. It is never guessed: a caller
that resolved none gets the flagless invocation, byte for byte.

That flag is the one that matters, and the reason is measured. On the committed
`tests/fixtures/models/tiny_int8.tflite` at `ethos-u85-256`, `ethos-u-vela`
5.1.0 reports:

  * with no profile flags -- `sram_memory_used = 0.0`,
    `dram_memory_used = 0.265625` (under `Ethos_U85_SYS_DRAM_Mid` /
    `Dedicated_Sram_384KB`, both of which vela warns it defaulted)
  * with `--memory-mode Sram_Only` -- `sram_memory_used = 0.03125`,
    `dram_memory_used = 0.0`, `on_chip_flash_memory_used = 0.234375`

Both exit 0. The first is the shape tan-cli#789 had to REFUSE: vela's built-in
default profile is DRAM-backed (`Ethos_U85_SYS_DRAM_Mid` /
`Dedicated_Sram_384KB`, `weights_storage_area=DRAM` in the summary CSV) and an
Alif Ensemble E-series module is MRAM + SRAM with no DRAM at all, so the whole
working set landed in memory the part does not have and the SRAM figure alp-sdk
sizes an arena from came back zero. Adding `--system-config
Ethos_U85_SYS_Flash_High` on top of the memory mode changes NO memory figure
(also measured) -- placement is decided by `--memory-mode`, bandwidth by
`--system-config`.

`--system-config` is therefore still not passed, and that is deliberate rather
than pending. The names alp-sdk's own examples use
(`Ethos_U85_SRAM_Only`, `RTSS_HE_SRAM_Only`;
`examples/aen/aen-npu-inference-alp/CMakeLists.txt:42-43` and its `-u55`
sibling) live ONLY in Alif's PROPRIETARY `ensemble_vela.ini`, which alp-sdk does
not redistribute, and handing vela a section it cannot resolve is a hard rc=1,
verbatim: `Section System_Config.Ethos_U85_SRAM_Only not found in Vela config
file`. So the SoC spec flags those as vendor-gated and `resolve_targets`
withholds them (`tan.model.targets._vela_profile`); vela's own default system
config is used and SAID SO through `_default_profile_caveats`, which now
distinguishes the two flags rather than blaming the whole profile on vela.
Never invent a profile here; a wrong one compiles a command stream for hardware
the module does not have.

WHO THE OLD DEFAULT HIT (tan-cli#789 review), i.e. what the memory mode fixes:
the DRAM-backed built-in default was NOT a U85/Alif-only fact. Measured, real
`ethos-u-vela` 5.1.0 over the committed fixture, both of these refused with no
memory mode:

  * `ethos-u85-256` -> `Ethos_U85_SYS_DRAM_Mid` / `Dedicated_Sram_384KB`
    (E1M-AEN401 / E1M-AEN601 / E1M-AEN801, Alif Ensemble E4/E6/E8)
  * `ethos-u65-256` -> `Ethos_U65_Client_Server` / `Dedicated_Sram_384KB`
    (E1M-NX9101, NXP i.MX 93 -- NOT an Alif part)

while every `ethos-u55-*` config (E1M-AEN301/501/701 and the U55 targets of the
three AEN SKUs above) resolved to the SRAM-backed
`Ethos_U55_High_End_Embedded` and reported a real footprint. The refusal is
kept, not deleted: a part whose spec carries no profile, or a compile that
reports no SRAM even under its module's own memory mode, must still be refused
rather than shipped as a zero. `_refuse_zero_sram_footprint` names the profile
the run ITSELF reported (`_parse_vela_profile`), never a hardcoded Alif one --
an NXP user must not read an error blaming `Ethos_U85_SYS_DRAM_Mid`.

...AND NEITHER IS THE REMEDY (tan-cli#789 review (g)). Naming the profile per
run only half-closed that: the REMEDY sentence still told every reader the
profile "lives in the proprietary ensemble_vela.ini", which is an Alif fact.
alp-sdk's own i.MX 93 vela invocation carries no proprietary `.ini` at all --
verbatim from `vendors/nxp-imx93/README.md`: `vela --accelerator-config
ethos-u65-256 --output-dir build/vela-imx93 --memory-mode Shared_Sram
mobilenet_v2_quantised.tflite`. So on an NXP part that clause sent the reader
after another vendor's file, which is not what their silicon needs and not
where their profile comes from. The clause is now gated on
@silicon_ref -- the SoM preset's own `silicon:` ref reaching `compile()`
(`alif:ensemble:e8` vs `nxp:imx9:imx93`, via `TargetSpec.silicon_ref`), a real
signal -- and NOT on the vela profile name, which is a property of the
accelerator config rather than of the vendor. That i.MX 93 line is no longer
restated as advice for a different reason than it was: `--memory-mode
Shared_Sram` is now what tan actually PASSES for that part, straight from
`imx93.json`'s own `npu_toolchain.vela` block, so there is nothing left to
advise a reader to do by hand."""
from __future__ import annotations
import csv
import math
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NoReturn

from . import CompilerAdapter, Blob

_VELA_TIMEOUT_S = 600        # vela compiles are minutes at most; never unbounded in CI

# ethosu.vela.stats_writer.print_performance_metrics_common (always printed to
# stdout, f=sys.stdout is its own default) emits exactly one "CPU operators ="
# and one "NPU operators =" line per run, e.g. "NPU operators = 0 (0.0%)". The
# regex intentionally ignores the printed percentage -- _parse_vela_placement
# recomputes it from the two integer counts so it never inherits vela's own
# text-formatting rounding.
_PLACEMENT_RE = re.compile(r"^(CPU|NPU) operators = (\d+)", re.MULTILINE)

# The same network-summary block names the profile vela ACTUALLY resolved --
# "System configuration             Ethos_U85_SYS_DRAM_Mid" / "Memory mode
# Dedicated_Sram_384KB" -- whether it came from a supplied .ini or from vela's
# built-in default. Read rather than assumed: it is both the summary CSV's own
# filename suffix and the only honest way to say WHICH profile a figure
# describes.
_PROFILE_RE = re.compile(r"^(System configuration|Memory mode) +(\S+) *$", re.MULTILINE)

# vela's verbatim warning when it falls back to its own built-in profile.
# Deliberately NOT matched on the "No configuration file specified" sibling:
# that one interpolates an absolute path into the host's site-packages, which
# must never be echoed into a customer-facing envelope.
_DEFAULTED_RE = re.compile(
    r"^Warning: No (system configuration|memory mode) specified\.", re.MULTILINE)

# vela's own names for the two profile flags, as they appear in that warning.
# Read PER FLAG, not as one boolean: since alp-sdk #1470 tan supplies the
# memory mode from SoC metadata and still supplies no system config, so a run
# is routinely half-defaulted -- measured, `--memory-mode Sram_Only` at
# `ethos-u85-256` still prints "Warning: No system configuration specified.
# Using a default of Ethos_U85_SYS_DRAM_Mid." and nothing else. Calling that
# whole run "vela's built-in default profile" would credit vela with the
# memory mode tan just passed, and would tell a customer the SRAM figure
# describes a memory model that is not theirs when it now describes exactly
# theirs.
_SYSTEM_CONFIG_FLAG = "system configuration"
_MEMORY_MODE_FLAG = "memory mode"

# `write_summary_metrics_csv_common` writes one `<mem_area>_memory_used` column
# per memory area the accelerator config declares (stats_writer.py's
# `area.identifier_name() + "_memory_used"`), so the area names are data, not a
# fixed list -- matched by shape so a vela version that adds an area is read,
# not silently dropped.
_MEM_USED_RE = re.compile(r"^(.+)_memory_used$")

# A directory component built from an accel_config that comes out of SoM
# metadata; anything outside this set is folded to "_" so a malformed
# accel_config can never walk out of @out_dir.
_UNSAFE_DIR_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# The one vendor whose vela profile tan can point at by name. `silicon_ref` is
# `<vendor>:<family>:<part>` straight from the SoM preset's `silicon:` key
# (metadata/e1m_modules/E1M-AEN801.yaml -> `alif:ensemble:e8`), so this is a
# family match on authored metadata, not a guess from an accel_config or from
# vela's own profile name -- `ethos-u55-*`/`Ethos_U55_High_End_Embedded` say
# nothing about who built the part. Anything else (`nxp:imx9:imx93`, a future
# vendor, or an unresolved None) gets no vendor clause at all.
_ALIF_ENSEMBLE_REF_PREFIX = "alif:ensemble:"


class VelaFootprintRefused(RuntimeError):
    """vela ran CLEANLY and tan refused the footprint it reported.

    A distinct type because the two callers must tell it apart from a real
    vela failure, and neither can do that by inspecting a message:

      * `tan.model.build.build_model` skips THIS target and keeps building the
        SKU's others (tan-cli#789 review BLOCKER 1: one refused
        `ethos-u85-256` used to abort the whole `.alpmodel`, taking the
        `ethos-u55-256` / `ethos-u55-128` / `cpu` targets that compiled fine
        down with it). A genuine `adapter.compile()` failure still fails the
        build loudly -- that is deliberate and unchanged.
      * `tan.model.check`'s `--exact` opens its note "vela compiled cleanly
        for <accel-config> ...", NOT "--exact compile with vela failed (...)"
        (tan-cli#789 review NIT 8) -- vela exited 0; it was the footprint that
        was refused. It also passes the message through at a wider note budget
        because -- unlike a vela subprocess re-raising its own raw stderr --
        this text is authored here, one line, and bounded.

    The message is a single line by contract (both callers put it in a
    one-line surface): a report note and an `.alpmodel` coverage reason."""


def _vela_version() -> str:
    try:
        return f"vela {version('ethos-u-vela')}"
    except PackageNotFoundError:
        return "vela"


def _run_dir(out_dir: Path, accel_config: str) -> Path:
    """A per-accel-config subdirectory of @out_dir for one vela run.

    vela names its summary `<stem>_summary_<system_config>.csv`, and the
    system-config is a property of the SILICON FAMILY, not of the accelerator
    config: `ethos-u55-256` and `ethos-u55-128` both resolve to
    `Ethos_U55_High_End_Embedded` and therefore write the SAME filename
    (measured, vela 5.1.0). `build_model` reuses one `out_dir` across every
    config a SoM declares, so a shared directory means one compile's summary
    silently answers for another's -- a u85 target inheriting a u55 compile's
    arena. One directory per run makes that impossible, and keeps vela's
    intermediate output from landing beside the `.alpmodel` package."""
    return out_dir / f"vela-{_UNSAFE_DIR_CHARS.sub('_', accel_config)}"


def _parse_vela_profile(stdout: str) -> tuple[str | None, str | None]:
    """(system_config, memory_mode) vela resolved for this run, from its own
    network-summary block. Either is None when the line is absent (an
    unexpected vela output shape) -- never guessed at."""
    found = dict(_PROFILE_RE.findall(stdout))
    return found.get("System configuration"), found.get("Memory mode")


def _summary_csv(out_dir: Path, stem: str, system_config: str | None) -> Path | None:
    """vela's summary CSV for THIS run: the exact `<stem>_summary_<system_
    config>.csv` name when the run's own system-config was readable, else the
    sole glob match. `None` for no match AND for an ambiguous one -- picking
    `sorted(matches)[0]` out of several is only ever right by accident of sort
    order, and a footprint attributed to the wrong compile is worse than no
    footprint at all (which the caller refuses loudly)."""
    if system_config:
        exact = out_dir / f"{stem}_summary_{system_config}.csv"
        if exact.is_file():
            return exact
    matches = sorted(out_dir.glob(f"{stem}_summary_*.csv"))
    return matches[0] if len(matches) == 1 else None


def _parse_vela_summary(out_dir: Path, stem: str,
                        *, system_config: str | None = None) -> dict[str, float]:
    """Every `<mem_area>_memory_used` figure vela reported for this run, keyed
    by lower-cased memory-area name ("sram", "dram", "on_chip_flash", ...), in
    KiB.

    The values are ALREADY KiB in the CSV, not bytes (`memory_used[...] /
    1024.0`, ethosu/vela/stats_writer.py:123 in
    write_summary_metrics_csv_common) -- despite looking byte-scale at a glance
    (e.g. `72.734375`). `arena_cache_size` (`arch.arena_cache_size / 1024`,
    stats_writer.py:107) is a DIFFERENT column -- the accelerator config's
    configured cache capacity, a build-time knob unrelated to any one model's
    footprint -- and is deliberately not among these.

    `{}` when the summary is missing or unparseable; a memory area vela did
    not write is simply absent, and one it wrote as 0.0 is present as 0.0 (the
    two are different facts, and `_refuse_zero_sram_footprint` says which)."""
    path = _summary_csv(out_dir, stem, system_config)
    if path is None:
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {}
    used: dict[str, float] = {}
    for key, val in rows[0].items():
        m = _MEM_USED_RE.match(key.lower()) if key else None
        if m is None:
            continue
        try:
            used[m.group(1)] = float(val)
        except (TypeError, ValueError):
            continue
    return used


def _footprint(used: dict[str, float]) -> tuple[int, int]:
    """(arena_bytes, req_sram_kib) from @used's SRAM working set.

    The model's arena requirement is its SRAM working set, `sram_memory_used`:
    the scratch region alp-sdk's `alp_model_open()` `arena`/`arena_bytes` sizes
    at runtime. req_sram_kib is rounded UP (ceil, never floor/truncate): the
    device-side fit gate (`t->req_sram_kib <= e->arena_sram_kib`,
    src/backends/inference/alp_model_select.c) must never under-report a
    model's requirement, or a model that doesn't actually fit could pass the
    gate as if it did.

    (0, 0) only when vela reported no SRAM at all -- correct for a full CPU
    fallback (measured: `float32_fc.tflite` at `ethos-u85-256` reports 0.0 for
    EVERY memory area), and refused by the caller for anything else.

    KNOWN GAP, OPEN, AND NOT SILENTLY PATCHABLE HERE: under `--memory-mode
    Sram_Only` this reads the ARENA ONLY. vela files the const/weights region
    under `on_chip_flash_memory_used`, and on a `Sram_Only` part that is a pure
    BOOKKEEPING RENAME rather than a placement -- verbatim from
    `ethosu/vela/architecture_features.py`, when the const area's port maps to
    SRAM and const/arena/cache are the same area: `"Info: Changing
    const_mem_area from Sram to OnChipFlash. This will use the same
    characteristics as Sram."`, after which `memory_clock_scales`,
    `memory_burst_length`, `memory_ports_used` and `memory_latency` for
    `OnChipFlash` are all assigned from `Sram`. It happens because
    `[Memory_Mode.Sram_Only]` puts `const_mem_area`, `arena_mem_area` and
    `cache_mem_area` all on `Axi0` (vela 5.1.0's own `vela.ini`) while vela's
    validity check forbids naming SRAM as a const area.

    On an Alif Ensemble module that region really IS SRAM-resident:
    alp-sdk's `examples/aen/aen-npu-inference-alp/src/main.c` memcpy's the
    model into `static uint8_t model_sram[NETWORK_MODEL_LEN]
    __aligned(NPU_ALIGN) __attribute__((section("SRAM0")));` with the tensor
    arena in SRAM0 beside it, and `src/backends/inference/ethos_u_aen.cpp`
    pins every NPU access to the SRAM AXI port for exactly this reason --
    verbatim, "everything a SRAM_Only model touches is SRAM0-resident". So on
    the real 44-op `person_detect_int8.tflite` at `ethos-u85-256`, measured
    with `ethos-u-vela` 5.1.0: `sram_memory_used = 72.0` and
    `on_chip_flash_memory_used = 235.265625`, i.e. 72.0 + 235.265625 =
    307.265625 KiB genuinely resident in SRAM0 against a reported
    `req_sram_kib = 72` -- an under-report, which is the one direction this
    function's own contract above says it must never go.

    Summing the columns is NOT the fix and is deliberately not done: an
    integration that XIPs weights out of flash would then be OVER-reported by
    the same figure, and a target that does not fit would be refused a board it
    fits. Getting it right needs a per-part statement of where the const region
    physically lands, which no metadata in either repo carries today. That is a
    maintainer decision, not something to guess at here -- so the gap is
    recorded rather than papered over, and
    `tests/model/test_build.py::test_the_soms_memory_mode_makes_the_refused_
    target_ship_at_all` asserts only that the figure is nonzero, never that it
    is right."""
    sram_kib = used.get("sram", 0.0)
    if sram_kib <= 0:
        return 0, 0
    return round(sram_kib * 1024), math.ceil(sram_kib)


def _defaulted_flags(stdout: str) -> frozenset[str]:
    """Which profile flags vela fell back to a built-in for, by vela's OWN
    names for them (`_SYSTEM_CONFIG_FLAG` / `_MEMORY_MODE_FLAG`), read from its
    own warnings. Empty when it defaulted neither."""
    return frozenset(_DEFAULTED_RE.findall(stdout))


def _profile_clause(system_config: str | None, memory_mode: str | None,
                    defaulted: frozenset[str]) -> str:
    """The profile THIS run resolved, named as the run itself reported it
    (`_parse_vela_profile`) -- never a hardcoded `Ethos_U85_*`, which would
    blame an Alif memory model for an `ethos-u65-256` refusal on the NXP
    E1M-NX9101 (tan-cli#789 review MAJOR 3).

    @defaulted is vela's OWN verdict (`_defaulted_flags` over its stdout), not
    an assumption, and it is per-flag: the blanket "vela's BUILT-IN default
    profile" tail is used only when vela defaulted EVERY name in the clause.
    When it defaulted some but not all -- the shape tan produces now that it
    passes a metadata-sourced `--memory-mode` and no `--system-config` -- each
    name says for itself which it was, so a supplied flag is never attributed
    to vela and a defaulted one is never presented as the module's."""
    parts = [(label, name) for label, name in
             ((_SYSTEM_CONFIG_FLAG, system_config), (_MEMORY_MODE_FLAG, memory_mode)) if name]
    if not parts:
        return "an unreported profile"
    named = " / ".join(name for _, name in parts)
    if all(label in defaulted for label, _ in parts):
        return f"{named}, vela's BUILT-IN default profile"
    if not any(label in defaulted for label, _ in parts):
        return named
    return " / ".join(f"{name} (vela's built-in default)" if label in defaulted else name
                      for label, name in parts)


def _refusal_remedy(defaulted: frozenset[str], silicon_ref: str | None) -> str:
    """What the reader can actually DO -- which today is: nothing to this
    target, and the rest of the SKU still builds.

    The message this replaced ended "Compile against a --system-config/
    --memory-mode matching this module's memory model instead", which
    prescribed an action tan cannot perform (tan-cli#789 review BLOCKER 2):
    `compile()` never reads `opts`, nothing under `tan/` passes either flag,
    and alp-sdk's `board.schema.json` declares `models[].compile` as
    `additionalProperties: false` over `deepx_dxm1`/`drpai` -- there is no
    `ethos_u` key to route a profile through. Adding one is an alp-sdk schema
    change (ADR-0028 leaves `metadata/schemas/` with alp-sdk), so this states
    the real position instead of promising a flag that does not exist.

    The `ensemble_vela.ini` pointer is likewise stated only where it is TRUE
    (tan-cli#789 review (g)). It was unconditional, so the `ethos-u65-256`
    refusal on `E1M-NX9101` -- an NXP i.MX 93, whose alp-sdk-documented vela
    invocation involves no proprietary `.ini` whatsoever -- sent that reader
    hunting an Alif file. Gated on @silicon_ref, the SoM preset's own
    `silicon:` ref, so `None` (caller could not resolve one) and every
    non-Alif vendor get the two clauses that hold for ALL parts and no vendor
    file at all. The first sentence stays vendor-neutral by construction: it
    says no profile was resolved for THIS part, which is a fact about that
    part's metadata, not about anyone's silicon.

    Gated on the MEMORY MODE specifically, not on "vela defaulted something":
    the memory mode is the flag that decides placement, and since alp-sdk
    #1470 tan passes it from SoC metadata while still passing no
    `--system-config`. A run that got its module's memory mode and STILL
    reported no SRAM has not been failed by a missing profile, so telling its
    reader "no module vela profile was resolved" would be false.

    That gate is also why the sentence no longer says tan "cannot pass one
    yet". It could not, when this text was written; since #1470 it does, for
    every part whose SoC spec publishes a `npu_toolchain.vela.memory_mode`.
    Reaching this branch now means THIS part's spec publishes none -- a part
    still marked TBD, which the sourcing rule leaves unset rather than
    guessing -- so the true statement is that none was resolved for it and
    vela therefore chose the placement itself. Re-sourcing the rest of the
    evidence (the "this SoC declares no DRAM interface" half) from metadata is
    a separate slice."""
    if _MEMORY_MODE_FLAG not in defaulted:
        return "`tan model build` skips this target and still builds the SKU's others."
    where = ""
    if silicon_ref and silicon_ref.startswith(_ALIF_ENSEMBLE_REF_PREFIX):
        where = ("; on this Alif Ensemble part it lives in the proprietary "
                 "ensemble_vela.ini alp-sdk does not redistribute")
    return (f"No module vela profile was resolved for this part, so vela chose its "
            f"own{where}. `tan model build` skips this target and still builds the "
            f"SKU's others.")


def _refuse_zero_sram_footprint(*, accel_config: str, npu_ops: int, cpu_ops: int | None,
                                used: dict[str, float], system_config: str | None,
                                memory_mode: str | None, defaulted: frozenset[str],
                                silicon_ref: str | None) -> NoReturn:
    """A successful compile that placed operators on the NPU but reports no
    SRAM working set is a refusal, not a zero.

    `req_sram_kib == 0` does not read as "this model needs no arena" on the
    device side: alp-sdk's selector reads it as *fits any envelope*
    (`return e->arena_sram_kib == 0u || t->req_sram_kib <= e->arena_sram_kib;`,
    src/backends/inference/alp_model_select.c) and then hands the caller
    `arena_bytes = 0`. A board would trust that. Measured, real `ethos-u-vela`
    5.1.0: `keyword_scrambled_8bit.tflite` at `ethos-u85-256` places 6 of 15
    operators on the NPU, exits 0, and reports `sram_memory_used = 0.0`
    alongside `dram_memory_used = 5.359375` -- because vela's own default
    profile is DRAM-backed. The footprint is real; it is just not expressed in
    a memory area this module has. Refusing loudly is the only honest answer
    available without inventing a profile (see the module docstring).

    Raises `VelaFootprintRefused`, not a bare `RuntimeError`: this refusal
    costs the caller ONE target, never the whole package (see that type)."""
    total = npu_ops + (cpu_ops or 0)
    elsewhere = ", ".join(f"{area} {kib:.2f} KiB"
                          for area, kib in sorted(used.items()) if kib > 0)
    where = (f"its working set went to {elsewhere}" if elsewhere
             else "it reported no memory use in any area")
    # NOTE the wording of the selector clause: it says "accepts ... against ANY
    # arena size", NEVER the retired "fits" vocabulary. This string reaches a
    # `basis: "static-screen"` report note through `tan.model.check`'s
    # `_footprint_refused_note`, and a static screen must never emit "fits" in
    # any form (`test_the_word_fits_never_appears_in_a_static_screen_report`,
    # `test_the_retired_word_never_leaks_into_the_clis_rendered_static_screen_
    # output`). Until this round the clause was there but the 200-character
    # note budget happened to cut it off; widening that budget would have
    # walked it straight into the envelope.
    raise VelaFootprintRefused(
        f"vela compiled cleanly for {accel_config} ({npu_ops}/{total} operators on the NPU) "
        f"but reported 0 KiB SRAM: {where} under "
        f"{_profile_clause(system_config, memory_mode, defaulted)}. Refused because alp-sdk's "
        f"on-device selector accepts req_sram_kib == 0 against ANY arena size "
        f"(src/backends/inference/alp_model_select.c). "
        f"{_refusal_remedy(defaulted, silicon_ref)}")


def _default_profile_caveats(defaulted: frozenset[str], system_config: str | None,
                             memory_mode: str | None) -> tuple[str, ...]:
    """vela's own "may be invalid or non-optimal" verdict, surfaced as a report
    caveat for whichever profile flags it had to default -- so a customer
    cannot mistake a default-profile figure for one authored for this module.
    `()` when vela defaulted neither.

    TWO SHAPES, because the two flags carry different weight and since alp-sdk
    #1470 they routinely differ:

      * NO MEMORY MODE -- the hard caveat, unchanged. vela chose the placement,
        its built-in default is DRAM-backed, and the arena/SRAM figures
        therefore describe a memory model the module may not have at all.
      * MEMORY MODE SUPPLIED, SYSTEM CONFIG DEFAULTED -- the ordinary shape of
        every compile tan issues today. The memory figures are the module's:
        measured on `tests/fixtures/models/tiny_int8.tflite` at
        `ethos-u85-256`, `--memory-mode Sram_Only` alone and
        `--system-config Ethos_U85_SYS_Flash_High --memory-mode Sram_Only`
        report byte-identical `sram_memory_used`/`on_chip_flash_memory_used`.
        What vela's default system config does decide is the bandwidth/latency
        cost model its scheduler optimises against, so the caveat says exactly
        that and does NOT repeat the "figures describe the wrong memory model"
        sentence, which would now be false.

    Takes @defaulted rather than re-searching stdout: `compile()` resolves it
    once (`_defaulted_flags`) and hands the SAME fact to this and to
    `_refuse_zero_sram_footprint`, so the caveat and the refusal can never
    disagree about which flags vela defaulted."""
    if not defaulted:
        return ()
    named = ", ".join(f"{cli} {name}" for label, cli, name in
                      ((_SYSTEM_CONFIG_FLAG, "system-config", system_config),
                       (_MEMORY_MODE_FLAG, "memory-mode", memory_mode))
                      if name and label in defaulted)
    if _MEMORY_MODE_FLAG in defaulted:
        return (f"vela used its BUILT-IN default profile ({named or 'unreported'}), not one "
                f"authored for this module -- vela's own warning for that is \"Compilation "
                f"may be invalid or non-optimal\". The arena/SRAM figures and the compiled "
                f"command stream describe that default memory model, not this module's.",)
    return (f"vela used its BUILT-IN default {named or 'system-config unreported'} for "
            f"bandwidth/latency estimates -- no module-authored one is available -- so its "
            f"scheduling is tuned for that system, not this module's. The arena/SRAM figures "
            f"are unaffected: they follow --memory-mode {memory_mode}, which came from this "
            f"module's SoC metadata.",)


def _parse_vela_placement(stdout: str) -> tuple[int, int] | None:
    """(cpu_op_count, npu_op_count) from vela's own "CPU/NPU operators = N
    (P%)" summary lines -- vela's REAL per-run placement verdict, not
    inferred from its exit code (0 either way). None when either line is
    absent (an unexpected vela output shape, e.g. a future version that
    changes this text): a caller must not fabricate a placement it can't
    actually read."""
    counts = {k: int(v) for k, v in _PLACEMENT_RE.findall(stdout)}
    if "CPU" not in counts or "NPU" not in counts:
        return None
    return counts["CPU"], counts["NPU"]


class VelaAdapter(CompilerAdapter):
    backend = "ethos_u"

    def is_available(self) -> bool:
        return shutil.which("vela") is not None

    def accepts(self, src_format: str) -> bool:
        return src_format == "tflite"

    def compile(self, source: Path, *, accel_config: str, out_dir: Path,
                opts: dict | None = None, silicon_ref: str | None = None,
                vela_memory_mode: str | None = None,
                vela_system_config: str | None = None) -> Blob:
        # @silicon_ref reaches ONLY the refusal wording (`_refusal_remedy`) --
        # the vela command line below is byte-identical with and without it,
        # so no vendor can ever get a different artifact out of this adapter.
        # @vela_memory_mode / @vela_system_config are the opposite: they are
        # the SILICON's own profile (`TargetSpec.vela_*`, out of the SoC spec's
        # `npu_toolchain.vela`) and they DO change the artifact -- that is the
        # point. Both stay optional and default to None, so a caller that
        # resolved no profile gets byte-for-byte the flagless invocation this
        # adapter has always issued.
        run_dir = _run_dir(out_dir, accel_config)
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["vela", str(source), "--accelerator-config", accel_config,
               "--output-dir", str(run_dir)]
        # Placement is decided by --memory-mode, bandwidth by --system-config;
        # measured on `tests/fixtures/models/tiny_int8.tflite` at
        # `ethos-u85-256`, adding `--system-config Ethos_U85_SYS_Flash_High`
        # alongside `--memory-mode Sram_Only` changes no memory figure at all.
        # So the memory mode is the load-bearing one, and every value that
        # reaches it is an Arm built-in -- this works with no vendor .ini.
        if vela_memory_mode:
            cmd += ["--memory-mode", vela_memory_mode]
        # NEVER emitted alone by this adapter's own default: `resolve_targets`
        # withholds a vendor-gated name (`_vela_profile`), because a section
        # vela cannot resolve is rc=1, not a degradation. Passing `--config`
        # so a vendor-tuned name becomes legal is a separate slice.
        if vela_system_config:
            cmd += ["--system-config", vela_system_config]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_VELA_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"vela timed out after {exc.timeout}s for {accel_config}") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"vela failed for {accel_config}: {proc.stderr.strip()}")
        produced = run_dir / f"{source.stem}_vela.tflite"
        if not produced.is_file():
            raise RuntimeError(f"vela produced no output at {produced}")
        system_config, memory_mode = _parse_vela_profile(proc.stdout)
        defaulted = _defaulted_flags(proc.stdout)
        used = _parse_vela_summary(run_dir, source.stem, system_config=system_config)
        arena, sram_kib = _footprint(used)
        placement = _parse_vela_placement(proc.stdout)
        cpu_ops, npu_ops = placement if placement is not None else (None, None)
        # Only a CONFIRMED NPU placement refuses: 0 NPU operators is a real,
        # legitimate 0 KiB footprint (full CPU fallback), and an unreadable
        # placement summary already degrades through `tan.model.check`'s
        # `_vela_placement_unreadable` rather than claiming anything.
        if npu_ops and sram_kib <= 0:
            _refuse_zero_sram_footprint(accel_config=accel_config, npu_ops=npu_ops,
                                        cpu_ops=cpu_ops, used=used,
                                        system_config=system_config, memory_mode=memory_mode,
                                        defaulted=defaulted, silicon_ref=silicon_ref)
        return Blob(format="vela_tflite", payload=produced.read_bytes(),
                    arena_bytes=arena, compiler_version=_vela_version(),
                    req_sram_kib=sram_kib, cpu_op_count=cpu_ops, npu_op_count=npu_ops,
                    caveats=_default_profile_caveats(defaulted, system_config, memory_mode))
