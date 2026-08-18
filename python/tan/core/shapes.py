# SPDX-License-Identifier: Apache-2.0
"""Two questions four command modules each answered with their own private
copy: "is this directory an alp-sdk checkout?" and "what shape is this YAML
value?" (tan-cli#408).

Neither is domain logic worth four implementations. `_is_sdk_root` had two
(`build_cmd.py`, `flash_cmd.py`) and `_yaml_kind` two
(`diff_cmd.py`, `pinmux_cmd.py`), and the copies had already drifted in TYPE
-- one took `str`, one took `Path` -- which is exactly how a "same" helper
stops being the same one.

Lives under `tan.core` rather than beside any one caller because
`tan/commands/*` import each other freely and `build_cmd` already imports
`sdk_cmd` at module level; a shared helper hosted in either would be a new
edge in that graph. `tan.core` imports no command module, so this direction
cannot cycle.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: THE marker for an alp-sdk checkout (I-31), as path segments so callers can
#: join it either way. `build_cmd.SDK_MARKER` was the previous single
#: spelling and now re-exports this one.
SDK_MARKER = ("scripts", "alp_project.py")


def is_sdk_root(path: Path | str) -> bool:
    """Whether `path` is an alp-sdk checkout -- port of `util.rs::
    has_loader_script`, and INCAPABLE OF RAISING.

    Accepts `Path` or `str` because the copies this replaces disagreed:
    `build_cmd`'s took a `Path`, `flash_cmd`'s took a `str`. Callers keep
    whichever they already hold rather than converting at
    every site.

    tan-cli#408 asks for a deliberate decision on the `except (OSError,
    ValueError)` guard the string-based copy carried, so: **it stays**,
    and the reason is that this is a PRE-FLIGHT guard. Every caller is asking
    "may I use this?" in a command whose whole job is to answer with an
    envelope; a path with an embedded NUL or an unreadable parent must read
    as "not an SDK root", never as a traceback where a refusal belongs.

    Measured on this tree's interpreter (CPython 3.14.6), both
    `Path.is_file()` and `os.path.isfile()` already return `False` rather
    than raising on an embedded NUL, so the guard catches nothing there
    today. It is kept anyway: `requires-python = ">=3.12"`, the floor was not
    measured here, and a guard that costs one `try` is not worth trading for
    an assumption about an interpreter nobody ran.
    """
    try:
        return os.path.isfile(os.path.join(str(path), *SDK_MARKER))
    except (OSError, ValueError):
        return False


def rejected_sdk_root_message(sdk_root: str, consequence: str) -> str:
    """The `<command>.sdk-root-unresolved` message for a `--sdk-root` the
    loader-marker check REJECTED, naming the value the caller typed.

    `--sdk-root` is TERMINAL (I-31), so a path without `scripts/alp_project.py`
    resolves to nothing rather than falling through to a lower tier. Five
    commands (`generate`, `model build`, `new-som`, `pinmux`, `validate`) still
    dropped the rejected path on the floor, and four of those (all but
    `pinmux`) answered with the SAME string they use when no flag was given at
    all -- "Use --sdk-root, place the project near an alp-sdk checkout, ...",
    i.e. recommending the flag the caller had just passed -- with the failing
    value nowhere in the envelope and nowhere in the stderr text either.
    `pinmux` used a message of its own with the same gap (tan-cli#497 defect
    7). A user who typos `--sdk-root ~/alp-sdk-typo` was told to pass
    `--sdk-root`, and could not see WHICH path had been rejected or why, on the
    one surface that knew both.

    `consequence` is the caller's own "and so this is what you got instead"
    clause, because it differs per command (no pinmux table read, nothing
    generated, nothing validated) and is the half a reader acts on. The
    remediation clause is deliberately NOT carried into it: naming the rejected
    path IS the remediation here.

    The no-flag branch is deliberately left alone at every call site. There is
    no typed value to name on it, and for `presets` its exact string is
    byte-pinned by the `presets-no-sdk` golden envelope.

    NOTE FOR WHOEVER MERGES SECOND: tan-cli#620 adds a same-named helper to
    `tan/commands/sdk_cmd.py` for `examples`/`presets`, with this same body.
    The two must be collapsed onto this one -- `tan.core` imports no command
    module, so this is the direction that cannot cycle (see the module
    docstring), and `SDK_MARKER` already lives here.
    """
    marker = "/".join(SDK_MARKER)
    return (
        f'alp-sdk root is unresolved: --sdk-root "{sdk_root}" is not an alp-sdk '
        f"checkout ({marker} not found under it). {consequence}"
    )


def yaml_kind(value: Any) -> str:
    """A short YAML-ish type name for an error message -- not a claim of
    matching serde's exact wording (see `diff_cmd`'s module docstring for the
    scope note this inherits).

    `bool` is tested BEFORE `int`, and that order is load-bearing rather than
    stylistic: `bool` is a subclass of `int` in Python, so the reverse order
    reports every `true`/`false` in a board.yaml as "a number".
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a sequence"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__
