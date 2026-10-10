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
#: join it either way. THE single spelling: `build_cmd` and, since
#: tan-cli#815, `sdk_cmd` both re-export this one rather than holding a
#: literal of their own. Relocating `scripts/alp_project.py` is a one-line
#: change here, which is the whole point -- a second literal meant missing it
#: silently broke SDK resolution in five commands.
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


def is_file(path: Path | str) -> bool:
    """`os.path.isfile`, INCAPABLE OF RAISING -- the same pre-flight contract
    `is_sdk_root` above documents at length.

    Four private copies preceded this one (tan-cli#815): `size_cmd`,
    `image_cmd` and `flash_cmd` each held a byte-identical `str` version, and
    `bootstrap_cmd`'s took a `Path` -- the TYPE drift this module's docstring
    was written about, so the signature is `Path | str` for the same reason
    `is_sdk_root`'s is: callers keep whichever they already hold.

    Its narrower `except OSError` (the others caught `(OSError, ValueError)`)
    was INERT: `pathlib` catches `ValueError` inside `Path.is_file()`, so
    there was nothing to miss. Measured, not assumed -- an earlier draft of
    tan-cli#815 called that half a behaviour change and it was not. The type
    change does move one input: `Path('')` normalises to `Path('.')`, so the
    old `_is_dir("")` answered `True` where this answers `False`. Unreachable
    from its call sites, every one of which passes a join.

    All three `str` callers read `build/system-manifest.yaml`, whose values
    are manifest-supplied strings that may carry an embedded NUL or an
    overlong component. Such a path is "not a file", never an exception
    escaping the envelope.
    """
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def is_dir(path: Path | str) -> bool:
    """`os.path.isdir`, INCAPABLE OF RAISING -- `is_file`'s sibling, same
    contract and same two prior copies (`image_cmd` took `str` and caught
    `(OSError, ValueError)`, `bootstrap_cmd` took `Path` and caught `OSError`).

    NOT the same question as `presets_cmd._is_dir` or
    `examples_cmd._is_dir_no_follow`, which take an `os.DirEntry` and differ
    on whether symlinks are followed. Those are a different predicate with a
    deliberate divergence recorded at each site; they are not copies of this
    and must not be folded into it.
    """
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False


def matches_glob_suffix(name: str, *suffixes: str) -> bool:
    """True if @name ends in any of @suffixes -- case-SENSITIVELY on POSIX,
    case-INSENSITIVELY on Windows, matching `Path.glob`'s own
    `case_sensitive=None` default (platform casing rules) exactly.

    THE one spelling of the casing rule every `Path.glob("*.<ext>")` ->
    `os.listdir`/`os.scandir` swap in this tree needs (tan-cli#1127 review
    round 2, then tan-cli#1132). Swapping a glob for a listing plus a plain
    `name.endswith(suffix)` silently NARROWS the match on Windows: a
    `Foo.YAML` board file, or a `prj.CONF` Kconfig fragment, that `glob`
    used to enumerate there becomes invisible, and nothing at the call site
    announces the change. Three call sites share this one rule rather than
    re-deriving it per swap -- `new_som_cmd._is_yaml_board_file`,
    `commands/build/configure_inputs`'s fragment/overlay walk, and
    `model/analyze._resolve_table`'s support-table listing.

    @suffixes are case-folded here, so a caller may spell them either way;
    what varies by platform is only whether @name is folded to match.
    """
    if os.name == "nt":
        return name.lower().endswith(tuple(s.lower() for s in suffixes))
    return name.endswith(suffixes)


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

    tan-cli#620 landed a same-named helper in `tan/commands/sdk_cmd.py` with
    a byte-identical body, and the note here asking for the two to be
    collapsed sat unexecuted for four releases (tan-cli#815). They are one
    function now: `sdk_cmd` imports this one. The direction is the only one
    that cannot cycle -- `tan.core` imports no command module (see the module
    docstring).
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
