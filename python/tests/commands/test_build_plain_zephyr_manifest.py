# SPDX-License-Identifier: Apache-2.0
"""``tan build --board`` writes a minimal ``system-manifest.yaml`` (tan-cli#1370).

`tan flash` / `tan size` / `tan image` read that manifest, and a board.yaml-less
build has nothing for the planner to project it from, so tan writes the slice
entry itself: planner core id (from the SoC JSON, not the Zephyr cluster name),
artefact paths, status and the SW-DP preflight pair a planned slice carries.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

from tan.core.plain_zephyr_manifest import minimal_manifest, target_facts
from tan.core.system_manifest import parse_system_manifest
from tests.commands.test_build_plain_zephyr_example import BOARD, _run, posix_only, world  # noqa: F401
from tests.commands.test_build_command import envelope_of

SOC = {
    "cores": [
        {"id": "m55_hp", "zephyr_cpucluster": "rtss_hp"},
        {"id": "m55_he", "zephyr_cpucluster": "rtss_he"},
    ],
    "variants": [
        {
            "order_code": "AE822FA0E5597LS0",
            "debug": {
                "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"},
                "jlink_flash_device": "AE822FA0E5597LS0_M55_HE",
                "expect_dpidr": "0x4C013477",
            },
        }
    ],
}


def test_target_maps_cluster_to_planner_core_id_and_preflight_pair():
    facts = target_facts(BOARD, "rtss_he", [SOC])
    assert facts.core_id == "m55_he" and facts.silicon == "AE822FA0E5597LS0"
    # The pair only: `jlink_flash_device` would arm Flow D without its address.
    assert facts.flash_args == {"expect_dpidr": "0x4C013477", "jlink_device": "Cortex-M55"}
    hp = target_facts(BOARD.replace("rtss_he", "rtss_hp"), "rtss_hp", [SOC])
    assert hp.core_id == "m55_hp"


@pytest.mark.parametrize("board", ["native_sim", "b/other0/rtss_he", "b/ae822fa0e5597ls0/cm33"])
def test_unmatched_target_keeps_slice_id_and_no_flash_profile(board):
    facts = target_facts(board, "slice_id", [SOC])
    assert (facts.core_id, facts.flash_args, facts.silicon) == ("slice_id", None, None)


def test_half_a_preflight_pair_is_not_emitted():
    soc = json.loads(json.dumps(SOC))
    del soc["variants"][0]["debug"]["expect_dpidr"]
    assert target_facts(BOARD, "rtss_he", [soc]).flash_args == {}
    raw, _ = minimal_manifest([("rtss_he", BOARD)], None)
    assert "flash_args" not in raw["slices"][0]  # no SDK metadata at all


@posix_only
def test_build_writes_manifest_flash_and_size_can_read(world):  # noqa: F811
    soc_dir = world["sdk"] / "metadata" / "socs" / "alif" / "ensemble"
    soc_dir.mkdir(parents=True)
    (soc_dir / "e8.json").write_text(json.dumps(SOC), encoding="utf-8")
    elf = world["out"] / "build" / "rtss_he-zephyr" / "build" / "zephyr" / "zephyr.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    proc = _run(world, "--board", BOARD)
    env = envelope_of(proc)
    assert env["ok"] is True, env
    assert "build.manifest-write-failed" not in [i["code"] for i in env["issues"]]
    assert "skipped writing system-manifest" not in proc.stderr
    text = (world["out"] / "build" / "system-manifest.yaml").read_text(encoding="utf-8")
    manifest = parse_system_manifest(text)  # the reader flash/size/image share
    (entry,) = manifest.slices
    assert entry["core_id"] == "m55_he" and entry["os"] == "zephyr"
    assert entry["board"] == BOARD and entry["status"] == "ok"
    assert entry["flash_method"] == "zephyr_west_flash"
    assert entry["output_artefact"].endswith("build/rtss_he-zephyr/build/zephyr/zephyr.elf")
    assert entry["build_dir"].endswith("build/rtss_he-zephyr/build")
    raw = yaml.safe_load(text)
    assert raw["slices"][0]["flash_args"] == {
        "expect_dpidr": "0x4C013477",
        "jlink_device": "Cortex-M55",
    }
    assert raw["hw_info"]["silicon"] == "AE822FA0E5597LS0"


@posix_only
def test_failed_slice_is_recorded_failed_without_artefact(world):  # noqa: F811
    env = envelope_of(_run(world, "--board", BOARD, env={"FAKE_WEST": "--fail"}))
    assert env["ok"] is False
    raw = yaml.safe_load((world["out"] / "build" / "system-manifest.yaml").read_text())
    assert raw["slices"][0]["status"] == "failed"
    assert "output_artefact" not in raw["slices"][0]
