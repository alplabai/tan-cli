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
    them shipped, and the same held for `E1M-AEN401`, `E1M-AEN601` and (at
    `ethos-u65-256`) `E1M-NX9101`.
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

from .adapters import Blob, CompilerAdapter
from .adapters.cpu import CpuAdapter
from .adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from .adapters.drpai import DrpaiAdapter
from .adapters.deepx import DeepxAdapter
from .adapters.executorch import ExecutorchAdapter
from .manifest import Manifest, Target, Coverage
from .package import write_package
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
    `ethos-u55-256`, `ethos-u55-128`), and `E1M-NX9101` one
    (`ethos-u65-256`). Dropped to a coverage skip instead -- legibly absent,
    with the reason, rather than silently present as a zero (tan-cli#789
    review MINOR 7).

    `cpu` is exempt: a CPU target legitimately places nothing on an
    accelerator, and its 0 arena is a real figure, not a missing one.
    `npu_op_count is None` (every adapter but vela today, and vela itself when
    its placement summary could not be parsed) is exempt too -- unknown is not
    zero, and inventing a verdict from an unreadable summary is exactly what
    `_parse_vela_placement` returning None exists to prevent."""
    return backend != "cpu" and blob.npu_op_count == 0


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
                compile_opts: dict[str, dict] | None = None) -> Path:
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
            blob = adapter.compile(source, accel_config=spec.accel_config,
                                   out_dir=out_dir, opts=backend_opts)
        except VelaFootprintRefused as err:
            # ONE target's refusal, not the package's. See the module docstring.
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped", str(err)))
            continue
        if _placed_nothing_on_accelerator(spec.backend, blob):
            coverage.append(Coverage(spec.backend, spec.accel_config, "skipped",
                                     _no_placement_reason(spec, blob)))
            continue
        targets.append(Target(
            backend=spec.backend, silicon_ref=spec.silicon_ref,
            blob_format=blob.format, accel_config=spec.accel_config,
            arena=blob.arena_bytes,
            requires={"sram_kib": blob.req_sram_kib, "op_features": []},
            blob=len(blobs), compiler_version=blob.compiler_version))
        blobs.append(blob.payload)

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
    out_path.write_bytes(write_package(mft, blobs))
    return out_path
