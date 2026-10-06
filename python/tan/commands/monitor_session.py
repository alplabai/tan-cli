# SPDX-License-Identifier: Apache-2.0
"""`tan monitor --break-uboot`: the in-process session (tan-cli#1315).

Split out of `monitor_cmd.py` to keep that file small: option validation, the
port open, the break-in call, and the hand-over to an in-process
`serial.tools.miniterm.Miniterm` on the SAME open port. Reopening the port
after the break-in could toggle DTR/RTS on a USB-UART adapter and reset the
board, losing the prompt just caught, so the held port is never closed
between the loop and the console. Every path out of this module closes it.

`monitor_cmd` imports this module lazily (it needs `MonitorError`, defined
there), so the plain `tan monitor` path never loads it.
"""

from __future__ import annotations

import re
import sys

from tan.commands.monitor_cmd import DATA_SCHEMA_VERSION, MonitorError, _pyserial_missing
from tan.core import console_filter as console_filter_mod
from tan.core import serial_capture, uboot_breakin
from tan.envelope import Issue
from tan.exit_codes import ExitCode

#: A stalled rfc2217:// or socket:// endpoint must not block a key write past
#: the break-in deadline; one write waits at most this long.
WRITE_TIMEOUT_S = 1.0

BreakOpts = tuple[bytes, bytes, float]


def _bad_option(message: str) -> MonitorError:
    return MonitorError(
        "monitor.break-bad-option",
        message,
        ExitCode.VALIDATION_FAILURE,
        {"schemaVersion": DATA_SCHEMA_VERSION},
    )


def break_opts(
    break_uboot: bool, key: str | None, prompt: str | None, timeout_s: float | None
) -> BreakOpts | None:
    """Validate `--break-uboot` and its companions.

    `--break-key`/`--prompt`/`--break-timeout` mean nothing without
    `--break-uboot`, so giving one alone is refused rather than ignored.
    """
    if not break_uboot:
        if key is not None or prompt is not None or timeout_s is not None:
            raise _bad_option("--break-key/--prompt/--break-timeout require --break-uboot")
        return None
    timeout_s = uboot_breakin.DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        if not timeout_s > 0:
            raise ValueError("--break-timeout must be greater than 0")
        return (
            uboot_breakin.parse_escaped(
                uboot_breakin.DEFAULT_KEY.decode() if key is None else key, "--break-key"
            ),
            uboot_breakin.parse_escaped(
                uboot_breakin.DEFAULT_PROMPT.decode() if prompt is None else prompt, "--prompt"
            ),
            timeout_s,
        )
    except ValueError as err:
        raise _bad_option(str(err)) from err


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def open_port(port: str, baud: int, capture: bool = False):
    """Open `port` in-process (local tty, by-id path or any pyserial URL) the
    way miniterm does: no explicit DTR/RTS, pyserial's open-time defaults."""
    try:
        import serial  # noqa: PLC0415 (optional at runtime)
    except ImportError as err:
        raise _pyserial_missing() from err
    try:
        return serial.serial_for_url(
            port,
            baud,
            timeout=uboot_breakin.DEFAULT_INTERVAL_S,
            write_timeout=WRITE_TIMEOUT_S,
        )
    except (OSError, ValueError, serial.SerialException) as err:
        data = {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud}
        if capture:
            raise MonitorError(
                "monitor.capture-open-failed",
                f"could not open '{port}' for --capture: {err}",
                ExitCode.RUNTIME_FAILURE,
                data,
            ) from err
        raise MonitorError(
            "monitor.break-open-failed",
            f"could not open '{port}' for --break-uboot: {err}",
            ExitCode.RUNTIME_FAILURE,
            data,
        ) from err


def break_in(ser, port: str, baud: int, opts: BreakOpts) -> dict:
    """Run the loop on the open `ser`; the port stays OPEN either way (the
    caller owns closing it). Returns the envelope's `breakIn` block."""
    key, prompt, timeout_s = opts
    try:
        result = uboot_breakin.break_into_uboot(ser, key=key, prompt=prompt, timeout_s=timeout_s)
    except OSError as err:  # includes pyserial's write/read timeouts
        raise MonitorError(
            "monitor.break-io-failed",
            f"serial I/O failed on '{port}' during --break-uboot: {err}",
            ExitCode.RUNTIME_FAILURE,
            {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud},
        ) from err
    return {
        "caught": result.caught,
        "elapsedSeconds": round(result.elapsed_s, 3),
        "timeoutSeconds": timeout_s,
        "bytesSeen": result.bytes_seen,
        "bytesSeenTail": result.tail,
    }


def attach_miniterm(ser, json_mode: bool, console_filter: str = "colors") -> int:
    """Run the interactive console on the ALREADY-OPEN `ser` (no reopen).

    Same session settings as `serial.tools.miniterm.main` except for the
    options tan does not expose: exit Ctrl+], menu Ctrl+T, UTF-8, CRLF. The
    output filter is `console_filter` (`--filter`, default tan's `colors`:
    SGR colours render, every other escape is neutralised; see
    `tan.core.console_filter`). Under `--format json` the
    `Console` is built with `sys.stdout` pointed at stderr, the same rule
    `monitor_cmd._child_stdout` states for the spawned path.

    Closes `ser` on every path, including a `Console` that cannot be built
    (it reads termios attributes off stdin).
    """
    term = None
    real_stdout = sys.stdout
    try:
        try:
            from serial.tools import miniterm  # noqa: PLC0415 (optional at runtime)
        except ImportError as err:
            raise _pyserial_missing() from err
        try:
            import termios  # noqa: PLC0415 (POSIX only)

            console_errors: tuple[type[BaseException], ...] = (OSError, ValueError, termios.error)
        except ImportError:
            console_errors = (OSError, ValueError)
        try:
            miniterm.TRANSFORMATIONS["colors"] = console_filter_mod.ColorsFilter
            if json_mode:
                sys.stdout = sys.__stderr__ if sys.__stderr__ is not None else sys.stderr
            try:
                term = miniterm.Miniterm(ser, echo=False, eol="crlf", filters=[console_filter])
            finally:
                sys.stdout = real_stdout
            term.exit_character = chr(0x1D)
            term.menu_character = chr(0x14)
            term.raw = False
            term.set_rx_encoding("UTF-8")
            term.set_tx_encoding("UTF-8")
            term.start()
            try:
                term.join(True)
            except KeyboardInterrupt:
                pass
            term.join()
        except console_errors as err:
            raise MonitorError(
                "monitor.launch-failed",
                f"failed to run the console on the open port: {err}",
                ExitCode.RUNTIME_FAILURE,
                {"schemaVersion": DATA_SCHEMA_VERSION},
            ) from err
    finally:
        if term is not None:
            term.close()
        ser.close()
    return 0


def run(
    port: str,
    baud: int,
    json_mode: bool,
    opts: BreakOpts,
    non_interactive: bool,
    console_filter: str = "colors",
) -> tuple[dict, list[Issue], ExitCode]:
    """`--break-uboot` end to end. `--non-interactive` stops after the
    break-in (exit 0 if caught); otherwise the console takes over the
    still-open port."""
    base = {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud}
    if not non_interactive and not _stdin_is_tty():
        raise MonitorError(
            "monitor.no-tty",
            "the interactive console needs a terminal on stdin; pass --non-interactive "
            "to stop after the break-in.",
            ExitCode.RUNTIME_FAILURE,
            base,
        )
    ser = open_port(port, baud)
    handed_over = False
    try:
        block = break_in(ser, port, baud, opts)
        data = {**base, "breakIn": block}
        if not block["caught"]:
            return (
                data,
                [Issue(
                    "monitor.break-timeout",
                    "error",
                    f"no U-Boot prompt within {opts[2]}s on {port}; "
                    "power-cycle the board and retry, or raise --break-timeout.",
                )],
                ExitCode.RUNTIME_FAILURE,
            )
        print(f"monitor: caught U-Boot prompt after {block['elapsedSeconds']}s", file=sys.stderr)
        if non_interactive:
            return data, [], ExitCode.SUCCESS
        handed_over = True
    except MonitorError as err:
        err.data = {**err.data, **(data if "data" in locals() else {})}
        raise
    finally:
        if not handed_over:
            ser.close()
    # attach_miniterm owns (and always closes) `ser` from here.
    print(f"monitor: {port} @ {baud} (Ctrl+] to quit)", file=sys.stderr)
    try:
        attach_miniterm(ser, json_mode, console_filter)
    except MonitorError as err:
        err.data = {**err.data, **data}
        raise
    return data, [], ExitCode.SUCCESS


#: (compiled --until or None, duration seconds, --log path or None)
CaptureOpts = tuple["re.Pattern[str] | None", float, "str | None"]


def capture_opts(
    capture: bool, duration: float | None, until: str | None, log: str | None
) -> CaptureOpts | None:
    """Validate `--capture`/`--duration`/`--until`/`--log` (tan-cli#1324).
    The companions are refused without `--capture` rather than ignored."""
    try:
        if not capture:
            if duration is not None or until is not None or log is not None:
                raise ValueError("--duration/--until/--log require --capture")
            return None
        pattern = serial_capture.compile_until(until) if until is not None else None
        if duration is None:
            if pattern is None:
                raise ValueError("--capture needs --duration and/or --until")
            duration = serial_capture.DEFAULT_UNTIL_DURATION_S
        if not duration > 0:
            raise ValueError("--duration must be greater than 0")
    except ValueError as err:
        raise MonitorError(
            "monitor.capture-bad-option",
            str(err),
            ExitCode.VALIDATION_FAILURE,
            {"schemaVersion": DATA_SCHEMA_VERSION},
        ) from err
    return pattern, duration, log


def run_capture(
    port: str, baud: int, cap: CaptureOpts, opts: BreakOpts | None
) -> tuple[dict, list[Issue], ExitCode]:
    """Headless capture on one in-process port: optional break-in first, then
    read for the duration / until the regex matches. No TTY is needed, and
    the log (`--log`) always receives the RAW bytes (no console filter)."""
    pattern, duration, log = cap
    data: dict = {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud}
    sink = None
    if log is not None:
        try:
            sink = open(log, "wb")  # noqa: SIM115 (closed in the finally below)
        except OSError as err:
            raise MonitorError(
                "monitor.capture-log-failed",
                f"cannot write --log '{log}': {err}",
                ExitCode.WRITE_FAILURE,
                data,
            ) from err
    ser = None
    try:
        ser = open_port(port, baud, capture=True)
        if opts is not None:
            data["breakIn"] = break_in(ser, port, baud, opts)
            if not data["breakIn"]["caught"]:
                return (
                    data,
                    [Issue("monitor.break-timeout", "error",
                           f"no U-Boot prompt within {opts[2]}s on {port}.")],
                    ExitCode.RUNTIME_FAILURE,
                )
        try:
            res = serial_capture.capture(ser, duration_s=duration, until=pattern, sink=sink)
        except OSError as err:
            raise MonitorError(
                "monitor.capture-io-failed",
                f"serial I/O failed on '{port}' during --capture: {err}",
                ExitCode.RUNTIME_FAILURE,
                data,
            ) from err
    finally:
        if ser is not None:
            ser.close()
        if sink is not None:
            sink.close()
    data["capture"] = {
        "matched": res.matched if pattern is not None else None,
        "matchedLine": res.matched_line,
        "elapsedSeconds": round(res.elapsed_s, 3),
        "durationSeconds": duration,
        "bytesSeen": res.bytes_seen,
        "bytesSeenTail": res.tail,
        "logFile": log,
    }
    if pattern is not None and not res.matched:
        return (
            data,
            [Issue("monitor.capture-timeout", "error",
                   f"no line matched --until within {duration}s on {port}.")],
            ExitCode.RUNTIME_FAILURE,
        )
    return data, [], ExitCode.SUCCESS
