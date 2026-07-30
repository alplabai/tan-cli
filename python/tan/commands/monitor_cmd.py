# SPDX-License-Identifier: Apache-2.0
"""`tan monitor` -- open a serial console to the attached board.

Port of `scripts/alp_cli/monitor.py` (73 lines): a thin front door over
pyserial's `miniterm`. Port comes from `--port`; baud from `--baud` (default
115200, the SDK-wide console default).

There is no safe cross-platform guess for the port itself (COMx vs
`/dev/ttyUSBx` vs `/dev/cu.*`), so when no port is given -- or the requested
one does not exist -- this command lists every serial port pyserial can see
and refuses instead of hanging on a wrong device.

Board-context port resolution (a `console:` block in the project's
`system-manifest.yaml`) is deliberately NOT implemented here either: the board
schema and orchestrator do not emit one today. Teach this verb to read it once
they do.

**No alp-sdk checkout required, unlike `model`.** The oracle's `monitor.py`
imports nothing from alp-sdk beyond `alp_cli._workspace.python_exe`, itself
just `sys.executable` -- the running interpreter. This port does NOT read
`sys.executable` directly, though: under PyInstaller `sys.executable` IS
`tan` itself, so spawning it would just re-enter this CLI instead of
launching miniterm -- the same reasoning `build_cmd.py`'s `_planner_python`
and `generate_cmd.py` already carry, spelled out there so it need not be
re-argued per call site. This port reuses that same function, a PATH name
(`python`/`python3`) never `sys.executable`, when frozen or when
`sys.executable` is empty (an embedded interpreter can report ""); the
running interpreter is still preferred otherwise, since it is guaranteed to
have `serial` importable already. Either way no SDK root is resolved, so
`tan monitor` no longer requires a resolvable alp-sdk checkout the way the
retired Rust forwarder did (`crates/tan-cli/src/commands/sdk_cli.rs` resolves
one unconditionally for every forward, `monitor` included, purely as an
artifact of sharing one function with `model`/`new-som`/`faultdecode`) -- a
deliberate, documented improvement, not a regression: `monitor` never read
anything an SDK root would supply.

**Exit code on a failed miniterm run is `RuntimeFailure` (1) regardless of the
child's own exit code** -- mirroring the shipped Rust forwarder
(`sdk_cli::run`'s `s.code().unwrap_or(1)` branch always maps to
`ExitCode::RuntimeFailure`), which is the customer-facing contract today, NOT
the oracle's literal `raise SystemExit(rc)` passthrough of whatever code
miniterm returned. The actual child code still reaches the issue message.
"""

from __future__ import annotations

import subprocess
import sys

import typer

from tan.commands.build_cmd import _planner_python
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: The SDK-wide console default, matching `monitor.py::DEFAULT_BAUD`.
DEFAULT_BAUD = 115200

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


class MonitorError(Exception):
    """A refusal whose issue code and exit code are already decided."""

    def __init__(self, code: str, message: str, exit_code: ExitCode, data: dict) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.data = data


def _available_ports() -> list[tuple[str, str]]:
    """`[(device, description)]` for every serial port pyserial can see."""
    from serial.tools import list_ports  # noqa: PLC0415 (optional at runtime)

    return [(p.device, p.description or "") for p in list_ports.comports()]


def _ports_data(ports: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"device": device, "description": description} for device, description in ports]


def _refuse_listing_ports(reason: str) -> MonitorError:
    """Port of `monitor.py::_die_listing_ports`: the reason plus every serial
    port pyserial can see, folded into one issue message so `--format json`
    carries the same information the oracle prints line-by-line to stderr."""
    ports = _available_ports()
    if ports:
        listing = "; ".join(f"{d}  {desc}".rstrip() for d, desc in ports)
        message = f"{reason} -- available serial ports: {listing}"
    else:
        message = f"{reason} -- no serial ports detected on this host."
    return MonitorError(
        "monitor.no-port",
        message,
        ExitCode.RUNTIME_FAILURE,
        {"schemaVersion": DATA_SCHEMA_VERSION, "availablePorts": _ports_data(ports)},
    )


def _run_monitor(port: str | None, baud: int) -> tuple[dict, list[Issue], ExitCode]:
    # Frozen (PyInstaller) or an embedded interpreter with no reportable
    # `sys.executable`: fall back to a PATH name, mirroring
    # `build_cmd._planner_python` -- NOT `sys.executable`, which under a
    # PyInstaller freeze IS `tan` itself and would just re-enter this CLI.
    using_this_interpreter = not getattr(sys, "frozen", False) and bool(sys.executable)
    python = sys.executable if using_this_interpreter else _planner_python()

    if using_this_interpreter:
        # This precheck only proves the interpreter about to be spawned --
        # THIS one -- has pyserial. It says nothing about a PATH `python`
        # resolved via `_planner_python()`, so skip it there; a missing
        # pyserial in the child surfaces as the child's own reported failure.
        try:
            import serial  # noqa: F401, PLC0415 (validates pyserial is installed)
        except ImportError as err:
            raise MonitorError(
                "monitor.pyserial-missing",
                "pyserial is required. Install via `pip install pyserial`.",
                ExitCode.RUNTIME_FAILURE,
                {"schemaVersion": DATA_SCHEMA_VERSION},
            ) from err

    if port is None:
        raise _refuse_listing_ports("no --port given")
    if port not in {device for device, _ in _available_ports()}:
        raise _refuse_listing_ports(f"port '{port}' not found")

    print(f"monitor: {port} @ {baud} (Ctrl+] to quit)", file=sys.stderr)
    try:
        rc = subprocess.run(
            [python, "-m", "serial.tools.miniterm", port, str(baud)]
        ).returncode
    except OSError as err:
        raise MonitorError(
            "monitor.launch-failed",
            f"failed to launch `{python} -m serial.tools.miniterm`: {err}",
            ExitCode.RUNTIME_FAILURE,
            {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud},
        ) from err

    data = {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud}
    if rc != 0:
        return (
            data,
            [
                Issue(
                    "monitor.failed",
                    "error",
                    f"`tan monitor` exited with code {rc} (see log above).",
                )
            ],
            ExitCode.RUNTIME_FAILURE,
        )
    return data, [], ExitCode.SUCCESS


def monitor(
    port: str = typer.Option(
        None,
        "--port",
        help="Serial port (COM7, /dev/ttyUSB0, /dev/cu.usbmodem...).",
    ),
    baud: int = typer.Option(
        DEFAULT_BAUD, "--baud", show_default=True, help="Baud rate."
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Open a serial console to the board."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    def finish(data: dict, issues: list[Issue], exit_code: ExitCode) -> None:
        if json_mode:
            emit(
                Envelope(
                    "monitor", Project(root=None, board_yaml=None), data, issues, exit_code
                )
            )
        else:
            for issue in issues:
                print(f"monitor: {issue.message}", file=sys.stderr)
        raise typer.Exit(int(exit_code))

    try:
        data, issues, exit_code = _run_monitor(port, baud)
    except MonitorError as err:
        finish(err.data, [Issue(err.code, "error", err.message)], err.exit_code)
        return
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        finish(
            {"schemaVersion": DATA_SCHEMA_VERSION},
            [
                Issue(
                    "monitor.internal-failure",
                    "error",
                    f"monitor failed unexpectedly: {type(err).__name__}: {err}",
                )
            ],
            ExitCode.INTERNAL_FAILURE,
        )
        return

    finish(data, issues, exit_code)
