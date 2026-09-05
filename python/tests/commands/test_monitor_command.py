# SPDX-License-Identifier: Apache-2.0
"""`tan monitor` -- the port-resolution refusals and the miniterm exit-code
mapping. Ported from `scripts/alp_cli/monitor.py`'s own shape (no committed
Rust `monitor.rs` exists; the retired forwarder in
`crates/tan-cli/src/commands/sdk_cli.rs` is the oracle for the outer envelope
contract).

**What is NOT exercised here, and why**: the actual `serial.tools.miniterm`
session against a real board. An `autouse` fixture below monkeypatches
`monitor_cmd.subprocess.run` to a stub that always raises for every test in
this module, so a spawn refused to reach in-process is structural (a
guard-ordering regression that lets a test fall through to the real spawn
fails loudly, rather than incidentally happening to hit an absent COM port)
rather than a run never opening a real port or blocking on interactive I/O --
there is no hardware attached to this test run, and faking a passing hardware
test would be worse than not testing it. Individual tests override the stub
where they need to observe or control the spawn's outcome. The one path this
file cannot prove is that a REAL board's bytes make it to the terminal; that
needs a bench with a device on it.

**pyserial may or may not be genuinely installed, and this file does not need
to know which (tan-cli#255).** `ci.yml` installs `-e ./python` with NO extras
on purpose -- that is the shape a customer's `pip install tan-cli` gives, and
the only one in which `tests/gates/test_declared_dependencies.py` can catch an
extras-only import escaping to a top-level-module import -- while `python-binaries.yml` and
`parity.yml` install `[monitor]`. The six cases below that exercise
`_run_monitor`'s real refusal/spawn logic used to SKIP outright in the
extras-less shape (`@needs_pyserial`), which silently dropped exactly the
coverage they were written for on the one install shape `ci.yml` actually
runs. `_stub_pyserial_if_absent()` replaces that: it plants an empty `serial`
module in `sys.modules` when the real one is not importable, so
`_run_monitor`'s precheck (a bare, function-local `import serial`) succeeds
either way. That is safe, not a fake pass, because every test that calls it
also replaces `_available_ports` with a canned list before `_run_monitor` ever
reaches pyserial's actual API -- a placeholder module with no attributes is
indistinguishable from the real one to the code under test. Only the tests
that assert the pyserial-ABSENT behaviour still force the real `ImportError`
themselves (`_block_pyserial`), since producing that failure honestly is the
whole point of them. Installing the extra in `ci.yml` instead was considered
and rejected: it would blind `test_declared_dependencies.py` to the shape a
bare `pip install tan-cli` actually produces.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import monitor_cmd
from tan.commands.monitor_cmd import monitor

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)

runner = CliRunner()


def _stub_pyserial_if_absent(monkeypatch) -> None:
    """Make a bare `import serial` succeed even when pyserial genuinely is not
    installed, so the test calling this exercises `_run_monitor`'s real logic
    in every environment `ci.yml` runs in -- not only the `[monitor]` extras
    shape. `_run_monitor`'s precheck imports `serial` in-process whenever the
    spawn would be THIS interpreter, and does nothing more with it (the actual
    port-listing call, `_available_ports`, is always monkeypatched away by the
    caller before this matters), so a bare placeholder module satisfies it."""
    if importlib.util.find_spec("serial") is None:
        monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    """Every test in this module gets a `subprocess.run` that refuses to run
    a real process, unless it overrides this with its own monkeypatch. A test
    relying only on a guard refusing before the spawn is proven, not assumed,
    to never reach a real port."""

    def _refuse(*args, **kwargs):
        raise AssertionError(
            f"a test reached the real subprocess.run spawn: args={args!r}"
        )

    monkeypatch.setattr(monitor_cmd.subprocess, "run", _refuse)


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def test_no_port_given_lists_available_ports_and_refuses(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(
        monitor_cmd, "_available_ports", lambda: [("COM7", "USB Serial"), ("COM8", "")]
    )
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["ok"] is False
    assert doc["exitCode"] == 1
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "no --port given" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == [
        {"device": "COM7", "description": "USB Serial"},
        {"device": "COM8", "description": ""},
    ]


def test_no_port_given_and_none_detected_says_so(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert "no serial ports detected" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == []


def test_port_not_in_the_detected_list_refuses(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    result = runner.invoke(app, ["--port", "COM9", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'COM9' not found" in doc["issues"][0]["message"]


def test_a_present_port_spawns_miniterm_and_reports_success(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(
        app, ["--port", "COM7", "--baud", "9600", "--format", "json"]
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"] == {"schemaVersion": "1", "port": "COM7", "baud": 9600}
    assert captured["argv"][-2:] == ["COM7", "9600"]
    assert "serial.tools.miniterm" in captured["argv"]
    # The interpreter this test runs under is not frozen, so the spawn must
    # use `sys.executable` -- never a bare re-derivation, and never empty.
    assert captured["argv"][0] == sys.executable


def test_frozen_interpreter_spawns_a_path_python_not_sys_executable(monkeypatch):
    """Under PyInstaller `sys.executable` IS `tan` itself, so the spawn must
    use a PATH name (`_planner_python()`), never `sys.executable` -- else
    `tan monitor` under a frozen build re-enters this CLI instead of
    launching miniterm."""
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    monkeypatch.setattr(monitor_cmd.sys, "frozen", True, raising=False)

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(
        app, ["--port", "COM7", "--baud", "9600", "--format", "json"]
    )
    assert result.exit_code == 0, result.stdout
    assert captured["argv"][0] == monitor_cmd._planner_python(str(Path.cwd()), None)
    assert captured["argv"][0] != sys.executable


def test_a_nonzero_miniterm_exit_maps_to_runtime_failure_not_the_raw_code(monkeypatch):
    """Mirrors the shipped Rust forwarder's `s.code().unwrap_or(1)` ->
    `ExitCode::RuntimeFailure` mapping -- NOT the oracle's literal
    `raise SystemExit(rc)`."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 42

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["exitCode"] == 1
    assert doc["issues"][0]["code"] == "monitor.failed"
    assert "code 42" in doc["issues"][0]["message"]
    assert captured["argv"][0] == sys.executable


def test_default_baud_is_the_sdk_wide_console_default(monkeypatch):
    """`--baud` omitted must fall back to `DEFAULT_BAUD` (115200), matching
    the oracle's `monitor.py::DEFAULT_BAUD` -- a silent drift here garbles
    every console session on the bench."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["data"]["baud"] == 115200
    assert captured["argv"][-1] == "115200"


def test_pyserial_missing_is_a_coded_refusal(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "serial":
            raise ImportError("no module named serial")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.pyserial-missing"


def test_text_mode_writes_nothing_to_stdout(monkeypatch):
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(app, [])
    assert result.exit_code == 1
    assert result.stdout == ""


def test_a_bad_format_value_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["--format", "xml"])
    assert result.exit_code == 2
    assert "Traceback" not in (result.output or "")


@pytest.mark.parametrize(
    "flag",
    [
        ["--project", "."],
        ["--board-yaml", "board.yaml"],
        ["--sdk-root", "."],
        ["--target", "zephyr-conf"],
        ["--all"],
        ["--verbose"],
        ["--quiet"],
        ["--no-color"],
        ["--non-interactive"],
        ["--ci"],
    ],
)
def test_the_globals_the_oracle_ignores_are_accepted_not_rejected(flag, monkeypatch):
    """tan-cli#255: the oracle's clap `GlobalArgs` are declared on EVERY verb,
    `monitor` included, and never read by it (confirmed live against
    `tan.exe monitor`: each flag alone reaches the identical port-resolution
    failure a bare `tan.exe monitor --port COM7` does). Without them declared
    here, `tan monitor --sdk-root <path> --port COM7` was a Click "No such
    option" usage error at exit 2 where the oracle exits 0/1 -- so a caller
    forwarding the global set unconditionally (the extension, a saved script)
    could never open a console."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    class _Completed:
        returncode = 0

    monkeypatch.setattr(
        monitor_cmd.subprocess, "run", lambda argv, **kwargs: _Completed()
    )

    result = runner.invoke(app, ["--port", "COM7", *flag, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert envelope(result)["command"] == "monitor"


def test_an_unknown_flag_is_still_a_usage_error():
    """The accepted-globals list above must not turn into "accept anything"."""
    assert runner.invoke(app, ["--not-a-real-flag"]).exit_code == 2


def _block_pyserial(monkeypatch):
    """Make `from serial.tools import list_ports` raise ImportError.

    Simulates a build without the `monitor` extra. Blocking at `sys.modules`
    with `None` is what makes the guarded import raise the same ImportError an
    absent distribution would, without uninstalling anything.
    """
    for name in ("serial", "serial.tools", "serial.tools.list_ports"):
        monkeypatch.setitem(sys.modules, name, None)


def test_frozen_build_without_the_extra_reports_pyserial_missing_not_a_tan_bug(monkeypatch):
    """The FROZEN path is the one the precheck deliberately skips.

    `_run_monitor` only validates pyserial when it is about to spawn THIS
    interpreter; under PyInstaller `sys.frozen` is set, that precheck is
    skipped, and `_available_ports()` becomes the first thing to touch
    pyserial -- in-process, before any child exists. Unguarded, that surfaced
    as `monitor.internal-failure` at exit 5, which tells a customer tan is
    broken when the honest answer is that an optional dependency was never
    installed. Regression pin for a real frozen no-extras build.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _block_pyserial(monkeypatch)

    result = runner.invoke(app, ["--format", "json"])

    envelope = json.loads(result.stdout)
    codes = [issue["code"] for issue in envelope["issues"]]
    assert codes == ["monitor.pyserial-missing"], envelope
    assert "monitor.internal-failure" not in codes
    assert result.exit_code == 1, f"expected RUNTIME_FAILURE, got {result.exit_code}"
    assert 'tan-cli[monitor]' in envelope["issues"][0]["message"]


def test_unfrozen_build_without_pyserial_reports_the_same_code(monkeypatch):
    """The source-install path must not diverge from the frozen one: same code,
    same message, so a consumer matching on `monitor.pyserial-missing` sees one
    spelling regardless of how tan was delivered."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _block_pyserial(monkeypatch)

    result = runner.invoke(app, ["--format", "json"])

    envelope = json.loads(result.stdout)
    assert [i["code"] for i in envelope["issues"]] == ["monitor.pyserial-missing"], envelope
    assert result.exit_code == 1


def test_test_ports_env_replaces_pyserial_enumeration_even_when_pyserial_is_blocked(
    monkeypatch,
):
    """tan-cli#1165: `contract/envelopes/monitor-no-port` needs a deterministic,
    non-empty `data.availablePorts` on any box, regardless of whether pyserial
    happens to be installed there -- so `_TEST_PORTS_ENV` must bypass BOTH the
    real `list_ports.comports()` call and `_run_monitor`'s own "pyserial is
    importable" precheck. Blocking pyserial outright and still getting
    `monitor.no-port` (not `monitor.pyserial-missing`) is the proof: if either
    bypass regressed, this would report the pyserial-missing refusal instead.
    """
    _block_pyserial(monkeypatch)
    monkeypatch.setenv(
        monitor_cmd._TEST_PORTS_ENV,
        json.dumps([["COM7", "USB Serial"], ["COM8", "n/a"]]),
    )

    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert doc["data"]["availablePorts"] == [
        {"device": "COM7", "description": "USB Serial"},
        {"device": "COM8", "description": "n/a"},
    ]


@pytest.mark.parametrize(
    "fake_value",
    [
        "not json",
        # A JSON *object* iterates its keys ("COM7"), and unpacking a 8-char
        # string into (device, description) raises ValueError -- covered by
        # the same `except (ValueError, TypeError)`, not a separate branch.
        json.dumps({"long-key": "USB Serial"}),
        # A 3-element inner list is "too many values to unpack" -- ValueError.
        json.dumps([["COM7", "USB Serial", "extra"]]),
    ],
)
def test_a_malformed_test_ports_env_falls_through_instead_of_becoming_internal_failure(
    monkeypatch, fake_value
):
    """A fixture typo in `TAN_MONITOR_TEST_PORTS_JSON` -- bad JSON, or valid
    JSON in the wrong shape -- is a harness bug, not a customer-facing one, so
    it must not surface as `monitor.internal-failure` the way an unguarded
    `json.loads`/unpacking exception would (`_available_ports` docstring): it
    falls through to the real enumeration instead, which either answers a list
    or raises the pre-existing `monitor.pyserial-missing` refusal -- never a
    bare `ValueError`/`TypeError` escaping this function.
    """
    monkeypatch.setenv(monitor_cmd._TEST_PORTS_ENV, fake_value)
    try:
        result = monitor_cmd._available_ports()
    except monitor_cmd.MonitorError as err:
        assert err.code == "monitor.pyserial-missing"
    else:
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# where miniterm's output goes (tan-cli#491 defect 6)
# ---------------------------------------------------------------------------


def _spawn_kwargs(monkeypatch, argv: list[str]) -> dict:
    """Run `tan monitor <argv>` with a stub spawn and hand back the `kwargs`
    the command passed to `subprocess.run`."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    captured: dict = {}

    class _Completed:
        returncode = 0

    def fake_run(_argv, **kwargs):
        captured.update(kwargs)
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout
    return captured


def test_json_mode_keeps_the_boards_bytes_off_stdout(monkeypatch):
    """tan-cli#491 defect 6. The spawn passed no `stdout=`, so miniterm
    INHERITED tan's stdout and wrote every received serial byte there --
    reproduced end to end against a real pty: the board's banner preceded the
    envelope and a whole-stdout `JSON.parse` failed on an `ok: true`, exit-0
    run. Under `--format json` the child's stdout must therefore be something
    other than tan's own.

    Asserted as "not inherit, and not stdout" rather than against a specific
    stream object: pytest's own capture replaces the process streams, so
    pinning identity here would test the harness, not the command. What the
    stream IS is `_child_stdout`'s own case below."""
    kwargs = _spawn_kwargs(monkeypatch, ["--port", "COM7", "--format", "json"])
    assert "stdout" in kwargs, kwargs
    assert kwargs["stdout"] is not None, "inherited stdout -- the #491 d6 defect"
    assert kwargs["stdout"] is not sys.stdout


def test_text_mode_still_inherits_stdout(monkeypatch):
    """The other half, and the reason this is not an unconditional redirect:
    in text mode there is no envelope to protect, board traffic on stdout is
    the whole point of an interactive console, and redirecting it would break
    `tan monitor > board.log`."""
    kwargs = _spawn_kwargs(monkeypatch, ["--port", "COM7"])
    assert kwargs.get("stdout") is None, kwargs


def test_child_stdout_is_the_real_stderr_stream_when_it_is_healthy(monkeypatch):
    """The DEVNULL fallback above is only reachable when `sys.__stderr__` is
    absent or closed. On the ordinary path -- a real, open, fd-backed stream
    -- `_child_stdout(True)` must hand back THAT stream, not DEVNULL: a
    mutant that collapses the healthy branch straight to `return
    subprocess.DEVNULL` (i.e. `_child_stdout = lambda json_mode: None if not
    json_mode else subprocess.DEVNULL`) silently drops every board byte
    under `--format json` while still returning *something* falsy-adjacent
    for `stdout=`, and neither test above catches it: the None-stderr and
    closed-stderr cases both already expect DEVNULL, and the d6 stdout test
    only asserts `is not sys.stdout`, which DEVNULL also satisfies. Built and
    confirmed RED against exactly that mutant before this test was kept (see
    the PR receipt)."""
    fd = os.open(os.devnull, os.O_WRONLY)
    stream = os.fdopen(fd, "w")
    try:
        monkeypatch.setattr(monitor_cmd.sys, "__stderr__", stream, raising=False)
        assert monitor_cmd._child_stdout(True) is sys.__stderr__
        assert monitor_cmd._child_stdout(True) is stream
        assert monitor_cmd._child_stdout(True) is not monitor_cmd.subprocess.DEVNULL
    finally:
        stream.close()


def test_child_stdout_falls_back_to_devnull_when_there_is_no_real_stderr(monkeypatch):
    """`_child_stdout` hands the child `sys.__stderr__` -- NOT `sys.stderr`,
    which `cli.main` binds to a `_TeeStderr` under `--format json`, an object
    with no `fileno()` for `subprocess` to hand over. `sys.__stderr__` can
    still be `None` (pythonw, an embedded interpreter) or closed, and the
    answer then is DEVNULL: dropping the board's bytes is bad, putting them on
    stdout is the defect."""
    monkeypatch.setattr(monitor_cmd.sys, "__stderr__", None, raising=False)
    assert monitor_cmd._child_stdout(True) is monitor_cmd.subprocess.DEVNULL

    class _Closed:
        def fileno(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(monitor_cmd.sys, "__stderr__", _Closed(), raising=False)
    assert monitor_cmd._child_stdout(True) is monitor_cmd.subprocess.DEVNULL
    # Text mode never consults the stream at all.
    assert monitor_cmd._child_stdout(False) is None


# ---------------------------------------------------------------------------
# tan-cli#701: `\\.\COM<n>` is the spelling Microsoft documents for COM10 and
# above, and pyserial's win32 backend opens it -- but `comports()` only ever
# REPORTS the bare `COM<n>` from the registry's `PortName`. A membership test
# against `comports()` therefore refused a port pyserial would have opened,
# while listing that same port in its own "not found" message.
# ---------------------------------------------------------------------------

#: The literal nine characters a user types: \ \ . \ C O M 3 8
_UNC_COM38 = "\\\\.\\COM38"


def test_the_unc_spelling_of_a_present_port_is_accepted(monkeypatch):
    """Measured on a real Windows host: `serial.Serial(r"\\\\.\\COM38")` opens
    (`is_open = True`), while `comports()` reports the device as `'COM38'` and
    `tan monitor --port "\\\\.\\COM38"` refused with
    `monitor.no-port : port '\\\\.\\COM38' not found`.
    """
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(
        monitor_cmd, "_available_ports", lambda: [("COM38", "USB Serial Port")]
    )

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(app, ["--port", _UNC_COM38, "--format", "json"])
    assert result.exit_code == 0, result.output
    doc = envelope(result)
    assert doc["ok"] is True
    # The user's own spelling is preserved end to end -- pyserial opens it, so
    # there is nothing to gain by rewriting what they asked for.
    assert doc["data"]["port"] == _UNC_COM38
    assert captured["argv"][-2:] == [_UNC_COM38, "115200"]


def test_a_unc_port_whose_bare_form_is_absent_is_still_refused(monkeypatch):
    """The gate is normalised, not widened.

    Without this case the fix would be indistinguishable from "accept anything
    starting with the device prefix", which would refuse nothing at all on
    Windows.
    """
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    result = runner.invoke(app, ["--port", _UNC_COM38, "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "not found" in doc["issues"][0]["message"]


def test_a_posix_device_path_is_not_rewritten(monkeypatch):
    """`_port_aliases` must leave every non-Windows spelling alone.

    tan-cli#569 covers `/dev/serial/by-id` and pyserial URLs separately; this
    only asserts that #701's normalisation does not reach them.
    """
    assert monitor_cmd._port_aliases("/dev/ttyUSB0") == {"/dev/ttyUSB0"}
    assert monitor_cmd._port_aliases("COM38") == {"COM38"}
    assert monitor_cmd._port_aliases(_UNC_COM38) == {_UNC_COM38, "COM38"}


def _stub_child(monkeypatch, returncode: int = 0) -> dict:
    """Capture the argv `monitor` hands miniterm, without spawning anything.

    Same three-line shape the tan-cli#701 tests above build inline; hoisted so
    the tan-cli#569 cases below do not add a fourth copy.
    """
    captured: dict = {}

    class _Completed:
        pass

    _Completed.returncode = returncode

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)
    return captured


# --------------------------------------------------------------------------
# tan-cli#569 -- the gate is openability, not enumeration.
#
# `comports()` does not enumerate `/dev/serial/by-id/...` symlinks or any of
# pyserial's URL schemes, and `serial.Serial()` opens all of them. Measured on
# a Linux host with a real Artery AT32 adapter before the fix:
#
#   $ python -c "import serial; serial.Serial('/dev/serial/by-id/usb-Artery_AT32_Virtual_Com_Port_10A2617F4486-if00')"
#   opened OK
#   $ tan monitor --port /dev/serial/by-id/usb-Artery_AT32_Virtual_Com_Port_10A2617F4486-if00
#   monitor: port '...' not found -- available serial ports: ... /dev/ttyACM0 ...
#
# The refused port's own raw node was in the list tan printed back.
# --------------------------------------------------------------------------


def test_a_character_device_not_in_comports_is_accepted(monkeypatch, tmp_path):
    """THE #569 case: a by-id symlink pyserial opens and `comports()` omits.

    `os.stat` follows the symlink, so the real check is "is the target a
    character device" -- which is what pyserial's own open does.
    """
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("/dev/ttyACM0", "AT32")])
    by_id = "/dev/serial/by-id/usb-Artery_AT32_Virtual_Com_Port_10A2617F4486-if00"
    monkeypatch.setattr(monitor_cmd, "_is_openable_device", lambda port: port == by_id)
    captured = _stub_child(monkeypatch, returncode=0)

    result = runner.invoke(app, ["--port", by_id, "--format", "json"])
    assert result.exit_code == 0, result.output
    # The operator's own spelling reaches miniterm -- rewriting it to the raw
    # node would hand back the unstable name #569 exists to avoid.
    assert captured["argv"][-2:] == [by_id, "115200"]


def test_a_pyserial_url_scheme_is_accepted(monkeypatch):
    """`socket://` and `rfc2217://` have NO local device path, so the
    character-device arm cannot cover them and there is no workaround."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    monkeypatch.setattr(monitor_cmd, "_is_openable_device", lambda _port: False)
    monkeypatch.setattr(
        monitor_cmd, "_url_handler_prefixes", lambda: frozenset({"socket://", "rfc2217://"})
    )
    captured = _stub_child(monkeypatch, returncode=0)

    result = runner.invoke(app, ["--port", "socket://localhost:7000", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert captured["argv"][-2:] == ["socket://localhost:7000", "115200"]


def test_a_port_matching_none_of_the_three_arms_is_still_refused(monkeypatch):
    """Non-vacuity for the whole gate. Without this, `_port_is_usable` could
    `return True` and every case above would still pass."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("/dev/ttyACM0", "AT32")])
    monkeypatch.setattr(monitor_cmd, "_is_openable_device", lambda _port: False)
    monkeypatch.setattr(monitor_cmd, "_url_handler_prefixes", lambda: frozenset({"socket://"}))

    result = runner.invoke(app, ["--port", "/dev/ttyNOPE99", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "not found" in doc["issues"][0]["message"]


def test_url_handler_prefixes_are_read_from_pyserial_not_hardcoded():
    """A hardcoded list drifts when pyserial adds or drops a handler. Measured
    on pyserial 3.5: alt, cp2110, hwgrep, loop, rfc2217, socket, spy."""
    prefixes = monitor_cmd._url_handler_prefixes()
    if not prefixes:
        pytest.skip("pyserial not installed in this environment")
    assert "socket://" in prefixes
    assert "rfc2217://" in prefixes
    # Every entry is a scheme, not a bare module name -- a set of
    # `protocol_socket` strings would never match a real `--port`.
    assert all(p.endswith("://") for p in prefixes), sorted(prefixes)
    assert not any(p.startswith("protocol_") for p in prefixes), sorted(prefixes)


def test_is_openable_device_refuses_a_regular_file_and_an_absent_path(tmp_path):
    """The character-device arm must not accept any old existing path. A
    regular file exists and stats fine, and is not a serial port."""
    regular = tmp_path / "not-a-tty"
    regular.write_text("")
    assert monitor_cmd._is_openable_device(str(regular)) is False
    assert monitor_cmd._is_openable_device(str(tmp_path / "absent")) is False
    # An embedded NUL raises ValueError from os.stat on CPython; a pre-flight
    # gate must answer False, never traceback.
    assert monitor_cmd._is_openable_device("/dev/tty\x00evil") is False
