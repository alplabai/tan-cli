# SPDX-License-Identifier: Apache-2.0
"""`tan.core.image_bundle`: the bundle shape, the forward-slash artefact paths,
and the path guard that keeps a hand-edited manifest from writing outside the
bundle."""
import json
import os

import pytest

from tan.core.image_bundle import (
    assemble_bundle_manifest,
    helper_artefact_rel,
    helper_entry,
    is_plain_relative,
    slice_archive_name,
    slice_artefact_rel,
    slice_entry,
    slice_should_bundle,
)


def test_archive_name_and_artefact_are_forward_slash_always():
    assert slice_archive_name("m55_hp", "zephyr") == "m55_hp-zephyr.tar.gz"
    assert slice_artefact_rel("m55_hp", "zephyr") == "slices/m55_hp-zephyr.tar.gz"
    assert "\\" not in slice_artefact_rel("m55_hp", "zephyr")
    assert helper_artefact_rel("gd32_bridge.bin") == "helper-mcus/gd32_bridge.bin"


@pytest.mark.parametrize(
    "core_id",
    ["../../../../home/u/x", "/etc/passwd", "..", ".", "", "./a", "a/..", "//"],
)
def test_archive_name_rejects_every_shape_that_escapes_a_joined_base(core_id):
    # Without this the caller's unlink + create could clobber an arbitrary
    # `*.tar.gz` outside the build tree. The guard lives in the shared module so
    # `slice_artefact_rel` cannot disagree with it.
    assert slice_archive_name(core_id, "zephyr") is None
    assert slice_artefact_rel(core_id, "zephyr") is None


def test_archive_name_rejects_an_unsafe_os_field_too():
    assert slice_archive_name("m55_hp", "../escape") is None
    assert slice_archive_name("m55_hp", "") is None


def test_archive_name_accepts_ordinary_identifiers_and_an_inner_dot():
    assert slice_archive_name("m55_hp", "zephyr") is not None
    # `.` is normalized away except at the START of a path, matching
    # `Path::components()`, so `a/./b` is plain and `./a` is not.
    assert is_plain_relative("a/./b")
    assert not is_plain_relative("./a")


@pytest.mark.skipif(os.name != "nt", reason="Windows path shapes")
def test_guard_rejects_windows_rooted_and_drive_relative_shapes():
    # `\\x` and `C:x` are NOT `isabs()` and carry no `..` -- exactly the shapes an
    # `isabs() or has-parent-dir` guard lets through while still escaping a join.
    assert slice_archive_name(r"\windows\system32", "zephyr") is None
    assert slice_archive_name("C:foo", "zephyr") is None
    assert slice_archive_name("C:/foo", "zephyr") is None
    assert not is_plain_relative(r"a\..\b")


def test_only_ok_slices_bundle():
    assert slice_should_bundle("ok")
    for status in ("pending", "failed", "skipped", "", None, 1, "OK"):
        assert not slice_should_bundle(status)


def test_manifest_serializes_in_the_exact_key_order():
    document = json.dumps(assemble_bundle_manifest({}, [], [], []))
    assert document == (
        '{"schema_version": 1, "generated_by": "tan image", "hw_info": {}, '
        '"slices": [], "helper_mcus": [], "boot_order": []}'
    )


def test_empty_collections_stay_present_arrays_never_null():
    bundle = assemble_bundle_manifest({}, [], [], [])
    assert bundle["slices"] == []
    assert bundle["helper_mcus"] == []


def test_entry_key_order_and_the_chip_field_not_role():
    entry = helper_entry(None, "cc3501e", helper_artefact_rel("fw.bin"), "deadbeef", 4)
    assert json.dumps(entry) == (
        '{"name": null, "chip": "cc3501e", "artefact": "helper-mcus/fw.bin", '
        '"sha256": "deadbeef", "size": 4}'
    )
    assert list(slice_entry("c", "zephyr", "slices/x.tar.gz", "ab", 1)) == [
        "core_id",
        "os",
        "artefact",
        "sha256",
        "size",
    ]


def test_hw_info_and_boot_order_are_carried_verbatim():
    bundle = assemble_bundle_manifest(
        {"sku": "E1M-AEN701", "eeprom": {"magic": 165}}, ["m55_hp"], [], []
    )
    assert bundle["hw_info"]["eeprom"] == {"magic": 165}
    assert bundle["boot_order"] == ["m55_hp"]
