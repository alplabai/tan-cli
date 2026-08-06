# SPDX-License-Identifier: Apache-2.0
"""`tan.core.doctor_render` -- pure unit tests for `doctor`'s text-mode
rendering, driven directly against `render_doctor_lines` rather than through
the CLI (see `tests/commands/test_doctor_command.py` for the one flag-wiring
test that needs a real dispatch). No terminal, no colour probe, no `print`
here -- the function takes `width`/`color` as plain arguments."""
from __future__ import annotations

from tan.core import doctor_render
from tan.core.doctor_render import render_doctor_footer, render_doctor_lines


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
    back to the last word boundary inside it, with a single trailing `…`.

    Width 100, not 80: `"  - sdk: "` plus a `_FOOTER_FIX_BUDGET`-truncated
    (75) fix plus `"…"` can run to 85 columns on its own, and THIS test is
    about truncation, not wrapping -- see
    `test_footer_lines_never_exceed_width_with_a_long_fix_and_a_long_check_
    name` below for the wrapping contract. A width narrow enough to also
    wrap this particular row would land the trailing `…` on a continuation
    line instead of the one row this test inspects, which is a real,
    already-covered behaviour (wrapping), not a broken truncation."""
    long_fix = "install " * 20  # > 75 chars, no punctuation at all
    checks = [{"name": "sdk", "status": "fail", "detail": "d", "fix": long_fix}]
    summary = {"pass": 0, "warn": 0, "fail": 1}

    lines = render_doctor_lines(checks, summary, [], 100, color=False)
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


# --------------------------------------------------------------------------
# Review finding 1: `render_doctor_footer` used to take no `width` at all --
# everything AFTER the check blocks (`extra_issue_lines`, the failed-checks
# rows) printed unwrapped, inside the very report this whole task exists to
# wrap. Measured: a real `doctor.fix-suppressed` line ran to 262 columns; a
# failed check's own footer row -- `"  - " + name + ": " + truncated fix` --
# reached `4 + len(name) + 2 + 76` = 107 for `zephyrSdkAvailableForHost`
# (25 chars). Fails red against the pre-fix `render_doctor_footer` two ways:
# it takes no fourth `width` argument at all, and even patched to accept and
# ignore one, the two fixtures below would come back unwrapped.
# --------------------------------------------------------------------------


def test_footer_lines_fit_width_except_a_single_overlong_token_left_intact():
    """`render_doctor_footer` wraps through `wrap_block`, which deliberately
    lets a single TOKEN that alone (plus whatever indent frames it) exceeds
    `width` overflow its own line rather than break it mid-character -- the
    identifier-fidelity fix this branch exists for (see `wrap_block`'s own
    docstring). So "every line fits `width`" is a STRONGER invariant than
    this function actually provides -- asserting it would invite someone to
    "fix" correct behaviour. The true invariant, proven here with a real
    Windows toolchain path (no spaces, so it survives as one token) in
    `extra_issue_lines`, plus the long-name/long-fix failed-checks row from
    the truncation test above: every returned line fits `width`, except one
    that is a single token, on its own line, over-length, and printed
    verbatim."""
    long_path = (
        r"C:\Users\dev\AppData\Local\Programs\zephyr-sdk-0.17.4"
        r"\arm-zephyr-eabi\bin\arm-zephyr-eabi-gcc.exe"
    )
    long_warning = f"warning: toolchain not found at {long_path}"
    long_fix = " ".join(["install"] * 20)
    checks = [
        {
            "name": "zephyrSdkAvailableForHost",
            "status": "fail",
            "detail": "d",
            "fix": long_fix,
        }
    ]
    summary = {"pass": 0, "warn": 1, "fail": 1}
    width = 60

    lines = render_doctor_footer(checks, summary, [long_warning], width)

    def _leading_spaces(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    overlong = [line for line in lines if len(line) > width]
    assert overlong, lines  # the fixture must actually exercise the exception
    for line in overlong:
        indent = _leading_spaces(line)
        token = line[indent:]
        assert " " not in token, (width, line)  # a single token, not a wrapped sentence
        assert len(token) > width - indent, (width, line)  # genuinely could not have fit

    for line in lines:
        if line not in overlong:
            assert len(line) <= width, (width, line)

    # Continuations of both shapes land on the SAME rail every check's own
    # detail/fix continuation already uses -- one column for the whole
    # report, not a second one invented for the tail. A continuation's
    # leading-space count is exactly `_CONTINUATION_INDENT` (10); the
    # `"  - "`-prefixed first line of a footer row has only 2, which is what
    # keeps this check from also matching that line.
    continuations = [
        line
        for line in lines
        if line and _leading_spaces(line) == doctor_render._CONTINUATION_INDENT
    ]
    assert continuations, lines  # both fixtures actually needed to wrap
