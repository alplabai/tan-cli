# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#1982 / tan-cli#1223: `tan build` must never hand out a `bitbake`
command for a Yocto MACHINE that is known not to build.

Ported from alp-sdk's own
`tests/scripts/test_orchestrate_buildplan.py::
test_emit_build_plan_aen_a32_machine_unbuildable_blocks_command`
(alp-sdk#1967, `orchestrator.YOCTO_MACHINE_UNBUILDABLE`) -- same board.yaml
fixture, same parametrization, same assertions, through tan's own
`tan.planner.buildplan.emit_build_plan` front door instead of
`alp_orchestrate.emit_build_plan`.

THE TRAP THIS TEST DELIBERATELY AVOIDS: the five AEN A32-cluster carriers
that declare a `topology.a32_cluster.machine:`
(`E1M-AEN{501,601,701,801,803}`) are NOT one uniform refusal --
`YOCTO_MACHINE_UNBUILDABLE`'s own comment (`tan/planner/orchestrator.py`)
splits them into two citation classes, and this test is parametrized to
match that split rather than asserting one shared message:

  * `E1M-AEN801` -- its `e1m-aen801-a32.conf` has an ACTIVE, uncommented
    `require conf/machine/devkit-e8.conf` naming a file that exists in NO
    branch of the public meta-alif-ensemble upstream (issue #1968) --
    fails at BitBake's own parse step. Cites BOTH #1968 and #1971.
  * `E1M-AEN701` -- its `e1m-aen701-a32.conf`'s `require
    conf/machine/devkit-e7.conf` is already commented out in-tree, so this
    MACHINE parses with no DEFAULTTUNE / kernel provider / TF-A platform
    set at all -- a DIFFERENT shape from AEN801's, but the citation class
    is the same as the "no conf at all" trio below: only #1971 (no #1968,
    since there is no broken `require` to blame here).
  * `E1M-AEN501` / `E1M-AEN601` / `E1M-AEN803` -- ship NO
    `meta-alp-sdk/conf/machine/*.conf` at all, so BitBake fails to find the
    MACHINE before parsing a single `require` -- strictly more unbuildable
    than the two above. Cites only #1971. E1M-AEN803 is the bench module
    issue #1982 itself names.

All five are unbuildable regardless of the proximate cause: meta-alif-
ensemble's `LAYERSERIES_COMPAT` is incompatible with this repo's Scarthgap
baseline (issue #1971) -- which is why every one of the five cites #1971,
and only AEN801 additionally cites #1968.

`YOCTO_MACHINE_UNBUILDABLE` is the SAME dict
`scripts/check_yocto_machine_tree_parity.py` consults upstream (its own
comment: "do not fork a second list") -- this test asserts against the
values `tan/planner/orchestrator.py` actually carries, not a second
hand-typed copy of the upstream strings.

SENSITIVITY, measured rather than assumed: reverting the
`YOCTO_MACHINE_UNBUILDABLE` dict + its `_slice_command` check out of
`tan/planner/orchestrator.py` (and the matching catch in
`tan/planner/buildplan.py`) turns every case in this parametrization red --
the a32_cluster slice instead emits a real `bitbake alp-image-edge`
command with `warnings: []`, exactly the failure alp-sdk#1982 exists to
stop.

Real-SDK-gated: needs the actual `E1M-AEN{501,601,701,801,803}` SoM presets
(`metadata/e1m_modules/*.yaml`) from a bound alp-sdk checkout -- same
requirement as `test_carveout_aperture_ordering.py`.

Run locally:

    python -m pytest python/tests/planner/test_yocto_machine_unbuildable.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `test_carveout_aperture_ordering.py` uses (tan-cli#1081: every
# real-SDK-gated module reuses this one definition rather than redefining
# it locally).
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout that ships the "
           "E1M-AEN{501,601,701,801,803} SoM presets to run the "
           "alp-sdk#1982 yocto-machine-unbuildable coverage",
)

# Every AEN A32-cluster carrier's default topology names `app:
# alp-image-edge` (the `STOCK_IMAGE_APP` token, exempt from the `recipe:`
# requirement) and `machine: <sku>-a32` -- both inherited from the SoM
# preset's own `topology.a32_cluster` block, so the board.yaml here need
# not spell either out. Both M55 cores are turned off so the plan's only
# slice is the one under test.
_AEN_A32_STOCK_DEFAULT = """\
som:
  sku: {sku}

cores:
  m55_hp:
    os: "off"
  m55_he:
    os: "off"
"""


def _write_board(tmp_path: Path, sku: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(_AEN_A32_STOCK_DEFAULT.format(sku=sku), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("sku", "machine", "expect_in_message", "expect_not_in_message"),
    [
        # e1m-aen501/601/803-a32 ship NO meta-alp-sdk/conf/machine/*.conf
        # at all -- the "strictly more unbuildable" class; only #1971
        # applies (there is no broken/commented-out `require` to blame,
        # so no #1968). AEN803 is the bench module #1982 itself names.
        ("E1M-AEN501", "e1m-aen501-a32", ("#1971",), ("#1968",)),
        ("E1M-AEN601", "e1m-aen601-a32", ("#1971",), ("#1968",)),
        ("E1M-AEN803", "e1m-aen803-a32", ("#1971",), ("#1968",)),
        # e1m-aen701-a32 ships a conf but its `require` is commented out
        # pending meta-alif-ensemble being vendored -- also #1971 only.
        ("E1M-AEN701", "e1m-aen701-a32", ("#1971",), ("#1968",)),
        # e1m-aen801-a32 ships a conf with an ACTIVE `require` naming a
        # file absent upstream -- the one class that also cites #1968.
        ("E1M-AEN801", "e1m-aen801-a32", ("#1968", "#1971"), ()),
    ],
)
def test_emit_build_plan_aen_a32_machine_unbuildable_blocks_command(
    tmp_path: Path,
    sku: str,
    machine: str,
    expect_in_message: tuple[str, ...],
    expect_not_in_message: tuple[str, ...],
) -> None:
    """The plan must never carry `bitbake alp-image-edge` for any of the
    five AEN A32-cluster SoMs -- the slice is still carried (never
    dropped) with `command: null` plus a `yocto-machine-unbuildable`
    warning naming the blocking issue(s), per-class rather than one
    uniform message across all five (see module docstring)."""
    from tan.planner import load_board_yaml
    from tan.planner.buildplan import emit_build_plan

    path = _write_board(tmp_path, sku)
    project = load_board_yaml(path)
    plan = json.loads(emit_build_plan(
        project, board_yaml=path, build_root=tmp_path / "build"))

    a32 = next(s for s in plan["slices"] if s["coreId"] == "a32_cluster")
    assert a32["command"] is None, (
        f"{sku}: a32_cluster slice carries a command "
        f"({a32['command']!r}) instead of `command: null` -- "
        f"a real `bitbake` target for a MACHINE that cannot build")

    warning = next(w for w in plan["warnings"] if w["coreId"] == "a32_cluster")
    assert warning["code"] == "yocto-machine-unbuildable"
    assert machine in warning["message"]
    for needle in expect_in_message:
        assert needle in warning["message"], (
            f"{sku}: expected {needle!r} in the warning message, got "
            f"{warning['message']!r}")
    for needle in expect_not_in_message:
        assert needle not in warning["message"], (
            f"{sku}: {needle!r} must NOT be cited for this SoM's failure "
            f"class, but it is: {warning['message']!r}")
