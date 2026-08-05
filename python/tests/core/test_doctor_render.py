# SPDX-License-Identifier: Apache-2.0
"""`tan.core.doctor_render` -- pure unit tests for `doctor`'s text-mode
rendering, driven directly against `render_doctor_lines` rather than through
the CLI (see `tests/commands/test_doctor_command.py` for the one flag-wiring
test that needs a real dispatch). No terminal, no colour probe, no `print`
here -- the function takes `width`/`color` as plain arguments."""
from __future__ import annotations

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
