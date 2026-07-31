# SPDX-License-Identifier: Apache-2.0
"""The shared timestamp helper: two wire shapes, and the promise that it NEVER
raises.

Called in-process, unlike the per-command regressions in
``tests/commands/test_debug_config_command.py``, ``test_init_command.py`` and
``test_doctor_command.py`` -- those prove the three CALLERS still emit an
envelope, this proves the shapes those callers' goldens pin. Both halves are
needed: a helper that returns a malformed stamp breaks the goldens without ever
throwing, and a helper that throws breaks the callers without ever being asked
for a shape.
"""
import re

import pytest

from tan.core.timestamp import generated_at_iso

#: Every value here is outside at least one supported platform's `time_t` range.
#: MILLISECONDS is the realistic one: CI and reproducible-build environments set
#: `SOURCE_DATE_EPOCH`, and 1700000000000 is the year 55838.
OUT_OF_RANGE = ["1700000000000", "99999999999", "-99999999999", "253402300799"]

#: The stamp SHAPE, as a regex rather than `time.strptime`. Python's `%Y` is
#: `(?P<Y>\d\d\d\d)` -- exactly four digits, see `_strptime.TimeRE` -- so it can
#: neither format nor parse a 5-digit or negative year. Those are exactly what
#: an out-of-range epoch legitimately produces on a 64-bit `time_t` host:
#: `SOURCE_DATE_EPOCH=1700000000000` is year 55838, which glibc's `strftime`
#: renders happily and `strptime` then refuses. That asymmetry is why the
#: parametrised case below passed on Windows -- whose CRT rejects the year, so
#: the helper falls back to the clock and a 4-digit year -- and failed on
#: Linux/macOS, in the TEST rather than in the helper.
#:
#: A 5-digit year is also what the Rust oracle emits (`tan_core::clock::
#: format_iso8601_utc` formats `{year:04}`, which widens rather than truncates),
#: so accepting it here is parity, not laxity.
_STAMP = re.compile(r"^-?\d{4,}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


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


@pytest.mark.parametrize("epoch", OUT_OF_RANGE)
@pytest.mark.parametrize("millis", [False, True])
def test_an_out_of_range_epoch_never_raises(epoch, millis, monkeypatch):
    """`time.gmtime` raises OverflowError or OSError (Errno 22 on Windows) past
    the platform's `time_t` range and `time.strftime` raises ValueError outside
    year [1; 9999] -- and the range differs per platform, so a value that is
    merely large here is fatal there. The helper catches rather than predicts,
    so every one of these returns a stamp.

    The stamp is checked against `_STAMP`, not `time.strptime`: on a host where
    the value is NOT out of range the year is 5-digit or negative, which
    `%Y` cannot parse."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)

    stamp = generated_at_iso(millis=millis)

    assert stamp.endswith("Z")
    assert _STAMP.match(stamp[:-5] if millis else stamp[:-1]), stamp
