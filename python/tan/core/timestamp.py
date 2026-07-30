# SPDX-License-Identifier: Apache-2.0
"""The port's ONE `generatedAt`/`updatedAt` timestamp helper. It NEVER raises.

`SOURCE_DATE_EPOCH` wins over the clock, so a captured envelope -- or a written
`.alp/sdk-path` -- is reproducible. Counterpart of `crate::util::
generated_at_iso` + `tan_core::clock::format_iso8601_utc`, which cannot fail
because the Rust does the civil-date arithmetic itself.

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

The realistic trigger is `SOURCE_DATE_EPOCH` in MILLISECONDS (1700000000000 ->
year 55838), and CI and reproducible-build environments are exactly what set
that variable. `time.gmtime` raises OverflowError or OSError (Errno 22 on
Windows) once past the platform's `time_t` range, and that range differs per
platform, so the value cannot be portably pre-validated -- catch rather than
predict.
"""

from __future__ import annotations

import os
import time

#: Both output shapes fall back to the Unix epoch itself: a wrong-but-shaped
#: stamp keeps the envelope parseable, which is the whole point.
_FALLBACK = "1970-01-01T00:00:00"


def generated_at_iso(*, millis: bool = False) -> str:
    """`SOURCE_DATE_EPOCH` when set and usable, else the wall clock, as
    `YYYY-MM-DDTHH:MM:SSZ` -- or `YYYY-MM-DDTHH:MM:SS.mmmZ` with `millis=True`.

    Two shapes because the callers' committed goldens pin two: `tan doctor`'s
    `data.generatedAt` and `.alp/sdk-path`'s `updatedAt` carry seconds,
    `tan debug-config`'s `data.generatedAt` carries milliseconds. Unifying the
    WIRE shapes is a contract change plus a golden rewrite; unifying the code
    behind them is not, so only the code is unified here.

    An unparseable value is the clock, exactly as the Rust does it -- never an
    error, because no caller of a timestamp is in a position to report one.
    """
    seconds = time.time()
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is not None:
        try:
            seconds = float(int(raw.strip()))
        except ValueError:
            pass
    for candidate in (seconds, time.time()):
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(candidate)))
        except (OverflowError, OSError, ValueError):
            continue  # outside this platform's time_t range -- try the clock
        if not millis:
            return f"{stamp}Z"
        return f"{stamp}.{int((candidate - int(candidate)) * 1000):03d}Z"
    # The supplied stamp AND the wall clock are both unusable: still no throw.
    return f"{_FALLBACK}.000Z" if millis else f"{_FALLBACK}Z"
