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
on purpose -- `tests/commands/test_validate_command.py`'s
`test_absolute_board_yaml_uri_is_a_file_uri_but_boardyamlpath_stays_host_
native` asserts that split so a future "tidy this up" sweep does not unify
the two contracts (NOT this module's own test file -- that file proves
[`path_to_uri_reference`]/[`root_to_base_uri`] in isolation and has no
`boardYamlPath` of its own to assert against).

## The relative case is NOT one answer for both consumers (review round 1)

A relative `path` (`resolve_board_path`'s own default, `"./board.yaml"`) is
returned as a relative reference rather than absolutised -- but what that
buys each consumer differs, and round 1 of this PR's review found the first
version of this module claimed one uniform justification ("no consumer-
facing gain") that was true for only one of them:

* **SARIF** -- a relative `artifactLocation.uri` is permitted and schema-
  valid (`format: uri-reference`), and `validate_cmd._sarif_document` now
  declares `runs[].originalUriBaseIds` / `results[].locations[].
  physicalLocation.artifactLocation.uriBaseId` (`%SRCROOT%`, via
  [`root_to_base_uri`]) so the relative reference has a defined base rather
  than being left to whatever a given consumer's CWD happens to be. This is
  the "zero-cost" fix: it moves no pinned golden (the `uri` value itself is
  untouched) and turns the comment beside it from an unverified claim into
  one the emitted document actually backs.
* **LSP** -- `DocumentUri` is a URI WITH A SCHEME by the LSP spec, and there
  is no LSP analogue of `uriBaseId`: a client that receives `"./board.yaml"`
  cannot resolve it against anything this document declares, so it will not
  match an open buffer's own absolute URI. Absolutising it here would fix
  that but would also (a) move the pre-existing pinned golden
  (`test_validate_command.py`'s `"./board.yaml"` pins, present since before
  this module existed) and (b) bake the process's CWD into the diagnostic.
  Neither this module nor `validate_cmd._issue_to_diagnostic` claims the LSP
  relative case is fully resolved -- it is a known, documented limitation:
  an LSP client consuming `tan validate --format diagnostic-v1` is expected
  to resolve a relative `uri` against ITS OWN notion of the workspace root
  (normally the same directory `tan validate` was invoked from), not treated
  as already resolvable in isolation.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

#: A Windows drive letter at the string's own start (`C:...`, no backslash
#: required yet) -- `resolve_board_path` never emits this bare, but
#: `--board-yaml` is caller-supplied and this oracle should not miss it.
#: Review round 1 measured this constant survives a mutation that deletes
#: it (`_is_windows_spelled` collapsing to `"\\" in path` alone): its only
#: DISTINCT input is a forward-slash-spelled drive path (`"c:/w/proj/
#: board.yaml"` -- a colon with no backslash anywhere), which the mutant
#: renders unchanged, i.e. with a first-segment colon a URI-reference
#: consumer parses as a scheme. `test_a_forward_slash_spelled_drive_path_
#: is_still_judged_windows` pins that input; see this module's own test
#: file for the per-branch (not whole-function) mutation that catches it.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_windows_spelled(path: str) -> bool:
    """Whether `path` is WINDOWS-spelled, judged from the string itself
    rather than `os.name` -- this repo has no Windows host (tan-cli#1089/
    #1090's own rule), so [`path_to_uri_reference`] must pick its oracle
    (`PureWindowsPath` vs `PurePosixPath`) from the path's own spelling,
    never the CI host's.

    A LEADING `/` is decided POSIX before any later backslash is even
    consulted (review round 1 MAJOR 1, a real regression this fixed): a
    backslash is a perfectly legal character inside a POSIX filename, e.g.
    `/tmp/proj/we\\ird.yaml`. The pre-fix version of this function read
    `"\\" in path` unconditionally and misrouted that exact path to the
    Windows branch, which does not consider it absolute (`PureWindowsPath(
    "/tmp/proj/we\\ird.yaml").is_absolute()` is `False` -- pathlib requires
    a drive too), so it fell to the RELATIVE Windows arm and had its
    backslash swapped for a slash: `/tmp/proj/we/ird.yaml`, silently naming
    a DIFFERENT file than the one on disk, and losing its `file:` scheme
    into the bargain. A leading `/` never appears at the start of a
    Windows-spelled string this repo's own resolver produces (a Windows
    drive path starts with a letter, a UNC path starts with `\\\\`), so this
    is a safe, targeted disambiguator, not a heuristic that trades one
    misclassification for another."""
    if path.startswith("/"):
        return False
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
      `as_uri()` itself percent-encodes non-ASCII and reserved bytes (a
      space, a literal backslash inside a POSIX filename, `#`, non-ASCII --
      all measured), so nothing further is needed on this branch.
    * A RELATIVE, POSIX-spelled `path` (`resolve_board_path`'s own default,
      `"./board.yaml"`) is returned percent-encoded but otherwise UNCHANGED
      -- see this module's docstring's "relative case" section for why it
      stays relative instead of being absolutised. `urllib.parse.quote`
      (`safe="/"`, so the separator itself is untouched) closes the
      remaining gap round 1 measured: a raw space or `#` in a relative
      `path` is not a legal URI-reference character/is a fragment
      delimiter, and the separator-less golden (`"./board.yaml"`) contains
      neither, so quoting it is a no-op and the pin is untouched.
    * A RELATIVE, WINDOWS-spelled `path` (`resolve_board_path("sub\\p",
      None)`'s shape, `"sub\\p\\board.yaml"`) has its backslashes swapped for
      forward slashes, then the same percent-encoding applied. A bare
      backslash is not a legal character in a URI reference at all (RFC
      3986's `pchar` excludes it unencoded) -- leaving it in would still be
      invalid, just differently invalid, and Windows itself accepts `/` as
      an equally valid path separator, so the swap is meaning-preserving.

    Known, deliberately undefended edge cases (round 1 nits, low
    reachability from this repo's own resolver): a `\\\\?\\`-prefixed
    Windows extended-length path renders its `\\\\?\\` host segment through
    `PureWindowsPath.as_uri()` unexamined (`file://%3F/C%3A/...`, the drive
    demoted into the path) rather than being rejected or normalised; and
    drive-letter casing is preserved verbatim from the input rather than
    canonicalised, so `"c:/w/x"` and `"C:\\w\\x"` for the same file produce
    differently-cased URIs. Neither is exercised by anything
    `resolve_board_path` itself produces.
    """
    if _is_windows_spelled(path):
        win = PureWindowsPath(path)
        if win.is_absolute():
            return win.as_uri()
        return quote(path.replace("\\", "/"), safe="/")
    posix = PurePosixPath(path)
    if posix.is_absolute():
        return posix.as_uri()
    return quote(path, safe="/")


def root_to_base_uri(root: str) -> str:
    """An absolute `file:` URI for `root`, THIS PROCESS'S OWN notion of it
    -- used only for SARIF's `originalUriBaseIds` (review round 1 MAJOR 3),
    never for [`path_to_uri_reference`]'s job.

    The two functions are not interchangeable and must not be merged:
    [`path_to_uri_reference`] renders a CALLER-SUPPLIED string that might be
    Windows-spelled on a POSIX host (`--board-yaml` is caller data), so it
    has to pick its oracle from the STRING alone and never touch the
    filesystem. `root_to_base_uri` answers a different question -- "where is
    this running process, really" -- which has exactly one honest answer on
    whatever host it is asked on, so it uses the REAL, concrete `pathlib.
    Path` (not a `Pure*Path` string oracle) and lets `Path.resolve()` walk
    the real filesystem (`strict=False` by default -- it does not require
    `root` to exist)."""
    return Path(root).resolve().as_uri()
