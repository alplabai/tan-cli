# SPDX-License-Identifier: Apache-2.0
"""`--break-uboot` over pyserial's REAL rfc2217 client, against a minimal
in-test RFC 2217 server (pyserial's own `PortManager` on a `loop://` port).
pyserial's rfc2217 client rejects `write_timeout`; this is the labgrid
ser2net console shape (tan-cli#1315 bench)."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import typer
from typer.testing import CliRunner

serial = pytest.importorskip("serial")
rfc2217 = pytest.importorskip("serial.rfc2217")

from tan.commands import monitor_cmd, monitor_session  # noqa: E402
from tan.commands.monitor_cmd import monitor  # noqa: E402

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)
runner = CliRunner()


class _Rfc2217Server:
    """Accepts connections one after another (the first, from a client that
    passes `write_timeout`, is dropped by the client itself)."""

    def __init__(self, preload: bytes) -> None:
        self.preload = preload
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.stop = threading.Event()
        self.received = bytearray()
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"rfc2217://127.0.0.1:{self.listener.getsockname()[1]}"

    def _accept(self) -> None:
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        ser = serial.serial_for_url("loop://", timeout=0.05)

        class Out:
            def write(self, data):
                conn.sendall(data)

        manager = rfc2217.PortManager(ser, Out())

        def board_to_client() -> None:
            time.sleep(0.6)  # the board prints after the client finished negotiating
            ser.write(self.preload)
            while not self.stop.is_set():
                try:
                    data = ser.read(ser.in_waiting or 1)
                    if data:
                        conn.sendall(b"".join(manager.escape(data)))
                except (OSError, serial.SerialException):
                    return

        threading.Thread(target=board_to_client, daemon=True).start()
        conn.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            payload = b"".join(manager.filter(data))
            self.received += payload
            ser.write(payload)  # loop:// echoes it back to the client too

    def close(self) -> None:
        self.stop.set()
        self.listener.close()


@pytest.fixture
def server():
    srv = _Rfc2217Server(b"U-Boot SPL\r\n=> ")
    yield srv
    srv.close()


def test_open_port_falls_back_when_rfc2217_rejects_write_timeout(server):
    ser = monitor_session.open_port(server.url, 115200)
    try:
        assert getattr(ser, "_tan_guard_writes", False) is True
    finally:
        ser.close()


def test_cli_break_uboot_over_rfc2217_exits_0_with_a_caught_envelope(server, monkeypatch):
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    r = runner.invoke(
        app,
        ["--port", server.url, "--break-uboot", "--non-interactive", "--break-timeout", "10",
         "--format", "json"],
    )
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["data"]["breakIn"]["caught"] is True
    assert doc["issues"] == []
