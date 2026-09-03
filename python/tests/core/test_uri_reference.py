# SPDX-License-Identifier: Apache-2.0
"""`tan.core.uri_reference` (tan-cli#1097).

This repo has NO Windows host (PR #1089/#1090's own rule, restated in
`test_board_context.py`'s header): a Windows-spelled path exercises the
Windows branch on Linux/mac CI identically, because
[`path_to_uri_reference`] picks its oracle (`PureWindowsPath` vs
`PurePosixPath`) from the STRING itself, never from `os.name`. `ntpath.isabs`
and `PureWindowsPath(...).as_uri()`/`.is_absolute()` are all pure string
operations -- no filesystem touch -- so they are real oracles here, not a
simulation.

## Two DELIBERATE uses of removal-scheduled stdlib APIs live in this file

Both are here because this repo has no Windows host, and both are documented
where they are used rather than silenced (tan-cli#1140): `PurePath.as_uri()`
as the byte-identity ORACLE in
[`test_the_exporter_still_emits_exactly_what_purepath_as_uri_emitted`] and
[`test_the_exporter_refuses_a_relative_path_the_way_purepath_as_uri_did`],
and `nturl2path` in
[`test_a_windows_file_uri_path_component_round_trips_through_nturl2path`].
CPython 3.14 deprecates both for removal in 3.19 and the
`python · pytest on the newest CPython` job REPORTS both, by design --
that job exists to surface exactly this class, so no `filterwarnings` hides
either one. The product module carries neither any more: tan-cli#1140 moved
`tan/core/uri_reference.py`'s exporter off `PurePath.as_uri()` into
[`_absolute_path_to_file_uri`], because that call sits on the envelope
surface (`tan validate --format sarif`'s `artifactLocation.uri`) and its
removal would break it. What is left here is test-side oracles, each with
its own docstring saying what a replacement would have to do.

`data.boardYamlPath` staying host-native (unchanged by this module) is
asserted in `tests/commands/test_validate_command.py`'s
`test_absolute_board_yaml_uri_is_a_file_uri_but_boardyamlpath_stays_host_
native`, NOT here -- this file proves [`path_to_uri_reference`] in isolation
and has no envelope of its own to assert `boardYamlPath` against.

## Review round 1 -- what each test class below closes

Round 1 found the first version of this module had a real regression (an
absolute POSIX path containing a backslash was silently renamed) and a live,
untested branch (the whole-function mutation used for the mutation-proof
reverted too much to exercise `_WINDOWS_DRIVE_RE` on its own). The tests
under "the classifier itself" section below mutate ONE branch/constant at a
time for exactly that reason -- see each test's own docstring for the exact
`git diff`-shaped mutation it is built to catch, and this module's own
docstring for the narrative.

Round 1's *second* review pass found one more control that could not fail:
un-anchoring `_WINDOWS_DRIVE_RE` (`^[A-Za-z]:` -> `[A-Za-z]:`) left every
test here green. The regex uses `.match()`, which already anchors at the
string's own start regardless of `^` -- so the `^` is genuinely redundant,
and no input distinguishes its removal under `.match()`. The load-bearing
property is calling `.match()` rather than `.search()`; see
`test_a_colon_later_in_a_relative_path_does_not_misclassify_it_as_windows`
below, which mutates that call instead.

A review round 1 attempt at SARIF's `originalUriBaseIds` was added here, then
reverted in round 2 after the base it declared turned out not to resolve the
reference it was attached to (see `tan.core.uri_reference`'s own module
docstring, and `validate_cmd._sarif_document`'s, for the account). tan-cli#1117
reintroduces it as [`cwd_base_uri`] and its `uriBaseId` gate,
[`is_absolute_path_reference`] -- the two sections at the bottom of this file
cover them, each mutating ONE branch at a time the same way the rest of this
file already does.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import pytest

from tan.core.uri_reference import (
    _WINDOWS_DRIVE_RE,
    _absolute_path_to_file_uri,
    _is_windows_spelled,
    cwd_base_uri,
    cwd_base_uri_or_none,
    is_absolute_path_reference,
    path_to_uri_reference,
)

#: Whether THIS interpreter's `ntpath.isabs` calls a ROOTED-BUT-DRIVELESS
#: Windows path (`"\proj\x"`, `"/w/proj/x"` -- a root with no drive letter in
#: front of it) absolute.
#:
#: tan-cli#1126: this is not a fixed stdlib fact, it is an interpreter-VERSION
#: one, and the two tests below used to assert the pre-3.13 answer as though it
#: were universal. They were the whole of what the full suite failed on 3.14.7
#: while staying green on 3.12.3 -- invisible until someone ran it, because
#: `parity.yml`'s seam1 was the only job resolving a floating `3.x` and it runs
#: `tests/gates` only. See that file's `python-version` block and
#: `tests/gates/test_interpreter_policy.py` for the policy that now REPORTS
#: this class of divergence instead of waiting to be tripped over.
#:
#: Measured directly on real python-build-standalone builds of each, on this
#: POSIX box -- `ntpath` is pure string logic with no filesystem or `os.name`
#: input, so the host is irrelevant to the answer (the same property that makes
#: this whole module testable without a Windows runner):
#:
#:     3.12.3     ntpath.isabs(r"\proj\x") True    .is_absolute() False
#:     3.13.15    ntpath.isabs(r"\proj\x") False   .is_absolute() False
#:     3.14.7     ntpath.isabs(r"\proj\x") False   .is_absolute() False
#:
#: The boundary is **3.13**, not 3.14 -- CPython made `ntpath.isabs` agree with
#: `PureWindowsPath.is_absolute()` there, and 3.14.7 is merely where this repo
#: happened to notice. Getting that release wrong is the same off-by-one the
#: `_IGNORED_ERRNOS` note in tan-cli#1121 needed a measurement to correct (the
#: constant vanished in 3.13, the raising behaviour survived to 3.13.15 and
#: changed in 3.14); the two facts are neighbours and neither is guessable from
#: the other.
#:
#: Deliberately NOT a `pytest.mark.skipif` and not a `>=`-shaped tolerance:
#: every site below asserts `is` this value, so an interpreter that flips the
#: answer BACK still reds here rather than quietly skipping. Only the version
#: test may vary; the assertion never stops running.
_NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT = sys.version_info < (3, 13)

#: Whether THIS interpreter's `pathlib.Path.as_uri()` still renders a
#: Windows-spelled path the way `PurePath.as_uri()` does when it is handed a
#: `PureWindowsPath` from a POSIX host -- i.e. whether the deprecation's own
#: named replacement would have been a drop-in for the exporter tan-cli#1140
#: moved into [`_absolute_path_to_file_uri`].
#:
#: Measured directly on real python-build-standalone builds of each, on this
#: POSIX box, for `PureWindowsPath(r"C:\w\proj\board.yaml")`:
#:
#:     3.12.3     Path has NO `as_uri` of its own; it INHERITS the deprecated
#:                `PurePath.as_uri`      -> "file:///C:/w/proj/board.yaml"
#:     3.13.15    `Path.as_uri` is a verbatim COPY of `PurePath.as_uri`
#:                                       -> "file:///C:/w/proj/board.yaml"
#:     3.14.7     `Path.as_uri` delegates to `urllib.request.pathname2url(
#:                str(self), add_scheme=True)`, which dispatches on `os.name`
#:                                       -> "file:C%3A%5Cw%5Cproj%5Cboard.yaml"
#:
#: The boundary is **3.14**. Below it the swap is invisible; at and above it
#: the swap silently destroys every Windows-spelled URI this module emits, on
#: a POSIX host, and takes the mirror-image damage on the `windows-latest`
#: pytest shard for the POSIX-spelled branch. That is why #1140 carried
#: `PurePath.as_uri()`'s BODY across instead of taking the replacement the
#: deprecation message names.
#:
#: Deliberately NOT a `skipif`: the site below asserts `is` this value, so an
#: interpreter that changes the answer in EITHER direction reds here rather
#: than quietly skipping.
_PATH_AS_URI_STILL_MATCHES_PUREPATH_CROSS_SPELLING = sys.version_info < (3, 14)

# ---------------------------------------------------------------------------
# The classifier itself, `_is_windows_spelled` -- one branch/constant at a
# time (round 1 MAJOR 1 and MAJOR 2)
# ---------------------------------------------------------------------------


def test_a_leading_slash_is_posix_even_with_a_later_backslash():
    """Round 1 MAJOR 1, the regression, reduced to the classifier alone.
    `we\\ird.yaml` is a legal POSIX filename (backslash is not special to a
    POSIX filesystem); a leading `/` must settle POSIX-ness before the `"\\"
    in path` check even runs. Mutating this test's own anchor -- deleting
    the `if path.startswith("/"): return False` early-return in
    `_is_windows_spelled` -- reds this exact assertion (verified as part of
    this PR's mutation-proof; see the PR description for the exact output)."""
    assert not _is_windows_spelled("/tmp/proj/we\\ird.yaml")


def test_a_forward_slash_spelled_drive_path_is_still_judged_windows():
    """Round 1 MAJOR 2. `_WINDOWS_DRIVE_RE` is the classifier's ONLY signal
    for a drive letter with no backslash anywhere in the string
    (`"c:/w/proj/board.yaml"`) -- deleting it (mutating `_is_windows_spelled`
    to `return "\\" in path` alone, the exact mutation round 1 measured)
    reds this assertion where the whole-function mutation used for the
    module-level mutation-proof could not reach it."""
    input_path = "c:/w/proj/board.yaml"
    assert "\\" not in input_path, "the drive-letter regex must be the ONLY signal firing here"
    assert _is_windows_spelled(input_path)


def test_a_colon_later_in_a_relative_path_does_not_misclassify_it_as_windows():
    """Round 1's SECOND review pass: the earlier version of this test used
    an ABSOLUTE (leading-`/`) input, so it was actually exercising MAJOR 1's
    guard rather than the drive-letter regex it claimed to pin -- and, it
    turns out, no INPUT could have exercised the regex's own anchoring in
    isolation anyway: `_WINDOWS_DRIVE_RE.match()` already anchors at the
    string's own start regardless of whether the pattern itself carries `^`
    (`.match()` is inherently start-anchored; verified directly, every input
    tried agrees between `re.compile(r"^[A-Za-z]:")` and
    `re.compile(r"[A-Za-z]:")` under `.match()`). This property is therefore
    DOUBLY defended -- both by `.match()`'s own anchor and by the pattern's
    redundant `^` -- and a genuine misclassification of a mid-string colon
    needs BOTH the anchor removed from the pattern AND the call switched to
    `.search()` at once (see [`test_the_unanchored_search_combination_
    would_misclassify_it`] just below for what that combination looks
    like, kept as documentation rather than a single-mutation claim). This
    test states the requirement directly -- "a colon later in a relative
    path is not Windows-spelled" -- rather than chasing a mutation that
    does not exist as a single line."""
    assert not _is_windows_spelled("sub/board:1.yaml")


def test_the_unanchored_search_combination_would_misclassify_it():
    """NOT a mutation-proof of the shipped code -- `_is_windows_spelled`
    uses `.match()` with an (redundantly) anchored pattern, and neither
    alone produces a false positive on this input (see the test above).
    This documents what the FAILURE would look like if a future edit
    weakened both at once: an unanchored copy of the same pattern, searched
    rather than matched, finds "d:" inside "board:1.yaml" -- a letter
    immediately followed by a colon, nowhere near the string's start."""
    unanchored = re.compile(_WINDOWS_DRIVE_RE.pattern.removeprefix("^"))
    assert unanchored.search("sub/board:1.yaml") is not None


# ---------------------------------------------------------------------------
# Absolute paths become absolute `file:` URIs
# ---------------------------------------------------------------------------


def test_an_absolute_posix_path_becomes_a_file_uri():
    uri = path_to_uri_reference("/w/proj/board.yaml")
    assert uri == "file:///w/proj/board.yaml"


def test_an_absolute_posix_path_containing_a_backslash_keeps_its_own_name():
    """Round 1 MAJOR 1's own reduction, end to end through
    [`path_to_uri_reference`] rather than the classifier alone: measured
    through the real CLI as `--board-yaml '/tmp/.../we\\ird.yaml'` rendering
    `uri` as `.../we/ird.yaml` -- a DIFFERENT, nonexistent file, with no
    `file:` scheme either (it fell to the relative Windows slash-swap
    branch). Fixed: this is an ABSOLUTE POSIX path, and stays one -- percent-
    encoded (`%5C`), not rewritten, not scheme-less."""
    path = "/tmp/proj/we\\ird.yaml"
    uri = path_to_uri_reference(path)
    assert uri == "file:///tmp/proj/we%5Cird.yaml"
    assert uri.startswith("file:///")
    assert "we/ird" not in uri, "must not silently rename the file"


def test_an_absolute_windows_drive_path_becomes_a_file_uri():
    """The issue's own reduction: `C:\\w\\proj\\board.yaml` must become a
    scheme-carrying, forward-slashed, percent-encoded `file:` URI, not a
    bare path with the backslashes swapped."""
    uri = path_to_uri_reference(r"C:\w\proj\board.yaml")
    assert uri == "file:///C:/w/proj/board.yaml"
    assert uri.startswith("file:///")
    # The two things the issue calls invalid in a URI reference at that
    # position: no bare backslash, and the drive letter is followed by `/`
    # under the `file:///C:/...` authority-less form, not a bare `:`.
    assert "\\" not in uri
    assert "C:/" in uri


def test_a_forward_slash_spelled_absolute_drive_path_also_becomes_a_file_uri():
    """The MAJOR 2 input carried all the way to an absolute result --
    `"c:/w/proj/board.yaml"` is Windows-spelled (drive letter, no backslash
    anywhere) AND absolute, so it must reach the SAME `file:` URI a
    backslash-spelled equivalent does, not the relative slash-swap branch."""
    uri = path_to_uri_reference("c:/w/proj/board.yaml")
    assert uri == "file:///c:/w/proj/board.yaml"


def test_a_pre_1090_mixed_separator_windows_path_still_becomes_a_file_uri():
    """Pre-#1090 the field could render the MIXED `C:\\w\\proj/board.yaml`
    (backslash root, forward-slash join) -- also invalid, for the same
    reason. `ntpath.isabs`/`PureWindowsPath` treat a mixed-separator string
    the same as a pure one, so the fix covers this shape too without a
    special case."""
    mixed = "C:\\w\\proj/board.yaml"
    uri = path_to_uri_reference(mixed)
    assert uri == "file:///C:/w/proj/board.yaml"
    assert uri.startswith("file:///")


def test_a_windows_unc_path_becomes_a_file_uri():
    uri = path_to_uri_reference(r"\\server\share\board.yaml")
    assert uri == "file://server/share/board.yaml"
    assert ntpath.isabs(r"\\server\share\board.yaml")


def test_a_forward_slash_only_absolute_root_is_judged_posix_not_windows():
    """No backslash and no drive letter -- `_is_windows_spelled` must read
    this as POSIX. This also documents WHY the gate is `.is_absolute()` and
    not `ntpath.isabs`: `PureWindowsPath("/w/proj/board.yaml")
    .is_absolute()` is `False` on every supported interpreter (pathlib
    requires a drive too), and calling [`_absolute_path_to_file_uri`] after
    trusting a predicate that says otherwise raises.

    tan-cli#1126 corrected the `ntpath.isabs` half of that sentence, which
    used to read "is `True`" flat: it is `True` only below 3.13 (see
    [`_NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT`] for the measurement), and
    asserting the 3.12 answer as universal is what failed this test on
    3.14.7. The `.is_absolute()` line below is the one that has never
    moved, which is the actual argument for gating on it."""
    uri = path_to_uri_reference("/w/proj/board.yaml")
    assert uri == "file:///w/proj/board.yaml"
    assert ntpath.isabs("/w/proj/board.yaml") is _NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT
    assert not PureWindowsPath("/w/proj/board.yaml").is_absolute()


def test_a_windows_rooted_but_driveless_path_does_not_raise_and_stays_slash_swapped():
    """`r"\\proj\\x"` is rooted with no drive to resolve that root against,
    and the exporter refuses it (`is_absolute()` requires drive AND root --
    `PurePath.as_uri()` did, and [`_absolute_path_to_file_uri`] kept the
    guard when it took that body over). Gating on `.is_absolute()` routes this to the
    backslash-swap branch instead of crashing; the result is a legal
    relative-ref (RFC 3986's `path-absolute` form: leading `/`, no scheme),
    not a claim about which drive it is rooted on.

    tan-cli#1126: this docstring used to open by stating that
    `ntpath.isabs` answers it `True` -- "absolute on the current drive" --
    as a flat fact, and the test asserted the same. Both were the 3.12
    answer only; from 3.13 `ntpath.isabs` agrees with pathlib and says
    `False` ([`_NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT`]). What this test
    actually pins -- the rendered output on the last line -- is identical
    on every measured interpreter, which is the point: this module's ANSWER
    never depended on the interpreter, only this piece of documentation
    did."""
    path = "\\proj\\board.yaml"
    assert ntpath.isabs(path) is _NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT
    assert not PureWindowsPath(path).is_absolute()
    assert path_to_uri_reference(path) == "/proj/board.yaml"


def test_the_absolute_gate_survives_every_supported_interpreter_ntpath_does_not():
    """tan-cli#1126, stated as a property of the SUPPORTED INTERPRETER
    RANGE rather than of whichever one happens to be running.

    `requires-python` is `>=3.12` and CI runs both ends of it deliberately
    (`parity.yml`'s seam1 and `ci.yml`'s `python-newest` float to the newest
    release; every other job pins the 3.12 floor -- see
    `tests/gates/test_interpreter_policy.py`), so "absolute" has to mean one
    thing across that whole range or this module's OUTPUT is
    interpreter-dependent. Its output is a SARIF `artifactLocation.uri` a
    consumer resolves against a base, not a detail.

    `ntpath.isabs` cannot be that predicate: it answers a driveless root
    `True` on 3.12 and `False` from 3.13.
    `PureWindowsPath(...).is_absolute()` answers `False` on all three
    measured interpreters and -- the load-bearing half -- IS the predicate
    the exporter ([`_absolute_path_to_file_uri`]) itself enforces, so gate
    and exporter cannot drift apart on any interpreter, present or future.
    "The two happen to disagree today" was only ever the symptom that made
    the choice visible."""
    for path in ("\\proj\\board.yaml", "/w/proj/board.yaml"):
        assert not PureWindowsPath(path).is_absolute()
        assert ntpath.isabs(path) is _NTPATH_ISABS_ACCEPTS_A_DRIVELESS_ROOT
    # The rendered answers -- the thing that must not vary by interpreter.
    assert path_to_uri_reference("\\proj\\board.yaml") == "/proj/board.yaml"
    assert path_to_uri_reference("/w/proj/board.yaml") == "file:///w/proj/board.yaml"


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
# The exporter is byte-identical to the `PurePath.as_uri()` it replaced
# (tan-cli#1140)
# ---------------------------------------------------------------------------

#: Every absolute shape [`path_to_uri_reference`] can render, in BOTH
#: spellings, paired with the PURE oracle class that spelling routes to.
#: Includes the two edge cases `tan/core/uri_reference.py`'s own docstring
#: lists as deliberately undefended (a `\\?\`-prefixed extended-length path,
#: and drive-letter casing) precisely so that "undefended" keeps meaning
#: "renders the same as it always did" rather than drifting unnoticed.
_ABSOLUTE_EXPORT_CORPUS = (
    ("/w/proj/board.yaml", PurePosixPath),
    ("/tmp/proj/we\\ird.yaml", PurePosixPath),
    ("/w/a b/board.yaml", PurePosixPath),
    ("/w/a#b/board.yaml", PurePosixPath),
    ("/w/\u00e9\u4e2d/board.yaml", PurePosixPath),
    ("/", PurePosixPath),
    ("//srv/x", PurePosixPath),
    (r"C:\w\proj\board.yaml", PureWindowsPath),
    ("c:/w/proj/board.yaml", PureWindowsPath),
    ("C:\\w\\proj/board.yaml", PureWindowsPath),
    (r"C:\w\a b\board.yaml", PureWindowsPath),
    (r"C:\w\a#b\board.yaml", PureWindowsPath),
    ("C:/w/\u00e9\u4e2d/board.yaml", PureWindowsPath),
    (r"\\server\share\board.yaml", PureWindowsPath),
    (r"\\?\C:\x\board.yaml", PureWindowsPath),
    ("C:/", PureWindowsPath),
)


def test_the_exporter_still_emits_exactly_what_purepath_as_uri_emitted():
    """tan-cli#1140's whole acceptance, as a test rather than a transcript.

    `tan/core/uri_reference.py`'s absolute-case exporter WAS
    `PurePath.as_uri()`, called directly on the `PureWindowsPath`/
    `PurePosixPath` [`_is_windows_spelled`] picks. CPython 3.14 deprecates
    that method for REMOVAL in 3.19, and it is not an internal detail: its
    output is the SARIF `artifactLocation.uri` `tan validate --format sarif`
    emits, so the removal would break the envelope surface outright. #1140
    moved the exporter to [`_absolute_path_to_file_uri`], which carries
    `PurePath.as_uri()`'s own body verbatim minus the warning. This test is
    the proof that "verbatim" is exactly true, per input, on whichever
    interpreter is running -- not a claim made once in a PR description.

    **`PurePath.as_uri()` on the right-hand side below is deliberate, and it
    is why this file's header names it.** It is the ORACLE: the retired call
    is the only thing that can prove the replacement emits the same bytes,
    so this one site keeps calling it and the `python \u00b7 pytest on the
    newest CPython` job REPORTS its `DeprecationWarning` -- unsuppressed, as
    that job intends. When 3.19 removes it this test stops being runnable;
    the correct response then is to DELETE it, not to reach for the nearest
    non-deprecated name, because by then the pinned literal goldens the
    absolute-`file:`-URI tests above assert (`"file:///C:/w/proj/board.yaml"`
    and friends) carry the same property with no stdlib call at all. That is
    the whole reason those tests were moved onto literals in #1140.

    The one thing a future replacement must NOT be is
    `pathlib.Path.as_uri()`, the replacement the deprecation message itself
    names -- see [`_PATH_AS_URI_STILL_MATCHES_PUREPATH_CROSS_SPELLING`] and
    the test below it for the measurement that rules it out."""
    for spelling, oracle_cls in _ABSOLUTE_EXPORT_CORPUS:
        pure = oracle_cls(spelling)
        assert pure.is_absolute(), spelling
        assert path_to_uri_reference(spelling) == pure.as_uri(), spelling


def test_pathlib_path_as_uri_is_not_a_usable_replacement_for_the_windows_branch():
    """The measurement that decided #1140 against the deprecation's own
    named replacement, pinned so nobody "finishes the migration" later by
    swapping [`_absolute_path_to_file_uri`] for `Path.as_uri()`.

    A `PureWindowsPath` cannot BECOME a `WindowsPath` on a POSIX host --
    `Path` is concrete and host-bound -- so the only spelling of that swap
    available here is the unbound call below, and from 3.14 it is wrong:
    `Path.as_uri` delegates to `urllib.request.pathname2url(str(self),
    add_scheme=True)`, which dispatches on `os.name` and therefore performs
    the POSIX conversion on this repo's POSIX CI, percent-encoding the drive
    colon and every separator instead of emitting an authority-less
    `file:///C:/...`. The POSIX branch takes the mirror-image damage on the
    `windows-latest` pytest shard.

    This is the same defect shape as tan-cli#1105 and the one PR #1125
    shipped a MERGE verdict on before failing four `windows-latest` shards:
    a substitution that looks like the platform-neutral one, passes on the
    host that runs it, and covers nothing. Below 3.14 the swap is invisible
    (`Path` has no `as_uri` of its own on 3.12; 3.13's is a verbatim copy),
    which is precisely what makes it dangerous -- a green local 3.12 run
    would have said nothing."""
    win = PureWindowsPath(r"C:\w\proj\board.yaml")
    correct = "file:///C:/w/proj/board.yaml"
    assert path_to_uri_reference(r"C:\w\proj\board.yaml") == correct
    assert _absolute_path_to_file_uri(win) == correct
    # The swap, spelled the only way it can be spelled from a POSIX host.
    naive = Path.as_uri(win)
    assert (naive == correct) is _PATH_AS_URI_STILL_MATCHES_PUREPATH_CROSS_SPELLING
    if not _PATH_AS_URI_STILL_MATCHES_PUREPATH_CROSS_SPELLING:
        assert naive == "file:C%3A%5Cw%5Cproj%5Cboard.yaml"


def test_the_exporter_refuses_a_relative_path_the_way_purepath_as_uri_did():
    """Documents the constraint [`path_to_uri_reference`]'s docstring
    states, and the reason [`_absolute_path_to_file_uri`] kept the guard
    when it took over the body: the exporter enforces the very predicate its
    caller gates on (`.is_absolute()`), so gate and exporter cannot drift
    apart, and a relative path reaches a `ValueError` rather than a
    malformed `file:` URI. Both halves are asserted -- the stdlib's refusal
    (the behaviour being preserved) and this module's (the behaviour that
    now has to hold on its own), down to the message text."""
    with pytest.raises(ValueError) as stdlib_refusal:
        PurePosixPath("./board.yaml").as_uri()
    assert "relative path" in str(stdlib_refusal.value)

    with pytest.raises(ValueError) as ours:
        _absolute_path_to_file_uri(PurePosixPath("./board.yaml"))
    assert str(ours.value) == "relative path can't be expressed as a file URI"


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


# ---------------------------------------------------------------------------
# Percent-encoding on the relative branches (round 1 minor)
# ---------------------------------------------------------------------------


def test_a_relative_posix_path_with_a_space_is_percent_encoded():
    """A raw space is not a legal URI-reference character. Measured
    pre-fix: `--board-yaml 'my board.yaml'` rendered `uri` as
    `./my board.yaml`, unencoded."""
    assert path_to_uri_reference("./my board.yaml") == "./my%20board.yaml"


def test_a_relative_posix_path_with_a_hash_is_percent_encoded():
    """`#` is the URI fragment delimiter, not cosmetic: an unencoded `#` in
    `./board#1.yaml` parses as path `./board` plus fragment `1.yaml`, not
    one filename. Measured pre-fix: emitted unencoded."""
    assert path_to_uri_reference("./board#1.yaml") == "./board%231.yaml"


def test_a_literal_colon_in_a_relative_path_is_percent_encoded():
    """tan-cli#1117 review round 3 MINOR: the SOUNDNESS INVARIANT
    [`is_absolute_path_reference`]'s delegation depends on, pinned directly
    rather than left to be inferred from three docstrings that call it
    load-bearing. `quote(path, safe="/")` treating `:` as UNSAFE is what
    keeps a literal `"file:"` substring in a RELATIVE reference from ever
    masquerading as [`path_to_uri_reference`]'s own absolute-only `file:`
    scheme prefix. Widening the safe set to `"/:"`  -- `quote(path, safe=
    "/:")` -- is GREEN across both this file and `test_validate_command.py`
    on its own (no existing assertion pins the colon's own encoding), and
    end-to-end it turns `--project 'file:sub'` into `uri: "file:sub/board
    .yaml"`, misclassified ABSOLUTE by [`is_absolute_path_reference`] --
    silently dropping `uriBaseId`/`originalUriBaseIds` on caller-supplied
    input, reverting this issue's whole fix without any test noticing.
    Measured directly against the real oracle: `path_to_uri_reference(
    "file:sub/board.yaml") == "file%3Asub/board.yaml"`."""
    assert path_to_uri_reference("file:sub/board.yaml") == "file%3Asub/board.yaml"
    assert not is_absolute_path_reference("file:sub/board.yaml")


def test_percent_encoding_does_not_touch_the_separator():
    """`quote(..., safe="/")` -- the separator itself must survive, or every
    relative path collapses to one percent-encoded segment."""
    assert path_to_uri_reference("sub/dir/board.yaml") == "sub/dir/board.yaml"


def test_a_relative_windows_spelled_path_with_a_space_is_percent_encoded_too():
    assert path_to_uri_reference("sub\\my dir\\board.yaml") == "sub/my%20dir/board.yaml"


def test_the_separator_less_golden_is_unaffected_by_percent_encoding():
    """`"./board.yaml"` contains no character `quote` touches -- the pinned
    golden this PR must not move stays byte-identical through the new
    encoding step, not merely through the old unconditional passthrough."""
    assert path_to_uri_reference("./board.yaml") == "./board.yaml"


# ---------------------------------------------------------------------------
# is_absolute_path_reference -- the SARIF uriBaseId gate (tan-cli#1117)
# ---------------------------------------------------------------------------


def test_a_posix_absolute_path_is_judged_absolute():
    assert posixpath.isabs("/tmp/proj/board.yaml")
    assert is_absolute_path_reference("/tmp/proj/board.yaml")


def test_a_posix_relative_path_is_judged_not_absolute():
    assert not posixpath.isabs("./board.yaml")
    assert not is_absolute_path_reference("./board.yaml")


def test_a_windows_absolute_path_is_judged_absolute():
    assert ntpath.isabs("C:\\w\\proj\\board.yaml")
    assert is_absolute_path_reference("C:\\w\\proj\\board.yaml")


def test_a_driveless_windows_relative_path_is_judged_not_absolute():
    """The one input a FIRST version of [`is_absolute_path_reference`]'s own
    docstring worried about: `C:board.yaml` (a drive letter with NO root).
    That worry was measured FALSE and corrected in review (see this repo's
    own history) -- `quote(..., safe="/")` percent-encodes the colon
    (`path_to_uri_reference("C:board.yaml") == "C%3Aboard.yaml"`), so the
    rendered form is never a spoofable `file:`-prefixed string in the first
    place, on this input or any other. [`is_absolute_path_reference`]
    DELEGATES to [`path_to_uri_reference`] rather than re-deriving
    `PureWindowsPath`'s own `.is_absolute()` a second time, so THIS test has
    no mutation power of its own over [`is_absolute_path_reference`] distinct
    from `path_to_uri_reference`'s own existing coverage above -- both
    assertions here restate the SAME fact (one via the real oracle, one via
    the function under test) as a pinned regression guard for the input the
    now-corrected worry singled out, not as a mutation-provable branch. See
    `test_a_posix_absolute_path_is_judged_absolute` /
    `test_a_windows_absolute_path_is_judged_absolute` just above for the
    assertions that DO red under a mutated `"file:"` prefix."""
    assert not PureWindowsPath("C:board.yaml").is_absolute()
    assert not is_absolute_path_reference("C:board.yaml")


def test_a_windows_relative_path_with_no_drive_is_judged_not_absolute():
    assert not is_absolute_path_reference("sub\\p\\board.yaml")


# ---------------------------------------------------------------------------
# cwd_base_uri -- the SARIF originalUriBaseIds base (tan-cli#1117)
# ---------------------------------------------------------------------------


def test_cwd_base_uri_ends_with_a_trailing_slash(tmp_path, monkeypatch):
    """RFC 3986 SS5.3's merge algorithm drops the base's own LAST PATH
    SEGMENT before appending a relative reference -- a slash-less base
    silently answers one directory up. Mutating this function's `+ "/"`
    away leaves this line red on its own; the property test directly below
    is the one that shows WHY (`urljoin` naming a sibling file instead of
    the real one)."""
    monkeypatch.chdir(tmp_path)
    assert cwd_base_uri().endswith("/")


def test_cwd_base_uri_resolves_a_relative_reference_to_the_real_file(tmp_path, monkeypatch):
    """The independent property tan-cli#1117's acceptance criteria state:
    `urljoin(cwd_base_uri(), <relative reference>)` must name the REAL file.
    Proven by actually creating one and reaching it back through
    `os.path`/`Path.samefile`, never by re-deriving the expected string from
    `Path.cwd().as_uri()` -- the same one-line expression [`cwd_base_uri`]
    itself is, and exactly the tautology tan-cli#1117 names as the trap that
    let a wrong base ship green once already.

    Catches BOTH round-1 defects at once: stripping the trailing slash makes
    `urljoin` drop `tmp_path`'s own last segment (naming a file one level up,
    which this dir does not have -- `FileNotFoundError` on `samefile`); and
    since [`cwd_base_uri`] takes no `root` parameter to anchor on instead of
    `Path.cwd()`, the anchoring-mismatch defect has no equivalent input to
    mutate here at all -- it is structurally excluded, not merely tested
    for (see the function's own docstring)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "board.yaml").write_text("som: E1M-AEN701\n", encoding="utf-8")
    resolved = urljoin(cwd_base_uri(), "./board.yaml")
    local_path = Path(url2pathname(urlsplit(resolved).path))
    assert local_path.exists()
    assert local_path.samefile(tmp_path / "board.yaml")


def test_cwd_base_uri_resolves_a_nested_relative_reference_to_the_real_file(
    tmp_path, monkeypatch
):
    """The `--project sub` shape: `resolve_board_path` renders the reference
    as `"sub/board.yaml"` (root folded INTO the reference, not into the
    base), so the base must stay CWD-anchored here too, not move to `sub`
    itself -- moving it would double the `sub/` prefix, the exact round-1
    defect that happened to cancel out for this one case by accident."""
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "board.yaml").write_text("som: E1M-AEN701\n", encoding="utf-8")
    resolved = urljoin(cwd_base_uri(), "sub/board.yaml")
    local_path = Path(url2pathname(urlsplit(resolved).path))
    assert local_path.exists()
    assert local_path.samefile(sub / "board.yaml")


def test_cwd_base_uri_does_not_double_the_slash_at_a_filesystem_root(monkeypatch):
    """tan-cli#1117 review round 2 minor: `Path("/").as_uri()` is already
    slash-terminated (measured on POSIX: `"file:///"`) -- APPENDING
    unconditionally doubles it (`"file:////"`), and SARIF 2.1.0 SS3.14.14
    requires the value "SHALL end with a single forward slash". Mutating the
    conditional back to `+ "/"` unconditionally reds this exact assertion;
    every OTHER test in this section stays green either way (none of them
    chdir to an actual filesystem root), which is why this one exists on
    its own.

    tan-cli#1117 review round 4: asserted as a PROPERTY relative to
    `Path.cwd().as_uri()` (an INDEPENDENT stdlib call, never `cwd_base_uri`
    itself), not the POSIX-specific literal `"file:///"` -- `os.chdir("/")`
    reaches "a filesystem root" on every platform, but not the same root
    STRING: on Windows it is the root of the CURRENT drive (`ntpath`'s own
    `/` semantics), so `cwd_base_uri()` there measured `"file:///D:/"` (the
    CI checkout's drive), not `"file:///"` -- and a plain `not endswith(
    "//")` check is ALSO wrong for the POSIX literal itself: `"file:///"`
    (scheme + empty authority `//` + root `/`) legitimately contains `"//"`
    as its own trailing substring while still carrying exactly ONE
    semantically-trailing slash. The property that survives both platforms:
    `Path.cwd().as_uri()` already ends in `/` on both (empty-authority
    POSIX root and Windows drive root both do), so `cwd_base_uri()` must
    equal it EXACTLY -- no character appended -- rather than needing a
    slash guessed onto it."""
    monkeypatch.chdir("/")
    raw = Path.cwd().as_uri()
    assert raw.endswith("/"), "test assumption: a filesystem root's own as_uri() is slash-terminated"
    assert cwd_base_uri() == raw


def test_cwd_base_uri_or_none_agrees_with_cwd_base_uri_on_the_happy_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cwd_base_uri_or_none() == cwd_base_uri()


def test_cwd_base_uri_or_none_returns_none_when_the_cwd_has_been_removed(monkeypatch):
    """tan-cli#1117 review round 2 BLOCKER: `cwd_base_uri()` calls
    `Path.cwd()` -> `os.getcwd()`, which raises `FileNotFoundError` when the
    process's own working directory has been removed out from under it --
    a real, reproducible condition, not a caller-supplied one. The GUARDED
    wrapper this test is really about, [`cwd_base_uri_or_none`], catches it
    and returns `None` instead. Mutating its `except OSError: return None`
    to re-raise reds this test's own final assertion; mutating it to catch
    `Exception` broadly instead of `OSError` specifically changes nothing
    observable here (still red on the same input either way) but is
    exercised as the narrower, correct type deliberately.

    tan-cli#1117 review round 4: `os.getcwd()` is made to raise via
    `monkeypatch.setattr`, NOT by `os.rmdir`-ing the real CWD -- POSIX lets
    a process delete its own working directory (`gone.rmdir()`'s old shape
    here), Windows does not (`os.rmdir` on the process CWD raises
    `PermissionError [WinError 32]`, measured on `windows-latest` CI: this
    exact test failed there under the removed-directory version). Patching
    `os.getcwd` directly reproduces the SAME contract this function depends
    on -- `Path.cwd()` calls `os.getcwd()` internally on every pathlib
    version this repo tests (3.12/3.13/3.14, measured directly) -- without
    touching a real directory or depending on platform-specific
    CWD-deletion semantics at all. This repo's own rule (tests/core/
    test_board_context.py's header, restated in this file's own module
    docstring): no Windows host, so a platform difference is verified by an
    oracle/mechanism that behaves identically everywhere, not by a real
    filesystem operation whose semantics diverge."""
    def _raise_removed_cwd() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "getcwd", _raise_removed_cwd)
    with pytest.raises(OSError):
        cwd_base_uri()
    assert cwd_base_uri_or_none() is None


# ---------------------------------------------------------------------------
# The urljoin-property tests' own OWN reversal helper -- pinned against the
# real Windows oracle (tan-cli#1117 review round 4)
# ---------------------------------------------------------------------------


def test_a_windows_file_uri_path_component_round_trips_through_nturl2path():
    """This module and `test_validate_command.py`'s urljoin-property tests
    each reverse a resolved `file:` URI back to a filesystem path with
    `Path(url2pathname(urlsplit(...).path))`, so their `os.path.exists`/
    `Path.samefile` assertions mean something. Round 4 found the PRE-fix
    version of that reversal (`Path(unquote(urlsplit(...).path))`, no
    `url2pathname`) failed for real on `windows-latest` CI -- `nturl2path`
    (the module Python's own `os.name`-dispatched `urllib.request.
    url2pathname` delegates to on Windows) strips the leading `/` a `file:
    ///C:/...` URI's path component carries before the drive letter;
    plain `unquote` does not, so `Path("/C:/Users/.../board.yaml")` names a
    ROOT-relative path whose first SEGMENT is literally `"C:"` -- not the
    drive -- and never exists.

    This is `tan`'s OWN code doing nothing wrong: `cwd_base_uri()`'s
    `file:///C:/...` output is exactly the standard `file:` URI spelling
    for a Windows absolute path (RFC 8089 appendix E; every browser, editor
    and SARIF consumer parses it the same way) -- the defect was entirely
    in these TEST files' own reversal helper, never in product code, which
    is why no `tan/core/uri_reference.py` line changed for this fix.

    `nturl2path` is importable and exercised on ANY host (pure Python, no
    OS syscalls) -- this repo's own rule (no Windows host; Windows
    behaviour verified via an `ntpath`/`PureWindowsPath`-shaped oracle, not
    real filesystem operations) applies here exactly as it does to
    [`_is_windows_spelled`] above. Reverting either test file's
    `url2pathname` import back to `unquote` does not red anything ON THIS
    HOST (Linux never takes the leading-slash-before-drive branch), which
    is precisely why this assertion exists: it pins the STDLIB oracle's own
    behaviour for the one input shape a Linux run can never otherwise
    exercise.

    ## `nturl2path` is REMOVAL-SCHEDULED, and stays anyway (tan-cli#1140)

    CPython 3.14 emits, on the `import` below, `DeprecationWarning:
    'nturl2path' is deprecated and slated for removal in Python 3.19; use
    'urllib.request' instead`. The import stays, unsuppressed -- the
    `python · pytest on the newest CPython` job exists to REPORT exactly
    this class, so a `filterwarnings` here would recreate the defect that
    job was added to catch.

    **The `urllib.request` the message names is not a replacement for what
    this test needs.** Measured on this POSIX box, 3.12.3 / 3.13.15 /
    3.14.7 alike, for the very input asserted below:

        urllib.request.url2pathname("/C:/Users/dev/.../board.yaml")
            -> "/C:/Users/dev/.../board.yaml"     (unchanged -- POSIX)
        nturl2path.url2pathname("/C:/Users/dev/.../board.yaml")
            -> "C:\\Users\\dev\\...\\board.yaml"      (Windows)

    The mechanism differs by version and the answer does not. On 3.12/3.13
    the platform choice is made at IMPORT time -- `urllib/request.py` runs
    `if os.name == 'nt': from nturl2path import url2pathname, pathname2url`
    at module level, so the POSIX name is bound to a body that is just
    `unquote(pathname)`. From 3.14 that branch moved INSIDE the function
    (`if os.name == 'nt':` around the drive/separator handling). Either
    way, called from a POSIX host you get the POSIX conversion, and the
    swap would leave this test still passing, still looking like it covers
    Windows, and covering nothing -- the shape of tan-cli#1105, and the
    shape PR #1125 shipped a MERGE verdict on before failing four
    `windows-latest` shards. `nturl2path` is imported precisely because it
    is the one spelling that performs the WINDOWS conversion regardless of
    the running platform, and this repo has no Windows host.

    **What a genuine replacement has to do when 3.19 lands.** Not "the
    nearest non-deprecated name": it has to perform the Windows `file:`
    URI-path -> Windows path conversion FROM A POSIX HOST -- strip the
    extra `/` before a drive letter, swap `/` for `\\`, percent-decode --
    without consulting `os.name`. As of 3.14.7 `urllib.request` offers no
    such platform-explicit spelling (its nt branch is reachable only by
    running on nt). If none has appeared by then, the two honest options
    are to write the conversion out here, pinned by this same assertion, or
    to delete this test and move the coverage onto a real `windows-latest`
    job. Silently rebinding this import to `urllib.request` is neither."""
    import nturl2path

    # `dev` here stands in for the real CI account this defect was measured
    # against (`tests/gates/test_no_leaked_host_paths.py`'s own placeholder
    # set) -- the shape (leading `/` before the drive letter, nested nested
    # segments) is what matters, not the literal account name.
    assert (
        nturl2path.url2pathname(
            "/C:/Users/dev/AppData/Local/Temp/pytest-of-dev/"
            "pytest-1/test_a_relative_sarif_uri_reso0/board.yaml"
        )
        == "C:\\Users\\dev\\AppData\\Local\\Temp\\pytest-of-dev"
        "\\pytest-1\\test_a_relative_sarif_uri_reso0\\board.yaml"
    )
