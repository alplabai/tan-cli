# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: pure probe-selection decisions, the spawn-time verification
helpers and the read-only enumerator. No hardware, no real sysfs."""
from __future__ import annotations

import pytest

from tan.core.jlink_probe import (
    JLinkProbe,
    ProbeEnumerationError,
    canon_serial,
    enumerate_jlinks,
    is_valid_usb_path,
    parse_show_emu_list,
    probe_set,
    resolve_probe_selection,
    snapshot_drift,
    verify_emulator_list,
)

SHARED = "000603000869"
A = JLinkProbe("3-4.1", SHARED)
B = JLinkProbe("3-4.2", SHARED)
D = JLinkProbe("3-4.4.3", SHARED)
C = JLinkProbe("3-4.3", "000999000001")


def _sel(probes, *, serial=None, path=None, manifest=None):
    return resolve_probe_selection(
        probes, cli_serial=serial, cli_usb_path=path, manifest_serial=manifest
    )


def test_canonical_serial_ignores_leading_zeros_and_whitespace():
    assert canon_serial("000603000869") == canon_serial("603000869") == "603000869"
    assert canon_serial(" 000603000869\n") == "603000869"
    assert canon_serial("0") == canon_serial("000") == "0"
    assert canon_serial("ab12") == "ab12"


def test_usb_path_with_unique_serial_selects_that_serial():
    sel = _sel([A, C], path="3-4.3")
    assert sel.refusal_code is None
    assert (sel.serial, sel.usb_path, sel.source) == ("000999000001", "3-4.3", "cli-usb-path")
    assert sel.candidates == ()


def test_usb_path_on_a_shared_serial_selects_and_lists_the_collision():
    """Selection alone cannot settle it (J-Link selects by serial only); the
    spawn-time ShowEmuList verification does. The collision is reported."""
    sel = _sel([A, B, D, C], path="3-4.2")
    assert sel.refusal_code is None
    assert sel.serial == SHARED
    assert sel.candidates == ("3-4.1", "3-4.2", "3-4.4.3")
    assert "shared by 3-4.1, 3-4.2, 3-4.4.3" in sel.describe()


def test_usb_path_not_visible_is_not_found():
    sel = _sel([A, C], path="9-9")
    assert sel.refusal_code == "not-found"
    assert "3-4.1" in sel.refusal


def test_usb_path_on_a_host_that_cannot_enumerate_refuses():
    assert _sel(None, path="3-4.2").refusal_code == "not-found"


def test_conflicting_serial_and_path_refuse_and_equivalent_spellings_do_not():
    assert _sel([A, C], path="3-4.3", serial=SHARED).refusal_code == "selector-conflict"
    assert _sel([A, C], path="3-4.3", serial="999000001").refusal_code is None


def test_cli_serial_overrides_manifest_and_says_so():
    sel = _sel([A, C], serial="000999000001", manifest=SHARED)
    assert sel.refusal_code is None
    assert sel.serial == "000999000001"
    assert sel.overrides_manifest_serial == SHARED
    assert sel.as_dict()["overridesFlashArgsSerial"] == SHARED


def test_cli_usb_path_overrides_manifest_serial():
    sel = _sel([A, C], path="3-4.3", manifest=SHARED)
    assert sel.serial == "000999000001"
    assert sel.overrides_manifest_serial == SHARED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"serial": SHARED},
        {"manifest": SHARED},
        # YAML turns an unquoted 000603000869 into the int 603000869 -> "603000869".
        {"serial": "603000869"},
        {"manifest": "603000869"},
    ],
)
def test_a_serial_shared_by_several_probes_refuses_in_every_spelling(kwargs):
    sel = _sel([A, B, D], **kwargs)
    assert sel.refusal_code == "ambiguous", kwargs
    assert sel.candidates == ("3-4.1", "3-4.2", "3-4.4.3")
    assert "expect_dpidr" in sel.refusal


def test_a_unique_serial_selects_and_passes_the_sysfs_spelling_on():
    sel = _sel([A, B, C], serial="999000001")
    assert sel.refusal_code is None
    assert (sel.serial, sel.usb_path) == ("000999000001", "3-4.3")


@pytest.mark.parametrize("kwargs", [{"serial": "nope"}, {"manifest": "nope"}, {"manifest": "7"}])
def test_any_serial_matching_no_visible_probe_is_not_found(kwargs):
    """CLI or manifest alike: on an enumerable host a serial nothing carries is
    refused, never passed through to J-Link to open whatever it likes."""
    sel = _sel([A, C], **kwargs)
    assert sel.refusal_code == "not-found"


def test_a_serial_on_a_host_that_cannot_enumerate_is_passed_to_verification():
    sel = _sel(None, serial=SHARED)
    assert sel.refusal_code is None and sel.serial == SHARED and sel.visible is None


def test_several_probes_and_no_selector_refuses():
    sel = _sel([A, C])
    assert sel.refusal_code == "ambiguous"
    assert "nothing selects one" in sel.refusal


def test_no_selector_one_probe_is_sole_visible_and_pins_its_serial():
    sel = _sel([B])
    assert sel.refusal_code is None
    assert (sel.source, sel.serial, sel.usb_path) == ("sole-visible", SHARED, "3-4.2")


def test_no_selector_no_probe_is_unpinned():
    for probes in ([], None):
        sel = _sel(probes)
        assert sel.refusal_code is None and sel.source == "unpinned" and sel.serial is None


def test_usb_path_syntax():
    assert all(is_valid_usb_path(p) for p in ("3-4", "3-4.2", "1-1.4.3"))
    assert not any(is_valid_usb_path(p) for p in ("", "3", "3-4:1.0", "usb3", "3-4.", "../x"))


# ── spawn-time verification ─────────────────────────────────────────────────


def test_show_emu_list_parsing():
    text = (
        "SEGGER J-Link Commander V9.74\n"
        "J-Link[0]: Connection: USB, Serial number: 000603000869, ProductName: J-Link EDU\n"
        "J-Link[1]: Connection: USB, Serial number: 000999000001, ProductName: J-Link EDU\n"
    )
    assert parse_show_emu_list(text) == ["000603000869", "000999000001"]
    assert parse_show_emu_list("No emulators connected via USB") == []


def test_verification_wants_exactly_one_canonical_match():
    assert verify_emulator_list(SHARED, ["603000869"]) is None
    assert verify_emulator_list(SHARED, [SHARED, "000999000001"]) is None
    assert "no emulator" in verify_emulator_list(SHARED, ["000999000001"])
    assert "no emulator" in verify_emulator_list(SHARED, [])
    msg = verify_emulator_list(SHARED, [SHARED, SHARED, SHARED])
    assert "3 emulators" in msg and "expect_dpidr" in msg


def test_snapshot_drift_is_a_set_comparison_on_canonical_serials():
    snap = probe_set([A, C])
    assert snapshot_drift(snap, [C, JLinkProbe("3-4.1", "603000869")]) is None
    assert "changed" in snapshot_drift(snap, [A])
    assert "changed" in snapshot_drift(snap, [A, C, B])
    assert "unenumerable" in snapshot_drift(snap, None)
    assert snapshot_drift(None, [A]) is None


# ── enumerator ──────────────────────────────────────────────────────────────


def _dev(root, name, vid, serial=None):
    d = root / name
    d.mkdir()
    (d / "idVendor").write_text(vid + "\n")
    if serial is not None:
        (d / "serial").write_text(serial + "\n")


def test_enumerate_reads_only_segger_devices_on_ports(tmp_path):
    _dev(tmp_path, "3-4.2", "1366", SHARED)
    _dev(tmp_path, "3-4.1", "1366", SHARED)
    _dev(tmp_path, "3-4.4.3", "1366", SHARED)
    _dev(tmp_path, "3-1", "0bda", "X")  # not a J-Link
    _dev(tmp_path, "3-4.2:1.0", "1366", SHARED)  # an interface, not a device
    (tmp_path / "usb3").mkdir()  # a root hub
    assert enumerate_jlinks(str(tmp_path)) == [
        JLinkProbe("3-4.1", SHARED),
        JLinkProbe("3-4.2", SHARED),
        JLinkProbe("3-4.4.3", SHARED),
    ]


def test_an_empty_device_directory_is_a_mask_not_a_probe(tmp_path):
    _dev(tmp_path, "3-4.1", "1366", SHARED)
    (tmp_path / "3-4.2").mkdir()  # what a mount-namespace mask leaves
    assert enumerate_jlinks(str(tmp_path)) == [JLinkProbe("3-4.1", SHARED)]


def test_a_populated_directory_with_unreadable_attributes_is_an_error(tmp_path):
    (tmp_path / "3-4.2").mkdir()
    (tmp_path / "3-4.2" / "bDeviceClass").write_text("00")  # populated, no idVendor
    with pytest.raises(ProbeEnumerationError, match="idVendor"):
        enumerate_jlinks(str(tmp_path))
    other = tmp_path / "x"
    other.mkdir()
    _dev(other, "3-4.5", "1366")  # a SEGGER device with no serial
    with pytest.raises(ProbeEnumerationError, match="serial"):
        enumerate_jlinks(str(other))


def test_enumerate_without_sysfs_is_none_not_empty(tmp_path):
    assert enumerate_jlinks(str(tmp_path / "missing")) is None
    assert enumerate_jlinks(str(tmp_path)) == []


def test_conflict_echo_keeps_the_candidates():
    sel = _sel([A, B, C], path="3-4.3", serial=SHARED)
    assert sel.refusal_code == "selector-conflict"
    assert sel.candidates == ("3-4.3",)
    other = _sel([A, B, C], path="3-4.1", serial="999000001")
    assert other.refusal_code == "selector-conflict"
    assert other.candidates == ("3-4.1", "3-4.2")


def test_zero_stripped_serial_with_a_usb_path_is_the_same_probe_not_a_conflict():
    """Bench defect (d): ShowEmuList prints 603000869, sysfs 000603000869."""
    sel = _sel([A, B, D], path="3-4.2", serial="603000869")
    assert sel.refusal_code is None
    assert sel.serial == SHARED  # J-Link is given the sysfs spelling
    assert sel.candidates == ("3-4.1", "3-4.2", "3-4.4.3")
