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


def test_binding_resolves_the_root_and_exposes_the_sdk_scripts_dir(tmp_path):
    # `<sdk>/scripts` on sys.path is what the fact-reader modules that STAYED in
    # alp-sdk (`alp_project`, `alp_registries`, `alp_cli.validator`) are imported
    # through -- the in-process equivalent of the old
    # `PYTHONPATH=<sdk>/scripts` subprocess environment.
    root = tmp_path / "sdk"
    (root / "scripts").mkdir(parents=True)
    assert planner_root.bind_sdk_root(root) == root.resolve()
    assert planner_root.sdk_root() == root.resolve()
    assert str(root.resolve() / "scripts") in sys.path


def test_rebinding_the_same_root_is_idempotent(tmp_path):
    root = tmp_path / "sdk"
    (root / "scripts").mkdir(parents=True)
    planner_root.bind_sdk_root(root)
    planner_root.bind_sdk_root(root)
    assert sys.path.count(str(root.resolve() / "scripts")) == 1


def test_rebinding_before_import_swaps_the_scripts_path(tmp_path, monkeypatch):
    # Legal: nothing has frozen a constant yet. The OLD scripts dir must leave
    # sys.path, or an `alp_project` from the first SDK shadows the second's.
    monkeypatch.delitem(sys.modules, "tan.planner", raising=False)
    first, second = tmp_path / "a", tmp_path / "b"
    for r in (first, second):
        (r / "scripts").mkdir(parents=True)
    planner_root.bind_sdk_root(first)
    planner_root.bind_sdk_root(second)
    assert str(first.resolve() / "scripts") not in sys.path
    assert str(second.resolve() / "scripts") in sys.path


def test_rebinding_a_different_root_after_import_is_refused(tmp_path, monkeypatch):
    # The whole point. Past this line the planner's METADATA_ROOT is already
    # frozen on the first root; silently accepting a second one is a build
    # against an SDK checkout the caller never named.
    monkeypatch.setitem(sys.modules, "tan.planner", object())
    first, second = tmp_path / "a", tmp_path / "b"
    for r in (first, second):
        (r / "scripts").mkdir(parents=True)
    planner_root.bind_sdk_root(first)
    with pytest.raises(planner_root.PlannerRootError) as err:
        planner_root.bind_sdk_root(second)
    assert "already bound" in str(err.value)
