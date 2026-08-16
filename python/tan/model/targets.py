# SPDX-License-Identifier: Apache-2.0
"""Derive .alpmodel compile targets from a SoM SKU (silicon-determined).

Targets come from the host SoC's npus[] *and* from any on-module discrete
accelerator (e.g. the DEEPX DX-M1 on V2M SoMs). A discrete accelerator is any
*other* SoC JSON whose variants[].alp_module_skus lists this SKU -- so the SoC
JSON's alp_module_skus stays the single source of truth (no hardcoded
backend->silicon map)."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

from tan.soc_ref import resolve_soc_path


@dataclass(frozen=True)
class TargetSpec:
    backend: str            # cpu | ethos_u | drpai | deepx_dxm1
    silicon_ref: str        # SoC ref e.g. "alif:ensemble:e7" | "deepx:dx:m1" | "*"
    accel_config: str       # vela accel-config e.g. "ethos-u55-256"; "" when N/A
    # The SILICON's vela invocation profile, from the SoC spec's
    # `npu_toolchain.vela` block (alp-sdk #1470) -- ethos_u targets only, None
    # everywhere else. Optional with None defaults so no existing TargetSpec
    # construction site has to change, and so a SoC spec that predates the
    # block resolves to "pass no profile flag", which is exactly today's
    # behaviour rather than a guess.
    vela_memory_mode: str | None = None      # Arm built-in, e.g. "Sram_Only"
    vela_system_config: str | None = None    # ONLY when it needs no vendor config
    # The other half of that block: the vendor-gated System_Config and the file
    # that defines it. Carried SEPARATELY from `vela_system_config` -- which
    # stays "safe to put on a command line alone" by construction -- because
    # these two are legal only TOGETHER and only alongside a `--config` the
    # customer supplies through `ALP_VELA_CONFIG`
    # (`tan.model.adapters.ethos_u`). None for every part shipped today: no SoC
    # spec names a `system_config` at all.
    vela_vendor_system_config: str | None = None
    # The vendor `.ini`'s FILENAME as metadata declares it
    # (`vendor_config_filename`, e.g. `ensemble_vela.ini`) -- a DIAGNOSTIC
    # fact, never a path: `tan.model.adapters.ethos_u`'s footprint refusal
    # names it to explain why tan cannot resolve a complete profile for this
    # part. A part whose metadata declares none gets no file named, so no
    # customer is ever sent after another vendor's download.
    vela_vendor_config_filename: str | None = None
    # Does this SoC declare ANY external DRAM interface
    # (`external_memory_interfaces[].kind`)? `False` is the machine-checkable
    # evidence behind the zero-SRAM refusal: vela's default profiles are
    # DRAM-backed, and a working set placed in DRAM on a part that declares no
    # DRAM interface describes memory the module does not have. `None` means
    # the spec says nothing, and an unknown must never be reported as a "no"
    # -- see `_soc_declares_dram`.
    soc_declares_dram: bool | None = None
    # Which CPU core drives this accelerator, from the SoC spec's own
    # `npus[].paired_core` -- `m55_hp` for the E8's high-perf Ethos-U55,
    # `m55_he` for its high-efficiency one. `None` where the spec declares no
    # pairing (the E8's Ethos-U85 today), and that `None` is a real answer, not
    # a gap to fill in: the metadata does not know which core drives it, so
    # nothing downstream may infer one. A bench-measured perf point's identity
    # includes the core (alp-sdk `f724d3e4`), and this is the only sourced
    # answer tan has for it.
    paired_core: str | None = None


class _VelaProfile(NamedTuple):
    """What one SoC spec's `npu_toolchain.vela` block resolves to, split by
    what is SAFE TO PASS: `system_config` is an Arm built-in that vela accepts
    on its own, `vendor_system_config` is a name that exists only inside
    `vendor_config_filename` and is rc=1 without it."""
    memory_mode: str | None
    system_config: str | None
    vendor_system_config: str | None
    vendor_config_filename: str | None


#: Substrings that make an `external_memory_interfaces[].kind` a DRAM
#: interface, matched case-insensitively against the declared kind. Covers
#: every DRAM spelling in metadata today -- `LPDDR4/4X` (nxp:imx9:imx93,
#: renesas:rzv2n:n44), `LPDDR4X / LPDDR5` (deepx:dx:m1) -- and nothing else:
#: `HexSPI`, `OctalSPI`, `FlexSPI`, `xSPI` and `SD/eMMC` (the ONLY kinds the
#: six Alif Ensemble parts declare) are not DRAM.
#:
#: Matched on `kind` alone and NOT on the interface's `supports` list, which
#: is where the Alif parts carry `HyperBus`. A HyperBus port can host HyperRAM,
#: but that is a populated-device question this spec does not answer, and vela's
#: `Dram` memory area is not it -- reading `supports` here would turn an
#: attachable-bus fact into a "this part has DRAM" claim nothing sourced.
_DRAM_INTERFACE_TOKENS = ("DDR", "DRAM")


def _soc_declares_dram(soc: dict) -> bool | None:
    """Does this SoC spec declare an external DRAM interface? `None` when it
    declares no `external_memory_interfaces` at all.

    THE `None` IS THE POINT, and it fails closed. The consumer
    (`tan.model.adapters.ethos_u._refuse_zero_sram_footprint`) states "this SoC
    declares no DRAM interface" as EVIDENCE in a refusal a customer sizes
    hardware from, so it may only ever say that on an explicit `False`. A spec
    that carries no interface list has not declared the absence of DRAM; it has
    said nothing, and saying nothing back is the only honest answer. Same
    reading for an EMPTY list, which is a stub far more often than a positive
    statement that a part has no external memory at all."""
    interfaces = soc.get("external_memory_interfaces")
    if not isinstance(interfaces, list) or not interfaces:
        return None
    for iface in interfaces:
        kind = str(iface.get("kind", "")) if isinstance(iface, dict) else ""
        if any(token in kind.upper() for token in _DRAM_INTERFACE_TOKENS):
            return True
    return False


def _vela_profile(soc: dict) -> _VelaProfile:
    """How to invoke vela for THIS SoC, read from its `npu_toolchain.vela`
    block. All-None for a spec that carries no block -- never a guessed
    profile; a wrong one compiles a command stream for memory the module does
    not have.

    THE SYSTEM_CONFIG GUARD IS LOAD-BEARING. `--memory-mode` values are Arm
    built-ins (the SoC schema constrains `memory_mode` to the `[Memory_Mode.*]`
    sections `ethos-u-vela` 5.1.0 ships), so passing one always works. A
    `System_Config` name usually is NOT: the Alif Ensemble parts' own
    `Ethos_U85_SRAM_Only` / `RTSS_HE_SRAM_Only` live only in the proprietary
    `ensemble_vela.ini`, and handing vela a section it cannot resolve is a hard
    rc=1, measured verbatim:

        ethosu.vela.errors.CliOptionError: 'Error: Incorrect argument to CLI
        option --system-config=Ethos_U85_SRAM_Only: Section
        System_Config.Ethos_U85_SRAM_Only not found in Vela config file'

    So a `system_config` is carried ONLY when the block declares
    `system_config_requires_vendor_config` EXPLICITLY FALSE. It fails CLOSED,
    not open: an ABSENT key withholds the name, exactly as `true` does. The
    defence for reading it as falsy was that alp-sdk's SoC schema makes the key
    `required` -- true today, but enforced in the OTHER repo, against a
    checkout tan binds at an arbitrary version (`ALP_SDK_ROOT`, any tag or
    branch a user has on disk). That is the same cross-repo drift class
    `tests/gates/test_planner_relocation_freshness.py` exists for, and here the
    failure mode is not a stale hash: an unannotated `system_config` would walk
    straight onto vela's command line and turn a build into a hard rc=1. `is
    False` is the only reading under which a spec that never made the promise
    cannot make it by omission.

    An ABSENT `system_config` is likewise "nothing to pass", never "no vendor
    config needed": no Alif or NXP spec names one today (an Alif System_Config
    describes one CORE SUBSYSTEM, not a die), so this branch yields None for
    every part currently shipped -- correct, and the mechanism is here for the
    spec that eventually does carry a built-in one.

    A vendor-gated name is WITHHELD FROM `system_config` AND CARRIED ON
    `vendor_system_config`, rather than dropped. The two fields are exactly
    "safe alone" versus "legal only with its `.ini`": the adapter may put the
    first on a command line unconditionally, and the second only alongside a
    `--config` pointing at a file the customer supplied through
    `ALP_VELA_CONFIG`. Splitting them here rather than passing one name plus a
    boolean keeps the fail-closed guard above the only place that decides which
    is which: an adapter cannot accidentally pass a vendor name by reading a
    flag the wrong way round, because it is never handed one in the field it
    passes freely."""
    block = soc.get("npu_toolchain")
    vela = block.get("vela") if isinstance(block, dict) else None
    if not isinstance(vela, dict):
        return _VelaProfile(None, None, None, None)
    memory_mode = vela.get("memory_mode") or None
    system_config = vela.get("system_config") or None
    if vela.get("system_config_requires_vendor_config") is not False:
        # The filename rides ONLY on this branch. A spec that declares it needs
        # no vendor config has no vendor file to name, and a contradictory spec
        # (`false` plus a filename) must not have that filename quoted at a
        # customer as something they need -- the refusal clause it feeds calls
        # the file proprietary and undistributed, which would then be advice
        # about a download nobody has to make.
        return _VelaProfile(memory_mode, None, system_config,
                            vela.get("vendor_config_filename") or None)
    return _VelaProfile(memory_mode, system_config, None, None)


def _npu_backend(npu_type: str, subtype: str) -> str | None:
    if npu_type.startswith("ethos-u"):
        return "ethos_u"
    if "drp" in npu_type or "drp" in subtype:   # renesas DRP-AI ("drp-ai" / "ai-mac+drp")
        return "drpai"
    if npu_type.startswith("dx") or "deepx" in npu_type:
        return "deepx_dxm1"
    return None


def _soc_targets(soc: dict, silicon_ref: str) -> list[TargetSpec]:
    """One TargetSpec per mappable NPU in a SoC's npus[] (deduped by the caller)."""
    out: list[TargetSpec] = []
    profile = _vela_profile(soc)
    declares_dram = _soc_declares_dram(soc)
    for npu in soc.get("npus", []):
        npu_type = npu.get("type", "")
        backend = _npu_backend(npu_type, npu.get("subtype", ""))
        if backend is None:
            continue
        ethos_u = backend == "ethos_u"
        accel = f"{npu_type}-{npu['mac_per_cycle']}" if ethos_u else ""
        # The profile is a vela flag, so it rides only on the targets vela
        # compiles: a DRP-AI or DEEPX target on the same die must not carry
        # (and its adapter must never be handed) an Ethos-U memory mode. The
        # DRAM fact is NOT a vela flag -- it is a property of the silicon this
        # target runs on, like `silicon_ref` -- so it rides on every target the
        # spec produces, whether or not any adapter reads it today.
        out.append(TargetSpec(backend=backend, silicon_ref=silicon_ref, accel_config=accel,
                              vela_memory_mode=profile.memory_mode if ethos_u else None,
                              vela_system_config=profile.system_config if ethos_u else None,
                              vela_vendor_system_config=(
                                  profile.vendor_system_config if ethos_u else None),
                              vela_vendor_config_filename=(
                                  profile.vendor_config_filename if ethos_u else None),
                              soc_declares_dram=declares_dram,
                              paired_core=npu.get("paired_core") or None))
    return out


def _discrete_socs(sku: str, host_ref: str, metadata_root: Path) -> list[tuple[str, dict]]:
    """SoCs (other than the host) whose variants[].alp_module_skus list this SKU --
    i.e. on-module discrete accelerators wired to the host (DEEPX DX-M1 on V2M)."""
    found: list[tuple[str, dict]] = []
    for path in sorted((metadata_root / "socs").glob("**/*.json")):
        soc = json.loads(path.read_text(encoding="utf-8"))
        ref = soc.get("ref")
        if not ref or ref == host_ref:
            continue
        skus = {s for v in soc.get("variants", []) for s in v.get("alp_module_skus", [])}
        if sku in skus:
            found.append((ref, soc))
    return found


def resolve_targets(sku: str, *, metadata_root: Path) -> list[TargetSpec]:
    preset_path = metadata_root / "e1m_modules" / f"{sku}.yaml"
    if not preset_path.is_file():
        raise FileNotFoundError(f"no SoM preset for SKU {sku} at {preset_path}")
    preset = yaml.safe_load(preset_path.read_text(encoding="utf-8"))

    silicon = preset["silicon"]                                 # host SoC, e.g. "alif:ensemble:e7"
    # resolve_soc_path() returns None where the old inline 3-tuple unpack
    # raised ValueError, so re-raise it here: callers distinguish a
    # malformed ref (ValueError) from a well-formed ref naming a spec that
    # isn't on disk (FileNotFoundError, below), and collapsing the two
    # would be a behaviour change, not a refactor (#1096).
    soc_path = resolve_soc_path(silicon, metadata_root)
    if soc_path is None:
        raise ValueError(
            f"malformed silicon ref {silicon!r} in {preset_path}: expected "
            f"exactly 3 colon-separated parts (<vendor>:<family>:<part>)"
        )
    if not soc_path.is_file():
        raise FileNotFoundError(f"no SoC spec for {silicon} at {soc_path}")
    host_soc = json.loads(soc_path.read_text(encoding="utf-8"))

    specs: list[TargetSpec] = []
    seen: set[tuple[str, str]] = set()

    def _add(spec: TargetSpec) -> None:
        key = (spec.backend, spec.accel_config)
        if key not in seen:
            seen.add(key)
            specs.append(spec)

    for spec in _soc_targets(host_soc, silicon):                   # host SoC NPUs
        _add(spec)
    for ref, dsoc in _discrete_socs(sku, silicon, metadata_root):  # on-module discrete NPUs
        for spec in _soc_targets(dsoc, ref):
            _add(spec)
    _add(TargetSpec(backend="cpu", silicon_ref="*", accel_config=""))  # CPU always present
    return specs
