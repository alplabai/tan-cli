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

import codecs
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


def parse_escaped(text: str, what: str) -> bytes:
    """Turn a CLI string like `\\x03` / `\\r` / ` ` into bytes.

    Raises `ValueError` (naming `what`) on an empty result or a bad escape.
    """
    try:
        raw = codecs.decode(text, "unicode_escape").encode("latin-1")
    except (UnicodeError, ValueError) as err:
        raise ValueError(f"{what} {text!r} is not a valid escaped string: {err}") from err
    if not raw:
        raise ValueError(f"{what} must not be empty")
    return raw


def _tail_text(buf: bytes) -> str:
    return buf[-TAIL_BYTES:].decode("utf-8", errors="replace")


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

    One iteration = a key write (at most one per `interval_s`), then one
    `read` of whatever is waiting (the port's own read timeout, set by the
    caller to about `interval_s`, paces the loop). The deadline is checked
    after the read, so at least one key is always sent. Stops
    sending the instant the prompt matches, so U-Boot is not fed stray keys
    after the break-in.
    """
    start = clock()
    deadline = start + timeout_s
    window = b""
    seen = 0
    last_write = None
    while True:
        now = clock()
        if last_write is None or now - last_write >= interval_s:
            port.write(key)
            last_write = now
        chunk = port.read(max(1, int(getattr(port, "in_waiting", 0) or 0))) or b""
        if chunk:
            seen += len(chunk)
            window = (window + chunk)[-_MATCH_WINDOW:]
            if prompt in window:
                return BreakInResult(True, clock() - start, seen, _tail_text(window))
        if clock() >= deadline:
            return BreakInResult(False, clock() - start, seen, _tail_text(window))
