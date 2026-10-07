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
CODE_CORE_UNSUPPORTED = "flash.ram-core-unsupported"
CODE_CORE_MISMATCH = "flash.ram-core-mismatch"
CODE_CORE_UNCONFIRMED = "flash.ram-core-unconfirmed"

#: The RAM console buffer symbol (`CONFIG_RAM_CONSOLE`).
CONSOLE_SYMBOL = "ram_console_buf"

#: Largest region one `mem8` command reads (bench-env.sh `bench_mem8_chunks`).
MEM8_CHUNK = 65536
#: Upper bound on the console read, so a corrupt symbol size cannot ask for MiBs.
MAX_CONSOLE_BYTES = 64 * 1024
#: ELF reader bounds: a symbol table this large, or one whose entries are smaller than
#: an Elf32_Sym, is not a Zephyr image and is not trusted for an address.
MAX_SYMBOLS = 200_000
MAX_SECTIONS = 4096
ELF32_SYM_SIZE = 16
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
    if shnum > MAX_SECTIONS:
        raise RamRunError(f"the ELF declares {shnum} sections; refusing to parse it")
    headers = [
        struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)
    ]
    for _name, sh_type, _fl, _addr, sh_off, sh_size, sh_link, _info, _align, sh_entsize in headers:
        if sh_type != 2:  # SHT_SYMTAB
            continue
        if sh_entsize < ELF32_SYM_SIZE:
            raise RamRunError(
                f"the ELF symbol table declares an entry size of {sh_entsize} B "
                f"(an Elf32_Sym is {ELF32_SYM_SIZE}); refusing to read symbols from it"
            )
        if sh_size // sh_entsize > MAX_SYMBOLS:
            raise RamRunError(
                f"the ELF symbol table has {sh_size // sh_entsize} entries (cap {MAX_SYMBOLS})"
            )
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


#: The cores tan will RAM-run. The HP core's debug AP selection (AP 0x00200000 vs the
#: HE's 0x00300000 on evk-01) is not bench-proven through tan, so a slice other than
#: the HE core is refused rather than risking loading HE's recipe into the wrong core.
SUPPORTED_CORE = "M55_HE"

#: Per-core ITCM global aliases (the recipe's load bases): HE 0x58000000, HP 0x50000000.
_ITCM_GLOBAL = {"M55_HE": 0x58000000, "M55_HP": 0x50000000}


@dataclass(frozen=True)
class Apertures:
    """Where a core's code and data may live, with SIZES from the SoC metadata's
    `sram_banks_kb` (never invented): `code` / `data` are `(base, size)` lists."""

    code: tuple[tuple[int, int], ...]
    data: tuple[tuple[int, int], ...]


def check_core(core_id: str, base: int) -> None:
    """Refuse a slice that is not the HE core (`flash.ram-core-unsupported`), and a
    base that is the OTHER core's ITCM alias (`flash.ram-core-mismatch`): an image
    linked for the HP core's 0x50000000 aperture on the HE slice would be loaded into
    whichever core the probe happens to be attached to."""
    core = core_id.upper()
    if core != SUPPORTED_CORE:
        raise RamRunError(
            f"tan flash --ram supports only the M55 HE core for now; '{core_id}' is not "
            "it. The HP core's debug access port selection is not bench-proven through "
            "tan, and loading the HE recipe into the wrong core is not recoverable by a "
            "retry. Use the raw alp-sdk helper for the HP core.",
            CODE_CORE_UNSUPPORTED,
        )
    for other, alias in _ITCM_GLOBAL.items():
        if other != core and base == alias:
            raise RamRunError(
                f"the image is linked for {other}'s ITCM alias 0x{alias:X} but the slice is "
                f"{core}: it would run on whichever core the probe is attached to. Rebuild "
                "it for the HE core.",
                CODE_CORE_MISMATCH,
            )


def apertures_for(core_id: str, banks_kib: Sequence[tuple[str, float]]) -> Apertures | None:
    """The HE core's apertures from a SoC variant's `sram_banks_kb` (`(name, KiB)`):
    its own ITCM bank at the local `0x0` and the global `0x58000000` alias, its DTCM
    bank at the architectural local `0x20000000`, and `SRAM0` at `0x02000000`.
    `None` when the metadata names no ITCM/DTCM bank for the core -- the caller then
    REFUSES; a size is never guessed."""
    token = core_id.upper()
    itcm = dtcm = sram0 = None
    for name, kib in banks_kib:
        up = name.upper()
        size = int(kib * 1024)
        if token in up and "ITCM" in up and itcm is None:
            itcm = size
        elif token in up and "DTCM" in up and dtcm is None:
            dtcm = size
        elif up == "SRAM0" and sram0 is None:
            sram0 = size
    if itcm is None or dtcm is None or token not in _ITCM_GLOBAL:
        return None
    code = [(0x0, itcm), (_ITCM_GLOBAL[token], itcm)]
    data = [(0x20000000, dtcm)]
    if sram0 is not None:
        code.append((0x02000000, sram0))
        data.append((0x02000000, sram0))
    return Apertures(tuple(code), tuple(data))


def _within(address: int, length: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(base <= address and address + length <= base + size for base, size in spans)


def _plausible_base(base: int) -> bool:
    return base in (0x0, 0x50000000, 0x58000000) or 0x02000000 <= base < 0x03000000


def plan_ram_image(
    elf: ElfImage,
    binary: bytes,
    *,
    core_id: str | None = None,
    apertures: Apertures | None = None,
) -> RamImage:
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
    if core_id is not None:
        check_core(core_id, base)
    if len(binary) < 8:
        raise RamRunError("zephyr.bin is too small to hold a vector table")
    if apertures is not None and not _within(base, len(binary), apertures.code):
        raise RamRunError(
            f"the {len(binary)} B image at 0x{base:X} does not fit any of the core's code "
            "apertures (" + ", ".join(f"0x{b:X}+0x{n:X}" for b, n in apertures.code) + "; sizes "
            "from the SoC metadata) -- loading it would run past the TCM into live memory.",
            CODE_NOT_RAM_LINKED,
        )
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
        length = min(size or DEFAULT_CONSOLE_BYTES, MAX_CONSOLE_BYTES)
        if apertures is not None and not _within(addr, length, apertures.data):
            raise RamRunError(
                f"{CONSOLE_SYMBOL} (0x{addr:X}+0x{length:X}) is outside the core's DTCM/SRAM "
                "apertures (" + ", ".join(f"0x{b:X}+0x{n:X}" for b, n in apertures.data)
                + ") -- refusing to read an address the ELF should not have put there."
            )
        console = (addr, length)
    return RamImage(base, entry, sp, reset, len(binary), console)


# ── J-Link scripts ──────────────────────────────────────────────────────────


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def preamble(serial: str | None, speed: int, device: str) -> list[str]:
    """Everything up to and including `connect`, shared by both sessions. `serial` and
    `device` must be plain tokens and `speed` an int, or this refuses -- they are
    the only free-form values in either script."""
    if not isinstance(speed, int) or isinstance(speed, bool) or speed <= 0:
        raise RamRunError(f"J-Link speed {speed!r} is not a positive integer")
    for label, value in (("serial", serial), ("device", device)):
        if value is not None and not _SAFE_TOKEN.match(value):
            raise RamRunError(f"the J-Link {label} {value!r} is not a plain token")
    lines = [f"SelectEmuBySN {serial}"] if serial else []
    return [*lines, "si SWD", f"speed {speed}", f"device {device}", "connect"]


def load_script(pre: Sequence[str], binary_path: str, image: RamImage) -> str:
    # Every address below is an `int` rendered with `0x%X`, never a string; the
    # caller passes only a path it staged itself (and already validated).
    """`connect; halt; loadbin; setpc; go` -- the proven Flow C load session.
    `loadbin` resets the core and re-reads the vector table (SP); `setpc` enters
    the reset handler. No MRAM address appears anywhere."""
    if any(c in binary_path for c in "\r\n\0"):
        raise RamRunError("the image path carries a control character")
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
    failure), a `loadbin` that did not say `O.K.`, or a rejected `setpc`/`go`.

    For the LOAD session (`loadbin=True`) the transcript must PROVE the load: the
    `loadbin` echo with an `O.K.` after it, a `setpc` echo with nothing but blank
    lines after it, and a `go` echo with at most the memory-map banner after it. A
    transcript that does not echo them cannot confirm the load -- a STALE image
    already in ITCM could otherwise boot and be read back as this run's -- so it is
    refused ("cannot confirm the load"). `None` when the transcript is clean."""
    for marker in _CONNECT_FAILURES:
        if marker in transcript:
            return f"J-Link reported `{marker}`"
    if "Script processing completed." not in transcript:
        return (
            "the J-Link transcript has no 'Script processing completed.' line -- the "
            "process crashed, was killed or was truncated before finishing"
        )
    if not loadbin:
        return None
    echoes = {
        word: next(
            (ln[len("J-Link>") :] for ln in transcript.splitlines() if ln.startswith(f"J-Link>{word} ")
             or ln.strip() == f"J-Link>{word}"),
            None,
        )
        for word in ("loadbin", "setpc", "go")
    }
    missing = [w for w, e in echoes.items() if e is None]
    if missing:
        return (
            "cannot confirm the load: the J-Link transcript has no echo of "
            + ", ".join(missing) + " (expected `J-Link>loadbin ...`, `J-Link>setpc ...`, `J-Link>go`)"
        )
    load = _window(transcript, echoes["loadbin"])
    if load is None or "O.K." not in load.split():
        return "loadbin did not report 'O.K.' -- refusing to treat this as a fresh load"
    bad = _window(transcript, echoes["setpc"])
    if bad and bad.strip():
        return f"setpc was rejected: {bad.strip()}"
    go = _window(transcript, echoes["go"]) or ""
    if any(
        ln.strip()
        and ln.strip() != "Script processing completed."
        and not re.fullmatch(r"Memory map '.*' is active", ln.strip())
        for ln in go.splitlines()
    ):
        return f"go was rejected: {go.strip()}"
    return None


_AP_ADDR = re.compile(r"AP\[(\d+)\]\s*\(APAddr\s+(0x[0-9A-Fa-f]+)\)")
_AP_CORE_FOUND = re.compile(r"AP\[(\d+)\]:\s*Core found")
_CPUID = re.compile(r"CPUID register:\s*(0x[0-9A-Fa-f]+)")
_FOUND = re.compile(r"^Found Cortex-M\S*.*$", re.MULTILINE)

#: The debug access ports of the two M55s on the Ensemble E8, from the bench: HE AP
#: 0x00300000, HP AP 0x00200000 (alp-sdk scripts/bench/aen/openocd-ram-run.sh:16-17;
#: changelog.d/2037-openocd-m55he-bench-core-selection.md:4 "0x00200000 is the M55-HP,
#: not the HE, and 0x00300000 is the HE"; changelog.d/2025.md:31 `AP[3] (APAddr
#: 0x00300000)` is the AHB-AP carrying the M55 debug).
HE_AP_ADDR = 0x00300000
HP_AP_ADDR = 0x00200000


def attached_core(transcript: str) -> dict | None:
    """Which core J-Link attached to, from its own `connect` banner: the AP that reports
    `AP[n]: Core found` (with its `APAddr`), the `CPUID register` and the `Found
    Cortex-M55 ...` line. The `APAddr` of the Core-found AP is what tells HE from HP --
    the `Found Cortex-M55 r1p0` line alone is identical for both. `None` when the
    transcript names no Core-found AP. More than one distinct Core-found AP is
    reported as `multiple`."""
    addrs = {int(m.group(1)): m.group(2) for m in _AP_ADDR.finditer(transcript)}
    found = [int(m.group(1)) for m in _AP_CORE_FOUND.finditer(transcript)]
    cpuid = _CPUID.search(transcript)
    line = _FOUND.search(transcript)
    if not found and not cpuid and not line:
        return None
    ports = sorted(set(found))
    out: dict = {
        "coreFoundAp": ports[0] if len(ports) == 1 else (ports or None),
        "apAddr": addrs.get(ports[0]) if len(ports) == 1 else None,
        "cpuid": cpuid.group(1) if cpuid else None,
        "found": line.group(0).strip() if line else None,
    }
    if len(ports) > 1:
        out["multiple"] = [addrs.get(p) for p in ports]
    return out


def ap_verdict(attached: dict | None) -> str:
    """`he` / `hp` from the Core-found AP's address; `multiple` / `unidentified` otherwise."""
    if not attached:
        return "unidentified"
    if "multiple" in attached:
        return "multiple"
    try:
        addr = int(attached["apAddr"], 16)
    except (TypeError, ValueError):
        return "unidentified"
    return {HE_AP_ADDR: "he", HP_AP_ADDR: "hp"}.get(addr, "unidentified")


def combine_verdicts(ap: str, itcm: str) -> str:
    """The AP is the PRIMARY identification; the ITCM-alias read corroborates. Agreement
    or silence from the secondary keeps the primary; a contradiction is `conflict`. With
    no usable AP the ITCM verdict stands alone."""
    if ap in ("he", "hp"):
        other = "hp" if ap == "he" else "he"
        return "conflict" if itcm == other else ap
    return itcm


# ── which core did the generic attach land on? (tan-cli#1354) ───────────────
#
# J-Link's "Found Cortex-M55 r1p0" is the same line for the HE and the HP core, and a
# generic `Cortex-M55` attach takes whichever M55 access port it finds. Each core's
# LOCAL ITCM at 0x0 is the same memory as its own GLOBAL window -- HE 0x58000000, HP
# 0x50000000 (alp-sdk metadata/socs/alif/ensemble/e8.json: `itcm_global_base` 1476395008
# at line 106 for m55_he, 1342177280 at line 91 for m55_hp; docs/aen-bench-bringup.md:20
# "`loadAddress=0x50000000` = HP ITCM global, vs HE's `0x58000000`"). So reading 4 words
# at the local 0x0 and at both globals identifies the attached core by which window the
# local view equals -- three READS, no writes, no halt.

HE_ALIAS = _ITCM_GLOBAL["M55_HE"]
HP_ALIAS = _ITCM_GLOBAL["M55_HP"]
CORE_CHECK_WORDS = 4

_MEM32_LINE = re.compile(r"^([0-9A-Fa-f]{8}) = ((?:[0-9A-Fa-f]{8}(?:\s+|$))+)$")


def core_check_script(pre: Sequence[str]) -> str:
    """The read-only identification session: `connect` (no halt, no loadbin), then
    `mem32` of the local ITCM and of each core's global alias. Addresses and the word
    count are ints rendered with `0x%X`."""
    reads = [f"mem32 0x{a:X}, 0x{CORE_CHECK_WORDS:X}" for a in (0x0, HE_ALIAS, HP_ALIAS)]
    return "\n".join([*pre, *reads, "exit"]) + "\n"


def parse_mem32(transcript: str, address: int, count: int = CORE_CHECK_WORDS) -> tuple[int, ...] | None:
    """The `count` 32-bit words J-Link dumped at `address`, or `None` when the dump is
    missing, short, or not contiguous from `address` (an unreadable window prints
    `Could not read memory.` and no dump line). Strict: a line that does not match
    `ADDR = WWWWWWWW ...` is ignored, never guessed at."""
    words: dict[int, int] = {}
    for raw in transcript.splitlines():
        match = _MEM32_LINE.match(raw.strip())
        if not match:
            continue
        base = int(match.group(1), 16)
        for i, tok in enumerate(match.group(2).split()):
            words.setdefault(base + 4 * i, int(tok, 16))
    wanted = [address + 4 * i for i in range(count)]
    if not all(a in words for a in wanted):
        return None
    return tuple(words[a] for a in wanted)


def core_verdict(
    local: tuple[int, ...] | None,
    he: tuple[int, ...] | None,
    hp: tuple[int, ...] | None,
) -> str:
    """`he` / `hp` when the local ITCM view equals EXACTLY one core's global window;
    `ambiguous` when it equals both (identical or erased content proves nothing);
    `no-match` when it equals neither; `unreadable` when the local view could not be
    read."""
    if local is None:
        return "unreadable"
    is_he = he is not None and local == he
    is_hp = hp is not None and local == hp
    if is_he and is_hp:
        return "ambiguous"
    if is_he:
        return "he"
    if is_hp:
        return "hp"
    return "no-match"


def core_check(transcript: str) -> tuple[str, dict]:
    """`(verdict, evidence)` for a [`core_check_script`] transcript: the Core-found AP
    (primary) combined with the ITCM-alias words (corroboration). `evidence` is the
    envelope's `ram.coreCheck` body."""
    local = parse_mem32(transcript, 0x0)
    he = parse_mem32(transcript, HE_ALIAS)
    hp = parse_mem32(transcript, HP_ALIAS)

    def hexed(words):
        return None if words is None else [f"0x{w:08X}" for w in words]

    attached = attached_core(transcript)
    ap = ap_verdict(attached)
    itcm = core_verdict(local, he, hp)
    evidence = {
        "ap": attached,
        "apVerdict": ap,
        "itcmWords": {
            "local0x00000000": hexed(local),
            f"he0x{HE_ALIAS:08X}": hexed(he),
            f"hp0x{HP_ALIAS:08X}": hexed(hp),
        },
        "itcmVerdict": itcm,
    }
    return combine_verdicts(ap, itcm), evidence
