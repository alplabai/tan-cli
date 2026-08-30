# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1000: `plan.warnings` must reach a human and a JSON consumer.

Before this, the plan's warnings reached ONE place -- `data.warnings` in the
JSON envelope (`build_cmd.py`'s `"warnings": plan.warnings`). Text mode never
printed them and `issues[]` never carried them, so the only message that
explains why a build produced nothing was invisible in both surfaces anyone
actually reads.

Measured on `E1M-AEN301`, a SKU `tan presets` lists: `tan init` rc=0,
`tan validate` rc=0 "clean", then `tan build` rc=1 whose COMPLETE text output
was four lines, none naming the cause:

    error: no slice was built -- every slice was skipped
    skipped: m55_he [zephyr] -- slice `m55_he` has no command
    skipped: m55_hp [zephyr] -- slice `m55_hp` has no command
    0 of 2 slice(s) built

while `--format json` carried, under `data.warnings`, the message that says
everything: "SoM 'E1M-AEN301' core 'm55_hp' wants Zephyr board
'alp_e1m_aen301_m55_hp', which has no tree under zephyr/boards/alp/ -- board
bring-up for this target has not happened yet."

This is the third door on a silence class already closed twice --
`_missing_tool_issues` (tan-cli#283, widened by tan-cli#801) and
`_cross_drive_issues` (tan-cli#697) both promote a reason into `issues[]` for
exactly this reason. That promoter matches `MISSING_TOOL_RE`, so a warning
that is not about a missing tool was outside the pattern.

Promoting into `issues[]` fixes BOTH surfaces at once, which is why there is
no separate text-rendering change for the ordinary build path:
`_print_text_issues` already prints every surviving issue as
`{severity}: {message}`.

The plan-mode recap is a SEPARATE surface and needed its own fix -- see
`test_plan_mode_recap_expands_each_warning` below.
"""

from __future__ import annotations

from tan.commands.build_cmd import (
    _MODE_PLAN,
    _plan_warning_issues,
    _text_issues,
    _text_recap,
)

#: The real pair `tan build` produced for E1M-AEN301, verbatim.
_AEN301_WARNINGS = [
    {
        "code": "board-tree-missing",
        "coreId": "m55_he",
        "message": (
            "SoM 'E1M-AEN301' core 'm55_he' wants Zephyr board "
            "'alp_e1m_aen301_m55_he', which has no tree under "
            "zephyr/boards/alp/ -- board bring-up for this target has not "
            "happened yet."
        ),
    },
    {
        "code": "board-tree-missing",
        "coreId": "m55_hp",
        "message": (
            "SoM 'E1M-AEN301' core 'm55_hp' wants Zephyr board "
            "'alp_e1m_aen301_m55_hp', which has no tree under "
            "zephyr/boards/alp/ -- board bring-up for this target has not "
            "happened yet."
        ),
    },
]


def test_each_plan_warning_becomes_one_issue():
    issues = _plan_warning_issues(_AEN301_WARNINGS)

    assert len(issues) == 2
    assert {i.code for i in issues} == {"build.plan-warning"}
    assert {i.severity for i in issues} == {"warning"}


def test_the_issue_carries_the_warning_code_core_and_message():
    """The whole point: the CAUSE has to survive the promotion. An issue that
    said only "the plan has a warning" would move the silence, not close it."""
    issue = _plan_warning_issues(_AEN301_WARNINGS)[1]

    assert "board-tree-missing" in issue.message
    assert "m55_hp" in issue.message
    assert "no tree under zephyr/boards/alp/" in issue.message


def test_a_warning_with_no_core_omits_the_core_rather_than_printing_none():
    """`coreId` is optional in the contract (`build_plan.py`'s `_warnings`
    accepts null). A plan-level warning must not render the string `None`."""
    issues = _plan_warning_issues(
        [{"code": "no-command", "coreId": None, "message": "nothing to build"}]
    )

    assert len(issues) == 1
    assert "None" not in issues[0].message
    assert "no-command" in issues[0].message
    assert "nothing to build" in issues[0].message


def test_a_warning_missing_coreId_entirely_is_handled_like_a_null_one():
    """`coreId` is optional, so an entry may omit the KEY, not just null it."""
    issues = _plan_warning_issues([{"code": "x", "message": "y"}])

    assert len(issues) == 1
    assert "None" not in issues[0].message


def test_no_warnings_produces_no_issues():
    assert _plan_warning_issues([]) == []


def test_the_promoted_issue_survives_the_text_dedup():
    """`_text_issues` (tan-cli#746) drops an issue whose message ENDS WITH a
    slice's own `reason`. The slice reasons here are `slice \\`m55_hp\\` has no
    command` -- the warning says something different and longer, so dropping
    it would delete the only report of the cause.

    This is the regression that matters most: a future dedup loosened to a
    substring match, or a producer that echoed the warning into `reason`,
    would silently re-close the hole this issue opened."""
    data = {
        "slices": [
            {"coreId": "m55_he", "backend": "zephyr", "status": "skipped",
             "reason": "slice `m55_he` has no command"},
            {"coreId": "m55_hp", "backend": "zephyr", "status": "skipped",
             "reason": "slice `m55_hp` has no command"},
        ]
    }
    issues = _plan_warning_issues(_AEN301_WARNINGS)

    assert _text_issues(issues, data) == issues


def test_plan_mode_recap_expands_each_warning(capsys):
    """Oracle parity, measured against the retired Rust `summarize_plan`
    (`crates/tan-core/src/build_plan.rs:358`, read at `2883cdf^` -- `crates/`
    was deleted by tan-cli#269/#601). It rendered

        warnings ({n}):
          - [{code}] {coreId}: {message}

    per entry, and `warnings: 0` when empty. The port printed only
    `warnings: {n}` -- the count, never a code or a message -- so the
    plan-inspection surface lost the same information the ordinary build path
    did."""
    data = {
        "schemaVersion": 1,
        "sku": "E1M-AEN301",
        "boardYaml": "/p/board.yaml",
        "buildRoot": "/p/build",
        "slices": [],
        "sharedArtefacts": [],
        "warnings": _AEN301_WARNINGS,
    }

    _text_recap(_MODE_PLAN, data)
    err = capsys.readouterr().err

    assert "board-tree-missing" in err
    assert "m55_hp" in err
    assert "no tree under zephyr/boards/alp/" in err


def test_plan_mode_recap_still_reports_an_empty_warning_list(capsys):
    """`warnings: 0` is information too -- the oracle printed it, and a recap
    that goes silent when there is nothing to say cannot be told apart from a
    recap whose warning block was dropped by a regression."""
    data = {
        "schemaVersion": 1, "sku": "E1M-AEN801", "boardYaml": "/p/board.yaml",
        "buildRoot": "/p/build", "slices": [], "sharedArtefacts": [],
        "warnings": [],
    }

    _text_recap(_MODE_PLAN, data)

    assert "warnings: 0" in capsys.readouterr().err
