# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1322: the specifics appended to the whole-ATOC acknowledgement."""
from __future__ import annotations

import pytest

from tan.core.atoc_replacement import replacement_detail, resident_entries, written_entries
from tan.core.flash_plan import FlashPlanError


def test_the_written_entries_lead_with_device():
    assert written_entries("m55_he", device_config=True) == ("DEVICE", "m55_he")
    assert written_entries("m55_he", device_config=False) == ("m55_he",)


def test_an_unknown_resident_table_is_said_to_be_unknown_not_empty():
    text = replacement_detail(("DEVICE", "m55_he"), None, device_config=True)
    assert "This ATOC names: DEVICE, m55_he." in text
    assert "UNKNOWN" in text
    assert "resident_atoc_entries" in text
    assert "NOT be rewritten" not in text


def test_a_known_resident_table_lists_exactly_the_entries_that_are_lost():
    text = replacement_detail(
        ("DEVICE", "m55_he"), ("DEVICE", "ALP-HE", "HP-OWNER", "m55_he"), device_config=True
    )
    assert "NOT be rewritten (delisted): ALP-HE, HP-OWNER." in text
    none = replacement_detail(("DEVICE", "m55_he"), ("DEVICE", "m55_he"), device_config=True)
    assert "NOT be rewritten (delisted): none." in none


def test_opting_out_of_the_device_entry_says_the_resident_one_is_deleted():
    text = replacement_detail(("m55_he",), None, device_config=False)
    assert "NO DEVICE entry (--no-device-config)" in text
    assert "resident DEVICE entry" in text and "is deleted" in text


def test_resident_entries_reads_a_list_and_refuses_a_malformed_one():
    assert resident_entries({}) is None
    assert resident_entries(None) is None
    assert resident_entries({"resident_atoc_entries": [" DEVICE ", "ALP-HE"]}) == (
        "DEVICE",
        "ALP-HE",
    )
    for bad in (None, "DEVICE", [1], [""], {"a": 1}):
        with pytest.raises(FlashPlanError):
            resident_entries({"resident_atoc_entries": bad})
