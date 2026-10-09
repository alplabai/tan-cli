# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1452: the pure half of `tan reset` -- the one-pulse nRESET script.

`tan reset` is ONE clean pin pulse through J-Link Commander: `r0` (drive nRESET
low), a `Sleep` of the pulse width, `r1` (release). It deliberately has none of
the machinery `tan flash`'s pin reset carries: no halt, no `RSetType`, no
connect-under-reset, no `g`, no retry. Each of those either wakes a target out
of STOP, loses the VBAT/BKRAM evidence of a non-waking STOP, or both.
`FORBIDDEN_VERBS` is what the tests hold the generated script to.
"""
from __future__ import annotations

from collections.abc import Sequence

from tan.core.ram_run import preamble

DEFAULT_PULSE_MS = 100
MIN_PULSE_MS = 1
MAX_PULSE_MS = 10000
#: The generic Cortex-M attach profile `tan probe` uses.
DEFAULT_DEVICE = "Cortex-M55"

#: Commander verbs a reset script must never contain.
FORBIDDEN_VERBS = (
    "h", "halt", "g", "go", "rsettype", "resettype", "reset", "r", "rx", "w1", "w2", "w4",
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


def pulse_script(pre: Sequence[str], pulse_ms: int) -> str:
    """`pre` (select probe, interface, speed, device, `connect`), then
    `r0`, `Sleep <ms>`, `r1`, `exit`."""
    check_pulse_ms(pulse_ms)
    return "\n".join([*pre, "r0", f"Sleep {pulse_ms}", "r1", "exit"]) + "\n"


def script_verbs(script: str) -> list[str]:
    """The first token of every non-empty line, lower-cased."""
    return [ln.split()[0].lower() for ln in script.splitlines() if ln.split()]


def make_preamble(serial: str | None, speed: int, device: str = DEFAULT_DEVICE) -> list[str]:
    return preamble(serial, speed, device)
