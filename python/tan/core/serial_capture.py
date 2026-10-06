# SPDX-License-Identifier: Apache-2.0
"""Headless serial capture: pure logic for `tan monitor --capture`
(tan-cli#1324).

Reads from an already-open port-like object (`read(size) -> bytes`, optional
`in_waiting`) for a fixed duration or until a regex matches, optionally
teeing the raw bytes to a binary sink. No TTY, no miniterm, no port opening;
the caller owns the port and the sink. The clock is injected so the timing is
testable without sleeping.

`--until` semantics: the regex is searched in every COMPLETE line (CR/LF
stripped) exactly once, and in the current UNTERMINATED line (so a prompt such
as `=> ` matches before any newline arrives), at most the last
`MAX_SEARCH_CHARS` of it per read. Long complete lines are searched on their
LAST `MAX_SEARCH_CHARS` too, the same window as the partial line. The deadline is checked between lines, so a
read that delivers thousands of lines cannot overrun `--duration`. Python's
`re` cannot be interrupted, so a pathological (catastrophic-backtracking)
pattern can still stall inside ONE search; the cap on the searched text and
the per-line deadline check bound the damage, they do not remove it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

TAIL_BYTES = 256
#: No single `re.search` is given more than this many characters.
MAX_SEARCH_CHARS = 4096
#: A partial (unterminated) line longer than this is truncated from the left
#: so a binary stream with no newline cannot grow memory without bound.
_MAX_PENDING = 65536
#: Duration used when `--until` is given without `--duration`.
DEFAULT_UNTIL_DURATION_S = 30.0


class ReadPort(Protocol):
    def read(self, size: int = 1, /) -> bytes: ...


class SinkError(Exception):
    """Writing the log failed (kept apart from serial I/O errors)."""


@dataclass(frozen=True)
class CaptureResult:
    matched: bool
    matched_line: str | None
    elapsed_s: float
    bytes_seen: int
    tail: str


def compile_until(pattern: str) -> re.Pattern[str]:
    """Compile `--until`; `ValueError` (with the regex error) if malformed or
    empty, so the caller can map it to one validation issue."""
    if not pattern:
        raise ValueError("--until must not be empty")
    try:
        return re.compile(pattern)
    except re.error as err:
        raise ValueError(f"--until is not a valid regex: {err}") from err


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _tail_text(buf: bytes) -> str:
    if len(buf) > TAIL_BYTES:
        buf = buf[-TAIL_BYTES:]
        while buf and 0x80 <= buf[0] < 0xC0:  # do not start mid-character
            buf = buf[1:]
    return _text(buf)


def capture(
    port: ReadPort,
    *,
    duration_s: float,
    until: re.Pattern[str] | None = None,
    sink: BinaryIO | None = None,
    initial: bytes = b"",
    clock: Callable[[], float] = time.monotonic,
) -> CaptureResult:
    """Read until `until` matches, or `duration_s` elapses.

    `initial` is data already received before this call (the `--break-uboot`
    tail): it is logged and searched like any other chunk, so a prompt that
    ended the break-in can satisfy `--until`. At least one read happens even
    with a zero duration. A sink failure raises `SinkError`; a port failure
    propagates as the port's own `OSError`.
    """
    start = clock()
    deadline = start + duration_s
    pending = b""
    tail = b""
    seen = 0
    first = True
    while True:
        if first and initial:
            chunk = initial
        else:
            n = max(1, int(getattr(port, "in_waiting", 0) or 0))
            chunk = port.read(n) or b""
        first = False
        if chunk:
            seen += len(chunk)
            tail = (tail + chunk)[-TAIL_BYTES:]
            if sink is not None:
                try:
                    sink.write(chunk)
                    sink.flush()
                except OSError as err:
                    raise SinkError(str(err)) from err
            if until is not None:
                *lines, pending = (pending + chunk).split(b"\n")
                pending = pending[-_MAX_PENDING:]
                for raw in lines:  # each complete line is new data: search it once
                    line = _text(raw).rstrip("\r")[-MAX_SEARCH_CHARS:]
                    if until.search(line):
                        return CaptureResult(True, line, clock() - start, seen, _tail_text(tail))
                    if clock() >= deadline:
                        return CaptureResult(False, None, clock() - start, seen, _tail_text(tail))
                partial = _text(pending).rstrip("\r")[-MAX_SEARCH_CHARS:]
                if partial and until.search(partial):
                    return CaptureResult(True, partial, clock() - start, seen, _tail_text(tail))
        if clock() >= deadline:
            return CaptureResult(False, None, clock() - start, seen, _tail_text(tail))
