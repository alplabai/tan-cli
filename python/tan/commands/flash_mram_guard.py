# SPDX-License-Identifier: Apache-2.0
"""The IO half of Flow D's link-address guard (tan-cli#1371).

`tan.core.mram_link` decides; this finds the artefact's ELF and parses
`slot0_load_address`, the floor the image's load addresses are held to.

**Scope, deliberately narrow.** The guard runs only for the Flow D shapes tan itself
controls: the mramxip `loadbin` at `slot0_load_address` and the SETOOLS auto-sign
(`mramAddress`). It does NOT run for an operator-supplied ATOC with no
`slot0_load_address` (that ATOC may carry a legitimate ITCM load entry), and it does
NOT run for Flow A (`zephyr_west_flash` on the `alif_flash` runner): alp-sdk's
`alif_flash.py` already refuses an unrecognised reset vector and supports images linked
at the ITCM global alias (0x58xxxxxx / 0x50xxxxxx) with an ATOC `loadAddress`, so an ELF
below `soc_flash_base` is not generally unbootable there. `west flash` generally and
`baremetal_cmake_flash` are likewise unchecked."""
from __future__ import annotations

import os
from typing import Any

from tan.core.flash_plan import FLOW_D_METHOD, FlashPlanError, fa_str_checked
from tan.core.mram_link import mram_link_refusal

#: Ceiling on the bytes read: a Zephyr ELF with debug info is tens of MiB, never this.
MAX_ELF_BYTES = 512 * 1024 * 1024
_ELF_MAGIC = b"\x7fELF"


#: How much older than the `.bin` its same-stem `.elf` may be before it is distrusted. A
#: normal build links the ELF and then `objcopy`s the `.bin` from it, seconds apart; an ELF
#: from an earlier build beside a swapped-in `.bin` is minutes or more older.
STALE_ELF_SECONDS = 60.0


def find_elf(artefact_path: str) -> tuple[bytes, bool] | None:
    """`(bytes, stale)` of the ELF behind a Flow D artefact: the artefact itself when it
    carries ELF magic, else the same-stem `.elf` beside a raw `.bin`. Read once.

    `stale` is True only for the sibling case, when the `.elf` is more than
    [`STALE_ELF_SECONDS`] older than the `.bin`: its link address cannot vouch for a
    newer binary. Limitation: the pairing is by file stem only. A `.bin` whose `.elf`
    sits elsewhere, or is named differently, is not found -- and a `.bin` with no ELF
    carries no link address, so it is not checked. `None` also for an unreadable or
    oversized file."""
    candidates = [artefact_path]
    stem, ext = os.path.splitext(artefact_path)
    if ext.lower() == ".bin":
        candidates.append(stem + ".elf")
    for path in candidates:
        try:
            if os.path.getsize(path) > MAX_ELF_BYTES:
                continue
            with open(path, "rb") as handle:
                data = handle.read()
            stale = path != artefact_path and (
                os.path.getmtime(artefact_path) - os.path.getmtime(path) > STALE_ELF_SECONDS
            )
        except OSError:
            continue
        if data[:4] == _ELF_MAGIC:
            return data, stale
    return None


def slot0_address(flash_args: Any) -> int | None:
    """`flash_args.slot0_load_address` as an int, or `None` when absent or malformed (a
    malformed value is refused later by `validate_flow_d_shape` with its own message)."""
    try:
        text = fa_str_checked(flash_args, "slot0_load_address", True)
        return None if text is None else int(text, 16)
    except (FlashPlanError, ValueError):
        return None


def mram_link_guard(artefact_path: str, entry_id: str, *, slot0: int) -> str | None:
    """The refusal for a Flow D entry whose ELF is loaded below `slot0`
    (`slot0_load_address`), or whose same-stem ELF is older than its `.bin`, else `None`.
    The caller invokes this only for the shapes tan controls (see the module docstring)."""
    found = find_elf(artefact_path)
    if found is None:
        return None
    data, stale = found
    prefix = f"{FLOW_D_METHOD}[{entry_id}]: refusing -- "
    if stale:
        return (
            f"{prefix}the ELF beside the .bin ({os.path.basename(os.path.splitext(artefact_path)[0])}"
            ".elf) is older than the .bin, so its link address cannot vouch for it. Rebuild, "
            "or pass the ELF itself as the artefact."
        )
    message = mram_link_refusal(data, slot0, "slot0_load_address")
    return None if message is None else prefix + message
