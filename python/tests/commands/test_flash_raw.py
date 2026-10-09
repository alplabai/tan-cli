# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1446: `tan flash --raw <file>@<addr>` -- a byte-exact MRAM sector write.

No hardware: `_spawn_jlink` is a stub JLinkExe that records every Commander script."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from tan.commands import flash_cmd, flash_raw
from tan.core import raw_write
from tan.core.raw_write import RawError, RawSpec

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SECTOR = 0x4000
MRAM = 0x580000  # 5.5 MiB, the E8 window [0x80000000, 0x80580000)
SLOT0 = 0x80010000
ATOC = 0x8057C000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── the pure logic ──────────────────────────────────────────────────────────


def test_parse_splits_on_the_last_at_and_wants_an_explicit_hex_address():
    spec = raw_write.parse_raw("/b/he@slot0.bin@0x80010000")
    assert spec == RawSpec("/b/he@slot0.bin", 0x80010000)
    for bad in ("nofile", "@0x80010000", "x.bin@", "x.bin@80010000", "x.bin@0xZZ", "x.bin@2147549184"):
        with pytest.raises(RawError):
            raw_write.parse_raw(bad)


@pytest.mark.parametrize(
    "specs,why",
    [
        ([RawSpec("a", SLOT0 + 4, SECTOR)], "sector-aligned"),
        ([RawSpec("a", SLOT0, SECTOR + 1)], "byte-exact"),
        ([RawSpec("a", SLOT0, 0)], "empty"),
        ([RawSpec("a", 0x7FFFC000, SECTOR)], "outside MRAM"),
        ([RawSpec("a", 0x80580000, SECTOR)], "outside MRAM"),
        ([RawSpec("a", ATOC, 2 * SECTOR)], "outside MRAM"),
        ([RawSpec("a", SLOT0, 2 * SECTOR), RawSpec("b", SLOT0 + SECTOR, SECTOR)], "overlap"),
        ([], "at least one"),
    ],
)
def test_ranges_are_refused(specs, why):
    with pytest.raises(RawError, match=why):
        raw_write.validate_ranges(specs, MRAM)


def test_the_atoc_sector_is_accepted_only_at_the_address_given():
    raw_write.validate_ranges([RawSpec("atoc", ATOC, SECTOR), RawSpec("s", SLOT0, 2 * SECTOR)], MRAM)


def test_the_script_writes_verifies_and_never_resets_or_runs():
    script = raw_write.raw_script(
        "si SWD\nspeed 4000\ndevice PART\nconnect\n",
        [RawSpec("/b/a.bin", SLOT0, SECTOR), RawSpec("/b/b.bin", ATOC, SECTOR)],
    )
    assert script == (
        "si SWD\nspeed 4000\ndevice PART\nconnect\n"
        "loadbin /b/a.bin 0x80010000\nloadbin /b/b.bin 0x8057C000\n"
        "verifybin /b/a.bin 0x80010000\nverifybin /b/b.bin 0x8057C000\nexit\n"
    )


# ── the command ─────────────────────────────────────────────────────────────


class FakeJlink:
    def __init__(self, monkeypatch, *, read_back=None, write_rc=0, write_out="O.K.\n"):
        self.scripts: list[str] = []
        self.read_back, self.write_rc, self.write_out = read_back or {}, write_rc, write_out
        self.blobs: list[bytes] = []
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, *a, **k):
        self.scripts.append(script)
        if "ShowEmuList" in script:
            return flash_cmd._Outcome(success=True, stdout="", returncode=0)
        if "savebin" in script:
            for n, m in enumerate(re.finditer(r'savebin\s+("[^"]+"|\S+)\s+', script)):
                Path(m.group(1).strip('"')).write_bytes(self.read_back.get(n, self.blobs[n]))
            return flash_cmd._Outcome(success=True, stdout="O.K.\n", returncode=0)
        return flash_cmd._Outcome(
            success=self.write_rc == 0, stdout=self.write_out, returncode=self.write_rc
        )

    def kinds(self):
        return ["read" if "savebin" in s else "write" for s in self.scripts if "ShowEmuList" not in s]


def _setup(tmp_path, monkeypatch, *, blobs=None):
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (build / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_he, os: zephyr, output_artefact: a.bin, status: ok,\n"
        "   flash_method: alif_mram_jlink, flash_args: {jlink_flash_device: PART}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8", newline="",
    )
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    stub = tools / "JLinkExe"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.delenv("ALP_FLASH_REQUIRE_DPIDR", raising=False)
    monkeypatch.setenv(flash_raw.RESERVATION_ENV, "e1m-aen-evk-02")
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_cmd, "_flow_d_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_raw, "_mram_bytes", lambda _ctx: MRAM)
    data = blobs or [os.urandom(2 * SECTOR), os.urandom(SECTOR)]
    paths = []
    for i, blob in enumerate(data):
        p = tmp_path / f"blob{i}.bin"
        p.write_bytes(blob)
        paths.append(p)
    return data, paths


def _run(tmp_path, raw, **kw):
    kw.setdefault("confirm_flag", True)
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"), board_yaml=None,
        core="m55_he", helper=None, dry_run=kw.pop("dry_run", False), skip_missing_tools=False,
        capture=True, cwd=str(tmp_path), raw=tuple(raw), **kw,
    )


def _codes(issues):
    return [i.code for i in issues]


def _specs(paths):
    return [f"{paths[0]}@0x80010000", f"{paths[1]}@0x8057C000"]


def test_a_raw_write_loads_verifies_hashes_and_never_resets(tmp_path, monkeypatch):
    blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _lines, _sdk = _run(tmp_path, _specs(paths))
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert entry["method"] == "mram_raw" and entry["status"] == "ok"
    assert [w["sha256"] for w in entry["raw"]["writes"]] == [_sha(b) for b in blobs]
    assert [w["address"] for w in entry["raw"]["writes"]] == ["0x80010000", "0x8057C000"]
    assert [w["sectorSpan"]["count"] for w in entry["raw"]["writes"]] == [2, 1]
    assert entry["raw"]["resets"] is False
    assert entry["jlink"]["reservation"] == {"env": "JLINK_RUN_PLACE", "place": "e1m-aen-evk-02"}
    assert jl.kinds() == ["write"]
    script = jl.scripts[-1]
    assert f"loadbin {paths[0]} 0x80010000" in script and f"verifybin {paths[1]} 0x8057C000" in script
    assert "device PART" in script
    assert not any(w in script for w in ("RSetType", "\nr\n", "\ng\n"))
    assert "power-cycle" in entry["message"] and "readback" not in entry["message"]


def test_readback_is_a_fresh_session_without_a_reset(tmp_path, monkeypatch):
    blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    jl.blobs = blobs
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), readback=True)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert jl.kinds() == ["write", "read"]
    read = jl.scripts[-1]
    assert "loadbin" not in read and not any(w in read for w in ("RSetType", "\nr\n", "\ng\n"))
    assert read.splitlines()[-1] == "exit"
    assert entry["jlink"]["verification"] == "readback-verified"
    assert entry["jlink"]["readback"]["ok"] is True
    assert [r["sha256Actual"] for r in entry["jlink"]["readback"]["regions"]] == [_sha(b) for b in blobs]
    assert entry["jlink"].get("reset") is None


def test_a_readback_that_differs_fails_with_its_code(tmp_path, monkeypatch):
    blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, read_back={1: b"\xff" * SECTOR})
    jl.blobs = blobs
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), readback=True)
    assert rc == 1 and _codes(issues) == ["flash.readback-mismatch"]


@pytest.mark.parametrize(
    "specs",
    [
        lambda p: [f"{p[0]}@0x80010004"],  # unaligned
        lambda p: [f"{p[0]}@0x80580000"],  # past the end
        lambda p: [f"{p[0]}@0x8057C000"],  # 2 sectors from the ATOC sector: out of MRAM
        lambda p: [f"{p[0]}@0x80010000", f"{p[1]}@0x80014000"],  # overlap
        lambda p: [f"{p[0]}@80010000"],  # no 0x
    ],
)
def test_a_bad_range_is_refused_before_any_spawn(tmp_path, monkeypatch, specs):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    for dry in (False, True):  # a preview refuses too
        rc, data, issues, _l, _s = _run(tmp_path, specs(paths), dry_run=dry)
        assert rc == 1 and _codes(issues) == ["flash.raw-invalid"], (dry, data, issues)
    assert jl.scripts == []


def test_a_missing_file_is_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, [f"{tmp_path}/nope.bin@0x80010000"])
    assert rc == 1 and _codes(issues) == ["flash.raw-invalid"] and jl.scripts == []


def test_without_confirm_it_only_plans(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), confirm_flag=False)
    assert data["entries"][0]["status"] == "planned" and "flash.confirm-required" in _codes(issues)
    assert jl.scripts == []


def test_a_dry_run_previews_the_script_and_spawns_nothing(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), dry_run=True)
    assert rc == 0 and jl.scripts == []
    plan = data["entries"][0]["plan"]["jlinkScript"]
    assert any(l.startswith("loadbin ") for l in plan) and "RSetType 2" not in plan


def test_a_confirmed_write_without_a_reservation_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    monkeypatch.delenv(flash_raw.RESERVATION_ENV)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths))
    assert rc == 1 and _codes(issues) == ["flash.raw-reservation-required"]
    assert jl.scripts == []


def test_a_failed_write_session_is_flash_raw_failed(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch, write_rc=1, write_out="Verify failed\n")
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths))
    assert rc == 1 and _codes(issues) == ["flash.raw-failed"]


def test_raw_and_ram_cannot_be_combined(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), ram=True)
    assert rc == 1 and _codes(issues) == ["flash.raw-invalid"] and jl.scripts == []
