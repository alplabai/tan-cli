# SPDX-License-Identifier: Apache-2.0
"""Pure logic for Flow D's link-address guard (tan-cli#1371).

An image destined for the app's MRAM slot must be LOADed at or above that slot. An
ITCM-linked image (`p_paddr` 0x0) written there is the mistake this catches, and the
manifest's `flash_method: ram_run_only` only stops it when the manifest is current --
a hand-edited manifest, a build whose manifest write failed, or a direct backend call
bypass it. It is the mirror of `flash.ram-image-not-ram-linked` (`tan.core.ram_run`).

Only the program headers are read, with no IO: the symbol table (and its limits) is
irrelevant to a load address and must neither skip nor refuse the check. `p_paddr` is
the LOAD address; `p_vaddr` is where the image runs, and an image loaded into MRAM may
legitimately run from ITCM."""
from __future__ import annotations

import struct

#: Registered issue code (`contract/issue-codes.json`).
CODE_NOT_MRAM_LINKED = "flash.mram-image-not-mram-linked"

_ELF_HEADER_SIZE = 52
_PHDR_SIZE = 32
_PT_LOAD = 1


class UnreadableElf(Exception):
    """The program headers cannot be read; the message is the reason."""


def lowest_load_paddr(data: bytes) -> int:
    """The lowest `p_paddr` among LOAD segments with a nonzero `p_filesz` (never "the
    first LOAD": a zero-FileSiz `.bss` in DTCM is often listed first). Raises
    [`UnreadableElf`] for a 64-bit or big-endian file, a truncated header, a program
    header table past the end of the file, or no such segment."""
    if len(data) < _ELF_HEADER_SIZE:
        raise UnreadableElf(f"truncated: {len(data)} B is shorter than an ELF32 header")
    if data[4] != 1:
        raise UnreadableElf("it is not an ELF32 file (only ELF32 images are supported)")
    if data[5] != 1:
        raise UnreadableElf("it is not little-endian (only little-endian images are supported)")
    phoff = struct.unpack_from("<I", data, 28)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    if phnum and phentsize < _PHDR_SIZE:
        raise UnreadableElf(f"its program header entry size is {phentsize} B (an Elf32_Phdr is 32)")
    if phoff + phnum * phentsize > len(data):
        raise UnreadableElf(
            f"its {phnum} program headers at offset {phoff} run past the end of the file "
            f"({len(data)} B)"
        )
    loadable = []
    for i in range(phnum):
        p_type, _off, _vaddr, p_paddr, p_filesz, _memsz = struct.unpack_from(
            "<IIIIII", data, phoff + i * phentsize
        )
        if p_type == _PT_LOAD and p_filesz:
            loadable.append(p_paddr)
    if not loadable:
        raise UnreadableElf("it has no LOAD segment with file content")
    return min(loadable)


def mram_link_refusal(data: bytes, floor: int, floor_name: str) -> str | None:
    """A refusal message when the ELF in `data` is not loaded at or above `floor`, else
    `None`. `floor` is `slot0_load_address` when known, else the SoC's `soc_flash_base`;
    `floor_name` names which, for the message. An ELF whose program headers cannot be
    read is REFUSED, never waved through: the guard exists for the image nothing else
    caught. Equality is not required -- an image header may offset the first segment."""
    try:
        lowest = lowest_load_paddr(data)
    except UnreadableElf as err:
        return (
            f"the artefact's ELF could not be parsed ({err}), so its load address cannot be "
            "verified against the MRAM slot -- refusing to write an unchecked image."
        )
    if lowest >= floor:
        return None
    return (
        f"the image's lowest LOAD segment (p_paddr) is at 0x{lowest:X}, below {floor_name} "
        f"0x{floor:X}: it is not linked for that MRAM slot (an ITCM/RAM-linked build). "
        "Rebuild it MRAM-linked, or RAM-run it with `tan flash --ram` (Flow C)."
    )
