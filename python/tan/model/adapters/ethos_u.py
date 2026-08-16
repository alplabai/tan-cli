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

THE PROFILE CAVEAT (tan-cli#789): this adapter passes NO `--system-config` and
NO `--memory-mode`, so vela falls back to its own built-in profile -- measured
verbatim on `ethos-u-vela` 5.1.0: "Warning: No system configuration specified.
Using a default of Ethos_U85_SYS_DRAM_Mid. Compilation may be invalid or
non-optimal." plus the same for "No memory mode specified. Using a default of
Dedicated_Sram_384KB." Both defaults are DRAM-backed
(`weights_storage_area=DRAM`, `feature_map_storage_area=DRAM` in the summary
CSV), and an Alif Ensemble E-series module is MRAM + SRAM with no DRAM at all.
A SoM-authoritative profile is NOT derivable here: the one alp-sdk itself uses
(`--system-config Ethos_U85_SRAM_Only --memory-mode Sram_Only`,
`examples/aen/aen-npu-inference-alp/CMakeLists.txt`) names sections that live
ONLY in Alif's PROPRIETARY `ensemble_vela.ini`, which alp-sdk does not
redistribute (it is `.gitignore`d there -- ".../ensemble_vela.ini ... ships in
alp-sdk-internal, NOT here") -- passing those names without that `.ini` makes
vela error outright. And there is no plumbing to pass one anyway: `compile()`
receives `opts` but has no profile keys to read, `board.schema.json`'s
`models[].compile` is `additionalProperties: false` over `deepx_dxm1`/`drpai`
only, and adding an `ethos_u` block there is an alp-sdk schema change (ADR-0028
leaves `metadata/schemas/` with alp-sdk). So the absence is made LOUD instead of
guessed at: `_default_profile_caveats` carries vela's own "may be invalid or
non-optimal" verdict, naming the defaults it actually resolved, out through
`Blob.caveats` into the report/envelope. Never invent a profile here; a wrong
one compiles a command stream for hardware the module does not have.

WHO THIS HITS (tan-cli#789 review): the DRAM-backed built-in default is NOT a
U85/Alif-only fact. Measured, real `ethos-u-vela` 5.1.0 over the committed
`tests/fixtures/models/tiny_int8.tflite`, both of these refuse:

  * `ethos-u85-256` -> `Ethos_U85_SYS_DRAM_Mid` / `Dedicated_Sram_384KB`
    (E1M-AEN401 / E1M-AEN601 / E1M-AEN801, Alif Ensemble E4/E6/E8)
  * `ethos-u65-256` -> `Ethos_U65_Client_Server` / `Dedicated_Sram_384KB`
    (E1M-NX9101, NXP i.MX 93 -- NOT an Alif part)

while every `ethos-u55-*` config (E1M-AEN301/501/701 and the U55 targets of the
three AEN SKUs above) resolves to the SRAM-backed `Ethos_U55_High_End_Embedded`
and reports a real footprint. Nor is it confined to tiny fixtures:
`keyword_scrambled_8bit.tflite` (29 KB, 6/15 partial placement) refuses on
`E1M-AEN801` too. `_refuse_zero_sram_footprint` therefore names the profile the
run ITSELF reported (`_parse_vela_profile`), never a hardcoded Alif one -- an
NXP user must not read an error blaming `Ethos_U85_SYS_DRAM_Mid`.

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
accelerator config rather than of the vendor. Note the Shared_Sram line above
is NOT restated as advice: `compile()` still passes no `--memory-mode` and has
no way to, and prescribing a flag tan cannot pass is the defect review BLOCKER
2 removed (see `_refusal_remedy`)."""
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
    EVERY memory area), and refused by the caller for anything else."""
    sram_kib = used.get("sram", 0.0)
    if sram_kib <= 0:
        return 0, 0
    return round(sram_kib * 1024), math.ceil(sram_kib)


def _profile_clause(system_config: str | None, memory_mode: str | None,
                    defaulted: bool) -> str:
    """The profile THIS run resolved, named as the run itself reported it
    (`_parse_vela_profile`) -- never a hardcoded `Ethos_U85_*`, which would
    blame an Alif memory model for an `ethos-u65-256` refusal on the NXP
    E1M-NX9101 (tan-cli#789 review MAJOR 3).

    @defaulted is vela's OWN verdict (`_DEFAULTED_RE` over its stdout), not an
    assumption: the "built-in default" clause is only appended when vela
    actually said it fell back, so the sentence stays true if a profile is
    ever supplied."""
    named = " / ".join(p for p in (system_config, memory_mode) if p) or "an unreported profile"
    return f"{named}, vela's BUILT-IN default profile" if defaulted else named


def _refusal_remedy(defaulted: bool, silicon_ref: str | None) -> str:
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
    says a profile was not supplied and cannot be passed, which is a fact
    about tan, not about anyone's silicon."""
    if not defaulted:
        return "`tan model build` skips this target and still builds the SKU's others."
    where = ""
    if silicon_ref and silicon_ref.startswith(_ALIF_ENSEMBLE_REF_PREFIX):
        where = ("; on this Alif Ensemble part it lives in the proprietary "
                 "ensemble_vela.ini alp-sdk does not redistribute")
    return (f"No module vela profile was supplied and tan cannot pass one yet{where}. "
            f"`tan model build` skips this target and still builds the SKU's others.")


def _refuse_zero_sram_footprint(*, accel_config: str, npu_ops: int, cpu_ops: int | None,
                                used: dict[str, float], system_config: str | None,
                                memory_mode: str | None, defaulted: bool,
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


def _default_profile_caveats(defaulted: bool, system_config: str | None,
                             memory_mode: str | None) -> tuple[str, ...]:
    """vela's own "may be invalid or non-optimal" verdict, surfaced as a report
    caveat when it fell back to its built-in profile -- so a customer cannot
    mistake a default-profile compile for one authored for this module. `()`
    when a real profile was supplied. See the module docstring for why no
    profile is invented here instead.

    Takes @defaulted rather than re-searching stdout: `compile()` resolves it
    once (`_DEFAULTED_RE`) and hands the SAME fact to this and to
    `_refuse_zero_sram_footprint`, so the caveat and the refusal can never
    disagree about whether vela defaulted."""
    if not defaulted:
        return ()
    named = ", ".join(f"{label} {name}" for label, name in
                      (("system-config", system_config), ("memory-mode", memory_mode)) if name)
    return (f"vela used its BUILT-IN default profile ({named or 'unreported'}), not one "
            f"authored for this module -- vela's own warning for that is \"Compilation "
            f"may be invalid or non-optimal\". The arena/SRAM figures and the compiled "
            f"command stream describe that default memory model, not this module's.",)


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
                opts: dict | None = None, silicon_ref: str | None = None) -> Blob:
        # @silicon_ref reaches ONLY the refusal wording (`_refusal_remedy`) --
        # the vela command line below is byte-identical with and without it,
        # so no vendor can ever get a different artifact out of this adapter.
        run_dir = _run_dir(out_dir, accel_config)
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["vela", str(source), "--accelerator-config", accel_config,
               "--output-dir", str(run_dir)]
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
        defaulted = _DEFAULTED_RE.search(proc.stdout) is not None
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
