# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1571: Alp Platform manifest -- version grammar, schema, gpe rule,
line resolution, doctor payload. Pure; no network."""
from __future__ import annotations

import copy

import pytest

import tan.core.platform_manifest as pm
from tan.core.platform_manifest import (
    PlatformError,
    latest_ga_for_line,
    load_manifest_text,
    parse_version,
    platform_report,
    validate_manifest,
)

GOOD = {
    "schema": "alp-platform-v1",
    "platform": "27.2.0",
    "released": "2027-02-26",
    "components": {
        "alp-sdk": {"version": "0.18.0"},
        "tan-cli": {"version": "0.8.0"},
        "alp-sdk-vscode": {"version": "0.8.0"},
        "gd32-bridge-firmware": {"version": "0.3.1", "protocol": "0.7"},
        "cc3501e-bridge-firmware": {
            "version": "0.9.2", "gpe_stamp": "0.254.43.0", "gpe_floor": "0.254.42.0",
        },
    },
    "recorded": {
        "zephyr": "v4.4.1", "hal_alif": "v2.3.0", "yocto": ["kirkstone"],
        "renesas_bsp": "v6.30", "renesas_ai_sdk": "7.1",
    },
    "validated_hw": [{"som": "E1M-AEN801", "rev": "2626-r2"}],
    "bench_evidence": None,
}


def manifest(**edit):
    data = copy.deepcopy(GOOD)
    for k, v in edit.items():
        data[k] = v
    return data


def test_version_parse_and_unpadded_print():
    assert str(parse_version("27.2.0")) == "27.2.0"
    assert parse_version("27.12.10-rc3").line == "27.12"


def test_padded_month_refused_naming_the_unpadded_form():
    with pytest.raises(PlatformError, match=r"'27\.2\.0'"):
        parse_version("27.02.0")


@pytest.mark.parametrize("bad", ["27.13.0", "27.0.0", "7.2.0", "27.2", "27.2.01", "27.2.0-rc0", "v27.2.0"])
def test_off_grammar_refused(bad):
    with pytest.raises(PlatformError):
        parse_version(bad)


def test_rc_sorts_before_ga_and_by_number():
    order = ["27.2.0-rc1", "27.2.0-rc2", "27.2.0", "27.2.1", "27.10.0", "28.1.0"]
    versions = [parse_version(v) for v in order]
    assert sorted(versions[::-1]) == versions


def test_valid_manifest():
    assert str(validate_manifest(copy.deepcopy(GOOD)).version) == "27.2.0"


def test_unknown_schema_refused_naming_supported():
    with pytest.raises(PlatformError, match="alp-platform-v1"):
        validate_manifest(manifest(schema="alp-platform-v2"))


def test_unknown_top_level_key_refused_but_component_extras_ignored():
    with pytest.raises(PlatformError, match="extra"):
        validate_manifest(manifest(extra=1))
    data = copy.deepcopy(GOOD)
    data["components"]["tan-cli"]["note"] = "ok"
    validate_manifest(data)


def test_components_must_be_exactly_the_five():
    data = copy.deepcopy(GOOD)
    del data["components"]["tan-cli"]
    with pytest.raises(PlatformError, match="components"):
        validate_manifest(data)


@pytest.mark.parametrize("stamp", ["0.254.42.0", "0.254.41.9", "0.253.99.0"])
def test_gpe_stamp_at_or_below_floor_refused_as_bricking(stamp):
    data = copy.deepcopy(GOOD)
    data["components"]["cc3501e-bridge-firmware"]["gpe_stamp"] = stamp
    with pytest.raises(PlatformError, match="permanently bricks"):
        validate_manifest(data)


def test_gpe_compared_numerically_not_lexically():
    data = copy.deepcopy(GOOD)
    cc = data["components"]["cc3501e-bridge-firmware"]
    cc["gpe_floor"], cc["gpe_stamp"] = "0.254.9.0", "0.254.10.0"
    validate_manifest(data)


def test_line_resolves_to_highest_ga_micro_ignoring_rc_and_other_lines():
    tags = ["platform-v27.2.0", "platform-v27.2.10", "platform-v27.2.9", "platform-v27.2.11-rc1",
            "platform-v27.3.4", "v0.1.0", "platform-v27.02.0"]
    assert str(latest_ga_for_line("27.2", tags)) == "27.2.10"
    assert latest_ga_for_line("27.4", tags) is None


def _pin(**edit) -> bytes:
    import yaml

    return yaml.safe_dump(manifest(**edit)).encode()


def test_report_no_pin():
    assert platform_report(None, {}) == (None, "pass", "no platform pinned")


def test_report_match_mismatch_and_unknown():
    data, status, _ = platform_report(_pin(), {"alp-sdk": "v0.18.0", "tan-cli": "0.8.0"})
    assert status == "pass" and data["line"] == "27.2" and len(data["manifestSha256"]) == 64
    assert [r["name"] for r in data["components"]][:2] == ["alp-sdk", "tan-cli"]
    assert data["components"][0]["status"] == "match"
    assert data["components"][2] == {"name": "alp-sdk-vscode", "pinned": "0.8.0", "installed": None, "status": "unknown"}
    data, status, detail = platform_report(_pin(), {"alp-sdk": "0.17.0", "tan-cli": "0.8.0-rc1"})
    assert status == "warn" and "alp-sdk 0.17.0" in detail and "tan-cli 0.8.0-rc1" in detail


def test_report_invalid_pin_fails():
    data, status, _ = platform_report(b"schema: nope\n", {})
    assert data is None and status == "fail"
    assert platform_report(b"\xff\xfe", {})[1] == "fail"


def test_load_manifest_text_rejects_non_yaml():
    with pytest.raises(PlatformError):
        load_manifest_text("a: [")


@pytest.mark.parametrize("rev", ["r2", "2626-R2", "2626-r0", "626-r2"])
def test_validated_hw_rev_must_be_the_composed_form(rev):
    with pytest.raises(PlatformError, match="validated_hw"):
        validate_manifest(manifest(validated_hw=[{"som": "E1M-AEN801", "rev": rev}]))


def test_yaml_alias_refused():
    text = "a: &x [1, 2]\nb: *x\n"
    with pytest.raises(pm.PlatformError, match="alias"):
        pm.load_manifest_text(text)


def test_oversized_manifest_refused():
    with pytest.raises(pm.PlatformError, match="larger than"):
        pm.load_manifest_text("x: " + "y" * (pm.MAX_MANIFEST_BYTES + 1))


def test_error_message_repr_is_bounded():
    with pytest.raises(pm.PlatformError) as exc:
        pm.parse_version("9" * 10000)
    assert len(str(exc.value)) < 400


def test_overlong_int_is_a_platform_error():
    with pytest.raises(pm.PlatformError):
        pm.load_manifest_text("platform: " + "9" * 5000 + "\n")
