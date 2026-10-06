# SPDX-License-Identifier: Apache-2.0
"""`tan monitor --break-uboot` command wiring (tan-cli#1315). The pure loop is
tested in tests/core/test_uboot_breakin.py; here pyserial is a scripted fake,
so no real port is opened and no board is needed (silicon validation on V2N is
still outstanding)."""
from __future__ import annotations

import json
import sys
import types

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import monitor_cmd
from tan.commands.monitor_cmd import monitor

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)

runner = CliRunner()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


# --- tan monitor --break-uboot (tan-cli#1315) -------------------------------


def _fake_serial_module(monkeypatch, *, caught: bool, opened=None):
    """Plant a `serial` whose `serial_for_url` returns a scripted port."""
    mod = types.ModuleType("serial")

    class SerialException(IOError):
        pass

    made = []

    class Port:
        def __init__(self):
            self.writes = []
            self.closed = False
            made.append(self)

        def write(self, data):
            self.writes.append(data)

        def read(self, size=1):
            return b"=> " if caught else b"noise"

        def close(self):
            self.closed = True

    def serial_for_url(url, baud, timeout=None):
        if opened is not None:
            opened.append((url, baud))
        if url == "rfc2217://refused:1":
            raise SerialException("refused")
        return Port()

    mod.serial_for_url = serial_for_url
    mod.made = made
    mod.SerialException = SerialException
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(
        monitor_cmd, "_url_handler_prefixes", lambda: frozenset({"rfc2217://", "socket://"})
    )


def _spawn_recorder(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)
    return calls


def test_break_uboot_caught_non_interactive_skips_miniterm(monkeypatch):
    opened = []
    _fake_serial_module(monkeypatch, caught=True, opened=opened)
    spawns = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(
        app,
        ["--port", "rfc2217://gw:4000", "--break-uboot", "--non-interactive", "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    bi = doc["data"]["breakIn"]
    assert bi["caught"] is True and bi["bytesSeenTail"].endswith("=> ")
    assert "elapsedSeconds" in bi
    assert opened == [("rfc2217://gw:4000", 115200)]
    assert spawns == []


def test_break_uboot_caught_hands_the_same_open_port_to_the_session(monkeypatch):
    opened = []
    _fake_serial_module(monkeypatch, caught=True, opened=opened)
    spawns = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    sessions = []

    def fake_attach(ser, json_mode):
        sessions.append((ser, ser.closed, json_mode))
        return 0

    monkeypatch.setattr(monitor_cmd, "_attach_miniterm", fake_attach)
    result = runner.invoke(
        app, ["--port", "socket://gw:4000", "--break-uboot", "--format", "json"]
    )
    assert result.exit_code == 0
    made = sys.modules["serial"].made
    # One open only; the session got that very object, still open (no reopen,
    # so no DTR/RTS toggle between the break-in and the console).
    assert len(opened) == 1 and len(made) == 1
    assert len(sessions) == 1
    assert sessions[0][0] is made[0] and sessions[0][1] is False
    assert spawns == []
    assert envelope(result)["data"]["breakIn"]["caught"] is True


def test_attach_miniterm_runs_miniterm_on_the_given_instance(monkeypatch):
    seen = {}

    class Term:
        def __init__(self, ser, **kw):
            seen["ser"], seen["kw"] = ser, kw
            seen["stdout_during_init"] = sys.stdout

        def start(self):
            seen["started"] = True

        def join(self, *a):
            pass

        def close(self):
            seen["closed"] = True

        def set_rx_encoding(self, e):
            pass

        set_tx_encoding = set_rx_encoding

    pkg = types.ModuleType("serial.tools")
    mt = types.ModuleType("serial.tools.miniterm")
    mt.Miniterm = Term
    monkeypatch.setitem(sys.modules, "serial", sys.modules.get("serial") or types.ModuleType("serial"))
    monkeypatch.setitem(sys.modules, "serial.tools", pkg)
    monkeypatch.setitem(sys.modules, "serial.tools.miniterm", mt)
    pkg.miniterm = mt
    sentinel = object()
    assert monitor_cmd._attach_miniterm(sentinel, True) == 0
    assert seen["ser"] is sentinel and seen["started"] and seen["closed"]
    assert seen["stdout_during_init"] is sys.__stderr__
    assert sys.stdout is not sys.__stderr__


def test_break_uboot_timeout_is_a_failure_and_never_spawns(monkeypatch):
    _fake_serial_module(monkeypatch, caught=False)
    spawns = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(
        app,
        ["--port", "socket://gw:4000", "--break-uboot", "--break-timeout", "0.3", "--format", "json"],
    )
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.break-timeout"
    assert doc["data"]["breakIn"]["caught"] is False
    assert doc["data"]["breakIn"]["bytesSeenTail"].endswith("noise")
    assert spawns == []


def test_break_uboot_open_failure_is_reported(monkeypatch):
    _fake_serial_module(monkeypatch, caught=True)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(
        app, ["--port", "rfc2217://refused:1", "--break-uboot", "--format", "json"]
    )
    assert result.exit_code == 1
    assert envelope(result)["issues"][0]["code"] == "monitor.break-open-failed"


@pytest.mark.parametrize(
    "extra", [["--break-timeout", "0"], ["--prompt", ""], ["--break-key", "\\x0"]]
)
def test_break_uboot_bad_options_are_validation_failures(monkeypatch, extra):
    _fake_serial_module(monkeypatch, caught=True)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(
        app, ["--port", "socket://gw:4000", "--break-uboot", "--format", "json", *extra]
    )
    assert result.exit_code == 2
    assert envelope(result)["issues"][0]["code"] == "monitor.break-bad-option"
