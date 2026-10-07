# SPDX-License-Identifier: Apache-2.0
"""`lowest_load_address` (tan-cli#1370): which segments count, and never raising."""
import struct
from pathlib import Path

import pytest

from tan.core.elf_load import lowest_load_address

REAL_MRAM_ELF = Path("/home/caner/zephyrproject/build-conf-v441-slot0/zephyr/zephyr.elf")


def elf32(segments, *, big=False, phentsize=32):
    """segments: (p_type, p_paddr, p_filesz, p_memsz)."""
    hdr = bytearray(52)
    hdr[:7] = b"\x7fELF\x01" + (b"\x02" if big else b"\x01") + b"\x01"
    struct.pack_into("<I", hdr, 28, 52)
    struct.pack_into("<HH", hdr, 42, phentsize, len(segments))
    body = b"".join(
        struct.pack("<8I", t, 0, pa, pa, fs, ms, 5, 4).ljust(phentsize, b"\0")
        for t, pa, fs, ms in segments
    )
    return bytes(hdr) + body


def elf64(segments):
    hdr = bytearray(64)
    hdr[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<Q", hdr, 32, 64)
    struct.pack_into("<HH", hdr, 54, 56, len(segments))
    body = b"".join(
        struct.pack("<IIQQQQQQ", t, 5, 0, pa, pa, fs, ms, 4) for t, pa, fs, ms in segments
    )
    return bytes(hdr) + body


def _lowest(tmp_path, data):
    p = tmp_path / "x.elf"
    p.write_bytes(data)
    return lowest_load_address(p)


def test_itcm_layout_is_zero_and_ignores_the_ram_nobits_segment(tmp_path):
    assert _lowest(tmp_path, elf32([(1, 0x0, 0x4000, 0x4000), (1, 0x20000000, 0, 0x800)])) == 0


def test_mram_layout_ignores_the_dtcm_bss_segment(tmp_path):
    data = elf32([(1, 0x80010000, 0x4000, 0x4000), (1, 0x200000C0, 0, 0x800)])
    assert _lowest(tmp_path, data) == 0x80010000


def test_elf64_and_non_load_segments(tmp_path):
    assert _lowest(tmp_path, elf64([(6, 0x10, 8, 8), (1, 0x80010000, 8, 8)])) == 0x80010000


@pytest.mark.parametrize("data", [
    b"",
    b"not an elf at all" * 10,
    elf32([(1, 0x0, 4, 4)], big=True),
    elf32([]),
    elf32([(6, 0, 4, 4)]),
    elf32([(1, 0x20000000, 0, 0x800)]),
    elf32([(1, 0, 4, 4)], phentsize=8),
    elf32([(1, 0, 4, 4)])[:-4],
    elf32([(1, 0, 4, 4)])[:30],
])
def test_unclassifiable_input_is_none_never_an_exception(tmp_path, data):
    assert _lowest(tmp_path, data) is None


def test_missing_file_is_none(tmp_path):
    assert lowest_load_address(tmp_path / "absent.elf") is None


@pytest.mark.skipif(not REAL_MRAM_ELF.is_file(), reason="bench ELF not on this host")
def test_the_real_mram_image_is_not_below_the_flash_base():
    assert lowest_load_address(REAL_MRAM_ELF) >= 0x80000000
