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


def test_initial_bytes_are_logged_and_searched_before_any_read():
    clk, sink = Clock(), io.BytesIO()
    port = Port(clk, [])
    res = sc.capture(
        port, duration_s=5, until=sc.compile_until(r"=> $"), sink=sink,
        initial=b"Hit any key\r\n=> ", clock=clk,
    )
    assert res.matched and res.matched_line == "=> " and port.i == 0
    assert sink.getvalue() == b"Hit any key\r\n=> "


class BadSink:
    def write(self, b):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass


def test_sink_failure_is_a_sink_error_not_a_serial_error():
    clk = Clock()
    with pytest.raises(sc.SinkError, match="No space"):
        sc.capture(Port(clk, [b"x"]), duration_s=5, sink=BadSink(), clock=clk)


class DeadPort:
    def read(self, size=1):
        raise OSError("link dropped")


def test_serial_failure_propagates_as_oserror():
    with pytest.raises(OSError, match="link dropped"):
        sc.capture(DeadPort(), duration_s=5)


class CountingPattern:
    """A pattern whose every `search` costs 1 s of fake time, to prove the
    deadline is honoured between lines of one big read."""

    def __init__(self, clock):
        self.clock, self.calls = clock, 0

    def search(self, s):
        self.calls += 1
        self.clock.t += 1.0
        return None


def test_a_flood_of_lines_cannot_overrun_the_duration():
    clk = Clock()
    pat = CountingPattern(clk)
    flood = b"line\n" * 10000
    res = sc.capture(Port(clk, [flood]), duration_s=3.0, until=pat, clock=clk)
    assert not res.matched
    assert pat.calls <= 5  # stopped at the deadline, not after 10000 searches


def test_search_text_per_regex_call_is_capped():
    clk = Clock()
    seen = []

    class P:
        def search(self, s):
            seen.append(len(s))

    sc.capture(Port(clk, [b"y" * 100000 + b"\n", b"z" * 100000]), duration_s=0.15, until=P(), clock=clk)
    assert seen and max(seen) <= sc.MAX_SEARCH_CHARS


def test_each_complete_line_is_searched_once_and_partial_rechecked_per_read():
    clk = Clock()
    seen = []

    class P:
        def search(self, s):
            seen.append(s)

    sc.capture(Port(clk, [b"one\ntw", b"o\n"]), duration_s=0.25, until=P(), clock=clk)
    assert seen == ["one", "tw", "two"]  # no line is searched twice as complete
