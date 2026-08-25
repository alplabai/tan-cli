# SPDX-License-Identifier: Apache-2.0
"""The pure matching logic for `test_planner_oracle_ref_tracks_pinned_sdk_tag.py`
(tan-cli#895/#897), split out by tan-cli#897 round-5 to keep the test file
under the house 800-line guideline (`_module_size_budget_core.py:51`) --
mirrors that same file's own split shape: a leading-underscore `_core` module
holds the regexes and the three-way comparison, the test file keeps the
module docstring, `POST_270_RETIRED` (a HAND-flipped marker, not mechanical
logic -- see its own comment in the test file for why it stays there) and
every `def test_*`.

Read `test_planner_oracle_ref_tracks_pinned_sdk_tag.py`'s own module
docstring for the full incident history and design rationale this module
implements; nothing here repeats it.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import _PINNED_SDK_TAG_RE

#: `PINNED_PLANNER_ORACLE_SDK_REF: <sha>` as a workflow-level `env:` entry --
#: same indentation shape as `PINNED_SDK_TAG`, so the same anchoring applies:
#: not anchored at column 0 (it is indented under `env:`), but anchored at
#: line start so a stray substring match elsewhere in the file cannot count.
_PINNED_PLANNER_ORACLE_SDK_REF_RE = re.compile(
    r"^[ \t]*PINNED_PLANNER_ORACLE_SDK_REF:[ \t]*([0-9a-f]{40})[ \t]*$", re.MULTILINE
)

#: Shape-agnostic twin of `_PINNED_PLANNER_ORACLE_SDK_REF_RE` -- a HEX-ONLY
#: plural count misses a second declaration whose value isn't 40-hex
#: (malformed, or re-cased); tan-cli#897 round-3. Also tolerates an optional
#: trailing `# comment` (tan-cli#897 round-5): a bare `[ \t]*$` tail does not
#: match a line carrying one, which silently dropped a shadowing SECOND
#: declaration out of the plural count instead of flagging it -- the exact
#: last-key-wins hazard this pattern exists to catch, just wearing a
#: comment. This pattern is deliberately still line-shape strict beyond
#: that: it does not tolerate a value split across lines or a `#` inside a
#: quoted value, neither of which this workflow's YAML uses. See
#: `_sole_match` below.
_PINNED_PLANNER_ORACLE_SDK_REF_ANY_RE = re.compile(
    r"^[ \t]*PINNED_PLANNER_ORACLE_SDK_REF:[ \t]*(\S+)[ \t]*(?:#.*)?$", re.MULTILINE
)

#: `PINNED_SDK_TAG: <value>` with NO shape constraint on the value -- unlike
#: `_PINNED_SDK_TAG_RE` (imported from `tests.conftest`, 40-hex only), this
#: matches ANY single-token value, so a legitimate tag-name pin
#: (`v0.16.0`, historically also `v0.13.0`/`v0.14.0`/`v0.15.0-rc1`) is found
#: rather than reported as absent. See `_pinned_sdk_tag_state` below for why
#: the distinction matters. Also tolerates an optional trailing `# comment`
#: (tan-cli#897 round-5), for the same last-key-wins reason given on
#: `_PINNED_PLANNER_ORACLE_SDK_REF_ANY_RE` above -- the HEX-only
#: `_PINNED_SDK_TAG_RE` (imported from `tests.conftest`) this pairs with in
#: `_pinned_sdk_tag_state` is deliberately NOT given the same tolerance and
#: still re-scans the raw text on its own, unfixed `[ \t]*$` tail: a SINGLE
#: commented hex declaration still reports as non-hex there (`(None, None)`,
#: the same "legitimate tag name" state a real tag-name value gets, per
#: that function's own docstring) -- unchanged from before this pattern's
#: fix, and not a regression, since duplicate detection above now catches
#: the dangerous shadowing case before this ambiguity is ever reached.
_PINNED_SDK_TAG_ANY_RE = re.compile(
    r"^[ \t]*PINNED_SDK_TAG:[ \t]*(\S+)[ \t]*(?:#.*)?$", re.MULTILINE
)

#: The only place the frozen oracle fixture's own capture ref is written down.
_PROVENANCE = Path(__file__).resolve().parents[1] / "fixtures" / "planner_oracle" / "PROVENANCE.txt"


def _sole_match(
    text: str, any_pattern: re.Pattern[str], hex_pattern: re.Pattern[str], name: str
) -> tuple[str | None, str | None]:
    """`(sha, None)` when `text` declares exactly one `name` and its value is a
    well-formed 40-hex commit, else `(None, <what went wrong>)`.

    A local, string-in-string-out twin of `tests.conftest._sole_pin` --
    not a call to that helper, deliberately: it takes a `Path` and its
    failure messages embed `path`, so reusing it against the synthetic
    fixtures in the sibling test file would mean constructing a fake `Path`
    for every case just to satisfy a signature this module does not
    otherwise need. Same refusal-of-a-plural-match behaviour (`parity.yml`'s
    own grep learned that the expensive way -- see `_sole_pin`'s docstring),
    applied to text already in hand.

    Counts with the shape-agnostic `any_pattern` FIRST, refusing a plural on
    THAT count before ever looking at `hex_pattern` -- a hex-only count is
    shape-blind: a first cut of this helper returned as soon as the hex
    pattern found one match, missing a SECOND `name:` declaration whose
    value merely wasn't 40-hex (tan-cli#897 round-3 review).
    """
    any_found = any_pattern.findall(text)
    if len(any_found) != 1:
        return None, f"expected exactly ONE {name}, found {len(any_found)}: {any_found}"
    hex_found = hex_pattern.findall(text)
    if len(hex_found) == 1:
        return hex_found[0], None
    # Present exactly once (the any_pattern count above already proved
    # that), but its value does not match the hex pattern -- reported the
    # same way a genuine absence is, matching this helper's pre-existing
    # "found 0" contract for a malformed/non-hex value.
    return None, f"expected exactly ONE {name}, found 0: []"


def _pinned_sdk_tag_state(parity_yml_text: str) -> tuple[str | None, str | None]:
    """`(hex_tag, problem)` for `PINNED_SDK_TAG`, with a THIRD, silent state:
    `(None, None)` when the pin is present exactly once but its value is not
    a 40-hex commit -- a legitimate, historically-real shape
    (`PINNED_SDK_TAG` has held `v0.13.0`, `v0.14.0`, and `v0.15.0-rc1` --
    commits `7ed5264c`, `c5dedc1c`, `f7cb325f`) that this gate must not
    mistake for absence.

    Reusing `_sole_match(text, _PINNED_SDK_TAG_ANY_RE, _PINNED_SDK_TAG_RE, ...)`
    directly here would report that state as "expected exactly ONE
    PINNED_SDK_TAG, found 0" -- technically true of the HEX-ONLY pattern, but
    naming the wrong cause to a reader: nothing is missing, the pin just is
    not (right now) a commit SHA. `conftest.sdk_pin_disagreements` hits the
    identical ambiguity for the same regex and resolves it by treating a
    `_sole_pin` miss as a non-fatal WARNING, never an assertion failure --
    this mirrors that judgement call rather than escalating it to a hard
    failure this test would then have to explain away.

    Counts with the shape-agnostic `_PINNED_SDK_TAG_ANY_RE` FIRST, and
    refuses a plural on THAT count before ever branching on hex-shape --
    tan-cli#897's round-3 review found that an earlier version of this
    function returned as soon as the HEX-ONLY count found exactly one match,
    without ever checking whether a second `PINNED_SDK_TAG:` declaration
    existed whose value was not 40-hex. That made a second declaration
    invisible whenever the FIRST hex count was already 1 -- e.g.
    `PINNED_SDK_TAG: <40-hex>` followed by `PINNED_SDK_TAG: v0.16.0` (either
    order): YAML last-key-wins makes the effective pin the tag name, exactly
    the state this gate exists to hard-fail, and the old code reported `[]`.
    """
    any_found = _PINNED_SDK_TAG_ANY_RE.findall(parity_yml_text)
    if len(any_found) != 1:
        return None, f"expected exactly ONE PINNED_SDK_TAG, found {len(any_found)}: {any_found}"
    hex_found = _PINNED_SDK_TAG_RE.findall(parity_yml_text)
    if len(hex_found) == 1:
        return hex_found[0], None
    # Present exactly once (the any-pattern count above already proved
    # that), just not 40-hex -- the legitimate tag-name state.
    return None, None


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
    if re.fullmatch(r"[0-9a-f]{40}", malformed):
        # A genuine 40-hex value WAS extracted -- the value itself is not the
        # problem, the shape around it is (extra prose words before/after the
        # "alp-sdk ref" phrase, e.g. "alp-sdk refs are recorded below as
        # <sha>"). Naming the value "not a full 40-character commit" here
        # would be wrong: it plainly is one, in isolation.
        return None, (
            f"'alp-sdk ref' line {candidates[0]!r} has a 40-hex-looking last "
            f"token ({malformed!r}) but does not match the required "
            "'alp-sdk ref <40-hex>' shape exactly -- extra surrounding words "
            "make the line unparseable, not the value itself"
        )
    return None, (
        f"'alp-sdk ref' line names {malformed!r}, not a full 40-character commit -- "
        "an abbreviated or malformed ref cannot be compared unambiguously"
    )


def find_problems(parity_yml_text: str, provenance_text: str | None) -> list[str]:
    """Every way the three-way pin above disagrees, as ready-to-print lines.
    Empty means agreement -- which requires `PINNED_SDK_TAG` to be a 40-hex
    commit this gate can actually verify, not merely that the other two facts
    happen to agree with each other. A release-name `PINNED_SDK_TAG` NEVER
    returns empty, even when `PINNED_PLANNER_ORACLE_SDK_REF` and
    `PROVENANCE.txt`'s recorded ref agree with each other (see
    `test_planner_oracle_ref_tracks_pinned_sdk_tag.py`'s own module
    docstring, "The tag-name blind spot" section, for why that internal
    agreement is not proof of lockstep).

    A pure function of its two text arguments, deliberately: no filesystem
    read and no pytest fixture inside it, so the sibling test file's
    negative self-tests can plant every failure shape (equal / diverged /
    missing PROVENANCE.txt / a malformed or absent ref line / a malformed or
    duplicated workflow pin / a tag-name PINNED_SDK_TAG) as an in-memory
    string and prove the detector actually fires, rather than asserting on
    this repository's own -- currently clean -- files alone.
    """
    problems: list[str] = []

    tag, tag_problem = _pinned_sdk_tag_state(parity_yml_text)
    if tag_problem is not None:
        problems.append(tag_problem)
    # `tag is None and tag_problem is None` is the legitimate tag-name state
    # (see `_pinned_sdk_tag_state`): PINNED_SDK_TAG holding a release name is
    # not itself an error -- it is a real, repeated historical shape
    # (v0.13.0/v0.14.0/v0.15.0-rc1). But the LOCKSTEP COMPARISON this module
    # exists to make is a hard failure in this state too (see below): this
    # gate has no checkout and no network with which to resolve a tag name to
    # the commit it names, so it cannot verify the one invariant it exists to
    # enforce, and reporting that as clean is the tan-cli#884 incident itself
    # (see `find_problems`'s docstring).
    tag_is_unresolvable_tag_name = tag is None and tag_problem is None

    oracle_ref, oracle_problem = _sole_match(
        parity_yml_text,
        _PINNED_PLANNER_ORACLE_SDK_REF_ANY_RE,
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
            "code had already moved past the frozen goldens. NOTE for whoever lands "
            "tan-cli#270 (deletes scripts/alp_orchestrate/ from alp-sdk): if THAT "
            "is why this fired, the fix is not to chase PINNED_SDK_TAG with a "
            "capture_planner_oracle.py run against a ref that no longer ships a "
            "planner -- it is to flip POST_270_RETIRED to True in "
            "test_planner_oracle_ref_tracks_pinned_sdk_tag.py instead. THREE "
            "sites move together, not one: (1) POST_270_RETIRED in "
            "test_planner_oracle_ref_tracks_pinned_sdk_tag.py, "
            "(2) test_the_retirement_marker_is_still_armed in that same file, "
            "and (3) the node-id grep in parity.yml's \"python/tests/gates\" "
            "step (search `test_pinned_planner_oracle_ref_tracks_pinned_sdk_tag "
            "PASSED`) -- flipping the marker turns that test's result from "
            "PASSED to SKIPPED, and the grep hard-fails the job on exactly "
            "that SKIP unless its `did not PASS` block is updated (or "
            "removed) in the same change."
        )

    if tag is not None and provenance_ref is not None and tag != provenance_ref:
        problems.append(
            "tests/fixtures/planner_oracle/PROVENANCE.txt's recorded alp-sdk ref "
            f"({provenance_ref}) does not match parity.yml's PINNED_SDK_TAG ({tag}) "
            "-- the checked-in fixture and the workflow pin disagree about which "
            "alp-sdk commit the frozen oracle bytes were captured from."
        )

    if tag_is_unresolvable_tag_name and oracle_ref is not None and provenance_ref is not None:
        # PINNED_SDK_TAG is currently a release name, not a commit, so it
        # cannot be compared directly. Two sub-cases, both a HARD problem --
        # not a free pass, and not a warn-only bypass either (tan-cli#897's
        # review): a gate that goes quiet in the one state it cannot check is
        # decoration, and this gate's own stated design already rejects a
        # written-down exemption for this pin (see
        # test_planner_oracle_ref_tracks_pinned_sdk_tag.py's own module
        # docstring).
        #
        # `_pinned_sdk_tag_state` discards the tag NAME (returns `(None,
        # None)`); re-extracted here so the messages below can echo the real
        # value instead of a hardcoded example. Safe unguarded:
        # `tag_is_unresolvable_tag_name` implies exactly one ANY match.
        tag_name = _PINNED_SDK_TAG_ANY_RE.findall(parity_yml_text)[0]
        if oracle_ref != provenance_ref:
            # The workflow's own oracle pin and the fixture's own recorded
            # capture ref are both still 40-hex, and are still checkable
            # against EACH OTHER regardless of what PINNED_SDK_TAG holds
            # right now.
            problems.append(
                f"parity.yml's PINNED_SDK_TAG ({tag_name}) is not currently a 40-hex "
                "commit (a release name), so it cannot be lockstep-compared directly -- "
                f"but PINNED_PLANNER_ORACLE_SDK_REF ({oracle_ref}) and "
                f"tests/fixtures/planner_oracle/PROVENANCE.txt's recorded alp-sdk ref "
                f"({provenance_ref}) still disagree with EACH OTHER, which is checkable "
                "independent of PINNED_SDK_TAG's current form."
            )
        else:
            # The blind spot tan-cli#897's review found: oracle_ref and
            # provenance_ref AGREE, but that agreement proves nothing here --
            # scripts/capture_planner_oracle.py writes both together, so they
            # always agree even when BOTH are stale relative to the commit
            # PINNED_SDK_TAG now names. This is the exact #884 incident shape,
            # reproduced verbatim against 6a826434~1's tree: re-pinning
            # PINNED_SDK_TAG from eb96112b to its own tag name v0.16.0, with
            # PINNED_PLANNER_ORACLE_SDK_REF and PROVENANCE.txt both left at
            # the stale 94378a05, passed clean before this branch. The
            # lockstep invariant this gate exists to enforce is simply
            # UNVERIFIABLE in this state, and an unverifiable state is
            # reported as a problem, not silently accepted as one.
            problems.append(
                f"parity.yml's PINNED_SDK_TAG is a release name ({tag_name}), not a "
                "40-hex commit -- this gate has no alp-sdk checkout and makes no network "
                "call, so it cannot resolve the tag to a commit and cannot verify that "
                f"PINNED_PLANNER_ORACLE_SDK_REF ({oracle_ref}) actually matches what "
                "PINNED_SDK_TAG names. PINNED_PLANNER_ORACLE_SDK_REF and "
                "tests/fixtures/planner_oracle/PROVENANCE.txt's recorded alp-sdk ref "
                "agree with EACH OTHER, but that alone does not prove lockstep: "
                "scripts/capture_planner_oracle.py writes both together, so they agree "
                "even when BOTH are stale relative to the commit PINNED_SDK_TAG now names "
                "(the tan-cli#884 incident shape -- see "
                "test_planner_oracle_ref_tracks_pinned_sdk_tag.py's own module "
                "docstring). Resolve "
                f"PINNED_SDK_TAG to the 40-hex commit it names (e.g. `git rev-parse "
                f"{tag_name}` against an alp-sdk checkout) so this gate can actually make "
                "the comparison."
            )

    return problems
