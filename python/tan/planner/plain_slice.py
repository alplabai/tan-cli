# SPDX-License-Identifier: Apache-2.0
"""The `system-manifest.yaml` a `board.yaml`-less Zephyr build writes (tan-cli#1370).

`tan build --board <zephyr-board-target>` has no `board.yaml` for the planner to
project a manifest from. This builds the same document the planner would have
written for ONE slice -- same schema, same `flash_method`/`flash_args` (composed
by the planner's own `_slice_flash_recipe`, not re-spelled here) -- from the SoM
preset whose `topology.<core>.board` names the target.

The planner core id (`m55_he`) is NOT the Zephyr cpucluster (`rtss_he`) the board
qualifier ends in: it comes from the preset's topology key, so no core name is
spelled in this module. An unknown target (`native_sim`, a non-Alp board) still
gets a one-slice manifest, with no SoC facts to carry.

Imports of the planner are local: the planner freezes its SDK root at import
time, so callers must `bind_sdk_root` first.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: A manifest slice linked below the SoC flash base runs from RAM only.
RAM_RUN_ONLY = "ram_run_only"
#: The flash_args a RAM-run-only slice keeps: the wrong-board preflight pair.
_RAM_RUN_KEYS = ("expect_dpidr", "jlink_device")


def _find_preset(board: str, metadata_root: Path) -> tuple[dict, str] | None:
    from .loader import _load_yaml

    for path in sorted((metadata_root / "e1m_modules").glob("*.yaml")):
        try:
            preset = _load_yaml(path)
        except Exception:  # noqa: BLE001 -- an unreadable preset is not THIS target's
            continue
        for core_id, topo in sorted((preset.get("topology") or {}).items()):
            if isinstance(topo, dict) and topo.get("board") == board:
                return preset, str(core_id)
    return None


def plain_core_id(board: str, metadata_root: Path) -> str | None:
    """The planner core id of the SoM topology entry that names `board`."""
    found = _find_preset(board, metadata_root)
    return found[1] if found else None


def plain_system_manifest(
    board: str, metadata_root: Path | None, *, fallback_core_id: str, elf_path: str | None = None
) -> str:
    """The manifest YAML for one plain Zephyr slice, status `pending`.

    `elf_path`, when it points at an ELF whose lowest load address is below the
    SoC's `soc_flash_base`, marks the slice `ram_run_only`."""
    import yaml

    from tan.core.elf_load import lowest_load_address

    from .loader import (
        _enforce_flow_d_preflight_pair, _jlink_flash_device_declared, _load_json, _resolve_flow_d_preflight,
        _resolve_jlink_flash_device, _resolve_slot0_load_address,
        _resolve_variant_debug, _silicon_to_soc_path, _slice_from_resolved)
    from .models import Slice
    from .validate import _enforce_os_matches_core_class
    from .orchestrator import _slice_flash_recipe

    found = _find_preset(board, metadata_root) if metadata_root is not None else None
    hw_info: dict[str, Any] = dict.fromkeys(
        ("sku", "som_hw_rev", "board_name", "board_hw_rev", "silicon"))
    hw_info["sku"] = ""
    flash_base: int | None = None
    if found is None:
        slice_ = Slice(core_id=fallback_core_id, os="zephyr", board=board)
    else:
        preset, core_id = found
        topo = preset["topology"][core_id]
        soc_spec = _load_json(_silicon_to_soc_path(preset["silicon"], metadata_root))
        debug = _resolve_variant_debug(preset, soc_spec)
        declared = _jlink_flash_device_declared(debug)
        flash_device = _resolve_jlink_flash_device(debug)
        expect_dpidr, jlink_device = _resolve_flow_d_preflight(debug, core_id)
        slice_ = _slice_from_resolved(
            core_id,
            {"os": "zephyr", "board": board, "toolchain": topo.get("toolchain")},
            jlink_flash_device=flash_device,
            jlink_flash_device_declared=declared,
            expect_dpidr=expect_dpidr,
            jlink_device=jlink_device,
            slot0_load_address=(
                _resolve_slot0_load_address(preset, core_id)
                if (declared or flash_device is not None) else None),
        )
        # The same refusals the planned route applies per slice (loader.py).
        core_type = next(
            (str(c.get("type") or "") for c in (soc_spec.get("cores") or [])
             if c.get("id") == core_id), "")
        _enforce_flow_d_preflight_pair(slice_, debug, preset["sku"])
        _enforce_os_matches_core_class(slice_, core_type)
        hw_info.update(sku=preset["sku"], silicon=preset.get("silicon"))
        base = soc_spec.get("soc_flash_base")
        flash_base = int(base) if isinstance(base, int) else None

    entry = slice_.to_manifest_entry()
    if found is None:
        # No SoM preset names this target: there is no recipe to compose, and a
        # guessed one could program the wrong thing. `tan flash` skips a slice
        # with no flash_method, naming this reason.
        entry["flash_method"] = "none"
        entry["flash_args"] = {}
        entry["reason"] = (
            f"no SoM preset in the SDK names board target `{board}`; "
            "no flash recipe is known for it")
    elif flash_base is not None:
        # The SoC has a flash window, so WHERE the image is linked decides how it
        # may be run. An image tan cannot classify must never inherit the MRAM
        # recipe (slot0_load_address / jlink_flash_device): refuse it as RAM-only.
        # TODO(tan-cli#1360): also set `slice_.link_target = "itcm"` and import
        # RAM_RUN_ONLY_METHOD from `tan.core.link_refusal` once #1360 is in this
        # base, so `tan flash` refuses the method by name.
        low = lowest_load_address(elf_path) if elf_path else None
        if low is None or low < flash_base:
            _, args = _slice_flash_recipe(slice_)
            entry["flash_method"] = RAM_RUN_ONLY
            entry["flash_args"] = {k: v for k, v in (args or {}).items() if k in _RAM_RUN_KEYS}
            if low is None:
                entry["reason"] = (
                    "the built ELF could not be classified (missing, unreadable, or "
                    "no loadable segment); not treated as a flash image")
    doc = {
        "schema_version": 1,
        "generated_by": "tan build --board",
        "hw_info": hw_info,
        "slices": [entry],
        "ipc": [],
        "helper_mcus": [],
        "boot_order": [],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
