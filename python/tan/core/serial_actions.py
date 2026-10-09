# SPDX-License-Identifier: Apache-2.0
"""Input and baud-change actions for `tan monitor --capture` (tan-cli#1451).

Pure logic over an already-open port-like object: the `--send` queue (sent
once, as a burst, at the start / after a delay / on the first line matching a
regex) and the `--reopen-at BAUD --on REGEX` baud switch (close the port and
reopen it at the new rate on the first match). The port is opened by the
caller; reopening goes through the injected `reopen(baud)` callable so this
module never imports pyserial and works the same over a local tty and a
pyserial URL such as `rfc2217://`. Clock and sleep are injected for tests.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Pause between two `--send` items so a firmware that polls one key per tick
#: sees them separately.
DEFAULT_SEND_GAP_S = 0.2


class ActionError(Exception):
    """A send or reopen failed; `kind` is `send` or `reopen`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ActionSpec:
    sends: tuple[bytes, ...] = ()
    send_after_s: float | None = None
    send_on: re.Pattern[str] | None = None
    send_gap_s: float = DEFAULT_SEND_GAP_S
    reopen_baud: int | None = None
    reopen_on: re.Pattern[str] | None = None

    @property
    def watches_lines(self) -> bool:
        return self.send_on is not None or self.reopen_on is not None


def _default_write(port: Any, data: bytes) -> None:
    port.write(data)


@dataclass
class CaptureActions:
    """The mutable run state. `port` is the CURRENT port: the capture loop reads
    it each iteration, because a reopen replaces it."""

    spec: ActionSpec
    port: Any
    reopen: Callable[[int], Any]
    write: Callable[[Any, bytes], None] = _default_write
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    events: list[dict[str, Any]] = field(default_factory=list)
    _start: float = field(init=False)
    _sent: bool = False
    _reopened: bool = False

    def __post_init__(self) -> None:
        self._start = self.clock()

    @property
    def sends_pending(self) -> int:
        return 0 if self._sent else len(self.spec.sends)

    def _at(self) -> float:
        return round(self.clock() - self._start, 3)

    def tick(self) -> None:
        """Fire the unconditional / timed send when it is due."""
        spec = self.spec
        if self._sent or not spec.sends or spec.send_on is not None:
            return
        if spec.send_after_s is None or self.clock() - self._start >= spec.send_after_s:
            self._send()

    def line(self, text: str) -> None:
        """Offer a complete or partial line; each regex fires at most once."""
        spec = self.spec
        if spec.sends and not self._sent and spec.send_on is not None and spec.send_on.search(text):
            self._send()
        if (
            spec.reopen_baud is not None
            and not self._reopened
            and spec.reopen_on is not None
            and spec.reopen_on.search(text)
        ):
            self._reopen(spec.reopen_baud, text)

    def _send(self) -> None:
        self._sent = True
        total = 0
        try:
            for i, item in enumerate(self.spec.sends):
                if i:
                    self.sleep(self.spec.send_gap_s)
                self.write(self.port, item)
                total += len(item)
            flush = getattr(self.port, "flush", None)
            if callable(flush):
                flush()
        except OSError as err:
            raise ActionError("send", f"writing --send input failed: {err}") from err
        self.events.append(
            {"action": "send", "atSeconds": self._at(), "items": len(self.spec.sends), "bytes": total}
        )

    def _reopen(self, baud: int, matched: str) -> None:
        self._reopened = True
        try:
            self.port.close()
        except OSError:
            pass  # closing a dead link is not the failure being reported
        try:
            self.port = self.reopen(baud)
        except Exception as err:  # noqa: BLE001 -- any open failure is one issue
            raise ActionError("reopen", f"reopening the port at {baud} baud failed: {err}") from err
        self.events.append(
            {"action": "reopen", "atSeconds": self._at(), "baud": baud, "matchedLine": matched[-256:]}
        )
