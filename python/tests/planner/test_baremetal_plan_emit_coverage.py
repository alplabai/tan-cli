# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1042, plan-emit half: `buildplan._slice_config_artefact`'s
baremetal arm (both halves -- it emits `alp-baremetal.cmake` only when the
slice actually has compile-time guards), `buildplan._slice_artifacts`'s
baremetal arm, `validate._enforce_loader_rules`' baremetal refusal, and the
`postCommands` emit site that consumes `_slice_post_commands`.

Why none of it was covered: identical structural blind spot to the
orchestrator half (see `test_baremetal_command_and_flash_coverage.py`'s
docstring). Every one of these is reached, outside a hand-written unit test,
only from the two `ALP_SDK_ROOT`-gated parity suites, and alp-sdk's pinned
tree has zero `os: baremetal` board.yaml examples for either of them to
parametrize over.

The last of the four is the one tan-cli#1044's review raised and this issue
picked up in a comment: `_slice_post_commands` itself now has a test, but its
CONSUMPTION did not. Mutating the emit site directly to a flat
`"postCommands": [],` -- bypassing the call entirely -- reopens tan-cli#550's
exact customer symptom (a baremetal slice configures and never builds) and
stays invisible to a test that drives the helper directly. So the last test
here goes through the real `emit_build_plan` front door rather than the
helper, which also makes it the one end-to-end proof that a baremetal
project reaches the plan at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# `base_dir` and `bound_sdk_root` are pytest fixtures, imported for their
# side effect -- see the twin note in
# `test_baremetal_command_and_flash_coverage.py`, and `_baremetal_support`
# for why the SDK-root binder has exactly one definition.
from tests.planner._baremetal_support import (  # noqa: F401
    PROJECT_INCLUDE_PREAMBLE,
    baremetal_project,
    base_dir,
    bound_sdk_root,
    slice_of,
)

#: `_NULL_ARTIFACTS` spelled out rather than imported: importing the module's
#: own constant would make an assertion that agrees with any mutation of it.
ALL_NULL_ARTIFACTS = {
    "elf": None, "map": None, "bin": None, "sizeReport": None,
    "symbols": None, "compileCommands": None, "outputDir": None,
}


# ---------------------------------------------------------------------
# buildplan.py:54-91 -- `_slice_config_artefact`'s baremetal arm
# ---------------------------------------------------------------------


def test_a_guarded_baremetal_slice_carries_the_project_include_file(base_dir):
    """`alp-baremetal.cmake` and its exact contents.

    This file has exactly ONE reader -- the `-DCMAKE_PROJECT_INCLUDE=` arg on
    this slice's own configure -- so the filename here and the one
    `_baremetal_project_include_arg` points at are the same fact, spelled
    once as `BAREMETAL_PROJECT_INCLUDE`. A slice that stops writing it stops
    compiling with its `ALP_BOARD_<SLUG>` / `ALP_SOM_<SKU>` guards, loudly
    (tan-cli#551).
    """
    from tan.planner.buildplan import _slice_config_artefact

    project, slice_ = baremetal_project(base_dir, board_name="E1M-EVK")

    assert _slice_config_artefact(project, slice_) == (
        "alp-baremetal.cmake",
        PROJECT_INCLUDE_PREAMBLE + "add_compile_definitions(ALP_BOARD_E1M_EVK)\n",
    )


def test_an_unguarded_baremetal_slice_carries_no_config_artefact(base_dir):
    """The absence-emits-nothing half -- the one that rots silently, because
    a mutant that always emits the file still produces a plan that builds.

    It is not cosmetic: `_slice_command` only appends
    `-DCMAKE_PROJECT_INCLUDE=` when there are guards, so an artefact emitted
    here without them would be a file materialised on the consumer's disk
    that nothing ever opens.
    """
    from tan.planner.buildplan import _slice_config_artefact

    project, slice_ = baremetal_project(base_dir)

    assert _slice_config_artefact(project, slice_) is None


def test_a_slice_whose_os_has_no_config_artefact_gets_none(base_dir):
    """The control: `off` (and any unknown `os:`) falls through every arm.
    Without it, returning the baremetal tuple unconditionally would satisfy
    the first assertion above."""
    from tan.planner.buildplan import _slice_config_artefact

    project, _ = baremetal_project(base_dir, board_name="E1M-EVK")

    assert _slice_config_artefact(project, slice_of("off")) is None


# ---------------------------------------------------------------------
# buildplan.py:278-280 -- `_slice_artifacts`' baremetal arm
# ---------------------------------------------------------------------


def test_a_baremetal_slice_reports_only_its_output_directory():
    """`outputDir` and nothing else, and both halves of that are deliberate:

      * the executable's NAME is the app's own `CMakeLists.txt` to pick, not
        an SDK convention this emitter may invent, so `elf`/`bin`/`map`/
        `sizeReport`/`symbols` stay null rather than guess;
      * `compileCommands` stays null EVEN THOUGH the configure passes
        `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, because CMake implements that
        variable "only by Makefile Generators and Ninja Generators" and this
        planner does not choose the generator. Naming the path anyway would
        be exactly the artifacts-lie tan-cli#550 is about, pointed the other
        way.
    """
    from tan.planner.buildplan import _slice_artifacts

    assert _slice_artifacts(Path("build/m55_he-baremetal"),
                            slice_of("baremetal", app="./src")) == dict(
        ALL_NULL_ARTIFACTS, outputDir="build/m55_he-baremetal/output")


def test_a_blocked_baremetal_slice_reports_no_output_directory_either():
    """`has_command=False` means nothing will ever configure that build dir,
    so `outputDir` would be a dangling promise pinned by a configure that
    never runs."""
    from tan.planner.buildplan import _slice_artifacts

    assert _slice_artifacts(Path("build/m55_he-baremetal"),
                            slice_of("baremetal", app="./src"),
                            has_command=False) == ALL_NULL_ARTIFACTS


def test_a_zephyr_slice_reports_wests_own_nested_tree_instead():
    """The control, and the one that pins WHICH key baremetal is allowed to
    fill: zephyr names six paths under west's own `<buildDir>/build/` tree
    (issue #1360) and leaves `outputDir` null -- the exact mirror image."""
    from tan.planner.buildplan import _slice_artifacts

    assert _slice_artifacts(Path("build/m55_hp-zephyr"),
                            slice_of("zephyr", app="./src")) == {
        "elf":             "build/m55_hp-zephyr/build/zephyr/zephyr.elf",
        "map":             "build/m55_hp-zephyr/build/zephyr/zephyr.map",
        "bin":             "build/m55_hp-zephyr/build/zephyr/zephyr.bin",
        "sizeReport":      "build/m55_hp-zephyr/build/zephyr/zephyr.stat",
        "symbols":         "build/m55_hp-zephyr/build/zephyr/zephyr.symbols",
        "compileCommands": "build/m55_hp-zephyr/build/compile_commands.json",
        "outputDir":       None,
    }


def test_a_yocto_slice_reports_no_artefact_paths_at_all():
    """The second control: `outputDir` is baremetal's alone, not a default
    every non-zephyr runtime gets."""
    from tan.planner.buildplan import _slice_artifacts

    assert _slice_artifacts(Path("build/a55-yocto"),
                            slice_of("yocto", image="alp-image-edge")
                            ) == ALL_NULL_ARTIFACTS


# ---------------------------------------------------------------------
# validate.py:299-350 -- `_enforce_loader_rules`' baremetal refusal, incl.
# `_enforce_baremetal_app_rule`'s stock-token arm (alp-sdk#1889 /
# tan-cli#1103 planner re-sync -- porting alp-sdk's
# tests/scripts/test_orchestrate_baremetal_slice.py)
# ---------------------------------------------------------------------


def test_a_baremetal_core_with_no_app_is_refused(base_dir):
    """Spec §4.5: every non-`off` slice must declare enough to actually
    build. The message names `CMakeLists.txt`, not `prj.conf` -- a customer
    reading it has to be told which file the directory it points at is
    missing, and the zephyr arm one line up says the other thing.
    """
    from tan.planner.models import OrchestratorError
    from tan.planner.validate import _enforce_loader_rules

    with pytest.raises(OrchestratorError) as excinfo:
        _enforce_loader_rules(slice_of("baremetal"), base_dir / "metadata")

    assert str(excinfo.value) == (
        "core 'm55_he': os: baremetal requires `app:` pointing at a "
        "CMakeLists.txt directory")


def test_a_baremetal_core_with_an_app_is_accepted(base_dir):
    """The other half of the same arm: `app:` present is the whole
    requirement -- no `board:`, no `image:`, no `recipe:`."""
    from tan.planner.validate import _enforce_loader_rules

    assert _enforce_loader_rules(
        slice_of("baremetal", app="./src"), base_dir / "metadata") is None


@pytest.mark.parametrize(
    ("stock_app", "other_os"),
    [
        ("alp-stock-shim", "zephyr"),
        ("alp-image-edge", "yocto"),
    ],
)
def test_a_baremetal_core_carrying_a_stock_token_is_refused_not_built(
        base_dir, stock_app, other_os):
    """alp-sdk#1889 / tan-cli#1103: a core with no `app:` of its own still
    resolves one -- `loader.py`'s `_resolve_topology_for_core` merges the
    SoM topology default OVER a project entry that omits `app:`, and every
    topology default is one of the two stock tokens parametrized above. The
    plain `not slice_.app` check above this one is always False for that
    inherited token (it is truthy), so without this arm a baremetal core
    silently carried a real, wrong-target Zephyr/Yocto build command instead
    of being refused. `stock_app` here stands in for that inherited token --
    `slice_of` takes `app=` directly, so this needs no SoM preset merge to
    reach the arm, only the resolved value the merge would have produced.
    """
    from tan.planner.models import OrchestratorError
    from tan.planner.orchestrator import STOCK_IMAGE_APP, STOCK_SHIM_APP
    from tan.planner.validate import _enforce_loader_rules

    assert stock_app in (STOCK_SHIM_APP, STOCK_IMAGE_APP)

    with pytest.raises(OrchestratorError) as excinfo:
        _enforce_loader_rules(
            slice_of("baremetal", app=stock_app), base_dir / "metadata")

    assert str(excinfo.value) == (
        f"core 'm55_he': os: baremetal requires `app:` pointing at a "
        f"CMakeLists.txt directory -- `{stock_app}` is the {other_os} "
        f"stock default (whether inherited from the SoM topology preset "
        f"when no `app:` was given, or set explicitly), and there is no "
        f"bare-metal stock default to fall back to")


def test_a_baremetal_core_with_a_real_app_that_merely_shares_no_tokens_is_accepted(
        base_dir):
    """The control for the parametrized refusal above: an ordinary `app:`
    that isn't either stock token still passes -- this arm must not over-fire
    on every non-empty `app:`, only the two literal tokens."""
    from tan.planner.validate import _enforce_loader_rules

    assert _enforce_loader_rules(
        slice_of("baremetal", app="./src"), base_dir / "metadata") is None


@pytest.mark.parametrize(
    ("slice_kwargs", "message"),
    [
        ({"os_": "zephyr"},
         "core 'm55_he': os: zephyr requires `app:` pointing at a "
         "prj.conf / CMakeLists.txt directory"),
        ({"os_": "yocto"},
         "core 'm55_he': os: yocto requires either `app:` (custom recipe) "
         "or `image:` (stock recipe)"),
    ],
)
def test_a_sibling_runtime_is_refused_in_its_own_words(base_dir, slice_kwargs,
                                                       message):
    """The control: the baremetal refusal is one arm of four, and a single
    hard-wired message would satisfy the baremetal assertion above."""
    from tan.planner.models import OrchestratorError
    from tan.planner.validate import _enforce_loader_rules

    with pytest.raises(OrchestratorError) as excinfo:
        _enforce_loader_rules(slice_of(slice_kwargs["os_"]),
                              base_dir / "metadata")

    assert str(excinfo.value) == message


def test_an_off_core_is_exempt_from_the_loader_rules(base_dir):
    """The second control: `off` returns before any arm, `app:` or not."""
    from tan.planner.validate import _enforce_loader_rules

    assert _enforce_loader_rules(
        slice_of("off"), base_dir / "metadata") is None


# ---------------------------------------------------------------------
# buildplan.py:599-606 -- the `postCommands` emit site
# ---------------------------------------------------------------------


def _emit_one_slice(project, base_dir: Path) -> dict:
    from tan.planner.buildplan import emit_build_plan

    plan = json.loads(emit_build_plan(project,
                                      board_yaml=base_dir / "board.yaml",
                                      build_root=base_dir / "build"))
    assert len(plan["slices"]) == 1
    return plan["slices"][0]


def test_a_baremetal_slice_reaches_the_plan_with_configure_then_build(base_dir):
    """The emit site, through the real front door.

    `_slice_post_commands` having its own test (tan-cli#1044) does not cover
    this: a mutant that replaces the call here with a flat `[]` leaves that
    test green and reopens tan-cli#550 -- the slice configures, exits 0 with
    a `CMakeCache.txt` and no binary, and every parity fixture still agrees
    because all 213 of their slices are zephyr or yocto, whose
    `postCommands` are legitimately empty.

    Asserted as one dict, not four `in` checks: `command` and `postCommands`
    share the slice's buildDir as `cwd` (which is what makes the configure's
    `-B .` and the build step's `.` resolve to the same tree), and
    `configArtefacts` names the file `command`'s
    `-DCMAKE_PROJECT_INCLUDE=` points at. Splitting them would let the four
    drift apart while each assertion stayed green.
    """
    build_dir = (base_dir / "build" / "m55_he-baremetal").as_posix()
    project, _ = baremetal_project(base_dir, board_name="E1M-EVK",
                                   toolchain="arm-zephyr-eabi")
    emitted = _emit_one_slice(project, base_dir)

    assert emitted["backend"] == "baremetal"
    assert emitted["buildDir"] == build_dir
    assert emitted["command"] == {
        "tool": "cmake",
        "args": [
            "-S", "${PROJECT_ROOT}/src", "-B", ".",
            "--no-warn-unused-cli",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$<1:"
            "${PROJECT_ROOT}/build/m55_he-baremetal/output>",
            "-DALP_SOM_SKU=E1M-AEN801",
            "-DALP_SOM_FAMILY=aen",
            "-DALP_CORE_ID=m55_he",
            "-DALP_TOOLCHAIN=arm-zephyr-eabi",
            "-DCMAKE_PROJECT_INCLUDE="
            "${PROJECT_ROOT}/build/m55_he-baremetal/alp-baremetal.cmake",
        ],
        "cwd": build_dir,
    }
    assert emitted["postCommands"] == [
        {"tool": "cmake", "args": ["--build", "."], "cwd": build_dir}]
    assert emitted["artifacts"] == dict(
        ALL_NULL_ARTIFACTS, outputDir=f"{build_dir}/output")
    assert [a["path"] for a in emitted["configArtefacts"]] == [
        f"{build_dir}/alp-baremetal.cmake"]


def test_a_baremetal_slice_that_cannot_configure_carries_no_build_step(base_dir):
    """The control on the same site, and a real rule rather than a
    formality: `postCommands` is empty whenever `command` is null, because
    there is nothing to build on top of a slice that was never configured.
    A `cmake --build .` hard-wired at the emit site would run in an
    unconfigured directory and fail with CMake's own error instead of the
    plan's `no-command` warning.

    `configArtefacts` is empty here for the neighbouring reason: a blocked
    baremetal command means the slice's `alp-baremetal.cmake` has no reader
    left, so materialising it would hand the consumer a path nothing will
    ever open.
    """
    project, _ = baremetal_project(base_dir, board_name="E1M-EVK", app=None)
    emitted = _emit_one_slice(project, base_dir)

    assert emitted["command"] is None
    assert emitted["postCommands"] == []
    assert emitted["configArtefacts"] == []
    assert emitted["artifacts"] == ALL_NULL_ARTIFACTS
