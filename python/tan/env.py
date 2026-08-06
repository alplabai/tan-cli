# SPDX-License-Identifier: Apache-2.0
"""Shared environment-variable contracts, kept in one place so no second
hand-rolled copy can drift from it (tan-cli#288).
"""
from __future__ import annotations

import os
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


def use_color(no_color: bool, ci: bool) -> bool:
    """`Theme::from_args`: human text goes to stderr, so the TTY probe is
    against stderr. Shared by every command's `--no-color`/`--ci` handling
    (moved out of `size_cmd.py`, tan-cli's UX-polish sweep) so a second,
    driftable copy never grows up beside this one -- `size_cmd._use_color`
    now delegates here."""
    if no_color or ci or no_color_requested():
        return False
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False
