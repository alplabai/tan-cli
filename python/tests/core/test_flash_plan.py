# SPDX-License-Identifier: Apache-2.0
"""`tan.core.flash_plan`: what the `swd_probe` backend REPORTS, and which
J-Link device profile it picks when the manifest never named one.

**tan-cli#402.** Both `swd_probe` success messages used to name a literal
`GD32G553` regardless of what the run actually programmed, and the J-Link
branch read `flash_args.jlink_device` while never consulting
`flash_args.target` -- so a SoM preset declaring `interface: cmsis-dap,
target: stm32h7x` and no `jlink_device` connected `-device GD32G553MEY7TR`
against a part that is not a GD32G553, with no diagnostic anywhere. Neither
message was asserted on either side of the port: `tests/parity/
test_flash_oracle_parity.py` records `--dry-run` previews, and a dry run
returns before an `ok_message` is ever built (`tan/commands/flash_cmd.py`),
so the port inherited the string with zero coverage. These are the assertions
that were missing.

Nothing here touches hardware. `plan_swd_probe` is pure: `which` is injected,
so every case decides a plan (or a refusal) without a probe, a tool or a
board attached.
"""
import pytest

from tan.core import flash_plan
from tan.core.flash_plan import FlashInputs, FlashPlanError, plan_swd_probe

#: The device profile `plan_swd_probe` substitutes when `flash_args` names no
#: `jlink_device` -- spelled out here rather than imported so this file pins
#: the literal an oracle fixture records, and a rename on the module side
#: shows up as a failure rather than as a silently-passing tautology.
INHERITED_JLINK_DEFAULT = "GD32G553MEY7TR"


def _inputs(flash_args, artefact="a.elf", core_id="cm7"):
    return FlashInputs(artefact=artefact, flash_args=flash_args, core_id=core_id, sku="S")


def _which(*present):
    """An injected PATH probe answering True for exactly `present`. Explicit
    per case: the J-Link branch is chosen by tool PRESENCE, so a test that let
    the real PATH decide would pass or fail on whether the host running it
    happens to have SEGGER installed."""
    found = frozenset(present)
    return lambda name: name in found


def test_jlink_success_message_names_the_resolved_device_not_a_baked_in_sku():
    """The message is `data.entries[].message` -- the only per-entry human text
    in the flash envelope, and what alp-sdk-vscode renders as the outcome of a
    flash. Reporting a part the run did not program is the same class of defect
    as the `<resolved-device>` bug fixed in `tan debug-config` (#180)."""
    plan = plan_swd_probe(_inputs({"jlink_device": "STM32H747XI_M7"}), _which("JLinkExe"))

    assert "STM32H747XI_M7" in plan.ok_message
    assert "GD32G553" not in plan.ok_message


def test_openocd_success_message_names_the_resolved_target():
    """The openocd line was worse than the J-Link one: it named `GD32G553` and
    echoed no device at all, so nothing in the message could contradict it."""
    plan = plan_swd_probe(
        _inputs({"use_openocd": True, "interface": "cmsis-dap", "target": "stm32h7x"}),
        _which("openocd"),
    )

    assert "stm32h7x" in plan.ok_message
    assert "GD32G553" not in plan.ok_message


def test_pyocd_success_message_names_the_resolved_target():
    """Same line, reached through the pyOCD arm -- both arms share one
    `ok_message`, so both are pinned rather than only whichever the host has."""
    plan = plan_swd_probe(
        _inputs({"use_pyocd": True, "interface": "cmsis-dap", "target": "stm32h7x"}),
        _which("pyocd"),
    )

    assert "stm32h7x" in plan.ok_message
    assert "GD32G553" not in plan.ok_message


def test_jlink_branch_refuses_a_target_declared_without_a_jlink_device():
    """The wrong-device WRITE half of #402, and the reason it is a `safety`
    label rather than a reporting nit: this is `tan flash`'s real-write path.
    An OpenOCD/pyOCD `target` is not a J-Link device profile -- the two
    spellings never match -- so the refusal names BOTH keys instead of
    deriving one from the other."""
    with pytest.raises(FlashPlanError) as raised:
        plan_swd_probe(
            _inputs({"interface": "cmsis-dap", "target": "stm32h7x"}), _which("JLinkExe")
        )

    message = str(raised.value)
    assert "flash_args.jlink_device" in message
    assert "flash_args.target" in message


def test_a_declared_target_never_reaches_the_built_in_jlink_device_profile():
    """The acceptance criterion stated as an outcome rather than as a mechanism:
    however `plan_swd_probe` resolves the conflict -- by consuming `target` or
    by refusing -- the one thing it must never do is hand J-Link the compiled-in
    profile for a SoM that named a different part."""
    argv = ()
    try:
        argv = plan_swd_probe(
            _inputs({"interface": "cmsis-dap", "target": "stm32h7x"}), _which("JLinkExe")
        ).argv
    except FlashPlanError:
        pass

    assert INHERITED_JLINK_DEFAULT not in argv


def test_an_undeclared_device_still_takes_the_inherited_oracle_default():
    """The guard on the fix itself. `flash_args` naming NEITHER key is the
    shipped state for at least one SoM preset and is what every recorded
    `swd_probe` parity fixture captures (`would run JLinkExe -device
    GD32G553MEY7TR ...`), so the refusal above must stay scoped to a manifest
    that actually declared a conflicting part. Widening it to fire on empty
    `flash_args` would diverge from the oracle on 13 fixtures at once."""
    plan = plan_swd_probe(_inputs({}), _which("JLinkExe"))

    assert plan.argv[:3] == ("JLinkExe", "-device", INHERITED_JLINK_DEFAULT)


def test_the_module_docstring_accounts_for_every_baked_in_hardware_default():
    """`flash_plan`'s own I-26 / ADR-0017 invariant claims a single inherited
    exception; there are two (`_DEFAULT_JLINK_DEVICE`, a part number, and
    `_DEFAULT_BASE`, an address). A docstring that undercounts its own debt is
    how the third one gets added without argument, so the count is asserted."""
    doc = flash_plan.__doc__

    assert "_DEFAULT_JLINK_DEVICE" in doc
    assert "_DEFAULT_BASE" in doc
