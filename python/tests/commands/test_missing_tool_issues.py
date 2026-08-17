# SPDX-License-Identifier: Apache-2.0
"""Regression tests for tan-cli#801: `_missing_tool_issues` promoted a
missing-tool skip/fail into `issues[]` by testing `outcome.message` with
`str.startswith("tool `")` -- which only ever matched the slice's OWN
`command` precheck. The #550 post-build-step refusal (`_missing_post_tool`,
`tan.commands.build.execute`) puts the identical `` tool `{tool}` not found
`` phrase after a `` slice `{core_id}` post-build step N of M (`{label}`)
cannot run: `` lead-in, so it was never at message position 0 and the
promoter silently dropped it -- the exact silence tan-cli#283 was filed to
close, just for a different refusal site.

No existing test drove `_missing_tool_issues` against a post-step skip
outcome (grep confirms `_missing_post_tool`'s message shape was never fed to
it) -- this file closes that gap directly, at the same narrow
promoter-in-isolation level `test_build_command.py` already uses for the
sibling `_cross_drive_issues` promoter."""
from __future__ import annotations

import json

from tan.commands.build.execute import SliceOutcome, _missing_post_tool
from tan.commands.build_cmd import _missing_tool_issues
from tan.core.build_plan import parse_build_plan
from tan.core.plan_exec import ExecutionPolicy, PolicyAction


def _plan(core_ids: list[str]) -> str:
    slices = ",".join(
        f"""{{
          "coreId": {json.dumps(cid)}, "backend": "zephyr", "buildDir": "build/{cid}",
          "appDir": null, "configArtefacts": [], "toolchain": null, "artifacts": [],
          "debug": {{}}, "command": {{"tool": "west", "args": ["build"], "cwd": null}},
          "env": {{}}, "envAppendPath": {{}}
        }}"""
        for cid in core_ids
    )
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"}},
      "slices": [{slices}]
    }}"""


def test_the_slice_command_missing_tool_case_still_promotes():
    """The pre-existing behaviour `_missing_tool_issues`'s docstring
    describes: the marker sits at message position 0. Must keep working
    after the fix widens the match from `startswith` to a `.search`."""
    plan = parse_build_plan(_plan(["c1"]))
    message = "tool `west` not found -- searched /usr/bin:/bin"
    outcomes = [SliceOutcome("c1", "skipped", None, message)]

    issues = _missing_tool_issues(plan, outcomes)

    assert len(issues) == 1
    assert issues[0].code == "build.missing-tool"
    assert issues[0].severity == "warning"
    assert issues[0].message == f"slice `c1` skipped: {message}"


def test_the_post_build_step_missing_tool_case_now_promotes():
    """tan-cli#801's actual gap: the #550 post-step refusal's marker is NOT
    at position 0 -- it follows `` ... cannot run: ``. FAILS before the fix
    (the old `startswith("tool `")` check drops this outcome, `issues == []`)."""
    plan = parse_build_plan(_plan(["c1"]))
    message = (
        "slice `c1` post-build step 1 of 2 (`gcc --version`) cannot run: "
        "tool `gcc` not found -- searched /usr/bin:/bin. The slice's own "
        "configure already ran."
    )
    outcomes = [SliceOutcome("c1", "skipped", None, message)]

    issues = _missing_tool_issues(plan, outcomes)

    assert len(issues) == 1
    assert issues[0].code == "build.missing-tool"
    assert issues[0].severity == "warning"
    assert issues[0].message == f"slice `c1` skipped: {message}"


def test_a_failed_post_build_step_missing_tool_case_is_an_error_severity():
    """`executionPolicy.missingTool: fail` routes `_missing_post_tool` to
    `failed`, not `skipped` -- the promoted issue must follow, same as the
    slice-command case already does."""
    plan = parse_build_plan(_plan(["c1"]))
    message = (
        "slice `c1` post-build step 1 of 1 (`objcopy`) cannot run: "
        "tool `objcopy` not found -- searched /usr/bin. The slice's own "
        "configure already ran."
    )
    outcomes = [SliceOutcome("c1", "failed", None, message)]

    issues = _missing_tool_issues(plan, outcomes)

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message == f"slice `c1` failed: {message}"


def test_the_real_post_tool_producer_message_promotes_not_a_hand_written_copy():
    """tan-cli#801 review finding 4: every other test in this file hand-writes
    the post-step message string rather than driving `_missing_post_tool`
    (the actual producer) -- which means a wording edit at that producer,
    with no matching edit here, would still pass a suite built entirely of
    hand-copied literals. This test feeds `_missing_post_tool(...).message`
    straight into `_missing_tool_issues`, so the two are coupled through the
    real code path instead of through two independently maintained strings."""
    plan = parse_build_plan(_plan(["c1"]))
    policy = ExecutionPolicy(missing_tool=PolicyAction.SKIP)
    where = "slice `c1` post-build step 1 of 1 (`gcc --version`)"
    outcome = _missing_post_tool(where, "gcc", "/usr/bin:/bin", policy)
    outcomes = [SliceOutcome("c1", outcome.status, outcome.exit_code, outcome.message)]

    issues = _missing_tool_issues(plan, outcomes)

    assert len(issues) == 1
    assert issues[0].code == "build.missing-tool"
    assert issues[0].severity == "warning"
    assert issues[0].message == f"slice `c1` skipped: {outcome.message}"


def test_an_unrelated_failure_is_never_mistaken_for_a_missing_tool():
    """The widened match must stay narrow: an ordinary non-zero exit is not
    a missing-tool refusal of either shape."""
    plan = parse_build_plan(_plan(["c1"]))
    outcomes = [SliceOutcome("c1", "failed", 1, "slice `c1` terminated with exit code: 1")]

    assert _missing_tool_issues(plan, outcomes) == []


def test_a_null_message_is_never_mistaken_for_a_missing_tool():
    plan = parse_build_plan(_plan(["c1"]))
    outcomes = [SliceOutcome("c1", "succeeded", 0, None)]

    assert _missing_tool_issues(plan, outcomes) == []


def test_mixed_slices_promote_only_the_missing_tool_ones():
    """Multi-slice plan, mirroring a real `tan build --plan-from` run: one
    slice built, one slice's post step was skipped for a missing tool."""
    plan = parse_build_plan(_plan(["c1", "c2"]))
    post_step_message = (
        "slice `c2` post-build step 1 of 1 (`cmake --build .`) cannot run: "
        "tool `cmake` not found -- searched /usr/bin. The slice's own "
        "configure already ran."
    )
    outcomes = [
        SliceOutcome("c1", "succeeded", 0, None),
        SliceOutcome("c2", "skipped", None, post_step_message),
    ]

    issues = _missing_tool_issues(plan, outcomes)

    assert len(issues) == 1
    assert issues[0].message == f"slice `c2` skipped: {post_step_message}"
