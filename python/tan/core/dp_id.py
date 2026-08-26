# SPDX-License-Identifier: Apache-2.0
"""Pure SW-DP ID banner parsing for the read-only DPIDR preflight (tan-cli#520
generalised this from Flow D-only; tan-cli#795 split it out of `tan.core.
flash_plan` on its own, since these functions are pure string/bool logic that
nothing in `flash_plan.py` itself calls -- only `tan.commands.flash_cmd.
_flow_d_preflight`, the one caller that spawns `JLinkExe` and reads its
banner, does. `flash_plan.py` keeps `_validate_expect_dpidr_width` (it raises
`FlashPlanError`, defined there); this module has no IO and raises nothing.

Extracted verbatim (names unchanged, including the leading underscore --
matches this codebase's existing cross-module convention for a name that is
implementation-private to the *feature*, not to the *file*, e.g. `flash_plan.
_DEV_ROOT` imported straight into `flash_cmd.py`), no behaviour change -- see
tan-cli#312/#369/#373/#795 in the docstrings below for the history of each
refusal shape this parsing feeds.
"""
from __future__ import annotations

import re


def _dp_id_matches(expected: str, banner: str) -> bool:
    """Whether the banner's reported SW-DP ID equals `expected`, compared as
    PARSED 32-bit values (tan-cli#795) rather than the prior unanchored
    substring match (`_hex_in`, since removed): that match accepted any
    truncation of `expected` that happened to appear in the banner --
    `0x2477` matched `Found SW-DP with ID 0x0BE12477`, and `0x477` (ARM's own
    JEP106 designer field, shared by every ARM SW-DP) matched every ARM
    board's banner. The caller already refuses a short `expected` via
    `flash_plan.validate_flow_d_preflight_args`'s own width check
    (`flash_plan._validate_expect_dpidr_width`, run at plan time, before this
    ever executes -- see `tan.commands.flash_cmd._flow_d_preflight`);
    `_dp_id_value` extracts the banner's own reported ID the same way the
    mismatch-reporting branch below already does."""
    actual = _dp_id_value(banner)
    if actual is None:
        return False
    return int(actual, 16) == int(expected, 16)


#: SEGGER's own wording for a successful SWD connect that read AN id, whatever
#: it turned out to be -- "Found SW-DP with ID 0x........" / "DPIDR: 0x........".
#: Matched loosely on purpose: what this distinguishes is "a real board
#: answered with a different identity" from "nothing answered", not the exact
#: firmware/DLL version's phrasing. The hex value is its own capture group
#: (tan-cli#512) so `_dp_id_value` can report what was ACTUALLY read, not only
#: whether something was -- `_dp_id_reported` still just asks whether this
#: matches at all.
_DP_ID_RE = re.compile(r"(?:with\s+ID|DPIDR)\s*:?\s*(0x[0-9A-Fa-f]+)", re.IGNORECASE)

#: SEGGER's own wording for the PROBE itself refusing the connection outright
#: -- measured verbatim on the rc3 bench run: "Connecting to J-Link ...FAILED:
#: Cannot connect to the probe/programmer." (tan-cli#312). Deliberately NOT a
#: bare `FAILED`/`Cannot connect` match (that was the tan-cli#312 review
#: finding): JLinkExe prints "FAILED" in many contexts, and "Cannot connect"
#: alone also fires on a TARGET-level refusal -- see `_CONNECT_FAILED_TARGET_RE`
#: below -- which is a real wiring/probe-selection problem, not a re-enumerating
#: probe.
_CONNECT_FAILED_RE = re.compile(r"Cannot connect to the probe/programmer", re.IGNORECASE)

#: SEGGER's own wording for a TARGET-level connect refusal -- "Cannot connect
#: to target." (unplugged SWD ribbon, unpowered board) or "Cannot connect to
#: J-Link." (a probe that IS reachable via USB but refuses the requested
#: `jlink_serial`). Both are genuine wiring/probe-selection problems, so their
#: presence forces `_connect_failed_outright` to False even alongside the
#: probe-level phrase above -- asserting "wiring is fine" here would be the
#: false negative tan-cli#312's review flagged (measured against a real
#: unplugged-ribbon and a real unpowered-target banner).
_CONNECT_FAILED_TARGET_RE = re.compile(r"Cannot connect to (?:target|J-Link)\b", re.IGNORECASE)


def _dp_id_reported(banner: str) -> bool:
    """Whether the banner names ANY SW-DP ID -- not whether it matches
    `expected` (the caller already ruled that out via `_dp_id_matches`), only
    whether a connect got far enough to read one at all."""
    return _DP_ID_RE.search(banner) is not None


def _dp_id_value(banner: str) -> str | None:
    """The actual SW-DP ID text the banner reported, verbatim (whatever hex
    casing/`0x` spelling SEGGER printed), or `None` if `_DP_ID_RE` does not
    match at all. tan-cli#512: a caller that already knows `_dp_id_reported(
    banner)` is `True` gets a non-`None` value here by construction -- both
    read the same regex against the same banner."""
    match = _DP_ID_RE.search(banner)
    return match.group(1) if match else None


def _connect_failed_outright(banner: str) -> bool:
    """Whether the banner carries SEGGER's own wording for the PROBE itself
    refusing the connection (still re-enumerating, no board reachable at all),
    as opposed to a TARGET-level refusal -- a real wiring/probe-selection
    problem that must keep the original remediation, not the re-enumeration
    one."""
    if _CONNECT_FAILED_TARGET_RE.search(banner) is not None:
        return False
    return _CONNECT_FAILED_RE.search(banner) is not None
