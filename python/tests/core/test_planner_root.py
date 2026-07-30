# SPDX-License-Identifier: Apache-2.0
"""The planner's SDK-root binding -- the one mechanical risk of the relocation.

alp-sdk's `alp_orchestrate/paths.py` derived `REPO` by walking up from its own
`__file__`. Inside `tan` that walk lands on the `tan` package, so the root became
an explicit binding. What makes it delicate is WHEN it is read: `paths.py`
evaluates `REPO = sdk_root()` at module scope, and a dozen planner functions take
`metadata_root: Path = METADATA_ROOT` as a *default argument*, which binds at
import time too. So the failure mode of getting this wrong is not an exception
somewhere obvious -- it is a planner that reads a different tree's `metadata/**`
than the caller believes, and emits a plan for the wrong SDK.

These cover the guards; `tests/parity/test_planner_emit_parity.py` covers what
they protect.
"""

from __future__ import annotations

import sys

import pytest

from tan import planner_root


@pytest.fixture(autouse=True)
def _isolate_binding(monkeypatch):
    """Bind/unbind is process-global by design; keep it out of other tests."""
    monkeypatch.setattr(planner_root, "_BOUND", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield


def test_an_unbound_root_raises_instead_of_guessing():
    # Never a default, never a walk from __file__, never "". An unbound import
    # cannot be repaired after the fact, so it has to be loud at the boundary.
    with pytest.raises(planner_root.PlannerRootError) as err:
        planner_root.sdk_root()
    assert "bind_sdk_root" in str(err.value)


def test_binding_resolves_the_root_and_touches_sys_path_not_at_all(tmp_path):
    # `<sdk>/scripts` used to go on sys.path here, back when the fact readers
    # (`alp_project`, `alp_registries`, `alp_cli.validator`) still lived in
    # alp-sdk. They have relocated into `tan.planner`, so nothing in tan's own
    # execution resolves through that entry -- and NOT adding it is what makes
    # the zero import closure structural instead of merely measured: a
    # re-introduced `from alp_project import ...` now fails at the moment it is
    # written, rather than working silently until someone re-runs
    # `test_the_in_process_path_loads_none_of_the_sdks_python`.
    root = tmp_path / "sdk"
    (root / "scripts").mkdir(parents=True)
    before = list(sys.path)
    assert planner_root.bind_sdk_root(root) == root.resolve()
    assert planner_root.sdk_root() == root.resolve()
    assert sys.path == before


def test_rebinding_the_same_root_is_idempotent(tmp_path):
    root = tmp_path / "sdk"
    (root / "scripts").mkdir(parents=True)
    before = list(sys.path)
    planner_root.bind_sdk_root(root)
    planner_root.bind_sdk_root(root)
    assert planner_root.sdk_root() == root.resolve()
    assert sys.path == before


def test_rebinding_before_import_swaps_the_root_and_leaves_no_first_root_route(
        tmp_path, monkeypatch):
    # Legal: nothing has frozen a constant yet, so the second root must win
    # outright. The hazard this covers is SHADOWING -- the first SDK's `scripts`
    # staying importable so an `alp_project` from checkout A answers a build
    # against checkout B. Binding adds no import route for either root, so the
    # shadow has nowhere to live; asserting that is what keeps a future
    # convenience `sys.path.insert` from reintroducing it.
    monkeypatch.delitem(sys.modules, "tan.planner", raising=False)
    first, second = tmp_path / "a", tmp_path / "b"
    for r in (first, second):
        (r / "scripts").mkdir(parents=True)
    before = list(sys.path)
    planner_root.bind_sdk_root(first)
    planner_root.bind_sdk_root(second)
    assert planner_root.sdk_root() == second.resolve()
    assert sys.path == before
    for r in (first, second):
        assert str(r.resolve() / "scripts") not in sys.path


def test_rebinding_a_different_root_after_import_is_refused(tmp_path, monkeypatch):
    # The whole point. Past this line the planner's METADATA_ROOT is already
    # frozen on the first root; silently accepting a second one is a build
    # against an SDK checkout the caller never named.
    monkeypatch.setitem(sys.modules, "tan.planner", object())
    first, second = tmp_path / "a", tmp_path / "b"
    planner_root.bind_sdk_root(first)
    with pytest.raises(planner_root.PlannerRootError) as err:
        planner_root.bind_sdk_root(second)
    assert "already bound" in str(err.value)
