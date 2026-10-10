# SPDX-License-Identifier: Apache-2.0
"""A new flash backend must not join the registry without someone deciding
whether the wrong-board SW-DP ID guard covers it (tan-cli#609).

## What went wrong without this gate

`flash_cmd` gated the `flash.dpidr-preflight-unarmed` advisory on `method ==
SWD_PROBE_METHOD`. The AEN dispatches `alif_mram_jlink` (Flow D), so a real
MRAM write on `e1m-aen-evk-01` emitted `ISSUES = []` -- no wrong-board guard
AND no signal that there was none -- on a bench where one J-Link serial is
OEM-cloned across two probes and `JLinkExe` selects by serial alone. The
advisory tracked the method someone had wired it to, not the methods that can
write.

An inline literal cannot be caught by a test, because there is nothing to
compare it against. A TABLE can: `DPIDR_GUARD_COVERAGE` has to name every
registered method, so adding a backend without classifying it fails here
rather than shipping as silence.

The gate deliberately does NOT assert WHICH side any method is on -- that is a
judgement about the backend, recorded in `DPIDR_GUARD_COVERAGE`'s own comment
and pinned for the two covered ones by the behaviour tests in
`tests/commands/test_flash_command.py`. What it asserts is that the judgement
was made at all.
"""
from __future__ import annotations

from tan.core.flash_plan import (
    DPIDR_GUARD_COVERAGE,
    FLOW_D_METHOD,
    registry_keys,
)


def test_dpidr_guard_coverage_names_every_registered_method():
    """Exact set equality, both directions. A method in the registry and not
    the table is the #609 silence; a method in the table and not the registry
    is a stale entry whose comment still reads as authoritative."""
    registered = set(registry_keys())
    classified = set(DPIDR_GUARD_COVERAGE)
    assert classified == registered, (
        "DPIDR_GUARD_COVERAGE and the flash backend registry disagree.\n"
        f"  registered but unclassified: {sorted(registered - classified)}\n"
        f"  classified but unregistered: {sorted(classified - registered)}\n"
        "Every flash_method must declare whether tan composes its probe "
        "session and can therefore run the read-only SW-DP IDR preflight "
        "(True), or cannot (False). Leaving a new backend out is exactly how "
        "tan-cli#609's AEN MRAM write came to run with no guard and no signal."
    )


def test_flow_d_is_the_covered_method():
    """The coverage set as it stands, pinned so that flipping a method's side
    is a deliberate edit with a diff, not a drive-by.

    tan-cli#732 removed the second covered method (`swd_probe`) along with
    the backend itself, so Flow D is the sole entry on the `True` side today
    -- the method tan itself composes a J-Link Commander session for, which
    is what `flash_args.expect_dpidr` arms. Widening this set means teaching
    the corresponding backend to run `flow_d_preflight_script` first --
    flipping the flag alone would advertise a guard that does not run."""
    covered = {method for method, on in DPIDR_GUARD_COVERAGE.items() if on}
    assert covered == {FLOW_D_METHOD}, covered
