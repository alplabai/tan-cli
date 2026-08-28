# SPDX-License-Identifier: Apache-2.0
"""`tan.core.toolchain_provision` -- ADR 0021 Lane 1 P1 (issue #474)'s pure
decision logic: host identity, the `metadata/toolchains.json` reader, the
artifact-keyed store's naming, the stamp's verdict function, and the disk
preflight. No IO, no subprocess, no network -- every case here is hermetic
and fast on purpose (the network path itself is exercised nowhere in this
file, matching this repo's own rule that CI must not download a real
toolchain on every PR).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tan.core import toolchain_provision as tp

#: The real producer output, vendored beside the consumer's own fixture --
#: never re-typed here, matching `test_bootstrap_command.py`'s own
#: `REAL_MANIFEST` convention ("a manifest fact re-spelled in a test is a
#: fact with two owners").
REAL_TOOLCHAINS_MANIFEST = (
    Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "toolchains" / "toolchains.json"
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sys_platform", "machine", "expected"),
    [
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "aarch64", "linux-aarch64"),
        ("darwin", "arm64", "macos-aarch64"),
        ("darwin", "x86_64", "macos-x86_64"),
        ("win32", "AMD64", "windows-x86_64"),
    ],
)
def test_toolchain_host_key_maps_the_hosts_west_sdk_install_actually_publishes(
    sys_platform, machine, expected
):
    assert tp.toolchain_host_key(sys_platform, machine) == expected


def test_windows_arm64_is_refused_by_name_with_the_wsl2_redirect_adr_0021_specifies():
    """ADR 0021: 'no official Zephyr SDK host build ... must be routed to
    WSL2-aarch64 with a clear message' -- not silently resolved to
    `windows-x86_64` (nothing on that host can execute those binaries)."""
    result = tp.toolchain_host_key("win32", "ARM64")
    assert isinstance(result, tp.UnsupportedHost)
    assert result.host_key == "windows-arm64"
    assert "WSL2" in result.reason


@pytest.mark.parametrize(
    ("sys_platform", "machine"),
    [("linux", "riscv64"), ("darwin", "i386"), ("win32", "ia64"), ("freebsd12", "x86_64")],
)
def test_an_unrecognised_host_is_a_coded_honest_refusal_not_a_crash(sys_platform, machine):
    result = tp.toolchain_host_key(sys_platform, machine)
    assert isinstance(result, tp.UnsupportedHost)
    assert result.reason  # never blank -- always names something


# ---------------------------------------------------------------------------
# The manifest reader
# ---------------------------------------------------------------------------


def test_the_real_manifest_parses_and_reports_the_pinned_version():
    manifest = tp.parse_toolchain_manifest(REAL_TOOLCHAINS_MANIFEST)
    assert manifest.version == "1.0.1"
    assert manifest.base_url.startswith("https://")
    assert len(manifest.artifacts) > 0
    assert manifest.extracted_bytes_whole_sdk == 2026739200


def test_intel_mac_is_a_coded_honest_skip_because_the_pinned_manifest_ships_no_row_for_it():
    """ADR 0021's named case, reproduced from the REAL manifest rather than a
    hand-built one: 'upstream ships no macos-x86_64 toolchain'. General,
    manifest-driven form -- no per-platform special-casing in the reader."""
    manifest = tp.parse_toolchain_manifest(REAL_TOOLCHAINS_MANIFEST)
    assert tp.artifacts_missing_for_host(manifest, "macos-x86_64") is True
    assert tp.artifacts_missing_for_host(manifest, "linux-x86_64") is False


@pytest.mark.parametrize(
    ("bad_text", "expected_fragment"),
    [
        ("not json at all {{{", "not valid JSON"),
        ("[]", "not a JSON object"),
        ("{}", "zephyrSdk"),
        (json.dumps({"zephyrSdk": {"version": "1.0.1"}}), "baseUrl"),
        (json.dumps({"zephyrSdk": {"version": "1.0.1", "baseUrl": "x"}}), "artifacts"),
        (
            json.dumps(
                {"zephyrSdk": {"version": "1.0.1", "baseUrl": "x", "artifacts": [{"host": "a"}]}}
            ),
            "artifacts",
        ),
    ],
)
def test_a_malformed_manifest_names_exactly_which_field_is_wrong(bad_text, expected_fragment):
    with pytest.raises(tp.ToolchainManifestError, match=expected_fragment):
        tp.parse_toolchain_manifest(bad_text)


def test_an_older_manifest_shape_with_no_measured_footprint_is_not_a_parse_error():
    """`extractedBytes` postdates the schema (2026-07-26 amendment) -- a
    manifest predating it must still parse; disk preflight is what treats
    the resulting `None` as "cannot preflight", not this function."""
    doc = {
        "zephyrSdk": {
            "version": "0.16.8",
            "baseUrl": "https://example.invalid/",
            "artifacts": [
                {
                    "host": "linux-x86_64", "component": "minimal-sdk",
                    "filename": "x.tar.xz", "sizeBytes": 1, "sha256": "a" * 64,
                }
            ],
        }
    }
    manifest = tp.parse_toolchain_manifest(json.dumps(doc))
    assert manifest.extracted_bytes_whole_sdk is None
    assert tp.required_bytes(manifest) is None


# ---------------------------------------------------------------------------
# The store's naming + root resolution
# ---------------------------------------------------------------------------


def test_store_dir_name_is_keyed_by_artifact_not_by_an_sdk_checkout():
    assert tp.store_dir_name("1.0.1") == "zephyr-sdk-1.0.1-arm-zephyr-eabi"
    assert tp.store_dir_name("0.17.0") == tp.store_dir_name("0.17.0")  # stable, pure
    assert tp.store_dir_name("1.0.1") != tp.store_dir_name("1.0.2")  # two pins, two dirs


def test_alp_toolchain_root_env_override_is_honoured_and_marked_adopted():
    resolved = tp.resolve_toolchain_root("/mnt/shared/toolchains", "/home/u/.alp")
    assert resolved.path_str == "/mnt/shared/toolchains"
    assert resolved.adopted is True


def test_a_blank_override_falls_back_to_the_default_and_is_not_adopted():
    resolved = tp.resolve_toolchain_root("   ", "/home/u/.alp")
    assert resolved.adopted is False
    assert resolved.path_str.endswith("toolchains")
    assert "/home/u/.alp" in resolved.path_str


def test_wreckage_glob_pattern_only_matches_this_leafs_own_tmp_siblings():
    pattern = tp.wreckage_glob_pattern("zephyr-sdk-1.0.1-arm-zephyr-eabi")
    assert pattern == "zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-*"
    # A DIFFERENT pin's directory never matches this pin's pattern.
    other = "zephyr-sdk-1.0.2-arm-zephyr-eabi"
    assert not Path(other).match(pattern)


# ---------------------------------------------------------------------------
# The stamp -- the ONE verdict function
# ---------------------------------------------------------------------------


def _manifest(version="1.0.1", extra=""):
    return tp.parse_toolchain_manifest(
        json.dumps(
            {
                "zephyrSdk": {
                    "version": version,
                    "baseUrl": "https://example.invalid/",
                    "artifacts": [
                        {
                            "host": "linux-x86_64", "component": "minimal-sdk",
                            "filename": "x.tar.xz", "sizeBytes": 1, "sha256": "a" * 64,
                        }
                    ],
                },
                "_extra": extra,
            }
        )
    )


def test_no_stamp_is_never_valid():
    assert tp.stamp_matches_pin(None, _manifest()) is False


def test_a_stamp_for_the_current_pin_is_valid():
    manifest = _manifest()
    stamp = tp.ToolchainStamp(manifest.version, manifest.digest(), "arm-zephyr-eabi-gcc 14.3.0")
    assert tp.stamp_matches_pin(stamp, manifest) is True


def test_a_stamp_naming_a_different_version_is_invalid_even_with_a_stale_digest_copied_over():
    old = _manifest(version="1.0.0")
    stamp = tp.ToolchainStamp(old.version, old.digest(), "triple")
    new = _manifest(version="1.0.1")
    assert tp.stamp_matches_pin(stamp, new) is False


def test_version_skew_masquerading_as_health_the_adr_0021_case():
    """'a stamped 1.0.1 store against a moved pin is a Fail with a fix, not
    "a toolchain exists"' -- the manifest's own BYTES rotated (a corrected
    sha256, a re-signed release) while the version string stayed put. The
    digest must catch what a bare version compare cannot.
    """
    before = _manifest(version="1.0.1", extra="before")
    stamp = tp.ToolchainStamp(before.version, before.digest(), "triple")
    after = _manifest(version="1.0.1", extra="after-the-pin-moved")
    assert before.digest() != after.digest()
    assert tp.stamp_matches_pin(stamp, after) is False


def test_stamp_round_trips_through_render_and_parse():
    stamp = tp.ToolchainStamp("1.0.1", "deadbeef" * 8, "arm-zephyr-eabi-gcc (Zephyr SDK) 14.3.0")
    parsed = tp.parse_stamp(tp.render_stamp(stamp))
    assert parsed == stamp


@pytest.mark.parametrize(
    "garbage",
    ["not json", "[]", "42", json.dumps({"version": "1.0.1"}), json.dumps({"version": 1, "manifestDigest": "x", "targetTriple": "y"})],
)
def test_an_unparseable_or_wrong_shaped_stamp_is_none_never_a_crash(garbage):
    assert tp.parse_stamp(garbage) is None


# ---------------------------------------------------------------------------
# Disk preflight -- "refuse with a number"
# ---------------------------------------------------------------------------


def test_disk_preflight_passes_with_room_to_spare():
    assert tp.disk_preflight_refusal(free_bytes=10 * (1 << 30), needed_bytes=2 * (1 << 30)) is None


def test_disk_preflight_refuses_and_names_both_numbers():
    refusal = tp.disk_preflight_refusal(free_bytes=1 * (1 << 30), needed_bytes=3 * (1 << 30))
    assert refusal is not None
    assert "1.00 GiB" in refusal
    assert "3.00 GiB" in refusal
    assert "--no-toolchain" in refusal


def test_required_bytes_applies_the_stated_margin():
    manifest = tp.parse_toolchain_manifest(REAL_TOOLCHAINS_MANIFEST)
    needed = tp.required_bytes(manifest)
    assert needed == int(2026739200 * 1.15)


def test_disk_preflight_boundary_is_inclusive_not_off_by_one():
    assert tp.disk_preflight_refusal(free_bytes=100, needed_bytes=100) is None
    assert tp.disk_preflight_refusal(free_bytes=99, needed_bytes=100) is not None


def test_low_disk_note_is_silent_with_room_to_spare():
    assert tp.low_disk_note(10 * (1 << 30)) is None


def test_low_disk_note_fires_under_the_floor_and_names_the_free_amount():
    note = tp.low_disk_note(100 * (1 << 20))  # 100 MiB
    assert note is not None
    assert "0.10 GiB" in note


def test_low_disk_note_boundary_is_inclusive_not_off_by_one():
    assert tp.low_disk_note(tp.LOW_DISK_AFTER_FAILURE_FLOOR_BYTES) is None
    assert tp.low_disk_note(tp.LOW_DISK_AFTER_FAILURE_FLOOR_BYTES - 1) is not None


# ---------------------------------------------------------------------------
# The `west sdk install` argv
# ---------------------------------------------------------------------------


def test_west_sdk_install_argv_uses_the_non_deprecated_gnu_toolchains_flag():
    argv = tp.west_sdk_install_argv("/venv/bin/west", version="1.0.1", install_dir="/tmp/x")
    assert argv == [
        "/venv/bin/west", "sdk", "install",
        "--version", "1.0.1",
        "--gnu-toolchains", "arm-zephyr-eabi",
        "--install-dir", "/tmp/x",
    ]
    assert "--toolchains" not in argv  # the deprecated alias, never emitted


# ---------------------------------------------------------------------------
# Failure-message augmentation -- the proxy/CA hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "sha256 mismatched: aaaa:bbbb",
        "SHA256 mismatched: AAAA:BBBB",
        "checksum verification failed",
    ],
)
def test_a_checksum_failure_gets_the_proxy_ca_hint(detail):
    augmented = tp.augment_acquisition_failure(detail)
    assert augmented.startswith(detail)
    assert "proxy" in augmented.lower()
    assert "TLS" in augmented or "CA" in augmented


@pytest.mark.parametrize(
    "detail",
    ["Connection refused", "Unavailable SDK version: 9.9.9.", "west: command not found"],
)
def test_an_unrelated_failure_is_never_augmented(detail):
    assert tp.augment_acquisition_failure(detail) == detail
