#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""SoM/SoC metadata readers -- the memory map, the capability set, the SoC ref.

RELOCATED (was alp-sdk `scripts/alp_project_loader.py`). These are the last
functions `tan/planner/*` reached for across the repo boundary: `carveout.py`,
`partition.py` and `loader.py` want `resolve_memory_map`, `libraries.py` and
`kconfig.py` want `resolve_capabilities`, and `kconfig.py` wants
`silicon_to_kconfig` + `som_unpopulated_capabilities`. While they lived in
alp-sdk every one of those module-scope imports dragged `alp_project` ->
`alp_project_loader` -> `alp_orchestrate` back into the process, so the planner
emitted from `tan` but still LOADED the SDK's Python.

**They read metadata; they do not carry it.** Every path below resolves under
`.paths.METADATA_ROOT`, i.e. the bound alp-sdk checkout -- the SoC JSON, the
SoM preset, the silicon-Kconfig registry. `metadata/**` stays in alp-sdk
(ADR-0017 / I-26): what moved is the derivation, never the fact. A copy of any
of these tables inside `tan` would be a second source of truth.

Implementations are the SDK's, moved substantially as-is: their precedence
rules (a SoM `memory_map:` override beating the SoC variant, SoC-level
`memory_regions` beating the per-bank derivation, SoM capabilities beating SoC
defaults, then the per-SKU `unpopulated` restriction forcing 0/False) are
accumulated behaviour that `tests/parity/test_planner_emit_parity.py` pins byte
for byte -- not something to re-derive.

`resolve_soc_path` itself is defined in the leaf module `tan.soc_ref` (ADR-0028
Task 2 follow-up) and re-exported here, so `tan.model.targets` can use it
without pulling in this package's own import-time metadata reads.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

from tan.soc_ref import resolve_soc_path  # noqa: F401  (re-export: see below)

from .paths import METADATA_ROOT

# ---------------------------------------------------------------------
# SKU-family mapping
# ---------------------------------------------------------------------
#
# The SKU-prefix -> family-dir map is a small, pure derivation with no
# second on-disk source, so we keep it inline.  When a new SoM family
# lands, add the entry here + update the schema's `som.sku` pattern.
# (The silicon -> Kconfig mapping, by contrast, lives in the versioned
# registry below -- see silicon_to_kconfig().)

_SKU_FAMILY = re.compile(r"^E1M-(AEN|V2N|V2M|NX9)")


def _sku_family(sku: str) -> str:
    """Return the SoM family directory name for a SKU string."""
    m = _SKU_FAMILY.match(sku)
    if m is None:
        raise ValueError(f"unrecognised SoM SKU pattern: {sku}")
    return {"AEN": "aen", "V2N": "v2n", "V2M": "v2n-m1", "NX9": "imx93"}[m.group(1)]


# Silicon ref -> Zephyr SoC-select Kconfig symbol.
#
# This is NOT a hand-maintained table: the symbol is computed from the
# ref, and the single source of the allowlist is the versioned registry
# at metadata/registries/silicon-kconfig.json.
SILICON_KCONFIG_REGISTRY = METADATA_ROOT / "registries" / "silicon-kconfig.json"


@functools.lru_cache(maxsize=1)
def _load_silicon_kconfig() -> tuple[str, frozenset[str]]:
    """Load (socSymbolPrefix, knownSilicon) from the versioned registry."""
    data = json.loads(SILICON_KCONFIG_REGISTRY.read_text(encoding="utf-8"))
    return data["socSymbolPrefix"], frozenset(data["knownSilicon"])


def silicon_to_kconfig(silicon: str | None) -> str | None:
    """Return the Zephyr Kconfig symbol that selects *silicon*, or ``None``.

    The symbol is computed as ``socSymbolPrefix + ref.upper().replace(':','_')``;
    e.g. ``alif:ensemble:e7`` -> ``ALP_SOC_ALIF_ENSEMBLE_E7``.  ``None`` is
    returned for any ref not in the registry allowlist (so an accelerator
    such as ``deepx:dx:m1`` -- or an unknown ref -- emits no CONFIG line).
    """
    if silicon is None:
        return None
    prefix, known = _load_silicon_kconfig()
    if silicon not in known:
        return None
    return prefix + silicon.upper().replace(":", "_")


# resolve_soc_path() moved to the leaf module `tan.soc_ref` (imported above)
# so that `tan.model.targets` can use it without pulling in `tan.planner`'s
# import-time metadata reads. Re-exported here (rather than inlined) so this
# module's own four call sites, and every external `from
# tan.planner.som_metadata import resolve_soc_path`, keep working unchanged.


def _resolve_silicon_variant(
    sku_preset: dict[str, Any],
    metadata_root: Path,
) -> dict[str, Any] | None:
    """Resolve a SoM preset to its matching SoC-JSON variant entry.

    Two paths, in order of preference:

    1. Forward: the SoM preset's top-level `silicon_variant:` field
       names the exact `variants[].order_code` to pick out.
    2. Reverse fallback: scan the SoC JSON's `variants[]` for one
       whose `alp_module_skus` array contains the SoM SKU.

    Returns the matched variant dict (with keys order_code, package,
    mram_mb, sram_kb, optional_features, ...) or None when neither
    path resolves (the SoC JSON has no variant for this SKU AND the
    preset declares no silicon_variant, or declares `silicon_variant:
    TBD` per the no-inventing-values rule).
    """
    soc_path = resolve_soc_path(sku_preset.get("silicon"), metadata_root)
    if soc_path is None or not soc_path.is_file():
        return None
    soc_spec = json.loads(soc_path.read_text(encoding="utf-8"))
    variants = soc_spec.get("variants") or []

    declared = sku_preset.get("silicon_variant")
    # alp-sdk #1048: case/whitespace-insensitive TBD match ("tbd", "Tbd",
    # " TBD " all match) -- a bare `!= "TBD"` let an all-lowercase
    # `silicon_variant: tbd` read as a real order_code instead of being
    # dropped as the placeholder it was hand-typed to mean.
    declared_is_tbd = isinstance(declared, str) and declared.strip().upper() == "TBD"
    if declared and not declared_is_tbd:
        for v in variants:
            if v.get("order_code") == declared:
                return v
        # Forward declared but not found -- noisy fall-through to reverse lookup.

    sku = sku_preset.get("sku")
    if sku:
        for v in variants:
            if sku in (v.get("alp_module_skus") or []):
                return v
    return None


def resolve_memory_map(
    sku_preset: dict[str, Any],
    metadata_root: Path,
) -> list[dict[str, Any]]:
    """Derive the effective memory-region table for a SoM.

    Precedence:
      1. If the SoM preset declares `memory_map:` explicitly (used
         only for non-stock partitioning -- e.g. reserving SRAM for
         a hardware secure enclave outside the default layout), it
         wins verbatim.
      2. Otherwise the loader derives the region list from the SoC
         JSON variant resolved via `silicon_variant:`. Each named
         SRAM bank becomes one region; the MRAM becomes one region.
         Per-core TCM banks (SRAM names ending `_<core>_(ITCM|DTCM)`)
         carry `accessible_from: [<core>]`; un-suffixed banks are
         shared across every core declared in the variant's parent
         SoC `cores[]` list.

    The returned dicts have the keys defined by the memory_region
    schema: `name`, `size_kib`, `accessible_from`, `cacheable` (plus
    optional `base` only when the SoM preset's override declares one
    -- silicon-default bases stay unset, so downstream emitters know
    to use the silicon's defaults).

    Returns an empty list when the silicon_variant cannot be resolved
    (e.g. NX9101's `silicon_variant: TBD`) -- callers should treat
    that as "memory layout pending the HW-config writeup".
    """
    declared = sku_preset.get("memory_map")
    if declared:
        # SoM-side override wins; the loader trusts the maintainer.
        return list(declared)

    variant = _resolve_silicon_variant(sku_preset, metadata_root)
    if not variant:
        return []

    # Re-load the SoC JSON to grab the cores[] topology (the variant
    # dict alone doesn't carry per-core ids).
    soc_path = resolve_soc_path(sku_preset.get("silicon"), metadata_root)
    if soc_path is None or not soc_path.is_file():
        return []
    soc_spec = json.loads(soc_path.read_text(encoding="utf-8"))
    soc_cores = [c.get("id") for c in soc_spec.get("cores", []) if c.get("id")]

    regions: list[dict[str, Any]] = []

    # Prefer SoC-level fixed memory_regions when present — these carry
    # authoritative base addresses (e.g. RZ/V2N OCRAM at 0x00010000).
    soc_memory_regions = soc_spec.get("memory_regions")
    if soc_memory_regions:
        return list(soc_memory_regions)

    # MRAM as one region (size in KiB; mram_mb -> *1024).
    mram_mb = variant.get("mram_mb")
    if isinstance(mram_mb, (int, float)) and mram_mb > 0:
        regions.append({
            "name": "mram_main",
            "size_kib": int(mram_mb * 1024),
            "accessible_from": list(soc_cores),
            "cacheable": True,
        })

    # SRAM banks. Per-core TCM banks get `accessible_from: [<core>]`;
    # the rest are shared across every core in the SoC.
    sram_banks = variant.get("sram_banks_kb") or {}
    for bank_name, size_kib in sram_banks.items():
        if not isinstance(size_kib, int) or size_kib <= 0:
            continue
        # Detect per-core TCM by suffix.
        accessible: list[str] = list(soc_cores)
        for core_id in soc_cores:
            suffix_token = f"_{core_id.upper()}_"
            if suffix_token in bank_name.upper():
                accessible = [core_id]
                break
        # TCMs are typically non-cacheable; bulk SRAM is cacheable.
        is_tcm = "ITCM" in bank_name.upper() or "DTCM" in bank_name.upper()
        regions.append({
            "name": bank_name.lower(),
            "size_kib": int(size_kib),
            "accessible_from": accessible,
            "cacheable": not is_tcm,
        })

    return regions


def resolve_capabilities(
    sku_preset: dict[str, Any],
    metadata_root: Path,
) -> dict[str, Any]:
    """Compose silicon capabilities from the SoC JSON with SoM-side overrides/extensions.

    Returns a dict where SoM-declared keys override SoC-declared defaults for the same key
    (e.g., V2N's `cau: true` via the GD32 bridge overrides the RZ/V2N silicon's `cau: false`).
    Keys that exist on only one side are passed through unchanged.

    Precedence:
      1. Resolve the SoC ref via the ``silicon:`` field (same path used by
         ``_resolve_silicon_variant`` and ``resolve_memory_map``).
      2. Read ``soc["capabilities"]`` (defaults to ``{}`` when absent --
         older SoC JSONs that pre-date this field continue to work).
      3. Read ``sku_preset.get("capabilities", {})``.
      4. Merge; SoM-side wins on key collision so that SoM add-on chips /
         GD32 bridge capabilities can override the host silicon's defaults.
      5. Apply the SKU's ``silicon_capabilities.unpopulated`` RESTRICTION
         list: each listed silicon capability is forced to 0 (count) /
         ``False`` (flag).  A SKU can only remove what the silicon offers
         (enforced by scripts/validate_metadata.py); presets without the
         field keep the full silicon capability set.
    """
    soc_path = resolve_soc_path(sku_preset.get("silicon"), metadata_root)
    soc_caps: dict[str, Any] = {}
    if soc_path is not None and soc_path.is_file():
        soc_spec = json.loads(soc_path.read_text(encoding="utf-8"))
        soc_caps = soc_spec.get("capabilities") or {}

    som_caps: dict[str, Any] = sku_preset.get("capabilities") or {}

    # SoM side wins on collision (bridge / add-on overrides silicon default).
    merged: dict[str, Any] = {**soc_caps, **som_caps}

    # SKU-level restriction: capabilities the silicon offers but this SKU
    # leaves unpopulated (per-SKU granularity -- one family, many SKUs).
    # Preserve the value class so count-style caps (ethos_u55_count, ...)
    # restrict to 0 and flag-style caps restrict to False.
    for name in som_unpopulated_capabilities(sku_preset):
        base = soc_caps.get(name)
        if isinstance(base, int) and not isinstance(base, bool):
            merged[name] = 0
        else:
            merged[name] = False
    return merged


def som_unpopulated_capabilities(sku_preset: dict[str, Any]) -> list[str]:
    """Return the SKU's `silicon_capabilities.unpopulated` list (or []).

    Single accessor for the per-SKU capability RESTRICTION block so this
    module (`resolve_capabilities`), the emitters (`kconfig.py`, which passes
    `-DALP_SOM_<TOKEN>` only for restricted SKUs) and alp-sdk's header
    generator (`gen_soc_caps.py`) agree on where the field lives.
    """
    block = sku_preset.get("silicon_capabilities") or {}
    if not isinstance(block, dict):
        return []
    names = block.get("unpopulated") or []
    if not isinstance(names, list):
        return []
    return [str(n) for n in names]
