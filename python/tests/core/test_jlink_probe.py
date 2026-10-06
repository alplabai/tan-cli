# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: pure probe-selection decisions and the read-only enumerator.
No hardware, no real sysfs."""
from __future__ import annotations

from tan.core.jlink_probe import (
    JLinkProbe,
    enumerate_jlinks,
    is_valid_usb_path,
    isolation_asserted,
    resolve_probe_selection,
)

SHARED = "000603000869"
A = JLinkProbe("3-4.1", SHARED)
B = JLinkProbe("3-4.2", SHARED)
C = JLinkProbe("3-4.3", "000999000001")


def _sel(probes, *, serial=None, path=None, manifest=None, isolated=False):
    return resolve_probe_selection(
        probes, cli_serial=serial, cli_usb_path=path, manifest_serial=manifest,
        isolated=isolated,
    )


def test_usb_path_with_unique_serial_selects_that_serial():
    sel = _sel([A, C], path="3-4.3")
    assert sel.refusal_code is None
    assert (sel.serial, sel.usb_path, sel.source) == ("000999000001", "3-4.3", "cli-usb-path")


def test_usb_path_with_shared_serial_refuses_naming_the_collision():
    sel = _sel([A, B, C], path="3-4.2")
    assert sel.refusal_code == "ambiguous"
    assert "3-4.1" in sel.refusal and "3-4.2" in sel.refusal
    assert SHARED in sel.refusal
    assert sel.candidates == ("3-4.1", "3-4.2")


def test_usb_path_under_a_masking_wrapper_one_probe_visible_is_unambiguous():
    sel = _sel([B], path="3-4.2")
    assert sel.refusal_code is None
    assert sel.serial == SHARED


def test_usb_path_with_shared_serial_passes_only_when_isolation_is_asserted():
    sel = _sel([A, B], path="3-4.2", isolated=True)
    assert sel.refusal_code is None
    assert sel.serial == SHARED
    assert sel.as_dict()["isolationAsserted"] is True
    # ...but never for a path that is not there.
    assert _sel([A, B], path="3-4.9", isolated=True).refusal_code == "not-found"


def test_usb_path_not_visible_is_not_found():
    sel = _sel([A, C], path="9-9")
    assert sel.refusal_code == "not-found"
    assert "3-4.1" in sel.refusal


def test_usb_path_on_a_host_that_cannot_enumerate_refuses():
    assert _sel(None, path="3-4.2").refusal_code == "not-found"


def test_usb_path_probe_with_unreadable_serial_refuses():
    assert _sel([JLinkProbe("3-4.1", None)], path="3-4.1").refusal_code == "not-found"


def test_conflicting_serial_and_path_refuse():
    sel = _sel([A, C], path="3-4.3", serial=SHARED)
    assert sel.refusal_code == "selector-conflict"


def test_matching_serial_and_path_are_accepted():
    sel = _sel([A, C], path="3-4.3", serial="000999000001")
    assert sel.refusal_code is None and sel.usb_path == "3-4.3"


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


def test_serial_shared_by_several_probes_refuses():
    for kwargs in ({"serial": SHARED}, {"manifest": SHARED}):
        sel = _sel([A, B, C], **kwargs)
        assert sel.refusal_code == "ambiguous", kwargs
        assert sel.candidates == ("3-4.1", "3-4.2")


def test_unique_serial_selects_and_reports_its_path():
    sel = _sel([A, B, C], serial="000999000001")
    assert sel.refusal_code is None and sel.usb_path == "3-4.3"


def test_cli_serial_matching_nothing_is_not_found_but_manifest_serial_is_left_alone():
    assert _sel([A, C], serial="nope").refusal_code == "not-found"
    # Legacy behaviour kept: a manifest serial that matches nothing is J-Link's
    # to report, not a tan refusal (no wrong board can be reached by it).
    assert _sel([A, C], manifest="nope").refusal_code is None


def test_serial_on_a_host_that_cannot_enumerate_is_passed_through():
    sel = _sel(None, serial=SHARED)
    assert sel.refusal_code is None and sel.serial == SHARED and sel.visible is None


def test_several_probes_and_no_selector_refuses():
    sel = _sel([A, C])
    assert sel.refusal_code == "ambiguous"
    assert "nothing selects one" in sel.refusal


def test_no_selector_one_probe_is_sole_visible_and_pins_nothing():
    sel = _sel([B])
    assert sel.refusal_code is None
    assert (sel.source, sel.serial, sel.usb_path) == ("sole-visible", None, "3-4.2")


def test_no_selector_no_probe_is_unpinned():
    for probes in ([], None):
        sel = _sel(probes)
        assert sel.refusal_code is None and sel.source == "unpinned"


def test_usb_path_syntax():
    assert all(is_valid_usb_path(p) for p in ("3-4", "3-4.2", "1-1.4.3"))
    assert not any(is_valid_usb_path(p) for p in ("", "3", "3-4:1.0", "usb3", "3-4.", "../x"))


def test_isolation_env():
    assert isolation_asserted({"TAN_PROBE_ISOLATED": "1"})
    assert not isolation_asserted({"TAN_PROBE_ISOLATED": "0"})
    assert not isolation_asserted({})


def _dev(root, name, vid, serial=None):
    d = root / name
    d.mkdir()
    (d / "idVendor").write_text(vid + "\n")
    if serial is not None:
        (d / "serial").write_text(serial + "\n")


def test_enumerate_reads_only_segger_devices_on_ports(tmp_path):
    _dev(tmp_path, "3-4.2", "1366", SHARED)
    _dev(tmp_path, "3-4.1", "1366", SHARED)
    _dev(tmp_path, "3-4.5", "1366")  # no serial attribute
    _dev(tmp_path, "3-1", "0bda", "X")  # not a J-Link
    _dev(tmp_path, "3-4.2:1.0", "1366", SHARED)  # an interface, not a device
    (tmp_path / "usb3").mkdir()  # a root hub
    got = enumerate_jlinks(str(tmp_path))
    assert got == [
        JLinkProbe("3-4.1", SHARED),
        JLinkProbe("3-4.2", SHARED),
        JLinkProbe("3-4.5", None),
    ]


def test_enumerate_without_sysfs_is_none_not_empty(tmp_path):
    assert enumerate_jlinks(str(tmp_path / "missing")) is None
    assert enumerate_jlinks(str(tmp_path)) == []
