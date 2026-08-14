# SPDX-License-Identifier: Apache-2.0
"""Shared environment-variable contracts, kept in one place so no second
hand-rolled copy can drift from it (tan-cli#288).
"""
from __future__ import annotations

import os
import shutil
import sys


def no_color_requested() -> bool:
    """Whether `NO_COLOR` disables colour, per the spec: PRESENCE, not
    truthiness -- `NO_COLOR=` (set, empty) must still disable colour, same as
    `NO_COLOR=0` or `NO_COLOR=false`. Mirrors the Rust oracle exactly
    (`crates/tan-cli/src/style.rs:27`'s `var_os("NO_COLOR").is_none()`).

    `faultdecode_cmd.py` and `size_cmd.py` each hand-rolled this check and
    drifted: one did `if os.environ.get("NO_COLOR")` (truthy -- wrong, an
    empty value read as unset), the other already did the correct presence
    check. Do NOT reach for `typer.Option(..., envvar="NO_COLOR")` instead --
    Click's bool-envvar coercion parses the STRING as a boolean (`1`/`true`/
    `yes`/`on`, ...), so `NO_COLOR=` is "not a valid boolean" and crashes the
    command outright, a regression neither original copy had.
    """
    return os.environ.get("NO_COLOR") is not None


def stderr_is_tty() -> bool:
    """The one place that probes `sys.stderr.isatty()` -- `AttributeError`
    when stderr has been replaced by something that doesn't implement it,
    `ValueError` when it has been closed. Shared by `use_color` and
    `wrap_width` below so neither hand-rolls its own copy of this
    try/except (tan-cli#288, the exact drift this module's own docstring
    exists to prevent).

    tan-cli#488 round 6: promoted from `_stderr_is_tty` (module-private) to
    a public name once `tan.core.consent.can_prompt`,
    `tan.commands.doctor_cmd.fix_suppressed_issue` and
    `tan.commands.build_cmd._dispatch` each turned out to hand-roll the exact
    `X is not None and X.isatty()` shape this function already guards against
    -- a `None` check alone stops the `AttributeError` from a detached
    handle, but not from a handle that exists and simply has no `.isatty()`
    at all, which is exactly what `tan.cli`'s `_TeeStderr` is under
    `--format json`. Duplicating the try/except a fourth time is the same
    drift this module's own docstring already names; importing the one
    version is the fix, not writing a fifth."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def stdin_is_tty() -> bool:
    """The `stdin` counterpart of `stderr_is_tty` above, same rationale and
    same exception pair: `AttributeError` when `sys.stdin` is `None` (a
    detached-stdio process) or has been replaced by something with no
    `.isatty()`, `ValueError` when it has been closed. `tan.core.consent.
    can_prompt` and `tan.commands.doctor_cmd.fix_suppressed_issue` each used
    to hand-roll `sys.stdin is not None and sys.stdin.isatty()` -- correct
    for today's `None`-or-real-stream shape, but the same one-operand-short
    pattern the stderr side was fixed for tan-cli#488 round 5/6; sharing this
    helper closes both sides the same way instead of leaving stdin's copy to
    rediscover the lesson on its own."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def use_color(no_color: bool, ci: bool) -> bool:
    """`Theme::from_args`: human text goes to stderr, so the TTY probe is
    against stderr. Shared by every command's `--no-color`/`--ci` handling
    (moved out of `size_cmd.py`, tan-cli's UX-polish sweep) so a second,
    driftable copy never grows up beside this one -- `size_cmd._use_color`
    now delegates here."""
    if no_color or ci or no_color_requested():
        return False
    return stderr_is_tty()


#: The floor a resolved terminal width may never drop below, moved here from
#: `doctor_cmd._DOCTOR_TEXT_MIN_WIDTH` (tan-cli's `doctor` wrap-seam adoption,
#: PR #480) so `wrap_width` below and `doctor_cmd.doctor` can share the one
#: number instead of drifting into two: a real terminal narrower than this
#: (or a redirected/piped run whose `shutil.get_terminal_size` fallback would
#: otherwise apply) must not be left to wrap one word per line. `doctor_cmd`
#: imports this name rather than keeping its own copy.
TEXT_WRAP_MIN_WIDTH = 60

#: Rows handed to `shutil.get_terminal_size`'s `fallback` pair by
#: `terminal_width` below. Never read by any caller -- every width probe in
#: this CLI wants columns only -- but the parameter takes a `(columns, lines)`
#: pair, so naming the number here keeps each call site from restating a bare
#: `24` it does not care about.
_TERMINAL_HEIGHT_FALLBACK = 24


def terminal_width(fallback: int) -> int:
    """The one place that MEASURES a terminal, beside `stderr_is_tty` above
    which is the one place that PROBES one -- the column count of the stream
    every text surface in this CLI actually prints to, `sys.stderr`.

    tan-cli#564: `wrap_width` below, `doctor_cmd.doctor`'s report width and
    `build_cmd._heartbeat_line_width` were each armed on `stderr_is_tty()`
    and then sized off a bare `shutil.get_terminal_size(fallback=(100, 24))`,
    which CPython resolves against `os.get_terminal_size(sys.__stdout__.
    fileno())`. Nothing in text mode is written to stdout, so with stdout
    redirected that ioctl raised `OSError [Errno 25] Inappropriate ioctl for
    device` -- swallowed by `shutil` itself -- and every probe silently took
    the hard-coded 100 columns while stderr's own fd read 70. The #536
    isatty-guard consolidation moved the PROBE onto one shared helper but
    left the MEASUREMENT on the other handle; this moves it.

    `COLUMNS` comes first so `shutil`'s existing precedence is preserved
    exactly: an operator who exports `COLUMNS=137` is overriding BOTH handles
    on purpose, and that override must not become the one thing the move
    takes away. A non-positive, empty or non-numeric value is ignored, and so
    is a 0-column ioctl answer (a known pty shape) -- both exactly as
    `shutil` ignores them, and because a literal 0 would reach
    `build_cmd._heartbeat_line_width` as a one-column line.

    The last step falls through to `shutil.get_terminal_size` rather than
    straight to `fallback`, so the mirror-image run -- stderr redirected
    while stdout is still a terminal -- resolves exactly the number it
    resolves today. `fallback` is a plain column count; the
    `(columns, lines)` pair `shutil` wants is assembled here.
    """
    try:
        columns = int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        pass
    else:
        if columns > 0:
            return columns
    try:
        # `AttributeError` when stderr has been replaced by something with no
        # `.fileno()`, `ValueError` when it has been closed, `OSError` when the
        # fd is real but not a terminal (the redirected case above).
        measured = os.get_terminal_size(sys.stderr.fileno()).columns
    except (AttributeError, ValueError, OSError):
        measured = 0
    if measured > 0:
        return measured
    return shutil.get_terminal_size(fallback=(fallback, _TERMINAL_HEIGHT_FALLBACK)).columns


def wrap_width() -> int | None:
    """The column budget for hard-wrapping prose to stderr, or `None` when
    stderr is not a terminal.

    `None` is not "80 columns", it is "do not wrap at all" -- a caller must
    print each logical line as one physical line. This is the design point
    `explain`/`sdk current` adopt this seam for: `tan explain | grep` (or any
    pipe) wants one record per line, and inserting a hard newline into a
    piped stream breaks that in a way a real terminal's own non-destructive
    soft-wrap never would. So the probe here is narrower than `use_color`'s:
    a redirected/piped stream (not a tty) always returns `None`, full stop.

    Deliberately takes no `no_color`/`ci` arguments, unlike `use_color`
    above, and must not gain them: `--no-color` asks only to drop ANSI
    colour, and `--ci` asks only to skip interactive prompts (`can_prompt`)
    -- neither says anything about whether stderr is a pipe, and a user who
    passed `--no-color` on a real terminal still wants readable line breaks.
    Folding either flag in here would silently also turn off wrapping for
    every `--no-color` invocation, which is a second, unrelated behaviour
    change riding on a flag that never asked for it.

    `doctor` deliberately does NOT use this seam: it resolves its width
    unconditionally, so `tan doctor > report.txt` still comes out wrapped.
    That is a difference in what the two kinds of output ARE, not an
    oversight, and it is the reason this function exists as a separate
    decision rather than something `render_check_lines` could have called
    for everybody. `doctor` prints a REPORT -- prose a human reads top to
    bottom, whose redirected form is a file someone opens later and wants
    wrapped exactly as the terminal would have shown it. `explain` and
    `sdk current` print RECORDS -- `Generation targets: ...`, `  path
    <value>` -- whose redirected form is an input to `grep`/`cut`, where a
    hard newline in the middle of a value is corruption. Shape of the
    output decides, so a future command adopting this seam should ask which
    of the two it is rather than copying whichever neighbour it read first.
    """
    if not stderr_is_tty():
        return None
    return max(terminal_width(100), TEXT_WRAP_MIN_WIDTH)
