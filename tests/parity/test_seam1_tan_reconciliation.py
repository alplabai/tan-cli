# SPDX-License-Identifier: Apache-2.0
"""tan-cli#320: `seam1_field_diff._tan_reconciled_refusal` -- the ONE piece of
`seam1_field_diff.py` that is NOT vendored-identical with alp-sdk's own copy
(see that module's tan-cli#320 docstring addendum for why the two can still
stay byte-identical). Deliberately its own file rather than folded into
`test_seam1_field_diff.py`: that file's own docstring says "Vendored from
alp-sdk's tests/parity/test_seam1_field_diff.py -- KEEP IN LOCKSTEP with the
original", and alp-sdk's copy has no `tan` package to test against.

Run: python -m pytest tests/parity/test_seam1_tan_reconciliation.py -q
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seam1_field_diff as s  # noqa: E402


def test_returns_none_when_tan_is_not_importable(monkeypatch):
    """The no-`tan`-installed case (alp-sdk's own copy of this file, always;
    a bare local checkout here without `pip install ./python`): must fall
    back to `None`, not raise or silently claim a verdict.

    `sys.modules["tan"] = None` is the documented way to force `import tan`
    to raise ImportError regardless of what is actually on sys.path.
    """
    monkeypatch.setitem(sys.modules, "tan", None)
    monkeypatch.setitem(sys.modules, "tan.planner_root", None)
    assert s._tan_reconciled_refusal(Path("/nonexistent"), "board.yaml") is None


def test_tan_also_refusing_is_reconciled_true(monkeypatch, tmp_path):
    """tan raising anything for the same board counts as "also refuses"."""
    def _boom(mode, *, root, board_yaml, **kw):
        raise RuntimeError("SoM E1M-NX9101 hw_rev 'r1' exists but is not buildable")

    fake_root_mod = types.SimpleNamespace(emit=_boom)
    monkeypatch.setitem(sys.modules, "tan", types.SimpleNamespace(planner_root=fake_root_mod))
    monkeypatch.setitem(sys.modules, "tan.planner_root", fake_root_mod)

    refused, detail = s._tan_reconciled_refusal(tmp_path, "board.yaml")
    assert refused is True
    assert "not buildable" in detail


def test_tan_succeeding_is_reconciled_false(monkeypatch, tmp_path):
    """tan's emit succeeding where alp-sdk's failed is the real #320-class
    divergence -- must be reported as NOT reconciled, not silently passed."""
    def _ok(mode, *, root, board_yaml, **kw):
        return "{}"

    fake_root_mod = types.SimpleNamespace(emit=_ok)
    monkeypatch.setitem(sys.modules, "tan", types.SimpleNamespace(planner_root=fake_root_mod))
    monkeypatch.setitem(sys.modules, "tan.planner_root", fake_root_mod)

    refused, detail = s._tan_reconciled_refusal(tmp_path, "board.yaml")
    assert refused is False
    assert detail == ""
