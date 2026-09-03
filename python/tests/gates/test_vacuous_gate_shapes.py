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
  of the directory, measured on `dev` at `658f2e37` with this change applied
  at `1187 passed, 60 skipped in 39.45s` plain against `80.01s` under
  `coverage`. It is deliberately NOT that a bound `ALP_SDK_ROOT` would measure something
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
        "def f(x):\n"
        "    assert True\n"
        "    assert x == x\n"
        '    assert f"{x} must be set"\n'
        '    assert (x, "x must be set")\n'
    )
    got = core.iter_tautologies(ast.parse(taut), "fab.py", taut)
    assert [f.lineno for f in got] == [2, 3, 4, 5], f"tautology walk: {got!r}"

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

    `assert x == y` is an ordinary comparison; `assert x, f"..."` is the
    CORRECT two-argument form and must not be confused with the tuple typo it
    is one comma away from; `assert ()` is an empty tuple, which always FAILS
    rather than always passing; and a `try` that catches something else does
    not swallow the assertion. Flagging any of these would make the rules
    wrong on their first run, which is how a gate gets deleted rather than
    fixed."""
    benign = (
        "def f(x, y):\n"
        "    assert x == y\n"
        "    assert False or x\n"
        '    assert x, f"{x} must be set"\n'   # the CORRECT two-argument form
        "    assert ()\n"                      # empty tuple: always FAILS
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
    """`iter_for_sites` claims `For`/`AsyncFor`, and `tests/gates` contains 283
    of the first and ZERO of the second -- so deleting the `AsyncFor` half
    leaves the count at 283, `MIN_FOR_SITES` green, and every other test in
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
    in a spawned interpreter -- and this repo spawns at roughly 150 call
    sites. There is no live instance today. Without a truthful label the first
    one gets filed as `healthy-empty` or `forward-looking`, both of which
    would be FALSE of it, and the allow-list stops being a record of
    decisions, which is the only thing it is for.
    """
    assert CLASSES == ("healthy-empty", "forward-looking", "unmeasurable")
    for name in CLASSES:
        assert f"{name}. Fabricated.".startswith(CLASSES), name
    assert not "genuinely-fine. Fabricated.".startswith(CLASSES)


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
    rel, source = key
    path = PYTHON_ROOT / rel
    assert path.is_file(), f"ALLOWED_EMPTY_LOOPS names a missing file: {rel}"
    text = path.read_text(encoding="utf-8")
    sites = core.iter_for_sites(ast.parse(text), rel, text)
    assert any(site.key == key for site in sites), (
        f"no `for` statement in {rel} reads {source!r} any more. The loop was "
        "deleted or reworded and this exemption now covers nothing -- drop "
        "the row in the same change, or re-key it. Reason on file:\n    "
        + core.ALLOWED_EMPTY_LOOPS[key]
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
    reason = core.ALLOWED_EMPTY_LOOPS[key]
    assert reason.startswith(CLASSES), (
        f"{key!r}'s reason starts with none of {CLASSES} -- say which of the "
        f"three this is before saying why: {reason!r}"
    )
    assert len(reason) >= 80, (
        f"{key!r} carries a classification but no argument: {reason!r}"
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


# --------------------------------------------------------------------------
# 4. The coverage audit is wired in, and can still fail
# --------------------------------------------------------------------------


def _audit_step() -> dict:
    """`ci.yml`'s `python` job step that runs the audit, matched BY SCRIPT
    PATH.

    Matching by path rather than by `name:` is the whole point: the checks
    below have to be able to say something about THAT step, and a `name:` is
    the one part of it a tidy-up is free to reword.
    """
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = data["jobs"]["python"]["steps"]
    matched = [step for step in steps if AUDIT_SCRIPT in str(step.get("run", ""))]
    assert len(matched) == 1, (
        f"expected exactly one step in ci.yml's `python` job to run "
        f"{AUDIT_SCRIPT}, found {len(matched)}. It is the only leg that "
        "measures never-iterating loops at all; without it the coverage half "
        "of tan-cli#1145 runs nowhere and the allow-list becomes "
        "documentation."
    )
    return matched[0]


def test_the_coverage_audit_script_exists():
    """`scripts/audit_narrow_except_contracts.py` shipped 290 lines of `ast`
    logic with no test coverage at all. The shared logic here lives in
    `_vacuous_gate_shapes_core.py` precisely so this file can drive it; this
    asserts the script that supplies its IO half has not been renamed out from
    under the CI step below."""
    assert (PYTHON_ROOT / AUDIT_SCRIPT).is_file(), AUDIT_SCRIPT


def test_the_coverage_audit_is_wired_into_ci():
    """A check that runs in no workflow is a check a human has to remember."""
    _audit_step()
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs = [str(step.get("run", "")) for step in data["jobs"]["python"]["steps"]]
    assert any("coverage" in run for run in runs), (
        "ci.yml's `python` job installs no `coverage` -- the audit step would "
        "fail at import, which is loud, but the install and the step belong "
        "in the same job and should move together."
    )


def test_the_audit_step_can_still_fail():
    """The two standard reaches for a step that reds, both blocked.

    This repo already names both forms (`test_interpreter_policy.py`'s
    ceiling-job checks), but that gate is scoped to the ceiling job and does
    not cover this step. Measured before this test existed: applying
    `continue-on-error: true` AND swapping in `--report` -- the audit's own
    documented "print the findings and exit 0 regardless" switch -- left the
    suite at `69 passed` with the coverage half completely dead.

    That is not a hypothetical reach. The step is one `pytest tests/gates`
    child away from someone else's red, so the motive to silence it arrives
    before the motive to read it.
    """
    step = _audit_step()
    assert step.get("continue-on-error") is not True, (
        "ci.yml's audit step carries `continue-on-error: true`. The step then "
        "runs, finds the undeclared loop, prints it, and the job reports "
        "green -- a gate about gates that cannot fail, made into one."
    )
    assert "--report" not in str(step["run"]), (
        "ci.yml's audit step passes `--report`, which is the script's "
        "documented never-exit-1 switch. It is for a local look, not for CI: "
        "with it the step prints findings and succeeds."
    )


def test_the_audit_step_binds_the_merge_group_base_ref():
    """The audit spawns its OWN `pytest tests/gates` child.

    That child inherits the job-wide environment, including
    `GITHUB_EVENT_NAME=merge_group`, but NOT the step-scoped `env:` on the
    pytest step above it. `ci.yml` triggers on `merge_group:` and the `dev`
    queue is live, and GitHub Actions does not populate `GITHUB_BASE_REF` for
    that event -- so without this binding
    `test_module_size_budget_log_append_only.py::test_the_ledger_only_ever_
    appends_since_the_prs_base` hits `BaseRefUnresolved` and fails by design.
    Measured with `GITHUB_EVENT_NAME=merge_group` and the variable unset:
    `1 failed, 1179 passed, 60 skipped`, then the audit refusing to measure a
    red run. The result is a `python` job red on every queued PR, blaming the
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
