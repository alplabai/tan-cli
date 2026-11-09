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
launching miniterm -- the same reasoning `tan.core.sdk_discovery`'s `_planner_python`
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

import json
import os
import stat
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path

import typer

from tan.core.sdk_discovery import _planner_python
from tan.core import console_filter as console_filter_mod
from tan.core import serial_url
from tan.core.subprocess_env import spawn_env
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: The SDK-wide console default, matching `monitor.py::DEFAULT_BAUD`.
DEFAULT_BAUD = 115200

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


class ConsoleFilter(str, Enum):
    """Console output filters. `colors` (tan's own, default) keeps SGR colour
    sequences and neutralises every other escape/control byte; `default`,
    `nocontrol`, `printable` are miniterm's own stripping filters. `direct`
    passes the device's bytes to the terminal unmodified and is UNSAFE for an
    untrusted target (OSC 52 clipboard writes, title changes, screen games)."""

    COLORS = "colors"
    DIRECT = "direct"
    DEFAULT = "default"
    NOCONTROL = "nocontrol"
    PRINTABLE = "printable"


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
        '`pip install "./python[monitor]"` from a tan-cli checkout (tan-cli is '
        "not on PyPI), or `pip install pyserial`. A frozen `tan` binary bundles "
        "it at build time, so a binary built without that extra cannot gain it here.",
        ExitCode.RUNTIME_FAILURE,
        {"schemaVersion": DATA_SCHEMA_VERSION},
    )


#: Contract-harness seam ONLY (tan-cli#1165) -- never a documented,
#: `--help`-visible flag. `_available_ports()`'s real source, pyserial's own
#: `list_ports.comports()`, enumerates whatever serial hardware happens to be
#: physically attached to the host running it, so no golden envelope can pin a
#: non-empty, deterministic `data.availablePorts` (the field
#: `contract/envelopes/monitor-no-port` exists to freeze) without depending on
#: the recording machine's own hardware -- and on a CI runner with nothing
#: plugged in, `data.availablePorts` would record as `[]` forever, pinning
#: nothing the issue asked for. Set to a JSON-encoded `[[device, description],
#: ...]` array, this REPLACES the pyserial enumeration outright, in the exact
#: `[(device, description)]` shape every regular CALLER (`_refuse_listing_ports`,
#: `_port_is_usable`) already expects, so none of THOSE need to know the seam
#: exists. `_run_monitor` is the one exception: it also checks this variable
#: directly, to skip its own "pyserial is importable" precheck (see that
#: function's comment) -- without that second check this golden would depend
#: on whether pyserial happens to be installed in whatever environment replays
#: it, the exact host-dependence this seam exists to remove. See
#: `contract/envelopes/monitor-no-port/PROVENANCE.txt` for how a golden arms
#: it via `env.json`.
_TEST_PORTS_ENV = "TAN_MONITOR_TEST_PORTS_JSON"


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

    `_TEST_PORTS_ENV`, when set, short-circuits all of the above -- see its own
    comment. A malformed value (bad JSON, the wrong shape) is a harness/fixture
    bug, not a customer-facing one -- the same "do not let this become
    `monitor.internal-failure`" reasoning the ImportError guard above states
    for itself -- so it is swallowed and falls through to the real enumeration
    below rather than escaping as an unexpected exception.
    """
    fake = os.environ.get(_TEST_PORTS_ENV)
    if fake is not None:
        try:
            return [(str(device), str(description)) for device, description in json.loads(fake)]
        except (ValueError, TypeError):
            pass

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


def _spawn_console(python: str, port: str, baud: int, console_filter: str, json_mode: bool) -> int:
    """Run the plain console child (`python -c <bootstrap>`), returning its exit code."""
    try:
        # Empty cwd: `-c` puts the cwd on sys.path, so a `serial/` planted in the
        # project dir would be imported instead of pyserial (tan-cli#1317). It
        # also neutralises empty/relative PYTHONPATH entries (they resolve
        # against this empty directory).
        with tempfile.TemporaryDirectory(prefix="tan-monitor-") as empty:
            return subprocess.run(
                [
                    python,
                    "-c",
                    console_filter_mod.BOOTSTRAP,
                    "--filter",
                    console_filter,
                    # The spawn runs from an empty cwd, so a relative device path
                    # must be made absolute first.
                    os.path.abspath(port) if os.path.exists(port) else port,
                    str(baud),
                ],
                stdout=_child_stdout(json_mode),
                env=spawn_env(),
                cwd=empty,
            ).returncode
    except OSError as err:
        raise MonitorError(
            "monitor.launch-failed",
            f"failed to launch `{python} -c <miniterm bootstrap>`: {err}",
            ExitCode.RUNTIME_FAILURE,
            {"schemaVersion": DATA_SCHEMA_VERSION, "port": port, "baud": baud},
        ) from err


def _run_monitor(
    port: str | None,
    baud: int,
    json_mode: bool,
    break_opts: tuple[bytes, bytes, float] | None = None,
    non_interactive: bool = False,
    console_filter: str = "colors",
    capture_opts: tuple | None = None,
    action_spec: object | None = None,
) -> tuple[dict, list[Issue], ExitCode]:
    # Frozen (PyInstaller) or an embedded interpreter with no reportable
    # `sys.executable`: fall back to a PATH name, mirroring
    # `tan.core.sdk_discovery._planner_python` -- NOT `sys.executable`, which under a
    # PyInstaller freeze IS `tan` itself and would just re-enter this CLI.
    using_this_interpreter = not getattr(sys, "frozen", False) and bool(sys.executable)
    python = (
        sys.executable
        if using_this_interpreter
        else _planner_python(str(Path.cwd()), None)
    )

    # `_TEST_PORTS_ENV` set means `_available_ports()` never touches real
    # pyserial for this run (see its own comment), so this precheck -- whose
    # only job is proving pyserial resolves in THIS interpreter -- would just
    # make a contract golden depend on whether pyserial happens to be
    # installed in whatever environment ran it, the exact host-dependence the
    # seam exists to remove.
    if using_this_interpreter and os.environ.get(_TEST_PORTS_ENV) is None:
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
    problem = serial_url.port_url_problem(port)
    if problem is not None:
        raise MonitorError(
            "monitor.bad-port",
            f"bad --port: {problem}",
            ExitCode.VALIDATION_FAILURE,
            {"schemaVersion": DATA_SCHEMA_VERSION, "port": port},
        )
    if not _port_is_usable(port, {device for device, _ in _available_ports()}):
        raise _refuse_listing_ports(f"port '{port}' not found")

    if capture_opts is not None:
        from tan.commands import monitor_session  # noqa: PLC0415 (only on --capture)

        return monitor_session.run_capture(port, baud, capture_opts, break_opts, action_spec)

    if break_opts is not None:
        from tan.commands import monitor_session  # noqa: PLC0415 (only on --break-uboot)

        return monitor_session.run(
            port, baud, json_mode, break_opts, non_interactive, console_filter
        )

    print(f"monitor: {port} @ {baud} (Ctrl+] to quit)", file=sys.stderr)
    rc = _spawn_console(python, port, baud, console_filter, json_mode)

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
    break_uboot: bool = typer.Option(
        False,
        "--break-uboot",
        help="After opening the port, send the autoboot interrupt key repeatedly "
        "until the U-Boot prompt appears or --break-timeout passes, then continue "
        "in the interactive console on the same open port. With --non-interactive "
        "it stops after the break-in instead (exit 0 if caught); the console "
        "itself needs a terminal. --non-interactive is a shared flag (see "
        "its own help line). Power-cycle the board yourself; works over "
        "rfc2217:// and socket:// URLs. Use rfc2217:// against ser2net: socket:// "
        "to a telnet/RFC2217 port delivers IAC negotiation bytes as data and "
        "adds about 4 s.",
    ),
    break_key: str = typer.Option(
        None,
        "--break-key",
        help="With --break-uboot: key that interrupts autoboot (default: a space; "
        "escapes \\xNN \\r \\n \\t \\\\ allowed).",
    ),
    prompt: str = typer.Option(
        None, "--prompt", help="With --break-uboot: prompt that ends it (default: '=> ')."
    ),
    break_timeout: float = typer.Option(
        None,
        "--break-timeout",
        help="With --break-uboot: seconds to keep sending the key (default: 30).",
    ),
    capture: bool = typer.Option(
        False,
        "--capture",
        help="Headless capture instead of an interactive console: no TTY needed, "
        "works over pyserial URLs, stores raw bytes. Needs --duration and/or "
        "--until; combine with --break-uboot to break in first. Never prompts "
        "(--non-interactive is accepted and has no further effect).",
    ),
    duration: float = typer.Option(
        None, "--duration", help="With --capture: seconds to read (default 30 with --until)."
    ),
    until: str = typer.Option(
        None,
        "--until",
        help="With --capture: stop at the first line (or unterminated partial line, "
        "e.g. a prompt) matching this regex; the envelope carries it. A line "
        "longer than 4 KiB is searched on its last 4 KiB (complete or partial). A "
        "regex cannot be interrupted inside one search. No match in time is an error.",
    ),
    log: str = typer.Option(None, "--log", help="With --capture: write the raw bytes to this file."),
    send: list[str] = typer.Option(
        None, "--send", metavar="TEXT",
        help="With --capture: write TEXT to the console (repeatable, in order; escapes "
        "\\xNN \\r \\n \\t \\\\, no newline added). All items go out as one burst, at the "
        "start unless --send-after or --send-on says when.",
    ),
    send_after: float = typer.Option(
        None, "--send-after", metavar="SECONDS", help="With --send: send the burst this long after the start."
    ),
    send_on: str = typer.Option(
        None, "--send-on", metavar="REGEX",
        help="With --send: send the burst on the first line (or partial line, e.g. a prompt) "
        "matching this regex. Excludes --send-after.",
    ),
    send_gap: float = typer.Option(
        None, "--send-gap", metavar="SECONDS", help="With --send: pause between items (default 0.2)."
    ),
    reopen_at: int = typer.Option(
        None, "--reopen-at", metavar="BAUD",
        help="With --on: change the baud (in place, else close and reopen) on the first complete "
        "line matching --on, then keep capturing. Works over rfc2217:// and local serial.",
    ),
    on: str = typer.Option(None, "--on", metavar="REGEX", help="With --reopen-at: the trigger regex."),
    console_filter: ConsoleFilter = typer.Option(
        None,
        "--filter",
        help="Console output filter (default: colors): colors (colours render, every other escape "
        "is neutralised), default/nocontrol/printable (strip control codes), "
        "direct (raw bytes to the terminal: unsafe for untrusted targets). "
        "colors cannot stop same-colour (invisible) text or \\b/\\r overdrawing.",
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
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="With --break-uboot: stop after the break-in."
    ),
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
    del verbose, quiet, no_color, ci
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
        from tan.commands import monitor_session  # noqa: PLC0415 (validation only)

        opts = monitor_session.break_opts(break_uboot, break_key, prompt, break_timeout)
        cap = monitor_session.capture_opts(
            capture, duration, until, log, console_filter is not None
        )
        from tan.commands import monitor_actions  # noqa: PLC0415 (validation only)

        spec = monitor_actions.action_opts(
            capture, send, send_after, send_on, send_gap, reopen_at, on
        )
        data, issues, exit_code = _run_monitor(
            port, baud, json_mode, opts, non_interactive,
            console_filter.value if console_filter else "colors", cap, spec
        )
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
