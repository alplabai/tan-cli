# SPDX-License-Identifier: Apache-2.0
"""The `tan.planner` drift guard in `tests/conftest.py` cannot fail if it is
gone -- or if it is present but neutered.

`unsharded-python-canary.yml` (tan-cli#943 review round 3) is a SECONDARY,
weekly-cadence backstop for the same defect shape; the `pytest_runtest_
teardown` hookwrapper in `tests/conftest.py` is the PRIMARY defence -- it
runs on every test, on every PR. But that PRIMARY defence had no guard of
its own, and this file's first check -- a text search for the hook's
signature and its `@pytest.hookimpl(wrapper=True)` decorator -- turned out
not to BE one (tan-cli#944 review): two mutants beat it while leaving the
hook either absent or inert:

* deleting the hook entirely and leaving only a HISTORY comment that quotes
  its old `def pytest_runtest_teardown(item, nextitem):` and
  `@pytest.hookimpl(wrapper=True)` lines verbatim -- the text search still
  finds those quoted strings inside the comment and passes, even though
  nothing is registered any more (`gate -> 1 passed`, plus a synthetic
  two-copy-drift probe -> `2 passed`, no error);
* keeping both lines but replacing the body with a no-op (`result = yield`
  / `return result`) -- the text search still finds both lines and passes,
  and the same synthetic probe again shows `2 passed`, no error, where HEAD
  reds it as `2 passed, 1 error`.

Neither mutant is caught by grepping `conftest.py`'s TEXT, because neither
changes the text the first check greps for. What actually matters is a
property of what pytest REGISTERED and what the registered function DOES,
so the two checks below test those directly instead:

* `test_teardown_hook_is_registered_as_a_wrapper_impl` walks
  `request.config.pluginmanager.hook.pytest_runtest_teardown.
  get_hookimpls()` -- the live hookimpl list pluggy actually built -- and
  requires one whose `plugin.__file__` resolves to this `conftest.py` and
  whose `.wrapper` is `True`. A HISTORY comment does not re-register
  anything, so this reds under the deletion mutant; a function auto-
  registered without the `wrapper=True` decorator comes back as a PLAIN
  (non-wrapper) hookimpl, so `.wrapper` is `False` and this reds too.
* `test_teardown_hook_actually_detects_a_synthetic_drift` takes the
  registered hookimpl's own `plugin` module, plants a synthetic tan-cli#943-
  shaped drift (two distinct `types.ModuleType("tan.planner.zz")` objects,
  one in `sys.modules`, the other hung off a FAKE `tan.planner` parent also
  planted in `sys.modules`, so the probe does not depend on whether the
  real `tan.planner` happens to be imported yet), drives the hook's own
  generator function directly the way pluggy drives a wrapper (`next()` to
  the `yield`, then `next()` again to resume past it), and requires an
  `AssertionError` naming the drift. This is what reds under the no-op-body
  mutant: registration is intact, but the body that would have raised no
  longer does anything.

Both are needed together: registration proves the hook is WIRED, the
behavioural probe proves its BODY still works. Either mutant on its own
defeats exactly one of the two.

Measured (tan-cli#943 review round 3, still true here): with the hook
removed and #953's fix intact, `pytest tests/commands tests/planner -q` is
`0 failed` -- the canary's own bound is blind to the hook's absence when
nothing is currently leaking, which is the common case. This gate (all
three checks together) closes that: it does not re-derive whether the hook
WORKS against a REAL leaking test (the mutation-proof block comment
directly above the hook in `conftest.py`, and the synthetic probe here,
both do that), only that the hook still EXISTS, is REGISTERED as a
wrapper, and its BODY still raises on a drift -- the same completeness
shape `_MIN_BOUNDED_JOBS` in
`test_parity_workflow_concurrency_and_timeouts.py` uses to catch a workflow
job silently disappearing rather than trusting that "it was there when the
gate was written" stays true forever.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

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
    # This is a "cannot pass by matching nothing" check, not a "the hook
    # works" check -- CONFTEST.read_text() itself raises FileNotFoundError
    # if the file were ever renamed out from under this gate, so a rename
    # cannot slip past as a silent pass either. What it does NOT prove is
    # that pytest actually REGISTERED what this text describes, or that the
    # registered function's body still does anything -- see the two checks
    # below for that (tan-cli#944 review).


def _conftest_teardown_hookimpls(request: pytest.FixtureRequest) -> list:
    """The `pytest_runtest_teardown` hookimpls pluggy actually registered
    that came from `tests/conftest.py`, matched by resolved file identity
    rather than by plugin name -- pytest derives the plugin name from the
    conftest's path and that string shape is not a contract this gate
    should depend on.
    """
    hookcaller = request.config.pluginmanager.hook.pytest_runtest_teardown
    return [
        impl
        for impl in hookcaller.get_hookimpls()
        if getattr(impl.plugin, "__file__", None) is not None
        and Path(impl.plugin.__file__).resolve() == CONFTEST.resolve()
    ]


def test_teardown_hook_is_registered_as_a_wrapper_impl(
    request: pytest.FixtureRequest,
) -> None:
    matches = _conftest_teardown_hookimpls(request)
    assert matches, (
        f"no pytest_runtest_teardown hookimpl is registered from {CONFTEST} "
        "-- the hook may have been deleted (a HISTORY comment quoting its "
        "old text does not re-register it: get_hookimpls() reflects what "
        "pluggy actually wired up, not what the source text says)."
    )
    assert all(impl.wrapper for impl in matches), (
        f"{CONFTEST} registers pytest_runtest_teardown but not as a "
        "hookwrapper (impl.wrapper is False for at least one match) -- the "
        "@pytest.hookimpl(wrapper=True) decorator is missing or malformed. "
        "pytest auto-registers a same-named function as a PLAIN hookimpl "
        "in that case, and calling a generator function as a plain "
        "hookimpl only constructs the generator without ever iterating it, "
        "so the drift check inside never runs."
    )


def test_teardown_hook_actually_detects_a_synthetic_drift(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matches = _conftest_teardown_hookimpls(request)
    assert matches, "see test_teardown_hook_is_registered_as_a_wrapper_impl"
    ct = matches[0].plugin

    monkeypatch.setattr(ct, "_PLANNER_DRIFT_ALREADY_REPORTED", set())

    # Two distinct real ModuleType objects named identically
    # ("tan.planner.zz") -- same repr, different id() -- one parked
    # directly in sys.modules, the other hung off a FAKE tan.planner
    # parent that is ALSO planted in sys.modules, so this probe does not
    # depend on whether the real tan.planner happens to be imported yet.
    fake_parent = types.ModuleType("tan.planner")
    child_on_parent = types.ModuleType("tan.planner.zz")
    fake_parent.zz = child_on_parent
    child_in_sys_modules = types.ModuleType("tan.planner.zz")
    monkeypatch.setitem(sys.modules, "tan.planner", fake_parent)
    monkeypatch.setitem(sys.modules, "tan.planner.zz", child_in_sys_modules)

    gen = ct.pytest_runtest_teardown(request.node, None)
    next(gen)  # advance to `result = yield`, mirroring pluggy's own drive
    with pytest.raises(AssertionError, match="drifted after"):
        next(gen)  # resume past the yield -- the drift walk raises here
