# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1406: the pure half of `tan probe` -- argument parsing, the region
safety rule and the read-only J-Link Commander scripts.

`tan probe` exists so the bench never needs raw `JLinkExe` for two READ-ONLY
questions: "which SW-DP / core is attached?" and "what do these words read?".
Every script built here is `connect` plus `mem32` reads and `exit`; nothing in
this module can emit a write, erase, `loadbin`, `setpc`, `go`, reset or
`w1`/`w2`/`w4` command, and `FORBIDDEN_VERBS` is what the tests hold the
generated scripts to.
"""
from __future__ import annotations

import re
from typing import Sequence

from tan.core.ram_run import core_check_script, preamble

CODE_BAD_ARGUMENT = "probe.bad-argument"
CODE_READ_TOO_LARGE = "probe.read-too-large"
CODE_READ_UNSAFE_REGION = "probe.read-unsafe-region"
CODE_READ_FAILED = "probe.read-failed"
CODE_FAILED = "probe.failed"
CODE_DPIDR_MISMATCH = "probe.dpidr-mismatch"
CODE_DPIDR_UNREAD = "probe.dpidr-unread"

#: Most words one `probe read` returns.
MAX_WORDS = 256
DEFAULT_WORDS = 4
#: The generic Cortex-M55 attach profile (the --ram attach check's own).
DEFAULT_DEVICE = "Cortex-M55"
CORES = ("m55_he", "m55_hp")

#: The window a session attached to the M55-HE must never touch: reading the HP
#: ITCM global alias from an HE attach leaves the core unhaltable until a PIN reset
#: (bench round 8, tan-cli#1354).
UNSAFE_WINDOW = (0x50000000, 0x5FFFFFFF)

#: Commander verbs a `probe` script must never contain (the tests scan for them).
FORBIDDEN_VERBS = (
    "w1", "w2", "w4", "w8", "write", "erase", "loadbin", "loadfile", "setpc", "go", "g",
    "reset", "r", "rx", "halt", "h", "unlock", "wreg", "savebin", "setbp", "step", "s",
)

_HEX = re.compile(r"^0[xX][0-9A-Fa-f]{1,8}$")
_DEC = re.compile(r"^[0-9]{1,10}$")


class ProbeArgError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_number(text: str, what: str) -> int:
    """Plain hex (`0x...`) or decimal only -- no signs, underscores, expressions."""
    text = text.strip() if isinstance(text, str) else ""
    if _HEX.match(text):
        return int(text, 16)
    if _DEC.match(text):
        return int(text, 10)
    raise ProbeArgError(
        CODE_BAD_ARGUMENT, f"{what} {text!r} is not a plain hex (0x...) or decimal number"
    )


def parse_read(address: str, count: str | int | None) -> tuple[int, int]:
    """`(address, words)` for a `probe read`, or `ProbeArgError`."""
    addr = parse_number(address, "the address")
    if addr % 4:
        raise ProbeArgError(CODE_BAD_ARGUMENT, f"address 0x{addr:X} is not 4-byte aligned")
    if count is None:
        words = DEFAULT_WORDS
    else:
        words = parse_number(str(count), "the word count")
    if words < 1:
        raise ProbeArgError(CODE_BAD_ARGUMENT, "the word count must be at least 1")
    if words > MAX_WORDS:
        raise ProbeArgError(
            CODE_READ_TOO_LARGE,
            f"{words} words requested; `tan probe read` returns at most {MAX_WORDS} "
            "(1 KiB) per call",
        )
    if addr + 4 * words > 0x100000000:
        raise ProbeArgError(CODE_BAD_ARGUMENT, "the read would run past the 32-bit address space")
    return addr, words


def normalise_core(core: str | None) -> str | None:
    if core is None:
        return None
    value = core.strip().lower().replace("-", "_")
    if value not in CORES:
        raise ProbeArgError(CODE_BAD_ARGUMENT, f"--core {core!r} is not one of {', '.join(CORES)}")
    return value


def touches_unsafe_window(address: int, words: int) -> bool:
    lo, hi = UNSAFE_WINDOW
    return address <= hi and address + 4 * words - 1 >= lo


def unsafe_region_message(address: int, words: int, why: str) -> str:
    return (
        f"refusing to read 0x{address:08X}..0x{address + 4 * words - 1:08X}: it overlaps "
        "0x50000000-0x5FFFFFFF, and an M55-HE session must not touch that window (reading the "
        "HP ITCM alias from an HE attach leaves the core unhaltable until a PIN reset). "
        f"{why}"
    )


def identity_script(pre: Sequence[str]) -> str:
    """The DP IDR read: exactly the Flow D / --ram DPIDR preflight --
    `[SelectEmuBySN], si SWD, speed, device, connect, exit`."""
    return "\n".join([*pre, "exit"]) + "\n"


def core_script(pre: Sequence[str]) -> str:
    """The --ram attach check, verbatim (`connect` + two read-only `mem32`)."""
    return core_check_script(pre)


def read_script(pre: Sequence[str], address: int, words: int) -> str:
    """`connect`, ONE `mem32`, `exit`. Address and count are ints rendered with `0x%X`."""
    return "\n".join([*pre, f"mem32 0x{address:X}, 0x{words:X}", "exit"]) + "\n"


def make_preamble(serial: str | None, speed: int, device: str = DEFAULT_DEVICE) -> list[str]:
    return preamble(serial, speed, device)


def script_verbs(script: str) -> list[str]:
    """The first token of every non-empty script line, lower-cased."""
    return [ln.split()[0].lower() for ln in script.splitlines() if ln.split()]
