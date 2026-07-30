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
"""
from __future__ import annotations

import json
import sys

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import monitor_cmd
from tan.commands.monitor_cmd import monitor

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)

runner = CliRunner()


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
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert "no serial ports detected" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == []


def test_port_not_in_the_detected_list_refuses(monkeypatch):
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    result = runner.invoke(app, ["--port", "COM9", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'COM9' not found" in doc["issues"][0]["message"]


def test_a_present_port_spawns_miniterm_and_reports_success(monkeypatch):
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
    assert captured["argv"][0] == monitor_cmd._planner_python()
    assert captured["argv"][0] != sys.executable


def test_a_nonzero_miniterm_exit_maps_to_runtime_failure_not_the_raw_code(monkeypatch):
    """Mirrors the shipped Rust forwarder's `s.code().unwrap_or(1)` ->
    `ExitCode::RuntimeFailure` mapping -- NOT the oracle's literal
    `raise SystemExit(rc)`."""
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
    assert 'alp-tan[monitor]' in envelope["issues"][0]["message"]


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
