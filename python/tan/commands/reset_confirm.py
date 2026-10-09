# SPDX-License-Identifier: Apache-2.0
"""`tan reset --confirm-console PORT --expect REGEX` (tan-cli#1452).

A J-Link `r0`/`r1` pulse that exits 0 proves only that Commander ran; it says
nothing about the board rebooting (a target in STOP can ignore it). This is the
evidence: the console is opened BEFORE the pulse (so an `rfc2217://` session is
already up), and once J-Link has exited the input queue is dropped and the port
is read until a line matches `--expect` or `--confirm-timeout` passes. Only
bytes that arrive after J-Link exits count, so a line the board printed before
the pulse cannot confirm it. Boot text emitted in the second between `r1` and
Commander's exit is therefore not seen: pick a banner printed some time after
reset.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from tan.core import serial_capture, serial_url

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_WINDOW_S = 3.0


class ConfirmError(ValueError):
    """A bad `--confirm-*` / `--expect` combination."""


@dataclass(frozen=True)
class ConfirmSpec:
    port: str
    expect: Any  # compiled regex
    baud: int
    timeout_s: float
    window_s: float = DEFAULT_WINDOW_S


class BadPort(ConfirmError):
    """`--confirm-console` is a malformed serial URL."""


def confirm_spec(
    port: str | None,
    expect: str | None,
    baud: int | None,
    timeout_s: float | None,
    window_s: float | None = None,
):
    """`None` when no confirmation was asked for; `ConfirmError` on a bad combination."""
    if all(v is None for v in (port, expect, baud, timeout_s, window_s)):
        return None
    if port is None or expect is None:
        raise ConfirmError("--confirm-console and --expect must be given together")
    problem = serial_url.port_url_problem(port)
    if problem is not None:
        raise BadPort(f"bad --confirm-console: {problem}")
    try:
        pattern = serial_capture.compile_until(expect)
    except ValueError as err:
        raise ConfirmError(str(err).replace("--until", "--expect")) from err
    baud = DEFAULT_BAUD if baud is None else baud
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    window_s = DEFAULT_WINDOW_S if window_s is None else window_s
    if baud <= 0 or not timeout_s > 0 or not window_s > 0:
        raise ConfirmError(
            "--confirm-baud, --confirm-timeout and --confirm-window must be greater than 0"
        )
    return ConfirmSpec(port, pattern, baud, timeout_s, window_s)


class Drain:
    """Reads and discards everything the console delivers while J-Link runs.

    Stale rfc2217 lines still in flight when the pulse is sent would otherwise
    arrive after J-Link exits and could match `--expect`. `stop()` is called the
    moment J-Link returns; whatever arrives after that is post-pulse data. (A
    fixed discard after exit would instead eat a real banner: one measured
    0.088 s after J-Link exit.)"""

    def __init__(self, ser) -> None:
        self._ser = ser
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._halt.is_set():
            try:
                got = self._ser.read(max(1, int(getattr(self._ser, "in_waiting", 0) or 0)))
            except Exception:  # noqa: BLE001 -- observe() reports a dead link
                return
            if not got:
                time.sleep(0.005)

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(timeout=1.0)


def open_console(spec: ConfirmSpec):
    """Open the console (before the pulse). `MonitorError` on failure."""
    from tan.commands import monitor_session  # noqa: PLC0415 (pyserial is optional)

    return monitor_session.open_port(spec.port, spec.baud, capture=True)


def observe(ser, spec: ConfirmSpec) -> dict[str, Any]:
    """After J-Link exited: drop what queued up and wait for `--expect`.
    Closes `ser`. Returns the `data.console` block. The FIRST matching line must arrive within
    `window_s` of J-Link exiting: a later one (e.g. an RTC-alarm wake out of STOP) is recorded as
    `lateMatchAtSeconds` and is not a reset."""
    try:
        try:
            ser.reset_input_buffer()
        except Exception:  # noqa: BLE001 -- a port without it just keeps its queue
            pass
        res = serial_capture.capture(
            ser, duration_s=max(spec.timeout_s, spec.window_s), until=spec.expect
        )
        latency = round(res.elapsed_s, 3) if res.matched else None
        in_window = res.matched and res.elapsed_s <= spec.window_s
        block = {
            "port": spec.port, "baud": spec.baud, "observed": in_window,
            "windowSeconds": spec.window_s, "matchLatencySeconds": latency if in_window else None,
            "matchedLine": res.matched_line if in_window else None,
            "elapsedSeconds": round(res.elapsed_s, 3),
            "bytesSeen": res.bytes_seen, "bytesSeenTail": res.tail,
        }
        if res.matched and not in_window:
            block["lateMatchAtSeconds"] = latency
            block["lateMatchedLine"] = res.matched_line
        return block
    except OSError as err:
        return {"port": spec.port, "baud": spec.baud, "observed": False, "error": str(err)}
    finally:
        try:
            ser.close()
        except OSError:
            pass
