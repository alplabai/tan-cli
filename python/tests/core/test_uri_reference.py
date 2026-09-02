# SPDX-License-Identifier: Apache-2.0
"""`tan.core.uri_reference.path_to_uri_reference` (tan-cli#1097).

This repo has NO Windows host (PR #1089/#1090's own rule, restated in
`test_board_context.py`'s header): a Windows-spelled path exercises the
Windows branch on Linux/mac CI identically, because
[`path_to_uri_reference`] picks its oracle (`PureWindowsPath` vs
`PurePosixPath`) from the STRING itself, never from `os.name`. `ntpath.isabs`
and `PureWindowsPath(...).as_uri()`/`.is_absolute()` are all pure string
operations -- no filesystem touch -- so they are real oracles here, not a
simulation.
"""
from __future__ import annotations

import ntpath
import posixpath
from pathlib import PurePosixPath, PureWindowsPath

from tan.core.uri_reference import path_to_uri_reference

# ---------------------------------------------------------------------------
# Absolute paths become absolute `file:` URIs
# ---------------------------------------------------------------------------


def test_an_absolute_posix_path_becomes_a_file_uri():
    uri = path_to_uri_reference("/w/proj/board.yaml")
    assert uri == "file:///w/proj/board.yaml"
    assert uri == PurePosixPath("/w/proj/board.yaml").as_uri()


def test_an_absolute_windows_drive_path_becomes_a_file_uri():
    """The issue's own reduction: `C:\\w\\proj\\board.yaml` must become a
    scheme-carrying, forward-slashed, percent-encoded `file:` URI, not a
    bare path with the backslashes swapped."""
    uri = path_to_uri_reference(r"C:\w\proj\board.yaml")
    assert uri == PureWindowsPath(r"C:\w\proj\board.yaml").as_uri()
    assert uri == "file:///C:/w/proj/board.yaml"
    assert uri.startswith("file:///")
    # The two things the issue calls invalid in a URI reference at that
    # position: no bare backslash, and the drive letter is followed by `/`
    # under the `file:///C:/...` authority-less form, not a bare `:`.
    assert "\\" not in uri
    assert "C:/" in uri


def test_a_pre_1090_mixed_separator_windows_path_still_becomes_a_file_uri():
    """Pre-#1090 the field could render the MIXED `C:\\w\\proj/board.yaml`
    (backslash root, forward-slash join) -- also invalid, for the same
    reason. `ntpath.isabs`/`PureWindowsPath` treat a mixed-separator string
    the same as a pure one, so the fix covers this shape too without a
    special case."""
    mixed = "C:\\w\\proj/board.yaml"
    uri = path_to_uri_reference(mixed)
    assert uri == PureWindowsPath(mixed).as_uri()
    assert uri.startswith("file:///")


def test_a_windows_unc_path_becomes_a_file_uri():
    uri = path_to_uri_reference(r"\\server\share\board.yaml")
    assert uri == PureWindowsPath(r"\\server\share\board.yaml").as_uri()
    assert ntpath.isabs(r"\\server\share\board.yaml")


def test_a_forward_slash_only_absolute_root_is_judged_posix_not_windows():
    """No backslash and no drive letter -- `_is_windows_spelled` must read
    this as POSIX. This also documents WHY the gate is `.is_absolute()` and
    not `ntpath.isabs`: `ntpath.isabs("/w/proj/board.yaml")` is `True`
    ("absolute on the current drive"), but `PureWindowsPath(...)
    .is_absolute()` is `False` (pathlib requires a drive too), and calling
    `.as_uri()` after trusting `ntpath.isabs` alone raises."""
    uri = path_to_uri_reference("/w/proj/board.yaml")
    assert uri == PurePosixPath("/w/proj/board.yaml").as_uri()
    assert ntpath.isabs("/w/proj/board.yaml")
    assert not PureWindowsPath("/w/proj/board.yaml").is_absolute()


def test_a_windows_rooted_but_driveless_path_does_not_raise_and_stays_slash_swapped():
    """`ntpath.isabs(r"\\proj\\x")` is `True` -- "absolute on the current
    drive" -- but there is no drive to resolve it against, and
    `PureWindowsPath(...).as_uri()` refuses it (`is_absolute()` requires
    drive AND root). Gating on `.is_absolute()` routes this to the
    backslash-swap branch instead of crashing; the result is a legal
    relative-ref (RFC 3986's `path-absolute` form: leading `/`, no scheme),
    not a claim about which drive it is rooted on."""
    path = "\\proj\\board.yaml"
    assert ntpath.isabs(path)
    assert not PureWindowsPath(path).is_absolute()
    assert path_to_uri_reference(path) == "/proj/board.yaml"


def test_a_relative_windows_spelled_path_gets_its_backslashes_swapped_for_slashes():
    """`resolve_board_path("sub\\p", None)`'s exact shape
    (`test_a_relative_windows_root_joins_with_a_backslash_too`,
    `test_board_context.py`). A raw backslash is not a legal URI-reference
    character at all (RFC 3986's `pchar` excludes it unencoded), so this
    case is NOT "returned unchanged" like the POSIX relative case below --
    it is made valid by swapping separators, which changes no Windows
    filesystem meaning (Windows accepts `/` as an equally valid separator)."""
    assert path_to_uri_reference("sub\\p\\board.yaml") == "sub/p/board.yaml"


# ---------------------------------------------------------------------------
# Relative paths stay relative -- this function's documented decision
# ---------------------------------------------------------------------------


def test_the_resolvers_own_relative_default_is_returned_unchanged():
    """`resolve_board_path(None, None)` -- `tan validate`'s own default --
    always answers `"./board.yaml"`. The pinned golden
    (`test_validate_command.py`, `contract/envelopes/`) expects this EXACT
    string back, not an absolute `file:` URI."""
    assert path_to_uri_reference("./board.yaml") == "./board.yaml"


def test_a_relative_posix_path_stays_unchanged():
    assert not posixpath.isabs("sub/p/board.yaml")
    assert path_to_uri_reference("sub/p/board.yaml") == "sub/p/board.yaml"


def test_as_uri_itself_would_raise_on_a_relative_path_which_is_why_this_function_exists():
    """Documents the constraint [`path_to_uri_reference`]'s docstring
    states: `PurePath.as_uri()` only accepts an absolute path, so this
    function cannot simply delegate to it unconditionally."""
    try:
        PurePosixPath("./board.yaml").as_uri()
    except ValueError as err:
        assert "relative path" in str(err)
    else:  # pragma: no cover - documents the guard, does not exercise it
        raise AssertionError("expected pathlib to refuse a relative as_uri()")
