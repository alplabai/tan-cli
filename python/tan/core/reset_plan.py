# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1452: the pure half of `tan reset` -- the one-pulse nRESET script.

`tan reset` is ONE clean pin pulse through J-Link Commander: `r0` (drive nRESET
low), a `sleep` of the pulse width, `r1` (release), `q`. It deliberately has none of
the machinery `tan flash`'s pin reset carries: no `connect`, no halt, no `RSetType`, no
connect-under-reset, no `g`, no retry. Each of those either wakes a target out
of STOP, loses the VBAT/BKRAM evidence of a non-waking STOP, or both.
`FORBIDDEN_VERBS` is what the tests hold the generated script to.
"""
from __future__ import annotations

import re

DEFAULT_PULSE_MS = 100
MIN_PULSE_MS = 1
MAX_PULSE_MS = 10000
#: Commander has to open the probe, never attach to the target: a `connect`
#: against an Alif target in STOP fails (debug domain gated) or disturbs the
#: evidence this verb exists to preserve. Bench-proven script: r0 / sleep 100 / r1 / q.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Commander verbs a reset script must never contain.
FORBIDDEN_VERBS = (
    "connect", "h", "halt", "g", "go", "rsettype", "resettype", "reset", "r", "rx", "w1", "w2", "w4",
    "loadbin", "loadfile", "erase", "unlock", "setpc", "step", "s",
)


class ResetArgError(ValueError):
    """A bad `--pulse-ms`."""


def check_pulse_ms(pulse_ms: int) -> int:
    if isinstance(pulse_ms, bool) or not isinstance(pulse_ms, int):
        raise ResetArgError(f"--pulse-ms {pulse_ms!r} is not an integer")
    if not MIN_PULSE_MS <= pulse_ms <= MAX_PULSE_MS:
        raise ResetArgError(
            f"--pulse-ms {pulse_ms} is outside {MIN_PULSE_MS}..{MAX_PULSE_MS} milliseconds"
        )
    return pulse_ms


def pulse_script(serial: str | None, pulse_ms: int) -> str:
    """`SelectEmuBySN <serial>` (only when a probe is selected), then
    `r0`, `sleep <ms>`, `r1`, `q`. No `connect`."""
    check_pulse_ms(pulse_ms)
    if serial is not None and not _SAFE_TOKEN.match(serial):
        raise ResetArgError(f"the J-Link serial {serial!r} is not a plain token")
    pre = [f"SelectEmuBySN {serial}"] if serial else []
    return "\n".join([*pre, "r0", f"sleep {pulse_ms}", "r1", "q"]) + "\n"


def script_verbs(script: str) -> list[str]:
    """The first token of every non-empty line, lower-cased."""
    return [ln.split()[0].lower() for ln in script.splitlines() if ln.split()]
