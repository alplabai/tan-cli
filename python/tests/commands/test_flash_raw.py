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
import json
import pwd
import socket

from tan.commands import build_output
from tan.core import raw_write
from tan.core.jlink_probe import JLinkProbe
from tan.core.raw_write import RawError, RawSpec

_real_window = flash_raw._mram_window

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SECTOR = 0x4000
MRAM = 0x580000  # 5.5 MiB, the E8 window [0x80000000, 0x80580000)
SLOT0 = 0x80010000
BASE = 0x80000000
ME = f"{socket.gethostname()}/{pwd.getpwuid(os.getuid()).pw_name}"
SERIAL = "000999000001"
USB = "3-4.3"
WRAPPER = '#!/bin/bash\nJLINK_RUN_STANDIN=1 exec jlink-run.sh "$@"\n'
ATOC = 0x8057C000


def _show(holder=ME, path=USB):
    block = f"Acquired resource 'swd' (e/p/NetworkUSBDebugger/swd):\n  {{'path': '{path}'}}\n" if path else ""
    return f"Place 'p':\n  matches:\n    e/NetworkUSBDebugger/swd\n  acquired: {holder}\n{block}"


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
        raw_write.validate_ranges(specs, MRAM, BASE)


def test_the_atoc_sector_is_accepted_only_at_the_address_given():
    raw_write.validate_ranges([RawSpec("atoc", ATOC, SECTOR), RawSpec("s", SLOT0, 2 * SECTOR)], MRAM, BASE)


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
            line = f"J-Link[0]: Connection: USB, Serial number: {SERIAL}, ProductName: J-Link"
            return flash_cmd._Outcome(success=True, stdout=line, returncode=0)
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
    stub.write_text(WRAPPER, encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.delenv("ALP_FLASH_REQUIRE_DPIDR", raising=False)
    monkeypatch.setenv(flash_raw.RESERVATION_ENV, "e1m-aen-evk-02")
    monkeypatch.setenv(flash_raw.WRAPPER_ENV, str(stub))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_cmd, "_flow_d_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_raw, "_mram_window", lambda _ctx: (BASE, MRAM))
    monkeypatch.setattr(flash_raw, "_labgrid_show", lambda place: (_show(), ""))
    data = blobs or [os.urandom(2 * SECTOR), os.urandom(SECTOR)]
    paths = []
    for i, blob in enumerate(data):
        p = tmp_path / f"blob{i}.bin"
        p.write_bytes(blob)
        paths.append(p)
    return data, paths


def _run(tmp_path, raw, **kw):
    kw.setdefault("confirm_flag", True)
    kw.setdefault("probe_usb_path", USB)
    kw.setdefault("enumerate_probes", lambda: [JLinkProbe(USB, SERIAL)])
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
    assert entry["raw"]["resetCommands"] is False
    assert entry["jlink"]["reservation"] == {
        "env": "JLINK_RUN_PLACE", "place": "e1m-aen-evk-02", "verified": "labgrid",
        "usbPath": USB}
    assert jl.kinds() == ["write"]
    script = jl.scripts[-1]
    assert f"loadbin {paths[0]} 0x80010000" in script and f"verifybin {paths[1]} 0x8057C000" in script
    assert "device PART" in script
    assert not any(w in script for w in ("RSetType", "\nr\n", "\ng\n"))
    assert "power-cycle" in entry["message"] and "readback" not in entry["message"]
    assert "no reset command was sent" in entry["message"] and "still runs" not in entry["message"]


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


def test_the_manifest_confirm_never_arms_a_raw_write(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    manifest = tmp_path / "build" / "system-manifest.yaml"
    manifest.write_text(
        manifest.read_text().replace("{jlink_flash_device: PART}", "{jlink_flash_device: PART, confirm: true}"),
        encoding="utf-8", newline="",
    )
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), confirm_flag=False)
    assert data["entries"][0]["status"] == "planned" and jl.scripts == []


def _refused(tmp_path, monkeypatch, paths, text):
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths))
    assert rc == 1 and _codes(issues) == ["flash.raw-reservation-required"], (data, issues)
    assert text in data["entries"][0]["message"] and jl.scripts == []


def test_a_look_alike_jlinkexe_on_path_printing_the_marker_is_refused(tmp_path, monkeypatch):
    """Review: a marker string in whatever is on PATH is not an interlock."""
    _blobs, paths = _setup(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    fake = other / "JLinkExe"
    fake.write_text("#!/bin/bash\n# JLINK_RUN_STANDIN JLINK_RUN_PLACE\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    monkeypatch.setenv("PATH", str(other))  # it is what tan finds
    _refused(tmp_path, monkeypatch, paths, "is not the configured wrapper")


def test_no_configured_wrapper_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv(flash_raw.WRAPPER_ENV)
    _refused(tmp_path, monkeypatch, paths, "TAN_JLINK_WRAPPER is not set")


def test_a_relative_wrapper_path_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv(flash_raw.WRAPPER_ENV, "tools/JLinkExe")
    _refused(tmp_path, monkeypatch, paths, "is not an absolute path")


def test_a_relative_jlink_override_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(flash_raw.WRAPPER_ENV, str(tmp_path / "tools" / "JLinkExe"))
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), jlink_path="tools/JLinkExe")
    assert rc == 1 and jl.scripts == []


def test_a_wrapper_under_the_cwd_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _refused(tmp_path, monkeypatch, paths, "lives under the current directory")


def test_a_world_writable_wrapper_is_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    os.chmod(tmp_path / "tools" / "JLinkExe", 0o777)
    _refused(tmp_path, monkeypatch, paths, "world-writable")


def test_a_symlink_to_an_untrusted_target_is_judged_by_its_target(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "tools" / "JLinkExe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(flash_raw.WRAPPER_ENV, str(link))
    _refused(tmp_path, monkeypatch, paths, "lives under the current directory")


def test_labgrid_client_is_never_found_through_path(tmp_path, monkeypatch):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    fake = shadow / "labgrid-client"
    fake.write_text(f"#!/bin/sh\necho '  acquired: {ME}'\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    monkeypatch.setenv("PATH", f"{shadow}:/usr/bin:/bin")
    monkeypatch.delenv(flash_raw.LABGRID_ENV, raising=False)
    monkeypatch.setattr(flash_raw, "LABGRID_DIRS", ())
    assert flash_raw._labgrid_client() is None
    assert flash_raw._labgrid_show("p")[0] is None
    monkeypatch.setenv(flash_raw.LABGRID_ENV, str(fake))  # explicitly configured is used
    assert flash_raw._labgrid_client() == os.path.realpath(fake)
    assert ME in flash_raw._labgrid_show("p")[0]


def test_labgrid_client_from_a_world_writable_dir_is_not_used(tmp_path, monkeypatch):
    d = tmp_path / "ww"
    d.mkdir()
    fake = d / "labgrid-client"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    os.chmod(d, 0o777)
    monkeypatch.setenv(flash_raw.LABGRID_ENV, str(fake))
    assert flash_raw._labgrid_client() is None


@pytest.mark.parametrize(
    "show,text",
    [
        ((None, "labgrid-client exited 1: no coordinator"), "no coordinator"),
        ((_show("other-host/someone"), ""), "do not hold the labgrid place"),
        (("Place 'p':\n", ""), "do not hold the labgrid place"),
        ((_show() + "  acquired: other/x\n", ""), "do not hold the labgrid place"),  # ambiguous
    ],
)
def test_a_lease_labgrid_does_not_show_as_ours_is_refused(tmp_path, monkeypatch, show, text):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    monkeypatch.setattr(flash_raw, "_labgrid_show", lambda place: show)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths))
    assert rc == 1 and _codes(issues) == ["flash.raw-reservation-required"]
    assert text in data["entries"][0]["message"] and jl.scripts == []


@pytest.mark.parametrize("usb", ["3-4.2", None])
def test_a_probe_that_is_not_the_leased_places_swd_port_is_refused(tmp_path, monkeypatch, usb):
    """Review: the lease must cover the probe actually selected."""
    _blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    monkeypatch.setattr(flash_raw, "_labgrid_show", lambda place: (_show(path=usb), ""))
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths))
    assert rc == 1 and _codes(issues) == ["flash.raw-reservation-required"]
    assert "not the leased place's swd port" in data["entries"][0]["message"] and jl.scripts == []


def test_an_unknown_selected_usb_path_cannot_be_matched_to_the_lease(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    wrapper = os.environ[flash_raw.WRAPPER_ENV]
    refusal, verified = flash_raw._reservation_refusal("p", wrapper, None)
    assert verified is None and "not the leased place's swd port" in refusal
    assert flash_raw._reservation_refusal("p", wrapper, USB) == (None, os.path.realpath(wrapper))


def test_the_verified_path_is_what_every_spawn_uses(tmp_path, monkeypatch):
    """Review: the env var is not re-read after verification."""
    blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    seen = []
    real = flash_cmd._execute
    wrapper = os.path.realpath(os.environ[flash_raw.WRAPPER_ENV])

    def _spy(plan, *a, **k):
        seen.append(k.get("jlink_exe"))
        monkeypatch.setenv(flash_raw.WRAPPER_ENV, "/nonexistent/elsewhere")  # swapped after the check
        return real(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", _spy)
    jl.blobs = blobs
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), readback=True)
    assert rc == 0, (data, issues)
    assert seen and set(seen) == {wrapper}


def test_group_writable_by_a_shared_group_and_foreign_owners_are_refused(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    tool = tmp_path / "tools" / "JLinkExe"
    monkeypatch.setattr(flash_raw, "_private_group", lambda gid: False)
    os.chmod(tool, 0o775)
    assert "group-writable" in flash_raw._unsafe(str(tool))
    os.chmod(tool, 0o755)
    monkeypatch.setattr(flash_raw.os, "getuid", lambda: os.stat(tool).st_uid + 12345)
    assert "owned by uid" in flash_raw._unsafe(str(tool))


def test_a_symlinks_own_directory_chain_is_checked_too(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    linkdir = tmp_path / "linkdir"
    linkdir.mkdir()
    link = linkdir / "JLinkExe"
    link.symlink_to(tmp_path / "tools" / "JLinkExe")
    os.chmod(linkdir, 0o777)
    assert "world-writable" in flash_raw._unsafe(str(link))


def test_the_user_comes_from_the_uid_not_the_environment(monkeypatch):
    monkeypatch.setenv("USER", "mallory")
    monkeypatch.setenv("LOGNAME", "mallory")
    assert flash_raw._current_user() == pwd.getpwuid(os.getuid()).pw_name


def test_labgrid_is_run_with_a_default_coordinator_and_no_python_steering(tmp_path, monkeypatch):
    fake = tmp_path / "labgrid-client"
    fake.write_text("#!/bin/sh\necho \"$LG_COORDINATOR|$PYTHONPATH|$PYTHONHOME\"\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    monkeypatch.setenv(flash_raw.LABGRID_ENV, str(fake))
    monkeypatch.delenv("LG_COORDINATOR", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("PYTHONHOME", "/evil")
    out, why = flash_raw._labgrid_show("p")
    assert out.strip() == "100.64.0.1:20408||" and why == ""
    monkeypatch.setenv("LG_COORDINATOR", "10.0.0.1:1")
    assert flash_raw._labgrid_show("p")[0].startswith("10.0.0.1:1|")


def test_a_failing_labgrid_client_explains_itself(tmp_path, monkeypatch):
    fake = tmp_path / "labgrid-client"
    fake.write_text("#!/bin/sh\necho 'no route to coordinator' >&2\nexit 1\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    monkeypatch.setenv(flash_raw.LABGRID_ENV, str(fake))
    out, why = flash_raw._labgrid_show("p")
    assert out is None and "exited 1: no route to coordinator" in why


def test_the_swd_path_is_read_from_the_resource_block_not_the_matches_list():
    assert raw_write.lease_swd_path(_show(path="3-4.2")) == "3-4.2"
    assert raw_write.lease_swd_path(_show(path=None)) is None
    assert raw_write.lease_swd_path("Place 'p':\n  matches:\n    e/NetworkUSBDebugger/swd\n") is None


def test_readback_compares_against_the_recorded_hash_not_a_rehash(tmp_path, monkeypatch):
    """The file changing on disk after it was hashed must not move the expectation."""
    blobs, paths = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    jl.blobs = blobs
    original = jl._spawn

    def _tamper(argv, script, *a, **k):
        out = original(argv, script, *a, **k)
        if "loadbin" in script:
            for p in paths:
                p.write_bytes(b"\x00" * len(p.read_bytes()))
        return out

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _tamper)
    rc, data, issues, _l, _s = _run(tmp_path, _specs(paths), readback=True)
    assert rc == 0, (data, issues)
    regions = data["entries"][0]["jlink"]["readback"]["regions"]
    assert [r["sha256Expected"] for r in regions] == [_sha(b) for b in blobs]


def test_the_lease_parser_is_strict():
    assert raw_write.lease_holder("x\n  acquired: h/u\n  y") == "h/u"
    assert raw_write.lease_holder("  acquired:\n") is None
    assert raw_write.lease_holder("  acquired: a/b\n  acquired: c/d\n") is None  # ambiguous
    assert raw_write.lease_holder("  acquired: a/b\n  acquired: a/b\n") == "a/b"
    assert raw_write.lease_holder("acquired: a/b\n") is None  # not the top-level field


def _soc(tmp_path, monkeypatch, variants, base=BASE):
    meta = tmp_path / "sdk" / "metadata" / "socs" / "alif" / "ensemble"
    meta.mkdir(parents=True)
    (meta / "e3.json").write_text(json.dumps({"soc_flash_base": base} if base else {}), encoding="utf-8")
    monkeypatch.setattr(
        build_output, "read_sdk_som_and_soc",
        lambda root, sku, **k: ("alif:ensemble:e3", "ORDER", variants, 5.5, []),
    )
    return type("Ctx", (), {"sdk_root": str(tmp_path / "sdk"), "sku": "S"})()


def test_the_mram_window_is_the_skus_own_variant_and_the_documents_base(tmp_path, monkeypatch):
    ctx = _soc(tmp_path, monkeypatch, [{"order_code": "ORDER", "mram_mb": 1.5}])
    window = _real_window(ctx)
    assert window == (BASE, int(1.5 * 1024 * 1024))


def test_an_unresolved_variant_or_missing_base_is_refused_not_defaulted(tmp_path, monkeypatch):
    ctx = _soc(tmp_path, monkeypatch, [{"order_code": "OTHER", "mram_mb": 5.5}])  # no match
    with pytest.raises(RawError, match="does not resolve"):
        _real_window(ctx)
    ctx = _soc(tmp_path / "b", monkeypatch, [{"order_code": "ORDER", "mram_mb": 5.5}], base=0)
    with pytest.raises(RawError, match="soc_flash_base"):
        _real_window(ctx)


def test_a_symlinks_own_0777_mode_is_not_a_finding(tmp_path, monkeypatch):
    _blobs, paths = _setup(tmp_path, monkeypatch)
    link = tmp_path / "ok-link"
    link.symlink_to(tmp_path / "tools" / "JLinkExe")
    assert flash_raw._unsafe(str(link)) is None
