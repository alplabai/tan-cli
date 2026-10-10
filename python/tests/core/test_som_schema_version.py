# SPDX-License-Identifier: Apache-2.0
"""`tan.core.som_schema_version` (tan-cli#1278): the version predicate and the
skip message `tan presets` and `tan size` share."""

from __future__ import annotations

import pytest

from tan.core.som_schema_version import (
    SOM_SCHEMA_VERSION,
    is_supported_som_schema_version,
    skipped_presets_message,
)


@pytest.mark.parametrize("version", [2])
def test_only_the_bare_integer_two_is_supported(version):
    assert SOM_SCHEMA_VERSION == 2
    assert is_supported_som_schema_version(version)


@pytest.mark.parametrize("version", [1, 3, None, "2", True, 2.0])
def test_everything_else_is_not(version):
    assert not is_supported_som_schema_version(version)


def test_an_older_sdk_is_told_it_predates_v2_and_versions_are_collapsed():
    message = skipped_presets_message("skipped 2 SoM presets under x", [1, 1])
    assert message == (
        "skipped 2 SoM presets under x: schema_version 1 (this tan reads "
        "som-preset schema_version 2) -- the bound alp-sdk predates som-preset "
        "v2 (alp-sdk#2024); point --sdk-root at an alp-sdk whose "
        "metadata/schemas/ ships som-preset-v2.schema.json, or use a tan "
        "release that matches this SDK."
    )


def test_a_newer_preset_is_told_to_upgrade_tan():
    message = skipped_presets_message("s", iter([3]))
    assert message.endswith("schema_version 3 (this tan reads som-preset "
                            "schema_version 2) -- the bound alp-sdk is newer "
                            "than this tan; upgrade tan.")


def test_a_missing_or_mixed_version_is_not_blamed_on_the_sdk_vintage():
    message = skipped_presets_message("s", [None, 1])
    assert "schema_version None, 1 " in message
    assert "predates" not in message
    assert message.endswith(
        "fix the preset's schema_version or use an alp-sdk whose presets "
        "match this tan."
    )
