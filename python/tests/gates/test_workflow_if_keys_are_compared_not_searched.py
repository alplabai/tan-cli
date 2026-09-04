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
enforced, not decorative), and never against a string it is only PART of.
Refused, therefore:

  * `<needle> in guard` / `not in` -- the guard as the CONTAINER, a substring
    search. Polarity, negation and whitespace all live inside the expression,
    so this frees every character that matters.
  * `guard in "<a str literal>"` / `guard in f"..."` -- the same substring
    search with the operands swapped. See below.
  * `guard is "<a str literal>"` / `is not` -- an identity comparison, which
    can be False for two equal strings. See below.
  * `guard < "z"` and the rest of the ordering operators, which are
    meaningless on a guard expression and which a constant that stopped being
    defined would still satisfy.

The guard on the ELEMENT side is refused only when the CONTAINER is a string.
`step.get("if") in {EXPR_A, EXPR_B}` and `guard in (EXPR_A, EXPR_B)` are
whole-value tests that move the entire expression with any rewrite, so
refusing those would tell the author to do the thing they already did. But
`guard in "<a str literal>"` and `guard in f"..."` are SUBSTRING searches with
the operands swapped, and a string container is decidable from the AST with no
name resolution. Measured against
`ALLOWED_GUARDS = "steps.sdk_list.outputs.outage != 'true'"`, Python answers
True to all four of `"steps.sdk_list.outputs.outage != 'true'"`,
`'steps.sdk_list.outputs.outage'`, `"outage != 'true'"` and `''` -- the last of
which means a step carrying no `if:` key at all passes it. An assertion on a
workflow guard that cannot fail is tan-cli#1145's own headline class, so a
string container is refused on whichever side the guard sits.

`is` and `is not` leave `_ALLOWED_OPS` on the same terms: `guard is "<str>"` is
an IDENTITY test that can be False for equal strings, and the compile-time
warning that says so is exactly what `_parse` swallows (see `_parse`).

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
  * a guard on the ELEMENT side of a bare `Name` CONTAINER
    (`guard in ALLOWED_GUARDS`). If that name holds a collection this is the
    whole-value test the gate asks for; if it holds a `str` it is a substring
    search, and `"" in ALLOWED_GUARDS` is True, so the assertion passes on a
    step carrying no `if:` key at all. Telling the two apart needs name
    resolution this per-file walk does not do, and refusing the shape outright
    would red the named-collection form -- `guard in ALLOWED_GUARDS` where
    `ALLOWED_GUARDS` is a `frozenset` is the whole-value test this gate asks
    for, and telling an author to inline it would be noise. A `str` or f-string
    container written INLINE is decidable and IS refused. Measured across the
    267 files this scan reads: 0 live sites put a guard on the element side of
    a `Name` container, so this is a disclosed gap, not a live miss.
  * a name bound to a COMPREHENSION whose guard read sits in the condition
    rather than the element. Measured:
    `off = {job for job, body in _publish_jobs().items() if
    _literal_false(body.get("if"))}` in
    `test_release_docs_match_the_workflow.py` taints `off`, which holds job
    NAMES. That is the over-approximation direction -- a false positive, not a
    miss -- and it is named here because `_tainted_names` promises exactly one
    of the two.

Within a module's local names the walk is a fixpoint (see `_tainted_names`),
so a chain of rebinds does not lose it. What is open is the module boundary,
the attribute/subscript boundary and the `Name` container; closing any of the
three costs more false positives, or more name resolution, than tan-cli#1145's
rule is worth. Every one of them has a control, so the answer is pinned and a
later narrowing reds a test rather than passing quietly.
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
#: on a guard expression, and `In` / `NotIn` where either operand is a string
#: -- the guard as the CONTAINER (`<needle> in guard`), and the guard as the
#: ELEMENT of a `str` or f-string container, which is the same substring search
#: written round the other way. `Is` / `IsNot` are allowed only when neither
#: operand is a `str` constant; see `_suspect_operands`.
_ALLOWED_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


def _parse(source: str) -> ast.AST:
    """`ast.parse`, without republishing the parsed file's own SyntaxWarnings.

    Widening the scan to the whole test tree brought in modules whose string
    literals the compiler warns about at parse time (tan-cli#1167's invalid
    escape sequence). Those warnings belong to the file that owns them, not to
    this gate's summary line, and pytest's own collection still surfaces them.

    The filter is by category, so it also swallows `'"is" with 'str' literal.
    Did you mean "=="?'` -- a warning about a shape this gate has an opinion
    about. Rather than rent that opinion from the compiler, `_suspect_operands`
    refuses `Is` / `IsNot` against a `str` constant outright, which is an
    assertion rather than a warning and survives any filter.
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


def _items_value_target(
    targets: list[ast.AST], source: ast.AST
) -> list[ast.AST] | None:
    """`[<value>]` for `for <key>, <value> in <expr>.items()`, else `None`.

    A mapping unpack puts the KEY first, and a key is a step name, not a
    guard. Tainting the whole tuple spilled the guard onto identifiers as
    generic as `name`: measured before this narrowing,
    `test_getting_started_sdk_outage_probe.py`'s taint set was
    `['found', 'name', 'wrong']`, so an unrelated `assert "..." in name`
    written anywhere later in that 230-line module would have red with a
    paragraph about workflow `if:` keys. `found` stays tainted on its own
    merits -- it is also bound directly from `_step(...).get("if")`.
    """
    if not (
        isinstance(source, ast.Call)
        and isinstance(source.func, ast.Attribute)
        and source.func.attr == "items"
        and not source.args
        and not source.keywords
    ):
        return None
    if len(targets) != 1 or not isinstance(targets[0], ast.Tuple):
        return None
    if len(targets[0].elts) != 2:
        return None
    return [targets[0].elts[1]]


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
        targets = _items_value_target(targets, source) or targets
        names: list[str] = []
        for target in targets:
            names.extend(_bound_names(target))
        if names:
            pairs.append((names, source))
    return pairs


def _tainted_names(tree: ast.AST) -> set[str]:
    """Local names bound to an expression that reads an `if:` key.

    Flow-insensitive and module-wide on purpose: a name that EVER holds a
    guard expression is treated as holding one everywhere. Missing the
    two-statement shape costs the defect this gate exists for, so the walk
    over-approximates -- but "a false positive a reviewer resolves in one
    line" understates what that costs, and this is what it actually is: the
    taint lands on a NAME, module-wide, and the name can be generic. Measured
    on the shipped tree, the three files with any taint at all are

        gates/test_getting_started_sdk_outage_probe.py: ['found', 'wrong']
        gates/test_interpreter_policy.py:               ['guard']
        gates/test_release_docs_match_the_workflow.py:  ['off']

    `off` holds job NAMES, not guards -- it is bound to a comprehension whose
    only `if:` read sits in the CONDITION -- so any later `<needle> in off` in
    that module reds on a paragraph about workflow guards. That is the shape of
    the cost: not one line of review, but an unrelated author in an unrelated
    module getting a red about `if:` keys. `name` used to be in the first set
    too, which is why `_items_value_target` exists.

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


def _str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _string_container(node: ast.AST) -> bool:
    """True for a container operand that makes `in` a SUBSTRING search.

    A `str` literal or an f-string, both decidable from the AST with no name
    resolution. `x in "<str>"` is a substring test however whole-valued `x`
    looks, and `"" in "<str>"` is True -- so a guard read from a step that
    carries no `if:` key at all satisfies it.
    """
    return _str_constant(node) or isinstance(node, ast.JoinedStr)


def _suspect_operands(
    op: ast.cmpop, left: ast.AST, right: ast.AST
) -> tuple[ast.AST, ...] | None:
    """The operands a REFUSED comparison makes suspect, or `None` if allowed.

    * `in` / `not in`: the CONTAINER is always suspect (`<needle> in guard`,
      the tan-cli#1137 shape). The ELEMENT is suspect too when the container is
      a `str` or an f-string, because that is the same substring search with
      the operands swapped. A container that is a literal collection, or a bare
      `Name`, leaves the element alone -- the first is a genuine whole-value
      test, the second is the disclosed blind spot in the module docstring.
    * `is` / `is not` against a `str` constant: an identity test that can be
      False for equal strings.
    * anything else outside `_ALLOWED_OPS`: the ordering operators.
    """
    if isinstance(op, (ast.In, ast.NotIn)):
        return (left, right) if _string_container(right) else (right,)
    if isinstance(op, (ast.Is, ast.IsNot)) and (
        _str_constant(left) or _str_constant(right)
    ):
        return (left, right)
    if isinstance(op, _ALLOWED_OPS):
        return None
    return (left, right)


def _membership_violations(source: str, label: str) -> list[str]:
    """`<label>:<line>` for every REFUSED comparison against an `if:`-key value.

    `_suspect_operands` decides which operands a given operator makes suspect;
    this walk only supplies the `Compare` nodes and the tainted-name set.
    """
    tree = _parse(source)
    tainted = _tainted_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            suspects = _suspect_operands(op, operands[index], operands[index + 1])
            if suspects is None:
                continue
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
        "the ELEMENT side of a LITERAL COLLECTION -- "
        "`guard in {ALLOWED_A, ALLOWED_B}` -- is a whole-value test and is "
        "not reported here. Membership in a `str` or f-string container IS "
        "reported however it is written round: `guard in \"a != b\"` is a "
        "substring search, and `\"\" in \"a != b\"` is True, so it passes on a "
        "step with no `if:` key at all.):\n  "
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

#: Whole-value membership: the guard is the ELEMENT and the container is a
#: LITERAL COLLECTION. Each of these moves the entire expression with any
#: rewrite, which is exactly what this gate asks for, so refusing them would
#: tell the author to do the thing they already did.
_FABRICATED_WHOLE_VALUE = '''
def test_x():
    assert step.get("if") in {EXPR_A, EXPR_B}
    guard = step.get("if")
    assert guard in (EXPR_A, EXPR_B)
    assert guard in [EXPR_A, EXPR_B]
    assert guard in frozenset((EXPR_A, EXPR_B))
'''

#: NOT a blessing -- the disclosed BLIND SPOT, kept as a control so the gap is
#: measured rather than assumed, and so a future narrowing of it reds here
#: instead of arguing with a green test. Whether `ALLOWED_GUARDS` holds a
#: collection (a whole-value test) or a `str` (a substring search that the
#: empty string satisfies) needs name resolution this per-file walk does not
#: do. Measured across the files this scan reads: 0 live sites of this shape,
#: so allowing it costs nothing today.
_FABRICATED_NAME_CONTAINER = '''
def test_x():
    guard = step.get("if")
    assert guard in ALLOWED_GUARDS
'''

#: The guard is on the ELEMENT side, but the CONTAINER is a `str` literal, so
#: `in` is a substring search with the operands swapped. Measured against
#: `ALLOWED_GUARDS = "steps.sdk_list.outputs.outage != 'true'"`, Python answers
#: True to the whole expression, to `'steps.sdk_list.outputs.outage'`, to
#: `"outage != 'true'"` AND to `''` -- the last meaning a step carrying no
#: `if:` key at all passes, which is an assertion that cannot fail.
_FABRICATED_STR_CONSTANT_CONTAINER = """
def test_x():
    assert step.get("if") in "steps.sdk_list.outputs.outage != 'true'"
"""

#: The same defect assembled at runtime. An f-string is a `JoinedStr`, equally
#: decidable from the AST with no name resolution.
_FABRICATED_FSTRING_CONTAINER = '''
def test_x():
    guard = step.get("if")
    assert guard in f"{EXPR_A} or {EXPR_B}"
'''

#: `is` against a `str` literal is an IDENTITY test that can be False for two
#: equal strings. CPython warns about it at compile time -- `"is" with 'str'
#: literal. Did you mean "=="?` -- and that warning is exactly what `_parse`
#: swallows, so the gate holds the opinion itself instead of renting it.
_FABRICATED_IS_STR_LITERAL = """
def test_x():
    assert step.get("if") is "${{ github.event_name == 'push' }}"
"""

#: The KEY of a `.items()` unpack is a step name, not a guard. Before
#: `_items_value_target` the whole tuple was tainted and this reported
#: `fab:4` -- an unrelated substring assertion on `name`, red with a paragraph
#: about workflow `if:` keys. The guard half must stay tainted, which the
#: `== GATE_EXPRESSION` line does not prove; `_FABRICATED_ITEMS_UNPACK` does.
_FABRICATED_ITEMS_KEY = '''
def test_x():
    wrong = {name: _step(name).get("if") for name in STEPS}
    for name, guard in wrong.items():
        assert "tan-cli#840" in name
        assert guard == GATE_EXPRESSION
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
    """The guard as the ELEMENT of a set/tuple/list/frozenset LITERAL is a
    whole-value test, which is what this gate asks for."""
    assert _membership_violations(_FABRICATED_WHOLE_VALUE, "fab") == []


def test_the_name_container_blind_spot_is_measured_not_assumed():
    """`guard in ALLOWED_GUARDS` is ALLOWED, and that is a gap, not a
    blessing. If `ALLOWED_GUARDS` is a `str` this is a substring search that
    even an absent `if:` key satisfies; the walk cannot tell. The module
    docstring enumerates it, and this control pins the current answer so
    closing the gap reds here rather than passing silently."""
    assert _membership_violations(_FABRICATED_NAME_CONTAINER, "fab") == []


def test_the_detector_fires_on_a_str_literal_container():
    """The element side is suspect too once the container is a string.

    `"" in "<any str>"` is True, so this shape passes on a step carrying no
    `if:` key at all -- tan-cli#1145's own headline class, an assertion on a
    workflow guard that cannot fail.
    """
    assert _membership_violations(_FABRICATED_STR_CONSTANT_CONTAINER, "fab") == [
        "fab:3"
    ]


def test_the_detector_fires_on_an_fstring_container():
    """Same search, assembled at runtime. A `JoinedStr` container is as
    decidable from the AST as a `Constant` one."""
    assert _membership_violations(_FABRICATED_FSTRING_CONTAINER, "fab") == ["fab:4"]


def test_the_detector_fires_on_an_identity_test_against_a_str_literal():
    """`guard is "<str>"` can be False for equal strings, and the compile-time
    warning that says so is the one `_parse` filters out -- so the refusal
    lives here, as an assertion, not as a warning that a filter can drop."""
    assert _membership_violations(_FABRICATED_IS_STR_LITERAL, "fab") == ["fab:3"]


def test_the_key_of_an_items_unpack_is_not_tainted():
    """Taint must not escape onto `name`. Before `_items_value_target` this
    reported `fab:4`."""
    assert _membership_violations(_FABRICATED_ITEMS_KEY, "fab") == []


def test_the_detector_does_not_taint_an_attribute_or_subscript_receiver():
    """`self.guard = step.get("if")` must not taint `self`."""
    assert _membership_violations(_FABRICATED_RECEIVER_COLLATERAL, "fab") == []
