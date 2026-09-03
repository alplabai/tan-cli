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
be compared (`==`, `!=`, `is`, `is not`) but never tested for membership
(`in`, `not in`).** Polarity, negation and whitespace all live inside the
expression, so a substring test frees every character that matters. An exact
comparison forces a semantically-equivalent rewrite (``${{ ! inputs.x }}``,
``${{ inputs.x != true }}``) to move the expected constant with it, which is
the point: a guard this consequential should not slip through on "it means the
same thing".

Two sites already conform and are what makes this a rule rather than a
heuristic -- `test_getting_started_sdk_outage_probe.py`'s
`step.get("if") == GATE_EXPRESSION`, and #1137's own fix, which converged
independently on `guard == RELEASE_GUARD_EXPRESSION`.

ASSIGNMENT TRACKING IS LOAD-BEARING, not thoroughness. The defect shape is
`guard = <if-key read>` followed by `<needle> in guard` -- two statements. A
detector that only looked at direct `Compare` operands would miss the single
instance this gate exists for, which is the punchline tan-cli#1145 warns
about: a meta-gate that cannot catch its own motivating example.

WHAT THIS DELIBERATELY DOES NOT DO. It does not judge substring assertions in
general. tan-cli#1145's audit found 70 such candidates and read 69 of them:
essentially all assert on driven subprocess stdout/stderr, where presence
genuinely IS the property. Only the `if:`-key surface is decidable, so only it
is gated.
"""
import ast
import pathlib

GATES_DIR = pathlib.Path(__file__).resolve().parent

#: Every `if:`-key read the scan must still be finding. A floor, not a count:
#: the hazard tan-cli#1145 names for a meta-gate is that its scan silently
#: stops matching, reports zero candidates and goes green. Measured 9 on
#: `dd6ac839`; floored at 6 so ordinary churn does not red it but a scanner
#: that has stopped working does. Pattern borrowed from
#: `test_inert_option_markers.py`'s `assert len(ALL_OPTIONS) >= 400`.
MIN_IF_KEY_READS = 6

#: Comparison operators that READ the whole value. Everything else -- `In`,
#: `NotIn`, and the ordering operators, which are meaningless on a guard
#: expression -- is refused.
_ALLOWED_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


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


def _tainted_names(tree: ast.AST) -> set[str]:
    """Local names bound to an expression that reads an `if:` key.

    Flow-insensitive and module-wide on purpose: a name that EVER holds a
    guard expression is treated as holding one everywhere. Over-approximating
    here costs a false positive that a reviewer resolves in one line; missing
    the two-statement shape costs the defect this gate exists for.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if node.value is None or not _contains_if_key_read(node.value):
            continue
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def _membership_violations(source: str, label: str) -> list[str]:
    """`<label>:<line>` for every membership test against an `if:`-key value."""
    tree = ast.parse(source)
    tainted = _tainted_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        for operand in (node.left, *node.comparators):
            touches = _contains_if_key_read(operand) or any(
                isinstance(sub, ast.Name) and sub.id in tainted
                for sub in ast.walk(operand)
            )
            if touches:
                out.append(f"{label}:{node.lineno}")
                break
    return out


def _if_key_read_count(source: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(source)) if _is_if_key_read(node))


def _gate_sources() -> list[tuple[str, str]]:
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(GATES_DIR.glob("test_*.py"))
    ]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_no_gate_searches_a_workflow_if_key_for_a_substring():
    violations: list[str] = []
    for label, source in _gate_sources():
        violations.extend(_membership_violations(source, label))
    assert not violations, (
        "A workflow `if:` value is tested for MEMBERSHIP here, which leaves "
        "every character that matters free -- polarity above all. tan-cli#1137 "
        "shipped `RELEASE_OPT_OUT_INPUT in guard`, and deleting the `!` from "
        "`${{ !inputs.skip_ceiling_interpreter }}` still passed.\n"
        "Compare the whole expression against a named constant instead, the "
        "way test_getting_started_sdk_outage_probe.py and "
        "test_interpreter_policy.py already do:\n  " + "\n  ".join(violations)
    )


def test_the_scan_still_finds_the_if_key_reads_it_is_scanning():
    """The candidate floor. A meta-gate's own failure mode is a scan that
    stopped matching: it then reports zero violations and goes green forever."""
    total = sum(_if_key_read_count(source) for _, source in _gate_sources())
    assert total >= MIN_IF_KEY_READS, (
        f"the `if:`-key scan found {total} reads across python/tests/gates/, "
        f"below the floor of {MIN_IF_KEY_READS}. Either the workflow-reading "
        f"gates were deleted, or -- far likelier -- `_is_if_key_read` stopped "
        f"matching the shape they use and this gate is now checking nothing."
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
