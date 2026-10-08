# SPDX-License-Identifier: Apache-2.0
"""`--emit dts-overlay` appends the M33-owned assignable nodes (alp-sdk#2673).

Upstream `alp_project._run_v2_per_core_emit` appends
`project_m33_overlay(project, args.core)[0]` to the `dts-overlay` output in
both the scoped (`--core`) and unscoped paths, and turns an
`OrchestratorError` into a refusal. `tan.planner_emit._render_v1_shaped` is
tan's mirror of that function and must do the same.

The bound SDK's `core-ownership.yaml` has no `m33:` block for any assignable
instance (the real one is a55-only), so the ownership document is substituted
at the `tan.planner.ownership.load_ownership_doc` seam AFTER the project has
loaded -- the board.yaml loader keeps the real one.

Real-SDK-gated: reads the bound checkout's metadata and
`examples/multicore/rpmsg-v2n`.
"""
from __future__ import annotations

import shutil

import pytest
import yaml

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound metadata/ tree and examples. "
           "A SKIP about the missing root, not a pass.",
)

#: What `m33_overlay` renders for the substituted `e1m_uart0` block below.
_OWNED = (
    '&sci0 {\n\tstatus = "okay";\n};\n'
    "/ {\n\taliases {\n\t\talp-uart9 = &sci0;\n\t};\n};"
)


def _board_yaml(tmp_path, ownership=None):
    dst = tmp_path / "p"
    shutil.copytree(SDK / "examples" / "multicore" / "rpmsg-v2n", dst)
    board = dst / "board.yaml"
    if ownership is not None:
        board.write_text(
            board.read_text(encoding="utf-8") + "\n"
            + yaml.safe_dump({"ownership": ownership}),
            encoding="utf-8")
    return board


def _doc_with_m33_uart0():
    from tan.planner.ownership import load_ownership_doc

    doc = load_ownership_doc(SDK / "metadata", "v2n")
    doc["assignable"]["e1m_uart0"]["m33"] = {
        "dt_label": "sci0", "alias": "alp-uart9", "kconfig": ["CONFIG_SERIAL=y"]}
    return doc


def _render(board, core):
    from tan import planner_emit

    return planner_emit.render(
        "dts-overlay", sdk_root=SDK, board_yaml=board, core=core)


def _own_m33_uart0(monkeypatch):
    """Substitute an ownership doc that carries the `m33:` block."""
    import tan.planner.ownership as own

    doc = _doc_with_m33_uart0()
    monkeypatch.setattr(own, "load_ownership_doc", lambda *a, **k: doc)


def test_scoped_overlay_enables_the_m33_owned_node(tmp_path, monkeypatch):
    board = _board_yaml(tmp_path)
    _own_m33_uart0(monkeypatch)
    # The loader would refuse an m33 override under the real doc, so set the
    # ownership on the loaded project directly.
    from tan import planner_emit
    import tan.planner as planner

    project = planner.load_board_yaml(board)
    project.ownership = {"e1m_uart0": "m33"}
    monkeypatch.setattr(planner, "load_board_yaml", lambda *a, **k: project)
    out = planner_emit.render("dts-overlay", sdk_root=SDK, board_yaml=board,
                              core="m33_sm")
    assert out.endswith(
        "\n/* Assignable peripherals owned by the M33 "
        "(board.yaml `ownership:`). */\n" + _OWNED + "\n")


def test_unscoped_overlay_enables_the_m33_owned_node(tmp_path, monkeypatch):
    board = _board_yaml(tmp_path)
    _own_m33_uart0(monkeypatch)
    from tan import planner_emit
    import tan.planner as planner

    project = planner.load_board_yaml(board)
    project.ownership = {"e1m_uart0": "m33"}
    monkeypatch.setattr(planner, "load_board_yaml", lambda *a, **k: project)
    out = planner_emit.render("dts-overlay", sdk_root=SDK, board_yaml=board)
    assert "&sci0 {" in out and "alp-uart9 = &sci0;" in out


def test_a55_scope_and_default_ownership_add_nothing(tmp_path, monkeypatch):
    board = _board_yaml(tmp_path)
    plain = _render(board, "m33_sm")
    assert "Assignable peripherals owned by the M33" not in plain
    assert "&sci0 {" not in plain


def test_missing_m33_block_is_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    from tan import planner_emit
    import tan.planner as planner

    board = _board_yaml(tmp_path)
    project = planner.load_board_yaml(board)
    project.ownership = {"e1m_uart0": "m33"}
    monkeypatch.setattr(planner, "load_board_yaml", lambda *a, **k: project)
    # The real doc carries no `m33:` block for e1m_uart0.
    with pytest.raises(planner_emit.PlannerEmitError,
                       match="e1m_uart0 is assigned to 'm33'.*no `m33:`"):
        planner_emit.render("dts-overlay", sdk_root=SDK, board_yaml=board,
                            core="m33_sm")
