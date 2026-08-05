# SPDX-License-Identifier: Apache-2.0
"""Pure text-mode rendering for `tan doctor`.

`doctor_cmd.py`'s text branch used to be one `print` per check, with `detail`
and `fix` as unwrapped strings: on a real terminal that wraps at column 0, a
long `fix:` body visually merges into the next `[   pass]` row, and there is
no colour to tell `fail` from `pass` at a glance. This module fixes both,
plus adds a failures-only footer so a customer with several failures gets an
action list without re-reading the whole log.

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

#: `    fix: ` -- the fix line's own prefix, indented under its check the same
#: way the old single-`print` form was (`"\n    fix: {fix}"`).
_FIX_PREFIX = "    fix: "


def _check_lines(check: dict, width: int, color: bool) -> list[str]:
    """`[status] name: detail`, wrapped, plus an indented `fix:` block when
    the check carries one. The status token's OWN width (`:>7`, matching
    `unknown`, the longest status word) is computed on the plain text first,
    so a painted token never throws off the hanging indent's alignment --
    the ANSI bytes are invisible and must not count as columns."""
    status = check["status"]
    name = check["name"]
    prefix = f"[{status:>7}] {name}: "
    indent = " " * len(prefix)
    lines = wrap_block(check["detail"], width, prefix, indent)

    hue = _STATUS_HUE.get(status)
    if color and hue:
        painted = f"[{hue}{status:>7}{_RESET}] {name}: "
        lines[0] = painted + lines[0][len(prefix) :]

    fix = check.get("fix")
    if fix is not None:
        lines.extend(wrap_block(fix, width, _FIX_PREFIX, " " * len(_FIX_PREFIX)))
    return lines


def render_doctor_lines(
    checks: list[dict],
    summary: dict,
    extra_issue_lines: list[str],
    width: int,
    color: bool,
) -> list[str]:
    """The lines `doctor_cmd.doctor`'s text branch prints, in order:

    1. One block per check, in the SAME order `checks` arrives in -- the
       order is a diagnostic progression (`hostPrerequisites` failing is why
       `west` fails too) and reordering would hide that.
    2. `extra_issue_lines` verbatim (already filtered by the caller against
       `checked_codes`, tan-cli#375 -- this function does not re-derive that
       filter).
    3. A failures-only footer, omitted entirely when nothing failed.
    4. The summary line, last, wording unchanged.
    """
    lines: list[str] = []
    for check in checks:
        lines.extend(_check_lines(check, width, color))

    lines.extend(extra_issue_lines)

    failed_names = [c["name"] for c in checks if c["status"] == "fail"]
    if failed_names:
        lines.append("")
        lines.append("Failed checks:")
        lines.extend(f"  - {name}" for name in failed_names)

    lines.append("")
    lines.append(
        f"{summary['pass']} passed, {summary['warn']} warning(s), {summary['fail']} failed."
    )
    return lines
