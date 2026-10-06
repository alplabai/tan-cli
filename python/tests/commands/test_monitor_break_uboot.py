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

from tan.commands import monitor_cmd, monitor_session
from tan.commands.monitor_cmd import monitor

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)

runner = CliRunner()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


class FakePort:
    def __init__(self, reply, raises=None):
        self.reply, self.raises = reply, raises
        self.writes, self.closed = [], False

    def write(self, data):
        if self.raises is OSError:
            raise OSError("write timeout")
        self.writes.append(data)

    def read(self, size=1):
        if self.raises is KeyboardInterrupt:
            raise KeyboardInterrupt
        return self.reply

    def close(self):
        self.closed = True


def _serial(monkeypatch, reply=b"=> ", raises=None, kwargs_seen=None):
    mod = types.ModuleType("serial")
    made = []

    class SerialException(IOError):
        pass

    def serial_for_url(url, baud, timeout=None, write_timeout=None):
        if kwargs_seen is not None:
            kwargs_seen.append((url, baud, timeout, write_timeout))
        if "refused" in url:
            raise SerialException("refused")
        made.append(FakePort(reply, raises))
        return made[-1]

    mod.serial_for_url, mod.SerialException, mod.made = serial_for_url, SerialException, made
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    monkeypatch.setattr(
        monitor_cmd, "_url_handler_prefixes", lambda: frozenset({"rfc2217://", "socket://"})
    )
    monkeypatch.setattr(
        monitor_cmd.subprocess, "run", lambda *a, **k: pytest.fail("spawned miniterm")
    )
    return made


def run(args, url="socket://gw:4000"):
    return runner.invoke(app, ["--port", url, "--break-uboot", "--format", "json", *args])


def test_caught_non_interactive_exits_0_closes_port_and_reports(monkeypatch):
    seen = []
    made = _serial(monkeypatch, kwargs_seen=seen)
    r = run(["--non-interactive"], url="rfc2217://gw:4000")
    assert r.exit_code == 0, r.stdout
    bi = envelope(r)["data"]["breakIn"]
    assert bi["caught"] is True and bi["bytesSeenTail"].endswith("=> ")
    assert {"elapsedSeconds", "timeoutSeconds", "bytesSeen"} <= bi.keys()
    assert seen == [("rfc2217://gw:4000", 115200, 0.1, monitor_session.WRITE_TIMEOUT_S)]
    assert made[0].closed and made[0].writes == [b" "]


def test_key_prompt_and_timeout_reach_the_loop(monkeypatch):
    made = _serial(monkeypatch, reply=b"V2N# ")
    r = run(["--non-interactive", "--break-key", "\\x03", "--prompt", "V2N# ", "--break-timeout", "7"])
    assert r.exit_code == 0, r.stdout
    assert made[0].writes[0] == b"\x03"
    assert envelope(r)["data"]["breakIn"]["timeoutSeconds"] == 7.0


def test_interactive_hands_the_same_open_port_to_the_session(monkeypatch):
    made = _serial(monkeypatch)
    monkeypatch.setattr(monitor_session, "_stdin_is_tty", lambda: True)
    sessions = []
    monkeypatch.setattr(
        monitor_session,
        "attach_miniterm",
        lambda ser, json_mode, f="colors": sessions.append((ser, ser.closed, json_mode)) or 0,
    )
    r = run([])
    assert r.exit_code == 0
    assert len(made) == 1 and len(sessions) == 1  # one open, no reopen
    assert sessions[0][0] is made[0] and sessions[0][1] is False and sessions[0][2] is True


def test_timeout_closes_port_and_is_a_failure(monkeypatch):
    made = _serial(monkeypatch, reply=b"noise")
    r = run(["--non-interactive", "--break-timeout", "0.3"])
    assert r.exit_code == 1
    doc = envelope(r)
    assert doc["issues"][0]["code"] == "monitor.break-timeout"
    assert doc["data"]["breakIn"]["caught"] is False
    assert doc["data"]["breakIn"]["bytesSeenTail"].endswith("noise")
    assert made[0].closed


def test_port_is_closed_on_baseexception(monkeypatch):
    made = _serial(monkeypatch, raises=KeyboardInterrupt)
    run(["--non-interactive"])
    assert made[0].closed


def test_io_error_during_loop_is_reported_and_closes(monkeypatch):
    made = _serial(monkeypatch, raises=OSError)
    r = run(["--non-interactive"])
    assert r.exit_code == 1
    assert envelope(r)["issues"][0]["code"] == "monitor.break-io-failed"
    assert made[0].closed


def test_open_failure_is_reported(monkeypatch):
    _serial(monkeypatch)
    r = run(["--non-interactive"], url="rfc2217://refused:1")
    assert r.exit_code == 1
    assert envelope(r)["issues"][0]["code"] == "monitor.break-open-failed"


def test_interactive_without_a_tty_is_refused_before_opening(monkeypatch):
    made = _serial(monkeypatch)
    monkeypatch.setattr(monitor_session, "_stdin_is_tty", lambda: False)
    r = run([])
    assert r.exit_code == 1
    assert envelope(r)["issues"][0]["code"] == "monitor.no-tty"
    assert made == []


@pytest.mark.parametrize(
    "extra",
    [["--break-timeout", "0"], ["--prompt", ""], ["--break-key", "\\x0"], ["--break-key", "\\q"]],
)
def test_bad_options_are_validation_failures(monkeypatch, extra):
    _serial(monkeypatch)
    r = run(extra)
    assert r.exit_code == 2
    assert envelope(r)["issues"][0]["code"] == "monitor.break-bad-option"


@pytest.mark.parametrize("flag", [["--break-key", "x"], ["--prompt", "x"], ["--break-timeout", "5"]])
def test_companion_flags_without_break_uboot_are_refused(monkeypatch, flag):
    _serial(monkeypatch)
    r = runner.invoke(app, ["--port", "socket://gw:1", "--format", "json", *flag])
    assert r.exit_code == 2
    assert envelope(r)["issues"][0]["code"] == "monitor.break-bad-option"


# --- attach_miniterm: the in-process console --------------------------------


def _miniterm(monkeypatch, term_cls):
    pkg = types.ModuleType("serial.tools")
    mt = types.ModuleType("serial.tools.miniterm")
    mt.Miniterm = term_cls
    mt.TRANSFORMATIONS = {}
    pkg.miniterm = mt
    monkeypatch.setitem(sys.modules, "serial.tools", pkg)
    monkeypatch.setitem(sys.modules, "serial.tools.miniterm", mt)
    return mt


class _Term:
    seen: dict = {}

    def __init__(self, ser, **kw):
        type(self).seen = {"ser": ser, "kw": kw, "stdout": sys.stdout, "closed": False}
        type(self).seen["self"] = self

    def start(self):
        pass

    def join(self, *a):
        pass

    def close(self):
        type(self).seen["closed"] = True

    def set_rx_encoding(self, e):
        pass

    set_tx_encoding = set_rx_encoding


def test_attach_uses_the_given_instance_default_filter_and_json_stderr(monkeypatch):
    mt = _miniterm(monkeypatch, _Term)
    ser = FakePort(b"")
    assert monitor_session.attach_miniterm(ser, True) == 0
    from tan.core import console_filter

    assert mt.TRANSFORMATIONS["colors"] is console_filter.ColorsFilter
    seen = _Term.seen
    assert seen["ser"] is ser and seen["kw"]["filters"] == ["colors"]  # tan's safe colour filter
    assert seen["stdout"] is sys.__stderr__ and sys.stdout is not sys.__stderr__
    assert seen["closed"] and ser.closed


def test_console_construction_failure_closes_port_and_restores_stdout(monkeypatch):
    termios = pytest.importorskip("termios")

    class Boom:
        def __init__(self, *a, **k):
            raise termios.error(25, "Inappropriate ioctl for device")

    _miniterm(monkeypatch, Boom)
    ser, before = FakePort(b""), sys.stdout
    with pytest.raises(monitor_cmd.MonitorError) as ei:
        monitor_session.attach_miniterm(ser, True)
    assert ei.value.code == "monitor.launch-failed"
    assert ser.closed and sys.stdout is before


@pytest.mark.parametrize("exc", [ValueError("x"), OSError("y")])
def test_other_console_failures_are_mapped_too(monkeypatch, exc):
    class Boom:
        def __init__(self, *a, **k):
            raise exc

    _miniterm(monkeypatch, Boom)
    ser = FakePort(b"")
    with pytest.raises(monitor_cmd.MonitorError):
        monitor_session.attach_miniterm(ser, False)
    assert ser.closed


def test_filter_option_reaches_the_in_process_console(monkeypatch):
    _serial(monkeypatch)
    monkeypatch.setattr(monitor_session, "_stdin_is_tty", lambda: True)
    got = []
    monkeypatch.setattr(
        monitor_session, "attach_miniterm", lambda ser, jm, f="colors": got.append(f) or 0
    )
    assert run(["--filter", "nocontrol"]).exit_code == 0
    assert run([]).exit_code == 0
    assert got == ["nocontrol", "colors"]


@pytest.mark.parametrize(("args", "want"), [([], "colors"), (["--filter", "printable"], "printable"), (["--filter", "direct"], "direct")])
def test_subprocess_path_passes_the_same_filter(monkeypatch, args, want):
    # The existing (non --break-uboot) path hands miniterm the same filter.
    mod = types.ModuleType("serial")
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    argv = []
    monkeypatch.setattr(
        monitor_cmd.subprocess,
        "run",
        lambda a, **k: argv.extend(a) or types.SimpleNamespace(returncode=0),
    )
    r = runner.invoke(app, ["--port", "COM7", "--format", "json", *args])
    assert r.exit_code == 0, r.stdout
    assert argv[argv.index("--filter") + 1] == want
