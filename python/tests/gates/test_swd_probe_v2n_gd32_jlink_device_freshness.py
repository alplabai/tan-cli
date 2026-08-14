# SPDX-License-Identifier: Apache-2.0
"""Staleness/regression gate for tan-cli#612: the four shipped V2N/V2M SoM
presets' `gd32_bridge` `swd_probe` block, read from a bound alp-sdk checkout.

**History.** As filed, the presets declared `flash_args.target` with no
`flash_args.jlink_device`, so `plan_swd_probe`'s J-Link arm refused
unconditionally (`_resolve_jlink_device`, tan-cli#402) -- and, measured wider
than the issue as filed, that refusal fired on ANY host with a J-Link binary
resolvable (not only a J-Link-only host), and `--dry-run` always took the
J-Link arm, so the entries could not even be PREVIEWED anywhere. The fix
landed upstream in `alp-sdk#1364` (closing `alp-sdk#1357`): all four presets
now carry `jlink_device: GD32G553MEY7TR`, plus a `flash_policy: recovery_only`
+ `update_channel: alp_ota_spi_bridge` pair from the same change (tan-cli#611
/ tan-cli#621 is the consumer side of that second half).

**What this file is for.** `tests/core/test_swd_probe_v2n_gd32_jlink_device_freshness`'s
sibling `tests/core/test_swd_probe_shipped_preset_shape.py` pins the CONSUMER
behaviour against a hand-typed literal -- it proves what `plan_swd_probe` does
with a `flash_args` shape, not what alp-sdk actually ships today. Nothing in
this tree previously read the real four presets and re-measured them, which is
exactly the gap that let "the YAML diff looks right" and "the flash actually
plans" diverge. This gate closes that gap: it reads the REAL shipped block out
of a bound `ALP_SDK_ROOT`, and would go red the moment any of the four drifts
from the other three, or loses `jlink_device` again, or stops planning /
stops being policy-gated.

Without `ALP_SDK_ROOT` (or `ALP_SDK_PARITY_ROOT`) there is no checkout to read,
so every test in this module SKIPS -- visibly, naming the missing env var,
never a silent pass (the exact failure mode tan-cli#275 taught this repo to
distrust).

**What this file does NOT verify** -- stated plainly per this dispatch's
instructions: no physical GD32 part, probe or bench is involved. `expect_dpidr`
is unset upstream on purpose (the SW-DP ID is contested between two
un-bench-measured hex constants -- alp-sdk#610) and this gate does not (and
cannot) arm or measure it. "Plans a correct argv" is proven; "flashes the
right silicon" is not, and never can be from a checkout with no probe
attached.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from tan.core.flash_plan import (
    HELPER,
    FlashInputs,
    FlashPlanError,
    FlashTarget,
    helper_flash_gate,
    plan_swd_probe,
)
from tests.conftest import sdk_root

#: Resolved at import time -- see `test_jlink_aen_device_freshness.py`'s
#: module docstring on why that matters: the autouse `_scrub_sdk_discovery_env`
#: fixture deletes `ALP_SDK_ROOT` from the process environment before every
#: test FUNCTION, so a read from inside a test body or a function-scoped
#: fixture always sees it already gone.
SDK: pathlib.Path | None = sdk_root()

#: One PCB, variant-populated (project convention: a fix to one preset's
#: shared metadata applies to all four, and they must stay identical).
PRESETS = ("E1M-V2N101", "E1M-V2N102", "E1M-V2M101", "E1M-V2M102")

HELPER_NAME = "gd32_bridge"


def _sdk_root() -> pathlib.Path:
    if SDK is None:
        pytest.skip(
            "ALP_SDK_ROOT is not set -- no bound alp-sdk checkout to read the "
            "shipped E1M-V2N101/V2N102/V2M101/V2M102 presets from, so this "
            "gate cannot run. This is a SKIP about the missing root, not a "
            "pass: set ALP_SDK_ROOT to a real alp-sdk checkout to actually "
            "exercise it."
        )
    return SDK


def _preset_path(name: str) -> pathlib.Path:
    return _sdk_root() / "metadata" / "e1m_modules" / f"{name}.yaml"


def _gd32_bridge_entry(name: str) -> dict[str, Any]:
    path = _preset_path(name)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = doc.get("helper_firmware") or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == HELPER_NAME:
            return entry
    raise AssertionError(
        f"{path} declares no helper_firmware[] entry named {HELPER_NAME!r} -- "
        f"either the preset dropped the GD32 bridge helper entirely, or it "
        f"was renamed, and this gate's assumption (tan-cli#612) is stale "
        f"either way."
    )


def _entries() -> dict[str, dict[str, Any]]:
    return {name: _gd32_bridge_entry(name) for name in PRESETS}


def _require_swd_probe_block(entries: dict[str, dict[str, Any]]) -> None:
    """Skip -- visibly -- once alp-sdk stops declaring the block this gate
    measures, but ONLY when all four agree (tan-cli#696).

    alp-sdk#1439 removed `flash_method: swd_probe` (and `flash_args`) from all
    four `gd32_bridge` entries: GD32 programming is separated out of `tan`
    entirely, so there is no longer a local flash path here for this gate's
    `jlink_device` / plans-on-the-J-Link-arm / previewable-under-`--dry-run`
    assertions to be about. Re-pinning this repo to an alp-sdk that carries
    that removal turns those three red against metadata that is gone BY
    DESIGN. tan-cli#732 retires the backend and this gate with it.

    Deliberately NOT an unconditional skip, and not a deletion: a PARTIAL
    absence -- some presets declaring `swd_probe`, others not -- is exactly
    the one-PCB drift this file exists to catch, so it still FAILS. Only the
    all-four-absent end-state skips, and it says which issue removed it.
    `test_the_four_shipped_presets_carry_an_identical_gd32_bridge_entry` and
    `test_the_shipped_block_stays_gated_recovery_only` do not call this: the
    entries and `flash_policy` both survive #1439, so those two keep running.
    """
    declaring = {n for n, e in entries.items()
                 if e.get("flash_method") == "swd_probe"}
    if not declaring:
        pytest.skip(
            "no shipped preset declares `flash_method: swd_probe` -- removed "
            "from all four gd32_bridge entries by alp-sdk#1439 (GD32 "
            "programming separated out of tan; tan-cli#732 is the backend "
            "removal that retires this gate). Nothing to measure, and this "
            "is the intended end-state, not drift.")
    missing = sorted(set(entries) - declaring)
    assert not missing, (
        "PARTIAL swd_probe removal across the four one-PCB presets -- "
        f"declared by {sorted(declaring)}, absent from {missing}. That is "
        "drift, not alp-sdk#1439's end-state, which removes all four.")


def test_the_four_shipped_presets_carry_an_identical_gd32_bridge_entry():
    """One PCB, variant-populated -- shared metadata MUST be identical across
    all four (tan-cli#612's own instruction: verify this rather than assume
    it)."""
    entries = _entries()
    baseline_name, baseline = next(iter(entries.items()))
    for name, entry in entries.items():
        assert entry == baseline, (
            f"{name}'s helper_firmware[{HELPER_NAME!r}] entry differs from "
            f"{baseline_name}'s -- these four presets are one PCB, "
            f"variant-populated, and their shared metadata must stay "
            f"identical:\n{baseline_name}: {baseline!r}\n{name}: {entry!r}"
        )


def test_the_shipped_block_declares_jlink_device_alongside_target():
    """The exact regression tan-cli#612 was filed over: `flash_args.target`
    present with no `flash_args.jlink_device` beside it. Checked on every
    preset independently (not just the baseline) so a future edit that
    reintroduces the gap on only ONE of the four still trips this."""
    _require_swd_probe_block(_entries())
    for name, entry in _entries().items():
        flash_args = entry.get("flash_args") or {}
        assert "target" in flash_args, (
            f"{name}: helper_firmware[{HELPER_NAME!r}].flash_args lost "
            f"'target' -- this gate's other assertions assume it is still "
            f"present."
        )
        device = flash_args.get("jlink_device")
        assert isinstance(device, str) and device, (
            f"{name}: helper_firmware[{HELPER_NAME!r}].flash_args declares "
            f"'target' ({flash_args.get('target')!r}) but no non-empty "
            f"'jlink_device' -- this is tan-cli#612 regressing: "
            f"plan_swd_probe's J-Link arm will refuse rather than guess a "
            f"SEGGER device profile from an OpenOCD/pyOCD target."
        )


def test_the_shipped_block_actually_plans_on_the_jlink_arm():
    """Not just "the YAML has the key" -- feeds the REAL shipped block through
    the REAL consumer (`plan_swd_probe`) on a J-Link-only host and proves it
    does not refuse. Runs for all four presets, not only one, since identity
    is asserted separately and a plan-time divergence between two
    byte-identical blocks would itself be a planner bug."""
    _require_swd_probe_block(_entries())
    which_jlink_only = lambda tool: tool == "JLinkExe"  # noqa: E731
    for name, entry in _entries().items():
        flash_args = entry["flash_args"]
        inp = FlashInputs(
            artefact="gd32-bridge.bin",
            flash_args=flash_args,
            core_id=HELPER_NAME,
            sku=name,
        )
        plan = plan_swd_probe(inp, which_jlink_only)
        assert plan.argv[0] == "JLinkExe", (
            f"{name}: expected the J-Link arm, got argv {plan.argv!r}"
        )
        assert flash_args["jlink_device"] in plan.argv, (
            f"{name}: {flash_args['jlink_device']!r} not in planned argv "
            f"{plan.argv!r}"
        )
        assert plan.preflight_device == flash_args["jlink_device"]


def test_the_shipped_block_can_be_previewed_with_dry_run():
    """Also measured wider than #612 as filed: `--dry-run` always takes the
    J-Link arm, so before the upstream fix these entries could not be
    PREVIEWED on any host, including one with nothing installed. Proves that
    is no longer true for the block actually shipped today.

    `swd_probe` never sets `FlashPlan.planning_only` (measured -- unlike Flow
    D's backend, this one has no confirm gate to hide a malformed
    `expect_dpidr` behind, per `plan_swd_probe`'s own comment); the
    observable "this previews rather than refuses" property here is simply
    that `plan_swd_probe` does not raise, and resolves the same J-Link arm
    with the same device profile a real run on a bare host would."""
    _require_swd_probe_block(_entries())
    which_nothing_installed = lambda tool: False  # noqa: E731
    for name, entry in _entries().items():
        flash_args = entry["flash_args"]
        inp = FlashInputs(
            artefact="gd32-bridge.bin",
            flash_args=flash_args,
            core_id=HELPER_NAME,
            sku=name,
            dry_run=True,
        )
        try:
            plan = plan_swd_probe(inp, which_nothing_installed)
        except FlashPlanError as err:
            raise AssertionError(
                f"{name}: --dry-run refused against the shipped block: {err}"
            ) from err
        assert plan.argv[0] == "JLinkExe"
        assert flash_args["jlink_device"] in plan.argv


def test_the_shipped_block_stays_gated_recovery_only():
    """The other half of the same upstream change (tan-cli#611/#621): adding
    `jlink_device` must not ALSO make this helper an ordinary customer flash
    target. An ordinary run declines it; `--recover --helper gd32_bridge`
    reaches it. Both are measured against the real entry, not a synthetic
    one."""
    for name, entry in _entries().items():
        target = FlashTarget(
            kind=HELPER,
            id=entry["name"],
            flash_method=entry.get("flash_method"),
            flash_args=entry.get("flash_args"),
            firmware_path=entry.get("firmware_path"),
            update_channel=entry.get("update_channel"),
            flash_policy=entry.get("flash_policy"),
        )
        ordinary = helper_flash_gate(target)
        assert ordinary is not None, (
            f"{name}: an ordinary `tan flash` no longer declines the "
            f"gd32_bridge helper -- it must stay recovery_only-gated."
        )
        recovered = helper_flash_gate(
            target, recovery_armed=True, helper_filter=entry["name"]
        )
        assert recovered is None, (
            f"{name}: `--recover --helper {entry['name']}` still declines: "
            f"{recovered!r}"
        )
