# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s `toolchain` check (issue #474, ADR 0021 Lane 1 P1) --
stamp-vs-pin, never directory-exists. Hermetic: no subprocess, no network;
`tan.core.toolchain_provision.stamp_matches_pin` is the only verdict this
check consults, exercised here through real files on disk under `tmp_path`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tan.commands import doctor_cmd
from tan.core import toolchain_provision as tp

#: `_zephyr_sdk_root_valid` (`doctor_cmd.py`) looks for THIS exact name --
#: `arm-zephyr-eabi-gcc.exe` on Windows, `arm-zephyr-eabi-gcc` everywhere
#: else. A fixture that always plants the POSIX name reports a false `fail`
#: on Windows CI (tan-cli#990 review's own blocker-fix test did exactly
#: this on its first real CI run) -- not because the adoption path is
#: broken, but because the fixture's "host toolchain" was never valid on
#: that platform to begin with.
_GCC_NAME = "arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc"

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


def test_a_stamp_for_a_moved_pin_is_a_fail_even_with_a_real_compiler_in_the_store(
    tmp_path, monkeypatch
):
    """tan-cli#1186 review regression: widening `_zephyr_sdk_scan_roots` to
    also cover the ADR 0021 store (so `zephyrSdk` stops missing a bootstrapped
    toolchain) made `_host_toolchain_matching_pin`'s digest-blind
    version-string adoption path reachable for an entry INSIDE tan's own
    store -- one `_zephyr_sdk_detected_root` can now return directly. A store
    directory can have a real compiler and a `sdk_version` marker matching
    the pin's nominal version while its OWN stamp names a different digest
    (the pin moved without the version string changing); the adoption path
    must not let the version-string match preempt that stamp-based fail."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    manifest = tp.parse_toolchain_manifest(MANIFEST)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    (store_dir / "gnu" / "arm-zephyr-eabi" / "bin").mkdir(parents=True)
    gcc = store_dir / "gnu" / "arm-zephyr-eabi" / "bin" / _GCC_NAME
    gcc.write_text("#!/bin/sh\necho fake gcc\n", encoding="utf-8")
    gcc.chmod(0o755)
    (store_dir / "sdk_version").write_text(f"{manifest.version}\n", encoding="utf-8")
    stale_stamp = tp.ToolchainStamp(manifest.version, "not-the-current-digest", "old-triple")
    (store_dir / tp.STAMP_FILENAME).write_text(tp.render_stamp(stale_stamp), encoding="utf-8")

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "fail"
    assert "different pin" in check.detail


def test_a_host_toolchain_at_the_pinned_version_is_a_pass_not_a_fail(tmp_path, monkeypatch):
    """tan-cli#990 review, BLOCKER: a host with a real, working, correctly
    pinned toolchain at a NON-tan path (`~/zephyr-sdk-1.0.1/`, the documented
    `west sdk install` layout) must not get a `toolchain` FAIL whose only
    prescribed remedy re-downloads a copy it already has. No stamp is ever
    written here -- tan never installed this one -- so this is the
    `_host_toolchain_matching_pin` adoption path, not `stamp_matches_pin`.
    """
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    home = tmp_path / "home"
    host_sdk = home / "zephyr-sdk-1.0.1"
    (host_sdk / "gnu" / "arm-zephyr-eabi" / "bin").mkdir(parents=True)
    gcc = host_sdk / "gnu" / "arm-zephyr-eabi" / "bin" / _GCC_NAME
    gcc.write_text("#!/bin/sh\necho fake gcc\n", encoding="utf-8")
    gcc.chmod(0o755)
    (host_sdk / "sdk_version").write_text("1.0.1\n", encoding="utf-8")
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(host_sdk))

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "pass"
    assert "1.0.1" in check.detail
    assert str(host_sdk) in check.detail
    # No tan-written stamp exists anywhere -- this is the host-scan path,
    # not `stamp_matches_pin`, that produced the pass.
    manifest = tp.parse_toolchain_manifest(MANIFEST)
    store_dir = home / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    assert not (store_dir / tp.STAMP_FILENAME).exists()


def test_a_host_toolchain_under_an_alp_toolchain_root_ancestor_still_passes(
    tmp_path, monkeypatch
):
    """ADR 0021's own escape hatch (`$ALP_TOOLCHAIN_ROOT` pointed at a
    bench/CI ancestor like `$HOME` or `/opt`) must not turn
    `_host_toolchain_matching_pin`'s "is this entry inside tan's OWN store"
    guard into "is this entry anywhere under the override root at all".
    `resolve_toolchain_root` takes an env override VERBATIM as the store
    root (no `/toolchains` suffix), so with the override set to `$HOME` a
    hand-installed `$HOME/zephyr-sdk-1.0.1/` sits directly inside that
    root -- and a guard keyed on the whole root, rather than tan's own
    per-version store leaf, misreads a legitimate adopted host toolchain as
    "tan's own store" and refuses to adopt it, turning a previously-passing
    configuration into a `toolchain` FAIL."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(home))
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    host_sdk = home / "zephyr-sdk-1.0.1"
    (host_sdk / "gnu" / "arm-zephyr-eabi" / "bin").mkdir(parents=True)
    gcc = host_sdk / "gnu" / "arm-zephyr-eabi" / "bin" / _GCC_NAME
    gcc.write_text("#!/bin/sh\necho fake gcc\n", encoding="utf-8")
    gcc.chmod(0o755)
    (host_sdk / "sdk_version").write_text("1.0.1\n", encoding="utf-8")

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "pass"
    assert "1.0.1" in check.detail
    assert str(host_sdk) in check.detail


def test_a_host_toolchain_at_the_wrong_version_still_fails(tmp_path, monkeypatch):
    """The adoption path is version-specific, not "any toolchain present":
    a host SDK whose `sdk_version` does NOT match the pin must not silently
    excuse the check."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _sdk_with_manifest(tmp_path, MANIFEST)
    home = tmp_path / "home"
    host_sdk = home / "zephyr-sdk-0.16.5"
    (host_sdk / "gnu" / "arm-zephyr-eabi" / "bin").mkdir(parents=True)
    gcc = host_sdk / "gnu" / "arm-zephyr-eabi" / "bin" / _GCC_NAME
    gcc.write_text("#!/bin/sh\necho fake gcc\n", encoding="utf-8")
    gcc.chmod(0o755)
    (host_sdk / "sdk_version").write_text("0.16.5\n", encoding="utf-8")
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(host_sdk))

    check = doctor_cmd.toolchain_check(sdk_root)
    assert check.status == "fail"
    assert check.fix == "tan bootstrap"


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
