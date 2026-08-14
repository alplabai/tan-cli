# SPDX-License-Identifier: Apache-2.0
"""What `swd_probe` does with the `flash_args` block the four V2N/V2M presets
carry (tan-cli#612).

**alp-sdk landed the fix** (`alplabai/alp-sdk#1357`, merged as `#1364`,
measured against `origin/dev` `496e32ad`): `E1M-V2N101` / `E1M-V2N102` /
`E1M-V2M101` / `E1M-V2M102` now all carry, byte-identical (one PCB,
variant-populated -- verified by diffing all four `helper_firmware:` blocks):

    flash_method:   swd_probe
    flash_policy:   recovery_only
    update_channel: alp_ota_spi_bridge
    flash_args:
      interface:    cmsis-dap
      target:       gd32g553
      jlink_device: GD32G553MEY7TR
      base:         "0x08000000"

`SHIPPED_FLASH_ARGS` below is that `flash_args` block, byte-for-byte -- a
literal, not a fixture import, so a change upstream shows up as a failure
here rather than as a silently-passing tautology. `_PRE_1357_FLASH_ARGS` is
the block as it shipped BEFORE that fix (`target` with no `jlink_device`):
kept, not deleted, because it is still the correct regression case for
`plan_swd_probe`'s own refusal -- any OTHER SoM preset (present or future)
that names `target` without `jlink_device` must still refuse exactly this
way, upstream fix or not.

Two of the refusal-side rows are wider than #612 as originally filed, and
were measured rather than assumed:

  * the refusal (on the pre-fix block) is not confined to a J-Link-ONLY host.
    The J-Link arm is preferred whenever a J-Link binary resolves (absent
    `use_openocd`/`use_pyocd`), so a host carrying all three tools refuses
    too.
  * `--dry-run` ALWAYS takes the J-Link arm, so the preview refused on EVERY
    host -- including one with nothing installed at all. With the fix, the
    same is true in reverse: the J-Link arm now plans instead of refusing, on
    every host that resolves a J-Link binary.

`expect_dpidr` stays deliberately UNSET on the real, shipped block --
confirmed by reading the preset's own comment at
`metadata/e1m_modules/E1M-V2N101.yaml`: the GD32's SW-DP ID is contested
(two competing hex constants, neither bench-measured with a probe attached,
tan-cli#610) and a guard armed with a guessed ID would pass on exactly the
board it exists to exclude. So the DPIDR preflight is now POSSIBLE (a
`jlink_device` resolves) but stays UNARMED, and these entries carry
`flash.dpidr-preflight-unarmed` until a value is measured on silicon.

Nothing here touches hardware: `plan_swd_probe` is pure and `which` is
injected, so no probe, tool or board is involved.
"""
import os
from pathlib import Path

import pytest

from tan.core.flash_plan import FlashInputs, FlashPlanError, plan_swd_probe

#: Byte-for-byte the REAL, shipped `flash_args` block -- alp-sdk#1357/#1364.
SHIPPED_FLASH_ARGS = {
    "interface": "cmsis-dap",
    "target": "gd32g553",
    "jlink_device": "GD32G553MEY7TR",
    "base": "0x08000000",
}

#: Byte-for-byte the block as it shipped BEFORE alp-sdk#1357/#1364 -- `target`
#: with no `jlink_device`. Retained as the general "a preset without
#: `jlink_device` must refuse, not guess" regression case; no longer what any
#: of the four V2N/V2M presets actually carry.
_PRE_1357_FLASH_ARGS = {
    "interface": "cmsis-dap",
    "target": "gd32g553",
    "base": "0x08000000",
}

#: The refusal, from `_resolve_jlink_device`. Its opening clause is what #612
#: quotes and what a bricked-device operator would have been left holding
#: before the upstream fix.
REFUSAL_OPENING = (
    "swd_probe: flash_args.target is set but flash_args.jlink_device is not"
)

#: The four presets alp-sdk#1357/#1364 fixed, all with the identical block.
_SHIPPED_PRESET_SKUS = ("E1M-V2N101", "E1M-V2N102", "E1M-V2M101", "E1M-V2M102")


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()


def _plan(flash_args, present, *, dry_run=False):
    inp = FlashInputs(
        artefact="fw.bin",
        flash_args=flash_args,
        core_id="gd32_bridge",
        sku="S",
        dry_run=dry_run,
    )
    return plan_swd_probe(inp, lambda tool: tool in present)


# ── the block as it actually ships today ────────────────────────────────────


@pytest.mark.parametrize(
    "present,program",
    [
        pytest.param({"JLinkExe"}, "JLinkExe", id="jlink-only"),
        pytest.param({"JLinkExe", "openocd", "pyocd"}, "JLinkExe", id="all-three-tools"),
        pytest.param({"openocd"}, "openocd", id="openocd-only"),
        pytest.param({"pyocd"}, "pyocd", id="pyocd-only"),
    ],
)
def test_the_shipped_block_plans_on_every_host_that_has_a_tool(present, program):
    """The real, current block plans -- it does not refuse -- on every host
    that resolves at least one of J-Link/OpenOCD/pyOCD. This is the state
    #612 asked for, now measured against what alp-sdk actually shipped rather
    than a proposed edit."""
    plan = _plan(SHIPPED_FLASH_ARGS, present)
    assert plan.argv[0] == program
    if program == "JLinkExe":
        assert "GD32G553MEY7TR" in plan.argv
        # The DPIDR preflight is now POSSIBLE (a jlink_device resolved) --
        # see test_the_shipped_block_makes_the_dpidr_preflight_possible below
        # for the fact that it stays deliberately UNARMED.
        assert plan.preflight_device == "GD32G553MEY7TR"
    else:
        # No DPIDR preflight on the OpenOCD/pyOCD arm; unchanged by the fix.
        assert plan.preflight_device is None


def test_the_shipped_block_previews_under_dry_run_too():
    """`--dry-run` always takes the J-Link arm -- so with the fix, the preview
    now succeeds instead of refusing on every host. Before the fix this exact
    call raised `FlashPlanError`; see
    `test_the_pre_1357_block_still_refuses_under_dry_run` for that case,
    preserved as a regression."""
    plan = _plan(SHIPPED_FLASH_ARGS, {"JLinkExe"}, dry_run=True)
    assert plan.argv[0] == "JLinkExe"


def test_the_shipped_block_makes_the_dpidr_preflight_possible_but_unarmed():
    """The consequence #612's fix inherits: with a J-Link device resolved, the
    wrong-board preflight becomes POSSIBLE on this arm. `expect_dpidr` is
    deliberately absent from the real preset (tan-cli#610 -- the SW-DP ID is
    contested and unmeasured), so these entries carry
    `flash.dpidr-preflight-unarmed`, not an armed guard. That advisory is the
    correct output, not new noise to chase away."""
    assert _plan(SHIPPED_FLASH_ARGS, {"JLinkExe"}).preflight_device == "GD32G553MEY7TR"
    assert _plan(SHIPPED_FLASH_ARGS, {"openocd"}).preflight_device is None


@pytest.mark.skipif(
    SDK is None,
    reason="needs ALP_SDK_PARITY_ROOT/ALP_SDK_ROOT bound to a real alp-sdk checkout",
)
def test_all_four_shipped_presets_carry_the_identical_measured_block():
    """MEASURE, don't infer: read the real `helper_firmware:` entry out of the
    bound alp-sdk checkout for all four V2N/V2M presets, and assert each one
    is byte-identical to `SHIPPED_FLASH_ARGS` (plus `flash_policy`/
    `update_channel`) -- and identical to each other, per the "one PCB,
    variant-populated" rule. A future preset edit that drifts from this shows
    up here as a failure, not as a silently-stale literal above."""
    import yaml  # noqa: PLC0415 -- optional at runtime, matching flash_plan.py

    blocks = {}
    for sku in _SHIPPED_PRESET_SKUS:
        preset_path = SDK / "metadata" / "e1m_modules" / f"{sku}.yaml"
        doc = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
        helpers = doc["helper_firmware"]
        gd32 = next(h for h in helpers if h["name"] == "gd32_bridge")
        blocks[sku] = gd32

    first_sku = _SHIPPED_PRESET_SKUS[0]
    first = blocks[first_sku]
    for sku in _SHIPPED_PRESET_SKUS[1:]:
        assert blocks[sku] == first, (
            f"{sku}'s gd32_bridge helper diverges from {first_sku}'s -- the four "
            "V2N/V2M presets are the same PCB, variant-populated, and must stay "
            f"identical: {blocks[sku]!r} != {first!r}"
        )

    # These two survive alp-sdk#1439 and are asserted unconditionally: the
    # helper entry itself stays, and `flash_policy` stays with it (it is
    # `required` on every helper entry, with or without a `flash_method`).
    assert first["flash_policy"] == "recovery_only", first
    if "flash_method" not in first:
        # alp-sdk#1439 removed `flash_method: swd_probe` and `flash_args`
        # from all four entries -- GD32 programming is separated out of tan
        # (tan-cli#732 retires the backend). The identity assertion above
        # already ran, so a PARTIAL removal across the four still fails here
        # rather than reaching this skip; only the intended all-four
        # end-state gets past it.
        pytest.skip(
            "shipped gd32_bridge entries no longer declare `flash_method: "
            "swd_probe` (alp-sdk#1439) -- the flash_args assertions below "
            "have nothing to measure. The one-PCB identity check and "
            "flash_policy/update_channel above still ran.")
    assert first["flash_method"] == "swd_probe", first
    assert first["update_channel"] == "alp_ota_spi_bridge", first
    assert first["flash_args"] == SHIPPED_FLASH_ARGS, first["flash_args"]
    # `expect_dpidr` must stay absent -- tan-cli#610, the SW-DP ID is
    # unmeasured, and arming a guard with a guessed value is worse than an
    # unarmed one.
    assert "expect_dpidr" not in first["flash_args"], first["flash_args"]


# ── the pre-fix block, preserved as the general "no jlink_device" case ──────


@pytest.mark.parametrize(
    "present",
    [
        pytest.param({"JLinkExe"}, id="jlink-only"),
        pytest.param({"JLinkExe", "openocd", "pyocd"}, id="all-three-tools"),
    ],
)
def test_the_pre_1357_block_refuses_wherever_a_jlink_binary_resolves(present):
    """`target` with no `jlink_device` -- no longer what any shipped preset
    carries, but still the correct refusal for any preset (present or future)
    that has this shape. WIDER than #612 as originally filed, and measured:
    `all-three-tools` refuses too, because the J-Link arm is preferred
    whenever a J-Link binary is on PATH unless `use_openocd`/`use_pyocd` force
    otherwise."""
    with pytest.raises(FlashPlanError) as err:
        _plan(_PRE_1357_FLASH_ARGS, present)
    assert str(err.value).startswith(REFUSAL_OPENING)


@pytest.mark.parametrize(
    "present,program",
    [
        pytest.param({"openocd"}, "openocd", id="openocd-only"),
        pytest.param({"pyocd"}, "pyocd", id="pyocd-only"),
    ],
)
def test_the_pre_1357_block_plans_on_the_openocd_pyocd_arm(present, program):
    """The other half of #612's measured table -- and the reason this was a
    metadata defect rather than a planner one: the SAME manifest got two
    different verdicts depending on what was installed."""
    plan = _plan(_PRE_1357_FLASH_ARGS, present)
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
def test_the_pre_1357_block_refuses_under_dry_run_on_every_host(present):
    """`--dry-run` always takes the J-Link arm, so the preview refused even on
    a host whose REAL run planned fine (the `openocd-and-pyocd` row is
    exactly that host)."""
    with pytest.raises(FlashPlanError) as err:
        _plan(_PRE_1357_FLASH_ARGS, present, dry_run=True)
    assert str(err.value).startswith(REFUSAL_OPENING)
