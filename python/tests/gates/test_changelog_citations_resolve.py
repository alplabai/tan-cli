# Copyright 2026 Alp Lab AB
# SPDX-License-Identifier: Apache-2.0
"""Every `<path>:<line>` coordinate in `changelog.d/**.md` must name a line
that exists.

## Why this is a gate and not a convention

A `file:line` citation written into a pending changelog fragment rots between
the day it is written and the day `assemble_changelog.py --write` folds it, and
until now nothing looked. `splice()` copies fragment bodies byte-for-byte and
never rewrites them, so a coordinate that went stale in `changelog.d/` ships
verbatim into a released `CHANGELOG.md`, where it is permanent.

Five instances were measured in a single day (tan-cli#1191): this file's own
motivating one plus PR #1168's `test_inert_option_markers.py:110` (the
assertion is at `:111`), PR #1166's `parity.yml:2306` (moved to `:2326` by that
PR's own `dev` merge), PR #1181's `release.yml:825` (moved to `:828` by that
PR's own edit), and `1134.fixed.md`'s rotted `parity.yml` set. The alternative
on the table -- "name the symbol, not the coordinate" -- is a convention, and
five instances in one day is what a convention with nothing behind it produces.

## WHAT THIS CATCHES

Exactly one thing, and it is narrow on purpose:

1. A citation whose path resolves to a file in THIS worktree and whose line
   number is outside `1..len(file)`. For a range `a-b`, BOTH endpoints must be
   in range -- `a-b` asserts that lines `a` through `b` are the cited material,
   so an end past EOF names material that is not there. For a comma list
   (`buildplan.py:610,620-623,636`) every item is checked.
2. The same check against a HISTORICAL blob when the citation names one
   explicitly as `<path>@<ref>:<line>` (see "Historical citations" below).

## WHAT THIS DOES NOT CATCH -- read this before trusting a green bar

**An in-range coordinate that points at the wrong line.** This is the single
biggest hole and it includes tan-cli#1191's own title case:
`1163.fixed.md` cited `tan/core/toolchain_provision.py:520` for
`SDK_TOKEN_ENV_VARS`, which is at `:599` in a 1172-line file. `:520` is a
perfectly valid line number, so this gate would have passed it. That citation
is corrected by hand in the same change that adds this file; the gate did not
find it and cannot.

A content-anchored second rung WAS attempted and is rejected as unsound, with
the measurement rather than an opinion. The rule tried was: take the inline
code span adjacent to the citation, pull identifier-shaped tokens out of it,
and require the cited line to be one of the lines that token occurs on. Run
over the 58 resolvable citations on `dev` `fd904d88` it flagged 13 of them, on
16 anchor tokens. Some of the 13 are real rot; several are not, and the two
kinds are indistinguishable to it -- `Exception` beside `model_cmd.py:404-407`,
`workspace` beside `README.md:174` and `bootstrap` beside
`test_every_issue_code_is_registered.py:1772` are generic tokens sitting in an
adjacent span that is about something else, while `infer_runtime_for_core_id`
beside `408.fixed.md`'s scaffold coordinate was a CORRECT past-tense citation.
Telling those apart needs a reader to know which span a coordinate belongs to,
which is the prose judgement this file cannot make; every tightening that
removed a false red removed a true one with it. Rejected as unsound rather
than tuned until it happened to fit these thirteen.

The same measurement is why the citation is not compared against the quoted
source text either: fragments quote source with the annotations stripped.
`1163.fixed.md` writes `SDK_TOKEN_ENV_VARS = ("TAN_GITHUB_TOKEN", ...)`; the
file says `SDK_TOKEN_ENV_VARS: tuple[str, ...] = (...)`. A verbatim match finds
nothing, and "found nothing" cannot be distinguished from "cited the wrong
line".

**A citation to a line that is blank.** Also attempted, also rejected.
Restricted to single-line citations (ranges legitimately span blank lines) it
fires three times on `dev` `fd904d88`. One of the three -- `408.fixed.md`'s
`tan/core/scaffold.py:647` -- is a past-tense citation to a definition that
has since been collapsed away, i.e. exactly the shape the "historical
citation" rule below exists to protect; it is re-cited at an explicit ref in
the same change that adds this file, which is strictly more useful to a reader
than a coordinate a rung merely tolerated. The other two are real rot in
`853.fixed.md`, and they are not two line edits: that fragment's whole
coordinate set has drifted (`_acquire_plan` cited at `build_cmd.py:839` is now
`:516`, `_emit_plan` at `:734` is now `:411`, the seven
`build.plan-unavailable` sites cited as `:734-833` now run `:447`-`:510`, and
a dozen bare `:NNN` continuations follow), so it is filed rather than bundled
into a change about building the gate. Adding the rung today would leave `dev`
red on a fragment this change is not rewriting.

**Anything outside `changelog.d/`.** Not PR bodies (instances 3 and 4 above
were PR bodies -- no gate can reach those), not `CHANGELOG.md` (its folded
sections cite the trees they were written against and are history), not
docstrings, not `docs/`.

**A coordinate on an extension-less path.** `_CITATION` requires a `.ext`, so
`Makefile:12` or `LICENSE:3` is not recognised as a citation at all and is
neither checked nor reported. Nothing in `changelog.d/` cites one today; the
alternative is matching every `word:number` in prose, which pulls in times,
ratios and `python:3.12`.

**A bare continuation coordinate.** `1163.fixed.md` and `853.fixed.md` both
carry runs of `:525`, `:566`, `:1488` that inherit their path from an earlier
sentence. Those name no path, so nothing can resolve them; the two in
`1163.fixed.md` were given their file back by hand in the change that added
this module, after both turned out to be rotted. The rest stay invisible.

**Whether a fragment's sentences are TRUE.** Until the change that added this
file, `1134.fixed.md` carried the claim "no Windows host is available
locally", which is false -- `parity.yml` records pre-landing re-measurement on
a Windows host and `PROVENANCE.txt` records a real win32 oracle capture. It
was corrected by hand, by a reader, in review. This gate would not have
noticed: it resolves coordinates and reads no prose. The disclaimer
`changelog.d/README.md` already carries for `--check` ("it still does not, and
never will, judge whether a fragment's sentences are true") applies here
verbatim.

## The false positive this MUST NOT produce

A fragment may legitimately cite a file its own PR DELETES, or a path in
another repo entirely. Cross-repo citations are not rare: 16 of the 74
coordinates on `dev` `fd904d88` name alp-sdk, alp-sdk-vscode, Zephyr or the
frozen `crates/` tree (`src/backends/inference/alp_model_select.c:88`,
`packages/alp-core/src/monitor/ports.ts:44`, `zephyr/cmake/modules/dts.cmake:371`,
`crates/tan-core/src/build_plan.rs:358`, ...).

So: **a path that does not resolve is never a failure.** Only a path that
resolves AND whose cited line is out of range fails. The cost is the obvious
one and is accepted: a citation whose file was deleted in the same PR is not
checked, and neither is a mistyped path -- a typo'd filename silently becomes
"absent", not "wrong".

Resolution is deliberately conservative, in two steps:

* the cited path as written, if it is a tracked file (`README.md`,
  `scripts/e2e-full.sh:872`, `python/tan/commands/build_cmd.py:839`);
* otherwise the UNIQUE tracked file whose path ends with `/<cited path>`.
  Fragments overwhelmingly cite a suffix, not a repo-relative path --
  `parity.yml`, `bootstrap_cmd.py`, `tan/core/toolchain_provision.py`. 49 of
  the 74 coordinates resolve this way and only this way; 9 more are exact.
* If the suffix matches two or more tracked files, the citation is UNRESOLVED
  and skipped. Picking one would be a guess, and a guess is how a gate starts
  reporting on a file the author never meant. (`README.md` is the standing
  example: 21 other tracked paths end in `/README.md`, and it resolves only
  because the exact-path step fires first.)

## `.github/workflows/**` versus `python/**`

Same rule, no exemption, and the asymmetry runs the other way from what one
might expect. Workflow coordinates rot HARDER -- `test_vacuous_gate_shapes.py`
records `parity.yml` line numbers moving by roughly 43 lines in a single day,
and two of tan-cli#1191's five instances are workflow coordinates. The only
real difference is accidental: workflows are almost always cited by bare
basename (`parity.yml`, `release.yml`) so they lean entirely on the unique-
suffix step, which is why that step insists on uniqueness.

## Fenced blocks and pasted tool output

Two shapes are skipped because they are TRANSCRIPTS -- a measurement of some
other tree at some other moment -- rather than claims about this one:

* anything inside a ``` or ~~~ fence. A fenced block in a fragment is pasted
  output: a diff hunk header, a pytest traceback, a `grep -n` capture. Its
  coordinates belong to whatever produced it.
* an inline span of the grep/compiler shape `<path>:<line>: <text>`, i.e. a
  coordinate that carries the cited line's own text after it. This is not
  hypothetical: `1145.vacuity.added.md` quotes
  `tests/gates/test_no_conflict_markers.py:86: for marker in ():` as the output
  a gate produces when run against a DELIBERATELY MUTATED tree with an extra
  never-iterating loop added. The real file is 82 lines, so range-checking that
  coordinate would red a fragment that is telling the truth about a mutant.

Indented (non-fenced) code blocks are NOT detected, and cannot be: every line
of a fragment is already indented, because a fragment is a Markdown bullet.

## Historical citations -- the explicit-ref form

Some fragments deliberately cite a coordinate at a PARENT commit ("before this
change, X was at `foo.py:647`"). Those must not red, and prose cannot be used
to tell them apart: "before this change" is a phrase, its distance from the
coordinate is unbounded, and matching on it would be a guess dressed as a rule.

So the marker goes in the coordinate, where a machine reads it:

    `tan/core/scaffold.py@8ca9a40adb9c4ee48345e10df58ae5e3516a9316:647`

`<path>@<ref>:<line>` is range-checked against `git show <ref>:<path>` instead
of the worktree. It is a REAL check, not an exemption -- the ref'd blob is
fetched and its length counted -- and it leaves the reader with a coordinate
they can actually visit, which a bare stale number does not.

Preferred first resort is still the cheaper one the issue proposes and PR
#1166 adopted: drop the number and name the symbol. Use `@<ref>` when the line
itself is the point.

Two honest holes in it:

* if the ref does not resolve in the local clone the citation is SKIPPED, not
  failed. `ci.yml`'s `python` job checks out at `fetch-depth: 0` so refs do
  resolve there, but a shallow clone or a ref from a deleted branch must not
  turn into a red build somebody cannot reproduce.
* which means `@<some-unresolvable-ref>` is a hole anyone can drive through.
  It is the same trade every `ALP_SDK_ROOT`-gated skip in this directory makes,
  and it is written down here rather than discovered later.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[3]
FRAGMENT_DIR = REPO / "changelog.d"

#: A coordinate that is the WHOLE of an inline code span. The house style puts
#: every citation in backticks on its own, which is what makes a full-span
#: match safe: matching mid-span would pick coordinates out of pasted argv and
#: expressions (`emit('build-plan', ..., board_yaml=examples/.../board.yaml)`).
#:
#: `:<column>` is accepted and ignored -- `release.yml:1202:9` and
#: `planner-resync.yml:151:9` are both live in `changelog.d/` and a column is
#: not something a line count can check.
_CITATION = re.compile(
    r"""^
    (?P<path>[A-Za-z0-9_.][A-Za-z0-9_./+-]*\.[A-Za-z0-9]{1,10})
    (?:@(?P<ref>[A-Za-z0-9._/~^-]+))?
    :
    (?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)
    (?::\d+)?
    $""",
    re.VERBOSE,
)

#: The grep/compiler shape -- a coordinate carrying its own line's text. See
#: the module docstring: this is pasted tool output, skipped like a fence.
_TRANSCRIPT = re.compile(
    r"""^
    [A-Za-z0-9_.][A-Za-z0-9_./+-]*\.[A-Za-z0-9]{1,10}
    (?:@[A-Za-z0-9._/~^-]+)?
    :\d+:(?!\d)
    """,
    re.VERBOSE,
)

#: Runs of backticks, so ``a `b` c`` is matched as one span rather than two.
#: A span may wrap across lines -- CommonMark folds the newline to a space --
#: so this runs over the whole (de-fenced) document, not line by line.
_CODE_SPAN = re.compile(r"(?P<ticks>`+)(?P<body>[^`]+)(?P=ticks)")

_FENCE = ("```", "~~~")


class Citation(NamedTuple):
    """One coordinate, with everything needed to report it without re-reading
    anything."""

    fragment: str
    line: int
    raw: str
    path: str
    ref: str | None
    numbers: tuple[int, ...]


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


TRACKED = _tracked()
TRACKED_SET = frozenset(TRACKED)


def defence(text: str) -> str:
    """`text` with every fenced-block line blanked, line numbering preserved.

    Blanked rather than dropped so an offset in the result still maps to the
    same line number in the original -- a fragment's error message is useless
    if it names the wrong line.
    """
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith(_FENCE):
            inside = not inside
            kept.append("")
            continue
        kept.append("" if inside else line)
    return "\n".join(kept)


def endpoints(spec: str) -> tuple[int, ...]:
    """Only the numbers WRITTEN in `spec` -- `620-623` gives `(620, 623)`.

    The range interior is not checked against anything; the interior of a
    range is what the range is for.
    """
    out: list[int] = []
    for item in spec.split(","):
        out.extend(int(end) for end in item.split("-"))
    return tuple(out)


def citations_in(name: str, text: str) -> tuple[list[Citation], list[str]]:
    """Every checkable coordinate in one fragment, and the transcript spans
    that were deliberately passed over."""
    found: list[Citation] = []
    skipped: list[str] = []
    body = defence(text)
    for span in _CODE_SPAN.finditer(body):
        raw = span.group("body").strip()
        hit = _CITATION.match(raw)
        if hit is None:
            if _TRANSCRIPT.match(raw):
                skipped.append(raw)
            continue
        found.append(
            Citation(
                fragment=name,
                line=body[: span.start()].count("\n") + 1,
                raw=raw,
                path=hit.group("path"),
                ref=hit.group("ref"),
                numbers=endpoints(hit.group("lines")),
            )
        )
    return found, skipped


def resolve(path: str) -> str | None:
    """The tracked file a cited path names, or `None` if that is a guess.

    Exact tracked path first, then the UNIQUE tracked path ending in
    `/<path>`. Two or more suffix matches is not a resolution.
    """
    if path in TRACKED_SET:
        return path
    matches = [t for t in TRACKED if t.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else None


def _line_count(text: str) -> int:
    return len(text.splitlines())


def worktree_length(rel: str) -> int | None:
    target = REPO / rel
    if not target.is_file() or target.is_symlink():
        return None
    try:
        return _line_count(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, OSError):
        return None


def ref_length(ref: str, rel: str) -> int | None:
    """Line count of `rel` at `ref`, or `None` if that blob is not reachable.

    `None` means SKIP, never fail: a shallow clone genuinely cannot see an old
    ref, and a gate nobody can reproduce locally is worse than a hole.
    """
    done = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        return None
    return _line_count(done.stdout)


def fragments() -> list[Path]:
    return sorted(FRAGMENT_DIR.glob("*.md"))


def documents() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in fragments()]


def audit(docs: list[tuple[str, str]] | None = None) -> tuple[list[str], dict[str, int]]:
    """Every out-of-range coordinate, plus a census of what was looked at.

    `docs` exists so the checks below can push a synthetic fragment through
    THIS function rather than through a second copy of its logic -- a gate
    whose tests exercise a re-implementation proves nothing about the gate.
    """
    offenders: list[str] = []
    census = {
        "coordinates": 0,
        "transcripts": 0,
        "unresolved": 0,
        "worktree": 0,
        "historical": 0,
        "unreachable_ref": 0,
    }
    for name, text in documents() if docs is None else docs:
        found, skipped = citations_in(name, text)
        census["transcripts"] += len(skipped)
        census["coordinates"] += len(found)
        for cite in found:
            rel = resolve(cite.path)
            if cite.ref is not None:
                # The cited path as written may not exist in TODAY's tree at
                # all (that is half the point of citing a ref), so try it at
                # the ref first and only then fall back to the resolution the
                # worktree offers.
                length = ref_length(cite.ref, cite.path)
                if length is not None:
                    rel = cite.path
                elif rel is not None:
                    length = ref_length(cite.ref, rel)
                if length is None:
                    census["unreachable_ref"] += 1
                    continue
                census["historical"] += 1
                where = f"{rel} at {cite.ref}"
            else:
                if rel is None:
                    census["unresolved"] += 1
                    continue
                length = worktree_length(rel)
                if length is None:
                    census["unresolved"] += 1
                    continue
                census["worktree"] += 1
                where = rel
            for number in cite.numbers:
                if 1 <= number <= length:
                    continue
                offenders.append(
                    f"changelog.d/{cite.fragment}:{cite.line}: `{cite.raw}` "
                    f"names line {number} of {where}, which has {length} lines"
                )
    return offenders, census


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_every_changelog_citation_names_a_line_that_exists():
    offenders, _ = audit()
    assert offenders == [], (
        "a changelog fragment cites a line that does not exist. The fragment "
        "ships byte-for-byte into CHANGELOG.md at the next fold, so this is "
        "permanent once released (tan-cli#1191). Re-measure the coordinate, or "
        "-- better -- name the symbol and drop the number. If the coordinate is "
        "deliberately at a PARENT commit, write it as `<path>@<ref>:<line>`; "
        "see this module's docstring:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Anti-vacuity. Every assertion above is over a corpus that is EMPTIED at
# release time (`assemble_changelog.py --write` deletes every fragment), so
# none of it may rest on the corpus being non-empty.
# --------------------------------------------------------------------------


def test_the_fragment_directory_was_actually_read():
    """`changelog.d/README.md` is permanent -- unlike the fragments, the fold
    never deletes it -- so this floor holds on a freshly-released tree too."""
    names = [p.name for p in fragments()]
    assert "README.md" in names, (
        f"changelog.d/ did not yield its own README: {names[:5]}. The glob "
        "found nothing and every assertion here would be vacuous."
    )


def test_the_repo_file_list_is_not_empty():
    assert len(TRACKED) > 100, (
        f"git ls-files returned {len(TRACKED)} paths; nothing would resolve "
        "and no citation could ever be checked."
    )


_GOOD = "- a claim about `changelog.d/README.md:1` and `README.md:1-2`.\n"
_ROTTED = "- a claim about `changelog.d/README.md:999999`.\n"
_FENCED = "- output:\n\n  ```\n  changelog.d/README.md:999999: whatever\n  ```\n"
_TRANSCRIPT_SPAN = "- a gate printed `changelog.d/README.md:999999: nope`.\n"
_ABSENT = "- a claim about `no/such/file/anywhere.py:999999`.\n"
_AMBIGUOUS = "- a claim about `board.yaml:999999`.\n"

_ROT_REPORT = (
    "changelog.d/synthetic.md:1: `changelog.d/README.md:999999` names line "
    "999999 of changelog.d/README.md, which has {length} lines"
)


def _readme_length() -> int:
    return worktree_length("changelog.d/README.md")


def _findings(text: str) -> list[str]:
    """`audit()` itself, over one synthetic fragment. Not a copy of it: the
    parser, the fence stripper, the resolver, the ref lookup and the range
    check under test here are the same objects the gate above calls."""
    return audit([("synthetic.md", text)])[0]


def test_a_rotted_coordinate_is_caught():
    """The control for [`test_every_changelog_citation_names_a_line_that_exists`]
    -- without it, a green bar is indistinguishable from a parser that matched
    nothing at all."""
    assert _findings(_ROTTED) == [_ROT_REPORT.format(length=_readme_length())]


def test_a_live_coordinate_is_not_caught():
    assert _findings(_GOOD) == []


def test_a_coordinate_inside_a_fence_is_not_caught():
    assert _findings(_FENCED) == []


def test_a_coordinate_carrying_its_own_line_text_is_not_caught():
    assert _findings(_TRANSCRIPT_SPAN) == []
    _, skipped = citations_in("synthetic.md", _TRANSCRIPT_SPAN)
    assert skipped == ["changelog.d/README.md:999999: nope"], (
        "the transcript shape must be RECOGNISED and passed over, not merely "
        "unmatched -- otherwise a future tightening of _CITATION silently "
        "starts reddening pasted tool output."
    )


def test_a_path_that_does_not_exist_is_not_caught():
    """The deletion / cross-repo case: 16 of the 74 coordinates in
    `changelog.d/` on `dev` `fd904d88` named another repo entirely."""
    assert _findings(_ABSENT) == []
    assert resolve("no/such/file/anywhere.py") is None
    assert audit([("synthetic.md", _ABSENT)])[1]["unresolved"] == 1


def test_an_ambiguous_suffix_is_not_resolved():
    """`board.yaml` matches many tracked paths under `contract/`; choosing one
    would be a guess."""
    assert _findings(_AMBIGUOUS) == []
    assert resolve("board.yaml") is None
    assert len([t for t in TRACKED if t.endswith("/board.yaml")]) > 1


def test_an_exact_path_wins_over_a_suffix_match():
    """`README.md` is both a tracked path and the suffix of 20-odd others."""
    assert resolve("README.md") == "README.md"
    assert len([t for t in TRACKED if t.endswith("/README.md")]) > 1


def test_a_suffix_is_resolved_when_it_is_unique():
    assert resolve("tan/core/toolchain_provision.py") == (
        "python/tan/core/toolchain_provision.py"
    )
    assert resolve("parity.yml") == ".github/workflows/parity.yml"


def test_a_range_end_past_eof_is_caught_even_when_its_start_is_not():
    """A range asserts its endpoint exists. `changelog.d/README.md` is nowhere
    near 999999 lines, so the start resolves and the end does not."""
    findings = _findings("- see `changelog.d/README.md:1-999999`.\n")
    assert findings == [
        "changelog.d/synthetic.md:1: `changelog.d/README.md:1-999999` names "
        f"line 999999 of changelog.d/README.md, which has {_readme_length()} lines"
    ]


def test_a_range_interior_is_not_checked():
    """`1-2` names two endpoints, not a promise about what sits between them
    -- and `853.fixed.md`'s `build_cmd.py:734-833` spans blank lines on
    purpose."""
    assert _findings("- see `changelog.d/README.md:1-2`.\n") == []


def test_a_comma_list_checks_every_item():
    findings = _findings("- see `changelog.d/README.md:1,999999,2`.\n")
    assert findings == [
        "changelog.d/synthetic.md:1: `changelog.d/README.md:1,999999,2` names "
        f"line 999999 of changelog.d/README.md, which has {_readme_length()} lines"
    ]


def test_a_column_suffix_is_accepted_and_ignored():
    """`release.yml:1202:9` is live in `changelog.d/`; a column is not a line
    count's business, but the LINE still is."""
    assert _findings("- see `changelog.d/README.md:1:9`.\n") == []
    assert _findings("- see `changelog.d/README.md:999999:9`.\n") == [
        "changelog.d/synthetic.md:1: `changelog.d/README.md:999999:9` names "
        f"line 999999 of changelog.d/README.md, which has {_readme_length()} lines"
    ]


def test_line_zero_is_caught():
    """`1..len` -- a file has no line 0, and a coordinate that names one is
    reporting an off-by-one somewhere upstream of the fragment."""
    assert _findings("- see `changelog.d/README.md:0`.\n") == [
        "changelog.d/synthetic.md:1: `changelog.d/README.md:0` names line 0 "
        f"of changelog.d/README.md, which has {_readme_length()} lines"
    ]


# --------------------------------------------------------------------------
# The historical-ref form
# --------------------------------------------------------------------------


_PROBE = "changelog.d/README.md"


def _shorter_ref() -> tuple[str, int] | None:
    """A commit at which [`_PROBE`] had a DIFFERENT length from the worktree.

    A ref whose blob happens to match the worktree would let a `ref_length`
    that silently fell back to the worktree pass -- so the test below insists
    on a ref where the two genuinely disagree, and skips rather than asserting
    on a coincidence when the history is too shallow to offer one.
    """
    now = worktree_length(_PROBE)
    done = subprocess.run(
        ["git", "log", "--format=%H", "-20", "--", _PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        return None
    for sha in done.stdout.split():
        length = ref_length(sha, _PROBE)
        if length is not None and length != now:
            return sha, length
    return None


def test_a_ref_citation_is_measured_against_that_ref_not_the_worktree():
    """Real blobs, real `git show`, no fixture: the length the gate reports is
    the length AT THE REF, and the ref is chosen so that differs from the
    worktree's."""
    found = _shorter_ref()
    if found is None:
        pytest.skip(
            f"no reachable commit in the last 20 touching {_PROBE} has a "
            "different length from the worktree (shallow clone?)"
        )
    sha, at_ref = found
    assert at_ref != worktree_length(_PROBE)
    assert _findings(f"- see `{_PROBE}@{sha}:{at_ref}`.\n") == []
    over = _findings(f"- see `{_PROBE}@{sha}:{at_ref + 1}`.\n")
    assert len(over) == 1 and f"which has {at_ref} lines" in over[0], over


def test_an_unreachable_ref_is_skipped_rather_than_failed():
    """A shallow clone, or a ref from a deleted branch, must not red a build
    nobody can reproduce -- so this is a SKIP, and the census says so out loud
    rather than the citation disappearing into the resolved count."""
    assert ref_length("no-such-ref-anywhere", "changelog.d/README.md") is None
    text = "- see `changelog.d/README.md@no-such-ref-anywhere:999999`.\n"
    offenders, census = audit([("synthetic.md", text)])
    assert offenders == []
    assert census["unreachable_ref"] == 1 and census["historical"] == 0, census


def test_a_ref_citation_to_a_path_gone_from_the_worktree_still_resolves():
    """The whole point of the form: a coordinate at a parent commit, in a file
    that no longer exists. `resolve()` cannot help, `git show <ref>:<path>`
    can -- and a path that never existed at that ref is still a skip, not a
    red."""
    found = _shorter_ref()
    if found is None:
        pytest.skip("no reachable historical blob to measure against")
    sha, at_ref = found
    assert resolve("no/such/file/anywhere.py") is None
    assert ref_length(sha, "no/such/file/anywhere.py") is None
    text = f"- see `no/such/file/anywhere.py@{sha}:1`.\n"
    offenders, census = audit([("synthetic.md", text)])
    assert offenders == [] and census["unreachable_ref"] == 1, census


def test_the_ref_form_parses_the_shapes_that_are_actually_written():
    for raw, path, ref in (
        ("foo.py@HEAD~1:12", "foo.py", "HEAD~1"),
        (
            "a/b.yml@8ca9a40adb9c4ee48345e10df58ae5e3516a9316:1-2",
            "a/b.yml",
            "8ca9a40adb9c4ee48345e10df58ae5e3516a9316",
        ),
        ("c.md@origin/dev:3", "c.md", "origin/dev"),
        ("d.py:4", "d.py", None),
    ):
        hit = _CITATION.match(raw)
        assert hit is not None, raw
        assert (hit.group("path"), hit.group("ref")) == (path, ref)


def test_the_census_reports_what_it_looked_at():
    """A census that reported zeroes everywhere would mean the walk found
    nothing -- and `README.md` alone guarantees at least one coordinate, at
    every point in the release cycle."""
    _, census = audit()
    assert census["coordinates"] >= 1, census
    assert census["worktree"] + census["historical"] >= 1, census
