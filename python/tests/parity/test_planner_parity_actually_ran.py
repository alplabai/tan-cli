# SPDX-License-Identifier: Apache-2.0
"""The one assertion in `tests/parity/` that is NOT under the byte-parity
module's own skip mark (tan-cli#500).

WHY IT LIVES IN ITS OWN FILE. `test_planner_emit_parity.py` carries a
module-level

    pytestmark = pytest.mark.skipif(not HAS_UPSTREAM, ...)

and `pytest` marks ACCUMULATE -- a function-level mark cannot cancel a
module-level one. So every test in that module skips together, including its
own non-vacuity canary `test_the_breadth_layer_still_covers_every_board`. The
canary measures how much the layer compared; it cannot measure that the layer
ran at all, because it is gated by the same condition. A separate module is the
only place an assertion can stand outside that gate.

WHAT WENT WRONG. `HAS_UPSTREAM` requires
`<ALP_SDK_ROOT>/scripts/alp_orchestrate/__init__.py`. The CI jobs that run this
suite with an SDK bound pre-flight-guard a DIFFERENT file --
`<ALP_SDK_ROOT>/scripts/alp_project.py`. A checkout that satisfies the guard
and ships no `scripts/alp_orchestrate/` therefore drops all 775 collected cases
in that module to SKIP at exit 0, with the canary skipping alongside them, and
`parity.yml`'s `python-tests` job stays green. That is not a hypothetical end
state: it is the STATED one -- the relocation's whole point is that alp-sdk
eventually stops shipping `scripts/alp_orchestrate/`, and the skip reason in
that module says so in as many words.

The cost is not a few cases. Measured coverage of `tan/planner/**` (3998
statements): 83% with this suite, 27% without it, with `zephyr_board.py` (261
statements), `project_emit/dts.py` (219), `west_libs.py` (95),
`native_sim.py` (61) and `hw_info.py` (57) all falling to 0%. A two-line
mutation to `tan/planner/partition.py` that halved every emitted partition
`base_kib` and doubled every ospi flash capacity produced an IDENTICAL suite
result with the package absent -- the defect was invisible.

WHAT THIS FILE DOES NOT DO. It does not require an SDK. No `ALP_SDK_ROOT` at
all is the everyday `pull_request` path (`ci.yml` binds a root only under
`sdk_parity: true`), and a run that never claimed to compare anything is
honest. What it refuses is the middle state: a root IS bound, CI believes the
byte-parity layer ran, and the layer silently compared nothing.
"""
from __future__ import annotations

import pytest

from tests.conftest import REAL_ENVIRON, sdk_root

#: The variables `sdk_root()` consults, in its own precedence order.
SDK_ROOT_VARS = ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT")

#: What the environment ACTUALLY said at collection time, before the autouse
#: `_scrub_sdk_discovery_env` deleted it. `sdk_root()` returns `None` both when
#: nothing was set and when what was set does not resolve -- a typo, a checkout
#: step that silently produced nothing, a path that moved -- and those are not
#: the same event. Without this the guard below would answer "no ALP_SDK_ROOT
#: bound" to an operator who had bound one, which is the same species of
#: comfortable-but-false report this whole file exists to refuse.
BOUND_VARS = tuple(
    (var, REAL_ENVIRON[var])
    for var in SDK_ROOT_VARS
    if REAL_ENVIRON.get(var, "").strip()
)

#: The file `test_planner_emit_parity.py`'s `HAS_UPSTREAM` actually reads.
#: Kept as a relative tuple rather than a joined path so the failure message
#: can render it the same way on every host.
ORCHESTRATOR_PACKAGE = ("scripts", "alp_orchestrate", "__init__.py")

#: MODULE IMPORT TIME, not inside the test -- `tests/conftest.py`'s
#: `_scrub_sdk_discovery_env` is autouse and deletes `ALP_SDK_ROOT` from the
#: process environment before every test function runs, so a `sdk_root()` call
#: made from a test body always sees `None` and always skips. Written the wrong
#: way first, and all three states (no root / good root / package-less root)
#: reported SKIP -- a guard against a silent skip that was itself a silent
#: skip. `sdk_root()`'s own docstring states the rule; the measurement is what
#: enforced it. `test_planner_emit_parity.py` binds it the same way.
SDK = sdk_root()


def test_a_bound_sdk_root_still_ships_the_planner_oracle() -> None:
    """A bound `ALP_SDK_ROOT` must be able to answer the byte-parity suite.

    This is the assertion that turns the coverage cliff from a silent SKIP
    into a red build. It deliberately reports the DIFFERENCE between the two
    files -- the one CI pre-flights and the one the suite reads -- because the
    whole defect is that they are not the same file and nobody noticed.
    """
    sdk = SDK
    if sdk is None:
        # A root that was SET but did not resolve is a third state, and it is
        # the dangerous one: `sdk_root()` collapses it into the same `None` as
        # "nothing bound", so an ALP_SDK_ROOT pointing at a typo, a moved
        # checkout, or a clone step that quietly produced nothing would skip
        # here under the message "no ALP_SDK_ROOT bound" -- false, and false in
        # the reassuring direction. Only the hand-written `alp_project.py`
        # pre-flight greps in `ci.yml` / `parity.yml` catch it, and neither
        # runs on the local gate.
        assert not BOUND_VARS, (
            "these are set but do not name a usable alp-sdk checkout, so "
            "`sdk_root()` returned None and the byte-parity layer will SKIP "
            "as though nothing had been bound at all:\n  "
            + "\n  ".join(f"{var}={value!r}" for var, value in BOUND_VARS)
            + "\n\n`sdk_root()` accepts a root only when "
            "<root>/scripts/alp_project.py is a file under it. Check the path "
            "exists and that the checkout step actually produced one; do NOT "
            "unset the variable to make this pass."
        )
        pytest.skip(
            "no ALP_SDK_ROOT / ALP_SDK_PARITY_ROOT bound -- the byte-parity "
            "layer is legitimately not running, and this run never claimed "
            "otherwise"
        )

    oracle = sdk.joinpath(*ORCHESTRATOR_PACKAGE)
    preflight = sdk / "scripts" / "alp_project.py"
    assert oracle.is_file(), (
        f"ALP_SDK_ROOT is bound to {sdk} and "
        f"{preflight.name} is {'present' if preflight.is_file() else 'absent'} "
        f"there, but {'/'.join(ORCHESTRATOR_PACKAGE)} is missing -- so "
        f"test_planner_emit_parity.py's 775 cases all SKIP while CI's "
        f"pre-flight guard (which checks scripts/alp_project.py, a DIFFERENT "
        f"file) reports the root as usable.\n\n"
        f"That combination means the planner byte-parity layer compared "
        f"NOTHING on this run: measured coverage of tan/planner/** drops from "
        f"83% to 27% without it, and zephyr_board.py, project_emit/dts.py, "
        f"west_libs.py, native_sim.py and hw_info.py all fall to 0%.\n\n"
        f"If alp-sdk has genuinely retired scripts/alp_orchestrate/, this "
        f"failure is the intended signal: the oracle is gone and the parity "
        f"layer needs replacing, not skipping. Do not silence it by widening "
        f"the skip -- that restores the exact hole tan-cli#500 closed."
    )
