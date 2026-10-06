# SPDX-License-Identifier: Apache-2.0
"""U-Boot autoboot break-in: pure matching/timing logic for `tan monitor
--break-uboot` (tan-cli#1315).

The bench recipe is "power-cycle the board, then spam a key on the console
until the `=> ` prompt appears". This module is that loop and nothing else: it
takes an already-open port-like object (anything with `write(bytes)` and
`read(size) -> bytes`; a pyserial `Serial`, a `loop://` port, an `rfc2217://`
or `socket://` URL port, or a test fake), keeps sending the interrupt key,
watches the received stream for the prompt, and stops the moment it is seen
or the deadline passes. It never opens a port and never controls power --
the operator or labgrid does the power-cycle.

The clock is injected so the timing logic is testable without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

#: pyserial `Serial` reads below this many seconds keep the loop responsive
#: and also pace the key presses (one write per read window).
DEFAULT_INTERVAL_S = 0.1
DEFAULT_KEY = b" "
DEFAULT_PROMPT = b"=> "
DEFAULT_TIMEOUT_S = 30.0

#: How much received text the envelope carries (`bytesSeenTail`), and how
#: much is kept for prompt matching (a prompt cannot be longer than this).
TAIL_BYTES = 256
_MATCH_WINDOW = 4096


class BytePort(Protocol):
    def write(self, data: bytes, /) -> object: ...
    def read(self, size: int = 1, /) -> bytes: ...


@dataclass(frozen=True)
class BreakInResult:
    caught: bool
    elapsed_s: float
    bytes_seen: int
    tail: str


_SIMPLE_ESCAPES = {"r": b"\r", "n": b"\n", "t": b"\t", "\\": b"\\"}
_HEX = "0123456789abcdefABCDEF"


def parse_escaped(text: str, what: str) -> bytes:
    """Turn a CLI string into bytes. Supported escapes: `\\xNN` (exactly two
    hex digits), `\\r`, `\\n`, `\\t`, `\\\\`. Any other backslash sequence is
    rejected rather than guessed at; other characters are taken literally
    (UTF-8). Raises `ValueError` naming `what` on a bad escape or empty result.
    """
    out = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[nxt]
            i += 2
        elif nxt == "x" and len(text[i + 2 : i + 4]) == 2 and all(c in _HEX for c in text[i + 2 : i + 4]):
            out.append(int(text[i + 2 : i + 4], 16))
            i += 4
        else:
            raise ValueError(
                f"{what} {text!r}: unsupported escape at position {i} "
                "(allowed: \\xNN, \\r, \\n, \\t, \\\\)"
            )
    if not out:
        raise ValueError(f"{what} must not be empty")
    return bytes(out)


def _tail_text(buf: bytes) -> str:
    if len(buf) > TAIL_BYTES:
        buf = buf[-TAIL_BYTES:]
        # Do not start mid-way through a multibyte UTF-8 character.
        while buf and 0x80 <= buf[0] < 0xC0:
            buf = buf[1:]
    return buf.decode("utf-8", errors="replace")


def break_into_uboot(
    port: BytePort,
    *,
    key: bytes = DEFAULT_KEY,
    prompt: bytes = DEFAULT_PROMPT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    interval_s: float = DEFAULT_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> BreakInResult:
    """Send `key` repeatedly until `prompt` is received or `timeout_s` passes.

    One iteration = drain what is already waiting (return if the prompt is in
    it), send a key if `interval_s` has passed since the last one, then one
    `read` (the port's own read timeout, set by the caller to about
    `interval_s`, paces the loop). The deadline is checked after the read, so
    at least one key is always sent. At most the one key already in flight
    when the prompt arrives is sent; none after.
    """
    start = clock()
    deadline = start + timeout_s
    window = b""
    seen = 0
    last_write = None

    def absorb(chunk: bytes) -> bool:
        nonlocal window, seen
        seen += len(chunk)
        window = (window + chunk)[-_MATCH_WINDOW:]
        return prompt in window

    def result(caught: bool) -> BreakInResult:
        return BreakInResult(caught, clock() - start, seen, _tail_text(window))

    while True:
        # Drain everything already received BEFORE deciding to send another
        # key, so no key goes out once the prompt is already in hand.
        waiting = int(getattr(port, "in_waiting", 0) or 0)
        if waiting and absorb(port.read(waiting) or b""):
            return result(True)
        now = clock()
        if last_write is None or now - last_write >= interval_s:
            port.write(key)
            last_write = now
        chunk = port.read(max(1, int(getattr(port, "in_waiting", 0) or 0))) or b""
        if chunk and absorb(chunk):
            return result(True)
        if clock() >= deadline:
            return result(False)
