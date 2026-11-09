# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1509: several `alif_mram_jlink` slices in one `tan flash` run are signed
into ONE ATOC (DEVICE + one entry per slice) and the package is written once.
A second ATOC at the same package address would delist the first core.

No hardware: a fake `app-gen-toc` reports the entries it was configured with."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from tan.commands import flash_cmd
from tan.core import setools as setools_module
from tan.core.flash_plan import (
    FlashInputs, FlashTarget, merge_flow_d_targets, plan_alif_mram_jlink,
)
from tan.core.setools import combined_slot0_config, slot0_config

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake app-gen-toc is a python shebang")

HE, HP = "0x80010000", "0x802b0000"
FAKE = f"""#!{sys.executable}
import json, os, sys
cfg = json.load(open(sys.argv[2]))
os.makedirs("build", exist_ok=True)
with open("build/app-package-map.txt", "w") as fh:
    fh.write("APP Package Start Address: 0x8057e030\\n")
    for name, ent in cfg.items():
        fh.write("APP TOC entry for %s obj_address %s\\n" % (name, ent.get("mramAddress", "0x80000000")))
open("build/AppTocPackage.bin", "wb").write(b"atoc")
"""

TWO = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: he.bin, status: ok, flash_method: alif_mram_jlink,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000"}}
- {core_id: m55_hp, os: zephyr, output_artefact: hp.bin, status: ok, flash_method: alif_mram_jlink,
   flash_args: {jlink_flash_device: PART_PROFILE, slot0_load_address: "0x802b0000"}}
helper_mcus: []
boot_order: []
"""
ONE = TWO.split("- {core_id: m55_hp")[0] + "helper_mcus: []\nboot_order: []\n"


def _run(tmp_path, monkeypatch, manifest, *, core=None, dry_run=True, extra_flags=None):
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    setools = tmp_path / "setools"
    (setools / "build" / "config").mkdir(parents=True)
    (setools / "build" / "config" / "app-device-config.json").write_text(
        '{"metadata": {"device": "PART_STOCK"}}\n', encoding="utf-8")
    tool = setools / setools_module.APP_GEN_TOC
    tool.write_text(FAKE, encoding="utf-8")
    os.chmod(tool, 0o755)
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    for n in ("he.bin", "hp.bin"):
        (build / n).write_bytes(b"\x50\x42\x00\x20" + b"\x00" * 64)
    (build / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="")
    tools = tmp_path / "faketools"
    tools.mkdir()
    jlink = tools / "JLinkExe"
    jlink.write_text("", encoding="utf-8")
    os.chmod(jlink, 0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SETOOLS_DIR", str(setools))
    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=core, helper=None, dry_run=dry_run,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path), **(extra_flags or {}),
    )


# ── config builder ──────────────────────────────────────────────────────────


def test_combined_config_names_device_then_every_slice():
    cfg = combined_slot0_config(
        [("m55_he", "m55_he.bin", HE), ("m55_hp", "m55_hp.bin", HP)], device_binary="dev.json"
    )
    assert list(cfg) == ["DEVICE", "m55_he", "m55_hp"]
    assert cfg["m55_hp"] == {
        "binary": "m55_hp.bin", "version": "1.0.0", "mramAddress": HP,
        "cpu_id": "M55_HP", "flags": ["boot"], "signed": True,
    }
    assert cfg["m55_he"] == slot0_config("m55_he", "m55_he.bin", HE, "M55_HE")["m55_he"]


def test_combined_config_of_one_slice_is_the_single_slice_config():
    for device in ("dev.json", None):
        assert json.dumps(combined_slot0_config([("m55_he", "m55_he.bin", HE)], device)) == (
            json.dumps(slot0_config("m55_he", "m55_he.bin", HE, "M55_HE", device_binary=device))
        )


# ── grouping + the plan ─────────────────────────────────────────────────────


def _t(cid, **fa):
    return FlashTarget("slice", cid, "alif_mram_jlink",
                       {"jlink_flash_device": "P", "slot0_load_address": HE, **fa}, f"{cid}.bin")


def test_merge_folds_signable_slices_into_the_first():
    merged = merge_flow_d_targets([_t("m55_he"), _t("m55_hp")])
    assert [t.id for t in merged] == ["m55_he"]
    assert [c.id for c in merged[0].co_slices] == ["m55_hp"]


def test_merge_leaves_lone_customer_signed_and_foreign_device_slices_alone():
    lone = [_t("m55_he")]
    assert merge_flow_d_targets(lone) == tuple(lone)
    own = [_t("a"), _t("b", atoc="x.bin", atoc_address="0x1")]
    assert merge_flow_d_targets(own) == tuple(own)
    other = [_t("a"), FlashTarget("slice", "b", "alif_mram_jlink",
                                  {"jlink_flash_device": "Q", "slot0_load_address": HP}, "b.bin")]
    assert merge_flow_d_targets(other) == tuple(other)


def test_plan_writes_both_apps_then_the_package_once():
    plan = plan_alif_mram_jlink(
        FlashInputs(
            artefact="he.bin", flash_args={
                "jlink_flash_device": "P", "slot0_load_address": HE,
                "atoc": "atoc.bin", "atoc_address": "0x8057e030"},
            core_id="m55_he", sku="S", dry_run=True, extra_apps=(("m55_hp", "hp.bin", HP),),
        ),
        lambda _t: True,
    )
    loads = [l for l in plan.jlink_script.splitlines() if l.startswith("loadbin")]
    assert loads == [f"loadbin he.bin {HE}", f"loadbin hp.bin {HP}", "loadbin atoc.bin 0x8057e030"]
    assert plan.jlink_script.count("verifybin") == 3


def test_single_slice_plan_is_unchanged():
    plan = plan_alif_mram_jlink(
        FlashInputs(
            artefact="he.bin", flash_args={
                "jlink_flash_device": "P", "slot0_load_address": HE,
                "atoc": "atoc.bin", "atoc_address": "0x8057ea50"},
            core_id="m55_he", sku="S", dry_run=True),
        lambda _t: True,
    )
    assert plan.jlink_script == (
        "si SWD\nspeed 4000\ndevice P\nconnect\n"
        f"loadbin he.bin {HE}\nloadbin atoc.bin 0x8057ea50\n"
        f"verifybin he.bin {HE}\nverifybin atoc.bin 0x8057ea50\n"
        "RSetType 2\nr\ng\nexit\n"
    )
    assert plan.ok_message == (
        f"alif_mram_jlink[m55_he]: app -> {HE}, signed ATOC -> 0x8057ea50 via J-Link (P); "
        "cache-verified and PIN-reset"
    )


# ── end to end (dry run) ────────────────────────────────────────────────────


def test_dry_run_two_slices_signs_one_atoc_naming_everything(tmp_path, monkeypatch):
    _code, data, _issues, _lines, _sdk = _run(tmp_path, monkeypatch, TWO)
    assert [x["id"] for x in data["entries"]] == ["m55_he", "m55_hp"]
    e = data["entries"][0]
    assert [x["name"] for x in e["atoc"]["entries"]] == ["DEVICE", "m55_he", "m55_hp"]
    assert e["atoc"]["address"] == "0x8057e030"
    assert "This ATOC names: DEVICE, m55_he, m55_hp." in e["message"]
    writes = e["plan"]["writes"]
    assert [w["name"] for w in writes] == ["app", "app:m55_hp", "atoc"]
    assert [w["address"] for w in writes] == [HE, HP, "0x8057e030"]
    assert [w["sectorSpan"]["count"] for w in writes] == [1, 1, 1]


def test_no_device_config_drops_only_device(tmp_path, monkeypatch):
    _c, data, *_ = _run(tmp_path, monkeypatch, TWO, extra_flags={"no_device_config": True})
    assert [x["name"] for x in data["entries"][0]["atoc"]["entries"]] == ["m55_he", "m55_hp"]


def test_core_subset_signs_one_entry_and_says_the_other_is_delisted(tmp_path, monkeypatch):
    _c, data, *_ = _run(tmp_path, monkeypatch, TWO, core="m55_he")
    e = data["entries"][0]
    assert [x["name"] for x in e["atoc"]["entries"]] == ["DEVICE", "m55_he"]
    assert "(m55_hp) are NOT named, so they will be delisted too" in e["message"]


def test_single_slice_manifest_has_no_delist_text_and_one_app(tmp_path, monkeypatch):
    _c, data, *_ = _run(tmp_path, monkeypatch, ONE)
    e = data["entries"][0]
    assert "NOT named" not in e["message"]
    assert [w["name"] for w in e["plan"]["writes"]] == ["app", "atoc"]
    assert [x["name"] for x in e["atoc"]["entries"]] == ["DEVICE", "m55_he"]
    assert e["atoc"]["address"] == "0x8057e030"



# ── failure paths ───────────────────────────────────────────────────────────


def _two(hp_args='slot0_load_address: "0x802b0000"', he_args='slot0_load_address: "0x80010000"'):
    return TWO.replace('slot0_load_address: "0x80010000"', he_args).replace(
        'slot0_load_address: "0x802b0000"', hp_args)


def _codes(issues):
    return [i.code for i in issues]


def test_sign_failure_fails_the_one_combined_entry(tmp_path, monkeypatch):
    def failing(*a, **k):
        assert [x[0] for x in k["extra_slices"]] == ["m55_hp"]
        from tan.core.flash_plan import FlashPlanError
        raise FlashPlanError("alif_mram_jlink: app-gen-toc -f x exited 1: boom")

    monkeypatch.setattr(flash_cmd, "sign_slot0", failing)
    code, data, issues, *_ = _run(tmp_path, monkeypatch, TWO)
    assert code != 0
    assert [e["status"] for e in data["entries"] if e["id"] == "m55_he"] == ["failed"]
    assert "boom" in data["entries"][0]["message"]


def test_co_slice_missing_bin_fails_the_whole_entry(tmp_path, monkeypatch):
    manifest = TWO.replace("hp.bin", "gone.bin")
    code, data, issues, *_ = _run(tmp_path, monkeypatch, manifest)
    assert code != 0
    assert data["entries"][0]["status"] == "failed"
    assert "gone.bin" in data["entries"][0]["message"]


def test_co_slice_not_mram_linked_is_refused_with_its_code(tmp_path, monkeypatch):
    real = flash_cmd.mram_link_guard
    monkeypatch.setattr(
        flash_cmd, "mram_link_guard",
        lambda path, cid, **k: "refusing -- hp is linked below slot0" if cid == "m55_hp"
        else real(path, cid, **k),
    )
    code, data, issues, *_ = _run(tmp_path, monkeypatch, TWO)
    assert code != 0
    assert "flash.mram-image-not-mram-linked" in _codes(issues)


def test_co_slice_without_slot0_address_is_refused(tmp_path, monkeypatch):
    code, data, issues, *_ = _run(tmp_path, monkeypatch, _two(hp_args="x: 1"))
    assert code != 0
    assert "slot0_load_address is required for slice 'm55_hp'" in data["entries"][0]["message"]


def test_apps_in_one_sector_are_refused(tmp_path, monkeypatch):
    code, data, issues, *_ = _run(tmp_path, monkeypatch, _two(hp_args='slot0_load_address: "0x80011000"'))
    assert code != 0
    assert "flash.write-sector-overlap" in _codes(issues)


@pytest.mark.parametrize("key,lead_only", [
    ("confirm: true", True), ("atoc_unqueryable: true", True),
    ("resident_atoc_entries: [DEVICE]", True), ("setools_device_config: dev.json", True),
])
def test_co_slice_disagreeing_with_the_lead_is_refused(tmp_path, monkeypatch, key, lead_only):
    manifest = TWO.replace('slot0_load_address: "0x802b0000"', f'slot0_load_address: "0x802b0000", {key}')
    code, data, issues, *_ = _run(tmp_path, monkeypatch, manifest)
    assert code != 0
    name = key.split(":")[0]
    assert f"flash_args.{name} differs" in data["entries"][0]["message"]


def test_different_probe_or_dpidr_means_separate_writes():
    a = _t("a", jlink_serial="1")
    b = _t("b", jlink_serial="2")
    assert merge_flow_d_targets([a, b]) == (a, b)
    c = _t("c", expect_dpidr="0x1", jlink_device="D")
    assert merge_flow_d_targets([_t("d", jlink_device="D"), c]) == (_t("d", jlink_device="D"), c)


def test_each_folded_slice_gets_its_own_row(tmp_path, monkeypatch):
    _c, data, *_ = _run(tmp_path, monkeypatch, TWO)
    rows = {e["id"]: e for e in data["entries"]}
    assert list(rows) == ["m55_he", "m55_hp"]
    assert rows["m55_hp"]["status"] == rows["m55_he"]["status"] == "ok"
    assert rows["m55_hp"]["rc"] == 0 and "combined ATOC led by m55_he" in rows["m55_hp"]["message"]
    assert "atoc" not in rows["m55_hp"]


def test_single_slice_envelope_has_one_plain_row(tmp_path, monkeypatch):
    _c, data, *_ = _run(tmp_path, monkeypatch, ONE)
    assert [e["id"] for e in data["entries"]] == ["m55_he"]
    assert "combined" not in data["entries"][0]["message"]


# ── confirmed runs (JLinkExe + the sign stubbed) ────────────────────────────

CONFIRMED = TWO.replace(
    "flash_args: {jlink_flash_device: PART_PROFILE,",
    "flash_args: {jlink_flash_device: PART_PROFILE, confirm: true, expect_dpidr: '0x6BA02477', "
    "jlink_device: CORE,",
)


def _confirmed(tmp_path, monkeypatch, manifest=CONFIRMED, **flags):
    order, scripts = [], []
    real_sign = flash_cmd.sign_slot0
    monkeypatch.setattr(
        flash_cmd, "_flow_d_preflight", lambda *a, **k: order.append("preflight")
    )
    monkeypatch.setattr(
        flash_cmd, "sign_slot0", lambda *a, **k: (order.append("sign"), real_sign(*a, **k))[1]
    )

    def fake_execute(plan, *a, **k):
        order.append("write")
        scripts.append(plan.jlink_script)
        return flash_cmd._Outcome(success=True, stdout="", stderr="")

    monkeypatch.setattr(flash_cmd, "_execute", fake_execute)
    out = _run(tmp_path, monkeypatch, manifest, dry_run=False, extra_flags=flags)
    return out, order, scripts


def test_confirmed_run_refuses_without_the_ack_and_names_both_slices(tmp_path, monkeypatch):
    (code, data, issues, *_), order, scripts = _confirmed(tmp_path, monkeypatch)
    assert code != 0 and scripts == []
    assert "flash.atoc-replacement-unacknowledged" in _codes(issues)
    assert "This ATOC names: DEVICE, m55_he, m55_hp." in data["entries"][0]["message"]
    assert "sign" not in order


def test_confirmed_run_preflights_before_signing_then_writes_once(tmp_path, monkeypatch):
    (code, data, issues, *_), order, scripts = _confirmed(
        tmp_path, monkeypatch, atoc_unqueryable=True
    )
    assert code == 0, data
    assert order == ["preflight", "sign", "write"]
    assert len(scripts) == 1 and scripts[0].count("loadbin") == 3
    assert [e["status"] for e in data["entries"]] == ["ok", "ok"]


def test_readback_covers_every_app_and_the_package(tmp_path, monkeypatch):
    (_code, data, _i, *_), _order, scripts = _confirmed(
        tmp_path, monkeypatch, atoc_unqueryable=True, readback=True
    )
    assert scripts[0].count("savebin") == 3
    assert sum(1 for l in scripts[0].splitlines() if l.startswith("savebin") and "0x802b0000" in l) == 1
