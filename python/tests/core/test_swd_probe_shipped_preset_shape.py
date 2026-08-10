# SPDX-License-Identifier: Apache-2.0
"""What `swd_probe` does with the `flash_args` block the four shipped V2N/V2M
presets actually carry (tan-cli#612).

The block is identical on `E1M-V2N101` / `E1M-V2N102` / `E1M-V2M101` /
`E1M-V2M102` -- one PCB, variant-populated -- and it names `target` with no
`jlink_device`:

    flash_method:  swd_probe
    flash_args:
      interface: cmsis-dap
      target:    gd32g553
      base:      "0x08000000"

The FIX is upstream: `metadata/**` is alp-sdk's, and did not move with the
planner relocation. What belongs here is the CONSUMER measurement the upstream
issue rests on, pinned so it cannot rot, plus proof that the proposed edit
(adding `jlink_device` beside `target`) actually plans on both arms against this
consumer rather than merely looking plausible in a YAML diff.

Two of the rows below are wider than #612 as filed, and were measured rather
than assumed:

  * the refusal is not confined to a J-Link-ONLY host. The J-Link arm is
    preferred whenever a J-Link binary resolves (absent `use_openocd`/
    `use_pyocd`), so a host carrying all three tools refuses too.
  * `--dry-run` ALWAYS takes the J-Link arm, so the preview refuses on EVERY
    host -- including one with nothing installed at all.

Nothing here touches hardware: `plan_swd_probe` is pure and `which` is injected,
so no probe, tool or board is involved.
"""
import pytest

from tan.core.flash_plan import FlashInputs, FlashPlanError, plan_swd_probe

#: Byte-for-byte the shipped block. A literal, not a fixture import: the point
#: is to pin what the presets say, so a change upstream shows up as a failure
#: here rather than as a silently-passing tautology.
SHIPPED_FLASH_ARGS = {
    "interface": "cmsis-dap",
    "target": "gd32g553",
    "base": "0x08000000",
}

#: The refusal, from `_resolve_jlink_device`. Its opening clause is what #612
#: quotes and what a bricked-device operator would be left holding.
REFUSAL_OPENING = (
    "swd_probe: flash_args.target is set but flash_args.jlink_device is not"
)


def _plan(flash_args, present, *, dry_run=False):
    inp = FlashInputs(
        artefact="fw.bin",
        flash_args=flash_args,
        core_id="gd32_bridge",
        sku="S",
        dry_run=dry_run,
    )
    return plan_swd_probe(inp, lambda tool: tool in present)


@pytest.mark.parametrize(
    "present",
    [
        pytest.param({"JLinkExe"}, id="jlink-only"),
        pytest.param({"JLinkExe", "openocd", "pyocd"}, id="all-three-tools"),
    ],
)
def test_the_shipped_block_refuses_wherever_a_jlink_binary_resolves(present):
    """WIDER than #612 as filed, and measured: `all-three-tools` refuses too.

    The J-Link arm is preferred whenever a J-Link binary is on PATH unless
    `use_openocd`/`use_pyocd` force otherwise, and these presets set neither --
    so "a J-Link-only host" understates it. Any host with J-Link installed
    refuses before any write."""
    with pytest.raises(FlashPlanError) as err:
        _plan(SHIPPED_FLASH_ARGS, present)
    assert str(err.value).startswith(REFUSAL_OPENING)


@pytest.mark.parametrize(
    "present,program",
    [
        pytest.param({"openocd"}, "openocd", id="openocd-only"),
        pytest.param({"pyocd"}, "pyocd", id="pyocd-only"),
    ],
)
def test_the_shipped_block_plans_on_the_openocd_pyocd_arm(present, program):
    """The other half of #612's measured table -- and the reason this is a
    metadata defect rather than a planner one: the SAME manifest gets two
    different verdicts depending on what is installed."""
    plan = _plan(SHIPPED_FLASH_ARGS, present)
    assert plan.argv[0] == program
    # No `jlink_device`, so no DPIDR preflight is possible on this arm either.
    assert plan.preflight_device is None


@pytest.mark.parametrize(
    "present",
    [
        pytest.param(set(), id="nothing-installed"),
        pytest.param({"openocd", "pyocd"}, id="openocd-and-pyocd"),
    ],
)
def test_dry_run_refuses_on_every_host_including_ones_that_can_really_flash(present):
    """ALSO wider than #612 as filed. `--dry-run` always takes the J-Link arm,
    so the preview refuses even on a host whose REAL run plans fine (the
    `openocd-and-pyocd` row is exactly that host). A customer cannot preview
    these entries anywhere."""
    with pytest.raises(FlashPlanError) as err:
        _plan(SHIPPED_FLASH_ARGS, present, dry_run=True)
    assert str(err.value).startswith(REFUSAL_OPENING)


def test_adding_jlink_device_makes_both_arms_reachable():
    """The proposed upstream edit, measured against this consumer.

    `jlink_device` beside `target` -- NOT `expect_dpidr`, which stays out of
    that edit: the SW-DP ID is contested and unmeasurable while the part is
    disconnected (tan-cli#610), and arming a wrong-board guard with a guessed ID
    is worse than leaving it unarmed."""
    fixed = dict(SHIPPED_FLASH_ARGS, jlink_device="GD32G553")
    jlink = _plan(fixed, {"JLinkExe"})
    assert jlink.argv[0] == "JLinkExe"
    assert "GD32G553" in jlink.argv
    assert _plan(fixed, {"openocd"}).argv[0] == "openocd"


def test_adding_jlink_device_also_makes_the_dpidr_preflight_POSSIBLE():
    """A consequence the upstream edit inherits and must be told about: with a
    J-Link device resolved, the wrong-board preflight becomes possible on this
    arm, so these entries start carrying `flash.dpidr-preflight-unarmed` until
    an `expect_dpidr` is MEASURED. That advisory is the correct output -- the
    guard genuinely is not armed -- but it is new noise arriving with a
    one-line metadata change, and it should not surprise anyone."""
    fixed = dict(SHIPPED_FLASH_ARGS, jlink_device="GD32G553")
    assert _plan(fixed, {"JLinkExe"}).preflight_device == "GD32G553"
    assert _plan(SHIPPED_FLASH_ARGS, {"openocd"}).preflight_device is None
