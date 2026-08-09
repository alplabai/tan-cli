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

Upstream has since made that oracle AMBIGUOUS on purpose. alp-sdk#1300
(`ccd34f06`, the commit this repo's SDK pins moved to) populated the BS0
package variant's `debug.jlink_flash_device` as well, so `e8.json` now
declares TWO distinct values -- `AE822FA0E5597BS0_M55_HE` and
`AE822FA0E5597LS0_M55_HE`. `doctor_cmd.jlink_flash_device` answers that with
its documented ambiguity fallback (it refuses to pick one arbitrarily, and
its docstring anticipated exactly this variant landing), which is correct
behaviour and NOT drift.

So the property this gate asserts is the one that survives that: the
constant must still be ONE OF the values the bound `e8.json` declares. That
is what "stale" actually means here -- a `tan doctor` run with no resolved
SDK advising a part number alp-sdk no longer publishes at all. The stricter
"came from the metadata-resolved branch" assertion is kept, but only for the
UNAMBIGUOUS case it was written for; asserting it unconditionally would make
this gate red on a deliberate upstream fact rather than on drift.
"""

from __future__ import annotations

import json
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


def _declared_devices() -> tuple[set[str], pathlib.Path]:
    """Every `variants[].debug.jlink_flash_device` the bound e8.json declares.

    Read HERE, straight out of the JSON, rather than through
    `doctor_cmd.jlink_flash_device` -- that function is the thing under test,
    so using it to compute its own expected value would prove nothing.
    """
    path = (pathlib.Path(_sdk_root()) / "metadata" / "socs" / "alif"
            / "ensemble" / "e8.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    devices = set()
    for variant in doc.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        debug = variant.get("debug")
        device = debug.get("jlink_flash_device") if isinstance(debug, dict) else None
        if isinstance(device, str) and device:
            devices.add(device)
    return devices, path


def test_jlink_aen_device_fallback_matches_the_bound_sdk_metadata():
    declared, path = _declared_devices()
    assert declared, (
        f"{path} declares no variants[].debug.jlink_flash_device at all, so "
        "there is no oracle to compare doctor_cmd.JLINK_AEN_DEVICE against. "
        "That is the pre-alp-sdk#1057 shape; if the key really has been "
        "retired upstream, this gate has to be retired with it rather than "
        "left passing on an empty set."
    )
    # The staleness property, and the one that survives alp-sdk#1300 declaring
    # a SECOND, different value (BS0 alongside LS0): the constant every
    # `tan doctor` run with no resolved --sdk-root advises must still be a
    # part number alp-sdk actually publishes.
    assert doctor_cmd.JLINK_AEN_DEVICE in declared, (
        f"doctor_cmd.JLINK_AEN_DEVICE ({doctor_cmd.JLINK_AEN_DEVICE!r}) is no "
        f"longer one of the values {path} declares "
        f"({sorted(declared)}). Update JLINK_AEN_DEVICE to one of them -- "
        "otherwise every `tan doctor` run with no --sdk-root silently advises "
        "a part number alp-sdk does not publish."
    )

    resolved, source = doctor_cmd.jlink_flash_device(_sdk_root())
    if len(declared) == 1:
        # NOT `"e8.json" in source` -- that is tautological. All three return
        # paths of jlink_flash_device mention the file: the resolved branch
        # returns `str(path)`, and BOTH fallback branches name the same path in
        # their prose. So the loose check passes even when the value came from
        # the hardcoded constant, which is exactly the case this gate exists to
        # catch. Pin the resolved branch by its exact return value instead --
        # the bare path, which no fallback branch can produce.
        assert (resolved, source) == (doctor_cmd.JLINK_AEN_DEVICE, str(path)), (
            f"{path} declares exactly one jlink_flash_device "
            f"({sorted(declared)}), so jlink_flash_device() must RESOLVE it "
            f"from metadata and return the bare path as its source; got "
            f"{resolved!r} from {source!r} -- a fallback branch answered."
        )
    else:
        # Two or more DISTINCT values: `jlink_flash_device`'s documented
        # ambiguity branch is the correct answer, not drift. Assert it took
        # THAT branch (and says so), so a future regression that silently picks
        # whichever variant serialises first still goes red here.
        assert resolved == doctor_cmd.JLINK_AEN_DEVICE and "ambiguous" in source, (
            f"{path} declares {len(declared)} distinct jlink_flash_device "
            f"values ({sorted(declared)}), so jlink_flash_device() must fall "
            f"back to the built-in constant and SAY it is ambiguous rather "
            f"than pick one; got {resolved!r} from {source!r}."
        )
