# SPDX-License-Identifier: Apache-2.0
"""Gate: `PINNED_PLANNER_ORACLE_SDK_REF` moves in lockstep with
`PINNED_SDK_TAG` (tan-cli#895, tightened by tan-cli#897's fix round).

## The incident this closes

`parity.yml` carries two alp-sdk pins, both `env:`-level 40-hex commits:

    PINNED_SDK_TAG                 the alp-sdk tan TRACKS. Moves forward.
    PINNED_PLANNER_ORACLE_SDK_REF  the alp-sdk `tan/planner/**` was last
                                    PORTED FROM / verified against.

Its own comment says the second "moves in lockstep with `PINNED_SDK_TAG`
until #270 lands, then stops for good" -- and until tan-cli#895, nothing
checked that. While PR #884 was in review, `dev` moved `PINNED_SDK_TAG` to
`eb96112b` (tan-cli#868/#888, the alp-sdk fix for the GD32 bridge chip's
missing `CONFIG_SPI=y`, ported into `tan/planner/`'s live
`_CHIP_SUBSYSTEMS` table). `PINNED_PLANNER_ORACLE_SDK_REF` -- and the frozen
oracle fixture it names -- stayed at `94378a05`. GitHub tests the merge
ref, so CI rendered `tan/planner/`'s now-CORRECT output against goldens
frozen from the STALE ref that predates the fix, and 19 cases across 19
boards on all three OSes failed on a missing `CONFIG_SPI=y` at line 24 --
after PR #884 had already been signed off twice.

## What this checks, and why PROVENANCE.txt is in the chain

Three facts must agree while the invariant is live:

  * `parity.yml`'s `PINNED_SDK_TAG`
  * `parity.yml`'s `PINNED_PLANNER_ORACLE_SDK_REF`
  * `tests/fixtures/planner_oracle/PROVENANCE.txt`'s `alp-sdk ref` line --
    the record of which commit the checked-in frozen bytes actually came
    from, read by `python scripts/capture_planner_oracle.py` on write and by
    `test_planner_oracle_regression.py` on every run. A workflow pin that
    was bumped without regenerating the fixture (or a fixture regenerated
    without bumping the pin) is exactly the "two pins, one checkout" trap
    `tests/conftest.py`'s `sdk_pin_disagreements` docstring already names for
    the OTHER pin pair -- this module closes the same class of gap for this
    pair, and unlike that one, this one is NOT advertised as sometimes
    deliberate: the postmortem above is what "not free to sit at an
    arbitrary pre-#270 ref" cost the last time it drifted.

Deliberately a HARD failure, not the `tests/gates/test_sdk_pin_disagreement_
warning.py` warn-only shape: that gate's own docstring says a
`PINNED_SDK_COMMIT`/`PINNED_SDK_TAG` split "is a legitimate state the
maintainer may choose" because the audit commit "can legitimately sit on
either side" of the parity tag. `PINNED_PLANNER_ORACLE_SDK_REF` has no such
exemption written down anywhere -- its OWN comment says the opposite ("is
NOT free to sit at an arbitrary pre-#270 ref while `PINNED_SDK_TAG` moves").
This module does NOT fold into `conftest.sdk_pin_disagreements` or
`test_planner_relocation_freshness.py` for exactly that reason: both keep
their own pin split warn-only on purpose, and reusing that seam would
silently inherit an exemption this pin must not have.

## The comparison is UNCONDITIONAL -- it needs no alp-sdk checkout at all

tan-cli#897 (this fix round): the first cut of this gate ran the three-way
comparison only when a bound `ALP_SDK_ROOT` checkout still shipped
`scripts/alp_orchestrate/__init__.py` -- mirroring
`test_planner_parity_actually_ran.py`'s guard shape, which is the RIGHT shape
for a test that reads FROM a live SDK checkout. This test does not: all three
facts it compares (`parity.yml`'s two pins, `PROVENANCE.txt`'s recorded ref)
are files already IN THIS REPOSITORY. Gating them on `ALP_SDK_ROOT` meant the
gate SKIPPED on every ordinary local `pytest tests -q` and on `ci.yml`'s
`python` job -- exactly the incident #895 was written to close, reproduced by
this very gate: replayed against the real historical tree at `6a826434~1`
(`PINNED_SDK_TAG=eb96112b`, `PINNED_PLANNER_ORACLE_SDK_REF=94378a05`,
`PROVENANCE.txt` at `94378a05`), the ordinary unbound local run passed clean
-- 15 passed, 1 skipped -- while the drift sat there in three plainly-readable
files. A gate that only fires in a specially-provisioned CI job is not the
"local pytest bar at author time" #895 promised; it is the exact merge-ref-CI
timing #895's postmortem blamed. Fixed by running the comparison always.

## The tag-name blind spot -- a HARD failure, not a warning, not a resolve

`PINNED_SDK_TAG` legitimately holds a release name rather than a 40-hex commit
(historically `v0.13.0`/`v0.14.0`/`v0.15.0-rc1`), and `_pinned_sdk_tag_state`
(in `_planner_oracle_ref_matching_core.py`) must not report that as an
absent pin. But tan-cli#897's review found a
second-order bug this fix round introduced: when `PINNED_SDK_TAG` is a release
name, `PINNED_PLANNER_ORACLE_SDK_REF` and `PROVENANCE.txt`'s own recorded ref
CAN still agree with each other while BOTH are stale relative to the commit
the tag actually names -- `capture_planner_oracle.py` writes both together, so
internal agreement between them proves nothing about the tag. Replayed
verbatim against `6a826434~1`'s tree (the real #884 incident state) with only
`PINNED_SDK_TAG` changed from `eb96112b` to its own tag name `v0.16.0`: **19
passed**, silently. That is the exact incident this whole module exists to
close, wearing the one disguise the first cut of this fix didn't check for.

Three ways to close it were weighed:

1. **Resolve the tag name to a commit** (e.g. shell out to `git ls-remote` or
   the GitHub API) so the three-way comparison can run for real. Rejected:
   it would make this test's "no alp-sdk checkout, no network" property
   (the whole point of the #895/#897 fix -- see the section above) conditional
   on network access or a bound checkout with the right tag fetched, which is
   exactly the "only correct when a specific checkout happens to be bound"
   failure mode #897 just removed from the OTHER half of this same gate. Not
   workable without giving back the property that makes this gate fire on an
   ordinary local `pytest tests -q`.
2. **A loud, non-fatal warning** (the `tests/conftest.py` tan-cli#691 shape --
   printed once per session via the terminal reporter, never failing the
   run). Rejected as insufficient here, specifically: #691's shape exists for
   a genuinely different kind of fact (which alp-sdk tree happens to be bound
   to THIS session -- environment-dependent, not a property of the committed
   files). This gate's whole three-way comparison is over files already
   checked into this repository; nothing about it depends on the session. A
   warning that doesn't fail the run is invisible to a merge gate, and #884's
   own postmortem is that a PR merged clean through two human sign-offs with
   the drift sitting there in plain text -- a state a human can read past is
   exactly what #895/#897 built an assertion instead of a comment to stop
   relying on. This module's own docstring already rejects a written-down
   exemption for this pin (see "Deliberately a HARD failure" above); "the
   comparison happens to be unresolvable right now" is not a stronger case
   for an exemption than "the comparison legitimately differs right now" --
   if anything it is weaker, since an unresolvable comparison hides its own
   risk rather than displaying it.
3. **A hard problem, appended to `find_problems`'s return, unconditionally,
   in this state** -- the choice implemented in `_planner_oracle_ref_matching_core.py`
   (`find_problems`, imported below). It costs a real thing:
   `PINNED_SDK_TAG` cannot be re-pinned to a release name without this one
   gate going red until either it moves back to a 40-hex commit, or a
   maintainer resolves the tag by hand (`git rev-parse <tag>` against any
   alp-sdk checkout, which is not an unusual thing to have open while doing
   the re-pin) and re-pins `PINNED_SDK_TAG` to that resolved 40-hex form
   instead. That cost is deliberate: this gate exists to make "the oracle
   moved out of lockstep and nothing said so" impossible, and a state that
   cannot be verified is treated the same as a state proven to have drifted,
   because from this gate's vantage point they are indistinguishable in risk
   -- only in whether the bad case has been observed yet.

## The post-#270 retirement -- an in-repo marker, not a bound-checkout probe

tan-cli#270 deletes `scripts/alp_orchestrate/` from alp-sdk. After that
lands, `PINNED_PLANNER_ORACLE_SDK_REF` is meant to freeze for good -- there
is no later ref that still ships a planner to capture, so nothing could ever
again make it equal a moving `PINNED_SDK_TAG`. A gate that kept asserting
equality past that point would be permanently red for a state the design
accepts on purpose.

Deciding "has #270 landed" by probing a bound `ALP_SDK_ROOT` checkout is what
tied the whole comparison's run/skip decision to that checkout in the first
place -- and #270 landing or not landing is a fact about alp-sdk's history,
not about which checkout happens to be bound to THIS pytest invocation
(unbound, bound-but-stale, bound-and-current all answer differently, and
`ALP_SDK_ROOT` is absent on the overwhelming majority of runs). `POST_270_RETIRED`
below is the in-repo marker instead: a hand-maintained flag flipped by whoever
lands the retirement, the same shape this repository already uses for its
other audit/retirement pins -- `PINNED_SDK_COMMIT`, `HAND_PORT_PINNED_SDK_COMMIT`,
and `STRICT_LOADERS_PINNED_SDK_COMMIT`
(`test_planner_relocation_freshness.py:516,753,934`) are all hand-edited
constants a maintainer updates when the state they describe changes, never
values probed from whatever happens to be bound at pytest time.

Run locally (no checkout needed -- this compares the checkout you already
have):

    python -m pytest tests/gates/test_planner_oracle_ref_tracks_pinned_sdk_tag.py -q
"""

from __future__ import annotations

import pytest

from tests.conftest import _PARITY_WORKFLOW
from tests.gates._planner_oracle_ref_matching_core import (
    _PINNED_PLANNER_ORACLE_SDK_REF_RE,
    _PROVENANCE,
    find_problems,
)

#: Flip this to `True` the day alp-sdk lands tan-cli#270 (deletes
#: `scripts/alp_orchestrate/` for good) and this test should be RETIRED --
#: from that day there is no live alp-sdk ref left that could ever again make
#: `PINNED_PLANNER_ORACLE_SDK_REF` equal a moving `PINNED_SDK_TAG`, and the
#: assertion below would be permanently red for a state the design accepts on
#: purpose.
#:
#: A HAND-MAINTAINED marker, deliberately -- not probed from a bound
#: `ALP_SDK_ROOT` checkout. The comparison this gate makes needs no alp-sdk
#: checkout at all (see the module docstring); tying its retirement decision
#: to one anyway would just reintroduce the same "only correct when a
#: specific checkout happens to be bound" failure this fix round removes from
#: the comparison itself. Follows the shape this repository already uses for
#: its other audit/retirement pins -- `PINNED_SDK_COMMIT`,
#: `HAND_PORT_PINNED_SDK_COMMIT`, `STRICT_LOADERS_PINNED_SDK_COMMIT`
#: (`test_planner_relocation_freshness.py:516,753,934`) -- all hand-edited,
#: none probed.
POST_270_RETIRED = False


def test_pinned_planner_oracle_ref_tracks_pinned_sdk_tag() -> None:
    """The real thing this gate exists to guard, against this checkout's OWN
    `parity.yml` and `PROVENANCE.txt`, right now -- UNCONDITIONALLY. This is
    three files already in this repository; no alp-sdk checkout is read, so
    there is nothing for an ordinary `pytest tests -q` to skip on."""
    if POST_270_RETIRED:
        pytest.skip(
            "PINNED_PLANNER_ORACLE_SDK_REF lockstep is retired for good: "
            "POST_270_RETIRED=True in this file records that tan-cli#270 has "
            "landed in alp-sdk, so there is no live alp-sdk ref left that could "
            "ever again make this pin equal a moving PINNED_SDK_TAG."
        )
    parity_text = _PARITY_WORKFLOW.read_text(encoding="utf-8")
    provenance_text = _PROVENANCE.read_text(encoding="utf-8") if _PROVENANCE.is_file() else None
    problems = find_problems(parity_text, provenance_text)
    assert problems == [], "\n".join(problems)


# ---------------------------------------------------------------------------
# Negative self-tests: prove `find_problems` actually fires, on synthetic
# fixtures, before trusting the clean self-check above. Mirrors
# `test_apt_bounded.py`'s `find_problems()` + tmp-fixture shape and the
# negative self-tests `test_parity_workflow_concurrency_and_timeouts.py`
# added in tan-cli#876 (`fc15a1d0`).
# ---------------------------------------------------------------------------

_A = "a" * 40
_B = "b" * 40


def _parity_text(tag: str, oracle_ref: str) -> str:
    return f"env:\n  PINNED_SDK_TAG: {tag}\n  PINNED_PLANNER_ORACLE_SDK_REF: {oracle_ref}\n"


def _provenance_text(ref: str) -> str:
    return f"THE FROZEN alp-sdk PLANNER ORACLE (tan-cli#509)\n\nalp-sdk ref   {ref}\n"


def test_matching_refs_are_clean() -> None:
    assert find_problems(_parity_text(_A, _A), _provenance_text(_A)) == []


def test_diverged_pins_are_caught() -> None:
    """The tan-cli#509 incident, replayed: `PINNED_SDK_TAG` moves (to `_B`)
    while `PINNED_PLANNER_ORACLE_SDK_REF` and the fixture's own
    `PROVENANCE.txt` both stay at the stale `_A` -- exactly what PR #884's
    review left behind. Both facts having drifted from the moved tag is
    reported as two independent problems (see the sibling test below), not
    collapsed into one; this test is the "it fires at all" case."""
    problems = find_problems(_parity_text(_B, _A), _provenance_text(_A))
    assert problems, "a diverged PINNED_PLANNER_ORACLE_SDK_REF produced no problems at all"
    joined = "\n".join(problems)
    assert _A in joined and _B in joined, problems
    assert "drifted from PINNED_SDK_TAG" in joined, problems


def test_only_the_workflow_pin_lagging_is_reported_in_isolation() -> None:
    """The fixture WAS regenerated and `PROVENANCE.txt` DOES record the new
    ref, but whoever did it forgot the one-line `parity.yml` bump --
    isolates the `PINNED_PLANNER_ORACLE_SDK_REF` vs `PINNED_SDK_TAG` check
    from the `PROVENANCE.txt` vs `PINNED_SDK_TAG` check, proving each fires
    on its own rather than only ever together."""
    problems = find_problems(_parity_text(_B, _A), _provenance_text(_B))
    assert len(problems) == 1, problems
    assert "PINNED_PLANNER_ORACLE_SDK_REF" in problems[0], problems
    assert "PROVENANCE.txt" not in problems[0], problems


def test_diverged_pins_report_both_directions_independently() -> None:
    """A tag that differs from BOTH the workflow's oracle pin and the
    fixture's own recorded ref reports both disagreements, not just the
    first one found -- a reader fixing only the workflow pin must not be
    told the run is clean when the fixture is still stale."""
    problems = find_problems(_parity_text(_B, _A), _provenance_text("c" * 40))
    assert len(problems) == 2, problems
    joined = "\n".join(problems)
    assert "PINNED_PLANNER_ORACLE_SDK_REF" in joined
    assert "PROVENANCE.txt's recorded alp-sdk ref" in joined


def test_provenance_missing_is_caught() -> None:
    problems = find_problems(_parity_text(_A, _A), None)
    assert len(problems) == 1, problems
    assert "PROVENANCE.txt is missing" in problems[0], problems


def test_provenance_ref_line_absent_is_caught() -> None:
    problems = find_problems(_parity_text(_A, _A), "THE FROZEN alp-sdk PLANNER ORACLE\n\nboards 100\n")
    assert len(problems) == 1, problems
    assert "no 'alp-sdk ref' line" in problems[0], problems


def test_provenance_ref_malformed_is_caught() -> None:
    problems = find_problems(_parity_text(_A, _A), _provenance_text("eb96112b"))
    assert len(problems) == 1, problems
    assert "not a full 40-character commit" in problems[0], problems


def test_provenance_ref_line_wraps_a_valid_sha_in_prose_names_the_real_cause() -> None:
    """A sole candidate line that STARTS WITH 'alp-sdk ref' but wraps a
    genuinely valid 40-hex value in surrounding prose (no well-formed line
    exists anywhere else in the text) must not be told its value "is not a
    full 40-character commit" -- the extracted value plainly IS one, in
    isolation. The real defect is the line's SHAPE, not its value."""
    problems = find_problems(_parity_text(_A, _A), f"alp-sdk refs are recorded below as {_A}\n")
    assert len(problems) == 1, problems
    assert "not a full 40-character commit" not in problems[0], problems
    assert "does not match the required" in problems[0] and _A in problems[0], problems


def test_provenance_prose_mention_does_not_shadow_the_real_ref_line() -> None:
    """A prose sentence that happens to START WITH the literal words
    'alp-sdk ref' (summarising something else, embedding a DIFFERENT sha as
    a passing mention) must not be mistaken for the actual capture record --
    only the exact 'alp-sdk ref   <40-hex>' line counts. Measured against the
    naive first-match parse this replaces: a prose line embedding the FRESH
    ref, placed above the real (stale) capture-record line, made a genuinely
    diverged fixture report clean."""
    provenance_text = f"alp-sdk refs are recorded below as {_A}\n\nalp-sdk ref   {_B}\n"
    problems = find_problems(_parity_text(_A, _A), provenance_text)
    assert problems, "a prose decoy line hid the real, diverged PROVENANCE.txt ref"
    assert _B in "\n".join(problems), problems


def test_provenance_duplicated_ref_lines_are_caught_not_first_wins() -> None:
    """A duplicated real capture-record line -- fresh first, stale second --
    must be refused as a plural, the same way `_sole_match` already refuses
    one for both workflow pins, not silently resolved by picking whichever
    one happens to come first."""
    provenance_text = f"alp-sdk ref   {_A}\nalp-sdk ref   {_B}\n"
    problems = find_problems(_parity_text(_A, _A), provenance_text)
    assert problems, "a duplicated 'alp-sdk ref' line (fresh first, stale second) was silently accepted"
    assert any("well-formed 'alp-sdk ref' lines" in p for p in problems), problems


def test_pinned_sdk_tag_absent_is_caught() -> None:
    text = "env:\n  PINNED_PLANNER_ORACLE_SDK_REF: " + _A + "\n"
    problems = find_problems(text, _provenance_text(_A))
    assert len(problems) == 1, problems
    assert "expected exactly ONE PINNED_SDK_TAG" in problems[0] and "found 0" in problems[0], problems


def test_pinned_sdk_tag_duplicated_is_caught() -> None:
    text = f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_SDK_TAG: {_B}\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
    problems = find_problems(text, _provenance_text(_A))
    assert len(problems) == 1, problems
    assert "found 2" in problems[0], problems


def test_pinned_sdk_tag_hex_plus_tag_name_duplicate_is_caught() -> None:
    """tan-cli#897 round-3, item 1: a hex-only plural count misses a SECOND
    `PINNED_SDK_TAG:` whose value isn't hex. `<40-hex>` then `v0.16.0`: YAML
    last-key-wins makes the EFFECTIVE pin the tag name -- old code: `[]`."""
    text = f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a hex PINNED_SDK_TAG shadowed by a later tag-name declaration was silently accepted"
    assert any("found 2" in p and "PINNED_SDK_TAG" in p for p in problems), problems


def test_pinned_sdk_tag_tag_name_plus_hex_duplicate_is_caught_either_order() -> None:
    """Same hole, reverse order: `v0.16.0` then `<40-hex>` -- the old
    hex-only count still found exactly one match and returned it clean."""
    text = f"env:\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_SDK_TAG: {_A}\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a tag-name PINNED_SDK_TAG shadowed by a later hex declaration was silently accepted"
    assert any("found 2" in p and "PINNED_SDK_TAG" in p for p in problems), problems


def test_pinned_sdk_tag_hex_plus_commented_tag_name_duplicate_is_caught() -> None:
    """tan-cli#897 round-5: a bare `[ \\t]*$` tail does not match a line
    carrying a trailing `# comment`, so that line dropped out of the plural
    count entirely -- not "found 0" (the round-3 hole above), silently
    accepted as clean instead, with the STALE first hex returned as the
    verdict while YAML's own last-key-wins makes `v0.16.0` the effective
    pin. Same incident as the round-3 test above, wearing a comment."""
    text = (
        f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_SDK_TAG: v0.16.0  # stale\n"
        f"  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
    )
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a hex PINNED_SDK_TAG shadowed by a commented tag-name duplicate was silently accepted"
    assert any("found 2" in p and "PINNED_SDK_TAG" in p for p in problems), problems


def test_pinned_sdk_tag_sole_hex_with_trailing_comment_names_the_real_cause() -> None:
    """tan-cli#897 round-5 review, item 4: a SINGLE `PINNED_SDK_TAG: <40-hex>
    # note` declaration (no duplicate at all) reaches
    `_pinned_sdk_tag_state`'s tag-name branch, because ANY_RE's `(?:#.*)?`
    tail matches it (one ANY match, as required) while the strict
    `PINNED_SDK_TAG_RE` (deliberately comment-intolerant) does not -- so
    `hex_found` is empty even though the pin plainly IS a commit. Fail-safe
    before this fix (both a duplicate-count problem and this one are
    non-empty, so no regression), but the OLD diagnosis named the wrong
    cause: "PINNED_SDK_TAG is a release name (aaaa...(40-hex)...), not a
    40-hex commit" and told the reader to `git rev-parse` that same 40-hex
    value -- unactionable, since it already is one. The fix must name the
    trailing comment, not claim a release name."""
    text = f"env:\n  PINNED_SDK_TAG: {_A}  # tracks alp-sdk main\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a sole 40-hex PINNED_SDK_TAG with a trailing comment was silently accepted as a release name"
    joined = "\n".join(problems)
    assert "release name" not in joined, joined
    assert _A in joined and "comment" in joined, joined


def test_pinned_sdk_tag_as_a_release_name_is_not_reported_as_absent() -> None:
    """`PINNED_SDK_TAG` has legitimately held a release name -- not a 40-hex
    commit -- three times before (`v0.13.0`/`v0.14.0`/`v0.15.0-rc1`, commits
    `7ed5264c`/`c5dedc1c`/`f7cb325f`). A future re-pin back to that shape must
    not be reported as "found 0" (a real absence), which names the wrong
    cause -- the pin is right there, just not a commit."""
    text = "env:\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_PLANNER_ORACLE_SDK_REF: " + _A + "\n"
    problems = find_problems(text, _provenance_text(_A))
    assert not any("found 0" in p for p in problems), problems


def test_pinned_sdk_tag_as_a_release_name_still_catches_oracle_provenance_drift() -> None:
    """Even when `PINNED_SDK_TAG` cannot be compared directly (a release
    name, not a commit), the gate is not vacuous: `PINNED_PLANNER_ORACLE_SDK_REF`
    and `PROVENANCE.txt`'s own recorded ref are both still 40-hex and still
    checkable against EACH OTHER -- "cannot crash or silently pass"."""
    text = "env:\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_PLANNER_ORACLE_SDK_REF: " + _A + "\n"
    problems = find_problems(text, _provenance_text(_B))
    assert problems, "PINNED_SDK_TAG being a release name silently passed the whole gate"
    joined = "\n".join(problems)
    assert _A in joined and _B in joined, problems


def test_pinned_sdk_tag_as_a_release_name_cannot_bypass_the_gate_via_internal_agreement() -> None:
    """tan-cli#897's review finding, replayed exactly: PINNED_SDK_TAG is a
    release name, and PINNED_PLANNER_ORACLE_SDK_REF happens to agree with
    PROVENANCE.txt's own recorded ref -- but that internal agreement proves
    NOTHING, because `capture_planner_oracle.py` writes both together, so
    they agree even when BOTH are stale relative to the commit the tag now
    names. This exact state -- replayed against the real `6a826434~1` #884
    incident tree with only `PINNED_SDK_TAG` changed from `eb96112b` to its
    own tag name `v0.16.0` -- used to pass clean (`19 passed`); that was the
    silent bypass this test now closes. Must be a hard problem, not a free
    pass: a gate that goes quiet in the one state it cannot check is
    decoration."""
    text = "env:\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_PLANNER_ORACLE_SDK_REF: " + _A + "\n"
    problems = find_problems(text, _provenance_text(_A))
    assert problems, (
        "a tag-name PINNED_SDK_TAG with internally-agreeing "
        "PINNED_PLANNER_ORACLE_SDK_REF/PROVENANCE.txt silently bypassed the gate -- "
        "this is the tan-cli#884 incident shape"
    )
    joined = "\n".join(problems)
    assert "PINNED_SDK_TAG" in joined and "release name" in joined, problems
    assert _A in joined, problems


def test_pinned_sdk_tag_as_a_40hex_with_genuinely_agreeing_refs_stays_clean() -> None:
    """The ordinary green case is unaffected by the fix above: when
    PINNED_SDK_TAG IS a 40-hex commit and all three facts agree, the gate
    stays clean -- the new hard-failure branch is scoped to the
    tag-name-not-a-commit state only, never fired for an ordinary pin."""
    assert find_problems(_parity_text(_A, _A), _provenance_text(_A)) == []


def test_pinned_planner_oracle_ref_absent_is_caught() -> None:
    text = "env:\n  PINNED_SDK_TAG: " + _A + "\n"
    problems = find_problems(text, _provenance_text(_A))
    assert len(problems) == 1, problems
    assert (
        "expected exactly ONE PINNED_PLANNER_ORACLE_SDK_REF" in problems[0]
        and "found 0" in problems[0]
    ), problems


def test_pinned_planner_oracle_ref_malformed_is_silently_absent_not_guessed() -> None:
    """A truncated / non-hex value simply does not match the anchored regex
    -- it must be reported as ABSENT (found 0), never guessed at or matched
    partially. Guessing is exactly the failure `_sole_pin`'s plural-refusal
    exists to rule out for the sibling PINNED_SDK_TAG/PINNED_SDK_COMMIT
    pins; this is the same rule applied to the new one."""
    text = "env:\n  PINNED_SDK_TAG: " + _A + "\n  PINNED_PLANNER_ORACLE_SDK_REF: not-a-sha\n"
    problems = find_problems(text, _provenance_text(_A))
    assert any("PINNED_PLANNER_ORACLE_SDK_REF" in p and "found 0" in p for p in problems), problems


def test_pinned_planner_oracle_ref_hex_plus_malformed_duplicate_is_caught() -> None:
    """tan-cli#897 round-3, item 2: `_sole_match` counted only hex matches
    for `PINNED_PLANNER_ORACLE_SDK_REF` too, so a SECOND non-hex declaration
    was invisible whenever the hex-only count already found one match."""
    text = (
        f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
        "  PINNED_PLANNER_ORACLE_SDK_REF: not-a-sha\n"
    )
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a hex PINNED_PLANNER_ORACLE_SDK_REF shadowed by a malformed duplicate was silently accepted"
    assert any("found 2" in p and "PINNED_PLANNER_ORACLE_SDK_REF" in p for p in problems), problems


def test_pinned_planner_oracle_ref_uppercase_hex_duplicate_is_caught() -> None:
    """Same hole, re-cased: the lowercase-only hex pattern never matches the
    uppercase line, so a hex-only count still saw one match and passed."""
    text = (
        f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
        f"  PINNED_PLANNER_ORACLE_SDK_REF: {_A.upper()}\n"
    )
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a hex PINNED_PLANNER_ORACLE_SDK_REF shadowed by an uppercase duplicate was silently accepted"
    assert any("found 2" in p and "PINNED_PLANNER_ORACLE_SDK_REF" in p for p in problems), problems


def test_pinned_planner_oracle_ref_commented_duplicate_is_caught() -> None:
    """Same round-5 hole as the PINNED_SDK_TAG test above, for the other
    pin: a trailing `# comment` on the SECOND declaration dropped it out of
    the plural count entirely rather than reporting `found 0` -- the count
    stayed at 1 and the first hex was returned as clean."""
    text = (
        f"env:\n  PINNED_SDK_TAG: {_A}\n  PINNED_PLANNER_ORACLE_SDK_REF: {_A}\n"
        "  PINNED_PLANNER_ORACLE_SDK_REF: not-a-sha  # stale\n"
    )
    problems = find_problems(text, _provenance_text(_A))
    assert problems, "a hex PINNED_PLANNER_ORACLE_SDK_REF shadowed by a commented duplicate was silently accepted"
    assert any("found 2" in p and "PINNED_PLANNER_ORACLE_SDK_REF" in p for p in problems), problems


def test_the_retirement_marker_is_still_armed() -> None:
    """`POST_270_RETIRED` is what a future reader flips, BY HAND, the day
    tan-cli#270 actually lands in alp-sdk -- it must currently read `False`
    (the invariant is still live). This is not a claim that it can never
    change; it documents intent so a stray `True` cannot slip in silently
    and retire a still-live gate. Whoever lands tan-cli#270 updates this
    assertion alongside the marker."""
    assert POST_270_RETIRED is False, (
        "POST_270_RETIRED is True -- if tan-cli#270 has genuinely landed in "
        "alp-sdk, update this test alongside it; if not, someone flipped the "
        "marker by mistake and silently retired a still-live gate."
    )


def test_this_repositorys_own_regex_matches_its_real_pins() -> None:
    """Not an assertion that the two pins agree (the pytest test above
    already makes that one, unconditionally); only that the regex this
    module adds is still aimed at a real line in `parity.yml` -- the day
    `PINNED_PLANNER_ORACLE_SDK_REF:` is renamed or reshaped, this fails
    loudly here instead of `find_problems` silently reporting `found 0`
    forever."""
    text = _PARITY_WORKFLOW.read_text(encoding="utf-8")
    found = _PINNED_PLANNER_ORACLE_SDK_REF_RE.findall(text)
    assert len(found) == 1, (
        f"expected exactly ONE PINNED_PLANNER_ORACLE_SDK_REF in {_PARITY_WORKFLOW}, "
        f"found {len(found)}: {found}"
    )
