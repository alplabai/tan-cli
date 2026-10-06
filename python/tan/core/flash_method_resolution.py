# SPDX-License-Identifier: Apache-2.0
"""Record, in `build/system-manifest.yaml`, the flash method `tan flash` will
actually dispatch (tan-cli#1320).

The planner emits the DECLARED method (`flash_method: zephyr_west_flash` for
every Zephyr slice). `tan flash` then upgrades it at run time to Flow D
(`alif_mram_jlink`) whenever `flash_args` carries `jlink_flash_device`
(`flash_plan.select_flash_method`). The manifest is the artefact a reviewer
reads to learn how a board will be written, and Flow A and Flow D differ in
what they destroy (tan-cli#1252/#1267) -- so it named the wrong transport.

This module adds ONE field, `flash_method_resolved`, next to the untouched
`flash_method` (the declared one). Additive on purpose: alp-sdk-vscode matches
the literal `"TBD"` on `flash_method` to gate its Flash button, so that field
keeps its value and meaning; a consumer that wants "how will this be written"
reads the new key. The resolution is the SAME `select_flash_method` `tan flash`
calls, so the two cannot disagree for a manifest nobody hand-edited, and `tan
flash` still resolves from `flash_args` itself (a hand-edited
`flash_method_resolved` never steers a write).

Pure: no IO, mutates the raw mapping in place, like
`system_manifest.overlay_run_results_raw`.
"""
from __future__ import annotations

from typing import Any

from tan.core.flash_plan import FlashTarget, is_pending, select_flash_method

#: The additive manifest key. A string, present only on an entry that declares
#: a real (non-`TBD`) `flash_method`.
RESOLVED_KEY = "flash_method_resolved"


def _resolved_for(kind: str, entry_id: str, entry: dict) -> str | None:
    declared = entry.get("flash_method")
    if not isinstance(declared, str) or not declared.strip() or is_pending(declared):
        return None
    return select_flash_method(
        FlashTarget(kind=kind, id=entry_id, flash_method=declared, flash_args=entry.get("flash_args"))
    )


def annotate_resolved_flash_methods(raw: dict) -> None:
    """Add `flash_method_resolved` to every `slices[]` / `helper_mcus[]` entry
    that declares a real `flash_method`. Entries with no method, or the `TBD`
    sentinel, are left untouched."""
    for key, kind, id_key in (("slices", "slice", "core_id"), ("helper_mcus", "helper", "name")):
        entries: Any = raw.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            resolved = _resolved_for(kind, str(entry.get(id_key, "")), entry)
            if resolved is not None:
                entry[RESOLVED_KEY] = resolved
