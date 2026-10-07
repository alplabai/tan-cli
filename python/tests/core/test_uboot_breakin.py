# SPDX-License-Identifier: Apache-2.0
"""Pure break-in logic for `tan monitor --break-uboot` (tan-cli#1315)."""
from __future__ import annotations

import pytest

from tan.core import uboot_breakin as ub


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakePort:
    """Advances the fake clock 0.1 s per read; emits `script[n]` on read n,
    and an optional prompt once `catch_after` keys have been written."""

    def __init__(self, clock, script=(), catch_after=None, prompt=b"=> "):
        self.clock, self.script, self.catch_after, self.prompt = clock, list(script), catch_after, prompt
        self.writes: list[bytes] = []
        self.reads = 0

    def write(self, data):
        self.writes.append(data)

    def read(self, size=1):
        self.clock.t += 0.1
        i, self.reads = self.reads, self.reads + 1
        if self.catch_after is not None and len(self.writes) >= self.catch_after:
            return self.prompt
        return self.script[i] if i < len(self.script) else b""


def test_catches_prompt_and_stops_sending():
    clk = FakeClock()
    port = FakePort(clk, script=[b"Hit any key to stop autoboot:  3\r\n"], catch_after=3)
    res = ub.break_into_uboot(port, timeout_s=30, clock=clk)
    assert res.caught
    assert res.tail.endswith("=> ")
    assert len(port.writes) == 3  # nothing sent after the prompt matched


def test_timeout_reports_not_caught_with_tail():
    clk = FakeClock()
    port = FakePort(clk, script=[b"U-Boot SPL\r\n", b"booting linux\r\n"])
    res = ub.break_into_uboot(port, timeout_s=1.0, clock=clk)
    assert not res.caught
    assert res.elapsed_s >= 1.0
    assert "booting linux" in res.tail
    assert res.bytes_seen == len(b"U-Boot SPL\r\n") + len(b"booting linux\r\n")


def test_at_least_one_key_is_sent_even_with_zero_timeout():
    clk = FakeClock()
    port = FakePort(clk)
    ub.break_into_uboot(port, timeout_s=0, clock=clk)
    assert len(port.writes) == 1


def test_key_is_paced_by_interval_not_per_read():
    clk = FakeClock()
    port = FakePort(clk)
    ub.break_into_uboot(port, timeout_s=1.0, interval_s=0.5, clock=clk)
    assert 2 <= len(port.writes) <= 3


def test_prompt_split_across_reads_is_matched():
    clk = FakeClock()
    port = FakePort(clk, script=[b"foo =", b"> "])
    res = ub.break_into_uboot(port, timeout_s=5, clock=clk)
    assert res.caught


def test_custom_key_and_prompt():
    clk = FakeClock()
    port = FakePort(clk, catch_after=1, prompt=b"V2N# ")
    res = ub.break_into_uboot(port, key=b"\x03", prompt=b"V2N# ", timeout_s=5, clock=clk)
    assert res.caught and port.writes[0] == b"\x03"


def test_tail_is_bounded_and_never_splits_a_multibyte_character():
    clk = FakeClock()
    port = FakePort(clk, script=[("\u00e9" * 1000).encode()])  # 2 bytes each
    res = ub.break_into_uboot(port, timeout_s=0.05, clock=clk)
    assert len(res.tail.encode()) <= ub.TAIL_BYTES
    assert res.tail == "\u00e9" * len(res.tail) and len(res.tail) > 100


class WaitingPort:
    """Prompt is already waiting (in_waiting) before the first key."""

    in_waiting = 3

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def read(self, size=1):
        return b"=> "


def test_no_key_is_sent_when_the_prompt_is_already_waiting():
    port = WaitingPort()
    res = ub.break_into_uboot(port, timeout_s=5)
    assert res.caught and port.writes == []


def test_works_against_pyserial_loop_url():
    serial = pytest.importorskip("serial")
    ser = serial.serial_for_url("loop://", timeout=0.01)
    ser.write(b"Hit any key\r\n=> ")  # pre-seeded "board" output echoes back
    res = ub.break_into_uboot(ser, key=b"x", timeout_s=2)
    ser.close()
    assert res.caught


@pytest.mark.parametrize(
    ("text", "want"),
    [(" ", b" "), ("\\r", b"\r"), ("\\n", b"\n"), ("\\t", b"\t"), ("\\\\", b"\\"),
     ("\\x03", b"\x03"), ("\\xFF", b"\xff"), ("=> ", b"=> "), ("\u00e9", "\u00e9".encode())],
)
def test_parse_escaped(text, want):
    assert ub.parse_escaped(text, "--x") == want


@pytest.mark.parametrize("text", ["", "\\x0", "\\xZZ", "\\x", "\\q", "\\u0041", "\\", "\\0"])
def test_parse_escaped_rejects(text):
    with pytest.raises(ValueError, match="--x"):
        ub.parse_escaped(text, "--x")


@pytest.mark.filterwarnings("error")
def test_parse_escaped_emits_no_deprecation_warning():
    assert ub.parse_escaped("\\x03\\q"[:4], "--x") == b"\x03"
    with pytest.raises(ValueError):
        ub.parse_escaped("\\q", "--x")
