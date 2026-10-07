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
    """Lowest `p_paddr` among non-empty `PT_LOAD` segments, or `None` when the
    file is unreadable, not a little-endian ELF32/ELF64, or has no such segment."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:4] != b"\x7fELF" or len(data) < 64 or data[5] != 1:
        return None
    if data[4] == 1:  # ELF32
        phoff, = struct.unpack_from("<I", data, 28)
        phentsize, phnum = struct.unpack_from("<HH", data, 42)
        paddr_at, memsz_at = 12, 20
    elif data[4] == 2:  # ELF64
        phoff, = struct.unpack_from("<Q", data, 32)
        phentsize, phnum = struct.unpack_from("<HH", data, 54)
        paddr_at, memsz_at = 24, 40
    else:
        return None
    is64 = data[4] == 2
    lowest: int | None = None
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + phentsize > len(data):
            return None
        p_type, = struct.unpack_from("<I", data, off)
        if p_type != _PT_LOAD:
            continue
        word = "<Q" if is64 else "<I"
        paddr, = struct.unpack_from(word, data, off + paddr_at)
        memsz, = struct.unpack_from(word, data, off + memsz_at)
        if memsz == 0:
            continue
        lowest = paddr if lowest is None else min(lowest, paddr)
    return lowest
