# SPDX-License-Identifier: Apache-2.0
"""Pure logic for the MRAM write path's link-address guard (tan-cli#1371).

The mirror of `flash.ram-image-not-ram-linked` (`tan.core.ram_run`): Flow C refuses
an MRAM-linked image, the MRAM write path (Flow D, and the SETOOLS auto-sign that
feeds it) refuses a RAM-linked one. An ITCM-linked image (`p_paddr` 0x0) written to
the MRAM slot0 boots as garbage, and the manifest's `flash_method: ram_run_only`
(PR #1360) only stops it when the manifest is current -- a hand-edited manifest, a
build whose manifest write failed, or a direct backend call all bypass it. This reads
the ELF itself, with `ram_run`'s ELF32 reader, and has no IO of its own."""
from __future__ import annotations

from tan.core.ram_run import RamRunError, parse_elf

#: Registered issue code (`contract/issue-codes.json`).
CODE_NOT_MRAM_LINKED = "flash.mram-image-not-mram-linked"


def lowest_load_paddr(data: bytes) -> int | None:
    """The lowest `p_paddr` among the ELF's LOAD segments with a nonzero `p_filesz`
    (never "the first LOAD segment": a zero-FileSiz `.bss` in DTCM is often listed
    first). `None` when `data` is not a readable ELF32 or has no such segment."""
    try:
        elf = parse_elf(data)
    except RamRunError:
        return None
    loadable = [s.paddr for s in elf.segments if s.filesz]
    return min(loadable) if loadable else None


def mram_link_refusal(data: bytes, mram_base: int | None) -> str | None:
    """A refusal message when the ELF in `data` is not linked into MRAM, else `None`.

    `mram_base` is the SoC's `soc_flash_base`. When it is `None` (the SoC metadata
    could not be read) an ELF is REFUSED rather than waved through: the guard exists
    for the case where nothing else caught the image, so it never silently skips.
    A file that is not a readable ELF has no link address to check and is left to the
    flow's own shape checks."""
    base = lowest_load_paddr(data)
    if base is None:
        return None
    if mram_base is None:
        return (
            f"cannot verify the image is MRAM-linked (lowest LOAD segment at 0x{base:X}): "
            "the SoC's soc_flash_base could not be resolved from the SDK metadata, so the "
            "MRAM aperture is unknown -- refusing to write an unchecked image to MRAM. "
            "Point --sdk-root at an SDK whose SoC metadata declares soc_flash_base."
        )
    if base < mram_base:
        return (
            f"the image's lowest LOAD segment is at 0x{base:X}, below the MRAM aperture "
            f"base 0x{mram_base:X} (the SoC's soc_flash_base): it is linked for RAM (ITCM), "
            "and written to MRAM it would not boot. Rebuild it MRAM-linked, or RAM-run it "
            "with `tan flash --ram` (Flow C)."
        )
    return None
