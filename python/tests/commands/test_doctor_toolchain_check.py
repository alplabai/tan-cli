# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s `toolchain` check (issue #474, ADR 0021 Lane 1 P1) --
stamp-vs-pin, never directory-exists. Hermetic: no subprocess, no network;
`tan.core.toolchain_provision.stamp_matches_pin` is the only verdict this
check consults, exercised here through real files on disk under `tmp_path`.
"""
from __future__ import annotations

import json
from pathlib import Path

from tan.commands import doctor_cmd
from tan.core import toolchain_provision as tp

MANIFEST = json.dumps(
    {
        "zephyrSdk": {
            "version": "1.0.1",
            "baseUrl": "https://example.invalid/",
            "artifacts": [
                {
                    "host": "linux-x86_64", "component": "minimal-sdk",
                    "filename": "x.tar.xz", "sizeBytes": 1, "sha256": "a" * 64,
                }
            ],
        }
    }
)


def _sdk_with_manifest(tmp_path: Path, manifest_text: str | None) -> str:
    sdk = tmp_path / "alp-sdk"
    (sdk / "metadata").mkdir(parents=True)
    if manifest_text is not None:
        (sdk / "metadata" / "toolchains.json").write_text(manifest_text, encoding="utf-8")
    return str(sdk)


def _point_home_at(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ALP_TOOLCHAIN_ROOT", raising=False)


def test_no_sdk_root_is_unknown_not_a_guess(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    check = doctor_cmd.toolchain_check(None)
    assert check.status == "unknown"
    assert check.name == "toolchain"


def test_a_missing_manifest_is_unknown_and_names_the_file(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, None)
    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "unknown"
    assert "toolchains.json" in check.detail


def test_no_toolchain_installed_at_all_is_a_fail_with_the_bootstrap_fix(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "fail"
    assert check.fix == "tan bootstrap"


def test_a_verified_stamp_matching_the_pin_is_a_pass(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    manifest = tp.parse_toolchain_manifest(MANIFEST)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    store_dir.mkdir(parents=True)
    stamp = tp.ToolchainStamp(manifest.version, manifest.digest(), "arm-zephyr-eabi-gcc 14.3.0")
    (store_dir / tp.STAMP_FILENAME).write_text(tp.render_stamp(stamp), encoding="utf-8")

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "pass"
    assert manifest.version in check.detail


def test_a_stamp_for_a_moved_pin_is_a_fail_not_a_pass_version_skew_masquerading_as_health(
    tmp_path, monkeypatch
):
    """ADR 0021's own words: 'a stamped 1.0.1 store against a moved pin is a
    Fail with a fix, not "a toolchain exists"'."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    manifest = tp.parse_toolchain_manifest(MANIFEST)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    store_dir.mkdir(parents=True)
    stale_stamp = tp.ToolchainStamp(manifest.version, "not-the-current-digest", "old-triple")
    (store_dir / tp.STAMP_FILENAME).write_text(tp.render_stamp(stale_stamp), encoding="utf-8")

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "fail"
    assert "different pin" in check.detail


def test_doctor_and_bootstrap_agree_by_construction_same_verdict_function(tmp_path, monkeypatch):
    """The point of importing `stamp_matches_pin` rather than re-deriving it:
    feed the SAME (stamp, manifest) pair to both call sites and they cannot
    disagree."""
    _point_home_at(monkeypatch, tmp_path)
    manifest = tp.parse_toolchain_manifest(MANIFEST)
    stamp = tp.ToolchainStamp(manifest.version, manifest.digest(), "triple")
    from tan.commands import bootstrap_cmd

    doctor_verdict = tp.stamp_matches_pin(stamp, manifest)
    bootstrap_verdict = tp.stamp_matches_pin(stamp, manifest)
    assert doctor_verdict is bootstrap_verdict is True
    # Both modules reach the identical function object, not two copies.
    assert doctor_cmd.toolchain_provision.stamp_matches_pin is bootstrap_cmd.toolchain_provision.stamp_matches_pin
