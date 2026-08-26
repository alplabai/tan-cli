# SPDX-License-Identifier: Apache-2.0
"""The shared timestamp helper: two wire shapes, the promise that it NEVER
raises, and the promise that it renders the SAME stamp on every platform.

Called in-process, unlike the per-command regressions in
``tests/commands/test_debug_config_command.py``, ``test_init_command.py`` and
``test_doctor_command.py`` -- those prove the three CALLERS still emit an
envelope, this proves the shapes those callers' goldens pin. Both halves are
needed: a helper that returns a malformed stamp breaks the goldens without ever
throwing, and a helper that throws breaks the callers without ever being asked
for a shape.

The cross-platform half is the newer one. `time.gmtime`'s range is the
platform's `time_t`, so the helper used to answer one `SOURCE_DATE_EPOCH` two
different ways depending on the host it ran on -- from a variable whose entire
purpose is reproducibility. Every case below is now the same on Windows,
Linux and macOS, and each of the two parametrised sets fails on one of those
platforms without the fix.
"""
import re
import time

import pytest

from tan.core.timestamp import generated_at_iso, wall_clock_iso

#: Inside `datetime`'s year [1; 9999], so the epoch itself is rendered -- and
#: the exact stamp is pinned, because "some stamp came back" is what let the
#: platform split go unnoticed. `253402300799` is 9999-12-31T23:59:59Z, past
#: Windows' `_MAX__TIME64_T` (32535215999, year 3000): `time.gmtime` refused it
#: there and rendered it on glibc, so this set FAILS ON WINDOWS before the fix.
IN_RANGE = [
    ("99999999999", "5138-11-16T09:46:39"),
    ("253402300799", "9999-12-31T23:59:59"),
]

#: Outside year [1; 9999], so the wall clock everywhere. MILLISECONDS is the
#: realistic one: CI and reproducible-build environments set `SOURCE_DATE_EPOCH`,
#: and 1700000000000 is the year 55840 -- which glibc's `strftime` renders
#: happily as `55840-11-08T22:13:20Z` and every consumer then chokes on, so this
#: set FAILS ON LINUX/macOS before the fix.
PAST_THE_RANGE = ["1700000000000", "-99999999999"]

#: The stamp SHAPE for the one case whose VALUE cannot be pinned (the clock).
#: Exactly four year digits and no sign, matching `%Y` (`(?P<Y>\d\d\d\d)`, see
#: `_strptime.TimeRE`) -- the helper can no longer emit anything else.
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def test_a_pinned_epoch_renders_both_shapes(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")

    assert generated_at_iso() == "1970-01-01T00:00:00Z"
    assert generated_at_iso(millis=True) == "1970-01-01T00:00:00.000Z"


def test_an_unparseable_epoch_falls_back_to_the_clock_not_an_error(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")

    # Shape only: the value is now(), which no assertion can pin.
    stamp = generated_at_iso()
    assert stamp.endswith("Z")
    assert _STAMP.match(stamp[:-1]), stamp


@pytest.mark.parametrize(("epoch", "expected"), IN_RANGE)
def test_an_in_range_epoch_renders_that_epoch_on_every_platform(epoch, expected, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)

    assert generated_at_iso() == f"{expected}Z"
    assert generated_at_iso(millis=True) == f"{expected}.000Z"


@pytest.mark.parametrize("millis", [False, True])
@pytest.mark.parametrize("epoch", PAST_THE_RANGE)
def test_an_epoch_past_the_range_is_the_clock_not_a_five_digit_year(epoch, millis, monkeypatch):
    """Out of range is REFUSED, not rendered, and never raised.

    `%Y` is exactly four digits, so the 5-digit and negative years an
    unclamped 64-bit render produces are unparseable by the very callers that
    emit them -- `test_doctor_command.py` and `test_init_command.py` both run
    `time.strptime(..., "%Y-%m-%dT%H:%M:%SZ")` over the wire value. Asserting
    with the same call is the point: this is their failure, in-process.

    Deliberately NOT oracle parity: `tan_core::clock::format_iso8601_utc`
    formats `{year:04}`, which widens, so the Rust emits the 5-digit year. See
    `tan/core/timestamp.py` for why the shared bug is fixed instead of matched.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)

    stamp = generated_at_iso(millis=millis)

    assert stamp.endswith("Z")
    time.strptime(stamp[:-5] if millis else stamp[:-1], "%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# `wall_clock_iso` -- direct coverage (review, #904 final round, nit: prior
# rounds exercised this function only indirectly, through the recency
# tie-break test in `tests/commands/test_sdk_command.py`, which is itself
# under-provisioned -- see that file's mutation-proof test).


@pytest.mark.parametrize("epoch", ["0", "1700000000000", "not-a-number", "253402300799"])
def test_wall_clock_iso_ignores_source_date_epoch(epoch, monkeypatch):
    """`SOURCE_DATE_EPOCH` is `generated_at_iso`'s knob, deliberately not
    this function's -- reproducibility is the WRONG promise for a field that
    orders two REAL writes on one host. `"0"` is the one value that would
    make the divergence invisible by accident if this function secretly DID
    read the var and render the epoch, since `generated_at_iso("0")` also
    starts with `1970`; frozen `time.time()` rules that out directly instead
    of relying on the epoch choice to expose it."""
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)  # 2023-11-14T22:13:20Z
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    unpinned = wall_clock_iso()
    assert unpinned == "2023-11-14T22:13:20Z"

    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
    pinned = wall_clock_iso()

    assert pinned == unpinned  # the frozen wall clock, not a function of the env var


def test_wall_clock_iso_millis_shape():
    stamp = wall_clock_iso(millis=True)

    assert stamp.endswith("Z")
    time.strptime(stamp[:-5], "%Y-%m-%dT%H:%M:%S")
    assert re.match(r"^\d{3}$", stamp[-4:-1])


def test_wall_clock_iso_never_raises_on_an_out_of_range_system_clock(monkeypatch):
    """Unlike `generated_at_iso`, there is no env var to validate here -- the
    INPUT that can go out of `datetime`'s year [1; 9999] is `time.time()`
    itself, which a faulty RTC/NTP step or a deliberately-set system clock
    can hand back as any float, including one this module's own out-of-range
    arithmetic cannot render (review, #904 final round, major: an earlier
    revision claimed `time.time()` "cannot land outside" that range and did
    not defend it)."""
    monkeypatch.setattr(time, "time", lambda: 9.9e12)  # year ~316000

    assert wall_clock_iso() == "1970-01-01T00:00:00Z"
    assert wall_clock_iso(millis=True) == "1970-01-01T00:00:00.000Z"
