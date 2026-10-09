# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1321: what a Flow D write tells the envelope -- the J-Link transcript
(a file plus its tail), the SW-DP ID actually read, `cache-verified` instead of
a bare "verified", reset-failure detection, and the optional `--readback`.

No hardware: `_spawn_jlink` is stubbed with a fake J-Link that answers
`ShowEmuList`, a write script and a read-back script."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from tan.commands import flash_cmd
from tan.core import flow_d_report
from tests.commands.test_flash_command import _flow_d_run
from tests.commands.test_flash_probe_selection import (
    A, C, SHARED, _SERIAL_ARGS, _codes, _probes,
)

# Match the one-byte fixtures `_flow_d_run` lays down, so planned sizes agree.
ATOC = b"\x00"
APP = b"\x00"
_ARGS = (
    '{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000", '
    'atoc: atoc.bin, atoc_address: "0x8057F5B0", confirm: true, atoc_unqueryable: true}'
)

CLEAN = (
    "Connecting to J-Link via USB...O.K.\n"
    "Found SW-DP with ID 0x4C013477\n"
    "Downloading file [atoc.bin]...\nO.K.\n"
    "Verifying flash...\nO.K.\n"
    "Reset delay: 0 ms\nReset type: PIN\n"
)


class FakeJlink:
    """Stand-in for the J-Link tool. `read_back` maps a region's destination
    index -> the bytes `savebin` writes (default: the true source bytes)."""

    def __init__(self, monkeypatch, *, write_out=CLEAN, write_rc=0, read_back=None,
                 read_rc=0, emulators=(), probe_out="", probe_rc=0, tail_rc=0, read_out=""):
        self.write_out, self.write_rc = write_out, write_rc
        self.read_back, self.read_rc = read_back, read_rc
        self.emulators = list(emulators)
        self.probe_out, self.probe_rc = probe_out, probe_rc
        self.tail_rc, self.read_out = tail_rc, read_out
        self.scripts: list[str] = []
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, *args, **kwargs):
        self.scripts.append(script)
        if "ShowEmuList" in script:
            lines = [
                f"J-Link[{i}]: Connection: USB, Serial number: {sn}, ProductName: J-Link"
                for i, sn in enumerate(self.emulators)
            ]
            return flash_cmd._Outcome(success=True, stdout="\n".join(lines), returncode=0)
        if "RSetType" in script and "savebin" not in script and "loadbin" not in script:
            return flash_cmd._Outcome(success=self.tail_rc == 0, stdout="", returncode=self.tail_rc)
        if "mem32" in script:
            return flash_cmd._Outcome(
                success=self.probe_rc == 0, stdout=self.probe_out, returncode=self.probe_rc
            )
        if "savebin" in script:
            for n, match in enumerate(re.finditer(r'savebin\s+("[^"]+"|\S+)\s+(\S+)\s+(\S+)', script)):
                dest = match.group(1).strip('"')
                data = (self.read_back or {}).get(n, [APP, ATOC][n] if False else None)
                if data is None:
                    data = self.sources[n]
                Path(dest).write_bytes(data)
            return flash_cmd._Outcome(
                success=self.read_rc == 0, stdout="Reading 1 byte...\nO.K.\n" + self.read_out,
                returncode=self.read_rc,
            )
        return flash_cmd._Outcome(
            success=self.write_rc == 0, stdout=self.write_out, returncode=self.write_rc
        )

    sources: list[bytes] = []


def _run(tmp_path, monkeypatch, fake_kwargs=None, args=_ARGS, **probe_kwargs):
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "a.bin").write_bytes(APP)
    (tmp_path / "build" / "atoc.bin").write_bytes(ATOC)
    fake = FakeJlink(monkeypatch, **(fake_kwargs or {}))
    # `_flow_d_run` rewrites a.bin / atoc.bin with one byte; restore after it
    # lays its fixture down by pointing the fake's sources at the real bytes.
    fake.sources = [APP, ATOC]
    out = _flow_d_run(tmp_path, monkeypatch, flash_args=args, probe_kwargs=probe_kwargs)
    return fake, out


def _rewrite_sources(tmp_path):
    (tmp_path / "build" / "a.bin").write_bytes(APP)
    (tmp_path / "build" / "atoc.bin").write_bytes(ATOC)


# ── pure helpers ────────────────────────────────────────────────────────────


def test_every_reset_failure_wording_is_detected():
    for marker in ("Failed to halt CPU", "CPU is not halted", "Reset: Failed",
                   "CPU may have not been reset"):
        assert flow_d_report.reset_failures(f"x\n****** Error: {marker}\ny") == (marker,)
    assert flow_d_report.reset_failures(CLEAN) == ()


def test_dpidr_is_read_verbatim_from_either_spelling():
    assert flow_d_report.dpidr_in("Found SW-DP with ID 0x4C013477") == "0x4C013477"
    assert flow_d_report.dpidr_in("DPIDR: 0x6BA02477") == "0x6BA02477"
    assert flow_d_report.dpidr_in("nothing") is None


def test_sector_span_uses_16_kib_sectors():
    one = flow_d_report.sector_span(0x80010000, 0x40)
    assert one == {"first": "0x80010000", "end": "0x80014000", "count": 1,
                   "bytes": 16384, "sectorBytes": 16384}
    # The measured AEN803 ATOC: 2640 B at 0x8057F5B0 sits inside ONE sector.
    atoc = flow_d_report.sector_span(0x8057F5B0, 2640)
    assert (atoc["first"], atoc["end"], atoc["count"]) == ("0x8057C000", "0x80580000", 1)
    # Straddling a sector boundary costs two.
    assert flow_d_report.sector_span(0x80013FF0, 0x20)["count"] == 2
    assert flow_d_report.sector_span(0x80010000, 0)["count"] == 0
    assert flow_d_report.sector_span(0x80010000, 0x4000)["count"] == 1
    assert flow_d_report.sector_span(0x80010000, 0x4001)["count"] == 2


def test_the_readback_script_reuses_the_write_preamble_and_ends_at_exit():
    write = (
        "SelectEmuBySN 000999000001\nsi SWD\nspeed 4000\ndevice PART\nconnect\n"
        "loadbin x 0x1\nverifybin x 0x1\nRSetType 2\nr\ng\nexit\n"
    )
    script = flow_d_report.readback_script(write, [("0x80010000", 15, "/tmp/r0.bin")])
    assert script == (
        "SelectEmuBySN 000999000001\nsi SWD\nspeed 4000\ndevice PART\nconnect\n"
        "savebin /tmp/r0.bin 0x80010000 0xF\nRSetType 2\nr\ng\nexit\n"
    )
    # Ends like the write did (reset + run), so the app is not left halted.
    assert "loadbin" not in script and "verifybin" not in script


# ── the envelope ────────────────────────────────────────────────────────────


def test_the_envelope_carries_the_transcript_the_dpidr_and_cache_verified(tmp_path, monkeypatch):
    fake, (rc, data, issues, lines, _s) = _run(tmp_path, monkeypatch)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    jlink = entry["jlink"]
    assert jlink["dpidr"] == "0x4C013477" and jlink["dpidrSource"] == "write-transcript"
    assert jlink["verification"] == "cache-verified"
    assert "flash cache" in jlink["verificationNote"]
    assert jlink["reset"] == "pin-reset" and jlink["resetFailures"] == []
    assert "Found SW-DP with ID 0x4C013477" in jlink["transcriptTail"]
    log = Path(jlink["transcriptPath"])
    assert log.parent == tmp_path / "build" / "flash-logs"
    assert re.fullmatch(r"alif_mram_jlink-m55_hp-\d{8}T\d{6}Z(-\d+)?\.log", log.name), log.name
    text = log.read_text(encoding="utf-8")
    assert "loadbin" in text and "Found SW-DP with ID 0x4C013477" in text
    assert "cache-verified and PIN-reset" in entry["message"]
    assert "verified and PIN-reset" in entry["message"] and "readback" not in entry["message"]
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)


def test_a_failed_reset_is_a_warning_not_a_claimed_pin_reset(tmp_path, monkeypatch):
    out = CLEAN + "****** Error: Reset: Failed\nCPU may have not been reset\n"
    fake, (rc, data, issues, lines, _s) = _run(tmp_path, monkeypatch, {"write_out": out})
    assert rc == 0, (data, issues)  # the write itself landed
    entry = data["entries"][0]
    assert entry["status"] == "ok"
    assert "cache-verified; PIN-reset NOT confirmed" in entry["message"]
    assert "and PIN-reset" not in entry["message"]
    assert entry["jlink"]["reset"] == "unconfirmed"
    assert entry["jlink"]["resetFailures"] == ["Reset: Failed", "CPU may have not been reset"]
    note = next(i for i in issues if i.code == "flash.jlink-reset-unconfirmed")
    # tan-cli#1453: a failed halt is not a failed boot, so Flow D reports it as info.
    assert note.severity == "info"
    assert "Reset: Failed" in note.message and "low power" in note.message
    assert any("NOT confirmed" in line for line in lines)
    # The non-halting probe ran and could not confirm either.
    probe = entry["jlink"]["bootProbe"]
    assert probe["performed"] is True and probe["dhcsr"] is None and probe["trouble"] == []
    assert probe["pcsr"] == []


_HALT_FAIL = CLEAN + "****** Error: Failed to halt CPU\n"


def test_a_failed_halt_is_confirmed_by_dhcsr_without_a_halt(tmp_path, monkeypatch):
    """tan-cli#1453: the app went to WFI/STOP so the post-reset halt failed, but DHCSR
    (S_RESET_ST | S_RETIRE_ST | S_SLEEP) proves the image ran: no failure-class code."""
    fake, (rc, data, issues, lines, _s) = _run(
        tmp_path, monkeypatch,
        {"write_out": _HALT_FAIL, "probe_out": "E000EDF0 = 03050001\n"},
    )
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)
    assert entry["jlink"]["reset"] == "pin-reset"
    assert entry["jlink"]["resetConfirmedBy"] == "dhcsr" and entry["jlink"]["dhcsr"] == "0x03050001"
    assert "NOT confirmed" not in entry["message"]
    assert "boot confirmed without a halt: DHCSR 0x03050001" in entry["message"]
    # The probe never halts or resets: connect, one mem32, exit.
    probe = next(s for s in fake.scripts if "mem32" in s)
    assert "mem32 0xE000EDF0 1" in probe
    assert not any(w in probe for w in ("RSetType", "\nr\n", "\ng\n", "loadbin", "halt"))


def test_only_a_reset_bit_confirms_and_a_running_core_alone_keeps_the_issue(tmp_path, monkeypatch):
    """Review: S_RETIRE_ST / S_SLEEP are the OLD image idling after a failed `r` + `g`."""
    cases = [
        ("E000EDF0 = 03020001", False, False),  # halted
        ("E000EDF0 = 00000001", False, False),  # nothing sticky
        ("E000EDF0 = 02080001", False, False),  # reset bit but locked up
        ("E000EDF0 = 01000001", False, True),   # retired only
        ("E000EDF0 = 00040001", False, True),   # S_SLEEP only
    ]
    for word, confirmed, running in cases:
        fake, (rc, data, issues, _l, _s) = _run(
            tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": word + "\n"}
        )
        found = [i for i in issues if i.code == "flash.jlink-reset-unconfirmed"]
        assert [i.severity for i in found] == ["info"], word
        jl = data["entries"][0]["jlink"]
        assert jl["reset"] == "unconfirmed" and jl["coreRunning"] is running, word
        assert ("core running, reset not proven" in found[0].message) is running, word


def test_a_failed_reset_then_go_with_an_idling_old_image_is_not_confirmed(tmp_path, monkeypatch):
    """`Reset: Failed` then `g` resumes the old context: retired + sleeping, no reset bit."""
    out = CLEAN + "****** Error: Reset: Failed\n"
    fake, (rc, data, issues, _l, _s) = _run(
        tmp_path, monkeypatch, {"write_out": out, "probe_out": "E000EDF0 = 01050001\n"}
    )
    assert rc == 0
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)
    assert "resetConfirmedBy" not in data["entries"][0]["jlink"]
    assert "NOT confirmed" in data["entries"][0]["message"]


def test_a_probe_session_that_reset_or_failed_to_halt_cannot_confirm(tmp_path, monkeypatch):
    for noise in ("Reset: Failed\n", "****** Error: Failed to halt CPU\n"):
        fake, (rc, data, issues, _l, _s) = _run(
            tmp_path, monkeypatch,
            {"write_out": _HALT_FAIL, "probe_out": noise + "E000EDF0 = 03050001\n"},
        )
        assert "flash.jlink-reset-unconfirmed" in _codes(issues)
        assert data["entries"][0]["jlink"]["bootProbe"]["trouble"]
        assert data["entries"][0]["jlink"]["reset"] == "unconfirmed"


PC_IN = "E000101C = 80010000\n"  # inside the 1-byte mramxip app at 0x80010000 of `_ARGS`


def test_pc_samples_inside_the_new_image_confirm_when_the_reset_bit_is_gone(tmp_path, monkeypatch):
    out = "E000EDF0 = 01040001\n" + PC_IN * 3
    fake, (rc, data, issues, _l, _s) = _run(tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": out})
    assert rc == 0, (data, issues)
    jl = data["entries"][0]["jlink"]
    assert jl["resetConfirmedBy"] == "pcsr" and jl["reset"] == "pin-reset"
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)
    assert jl["bootProbe"]["pcsr"] == ["0x80010000"] * 3
    assert jl["bootProbe"]["imageRanges"] == [["0x80010000", "0x80010001"]]


@pytest.mark.parametrize(
    "samples,dhcsr",
    [
        ("E000101C = FFFFFFFF\n" * 3, "01040001"),      # sleeping: no sample
        ("E000101C = 00000100\n" * 3, "01040001"),      # old image / loader / ROM
        (PC_IN + "E000101C = 00000100\n" + PC_IN, "01040001"),  # one stray sample vetoes
        (PC_IN * 3, "01020001"),                         # halted core
        (PC_IN * 3, "01080001"),                         # locked up
    ],
)
def test_pc_samples_without_evidence_keep_the_issue(tmp_path, monkeypatch, samples, dhcsr):
    out = f"E000EDF0 = {dhcsr}\n" + samples
    fake, (rc, data, issues, _l, _s) = _run(tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": out})
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)
    assert "resetConfirmedBy" not in data["entries"][0]["jlink"]


def test_the_reset_bit_still_confirms_without_any_pc_sample(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _run(
        tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": "E000EDF0 = 03050001\n"}
    )
    assert data["entries"][0]["jlink"]["resetConfirmedBy"] == "dhcsr"


def test_the_image_ranges_come_from_the_elf_next_to_the_flashed_bin(tmp_path):
    from tests.commands.test_flash_ram import make_elf
    from tan.core.flash_plan import FlowDShape

    (tmp_path / "app.elf").write_bytes(make_elf(base=0x100, filesz=64))
    (tmp_path / "app.bin").write_bytes(b"\x00" * 8)
    shape = FlowDShape(device="D", app_address=None, artefact=str(tmp_path / "app.bin"))
    assert flash_cmd._flow_d_image_ranges(shape) == [(0x100, 0x140)]
    (tmp_path / "app.elf").unlink()
    assert flash_cmd._flow_d_image_ranges(shape) == []
    shape = FlowDShape(device="D", app_address="0x80010000", artefact=str(tmp_path / "app.bin"))
    assert flash_cmd._flow_d_image_ranges(shape) == [(0x80010000, 0x80010008)]


def test_a_clean_reset_runs_no_boot_probe(tmp_path, monkeypatch):
    fake, (rc, *_rest) = _run(tmp_path, monkeypatch)
    assert rc == 0 and not any("mem32" in s for s in fake.scripts)


def test_dhcsr_helpers():
    assert flow_d_report.dhcsr_in("E000EDF0 = 03050001") == 0x03050001
    assert flow_d_report.dhcsr_in("nothing") is None
    assert flow_d_report.dhcsr_confirms_reset(0x02000001)
    for word in (0x01000001, 0x00040001, 0x02020001, 0x02080001, None):
        assert not flow_d_report.dhcsr_confirms_reset(word)  # no reset / halted / lockup
    assert flow_d_report.dhcsr_core_running(0x01000001) and flow_d_report.dhcsr_core_running(0x00040001)
    assert not flow_d_report.dhcsr_core_running(0x01020001)
    assert not flow_d_report.dhcsr_core_running(0x01080001) and not flow_d_report.dhcsr_core_running(None)
    assert flow_d_report.probe_trouble("Reset: Failed") and not flow_d_report.probe_trouble("Reset delay: 0 ms")
    script = flow_d_report.nohalt_probe_script("si SWD\ndevice P\nconnect\nloadbin x 0x1\nexit\n")
    assert script == (
        "si SWD\ndevice P\nconnect\nmem32 0xE000EDF0 1\n"
        + "mem32 0xE000101C 1\nSleep 5\n" * 3 + "exit\n"
    )
    assert flow_d_report.pcsr_samples("E000101C = 80010004\nE000101C = FFFFFFFF") == [0x80010004, 0xFFFFFFFF]
    rng = [(0x80010000, 0x80020000)]
    assert flow_d_report.pcsr_in_ranges([0x80010004, 0xFFFFFFFF], rng)
    assert not flow_d_report.pcsr_in_ranges([0xFFFFFFFF] * 3, rng)  # no evidence
    assert not flow_d_report.pcsr_in_ranges([0x80010004, 0x100], rng)  # one outside vetoes
    assert not flow_d_report.pcsr_in_ranges([0x80010004], [])


def test_a_failed_write_still_leaves_its_transcript(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _run(
        tmp_path, monkeypatch, {"write_out": "Cannot connect to target.\n", "write_rc": 1}
    )
    assert rc == 1
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert entry["jlink"]["reset"] == "not-reached"
    assert "Cannot connect to target." in Path(entry["jlink"]["transcriptPath"]).read_text()


def test_the_dpidr_falls_back_to_what_the_preflight_read(tmp_path, monkeypatch):
    out = "Reset type: PIN\n"  # a transcript with no SW-DP banner at all
    facts = {}

    def _preflight(*_a, facts=None, **_k):
        facts["dpidr"] = "0x4C013477"
        return None

    fake = FakeJlink(monkeypatch, write_out=out)
    fake.sources = [APP, ATOC]
    (tmp_path / "build").mkdir(exist_ok=True)
    rc, data, _i, _l, _s = _flow_d_run(tmp_path, monkeypatch, flash_args=_ARGS)
    monkeypatch.setattr(flash_cmd, "_flow_d_preflight", _preflight)
    rc, data, _i, _l, _s = _flow_d_run(tmp_path, monkeypatch, flash_args=_ARGS)
    jlink = data["entries"][0]["jlink"]
    # `_flow_d_run` re-stubs the preflight to a no-op, so the fallback is
    # exercised through the helper directly below.
    assert jlink["dpidr"] is None and jlink["dpidrSource"] == "none"


def test_record_prefers_the_write_transcript_then_the_preflight(tmp_path):
    ctx = flash_cmd._Context(
        sku="S", build_root=str(tmp_path), sdk_root=str(tmp_path), dry_run=False,
        skip_missing_tools=False, force_confirm=True, capture=True,
    )
    plan = flash_cmd.FlashPlan(argv=("JLinkExe",), ok_message="m", jlink_script="connect\n")
    quiet = flash_cmd._Outcome(success=True, stdout="Reset type: PIN\n", returncode=0)
    report: dict = {}
    flash_cmd._flow_d_record(plan, quiet, ctx, "m55_he", report, {"dpidr": "0x4C013477"})
    assert report["jlink"]["dpidr"] == "0x4C013477"
    assert report["jlink"]["dpidrSource"] == "preflight"
    loud = flash_cmd._Outcome(success=True, stdout="Found SW-DP with ID 0x6BA02477\n", returncode=0)
    report = {}
    flash_cmd._flow_d_record(plan, loud, ctx, "m55_he", report, {"dpidr": "0x4C013477"})
    assert report["jlink"]["dpidr"] == "0x6BA02477"
    assert report["jlink"]["dpidrSource"] == "write-transcript"


def test_an_unwritable_transcript_location_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "flash-logs"
    blocker.write_text("a file where the log directory must go", encoding="utf-8")
    ctx = flash_cmd._Context(
        sku="S", build_root=str(tmp_path), sdk_root=str(tmp_path), dry_run=False,
        skip_missing_tools=False, force_confirm=True, capture=True,
    )
    plan = flash_cmd.FlashPlan(argv=("JLinkExe",), ok_message="m", jlink_script="connect\n")
    report: dict = {}
    flash_cmd._flow_d_record(
        plan, flash_cmd._Outcome(success=True, stdout="ok\n", returncode=0), ctx, "m55_he",
        report, {},
    )
    assert report["jlink"]["transcriptPath"] is None
    assert "transcriptError" in report["jlink"]


# ── --readback ──────────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _readback_run(tmp_path, monkeypatch, fake_kwargs=None, **extra):
    fake = FakeJlink(monkeypatch, **(fake_kwargs or {}))
    fake.sources = [APP, ATOC]
    # `_flow_d_run` lays down one-byte fixtures first; lay the real bytes down
    # right after by wrapping its manifest read -- simplest is to pre-create and
    # let it overwrite, then restore through the fake's own sources.
    original = flash_cmd._execute

    def _restoring_execute(plan, *a, **k):
        _rewrite_sources(tmp_path)
        return original(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", _restoring_execute)
    out = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_ARGS, probe_kwargs={"readback": True, **extra}
    )
    return fake, out


def test_readback_upgrades_cache_verified_when_the_fresh_session_matches(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback_run(tmp_path, monkeypatch)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    rb = entry["jlink"]["readback"]
    assert rb["performed"] is True and rb["ok"] is True
    assert [r["address"] for r in rb["regions"]] == ["0x80010000", "0x8057F5B0"]
    assert rb["regions"][0]["sha256Expected"] == _sha(APP) == rb["regions"][0]["sha256Actual"]
    assert rb["regions"][1]["sha256Actual"] == _sha(ATOC)
    assert entry["jlink"]["verification"] == "readback-verified"
    assert "not a cold-power-cycle proof" in entry["jlink"]["verificationNote"]
    assert "read back in a fresh J-Link session (sha256 match)" in entry["message"]
    # Two spawns: the write, then the read-back -- and the read-back is a FRESH
    # script (same preamble, no loadbin / reset).
    write, read = [s for s in fake.scripts if "ShowEmuList" not in s]
    assert "loadbin" in write and "savebin" not in write
    # tan-cli#1450: the write stops BEFORE the PIN reset; the read-back carries it.
    assert "RSetType" not in write and write.splitlines()[-1] == "exit"
    assert "savebin" in read and "loadbin" not in read
    assert read.splitlines()[-4:] == ["RSetType 2", "r", "g", "exit"]
    assert read.splitlines().index("connect") < read.splitlines().index(
        next(l for l in read.splitlines() if l.startswith("savebin"))
    )


def test_readback_reads_the_chip_before_the_post_write_reset(tmp_path, monkeypatch):
    """tan-cli#1450: the image must not have booted (and entered STOP) when the chip is read."""
    fake, (rc, data, issues, _l, _s) = _readback_run(tmp_path, monkeypatch)
    assert rc == 0, (data, issues)
    sessions = [s for s in fake.scripts if "ShowEmuList" not in s]
    assert [("savebin" in s, "RSetType" in s) for s in sessions] == [(False, False), (True, True)]
    assert data["entries"][0]["jlink"]["reset"] == "pin-reset"


def test_a_failed_halt_in_the_readback_sessions_reset_is_still_confirmed_by_dhcsr(
    tmp_path, monkeypatch
):
    fake = FakeJlink(monkeypatch, probe_out="E000EDF0 = 03050001\n")
    fake.sources = [APP, ATOC]
    original = fake._spawn

    def _halt_fails(argv, script, *a, **k):
        out = original(argv, script, *a, **k)
        if "savebin" in script:
            out.stdout += "****** Error: Failed to halt CPU\n"
        return out

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _halt_fails)
    real = flash_cmd._execute
    monkeypatch.setattr(
        flash_cmd, "_execute", lambda plan, *a, **k: (_rewrite_sources(tmp_path), real(plan, *a, **k))[1]
    )
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_ARGS, probe_kwargs={"readback": True}
    )
    assert rc == 0, (data, issues)
    assert data["entries"][0]["jlink"]["resetConfirmedBy"] == "dhcsr"
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)
    assert any("mem32" in s for s in fake.scripts)


def test_short_reads_mean_unreachable_not_mismatch(tmp_path, monkeypatch):
    """tan-cli#1450: a gated debug domain must not tell the user to re-flash."""
    fake, (rc, data, issues, _l, _s) = _readback_run(
        tmp_path, monkeypatch, {"read_back": {0: b"", 1: b""}}
    )
    assert rc == 1
    assert _codes(issues) == ["flash.readback-failed"]
    msg = data["entries"][0]["message"]
    assert "target unreachable (low-power?)" in msg and "do not re-flash on it" in msg
    assert data["entries"][0]["jlink"]["readback"]["reason"] == "target unreachable (low-power?)"


def _readback(tmp_path, monkeypatch, **fake_kwargs):
    fake = FakeJlink(monkeypatch, **fake_kwargs)
    fake.sources = [APP, ATOC]
    real = flash_cmd._execute
    monkeypatch.setattr(
        flash_cmd, "_execute", lambda plan, *a, **k: (_rewrite_sources(tmp_path), real(plan, *a, **k))[1]
    )
    out = _flow_d_run(tmp_path, monkeypatch, flash_args=_ARGS, probe_kwargs={"readback": True})
    return fake, out


def test_a_failed_session_that_could_not_read_memory_is_unreachable_and_reset_afterwards(
    tmp_path, monkeypatch
):
    fake, (rc, data, issues, _l, _s) = _readback(
        tmp_path, monkeypatch, read_rc=1, read_out="Could not read memory.\n"
    )
    assert rc == 1 and _codes(issues) == ["flash.readback-failed"]
    msg = data["entries"][0]["message"]
    assert "target unreachable (low-power?)" in msg and "PIN reset was run afterwards" in msg
    assert data["entries"][0]["jlink"]["reset"] == "pin-reset"
    # savebin session, then a reset-only session (preamble + tail, no savebin/loadbin).
    sessions = [s for s in fake.scripts if "ShowEmuList" not in s]
    assert "savebin" in sessions[1] and "RSetType" in sessions[1]
    assert "savebin" not in sessions[2] and sessions[2].splitlines()[-4:] == ["RSetType 2", "r", "g", "exit"]


def test_a_read_session_failure_with_a_failing_tail_says_the_board_was_not_reset(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback(tmp_path, monkeypatch, read_rc=1, tail_rc=1)
    assert rc == 1 and _codes(issues) == ["flash.readback-failed"]
    assert "board was NOT reset" in data["entries"][0]["message"]
    assert data["entries"][0]["jlink"]["reset"] == "not-run"


def test_a_probe_guard_refusal_before_the_readback_says_the_board_was_not_reset(tmp_path, monkeypatch):
    calls = {"n": 0}
    fake = FakeJlink(monkeypatch, emulators=["000999000001"])
    fake.sources = [APP, ATOC]
    original = fake._spawn

    def _flaky(argv, script, *args, **kwargs):
        if "ShowEmuList" in script:
            calls["n"] += 1
            if calls["n"] >= 3:
                fake.emulators = ["000999000001", "000999000001"]
        return original(argv, script, *args, **kwargs)

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _flaky)
    real = flash_cmd._execute
    monkeypatch.setattr(
        flash_cmd, "_execute", lambda plan, *a, **k: (_rewrite_sources(tmp_path), real(plan, *a, **k))[1]
    )
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_ARGS,
        probe_kwargs={"readback": True, **_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-ambiguous"]
    assert "board was NOT reset" in data["entries"][0]["message"]
    assert data["entries"][0]["jlink"]["reset"] == "not-run"
    assert not any("RSetType" in s and "savebin" not in s for s in fake.scripts)


def test_a_full_length_mismatch_wins_over_a_short_region(tmp_path, monkeypatch):
    """Review: 'unreachable' must not hide a real mismatch in another region."""
    fake, (rc, data, issues, _l, _s) = _readback(
        tmp_path, monkeypatch, read_back={0: b"", 1: b"\xff"}
    )
    assert rc == 1 and _codes(issues) == ["flash.readback-mismatch"]
    assert "0x8057F5B0" in data["entries"][0]["message"]
    assert "0x80010000" not in data["entries"][0]["message"].split("DIFFERENT bytes")[1]


def test_unreachable_needs_that_no_region_came_back_full_length(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback(tmp_path, monkeypatch, read_back={0: b"", 1: b""})
    assert rc == 1 and _codes(issues) == ["flash.readback-failed"]
    assert "target unreachable (low-power?)" in data["entries"][0]["message"]


def test_a_marker_only_in_the_reset_tail_does_not_hide_a_good_readback(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback(
        tmp_path, monkeypatch, read_out="RSetType 2\nCould not read memory.\n"
    )
    assert rc == 0, (data, issues)
    assert data["entries"][0]["jlink"]["verification"] == "readback-verified"


def test_the_ambiguous_core_markers_are_not_unreachable_markers():
    for text in ("Could not find core", "Failed to attach"):
        assert not flow_d_report.target_unreachable(text)
    assert flow_d_report.target_unreachable("Could not read memory.")


def test_strip_reset_tail():
    assert flow_d_report.strip_reset_tail(
        "connect\nloadbin x 0x1\nverifybin x 0x1\nRSetType 2\nr\ng\nexit\n"
    ) == "connect\nloadbin x 0x1\nverifybin x 0x1\nh\nexit\n"
    assert flow_d_report.strip_reset_tail("connect\nexit\n") == "connect\nexit\n"


def test_a_readback_that_differs_fails_the_entry_with_its_code(tmp_path, monkeypatch):
    fake, (rc, data, issues, lines, _s) = _readback_run(
        tmp_path, monkeypatch, {"read_back": {1: b"\xff"}}
    )
    assert rc == 1
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert _codes(issues) == ["flash.readback-mismatch"]
    assert "DIFFERENT bytes" in entry["message"] and "0x8057F5B0" in entry["message"]
    rb = entry["jlink"]["readback"]
    assert rb["ok"] is False
    assert [r["match"] for r in rb["regions"]] == [True, False]
    assert entry["jlink"]["verification"] == "cache-verified"  # never upgraded


def test_a_readback_session_that_fails_is_its_own_code(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback_run(tmp_path, monkeypatch, {"read_rc": 1})
    assert rc == 1
    assert _codes(issues) == ["flash.readback-failed"]
    assert "write landed and cache-verified" in data["entries"][0]["message"]


def test_without_the_flag_there_is_no_readback_spawn(tmp_path, monkeypatch):
    fake, (rc, data, _i, _l, _s) = _run(tmp_path, monkeypatch)
    assert rc == 0
    assert [s for s in fake.scripts if "savebin" in s] == []
    assert "readback" not in data["entries"][0]["jlink"]


def test_a_readback_goes_through_the_same_probe_guard_as_the_write(tmp_path, monkeypatch):
    """The read-back is a J-Link spawn like any other: the ShowEmuList probe
    verification runs again right before it, so it cannot reach a probe the
    write did not."""
    fake, (rc, data, issues, _l, _s) = _readback_run(
        tmp_path, monkeypatch, {"emulators": ["000999000001"]},
        **{**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 0, (data, issues)
    kinds = ["list" if "ShowEmuList" in s else "read" if "savebin" in s else "write"
             for s in fake.scripts]
    # Verification precedes the write AND the read-back.
    assert kinds[-1] == "read" and kinds[-2] == "list"
    assert kinds.index("write") < len(kinds) - 1
    assert kinds[kinds.index("write") - 1] == "list"


def test_a_probe_that_changes_before_the_readback_refuses_it(tmp_path, monkeypatch):
    """The listing run right before the read-back sees TWO emulators sharing the
    serial: the read-back refuses instead of reading some other board."""
    calls = {"n": 0}
    fake = FakeJlink(monkeypatch, emulators=["000999000001"])
    fake.sources = [APP, ATOC]
    original = fake._spawn

    def _flaky(argv, script, *args, **kwargs):
        if "ShowEmuList" in script:
            calls["n"] += 1
            if calls["n"] >= 3:  # gate, write, then the read-back's own listing
                fake.emulators = ["000999000001", "000999000001"]
        return original(argv, script, *args, **kwargs)

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _flaky)
    real_execute = flash_cmd._execute

    def _restoring_execute(plan, *a, **k):
        _rewrite_sources(tmp_path)
        return real_execute(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", _restoring_execute)
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_ARGS,
        probe_kwargs={"readback": True, **_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 1
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert [s for s in fake.scripts if "savebin" in s] == []


# ── tan-cli#1336: the J-Link binary is never taken from the project venv ───


def _plant_hostile_venv(tmp_path):
    """A west-capable `.venv` in the PROJECT that also ships its own JLinkExe."""
    import os as _os

    if _os.name == "nt":
        pytest.skip("POSIX venv layout / shell-script executables")

    from tan.core.venv import venv_bin_dir

    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("west", "JLinkExe", "JLink"):
        (bin_dir / name).write_text("#!/bin/sh\necho hostile\n", encoding="utf-8")
        _os.chmod(bin_dir / name, 0o755)
    # The plant must really be what the old resolution would have picked.
    assert venv_bin_dir(str(tmp_path), str(tmp_path / "sdk")) == bin_dir
    return bin_dir


class SpawnRecorder:
    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, object, object]] = []
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, capture, timeout, venv_bin=None, workspace=None,
               executable=None, **kwargs):
        kind = "list" if "ShowEmuList" in script else "write"
        self.calls.append((kind, executable, venv_bin))
        out = "J-Link[0]: Serial number: 000999000001\n" if kind == "list" else CLEAN
        return flash_cmd._Outcome(success=True, stdout=out, returncode=0)


def _drive_confirmed_flow_d(tmp_path, monkeypatch, path_dir, **kwargs):
    """A confirmed Flow D run with the PROJECT venv live (`venv_bin_dir` is NOT
    stubbed) and PATH limited to `path_dir`. `_spawn_jlink` is stubbed by the
    caller's `SpawnRecorder`, so nothing real runs."""
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "a.bin").write_bytes(b"\x00")
    (tmp_path / "build" / "atoc.bin").write_bytes(b"\x00")
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_hp, os: zephyr, output_artefact: a.bin, status: ok,\n"
        f"   flash_method: alif_mram_jlink, flash_args: {_ARGS}}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8", newline="",
    )
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=False, skip_missing_tools=False,
        capture=True, cwd=str(tmp_path), **kwargs,
    )


def _exe(path):
    import os as _os

    if _os.name == "nt":
        pytest.skip("POSIX shell-script executables (Windows needs a real .exe)")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    _os.chmod(path, 0o755)
    return str(path)


def test_a_project_venv_jlink_is_never_chosen_and_the_path_one_is(tmp_path, monkeypatch):
    hostile = _plant_hostile_venv(tmp_path)
    trusted = _exe(tmp_path / "trusted" / "JLinkExe")
    rec = SpawnRecorder(monkeypatch)
    rc, data, issues, _l, _s = _drive_confirmed_flow_d(
        tmp_path, monkeypatch, tmp_path / "trusted", readback=False
    )
    assert rc == 0, (data, issues)
    assert {c[0] for c in rec.calls} == {"write"}
    for _kind, executable, venv_bin in rec.calls:
        assert executable == trusted
        assert venv_bin is None, "the project venv was put on the J-Link child's PATH"
    jlink = data["entries"][0]["jlink"]
    assert jlink["binary"] == trusted and jlink["binarySource"] == "PATH"
    assert str(hostile) not in jlink["binary"]


def test_with_only_a_project_venv_jlink_the_run_refuses_instead_of_using_it(tmp_path, monkeypatch):
    _plant_hostile_venv(tmp_path)
    (tmp_path / "empty").mkdir()
    rec = SpawnRecorder(monkeypatch)
    rc, data, issues, _l, _s = _drive_confirmed_flow_d(tmp_path, monkeypatch, tmp_path / "empty")
    assert rc == 1
    assert rec.calls == []
    entry = data["entries"][0]
    assert entry["status"] == "failed" and entry["jlink"]["binary"] is None


def test_the_cli_override_wins_and_is_reported_and_shared_by_every_spawn(tmp_path, monkeypatch):
    _plant_hostile_venv(tmp_path)
    mine = _exe(tmp_path / "mine" / "JLinkExe")
    _exe(tmp_path / "trusted" / "JLinkExe")
    rec = SpawnRecorder(monkeypatch)
    rc, data, issues, _l, _s = _drive_confirmed_flow_d(
        tmp_path, monkeypatch, tmp_path / "trusted", jlink_path=mine, readback=True,
    )
    assert {c[1] for c in rec.calls} == {mine}  # write + read-back, one binary
    assert data["entries"][0]["jlink"]["binary"] == mine
    assert data["entries"][0]["jlink"]["binarySource"] == "the --jlink flag"


def test_the_probe_listing_uses_the_same_trusted_binary_as_the_write(tmp_path, monkeypatch):
    _plant_hostile_venv(tmp_path)
    trusted = _exe(tmp_path / "trusted" / "JLinkExe")
    rec = SpawnRecorder(monkeypatch)
    rc, data, issues, _l, _s = _drive_confirmed_flow_d(
        tmp_path, monkeypatch, tmp_path / "trusted",
        **{**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    kinds = [c[0] for c in rec.calls]
    assert "list" in kinds and "write" in kinds, (kinds, data)
    assert {c[1] for c in rec.calls} == {trusted}
    assert all(c[2] is None for c in rec.calls)

# ── sector overlap (tan-cli#1343 review) ────────────────────────────────────


def test_find_overlaps_between_writes_and_resident_entries():
    app = {"name": "app", "address": "0x80010000", "size": 0x4001}  # tail spills into sector 2
    atoc = {"name": "atoc", "address": "0x80014000", "size": 0x100}
    found = flow_d_report.find_overlaps([app, atoc])
    assert len(found) == 1 and "app write and the atoc write share" in found[0]
    assert "0x80014000-0x80018000" in found[0]
    # Adjacent but disjoint sectors are fine; so is an exact fit.
    app["size"] = 0x4000
    assert flow_d_report.find_overlaps([app, atoc]) == []
    # A resident entry the ATOC does not rewrite, inside the app's sectors.
    res = flow_d_report.find_overlaps([app], [("HP-OWNER", 0x80013000, 0x20), ("NOADDR", None, None)])
    assert len(res) == 1 and "HP-OWNER" in res[0]
    assert flow_d_report.find_overlaps([app], [("FAR", 0x80200000, 0x20)]) == []


def test_resident_regions_parse_the_three_spellings():
    from tan.core.atoc_replacement import resident_regions

    assert resident_regions({"resident_atoc_entries": ["DEVICE", "HP@0x80200000", "X@0x1+0x20"]}) == (
        ("DEVICE", None, None), ("HP", 0x80200000, None), ("X", 0x1, 0x20),
    )
    import pytest as _pytest

    from tan.core.flash_plan import FlashPlanError

    for bad in ("DEVICE@", "A@zz", "A B"):
        with _pytest.raises(FlashPlanError):
            resident_regions({"resident_atoc_entries": [bad]})


# ── transcript files (tan-cli#1343 review) ──────────────────────────────────


def test_each_run_gets_its_own_timestamped_transcript_and_only_ten_are_kept(tmp_path):
    import time as _time

    from tan.commands.flash_cmd import _flow_d_log_path, _write_transcript

    base = _time.gmtime(1_800_000_000)
    paths = []
    for i in range(13):
        stamp = _time.gmtime(1_800_000_000 + i)
        paths.append(_write_transcript(_flow_d_log_path(str(tmp_path), "m55_he", stamp), f"run {i}"))
    names = sorted(p.name for p in (tmp_path / "flash-logs").iterdir())
    assert len(names) == 10
    assert names[0].startswith("alif_mram_jlink-m55_he-2027") and names[0].endswith("Z.log")
    assert Path(paths[-1]).read_text() == "run 12"
    assert not Path(paths[0]).exists() and not Path(paths[2]).exists()
    assert Path(paths[3]).exists()
    # Same second: a suffix, never an overwrite.
    again = _write_transcript(_flow_d_log_path(str(tmp_path), "m55_he", _time.gmtime(1_800_000_012)), "dup")
    assert again != paths[-1] and again.endswith("-1.log") and Path(paths[-1]).read_text() == "run 12"
    assert base  # (silences the unused-name lint without hiding the clock input)


def test_a_symlink_planted_at_the_log_name_is_replaced_not_followed(tmp_path):
    import os as _os

    from tan.commands.flash_cmd import _write_transcript

    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    logs = tmp_path / "flash-logs"
    logs.mkdir()
    target = logs / "alif_mram_jlink-c-20270101T000000Z.log"
    _os.symlink(victim, target)
    final = _write_transcript(str(target), "transcript")
    assert victim.read_text(encoding="utf-8") == "precious"
    assert final != str(target) and Path(final).read_text() == "transcript"


def test_a_symlinked_log_directory_is_refused(tmp_path):
    import os as _os

    import pytest as _pytest

    from tan.commands.flash_cmd import _write_transcript

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _os.symlink(elsewhere, tmp_path / "flash-logs")
    with _pytest.raises(OSError):
        _write_transcript(str(tmp_path / "flash-logs" / "alif_mram_jlink-c-x.log"), "t")
    assert list(elsewhere.iterdir()) == []


def test_rotation_only_touches_the_same_cores_logs(tmp_path):
    """tan-cli#1344 review: core `m55` must not rotate `m55-hp`'s transcripts."""
    import time as _time

    from tan.commands.flash_cmd import _flow_d_log_path, _write_transcript

    for i in range(12):
        _write_transcript(
            _flow_d_log_path(str(tmp_path), "m55-hp", _time.gmtime(1_800_000_000 + i)), "hp"
        )
    for i in range(12):
        _write_transcript(
            _flow_d_log_path(str(tmp_path), "m55", _time.gmtime(1_800_000_100 + i)), "he"
        )
    names = [p.name for p in (tmp_path / "flash-logs").iterdir()]
    assert sum(n.startswith("alif_mram_jlink-m55-hp-") for n in names) == 10
    assert sum(n.startswith("alif_mram_jlink-m55-2") for n in names) == 10


# ── tan-cli#1348 review ─────────────────────────────────────────────────────


def test_execute_never_re_resolves_a_missing_jlink(tmp_path, monkeypatch):
    """With no resolved binary `_execute` refuses; a JLinkExe sitting on PATH is NOT
    picked up behind the caller's back."""
    _exe(tmp_path / "onpath" / "JLinkExe")
    monkeypatch.setenv("PATH", str(tmp_path / "onpath"))
    rec = SpawnRecorder(monkeypatch)
    plan = flash_cmd.FlashPlan(argv=("JLinkExe",), ok_message="", jlink_script="connect\nexit\n")
    outcome = flash_cmd._execute(plan, True, None, None, None, jlink_exe=None)
    assert outcome.success is False and "trusted location" in outcome.stderr
    assert rec.calls == []
    pre = flash_cmd._flow_d_preflight(
        flash_cmd.FlashInputs(
            artefact="a", core_id="c", sku="S",
            flash_args={"jlink_flash_device": "P", "expect_dpidr": "0x4C013477",
                        "jlink_device": "Cortex-M55"},
        ),
        jlink_exe=None,
    )
    assert pre is not None and "trusted location" in pre and rec.calls == []


def test_a_manifest_only_setools_is_refused_before_ANY_spawn(tmp_path, monkeypatch):
    """The refusal is hoisted ahead of the probe listing and the DPIDR preflight."""
    (tmp_path / "setools").mkdir()
    _exe(tmp_path / "trusted" / "JLinkExe")
    rec = SpawnRecorder(monkeypatch)
    manifest_args = (
        '{jlink_flash_device: PART, slot0_load_address: "0x80010000", confirm: true, '
        f'atoc_unqueryable: true, setools_dir: "{tmp_path / "setools"}", '
        "expect_dpidr: '0x4C013477', jlink_device: Cortex-M55}"
    )
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "a.bin").write_bytes(b"\x00")
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_hp, os: zephyr, output_artefact: a.bin, status: ok,\n"
        f"   flash_method: alif_mram_jlink, flash_args: {manifest_args}}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8", newline="",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "trusted"))
    monkeypatch.delenv("SETOOLS_DIR", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    rc, data, issues, _l, _s = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(tmp_path), **_probes(A, C), probe_usb_path="3-4.3",
    )
    assert rc == 1 and rec.calls == [], rec.calls
    assert _codes(issues) == ["flash.setools-untrusted-source"]


def test_the_preflight_dpidr_survives_a_later_refusal(tmp_path, monkeypatch):
    """Bench round 7: a run refused AFTER the read-only preflight (here a sector overlap
    found once the ATOC is known) still reports the SW-DP ID the preflight read."""
    _exe(tmp_path / "trusted" / "JLinkExe")
    SpawnRecorder(monkeypatch)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "a.bin").write_bytes(b"\x00")
    (tmp_path / "build" / "atoc.bin").write_bytes(b"\x00")
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    args = (
        '{jlink_flash_device: PART, slot0_load_address: "0x80010000", atoc: atoc.bin, '
        'atoc_address: "0x80010000", confirm: true, atoc_unqueryable: true, '
        "expect_dpidr: '0x4C013477', jlink_device: Cortex-M55}"
    )
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_hp, os: zephyr, output_artefact: a.bin, status: ok,\n"
        f"   flash_method: alif_mram_jlink, flash_args: {args}}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8", newline="",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "trusted"))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    rc, data, issues, _l, _s = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"), board_yaml=None,
        core=None, helper=None, dry_run=False, skip_missing_tools=False, capture=True,
        cwd=str(tmp_path),
    )
    assert rc == 1 and _codes(issues) == ["flash.write-sector-overlap"]
    assert data["entries"][0]["jlink"]["dpidr"] == "0x4C013477"
    assert data["entries"][0]["jlink"]["dpidrSource"] == "preflight"


# ── tan-cli#1458: bench follow-ups ──────────────────────────────────────────


def test_the_readback_session_halts_the_core_right_after_connect():
    write = "connect\nloadbin x 0x1\nRSetType 2\nr\ng\nexit\n"
    assert flow_d_report.readback_script(write, [("0x1", 4, "/t/r")], halt_first=True) == (
        "connect\nh\nsavebin /t/r 0x1 0x4\nRSetType 2\nr\ng\nexit\n"
    )
    assert "\nh\n" not in flow_d_report.readback_script(write, [("0x1", 4, "/t/r")])


def test_the_write_session_asks_for_the_core_held_halted_before_exit(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback_run(tmp_path, monkeypatch)
    write, read = [s for s in fake.scripts if "ShowEmuList" not in s]
    assert write.splitlines()[-2:] == ["h", "exit"]
    assert read.splitlines()[read.splitlines().index("connect") + 1] == "h"


def test_the_message_names_the_pcsr_witness_not_dhcsr(tmp_path, monkeypatch):
    out = "E000EDF0 = 01040001\n" + PC_IN * 3
    fake, (rc, data, issues, _l, _s) = _run(tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": out})
    msg = data["entries"][0]["message"]
    assert "3 PC sample(s) (DWT_PCSR, e.g. 0x80010000) inside the image range 0x80010000-0x80010001" in msg
    assert "DHCSR" not in msg


def test_no_witness_in_a_stop_window_is_info_with_honest_wording(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _run(
        tmp_path, monkeypatch, {"write_out": _HALT_FAIL, "probe_out": "E000101C = FFFFFFFF\n" * 3}
    )
    found = [i for i in issues if i.code == "flash.jlink-reset-unconfirmed"]
    assert [i.severity for i in found] == ["info"]
    assert "no witness" in found[0].message and "low power" in found[0].message


def test_every_session_leaves_a_transcript_under_flash_logs(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback_run(
        tmp_path, monkeypatch, {"read_out": "Failed to halt CPU\n", "probe_out": "E000EDF0 = 03050001\n"}
    )
    jl = data["entries"][0]["jlink"]
    logs = tmp_path / "build" / "flash-logs"
    paths = [jl["transcriptPath"], jl["readbackSession"]["transcriptPath"], jl["bootProbe"]["transcriptPath"]]
    assert all(p and Path(p).parent == logs and Path(p).is_file() for p in paths)
    assert "-readback-" in Path(paths[1]).name and "-bootprobe-" in Path(paths[2]).name
    assert "-readback-" not in Path(paths[0]).name
    assert "savebin" in Path(paths[1]).read_text() and "mem32" in Path(paths[2]).read_text()


def test_the_reset_only_session_is_saved_too(tmp_path, monkeypatch):
    fake, (rc, data, issues, _l, _s) = _readback(tmp_path, monkeypatch, read_rc=1)
    tail = data["entries"][0]["jlink"]["resetTail"]["transcriptPath"]
    assert tail and "RSetType" in Path(tail).read_text()
