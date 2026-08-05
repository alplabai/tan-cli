# SPDX-License-Identifier: Apache-2.0
"""Pure text-mode rendering for `tan doctor`.

`doctor_cmd.py`'s text branch used to be one `print` per check, with `detail`
and `fix` as unwrapped strings: on a real terminal that wraps at column 0, a
long `fix:` body visually merges into the next `[   pass]` row, and there is
no colour to tell `fail` from `pass` at a glance. This module fixes both,
plus adds a failures-only footer so a customer with several failures gets an
action list without re-reading the whole log.

v2 (three changes, bundled because they interact -- see
`doctor-v2-brief.md`): (1) one FIXED continuation indent for both `detail`
and `fix` bodies, replacing the old per-check-name-length indent, which is
also what makes (3) possible -- a check can be rendered without knowing any
other check's name length; (2) the failures footer now names the start of
each fix, not just the bare check name; (3) `render_check_lines` (one
check) and `render_doctor_footer` (everything after the checks) are exposed
separately so `doctor_cmd`'s streaming path can print each check as it
completes and call the footer once, at the end, instead of only ever having
the all-at-once `render_doctor_lines` this module used to expose.

Pure and IO-free -- no terminal probe, no `print` -- so it is testable
without one; `doctor_cmd.doctor` is the only caller, and reads `width` off
`shutil.get_terminal_size()` and `color` off `tan.env.use_color` before
calling in.
"""
from __future__ import annotations

from tan.core.text_layout import wrap_block

# colorama Fore/Style literals, matching `tan.core.size`'s and
# `tan.core.faultdecode`'s convention rather than a new dependency.
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"

_STATUS_HUE: dict[str, str] = {
    "pass": _GREEN,
    "warn": _YELLOW,
    "fail": _RED,
    "unknown": _CYAN,
}

#: Every continuation line -- wrapped `detail` text AND the `fix:` block --
#: lands on this ONE column now (review O-2: the old code re-derived the
#: detail indent from `len(prefix)`, so it drifted with name length (15 for
#: `sdk`, 23 for `pythonFloor`), while the fix block used its own separate
#: fixed 9 -- two rails that agreed with neither each other nor themselves
#: down the report). 10 is `len("[   fail] ")`, the bracket+status prefix
#: every check shares regardless of its name -- the floor a continuation can
#: never sit left of without reading as a new entry rather than one -- so a
#: continuation lands exactly under where the check's own name starts, for
#: every check, independent of that name's length.
_CONTINUATION_INDENT = 10

#: `fix: ` printed at `_CONTINUATION_INDENT`, like a second paragraph under
#: the check -- its own wrapped continuation lines land back on that SAME
#: column, not indented further under the label, which is the "same rail
#: for detail and fix" this task's change 1 asks for.
_FIX_PREFIX = " " * _CONTINUATION_INDENT + "fix: "

#: ~75 characters of a failing check's `fix` text in the footer row (review
#: range: 70-80). A CHARACTER budget, not a sentence split -- the `sdk`
#: check's own fix (its longest) is one comma-joined clause with no sentence
#: break, so first-sentence extraction would not shorten it at all.
_FOOTER_FIX_BUDGET = 75


def _truncate_fix(fix: str, budget: int = _FOOTER_FIX_BUDGET) -> str:
    """`fix`, cut to `budget` characters on a word boundary where the budget
    lands inside one, with a single `…` appended -- never a mid-word cut,
    never a bare truncation with no marker. Returns `fix` byte-for-byte when
    it already fits, with no trailing `…` added to text that was not cut."""
    if len(fix) <= budget:
        return fix
    cut = fix[:budget]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return f"{cut}…"


def render_check_lines(check: dict, width: int, color: bool) -> list[str]:
    """`[status] name: detail`, wrapped, plus an indented `fix:` block when
    the check carries one -- ONE check's lines. `render_doctor_lines` below
    (the batch path) and `doctor_cmd`'s streaming path (print the moment a
    check completes, change 3) both call this SAME function, so the two
    never fork into separate formatting code. The status token's OWN width
    (`:>7`, matching `unknown`, the longest status word) is computed on the
    plain text first, so a painted token never throws off the hanging
    indent's alignment -- the ANSI bytes are invisible and must not count as
    columns.
    """
    status = check["status"]
    name = check["name"]
    prefix = f"[{status:>7}] {name}: "
    indent = " " * _CONTINUATION_INDENT
    lines = wrap_block(check["detail"], width, prefix, indent)

    hue = _STATUS_HUE.get(status)
    if color and hue:
        painted = f"[{hue}{status:>7}{_RESET}] {name}: "
        lines[0] = painted + lines[0][len(prefix) :]

    fix = check.get("fix")
    if fix is not None:
        lines.extend(wrap_block(fix, width, _FIX_PREFIX, indent))
    return lines


def render_doctor_footer(
    checks: list[dict], summary: dict, extra_issue_lines: list[str], width: int
) -> list[str]:
    """Everything AFTER the per-check blocks, in order:

    1. `extra_issue_lines` verbatim (already filtered by the caller against
       `checked_codes`, tan-cli#375), each wrapped through `wrap_block` at
       `width` -- the same seam every check block above already goes
       through. Measured before this wrapping existed: a real
       `doctor.fix-suppressed` line ran to 262 columns, unwrapped, inside
       the very report this whole task exists to wrap.
    2. A failures-only footer, omitted entirely when nothing failed -- each
       row names the START of its own fix, truncated to
       `_FOOTER_FIX_BUDGET`, then wrapped the same way as (1): truncation
       keeps the row SHORT, wrapping keeps whatever survives that cut
       actually on screen. Measured unwrapped: `4 + len(name) + 2 +
       _FOOTER_FIX_BUDGET + 1` (the trailing `+ 1` is `_truncate_fix`'s own
       `…`) = 107 columns for `zephyrSdkAvailableForHost` (25 chars). A
       failing check with no `fix` falls back to the bare `- <name>` row.
    3. The summary line, last, never wrapped -- one short, fixed sentence.

    `width` is the SAME value `doctor_cmd.doctor` resolves for the per-check
    blocks above -- never a second, independently floored number. Every
    continuation in (1)/(2) lands at `_CONTINUATION_INDENT`, the same rail
    check `detail`/`fix` continuations already use.

    Split out from `render_doctor_lines` so `doctor_cmd`'s streaming path
    can print each check as it completes and call this once, for the tail.
    """
    hanging = " " * _CONTINUATION_INDENT
    lines: list[str] = []
    for issue_line in extra_issue_lines:
        lines.extend(wrap_block(issue_line, width, "", hanging))

    failed = [c for c in checks if c["status"] == "fail"]
    if failed:
        lines.append("")
        lines.append("Failed checks:")
        for c in failed:
            fix = c.get("fix")
            body = f"{c['name']}: {_truncate_fix(fix)}" if fix else c["name"]
            lines.extend(wrap_block(body, width, "  - ", hanging))

    lines.append("")
    lines.append(
        f"{summary['pass']} passed, {summary['warn']} warning(s), {summary['fail']} failed."
    )
    return lines


def render_doctor_lines(
    checks: list[dict],
    summary: dict,
    extra_issue_lines: list[str],
    width: int,
    color: bool,
) -> list[str]:
    """The lines `doctor_cmd.doctor`'s text branch prints, in order: one
    block per check (`render_check_lines`, SAME order `checks` arrives in --
    the order is a diagnostic progression, `hostPrerequisites` failing is why
    `west` fails too, and reordering would hide that), then the tail
    (`render_doctor_footer`).

    The one-call batch form, for a caller holding the full check list up
    front (this module's own tests); the streaming caller in `doctor_cmd`
    calls `render_check_lines`/`render_doctor_footer` directly instead, see
    their docstrings for why.
    """
    lines: list[str] = []
    for check in checks:
        lines.extend(render_check_lines(check, width, color))
    lines.extend(render_doctor_footer(checks, summary, extra_issue_lines, width))
    return lines
