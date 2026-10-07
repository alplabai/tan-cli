# SPDX-License-Identifier: Apache-2.0
"""Pure logic for AEN Flow C -- a J-Link ITCM RAM-run (tan-cli#1313).

Flow C loads a Zephyr image into ITCM over SWD, points the core at its reset
handler, lets it run and reads `ram_console_buf` back. **Nothing touches MRAM.**
The proven raw helper is alp-sdk `scripts/bench/aen/ram-run.sh` (+ `reread.sh`);
this module is its decision half, with no IO beyond reading the ELF bytes it is
given:

* a minimal ELF32 reader (no `pyelftools`: tan declares no such dependency and
  the toolchain's `readelf`/`nm` are not guaranteed to be on the host) -- the
  LOAD segments and the symbol table;
* [`plan_ram_image`]: the load address DERIVED from the LOAD segment with the
  lowest `p_paddr` among those with a nonzero `p_filesz` (never "the first LOAD
  segment", which is often a zero-FileSiz `.bss` in DTCM -- loading there splats
  live RAM), the refusals the script makes (MRAM-linked image, implausible base,
  vector table that disagrees with the ELF entry), and the initial SP/PC read
  from the vector table;
* the J-Link Commander scripts for the load session and the console read;
* [`decode_console`]: the `mem8` dump -> text, exactly the script's awk.
"""
from __future__ import annotations

import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass

#: Registered issue codes (`contract/issue-codes.json`).
CODE_NOT_RAM_LINKED = "flash.ram-image-not-ram-linked"
CODE_CONSOLE_SYMBOL_MISSING = "flash.ram-console-symbol-missing"
CODE_FAILED = "flash.ram-failed"

#: The RAM console buffer symbol (`CONFIG_RAM_CONSOLE`).
CONSOLE_SYMBOL = "ram_console_buf"

#: Largest region one `mem8` command reads (bench-env.sh `bench_mem8_chunks`).
MEM8_CHUNK = 65536
#: Upper bound on the console read, so a corrupt symbol size cannot ask for MiBs.
MAX_CONSOLE_BYTES = 1 << 20
#: What the script reads when the symbol carries no size.
DEFAULT_CONSOLE_BYTES = 0x600

MRAM_BASE = 0x80000000


class RamRunError(Exception):
    """A Flow C refusal. `code` is the registered issue code."""

    def __init__(self, message: str, code: str = CODE_FAILED) -> None:
        super().__init__(message)
        self.code = code


# ── ELF32 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    paddr: int
    vaddr: int
    filesz: int
    memsz: int


@dataclass(frozen=True)
class ElfImage:
    entry: int
    segments: tuple[Segment, ...]
    #: name -> (address, size)
    symbols: dict[str, tuple[int, int]]


def parse_elf(data: bytes) -> ElfImage:
    """Parse the parts of a little-endian ELF32 that Flow C needs. Raises
    `RamRunError` for anything else (a 64-bit or big-endian file, a truncated
    one): guessing at a load address from a file we cannot read is how a
    resident app's stack gets overwritten."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise RamRunError("not an ELF file")
    if data[4] != 1 or data[5] != 1:
        raise RamRunError("only little-endian ELF32 images are supported")
    try:
        (_t, _m, _v, entry, phoff, shoff, _f, _eh, phentsize, phnum, shentsize, shnum, _si) = (
            struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
        )
        segments = []
        for i in range(phnum):
            off = phoff + i * phentsize
            p_type, _o, p_vaddr, p_paddr, p_filesz, p_memsz = struct.unpack_from("<IIIIII", data, off)
            if p_type == 1:  # PT_LOAD
                segments.append(Segment(p_paddr, p_vaddr, p_filesz, p_memsz))
        symbols = _symbols(data, shoff, shentsize, shnum)
    except struct.error as err:
        raise RamRunError(f"truncated ELF file ({err})") from err
    return ElfImage(entry, tuple(segments), symbols)


def _symbols(data: bytes, shoff: int, shentsize: int, shnum: int) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    if not shoff or not shnum:
        return out
    headers = [
        struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)
    ]
    for _name, sh_type, _fl, _addr, sh_off, sh_size, sh_link, _info, _align, sh_entsize in headers:
        if sh_type != 2 or not sh_entsize:  # SHT_SYMTAB
            continue
        if sh_link >= len(headers):
            continue
        str_off, str_size = headers[sh_link][4], headers[sh_link][5]
        strtab = data[str_off : str_off + str_size]
        for i in range(sh_size // sh_entsize):
            st_name, st_value, st_size, _info2, _other, _shndx = struct.unpack_from(
                "<IIIBBH", data, sh_off + i * sh_entsize
            )
            end = strtab.find(b"\0", st_name)
            name = strtab[st_name : end if end >= 0 else None].decode("ascii", "replace")
            if name:
                out.setdefault(name, (st_value, st_size))
    return out


# ── the image plan ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RamImage:
    base: int
    entry: int  # PC to run from, thumb bit cleared
    initial_sp: int
    reset_vector: int
    size: int
    console: tuple[int, int] | None  # (address, size) of ram_console_buf


def _plausible_base(base: int) -> bool:
    return base in (0x0, 0x50000000, 0x58000000) or 0x02000000 <= base < 0x03000000


def plan_ram_image(elf: ElfImage, binary: bytes) -> RamImage:
    """Decide whether `binary` (the raw `zephyr.bin` beside `elf`) can be
    RAM-run and where. Refuses with [`CODE_NOT_RAM_LINKED`] when it is linked
    for MRAM or any base that is not an ITCM/SRAM target, and with
    [`CODE_FAILED`] when the pieces disagree."""
    loadable = [s for s in elf.segments if s.filesz]
    if not loadable:
        raise RamRunError("the ELF has no LOAD segment with file content")
    base = min(s.paddr for s in loadable)
    if base >= MRAM_BASE:
        raise RamRunError(
            f"the image is slot0/MRAM-linked (LOAD segment at 0x{base:X}); loading it "
            "re-enters the ALREADY-RESIDENT MRAM image, not the freshly built one. "
            "Rebuild with the Flow C ITCM retarget (aen-flowc-itcm.conf + "
            "aen-flowc-itcm.overlay) or flash it with Flow D.",
            CODE_NOT_RAM_LINKED,
        )
    if not _plausible_base(base):
        raise RamRunError(
            f"derived load base 0x{base:X} is not a Flow C target (expected 0x0, the ITCM "
            "global alias 0x50000000/0x58000000, or SRAM 0x02xxxxxx) -- a DTCM/data "
            "segment was picked, which would corrupt live RAM. Refusing to load.",
            CODE_NOT_RAM_LINKED,
        )
    if len(binary) < 8:
        raise RamRunError("zephyr.bin is too small to hold a vector table")
    span = max(s.paddr + s.filesz for s in loadable) - base
    if len(binary) < span:
        raise RamRunError(
            f"zephyr.bin is {len(binary)} B but the ELF's LOAD segments span {span} B from "
            f"0x{base:X} -- the binary and the ELF are from different builds."
        )
    sp, reset = struct.unpack_from("<II", binary, 0)
    entry = reset & ~1
    if entry != elf.entry & ~1:
        raise RamRunError(
            f"the vector table's reset handler (0x{reset:X}) and the ELF entry point "
            f"(0x{elf.entry:X}) disagree -- refusing to guess the PC."
        )
    if sp == 0 or sp >= MRAM_BASE:
        raise RamRunError(f"the vector table's initial SP 0x{sp:X} is not a RAM address.")
    console = None
    sym = elf.symbols.get(CONSOLE_SYMBOL)
    if sym is not None:
        addr, size = sym
        console = (addr, min(size or DEFAULT_CONSOLE_BYTES, MAX_CONSOLE_BYTES))
    return RamImage(base, entry, sp, reset, len(binary), console)


# ── J-Link scripts ──────────────────────────────────────────────────────────


def preamble(serial: str | None, speed: int, device: str) -> list[str]:
    """Everything up to and including `connect`, shared by both sessions."""
    lines = [f"SelectEmuBySN {serial}"] if serial else []
    return [*lines, "si SWD", f"speed {speed}", f"device {device}", "connect"]


def load_script(pre: Sequence[str], binary_path: str, image: RamImage) -> str:
    """`connect; halt; loadbin; setpc; go` -- the proven Flow C load session.
    `loadbin` resets the core and re-reads the vector table (SP); `setpc` enters
    the reset handler. No MRAM address appears anywhere."""
    return "\n".join(
        [*pre, "halt", f"loadbin {binary_path} 0x{image.base:X}", f"setpc 0x{image.entry:X}", "go", "exit"]
    ) + "\n"


def mem8_lines(address: int, size: int) -> list[str]:
    out, off = [], 0
    while off < size:
        chunk = min(MEM8_CHUNK, size - off)
        out.append(f"mem8 0x{address + off:X}, 0x{chunk:X}")
        off += chunk
    return out


def read_script(pre: Sequence[str], address: int, size: int) -> str:
    return "\n".join([*pre, *mem8_lines(address, size), "exit"]) + "\n"


_DUMP_LINE = re.compile(r"^([0-9A-Fa-f]+) = ((?:[0-9A-Fa-f]{2}\s*)+)$")


def parse_dump(transcript: str) -> dict[int, bytes]:
    """Every `ADDR = xx xx ...` line of a `mem8` transcript, concatenated per
    contiguous run, keyed by the address of the run's first byte."""
    runs: dict[int, bytearray] = {}
    cursor: tuple[int, int] | None = None  # (run start, next address)
    for raw in transcript.splitlines():
        match = _DUMP_LINE.match(raw.strip())
        if not match:
            continue
        addr = int(match.group(1), 16)
        data = bytes(int(b, 16) for b in match.group(2).split())
        if cursor is not None and cursor[1] == addr:
            runs[cursor[0]].extend(data)
            cursor = (cursor[0], addr + len(data))
        else:
            runs[addr] = bytearray(data)
            cursor = (addr, addr + len(data))
    return {k: bytes(v) for k, v in runs.items()}


def read_back(transcript: str, address: int, size: int) -> bytes:
    """The `size` bytes at `address` out of a `mem8` transcript, or raise
    `RamRunError` when any part is missing (J-Link can drop one chunk's dump
    silently, alp-sdk#2313)."""
    buf = bytearray(size)
    have = bytearray(size)
    for start, data in parse_dump(transcript).items():
        lo = max(start, address)
        hi = min(start + len(data), address + size)
        if lo < hi:
            buf[lo - address : hi - address] = data[lo - start : hi - start]
            have[lo - address : hi - address] = b"\x01" * (hi - lo)
    if not all(have):
        missing = have.index(0)
        raise RamRunError(
            f"the RAM console read is incomplete: no dump for 0x{address + missing:X} "
            f"(of {size} bytes at 0x{address:X})"
        )
    return bytes(buf)


def decode_console(data: bytes) -> str:
    """The script's awk: stop after more than four consecutive NULs, skip a NUL
    run, newline for CR/LF, printable ASCII kept, everything else dropped."""
    out: list[str] = []
    nul = 0
    for b in data:
        if b == 0:
            nul += 1
            if nul > 4:
                break
            continue
        nul = 0
        if b in (10, 13):
            out.append("\n")
        elif 32 <= b < 127:
            out.append(chr(b))
    return "".join(out)


# ── transcript checks ───────────────────────────────────────────────────────

_CONNECT_FAILURES = ("Cannot connect", "Could not connect", "Failed to power up DAP")


def _window(transcript: str, echo: str) -> str | None:
    """What J-Link printed between the echo of `echo` and the next prompt, or
    `None` when the command was never echoed (a stub, or a quiet build)."""
    lines = transcript.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"J-Link>{echo}":
            out = []
            for follow in lines[i + 1 :]:
                if follow.startswith("J-Link>"):
                    break
                out.append(follow)
            return "\n".join(out)
    return None


def check_session(transcript: str, *, loadbin: bool) -> str | None:
    """The refusals `ram-run.sh` makes on a session transcript: a connect that
    did not happen, a session that never reached `Script processing
    completed.` (the J-Link process died mid-way -- NOT a reported command
    failure), a `loadbin` that did not say `O.K.` (a STALE image already in
    ITCM could otherwise boot and be read back as this run's), or a rejected
    `setpc`. `None` when the transcript is clean."""
    for marker in _CONNECT_FAILURES:
        if marker in transcript:
            return f"J-Link reported `{marker}`"
    if "Script processing completed." not in transcript:
        return (
            "the J-Link transcript has no 'Script processing completed.' line -- the "
            "process crashed, was killed or was truncated before finishing"
        )
    if loadbin:
        window = None
        for line in transcript.splitlines():
            if line.startswith("J-Link>loadbin "):
                window = _window(transcript, line[len("J-Link>") :])
                break
        if window is not None and "O.K." not in window.split():
            return "loadbin did not report 'O.K.' -- refusing to treat this as a fresh load"
        for line in transcript.splitlines():
            if line.startswith("J-Link>setpc "):
                bad = _window(transcript, line[len("J-Link>") :])
                if bad and bad.strip():
                    return f"setpc was rejected: {bad.strip()}"
                break
    return None
