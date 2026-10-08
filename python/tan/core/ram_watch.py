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

**Unsafe addresses.** The attached core must be the M55 HE (the core check refuses
anything else), and a read of the HP core's ITCM global window from an HE attach is the
bench's known-bad read: it leaves the core unhaltable until a PIN reset (bench round 8,
see `tan.core.ram_run`). The whole HP global span below the HE window is refused.
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
#: HP core globals the HE attach must never read: [HP ITCM global, HE ITCM global).
DENIED_SPANS = ((_ITCM_GLOBAL["M55_HP"], HE_ALIAS),)

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


def parse_watch(text: str) -> WatchSpec:
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
    for lo, hi in DENIED_SPANS:
        if address < hi and last >= lo:
            raise WatchError(
                f"--watch {text!r}: 0x{address:08X}..0x{last:08X} overlaps the HP core's "
                f"ITCM window 0x{lo:08X}..0x{hi - 1:08X}; reading it from the HE attach "
                "leaves the core unhaltable until a PIN reset",
                CODE_UNSAFE,
            )
    return WatchSpec(address, words, period, text.strip())


def parse_watches(texts: Sequence[str]) -> list[WatchSpec]:
    if len(texts) > MAX_WATCHES:
        raise WatchError(f"{len(texts)} --watch options; at most {MAX_WATCHES}")
    return [parse_watch(t) for t in texts]


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


def parse_samples(transcript: str, specs: Sequence[WatchSpec], duration_ms: int) -> list[dict]:
    """The envelope's `data.watch[]`: one entry per scheduled sample, in order, each
    `{address, words, index, elapsedMs, values}`; `values` is `None` when its dump is
    missing. Strict: only `ADDR = WWWWWWWW ...` lines count, matched in transcript order
    against the address each sample expects."""
    dumps = []
    for raw in transcript.splitlines():
        m = _MEM32_LINE.match(raw.strip())
        if m:
            dumps.append((int(m[1], 16), [int(w, 16) for w in m[2].split()]))
    out: list[dict] = []
    cursor = 0
    counts: dict[int, int] = {}
    for offset, i in schedule(specs, duration_ms):
        spec = specs[i]
        values: list[int] = []
        pos = cursor
        while len(values) < spec.words and pos < len(dumps):
            base, words = dumps[pos]
            if base != spec.address + 4 * len(values):
                break
            values.extend(words)
            pos += 1
        complete = len(values) == spec.words
        if complete:
            cursor = pos
        idx = counts.get(i, 0)
        counts[i] = idx + 1
        out.append(
            {
                "address": f"0x{spec.address:08X}",
                "words": spec.words,
                "index": idx,
                "elapsedMs": offset,
                "values": [f"0x{v:08X}" for v in values] if complete else None,
            }
        )
    return out
