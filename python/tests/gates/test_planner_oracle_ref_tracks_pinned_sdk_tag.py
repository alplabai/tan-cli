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

import re
from pathlib import Path

import pytest

from tests.conftest import _PARITY_WORKFLOW, _PINNED_SDK_TAG_RE

#: `PINNED_PLANNER_ORACLE_SDK_REF: <sha>` as a workflow-level `env:` entry --
#: same indentation shape as `PINNED_SDK_TAG`, so the same anchoring applies:
#: not anchored at column 0 (it is indented under `env:`), but anchored at
#: line start so a stray substring match elsewhere in the file cannot count.
_PINNED_PLANNER_ORACLE_SDK_REF_RE = re.compile(
    r"^[ \t]*PINNED_PLANNER_ORACLE_SDK_REF:[ \t]*([0-9a-f]{40})[ \t]*$", re.MULTILINE
)

#: `PINNED_SDK_TAG: <value>` with NO shape constraint on the value -- unlike
#: `_PINNED_SDK_TAG_RE` (imported from `tests.conftest`, 40-hex only), this
#: matches ANY single-token value, so a legitimate tag-name pin
#: (`v0.16.0`, historically also `v0.13.0`/`v0.14.0`/`v0.15.0-rc1`) is found
#: rather than reported as absent. See `_pinned_sdk_tag_state` below for why
#: the distinction matters.
_PINNED_SDK_TAG_ANY_RE = re.compile(r"^[ \t]*PINNED_SDK_TAG:[ \t]*(\S+)[ \t]*$", re.MULTILINE)

#: The only place the frozen oracle fixture's own capture ref is written down.
_PROVENANCE = Path(__file__).resolve().parents[1] / "fixtures" / "planner_oracle" / "PROVENANCE.txt"

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


def _sole_match(text: str, pattern: re.Pattern[str], name: str) -> tuple[str | None, str | None]:
    """`(sha, None)` when `text` declares exactly one `name`, else `(None,
    <what went wrong>)`.

    A local, string-in-string-out twin of `tests.conftest._sole_pin` --
    not a call to that helper, deliberately: it takes a `Path` and its
    failure messages embed `path`, so reusing it against the synthetic
    fixtures below would mean constructing a fake `Path` for every case
    just to satisfy a signature this module does not otherwise need. Same
    refusal-of-a-plural-match behaviour (`parity.yml`'s own grep learned
    that the expensive way -- see `_sole_pin`'s docstring), applied to text
    already in hand.
    """
    found = pattern.findall(text)
    if len(found) != 1:
        return None, f"expected exactly ONE {name}, found {len(found)}: {found}"
    return found[0], None


def _pinned_sdk_tag_state(parity_yml_text: str) -> tuple[str | None, str | None]:
    """`(hex_tag, problem)` for `PINNED_SDK_TAG`, with a THIRD, silent state:
    `(None, None)` when the pin is present exactly once but its value is not
    a 40-hex commit -- a legitimate, historically-real shape
    (`PINNED_SDK_TAG` has held `v0.13.0`, `v0.14.0`, and `v0.15.0-rc1` --
    commits `7ed5264c`, `c5dedc1c`, `f7cb325f`) that this gate must not
    mistake for absence.

    Reusing `_sole_match(text, _PINNED_SDK_TAG_RE, ...)` directly here would
    report that state as "expected exactly ONE PINNED_SDK_TAG, found 0" --
    technically true of the HEX-ONLY pattern, but naming the wrong cause to a
    reader: nothing is missing, the pin just is not (right now) a commit SHA.
    `conftest.sdk_pin_disagreements` hits the identical ambiguity for the
    same regex and resolves it by treating a `_sole_pin` miss as a
    non-fatal WARNING, never an assertion failure -- this mirrors that
    judgement call rather than escalating it to a hard failure this test
    would then have to explain away.
    """
    hex_found = _PINNED_SDK_TAG_RE.findall(parity_yml_text)
    if len(hex_found) == 1:
        return hex_found[0], None
    if len(hex_found) > 1:
        return None, f"expected exactly ONE PINNED_SDK_TAG, found {len(hex_found)}: {hex_found}"
    any_found = _PINNED_SDK_TAG_ANY_RE.findall(parity_yml_text)
    if len(any_found) == 1:
        # Present once, just not 40-hex -- the legitimate tag-name state.
        return None, None
    if len(any_found) == 0:
        return None, "expected exactly ONE PINNED_SDK_TAG, found 0: []"
    return None, f"expected exactly ONE PINNED_SDK_TAG, found {len(any_found)}: {any_found}"


def _provenance_ref(text: str) -> tuple[str | None, str | None]:
    """`(ref, None)` for exactly one well-formed `alp-sdk ref <40-hex>` line,
    else `(None, <what went wrong>)`.

    Two traps a naive "take the first line starting with 'alp-sdk ref'"
    parse falls into, both measured against this file's own PROVENANCE.txt
    shape:

      * A PROSE sentence -- e.g. "alp-sdk refs are recorded below as
        <sha>" -- also starts with the literal substring "alp-sdk ref" (the
        plural `s` lands immediately after it). Taking the first line that
        merely STARTS WITH that substring lets such a sentence shadow the
        REAL capture-record line beneath it: the prose's own embedded sha
        wins the comparison, the actual (possibly stale) recorded ref is
        never read, and a genuinely diverged fixture reports clean.
      * A DUPLICATED real line -- the capture record appended twice, fresh
        first / stale second (or the reverse) -- taking the first match
        again silently picks one arbitrarily, instead of refusing the
        plural the way `_sole_match` above already refuses one for both
        `parity.yml` pins.

    Fixed by requiring the WHOLE stripped line to match `alp-sdk ref` +
    whitespace + exactly 40 hex characters before it counts as a REF (a
    prose mention with different or extra trailing text never clears that
    bar), and by collecting every line that at least STARTS WITH the phrase
    so a plural or a malformed lone candidate is reported instead of
    resolved by picking the first one found.
    """
    candidates = [line.strip() for line in text.splitlines() if line.strip().startswith("alp-sdk ref")]
    well_formed = [
        match.group(1)
        for match in (re.fullmatch(r"alp-sdk ref\s+([0-9a-f]{40})", line) for line in candidates)
        if match is not None
    ]
    if len(well_formed) == 1:
        return well_formed[0], None
    if len(well_formed) > 1:
        return None, (
            f"declares {len(well_formed)} well-formed 'alp-sdk ref' lines "
            f"({well_formed}) -- exactly one is required, and taking the first "
            "would silently pick one of them arbitrarily"
        )
    if not candidates:
        return None, "has no 'alp-sdk ref' line"
    malformed = candidates[0].split()[-1] if candidates[0].split() else candidates[0]
    return None, (
        f"'alp-sdk ref' line names {malformed!r}, not a full 40-character commit -- "
        "an abbreviated or malformed ref cannot be compared unambiguously"
    )


def find_problems(parity_yml_text: str, provenance_text: str | None) -> list[str]:
    """Every way the three-way pin above disagrees, as ready-to-print lines.
    Empty means agreement.

    A pure function of its two text arguments, deliberately: no filesystem
    read and no pytest fixture inside it, so the tests below can plant every
    failure shape (equal / diverged / missing PROVENANCE.txt / a malformed or
    absent ref line / a malformed or duplicated workflow pin / a tag-name
    PINNED_SDK_TAG) as an in-memory string and prove the detector actually
    fires, rather than asserting on this repository's own -- currently clean
    -- files alone.
    """
    problems: list[str] = []

    tag, tag_problem = _pinned_sdk_tag_state(parity_yml_text)
    if tag_problem is not None:
        problems.append(tag_problem)
    # `tag is None and tag_problem is None` is the legitimate tag-name state
    # (see `_pinned_sdk_tag_state`): not an error, but also not silently
    # treated as "nothing to check" -- the oracle-ref-vs-PROVENANCE.txt
    # comparison below still runs regardless of whether `tag` resolved.
    tag_is_unresolvable_tag_name = tag is None and tag_problem is None

    oracle_ref, oracle_problem = _sole_match(
        parity_yml_text,
        _PINNED_PLANNER_ORACLE_SDK_REF_RE,
        "PINNED_PLANNER_ORACLE_SDK_REF",
    )
    if oracle_problem is not None:
        problems.append(oracle_problem)

    if provenance_text is None:
        problems.append(
            "tests/fixtures/planner_oracle/PROVENANCE.txt is missing -- the frozen "
            "oracle fixture has no recorded alp-sdk ref to compare against "
            "PINNED_SDK_TAG / PINNED_PLANNER_ORACLE_SDK_REF"
        )
        provenance_ref: str | None = None
    else:
        provenance_ref, provenance_problem = _provenance_ref(provenance_text)
        if provenance_problem is not None:
            problems.append(f"tests/fixtures/planner_oracle/PROVENANCE.txt: {provenance_problem}")

    if tag is not None and oracle_ref is not None and tag != oracle_ref:
        problems.append(
            "parity.yml's PINNED_PLANNER_ORACLE_SDK_REF "
            f"({oracle_ref}) has drifted from PINNED_SDK_TAG ({tag}). tan-cli#509's "
            "own postmortem: this pin moves in lockstep with PINNED_SDK_TAG until "
            "#270 lands. Regenerate tests/fixtures/planner_oracle (`python "
            "scripts/capture_planner_oracle.py --sdk <checkout> --sdk-ref "
            f"{tag}`) and re-pin PINNED_PLANNER_ORACLE_SDK_REF to {tag} -- otherwise "
            "this is the tan-cli#509 incident recurring: PR #884's merge-ref CI "
            "failed 19 cases across 19 boards on all three OSes after two review "
            "sign-offs, because GitHub tests the merge ref and tan/planner's LIVE "
            "code had already moved past the frozen goldens."
        )

    if tag is not None and provenance_ref is not None and tag != provenance_ref:
        problems.append(
            "tests/fixtures/planner_oracle/PROVENANCE.txt's recorded alp-sdk ref "
            f"({provenance_ref}) does not match parity.yml's PINNED_SDK_TAG ({tag}) "
            "-- the checked-in fixture and the workflow pin disagree about which "
            "alp-sdk commit the frozen oracle bytes were captured from."
        )

    if (
        tag_is_unresolvable_tag_name
        and oracle_ref is not None
        and provenance_ref is not None
        and oracle_ref != provenance_ref
    ):
        # PINNED_SDK_TAG is currently a release name, not a commit, so it
        # cannot be compared directly -- but this is not a free pass: the
        # workflow's own oracle pin and the fixture's own recorded capture
        # ref are both still 40-hex, and are still checkable against EACH
        # OTHER regardless of what PINNED_SDK_TAG holds right now.
        problems.append(
            "parity.yml's PINNED_SDK_TAG is not currently a 40-hex commit (a release "
            "name, e.g. v0.16.0), so it cannot be lockstep-compared directly -- but "
            f"PINNED_PLANNER_ORACLE_SDK_REF ({oracle_ref}) and "
            f"tests/fixtures/planner_oracle/PROVENANCE.txt's recorded alp-sdk ref "
            f"({provenance_ref}) still disagree with EACH OTHER, which is checkable "
            "independent of PINNED_SDK_TAG's current form."
        )

    return problems


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


def test_pinned_sdk_tag_as_a_release_name_is_clean_when_oracle_and_provenance_agree() -> None:
    text = "env:\n  PINNED_SDK_TAG: v0.16.0\n  PINNED_PLANNER_ORACLE_SDK_REF: " + _A + "\n"
    assert find_problems(text, _provenance_text(_A)) == []


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
