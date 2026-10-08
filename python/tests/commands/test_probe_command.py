# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1406: `tan probe identify|read` -- READ-ONLY J-Link probes.

No hardware: `_spawn_jlink` is a stub J-Link answering fake banners."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import flash_cmd, probe_cmd
from tan.core import probe_plan as pp
from tan.core.jlink_probe import JLinkProbe
from tan.core.ram_run import preamble

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SERIAL = "000999000001"
PROBE = JLinkProbe("3-4.2", SERIAL)
ITCM = (0x20003000, 0x00000101, 0x00000105, 0x00000109)


def _banner(ap_addr="0x00300000", dpidr="0x4C013477"):
    return (
        f"Found SW-DP with ID {dpidr}\nDPv3 detected\n"
        f"AP[3] (APAddr {ap_addr}): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n"
        "CPUID register: 0x411FD220\nFound Cortex-M55 r1p0, Little endian.\n"
    )


def _dump(addr, words):
    return f"{addr:08X} = " + " ".join(f"{w:08X}" for w in words) + "\n"


class FakeJlink:
    def __init__(self, monkeypatch, *, dpidr="0x4C013477", ap_addr="0x00300000", mem=None):
        self.scripts: list[str] = []
        self.dpidr, self.ap_addr, self.mem = dpidr, ap_addr, mem or {}
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, capture, timeout, venv_bin=None, workspace=None,
               executable=None, **kw):
        self.scripts.append(script)
        if "ShowEmuList" in script:
            out = f"J-Link[0]: Connection: USB, Serial number: {SERIAL}, ProductName: J-Link\n"
        elif "mem32 0x0," in script:
            out = _banner(self.ap_addr, self.dpidr) + _dump(0, ITCM) + _dump(0x58000000, ITCM)
        elif "mem32" in script:
            out = _banner(self.ap_addr, self.dpidr)
            for line in script.splitlines():
                if line.startswith("mem32"):
                    a, n = (int(x.strip(), 16) for x in line.split()[1:] for x in [x.rstrip(",")])
                    out += _dump(a, self.mem.get(a, tuple(range(n))))
        else:
            out = _banner(self.ap_addr, self.dpidr)
        out += "Script processing completed.\n"
        return flash_cmd._Outcome(success=True, stdout=out, returncode=0)

    def probe_scripts(self):
        return [s for s in self.scripts if "ShowEmuList" not in s]


@pytest.fixture
def env(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub = tools / "JLinkExe"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [PROBE])
    return tmp_path


def _manifest(tmp_path, expect="0x4C013477"):
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_he, os: zephyr, output_artefact: zephyr.elf, status: ok,\n"
        "   flash_method: alif_mram_jlink, flash_args: {jlink_device: Cortex-M55, "
        f"expect_dpidr: '{expect}'}}}}\nhelper_mcus: []\nboot_order: []\n",
        encoding="utf-8",
    )
    return str(build)


def _run(tmp_path, verb, address=None, count=None, *, core=None, build_root=None, **kw):
    return probe_cmd._run(
        verb, address, count, core, build_root or str(tmp_path / "build"), None, "3-4.2", None,
        str(tmp_path), enumerate_probes=lambda: [PROBE], **kw,
    )


def test_identify_happy_path_matches_the_manifest(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _ = _run(env, "identify", build_root=_manifest(env), core="m55_he")
    assert rc == 0 and issues == []
    ident = data["identity"]
    assert ident["dpidr"] == "0x4C013477" and ident["expectedDpidr"] == "0x4C013477"
    assert ident["dpidrMatch"] is True
    assert ident["apAddr"] == "0x00300000" and ident["cpuid"] == "0x411FD220"
    assert ident["core"] == "he" and ident["itcmVerdict"] == "match"
    assert "isolation" in ident and data["writes"] is False
    assert len(jl.probe_scripts()) == 2


def test_identify_runs_the_preflight_script_exactly(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _run(env, "identify", build_root=_manifest(env))
    from tan.core.flash_plan import FlashInputs, flow_d_preflight_script

    flash_args = {"jlink_device": "Cortex-M55", "expect_dpidr": "0x4C013477", "jlink_serial": SERIAL}
    shared, _expected = flow_d_preflight_script(
        FlashInputs(artefact="x.elf", flash_args=flash_args, core_id="m55_he", sku="S",
                    dry_run=False, force_confirm=False)
    )
    assert jl.probe_scripts()[0] == shared


def test_identify_flags_a_dpidr_mismatch(env, monkeypatch):
    FakeJlink(monkeypatch, dpidr="0x0BE12477")
    rc, data, issues, _ = _run(env, "identify", build_root=_manifest(env))
    assert rc == 1
    assert [i.code for i in issues] == ["probe.dpidr-mismatch"]
    assert data["identity"]["dpidr"] == "0x0BE12477" and data["identity"]["dpidrMatch"] is False


def test_identify_without_a_manifest_reports_but_does_not_compare(env, monkeypatch):
    FakeJlink(monkeypatch)
    rc, data, issues, _ = _run(env, "identify")
    assert rc == 0 and issues == []
    assert data["identity"]["expectedDpidr"] is None and data["identity"]["dpidrMatch"] is None


def test_read_happy_path_returns_hex_words(env, monkeypatch):
    jl = FakeJlink(monkeypatch, mem={0x80010000: (0xAABBCCDD, 1, 2, 3)})
    rc, data, issues, _ = _run(env, "read", "0x80010000", "4")
    assert rc == 0 and issues == []
    assert data["read"]["data"] == ["0xAABBCCDD", "0x00000001", "0x00000002", "0x00000003"]
    assert data["read"]["address"] == "0x80010000" and data["read"]["words"] == 4
    assert [l for l in jl.probe_scripts()[0].splitlines() if l.startswith("mem32")] == [
        "mem32 0x80010000, 0x4"
    ]


def test_read_defaults_to_four_words_and_accepts_decimal(env, monkeypatch):
    FakeJlink(monkeypatch)
    rc, data, _i, _l = _run(env, "read", "2147549184")
    assert rc == 0 and data["read"]["address"] == "0x80010000" and data["read"]["words"] == 4


def test_read_too_large_is_refused_before_any_spawn(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, _d, issues, _ = _run(env, "read", "0x80010000", "257")
    assert rc != 0 and [i.code for i in issues] == ["probe.read-too-large"]
    assert jl.scripts == []
    rc, data, issues, _ = _run(env, "read", "0x80010000", "256")
    assert rc == 0 and data["read"]["words"] == 256


@pytest.mark.parametrize("addr", ["0x50000000", "0x58000000", "0x5FFFFFFC", "0x4FFFFFFC"])
def test_read_in_the_hp_window_is_refused_on_he(env, monkeypatch, addr):
    jl = FakeJlink(monkeypatch)
    # selected core HE: refused with no J-Link session at all
    rc, _d, issues, _ = _run(env, "read", addr, "4", core="m55_he")
    assert rc != 0 and [i.code for i in issues] == ["probe.read-unsafe-region"]
    assert "HE" in issues[0].message and jl.scripts == []


def test_read_in_the_window_is_refused_when_the_attach_is_he(env, monkeypatch):
    FakeJlink(monkeypatch, ap_addr="0x00300000")
    rc, data, issues, _ = _run(env, "read", "0x50000000", "4")
    assert rc != 0 and [i.code for i in issues] == ["probe.read-unsafe-region"]
    assert "read" not in data


def test_read_in_the_window_is_allowed_on_a_confirmed_hp_attach(env, monkeypatch):
    FakeJlink(monkeypatch, ap_addr="0x00200000")
    rc, data, issues, _ = _run(env, "read", "0x50000000", "4", core="m55_hp")
    assert rc == 0, issues
    assert data["read"]["address"] == "0x50000000"


@pytest.mark.parametrize("addr", ["0x80010000;erase", "-4", "1e3", "0x8001000G", "", "0x1_0000", "0x80010002"])
def test_read_refuses_anything_but_aligned_plain_numbers(env, monkeypatch, addr):
    jl = FakeJlink(monkeypatch)
    rc, _d, issues, _ = _run(env, "read", addr, "4")
    assert rc != 0 and [i.code for i in issues] == ["probe.bad-argument"] and jl.scripts == []


def test_no_probe_script_contains_a_mutating_verb(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _run(env, "identify", build_root=_manifest(env))
    _run(env, "read", "0x80010000", "4")
    jl2 = FakeJlink(monkeypatch, ap_addr="0x00200000")
    _run(env, "read", "0x50000000", "4", core="m55_hp")
    scripts = jl.probe_scripts() + jl2.probe_scripts()
    assert len(scripts) >= 5
    pre = preamble("123", 4000, "Cortex-M55")
    scripts += [pp.identity_script(pre), pp.core_script(pre), pp.read_script(pre, 0x80010000, 256)]
    allowed = {"selectemubysn", "si", "speed", "device", "connect", "mem32", "exit", "exec"}
    for script in scripts:
        verbs = pp.script_verbs(script)
        assert not set(verbs) & set(pp.FORBIDDEN_VERBS), script
        assert set(verbs) <= allowed, script


def test_the_cli_emits_the_envelope_shape(env, monkeypatch):
    FakeJlink(monkeypatch, mem={0x80010000: ITCM})
    result = CliRunner().invoke(
        app, ["probe", "read", "0x80010000", "4", "--build-root", str(env / "build"),
              "--project", str(env), "--format", "json"],
    )
    doc = json.loads(result.stdout)
    assert result.exit_code == 0 and doc["command"] == "probe" and doc["ok"] is True
    assert doc["exitCode"] == 0 and doc["issues"] == []
    assert doc["data"]["verb"] == "read" and doc["data"]["read"]["data"][0] == "0x20003000"
    bad = CliRunner().invoke(app, ["probe", "read", "0x80010000", "999", "--format", "json"])
    doc = json.loads(bad.stdout)
    assert bad.exit_code != 0 and doc["ok"] is False and doc["issues"][0]["code"] == "probe.read-too-large"
    unk = CliRunner().invoke(app, ["probe", "bogus", "--format", "json"])
    assert json.loads(unk.stdout)["issues"][0]["code"] == "probe.unknown-verb"
