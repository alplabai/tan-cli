# SPDX-License-Identifier: Apache-2.0
"""The minimal `system-manifest.yaml` of a `tan build --board` build (tan-cli#1370).

A planned build gets its manifest projected from a board.yaml by the planner;
the board.yaml-less route (tan-cli#1359) has none, yet `tan flash` / `tan size`
/ `tan image` read that file. This module writes the slice entry a planned
Zephyr slice would carry, from what tan already knows plus the SDK's SoC
metadata -- nothing here is a hardware literal.

Board target -> planner core id. The Zephyr target is
`<board>/<soc>/<cpucluster>`; the SoC JSON (`metadata/socs/**`) publishes both
halves: `variants[].order_code` (lower-cased it IS the `<soc>` segment, e.g.
`ae822fa0e5597ls0`) and `cores[].zephyr_cpucluster` (`rtss_he` -> core `m55_he`).
An unmatched target (native_sim, a non-Alif SoC, no SDK metadata) keeps the
slice's own id and carries no flash profile.

Flash decision. `flash_method` is `zephyr_west_flash`, the constant the planner
gives every Zephyr slice. `flash_args` carries the SW-DP wrong-board preflight
PAIR (`expect_dpidr` + `jlink_device[<core>]`) exactly as a planned slice does,
and only as a pair. It does NOT carry `jlink_flash_device` or
`slot0_load_address`, so Flow D (the J-Link MRAM path, selected by
`FLOW_D_KEYS`) is not armed: a plain `tan flash` takes Flow A, `west flash` with
the board's default runner (`alif_flash` over the SE-UART on AEN), which writes
the Zephyr-built image. The pair arms the wrong-board preflight. Flow D is left
out because `slot0_load_address` comes from the SoM preset's `memory_map:`,
which a bare board target does not name.
"""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PLAIN_GENERATED_BY = "tan build --board"
_FLASH_METHOD = "zephyr_west_flash"


@dataclass(frozen=True)
class PlainSlice:
    """One plan slice of the `--board` route: its id, `west -b` target and
    plan-relative build dir (`build/<id>-zephyr`)."""

    slice_id: str
    board: str
    build_dir: str


@dataclass(frozen=True)
class TargetFacts:
    core_id: str
    silicon: str | None = None
    flash_args: dict | None = None


def target_facts(board: str, fallback_core: str, socs: Sequence[dict]) -> TargetFacts:
    """Resolve `board` against parsed SoC JSON documents (pure)."""
    parts = board.split("/")
    if len(parts) < 3:
        return TargetFacts(fallback_core)
    soc_segment, cluster = parts[1].lower(), parts[2]
    for soc in socs:
        core = next(
            (c for c in soc.get("cores") or [] if c.get("zephyr_cpucluster") == cluster), None
        )
        variant = next(
            (v for v in soc.get("variants") or []
             if str(v.get("order_code", "")).lower() == soc_segment),
            None,
        )
        if core is None or variant is None or not core.get("id"):
            continue
        debug = variant.get("debug") or {}
        dpidr = debug.get("expect_dpidr")
        attach = (debug.get("jlink_device") or {}).get(core["id"])
        flash_args = {"expect_dpidr": dpidr, "jlink_device": attach} if dpidr and attach else {}
        return TargetFacts(core["id"], variant["order_code"], flash_args)
    return TargetFacts(fallback_core)


def load_socs(sdk_root: str | None) -> list[dict]:
    """Every readable `metadata/socs/**/*.json` under the SDK, sorted by path."""
    if not sdk_root:
        return []
    out: list[dict] = []
    for path in sorted((Path(sdk_root) / "metadata" / "socs").rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def minimal_manifest(
    slices_in: Sequence[PlainSlice], sdk_root: str | None, base: str
) -> tuple[dict, dict[str, str]]:
    """The raw manifest for the plan's slices, plus the `slice id -> manifest
    core id` rename the run results need (pure but for the SoC metadata read).
    `build_dir` is always the absolute west tree `<base>/<plan build dir>/build`,
    so a failed or never-dispatched slice still resolves it (never the
    `<core>-<os>` default, which names the renamed core, not the slice dir)."""
    socs = load_socs(sdk_root)
    slices: list[dict] = []
    renames: dict[str, str] = {}
    silicon: str | None = None
    for item in slices_in:
        facts = target_facts(item.board, item.slice_id, socs)
        renames[item.slice_id] = facts.core_id
        silicon = silicon or facts.silicon
        entry = {
            "core_id": facts.core_id,
            "os": "zephyr",
            "board": item.board,
            "build_dir": os.path.abspath(os.path.join(base, item.build_dir, "build")),
            "status": "pending",
            "flash_method": _FLASH_METHOD,
        }
        if facts.flash_args:
            entry["flash_args"] = facts.flash_args
        slices.append(entry)
    hw_info = {"sku": ""}
    if silicon:
        hw_info["silicon"] = silicon
    return {
        "schema_version": 1,
        "generated_by": PLAIN_GENERATED_BY,
        "hw_info": hw_info,
        "slices": slices,
    }, renames


def slice_boards(plan_slices) -> list[PlainSlice]:
    """Each plan slice's `west build -b` target, with its id and build dir."""
    out: list[PlainSlice] = []
    for s in plan_slices:
        args = s.command.args if s.command else []
        if "-b" in args and args.index("-b") + 1 < len(args):
            out.append(PlainSlice(s.core_id, args[args.index("-b") + 1], s.build_dir))
    return out
