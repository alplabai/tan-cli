# SPDX-License-Identifier: Apache-2.0
"""The IO half of the MRAM write path's link-address guard (tan-cli#1371).

`tan.core.mram_link` decides; this finds the artefact's ELF, reads it, and resolves
the MRAM aperture base from the SoC metadata (`soc_flash_base` -- the same key
`tan.planner.aperture` anchors its checks on, never a literal)."""
from __future__ import annotations

import json
import os
from typing import Any

from tan.core.flash_plan import FLOW_D_METHOD
from tan.core.mram_link import mram_link_refusal

#: `read_elf`'s ceiling: a Zephyr ELF with debug info is tens of MiB, never this.
MAX_ELF_BYTES = 512 * 1024 * 1024
_ELF_MAGIC = b"\x7fELF"


def find_elf(artefact_path: str) -> str | None:
    """The ELF behind a Flow D artefact: the artefact itself when it is one, else the
    same-stem `.elf` beside a raw `.bin` (the shape `resolve_slot0_binary` pairs). `None`
    when there is none -- a bare `.bin` carries no link address to check."""
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
        except OSError:
            continue
        if data[:4] == _ELF_MAGIC:
            return path
    return None


def soc_flash_base(ctx: Any) -> int | None:
    """The MRAM aperture base: `soc_flash_base` of the SoC JSON the manifest SKU's SoM
    preset names, under the SDK in use. `None` when any link of that chain is missing."""
    from tan.commands.build_output import read_sdk_som_and_soc

    metadata_root = os.path.join(ctx.sdk_root, "metadata") if ctx.sdk_root else None
    if not (metadata_root and ctx.sku and os.path.isdir(metadata_root)):
        return None
    walked = read_sdk_som_and_soc(metadata_root, ctx.sku)
    parts = walked[0].split(":") if walked else []
    if len(parts) != 3:
        return None
    soc_path = os.path.join(metadata_root, "socs", parts[0], parts[1], f"{parts[2]}.json")
    try:
        with open(soc_path, encoding="utf-8") as handle:
            base = json.load(handle).get("soc_flash_base")
    except (OSError, ValueError, AttributeError):
        return None
    return base if isinstance(base, int) and not isinstance(base, bool) else None


def mram_link_guard(
    artefact_path: str, entry_id: str, ctx: Any, *, method: str = FLOW_D_METHOD,
    skip_unresolved_base: bool = False,
) -> str | None:
    """The refusal for an entry whose ELF is not MRAM-linked, else `None`.

    Flow D (`alif_mram_jlink`) is strict: an ELF with an unresolvable `soc_flash_base`
    is refused. Flow A (`zephyr_west_flash` on the Alif runner) passes
    `skip_unresolved_base=True` and is NOT checked in that case -- that method also
    serves non-MRAM boards, so an unknown MRAM aperture is not evidence of a problem."""
    elf_path = find_elf(artefact_path)
    if elf_path is None:
        return None
    base = soc_flash_base(ctx)
    if base is None and skip_unresolved_base:
        return None
    with open(elf_path, "rb") as handle:
        data = handle.read()
    message = mram_link_refusal(data, base)
    return None if message is None else f"{method}[{entry_id}]: refusing -- {message}"


#: Runners that burn MRAM slot0 over the SE-UART. `None` is `west flash` falling back
#: to the board default, which on an Alif board is `alif_flash`.
ALIF_MRAM_RUNNERS = (None, "alif_flash")
