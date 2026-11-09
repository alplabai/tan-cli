# SPDX-License-Identifier: Apache-2.0
"""What a Flow D write does to the boot table, spelled out (tan-cli#1322).

A Flow D `loadbin` of the ATOC REPLACES the whole table (tan-cli#1252). The
generic acknowledgement text says so, but an operator reading it cannot tell
WHICH entries survive. This module adds the specifics tan can honestly know:

* the entries the new ATOC will carry -- `DEVICE` (unless `--no-device-config`)
  and the entry being written -- known BEFORE anything is signed, so the refusal
  can name them, and known exactly from the signed report once it exists, so a
  preview can name those instead;
* the resident entries that will NOT be rewritten -- known only when the
  operator supplies them (`flash_args.resident_atoc_entries`, e.g. read off the
  SE-UART `maintenance -opt gettoc` output), because Flow D itself has no
  SE-UART channel to enumerate the resident table. When they are not supplied
  the text says they are unknown rather than implying there are none.

Pure: no IO.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from tan.core.flash_plan import FlashPlanError, _fa_has_key

#: The optional manifest key listing the entries resident on the board.
RESIDENT_KEY = "resident_atoc_entries"

_RESIDENT_ITEM = re.compile(r"^([^@\s]+)(?:@(0[xX][0-9A-Fa-f]+)(?:\+(0[xX][0-9A-Fa-f]+))?)?$")

#: The `DEVICE` entry name (`app-gen-toc`'s own spelling).
DEVICE_ENTRY = "DEVICE"


def written_entries(
    entry_id: str, *more: str, device_config: bool
) -> tuple[str, ...]:
    """The entry names the new ATOC will carry, in table order, as known
    without signing: `DEVICE` (when a device config is included) then the
    entry itself, then any further slices signed into the same ATOC (`more`,
    tan-cli#1509)."""
    return ((DEVICE_ENTRY,) if device_config else ()) + (entry_id, *more)


def resident_regions(flash_args: Any) -> tuple[tuple[str, int | None, int | None], ...] | None:
    """`flash_args.resident_atoc_entries` as `(name, address, size)` tuples, or
    `None` when the key is absent (the resident table is unknown). Each item is
    `NAME`, `NAME@0xADDR` or `NAME@0xADDR+0xSIZE`; the address (and size) are what
    let tan refuse a write whose sectors would erase that entry
    (`flash.write-sector-overlap`). A present but malformed value raises
    `FlashPlanError`: guessing here would understate what a write deletes."""
    if not _fa_has_key(flash_args, RESIDENT_KEY):
        return None
    raw = flash_args[RESIDENT_KEY]
    bad = FlashPlanError(
        f"alif_mram_jlink: flash_args.{RESIDENT_KEY} must be a list of ATOC entry names, "
        f"each NAME, NAME@0xADDR or NAME@0xADDR+0xSIZE (e.g. [DEVICE, ALP-HE@0x80010000+0x4000]); "
        f"got {raw!r}"
    )
    if not isinstance(raw, list):
        raise bad
    out: list[tuple[str, int | None, int | None]] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise bad
        match = _RESIDENT_ITEM.match(item.strip())
        if match is None:
            raise bad
        name, addr, size = match.groups()
        out.append((name, int(addr, 16) if addr else None, int(size, 16) if size else None))
    return tuple(out)


def resident_entries(flash_args: Any) -> tuple[str, ...] | None:
    """The NAMES in `flash_args.resident_atoc_entries`, or `None` when absent."""
    regions = resident_regions(flash_args)
    return None if regions is None else tuple(name for name, _a, _s in regions)


def replacement_detail(
    writes: Sequence[str],
    resident: Sequence[str] | None,
    *,
    device_config: bool,
    left_out: Sequence[str] = (),
) -> str:
    """The sentence appended to the whole-ATOC refusal and preview note.
    `left_out` (tan-cli#1509) names the manifest's other `alif_mram_jlink` slices
    this run does not flash: they are in the manifest, so tan KNOWS they are
    delisted whatever `resident_atoc_entries` says."""
    parts = [f"This ATOC names: {', '.join(writes)}."]
    if left_out:
        parts.append(
            f"The manifest's other alif_mram_jlink slice(s) ({', '.join(left_out)}) are NOT "
            "named, so they will be delisted too -- drop --core to flash them in the same ATOC."
        )
    if not device_config:
        parts.append(
            "It carries NO DEVICE entry (--no-device-config), so the resident DEVICE "
            "entry -- firewall regions, HFXO trims, SE_BOOT_INFO -- is deleted."
        )
    if resident is None:
        parts.append(
            "The resident table is UNKNOWN (Flow D cannot enumerate it): every entry "
            "not named above will be delisted. Read it first over the SE-UART "
            f"(`maintenance -opt gettoc`) and list it in flash_args.{RESIDENT_KEY} to "
            "have tan name them."
        )
    else:
        lost = [n for n in resident if n not in set(writes)]
        parts.append(
            "Resident entries that will NOT be rewritten (delisted): "
            + (", ".join(lost) if lost else "none")
            + "."
        )
    return " ".join(parts)
