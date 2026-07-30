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
import time

import pytest

from tan.core.timestamp import generated_at_iso

#: Every value here is outside at least one supported platform's `time_t` range.
#: MILLISECONDS is the realistic one: CI and reproducible-build environments set
#: `SOURCE_DATE_EPOCH`, and 1700000000000 is the year 55838.
OUT_OF_RANGE = ["1700000000000", "99999999999", "-99999999999", "253402300799"]


def test_a_pinned_epoch_renders_both_shapes(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")

    assert generated_at_iso() == "1970-01-01T00:00:00Z"
    assert generated_at_iso(millis=True) == "1970-01-01T00:00:00.000Z"


def test_an_unparseable_epoch_falls_back_to_the_clock_not_an_error(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")

    # Shape only: the value is now(), which no assertion can pin.
    time.strptime(generated_at_iso(), "%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("epoch", OUT_OF_RANGE)
@pytest.mark.parametrize("millis", [False, True])
def test_an_out_of_range_epoch_never_raises(epoch, millis, monkeypatch):
    """`time.gmtime` raises OverflowError or OSError (Errno 22 on Windows) past
    the platform's `time_t` range and `time.strftime` raises ValueError outside
    year [1; 9999] -- and the range differs per platform, so a value that is
    merely large here is fatal there. The helper catches rather than predicts,
    so every one of these returns a stamp."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)

    stamp = generated_at_iso(millis=millis)

    assert stamp.endswith("Z")
    time.strptime(stamp[:-5] if millis else stamp[:-1], "%Y-%m-%dT%H:%M:%S")
