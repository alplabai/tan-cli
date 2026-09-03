# SPDX-License-Identifier: Apache-2.0
"""AST primitives for the empty-collection half of tan-cli#1145 -- the gate
that cannot fail because its loop never runs. Pure functions over `ast` trees
and a line-coverage mapping; no IO, no `coverage` import, no pytest.
`scripts/audit_vacuous_gate_shapes.py` supplies the IO,
`tests/gates/test_vacuous_gate_shapes.py` supplies the negative controls --
deliberately, because the failure this module exists to prevent is a scan that
silently stops matching and reports a clean zero.

tan-cli#1145 asks for two gates. The OTHER one -- an `if:` key must be
compared with `==`, never searched with `in` -- ships in PR #1168, which
tan-cli#1145 says to build first. Nothing about it is here.

## What is decidable here, and what is not

  * **A never-iterating `for`** -- a statement whose header line executed but
    none of whose body lines ever did, across a whole run of `tests/gates`.
    Every assertion inside such a loop is unreachable, so the loop's negative
    half cannot fail for any input.
  * **A tautological assertion** and **an assertion swallowed by its own
    `try`**. The tan-cli#1145 sweep found ZERO of each; both are carried as
    regression insurance because both are trivially `ast`-decidable and cost
    nothing.

Everything else the audit behind tan-cli#1145 looked at is NOT decidable and
is not attempted, so nobody reads a clean run as a broader claim than it is:

  * Unreachability that needs a dataflow invariant. Seeing that
    `sorted(_covered - set(_SEEDED_CONTRACTS))` is empty for every input
    requires knowing that `_covers` enforces membership before populating
    `_covered` -- a refinement-typing problem, not a lint (tan-cli#1062's
    round-3 review, an incidental finding on a PR about retiring `tan build
    --no-auto-bootstrap`). Review discipline, permanently.
  * Substring-vs-structural in general. 70 candidates were audited and
    essentially all assert on driven subprocess stdout/stderr, where presence
    genuinely IS the property. Only the `if:`-key surface is decidable, and
    that is PR #1168's.

## The blind spots, named because they are silent

This module rests on exactly two assumptions, and neither is about what any
NAME holds -- it does no name resolution and follows no values, so there is no
`for`-target, comprehension-target, `with ... as`, attribute or walrus binding
for it to lose track of. The two are:

  A. `coverage` attributes an executed line to the line the source occupies.
  B. `header covered and body uncovered` means THE COLLECTION WAS EMPTY.

Assumption B is the weaker of the two and is stated here because it was
unnamed for a while. A `for` whose ITERABLE RAISES has the identical coverage
signature to one whose collection is empty -- measured with real `coverage`
over a real module, `for r in rows():  # iterable RAISES` and
`for e in empty():  # collection EMPTY` both report
`header_covered=True, body_covered=False` and both are reported. So does a
loop whose body only ever runs in a SPAWNED interpreter the parent's coverage
never saw, and this repo spawns at roughly 150 call sites. There is no live
instance today -- all eight reported loops are genuine empty-collection cases
-- but the first one to appear would be MISCLASSIFIED rather than declared,
because neither "healthy-empty" nor "forward-looking" is true of it. That is
what [`ALLOWED_EMPTY_LOOPS`]' third class, `unmeasurable`, exists to say; see
its comment for the shape of such a row.

Five shapes defeat assumption A, and every one of them is skipped rather than
guessed at:

  * **Comprehensions.** A comprehension's header and body usually share a
    physical line, so the coverage signal cannot separate them.
    [`iter_for_sites`] walks `For`/`AsyncFor` STATEMENTS only.
  * **`while`.** A condition that is false on first evaluation is the
    ordinary, correct shape of a retry loop, not a defect.
  * **A single-line body.** `for x in y: f(x)` puts the body on the header's
    own line, so [`body_line_span`] returns an empty set and
    [`never_iterating`] skips the site -- there is nothing to compare. Two
    such loops would be indistinguishable whatever the truth.
  * **An uncovered header.** A loop in a function nobody called, or in a
    skipped test. That is a different defect with a different remedy, and
    folding it in would bury the eight real findings under every
    `ALP_SDK_ROOT`-gated skip in the directory.
  * **A body measured by somebody else's interpreter.** A loop whose body runs
    only inside a spawned `python -c ...` child is uncovered in the parent's
    data no matter how many times it ran. Nothing here can see the child, so
    such a loop is reported and must be declared `unmeasurable` -- the second
    half of assumption B above.

## Two implementation notes, both of them bugs that were made first

* [`body_line_span`] takes the union of every line in the body SUBTREE, not
  `body[0].lineno`. Keying on the first body statement's own line produced the
  detector's only two false positives: multi-line `if` bodies whose CONDITION
  line was covered while the first nested statement's line was not.
* A file absent from the coverage mapping contributes nothing -- see
  [`never_iterating`]. That silence is why the audit's floor on MEASURED gate
  modules is derived from the number on disk rather than typed as a constant:
  a module that stopped being imported would exempt every loop in it.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ForSite:
    """One `for`/`async for` statement, located and split into the two line
    sets the coverage comparison needs."""

    rel: str
    header_line: int
    body_lines: frozenset[int]
    source: str

    @property
    def key(self) -> tuple[str, str]:
        """The allow-list key: file plus the stripped header text.

        Deliberately NOT `(rel, lineno)`. A line number churns on every edit
        above it, so a line-keyed allow-list rots into noise within a week and
        the next person deletes the gate rather than the rows.
        """
        return (self.rel, self.source)


@dataclass(frozen=True, slots=True)
class Finding:
    """A located defect, with the source line so a reader can judge it without
    opening the file."""

    rel: str
    lineno: int
    source: str

    def __str__(self) -> str:
        return f"{self.rel}:{self.lineno}: {self.source}"


def source_line(text: str, lineno: int) -> str:
    """The `lineno`-th physical line of `text`, stripped; `""` past the end."""
    lines = text.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _lines_of(nodes: Iterable[ast.AST]) -> set[int]:
    """Every physical line any of `nodes` (and their descendants) occupies."""
    out: set[int] = set()
    for node in nodes:
        for sub in ast.walk(node):
            start = getattr(sub, "lineno", None)
            if start is None:
                continue
            end = getattr(sub, "end_lineno", None) or start
            out.update(range(start, end + 1))
    return out


def body_line_span(node: ast.For | ast.AsyncFor) -> frozenset[int]:
    """The union of every line in the loop's body subtree.

    `orelse` is excluded: a `for ... else` runs its `else` precisely when the
    loop did NOT break, which includes the never-iterated case, so counting
    those lines as "the body ran" would hide the defect this looks for.

    The header's own line is subtracted, which is also what makes a
    single-line body report as empty -- see the blind-spot list above.
    """
    return frozenset(_lines_of(node.body) - {node.lineno})


def iter_for_sites(tree: ast.AST, rel: str, text: str) -> list[ForSite]:
    """Every `for`/`async for` statement in `tree`, in source order."""
    sites: list[ForSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        sites.append(
            ForSite(
                rel=rel,
                header_line=node.lineno,
                body_lines=body_line_span(node),
                source=source_line(text, node.lineno),
            )
        )
    return sorted(sites, key=lambda s: (s.rel, s.header_line))


def never_iterating(
    sites: Iterable[ForSite], covered: Mapping[str, frozenset[int]]
) -> list[ForSite]:
    """The sites whose header line executed and whose body never did.

    A file absent from `covered` contributes nothing: it was not imported at
    all, so no claim about its loops is measurable from this run. That is a
    real silence, and the audit's measured-module floor is what refuses to let
    it grow -- not this function, which cannot tell "not imported" from
    "does not exist".
    """
    found = []
    for site in sites:
        hit = covered.get(site.rel)
        if hit is None or site.header_line not in hit:
            continue
        if site.body_lines and not (site.body_lines & hit):
            found.append(site)
    return sorted(found, key=lambda s: (s.rel, s.header_line))


def iter_tautologies(tree: ast.AST, rel: str, text: str) -> list[Finding]:
    """An assertion that holds for every input. Four decidable spellings:

      * `assert <truthy literal>` -- `assert True`, `assert "TODO"`, `assert 1`.
      * `assert x == x` / `assert x is x`.
      * `assert f"..."` -- an f-string is always a non-empty `str` here, so
        the assertion is a no-op no matter what it interpolates.
      * `assert (x, "message")` -- the classic mistyped `assert x, "message"`.
        A non-empty tuple is truthy, so the real condition is discarded and
        the assertion can never fail. Python's own `SyntaxWarning` catches the
        two-element literal case; this catches it whatever the arity and
        wherever the file is parsed rather than imported.

    The tan-cli#1145 sweep found ZERO of the first two, and re-measuring the
    tree for the second two found ZERO of those as well (`python/{tan,tests,
    scripts}`, 1335 asserts under `tests/gates` alone). All four are carried
    as regression insurance at essentially no cost: every shape is trivially
    AST-decidable, and a future `assert True  # TODO` is exactly the kind of
    placeholder that survives review because it reads as deliberate.
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if _is_tautology(node.test):
            findings.append(Finding(rel, node.lineno, source_line(text, node.lineno)))
    return findings


def _is_tautology(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.JoinedStr):
        return True  # an f-string is a non-empty str: always truthy
    if isinstance(test, ast.Tuple):
        return bool(test.elts)  # `assert (x, "msg")` -- the missing comma
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], (ast.Eq, ast.Is)):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and isinstance(right, ast.Name)
        and left.id == right.id
    )


def iter_swallowed_asserts(tree: ast.AST, rel: str, text: str) -> list[Finding]:
    """An `assert` inside a `try` whose `except` catches its `AssertionError`.

    A caught `AssertionError` turns the assertion into a no-op with extra
    steps: the gate reports green whether the property holds or not. Bare
    `except:`, `except Exception`, `except BaseException` and an explicit
    `except AssertionError` all swallow it. The tan-cli#1145 sweep found ZERO;
    carried for the same reason as [`iter_tautologies`].
    """
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_catches_assertion_error(h) for h in node.handlers):
            continue
        for lineno in _lines_of_asserts(node.body):
            findings.append(Finding(rel, lineno, source_line(text, lineno)))
    return findings


def _lines_of_asserts(body: list[ast.stmt]) -> list[int]:
    return sorted(
        node.lineno
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Assert)
    )


def _catches_assertion_error(handler: ast.ExceptHandler) -> bool:
    names = _handler_names(handler.type)
    return names is None or bool(
        names & {"AssertionError", "Exception", "BaseException"}
    )


def _handler_names(node: ast.expr | None) -> set[str] | None:
    """`None` means a bare `except:`, which catches everything."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        out: set[str] = set()
        for elt in node.elts:
            got = _handler_names(elt)
            if got is None:
                return None
            out |= got
        return out
    return set()


def iter_assert_lines(tree: ast.AST) -> frozenset[int]:
    """Every line carrying an `assert` statement -- the input to the
    coarse per-file "this file executed no assertion at all" check."""
    return frozenset(
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)
    )


# --------------------------------------------------------------------------
# Floors. Every check above reports zero on a healthy tree, which is exactly
# what a scan that stopped matching also reports -- these numbers are the only
# thing that tells the two apart. Measured on `dev` at `658f2e37` WITH this
# change applied and no `ALP_SDK_ROOT` bound; each is set below the
# measurement with headroom, so ordinary churn does not move them but a
# collapse does. Raise one deliberately, never to make a red go away.
# --------------------------------------------------------------------------

#: `for`/`async for` statements under `tests/gates/`. Measured: 283.
MIN_FOR_SITES = 240

#: `assert` statements under `tests/gates/`. Measured: 1335.
MIN_ASSERT_SITES = 1100

#: How many `.py` files under `tests/gates/` the coverage run is allowed NOT
#: to have measured. The audit derives its floor as `len(gate_modules()) -
#: this`, against the number on disk rather than against a typed absolute
#: (`MIN_MEASURED_GATE_FILES = 55` was the first version, nine below a number
#: the script knows exactly): both `never_iterating` and
#: `check_zero_assert_files` silently skip a file absent from the coverage
#: mapping, so a constant floor with slack in it lets that many gate modules
#: stop being measured -- every loop and assertion in them exempted -- with
#: the floor still green.
#:
#: ZERO, not one. A slack of one was the first fix and is still too loose,
#: measured: deleting `test_no_new_hardware_facts.py` from a real coverage
#: mapping took the run to `64 measured` against a floor of `65 - 1 = 64`,
#: PASSING, while the never-iterating count silently fell from 8 to 7 --
#: exactly one loop exempted by nothing but absence. All 65 gate modules are
#: measured today, so zero costs nothing. A new helper module under
#: `tests/gates/` that NOTHING imports would red here, which is the right
#: answer: import it from the test that needs it, or move it out of the
#: directory the audit walks. Raising this is a deliberate change with a
#: reason, never a way to make a red go away.
MAX_UNMEASURED_GATE_FILES = 0


#: `for` loops that legitimately never iterate across a whole `tests/gates`
#: run, keyed by [`ForSite.key`] -- `(rel, stripped header source)`. A row is
#: a REVIEWED DECISION, not a suppression: each says why zero iterations is
#: the correct measurement for that loop today. A loop not listed here reds.
#:
#: Rows carry three genuinely different meanings and the difference is the
#: point of writing them down:
#:
#:   * "healthy-empty" -- the loop populates a violation list and its being
#:     empty IS the gate passing; it iterating would be the failure.
#:   * "forward-looking" -- the loop measures a population that is empty
#:     today, so the gate around it is currently vacuous and everyone can see
#:     that it is.
#:   * "unmeasurable" -- the collection was NOT empty, or cannot be shown to
#:     have been. The two live shapes are an iterable that RAISES (identical
#:     coverage signature to an empty one -- see the module docstring's
#:     assumption B) and a body that only ever runs in a spawned interpreter
#:     this run never measured. There are ZERO such rows today, and the class
#:     exists so the first one is declared honestly instead of being squeezed
#:     into one of the two labels that would be false of it. Such a row must
#:     say WHICH shape it is and what does exercise the body -- a
#:     `pytest.raises` around the loop, or the name of the child command --
#:     because "unmeasurable" is a statement about this run, not about the
#:     code, and the next person needs to know where the real coverage lives.
#:
#: No row may cite a `<file>.py:<line>`, and
#: `test_no_allow_list_reason_cites_a_line_number` enforces it. The KEY is
#: line-free on purpose so it cannot rot; a first draft then let the PROSE rot
#: inside one commit, because the same PR's own edit two hundred lines higher
#: moved the constant a reason pointed at. Cite the symbol.
ALLOWED_EMPTY_LOOPS: dict[tuple[str, str], str] = {
    (
        "tests/gates/test_core_does_not_import_commands.py",
        "for module in _command_imports(path):",
    ): (
        "healthy-empty. The loop collects `tan.commands.*` imports found "
        "inside `tan/core/**`; the Dependency Rule (tan-cli#408) says there "
        "are none, and there are none. An iteration here is the defect."
    ),
    (
        "tests/gates/test_interpreter_policy.py",
        "for pin in sorted(INTERIOR_PINS):",
    ): (
        "forward-looking, and ALREADY declared in prose -- the test's own "
        "docstring says 'Vacuous while the set is empty, and that is fine: "
        "it exists so the first entry cannot be wrong quietly.' "
        "`INTERIOR_PINS` is bound to `frozenset()` in that same module."
    ),
    (
        "tests/gates/test_module_size_budget.py",
        "for rel in sorted(set(measured) - set(recorded)):",
    ): (
        "healthy-empty. The measured-not-recorded half of the record-tree "
        "membership check; a hit means a module went over the cap with no "
        "record. Empty is the sidecar being in sync."
    ),
    (
        "tests/gates/test_module_size_budget.py",
        "for rel in sorted(set(recorded) - set(measured)):",
    ): (
        "healthy-empty. The recorded-not-measured half of the same check; a "
        "hit means a record outlived the module it describes."
    ),
    (
        "tests/gates/test_no_new_hardware_facts.py",
        "for match in pattern.finditer(text):",
    ): (
        "healthy-empty. Iterates the hardware-fact regex hits inside "
        "`tan/**`; ADR-0017 invariant I-26 says every such fact lives once "
        "under alp-sdk's `metadata/**`, and none has leaked."
    ),
    (
        "tests/gates/test_oracle_capture_store_is_labelled.py",
        "for name in _PATTERN_LOAD_CALL.findall(text):",
    ): (
        "forward-looking, and this is the declaration nobody had made. Its "
        "sibling two lines up (`_PATTERN_CAPTURES_DIR`) DOES iterate, which "
        "is why the reader-map this builds is not empty and the file's "
        "assertions are not vacuous -- but the `load(...)`-call spelling "
        "matches nothing in the tree today, so this half measures 0. Kept "
        "because a future reader that uses the helper instead of the "
        "directory constant must still be attributed."
    ),
    (
        "tests/gates/test_release_docs_match_the_workflow.py",
        "for hit in _uncaveated(rel, patterns[registry]):",
    ): (
        "healthy-empty. Iterates install commands presented as usable for a "
        "registry this release does not publish to. No live doc carries one."
    ),
    (
        "tests/gates/test_subprocess_env_routes_through_the_helper.py",
        "for kw in call.keywords:",
    ): (
        "forward-looking. The keyword half of the trusted-name fixpoint; the "
        "positional half four lines up DOES iterate. No bare call to the "
        "helper passes its env by keyword today, so this branch is kept for "
        "symmetry rather than measured."
    ),
}


#: Gate FILES that execute no `assert` at all in the `pull_request`
#: configuration (no `ALP_SDK_ROOT`), with the reason. This is the COARSE
#: skip-guard check tan-cli#1145 sanctions and nothing finer: it says "this
#: file asserted nothing today", not "this file's assertions are
#: load-bearing", which does not look mechanisable.
#:
#: Measured note, against tan-cli#1145's own prediction:
#: `test_planner_relocation_freshness.py` was expected here and is NOT --
#: measured on `dev` at `658f2e37` it executes assertions with no
#: `ALP_SDK_ROOT` bound. The prediction was wrong; the measurement stands.
ALLOWED_ZERO_ASSERT_FILES: dict[str, str] = {
    "tests/gates/test_example_catalog_cores_selector_agrees_with_planner.py": (
        "a module-level `pytestmark = pytest.mark.skipif(...)` on "
        "ALP_SDK_ROOT. Every test in the file compares tan's example "
        "catalogue against a bound alp-sdk checkout; with none bound there "
        "is nothing to compare. It DOES assert in the `seam1-plan-shape` "
        "leg, which binds one."
    ),
    "tests/gates/test_jlink_aen_device_freshness.py": (
        "a `pytest.skip(...)` on ALP_SDK_ROOT, for the same reason: the "
        "J-Link AEN device table is compared against alp-sdk's, and without "
        "a checkout there is no oracle. It DOES assert in `seam1-plan-shape`."
    ),
}
