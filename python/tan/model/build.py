# SPDX-License-Identifier: Apache-2.0
"""Build driver: SKU + source model -> .alpmodel package (compile-what's-available).

Resolves the SoM's targets, runs each *available* compiler adapter, and assembles
the package. A backend whose adapter is missing, or whose tool is not installed,
is recorded as a `coverage` skip; a source format no adapter accepts is
`incompatible`. If *no* blob is produced the build fails loudly -- an .alpmodel
with zero runnable blobs is broken.

PER-TARGET, NOT PER-PACKAGE (tan-cli#789 review BLOCKER 1). A SKU resolves to
several targets and they succeed or fail independently, so two of them are
scoped to ONE target's coverage entry instead of killing the package:

  * `VelaFootprintRefused` -- vela compiled cleanly and tan refused the
    footprint it reported (see `tan.model.adapters.ethos_u`). Measured, real
    `ethos-u-vela` 5.1.0 over `tests/fixtures/models/tiny_int8.tflite`: with no
    guard here, `E1M-AEN801`'s `ethos-u85-256` refusal aborted the whole build
    -- `ethos-u55-256`, `ethos-u55-128` and `cpu` all compiled fine and NONE of
    them shipped, and the same held for `E1M-AEN401` and `E1M-AEN601`.
  * A ZERO-placement accelerator compile -- see `_placed_nothing_on_accelerator`.

Every OTHER `adapter.compile()` exception still fails the whole build loudly.
That is deliberate: a toolchain that crashed, timed out or produced no artifact
is a broken build, not a coverage line (`tests/model/test_build.py` states the
same contract from the other side).

If EVERY target ends up skipped there is no blob, and the existing zero-blob
guard at the bottom raises with the full coverage detail -- each refusal named.
A package with no runnable blob is never written: that is worse than an error,
because the customer only finds out on the device."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path

from ..core.atomic_write import atomic_write_bytes
from .adapters import Blob, CompilerAdapter
from .adapters.cpu import CpuAdapter
from .adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from .adapters.drpai import DrpaiAdapter
from .adapters.deepx import DeepxAdapter
from .adapters.executorch import ExecutorchAdapter
from .manifest import Manifest, Target, Coverage
from .package import write_package
from .sram_fit import SramFit, evaluate_sram_fit
from .targets import TargetSpec, resolve_targets
from .tensorio import extract_io

# Default adapter registry. Each is detect-and-skip (is_available() False when
# its tool is absent); vela (ethos_u) skips on hosts without the ethos-u-vela package.
# A backend may carry more than one adapter (cpu: CpuAdapter for .tflite,
# ExecutorchAdapter for .pte) -- see the by_backend grouping below, which
# selects among a backend's adapters by accepts(src_fmt), not by last-one-wins.
_ADAPTERS: list[CompilerAdapter] = [
    CpuAdapter(), VelaAdapter(), DrpaiAdapter(), DeepxAdapter(), ExecutorchAdapter(),
]

# #1125: mirrors metadata/schemas/board.schema.json's `models[].name` pattern.
# build_model() is called directly by non-CLI callers (tests, future tooling),
# not just alp_cli.model's schema-validated path -- an allowlist here is the
# root-cause guard, independent of whether the caller validated board.yaml.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _src_format(source: Path) -> str:
    return source.suffix.lstrip(".").lower()        # "tflite" | "onnx"


def _placed_nothing_on_accelerator(backend: str, blob: Blob) -> bool:
    """True for an accelerator compile whose own compiler reports it placed
    ZERO operators on the accelerator.

    Such a target is not an accelerator target. It carries `arena=0` and
    `requires={'sram_kib': 0}` -- the exact "fits any envelope" shape
    alp-sdk's on-device selector reads unconditionally
    (`return e->arena_sram_kib == 0u || t->req_sram_kib <= e->arena_sram_kib;`,
    src/backends/inference/alp_model_select.c) -- so the board would happily
    select an `ethos_u` blob that runs every operator on the CPU anyway, while
    the honest `cpu` target sits beside it in the same package. Measured with
    real `ethos-u-vela` 5.1.0 on `tests/fixtures/models/float32_fc.tflite`:
    `E1M-AEN801` shipped THREE such `ethos_u` targets (`ethos-u85-256`,
    `ethos-u55-256`, `ethos-u55-128`). Dropped to a coverage skip instead -- legibly absent,
    with the reason, rather than silently present as a zero (tan-cli#789
    review MINOR 7).

    `cpu` is exempt: a CPU target legitimately places nothing on an
    accelerator, and its 0 arena is a real figure, not a missing one.
    `npu_op_count is None` (every adapter but vela today, and vela itself when
    its placement summary could not be parsed) is exempt too -- unknown is not
    zero, and inventing a verdict from an unreadable summary is exactly what
    `_parse_vela_placement` returning None exists to prevent."""
    return backend != "cpu" and blob.npu_op_count == 0


class SramNoFitRefused(Exception):
    """A certain arena or SRAM0-residency NO-FIT under `Sram_Only`
    (tan-cli#1288, `tan.model.sram_fit`) on EVERY ethos_u target the build
    reached -- refuses the WHOLE per-model build (a single target's no-fit
    is a coverage skip, like `VelaFootprintRefused`; tan-cli#1486): every
    ethos_u variant this SKU would ship describes a placement that cannot
    execute on it (`src/backends/inference/ethos_u_aen.cpp` pins every NPU
    access to the SRAM AXI port under `Sram_Only`), so no partial package is
    written -- `_run_build` (`tan.commands.model_cmd`) catches this ahead of
    the generic per-model except-clause and reports `model.sram-no-fit` at
    `ExitCode.VALIDATION_FAILURE`, not the generic `model.build-failed`."""


def _sram_no_fit_message(spec: TargetSpec, fit: SramFit) -> str:
    parts = []
    if fit.arena.verdict == "no-fit":
        parts.append(f"arena needs {fit.arena.needed_kib} KiB, only "
                     f"{fit.arena.limit_kib} KiB available "
                     f"(cores.<id>.inference.default_arena_kib)")
    if fit.sram0.verdict == "no-fit":
        # NOT `fit.arena.limit_kib` (tan-cli#1288 review round 2, finding 4):
        # the arena figure `sram0` actually summed against is whichever
        # `resolve_arena_budget` shape decided it -- the SAME core's budget
        # only when `arena.kind == "single"`; `sram0` uses the SMALLEST of a
        # `"range"`'s several candidates (`arena.limit_kib` there is the
        # LARGEST, a different number), and a `MIN_ARENA_KIB` lower bound
        # when arena itself is unresolved (`arena.limit_kib` is `None`
        # there -- this would have literally printed "arena None KiB").
        # `sram0.needed_kib` is ALWAYS `blob_kib + <that figure>` by
        # construction (`tan.model.sram_fit._evaluate_sram0_*`), so deriving
        # it back out this way is correct regardless of which shape produced
        # the verdict -- reading `fit.arena.limit_kib` instead is not.
        arena_used = fit.sram0.needed_kib - fit.blob_kib
        parts.append(f"SRAM0 needs {fit.sram0.needed_kib} KiB (blob "
                     f"{fit.blob_kib} KiB + arena {arena_used} KiB), "
                     f"only {fit.sram0.limit_kib} KiB available")
    detail = "; ".join(parts)
    return (f"{spec.accel_config or spec.backend}: {detail} -- refusing to ship a "
            f"Sram_Only target whose weights/arena cannot be SRAM0-resident "
            f"(src/backends/inference/ethos_u_aen.cpp pins every NPU access "
            f"to the SRAM AXI port). No package written for this model.")


def _no_placement_reason(spec: TargetSpec, blob: Blob) -> str:
    """The coverage reason for a dropped zero-placement accelerator target --
    the compiler's own verdict, the target it applies to, and what the package
    still offers instead. One line, same as every other coverage reason."""
    total = (blob.npu_op_count or 0) + (blob.cpu_op_count or 0)
    tool = blob.compiler_version or spec.backend
    # "accepts ... against ANY arena size", not the retired "fits" vocabulary --
    # same reason `ethos_u._refuse_zero_sram_footprint` words it that way.
    return (f"{tool} placed 0 of {total} operators on {spec.accel_config or spec.backend}; "
            f"an accelerator target with no accelerator placement would ship arena 0 / "
            f"sram_kib 0, which alp-sdk's on-device selector accepts against ANY arena "
            f"size (src/backends/inference/alp_model_select.c). The cpu target runs this "
            f"model.")


def build_model(*, sku: str, name: str, source: Path, out_dir: Path,
                metadata_root: Path,
                adapters: list[CompilerAdapter] | None = None,
                compile_opts: dict[str, dict] | None = None,
                board_doc: dict | None = None) -> Path:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid model name {name!r}: must match {_NAME_RE.pattern!r}")
    registry = list(_ADAPTERS if adapters is None else adapters)
    by_backend: dict[str, list[CompilerAdapter]] = {}
    for a in registry:
        by_backend.setdefault(a.backend, []).append(a)
    specs = resolve_targets(sku, metadata_root=metadata_root)
    src_fmt = _src_format(source)
    opts_by_backend = compile_opts or {}

    out_dir.mkdir(parents=True, exist_ok=True)
    targets: list[Target] = []
    coverage: list[Coverage] = []
    blobs: list[bytes] = []
    no_fit_msgs: list[str] = []
    ethos_u_fit = 0
    for spec in specs:
        candidates = by_backend.get(spec.backend, [])
        if not candidates:
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped",
                                     f"no compiler adapter for {spec.backend}"))
            continue
        if len(candidates) > 1:
            # A backend with more than one adapter (cpu: CpuAdapter + ExecutorchAdapter)
            # is disambiguated by source format up front -- accepts() decides identity,
            # not registration order. A single-adapter backend keeps the original order
            # below (requires_compile_opts / is_available reported before "incompatible"),
            # so an unrelated format mismatch doesn't mask a "no compile config" skip.
            adapter = next((a for a in candidates if a.accepts(src_fmt)), None)
            if adapter is None:
                coverage.append(Coverage(spec.backend, spec.accel_config, "incompatible",
                                         f"{spec.backend} does not accept .{src_fmt}"))
                continue
        else:
            adapter = candidates[0]
        backend_opts = opts_by_backend.get(spec.backend)
        if adapter.requires_compile_opts and not backend_opts:
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped",
                                     f"no compile config for {spec.backend} "
                                     f"(add models[].compile.{spec.backend} to board.yaml)"))
            continue
        if not adapter.is_available():
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped",
                                     f"{spec.backend} compiler not installed"))
            continue
        if not adapter.accepts(src_fmt):
            coverage.append(Coverage(spec.backend, spec.accel_config, "incompatible",
                                     f"{spec.backend} does not accept .{src_fmt}"))
            continue
        try:
            # `spec.vela_vendor_config_filename` / `spec.soc_declares_dram` are
            # passed for the adapter's DIAGNOSTICS, not its output -- see
            # `CompilerAdapter.compile`. They are what let a vela footprint
            # refusal name the proprietary profile file a part's own SoC spec
            # declares -- and stay silent for one that declares none
            # (tan-cli#789 review (g)) -- and say that a DRAM
            # placement went to memory this part declares no interface to.
            #
            # `spec.vela_memory_mode` / `.vela_system_config` /
            # `.vela_vendor_system_config` are the opposite: the silicon's own
            # vela memory profile (SoC spec `npu_toolchain.vela`, alp-sdk
            # #1470), and it DOES change the artifact -- that is the point. It
            # is what makes an `ethos-u85-256` target ship at all: measured,
            # real `ethos-u-vela` 5.1.0 over `tests/fixtures/models/
            # tiny_int8.tflite` reported `sram_memory_used = 0.0` for
            # E1M-AEN801 without it (refused, a coverage row) and 0.03125 KiB
            # with `--memory-mode Sram_Only` (a real target). Passed on the
            # same call as everything else so a target can never be compiled
            # with one target's accel-config and another's memory model; every
            # vela field is None for a non-ethos_u backend by construction
            # (`targets._soc_targets`).
            blob = adapter.compile(source, accel_config=spec.accel_config,
                                   out_dir=out_dir, opts=backend_opts,
                                   vela_memory_mode=spec.vela_memory_mode,
                                   vela_system_config=spec.vela_system_config,
                                   vela_vendor_system_config=spec.vela_vendor_system_config,
                                   vela_vendor_config_filename=spec.vela_vendor_config_filename,
                                   soc_declares_dram=spec.soc_declares_dram)
        except VelaFootprintRefused as err:
            # ONE target's refusal, not the package's. See the module docstring.
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped", str(err)))
            continue
        if spec.backend == "ethos_u":
            # tan-cli#1288: a certain arena/SRAM0 no-fit under `Sram_Only`
            # skips THIS target as a coverage row; the model is refused
            # (see `SramNoFitRefused`) only when no ethos_u target ships
            # (tan-cli#1486) -- a no-op for every other memory mode
            # (`evaluate_sram_fit` itself gates on it).
            fit = evaluate_sram_fit(
                memory_mode=spec.vela_memory_mode, req_sram_kib=blob.req_sram_kib,
                blob_len_bytes=len(blob.payload), board_doc=board_doc,
                paired_core=spec.paired_core, sku=sku, metadata_root=metadata_root,
            )
            if fit.no_fit:
                # ONE target's no-fit skips that target only; the model is
                # refused as a whole (below) only when NO ethos_u target
                # survives (tan-cli#1486).
                msg = _sram_no_fit_message(spec, fit)
                no_fit_msgs.append(msg)
                coverage.append(Coverage(spec.backend, spec.accel_config, "skipped", msg))
                continue
        if _placed_nothing_on_accelerator(spec.backend, blob):
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped",
                                     _no_placement_reason(spec, blob)))
            continue
        if spec.backend == "ethos_u":
            ethos_u_fit += 1       # fits AND ships; a fit that places nothing does not count
        targets.append(Target(
            backend=spec.backend, silicon_ref=spec.silicon_ref,
            blob_format=blob.format, accel_config=spec.accel_config,
            arena=blob.arena_bytes,
            requires={"sram_kib": blob.req_sram_kib, "op_features": []},
            blob=len(blobs), compiler_version=blob.compiler_version,
            # The compiler's unresolved caveats travel WITH the blob into the
            # package. Dropping them here was the gap: `tan model check
            # --exact` reported them and shipped nothing, `tan model build`
            # shipped bytes and reported nothing, so a blob compiled against
            # vela's BUILT-IN default memory model could reach a board with
            # the package silent about it -- while `arena`/`sram_kib` right
            # beside it, figures that describe THAT default memory model, are
            # what alp-sdk's on-device selector consumes
            # (src/backends/inference/alp_model_select.c).
            caveats=list(blob.caveats)))
        blobs.append(blob.payload)

    if no_fit_msgs and not ethos_u_fit:
        raise SramNoFitRefused("; ".join(no_fit_msgs))

    if not blobs:
        detail = "; ".join(f"{c.backend}:{c.status} ({c.reason})" for c in coverage)
        raise ValueError(f"no blob compiled for model '{name}' (.{src_fmt}); coverage: {detail}")

    src_bytes = source.read_bytes()          # read once: shared by the sha + tensor-I/O
    inputs, outputs = extract_io(source, raw=src_bytes)
    mft = Manifest(name=name, src_sha=hashlib.sha256(src_bytes).digest(),
                   inputs=inputs, outputs=outputs,
                   targets=targets, coverage=coverage)
    out_path = out_dir / f"{name}.alpmodel"
    # Belt-and-suspenders: the name allowlist above already makes escape
    # impossible for a bare filename, but fail closed on containment too --
    # a resolved-path check catches this even if the join expression above
    # ever grows a second path segment.
    resolved_out_dir = out_dir.resolve()
    if not out_path.resolve().is_relative_to(resolved_out_dir):
        raise ValueError(f"refusing to write outside out_dir: {out_path}")
    atomic_write_bytes(str(out_path), write_package(mft, blobs))
    return out_path
