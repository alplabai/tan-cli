# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1539: a boot witness for Flow D -- the kernel uptime, bounded by the wall clock.

After a write whose halt failed, DHCSR `S_RESET_ST` is usually gone and PC samples cannot tell the
new image from the replaced one (tan-cli#1537). A free-running tick counter can, provided it is
compared with the time tan itself measured: the probe's last read must show an uptime no larger
than the monotonic time elapsed since the write session ended (plus a slack for the reset tail
inside that session). An image that was already running when the write began has an uptime of at
least the write's own duration more than that bound allows, unless it was reset by the write.
This proves a RECENT BOOT, not that the SE PIN reset took (reported as `reset: "booted"`).

The counter is Zephyr's kernel tick count (`curr_tick`, a `static uint64_t` in `kernel/timeout.c`;
present in an AEN alp-console ELF at a DTCM address). Its address is resolved from the ELF being
flashed, never hardcoded, and only an address inside a RAM LOAD segment of the slice core's own
DTCM / SRAM0 is ever read (never HP ITCM 0x50000000, never anything outside the image). The tick
rate comes from the same ELF's absolute `CONFIG_SYS_CLOCK_TICKS_PER_SEC` symbol, else the build's
`.config` beside it. Anything missing, ambiguous or out of range yields no probe, and the caller
keeps the honest `unconfirmed`. Pure: no IO except `load_uptime_probe` reading two local files."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Sequence

from tan.core.ram_run import ElfImage, RamRunError, parse_elf

UPTIME_SYMBOL = "curr_tick"
TICKS_PER_SEC_SYMBOL = "CONFIG_SYS_CLOCK_TICKS_PER_SEC"
#: Reads per session, and the pause between them (ms). The tick only advances when the timeout
#: ISR announces, so the window must span more than one idle wake-up of a typical sleeper.
UPTIME_READS = 4
UPTIME_SLEEP_MS = 600
#: Slack (s) added to the elapsed time: the reset tail runs inside the write session, so the
#: image boots slightly BEFORE the session's end timestamp.
UPTIME_SLACK_SECONDS = 1.0
#: The only address windows ever read: the slice core's DTCM (local alias, 1 MiB window) and the
#: E8's 4 MiB SRAM0. HP ITCM (0x50000000) and every other aperture are out.
_READABLE = ((0x20000000, 0x20100000), (0x02000000, 0x02400000))


@dataclass(frozen=True)
class UptimeProbe:
    address: int
    size: int  # 4 or 8 bytes
    ticks_per_sec: int
    symbol: str = UPTIME_SYMBOL

    @property
    def words(self) -> int:
        return self.size // 4


def plan_uptime_probe(elf: ElfImage, config_ticks_per_sec: int | None = None) -> UptimeProbe | None:
    """The probe for `elf`, or `None` when the symbol is missing, ambiguous, oddly sized, outside
    the readable RAM windows or outside a RAM LOAD segment, or the tick rate is unknown."""
    if UPTIME_SYMBOL in elf.ambiguous or UPTIME_SYMBOL not in elf.symbols:
        return None
    address, size = elf.symbols[UPTIME_SYMBOL]
    if size not in (4, 8) or address % 4:
        return None
    end = address + size
    if not any(lo <= address and end <= hi for lo, hi in _READABLE):
        return None
    if not any(s.vaddr <= address and end <= s.vaddr + s.memsz for s in elf.segments):
        return None
    rate = config_ticks_per_sec
    if TICKS_PER_SEC_SYMBOL in elf.symbols and TICKS_PER_SEC_SYMBOL not in elf.ambiguous:
        rate = elf.symbols[TICKS_PER_SEC_SYMBOL][0]
    if not rate or rate <= 0:
        return None
    return UptimeProbe(address, size, rate)


def config_ticks_per_sec(config_text: str) -> int | None:
    match = re.search(r"^CONFIG_SYS_CLOCK_TICKS_PER_SEC=(\d+)\s*$", config_text, re.MULTILINE)
    return int(match.group(1)) if match else None


def load_elf(elf_path: str) -> ElfImage | None:
    """The parsed ELF at `elf_path`, or `None` when it cannot be read or parsed."""
    try:
        with open(elf_path, "rb") as fh:
            return parse_elf(fh.read())
    except (OSError, RamRunError):
        return None


def load_uptime_probe(elf: ElfImage | None, elf_path: str) -> UptimeProbe | None:
    """`plan_uptime_probe` for the parsed `elf` (read from `elf_path`, whose directory may hold
    the build's `.config`); `None` when there is no ELF."""
    if elf is None:
        return None
    rate = None
    try:
        with open(os.path.join(os.path.dirname(elf_path), ".config"), encoding="utf-8") as fh:
            rate = config_ticks_per_sec(fh.read())
    except OSError:
        pass
    return plan_uptime_probe(elf, rate)


def uptime_script_lines(probe: UptimeProbe) -> list[str]:
    """Read-only Commander lines: `UPTIME_READS` non-halting `mem32` reads of the counter."""
    out: list[str] = []
    for i in range(UPTIME_READS):
        out.append(f"mem32 0x{probe.address:08X} {probe.words}")
        if i + 1 < UPTIME_READS:
            out.append(f"Sleep {UPTIME_SLEEP_MS}")
    return out


def uptime_ticks(transcript: str, probe: UptimeProbe) -> list[int]:
    """Every counter value the probe's `mem32` lines printed, in order (little-endian words)."""
    pattern = rf"(?<![0-9A-Fa-f]){probe.address:08X}\s*=\s*((?:[0-9A-Fa-f]{{8}}\s*){{{probe.words}}})"
    values: list[int] = []
    for match in re.finditer(pattern, transcript, re.IGNORECASE):
        words = [int(w, 16) for w in match.group(1).split()]
        values.append(sum(w << (32 * i) for i, w in enumerate(words)))
    return values


def uptime_confirms_boot(
    ticks: Sequence[int], ticks_per_sec: int, elapsed_s: float,
    slack_s: float = UPTIME_SLACK_SECONDS,
) -> bool:
    """True only for a counter that moved (never going backwards, last > first) and whose last
    value is no more uptime than `elapsed_s` (monotonic seconds since the write session ended)
    plus `slack_s`. A constant counter is a halted or gated core; a larger uptime is an image
    that was running before the write; an all-ones word is an unreadable bus, not a count. The
    64-bit counter is read as two 32-bit words, so a read can tear across a carry: it shows as a
    huge jump or a step backwards and is rejected by the same rules."""
    if len(ticks) < 2 or any(t in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF) for t in ticks):
        return False
    if any(b < a for a, b in zip(ticks, ticks[1:])) or ticks[-1] <= ticks[0]:
        return False
    return ticks[-1] / ticks_per_sec <= elapsed_s + slack_s


def uptime_evidence(
    transcript: str, probe: UptimeProbe, elapsed_s: float
) -> tuple[dict, dict | None]:
    """`(bootProbe.uptime evidence, jlink-block update)`; the update is `None` unless the
    counter confirms a boot."""
    ticks = uptime_ticks(transcript, probe)
    evidence = {
        "symbol": probe.symbol, "address": f"0x{probe.address:08X}",
        "ticks": [f"0x{t:X}" for t in ticks], "ticksPerSec": probe.ticks_per_sec,
        "elapsedSinceWriteSeconds": round(elapsed_s, 2),
    }
    if not uptime_confirms_boot(ticks, probe.ticks_per_sec, elapsed_s):
        return evidence, None
    return evidence, {
        "coreRunning": True, "reset": "booted", "resetConfirmedBy": "uptime",
        "witness": f"{probe.symbol} 0x{ticks[0]:X} -> 0x{ticks[-1]:X}, up "
        f"{ticks[-1] / probe.ticks_per_sec:.1f} s, {elapsed_s:.1f} s after the write",
        "resetNote": "the halt after the reset failed, but the kernel tick counter "
        f"({probe.symbol}, read without halting at the flashed ELF's own address) is rising and "
        "shows no more uptime than the time elapsed since the write ended: the image booted "
        "after the write. That proves a recent boot, not that the SE PIN reset took.",
    }
