# SPDX-License-Identifier: Apache-2.0
"""`tan.core.flash_plan.helper_flash_gate`: WHO may flash a helper, and WHEN
(tan-cli#611).

The defect this pins is a modelling one. `update_channel` answers how a device
is updated in the FIELD; `flash_method` answers by what TRANSPORT it is written.
Neither answers who may write it -- and `_flash_entry` was inferring the first
from the second, honouring `update_channel` only for a helper that declared no
`flash_method` at all. That mirrored an alp-sdk schema rule making the two
mutually exclusive, so a helper with an OTA channel for the normal case AND a
recovery-only flash method for the bricked case could not describe itself, and a
declaration added to such an entry would have been silently dropped.

`flash_policy` is the fact that was missing, and this file pins its consumer
semantics EXACTLY: the strings, not merely the truthiness. Two properties are
load-bearing and each has its own case below:

  * an ordinary `tan flash` DECLINES a factory/recovery-only helper, and
  * recovery stays REACHABLE -- an unconditional skip would remove the one path
    that matters at the one moment it matters, when a customer's device is
    bricked and they are holding Alp Lab-supplied binaries.

Pure. No IO, no `flash_args`, no PATH, no hardware.
"""
import pytest

from tan.core.flash_plan import (
    FLASH_POLICIES,
    FLASH_POLICY_CUSTOMER,
    FLASH_POLICY_FACTORY,
    FLASH_POLICY_RECOVERY_ONLY,
    HELPER,
    FlashTarget,
    helper_flash_gate,
)


def _helper(policy=None, method="swd_probe", channel=None, entry_id="gd32_bridge"):
    return FlashTarget(
        kind=HELPER,
        id=entry_id,
        flash_method=method,
        flash_args={},
        firmware_path="fw.bin",
        update_channel=channel,
        flash_policy=policy,
    )


# ── the values, spelled out rather than imported into the assertion ─────────


def test_the_three_policy_values_are_exactly_these():
    """A rename here changes what a SoM preset must write, so the literals are
    pinned rather than round-tripped through the constants they come from --
    `assert FLASH_POLICY_FACTORY == FLASH_POLICY_FACTORY` would pass forever."""
    assert FLASH_POLICY_CUSTOMER == "customer"
    assert FLASH_POLICY_FACTORY == "factory"
    assert FLASH_POLICY_RECOVERY_ONLY == "recovery_only"
    assert FLASH_POLICIES == ("customer", "factory", "recovery_only")


# ── nothing that predates the field changes behaviour ───────────────────────


@pytest.mark.parametrize(
    "policy,method,channel",
    [
        pytest.param(None, "swd_probe", None, id="method-only-no-policy"),
        pytest.param(None, None, "alp_ota_spi_otp", id="channel-only-no-policy"),
        pytest.param(None, None, None, id="neither-no-policy"),
        pytest.param("customer", "swd_probe", None, id="explicit-customer"),
        pytest.param("customer", "swd_probe", "alp_ota_spi", id="customer-with-channel"),
    ],
)
def test_a_shape_that_predates_flash_policy_is_let_through_unchanged(policy, method, channel):
    """`None` from the gate means "carry on to `_flash_entry`'s own guards".

    The `channel-only-no-policy` row is the shipped CC3501E: it still reaches
    the `if not raw_method:` branch and still gets that branch's own
    `is Alp-OTA-updated` wording. The hoist adds a decision ABOVE that guard; it
    does not reword it."""
    assert helper_flash_gate(_helper(policy, method, channel)) is None


# ── the hoist: a declaration is honoured even WITH a flash_method ────────────


def test_a_factory_helper_is_declined_even_though_it_declares_a_flash_method():
    """tan-cli#611's core defect, stated as an assertion.

    Before the hoist this entry -- a `flash_method` AND a non-customer
    declaration -- was flashed like any other target, because the only place a
    declaration was read sat inside `if not raw_method:`."""
    message = helper_flash_gate(_helper(FLASH_POLICY_FACTORY, method="swd_probe"))
    assert message == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production, "
        "not a customer flash target; skipping."
    )


def test_the_factory_decline_names_the_update_channel_only_when_one_exists():
    """"Mention the update channel only where one exists."

    The old message asserted an OTA mechanism as the REASON for the skip. The
    reason is who programs it; the channel is a separate fact about the device's
    life afterwards, and there is no true sentence of the old shape for a helper
    that carries no channel at all."""
    with_channel = helper_flash_gate(
        _helper(FLASH_POLICY_FACTORY, channel="alp_ota_spi_bridge")
    )
    assert with_channel == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production, "
        "not a customer flash target; skipping. Field updates arrive over "
        "update_channel: alp_ota_spi_bridge."
    )
    assert "update_channel" not in helper_flash_gate(_helper(FLASH_POLICY_FACTORY))


# ── recovery stays reachable, and stays deliberate ──────────────────────────


def test_recovery_only_declines_an_ordinary_run_and_names_the_exact_re_run():
    """The operator here has a bricked device and no second channel to work
    anything out from, so the decline carries the whole command, not the flag
    name alone."""
    message = helper_flash_gate(
        _helper(FLASH_POLICY_RECOVERY_ONLY, channel="alp_ota_spi_bridge")
    )
    assert message == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production and "
        "is customer-flashable only to recover a bricked device, with Alp "
        "Lab-supplied binaries; skipping. Field updates arrive over "
        "update_channel: alp_ota_spi_bridge. To recover a bricked device "
        "deliberately, re-run with `--helper gd32_bridge --recover`."
    )


def test_recover_alone_does_not_reach_the_write_and_says_why():
    """`--recover` on a whole-manifest run must not sweep a recovery helper in.
    The tail is DIFFERENT from the unarmed one on purpose: "you did not pass the
    flag" and "you passed it but did not narrow the run" are different mistakes
    and take different corrections."""
    message = helper_flash_gate(
        _helper(FLASH_POLICY_RECOVERY_ONLY), recovery_armed=True, helper_filter=None
    )
    assert message is not None
    assert message.endswith(
        " --recover was given but this run is not narrowed to it; a recovery "
        "flash must name its single target. Re-run with "
        "`--helper gd32_bridge --recover`."
    )


def test_recover_narrowed_to_a_DIFFERENT_helper_does_not_reach_the_write():
    """`--helper other --recover` on a manifest whose OTHER helper is
    recovery-only. The filter is compared to THIS entry's id, not merely
    checked for presence."""
    message = helper_flash_gate(
        _helper(FLASH_POLICY_RECOVERY_ONLY),
        recovery_armed=True,
        helper_filter="some_other_helper",
    )
    assert message is not None
    assert "not narrowed to it" in message


def test_recover_plus_the_named_helper_reaches_the_write():
    """The path that must NOT be closed. An unconditional skip would be wrong:
    it would remove the one path that matters when a customer actually needs
    it."""
    assert (
        helper_flash_gate(
            _helper(FLASH_POLICY_RECOVERY_ONLY),
            recovery_armed=True,
            helper_filter="gd32_bridge",
        )
        is None
    )


# ── the two fail-safe arms ──────────────────────────────────────────────────


def test_an_unrecognised_policy_declines_rather_than_flashing():
    """A preset from a newer SDK naming a restriction this build does not
    understand. On a command that writes hardware the fail-safe direction is to
    decline: an unrecognised restriction must not become permission."""
    message = helper_flash_gate(_helper("alp_lab_only_on_tuesdays"))
    assert message == (
        "flash: helper 'gd32_bridge' declares flash_policy "
        "'alp_lab_only_on_tuesdays', which this tan does not recognise (known: "
        "customer, factory, recovery_only); refusing to treat an unrecognised "
        "restriction as permission; skipping. Upgrade tan, or correct the SoM "
        "preset."
    )


def test_both_halves_and_no_policy_is_declined_as_under_declared():
    """tan-cli#611 asks explicitly whether an entry carrying both should be
    flagged. It should: the combination is legal only once the upstream XOR is
    relaxed, and an entry that took that freedom without saying who may write it
    is a metadata bug someone has to see. Declining is the fail-safe half; the
    message naming the missing field is the visible half."""
    message = helper_flash_gate(_helper(None, method="swd_probe", channel="alp_ota_spi"))
    assert message == (
        "flash: helper 'gd32_bridge' declares both flash_method 'swd_probe' and "
        "update_channel 'alp_ota_spi' but no flash_policy, so nothing says who "
        "may flash it; skipping. Add flash_policy (customer / factory / "
        "recovery_only) to the helper_firmware entry in the SoM preset."
    )


# ── shape details a caller depends on ───────────────────────────────────────


def test_whitespace_around_a_policy_value_does_not_defeat_it():
    """YAML makes a trailing space easy and invisible. A `factory ` that fell
    through to the unrecognised arm would still decline (fail-safe), but with
    the wrong diagnosis -- so it is stripped, and this proves the stripping
    rather than the fallback is what answered."""
    assert helper_flash_gate(_helper("  factory  ")) == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production, "
        "not a customer flash target; skipping."
    )


def test_an_empty_policy_string_reads_as_absent_not_as_unrecognised():
    """`flash_policy: ""` (or `flash_policy:` with nothing after it, which the
    manifest reader turns into `None`) must not become an unrecognised-value
    decline for every helper in the tree."""
    assert helper_flash_gate(_helper("", method="swd_probe")) is None
