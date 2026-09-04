# SPDX-License-Identifier: Apache-2.0
"""tan-cli#746: text mode must not print the searched-PATH twice.

`_missing_tool_issues` promotes a skip's reason into `issues[]` so a JSON
consumer sees it (tan-cli#283), and that reason carries the literal
searched-PATH list so a customer can fix it themselves (tan-cli#510). Both
are deliberate. In `--format json` they are two different fields; in text
they were two adjacent lines carrying the same 2.8 KB string.

Measured on a clean-room Windows build where the AEN801 SoM's `a32_cluster`
yocto slice is an EXPECTED skip (no `bitbake` on the host): 2807 + 2801 =
5608 characters for one benign skip, pushing the two `ok:` lines and the
`2 of 3 slice(s) built` summary below it. After the fix, 2801 -- one copy,
the per-slice one, which is the one #510 wants.

`_text_issues` is TEXT-ONLY on purpose: the JSON envelope is the API and a
consumer may already read `issues[].message`, so it keeps every issue.
"""

from __future__ import annotations

from tan.commands.build_cmd import _text_issues
from tan.envelope import Issue

_PATH = r"E:\venv\Scripts;C:\Windows\system32;C:\Program Files\Git\cmd"
_REASON = f"tool `bitbake` not found -- searched PATH: {_PATH}"

_DATA = {
    "slices": [
        {"coreId": "a32_cluster", "backend": "yocto",
         "status": "skipped", "reason": _REASON},
        {"coreId": "m55_hp", "backend": "zephyr", "status": "ok"},
    ]
}


def _issue(message: str, code: str = "build.missing-tool") -> Issue:
    return Issue(code, "warning", message)


def test_the_duplicate_of_a_slice_reason_is_dropped():
    """The fix: the per-slice recap prints this verbatim a line later."""
    issues = [_issue(f"slice `a32_cluster` skipped: {_REASON}")]
    assert _text_issues(issues, _DATA) == []


def test_an_issue_that_is_not_a_slice_reason_is_kept():
    """This must only remove a SECOND copy, never information. An issue no
    slice line carries has to survive, or the dedup silently swallows the
    only report of something."""
    issues = [_issue("workspace is on a different drive", "build.cross-drive")]
    assert _text_issues(issues, _DATA) == issues


def test_an_issue_that_merely_mentions_a_reason_mid_string_is_kept():
    """Anchored on the END of the message, not `in` alone: a future producer
    that wraps a reason with a trailing hint is saying something the slice
    line does not, so it is not a duplicate."""
    issues = [_issue(f"slice `a32_cluster` skipped: {_REASON} -- install it")]
    assert _text_issues(issues, _DATA) == issues


def test_slices_without_reasons_change_nothing():
    issues = [_issue("something else entirely", "build.other")]
    data = {"slices": [{"coreId": "m55_hp", "backend": "zephyr", "status": "ok"}]}
    assert _text_issues(issues, data) == issues


def test_a_plan_mode_or_absent_data_is_passed_through_untouched():
    """`data` is None in some modes and carries PLAN slices (no `status`,
    no `reason`) in another -- neither may lose an issue."""
    issues = [_issue("plan-mode issue", "build.plan")]
    assert _text_issues(issues, None) == issues
    assert _text_issues(issues, {}) == issues
    assert _text_issues(issues, {"slices": [{"coreId": "m55_hp"}]}) == issues


def test_every_issue_survives_when_two_slices_share_one_reason():
    """Two cores missing the same tool produce one reason string twice; the
    single wrapped issue is still just a duplicate of a printed line."""
    data = {
        "slices": [
            {"coreId": "m55_he", "backend": "zephyr",
             "status": "skipped", "reason": _REASON},
            {"coreId": "m55_hp", "backend": "zephyr",
             "status": "skipped", "reason": _REASON},
        ]
    }
    issues = [
        _issue(f"slice `m55_he` skipped: {_REASON}"),
        _issue(f"slice `m55_hp` skipped: {_REASON}"),
        _issue("unrelated", "build.other"),
    ]
    assert _text_issues(issues, data) == [_issue("unrelated", "build.other")]


# ---------------------------------------------------------------------
# Printing is covered together with the filtering, not separately.
#
# A filter-only helper can be dropped from its call site with every test of
# it still green -- measured on the first version of this file: 6 of 6
# passed with the call removed. `_print_text_issues` owns both halves so
# capturing its output exercises them together.
# ---------------------------------------------------------------------


def test_the_printer_emits_the_surviving_issue_and_not_the_duplicate(capsys):
    from tan.commands.build_cmd import _print_text_issues

    _print_text_issues(
        [
            _issue(f"slice `a32_cluster` skipped: {_REASON}"),
            _issue("workspace is on a different drive", "build.cross-drive"),
        ],
        _DATA,
    )
    err = capsys.readouterr().err
    assert "workspace is on a different drive" in err
    assert "searched PATH" not in err, err
    assert err.count("\n") == 1, err


def test_the_printer_still_emits_an_issue_no_slice_line_repeats(capsys):
    """Guards the other direction: a dedup that swallowed everything would
    pass the test above while destroying the report."""
    from tan.commands.build_cmd import _print_text_issues

    _print_text_issues([_issue("only report of this", "build.other")], _DATA)
    assert "only report of this" in capsys.readouterr().err
