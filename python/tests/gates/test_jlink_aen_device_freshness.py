# SPDX-License-Identifier: Apache-2.0
"""Staleness gate: catch `doctor_cmd.JLINK_AEN_DEVICE` drifting from
alp-sdk's own `metadata/socs/alif/ensemble/e8.json`.

`JLINK_AEN_DEVICE` is a SECOND source of truth for the AE822 Flow-D J-Link
device profile, kept in sync with `jlink_flash_device`'s metadata read only by
hand -- it exists purely as the fallback for a host with no `--sdk-root`
resolved (see `doctor_cmd.jlink_flash_device`'s docstring). If
`variants[].debug.jlink_flash_device` in the bound SDK's `e8.json` ever
changes, this constant goes stale and every `tan doctor` run with no resolved
SDK checkout silently advises the wrong part number, with no test going red.

Without `ALP_SDK_ROOT` there is no oracle to compare against, so the gate
SKIPS -- visibly, naming the missing env var, never a silent pass.
"""

from __future__ import annotations

import os

import pytest

from tan.commands import doctor_cmd


def _sdk_root() -> str:
    raw = os.environ.get("ALP_SDK_ROOT")
    if not raw:
        pytest.skip(
            "ALP_SDK_ROOT is not set -- no bound alp-sdk checkout to compare "
            "doctor_cmd.JLINK_AEN_DEVICE against, so this staleness gate cannot "
            "run. This is a SKIP about the missing root, not a pass: set "
            "ALP_SDK_ROOT to a real alp-sdk checkout to actually exercise the gate."
        )
    return raw


def test_jlink_aen_device_fallback_matches_the_bound_sdk_metadata():
    resolved, source = doctor_cmd.jlink_flash_device(_sdk_root())
    assert resolved == doctor_cmd.JLINK_AEN_DEVICE, (
        "alp-sdk's metadata/socs/alif/ensemble/e8.json "
        "variants[].debug.jlink_flash_device now resolves to "
        f"{resolved!r}, which no longer matches doctor_cmd.JLINK_AEN_DEVICE "
        f"({doctor_cmd.JLINK_AEN_DEVICE!r}). Update JLINK_AEN_DEVICE to match "
        f"-- otherwise every `tan doctor` run with no --sdk-root silently "
        "advises a stale part number."
    )
    assert "e8.json" in source, f"expected a resolved-from-metadata source, got {source!r}"
