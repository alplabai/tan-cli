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
  * alp-sdk @ `722320a1abe3cea675e99e97300b8a484b4e8464` has zero
    `os: baremetal` board.yaml examples for `test_planner_emit_parity.py` to
    parametrize over.
  * outside the planner-relocation freshness hash, no `tests/**/*.py`
    referenced `_slice_post_commands` at all.

This is `_slice_post_commands` in isolation, at the planner layer, with no
board.yaml and no bound alp-sdk checkout -- `Slice` and `_slice_post_commands`
are both pure data / pure functions of `slice_.os`, so nothing here needs a
real metadata tree to answer. The one thing importing `tan.planner.orchestrator`
DOES need is *some* bound SDK root (`tan/planner_root.py`: `paths.py` evaluates
`REPO = sdk_root()` at import time) -- satisfied below by REUSING whatever the
process already has bound (see `_bound_sdk_root`), falling back to a throwaway
`tmp_path` only when nothing is bound at all, so this module runs
unconditionally in the default `gates` job (`python -m pytest tests -q`, no
`ALP_SDK_ROOT`) rather than being skipped the way the real-SDK-gated planner
tests are. That is the whole point: a test that only ran in the `sdk_parity`
job would reproduce the exact same blind spot the missing baremetal fixture
created.

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
    """Bind the planner's SDK root -- reusing whatever the process already
    has bound, never resetting `tan.planner_root._BOUND` to `None`.

    An earlier revision of this fixture did `monkeypatch.setattr(planner_root,
    "_BOUND", None)` before every test and rebound a fresh `tmp_path` dummy
    each time, on the theory that `monkeypatch` would undo the damage at
    teardown. It does undo the *variable* -- but `tan/planner/paths.py`
    evaluates `REPO = sdk_root()` at ITS OWN import time, once, and caches
    that module in `sys.modules` for the rest of the process; unbinding
    `_BOUND` afterwards cannot un-freeze an already-imported `tan.planner.
    paths`. So the very first test in this module to import `tan.planner`
    froze `REPO` at this module's own throwaway directory for every OTHER
    test in the session that later reads it -- reproduced: with a real
    alp-sdk checkout bound, `pytest tests/planner -q` went from `122 passed`
    to `14 failed, 111 passed, 1 skipped` with this module present (it sorts
    first alphabetically and got there before any test that needed the real
    checkout did); deleting just this file restored `122 passed`.

    Fixed by never forcing an unbind: reuse an already-bound root (real or a
    prior call of this same fixture) when there is one; otherwise prefer the
    REAL bound checkout (`ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT`, captured as
    `_SDK` above) over a fresh dummy, so an `ALP_SDK_ROOT`-bound run keeps
    `REPO` pointed at real content throughout; fall back to a throwaway
    `tmp_path` stub only when nothing is bound anywhere in the process (the
    default, unbound `gates` job). `_slice_post_commands` reads only
    `slice_.os`, never `REPO`'s content, so which of the three the process
    ends up bound to is inert to the assertions below either way. Same idiom
    as `tests/core/test_flow_d_manifest_fields.py::_bind_stub_sdk_root`.
    """
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
