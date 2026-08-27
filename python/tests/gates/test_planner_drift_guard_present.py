# SPDX-License-Identifier: Apache-2.0
"""The `tan.planner` drift guard in `tests/conftest.py` cannot fail if it is
gone.

`unsharded-python-canary.yml` (tan-cli#943 review round 3) is a SECONDARY,
weekly-cadence backstop for the same defect shape; the `pytest_runtest_
teardown` hookwrapper in `tests/conftest.py` is the PRIMARY defence -- it
runs on every test, on every PR. But that PRIMARY defence has no guard of
its own: delete the hook (or its `def pytest_runtest_teardown` line, or the
`@pytest.hookimpl` decorator that makes pytest treat it as a hook rather
than an inert function) in some future `conftest.py` refactor, and nothing
in the suite notices except the canary -- up to a week later, and only for
the narrow `tests/commands` + `tests/planner` pairing it happens to cover,
not the whole-suite reach the hook itself has.

Measured (tan-cli#943 review round 3): with the hook removed and #953's
fix intact, `pytest tests/commands tests/planner -q` is `0 failed` -- the
canary's own bound is blind to the hook's absence when nothing is
currently leaking, which is the common case. This gate closes that:
it does not re-derive whether the hook WORKS (the mutation-proof block
comment directly above the hook in `conftest.py` does that), only that the
hook still EXISTS to be exercised at all -- the same completeness shape
`_MIN_BOUNDED_JOBS` in `test_parity_workflow_concurrency_and_timeouts.py`
uses to catch a workflow job silently disappearing rather than trusting
that "it was there when the gate was written" stays true forever.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONFTEST = REPO / "python" / "tests" / "conftest.py"


def test_conftest_still_defines_the_planner_drift_teardown_hook():
    text = CONFTEST.read_text(encoding="utf-8")
    assert "def pytest_runtest_teardown(item, nextitem):" in text, (
        f"{CONFTEST} no longer defines pytest_runtest_teardown(item, "
        "nextitem) -- this is the tan-cli#943 tan.planner drift guard "
        "(sys.modules vs. parent-attribute identity, checked after every "
        "test). If it was intentionally renamed or restructured, update "
        "this gate to match the new shape; if it was deleted, the suite "
        "has lost its PRIMARY defence against the #943 class of bug and "
        "only the weekly unsharded-python-canary.yml backstop remains."
    )
    assert "@pytest.hookimpl(wrapper=True)" in text, (
        f"{CONFTEST} defines pytest_runtest_teardown but the "
        "@pytest.hookimpl(wrapper=True) decorator immediately above it is "
        "gone -- without it pytest never registers the function as a "
        "hookwrapper at all, and it silently stops running (an earlier "
        "draft's plain @pytest.fixture(autouse=True) shape produced a real "
        "false positive for exactly this reason; see the block comment "
        "above the hook for what replaced it and why)."
    )
