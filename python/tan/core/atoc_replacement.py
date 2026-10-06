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

from collections.abc import Sequence
from typing import Any

from tan.core.flash_plan import FlashPlanError, _fa_has_key

#: The optional manifest key listing the entries resident on the board.
RESIDENT_KEY = "resident_atoc_entries"

#: The `DEVICE` entry name (`app-gen-toc`'s own spelling).
DEVICE_ENTRY = "DEVICE"


def written_entries(entry_id: str, *, device_config: bool) -> tuple[str, ...]:
    """The entry names the new ATOC will carry, in table order, as known
    without signing: `DEVICE` (when a device config is included) then the
    entry itself."""
    return ((DEVICE_ENTRY,) if device_config else ()) + (entry_id,)


def resident_entries(flash_args: Any) -> tuple[str, ...] | None:
    """`flash_args.resident_atoc_entries` as a tuple of names, or `None` when
    the key is absent (the resident table is unknown). A present but malformed
    value (not a list of non-empty strings) raises `FlashPlanError`: guessing
    here would understate what a write deletes."""
    if not _fa_has_key(flash_args, RESIDENT_KEY):
        return None
    raw = flash_args[RESIDENT_KEY]
    if not isinstance(raw, list) or not all(isinstance(n, str) and n.strip() for n in raw):
        raise FlashPlanError(
            f"alif_mram_jlink: flash_args.{RESIDENT_KEY} must be a list of ATOC entry "
            f"names (e.g. [DEVICE, ALP-HE, HP-OWNER]); got {raw!r}"
        )
    return tuple(n.strip() for n in raw)


def replacement_detail(
    writes: Sequence[str],
    resident: Sequence[str] | None,
    *,
    device_config: bool,
) -> str:
    """The sentence appended to the whole-ATOC refusal and preview note."""
    parts = [f"This ATOC names: {', '.join(writes)}."]
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
