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
`REPO = sdk_root()` at import time) -- satisfied below with a bare `tmp_path`,
never a real alp-sdk checkout, so this module runs unconditionally in the
default `gates` job (`python -m pytest tests -q`, no `ALP_SDK_ROOT`) rather
than being skipped the way the real-SDK-gated planner tests are. That is the
whole point: a test that only ran in the `sdk_parity` job would reproduce the
exact same blind spot the missing baremetal fixture created.

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


@pytest.fixture(autouse=True)
def _isolate_binding(monkeypatch):
    """Bind/unbind is process-global (`tan/planner_root.py`); keep this
    module's dummy binding from leaking into -- or colliding with -- a real
    `ALP_SDK_ROOT` binding some other test module in the same session made.
    Same idiom as `tests/core/test_planner_root.py::_isolate_binding`."""
    monkeypatch.setattr(planner_root, "_BOUND", None)
    yield


@pytest.fixture(autouse=True)
def _bound_dummy_root(tmp_path, _isolate_binding):
    """A bound root that satisfies `paths.py`'s import-time `sdk_root()` call
    with no alp-sdk checkout at all -- `_slice_post_commands` reads only
    `slice_.os`, never `REPO`/`METADATA_ROOT`, so the bound path's CONTENT is
    irrelevant; only its presence is required."""
    root = tmp_path / "fake-sdk-root"
    root.mkdir()
    planner_root.bind_sdk_root(root)
    yield root


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
