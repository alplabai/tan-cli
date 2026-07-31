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

The root is resolved through `tests.conftest.sdk_root()` AT MODULE IMPORT
TIME, and that is load-bearing, not style: the autouse
`_scrub_sdk_discovery_env` fixture deletes `ALP_SDK_ROOT` from the process
environment before every test function, so an `os.environ` read inside the
body skips on EVERY run -- including runs with the variable exported. This
gate did exactly that and so had never once compared a value; its sibling
`test_planner_relocation_freshness.py` had the same defect, and that is how
`tan/planner/` drifted eleven alp-sdk commits unnoticed (tan-cli#275).
"""

from __future__ import annotations

import pathlib

import pytest

from tan.commands import doctor_cmd
from tests.conftest import sdk_root

#: Resolved at import time -- see the module docstring on why that matters.
SDK: pathlib.Path | None = sdk_root()


def _sdk_root() -> str:
    if SDK is None:
        pytest.skip(
            "ALP_SDK_ROOT is not set -- no bound alp-sdk checkout to compare "
            "doctor_cmd.JLINK_AEN_DEVICE against, so this staleness gate cannot "
            "run. This is a SKIP about the missing root, not a pass: set "
            "ALP_SDK_ROOT to a real alp-sdk checkout to actually exercise the gate."
        )
    return str(SDK)


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
    # NOT `"e8.json" in source` -- that is tautological. All three return paths
    # of jlink_flash_device mention the file: the resolved branch returns
    # `str(path)`, and BOTH fallback branches name the same path in their prose.
    # So the loose check passes even when the value came from the hardcoded
    # constant, which is exactly the case this gate exists to catch: `resolved`
    # would then equal JLINK_AEN_DEVICE trivially, because it IS
    # JLINK_AEN_DEVICE, and the assertion above would pass while proving
    # nothing. Pin the resolved branch by its exact return value instead -- the
    # bare path, which no fallback branch can produce.
    expected_source = str(
        pathlib.Path(_sdk_root()) / "metadata" / "socs" / "alif" / "ensemble" / "e8.json"
    )
    assert source == expected_source, (
        f"expected the metadata-resolved source {expected_source!r}, got {source!r} "
        "-- a fallback branch answered, so `resolved` above is the hardcoded "
        "constant compared against itself and proves nothing"
    )
