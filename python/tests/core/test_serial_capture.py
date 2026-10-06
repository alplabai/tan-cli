# SPDX-License-Identifier: Apache-2.0
"""Pure capture logic for `tan monitor --capture` (tan-cli#1324)."""
from __future__ import annotations

import io

import pytest

from tan.core import serial_capture as sc


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class Port:
    def __init__(self, clock, chunks):
        self.clock, self.chunks, self.i = clock, list(chunks), 0

    def read(self, size=1):
        self.clock.t += 0.1
        if self.i < len(self.chunks):
            self.i += 1
            return self.chunks[self.i - 1]
        return b""


def test_duration_without_pattern_reads_until_deadline_and_logs_raw_bytes():
    clk, sink = Clock(), io.BytesIO()
    res = sc.capture(Port(clk, [b"abc\x00\xff", b"def\n"]), duration_s=1.0, sink=sink, clock=clk)
    assert not res.matched and res.matched_line is None
    assert res.elapsed_s >= 1.0 and res.bytes_seen == 9
    assert sink.getvalue() == b"abc\x00\xffdef\n"


def test_until_stops_on_first_matching_line_and_returns_it():
    clk = Clock()
    port = Port(clk, [b"boot\r\nZephyr version 4.1\r\n", b"never read\n"])
    res = sc.capture(port, duration_s=30, until=sc.compile_until(r"Zephyr version \d"), clock=clk)
    assert res.matched and res.matched_line == "Zephyr version 4.1"
    assert port.i == 1


def test_line_split_across_reads_is_matched():
    clk = Clock()
    res = sc.capture(Port(clk, [b"Zeph", b"yr ok\n"]), duration_s=5, until=sc.compile_until("Zephyr ok"), clock=clk)
    assert res.matched


def test_unterminated_prompt_matches_without_newline():
    clk = Clock()
    res = sc.capture(Port(clk, [b"hello\r\nlogin: "]), duration_s=5, until=sc.compile_until(r"login: $"), clock=clk)
    assert res.matched and res.matched_line == "login: "


def test_until_timeout_reports_unmatched_with_tail():
    clk = Clock()
    res = sc.capture(Port(clk, [b"nothing here\n"]), duration_s=0.5, until=sc.compile_until("zzz"), clock=clk)
    assert not res.matched and "nothing here" in res.tail


def test_zero_duration_still_reads_once():
    clk = Clock()
    port = Port(clk, [b"x"])
    sc.capture(port, duration_s=0, clock=clk)
    assert port.i == 1


def test_pending_line_is_bounded():
    clk = Clock()
    res = sc.capture(Port(clk, [b"x" * 200000]), duration_s=0.05, until=sc.compile_until("nope"), clock=clk)
    assert len(res.tail) == sc.TAIL_BYTES


@pytest.mark.parametrize("bad", ["", "("])
def test_compile_until_rejects(bad):
    with pytest.raises(ValueError, match="--until"):
        sc.compile_until(bad)
