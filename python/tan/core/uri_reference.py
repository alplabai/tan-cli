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
[`path_to_uri_reference`] in isolation and has no `boardYamlPath` of its own
to assert against).

## The relative case is left relative -- deliberately, and scoped narrowly

A relative `path` (`resolve_board_path`'s own default, `"./board.yaml"`) is
returned as a relative reference rather than absolutised. It is already a
legal relative URI reference per RFC 3986 SS4.2 -- SARIF 2.1.0's
`artifactLocation.uri` accepts one (`format: uri-reference`). alp-sdk's
`metadata/schemas/diagnostic-v1.schema.json` (NOT this repo's -- it lives in
alp-sdk, `git ls-files` here has zero hits for it) types the analogous field
as a bare `"type": "string"` and accepts a raw filesystem path too, so it
constrains nothing either way and is not cited as support for staying
relative. Absolutising `path` here would (a) move the pre-existing pinned
golden (`test_validate_command.py`'s `"./board.yaml"` pins) and (b) bake the
process's CWD into an otherwise portable document.

**This module used to make NO claim about how a relative reference gets
resolved -- tan-cli#1117 closes that gap.** An earlier version of this
docstring (tan-cli#1097 review round 1) tried the same thing by having
`validate_cmd._sarif_document` declare a SARIF `originalUriBaseIds`/
`uriBaseId` pair -- round 2 review found that addition declared a base that
did not actually resolve the reference in the default case (an anchoring
mismatch between `root` and the CWD `board_path` is actually relative to,
compounded by a missing trailing slash), and reached that wrong answer
through an unguarded `Path.resolve()` that could raise on a caller-supplied
`--project` containing a symlink loop, crashing the command it touched.
Declaring an authoritatively WRONG base is worse than declaring none --
round 1's undefined base at least let a spec-conformant consumer fall back to
its own CWD and succeed. That work was reverted rather than patched a third
time in place; tan-cli#1117 was filed with both measurements (the wrong-base
`urljoin` result and the `Path.resolve(strict=False)` symlink-loop crash) so
whoever picked it up next would not rediscover either.

[`cwd_base_uri`] is that pick-up: it anchors on `Path.cwd()` -- never
`root`, which is what produced the anchoring mismatch -- and never resolves
a caller-supplied path, so the symlink-loop crash class cannot recur here.
`validate_cmd._sarif_document` wires it into `originalUriBaseIds`, gated on
[`is_absolute_path_reference`] so an absolute `--board-yaml` reference (which
already resolves on its own) never carries a `uriBaseId` SARIF 2.1.0 SS3.4.4
says it must not.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote, quote_from_bytes

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
#:
#: The leading `^` here is REDUNDANT with `.match()` (`_is_windows_spelled`
#: below uses `.match()`, not `.search()`) -- `re.Pattern.match()` already
#: only tries at the string's own start regardless of `^`, so removing `^`
#: alone changes nothing for any input (round 1 review round 2 measured
#: this directly: un-anchoring the compiled pattern is behaviourally
#: indistinguishable from the original under `.match()` for every input
#: tried). A genuine misclassification of a mid-string colon (e.g.
#: `"sub/board:1.yaml"`) needs BOTH the anchor removed from the pattern AND
#: the call switched to `.search()` at once -- this property is doubly
#: defended, not fragile, and `test_a_colon_later_in_a_relative_path_does_
#: not_misclassify_it_as_windows` (this module's test file) states it
#: directly rather than chasing a single-line mutation that does not exist.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_windows_spelled(path: str) -> bool:
    """Whether `path` is WINDOWS-spelled, judged from the string itself
    rather than `os.name` (tan-cli#1089/#1090's own rule): a Windows-spelled
    path reaches [`path_to_uri_reference`] on EVERY host, not only on the
    `windows-latest` leg, so the oracle (`PureWindowsPath` vs
    `PurePosixPath`) must come from the path's own spelling and never from
    the running host's. Reading `os.name` would make the ubuntu and macos
    legs answer the POSIX question about a Windows path -- which is the same
    failure [`_absolute_path_to_file_uri`] below records for `Path.as_uri()`,
    and it does not disappear on a Windows runner, it MOVES.

    An earlier version of this docstring justified the rule with "this repo
    has no Windows host". That is false and is corrected repo-wide in
    tan-cli#1153: `parity.yml`'s `python-tests-shard` job runs this suite on
    `windows-latest` (its matrix `os:` carries all three platforms, with
    `runs-on: ${{ matrix.os }}`) on every `pull_request`. The rule above is
    right for its own reason, not for that one.

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


def _absolute_path_to_file_uri(path: PurePosixPath | PureWindowsPath) -> str:
    """`path` -- which MUST be absolute by its own oracle's rules -- rendered
    as an absolute `file:` URI, host-independently.

    This is `pathlib.PurePath.as_uri()`'s own body, verbatim, minus the
    `DeprecationWarning` 3.14 added to it. `PurePath.as_uri()` is deprecated
    and scheduled for REMOVAL in Python 3.19 (tan-cli#1140), and it was this
    module's exporter -- so the warning fired from the product module, not
    only from tests, and the removal would break `tan validate --format
    sarif`'s `artifactLocation.uri` outright. Carrying the body here retires
    the deprecated call while keeping the emitted bytes identical;
    `tests/core/test_uri_reference.py` pins that byte-identity against the
    stdlib itself, per input, on every supported interpreter.

    **The deprecation's own named replacement, `pathlib.Path.as_uri()`,
    cannot be used here**, and that is measured, not assumed: from 3.14 it
    delegates to `urllib.request.pathname2url(str(self), add_scheme=True)`,
    which dispatches on `os.name`, so the conversion it performs is the
    RUNNING HOST's, not the one the path is spelled in. The damage does not
    disappear on a Windows runner, it MOVES. Measured on 3.14.7 from a POSIX
    host, all nine Windows-spelled shapes in the test corpus come out wrong
    (`C:\\w\\proj\\board.yaml` -> `"file:C%3A%5Cw%5Cproj%5Cboard.yaml"`
    rather than `"file:///C:/w/proj/board.yaml"`) while every POSIX-spelled
    one survives; under a simulated `os.name = "nt"` that inverts, and one of
    the POSIX shapes it then breaks is `/tmp/proj/we\\ird.yaml` ->
    `"file:///tmp/proj/we/ird.yaml"` -- silently naming a DIFFERENT file,
    which is round 1 MAJOR 1, the regression [`_is_windows_spelled`] exists
    to prevent. Below 3.14 there is no divergence on either host, and that is
    what would have made the swap dangerous. The full four-cell account, and
    the assertion that keeps it honest, are on the test file's
    `test_pathlib_path_as_uri_is_not_a_usable_replacement_for_this_exporter`.

    The deeper reason is structural: `Path` is CONCRETE and host-bound -- a
    `PureWindowsPath` cannot become a `WindowsPath` on a POSIX host at all --
    while [`_is_windows_spelled`] deliberately picks its oracle from the path
    STRING, so BOTH branches are exercised on every runner rather than only
    the one matching that runner's `os.name`. Routing the export through
    `Path` hands that choice back to the host, which is the defect class
    tan-cli#1105 and PR #1125 were bitten by.

    `os.fsencode` below is the one host-sensitive call, and it is inherited
    verbatim from the stdlib body rather than introduced: it resolves to
    utf-8/`surrogateescape` on POSIX and utf-8/`surrogatepass` on Windows,
    which differ only for a `str` carrying lone surrogates. Keeping it is
    what makes this byte-identical to the call it replaces; it is not a
    branch on `os.name`, and [`_is_windows_spelled`]'s "never the host's
    rules" premise is untouched -- nothing below consults the host to decide
    which SPELLING to emit.

    The `.is_absolute()` guard is kept, and kept raising, for the reason
    [`path_to_uri_reference`]'s docstring gives: the exporter must enforce
    the very predicate its caller gates on, so gate and exporter cannot
    drift apart. Dropping it would let a relative path through as a
    malformed `file:` URI instead of the `ValueError` the caller's own
    branch is written against."""
    if not path.is_absolute():
        raise ValueError("relative path can't be expressed as a file URI")

    drive = path.drive
    if len(drive) == 2 and drive[1] == ":":
        # A path on a local drive => `file:///c:/a/b`.
        prefix = "file:///" + drive
        rest = path.as_posix()[2:]
    elif drive:
        # A path on a network drive => `file://host/share/a/b`.
        prefix = "file:"
        rest = path.as_posix()
    else:
        # A POSIX path => `file:///etc/hosts`.
        prefix = "file://"
        rest = str(path)
    return prefix + quote_from_bytes(os.fsencode(rest))


def path_to_uri_reference(path: str) -> str:
    """`path` rendered as a valid URI reference (RFC 3986).

    [`_absolute_path_to_file_uri`] is the exporter for the absolute case,
    and it carries the gate too: `.is_absolute()`, NOT `ntpath.isabs`/
    `posixpath.isabs`. The load-bearing reason is that `.is_absolute()` IS
    the predicate the exporter itself enforces, so the gate and the exporter
    cannot disagree -- on any interpreter, now or later. Gating on a
    DIFFERENT notion of "absolute" would call the exporter on a path it then
    raises `ValueError` on. (That exporter used to be `PurePath.as_uri()`
    called directly here; 3.14 deprecates it for removal in 3.19, so
    tan-cli#1140 carried its body into this module byte-for-byte, guard
    included, rather than let a removal take out `tan validate --format
    sarif`'s `artifactLocation.uri`. See that function's own docstring for
    why the deprecation's named replacement, `Path.as_uri()`, is not usable
    here.)

    A weaker reason used to stand here, and it has since expired
    (tan-cli#1126). It said the two oracles simply disagree for a
    rooted-but-driveless Windows path -- `ntpath.isabs(r"\\proj\\x")` is
    `True` ("absolute on the current drive") against
    `PureWindowsPath(r"\\proj\\x").is_absolute()`'s `False` (pathlib
    requires BOTH a drive and a root) -- stated as a fixed stdlib fact.
    It is not one. Measured on 3.12.3 / 3.13.15 / 3.14.7: `ntpath.isabs`
    answers that shape `True` on 3.12 and `False` from **3.13** on, where
    CPython made it agree with pathlib; `.is_absolute()` is `False` on all
    three, unchanged. So on every supported interpreter but the `>=3.12`
    floor itself the two now AGREE -- which changes nothing about this
    gate, precisely because the choice never rested on them differing.
    Two tests in `tests/core/test_uri_reference.py` had encoded the 3.12
    answer as universal and failed the full suite on 3.14.7 for it; the
    version-dependent fact now lives in one measured constant there
    (`_NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT`), asserted on both sides of
    the boundary rather than restated in prose here.

    * An ABSOLUTE `path` becomes an absolute `file:` URI --
      `file:///C:/w/proj/board.yaml` for the Windows-spelled root the issue
      opened on, `file:///w/proj/board.yaml` for a POSIX one -- which is what
      both consumers actually parse as a URI reference. Which oracle renders
      it is decided by [`_is_windows_spelled`], not `os.name`: `PureWindowsPath`
      for a Windows-spelled path, `PurePosixPath` otherwise. (Plain `Path`
      would use the CI HOST's rules and misjudge a Windows-spelled string on
      this repo's Linux CI -- the same trap PR #1089/#1090 already named.)
      [`_absolute_path_to_file_uri`] itself percent-encodes non-ASCII and
      reserved bytes (a space, a literal backslash inside a POSIX filename,
      `#`, non-ASCII -- all measured), so nothing further is needed on this
      branch.
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
    [`_absolute_path_to_file_uri`] unexamined (`file://%3F/C%3A/...`, the
    drive demoted into the path) rather than being rejected or normalised;
    and drive-letter casing is preserved verbatim from the input rather than
    canonicalised, so `"c:/w/x"` and `"C:\\w\\x"` for the same file produce
    differently-cased URIs. Neither is exercised by anything
    `resolve_board_path` itself produces.
    """
    if _is_windows_spelled(path):
        win = PureWindowsPath(path)
        if win.is_absolute():
            return _absolute_path_to_file_uri(win)
        return quote(path.replace("\\", "/"), safe="/")
    posix = PurePosixPath(path)
    if posix.is_absolute():
        return _absolute_path_to_file_uri(posix)
    return quote(path, safe="/")


def is_absolute_path_reference(path: str) -> bool:
    """Whether [`path_to_uri_reference`] renders `path` as an ABSOLUTE
    `file:` URI -- the gate `_sarif_document` (tan-cli#1117) uses to decide
    whether a location may carry SARIF's `uriBaseId` at all: 2.1.0 SS3.4.4
    -- "If the uri property is an absolute URI, the uriBaseId property SHALL
    be absent."

    Delegates to [`path_to_uri_reference`] itself rather than RE-DERIVING
    the same `_is_windows_spelled` + `.is_absolute()` branch a second time:
    a `file:` scheme in that function's OUTPUT is produced by exactly its
    two [`_absolute_path_to_file_uri`] calls, both already gated on the
    identical `is_absolute()` check a second copy here would only
    duplicate -- drifting the two apart the moment [`path_to_uri_reference`]
    grows a branch this one does not learn about is exactly the
    per-command-copy class of defect `tan.core.board_context`'s own module
    docstring names for a resolver retyped instead of shared.

    A first version of this docstring worried the sniff was unsound for a
    driveless Windows path (`"C:board.yaml"`) on the theory that its
    rendered form is "quoted unchanged" and therefore a syntactically valid
    URI with scheme `C`. Measured, that is false: `quote(..., safe="/")`
    does not treat `:` as safe, so `path_to_uri_reference("C:board.yaml")`
    is actually `"C%3Aboard.yaml"` -- the colon is percent-encoded, and
    `.startswith("file:")` on it is `False`, correctly. The SAME encoding
    rule is what makes the sniff safe in general: a `file:` PREFIX can only
    ever appear in the output through the two absolute-only
    [`_absolute_path_to_file_uri`] branches -- any RELATIVE reference
    containing a literal `"file:"` substring has its colon percent-encoded
    before this check ever sees it
    (`path_to_uri_reference("file:sub/board.yaml") == "file%3Asub/board
    .yaml"`, measured), so it can never masquerade as one."""
    return path_to_uri_reference(path).startswith("file:")


def cwd_base_uri() -> str:
    """The SARIF `originalUriBaseIds` base for a RELATIVE `artifactLocation
    .uri` (tan-cli#1117): the process's own working directory, as an
    absolute `file:` URI with a TRAILING SLASH. **Can raise** -- see the
    last section below; [`cwd_base_uri_or_none`] is the guarded call site
    should actually use.

    **Anchored on `Path.cwd()`, never on `--project`/`root`.**
    `resolve_board_path` already prepends `root` onto the relative reference
    itself (`--project sub` renders `"sub/board.yaml"`, not `"board.yaml"`),
    so the reference is CWD-relative by construction. A base ALSO anchored on
    `root` would apply that prefix a SECOND time -- `urljoin("file://.../sub/
    ", "sub/board.yaml")` names `.../sub/sub/board.yaml`, which does not
    exist -- reproducing the exact tan-cli#1097-round-2 defect this issue was
    filed to fix (measured there: `root="."`'s base resolved `./board.yaml`
    onto the WRONG file because the base and the reference were anchored to
    two different roots). `tests/core/test_uri_reference.py`'s mutation for
    this line swaps `Path.cwd()` for a `root`-derived path and reds on the
    `--project sub` case for exactly that reason.

    **The trailing slash is not decorative, and not unconditional.** RFC 3986
    SS5.3's reference-resolution algorithm drops the base's own LAST PATH
    SEGMENT before appending a relative reference: `urljoin("file:///a/b",
    "c")` names `"file:///a/c"`, not `"file:///a/b/c"` -- a slash-less base
    names a RESOURCE, indistinguishable from a file, one level above the
    directory it reads as. Dropping the slash entirely reproduces the same
    wrong-file mismatch tan-cli#1117 measured (`EXISTS? False` against the
    real, differently-pathed file). But `Path.as_uri()` DOES already append
    one at a filesystem root -- measured, `Path("/").as_uri() ==
    "file:///"` -- so appending UNCONDITIONALLY doubles it there
    (`"file:////"`, and `PureWindowsPath("C:/").as_uri()` -> `"file:///C://"`
    the same way). SARIF 2.1.0 SS3.14.14 requires the value "SHALL end with a
    single forward slash", NOTE 2 explaining that resolution works "by
    simple concatenation" -- a doubled slash breaks exactly that
    concatenation (`"file:////" + "./board.yaml"` is not the intended path,
    even though `urljoin` itself tolerates it and recovers). Appending only
    when not already slash-terminated keeps both properties -- always ends
    in exactly one slash, never two.

    **`Path.cwd()` CAN raise, even though it touches no caller-supplied
    data.** `Path.cwd()` -> `os.getcwd()` raises `FileNotFoundError` (an
    `OSError` subclass) when the process's OWN working directory has been
    removed out from under it -- measured directly. This function does not
    guard it: [`cwd_base_uri_or_none`] is the call site `validate_cmd.py`
    actually uses, and it exists SPECIFICALLY because tan-cli#1117 review
    round 2 measured that an unguarded call here reached `_sarif_document`
    from INSIDE `validate_cmd.py`'s own `except Exception as err:` handler
    and double-faulted the same way the reverted round-1 attempt's
    `Path.resolve(strict=False)` symlink-loop crash did -- a `#1116`
    never-raises instance reached through a filesystem call that happens not
    to be caller-supplied, rather than through caller data itself. (For the
    record, since it is the OTHER crash surface tan-cli#1117 was filed over:
    `.resolve()`'s own symlink-loop behaviour is not uniform across pathlib
    versions either -- measured directly, `python-build-standalone`
    3.12.3/3.13.15/3.14.7: on 3.12, `Path("loop").resolve(strict=False)`
    raises `RuntimeError` regardless of `strict=False`'s own promise not to;
    on 3.13/3.14 that promise IS kept for `strict=False` (returns the path
    unresolved, no raise), but `strict=True` still raises `OSError [Errno
    40]` on both. This function calls neither `.resolve()` nor anything on a
    caller-supplied path at all, so that particular crash class cannot recur
    here regardless.)"""
    base = Path.cwd().as_uri()
    return base if base.endswith("/") else base + "/"


def cwd_base_uri_or_none() -> str | None:
    """[`cwd_base_uri`], guarded against the one way it can raise: the
    process's own working directory has been removed. `validate_cmd.py`'s
    `validate()` calls this ONCE, before any guard in its own body can fire,
    and threads the result through `_emit`/`_sarif_document` as a plain
    value -- so nothing downstream of this call touches the filesystem for
    the SARIF base again, and nothing there can raise from a missing CWD no
    matter which branch of `validate()` eventually reaches the SARIF
    exporter (tan-cli#1117 review round 2's blocker).

    `None` means exactly what `dev` emitted before tan-cli#1117: a relative
    `uri` with no declared base -- the safe fallback, not a degraded one;
    see `_sarif_document`'s own docstring for how it is threaded through."""
    try:
        return cwd_base_uri()
    except OSError:
        return None
