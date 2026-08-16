# SPDX-License-Identifier: Apache-2.0
"""Compiler-adapter interface: one adapter per backend toolchain."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Blob:
    """One compiled artifact + the manifest metadata the writer needs."""
    format: str                 # one of manifest.VALID_BLOB_FORMATS
                                 # (vela_tflite | tflite | drpai_dir | dxnn | onnx)
    payload: bytes
    arena_bytes: int = 0
    compiler_version: str = ""
    req_sram_kib: int = 0
    # Real per-operator NPU-vs-CPU placement, as the compiler itself reports
    # it -- today only `VelaAdapter` populates these (parsed from vela's own
    # "CPU/NPU operators = N (P%)" summary line, `ethos_u._parse_vela_
    # placement`). None for every other adapter, and None when the compiler
    # ran but didn't print a placement summary this could parse: a compiler
    # exiting 0 is NOT proof every op landed on the NPU (vela exits 0 on a
    # full CPU fallback by design), so a caller that wants "did this compile
    # actually place anything on the accelerator" must read these, never
    # infer it from the mere absence of an exception.
    npu_op_count: int | None = None
    cpu_op_count: int | None = None
    # Adapter-authored caveats about THIS compile that a report must not drop
    # -- each already a complete, customer-readable sentence. Today only
    # `VelaAdapter` populates them, for the profile flags vela had to default:
    # `--memory-mode` now comes from the SoC spec (alp-sdk #1470), but
    # `--system-config` still does not, because the SoM-authoritative names
    # live only in a proprietary .ini alp-sdk does not redistribute. Surfaced
    # rather than swallowed so a compile tuned to vela's own bandwidth model
    # -- or, under a memory mode whose const area sits on the arena's OTHER
    # AXI port (`Shared_Sram`), one whose WEIGHTS vela's default system config
    # placed as well, or, when no memory mode was resolvable at all, one sent
    # to vela's DRAM-backed default on a module that has no DRAM -- cannot be
    # mistaken for a module-authoritative one; `tan.model.check`'s
    # `_report_from_vela_compile` carries them into `BackendReport.notes` and
    # so into the JSON envelope. Never a substitute for failing: a figure that
    # cannot be derived is refused (`ethos_u._refuse_zero_sram_footprint`),
    # not caveated.
    caveats: tuple[str, ...] = ()


class CompilerAdapter(ABC):
    backend: str                # cpu | ethos_u | drpai | deepx_dxm1
    # True for backends that need a per-model compile config the SDK can't
    # derive (DEEPX JSON+calibration, DRP-AI spec). build_model records a
    # "no compile config" coverage skip for these when no opts block is given.
    requires_compile_opts: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """True if this backend's compiler is installed/usable on this host."""

    @abstractmethod
    def accepts(self, src_format: str) -> bool:
        """True if this adapter can consume the given source format (onnx|tflite)."""

    @abstractmethod
    def compile(self, source: Path, *, accel_config: str, out_dir: Path,
                opts: dict | None = None, silicon_ref: str | None = None,
                vela_memory_mode: str | None = None,
                vela_system_config: str | None = None) -> Blob:
        """Compile @source for @accel_config; return the Blob.

        @opts is the per-model compile config for this backend
        (board.yaml `models[].compile.<backend>`), with any path values already
        resolved to absolute paths by the caller; None when the backend needs
        no per-model config (cpu, ethos_u).

        @silicon_ref is the `<vendor>:<family>:<part>` ref of the silicon this
        target runs on -- `TargetSpec.silicon_ref`, straight out of the SoM
        preset's `silicon:` (`alif:ensemble:e8`, `nxp:imx9:imx93`, ...) -- for
        an adapter whose DIAGNOSTICS are vendor-specific even though its
        compile is not. Today only `VelaAdapter` reads it, to name Alif's
        proprietary `ensemble_vela.ini` in a footprint refusal on an Alif
        Ensemble part and NOT on the NXP i.MX 93 (tan-cli#789 review (g)).
        `None` means the caller could not resolve one; an adapter must then
        stay vendor-neutral, never guess a vendor from an accelerator config
        or a compiler-reported profile name. It is NOT a compile input: no
        adapter may change the artifact it emits based on this.

        @vela_memory_mode / @vela_system_config ARE compile inputs, and the
        only backend-specific pair on this interface: `TargetSpec.vela_*`, the
        silicon's own vela invocation profile out of the SoC spec's
        `npu_toolchain.vela` (alp-sdk #1470). Only `VelaAdapter` acts on them
        -- every other adapter accepts and ignores them, exactly as `cpu` and
        `ethos_u` accept and ignore `opts` -- and they are named for vela on
        purpose, so a future DRP-AI or DEEPX profile cannot be mistaken for
        being carried by them. `None` means the caller resolved no profile,
        and an adapter must then invoke its compiler exactly as it did before
        the profile existed rather than substituting a default of its own: a
        guessed memory model compiles a command stream for hardware the module
        does not have."""
