# SPDX-License-Identifier: Apache-2.0
"""`_slice_post_commands` (tan-cli#1036, filed as the precondition for closing
tan-cli#492) had zero regression coverage. Reverting it to its pre-#550 shape
-- dropping the `if slice_.os == "baremetal": return [["cmake", "--build",
"."]]` branch back to an unconditional `return []` -- left the WHOLE suite
green, because nothing structurally reachable by the parity suites is
baremetal:

  * every `build-plan.json` fixture's 213 slices are `zephyr` or `yocto`, and
    every one of their `postCommands` is already `[]` -- so a mutant that
    makes EVERY slice answer `[]` agrees with all 213 fixtures.
  * alp-sdk @ `f1b1c9df0edd23988961150439863e70e5d99211` has zero
    `os: baremetal` board.yaml examples for `test_planner_emit_parity.py` to
    parametrize over.
  * outside the planner-relocation freshness hash, no `tests/**/*.py`
    referenced `_slice_post_commands` at all.

This is `_slice_post_commands` in isolation, at the planner layer, with no
board.yaml and no bound alp-sdk checkout -- `Slice` and `_slice_post_commands`
are both pure data / pure functions of `slice_.os`, so nothing here needs a
real metadata tree to answer. The one thing importing `tan.planner.orchestrator`
DOES need is *some* bound SDK root (`tan/planner_root.py`: `paths.py` evaluates
`REPO = sdk_root()` at import time) -- satisfied below by BINDING the real
checkout first when one is available (see `_bound_sdk_root`), falling back to
reusing whatever is already bound, or a throwaway `tmp_path` when nothing is
bound at all, so this module runs unconditionally in the default `gates` job
(`python -m pytest tests -q`, no `ALP_SDK_ROOT`) rather than being skipped the
way the real-SDK-gated planner tests are. That is the whole point: a test
that only ran in the `sdk_parity` job would reproduce the exact same blind
spot the missing baremetal fixture created.

The rest of the `os: baremetal` branch family (`_slice_command`,
`_slice_flash_recipe`, `_slice_config_artefact`, `_slice_artifacts`,
`_enforce_loader_rules`) shares this exact blind spot and is likewise
uncovered -- tracked separately as tan-cli#1042, not fixed here.

Mutation-proven: reverting `_slice_post_commands` to
`return []` unconditionally (its pre-#550 shape) turns
`test_a_baremetal_slice_emits_the_cmake_build_step` RED on its own assertion
(`[] != [["cmake", "--build", "."]]`); the three non-baremetal cases stay
green either way, which is exactly why they cannot catch the mutant alone --
`test_a_baremetal_slice_emits_the_cmake_build_step` is the only one that can.
Restoring the branch turns it GREEN again.
"""
from __future__ import annotations

import sys

import pytest

from tan import planner_root
from tests.conftest import sdk_root as _real_sdk_root

# Captured at COLLECTION time (module import), before `tests/conftest.py`'s
# per-test, autouse `_scrub_sdk_discovery_env` fixture deletes `ALP_SDK_ROOT`
# from the process environment -- `sdk_root()`'s own docstring requires the
# module-level call for exactly this reason.
_SDK = _real_sdk_root()


@pytest.fixture(autouse=True)
def _bound_sdk_root(tmp_path):
    """Bind the planner's SDK root -- preferring the REAL bound checkout
    (`_SDK`, captured at collection time above) over whatever happens to be
    already bound, so THIS module can never be the one that freezes
    `tan.planner`'s module-level constants at a non-SDK stub in the
    `ALP_SDK_ROOT`-bound job, the only job where a real root is available to
    bind. In the default unbound `gates` job (`_SDK` is None) this fixture is
    no different from any other binder in the suite: it reuses whatever root
    is already bound, or falls back to a throwaway `tmp_path` stub, same as
    the rest -- it does not itself keep a non-SDK stub from being frozen
    there. See the "Fixed by binding the real checkout FIRST" paragraph below
    for the precise condition.

    Round 1 (this fixture's first revision) did `monkeypatch.setattr(
    planner_root, "_BOUND", None)` before every test and rebound a fresh
    `tmp_path` dummy each time, on the theory that `monkeypatch` would undo
    the damage at teardown. It does undo the *variable* -- but `tan/planner/
    paths.py` evaluates `REPO = sdk_root()` at ITS OWN import time, once, and
    caches that module in `sys.modules` for the rest of the process;
    unbinding `_BOUND` afterwards cannot un-freeze an already-imported
    `tan.planner.paths`. So the very first test in this module to import
    `tan.planner` froze `REPO` at this module's own throwaway directory for
    every OTHER test in the session that later reads it -- reproduced: with a
    real alp-sdk checkout bound, `pytest tests/planner -q` went from
    `122 passed` to `14 failed, 111 passed, 1 skipped` with this module
    present (it sorts first alphabetically and got there before any test
    that needed the real checkout did); deleting just this file restored
    `122 passed`.

    Round 2 fixed THAT by never forcing an unbind -- reuse an already-bound
    root (real or a prior call of this same fixture) when there is one,
    falling back to `_SDK` (if set) or a throwaway `tmp_path` only when
    nothing is bound anywhere in the process. That traded the freeze-at-a-
    stub bug for a subtler version of the exact same failure: "whatever is
    already bound" is not always the real SDK -- `tests/core/
    test_flow_d_manifest_fields.py::_bind_stub_sdk_root` binds a NON-SDK stub
    (its own directory) the same way, and never imports the real
    `tan.planner` package itself (it loads `loader.py`/`orchestrator.py`
    standalone under a synthetic package name), so it is not itself the
    hazard -- but it leaves `_BOUND` pointed at that stub afterwards.
    `tests/core` sorts before `tests/planner`, so in an `ALP_SDK_ROOT`-bound
    run that stub fixture ran FIRST, and this module's round-2 fixture then
    reused the stub it left behind and imported the REAL `tan.planner`
    (`from tan.planner import _slice_post_commands`) under it -- freezing
    `REPO` there for the rest of the process. Every later
    `planner_root.bind_sdk_root(<real SDK>)` call then raised
    `PlannerRootError`, because `bind_sdk_root` only forbids a rebind once
    `"tan.planner" in sys.modules`, which by then it was. Reproduced: with a
    real alp-sdk checkout bound, `pytest tests/core/
    test_flow_d_manifest_fields.py tests/planner -q` went `18 passed,
    122 errors`; deleting just this file restored `136 passed`; running
    `tests/planner` before the flow-d module (reverse order) passed too,
    `140 passed` -- proving the bug is an ORDERING hazard, not specific to
    this file's alphabetical position.

    Fixed by binding the real checkout FIRST, before anything (this module or
    another) can freeze `tan.planner` at a stub: if `_SDK` is set and
    `tan.planner` has not been imported yet in this process, bind to it
    unconditionally -- `bind_sdk_root` permits exactly this rebind (it only
    forbids rebinding to a *different* root once `"tan.planner" in
    sys.modules`), so it wins over any stub bound earlier in the session and
    is a correct no-op if the real root is already bound. Only when `_SDK`
    is unset (the default, unbound `gates` job) or `tan.planner` is already
    imported (nothing left to protect by rebinding) does this fall back to
    reusing whatever is already bound, or a throwaway `tmp_path` stub if
    nothing is. `_slice_post_commands` reads only `slice_.os`, never `REPO`'s
    content, so which of these three roots the process ends up bound to is
    inert to the assertions below either way. Same idiom as `tests/core/
    test_flow_d_manifest_fields.py::_bind_stub_sdk_root`, minus the bug: that
    fixture cannot pull the same trick back the other way because it never
    imports the real `tan.planner` package.
    """
    if _SDK is not None and "tan.planner" not in sys.modules:
        planner_root.bind_sdk_root(_SDK)
    else:
        try:
            planner_root.sdk_root()
        except planner_root.PlannerRootError:
            root = _SDK if _SDK is not None else tmp_path / "fake-sdk-root"
            root.mkdir(parents=True, exist_ok=True)
            planner_root.bind_sdk_root(root)
    yield


def _slice(os_: str):
    from tan.planner.models import Slice

    return Slice(core_id="test_core", os=os_, app="./src")


def test_a_baremetal_slice_emits_the_cmake_build_step():
    """The whole fix: `cmake -S ... -B .` only CONFIGURES a baremetal slice
    -- without this step a baremetal build exits 0 having produced a
    `CMakeCache.txt` and no binary (tan-cli#550)."""
    from tan.planner import _slice_post_commands

    assert _slice_post_commands(_slice("baremetal")) == [["cmake", "--build", "."]]


@pytest.mark.parametrize("os_", ["zephyr", "yocto", "off"])
def test_a_non_baremetal_slice_emits_no_post_commands(os_):
    """The control: `west build` (zephyr) and `bitbake` (yocto) each
    configure AND build in one invocation, so they carry none; an `off` core
    is never a buildable slice at all. Without this control, hard-wiring
    `[["cmake", "--build", "."]]` unconditionally would also pass the
    baremetal assertion above."""
    from tan.planner import _slice_post_commands

    assert _slice_post_commands(_slice(os_)) == []
