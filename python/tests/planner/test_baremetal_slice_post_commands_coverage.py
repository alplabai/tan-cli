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
`REPO = sdk_root()` at import time) -- satisfied by the shared
`_baremetal_support.bound_sdk_root` fixture imported below, which binds the
real checkout first when one is available, falling back to reusing whatever
is already bound, or a throwaway `tmp_path` when nothing is bound at all, so
this module runs unconditionally in the default `gates` job
(`python -m pytest tests -q`, no `ALP_SDK_ROOT`) rather than being skipped the
way the real-SDK-gated planner tests are. That is the whole point: a test
that only ran in the `sdk_parity` job would reproduce the exact same blind
spot the missing baremetal fixture created.

The rest of the `os: baremetal` branch family (`_slice_command`,
`_slice_flash_recipe`, `_slice_config_artefact`, `_slice_artifacts`,
`_enforce_loader_rules` and the four baremetal-only helpers) shares this exact
blind spot; it was tracked separately as tan-cli#1042 and is now covered by
`test_baremetal_command_and_flash_coverage.py` and
`test_baremetal_plan_emit_coverage.py`, which share this module's SDK-root
binder -- see `tests/planner/_baremetal_support.py`.

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

# `bound_sdk_root` is an autouse pytest fixture, imported for its side effect
# -- the same idiom `tests/commands/test_build_streaming.py` uses for
# `project`. It used to be a private `_bound_sdk_root` defined here, and the
# three rounds of review that shaped it (tan-cli#1044) are recorded on
# `bind_planner_sdk_root`, which is now its single definition: a second copy
# is the exact way a round-four fix reaches one binder and not the other and
# silently reinstates round 2's `18 passed, 122 errors`, and no gate catches
# that -- `tests/gates/test_shared_helpers_have_one_definition.py` walks
# `python/tan/**` only.
from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401


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
