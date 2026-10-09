# SPDX-License-Identifier: Apache-2.0
"""`tan monitor --capture --send / --reopen-at --on` wiring (tan-cli#1451).
pyserial is a scripted fake that records every open (url, baud)."""
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


def _fake_serial(monkeypatch, scripts, *, open_fails_after=None):
    """`scripts`: one chunk list per successive open."""
    mod = types.ModuleType("serial")
    opened = []

    class Port:
        def __init__(self, chunks, baud):
            self.chunks, self.baud = list(chunks), baud
            self.writes, self.closed = [], False

        def write(self, d):
            self.writes.append(d)

        def read(self, size=1):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            self.closed = True

    def serial_for_url(url, baud, timeout=None, write_timeout=None):
        if open_fails_after is not None and len(opened) >= open_fails_after:
            raise IOError("refused")
        port = Port(scripts[len(opened)] if len(opened) < len(scripts) else [], baud)
        opened.append((url, baud, port))
        return port

    mod.serial_for_url, mod.SerialException = serial_for_url, IOError
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    monkeypatch.setattr(
        monitor_cmd, "_url_handler_prefixes", lambda: frozenset({"rfc2217://", "socket://"})
    )
    return opened


def run(args):
    return runner.invoke(
        app, ["--port", "rfc2217://gw:4000", "--format", "json", "--capture", *args]
    )


def data(result):
    return json.loads(result.stdout)


def test_send_goes_to_the_port_and_is_reported(monkeypatch):
    opened = _fake_serial(monkeypatch, [[b"ready\n"]])
    r = run(["--duration", "0.3", "--send", "v", "--send", "m\\n", "--send-gap", "0"])
    assert r.exit_code == 0, r.stdout
    assert opened[0][2].writes == [b"v", b"m\n"]
    actions = data(r)["data"]["capture"]["actions"]
    assert actions["events"][0]["action"] == "send" and actions["sendsPending"] == 0


def test_send_on_pattern_sends_after_the_prompt(monkeypatch):
    opened = _fake_serial(monkeypatch, [[b"boot\n", b"Press a key\n"]])
    r = run(["--duration", "0.3", "--send", "s", "--send-on", "Press a key"])
    assert r.exit_code == 0, r.stdout
    assert opened[0][2].writes == [b"s"]


def test_reopen_at_switches_baud_on_match_and_keeps_capturing(monkeypatch):
    opened = _fake_serial(monkeypatch, [[b"entering STOP\n"], [b"alive at 23040\n"]])
    r = run(["--baud", "115200", "--until", "alive at", "--reopen-at", "23040", "--on", "STOP"])
    assert r.exit_code == 0, r.stdout
    assert [(u, b) for u, b, _ in opened] == [
        ("rfc2217://gw:4000", 115200),
        ("rfc2217://gw:4000", 23040),
    ]
    assert opened[0][2].closed and opened[1][2].closed
    cap = data(r)["data"]["capture"]
    assert cap["matchedLine"] == "alive at 23040"
    assert cap["actions"]["events"][0]["baud"] == 23040
    assert cap["actions"]["reopenPending"] is False


def test_a_failed_reopen_is_coded(monkeypatch):
    _fake_serial(monkeypatch, [[b"STOP\n"]], open_fails_after=1)
    r = run(["--duration", "0.3", "--reopen-at", "23040", "--on", "STOP"])
    assert r.exit_code == 1
    assert data(r)["issues"][0]["code"] == "monitor.capture-reopen-failed"


@pytest.mark.parametrize(
    "args",
    [
        ["--duration", "1", "--send-after", "1"],
        ["--duration", "1", "--send", "a", "--send-after", "1", "--send-on", "x"],
        ["--duration", "1", "--reopen-at", "9600"],
        ["--duration", "1", "--on", "x"],
        ["--duration", "1", "--reopen-at", "0", "--on", "x"],
        ["--duration", "1", "--send", "a\\q"],
        ["--duration", "1", "--send", "a", "--send-on", "("],
    ],
)
def test_bad_combinations_are_validation_failures(monkeypatch, args):
    _fake_serial(monkeypatch, [[]])
    r = run(args)
    assert r.exit_code == 2
    assert data(r)["issues"][0]["code"] == "monitor.capture-bad-option"


def test_actions_require_capture(monkeypatch):
    _fake_serial(monkeypatch, [[]])
    r = runner.invoke(
        app, ["--port", "rfc2217://gw:4000", "--format", "json", "--send", "v"]
    )
    assert r.exit_code == 2
    assert data(r)["issues"][0]["code"] == "monitor.capture-bad-option"
