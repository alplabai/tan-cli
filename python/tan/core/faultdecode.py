# SPDX-License-Identifier: Apache-2.0
"""Pure decode core for `tan faultdecode` -- ARM Cortex-M (ARMv8-M) fault
register arithmetic, register dump parsing, and rendering. No I/O.

Port of `alp_cli/faultdecode.py` (alp-sdk `scripts/alp_cli/faultdecode.py`),
minus its CLI plumbing and its `resolve_symbol` subprocess call, which live in
`tan.commands.faultdecode_cmd` -- this module stays pure so it unit-tests
without a process or a filesystem, matching the `tan/core/` convention (pure
logic; `tan/commands/*_cmd.py` owns IO/executor concerns).

**Register-level fidelity is the whole value of this port.** Every bit table
entry (register, bit, flag name, meaning) and every `_root_cause` branch below
is TRANSCRIBED verbatim from the SDK original, not retyped from memory --
a shifted bit or a dropped mask produces a confident wrong diagnosis of a
customer's crash. References: ARMv8-M Architecture Reference Manual, SCB
CFSR/HFSR/DFSR.

Targets the ARMv8-M fault model shared by the SoMs alp-sdk drives: Cortex-M55
(ARMv8.1-M, AEN `m55_hp`/`m55_he`) and Cortex-M33 (ARMv8-M, V2N CM33). These
are ARM ARCHITECTURAL register definitions -- CFSR/HFSR/DFSR bit layout is
fixed by the ARMv8-M Architecture Reference Manual for every Cortex-M55/M33
part in existence, not an Alp Lab SoM/vendor fact recorded once under
alp-sdk's `metadata/**`. Nothing here is keyed on a SKU, a part number, an I2C
address or a pin name, so invariant I-26 (ADR-0017: no SoM hardware fact
outside `metadata/**`) does not reach it -- there is no metadata source for
"what CFSR bit 9 means on any Cortex-M", the same way there is none for
"what a C `for` loop does".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# -------- bit tables ----------------------------------------------------------
#
# Each entry is (bit, flag-name, one-line plain-English meaning). Bit numbers
# for MMFSR/BFSR/UFSR are absolute positions inside the 32-bit CFSR word, so the
# same mask logic works whether the caller supplied a combined CFSR or the three
# sub-registers separately. References: ARMv8-M Architecture Reference Manual,
# SCB CFSR/HFSR/DFSR.

# MemManage Fault Status (CFSR bits 0-7).
MMFSR_BITS: tuple[tuple[int, str, str], ...] = (
    (0, "IACCVIOL", "Instruction access violation -- the MPU blocked an instruction fetch "
                    "(executing from a no-execute / unprivileged region)."),
    (1, "DACCVIOL", "Data access violation -- the MPU blocked a load/store; MMFAR holds the address."),
    (3, "MUNSTKERR", "MemManage fault unstacking on exception return -- the exception frame sits "
                     "in MPU-protected memory (often a corrupted/overflowed stack)."),
    (4, "MSTKERR", "MemManage fault stacking on exception entry -- the stack pointer points into "
                   "MPU-protected/invalid memory (a bad or overflowed stack)."),
    (5, "MLSPERR", "MemManage fault during lazy floating-point state preservation."),
    (7, "MMARVALID", "MMFAR holds a valid faulting data address (see below)."),
)

# BusFault Status (CFSR bits 8-15).
BFSR_BITS: tuple[tuple[int, str, str], ...] = (
    (8, "IBUSERR", "Instruction bus error -- a prefetch faulted, usually a branch/call through a "
                   "bad function pointer into unmapped memory."),
    (9, "PRECISERR", "Precise data bus error -- a load/store faulted and BFAR holds the exact "
                     "faulting address (commonly an unclocked/absent peripheral or a bad pointer)."),
    (10, "IMPRECISERR", "Imprecise data bus error -- a buffered/late write faulted; the PC has moved "
                        "on, so BFAR is not reliable (look for an earlier bad store)."),
    (11, "UNSTKERR", "Bus fault unstacking on exception return -- a corrupted stack pointer."),
    (12, "STKERR", "Bus fault stacking on exception entry -- a bad or overflowed stack pointer."),
    (13, "LSPERR", "Bus fault during lazy floating-point state preservation."),
    (15, "BFARVALID", "BFAR holds a valid faulting data address (see below)."),
)

# UsageFault Status (CFSR bits 16-31).
UFSR_BITS: tuple[tuple[int, str, str], ...] = (
    (16, "UNDEFINSTR", "Undefined instruction -- executed a bad/corrupted opcode (often a wild PC "
                       "or jumping into data)."),
    (17, "INVSTATE", "Invalid state -- EPSR.T cleared or illegal IT state, classically a function "
                     "pointer called without the Thumb bit (bit 0) set."),
    (18, "INVPC", "Invalid PC on exception return -- a bad EXC_RETURN or a corrupted stacked PC."),
    (19, "NOCP", "No coprocessor -- access to a disabled/absent coprocessor, most often the FPU "
                 "used before CPACR enables it."),
    (20, "STKOF", "Stack overflow -- the hardware stack-limit (PSPLIM/MSPLIM) tripped (ARMv8-M)."),
    (24, "UNALIGNED", "Unaligned access -- an unaligned load/store while alignment trapping is on "
                      "(or an LDM/STM/LDRD that must be aligned)."),
    (25, "DIVBYZERO", "Divide by zero -- SDIV/UDIV by zero with DIV_0_TRP enabled."),
)

# HardFault Status (HFSR).
HFSR_BITS: tuple[tuple[int, str, str], ...] = (
    (1, "VECTTBL", "Vector-table read fault -- a bus error reading an exception vector."),
    (30, "FORCED", "Forced HardFault -- a configurable fault (MemManage/BusFault/UsageFault) was "
                   "escalated; the real cause is in CFSR above."),
    (31, "DEBUGEVT", "Debug event -- a breakpoint/watchpoint fired with no debugger attached."),
)

# Debug Fault Status (DFSR) -- optional, informational.
DFSR_BITS: tuple[tuple[int, str, str], ...] = (
    (0, "HALTED", "Halt request (debugger single-step / halt)."),
    (1, "BKPT", "Breakpoint -- a BKPT instruction or hardware breakpoint."),
    (2, "DWTTRAP", "DWT watchpoint / debug-monitor trap."),
    (3, "VCATCH", "Vector catch triggered."),
    (4, "EXTERNAL", "External debug request (EDBGRQ)."),
)


# -------- result types --------------------------------------------------------


@dataclass(slots=True)
class DecodedFlag:
    """One set status-register bit, with its register and plain-English meaning."""

    reg: str  # MMFSR | BFSR | UFSR | HFSR | DFSR
    name: str
    bit: int  # absolute bit position within its register word
    meaning: str


@dataclass(slots=True)
class FaultReport:
    """The pure decode result: set flags, faulting addresses, and a root cause."""

    flags: list[DecodedFlag] = field(default_factory=list)
    bfar: int | None = None
    bfar_valid: bool = False
    mmfar: int | None = None
    mmfar_valid: bool = False
    root_cause: str = ""
    inputs: dict[str, int] = field(default_factory=dict)

    @property
    def fault_detected(self) -> bool:
        return bool(self.flags)

    def has(self, name: str) -> bool:
        return any(f.name == name for f in self.flags)


@dataclass(slots=True)
class Symbol:
    addr: int
    func: str
    location: str  # file:line, or "??:?" when unknown


# -------- the pure decode core ------------------------------------------------


def _scan(value: int, table: tuple[tuple[int, str, str], ...], reg: str) -> list[DecodedFlag]:
    return [
        DecodedFlag(reg=reg, name=name, bit=bit, meaning=meaning)
        for bit, name, meaning in table
        if value & (1 << bit)
    ]


def decode(
    *,
    cfsr: int = 0,
    hfsr: int = 0,
    dfsr: int = 0,
    bfar: int | None = None,
    mmfar: int | None = None,
) -> FaultReport:
    """Decode ARMv8-M fault registers into a :class:`FaultReport`.

    Pure function -- no I/O, no shelling out -- so it is trivially unit-testable.
    ``cfsr`` carries MMFSR (bits 0-7), BFSR (bits 8-15) and UFSR (bits 16-31).
    ``bfar``/``mmfar`` are only treated as authoritative when the matching VALID
    bit is set in CFSR; an address supplied without its VALID bit is reported but
    flagged as possibly stale.
    """
    report = FaultReport()
    report.inputs = {
        "cfsr": cfsr,
        "hfsr": hfsr,
        "dfsr": dfsr,
        **({"bfar": bfar} if bfar is not None else {}),
        **({"mmfar": mmfar} if mmfar is not None else {}),
    }

    report.flags.extend(_scan(cfsr, MMFSR_BITS, "MMFSR"))
    report.flags.extend(_scan(cfsr, BFSR_BITS, "BFSR"))
    report.flags.extend(_scan(cfsr, UFSR_BITS, "UFSR"))
    report.flags.extend(_scan(hfsr, HFSR_BITS, "HFSR"))
    report.flags.extend(_scan(dfsr, DFSR_BITS, "DFSR"))

    report.bfar_valid = report.has("BFARVALID")
    report.mmfar_valid = report.has("MMARVALID")
    report.bfar = bfar
    report.mmfar = mmfar

    report.root_cause = _root_cause(report)
    return report


def _addr_phrase(report: FaultReport) -> str:
    if report.bfar_valid and report.bfar is not None:
        return f" at 0x{report.bfar:08x} (BFAR)"
    if report.mmfar_valid and report.mmfar is not None:
        return f" at 0x{report.mmfar:08x} (MMFAR)"
    return ""


def _root_cause(report: FaultReport) -> str:
    """Pick the single most likely root cause from the set flags.

    Ordered most-specific-first: a precise address or a stack-overflow trap tells
    you far more than the generic "forced HardFault" escalation bit, so those win.
    """
    if not report.flags:
        return "No fault status bits are set -- nothing to decode."

    addr = _addr_phrase(report)

    if report.has("STKOF"):
        return ("Stack overflow: the ARMv8-M stack-limit register (PSPLIM/MSPLIM) tripped. "
                "Grow the offending thread/ISR stack or fix unbounded recursion / large stack buffers.")
    if report.has("PRECISERR"):
        return (f"Precise data bus fault{addr or ''} -- a load/store hit a faulting address, "
                "commonly an access to an unclocked/absent peripheral or a bad/dangling pointer.")
    if report.has("IMPRECISERR"):
        return ("Imprecise data bus fault -- a buffered write faulted after the CPU moved on, so the "
                "PC/BFAR do not pinpoint it. Suspect an earlier bad store; a DSB after suspect writes "
                "makes it precise.")
    if report.has("DACCVIOL"):
        return (f"MPU data access violation{addr or ''} -- a load/store hit a region the MPU forbids "
                "(wrong permissions, or an unmapped/unprivileged address).")
    if report.has("IACCVIOL"):
        return ("MPU instruction access violation -- the core tried to execute from a no-execute / "
                "forbidden region (often a corrupted PC or a bad function pointer).")
    if report.has("IBUSERR"):
        return ("Instruction bus fault -- a fetch faulted, typically a branch/call through a bad "
                "function pointer into unmapped memory.")
    if report.has("MSTKERR") or report.has("STKERR"):
        return ("Fault while stacking the exception frame on entry -- the stack pointer is bad or "
                "overflowed (check SP/PSPLIM and the offending stack's size).")
    if report.has("MUNSTKERR") or report.has("UNSTKERR"):
        return ("Fault while unstacking on exception return -- the saved exception frame is corrupted "
                "(a stack overwrite or a clobbered SP).")
    if report.has("DIVBYZERO"):
        return ("Divide by zero -- SDIV/UDIV with a zero divisor and DIV_0_TRP enabled. Guard the "
                "divisor or disable the trap.")
    if report.has("UNALIGNED"):
        return ("Unaligned access fault -- an unaligned load/store with alignment trapping on (or an "
                "LDM/STM/LDRD that requires alignment). Fix the pointer alignment or use packed access.")
    if report.has("INVSTATE"):
        return ("Invalid state (EPSR.T / IT) -- almost always a function pointer called without the "
                "Thumb bit (bit 0) set, or a corrupted PSR.")
    if report.has("INVPC"):
        return ("Invalid PC on exception return -- a bad EXC_RETURN value or a corrupted stacked PC "
                "(stack overflow / FNC return into a clobbered frame).")
    if report.has("NOCP"):
        return ("No-coprocessor fault -- code used a coprocessor that is disabled, most often the FPU "
                "before CPACR (CP10/CP11) enables it. Enable the FPU in CPACR / Kconfig.")
    if report.has("UNDEFINSTR"):
        return ("Undefined instruction -- a corrupted/wild PC executed a bad opcode, or code was "
                "built for a different ISA than the running core.")
    if report.has("VECTTBL"):
        return ("Vector-table read fault -- a bus error reading an exception vector (VTOR points at "
                "bad memory, or the vector table is unmapped).")
    if report.has("DEBUGEVT"):
        return ("Debug event with no debugger attached -- a stray BKPT or a watchpoint firing in a "
                "free-running build.")
    if report.has("FORCED"):
        return ("Forced HardFault -- a configurable fault escalated but its own status bits are clear; "
                "the escalation usually means faults are disabled (SHCSR) or it faulted at priority -1.")

    first = report.flags[0]
    return f"{first.name} set ({first.reg}): {first.meaning}"


# -------- dump parsing --------------------------------------------------------
#
# Register tokens we will grep out of a pasted dump. Maps the token (matched
# case-insensitively) to a canonical key. Sub-registers (mmfsr/bfsr/ufsr) get
# composed back into CFSR. Order matters only for the regex alternation; the
# longer tokens are listed first so e.g. "mmfar" is not eaten by a shorter name.

_DUMP_TOKENS: tuple[tuple[str, str], ...] = (
    ("cfsr", "cfsr"),
    ("hfsr", "hfsr"),
    ("dfsr", "dfsr"),
    ("mmfar", "mmfar"),
    ("bfar", "bfar"),
    ("mmfsr", "mmfsr"),
    ("bfsr", "bfsr"),
    ("ufsr", "ufsr"),
    ("pc", "pc"),
    ("lr", "lr"),
)

# token, optional same-line noise (e.g. "Address:"), then a 0x or bare-hex value.
# The noise gap allows any non-newline chars (non-greedy) so hex-letter words
# like "Address" between the name and its value are skipped; the value still has
# to be a 0x literal or a word-bounded hex run, so it cannot land mid-word.
_DUMP_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(t) for t, _ in _DUMP_TOKENS) + r")\b"
    r"[^\r\n]{0,24}?"
    r"(0x[0-9A-Fa-f]+|\b[0-9A-Fa-f]{2,8}\b)"
)


def parse_dump(text: str) -> dict[str, int]:
    """Grep known register names + values out of a pasted fault dump.

    Recognises ``CFSR``/``HFSR``/``DFSR``/``BFAR``/``MMFAR`` plus the split
    ``MMFSR``/``BFSR``/``UFSR`` (composed back into CFSR) and ``PC``/``LR``.
    Accepts ``NAME: 0x..`` / ``NAME = 0x..`` / ``MMFAR Address: 0x..`` shapes and
    bare hex. Last occurrence of a token wins.
    """
    canon = dict(_DUMP_TOKENS)
    found: dict[str, int] = {}
    for m in _DUMP_RE.finditer(text):
        key = canon[m.group(1).lower()]
        raw = m.group(2)
        try:
            found[key] = int(raw, 16)
        except ValueError:  # pragma: no cover - regex already constrains this
            continue

    # Compose CFSR from sub-registers if a combined CFSR was not given outright.
    if "cfsr" not in found:
        composed = 0
        if "mmfsr" in found:
            composed |= found["mmfsr"] & 0xFF
        if "bfsr" in found:
            composed |= (found["bfsr"] & 0xFF) << 8
        if "ufsr" in found:
            composed |= (found["ufsr"] & 0xFFFF) << 16
        if composed:
            found["cfsr"] = composed
    for k in ("mmfsr", "bfsr", "ufsr"):
        found.pop(k, None)
    return found


# -------- rendering -----------------------------------------------------------
#
# Raw ANSI SGR codes, matching `tan.core.size`'s convention rather than pulling
# in colorama (not a dependency of this package -- see pyproject.toml's
# four/five-name ceiling). Values are colorama's `Fore`/`Style` equivalents for
# the exact hues the original used.
_MAGENTA = "\x1b[35m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_WHITE = "\x1b[37m"
_BRIGHT = "\x1b[1m"
_RESET = "\x1b[0m"

_REG_HUE = {
    "MMFSR": _MAGENTA,
    "BFSR": _RED,
    "UFSR": _YELLOW,
    "HFSR": _RED,
    "DFSR": _CYAN,
}


def _paint(s: str, hue: str, color: bool) -> str:
    return f"{hue}{s}{_RESET}" if color else s


def render_human(
    report: FaultReport,
    symbols: dict[str, Symbol] | None,
    color: bool,
) -> str:
    lines: list[str] = []
    head = "ARM Cortex-M (ARMv8-M) fault decode"
    lines.append(_paint(head, _CYAN + _BRIGHT, color))

    # Echo the registers we actually decoded.
    reg_bits = [f"{k.upper()}=0x{v:08x}" for k, v in report.inputs.items()]
    lines.append("  " + "  ".join(reg_bits))
    lines.append("")

    if not report.fault_detected:
        lines.append(_paint("  No fault flags set.", _GREEN, color))
        lines.append("  " + report.root_cause)
        return "\n".join(lines)

    lines.append(_paint("Set flags:", _WHITE + _BRIGHT, color))
    for f in report.flags:
        tag = _paint(f"[{f.reg}] {f.name}", _REG_HUE.get(f.reg, ""), color)
        lines.append(f"  {tag} (bit {f.bit}): {f.meaning}")

    # Faulting addresses.
    if report.bfar is not None:
        note = "" if report.bfar_valid else "  (BFARVALID not set -- address may be stale)"
        lines.append(f"  Faulting address (BFAR): 0x{report.bfar:08x}{note}")
    if report.mmfar is not None:
        note = "" if report.mmfar_valid else "  (MMARVALID not set -- address may be stale)"
        lines.append(f"  Faulting address (MMFAR): 0x{report.mmfar:08x}{note}")

    lines.append("")
    lines.append(_paint("Most likely cause:", _WHITE + _BRIGHT, color))
    lines.append("  " + report.root_cause)

    if symbols:
        lines.append("")
        lines.append(_paint("Symbolication:", _WHITE + _BRIGHT, color))
        for which, sym in symbols.items():
            lines.append(
                f"  {which.upper()} 0x{sym.addr:08x} -> {_paint(sym.func, _GREEN, color)} "
                f"({sym.location})"
            )

    return "\n".join(lines)


def report_to_json(report: FaultReport, symbols: dict[str, Symbol] | None) -> dict:
    """Machine-readable shape for the extension's troubleshooting panel."""
    return {
        "fault_detected": report.fault_detected,
        "inputs": {k: f"0x{v:08x}" for k, v in report.inputs.items()},
        "flags": [
            {"reg": f.reg, "name": f.name, "bit": f.bit, "meaning": f.meaning}
            for f in report.flags
        ],
        "addresses": {
            "bfar": None if report.bfar is None else f"0x{report.bfar:08x}",
            "bfar_valid": report.bfar_valid,
            "mmfar": None if report.mmfar is None else f"0x{report.mmfar:08x}",
            "mmfar_valid": report.mmfar_valid,
        },
        "root_cause": report.root_cause,
        "symbols": (
            None
            if not symbols
            else {
                which: {"addr": f"0x{s.addr:08x}", "func": s.func, "location": s.location}
                for which, s in symbols.items()
            }
        ),
    }
