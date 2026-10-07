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

from tan.core.flash_plan import FlashTarget, select_flash_method
from tan.core.plain_zephyr_manifest import PlainSlice, minimal_manifest, target_facts
from tan.core.system_manifest import parse_system_manifest
from tests.commands.test_build_plain_zephyr_example import BOARD, _run, posix_only, world  # noqa: F401
from tests.commands.test_build_command import envelope_of
from tests.commands.test_flash_command import envelope as flash_envelope
from tests.commands.test_flash_command import run_flash
from tests.commands.test_size_command import envelope as size_envelope
from tests.commands.test_size_command import run_cli as run_size

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


def _soc(core_ids, order_code, *, cluster="rtss_he"):
    soc = json.loads(json.dumps(SOC))
    soc["cores"] = [{"id": c, "zephyr_cpucluster": cluster} for c in core_ids]
    soc["variants"][0]["order_code"] = order_code
    soc["variants"][0]["debug"]["jlink_device"] = {c: "Cortex-M55" for c in core_ids}
    return soc


@pytest.mark.parametrize(
    ("socs", "expected"),
    [
        # Two SoC docs share `rtss_he`; only the second owns the variant.
        ([_soc(["other_he"], "OTHER0"), _soc(["m55_he"], "AE822FA0E5597LS0")], "m55_he"),
        # The core matches but the variant belongs to a different part: no match.
        ([_soc(["other_he"], "OTHER0")], "rtss_he"),
        # Both docs match; the first (path order) wins, deterministically.
        ([_soc(["m55_he"], "AE822FA0E5597LS0"), _soc(["x_he"], "AE822FA0E5597LS0")], "m55_he"),
    ],
)
def test_shared_cluster_names_resolve_by_variant(socs, expected):
    assert target_facts(BOARD, "rtss_he", socs).core_id == expected


def test_half_a_preflight_pair_is_not_emitted():
    soc = json.loads(json.dumps(SOC))
    del soc["variants"][0]["debug"]["expect_dpidr"]
    assert target_facts(BOARD, "rtss_he", [soc]).flash_args == {}
    raw, _ = minimal_manifest([PlainSlice("rtss_he", BOARD, "build/rtss_he-zephyr")], None, "/b")
    assert "flash_args" not in raw["slices"][0]  # no SDK metadata at all


def _built_manifest(world):  # noqa: F811
    soc_dir = world["sdk"] / "metadata" / "socs" / "alif" / "ensemble"
    soc_dir.mkdir(parents=True)
    (soc_dir / "e8.json").write_text(json.dumps(SOC), encoding="utf-8")
    nested = world["out"] / "build" / "rtss_he-zephyr" / "build"
    (nested / "zephyr").mkdir(parents=True)
    (nested / "zephyr" / "zephyr.elf").write_bytes(b"\x7fELF")
    # `tan size` measures rom.json/ram.json when the bytes are not a real ELF.
    (nested / "rom.json").write_text('{"symbols":{"size":4096}}', encoding="utf-8")
    (nested / "ram.json").write_text('{"symbols":{"size":2048}}', encoding="utf-8")
    proc = _run(world, "--board", BOARD)
    return proc, envelope_of(proc)


@posix_only
def test_build_writes_manifest_that_size_and_flash_consume(world):  # noqa: F811
    proc, env = _built_manifest(world)
    assert env["ok"] is True, env
    assert "build.manifest-write-failed" not in [i["code"] for i in env["issues"]]
    assert "skipped writing system-manifest" not in proc.stderr
    path = world["out"] / "build" / "system-manifest.yaml"
    text = path.read_text(encoding="utf-8")
    (entry,) = parse_system_manifest(text).slices  # the reader flash/size/image share
    assert entry["core_id"] == "m55_he" and entry["os"] == "zephyr"
    assert entry["board"] == BOARD and entry["status"] == "ok"
    assert entry["output_artefact"].endswith("build/rtss_he-zephyr/build/zephyr/zephyr.elf")
    assert entry["build_dir"].endswith("build/rtss_he-zephyr/build")
    raw = yaml.safe_load(text)
    assert raw["slices"][0]["flash_args"] == {
        "expect_dpidr": "0x4C013477",
        "jlink_device": "Cortex-M55",
    }
    assert raw["hw_info"]["silicon"] == "AE822FA0E5597LS0"

    # `tan size` finds the slice's tree through the recorded build_dir.
    sized = run_size(
        world["out"], "--format", "json", "--build-root", str(path.parent),
        "--sdk-root", str(world["sdk"]),
    )
    row = size_envelope(sized)["data"]["slices"][0]
    # Measured, but no SoM names a budget for a bare board target.
    assert row["core_id"] == "m55_he" and row["status"] == "no-budget", row
    assert row["flash"]["used"] == 4096 and row["ram"]["used"] == 2048

    # `tan flash --dry-run` plans the slice as Flow A on the planner core id.
    code, out, _ = run_flash(world["out"], "--format", "json", "--dry-run", manifest=text)
    flashed = flash_envelope(out)
    (flash_entry,) = flashed["data"]["entries"]
    assert code == 0 and flash_entry["id"] == "m55_he", flashed
    assert flash_entry["method"] == "zephyr_west_flash", flash_entry
    assert flash_entry["message"].startswith("would run west flash --build-dir "), flash_entry
    assert flash_entry["message"].endswith("build/rtss_he-zephyr/build"), flash_entry


def test_selected_method_is_flow_a_not_flow_d():
    raw, _ = minimal_manifest(
        [PlainSlice("rtss_he", BOARD, "build/rtss_he-zephyr")], None, "/b"
    )
    # No SDK metadata -> no args; and with it, still no Flow D keys.
    assert raw["slices"][0]["flash_method"] == "zephyr_west_flash"
    args = target_facts(BOARD, "rtss_he", [SOC]).flash_args
    target = FlashTarget(
        kind="slice", id="m55_he", flash_method="zephyr_west_flash", flash_args=args,
        output_artefact="a.elf",
    )
    assert select_flash_method(target) == "zephyr_west_flash"


@posix_only
def test_failed_slice_is_recorded_failed_without_artefact(world):  # noqa: F811
    env = envelope_of(_run(world, "--board", BOARD, env={"FAKE_WEST": "--fail"}))
    assert env["ok"] is False
    raw = yaml.safe_load((world["out"] / "build" / "system-manifest.yaml").read_text())
    assert raw["slices"][0]["status"] == "failed"
    assert "output_artefact" not in raw["slices"][0]
    # The slice's own west tree, not `<core>-zephyr` named after the renamed core.
    assert raw["slices"][0]["build_dir"].endswith("build/rtss_he-zephyr/build")
