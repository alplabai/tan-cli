# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1436: `--watch` spec parsing, script generation and mem32 parsing."""
import pytest

from tan.core import ram_watch as rw


def test_defaults_and_forms():
    s = rw.parse_watch("0x42002000")
    assert (s.address, s.words, s.period_ms) == (0x42002000, 1, 100)
    s = rw.parse_watch("1073741824:4@250")
    assert (s.address, s.words, s.period_ms) == (0x40000000, 4, 250)


def test_script_interleaves_sleep_and_reads_and_pads_to_the_window():
    specs = rw.parse_watches(["0x42002000@100", "0x20000000:2@250"])
    lines = rw.watch_lines(specs, 500)
    assert all(l.startswith(("mem32 ", "Sleep ")) for l in lines)  # read-only, nothing else
    total = sum(int(l.split()[1]) for l in lines if l.startswith("Sleep"))
    assert total == 500


def test_too_many_samples_refused():
    with pytest.raises(rw.WatchError):
        rw.watch_lines(rw.parse_watches(["0x0@10"]), 60_000)


def test_parse_samples_multiword_and_missing():
    specs = rw.parse_watches(["0x20000000:6@100"])
    text = (
        "20000000 = 00000001 00000002 00000003 00000004\n"
        "20000010 = 00000005 00000006\n"
        "noise\n"
        "20000000 = 0000000A 0000000B 0000000C 0000000D\n"
    )
    out = rw.parse_samples(text, specs, 100)
    assert out[0]["values"] == [f"0x{n:08X}" for n in range(1, 7)]
    assert out[0]["elapsedMs"] == 0 and out[1]["elapsedMs"] == 100
    assert out[1]["values"] is None  # second line of the second read is absent


@pytest.mark.parametrize("bad", ["0x1", "0x4:0", "0x4:65", "0x4@9", "0x4@60001", "zz", "0x100000000", "0xFFFFFFFC:2"])
def test_invalid_specs(bad):
    with pytest.raises(rw.WatchError) as e:
        rw.parse_watch(bad)
    assert e.value.code == rw.CODE_INVALID


def test_denylist_is_the_hp_window():
    for bad in ("0x50000000", "0x57FFFFFC", "0x4FFFFFF8:4"):
        with pytest.raises(rw.WatchError) as e:
            rw.parse_watch(bad)
        assert e.value.code == rw.CODE_UNSAFE
    assert rw.parse_watch("0x58000000").address == 0x58000000
