# SPDX-License-Identifier: Apache-2.0
"""The empty-collection half of tan-cli#1145: a gate that cannot fail because
its loop never runs (2 of 2).

Four gates whose negative half could not fail have been found in this repo,
each one by accident during a review of something else. tan-cli#1145 asks for
two mechanical gates. The OTHER one -- an `if:` key must be compared with
`==`, never searched with `in` -- ships in PR #1168, which tan-cli#1145 says
to build first; nothing about it is here.

This file holds the part of the empty-collection gate that is decidable
without executing anything, and every negative control for the part that is
not.

## What this file enforces, exactly

1. **No tautological assertion** (`assert True`, `assert x == x`) and **no
   assertion swallowed by its own `try`**, anywhere under
   `python/{tan,tests,scripts}`. The tan-cli#1145 sweep found ZERO of each;
   both are carried as regression insurance because both are trivially
   `ast`-decidable and cost nothing.
2. **Every `ALLOWED_EMPTY_LOOPS` / `ALLOWED_ZERO_ASSERT_FILES` row still names
   something real, carries a classified reason, and cites no line number.**
   This is the STRUCTURAL staleness half of the coverage gate: a row whose
   loop was deleted or reworded reds here, in a leg that needs no coverage
   run.
3. **The negative controls and floors for the coverage gate itself**, driven
   with fabricated input so they need no coverage run either.
4. **That the coverage audit is still wired into CI, still able to fail, and
   still binds the one variable its child run needs.**

## What this file does NOT enforce, and where that lives

The never-iterating-loop measurement needs execution data, so it lives in
`scripts/audit_vacuous_gate_shapes.py`, wired into `ci.yml`'s `python` job and
pinned by `test_the_coverage_audit_is_wired_into_ci` below. Two disclosures
rather than an implication:

* That job is deliberately NOT a required status context -- `ci.yml`'s BRANCH
  PROTECTION block says "`python` -- this file's own job below -- is
  deliberately NOT required". A never-iterating loop therefore reds VISIBLY on
  the PR but does not block the merge. The structural half in THIS file does
  block, via `parity.yml`'s required `seam1-plan-shape` leg.
* The coverage run is not duplicated into the other two legs that run
  `tests/gates` (`parity.yml`'s `seam1-plan-shape` and `ci.yml`'s
  `python-newest`). The reason is COST and only cost: a second full execution
  of the directory, re-measured on this branch's head at
  `1197 passed, 59 skipped in 32.05s` plain against `63.14s` under
  `coverage`, with the audit end to end at 63.84s. It is deliberately NOT
  that a bound `ALP_SDK_ROOT` would measure something
  wrong -- binding one only unskips tests, which can only SHRINK the
  never-iterated set, so seam1 could produce fewer findings but never a false
  one.

Nothing here claims to catch the class in general. Unreachability that needs a
dataflow invariant (tan-cli#1062's round-3 review) is a refinement-typing
problem, not a lint, and the four line-attribution shapes the detector is
blind to are named in `_vacuous_gate_shapes_core.py`'s docstring rather than
guessed at.
"""
from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest
import yaml

import _vacuous_gate_shapes_core as core

PYTHON_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PYTHON_ROOT.parent
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AUDIT_SCRIPT = "scripts/audit_vacuous_gate_shapes.py"

#: The variable `ci.yml`'s pytest step binds and the audit step must bind too:
#: the audit spawns its own `pytest tests/gates` child, which inherits the
#: job-wide `GITHUB_EVENT_NAME=merge_group` but NOT a step-scoped `env:`.
MERGE_GROUP_VAR = "TAN_MERGE_GROUP_BASE_REF"

#: The three meanings an `ALLOWED_EMPTY_LOOPS` row may carry. See
#: `test_every_allowed_empty_loop_row_carries_a_classified_reason`, and
#: `_vacuous_gate_shapes_core.py`'s comment on the table for what each means.
CLASSES = ("healthy-empty", "forward-looking", "unmeasurable")

#: The two shapes that defeat assumption B (see `_vacuous_gate_shapes_core.py`)
#: and are therefore the only things an `unmeasurable` row may be. A row must
#: name one -- "unmeasurable" with no shape is a shrug, not a decision.
UNMEASURABLE_SHAPES = ("raises", "spawned")

#: And it must say where the body IS exercised, since "unmeasurable" is a claim
#: about THIS run rather than about the code. The next person needs to know
#: whether real coverage exists elsewhere or nowhere.
UNMEASURABLE_EVIDENCE = "exercised by"

#: The audit step's `run:`, compared EXACTLY. That is the shape
#: `test_interpreter_policy.py`'s `FULL_SUITE_COMMAND` uses, and its own
#: docstring records why: of the four neuterings PR #1137's third review round
#: measured against the ceiling job, `|| true` appended to the command was the
#: ONE that was already caught, "because [`FULL_SUITE_COMMAND`] is compared
#: exactly". The targeted checks below run first because they NAME the reach;
#: this is the backstop for the reach nobody predicted.
AUDIT_COMMAND = f"python {AUDIT_SCRIPT}"

#: The script's own "print the findings and exit 0 regardless" switch.
#: `argparse` accepts any UNAMBIGUOUS PREFIX of a long option, and `--report`
#: shares none with the script's only other one (`--coverage-data`), so
#: `--r`, `--re`, `--rep`, `--repo`, `--repor` and `--report` ALL parse to
#: `Namespace(coverage_data=None, report=True)` -- measured against the
#: script's own parser. A substring test for the full spelling sees none of
#: the first five, which is why [`_is_report_flag`] compares prefixes.
REPORT_FLAG = "--report"

#: Shell suffixes that throw the command's exit status away, so the step
#: reports success whatever the script returned. `|| true` is the spelling
#: `test_interpreter_policy.py` names; `; true`, `|| :` and `|| exit 0` are
#: the same move spelled differently, and a check that knew only the first
#: would be an instruction to spell it one of the other three.
DISCARDS_EXIT_STATUS = re.compile(r"(?:\|\||;)\s*(?::|true\b|exit\s+0\b)")

#: A `<file>:<line>` citation, which no allow-list reason may carry. Workflow
#: files too, not only `.py`: a reason naming `ci.yml:640` rots exactly the way
#: the `test_interpreter_policy.py:230` one did, and this repo's `parity.yml`
#: line numbers moved by roughly 43 lines in a single day.
LINE_CITATION = re.compile(r"\.(?:py|ya?ml):\d+")


def _sources() -> list[tuple[str, str, ast.Module]]:
    """The three package trees under `python/`, parsed once.

    `tan/`, `tests/` and `scripts/` -- named rather than globbed, so a `.venv`
    or a build tree that appears beside them is excluded by construction
    rather than by a pattern somebody has to keep right. `python/conftest.py`
    is the one `.py` outside all three and is deliberately not walked: it is
    16 lines and contains no `assert` and no `try`.

    `SyntaxWarning` is suppressed for the parse and only for the parse. One
    file in the tree carries a real one (`tests/commands/
    test_build_text_issue_dedup.py`, a non-raw Windows PATH literal whose
    `\\v` is a vertical tab), and a static walk re-emitting somebody else's
    warning once per collection is noise this file did not create and must
    not appear to own.
    """
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for root in ("tan", "tests", "scripts"):
            for path in sorted((PYTHON_ROOT / root).rglob("*.py")):
                text = path.read_text(encoding="utf-8")
                rel = path.relative_to(PYTHON_ROOT).as_posix()
                out.append((rel, text, ast.parse(text)))
    return out


SOURCES = _sources()


# --------------------------------------------------------------------------
# 1. Tautologies and swallowed assertions
# --------------------------------------------------------------------------


def test_no_assertion_in_the_tree_is_a_tautology():
    findings = [
        finding
        for rel, text, tree in SOURCES
        for finding in core.iter_tautologies(tree, rel, text)
    ]
    assert findings == [], (
        "an assertion holds for every input (`assert True` or `assert x == x`)"
        " -- it reports green having checked nothing:\n  "
        + "\n  ".join(str(f) for f in findings)
    )


def test_no_assertion_is_swallowed_by_its_own_try():
    findings = [
        finding
        for rel, text, tree in SOURCES
        for finding in core.iter_swallowed_asserts(tree, rel, text)
    ]
    assert findings == [], (
        "an `assert` sits inside a `try` whose `except` catches "
        "`AssertionError` -- the assertion is a no-op with extra steps and "
        "the test passes whether the property holds or not:\n  "
        + "\n  ".join(str(f) for f in findings)
    )


def test_the_tautology_and_swallow_walks_flag_fabricated_input():
    """Both walks report zero on this tree, which is exactly what a walk that
    stopped matching also reports. These are the only things that tell the two
    apart."""
    taut = (
        "def f(x, xs):\n"
        "    assert True\n"
        "    assert x == x\n"
        '    assert f"{x} must be set"\n'      # literal text: always truthy
        '    assert (x, "x must be set")\n'    # the dropped comma
        "    assert (*xs, x)\n"                # one non-* element: arity >= 1
    )
    got = core.iter_tautologies(ast.parse(taut), "fab.py", taut)
    assert [f.lineno for f in got] == [2, 3, 4, 5, 6], f"tautology walk: {got!r}"

    swallowed = (
        "def f(x):\n"
        "    try:\n"
        "        assert x\n"
        "    except AssertionError:\n"
        "        pass\n"
        "    try:\n"
        "        assert x\n"
        "    except Exception:\n"
        "        pass\n"
    )
    got = core.iter_swallowed_asserts(ast.parse(swallowed), "fab.py", swallowed)
    assert [f.lineno for f in got] == [3, 7], f"swallowed-assert walk: {got!r}"


def test_the_walks_leave_the_benign_spellings_alone():
    """The false-positive controls, one per rule that could over-reach.

    `assert x == y` is an ordinary comparison. `assert x, f"..."` is the
    CORRECT two-argument form and must not be confused with the tuple typo it
    is one comma away from. And four spellings that LOOK like the two new
    rules but can genuinely fail, so flagging them would be this walk
    committing the over-claim it exists to catch (six controls in the source
    below: two more follow this list):

      * `assert f""` -- `JoinedStr(values=[])`, falsy, always fails.
      * `assert f"{x}"` -- `""` when `x` is `""`; measured on CPython 3.12.3,
        `bool(f"{x}")` with `x = ""` is `False`. The rule the code
        implements is "the `JoinedStr` carries a non-empty literal
        `Constant` segment", and neither of these does.
      * `assert ()` -- empty tuple, always fails.
      * `assert (*xs,)` -- `()` when `xs` is empty, so it can fail. One non-`*`
        element is what makes a tuple's arity at least 1.

    And two more that are truthy for every BUILT-IN operand and are declined
    anyway, so the rule is an UNDER-claim rather than an over-claim. Measured
    on the same interpreter with `x = ""`: `bool(f"{x!r}")` is `True` (the
    two-character repr of the empty string) and `bool(f"{x:>10}")` is `True`
    (ten spaces). Neither is flagged, because a conversion runs
    `__repr__`/`__str__` and a format spec runs `__format__`, both
    overridable to return `""` -- so neither is UNCONDITIONALLY truthy and
    flagging it would be the over-claim this walk exists to catch. (`f"{x=}"`
    IS flagged, and correctly: the `=` spec emits a real `Constant("x=")`.)

      * `assert f"{x!r}"` -- a conversion, declined.
      * `assert f"{x:>10}"` -- a format spec, declined.

    Flagging any of these would make the rules wrong on their first run, which
    is how a gate gets deleted rather than fixed."""
    benign = (
        "def f(x, y, xs):\n"
        "    assert x == y\n"
        "    assert False or x\n"
        '    assert x, f"{x} must be set"\n'   # the CORRECT two-argument form
        '    assert f""\n'                     # JoinedStr(values=[]): FALSY
        '    assert f"{x}"\n'                  # "" for an empty x: can FAIL
        '    assert f"{x!r}"\n'                # a custom __repr__ may be ""
        '    assert f"{x:>10}"\n'              # a custom __format__ may be ""
        "    assert ()\n"                      # empty tuple: always FAILS
        "    assert (*xs,)\n"                  # () for an empty xs: can FAIL
        "    try:\n"
        "        assert x\n"
        "    except OSError:\n"
        "        pass\n"
    )
    tree = ast.parse(benign)
    assert core.iter_tautologies(tree, "benign.py", benign) == []
    assert core.iter_swallowed_asserts(tree, "benign.py", benign) == []


# --------------------------------------------------------------------------
# 2. The coverage gate's negative controls and floors
# --------------------------------------------------------------------------


def test_the_for_and_assert_walks_reach_a_real_population():
    """The coverage gate's candidate floors, in the style of
    `test_inert_option_markers.py`'s `assert len(ALL_OPTIONS) >= 400`.

    They run HERE, not only in the script, so a collapse in the `ast` walk
    reds in every leg rather than only in the one leg that pays for a coverage
    run.
    """
    sites = [
        site
        for rel, text, tree in SOURCES
        if rel.startswith("tests/gates/")
        for site in core.iter_for_sites(tree, rel, text)
    ]
    assert len(sites) >= core.MIN_FOR_SITES, (
        f"only {len(sites)} `for` sites walked under tests/gates/, below the "
        f"floor of {core.MIN_FOR_SITES}. Either the directory shrank -- move "
        "the floor in the same change, deliberately -- or the walk stopped "
        "matching and the audit is now permanently clean."
    )
    asserts = sum(
        len(core.iter_assert_lines(tree))
        for rel, _, tree in SOURCES
        if rel.startswith("tests/gates/")
    )
    assert asserts >= core.MIN_ASSERT_SITES, (
        f"only {asserts} `assert` statements walked under tests/gates/, below "
        f"the floor of {core.MIN_ASSERT_SITES}"
    )


def test_never_iterating_flags_a_fabricated_loop():
    """The negative control for the coverage comparison itself, driven with a
    synthesised coverage map so it needs no coverage run -- the pattern
    installed at `test_every_issue_code_is_registered.py`'s
    `_stale_forwards(live | {fabricated}) == [fabricated]`.

    Three loops, one of each outcome: iterated (body line hit), never iterated
    (header hit, body not), and never reached at all (header not hit). Only
    the middle one may be reported -- a walk that returned all three, or none,
    would pass a weaker assertion.
    """
    src = (
        "def f(a, b, c):\n"
        "    for x in a:\n"
        "        touched(x)\n"
        "    for y in b:\n"
        "        never(y)\n"
        "    for z in c:\n"
        "        unreached(z)\n"
    )
    sites = core.iter_for_sites(ast.parse(src), "fab.py", src)
    assert [s.header_line for s in sites] == [2, 4, 6], sites
    covered = {"fab.py": frozenset({1, 2, 3, 4})}
    found = core.never_iterating(sites, covered)
    assert [s.header_line for s in found] == [4], (
        "the never-iterating detector did not report exactly the one loop "
        f"whose header ran and whose body did not: {found!r}"
    )


def test_an_async_for_is_walked_exactly_like_a_for():
    """`iter_for_sites` claims `For`/`AsyncFor`, and `tests/gates` contains 295
    of the first and ZERO of the second -- so deleting the `AsyncFor` half
    leaves the count at 295, `MIN_FOR_SITES` green, and every other test in
    this file passing. A forward-looking branch that measures nothing, inside a
    gate about forward-looking branches that measure nothing, and unlike the
    allow-list rows it carried no declaration. This is the declaration: the
    branch driven on fabricated input in BOTH states, so it cannot be deleted
    or broken silently."""
    src = (
        "async def f(a, b):\n"
        "    async for x in a:\n"
        "        touched(x)\n"
        "    async for y in b:\n"
        "        never(y)\n"
    )
    sites = core.iter_for_sites(ast.parse(src), "fab.py", src)
    assert [s.header_line for s in sites] == [2, 4], sites
    assert [sorted(s.body_lines) for s in sites] == [[3], [5]], sites
    found = core.never_iterating(sites, {"fab.py": frozenset({2, 3, 4})})
    assert [s.header_line for s in found] == [4], found


def test_the_third_allow_list_class_is_accepted_before_it_has_a_row():
    """`unmeasurable` has ZERO rows, so the parametrized classification test
    never exercises it -- precisely the shape this whole gate objects to.
    Driven on fabricated reasons instead: each declared class is accepted, an
    invented fourth is not.

    The class is not decoration. `header covered and body uncovered` does NOT
    mean "the collection was empty": a `for` whose ITERABLE RAISES has the
    identical coverage signature (measured), as does one whose body only runs
    in a spawned interpreter -- and this repo spawns at 336 measured call
    sites (`grep -rnE "subprocess[.](run|Popen|check_output|check_call)"
    python/{tan,tests,scripts} --include=*.py | wc -l`; `tests/gates` alone,
    48). There is no live instance today. Without a truthful label the first
    one gets filed as `healthy-empty` or `forward-looking`, both of which
    would be FALSE of it, and the allow-list stops being a record of
    decisions, which is the only thing it is for.

    The third class also carries a HIGHER bar than the other two -- it must
    name the shape and where the body really runs -- and that branch is
    likewise unreachable from the parametrized row test while no row exists,
    so it is driven here too, in both directions.
    """
    assert CLASSES == ("healthy-empty", "forward-looking", "unmeasurable")
    for name in CLASSES:
        assert f"{name}. Fabricated.".startswith(CLASSES), name
    assert not "genuinely-fine. Fabricated.".startswith(CLASSES)

    # And the extra bar the third class carries, which the parametrized test
    # above cannot reach either. A row that names the shape AND where the
    # body really runs is accepted; the two half-answers are not.
    good = (
        "unmeasurable. The iterable raises StopSomething on the first "
        "`next`, so the body is unreachable in THIS run; it is exercised by "
        "the `pytest.raises(...)` case two functions down."
    )
    _assert_row_reason_is_sound("fabricated", good)
    for bad in (
        "unmeasurable. It is fine. Nothing here says which of the two shapes "
        "it is, nor what does exercise the body, and nothing checks it.",
        "unmeasurable. The body only runs in a spawned interpreter, and this "
        "sentence stops there without saying where the real coverage lives.",
        "unmeasurable. It is exercised by something, somewhere, but this "
        "reason never says which of the two shapes defeated the measurement.",
    ):
        with pytest.raises(AssertionError):
            _assert_row_reason_is_sound("fabricated", bad)
    # ...and the two lower bars still apply to it, through the same helper.
    for bad in ("unmeasurable. Too short.", "invented-class. " + "x" * 90):
        with pytest.raises(AssertionError):
            _assert_row_reason_is_sound("fabricated", bad)


def test_body_line_span_is_the_whole_subtree_not_the_first_statement():
    """The detector's original two false positives, pinned.

    Keying on `body[0].lineno` misreports a loop whose body opens with a
    multi-line `if`: the CONDITION line is covered, the first nested
    statement's line is not, and the loop reads as never-iterated when it
    plainly iterated. `body_line_span` takes the union of the whole subtree,
    so the condition line counts.
    """
    src = (
        "def f(items):\n"
        "    for item in items:\n"
        "        if (\n"
        "            item\n"
        "        ):\n"
        "            handle(item)\n"
    )
    sites = core.iter_for_sites(ast.parse(src), "fab.py", src)
    assert sites[0].body_lines >= frozenset({3, 4, 5, 6}), sites[0].body_lines
    covered = {"fab.py": frozenset({2, 3})}  # header + the `if` condition only
    assert core.never_iterating(sites, covered) == [], (
        "a loop whose body's multi-line `if` condition WAS covered was "
        "reported as never-iterating -- the `body[0].lineno` bug is back"
    )


def test_a_for_else_orelse_does_not_count_as_the_body():
    """`for ... else` runs its `else` precisely when the loop did not break,
    which INCLUDES never having iterated. Counting those lines as body would
    hide the defect."""
    src = "def f(a):\n    for x in a:\n        body(x)\n    else:\n        after()\n"
    site = core.iter_for_sites(ast.parse(src), "fab.py", src)[0]
    assert 5 not in site.body_lines, site.body_lines
    assert core.never_iterating([site], {"fab.py": frozenset({2, 4, 5})}) == [site]


def test_a_single_line_body_is_skipped_rather_than_guessed_at():
    """One of the four blind spots the core docstring names, asserted rather
    than only described. `for x in y: f(x)` puts the body on the header's own
    line, so no coverage signal can separate them -- the site must report an
    empty body span and be skipped, not reported either way."""
    src = "def f(a):\n    for x in a: touch(x)\n"
    site = core.iter_for_sites(ast.parse(src), "fab.py", src)[0]
    assert site.body_lines == frozenset(), site.body_lines
    assert core.never_iterating([site], {"fab.py": frozenset({2})}) == []


def test_a_file_absent_from_the_coverage_map_contributes_nothing():
    """The silence `MAX_UNMEASURED_GATE_FILES` exists to bound, asserted here
    so the audit's floor is not the only thing that knows about it."""
    src = "def f(a):\n    for x in a:\n        never(x)\n"
    sites = core.iter_for_sites(ast.parse(src), "unmeasured.py", src)
    assert core.never_iterating(sites, {}) == []
    assert core.never_iterating(sites, {"unmeasured.py": frozenset({2})}) == sites


# --------------------------------------------------------------------------
# 3. Allow-list staleness, checked without a coverage run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(core.ALLOWED_EMPTY_LOOPS))
def test_every_allowed_empty_loop_row_still_names_a_real_loop(key: tuple[str, str]):
    """The allow-list is deliberately keyed on `(file, header source)` rather
    than `(file, line)`, so it survives edits above it -- but that also means
    a row can outlive the loop it describes and quietly widen the exemption.
    This is the check that stops it.

    Note the ONE direction that is NOT checked anywhere: a row whose loop DID
    iterate in some run is not an error, because whether a loop iterates
    depends on the configuration (`ALP_SDK_ROOT` bound unskips tests, and more
    execution can only shrink the never-iterated set). A gate that flips on
    the leg it runs in is worse than no gate.
    """
    rel, _ = key
    path = PYTHON_ROOT / rel
    assert path.is_file(), f"ALLOWED_EMPTY_LOOPS names a missing file: {rel}"
    text = path.read_text(encoding="utf-8")
    _assert_key_names_exactly_one_loop(key, core.iter_for_sites(ast.parse(text), rel, text))


def _assert_key_names_exactly_one_loop(
    key: tuple[str, str], sites: list[core.ForSite]
) -> None:
    """A row must match EXACTLY one `for` site, not at least one.

    `any(site.key == key ...)` was the first version and it checked only the
    staleness half. `ForSite.key` is `(file, header source)` and is NOT unique
    within a file: measured over this branch's 295 sites under `tests/gates/`,
    263 distinct keys and 26 shared by 2-4 sites --
    `for node in ast.walk(tree):` occurs 4x in
    `test_subprocess_env_routes_through_the_helper.py` and 4x in
    `_vacuous_gate_shapes_core.py` itself. None of the eight rows collides
    today, so nothing was wrong; but the audit exempts a site by `site.key in
    ALLOWED_EMPTY_LOOPS`, so the first row keyed on a duplicated header would
    exempt every sibling sharing it -- one reviewed decision silently covering
    loops nobody read -- with this test still green.

    Fails closed. The remedy when it fires is a decision, not a widening:
    re-key the row, or reword one of the headers so the two are
    distinguishable.
    """
    rel, source = key
    matched = [site for site in sites if site.key == key]
    reason = core.ALLOWED_EMPTY_LOOPS.get(key, "(fabricated)")
    assert matched, (
        f"no `for` statement in {rel} reads {source!r} any more. The loop was "
        "deleted or reworded and this exemption now covers nothing -- drop "
        "the row in the same change, or re-key it. Reason on file:\n    "
        + reason
    )
    assert len(matched) == 1, (
        f"{len(matched)} `for` statements in {rel} read {source!r} (lines "
        f"{[site.header_line for site in matched]}). The allow-list key is "
        "`(file, header source)`, so this ONE reviewed row exempts all of "
        "them -- and only one of them was read. Re-key the row, or reword "
        "one of the headers. Reason on file:\n    " + reason
    )


@pytest.mark.parametrize("key", sorted(core.ALLOWED_EMPTY_LOOPS))
def test_every_allowed_empty_loop_row_carries_a_classified_reason(
    key: tuple[str, str],
):
    """A row without a reason is a suppression; a row with one is a reviewed
    decision. That difference is the whole point of the allow-list.

    The reason must also pick one of the three CLASSES, because they mean
    different things and conflating them is how a vacuous gate gets waved
    through as a healthy one: `healthy-empty` says the loop collects
    violations and its being empty IS the gate passing, so an iteration would
    be the failure; `forward-looking` says the population it measures is empty
    today, which means the assertions inside it are currently checking nothing
    and everyone can see that; `unmeasurable` says the collection was NOT
    empty and this run could not see the body -- an iterable that raised, or a
    body that only runs in a spawned interpreter. There are zero rows of the
    third kind today; it exists so the first one is declared honestly rather
    than squeezed into one of the two labels that would be false of it.
    """
    _assert_row_reason_is_sound(repr(key), core.ALLOWED_EMPTY_LOOPS[key])


def _assert_row_reason_is_sound(label: str, reason: str) -> None:
    """The whole per-row bar, in ONE place.

    A helper rather than a test body because the `unmeasurable` branch below
    is unreachable from the parametrized caller while that class has zero
    rows -- so `test_the_third_allow_list_class_is_accepted_before_it_has_a_row`
    drives this same function with fabricated reasons, and the DISPATCH is
    exercised too, not only the thing it dispatches to. A branch reachable
    from exactly one caller that never takes it is the defect this file is
    about.
    """
    assert reason.startswith(CLASSES), (
        f"{label}'s reason starts with none of {CLASSES} -- say which of the "
        f"three this is before saying why: {reason!r}"
    )
    assert len(reason) >= 80, (
        f"{label} carries a classification but no argument: {reason!r}"
    )
    if reason.startswith("unmeasurable"):
        _assert_unmeasurable_reason_is_specific(label, reason)


def _assert_unmeasurable_reason_is_specific(label: str, reason: str) -> None:
    """The extra bar an `unmeasurable` row carries, and the other two do not.

    `healthy-empty` and `forward-looking` are both claims about the CODE, and
    the class name carries most of the meaning. `unmeasurable` is a claim
    about THIS RUN -- the collection was not empty, the run simply could not
    see the body -- so the class name alone says almost nothing. The table
    comment in `_vacuous_gate_shapes_core.py` already demands the shape and
    the real coverage location; before this, nothing checked, and a row
    reading "unmeasurable. It is fine." plus filler to 80 characters passed.
    """
    assert any(shape in reason for shape in UNMEASURABLE_SHAPES), (
        f"{label}'s `unmeasurable` reason names neither shape that defeats "
        f"assumption B -- say which it is (one of {UNMEASURABLE_SHAPES}: an "
        "iterable that RAISES, or a body that only runs in a SPAWNED "
        f"interpreter):\n    {reason}"
    )
    assert UNMEASURABLE_EVIDENCE in reason, (
        f"{label}'s `unmeasurable` reason does not say where the body IS "
        f"{UNMEASURABLE_EVIDENCE!r} -- name the `pytest.raises` around it, or "
        "the child command, or say plainly that nothing exercises it. "
        "'Unmeasurable' is a statement about this run, not about the code, "
        f"and without that the row is a shrug:\n    {reason}"
    )


@pytest.mark.parametrize(
    "reason",
    sorted(core.ALLOWED_EMPTY_LOOPS.values()) + sorted(core.ALLOWED_ZERO_ASSERT_FILES.values()),
)
def test_no_allow_list_reason_cites_a_line_number(reason: str):
    """Cite the symbol, never the line.

    Both keys are line-free on purpose so they cannot rot. A first draft then
    let the PROSE rot inside a single commit: a row said "`INTERIOR_PINS` is
    `frozenset()` at test_interpreter_policy.py:230", and that same PR's own
    unrelated edit two hundred lines higher in the same file moved it to 232.
    Nothing caught it, because the classification check reads only the prefix
    and the length. This is the check that would have.

    Workflow files are in scope too, not only `.py`: a reason naming
    `ci.yml:640` rots the same way, and this repo's `parity.yml` line numbers
    moved by roughly 43 lines in a single day.
    """
    hit = LINE_CITATION.search(reason)
    assert hit is None, (
        f"an allow-list reason cites {hit.group(0)!r}. A line number in prose "
        "beside a deliberately line-free key rots on the next edit anywhere "
        f"above it -- name the symbol instead:\n    {reason}"
    )


@pytest.mark.parametrize("rel", sorted(core.ALLOWED_ZERO_ASSERT_FILES))
def test_every_zero_assert_exemption_still_names_a_real_file(rel: str):
    path = PYTHON_ROOT / rel
    assert path.is_file(), f"ALLOWED_ZERO_ASSERT_FILES names a missing file: {rel}"
    reason = core.ALLOWED_ZERO_ASSERT_FILES[rel]
    assert "ALP_SDK_ROOT" in reason, (
        f"{rel}'s exemption does not name the skip guard that earns it: "
        f"{reason!r}. Every row today is an `ALP_SDK_ROOT`-gated skip; a new "
        "kind of exemption needs its own argument, in review, not a widened "
        "assertion here."
    )
    assert path.read_text(encoding="utf-8").count("ALP_SDK_ROOT") > 0, (
        f"{rel} no longer mentions ALP_SDK_ROOT at all -- the skip guard its "
        "exemption rests on is gone, so the file may now be silently "
        "asserting nothing for some OTHER reason."
    )


def test_a_duplicated_loop_header_is_refused_rather_than_exempted():
    """The control for [`_assert_key_names_exactly_one_loop`]'s second half.

    No row collides today, so the branch that objects to a duplicated header
    never runs against the real allow-list -- the exact shape this file is
    about. Driven on fabricated source carrying two byte-identical headers,
    which `ForSite.key` cannot tell apart by construction.
    """
    src = (
        "def f(a, b):\n"
        "    for x in sorted(a):\n"
        "        first(x)\n"
        "    for x in sorted(a):\n"
        "        second(x)\n"
        "    for y in b:\n"
        "        only(y)\n"
    )
    sites = core.iter_for_sites(ast.parse(src), "fab.py", src)
    duplicated = ("fab.py", "for x in sorted(a):")
    unique = ("fab.py", "for y in b:")
    assert [s.key for s in sites].count(duplicated) == 2, sites
    _assert_key_names_exactly_one_loop(unique, sites)
    with pytest.raises(AssertionError):
        _assert_key_names_exactly_one_loop(duplicated, sites)
    with pytest.raises(AssertionError):
        _assert_key_names_exactly_one_loop(("fab.py", "for z in c:"), sites)


@pytest.mark.parametrize("name", ["ALLOWED_EMPTY_LOOPS", "ALLOWED_ZERO_ASSERT_FILES"])
def test_no_allow_list_key_is_spelled_twice(name: str):
    """Both allow-lists are dict LITERALS, and Python drops a duplicate key in
    silence: the later spelling wins and the earlier row never existed.

    Measured on this branch before this check: inserting a full
    `unmeasurable`-class row for the already-present key
    `("tests/gates/test_core_does_not_import_commands.py", "for module in
    _command_imports(path):")` left the suite at `44 passed`. Not a widening
    -- the key was already exempt -- but the REVIEWED REASON beside it can be
    replaced by anything, or by nothing, with no gate objecting, and a
    reviewed reason that can be quietly replaced is worth less than one that
    cannot.

    `_module_size_budget_core.py`'s `_load_json` already refuses exactly this
    for its records, citing tan-cli#586: "the last spelling silently wins and
    any other is dead weight". This reads the SOURCE rather than the imported
    dict, because by the time the object exists the evidence is gone.
    """
    text = Path(core.__file__).read_text(encoding="utf-8")
    duplicates = core.duplicate_literal_dict_keys(ast.parse(text), name)
    assert duplicates == [], (
        f"{name} spells these keys more than once: {duplicates}. Python keeps "
        "the LAST one and discards every earlier row silently, reason and "
        "all. Merge the rows deliberately, or re-key one of them."
    )


def test_the_duplicate_key_walk_flags_fabricated_input():
    """[`core.duplicate_literal_dict_keys`] in both directions. Neither
    allow-list carries a duplicate, so its reporting branch never runs against
    the real module.

    `ANNOTATED` is not decoration. Both real allow-lists are spelled
    `NAME: dict[...] = {...}`, which is an `ast.AnnAssign` and not an
    `ast.Assign`; the first draft of the walk matched only the latter, reached
    neither table, and reported a clean zero -- this file's own subject,
    committed inside the fix for it. The plain form is driven too, because
    both are legal and a walk that handled only the annotated one would be
    the same defect mirrored.
    """
    src = (
        "TABLE = {\n"
        '    ("a.py", "for x in y:"): "first",\n'
        '    ("b.py", "for x in y:"): "distinct",\n'
        '    ("a.py", "for x in y:"): "second, and the first is now gone",\n'
        "}\n"
        'ANNOTATED: dict[str, str] = {"k": "first", "j": "x", "k": "last"}\n'
        'OTHER = {"k": 1, "k": 2}\n'
        "CLEAN = {'p': 1, 'q': 2}\n"
    )
    tree = ast.parse(src)
    assert core.duplicate_literal_dict_keys(tree, "TABLE") == [
        ("a.py", "for x in y:")
    ]
    assert core.duplicate_literal_dict_keys(tree, "ANNOTATED") == ["k"]
    assert core.duplicate_literal_dict_keys(tree, "OTHER") == ["k"]
    assert core.duplicate_literal_dict_keys(tree, "CLEAN") == []
    assert core.duplicate_literal_dict_keys(tree, "ABSENT") == []

    # And the real tables ARE reached -- the check above is worthless if the
    # walk cannot see them. Both are `AnnAssign`, and both are non-empty.
    real = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
    for name, table in (
        ("ALLOWED_EMPTY_LOOPS", core.ALLOWED_EMPTY_LOOPS),
        ("ALLOWED_ZERO_ASSERT_FILES", core.ALLOWED_ZERO_ASSERT_FILES),
    ):
        assert _literal_dict_key_count(real, name) == len(table), (
            f"the duplicate-key walk reaches {_literal_dict_key_count(real, name)} "
            f"literal keys for {name}, but the imported table has {len(table)}. "
            "The walk is not looking at the table it is supposed to guard, so "
            "`test_no_allow_list_key_is_spelled_twice` is reporting a clean "
            "zero from having found nothing to check."
        )


def _literal_dict_key_count(tree: ast.AST, name: str) -> int:
    """How many literal keys the `name = {...}` dict literal spells, counting
    a duplicate twice -- the reachability half of the check above."""
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(x, ast.Name) and x.id == name for x in targets):
            total += sum(1 for key in node.value.keys if key is not None)
    return total


# --------------------------------------------------------------------------
# 4. The coverage audit is wired in, and can still fail
# --------------------------------------------------------------------------


def _python_job() -> dict:
    """`ci.yml`'s `python` job, parsed."""
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["python"]


def _audit_step(job: dict | None = None) -> dict:
    """The `python` job step that runs the audit, matched BY SCRIPT PATH.

    Matching by path rather than by `name:` is the whole point: the checks
    below have to be able to say something about THAT step, and a `name:` is
    the one part of it a tidy-up is free to reword.

    Takes an already-parsed job so the fabricated-input control below can
    drive the same checks against a job that is not `ci.yml`.
    """
    steps = (_python_job() if job is None else job)["steps"]
    matched = [step for step in steps if AUDIT_SCRIPT in str(step.get("run", ""))]
    assert len(matched) == 1, (
        f"expected exactly one step in the `python` job to run "
        f"{AUDIT_SCRIPT}, found {len(matched)}. It is the only leg that "
        "measures never-iterating loops at all; without it the coverage half "
        "of tan-cli#1145 runs nowhere and the allow-list becomes "
        "documentation."
    )
    return matched[0]


def _is_report_flag(token: str) -> bool:
    """`--report`, and every `argparse` abbreviation of it.

    A prefix comparison, not a substring one. `"--report" in run` is what the
    first version of this check did, and it saw none of `--r`, `--re`,
    `--rep`, `--repo`, `--repor` -- all five of which `argparse` resolves to
    `--report` because it is the only long option beginning with `r`.
    """
    return token.startswith("--") and len(token) > 2 and REPORT_FLAG.startswith(token)


def _assert_the_audit_step_can_still_fail(job: dict) -> None:
    """The whole neutering bar for the audit step, in ONE place.

    A helper rather than a test body because every branch in it is FALSE
    against a healthy `ci.yml` -- the vacuity this file is about, one level
    up. `test_a_neutered_audit_step_is_caught` drives the same function with a
    fabricated job per neutering (see [`NEUTERINGS`]), so each branch is
    exercised in BOTH directions rather than only in the direction that says
    "fine".

    SIX checks, and the count is the finding. The first version of this one
    had two: step-level `continue-on-error is not True`, and
    `"--report" not in step["run"]`. PR #1166's fourth review round measured
    three standard neuterings surviving that, each still `44 passed` --
    `continue-on-error: true` on the JOB, `if: false` on the step, and
    `--rep || true` on the command, where `--rep` is an `argparse`
    abbreviation the substring test never saw and `|| true` was checked
    nowhere. The precedent this test's own docstring already cited --
    `test_interpreter_policy.py`'s ceiling-job checks -- carries all three of
    job-level, step-level and `if`, and this reimplementation had dropped two
    of them. Mirrored rather than reinvented, then extended with the two
    command shapes that precedent does not cover.
    """
    assert job.get("continue-on-error") is not True, (
        "ci.yml's `python` job carries `continue-on-error: true` at JOB "
        "level. Every step in it, the audit included, then reports green "
        "whatever it did -- and this is the likeliest of the six, because "
        "the audit reds on somebody ELSE's undeclared loop and softening the "
        "job is the standard response to a job that reds for a reason you "
        "did not cause."
    )
    for index, step in enumerate(job["steps"]):
        label = step.get("name") or step.get("uses") or f"step {index}"
        assert step.get("continue-on-error") is not True, (
            f"ci.yml's `python` job step {label!r} carries "
            "`continue-on-error: true`. On the audit step that is the whole "
            "gate; on the `pip install coverage` step above it the audit "
            "then dies at import inside a step that reports success."
        )
    step = _audit_step(job)
    assert "if" not in step, (
        f"ci.yml's audit step carries a step-level `if:` ({step.get('if')!r}). "
        "The job's own guard is the only condition allowed here: a "
        "step-level one lets the job run, install everything, skip the audit "
        "and report success -- indistinguishable from a real green in the "
        "checks list, which is worse than the step being deleted."
    )
    command = str(step["run"]).strip()
    for token in command.split():
        assert not _is_report_flag(token), (
            f"ci.yml's audit step passes {token!r}, which `argparse` resolves "
            f"to {REPORT_FLAG} -- the script's documented never-exit-1 "
            "switch. It is for a local look, not for CI: with it the step "
            "prints the findings and succeeds."
        )
    discarded = DISCARDS_EXIT_STATUS.search(command)
    assert discarded is None, (
        f"ci.yml's audit step ends its command with {discarded.group(0)!r}, "
        "which throws the script's exit status away. The step then runs the "
        "whole coverage measurement, prints the undeclared loop, and reports "
        "success."
    )
    assert command == AUDIT_COMMAND, (
        f"ci.yml's audit step runs {command!r}, not {AUDIT_COMMAND!r}. The "
        "checks above all name reaches that have actually been "
        "measured; this one is the backstop for the next one, and any "
        "genuine change to the command belongs in AUDIT_COMMAND in the same "
        "edit, deliberately."
    )


def _assert_coverage_is_installed_beside_the_audit(job: dict) -> None:
    """`coverage` is installed by some step OTHER than the audit step itself.

    The exclusion is the point. Scanning every `run:` in the job includes the
    audit step's own command, so passing `--coverage-data <path>` to it would
    satisfy the check with `pip install ... coverage` deleted -- the check
    reading its own subject and calling it evidence.
    """
    audit = _audit_step(job)
    runs = [str(step.get("run", "")) for step in job["steps"] if step is not audit]
    assert any("coverage" in run for run in runs), (
        "ci.yml's `python` job installs no `coverage` in any step other than "
        "the audit step itself -- the audit would fail at import, which is "
        "loud, but the install and the step belong in the same job and "
        "should move together. The audit step's own `run:` is excluded on "
        "purpose: a check that reads its own subject proves nothing."
    )


def _fabricated_python_job(job: dict | None = None, step: dict | None = None) -> dict:
    """A minimal `python`-job shape for the controls: an install step and an
    audit step, both healthy, with `job`/`step` overlaid to neuter one."""
    audit = {"name": "the audit", "run": AUDIT_COMMAND}
    audit.update(step or {})
    out = {
        "steps": [
            {"name": "install the test runner", "run": "pip install pytest coverage"},
            audit,
        ]
    }
    out.update(job or {})
    return out


def test_the_coverage_audit_script_exists():
    """`scripts/audit_narrow_except_contracts.py` shipped 290 lines of `ast`
    logic with no test coverage at all. The shared logic here lives in
    `_vacuous_gate_shapes_core.py` precisely so this file can drive it; this
    asserts the script that supplies its IO half has not been renamed out from
    under the CI step below."""
    assert (PYTHON_ROOT / AUDIT_SCRIPT).is_file(), AUDIT_SCRIPT


def test_the_coverage_audit_is_wired_into_ci():
    """A check that runs in no workflow is a check a human has to remember."""
    _assert_coverage_is_installed_beside_the_audit(_python_job())


def test_the_audit_step_can_still_fail():
    """Present and named is not the same as BLOCKING.

    The step is one `pytest tests/gates` child away from somebody else's red,
    so the motive to silence it arrives before the motive to read it. Six
    checks, all in [`_assert_the_audit_step_can_still_fail`] -- see there for
    the shapes and for the three neuterings that survived this test's first
    version.
    """
    _assert_the_audit_step_can_still_fail(_python_job())


#: One fabricated `python` job per way to silence the audit step, as
#: `(id, job overlay, audit-step overlay)`. Each was MEASURED on this branch
#: against the FIRST version of `test_the_audit_step_can_still_fail`: all of
#: them survived it at `44 passed` except `step-continue-on-error` and
#: `run---report`, which were the only two it checked.
NEUTERINGS: list[tuple[str, dict, dict]] = [
    ("job-continue-on-error", {"continue-on-error": True}, {}),
    ("step-continue-on-error", {}, {"continue-on-error": True}),
    ("step-if-false", {}, {"if": False}),
    ("step-if-expression", {}, {"if": "github.event_name == 'schedule'"}),
] + [
    (f"run{suffix.replace(' ', '-')}", {}, {"run": AUDIT_COMMAND + suffix})
    for suffix in (
        " --report",
        " --rep",  # an argparse abbreviation of it
        " --r",  # the shortest one argparse still resolves
        " || true",
        " ; true",
        " || :",
        " || exit 0",
        " --coverage-data /tmp/x",  # a narrowing rather than a silencing
    )
]


def test_the_fabricated_healthy_job_passes_every_check():
    """The controls below are worthless if the fabricated shape reds for a
    reason that has nothing to do with the neutering -- then every one of them
    "passes" without proving anything. This is the positive half."""
    healthy = _fabricated_python_job()
    _assert_the_audit_step_can_still_fail(healthy)
    _assert_coverage_is_installed_beside_the_audit(healthy)


@pytest.mark.parametrize(
    "job, step", [case[1:] for case in NEUTERINGS], ids=[case[0] for case in NEUTERINGS]
)
def test_a_neutered_audit_step_is_caught(job: dict, step: dict):
    """Every branch in [`_assert_the_audit_step_can_still_fail`] is FALSE
    against a healthy `ci.yml`, so none of them ever runs there. That is this
    file's own subject applied to itself: a check whose flagging half never
    executes is a check nobody has seen work. One fabricated job per
    neutering, driven through the same helper the real check uses."""
    with pytest.raises(AssertionError):
        _assert_the_audit_step_can_still_fail(
            _fabricated_python_job(job=job, step=step)
        )


def test_continue_on_error_on_a_step_beside_the_audit_is_caught():
    """The reach `test_interpreter_policy.py` covers by walking EVERY step
    rather than only the one it names: `continue-on-error: true` on the
    `pip install ... coverage` step above the audit leaves the audit dying at
    import inside a job that reports green."""
    fabricated = _fabricated_python_job()
    fabricated["steps"][0]["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_the_audit_step_can_still_fail(fabricated)


def test_the_coverage_check_is_not_satisfied_by_the_audit_step_itself():
    """The self-satisfying shape: the ONLY mention of `coverage` in the job is
    inside the audit step's own `run:`, because it was handed
    `--coverage-data` while `pip install ... coverage` was deleted. Before the
    exclusion in [`_assert_coverage_is_installed_beside_the_audit`] this
    passed."""
    fabricated = _fabricated_python_job(
        step={"run": AUDIT_COMMAND + " --coverage-data /tmp/x"}
    )
    fabricated["steps"][0]["run"] = "pip install pytest"
    with pytest.raises(AssertionError):
        _assert_coverage_is_installed_beside_the_audit(fabricated)


def test_the_report_flag_predicate_covers_every_argparse_abbreviation():
    """[`_is_report_flag`] in both directions, on fabricated tokens.

    Every prefix down to `--r` is accepted by the script's own parser
    (measured: each of `--r`, `--re`, `--rep`, `--repo`, `--repor`,
    `--report` yields `Namespace(coverage_data=None, report=True)`), and
    nothing else may be. `--c` is the abbreviation of the OTHER option and
    takes a value; flagging it would make this check wrong on a legitimate
    local invocation.
    """
    for token in ("--report", "--repor", "--repo", "--rep", "--re", "--r"):
        assert _is_report_flag(token), token
    for token in ("--reports", "--report=1", "--coverage-data", "--c", "-r", "--", "report"):
        assert not _is_report_flag(token), token


def test_the_exit_status_regex_covers_the_four_spellings():
    """[`DISCARDS_EXIT_STATUS`] in both directions.

    `&&` is NOT one of them: it runs the second command only when the first
    SUCCEEDED, so it cannot mask a failure, and matching it would red on a
    legitimate two-command step.
    """
    for suffix in ("|| true", "|| :", "; true", "; :", "|| exit 0", "; exit 0"):
        command = f"{AUDIT_COMMAND} {suffix}"
        assert DISCARDS_EXIT_STATUS.search(command), command
    for command in (
        AUDIT_COMMAND,
        f"{AUDIT_COMMAND} --coverage-data /tmp/x",
        f"{AUDIT_COMMAND} && echo done",
        "python -m pytest tests -q",
    ):
        assert DISCARDS_EXIT_STATUS.search(command) is None, command


def test_the_audit_step_binds_the_merge_group_base_ref():
    """The audit spawns its OWN `pytest tests/gates` child.

    That child inherits the job-wide environment, including
    `GITHUB_EVENT_NAME=merge_group`, but NOT the step-scoped `env:` on the
    pytest step above it. `ci.yml` triggers on `merge_group:` and the `dev`
    queue is live, and GitHub Actions does not populate `GITHUB_BASE_REF` for
    that event -- so without this binding
    `test_module_size_budget_log_append_only.py::test_the_ledger_only_ever_
    appends_since_the_prs_base` hits `BaseRefUnresolved` and fails by design.
    Re-measured on this branch's head with `GITHUB_EVENT_NAME=merge_group`
    and the variable unset: `1 failed, 1196 passed, 59 skipped`, then the
    audit refusing to measure a red run. With it bound, the same audit exits
    0 in 64.29s. The result is a `python` job red on every queued PR, blaming the
    suite rather than the missing variable, with the never-iterating
    measurement running zero times in the merge queue.
    """
    step = _audit_step()
    env = step.get("env") or {}
    assert MERGE_GROUP_VAR in env, (
        f"ci.yml's audit step binds no {MERGE_GROUP_VAR}. Its `pytest "
        "tests/gates` child cannot see the step-scoped binding on the pytest "
        "step above it, so every merge-queue run reds on a base-ref the child "
        "cannot resolve and the audit measures nothing. Copy the two lines "
        "from the pytest step."
    )
    assert env[MERGE_GROUP_VAR] == "${{ github.event.merge_group.base_ref }}", (
        f"ci.yml's audit step binds {MERGE_GROUP_VAR} to "
        f"{env[MERGE_GROUP_VAR]!r}, not the `merge_group` base ref the gate "
        "that consumes it expects."
    )
