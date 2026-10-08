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


def window_refusal(address: int, words: int) -> str | None:
    """The refusal for ANY overlap with 0x50000000-0x5FFFFFFF, on every core -- there is
    no HP exception: a bench-proven-safe HP read is not something an attach check can
    establish (a multiple-AP banner or `conflict-hp` still looks HP-ish), so the window
    is simply not readable through `tan probe`."""
    if not touches_unsafe_window(address, words):
        return None
    return (
        f"refusing to read 0x{address:08X}..0x{address + 4 * words - 1:08X}: it overlaps "
        "0x50000000-0x5FFFFFFF, and an M55-HE session must not touch that window (reading "
        "the HP ITCM alias from an HE attach leaves the core unhaltable until a PIN reset). "
        "`tan probe read` refuses the window on every core."
    )


def _slice_ambiguity(distinct: dict[str, set[str]]) -> str | None:
    for key, values in distinct.items():
        if len(values) > 1:
            return (
                f"the manifest's slices pin different {key} values ({', '.join(sorted(values))}); "
                "pass --core to pick one, as `tan flash` requires"
            )
    return None


def select_slice(
    slices: Sequence[tuple[str, dict]], core: str | None
) -> tuple[dict, str | None, str | None]:
    """`(flash_args, selected core id, ambiguity message)` from `(core_id, flash_args)` pairs.

    The args are taken whether or not `expect_dpidr` is armed (`jlink_serial`,
    `jlink_speed`, `jlink_device` apply either way). With `--core` the matching slice
    wins; without it one slice is used as is, and several are acceptable only when they
    do not disagree on `jlink_serial` / `expect_dpidr` (then the selected id is unknown)."""
    pool = [(c, a) for c, a in slices if isinstance(a, dict)]
    if core is not None:
        pool = [(c, a) for c, a in pool if c.lower() == core]
    if not pool:
        return {}, None, None
    if len(pool) == 1:
        return dict(pool[0][1]), pool[0][0].lower(), None
    distinct = {
        key: {str(a[key]) for _c, a in pool if a.get(key) is not None}
        for key in ("jlink_serial", "expect_dpidr")
    }
    problem = _slice_ambiguity(distinct)
    if problem:
        return {}, None, problem
    armed = [a for _c, a in pool if a.get("expect_dpidr")]
    return dict((armed or [a for _c, a in pool])[0]), None, None


def is_he_target(core: str | None, selected_id: str | None) -> bool:
    """The target is the M55-HE: `--core m55_he`, or no `--core` and the manifest's
    selected slice is the HE."""
    return core == "m55_he" or (core is None and selected_id == "m55_he")


def core_contradiction(core: str | None, verdict: str) -> str | None:
    """A message when the attach `verdict` (`ram_run` vocabulary, or the bare AP verdict)
    contradicts the claimed `--core`; `None` when it agrees or says nothing."""
    if core == "m55_he" and verdict in ("hp", "conflict-hp"):
        return f"--core m55_he, but the probe attached to the M55-HP ({verdict})"
    if core == "m55_hp" and verdict in ("he", "conflict"):
        return f"--core m55_hp, but the probe attached to the M55-HE ({verdict})"
    return None


def dpidr_state(expected: str | None, actual: str | None) -> str | None:
    """`None` when no `expect_dpidr` is armed or it matches; `"unread"` / `"mismatch"` else."""
    if not expected:
        return None
    if actual is None:
        return "unread"
    try:
        return None if int(actual, 16) == int(expected, 16) else "mismatch"
    except ValueError:
        return "mismatch"


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
