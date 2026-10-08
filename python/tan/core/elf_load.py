# SPDX-License-Identifier: Apache-2.0
"""The lowest load address of an ELF image -- pure, stdlib-only.

`tan build --board` (tan-cli#1370) needs one fact about a Zephyr ELF to decide
how the image may be run: whether it is linked into the TCM (its lowest
`PT_LOAD` sits below the SoC's flash base) or into flash. This reads the program
headers and nothing else; it never raises on a file that is not an ELF.
"""
from __future__ import annotations

import struct
from pathlib import Path

_PT_LOAD = 1


def lowest_load_address(path: str | Path) -> int | None:
    """Lowest `p_paddr` among `PT_LOAD` segments that carry bytes (`p_filesz > 0`).

    A NOBITS `.bss` segment (filesz 0) sits in RAM whatever the image is linked
    to, so counting it would call every flash image a RAM image. `None` means
    UNCLASSIFIABLE -- unreadable, not a little-endian ELF32/ELF64, a malformed
    header table, or no loadable segment with bytes -- and never raises."""
    try:
        data = Path(path).read_bytes()
        return _lowest(data)
    except (OSError, struct.error, ValueError):
        return None


def _lowest(data: bytes) -> int | None:
    if len(data) < 52 or data[:4] != b"\x7fELF" or data[5] != 1:
        return None
    if data[4] == 1:  # ELF32
        min_ent, word = 32, "<I"
        phoff, = struct.unpack_from("<I", data, 28)
        phentsize, phnum = struct.unpack_from("<HH", data, 42)
        paddr_at, filesz_at = 12, 16
    elif data[4] == 2:  # ELF64
        min_ent, word = 56, "<Q"
        if len(data) < 64:
            return None
        phoff, = struct.unpack_from("<Q", data, 32)
        phentsize, phnum = struct.unpack_from("<HH", data, 54)
        paddr_at, filesz_at = 24, 32
    else:
        return None
    if phentsize < min_ent or phnum == 0:
        return None
    lowest: int | None = None
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + phentsize > len(data):
            return None
        p_type, = struct.unpack_from("<I", data, off)
        if p_type != _PT_LOAD:
            continue
        filesz, = struct.unpack_from(word, data, off + filesz_at)
        if filesz == 0:
            continue
        paddr, = struct.unpack_from(word, data, off + paddr_at)
        lowest = paddr if lowest is None else min(lowest, paddr)
    return lowest
