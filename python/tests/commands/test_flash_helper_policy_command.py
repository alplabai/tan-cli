# SPDX-License-Identifier: Apache-2.0
"""`tan flash` end-to-end for the helper-flashing model (tan-cli#611).

`tests/core/test_helper_flash_policy.py` pins the pure decision. This file pins
that the decision REACHES the customer -- through the real subprocess, in the
envelope AND in the default text transcript, which is the one a bench operator
actually sees (`--format json` is opt-in).

The manifests below deliberately declare BOTH an `update_channel` and a
`flash_method` on one helper. That combination was what alp-sdk's schema
forbade (`{"required": ["update_channel"], "not": {"required": ["flash_method"]}}`
-- "an entry carries exactly one of the two") at the time tan's consumer half
landed (tan-cli#621) -- tan had to be correct before the upstream edit landed,
since an entry that declares a policy tan ignores is exactly the silent-drop
defect #611 reports, one field over. **alp-sdk has since relaxed the XOR**
(`alplabai/alp-sdk#1357`, merged as `#1364`, measured against `origin/dev`
`496e32ad`): the schema now REQUIRES `flash_policy` on every entry, unconditionally,
using the exact three values (`customer`/`factory`/`recovery_only`) and message
shape this file already pinned. The section below titled "nothing that predates
the field moved" grounds the CC3501E cases against that real, now-shipped shape
rather than only the pre-1357 fallback.

No case touches hardware. Every one stops at a skip decided before dispatch, or
at `--dry-run`, which returns before anything spawns.
"""
import pytest

from tests.commands.test_flash_command import codes, envelope, run_flash

_HELPER = """schema_version: 1
hw_info: {{sku: E1M-V2N101}}
slices: []
helper_mcus:
- {{name: gd32_bridge, chip: gd32g553, firmware_path: fw.bin,
   flash_method: swd_probe, flash_args: {{interface: cmsis-dap}}{extra}}}
boot_order: []
"""


def _manifest(policy=None, channel=None):
    extra = ""
    if channel is not None:
        extra += f",\n   update_channel: {channel}"
    if policy is not None:
        extra += f",\n   flash_policy: {policy}"
    return _HELPER.format(extra=extra)


def _only_entry(payload):
    assert len(payload["data"]["entries"]) == 1, payload
    return payload["data"]["entries"][0]


# Every case below asserts the ENTRY before the exit code, deliberately. A
# let-through entry does not merely change `entries[0]`: it goes on to attempt a
# real write, which fails on a host with no board and turns `exitCode` into 1 --
# so an exit-code assertion placed first fails on the CONSEQUENCE of the defect
# and reports "1 != 0", saying nothing about the policy that was ignored. Found
# by mutation: two guards' first mutants died on `assert exit_code == 0` and had
# to be re-driven to die on the assertion that states the property.


# ── the hoist, end to end ───────────────────────────────────────────────────


def test_a_factory_helper_declaring_a_flash_method_is_declined(tmp_path):
    """The #611 defect at the command boundary: before the hoist, this manifest
    -- a non-customer declaration alongside a `flash_method` -- reached a real
    flasher, because the declaration was only ever read inside the "no
    flash_method" branch."""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", manifest=_manifest(policy="factory")
    )
    payload = envelope(out)
    entry = _only_entry(payload)
    assert entry["status"] == "skipped", payload
    assert entry["rc"] == -1, payload
    assert entry["message"] == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production, "
        "not a customer flash target; skipping."
    ), payload
    assert exit_code == 0, payload
    assert codes(payload) == ["flash.entries-skipped"], payload


def test_a_policy_declined_entry_advertises_no_method(tmp_path):
    """`as_dict()` omits a `None` method entirely -- the shape the
    `update_channel` skip has always emitted. An entry nobody was allowed to
    flash must not report the transport it would have used, or a consumer reads
    `method: swd_probe` on a target that was never a flash target."""
    _, out, _ = run_flash(
        tmp_path, "--format", "json", manifest=_manifest(policy="factory")
    )
    entry = _only_entry(envelope(out))
    assert "method" not in entry, entry


def test_the_decline_reaches_the_DEFAULT_text_transcript(tmp_path):
    """No `--format json`. The bench operator this protects does not pass it,
    and a decision visible only in an envelope they never ask for is a decision
    they never see."""
    exit_code, out, err = run_flash(tmp_path, manifest=_manifest(policy="factory"))
    assert exit_code == 0, err
    assert out == "", out
    assert "is programmed by Alp Lab in production" in err, err


# ── recovery: declined by default, reachable deliberately ───────────────────


def test_recovery_only_is_declined_by_an_ordinary_run(tmp_path):
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        manifest=_manifest(policy="recovery_only", channel="alp_ota_spi_bridge"),
    )
    payload = envelope(out)
    entry = _only_entry(payload)
    assert entry["status"] == "skipped", payload
    assert exit_code == 0, payload
    assert entry["message"] == (
        "flash: helper 'gd32_bridge' is programmed by Alp Lab in production and "
        "is customer-flashable only to recover a bricked device, with Alp "
        "Lab-supplied binaries; skipping. Field updates arrive over "
        "update_channel: alp_ota_spi_bridge. To recover a bricked device "
        "deliberately, re-run with `--helper gd32_bridge --recover`."
    ), payload
    assert "flash.recovery-flash-armed" not in codes(payload), payload


def test_naming_the_helper_without_recover_still_declines(tmp_path):
    """The other half of the pair. `--helper gd32_bridge` alone is an ordinary
    run that happens to be narrowed; it is not an authorisation, and a customer
    who narrows a run to inspect one helper must not thereby be flashing it."""
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        "--helper",
        "gd32_bridge",
        manifest=_manifest(policy="recovery_only"),
    )
    payload = envelope(out)
    entry = _only_entry(payload)
    assert entry["status"] == "skipped", payload
    assert "To recover a bricked device deliberately" in entry["message"], payload
    assert exit_code == 0, payload
    assert "flash.recovery-flash-armed" not in codes(payload), payload


def test_recover_without_helper_flashes_nothing(tmp_path):
    """A whole-manifest `tan flash --recover` must not sweep the recovery helper
    in. The flag is inert on its own."""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", "--recover", manifest=_manifest(policy="recovery_only")
    )
    payload = envelope(out)
    entry = _only_entry(payload)
    assert entry["status"] == "skipped", payload
    assert "not narrowed to it" in entry["message"], payload
    assert exit_code == 0, payload
    assert "flash.recovery-flash-armed" not in codes(payload), payload


def test_recover_with_the_named_helper_reaches_dispatch_and_says_so(tmp_path):
    """The path that must stay open, plus its advisory.

    `--dry-run` so nothing spawns: what is under test is that the entry got PAST
    the policy gate and into the backend, and that the run states -- in both
    channels -- that a normally-declined target was authorised."""
    exit_code, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        "--helper",
        "gd32_bridge",
        "--recover",
        "--dry-run",
        manifest=_manifest(policy="recovery_only"),
    )
    payload = envelope(out)
    assert exit_code == 0, payload
    entry = _only_entry(payload)
    assert entry["status"] == "ok", payload
    assert entry["method"] == "swd_probe", payload
    assert "programmed by Alp Lab in production" not in entry["message"], payload
    assert "flash.recovery-flash-armed" in codes(payload), payload
    armed = [i for i in payload["issues"] if i["code"] == "flash.recovery-flash-armed"]
    assert armed[0]["severity"] == "warning", payload
    assert armed[0]["message"] == (
        "gd32_bridge: RECOVERY FLASH ARMED (--recover). This helper is "
        "programmed by Alp Lab in production; this path exists only to recover "
        "a bricked device, with Alp Lab-supplied binaries. A working device is "
        "updated over its OTA channel instead."
    ), payload


def test_the_armed_advisory_reaches_the_DEFAULT_text_transcript(tmp_path):
    _, out, err = run_flash(
        tmp_path,
        "--helper",
        "gd32_bridge",
        "--recover",
        "--dry-run",
        manifest=_manifest(policy="recovery_only"),
    )
    assert out == "", out
    assert "RECOVERY FLASH ARMED (--recover)" in err, err


def test_recover_does_not_widen_a_customer_helper(tmp_path):
    """`--recover` is not a global permission bit. A `customer` helper behaves
    identically with and without it -- the flag only ever unlocks the one policy
    value it names."""
    args = ("--helper", "gd32_bridge", "--dry-run", "--format", "json")
    manifest = _manifest(policy="customer")
    _, plain, _ = run_flash(tmp_path, *args, manifest=manifest)
    _, armed, _ = run_flash(tmp_path, *args, "--recover", manifest=manifest)
    assert _only_entry(envelope(plain)) == _only_entry(envelope(armed))
    assert "flash.recovery-flash-armed" not in codes(envelope(armed))


# ── the fail-safe arms ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "policy,channel,fragment",
    [
        pytest.param(
            "alp_lab_only_on_tuesdays",
            None,
            "does not recognise",
            id="unrecognised-policy",
        ),
        pytest.param(None, "alp_ota_spi_bridge", "no flash_policy", id="under-declared"),
    ],
)
def test_a_helper_tan_cannot_reason_about_is_declined_not_flashed(
    tmp_path, policy, channel, fragment
):
    """Both arms decline rather than fall back to flashing. `--dry-run` is NOT
    passed: the point is that the entry never reaches a backend at all, on a run
    that would otherwise have spawned one."""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", manifest=_manifest(policy=policy, channel=channel)
    )
    payload = envelope(out)
    entry = _only_entry(payload)
    assert entry["status"] == "skipped", payload
    assert entry["rc"] == -1, payload
    assert fragment in entry["message"], payload
    assert exit_code == 0, payload


# ── nothing that predates the field moved ───────────────────────────────────


def test_a_pre_1357_cc3501e_shape_is_untouched(tmp_path):
    """`update_channel` and no `flash_method`, no `flash_policy` at all -- the
    CC3501E shape from BEFORE alp-sdk#1357/#1364 landed `flash_policy` on every
    `helper_firmware` entry. No longer what `E1M-AEN801` actually ships (see
    `test_the_shipped_cc3501e_shape_declines_via_the_new_recovery_only_path`
    below for that), but still the correct fallback for a manifest generated by
    an older alp-sdk / an as-yet-unregenerated preset: it takes `_flash_entry`'s
    own branch and gets that branch's wording. The hoist adds a decision ABOVE
    that guard; it does not reword the guard."""
    manifest = """schema_version: 1
hw_info: {sku: E1M-AEN801}
slices: []
helper_mcus:
- {name: cc3501e_otp, chip: cc3501e, firmware_path: fw.bin,
   update_channel: alp_ota_spi_otp}
boot_order: []
"""
    _, out, _ = run_flash(tmp_path, "--format", "json", manifest=manifest)
    entry = _only_entry(envelope(out))
    assert entry["message"] == (
        "flash: helper 'cc3501e_otp' is Alp-OTA-updated (update_channel: "
        "alp_ota_spi_otp), not a customer flash target; skipping"
    ), entry


# MEASURED against `alplabai/alp-sdk` `origin/dev` `496e32ad` (#1357/#1364):
# all six `E1M-AEN*` presets' `cc3501e_otp` entry now carries
# `flash_policy: recovery_only` alongside its existing `update_channel` (and a
# real `firmware_path`, unlike the GD32 bridge). `flash_policy` is REQUIRED on
# every `helper_firmware` entry now, so this -- not the pre-1357 shape above --
# is what a freshly-generated E1M-AEN801 manifest actually contains.
_CC3501E_SHIPPED_MANIFEST = """schema_version: 1
hw_info: {sku: E1M-AEN801}
slices: []
helper_mcus:
- {name: cc3501e_otp, chip: cc3501e,
   firmware_path: firmware/cc3501e/prebuilt/cc3501e-v0.2.0.bin,
   flash_policy: recovery_only, update_channel: alp_ota_spi_otp}
boot_order: []
"""


def test_the_shipped_cc3501e_shape_declines_via_the_new_recovery_only_path(tmp_path):
    """The REAL, current shape: `flash_policy: recovery_only` now reaches
    `helper_flash_gate` before `_flash_entry`'s own `update_channel` branch ever
    runs, so an ordinary run gets the recovery-only decline -- naming the exact
    re-run -- rather than the older "is Alp-OTA-updated" wording. Both messages
    are correct declines; a customer reading either one is safe. What changed
    is which one they actually see, and a test still pinning the old wording as
    "what ships" would be wrong about the field."""
    exit_code, out, _ = run_flash(
        tmp_path, "--format", "json", manifest=_CC3501E_SHIPPED_MANIFEST
    )
    entry = _only_entry(envelope(out))
    assert entry["status"] == "skipped", entry
    assert entry["message"] == (
        "flash: helper 'cc3501e_otp' is programmed by Alp Lab in production and "
        "is customer-flashable only to recover a bricked device, with Alp "
        "Lab-supplied binaries; skipping. Field updates arrive over "
        "update_channel: alp_ota_spi_otp. To recover a bricked device "
        "deliberately, re-run with `--helper cc3501e_otp --recover`."
    ), entry
    assert exit_code == 0, entry


def test_the_shipped_cc3501e_shape_recovery_still_has_no_flash_method(tmp_path):
    """Even armed (`--recover --helper cc3501e_otp`), the CC3501E has never
    declared a `flash_method` -- there is nothing for `--recover` to unlock,
    because the OTP write has never gone through `tan flash`'s dispatcher at
    all. Dispatch falls through to `_flash_entry`'s original `not raw_method`
    branch, which keeps its own "is Alp-OTA-updated" wording -- the SAME
    message the pre-1357 shape gets on an ORDINARY run, but reached here from
    an armed, narrowed one, which is the correct distinct path."""
    _, out, _ = run_flash(
        tmp_path,
        "--format",
        "json",
        "--helper",
        "cc3501e_otp",
        "--recover",
        manifest=_CC3501E_SHIPPED_MANIFEST,
    )
    entry = _only_entry(envelope(out))
    assert entry["status"] == "skipped", entry
    assert entry["message"] == (
        "flash: helper 'cc3501e_otp' is Alp-OTA-updated (update_channel: "
        "alp_ota_spi_otp), not a customer flash target; skipping"
    ), entry


def test_a_helper_with_no_policy_at_all_still_dispatches(tmp_path):
    """The regression that matters most: every SoM preset in the tree predates
    this field, and a gate that declined an undeclared helper would stop all of
    them flashing."""
    _, out, _ = run_flash(
        tmp_path, "--format", "json", "--dry-run", manifest=_manifest()
    )
    entry = _only_entry(envelope(out))
    assert entry["status"] == "ok", entry
    assert entry["method"] == "swd_probe", entry
