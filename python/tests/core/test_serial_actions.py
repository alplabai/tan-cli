# SPDX-License-Identifier: Apache-2.0
"""`--send` / `--reopen-at` actions inside the capture loop (tan-cli#1451)."""
from __future__ import annotations

import re

import pytest

from tan.core import serial_actions as sa
from tan.core import serial_capture as sc


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class Port:
    def __init__(self, clock, chunks, name="p0"):
        self.clock, self.chunks, self.name = clock, list(chunks), name
        self.writes: list[bytes] = []
        self.closed = False

    def read(self, size=1):
        self.clock.t += 0.1
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


def _actions(clk, port, spec, reopen=None):
    return sa.CaptureActions(spec, port, reopen or (lambda baud: pytest.fail("reopened")),
                             clock=clk, sleep=lambda s: setattr(clk, "t", clk.t + s))


def test_send_without_trigger_goes_out_at_the_first_iteration_in_order():
    clk = Clock()
    port = Port(clk, [b"boot\n"])
    acts = _actions(clk, port, sa.ActionSpec(sends=(b"v", b"m\n")))
    sc.capture(port, duration_s=1.0, clock=clk, actions=acts)
    assert port.writes == [b"v", b"m\n"]
    assert acts.events[0]["action"] == "send" and acts.events[0]["bytes"] == 3
    assert acts.sends_pending == 0


def test_send_after_waits_for_the_delay():
    clk = Clock()
    port = Port(clk, [b"x"] * 20)
    acts = _actions(clk, port, sa.ActionSpec(sends=(b"s",), send_after_s=0.5))
    sc.capture(port, duration_s=2.0, clock=clk, actions=acts)
    assert port.writes == [b"s"]
    assert 0.5 <= acts.events[0]["atSeconds"] < 0.8


def test_send_on_fires_once_on_the_first_matching_line_even_without_until():
    clk = Clock()
    port = Port(clk, [b"hello\n", b"Press key:\n", b"Press key:\n"])
    spec = sa.ActionSpec(sends=(b"l",), send_on=re.compile("Press key"))
    acts = _actions(clk, port, spec)
    res = sc.capture(port, duration_s=1.0, clock=clk, actions=acts)
    assert port.writes == [b"l"] and len(acts.events) == 1
    assert not res.matched and res.bytes_seen == len(b"hello\nPress key:\nPress key:\n")


def test_send_on_matches_an_unterminated_prompt():
    clk = Clock()
    port = Port(clk, [b"menu> "])
    acts = _actions(clk, port, sa.ActionSpec(sends=(b"1",), send_on=re.compile(r"menu> $")))
    sc.capture(port, duration_s=0.5, clock=clk, actions=acts)
    assert port.writes == [b"1"]


def test_send_never_fires_when_the_trigger_never_matches():
    clk = Clock()
    port = Port(clk, [b"nothing\n"])
    acts = _actions(clk, port, sa.ActionSpec(sends=(b"v",), send_on=re.compile("zzz")))
    sc.capture(port, duration_s=0.5, clock=clk, actions=acts)
    assert port.writes == [] and acts.sends_pending == 1


def test_reopen_replaces_the_port_and_capture_continues_on_the_new_one():
    clk = Clock()
    old = Port(clk, [b"wake garbage\n", b"never read\n"], "old")
    new = Port(clk, [b"hello at 23040\n"], "new")
    opened: list[int] = []

    def reopen(baud):
        opened.append(baud)
        return new

    spec = sa.ActionSpec(reopen_baud=23040, reopen_on=re.compile("wake"))
    acts = _actions(clk, old, spec, reopen)
    res = sc.capture(old, duration_s=1.0, until=re.compile("hello at"), clock=clk, actions=acts)
    assert opened == [23040] and old.closed and acts.port is new
    assert res.matched and res.matched_line == "hello at 23040"
    assert acts.events[0]["baud"] == 23040


def test_reopen_happens_once_only():
    clk = Clock()
    ports = [Port(clk, [b"wake\n", b"wake\n"]), Port(clk, [b"wake\n"])]
    opened: list[int] = []

    def reopen(baud):
        opened.append(baud)
        return ports[1]

    spec = sa.ActionSpec(reopen_baud=9600, reopen_on=re.compile("wake"))
    acts = _actions(clk, ports[0], spec, reopen)
    sc.capture(ports[0], duration_s=1.0, clock=clk, actions=acts)
    assert opened == [9600]


def test_a_failed_reopen_is_an_action_error():
    clk = Clock()
    port = Port(clk, [b"wake\n"])

    def reopen(baud):
        raise OSError("gone")

    acts = _actions(clk, port, sa.ActionSpec(reopen_baud=9600, reopen_on=re.compile("wake")), reopen)
    with pytest.raises(sa.ActionError) as err:
        sc.capture(port, duration_s=1.0, clock=clk, actions=acts)
    assert err.value.kind == "reopen" and "gone" in str(err.value)


def test_a_failed_write_is_an_action_error():
    clk = Clock()
    port = Port(clk, [b"x"])

    def boom(data):
        raise OSError("stalled")

    port.write = boom
    acts = _actions(clk, port, sa.ActionSpec(sends=(b"v",)))
    with pytest.raises(sa.ActionError) as err:
        sc.capture(port, duration_s=1.0, clock=clk, actions=acts)
    assert err.value.kind == "send"
