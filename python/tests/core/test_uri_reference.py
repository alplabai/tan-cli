# SPDX-License-Identifier: Apache-2.0
"""`tan.core.uri_reference` (tan-cli#1097).

A Windows-spelled path exercises the Windows branch on Linux/mac CI
identically, because [`path_to_uri_reference`] picks its oracle
(`PureWindowsPath` vs `PurePosixPath`) from the STRING itself, never from
`os.name` (PR #1089/#1090's own rule). `ntpath.isabs` and
`PureWindowsPath(...).as_uri()`/`.is_absolute()` are all pure string
operations -- no filesystem touch -- so they are real oracles here, not a
simulation, and they answer identically on whichever host runs them. That
property, and NOT "this repo has no Windows host" (false -- see "## The
Windows-host premise, corrected" below, which is where this repo's canonical
statement lives), is why this file is written the way it is.

## Two DELIBERATE uses of removal-scheduled stdlib APIs live in this file

Both are here because Windows behaviour is established against
`ntpath`/`PureWindowsPath` string oracles, which answer on all three legs and
while the change is being written, rather than by running the real thing on
the one leg that could (see "## The Windows-host premise, corrected" below --
CI having Windows is a different matter, and it does). Both are documented
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

## The Windows-host premise, corrected (tan-cli#1140, finished at #1153)

**"This repo has no Windows host" is false, and no site in this repo says it
any more.** It stood in nine places across five files, and tan-cli#1153
removed the last of them; this section is the canonical statement the
siblings now cite instead of restating it.

The measurement, cited by JOB AND KEY rather than by offset -- see "why no
line numbers" below. `.github/workflows/parity.yml` defines a
`python-tests-shard` job whose `strategy.matrix` carries
`os: [ubuntu-latest, windows-latest, macos-latest]` and whose `runs-on:` is
`${{ matrix.os }}`; its pytest step runs
`python -m pytest -q --ignore=tests/gates --ignore=tests/parity` with
`--shard-id`/`--num-shards=4` on every `pull_request`. THIS FILE therefore
runs on a real Windows host on every PR, and its result rolls up into the
required contexts `python -- pytest across python/ (<os>)` through the
`python-tests` job -- which is a different job, `runs-on: ubuntu-latest`,
with no checkout and no pytest, whose own `matrix.os` exists only to
reproduce those three context strings. Cite the SHARD job, never the
aggregate: the aggregate is not a Windows run.

Three facts had been compressed into that one false sentence, and they come
apart like this:

1. **A pure string oracle is what makes Windows behaviour checkable at all
   three legs and before a push.** `ntpath.isabs`, `PureWindowsPath` and
   `nturl2path` touch no filesystem and read no `os.name`, so they ask the
   WINDOWS question on the ubuntu and macos legs too, and on whatever box the
   change is being written on. A real filesystem operation asks it on exactly
   one leg. That, not any absence, is why every oracle in this file is
   string-shaped -- and it is the part of the old sentence's advice that was
   always right.
2. **CI's Windows shards are real and will red you after a push.** PR #1125
   shipped a MERGE verdict from local measurement and then failed four
   `windows-latest` shards; PR #1151 was caught by `python -- pytest shard
   (windows-latest 2/4)` for a CRLF fixture bug while its review was still
   running. Both stories require CI to have Windows.
3. **Their coverage has two holes**, which is the part nobody had written
   down:
   * the shards run `--ignore=tests/gates`, and `tests/gates` is
     ubuntu-only by a recorded decision (see `ci.yml`'s comment above its
     `python` job, tan-cli#372/#1152). Nothing under `tests/gates` is ever
     executed on Windows or macOS.
   * the shard's `actions/setup-python` step pins `python-version: "3.12"`,
     so a divergence that only appears on 3.13+ CANNOT be caught by a
     required context there. PR #1150 found a live instance: `Path.as_uri`
     is host-dispatched only from 3.14, so "finishing the migration" to it
     would pass every required context and red only in the advisory
     `python -- pytest on the newest CPython` job, which is ubuntu.

Note what is NOT claimed here. "No Windows host is available locally" is a
tempting replacement and it is ALSO an overstatement: `parity.yml`'s own
pin-bump comments record byte-parity re-measurements taken "(Windows host,
`py -3.14`)" before landing, `tests/fixtures/oracle_captures/PROVENANCE.txt`
records a real win32 oracle capture, and
`tests/commands/test_monitor_command.py`'s UNC-port case is measured against
a real `serial.Serial` on a Windows box. A Windows host is sometimes
available and sometimes not; the oracles above hold either way, which is
precisely why the justification is stated as fact 1 and not as an inventory
of anybody's desk.

**Why no line numbers.** This paragraph has carried three different sets --
`:2279`/`:2284`/`:2302`/`:2382`/`:2555` when tan-cli#1140 wrote it,
`:2322`/`:2327`/`:2345`/`:2425`/`:2598` after tan-cli#1162 re-measured them,
and both sets were stale within days because `parity.yml` is a file several
PRs a week touch. Job names and YAML keys are what the claim actually rests
on and they do not rot, and one command re-derives an offset whenever one is
genuinely needed:

    grep -n "python-tests-shard" .github/workflows/parity.yml

Two consequences worth keeping straight:

* The `nturl2path` conclusion below is unchanged. The ubuntu and macos legs
  still have to perform the Windows conversion from a POSIX host, so the
  deprecated import is still the only spelling that does it.
* The Windows shard pins `python-version: "3.12"` (`parity.yml:2425`), where
  `Path.as_uri()` is not host-dispatched at all. So the Windows leg cannot
  catch the `Path.as_uri()` class today no matter what it asserts, and
  `python · pytest on the newest CPython` -- advisory, ubuntu, ceiling -- is
  the only job that can.

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

#: Whether THIS interpreter's `pathlib.Path.as_uri()` is HOST-DISPATCHED --
#: i.e. whether the conversion it performs is chosen by the running machine's
#: `os.name` rather than by the path's own spelling.
#:
#: A pure VERSION fact, with no `os.name` term in it, which is the point:
#: 3.14 is where `Path.as_uri()` stopped sharing `PurePath.as_uri()`'s body
#: and became `return pathname2url(str(self), add_scheme=True)`, and
#: `urllib.request.pathname2url` opens with a literal `if os.name == 'nt':`.
#: Read on real python-build-standalone builds of each:
#:
#:     3.12.3     `Path` has NO `as_uri` in its own `__dict__`; it INHERITS
#:                the deprecated `PurePath.as_uri`      -- not dispatched
#:     3.13.15    `Path.as_uri` is a verbatim COPY of `PurePath.as_uri`
#:                                                      -- not dispatched
#:     3.14.7     `Path.as_uri` -> `pathname2url(..., add_scheme=True)`
#:                                                      -- DISPATCHED
#:
#: Review round 1 MAJOR 1 killed the previous shape of this constant, and the
#: kill is worth keeping written down. It read
#: `_PATH_AS_URI_STILL_MATCHES_PUREPATH_CROSS_SPELLING = sys.version_info <
#: (3, 14)` and the test asserted `(naive == correct) is` that value -- a
#: version-ONLY constant keyed to an outcome that depends on version AND
#: `os.name`. On 3.14.7 with `os.name = 'nt'` the Windows-spelled input
#: MATCHES, so the constant said `False` where the outcome was `True` and the
#: assertion failed. It passed only because the Windows shard pins 3.12
#: (`parity.yml:2425`); the pin whose whole job was to survive a future
#: migration was itself keyed on half the condition. Hence the split below:
#: this constant states the mechanism, which is version-only and therefore
#: safe to predict, and the test MEASURES the outcome over a corpus instead
#: of predicting it per input.
#:
#: Deliberately NOT a `skipif`: the test asserts a property on BOTH sides of
#: this boundary, so an interpreter that changes the answer in either
#: direction reds rather than quietly skipping.
_PATH_AS_URI_IS_HOST_DISPATCHED = sys.version_info >= (3, 14)

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
    guard when it took that body over). Gating on `.is_absolute()` routes this
    to the backslash-swap branch instead of crashing; the result is a legal
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
#:
#: `"/w/\udcff.yaml"` carries a LONE SURROGATE, and it is the one input that
#: exercises the single host-sensitive call
#: [`_absolute_path_to_file_uri`]'s docstring admits to: `os.fsencode` is
#: utf-8/`surrogateescape` on POSIX and utf-8/`surrogatepass` on Windows.
#: Measured old-vs-new identical on 3.12.3/3.13.15/3.14.7 (`file:///w/%FF
#: .yaml`), and it stays in the corpus so the Windows shard re-checks the
#: claim rather than leaving it as prose. (`"/w/\ud800.yaml"` -- an UNPAIRED
#: HIGH surrogate -- raises `UnicodeEncodeError` on POSIX, old and new alike;
#: it is deliberately not in the corpus, since this table asserts rendered
#: strings, and the raise is behaviour inherited verbatim from the stdlib
#: body rather than a property #1140 chose.)
_ABSOLUTE_EXPORT_CORPUS = (
    ("/w/proj/board.yaml", PurePosixPath),
    ("/w/\udcff.yaml", PurePosixPath),
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
    names -- see [`_PATH_AS_URI_IS_HOST_DISPATCHED`] and the test below for
    the measurement that rules it out."""
    for spelling, oracle_cls in _ABSOLUTE_EXPORT_CORPUS:
        pure = oracle_cls(spelling)
        assert pure.is_absolute(), spelling
        assert path_to_uri_reference(spelling) == pure.as_uri(), spelling


def _path_as_uri_or_none(pure: PurePosixPath | PureWindowsPath) -> str | None:
    """`Path.as_uri()` applied to a PURE path -- the only spelling the "just
    take the replacement the deprecation names" swap can have here, since
    `Path` is concrete and a `PureWindowsPath` cannot become a `WindowsPath`
    on a POSIX host (nor a `PurePosixPath` a `PosixPath` on Windows).

    Returns `None` if it raises, so a caller can treat "raised" and "returned
    the wrong string" alike: both mean not-a-drop-in, and which one a given
    host produces is not something this repo can enumerate from one box."""
    try:
        return Path.as_uri(pure)
    # BLE001 is suppressed rather than narrowed on purpose: the question this
    # helper answers is "is `Path.as_uri()` a drop-in for the exporter", and
    # ANY failure is a `no`. Enumerating what it can raise per host and per
    # interpreter (`ValueError`, `urllib.error.URLError` from 3.14's
    # `pathname2url`, `UnicodeEncodeError` on a lone surrogate) would make the
    # answer depend on a list this box cannot finish measuring -- exactly the
    # host-dependence the caller exists to expose.
    except Exception:  # noqa: BLE001
        return None


def test_pathlib_path_as_uri_is_not_a_usable_replacement_for_this_exporter():
    r"""The measurement that decided #1140 against the deprecation's own named
    replacement, pinned so nobody "finishes the migration" later by swapping
    [`_absolute_path_to_file_uri`] for `Path.as_uri()`.

    From 3.14 `Path.as_uri` is `return pathname2url(str(self),
    add_scheme=True)`, and `urllib.request.pathname2url` opens with a literal
    `if os.name == 'nt':`. So the conversion it performs is the RUNNING
    HOST's, while [`path_to_uri_reference`] picks its oracle from the path's
    own SPELLING. Those two agree only where host and spelling happen to
    coincide, which is why this test asserts the OUTCOME over a corpus rather
    than predicting it per input from an interpreter version -- review round
    1 MAJOR 1, recorded on [`_PATH_AS_URI_IS_HOST_DISPATCHED`], killed the
    version-only prediction that came before it.

    All four cells, measured -- the two POSIX ones on real
    python-build-standalone builds on this box, the two `nt` ones by
    SIMULATION (`os.name = "nt"; os.path = ntpath`, which is what
    `pathname2url` branches on) -- which is what puts all four cells in one
    place, measurable from one host, rather than two of them behind a push:

        3.12/3.13, either host   0 of 17 corpus inputs disagree
        3.14.7, os.name=posix    9 of 9 WINDOWS-spelled inputs disagree,
                                 0 of 8 POSIX-spelled
        3.14.7, os.name='nt'     1 of 9 Windows-spelled disagrees (the
                                 `\\?\` extended-length path, which
                                 `Path.as_uri()` normalises and
                                 `PurePath.as_uri()` did not), and 2 of 8
                                 POSIX-spelled do (`/tmp/proj/we\ird.yaml`
                                 and `//srv/x`)

    The damage does not vanish on a Windows runner, it MOVES -- and the
    POSIX-spelled shape it breaks there is `/tmp/proj/we\ird.yaml` ->
    `"file:///tmp/proj/we/ird.yaml"`, silently naming a DIFFERENT file, which
    is round 1 MAJOR 1 verbatim. That is the whole argument in one input.

    So the assertion below is: **`Path.as_uri()` agrees with this module's
    exporter on every corpus input if and only if it is not host-dispatched.**
    Both directions are asserted, so the test cannot go vacuous. Mutating both
    exporter call sites to `Path.as_uri()` reds it (and five other tests) on
    3.14.7; on 3.12.3 and 3.13.15 that mutation is invisible, which is
    precisely what would have made the swap dangerous and why nobody should
    read a green local floor run as this pin holding. Below 3.14 this test is
    still asserting something real -- that the swap was genuinely equivalent
    then -- rather than skipping.

    This is the defect shape of tan-cli#1105, and of PR #1125, which shipped a
    MERGE verdict from local measurement and then failed four `windows-latest`
    shards: a substitution that looks platform-neutral, passes on the host
    that runs it, and covers nothing."""
    disagreements = [
        spelling
        for spelling, oracle_cls in _ABSOLUTE_EXPORT_CORPUS
        if _path_as_uri_or_none(oracle_cls(spelling)) != path_to_uri_reference(spelling)
    ]
    if _PATH_AS_URI_IS_HOST_DISPATCHED:
        assert disagreements, (
            "Path.as_uri() dispatches on os.name from 3.14, so some corpus "
            "spelling foreign to this host must disagree with the exporter. "
            "None did -- either the exporter has been swapped for it, or "
            "CPython changed the dispatch."
        )
    else:
        assert not disagreements, (
            "below 3.14 Path.as_uri() IS PurePath.as_uri(), so nothing may "
            f"disagree; these did: {disagreements}"
        )

    # The exact damage, pinned only on the host it was actually measured on.
    # The `nt` strings above are documented, not asserted: they come from a
    # simulation, and a wrong prediction would red the Windows shard for a
    # false reason the day it moves off its 3.12 pin (`parity.yml:2425`).
    if _PATH_AS_URI_IS_HOST_DISPATCHED and os.name != "nt":
        win = PureWindowsPath(r"C:\w\proj\board.yaml")
        assert _absolute_path_to_file_uri(win) == "file:///C:/w/proj/board.yaml"
        assert _path_as_uri_or_none(win) == "file:C%3A%5Cw%5Cproj%5Cboard.yaml"


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
    CWD-deletion semantics at all. This repo's own rule (this file's module
    docstring, "## The Windows-host premise, corrected", is where it is
    stated): a platform difference is verified by an oracle/mechanism that
    behaves identically everywhere, not by a real filesystem operation whose
    semantics diverge -- so it is checked on all three shard legs and before
    a push, rather than only on `windows-latest` and only afterwards."""
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
    OS syscalls) -- this repo's own rule (Windows behaviour verified via an
    `ntpath`/`PureWindowsPath`-shaped oracle that answers on every runner,
    not via real filesystem operations that answer on one) applies here
    exactly as it does to [`_is_windows_spelled`] above. Reverting either
    test file's
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
    the running platform.

    That reason survives CI having a Windows runner, which it does -- see
    "## The Windows-host premise, corrected" in this file's header. Two of
    the three platforms this suite runs on (`parity.yml`'s
    `python-tests-shard` matrix `os:`) are POSIX, and on those legs
    `urllib.request` would do the POSIX conversion and this
    assertion would stop meaning anything. A test that is real on one of
    three runners and vacuous on the other two is not coverage; the
    deprecated import is what makes it real on all three.

    **What a genuine replacement has to do when 3.19 lands.** Not "the
    nearest non-deprecated name": it has to perform the Windows `file:`
    URI-path -> Windows path conversion FROM A POSIX HOST -- strip the
    extra `/` before a drive letter, swap `/` for `\\`, percent-decode --
    without consulting `os.name`. As of 3.14.7 `urllib.request` offers no
    such platform-explicit spelling (its nt branch is reachable only by
    running on nt). If none has appeared by then, the honest option is to
    write the conversion out here, pinned by this same assertion.

    An earlier draft offered "or delete this test and move the coverage onto
    a real `windows-latest` job" as the alternative. That is not an
    alternative, and the reason is the correction in this file's header: the
    `windows-latest` job already exists and this test already runs on it. It
    is the two POSIX legs that would lose their coverage, which is the thing
    the deprecated import buys. Silently rebinding this import to
    `urllib.request` is not an option either way."""
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
