# SPDX-License-Identifier: Apache-2.0
"""Headless serial capture: pure logic for `tan monitor --capture`
(tan-cli#1324).

Reads from an already-open port-like object (`read(size) -> bytes`, optional
`in_waiting`) for a fixed duration or until a regex matches a line, optionally
teeing the raw bytes to a binary sink. No TTY, no miniterm, no port opening;
the caller owns the port. The clock is injected so the timing is testable
without sleeping.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

TAIL_BYTES = 256
#: A partial (unterminated) line longer than this is truncated from the left
#: so a binary stream with no newline cannot grow memory without bound.
_MAX_PENDING = 65536
#: Duration used when `--until` is given without `--duration`.
DEFAULT_UNTIL_DURATION_S = 30.0


class ReadPort(Protocol):
    def read(self, size: int = 1, /) -> bytes: ...


@dataclass(frozen=True)
class CaptureResult:
    matched: bool  # only meaningful when a pattern was given
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


def capture(
    port: ReadPort,
    *,
    duration_s: float,
    until: re.Pattern[str] | None = None,
    sink: BinaryIO | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CaptureResult:
    """Read until `until` matches a line, or `duration_s` elapses.

    Each complete line (CR/LF stripped) is searched, and so is the pending
    partial line, since a prompt arrives without a trailing newline. At least
    one read happens even with a zero duration.
    """
    start = clock()
    deadline = start + duration_s
    pending = b""
    tail = b""
    seen = 0
    while True:
        n = max(1, int(getattr(port, "in_waiting", 0) or 0))
        chunk = port.read(n) or b""
        if chunk:
            seen += len(chunk)
            tail = (tail + chunk)[-TAIL_BYTES:]
            if sink is not None:
                sink.write(chunk)
                sink.flush()
            if until is not None:
                pending += chunk
                *lines, pending = pending.split(b"\n")
                pending = pending[-_MAX_PENDING:]
                for raw in [*lines, pending]:
                    line = _text(raw).rstrip("\r")
                    if until.search(line):
                        return CaptureResult(True, line, clock() - start, seen, _text(tail))
        if clock() >= deadline:
            return CaptureResult(False, None, clock() - start, seen, _text(tail))
