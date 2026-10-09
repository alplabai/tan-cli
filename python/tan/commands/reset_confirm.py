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

from dataclasses import dataclass
from typing import Any

from tan.core import serial_capture

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT_S = 30.0


class ConfirmError(ValueError):
    """A bad `--confirm-*` / `--expect` combination."""


@dataclass(frozen=True)
class ConfirmSpec:
    port: str
    expect: Any  # compiled regex
    baud: int
    timeout_s: float


def confirm_spec(port: str | None, expect: str | None, baud: int | None, timeout_s: float | None):
    """`None` when no confirmation was asked for; `ConfirmError` on a bad combination."""
    if port is None and expect is None and baud is None and timeout_s is None:
        return None
    if port is None or expect is None:
        raise ConfirmError("--confirm-console and --expect must be given together")
    try:
        pattern = serial_capture.compile_until(expect)
    except ValueError as err:
        raise ConfirmError(str(err).replace("--until", "--expect")) from err
    baud = DEFAULT_BAUD if baud is None else baud
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    if baud <= 0 or not timeout_s > 0:
        raise ConfirmError("--confirm-baud and --confirm-timeout must be greater than 0")
    return ConfirmSpec(port, pattern, baud, timeout_s)


def open_console(spec: ConfirmSpec):
    """Open the console (before the pulse). `MonitorError` on failure."""
    from tan.commands import monitor_session  # noqa: PLC0415 (pyserial is optional)

    return monitor_session.open_port(spec.port, spec.baud, capture=True)


def observe(ser, spec: ConfirmSpec) -> dict[str, Any]:
    """After J-Link exited: drop what queued up and wait for `--expect`.
    Closes `ser`. Returns the `data.console` block."""
    try:
        try:
            ser.reset_input_buffer()
        except Exception:  # noqa: BLE001 -- a port without it just keeps its queue
            pass
        res = serial_capture.capture(ser, duration_s=spec.timeout_s, until=spec.expect)
        return {
            "port": spec.port, "baud": spec.baud, "observed": res.matched,
            "matchedLine": res.matched_line, "elapsedSeconds": round(res.elapsed_s, 3),
            "bytesSeen": res.bytes_seen, "bytesSeenTail": res.tail,
        }
    except OSError as err:
        return {"port": spec.port, "baud": spec.baud, "observed": False, "error": str(err)}
    finally:
        try:
            ser.close()
        except OSError:
            pass
