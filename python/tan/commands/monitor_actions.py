# SPDX-License-Identifier: Apache-2.0
"""`tan monitor --capture` input and baud-switch options (tan-cli#1451).

Validation of `--send`/`--send-after`/`--send-on`/`--send-gap`/`--reopen-at`/
`--on` into a `serial_actions.ActionSpec`, and the glue that gives the capture
loop a `CaptureActions` bound to the open port (reopening through
`monitor_session.open_port`, so it behaves the same over a local tty and over
`rfc2217://`). The logic itself is `tan.core.serial_actions`.
"""

from __future__ import annotations

import re

from tan.commands.monitor_cmd import DATA_SCHEMA_VERSION, MonitorError
from tan.core import serial_actions, uboot_breakin
from tan.exit_codes import ExitCode


def _bad(message: str) -> MonitorError:
    return MonitorError(
        "monitor.capture-bad-option",
        message,
        ExitCode.VALIDATION_FAILURE,
        {"schemaVersion": DATA_SCHEMA_VERSION},
    )


def _regex(text: str, flag: str) -> re.Pattern[str]:
    if not text:
        raise ValueError(f"{flag} must not be empty")
    try:
        return re.compile(text)
    except re.error as err:
        raise ValueError(f"{flag} is not a valid regex: {err}") from err


def _check(capture, sends, send_after, send_on, send_gap, reopen_at, on) -> None:
    if not capture:
        raise ValueError("--send/--send-after/--send-on/--send-gap/--reopen-at/--on require --capture")
    if not sends and any(v is not None for v in (send_after, send_on, send_gap)):
        raise ValueError("--send-after/--send-on/--send-gap require --send")
    if send_after is not None and send_on is not None:
        raise ValueError("--send-after and --send-on are mutually exclusive")
    if (reopen_at is None) != (on is None):
        raise ValueError("--reopen-at and --on must be given together")
    if send_after is not None and send_after < 0:
        raise ValueError("--send-after must not be negative")
    if send_gap is not None and send_gap < 0:
        raise ValueError("--send-gap must not be negative")
    if reopen_at is not None and reopen_at <= 0:
        raise ValueError("--reopen-at must be greater than 0")


def action_opts(
    capture: bool,
    send: list[str] | None,
    send_after: float | None,
    send_on: str | None,
    send_gap: float | None,
    reopen_at: int | None,
    on: str | None,
) -> serial_actions.ActionSpec | None:
    """Validate the input / baud-switch options. `None` when none was given.

    All of them need `--capture`; `--send-after`/`--send-on`/`--send-gap` need
    `--send` (and the two triggers exclude each other); `--reopen-at` and
    `--on` need each other. Without a trigger the `--send` items go out at the
    start of the capture."""
    sends = list(send or [])
    values = (send_after, send_on, send_gap, reopen_at, on)
    if not sends and all(v is None for v in values):
        return None
    try:
        _check(capture, sends, send_after, send_on, send_gap, reopen_at, on)
        return serial_actions.ActionSpec(
            sends=tuple(uboot_breakin.parse_escaped(item, "--send") for item in sends),
            send_after_s=send_after,
            send_on=_regex(send_on, "--send-on") if send_on is not None else None,
            send_gap_s=serial_actions.DEFAULT_SEND_GAP_S if send_gap is None else send_gap,
            reopen_baud=reopen_at,
            reopen_on=_regex(on, "--on") if on is not None else None,
        )
    except ValueError as err:
        raise _bad(str(err)) from err


def build(spec: serial_actions.ActionSpec, ser, port: str):
    """The `CaptureActions` for the open `ser`: sends honour the rfc2217 write
    guard; a reopen goes through `monitor_session.open_port`."""
    from tan.commands import monitor_session  # noqa: PLC0415 (it imports this module's peers)

    def write(current, data: bytes) -> None:
        target = (
            monitor_session._GuardedWrites(current)
            if getattr(current, "_tan_guard_writes", False)
            else current
        )
        target.write(data)

    def reopen(baud: int):
        return monitor_session.open_port(port, baud, capture=True)

    return serial_actions.CaptureActions(spec, ser, reopen, write=write)


def action_error(err: serial_actions.ActionError, port: str, data: dict) -> MonitorError:
    code = "monitor.capture-send-failed" if err.kind == "send" else "monitor.capture-reopen-failed"
    return MonitorError(
        code,
        f"{err} (port '{port}')",
        ExitCode.RUNTIME_FAILURE,
        data,
    )


def report(actions: serial_actions.CaptureActions) -> dict:
    """`data.capture.actions`: what fired, and what is still pending."""
    spec = actions.spec
    reopened = any(e["action"] == "reopen" for e in actions.events)
    return {
        "events": actions.events,
        "sendsPending": actions.sends_pending,
        "reopenPending": spec.reopen_baud is not None and not reopened,
    }
