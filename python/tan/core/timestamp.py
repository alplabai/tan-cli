# SPDX-License-Identifier: Apache-2.0
"""The port's ONE `generatedAt`/`updatedAt` timestamp helper. It NEVER raises,
and it renders the SAME stamp on every platform.

`SOURCE_DATE_EPOCH` wins over the clock, so a captured envelope -- or a written
`.alp/sdk-path` -- is reproducible. Counterpart of `crate::util::
generated_at_iso` + `tan_core::clock::format_iso8601_utc`.

Shared rather than per-command because three commands each carried their own
copy of these four lines, ONE copy was hardened against an out-of-range epoch,
and the other two shipped the defect. A timestamp helper that throws is not
cosmetic in this port -- each caller breaks differently and none of them
harmlessly:

* `tan debug-config` calls it from the recovery path of its own exception
  guard, so a throw DOUBLE-FAULTS: the first failure is caught, the recovery
  re-raises, and the process dies with a raw traceback and EMPTY stdout --
  precisely the break that guard exists to prevent.
* `tan init` calls it through `tan.core.scaffold.sdk_pointer_json` while
  writing `.alp/sdk-path`, i.e. AFTER the customer's project files already
  landed.
* `tan doctor` calls it inside its own try/except, so it degrades to
  `doctor.internal-failure` instead -- a fabricated "this host is broken"
  verdict on a host that is fine.

The realistic trigger is `SOURCE_DATE_EPOCH` in MILLISECONDS (1700000000000 is
year 55840, not 2023), and CI and reproducible-build environments are exactly
what set that variable.

**Out of range is REFUSED, not rendered, and the range is `datetime`'s -- year
[1; 9999] -- on every platform.** The arithmetic is deliberately `datetime`'s
and not `time.gmtime`'s: gmtime's range is the platform's `time_t`, so the same
env var used to render `55840-11-08T22:13:20Z` on glibc and fall back to the
wall clock on Windows, whose CRT rejects the year. One input, two answers, from
a variable whose entire purpose is reproducibility -- and `253402300799`
(9999-12-31T23:59:59Z) split the same way in the other direction. `datetime`'s
range is fixed by the language, so the fallback now happens on the same inputs
everywhere. Note this is still catch-not-predict: the OverflowError is caught,
never anticipated -- only the range that raises it is now portable.

**This deliberately DIVERGES from the Rust oracle, which renders the 5-digit
year.** `tan_core::clock::format_iso8601_utc` does its own proleptic-Gregorian
arithmetic (Hinnant's `civil_from_days`) and formats `{year:04}`, which widens
rather than truncates, so the Rust emits `55840-11-08T22:13:20.000Z` and cannot
fail. Matching that would mean hand-rolling the same civil-date algorithm here
to emit a stamp no consumer can read back: `%Y` is exactly four digits (see
`_strptime.TimeRE`), and ECMAScript's Date Time String Format spells a year
past 9999 as `+055840`, not `55840`. The wall clock is a wrong-but-parseable
answer to an epoch that was already nonsense; a 5-digit year is an unparseable
one. Fixing the shared bug beats bug-for-bug parity here -- if the Rust is ever
unfrozen, it should refuse the same range.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

#: Both output shapes fall back to the Unix epoch itself: a wrong-but-shaped
#: stamp keeps the envelope parseable, which is the whole point.
_FALLBACK = "1970-01-01T00:00:00"

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def generated_at_iso(*, millis: bool = False) -> str:
    """`SOURCE_DATE_EPOCH` when set and usable, else the wall clock, as
    `YYYY-MM-DDTHH:MM:SSZ` -- or `YYYY-MM-DDTHH:MM:SS.mmmZ` with `millis=True`.

    Two shapes because the callers' committed goldens pin two: `tan doctor`'s
    `data.generatedAt` and `.alp/sdk-path`'s `updatedAt` carry seconds,
    `tan debug-config`'s `data.generatedAt` carries milliseconds. Unifying the
    WIRE shapes is a contract change plus a golden rewrite; unifying the code
    behind them is not, so only the code is unified here.

    An unparseable OR out-of-range value is the clock -- never an error,
    because no caller of a timestamp is in a position to report one.
    """
    seconds: float | int = time.time()
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is not None:
        try:
            # Kept as an `int`, never floated: `float(int("1" * 400))` raises
            # OverflowError, which this helper promises never to let out.
            seconds = int(raw.strip())
        except ValueError:
            pass
    for candidate in (seconds, time.time()):
        try:
            moment = _UNIX_EPOCH + timedelta(seconds=candidate)
        except (OverflowError, OSError, ValueError):
            continue  # outside year [1; 9999] -- try the clock
        return (
            moment.replace(tzinfo=None).isoformat(
                timespec="milliseconds" if millis else "seconds"
            )
            + "Z"
        )
    # The supplied stamp AND the wall clock are both unusable: still no throw.
    return f"{_FALLBACK}.000Z" if millis else f"{_FALLBACK}Z"
