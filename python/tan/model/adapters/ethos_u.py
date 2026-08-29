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
Ethos_U85_SYS_Flash_High` on top of `--memory-mode Sram_Only` changes NO memory
figure (measured on `tiny_int8.tflite` AND on the 44-op
`person_detect_int8.tflite`, both at `ethos-u85-256`, against the default
`Ethos_U85_SYS_DRAM_Mid`).

THAT INVARIANCE IS SCOPED TO `Sram_Only` AND IS NOT A GENERAL RULE. The two
flags compose in two levels, verbatim from vela 5.1.0's own `vela.ini`: a
`Memory_Mode` section assigns const/arena/cache to AXI PORTS
(`[Memory_Mode.Sram_Only]` -> `const_mem_area` = `arena_mem_area` =
`cache_mem_area` = `Axi0`, `vela.ini:235-238`), and a `System_Config` section
maps those ports to MEMORY AREAS (`axi0_port=`/`axi1_port=`, `vela.ini:60-117`).
Under `Sram_Only` all three sit on `Axi0` and all 11 `System_Config` sections
vela 5.1.0 ships set `axi0_port=Sram`, so no system config can move anything --
which is the only reason those two measurements matched. `Shared_Sram` sets
`const_mem_area=Axi1` (`vela.ini:242-245`), and that is the mode tan passes for
`E1M-NX9101`; there the system config decides the const region's memory area
outright. Measured, `person_detect_int8.tflite` at `ethos-u65-256 --memory-mode
Shared_Sram`, changing ONLY `--system-config` (KiB, from vela's own summary):

  * `Ethos_U65_Embedded` (`axi1_port=OffChipFlash`) -- `sram 72.734375`,
    `dram 0.0`, `off_chip_flash 228.265625`
  * `Ethos_U65_Mid_End` (`axi1_port=Dram`) -- `sram 72.734375`,
    `dram 228.3125`, `off_chip_flash 0.0`
  * `Ethos_U65_Client_Server` (`axi1_port=Dram`) -- `sram 72.734375`,
    `dram 228.25`, `off_chip_flash 0.0`

228 KiB of weights moves on the system config alone. So the honest rule is:
under `Sram_Only` the memory mode decides placement outright and the system
config is bandwidth/latency only; under an `Axi1` const mode the system config
decides placement too.

`--system-config` is therefore still not passed by default, and that is
deliberate rather than pending. The names alp-sdk's own examples use
(`Ethos_U85_SRAM_Only`, `RTSS_HE_SRAM_Only`;
`examples/aen/aen-npu-inference-alp/CMakeLists.txt:42-43` and its `-u55`
sibling) live ONLY in Alif's PROPRIETARY `ensemble_vela.ini`, which alp-sdk does
not redistribute, and handing vela a section it cannot resolve is a hard rc=1,
verbatim: `Section System_Config.Ethos_U85_SRAM_Only not found in Vela config
file`. So the SoC spec flags those as vendor-gated and `resolve_targets`
withholds them from the field this adapter passes freely
(`tan.model.targets._vela_profile`); vela's own default system config is used
and SAID SO through `_default_profile_caveats`, which now distinguishes the two
flags rather than blaming the whole profile on vela. Never invent a profile
here; a wrong one compiles a command stream for hardware the module does not
have.

A LICENSED CUSTOMER CAN SUPPLY THAT FILE, through `ALP_VELA_CONFIG` and never
through `board.yaml` (the path is environment, not hardware). It buys the
vendor-tuned profile only as a complete set: `--config` + the part's vendor
`System_Config` + its memory mode, all three or none -- see `compile()`, where
each half of that rule is pinned to a measured `ethos-u-vela` 5.1.0 rc. No SoC
spec names a vendor `System_Config` today, so the mechanism is wired and does
nothing, which is the only correct thing it can do: passing `--config` alone is
rc=1 and naming a profile nobody published would be an invention.

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
where their profile comes from. That i.MX 93 line is no longer restated as
advice for a different reason than it was: `--memory-mode Shared_Sram` is now
what tan actually PASSES for that part, straight from `imx93.json`'s own
`npu_toolchain.vela` block, so there is nothing left to advise a reader to do
by hand.

BOTH HALVES OF THE REFUSAL'S EVIDENCE NOW COME FROM METADATA, not from prose in
this file: WHICH vendor file (if any) to name is the SoC spec's own
`npu_toolchain.vela.vendor_config_filename` (`@vela_vendor_config_filename`),
and WHY a DRAM placement is wrong here is `@soc_declares_dram`, resolved from
its `external_memory_interfaces[]` -- `alif:ensemble:e8` lists exactly `HexSPI`
and `SD/eMMC`, `nxp:imx9:imx93` lists `LPDDR4/4X`. Each is then right for every
part automatically, including one nobody has looked at yet.

Neither is read from `metadata/` HERE: `resolve_targets` already has the SoC
spec open, an adapter that reads the SDK's fact tree would be a second
unversioned reader of it, and a fact resolved once and threaded cannot disagree
with itself between the target and the artifact it produced."""
from __future__ import annotations
import csv
import math
import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NoReturn

from tan.core.subprocess_env import spawn_env
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

# The memory modes whose CONST/weights area sits on the same AXI port as the
# arena (`Axi0`), read off vela 5.1.0's own `vela.ini`: `[Memory_Mode.Sram_Only]`
# is `const_mem_area=arena_mem_area=cache_mem_area=Axi0` (`vela.ini:235-238`),
# and it is the only shipped section in that shape -- `Shared_Sram` and all four
# `Dedicated_Sram*` sections set `const_mem_area=Axi1` (`vela.ini:242-269`).
#
# It matters for the CAVEAT, not the command line: a `System_Config` maps ports
# to memory AREAS, so on an `Axi0` const mode a defaulted system config cannot
# move the const region anywhere (all 11 sections set `axi0_port=Sram`) and the
# caveat is honestly bandwidth-only, while on an `Axi1` const mode that same
# default DECIDED where the const region lives -- measured on
# `person_detect_int8.tflite` at `ethos-u65-256 --memory-mode Shared_Sram`,
# 228 KiB of weights moves between `off_chip_flash` and `dram` on the system
# config alone. An UNRECOGNISED name is treated as NOT-Axi0 on purpose: the
# under-claiming caveat is the safe direction.
_AXI0_CONST_MEMORY_MODES = frozenset({"Sram_Only"})

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

# vela's own memory-area name for external DRAM, as it appears in its summary
# CSV columns (`dram_memory_used`) and therefore as `_parse_vela_summary` keys
# it -- the area a working set must NOT land in on a part whose SoC spec
# declares no DRAM interface.
_DRAM_AREA = "dram"

# What the refusal appends to that area's figure when the caller resolved
# `soc_declares_dram=False` -- i.e. the SoC spec's `external_memory_interfaces`
# names no DDR/DRAM kind at all (`tan.model.targets._soc_declares_dram`; on
# `alif:ensemble:e8` it lists exactly `HexSPI` and `SD/eMMC`). Machine-checked
# per part rather than asserted in prose, so it is silent for a part that
# declares DRAM (`nxp:imx9:imx93` -- LPDDR4/4X) or declares nothing.
#
# Kept SHORT deliberately: it lands inside a one-line note bounded by
# `tan.model.check._VELA_REFUSAL_NOTE_BUDGET` (700), against which the maximal
# refusal measures 691 with this marker and a 17-character
# `vendor_config_filename` in it (`tests/model/test_check.py::
# test_the_refusal_note_budget_covers_a_maximal_refusal`, which renders the
# real template rather than a copy). That filename is the one variable-length
# input in the whole template, so those 9 characters are the headroom a longer
# future one has to fit inside.
_NO_DRAM_MARKER = " (no DRAM interface on this SoC)"


#: Where a licensed customer names their vendor vela config `.ini` -- an
#: ENVIRONMENT fact, never a hardware one, so it is read here and never from
#: `board.yaml`. alp-sdk's own schema says why: `models[].compile` is for a
#: per-model config "the SDK cannot derive", and a vela profile IS derivable
#: from the SKU; a local absolute path in a committed board file would also
#: travel to every other machine that opens it. Same shape as the other two
#: toolchain env vars tan already reads in adapters (`ALP_DRPAI_TVM_HOME`,
#: `ALP_DEEPX_SDK_HOME`).
_VELA_CONFIG_ENV = "ALP_VELA_CONFIG"


def _vendor_config_path() -> Path | None:
    """The vendor vela `.ini` this host has, or `None`.

    `None` for unset, empty, and for a value that does not name a readable
    FILE -- vela would fail on a path it cannot open, and an env var pointing
    at a stale location must degrade to "no vendor config" (Arm's built-in
    profile, which is what makes the arena figures correct in the first place)
    rather than break a build that worked yesterday."""
    raw = os.environ.get(_VELA_CONFIG_ENV)
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


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
    one-line surface): a report note and an `.alpmodel` coverage reason.

    WHERE THE FIRST BULLET IS ACTUALLY ENFORCED, so nobody re-derives it the
    hard way: the four tests that pin "one refusal costs ONE target" live in
    `tests/model/test_build.py`, which carries a MODULE-LEVEL
    `pytest.mark.skipif` on `ALP_SDK_ROOT` (`test_build.py:44`). So in the bare
    install CI's `gates` job uses -- `pip install -e ./python`, no
    `ALP_SDK_ROOT` -- replacing `build_model`'s `except VelaFootprintRefused`
    body with a bare `raise` passes the whole suite green. The guard is real,
    but it lives in `parity.yml`, which binds `ALP_SDK_ROOT` from
    `PINNED_SDK_TAG` on every `pull_request`/`push`; `ci.yml`'s `gates` job
    binds it only on the release path (its `sdk_parity` input). Do not read a
    green bare run as having exercised this."""


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


# THE VENDOR CLAUSE IN `_refusal_remedy` NAMES THE `System_Config`
# SPECIFICALLY, and must keep doing so (tan-cli#789 review MINOR 4). It used to
# read "it lives in the proprietary ensemble_vela.ini", whose antecedent is the
# "module vela profile" of the sentence before -- and since alp-sdk #1470 that
# is false for the load-bearing half of the profile: the memory mode
# (`Sram_Only` on every Alif Ensemble part) is an ARM BUILT-IN tan passes with
# no `.ini` at all. Only the tuned `System_Config` names (`Ethos_U85_SRAM_Only`,
# `RTSS_HE_SRAM_Only`) live in Alif's file. And since that branch is now reached
# only when the part's SoC spec publishes no `npu_toolchain.vela.memory_mode`,
# the reader's REAL fix is a metadata entry in alp-sdk, not a vendor download --
# the vendor clause survives only to explain why tan cannot name a complete
# profile there, never as the remedy. (Stating the metadata fix in the shipped
# string does not fit: `_VELA_REFUSAL_NOTE_BUDGET` is 700 and the maximal
# refusal already measures 691 with this clause in it --
# `tests/model/test_check.py::test_the_refusal_note_budget_covers_a_maximal_
# refusal` is what holds that line.)
#
# WHICH FILE IT NAMES IS NOW METADATA'S ANSWER, NOT A VENDOR PREFIX MATCH. The
# gate used to be `silicon_ref.startswith("alif:ensemble:")` with the filename
# `ensemble_vela.ini` hardcoded beside it -- a standing claim about one vendor
# that no data could correct. It is now emitted iff this part's own SoC spec
# declares `npu_toolchain.vela.vendor_config_filename`, and names exactly that.
# So "a non-Alif refusal never names an Alif file" holds for the stronger
# reason that no part is ever handed another part's file at all
# (`nxp:imx9:imx93` declares none). `silicon_ref` was the gate's only reader
# and is therefore gone from this adapter's interface.
#
# `System_Config` and NOT `--system-config`: that is vela's own INI section name
# (`Section System_Config.<name> not found in Vela config file`), so it
# identifies the right half of the profile without spelling a CLI flag into a
# message whose whole point is that it prescribes nothing tan can pass
# (tan-cli#789 review BLOCKER 2, pinned by
# `test_the_refusal_prescribes_nothing_tan_cannot_actually_do`).
#
# The MEMORY-MODE gate is also why that sentence no longer says tan "cannot pass
# one yet". It could not, when the text was written; since #1470 it does, for
# every part whose SoC spec publishes a `npu_toolchain.vela.memory_mode`.
# Reaching the branch now means THIS part's spec publishes none -- a part still
# marked TBD, which the sourcing rule leaves unset rather than guessing -- so the
# true statement is that none was resolved for it and vela therefore chose the
# placement itself.
def _refusal_remedy(defaulted: frozenset[str], vendor_config_filename: str | None) -> str:
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

    The vendor-file pointer is likewise stated only where it is TRUE
    (tan-cli#789 review (g)). It was unconditional, so the `ethos-u65-256`
    refusal on `E1M-NX9101` -- an NXP i.MX 93, whose alp-sdk-documented vela
    invocation involves no proprietary `.ini` whatsoever -- sent that reader
    hunting an Alif file. Gated now on @vendor_config_filename, this part's own
    SoC-spec declaration, so `None` (the part declares none, or the caller
    resolved no spec) gets the two clauses that hold for ALL parts and no
    vendor file at all. The first sentence stays part-neutral by construction:
    it says no profile was resolved for THIS part, which is a fact about that
    part's metadata, not about anyone's silicon.

    Gated on the MEMORY MODE specifically, not on "vela defaulted something":
    the memory mode is the flag that decides placement, and since alp-sdk
    #1470 tan passes it from SoC metadata while still passing no
    `--system-config`. A run that got its module's memory mode and STILL
    reported no SRAM has not been failed by a missing profile, so telling its
    reader "no module vela profile was resolved" would be false. See the
    comment block above this function for what that gate implies for the
    vendor clause and for the retired "cannot pass one yet" wording."""
    if _MEMORY_MODE_FLAG not in defaulted:
        return "`tan model build` skips this target and still builds the SKU's others."
    where = ""
    if vendor_config_filename:
        where = (f"; its System_Config lives in the proprietary {vendor_config_filename} "
                 f"alp-sdk does not redistribute")
    return (f"No module vela profile was resolved for this part, so vela chose its "
            f"own{where}. `tan model build` skips this target and still builds the "
            f"SKU's others.")


def _no_dram_marker(area: str, soc_declares_dram: bool | None) -> str:
    """`_NO_DRAM_MARKER` for the DRAM area on a part whose SoC spec declares no
    DRAM interface; `""` for every other area and for every other answer.

    `is False` and never a truthiness test: `None` means the spec declared no
    `external_memory_interfaces` at all (`tan.model.targets._soc_declares_dram`),
    and an unknown must not be rendered as evidence that a part has no DRAM.
    The refusal is a string a customer sizes hardware from; the safe direction
    is silence."""
    if area == _DRAM_AREA and soc_declares_dram is False:
        return _NO_DRAM_MARKER
    return ""


def _refuse_zero_sram_footprint(*, accel_config: str, npu_ops: int, cpu_ops: int | None,
                                used: dict[str, float], system_config: str | None,
                                memory_mode: str | None, defaulted: frozenset[str],
                                vendor_config_filename: str | None = None,
                                soc_declares_dram: bool | None = None) -> NoReturn:
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
    costs the caller ONE target, never the whole package (see that type).

    THE EVIDENCE FOR "a memory area this module does not have" IS SOURCED, NOT
    ASSERTED. @soc_declares_dram is this part's own SoC spec answering whether
    it declares any external DRAM interface at all, resolved by
    `tan.model.targets._soc_declares_dram` from `external_memory_interfaces[]`
    and threaded here on `TargetSpec` -- so the clause is right for every part
    automatically, including one nobody has looked at yet, and absent for a part
    that does have DRAM. It is deliberately NOT resolved by reading a SoC JSON
    from inside this adapter: a compiler adapter that opens `metadata/` would be
    a second, unversioned reader of the SDK's fact tree, and the caller already
    has the spec open."""
    total = npu_ops + (cpu_ops or 0)
    elsewhere = ", ".join(f"{area} {kib:.2f} KiB{_no_dram_marker(area, soc_declares_dram)}"
                          for area, kib in sorted(used.items()) if kib > 0)
    where = (f"its working set went to {elsewhere}" if elsewhere
             else "it reported no memory use in any area")
    # NOTE the wording of the selector clause: it says "accepts ... against ANY
    # arena size", NEVER the retired "fits" vocabulary. This string reaches a
    # `basis: "static-screen"` report note through `tan.model.check`'s
    # `_footprint_refused_note`, and a static screen must never emit "fits" in
    # any form. Until this round the clause was there but the 200-character
    # note budget happened to cut it off; widening that budget would have
    # walked it straight into the envelope.
    #
    # THE ONE TEST THAT ENFORCES THAT is
    # `test_a_refused_footprint_is_not_reported_as_a_failed_compile`
    # (`tests/model/test_check.py`), and only because it renders its note by
    # CALLING this function. It used to raise a hand-copied literal of this
    # template, which enforced nothing: the copy was byte-identical to the
    # template it came from, so rewording this clause to "reads req_sram_kib
    # == 0 as fits any envelope" left the whole suite green while that phrase
    # reached the customer note and the JSON envelope (tan-cli#789 review
    # BLOCKER). The two static-screen guards this comment used to name --
    # `test_the_word_fits_never_appears_in_a_static_screen_report`
    # (`tests/model/test_analyze.py`) and `test_the_retired_word_never_leaks_
    # into_the_clis_rendered_static_screen_output`
    # (`tests/commands/test_model_check_command.py`) -- do NOT reach this
    # template and never did: the first runs live reports that never provoke a
    # refusal, and the second monkeypatches `check_model_backends` with
    # fabricated reports whose notes are hand-written literals. Naming them
    # here claimed an enforcement that did not exist, which is exactly what
    # stops the next reader from adding a real one.
    raise VelaFootprintRefused(
        f"vela compiled cleanly for {accel_config} ({npu_ops}/{total} operators on the NPU) "
        f"but reported 0 KiB SRAM: {where} under "
        f"{_profile_clause(system_config, memory_mode, defaulted)}. Refused because alp-sdk's "
        f"on-device selector accepts req_sram_kib == 0 against ANY arena size "
        f"(src/backends/inference/alp_model_select.c). "
        f"{_refusal_remedy(defaulted, vendor_config_filename)}")


# `_default_profile_caveats` EMITS THREE SHAPES, because the two profile flags
# carry different weight, since alp-sdk #1470 they routinely differ, and how
# much a DEFAULTED system config cost depends on the memory mode beside it:
#
#   * NO MEMORY MODE -- the hard caveat, unchanged. vela chose the placement,
#     its built-in default is DRAM-backed, and the arena/SRAM figures therefore
#     describe a memory model the module may not have at all.
#   * MEMORY MODE SUPPLIED WITH `Axi0` CONST (`Sram_Only`), SYSTEM CONFIG
#     DEFAULTED -- the shape every Alif Ensemble compile takes. Here the default
#     really is bandwidth-only: const, arena and cache are all on `Axi0` and all
#     11 Arm `System_Config` sections map `axi0_port=Sram`, so nothing the
#     system config says can move a byte. Measured, `--memory-mode Sram_Only`
#     alone versus `--system-config Ethos_U85_SYS_Flash_High --memory-mode
#     Sram_Only`, on `tests/fixtures/models/tiny_int8.tflite` AND on the 44-op
#     `person_detect_int8.tflite`, both at `ethos-u85-256`: identical
#     `sram_memory_used`/`on_chip_flash_memory_used`. So this caveat does NOT
#     repeat the "figures describe the wrong memory model" sentence, which would
#     be false.
#   * MEMORY MODE SUPPLIED WITH `Axi1` CONST (`Shared_Sram`, the mode tan passes
#     for `E1M-NX9101`; also every `Dedicated_Sram*`), SYSTEM CONFIG DEFAULTED --
#     calling THAT bandwidth-only would be false, which is why it is its own
#     shape (tan-cli#789 review MAJOR 2). The const/weights region is on the
#     other port, and it is the system config that maps ports to memory areas,
#     so vela's default picked where the weights live. Measured on
#     `person_detect_int8.tflite` at `ethos-u65-256 --memory-mode Shared_Sram`,
#     changing only `--system-config`: `Ethos_U65_Embedded` files 228.265625 KiB
#     under `off_chip_flash`, `Ethos_U65_Mid_End` 228.3125 KiB under `dram`,
#     `Ethos_U65_Client_Server` (vela's own default for this accel config)
#     228.25 KiB under `dram` -- same `sram 72.734375` throughout. This text
#     ships inside the customer's `.alpmodel`, so it must say that placement was
#     vela's, not the module's.
#
# A memory mode this module cannot classify (`_AXI0_CONST_MEMORY_MODES`) takes
# the third shape too -- under-claiming is the safe direction for a string a
# customer sizes hardware from. A run whose summary block named NO memory mode
# at all gets a fourth, narrower sentence: it says tan cannot tell which of the
# two happened, rather than picking one.
def _default_profile_caveats(defaulted: frozenset[str], system_config: str | None,
                             memory_mode: str | None) -> tuple[str, ...]:
    """vela's own "may be invalid or non-optimal" verdict, surfaced as a report
    caveat for whichever profile flags it had to default -- so a customer
    cannot mistake a default-profile figure for one authored for this module.
    `()` when vela defaulted neither.

    THREE SHAPES -- see the comment block above this function for what each
    says and the measurements behind it.

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
    default_of = named or "system-config unreported"
    if memory_mode in _AXI0_CONST_MEMORY_MODES:
        return (f"vela used its BUILT-IN default {default_of} for "
                f"bandwidth/latency estimates -- no module-authored one is available -- so its "
                f"scheduling is tuned for that system, not this module's. The arena/SRAM figures "
                f"are unaffected: they follow --memory-mode {memory_mode}, which came from this "
                f"module's SoC metadata, whose const/arena/cache areas are all one AXI port every "
                f"system config maps to SRAM.",)
    if memory_mode is None:
        return (f"vela used its BUILT-IN default {default_of} -- no module-authored one is "
                f"available -- so its scheduling is tuned for that system, not this module's. "
                f"This run reported no memory mode, so tan cannot say whether that default also "
                f"chose the memory area the const/weights region landed in.",)
    return (f"vela used its BUILT-IN default {default_of} -- no module-authored one is "
            f"available -- so its scheduling is tuned for that system, not this module's, and "
            f"under --memory-mode {memory_mode} it was NOT bandwidth-only: that mode puts the "
            f"const/weights region on a different AXI port from the arena, and it is the system "
            f"config that maps ports to memory areas, so vela's default also chose which memory "
            f"the weights land in. Only the arena/SRAM figures follow --memory-mode "
            f"{memory_mode}, which came from this module's SoC metadata.",)


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
                opts: dict | None = None,
                vela_memory_mode: str | None = None,
                vela_system_config: str | None = None,
                vela_vendor_system_config: str | None = None,
                vela_vendor_config_filename: str | None = None,
                soc_declares_dram: bool | None = None) -> Blob:
        # @vela_vendor_config_filename / @soc_declares_dram reach ONLY the
        # refusal wording (`_refusal_remedy`, `_no_dram_marker`) -- the vela
        # command line below is byte-identical with and without them, so no
        # part can get a different artifact out of a diagnostic fact.
        # @vela_memory_mode / @vela_system_config / @vela_vendor_system_config
        # are the opposite: they are the SILICON's own profile
        # (`TargetSpec.vela_*`, out of the SoC spec's `npu_toolchain.vela`) and
        # they DO change the artifact -- that is the point. All stay optional
        # and default to None, so a caller that resolved no profile gets
        # byte-for-byte the flagless invocation this adapter has always issued.
        run_dir = _run_dir(out_dir, accel_config)
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["vela", str(source), "--accelerator-config", accel_config,
               "--output-dir", str(run_dir)]
        # The memory mode is the load-bearing flag because it assigns const,
        # arena and cache to AXI PORTS; the system config only maps those ports
        # to memory areas. Under `Sram_Only` all three land on `Axi0` and every
        # Arm `System_Config` section maps `axi0_port=Sram`, so the system
        # config cannot move a byte -- measured, adding `--system-config
        # Ethos_U85_SYS_Flash_High` alongside `--memory-mode Sram_Only` on
        # `tiny_int8.tflite` and on `person_detect_int8.tflite` at
        # `ethos-u85-256` changes no memory figure. That is a `Sram_Only` fact,
        # NOT a general one: under `Shared_Sram` (`const_mem_area=Axi1`, the
        # mode tan passes for `E1M-NX9101`) the system config alone moves
        # 228 KiB of weights between areas -- see the module docstring's
        # measured `ethos-u65-256` sweep. Every memory-mode value that reaches
        # here is an Arm built-in, so this works with no vendor .ini.
        #
        # SIDE EFFECT WORTH KNOWING, recorded not acted on: `Sram_Only` names
        # no `arena_cache_size`, so vela schedules against an effectively
        # unbounded SRAM budget. Measured on `person_detect_int8.tflite` at
        # `ethos-u85-256`, vela's summary column moves `arena_cache_size`
        # 384.0 -> 1073741824.0 and `total_npu_encoded_weights` 205472 ->
        # 212096. Nothing in tan reads either column (`_parse_vela_summary`
        # takes `*_memory_used` only, and tan-cli#789 stopped reading
        # `arena_cache_size`), so this changes no shipped figure today.
        if vela_memory_mode:
            cmd += ["--memory-mode", vela_memory_mode]
        # An ARM BUILT-IN system config is safe on its own -- `resolve_targets`
        # only ever puts one here (`targets._vela_profile` withholds a
        # vendor-gated name from this field), because a section vela cannot
        # resolve is rc=1, not a degradation.
        if vela_system_config:
            cmd += ["--system-config", vela_system_config]
        # THE VENDOR PROFILE: ALL THREE FLAGS OR NONE OF THEM. A licensed
        # customer names their `.ini` in `ALP_VELA_CONFIG`, and only then does
        # this part's vendor-gated `System_Config` become legal to pass.
        #
        # It is a TRIPLE, not the pair the design first assumed, and that is
        # measured against real `ethos-u-vela` 5.1.0 rather than reasoned
        # (`architecture_features.py:588-608`): supplying `--config` REPLACES
        # vela's built-in `vela.ini` outright -- `self.vela_config_files =
        # vela_config_files`, no merge -- and vela then refuses any default
        # left beside it. Each of these was run:
        #
        #   * `--config <ini>` alone -> rc=1, verbatim `Error: Incorrect
        #     argument to CLI option --config=['fake_vendor.ini']: Specifying a
        #     configuration file is not allowed when using a default system
        #     configuration`
        #   * `--config <ini> --system-config <name>` -> rc=1, `... Specifying a
        #     configuration file is not allowed when using a default memory
        #     mode`
        #   * the full triple, against an .ini defining BOTH sections -> rc=0,
        #     `System configuration Ethos_U85_SRAM_Only` / `Memory mode
        #     Sram_Only` in vela's own summary block
        #
        # so the vendor `.ini` must also define the `[Memory_Mode.<mode>]` this
        # part declares -- Arm's built-in one is no longer being read. alp-sdk's
        # own Alif recipe passes exactly this triple, and only
        # `if(AEN_NPU_VELA_CONFIG)`
        # (`examples/aen/aen-npu-inference-alp/CMakeLists.txt`).
        #
        # TODAY THIS BRANCH NEVER FIRES, and doing nothing is the correct
        # behaviour rather than a gap: no SoC spec carries a `system_config` at
        # all, because an Alif `System_Config` is per CORE SUBSYSTEM, not per
        # SoC (one Ensemble die sources both `Ethos_U85_SRAM_Only` and
        # `RTSS_HE_SRAM_Only`), so a per-SoC scalar would be right for one of
        # its two or three Ethos-U accelerators and wrong for the rest. With
        # `ALP_VELA_CONFIG` set and no vendor name resolved, passing `--config`
        # anyway would be rc=1 and inventing a profile name would compile a
        # command stream for hardware nobody described. The mechanism goes live
        # the day metadata can name one per accelerator; until then it is wired
        # and silent.
        elif vela_memory_mode and vela_vendor_system_config:
            vendor_config = _vendor_config_path()
            if vendor_config is not None:
                cmd += ["--config", str(vendor_config),
                        "--system-config", vela_vendor_system_config]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_VELA_TIMEOUT_S, env=spawn_env()
            )
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
            _refuse_zero_sram_footprint(
                accel_config=accel_config, npu_ops=npu_ops, cpu_ops=cpu_ops, used=used,
                system_config=system_config, memory_mode=memory_mode, defaulted=defaulted,
                vendor_config_filename=vela_vendor_config_filename,
                soc_declares_dram=soc_declares_dram)
        return Blob(format="vela_tflite", payload=produced.read_bytes(),
                    arena_bytes=arena, compiler_version=_vela_version(),
                    req_sram_kib=sram_kib, cpu_op_count=cpu_ops, npu_op_count=npu_ops,
                    caveats=_default_profile_caveats(defaulted, system_config, memory_mode))
