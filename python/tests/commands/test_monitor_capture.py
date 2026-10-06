# SPDX-License-Identifier: Apache-2.0
"""`tan monitor --capture` command wiring (tan-cli#1324). pyserial is a
scripted fake; the pure loop is tested in tests/core/test_serial_capture.py."""
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


def _fake_serial(monkeypatch, chunks, *, prompt_on_write=False, read_raises=False):
    mod = types.ModuleType("serial")
    made = []

    class SerialException(IOError):
        pass

    class Port:
        def __init__(self):
            self.chunks = list(chunks)
            self.closed = False
            self.writes = []
            made.append(self)

        def write(self, d):
            self.writes.append(d)
            if prompt_on_write:
                self.chunks.insert(0, b"=> ")

        def read(self, size=1):
            if read_raises:
                raise OSError("link dropped")
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            self.closed = True

    def serial_for_url(url, baud, timeout=None, write_timeout=None):
        if "refused" in url:
            raise SerialException("refused")
        return Port()

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


def run(args):
    return runner.invoke(app, ["--port", "rfc2217://gw:4000", "--format", "json", *args])


def test_until_match_reports_line_logs_raw_and_closes_port(monkeypatch, tmp_path):
    made = _fake_serial(monkeypatch, [b"boot\r\n", b"Zephyr version 4.1\r\n"])
    log = tmp_path / "boot.log"
    r = run(["--capture", "--until", r"Zephyr version", "--log", str(log)])
    assert r.exit_code == 0, r.stdout
    cap = envelope(r)["data"]["capture"]
    assert cap["matched"] is True and cap["matchedLine"] == "Zephyr version 4.1"
    assert cap["logFile"] == str(log)
    assert log.read_bytes() == b"boot\r\nZephyr version 4.1\r\n"
    assert made[0].closed


def test_until_without_match_is_capture_timeout(monkeypatch):
    _fake_serial(monkeypatch, [b"nothing\n"])
    r = run(["--capture", "--until", "zzz", "--duration", "0.3"])
    assert r.exit_code == 1
    doc = envelope(r)
    assert doc["issues"][0]["code"] == "monitor.capture-timeout"
    assert doc["data"]["capture"]["matched"] is False


def test_duration_only_succeeds_with_matched_null(monkeypatch):
    _fake_serial(monkeypatch, [b"abc"])
    r = run(["--capture", "--duration", "0.2"])
    assert r.exit_code == 0
    cap = envelope(r)["data"]["capture"]
    assert cap["matched"] is False and cap["untilGiven"] is False and cap["bytesSeen"] == 3


def test_capture_composes_with_break_uboot(monkeypatch):
    made = _fake_serial(monkeypatch, [b"U-Boot 2025\r\n"], prompt_on_write=True)
    r = run(["--capture", "--break-uboot", "--until", r"U-Boot 2025", "--duration", "5"])
    assert r.exit_code == 0, r.stdout
    data = envelope(r)["data"]
    assert data["breakIn"]["caught"] is True
    assert data["capture"]["matched"] is True
    assert len(made) == 1 and made[0].closed  # one open, shared by both phases


def test_capture_after_missed_break_in_is_break_timeout(monkeypatch):
    _fake_serial(monkeypatch, [b"noise"])
    r = run(["--capture", "--break-uboot", "--break-timeout", "0.2", "--duration", "1"])
    assert r.exit_code == 1
    doc = envelope(r)
    assert doc["issues"][0]["code"] == "monitor.break-timeout"
    assert "capture" not in doc["data"]


def test_open_failure(monkeypatch):
    _fake_serial(monkeypatch, [])
    r = runner.invoke(
        app, ["--port", "socket://refused:1", "--format", "json", "--capture", "--duration", "1"]
    )
    assert r.exit_code == 1
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-open-failed"


def test_unwritable_log(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [])
    r = run(["--capture", "--duration", "1", "--log", str(tmp_path / "no" / "dir" / "x.log")])
    assert r.exit_code == 3
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-log-failed"


@pytest.mark.parametrize(
    "args",
    [["--capture"], ["--capture", "--duration", "0"], ["--capture", "--until", "("],
     ["--duration", "5"], ["--until", "x"], ["--log", "x"]],
)
def test_bad_options(monkeypatch, args):
    _fake_serial(monkeypatch, [])
    r = run(args)
    assert r.exit_code == 2
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-bad-option"


def test_io_error_while_reading_is_reported_and_closes(monkeypatch):
    made = _fake_serial(monkeypatch, [], read_raises=True)
    r = run(["--capture", "--duration", "1"])
    assert r.exit_code == 1
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-io-failed"
    assert made[0].closed


def test_log_is_0600_raw_bytes_and_path_is_reported_absolute(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [b"\x1b[1;32mok\x1b[0m\r\n"])
    monkeypatch.chdir(tmp_path)
    r = run(["--capture", "--duration", "0.2", "--log", "raw.log"])
    assert r.exit_code == 0
    log = tmp_path / "raw.log"
    assert log.read_bytes() == b"\x1b[1;32mok\x1b[0m\r\n"
    assert (log.stat().st_mode & 0o777) == 0o600
    assert envelope(r)["data"]["capture"]["logFile"] == str(log)


def test_log_refuses_a_symlink(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [])
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "x.log"
    link.symlink_to(victim)
    r = run(["--capture", "--duration", "0.2", "--log", str(link)])
    assert r.exit_code == 3
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-log-failed"
    assert victim.read_text() == "keep"


def test_filter_is_refused_under_capture(monkeypatch):
    _fake_serial(monkeypatch, [])
    r = run(["--capture", "--duration", "1", "--filter", "printable"])
    assert r.exit_code == 2
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-bad-option"


def test_non_interactive_is_accepted_under_capture(monkeypatch):
    _fake_serial(monkeypatch, [b"x"])
    assert run(["--capture", "--duration", "0.2", "--non-interactive"]).exit_code == 0


def test_port_is_closed_after_a_capture_timeout(monkeypatch):
    made = _fake_serial(monkeypatch, [b"nothing\n"])
    r = run(["--capture", "--until", "zzz", "--duration", "0.2"])
    assert r.exit_code == 1 and made[0].closed


def test_log_is_not_created_when_the_break_in_fails(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [b"noise"])
    log = tmp_path / "never.log"
    r = run(["--capture", "--break-uboot", "--break-timeout", "0.2", "--duration", "1", "--log", str(log)])
    assert r.exit_code == 1
    assert not log.exists()


def test_log_is_not_created_when_the_port_cannot_open(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [])
    log = tmp_path / "never.log"
    r = runner.invoke(
        app,
        ["--port", "socket://refused:1", "--format", "json", "--capture", "--duration", "1",
         "--log", str(log)],
    )
    assert r.exit_code == 1 and not log.exists()


def test_break_in_output_reaches_the_log_and_until(monkeypatch, tmp_path):
    _fake_serial(monkeypatch, [], prompt_on_write=True)
    log = tmp_path / "b.log"
    r = run(["--capture", "--break-uboot", "--until", r"=> $", "--duration", "2", "--log", str(log)])
    assert r.exit_code == 0, r.stdout
    cap = envelope(r)["data"]["capture"]
    assert cap["matched"] is True and cap["matchedLine"] == "=> "
    assert log.read_bytes().endswith(b"=> ")


def test_sink_failure_closes_port_and_sink_and_is_not_a_serial_error(monkeypatch, tmp_path):
    from tan.commands import monitor_session

    made = _fake_serial(monkeypatch, [b"data"])
    state = {}

    class Full:
        def write(self, b):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(monitor_session, "_open_log", lambda path, data: Full())
    r = run(["--capture", "--duration", "1", "--log", str(tmp_path / "f.log")])
    assert r.exit_code == 3
    assert envelope(r)["issues"][0]["code"] == "monitor.capture-log-failed"
    assert made[0].closed and state["closed"]
