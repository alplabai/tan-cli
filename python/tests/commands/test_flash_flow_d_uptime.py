# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1539: the kernel-uptime witness that the NEW image booted (Flow D).

No hardware: a hand-built ELF32 carrying `curr_tick` and a fake J-Link transcript."""
from __future__ import annotations

import json
import struct

import pytest

from tan.commands import flash_cmd
from tan.core import flow_d_uptime
from tan.core.flow_d_uptime import UptimeProbe
from tan.core.ram_run import parse_elf
from tests.commands.test_flash_flow_d_report import _HALT_FAIL, _run
from tests.commands.test_flash_probe_selection import _codes

TICK = 0x200088C0
HZ = 10000
DHCSR = "E000EDF0 = 01040001\n"  # running, no S_RESET_ST: the DHCSR witness does not apply


def make_elf(symbols, load=(0x20000000, 0x10000)) -> bytes:
    """ELF32 LE: the slot0 code PT_LOAD plus one RW PT_LOAD at `load` and `symbols` {name: (value, size)}."""
    strtab = b"\0"
    syms = struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)
    for name, (value, size) in (symbols.items() if isinstance(symbols, dict) else symbols):
        syms += struct.pack("<IIIBBH", len(strtab), value, size, 0x11, 0, 1)
        strtab += name.encode() + b"\0"
    ehsize, phsize, shsize = 52, 32, 40
    sym_off = ehsize + 2 * phsize
    str_off = sym_off + len(syms)
    sh_off = str_off + len(strtab)
    out = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    out += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0x80010000, ehsize, sh_off, 0,
                       ehsize, phsize, 2, shsize, 3, 0)
    out += struct.pack("<IIIIIIII", 1, 0, 0x80010000, 0x80010000, 1, 1, 5, 4)  # the slot0 code
    out += struct.pack("<IIIIIIII", 1, 0, load[0], load[0], 0, load[1], 6, 4)
    out += syms + strtab
    out += b"\0" * shsize
    out += struct.pack("<IIIIIIIIII", 0, 2, 0, 0, sym_off, len(syms), 2, 0, 4, 16)
    out += struct.pack("<IIIIIIIIII", 0, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)
    return out


GOOD = {"curr_tick": (TICK, 8), "CONFIG_SYS_CLOCK_TICKS_PER_SEC": (HZ, 0)}


def reads(*values: int, address: int = TICK) -> str:
    return "".join(
        f"{address:08X} = {v & 0xFFFFFFFF:08X} {v >> 32:08X}\n" for v in values
    )


def _flash(tmp_path, monkeypatch, probe_out, elf: bytes | None, elapsed: float = 20.0):
    """`elapsed` fakes the monotonic seconds between the write ending and the probe ending."""
    stamps = [0.0, elapsed]
    # Tolerates extra `_now()` calls: every call after the second keeps returning `elapsed`.
    monkeypatch.setattr(flash_cmd, "_now", lambda: stamps.pop(0) if len(stamps) > 1 else stamps[0])
    (tmp_path / "build").mkdir(exist_ok=True)
    if elf is not None:
        (tmp_path / "build" / "a.elf").write_bytes(elf)
    fake, out = _run(
        tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": probe_out}
    )
    return fake, out


def _jl(out):
    rc, data, issues, _lines, _s = out
    assert rc == 0, (data, issues)
    return data["entries"][0]["jlink"], issues, data


def test_a_small_increasing_tick_counter_confirms_the_new_image(tmp_path, monkeypatch):
    fake, out = _flash(tmp_path, monkeypatch, DHCSR + reads(12000, 18000, 24000, 30000), make_elf(GOOD))
    jl, issues, data = _jl(out)
    assert jl["reset"] == "booted" and jl["resetConfirmedBy"] == "uptime"
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)
    assert jl["bootProbe"]["uptime"]["address"] == "0x200088C0"
    assert jl["bootProbe"]["uptime"]["ticks"] == ["0x2EE0", "0x4650", "0x5DC0", "0x7530"]
    assert jl["bootProbe"]["uptime"]["ticksPerSec"] == HZ
    msg = data["entries"][0]["message"]
    assert "boot proven, PIN reset not" in msg and "curr_tick" in msg and "and PIN-reset" not in msg
    assert jl["bootProbe"]["uptime"]["elapsedSinceWriteSeconds"] == 20.0
    probe = next(s for s in fake.scripts if "mem32" in s)
    assert f"mem32 0x{TICK:08X} 2" in probe  # the address came from the ELF
    assert not any(w in probe for w in ("RSetType", "loadbin", "halt", "\ng\n"))


def test_an_uptime_longer_than_the_time_since_the_write_is_not_this_boot(tmp_path, monkeypatch):
    """A replaced image that booted before the write keeps (or resumes) counting: 3 s of
    uptime cannot belong to a boot that happened after a write that ended 1 s ago."""
    _f, out = _flash(tmp_path, monkeypatch, DHCSR + reads(12000, 18000, 24000, 30000),
                     make_elf(GOOD), elapsed=1.0)
    jl, issues, _d = _jl(out)
    assert jl["reset"] == "unconfirmed" and "resetConfirmedBy" not in jl
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)
    assert jl["bootProbe"]["uptime"]["ticks"][-1] == "0x7530"


def test_a_large_counter_is_the_old_image_up_a_long_time(tmp_path, monkeypatch):
    two_hours = 2 * 3600 * HZ
    _f, out = _flash(tmp_path, monkeypatch, DHCSR + reads(two_hours, two_hours + 6000, two_hours + 12000), make_elf(GOOD))
    jl, issues, _d = _jl(out)
    assert jl["reset"] == "unconfirmed" and "resetConfirmedBy" not in jl
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)


def test_a_counter_that_does_not_advance_is_not_a_boot(tmp_path, monkeypatch):
    _f, out = _flash(tmp_path, monkeypatch, DHCSR + reads(500, 500, 500), make_elf(GOOD))
    jl, issues, _d = _jl(out)
    assert jl["reset"] == "unconfirmed" and "resetConfirmedBy" not in jl
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)


def test_a_missing_ambiguous_or_misplaced_symbol_stays_unconfirmed(tmp_path, monkeypatch):
    transcript = DHCSR + reads(12000, 18000, 24000)
    for name, elf in {
        "no elf": None,
        "no symbol": make_elf({"CONFIG_SYS_CLOCK_TICKS_PER_SEC": (HZ, 0)}),
        "hp itcm": make_elf({"curr_tick": (0x50000100, 8), "CONFIG_SYS_CLOCK_TICKS_PER_SEC": (HZ, 0)},
                            load=(0x50000000, 0x10000)),
        "outside the image": make_elf(GOOD, load=(0x20010000, 0x1000)),
        "no tick rate": make_elf({"curr_tick": (TICK, 8)}),
    }.items():
        (tmp_path / "build").mkdir(exist_ok=True)
        (tmp_path / "build" / "a.elf").unlink(missing_ok=True)
        _f, out = _flash(tmp_path, monkeypatch, transcript, elf)
        jl, issues, _d = _jl(out)
        assert jl["reset"] == "unconfirmed" and "resetConfirmedBy" not in jl, name
        assert "uptime" not in jl["bootProbe"], name
        assert not any("0x5000" in s for s in _f.scripts), name
        assert "flash.jlink-reset-unconfirmed" in _codes(issues), name


def test_the_tick_rate_falls_back_to_the_build_config(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / ".config").write_text("CONFIG_SYS_CLOCK_TICKS_PER_SEC=100\n")
    _f, out = _flash(tmp_path, monkeypatch, DHCSR + reads(300, 400, 500), make_elf({"curr_tick": (TICK, 8)}))
    jl, _i, _d = _jl(out)
    assert jl["resetConfirmedBy"] == "uptime" and jl["bootProbe"]["uptime"]["ticksPerSec"] == 100


def test_the_same_pc_samples_without_a_counter_still_do_not_confirm(tmp_path, monkeypatch):
    """tan-cli#1537 stays: PCSR alone proves nothing even when an ELF is present."""
    out = DHCSR + "E000101C = 80010000\n" * 3
    _f, res = _flash(tmp_path, monkeypatch, out, make_elf(GOOD))
    jl, _i, data = _jl(res)
    assert jl["reset"] == "unconfirmed" and "new-image-running" not in json.dumps(data)


def test_a_duplicate_static_symbol_is_ambiguous_and_gets_no_probe():
    one = parse_elf(make_elf({"curr_tick": (TICK, 8)}))
    assert flow_d_uptime.plan_uptime_probe(one, HZ) == UptimeProbe(TICK, 8, HZ)
    twice = parse_elf(make_elf([("curr_tick", (TICK, 8)), ("curr_tick", (TICK + 8, 8))]))
    assert "curr_tick" in twice.ambiguous
    assert flow_d_uptime.plan_uptime_probe(twice, HZ) is None


def test_confirmation_arithmetic():
    ok = flow_d_uptime.uptime_confirms_boot
    assert ok([10, 20], 10, 5.0) and ok([0, 5], 10, 0.0)
    assert not ok([20], 10, 5.0) and not ok([20, 20], 10, 5.0) and not ok([20, 10], 10, 5.0)
    assert not ok([0xFFFFFFFF, 0xFFFFFFFF], 10, 5.0)
    # the bound is the elapsed time plus the slack, not a fixed number of seconds
    assert ok([10, 10 + 59 * 10], 10, 59.5) and not ok([10, 10 + 59 * 10], 10, 58.0)
    # a read torn across a 32-bit carry (low word wrapped, high word not yet bumped)
    assert not ok([0xFFFFFF00, 0x00000010], 10, 1e9) and not ok([5, 0x1_0000_0005], 10, 100.0)


@pytest.mark.parametrize("over,confirmed", [(0.9, True), (1.1, False)])
def test_the_slack_boundary(tmp_path, monkeypatch, over, confirmed):
    """Realistic timing: uptime 3.0 s (30000 ticks) against elapsed = 3.0 - over."""
    _f, out = _flash(tmp_path, monkeypatch, DHCSR + reads(12000, 18000, 24000, 30000),
                     make_elf(GOOD), elapsed=3.0 - over)
    jl, _issues, _d = _jl(out)
    assert (jl["reset"] == "booted") is confirmed
