# SPDX-License-Identifier: Apache-2.0
"""Render a host-native filesystem path as a valid URI reference
(tan-cli#1097).

`validate`'s `--format diagnostic-v1` (LSP `uri`) and `--format sarif`
(`artifactLocation.uri`) both carry a field that is a URI reference by spec,
not a filesystem path -- SARIF 2.1.0 says so explicitly for
`artifactLocation.uri`, and LSP diagnostics are keyed by document URI, which
an editor compares against the URI of an open buffer. Both sites used to emit
`board_path` bare. On Windows that renders `C:\\w\\proj\\board.yaml`: the
drive-letter colon and the backslashes are not valid in that position, and
the string carries no scheme, so neither consumer can resolve it -- silently,
with no crash or error, exactly the class of defect tan-cli#1073/#1077/#1082/
#1084 already closed elsewhere in this repo.

This is a RENDERING concern of those two exporters, not a resolution one, so
it lives here rather than in `tan.core.board_context.resolve_board_path`
(which PR #1090 built to answer "where is the file", with a deliberate
separator-follows-root rule) or in `validate_cmd.py` itself. Only the two
`uri` fields are meant to go through [`path_to_uri_reference`]:
`data.boardYamlPath`, the `--input` argv passed to the SDK validator
subprocess, and every other `Path()` consumer in this repo stay host-native
on purpose -- `tests/core/test_uri_reference.py` asserts that split so a
future "tidy this up" sweep does not unify the two contracts.
"""
from __future__ import annotations

import ntpath
import re
from pathlib import PurePosixPath, PureWindowsPath

#: A Windows drive letter at the string's own start (`C:...`, no backslash
#: required yet) -- `resolve_board_path` never emits this bare, but
#: `--board-yaml` is caller-supplied and this oracle should not miss it.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_windows_spelled(path: str) -> bool:
    """Whether `path` is WINDOWS-spelled, judged from the string itself
    rather than `os.name` -- this repo has no Windows host (tan-cli#1089/
    #1090's own rule), so [`path_to_uri_reference`] must pick its oracle
    (`PureWindowsPath` vs `PurePosixPath`) from the path's own spelling,
    never the CI host's."""
    return "\\" in path or bool(_WINDOWS_DRIVE_RE.match(path))


def path_to_uri_reference(path: str) -> str:
    """`path` rendered as a valid URI reference (RFC 3986).

    `PurePath.as_uri()` is the exporter for the absolute case, and it is the
    gate too: `.is_absolute()`, NOT `ntpath.isabs`/`posixpath.isabs`. Those
    two disagree with `PureWindowsPath.is_absolute()` for a rooted-but-
    driveless Windows path (`ntpath.isabs(r"\\proj\\x")` is `True` --
    "absolute on the current drive" -- but `PureWindowsPath(r"\\proj\\x")
    .is_absolute()` is `False`, since pathlib requires BOTH a drive and a
    root); gating on the wrong one would call `.as_uri()` on a path it then
    raises `ValueError` on. Using `.is_absolute()` as both the gate and the
    thing `.as_uri()` itself checks keeps the two in lockstep by
    construction.

    * An ABSOLUTE `path` becomes an absolute `file:` URI --
      `file:///C:/w/proj/board.yaml` for the Windows-spelled root the issue
      opened on, `file:///w/proj/board.yaml` for a POSIX one -- which is what
      both consumers actually parse as a URI reference. Which oracle renders
      it is decided by [`_is_windows_spelled`], not `os.name`: `PureWindowsPath`
      for a Windows-spelled path, `PurePosixPath` otherwise. (Plain `Path`
      would use the CI HOST's rules and misjudge a Windows-spelled string on
      this repo's Linux CI -- the same trap PR #1089/#1090 already named.)
    * A RELATIVE, POSIX-spelled `path` (`resolve_board_path`'s own default,
      `"./board.yaml"`) is returned UNCHANGED. It is already a legal
      relative URI reference per RFC 3986 SS4.2 -- both SARIF 2.1.0's
      `artifactLocation.uri` and this repo's own `diagnostic-v1.schema.json`
      accept one -- and making it absolute would mean resolving it against
      the process's CWD, baking a host-specific absolute path into an
      otherwise portable document for no consumer-facing gain. It would also
      move the pinned separator-less golden (`test_validate_command.py`'s
      `"./board.yaml"` pins, and the `contract/envelopes/` fixtures
      tan-cli#1031 measured as an empty diff) for nothing.
    * A RELATIVE, WINDOWS-spelled `path` (`resolve_board_path("sub\\p",
      None)`'s shape, `"sub\\p\\board.yaml"`) has its backslashes swapped for
      forward slashes rather than left alone. A bare backslash is not a
      legal character in a URI reference at all (RFC 3986's `pchar`
      excludes it unencoded) -- leaving it in would still be invalid, just
      differently invalid, and Windows itself accepts `/` as an equally
      valid path separator, so the swap is meaning-preserving.
    """
    if _is_windows_spelled(path):
        win = PureWindowsPath(path)
        if win.is_absolute():
            return win.as_uri()
        return path.replace("\\", "/")
    posix = PurePosixPath(path)
    if posix.is_absolute():
        return posix.as_uri()
    return path
