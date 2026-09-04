# SPDX-License-Identifier: Apache-2.0
"""A gate reading a GitHub Actions `if:` key must COMPARE it, never SEARCH it.

tan-cli#1145, first of the two mechanical shapes. The motivating instance is
tan-cli#1137's own review finding: the guard gate asserted

    assert RELEASE_OPT_OUT_INPUT in guard

where `guard` is the workflow string ``${{ !inputs.skip_ceiling_interpreter }}``.
A substring test, so deleting the `!` still passed -- measured `32 passed`
before and after the mutation. That one character would have deleted the
ceiling job from every PR *and* run it on the release path, and the gate
written to prevent exactly that was blind to it.

The rule is deliberately small: **a value read out of a workflow `if:` key may
only be compared with `==`, `!=`, `is` or `is not`** (`_ALLOWED_OPS`, which is
enforced, not decorative). Two shapes are therefore refused:

  * `<needle> in guard` / `not in` -- the guard as the CONTAINER, a substring
    search. Polarity, negation and whitespace all live inside the expression,
    so this frees every character that matters.
  * `guard < "z"` and the rest of the ordering operators, which are
    meaningless on a guard expression and which a constant that stopped being
    defined would still satisfy.

The guard on the ELEMENT side is NOT refused: `step.get("if") in {EXPR_A,
EXPR_B}`, `guard in (EXPR_A, EXPR_B)` and `guard in ALLOWED_GUARDS` are
whole-value tests that move the entire expression with any rewrite, which is
exactly what this gate asks for. Refusing them would tell the author to do the
thing they already did.

An exact comparison forces a semantically-equivalent rewrite
(``${{ ! inputs.x }}``, ``${{ inputs.x != true }}``) to move the expected
constant with it, which is the point: a guard this consequential should not
slip through on "it means the same thing".

Two sites already conform and are what makes this a rule rather than a
heuristic -- `test_getting_started_sdk_outage_probe.py`'s
`step.get("if") == GATE_EXPRESSION`, and #1137's own fix, which converged
independently on `guard == RELEASE_GUARD_EXPRESSION`.

BINDING TRACKING IS LOAD-BEARING, not thoroughness. The defect shape is
`guard = <if-key read>` followed by `<needle> in guard` -- two statements. A
detector that only looked at direct `Compare` operands would miss the single
instance this gate exists for, which is the punchline tan-cli#1145 warns
about: a meta-gate that cannot catch its own motivating example.

Assignment alone is not enough either, and this is measured rather than
argued: rewritten as `wrong = {n: _step(n).get("if") for n in STEPS}` then
`for name, guard in wrong.items()`, that same defect -- with #1137's polarity
fully inverted in the workflow -- reported CLEAN against an assignment-only
walk, in the very file this docstring cites above as conforming. The read
floor did not save it (reads went 9 to 8, still above 6). So `_tainted_names`
covers the `for` target, the comprehension target, `with ... as` and the
walrus as well, treats an already-tainted name as a guard source, and
iterates to a fixpoint.

WHAT THIS DELIBERATELY DOES NOT DO. It does not judge substring assertions in
general. tan-cli#1145's audit found 70 such candidates and read 69 of them:
essentially all assert on driven subprocess stdout/stderr, where presence
genuinely IS the property. Only the `if:`-key surface is decidable, so only it
is gated.

It also does not follow a guard out of the module's own local names. Every
form below is MEASURED as missed, not guessed at, and each one needs a rule
this walk does not have:

  * a guard passed as a function PARAMETER, or RETURNED / YIELDED out of one
    (`def f(): return step.get("if")` then `g = f()`) -- both need
    interprocedural flow.
  * a guard stored on an ATTRIBUTE or a SUBSCRIPT and read back through it
    (`self.guard = step.get("if")` then `NEEDLE in self.guard`;
    `d["k"] = ...` then `NEEDLE in d["k"]`), and the class-attribute form
    `C.guard`. Binding those would mean tainting the RECEIVER, which taints
    `self` and reds every unrelated `NEEDLE in self.stdout` in the module.
  * a guard IMPORTED from another module -- the scan is per-file.

Within a module's local names the walk is a fixpoint (see `_tainted_names`),
so a chain of rebinds does not lose it. It is the module boundary and the
attribute/subscript boundary that are open, and closing either costs more
false positives than tan-cli#1145's rule is worth.
"""
import ast
import pathlib
import warnings

GATES_DIR = pathlib.Path(__file__).resolve().parent

#: The whole test tree, not just `gates/`. A non-recursive `test_*.py` glob
#: over `gates/` alone skipped `conftest.py` and the `_*_core.py` helpers in
#: that same directory, and everything outside it -- so a workflow-reading
#: helper one directory up would have been unwatched. Measured: 0 additional
#: `if:`-key reads and 0 additional violations at the widening, so the scan
#: costs nothing today and covers the whole surface tomorrow.
TESTS_DIR = GATES_DIR.parent

#: Every `if:`-key read the scan must still be finding. A floor, not a count:
#: the hazard tan-cli#1145 names for a meta-gate is that its scan silently
#: stops matching, reports zero candidates and goes green. Measured 9 on
#: `dd6ac839`; floored at 6 so ordinary churn does not red it but a scanner
#: that has stopped working does. Pattern borrowed from
#: `test_inert_option_markers.py`'s `assert len(ALL_OPTIONS) >= 400`.
MIN_IF_KEY_READS = 6

#: Comparison operators that READ the whole value. Everything else is refused
#: by `_membership_violations`: the ordering operators, which are meaningless
#: on a guard expression, and `In` / `NotIn` where the guard is the CONTAINER
#: (`<needle> in guard`) rather than the element.
_ALLOWED_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


def _parse(source: str) -> ast.AST:
    """`ast.parse`, without republishing the parsed file's own SyntaxWarnings.

    Widening the scan to the whole test tree brought in modules whose string
    literals the compiler warns about at parse time. Those warnings belong to
    the file that owns them, not to this gate's summary line.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def _is_if_key_read(node: ast.AST) -> bool:
    """True for `<expr>["if"]` and `<expr>.get("if", ...)`.

    Keyed on the literal `"if"` rather than on the receiver, because the
    receiver is a workflow dict under half a dozen different local names and
    pinning those would make this gate a list of the call sites it is supposed
    to discover.
    """
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "if"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "get" or not node.args:
            return False
        first = node.args[0]
        return isinstance(first, ast.Constant) and first.value == "if"
    return False


def _contains_if_key_read(node: ast.AST) -> bool:
    return any(_is_if_key_read(child) for child in ast.walk(node))


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(sub, ast.Name) and sub.id in names for sub in ast.walk(node)
    )


def _touches_guard(node: ast.AST, tainted: set[str]) -> bool:
    return _contains_if_key_read(node) or _mentions(node, tainted)


def _bound_names(target: ast.AST) -> list[str]:
    """The local names a binding target binds.

    A bare `Name`, or a `Tuple`/`List`/`Starred` of them, and nothing else.
    An attribute or subscript target binds no local name at all -- walking it
    for every `Name` would taint the RECEIVER instead, so
    `self.guard = step.get("if")` would taint `self` and any later
    `assert "x" in self.stdout` in the same module would red, and
    `workflow["jobs"][j]["if"] = ...` would taint `workflow`.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for element in target.elts:
            out.extend(_bound_names(element))
        return out
    return []


def _bindings(tree: ast.AST) -> list[tuple[list[str], ast.AST]]:
    """`(bound names, source expression)` for every binding form that can
    carry a guard from one name to another.

    Assignment alone is not enough: the guard reaches a name through a `for`
    target, a comprehension target, a `with ... as`, and a walrus just as
    readily, and the shape measured to slip past an assignment-only walk is a
    dict comprehension followed by `for name, guard in wrong.items()`.
    """
    pairs: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        targets: list[ast.AST]
        source: ast.AST | None
        if isinstance(node, ast.Assign):
            targets, source = list(node.targets), node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, source = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, source = [node.target], node.iter
        elif isinstance(node, ast.comprehension):
            targets, source = [node.target], node.iter
        elif isinstance(node, ast.withitem):
            if node.optional_vars is None:
                continue
            targets, source = [node.optional_vars], node.context_expr
        elif isinstance(node, ast.NamedExpr):
            targets, source = [node.target], node.value
        else:
            continue
        if source is None:
            continue
        names: list[str] = []
        for target in targets:
            names.extend(_bound_names(target))
        if names:
            pairs.append((names, source))
    return pairs


def _tainted_names(tree: ast.AST) -> set[str]:
    """Local names bound to an expression that reads an `if:` key.

    Flow-insensitive and module-wide on purpose: a name that EVER holds a
    guard expression is treated as holding one everywhere. Over-approximating
    here costs a false positive that a reviewer resolves in one line; missing
    the two-statement shape costs the defect this gate exists for.

    Iterated to a FIXPOINT, because one rebind is enough to lose the trail: a
    single pass sees `a = step.get("if")` but not the `b = a` behind it, and
    the measured miss was exactly that -- `wrong = {... .get("if") ...}` then
    `for name, guard in wrong.items()`, where `guard` is two hops from the
    read. A name already in the set is itself a guard source for a later
    binding, so the walk repeats until nothing new is added.
    """
    bindings = _bindings(tree)
    names: set[str] = set()
    while True:
        grew = False
        for bound, source in bindings:
            if not _touches_guard(source, names):
                continue
            for name in bound:
                if name not in names:
                    names.add(name)
                    grew = True
        if not grew:
            return names


def _membership_violations(source: str, label: str) -> list[str]:
    """`<label>:<line>` for every REFUSED comparison against an `if:`-key value.

    Two refused shapes, matching `_ALLOWED_OPS`:

    * `in` / `not in` where the guard is the CONTAINER -- `<needle> in guard`,
      the tan-cli#1137 shape. The guard on the ELEMENT side is a whole-value
      test (`guard in {EXPR_A, EXPR_B}`, `guard in ALLOWED_GUARDS`) which
      moves the entire expression with any rewrite, so it is exactly what this
      gate asks for and is not refused.
    * any operator outside `_ALLOWED_OPS` on either side -- the ordering
      operators, which are meaningless on a guard expression.
    """
    tree = _parse(source)
    tainted = _tainted_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            if isinstance(op, _ALLOWED_OPS):
                continue
            if isinstance(op, (ast.In, ast.NotIn)):
                suspects: tuple[ast.AST, ...] = (operands[index + 1],)
            else:
                suspects = (operands[index], operands[index + 1])
            if any(_touches_guard(operand, tainted) for operand in suspects):
                out.append(f"{label}:{node.lineno}")
                break
    return out


def _if_key_read_count(source: str) -> int:
    return sum(1 for node in ast.walk(_parse(source)) if _is_if_key_read(node))


def _gate_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(TESTS_DIR).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(TESTS_DIR.rglob("*.py"))
    ]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_no_gate_searches_a_workflow_if_key_for_a_substring():
    violations: list[str] = []
    for label, source in _gate_sources():
        violations.extend(_membership_violations(source, label))
    assert not violations, (
        "A workflow `if:` value is SEARCHED here (`<needle> in guard`) or "
        "compared with an operator that does not read the whole value, which "
        "leaves every character that matters free -- polarity above all. "
        "tan-cli#1137 shipped `RELEASE_OPT_OUT_INPUT in guard`, and deleting "
        "the `!` from `${{ !inputs.skip_ceiling_interpreter }}` still "
        "passed.\n"
        "Compare the whole expression with `==` / `!=` / `is` / `is not` "
        "against a named constant instead, the way "
        "test_getting_started_sdk_outage_probe.py and "
        "test_interpreter_policy.py already do. (Membership with the guard on "
        "the ELEMENT side -- `guard in {ALLOWED_A, ALLOWED_B}` -- is a "
        "whole-value test and is not reported here.):\n  "
        + "\n  ".join(violations)
    )


def test_the_scan_still_finds_the_if_key_reads_it_is_scanning():
    """The candidate floor. A meta-gate's own failure mode is a scan that
    stopped matching: it then reports zero violations and goes green forever."""
    total = sum(_if_key_read_count(source) for _, source in _gate_sources())
    assert total >= MIN_IF_KEY_READS, (
        f"the `if:`-key scan found {total} reads across python/tests/, "
        f"below the floor of {MIN_IF_KEY_READS}. Either the workflow-reading "
        f"gates were deleted, or -- far likelier -- `_is_if_key_read` stopped "
        f"matching the shape they use and this gate is now checking nothing."
    )


def test_the_scan_reaches_past_the_gates_test_files():
    """The widening has no violation to catch today, so nothing else reds if
    it is narrowed back. This is what makes it a checked property: the scan
    must reach the non-`test_*.py` modules in `gates/` and at least one file
    outside `gates/` entirely, or a workflow-reading helper there would be
    unwatched."""
    scanned = {label for label, _ in _gate_sources()}
    for expected in (
        "gates/conftest.py",
        "gates/_module_size_budget_core.py",
        "gates/_planner_oracle_ref_matching_core.py",
        "conftest.py",
    ):
        assert expected in scanned, (
            f"{expected} is outside the `if:`-key scan. A non-recursive "
            f"`gates/test_*.py` glob skipped every one of these."
        )


# ---------------------------------------------------------------------------
# Negative controls -- fabricated input, so the detector is proven to fire
# ---------------------------------------------------------------------------
_FABRICATED_DIRECT = '''
def test_x():
    assert NEEDLE in step.get("if")
'''

_FABRICATED_ASSIGNED = '''
def test_x():
    guard = str(ci["jobs"][JOB].get("if", "")).strip()
    assert NEEDLE in guard
'''

_FABRICATED_SUBSCRIPT = '''
def test_x():
    assert "!" not in job["if"]
'''

_FABRICATED_CONFORMING = '''
def test_x():
    guard = ci["jobs"][JOB].get("if", "")
    assert guard == RELEASE_GUARD_EXPRESSION
    assert step.get("if") is None
    assert "needle" in some_unrelated_run_output
'''

#: The shape that slipped past an assignment-only walk. Two hops from the read
#: -- a dict comprehension, then a `.items()` unpack -- and it is #1137's
#: defect with its polarity inverted, reintroduced in the file this gate's
#: docstring cites as conforming. Measured on the pre-fix walk: reported
#: clean, and the read floor did not save it (reads went 9 -> 8, still >= 6).
_FABRICATED_ITEMS_UNPACK = '''
def test_x():
    wrong = {name: _step(name).get("if") for name in SDK_DEPENDENT_STEPS}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
'''

#: One rebind is all it took: an assignment-only walk taints `a` and never
#: reaches the `b` behind it, because `b = a.strip()` reads no `if:` key of its
#: own -- it only mentions a name that already holds one.
_FABRICATED_REBIND = '''
def test_x():
    a = step.get("if")
    b = a.strip()
    assert NEEDLE in b
'''

#: `_ALLOWED_OPS` refuses the ordering operators too -- they are meaningless on
#: a guard expression, and a constant that stopped being defined would still
#: satisfy one.
_FABRICATED_ORDERING = '''
def test_x():
    assert step.get("if") < "z"
'''

#: Whole-value membership: the guard is the ELEMENT, not the container. Each
#: of these moves the entire expression with any rewrite, which is exactly what
#: this gate asks for, so refusing them would tell the author to do the thing
#: they already did.
_FABRICATED_WHOLE_VALUE = '''
def test_x():
    assert step.get("if") in {EXPR_A, EXPR_B}
    guard = step.get("if")
    assert guard in (EXPR_A, EXPR_B)
    assert guard in [EXPR_A, EXPR_B]
    assert guard in frozenset((EXPR_A, EXPR_B))
    assert guard in ALLOWED_GUARDS
'''

#: The binding whose SOURCE is tainted only later in the walk. `ast.walk` is
#: breadth-first, so `guard = raw` (function-body depth) is visited BEFORE the
#: `raw = step.get("if")` nested inside the loop above it -- which is why the
#: walk has to iterate rather than sweep once. Measured: a single sweep taints
#: `raw` alone and reports this clean.
_FABRICATED_NESTED_PRODUCER = '''
def test_x():
    for step in _steps():
        raw = step.get("if")
    guard = raw
    assert NEEDLE in guard
'''

#: An attribute or subscript target binds no local NAME. Walking it for every
#: `Name` tainted the RECEIVER instead, so this module's unrelated
#: `"tan build" in self.stdout` red on a guard it never touched.
_FABRICATED_RECEIVER_COLLATERAL = '''
def test_x(self):
    self.guard = step.get("if")
    workflow["jobs"][JOB]["if"] = GATE_EXPRESSION
    assert "tan build" in self.stdout
    assert "jobs" in workflow
'''


def test_the_detector_fires_on_a_direct_membership_test():
    assert _membership_violations(_FABRICATED_DIRECT, "fab") == ["fab:3"]


def test_the_detector_fires_on_the_two_statement_shape_that_shipped():
    """The tan-cli#1137 shape exactly: read into a local, then search it.

    This is the control that matters. A detector looking only at `Compare`
    operands passes `_FABRICATED_DIRECT` and misses this one -- and this one
    is the instance that actually shipped.
    """
    assert _membership_violations(_FABRICATED_ASSIGNED, "fab") == ["fab:4"]


def test_the_detector_fires_on_subscript_reads_and_on_not_in():
    assert _membership_violations(_FABRICATED_SUBSCRIPT, "fab") == ["fab:3"]


def test_the_detector_is_silent_on_conforming_code():
    """Including a membership test on something that is NOT an `if:` value --
    the false-positive direction, which is what would make this gate noise."""
    assert _membership_violations(_FABRICATED_CONFORMING, "fab") == []


def test_the_detector_fires_through_a_dict_items_unpack():
    """Two hops from the read, and the pre-fix walk reported it clean.

    Needs BOTH halves of the rewrite: a `for` target has to be a binding form
    at all, and an already-tainted name (`wrong`) has to count as a guard
    SOURCE for the binding behind it.
    """
    assert _membership_violations(_FABRICATED_ITEMS_UNPACK, "fab") == ["fab:5"]


def test_the_walk_iterates_rather_than_sweeping_once():
    """The control for the loop itself, not just for the tainted-name source.

    `ast.walk` is breadth-first, so a binding can be visited before the
    binding that taints its source. Measured: one sweep over the same
    bindings taints `raw` and not `guard`, and reports this clean.
    """
    assert _membership_violations(_FABRICATED_NESTED_PRODUCER, "fab") == ["fab:6"]


def test_the_detector_fires_through_a_rebind():
    assert _membership_violations(_FABRICATED_REBIND, "fab") == ["fab:5"]


def test_the_detector_fires_on_an_ordering_operator():
    """`_ALLOWED_OPS` is enforced, not decorative -- the constant named the
    ordering operators as refused while `_membership_violations` hardcoded
    `In`/`NotIn`, so `step.get("if") < "z"` went through (tan-cli#1145's own
    criterion: whatever the docstrings claim is enforced is exactly true)."""
    assert _membership_violations(_FABRICATED_ORDERING, "fab") == ["fab:3"]


def test_the_detector_is_silent_on_whole_value_membership():
    """The guard as the ELEMENT of a set/tuple/list/frozenset or a named
    collection is a whole-value test, which is what this gate asks for."""
    assert _membership_violations(_FABRICATED_WHOLE_VALUE, "fab") == []


def test_the_detector_does_not_taint_an_attribute_or_subscript_receiver():
    """`self.guard = step.get("if")` must not taint `self`."""
    assert _membership_violations(_FABRICATED_RECEIVER_COLLATERAL, "fab") == []
