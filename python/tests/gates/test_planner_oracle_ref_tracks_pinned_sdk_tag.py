# SPDX-License-Identifier: Apache-2.0
"""Gate: `PINNED_PLANNER_ORACLE_SDK_REF` moves in lockstep with
`PINNED_SDK_TAG` (tan-cli#895).

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
after PR #884 had already been signed off twice. `grep -rn
PINNED_PLANNER_ORACLE_SDK_REF` found exactly three hits, all in
`parity.yml` itself (the postmortem comment, the definition, the checkout
`ref:`); nothing compared it to `PINNED_SDK_TAG`, and `pin-move-verify.yml`
never mentioned it either.

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

## The post-#270 guard

tan-cli#270 deletes `scripts/alp_orchestrate/` from alp-sdk. After that
lands, `PINNED_PLANNER_ORACLE_SDK_REF` is meant to freeze for good -- there
is no later ref that still ships a planner to capture, so nothing could
ever again make it equal a moving `PINNED_SDK_TAG`. A gate that kept
asserting equality past that point would be permanently red for a state the
design accepts on purpose.

Guarded the same way `test_planner_parity_actually_ran.py` guards the
sibling byte-parity layer: on whether the bound `ALP_SDK_ROOT` checkout
still ships `scripts/alp_orchestrate/__init__.py`, not on a hand-maintained
flag someone has to remember to flip. Nothing bound -- the ordinary local
`pytest tests -q` and `ci.yml`'s `python` job -- skips, same as every other
gate in this file that needs a live alp-sdk tree. A bound tree that has lost
the package skips too, and that skip IS the retirement: once #270 actually
lands, this assertion stops firing on its own, with no second commit
required to silence it.

Run locally (with a live alp-sdk checkout bound, to actually exercise it):

    ALP_SDK_ROOT=<path-to-alp-sdk> python -m pytest \
        tests/gates/test_planner_oracle_ref_tracks_pinned_sdk_tag.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import _PARITY_WORKFLOW, _PINNED_SDK_TAG_RE, sdk_root

#: `PINNED_PLANNER_ORACLE_SDK_REF: <sha>` as a workflow-level `env:` entry --
#: same indentation shape as `PINNED_SDK_TAG`, so the same anchoring applies:
#: not anchored at column 0 (it is indented under `env:`), but anchored at
#: line start so a stray substring match elsewhere in the file cannot count.
_PINNED_PLANNER_ORACLE_SDK_REF_RE = re.compile(
    r"^[ \t]*PINNED_PLANNER_ORACLE_SDK_REF:[ \t]*([0-9a-f]{40})[ \t]*$", re.MULTILINE
)

#: The only place the frozen oracle fixture's own capture ref is written down.
_PROVENANCE = Path(__file__).resolve().parents[1] / "fixtures" / "planner_oracle" / "PROVENANCE.txt"

#: What `scripts/alp_orchestrate/__init__.py`'s ABSENCE from a bound alp-sdk
#: checkout means: tan-cli#270 has landed there, and the ref this module cares
#: about has no live counterpart left to track. Mirrors
#: `test_planner_parity_actually_ran.py`'s `ORCHESTRATOR_PACKAGE`.
_ORCHESTRATOR_PACKAGE = ("scripts", "alp_orchestrate", "__init__.py")


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


def _provenance_ref(text: str) -> tuple[str | None, str | None]:
    """`(ref, None)` for a well-formed `alp-sdk ref <40-hex>` line, else
    `(None, <what went wrong>)`.

    Mirrors `test_planner_oracle_regression.py::test_the_bound_checkout_is_
    the_ref_the_fixture_was_captured_from`'s own parse -- a third,
    independent implementation of the same three lines would be exactly the
    kind of drift-prone duplication this file exists to catch elsewhere.
    """
    for line in text.splitlines():
        if line.startswith("alp-sdk ref"):
            candidate = line.split()[-1]
            if not re.fullmatch(r"[0-9a-f]{40}", candidate):
                return None, (
                    f"PROVENANCE.txt's 'alp-sdk ref' line names {candidate!r}, not a "
                    "full 40-character commit -- an abbreviated or malformed ref "
                    "cannot be compared unambiguously"
                )
            return candidate, None
    return None, "PROVENANCE.txt has no 'alp-sdk ref' line"


def find_problems(parity_yml_text: str, provenance_text: str | None) -> list[str]:
    """Every way the three-way pin above disagrees, as ready-to-print lines.
    Empty means agreement.

    A pure function of its two text arguments, deliberately: no filesystem
    read and no pytest fixture inside it, so the tests below can plant every
    failure shape (equal / diverged / missing PROVENANCE.txt / a malformed or
    absent ref line / a malformed or duplicated workflow pin) as an in-memory
    string and prove the detector actually fires, rather than asserting on
    this repository's own -- currently clean -- files alone.
    """
    problems: list[str] = []

    tag, tag_problem = _sole_match(parity_yml_text, _PINNED_SDK_TAG_RE, "PINNED_SDK_TAG")
    if tag_problem is not None:
        problems.append(tag_problem)

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
            problems.append(
                f"tests/fixtures/planner_oracle/PROVENANCE.txt: {provenance_problem}"
            )

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

    return problems


def _oracle_is_still_trackable(sdk: Path | None) -> bool:
    """True only while a bound `sdk` still ships the planner alp-sdk-side.

    False both when nothing is bound (the ordinary run) and when alp-sdk has
    retired `scripts/alp_orchestrate/` (tan-cli#270 landed) -- in the second
    case there is no live tree left to say whether
    PINNED_PLANNER_ORACLE_SDK_REF SHOULD still equal PINNED_SDK_TAG, so the
    caller skips rather than asserting a design invariant that no longer
    applies.
    """
    if sdk is None:
        return False
    return sdk.joinpath(*_ORCHESTRATOR_PACKAGE).is_file()


#: Read at MODULE IMPORT TIME, like every other `ALP_SDK_ROOT` consumer in
#: this directory -- `tests/conftest.py`'s autouse `_scrub_sdk_discovery_env`
#: deletes the variable from the process environment before any test body
#: runs, so a call made from inside a test always sees `None` and always
#: skips (tan-cli#275's exact failure mode, re-learned once already by
#: `test_planner_relocation_freshness.py`).
SDK = sdk_root()


def test_pinned_planner_oracle_ref_tracks_pinned_sdk_tag() -> None:
    """The real thing this gate exists to guard, against this checkout's
    OWN `parity.yml` and `PROVENANCE.txt`, right now."""
    if not _oracle_is_still_trackable(SDK):
        pytest.skip(
            "no ALP_SDK_ROOT bound, or the bound checkout no longer ships "
            f"{'/'.join(_ORCHESTRATOR_PACKAGE)} (tan-cli#270 has landed there) -- "
            "PINNED_PLANNER_ORACLE_SDK_REF is then permanently frozen and this "
            "lockstep invariant no longer applies"
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


def test_the_post_270_case_is_not_trackable() -> None:
    """Once alp-sdk retires `scripts/alp_orchestrate/`, a bound checkout no
    longer answers the question this gate asks -- `_oracle_is_still_
    trackable` must say so, which is what lets the pytest-level assertion
    skip instead of asserting a design invariant the postmortem itself says
    stops applying past that point."""
    assert _oracle_is_still_trackable(None) is False


def test_the_post_270_case_is_not_trackable_even_with_a_bound_but_retired_checkout(
    tmp_path: Path,
) -> None:
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("# stand-in\n", encoding="utf-8")
    # Deliberately no scripts/alp_orchestrate/__init__.py -- the retired shape.
    assert _oracle_is_still_trackable(sdk) is False


def test_a_bound_checkout_that_still_ships_the_planner_is_trackable(tmp_path: Path) -> None:
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts" / "alp_orchestrate").mkdir(parents=True)
    (sdk / "scripts" / "alp_orchestrate" / "__init__.py").write_text("", encoding="utf-8")
    assert _oracle_is_still_trackable(sdk) is True


def test_this_repositorys_own_regex_matches_its_real_pins() -> None:
    """Not an assertion that the two pins agree (the pytest test above
    already makes that one, guarded); only that the regex this module adds
    is still aimed at a real line in `parity.yml` -- the day
    `PINNED_PLANNER_ORACLE_SDK_REF:` is renamed or reshaped, this fails
    loudly here instead of `find_problems` silently reporting `found 0`
    forever."""
    text = _PARITY_WORKFLOW.read_text(encoding="utf-8")
    found = _PINNED_PLANNER_ORACLE_SDK_REF_RE.findall(text)
    assert len(found) == 1, (
        f"expected exactly ONE PINNED_PLANNER_ORACLE_SDK_REF in {_PARITY_WORKFLOW}, "
        f"found {len(found)}: {found}"
    )
