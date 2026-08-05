# SPDX-License-Identifier: Apache-2.0
"""`tan.core.doctor_render` -- pure unit tests for `doctor`'s text-mode
rendering, driven directly against `render_doctor_lines` rather than through
the CLI (see `tests/commands/test_doctor_command.py` for the one flag-wiring
test that needs a real dispatch). No terminal, no colour probe, no `print`
here -- the function takes `width`/`color` as plain arguments."""
from __future__ import annotations

from tan.core import doctor_render
from tan.core.doctor_render import render_doctor_lines


def test_fail_status_is_colored_when_requested_and_plain_when_not():
    checks = [{"name": "west", "status": "fail", "detail": "west not found"}]
    summary = {"pass": 0, "warn": 0, "fail": 1}

    colored = render_doctor_lines(checks, summary, [], 80, color=True)
    assert "\x1b[31m" in colored[0], colored[0]
    assert "\x1b[0m" in colored[0], colored[0]

    plain = render_doctor_lines(checks, summary, [], 80, color=False)
    assert not any("\x1b[" in line for line in plain), plain


def test_long_detail_wraps_and_no_continuation_line_starts_at_column_0():
    detail = " ".join(["reallylongword"] * 20)
    checks = [{"name": "pythonFloor", "status": "warn", "detail": detail}]
    summary = {"pass": 0, "warn": 1, "fail": 0}

    lines = render_doctor_lines(checks, summary, [], 40, color=False)

    # Everything up to the blank line before the summary is this one check's
    # block -- more than one line, since 40 columns cannot hold the detail.
    block = lines[: lines.index("")]
    assert len(block) > 1, block
    assert block[0].startswith("[")
    for continuation in block[1:]:
        assert continuation.startswith(" "), repr(continuation)


def test_check_order_is_preserved_not_sorted():
    checks = [
        {"name": "zCheck", "status": "pass", "detail": "z detail"},
        {"name": "aCheck", "status": "fail", "detail": "a detail"},
    ]
    summary = {"pass": 1, "warn": 0, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    z_at = next(i for i, line in enumerate(lines) if "zCheck:" in line)
    a_at = next(i for i, line in enumerate(lines) if "aCheck:" in line)
    assert z_at < a_at, lines


def test_failures_footer_names_every_fail_and_no_pass_or_warn():
    checks = [
        {"name": "ok", "status": "pass", "detail": "d"},
        {"name": "meh", "status": "warn", "detail": "d"},
        {"name": "bad1", "status": "fail", "detail": "d"},
        {"name": "bad2", "status": "fail", "detail": "d"},
    ]
    summary = {"pass": 1, "warn": 1, "fail": 2}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    assert "Failed checks:" in lines
    assert "  - bad1" in lines
    assert "  - bad2" in lines
    assert "  - ok" not in lines
    assert "  - meh" not in lines


def test_no_failures_footer_when_nothing_failed():
    checks = [{"name": "ok", "status": "pass", "detail": "d"}]
    summary = {"pass": 1, "warn": 0, "fail": 0}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    assert "Failed checks:" not in lines


def test_summary_line_is_last_and_keeps_its_exact_wording():
    checks = [{"name": "ok", "status": "pass", "detail": "d"}]
    summary = {"pass": 3, "warn": 2, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    assert lines[-1] == "3 passed, 2 warning(s), 1 failed."


# --------------------------------------------------------------------------
# Change 1: one fixed continuation indent, shared by `detail` continuations
# AND the `fix:` block, regardless of the check's own name length (review
# O-2 -- the old code re-derived the detail indent from `len(prefix)`, so it
# drifted with name length while the fix block used its own separate fixed
# 9, and the two never agreed).
# --------------------------------------------------------------------------


def test_continuation_indent_is_fixed_for_both_detail_and_fix_across_name_lengths():
    """`sdk` (3 chars) and `zephyrSdkAvailableForHost` (25 chars) must land
    every continuation line -- wrapped `detail` AND the `fix:` block -- on
    the exact SAME column, never left of it."""
    long_detail = " ".join(["detailword"] * 20)
    long_fix = " ".join(["fixword"] * 20)
    checks = [
        {"name": "sdk", "status": "fail", "detail": long_detail, "fix": long_fix},
        {
            "name": "zephyrSdkAvailableForHost",
            "status": "fail",
            "detail": long_detail,
            "fix": long_fix,
        },
    ]
    summary = {"pass": 0, "warn": 0, "fail": 2}

    lines = render_doctor_lines(checks, summary, [], 60, color=False)
    footer_at = lines.index("")
    block = lines[:footer_at]

    # Every continuation line -- detail wrap AND the `fix:` line and its own
    # wrap -- is everything that does NOT open a new check (`[status] name:`).
    continuations = [line for line in block if not line.startswith("[")]
    assert len(continuations) > 2, block  # both checks actually wrapped

    indents = {len(line) - len(line.lstrip(" ")) for line in continuations}
    assert indents == {doctor_render._CONTINUATION_INDENT}, block
    assert doctor_render._CONTINUATION_INDENT >= 10, "must never sit left of `[   fail] ` (10 cols)"


# --------------------------------------------------------------------------
# Change 2: the failures footer names the start of each fix (review O-1).
# --------------------------------------------------------------------------


def test_footer_row_truncates_a_long_fix_to_the_budget_on_a_word_boundary():
    """No sentence break to lean on (`sdk`'s own real fix is exactly this
    shape: one long comma-joined clause) -- a plain character budget, cut
    back to the last word boundary inside it, with a single trailing `…`."""
    long_fix = "install " * 20  # > 75 chars, no punctuation at all
    checks = [{"name": "sdk", "status": "fail", "detail": "d", "fix": long_fix}]
    summary = {"pass": 0, "warn": 0, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    footer_row = next(line for line in lines if line.startswith("  - sdk"))

    assert footer_row.startswith("  - sdk: "), footer_row
    assert footer_row.endswith("…") and footer_row.count("…") == 1, footer_row
    body = footer_row[len("  - sdk: ") : -1]
    assert len(body) <= doctor_render._FOOTER_FIX_BUDGET, footer_row
    assert not body.endswith(" "), "must cut on a word boundary, not mid-word"


def test_footer_row_is_bare_name_when_the_check_has_no_fix():
    checks = [{"name": "workspace", "status": "fail", "detail": "d"}]
    summary = {"pass": 0, "warn": 0, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    assert "  - workspace" in lines
    assert not any(line.startswith("  - workspace:") for line in lines)


def test_footer_row_keeps_a_short_fix_verbatim_with_no_ellipsis():
    checks = [{"name": "sdk", "status": "fail", "detail": "d", "fix": "--sdk-root <path>"}]
    summary = {"pass": 0, "warn": 0, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 80, color=False)
    assert "  - sdk: --sdk-root <path>" in lines
