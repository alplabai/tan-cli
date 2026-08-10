# SPDX-License-Identifier: Apache-2.0
"""tan-cli#550, the CONSUMER half: `slices[].postCommands` must survive the
plan parser and the token-substitution pass.

alp-sdk #1344 added the key and the #608 re-sync landed the planner half in
`tan/planner/`, but `tan.core.build_plan` never read it -- so the executor
could not have run those steps even in principle. These tests pin the parse
and the substitution; `tests/commands/test_execute_post_commands.py` pins what
the executor then does with them.
"""
import json

import pytest

from tan.core.build_plan import PlanParseError, parse_build_plan
from tan.core.plan_tokens import (
    LeftoverToken,
    TokenValues,
    substitute_plan_tokens,
)

_VALUES = TokenValues(
    sdk_root="/sdk", project_root="/proj", python="/py/bin/python", toolchain_root="/tc"
)


def _plan(slice_extra: dict | None = None, *, path_mode: str | None = None) -> str:
    sl = {
        "coreId": "m55_hp",
        "backend": "baremetal",
        "buildDir": "build/m55_hp-baremetal",
        "appDir": "src",
        "configArtefacts": [],
        "toolchain": None,
        "artifacts": {},
        "debug": {},
        "command": {"tool": "cmake", "args": ["-S", "src", "-B", "."], "cwd": "build/x"},
        "env": {},
        "envAppendPath": {},
    }
    sl.update(slice_extra or {})
    plan = {
        "schemaVersion": 1,
        "generatedBy": "g",
        "boardYaml": "board.yaml",
        "sku": "E1M-AEN801",
        "buildRoot": "build",
        "slices": [sl],
        "sharedArtefacts": [],
        "warnings": [],
    }
    if path_mode is not None:
        plan["planPathMode"] = path_mode
    return json.dumps(plan)


def test_a_baremetal_slices_post_commands_are_parsed_in_order():
    """The shape alp-sdk actually emits (measured against a real
    `--emit build-plan` for an `os: baremetal` core): one `cmake --build .`
    step whose `cwd` is the slice's own buildDir."""
    plan = parse_build_plan(
        _plan(
            {
                "postCommands": [
                    {"tool": "cmake", "args": ["--build", "."], "cwd": "build/m55_hp-baremetal"}
                ]
            }
        )
    )
    steps = plan.slices[0].post_commands
    assert len(steps) == 1
    assert steps[0].tool == "cmake"
    assert steps[0].args == ["--build", "."]
    assert steps[0].cwd == "build/m55_hp-baremetal"


def test_several_post_commands_keep_the_plans_own_order():
    """`postCommands` is an ORDERED list -- a plan that post-processes a link
    (build, then objcopy) is meaningless if the executor may reorder it."""
    plan = parse_build_plan(
        _plan(
            {
                "postCommands": [
                    {"tool": "cmake", "args": ["--build", "."], "cwd": None},
                    {"tool": "objcopy", "args": ["-O", "binary", "a.elf", "a.bin"], "cwd": None},
                ]
            }
        )
    )
    assert [s.tool for s in plan.slices[0].post_commands] == ["cmake", "objcopy"]


def test_a_plan_without_the_key_parses_with_no_post_commands():
    """Strict producer / tolerant consumer: an alp-sdk predating #1344 emits
    no `postCommands` at all, and that must stay a valid plan rather than
    becoming `build.plan-invalid` on every slice."""
    assert parse_build_plan(_plan()).slices[0].post_commands == ()


def test_an_explicit_null_is_the_same_as_absent():
    assert parse_build_plan(_plan({"postCommands": None})).slices[0].post_commands == ()


def test_a_non_list_post_commands_is_a_coded_plan_error():
    with pytest.raises(PlanParseError) as err:
        parse_build_plan(_plan({"postCommands": {"tool": "cmake"}}))
    assert err.value.code == "build.plan-invalid"
    assert "`slices[0].postCommands` must be a list of command objects or null" in err.value.message


def test_a_null_step_inside_the_list_is_refused_not_dropped():
    """A `null` STEP is not the plan's "nothing to run" signal -- that is a
    null `command` on the slice. Dropping it silently would be a build step
    that never ran with nothing on the wire to say so."""
    with pytest.raises(PlanParseError) as err:
        parse_build_plan(_plan({"postCommands": [None]}))
    assert err.value.code == "build.plan-invalid"
    assert "`slices[0].postCommands[0]` must be an object" in err.value.message


def test_a_post_step_gets_the_same_tool_shape_rules_as_the_slices_own_command():
    """A relative path carrying a separator is neither a bare identity nor an
    already-resolved absolute path -- the same refusal `command.tool` gets,
    because the same executor spawns both."""
    with pytest.raises(PlanParseError) as err:
        parse_build_plan(_plan({"postCommands": [{"tool": "bin/sh", "args": [], "cwd": None}]}))
    assert err.value.code == "build.plan-invalid"
    assert "`slices[0].postCommands[0].tool` (`bin/sh`) is a relative path" in err.value.message


def test_a_post_step_arg_with_an_embedded_nul_is_refused():
    """Same reason `command.args` refuses one: every C-level exec interface
    takes NUL-terminated strings, so this reaches `subprocess` as a bare
    `ValueError` and gets reported as a tan bug rather than a bad plan."""
    with pytest.raises(PlanParseError) as err:
        parse_build_plan(
            _plan({"postCommands": [{"tool": "cmake", "args": ["--build\0"], "cwd": None}]})
        )
    assert err.value.code == "build.plan-invalid"
    assert "`slices[0].postCommands[0].args[0]` contains an embedded NUL byte" in err.value.message


def test_a_post_steps_tokens_are_substituted_like_the_slices_own_command():
    """The executor has no second substitution pass: a token this pass leaves
    standing in a post step is handed to `subprocess` verbatim."""
    plan = parse_build_plan(
        _plan(
            {
                "postCommands": [
                    {
                        "tool": "cmake",
                        "args": ["--build", "${PROJECT_ROOT}/build/x"],
                        "cwd": "${PROJECT_ROOT}/build/x",
                    }
                ]
            },
            path_mode="tokened",
        )
    )
    out, demoted = substitute_plan_tokens(plan, _VALUES)
    assert demoted == []
    step = out.slices[0].post_commands[0]
    assert step.args == ["--build", "/proj/build/x"]
    assert step.cwd == "/proj/build/x"


def test_an_unknown_token_in_a_post_step_still_fails_the_whole_pass():
    """`LeftoverToken` is a version/bug fact wherever it survives -- a post
    step must not be the one field where an unknown token sails through."""
    plan = parse_build_plan(
        _plan(
            {
                "postCommands": [
                    {"tool": "cmake", "args": ["--build", "${MYSTERY}"], "cwd": None}
                ]
            },
            path_mode="tokened",
        )
    )
    with pytest.raises(LeftoverToken) as err:
        substitute_plan_tokens(plan, _VALUES)
    assert err.value.field == "slices[0].postCommands[0].args[1]"
    assert err.value.token == "${MYSTERY}"


def test_an_unresolved_toolchain_root_in_a_post_step_demotes_the_slice():
    """A host-provisioning fact, not a plan bug -- routed to
    `executionPolicy.missingTool` at dispatch exactly like the same token in
    the slice's own command."""
    plan = parse_build_plan(
        _plan(
            {
                "postCommands": [
                    {"tool": "cmake", "args": ["--build", "${TOOLCHAIN_ROOT}/x"], "cwd": None}
                ]
            },
            path_mode="tokened",
        )
    )
    values = TokenValues(
        sdk_root="/sdk", project_root="/proj", python="/py/bin/python", toolchain_root=None
    )
    _out, demoted = substitute_plan_tokens(plan, values)
    assert len(demoted) == 1
    assert demoted[0].core_id == "m55_hp"
    assert demoted[0].field == "slices[0].postCommands[0].args[1]"
