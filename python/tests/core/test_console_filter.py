# SPDX-License-Identifier: Apache-2.0
"""The `colors` console filter: SGR passes, every other escape is neutralised
(terminal-escape injection from an untrusted serial target), stream-safe."""
from __future__ import annotations

import subprocess
import sys

import pytest

from tan.core import console_filter as cf

P = cf.ColorsFilter.PLACEHOLDER


def feed(*chunks: str) -> str:
    f = cf.ColorsFilter()
    return "".join(f.rx(c) for c in chunks)


def test_sgr_colours_pass_through_unchanged():
    s = "\x1b[1;32mok\x1b[0m \x1b[m\x1b[38;5;196mred\x1b[0m\r\n"
    assert feed(s) == s


def test_plain_text_and_allowed_controls_pass():
    assert feed("a\tb\bc\r\n") == "a\tb\bc\r\n"
    assert feed("héllo 世") == "héllo 世"


@pytest.mark.parametrize(
    "seq",
    [
        "\x1b]52;c;ZXZpbA==\x07",  # OSC 52 clipboard write, BEL-terminated
        "\x1b]52;c;ZXZpbA==\x1b\\",  # ... ST-terminated
        "\x1b]0;pwned\x07",  # title
        "\x1b]2;pwned\x1b\\",
        "\x1bPq#0;2;0;0;0\x1b\\",  # DCS
        "\x1b_payload\x1b\\",  # APC
        "\x1b^payload\x1b\\",  # PM
        "\x1bXpayload\x1b\\",  # SOS
        "\x1b[2J",  # erase screen
        "\x1b[H",  # cursor home
        "\x1b[10;20H",
        "\x1b[?1049h",  # alt screen
        "\x1b[6n",  # cursor position report (reply injection)
        "\x1b[1;32;?m",  # malformed SGR
        "\x1b[1 m",  # intermediate byte: not SGR
        "\x1bc",  # RIS reset
        "\x1b(B",  # charset select
        "\x9b2J",  # C1 CSI
        "\x9d52;c;AAAA\x9c",  # C1 OSC .. C1 ST
        "\x90q\x9c",  # C1 DCS
        "\x07", "\x00", "\x7f", "\x0b", "\x1a",
    ],
)
def test_everything_else_is_neutralised(seq):
    out = feed("<", seq, ">")
    assert "\x1b" not in out and not any(0x80 <= ord(c) <= 0x9F for c in out)
    assert out.startswith("<") and out.endswith(">")
    assert P in out


def test_osc_payload_is_swallowed_not_printed():
    assert "ZXZpbA" not in feed("\x1b]52;c;ZXZpbA==\x07rest")
    assert feed("\x1b]52;c;ZXZpbA==\x07rest").endswith("rest")


@pytest.mark.parametrize("split", range(1, 14))
def test_sgr_split_across_reads(split):
    s = "\x1b[1;32mok\x1b[0m"
    assert feed(s[:split], s[split:]) == s


@pytest.mark.parametrize("split", range(1, 12))
def test_osc_split_across_reads_is_still_swallowed(split):
    s = "\x1b]52;c;QUJD\x07z"
    out = feed(s[:split], s[split:])
    assert out == P + "z"


def test_byte_by_byte_stream_matches_one_shot():
    s = "a\x1b[31mb\x1b]0;t\x07c\x1b[2Jd\x9b1;2He\x1b\\"
    assert feed(*s) == feed(s)


def test_unterminated_osc_cannot_swallow_the_stream_forever():
    # The string is abandoned once it exceeds the cap (4097th payload char, 2 of which are "0;"),
    # one placeholder marks it, and later output flows again.
    out = feed("\x1b]0;" + "x" * 10000, "visible")
    assert out == P + "x" * (10000 - 4095) + "visible"


def test_unterminated_csi_is_bounded():
    out = feed("\x1b[" + "1;" * 100 + "m", "ok")
    assert "\x1b" not in out and out.endswith("ok")


def test_esc_inside_string_starts_a_new_sequence():
    # OSC aborted by a fresh SGR: the SGR is honoured, nothing else leaks.
    assert feed("\x1b]0;t\x1b[1mX") == P + "\x1b[1mX"


def test_bootstrap_runs_miniterm_main_with_the_filter_registered():
    # Real interpreter, real pyserial: the spawned-console script must import,
    # register `colors` and reach miniterm's argparse (`--help` exits 0).
    pytest.importorskip("serial")
    r = subprocess.run(
        [sys.executable, "-c", cf.BOOTSTRAP, "--help"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    assert "usage" in r.stdout.lower()


def test_a_stray_esc_does_not_eat_the_following_newline():
    assert feed("a\x1b\nb") == "a" + P + "\nb"
    assert feed("a\x1b\r\nb") == "a" + P + "\r\nb"
    assert feed("a\x1b\tb") == "a" + P + "\tb"


@pytest.mark.parametrize(
    ("sgr", "want"),
    [
        ("\x1b[8m", ""),  # conceal
        ("\x1b[5m", ""),  # slow blink
        ("\x1b[6m", ""),  # rapid blink
        ("\x1b[1;8;31m", "\x1b[1;31m"),  # rest of the sequence kept
        ("\x1b[5;1m", "\x1b[1m"),
        ("\x1b[38;5;5m", "\x1b[38;5;5m"),  # 5 is an operand here, not blink
        ("\x1b[48;5;8m", "\x1b[48;5;8m"),
        ("\x1b[38;2;8;5;6m", "\x1b[38;2;8;5;6m"),
        ("\x1b[m", "\x1b[m"),
        ("\x1b[0m", "\x1b[0m"),
        ("\x1b[;1m", "\x1b[;1m"),
    ],
)
def test_sgr_conceal_and_blink_are_removed_but_the_rest_survives(sgr, want):
    assert feed(sgr) == want


def test_colon_form_sgr_is_dropped():
    out = feed("\x1b[38:2::1:2:3mX")
    assert out == P + "X"


@pytest.mark.parametrize(
    "ch", ["\u202a", "\u202e", "\u2066", "\u2069", "\u2028", "\u2029", "\u200b", "\u200f", "\ufeff"]
)
def test_bidi_and_format_controls_are_neutralised(ch):
    assert feed("a" + ch + "b") == "a" + P + "b"


def test_placeholder_falls_back_to_ascii_when_the_stream_cannot_encode_it(monkeypatch):
    import io

    ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    monkeypatch.setattr(sys, "stdout", ascii_out)
    f = cf.ColorsFilter()
    assert f.rx("a\x07b") == "a?b"
