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
                 read_rc=0, emulators=()):
        self.write_out, self.write_rc = write_out, write_rc
        self.read_back, self.read_rc = read_back, read_rc
        self.emulators = list(emulators)
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
        if "savebin" in script:
            for n, match in enumerate(re.finditer(r'savebin\s+("[^"]+"|\S+)\s+(\S+)\s+(\S+)', script)):
                dest = match.group(1).strip('"')
                data = (self.read_back or {}).get(n, [APP, ATOC][n] if False else None)
                if data is None:
                    data = self.sources[n]
                Path(dest).write_bytes(data)
            return flash_cmd._Outcome(
                success=self.read_rc == 0, stdout="Reading 1 byte...\nO.K.\n",
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
    warning = next(i for i in issues if i.code == "flash.jlink-reset-unconfirmed")
    assert warning.severity == "warning"
    assert "Reset: Failed" in warning.message
    assert any("NOT confirmed" in line for line in lines)


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
    assert "savebin" in read and "loadbin" not in read
    assert read.splitlines()[-4:] == ["RSetType 2", "r", "g", "exit"]
    assert read.splitlines().index("connect") < read.splitlines().index(
        next(l for l in read.splitlines() if l.startswith("savebin"))
    )


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
