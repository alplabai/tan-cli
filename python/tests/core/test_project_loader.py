# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.project_loader._hwrev_pad_route_overrides` -- the
`--emit carrier-netlist` / `--emit composed-route-table` half of alp-sdk
#1025's existence gate (the SAFE half: does `hw_rev` exist at all in the
resolved `hw_revisions:` table).

`tests/core/test_sdk_revision_gate.py` proves the OTHER half reached tan --
`loader._check_hw_rev_exists`, run inside `loader.load_board_yaml` -- but
every case there drives `load_board_yaml`. Neither `--emit carrier-netlist`
nor `--emit composed-route-table` calls it: `planner_emit._render_route_table`
resolves the SKU/board preset itself and never runs the loader's per-core
resolve pipeline (see that function's own comment on why -- it mirrors
`alp_project.main()`, which dispatches these two modes before its per-core
machinery). A test that only drives `load_board_yaml` cannot see whether
`project_loader.py`'s own, independent copy of the check was ported -- it
wasn't, until now: an unrecognised `hw_rev` fell through to `{}` (empty pad-
route overrides), i.e. base-revision pad routing with a clean exit code, a
wrong-hardware emit on AEN where r1/r2 differ in real routes (IO8 ->
WIFI_GPIO30, IO10 -> WIFI_GPIO35, IO21 unrouted). This module drives the
actual front door where that bug lived: `tan.planner_emit.render`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import sdk_root

SDK = sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner_emit can bind a "
    "root and import (same requirement as the parity suite)"
)


def _fixture_board_yaml(tmp_path: Path, hw_rev: str) -> Path:
    """Same relevant shape as `test_sdk_revision_gate.py`'s own fixture: a
    self-authored board.yaml (not an in-tree alp-sdk example) naming
    `E1M-AEN801` / `e1m-evk`, so this suite's coverage does not depend on a
    file path inside a second, independently moving repo."""
    path = tmp_path / "fixture-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        f"  hw_rev: {hw_rev}\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
        "    app: ./src\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("mode", ["carrier-netlist", "composed-route-table"])
def test_an_unknown_hw_rev_refuses_through_the_route_table_emit(tmp_path, mode):
    """The #1025 safe half, ported into `project_loader.py`'s own copy.
    Drives `planner_emit.render`, NOT `load_board_yaml` -- recreating the
    `load_board_yaml`-only coverage would recreate the exact blind spot
    that let this ship."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    # Bind before importing `tan.planner` names: `paths.py` freezes
    # `REPO = sdk_root()` at import time (see `planner_root`'s module
    # docstring), and `planner_emit.render` only binds internally once it
    # actually runs -- too late for a plain `from tan.planner import ...`
    # above the call.
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    from tan import planner_emit
    from tan.planner import SdkRevisionUnknown

    board = _fixture_board_yaml(tmp_path, "r99")
    with pytest.raises(SdkRevisionUnknown) as excinfo:
        planner_emit.render(mode, sdk_root=SDK, board_yaml=board)

    message = str(excinfo.value)
    assert "r99" in message           # the unknown revision
    assert "E1M-AEN801" in message    # which SoM
    assert "r2" in message            # the available list, not just the refusal


@pytest.mark.parametrize("mode", ["carrier-netlist", "composed-route-table"])
def test_a_known_hw_rev_still_emits(tmp_path, mode):
    """The success path is unchanged: r1 -- which declares real pad-route
    overrides on AEN (IO8/IO10/IO21, see the module docstring) -- still
    renders, so the new gate does not refuse a revision that IS known."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan import planner_emit

    board = _fixture_board_yaml(tmp_path, "r1")
    text = planner_emit.render(mode, sdk_root=SDK, board_yaml=board)
    assert "E1M-AEN801" in text


def test_the_unknown_revision_refusal_is_an_orchestrator_error_subclass():
    """Existing `except OrchestratorError` handlers (`tan.planner.cli.main`'s
    `PROJECT_MODES` branch) must keep catching it."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    from tan.planner import OrchestratorError, SdkRevisionUnknown

    assert issubclass(SdkRevisionUnknown, OrchestratorError)


@pytest.mark.parametrize("mode", ["carrier-netlist", "composed-route-table"])
def test_a_reserved_hw_rev_refuses_through_the_route_table_emit(tmp_path, mode):
    """tan-cli#485 (#320 recurred): alp-sdk `1a91a232` (#1025's STATUS half,
    landed in the same function this module ports) added a second gate --
    `SdkRevisionNotBuildable` -- alongside the existence gate above, for an
    `hw_rev` that EXISTS in the table but is `status: reserved` / `status:
    tbd` / status-less. `_hwrev_pad_route_overrides` only ever carried the
    existence half (`SdkRevisionUnknown`), so `tan generate --target
    carrier-netlist` / `--target composed-route-table` happily emitted a
    full net-by-net routing table for `E1M-AEN801` `hw_rev: r3` (real
    fixture data: r3 is `status: reserved` in
    metadata/e1m_modules/aen/hw-revisions.yaml) with no defined routing --
    while `tan build` (which DOES run `load_board_yaml`, and DOES carry
    this gate -- see `tests/core/test_sdk_revision_gate.py`) refused the
    identical input.  This is the exact contradiction the issue names:
    `tan validate`/`tan build` refuse what the generators hand out.

    Fails against unfixed `project_loader.py` (verified before the fix
    landed: `_hwrev_pad_route_overrides` returned r3's pad_route_overrides
    with no exception, so this `pytest.raises` block never entered and the
    test failed with `Failed: DID NOT RAISE`)."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    from tan import planner_emit
    from tan.planner import SdkRevisionNotBuildable

    board = _fixture_board_yaml(tmp_path, "r3")
    with pytest.raises(SdkRevisionNotBuildable) as excinfo:
        planner_emit.render(mode, sdk_root=SDK, board_yaml=board)

    message = str(excinfo.value)
    assert "r3" in message            # the not-buildable revision
    assert "E1M-AEN801" in message    # which SoM
    assert "reserved" in message      # names the actual status, not just refuses
