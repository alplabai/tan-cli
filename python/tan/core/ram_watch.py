# SPDX-License-Identifier: Apache-2.0
"""`tan flash --ram --watch` -- read-only memory sampling while a RAM-run image runs
(tan-cli#1436). Pure logic, no IO:

* [`parse_watch`]: one `--watch <addr>[:<words>][@<period-ms>]` spec -> [`WatchSpec`],
  refusing an unaligned address, a zero or excessive word count, a period outside the
  supported range, and any address the probe cannot safely read.
* [`schedule`]: the specs + the `--wait` window -> the sample times, merged in order.
* [`watch_lines`]: the J-Link Commander lines appended after `go` -- `mem32` reads
  separated by `Sleep <ms>`. `mem32` only: no write command is ever produced.
* [`parse_samples`]: the load transcript -> the envelope's `data.watch[]`.

**Time basis.** J-Link Commander prints no timestamps, so a sample's `elapsedMs` is its
SCHEDULED offset after `go` (the `Sleep` sum), not a measurement; every `mem32` adds SWD
latency on top, so the real offset is later and drifts upward over a long window.

**Unsafe addresses.** The denylist is keyed by the attached core. For the M55 HE (the
only core a RAM-run attaches to) the HP TCM windows `[0x50000000, 0x58000000)` (ITCM and
DTCM globals) are refused: reading them from an HE attach is the bench's known-bad read,
it leaves the core unhaltable until a PIN reset (bench round 8, see `tan.core.ram_run`).

**Side effects.** `mem32` is read-only on memory, but a register read (a FIFO, a
clear-on-read status, a clock-gated block) may have side effects; tan cannot tell them
apart, so watch RAM or plain data registers.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tan.core.ram_run import HE_ALIAS, _ITCM_GLOBAL

CODE_INVALID = "flash.ram-watch-invalid"
CODE_UNSAFE = "flash.ram-watch-unsafe-address"
CODE_INCOMPLETE = "flash.ram-watch-incomplete"

DEFAULT_WORDS = 1
MAX_WORDS = 64
DEFAULT_PERIOD_MS = 100
MIN_PERIOD_MS = 10
MAX_PERIOD_MS = 60_000
MAX_WATCHES = 8
#: Upper bound on the `mem32` reads in one session, so a long `--wait` with a short
#: period cannot build a script of unbounded size.
MAX_SAMPLES = 2000
#: Per attached core, the spans its attach must never read. HE: the HP TCM windows
#: [HP ITCM global, HE ITCM global) -- ITCM and DTCM.
DENIED_SPANS = {"M55_HE": ((_ITCM_GLOBAL["M55_HP"], HE_ALIAS),)}
#: Wall-clock budget per `mem32` (SWD read + J-Link command overhead), generous.
PER_READ_BUDGET_S = 2.0
TIMEOUT_MARGIN_S = 60.0


def session_timeout_s(specs: "Sequence[WatchSpec]", duration_ms: int) -> float:
    """The load session's spawn timeout: the window, plus a budget per scheduled read,
    plus a margin."""
    return duration_ms / 1000 + len(schedule(specs, duration_ms)) * PER_READ_BUDGET_S + TIMEOUT_MARGIN_S

_SPEC = re.compile(
    r"^(?P<addr>0[xX][0-9A-Fa-f]{1,8}|[0-9]{1,10})(?::(?P<words>[0-9]+))?(?:@(?P<period>[0-9]+))?$"
)


class WatchError(Exception):
    """A `--watch` refusal. `code` is the registered issue code."""

    def __init__(self, message: str, code: str = CODE_INVALID):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WatchSpec:
    address: int
    words: int
    period_ms: int
    text: str


def parse_watch(text: str, core: str = "M55_HE") -> WatchSpec:
    match = _SPEC.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise WatchError(
            f"--watch {text!r} is not <addr>[:<words>][@<period-ms>] (addr is 0x-hex or decimal)"
        )
    raw = match["addr"]
    address = int(raw, 16) if raw[:2].lower() == "0x" else int(raw)
    words = int(match["words"]) if match["words"] is not None else DEFAULT_WORDS
    period = int(match["period"]) if match["period"] is not None else DEFAULT_PERIOD_MS
    if address > 0xFFFFFFFF:
        raise WatchError(f"--watch {text!r}: address 0x{address:X} is beyond 32 bits")
    if address % 4:
        raise WatchError(f"--watch {text!r}: address 0x{address:08X} is not 4-byte aligned (mem32)")
    if not 1 <= words <= MAX_WORDS:
        raise WatchError(f"--watch {text!r}: {words} words is outside 1..{MAX_WORDS}")
    if not MIN_PERIOD_MS <= period <= MAX_PERIOD_MS:
        raise WatchError(
            f"--watch {text!r}: period {period} ms is outside {MIN_PERIOD_MS}..{MAX_PERIOD_MS}"
        )
    last = address + 4 * words - 1
    if last > 0xFFFFFFFF:
        raise WatchError(f"--watch {text!r}: the read runs past the end of the address space")
    for lo, hi in DENIED_SPANS.get(core, ()):
        if address < hi and last >= lo:
            raise WatchError(
                f"--watch {text!r}: 0x{address:08X}..0x{last:08X} overlaps the HP TCM "
                f"windows 0x{lo:08X}..0x{hi - 1:08X}; reading it from the HE attach "
                "leaves the core unhaltable until a PIN reset",
                CODE_UNSAFE,
            )
    return WatchSpec(address, words, period, text.strip())


def parse_watches(texts: Sequence[str], core: str = "M55_HE") -> list[WatchSpec]:
    if len(texts) > MAX_WATCHES:
        raise WatchError(f"{len(texts)} --watch options; at most {MAX_WATCHES}")
    return [parse_watch(t, core) for t in texts]


def schedule(specs: Sequence[WatchSpec], duration_ms: int) -> list[tuple[int, int]]:
    """`(offset_ms, spec_index)` for every sample in `[0, duration_ms]`, time-ordered
    (ties in spec order). Refuses more than [`MAX_SAMPLES`]."""
    out = [
        (t, i)
        for i, s in enumerate(specs)
        for t in range(0, max(duration_ms, 0) + 1, s.period_ms)
    ]
    if len(out) > MAX_SAMPLES:
        raise WatchError(
            f"{len(out)} samples over {duration_ms} ms exceeds {MAX_SAMPLES}; "
            "raise the period or shorten --wait"
        )
    return sorted(out)


def watch_lines(specs: Sequence[WatchSpec], duration_ms: int) -> list[str]:
    """Commander lines for after `go`: `mem32` reads with `Sleep` between, padded so the
    session lasts the whole `--wait` window. Addresses and counts are ints rendered `0x%X`."""
    lines: list[str] = []
    now = 0
    for offset, i in schedule(specs, duration_ms):
        if offset > now:
            lines.append(f"Sleep {offset - now}")
            now = offset
        lines.append(f"mem32 0x{specs[i].address:X}, 0x{specs[i].words:X}")
    if duration_ms > now:
        lines.append(f"Sleep {duration_ms - now}")
    return lines


_MEM32_LINE = re.compile(r"^([0-9A-Fa-f]{8}) = ((?:[0-9A-Fa-f]{8}(?:\s+|$))+)$")


_ECHO = re.compile(r"^J-Link>\s*mem32\s+0[xX]([0-9A-Fa-f]+)\s*,\s*0[xX]([0-9A-Fa-f]+)\s*$")


def _blocks(transcript: str) -> list[tuple[int, list[tuple[int, list[int]]]]] | None:
    """Each echoed `J-Link>mem32 0xADDR, 0xN` with the dump lines that follow it (up to the
    next `J-Link>` echo). `None` when the transcript echoes no `mem32` at all."""
    blocks: list[tuple[int, list[tuple[int, list[int]]]]] = []
    for raw in transcript.splitlines():
        line = raw.strip()
        echo = _ECHO.match(line)
        if echo:
            blocks.append((int(echo[1], 16), []))
        elif line.startswith("J-Link>"):
            blocks.append((-1, []))  # another command: closes the open block
        else:
            m = _MEM32_LINE.match(line)
            if m and blocks:
                blocks[-1][1].append((int(m[1], 16), [int(w, 16) for w in m[2].split()]))
    reads = [b for b in blocks if b[0] >= 0]
    return reads if reads else None


def _collect(address: int, words: int, dumps: list[tuple[int, list[int]]]) -> list[int] | None:
    values: list[int] = []
    for base, got in dumps:
        if base != address + 4 * len(values):
            break
        values.extend(got)
    return values if len(values) == words else None


def parse_samples(transcript: str, specs: Sequence[WatchSpec], duration_ms: int) -> list[dict]:
    """The envelope's `data.watch[]`: one entry per scheduled sample, in order, each
    `{address, words, index, elapsedMs, values}`; `values` is `None` when its dump is
    missing. Strict: only `ADDR = WWWWWWWW ...` lines count. When J-Link echoes the
    commands, each echoed `mem32` is paired with the dumps that follow IT, so a missing
    dump is `None` at its own index and shifts nothing; a transcript with no echo falls
    back to matching dump lines in order against each sample's expected address."""
    sched = schedule(specs, duration_ms)
    blocks = _blocks(transcript)
    flat = []
    for raw in transcript.splitlines():
        m = _MEM32_LINE.match(raw.strip())
        if m:
            flat.append((int(m[1], 16), [int(w, 16) for w in m[2].split()]))
    out: list[dict] = []
    cursor = 0
    counts: dict[int, int] = {}
    for n, (offset, i) in enumerate(sched):
        spec = specs[i]
        if blocks is not None:
            got = None
            if n < len(blocks) and blocks[n][0] == spec.address:
                got = _collect(spec.address, spec.words, blocks[n][1])
        else:
            got = None
            pos, values = cursor, []
            while len(values) < spec.words and pos < len(flat):
                base, words = flat[pos]
                if base != spec.address + 4 * len(values):
                    break
                values.extend(words)
                pos += 1
            if len(values) == spec.words:
                got, cursor = values, pos
        idx = counts.get(i, 0)
        counts[i] = idx + 1
        out.append(
            {
                "address": f"0x{spec.address:08X}",
                "words": spec.words,
                "index": idx,
                "elapsedMs": offset,
                "values": [f"0x{v:08X}" for v in got] if got is not None else None,
            }
        )
    return out
