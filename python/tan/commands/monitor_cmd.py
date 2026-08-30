# SPDX-License-Identifier: Apache-2.0
"""`tan monitor` -- open a serial console to the attached board.

Port of `scripts/alp_cli/monitor.py` (73 lines): a thin front door over
pyserial's `miniterm`. Port comes from `--port`; baud from `--baud` (default
115200, the SDK-wide console default).

There is no safe cross-platform guess for the port itself (COMx vs
`/dev/ttyUSBx` vs `/dev/cu.*`), so when no port is given -- or the requested
one does not exist -- this command lists every serial port pyserial can see
and refuses instead of hanging on a wrong device.

"Does not exist" is the stated rule, and until tan-cli#569 the code was
stricter than it: the gate tested membership in `comports()`, which refuses
every port pyserial can OPEN but does not ENUMERATE. `_port_is_usable` is now
the gate, with three accepting arms -- enumerated (plus the `\\\\.\\` alias of
tan-cli#701), a character device on this host (`/dev/serial/by-id/...`
symlinks, the case #569 was filed on), or one of pyserial's own URL schemes
(`socket://`, `rfc2217://`, ... , which have no local device path at all).

Board-context port resolution -- filling in `--port` from the current project
instead of asking for it -- is deliberately NOT implemented here, and it is
not simply unstarted (tan-cli#255): the build-plan already carries a
`slices[].debug.console` selector per slice (`build-plan-v1.schema.json`,
issue #610 §4; computed here too, at `tan/planner/buildplan.py::_slice_debug`,
and independently in alp-sdk's own `scripts/alp_orchestrate/buildplan.py`),
resolving to `"uart"` / `"ram"` / `"linux"` / `null`. That is a console
BACKEND CLASS, not a port: it says a slice's console is a UART (as opposed to
a RAM console read over SWD, or a Linux tty), never which host-visible device
that UART shows up as. Nothing in `board.yaml` or the build-plan carries a
VID:PID, serial number, or platform-specific device path for a board's
console UART, so `debug.console == "uart"` still leaves every USB-serial
adapter on the bench indistinguishable to this host OS -- reading it would not
let this command fill in `--port`. Teach this verb to read a real per-board
physical-port fact once metadata carries one; `debug.console` alone is not
that fact.

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

import os
import stat
import subprocess
import sys
from pathlib import Path

import typer

from tan.commands.build_cmd import _planner_python
from tan.core.subprocess_env import spawn_env
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

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


def _pyserial_missing() -> MonitorError:
    """The one spelling of "pyserial is not installed".

    The hint names the EXTRA rather than the bare distribution because that is
    the supported way to get it: pyserial is declared in
    `[project.optional-dependencies] monitor`, not in `dependencies`. A frozen
    `--onefile` build resolves it at BUILD time, so a customer holding a binary
    built without the extra cannot pip-install their way out -- hence the second
    sentence, which is the only actionable thing to tell them.
    """
    return MonitorError(
        "monitor.pyserial-missing",
        "pyserial is required for `tan monitor`. Install it with "
        '`pip install "alp-tan[monitor]"`. A frozen `tan` binary bundles it at '
        "build time, so a binary built without that extra cannot gain it here.",
        ExitCode.RUNTIME_FAILURE,
        {"schemaVersion": DATA_SCHEMA_VERSION},
    )


def _available_ports() -> list[tuple[str, str]]:
    """`[(device, description)]` for every serial port pyserial can see.

    The import is guarded HERE, not only at the caller, because this is the one
    choke point every port-listing path routes through -- and because
    `_run_monitor`'s precheck is deliberately skipped on a FROZEN build (there
    is no `sys.executable` worth validating there). On a `--onefile` binary
    built without the `monitor` extra this line is therefore the FIRST place
    pyserial is touched, and it is reached IN-PROCESS before any child is
    spawned. Left unguarded the ImportError escaped as an unexpected exception
    and surfaced as `monitor.internal-failure` at exit 5 -- "tan has a bug" --
    for what is simply an optional dependency the customer never installed.
    """
    try:
        from serial.tools import list_ports  # noqa: PLC0415 (optional at runtime)
    except ImportError as err:
        raise _pyserial_missing() from err

    return [(p.device, p.description or "") for p in list_ports.comports()]


#: Windows' documented device-namespace prefix. `\\.\COM38` is the spelling
#: Microsoft documents for COM10 and above, and pyserial's win32 backend opens
#: it -- `serial/serialwin32.py` hands any string that does not
#: `startswith('COM')` straight to `CreateFile`. What it is NOT is a spelling
#: `comports()` ever REPORTS: `serial/tools/list_ports_windows.py` builds each
#: `ListPortInfo` from the registry `PortName`, which is the bare `COM38`. So a
#: membership test against `comports()` refuses a port pyserial would have
#: opened, while listing that same port in its own "not found" message
#: (tan-cli#701).
_WINDOWS_DEVICE_PREFIX = "\\\\.\\"


def _port_aliases(port: str) -> set[str]:
    """Every spelling of `port` that `comports()` might report it under.

    Deliberately narrow: this resolves a spelling pyserial itself accepts back
    to the bare device name, rather than widening the gate. A port that is
    genuinely absent is still refused -- see
    `test_a_unc_port_whose_bare_form_is_absent_is_still_refused`.
    """
    aliases = {port}
    if port.startswith(_WINDOWS_DEVICE_PREFIX):
        aliases.add(port[len(_WINDOWS_DEVICE_PREFIX):])
    return aliases


def _url_handler_prefixes() -> frozenset[str]:
    """pyserial's own URL schemes, read from `serial.urlhandler` rather than
    hardcoded here (tan-cli#569).

    `serial.serial_for_url` dispatches on these, and miniterm -- which this
    command spawns -- goes through it. `comports()` enumerates none of them, so
    a membership test refuses every one, and for `socket://`/`rfc2217://` there
    is no device path to fall back on: the port does not exist locally at all.

    Derived by listing `serial/urlhandler/protocol_*.py`, measured on pyserial
    3.5 as `alt://`, `cp2110://`, `hwgrep://`, `loop://`, `rfc2217://`,
    `socket://`, `spy://`. Read from the installed package so a pyserial that
    adds or drops one moves this set with it. An empty result (no such package)
    yields an empty set, which leaves the gate exactly as strict as before.
    """
    try:
        import pkgutil  # noqa: PLC0415
        import serial.urlhandler  # noqa: PLC0415
    except ImportError:
        return frozenset()
    return frozenset(
        f"{module.name[len('protocol_'):]}://"
        for module in pkgutil.iter_modules(serial.urlhandler.__path__)
        if module.name.startswith("protocol_")
    )


def _is_openable_device(port: str) -> bool:
    """Whether `port` is a character device on this host.

    THE case tan-cli#569 was filed on: `/dev/serial/by-id/usb-..._-if00`
    symlinks. `serial.tools.list_ports` reports raw nodes (`/dev/ttyACM0`) and
    never the by-id path, while `serial.Serial()` opens the symlink fine --
    measured on this host against a real Artery AT32 adapter. On a bench with
    two identical adapters whose `ttyACM` ordering swaps across reboots, the
    by-id path is the only stable name for the intended board, so refusing it
    forces the operator onto the unstable one.

    `os.stat` follows symlinks, which is the point. Any `OSError` -- absent
    path, permission, an embedded NUL -- reads as "not a device", so a
    genuinely missing port is still refused rather than becoming a traceback.
    Windows reaches this too and answers False for `COM7` (no stat-able
    character device there), which is correct: `_port_aliases` is what covers
    Windows, and this must not quietly widen that platform's gate.
    """
    try:
        return stat.S_ISCHR(os.stat(port).st_mode)
    except (OSError, ValueError):
        return False


def _port_is_usable(port: str, enumerated: set[str]) -> bool:
    """The tan-cli#569 gate: openable, not merely enumerated.

    Three accepting arms -- enumerated (including the `\\\\.\\` alias of
    tan-cli#701), a character device on this host, or one of pyserial's own URL
    schemes. A name matching none of the three is still refused, which is the
    only reason this function is worth having over `True`.
    """
    if _port_aliases(port) & enumerated:
        return True
    if _is_openable_device(port):
        return True
    return any(port.startswith(scheme) for scheme in _url_handler_prefixes())


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


def _child_stdout(json_mode: bool):
    """What miniterm's stdout is wired to (tan-cli#491 defect 6).

    TEXT mode: `None`, i.e. inherit -- board traffic on tan's stdout is the
    whole point of an interactive console, and redirecting it would break
    `tan monitor > board.log`.

    `--format json`: the board's bytes must NOT land on stdout. pyserial's
    miniterm writes every received byte through `Console.write` to its own
    `sys.stdout`, so an inherited stdout puts board traffic AHEAD of the
    envelope and a consumer's whole-stdout `JSON.parse` fails on an `ok: true`,
    exit-0 run -- reproduced end to end against a real pty. Same rule
    `flash_cmd` states for itself: nothing but the single JSON envelope may
    reach stdout under `--format json`. The traffic is kept, on stderr, where
    this command's own `monitor: <port> @ <baud>` banner and miniterm's own
    banner already go.

    `sys.__stderr__`, not `sys.stderr`: under `--format json` `cli.main` binds
    `sys.stderr` to a `_TeeStderr`, which implements only `write`/`flush`/
    `getvalue` -- it has no `fileno()`, and `subprocess` needs a real one to
    hand the child. `sys.__stderr__` is the interpreter's original stream and
    is unaffected by that rebinding. It can still be `None` (pythonw, an
    embedded interpreter) or closed, hence the guard; `DEVNULL` is the last
    resort, because dropping the board's bytes is bad and putting them on
    stdout is the defect.
    """
    if not json_mode:
        return None
    stream = sys.__stderr__
    try:
        if stream is not None and stream.fileno() >= 0:
            return stream
    except (AttributeError, OSError, ValueError):
        pass
    return subprocess.DEVNULL


def _run_monitor(
    port: str | None, baud: int, json_mode: bool
) -> tuple[dict, list[Issue], ExitCode]:
    # Frozen (PyInstaller) or an embedded interpreter with no reportable
    # `sys.executable`: fall back to a PATH name, mirroring
    # `build_cmd._planner_python` -- NOT `sys.executable`, which under a
    # PyInstaller freeze IS `tan` itself and would just re-enter this CLI.
    using_this_interpreter = not getattr(sys, "frozen", False) and bool(sys.executable)
    python = (
        sys.executable
        if using_this_interpreter
        else _planner_python(str(Path.cwd()), None)
    )

    if using_this_interpreter:
        # This precheck only proves the interpreter about to be spawned --
        # THIS one -- has pyserial. It says nothing about a PATH `python`
        # resolved via `_planner_python()`, so skip it there; a missing
        # pyserial in the child surfaces as the child's own reported failure.
        try:
            import serial  # noqa: F401, PLC0415 (validates pyserial is installed)
        except ImportError as err:
            raise _pyserial_missing() from err

    if port is None:
        raise _refuse_listing_ports("no --port given")
    if not _port_is_usable(port, {device for device, _ in _available_ports()}):
        raise _refuse_listing_ports(f"port '{port}' not found")

    print(f"monitor: {port} @ {baud} (Ctrl+] to quit)", file=sys.stderr)
    try:
        rc = subprocess.run(
            [python, "-m", "serial.tools.miniterm", port, str(baud)],
            stdout=_child_stdout(json_mode),
            env=spawn_env(),
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
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    project: str = typer.Option(None, "--project", hidden=True),
    board_yaml: str = typer.Option(None, "--board-yaml", hidden=True),
    sdk_root: str = typer.Option(None, "--sdk-root", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
) -> None:
    """Open a serial console to the board."""
    # The ten options above are clap's `GlobalArgs` members (`global = true`)
    # that the oracle accepts on EVERY verb, `monitor` included, and never
    # reads for this one -- confirmed live (`tan.exe monitor --non-interactive
    # --ci --target zephyr-conf --all --project . --board-yaml x --sdk-root x
    # --port COM7` reaches the identical "port not found" failure a bare
    # `tan.exe monitor --port COM7` does). Declared here purely so the argv
    # SURFACE matches: `tan monitor --sdk-root <path> --port COM7` exited 2 as
    # a Click "No such option" usage error without this, breaking any caller
    # (or saved script) forwarding the global set unconditionally -- unlike
    # `model`/`new-som`/`faultdecode`, `monitor` never resolves an SDK root at
    # all (see the module docstring), so `--project`/`--board-yaml`/
    # `--sdk-root` are genuinely unread here too, not merely deferred. Hidden
    # from `--help` because they do nothing. Same port-wide gap as
    # `clean_cmd.clean`/`new_som_cmd.new_som`.
    del project, board_yaml, sdk_root, target, all_targets
    del verbose, quiet, no_color, non_interactive, ci
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
        data, issues, exit_code = _run_monitor(port, baud, json_mode)
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
