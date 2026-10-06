# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1320: the manifest records the flash method `tan flash` will
actually dispatch, beside the declared one."""
from __future__ import annotations

from tan.core.flash_method_resolution import RESOLVED_KEY, annotate_resolved_flash_methods
from tan.core.flash_plan import select_flash_method, FlashTarget


def _raw():
    return {
        "slices": [
            # The measured E1M-AEN803 case: declared west, resolved Flow D.
            {"core_id": "m55_he", "os": "zephyr", "flash_method": "zephyr_west_flash",
             "flash_args": {"jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}},
            # No part-number profile: stays Flow A.
            {"core_id": "m55_hp", "os": "zephyr", "flash_method": "zephyr_west_flash",
             "flash_args": {}},
            {"core_id": "a32", "os": "linux", "flash_method": "TBD", "flash_args": {}},
            {"core_id": "off", "os": "off"},
        ],
        "helper_mcus": [
            {"name": "gd32", "chip": "GD32G553", "flash_method": "xspi_flashwriter",
             "flash_args": None},
            {"name": "cc35", "chip": "CC3501E"},
        ],
    }


def test_a_zephyr_slice_with_a_jlink_flash_device_resolves_to_flow_d():
    raw = _raw()
    annotate_resolved_flash_methods(raw)
    he, hp, a32, off = raw["slices"]
    assert he["flash_method"] == "zephyr_west_flash"  # declared, untouched
    assert he[RESOLVED_KEY] == "alif_mram_jlink"
    assert hp[RESOLVED_KEY] == "zephyr_west_flash"
    # The `TBD` sentinel alp-sdk-vscode matches on is never rewritten or resolved.
    assert a32["flash_method"] == "TBD" and RESOLVED_KEY not in a32
    assert RESOLVED_KEY not in off


def test_helpers_are_annotated_and_methodless_entries_are_left_alone():
    raw = _raw()
    annotate_resolved_flash_methods(raw)
    gd32, cc35 = raw["helper_mcus"]
    assert gd32[RESOLVED_KEY] == "xspi_flashwriter"
    assert RESOLVED_KEY not in cc35


def test_the_resolution_is_the_one_tan_flash_dispatches():
    raw = _raw()
    annotate_resolved_flash_methods(raw)
    for entry in raw["slices"][:2]:
        target = FlashTarget(
            kind="slice", id=entry["core_id"], flash_method=entry["flash_method"],
            flash_args=entry["flash_args"],
        )
        assert entry[RESOLVED_KEY] == select_flash_method(target)


def test_a_malformed_document_never_raises():
    for raw in ({}, {"slices": None}, {"slices": [1, "x"]}, {"helper_mcus": "no"}):
        annotate_resolved_flash_methods(raw)
