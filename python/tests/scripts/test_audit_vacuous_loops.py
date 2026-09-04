# SPDX-License-Identifier: Apache-2.0
"""Negative controls for `scripts/audit_vacuous_loops.py`.

tan-cli#1145 names the hazard this file exists for: *"a meta-gate for this
defect class can become the thing it detects: if its scan silently stops
matching, it reports zero candidates and goes green."* That is not
hypothetical here -- `scripts/audit_narrow_except_contracts.py` shipped 869
lines of AST logic with no test coverage of its own and had to be retrofitted
in PR #1138. This one ships its controls with it.

Every case below feeds FABRICATED source and a FABRICATED covered-line set, so
none of it depends on a coverage run, on the real gate suite, or on which
tests happened to skip. A detector that has stopped matching cannot pass these.
"""
import importlib.util
import pathlib

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / "python" / "scripts"
    / "audit_vacuous_loops.py"
)
if not _SCRIPT.exists():  # running from a checkout laid out differently
    _SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "audit_vacuous_loops.py"

_spec = importlib.util.spec_from_file_location("audit_vacuous_loops", _SCRIPT)
avl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(avl)


def _lines(source: str) -> range:
    return range(1, len(source.splitlines()) + 2)


# ---------------------------------------------------------------------------
# The positive controls -- the detector must FIRE
# ---------------------------------------------------------------------------
_NEVER_ITERATES = '''
def test_x():
    for job, variables in _gating_variables().items():
        assert job in text
'''


def test_a_loop_whose_body_never_ran_is_reported():
    """The shape all four found instances take: header executed, body did not."""
    covered = {2, 3}  # def + the `for` header; body line 4 absent
    assert avl.find_vacuous(_NEVER_ITERATES, covered, label="fab") == [
        ("fab", 3, "for")
    ]


_MULTILINE_COMPREHENSION = '''
def test_x():
    bad = [
        name
        for name in _publish_jobs()
        if name not in KNOWN
    ]
'''


def test_an_empty_multi_line_comprehension_is_reported():
    """A comprehension's `elt` and generator conditions are its body: they are
    exactly what fails to run when the source iterable is empty."""
    covered = {2, 3}  # the assignment and the `[`; elt/condition lines absent
    found = avl.find_vacuous(_MULTILINE_COMPREHENSION, covered, label="fab")
    assert found == [("fab", 3, "listcomp")], found


_ONE_LINE_COMPREHENSION = '''
def test_x():
    bad = [name for name in _publish_jobs() if name not in KNOWN]
'''


def test_a_one_line_comprehension_is_undecidable_and_is_skipped():
    """A REAL blind spot, asserted so it is not mistaken for coverage.

    Header, `elt` and generator conditions all sit on the same line, so
    line-granularity coverage cannot separate "iterated" from "did not". The
    detector skips rather than guesses -- reporting would false-positive on
    every one-line comprehension in the tree. Found by this file's own control
    while building the detector, which is the argument for having built it.
    """
    assert avl.find_vacuous(_ONE_LINE_COMPREHENSION, {2, 3}, label="fab") == []


# ---------------------------------------------------------------------------
# The negative controls -- the detector must be SILENT
# ---------------------------------------------------------------------------
_ITERATES = '''
def test_x():
    for job in jobs:
        assert job.ok
'''


def test_a_loop_that_ran_is_not_reported():
    assert avl.find_vacuous(_ITERATES, {2, 3, 4}, label="fab") == []


_MULTILINE_IF_BODY = '''
def test_x():
    for job in jobs:
        if (job.name
                and job.enabled):
            assert job.ok
'''


def test_the_body_subtree_is_used_not_the_first_body_line():
    """tan-cli#1145 records this as the detector's ONLY two false positives
    during its audit: keying on `body[0].lineno` flags a loop whose multi-line
    `if` condition lines ARE covered. The union of the body subtree has no such
    hole, so a loop that reached its condition is not reported."""
    covered = {2, 3, 4, 5}  # header + the two condition lines; assert absent
    assert avl.find_vacuous(_MULTILINE_IF_BODY, covered, label="fab") == []


def test_an_unexecuted_loop_header_is_not_reported():
    """A loop inside a skipped test executed NOTHING, header included. That is
    a skip, not a vacuous loop, and reporting it would bury the real signal --
    tan-cli#1145 measured 33 of 37 dead assertions inside `ALP_SDK_ROOT` skips."""
    assert avl.find_vacuous(_NEVER_ITERATES, {2}, label="fab") == []


# ---------------------------------------------------------------------------
# The assertion pass, which rides along free
# ---------------------------------------------------------------------------
def test_never_executed_assertions_are_reported_separately():
    found = avl.find_never_executed_asserts(_NEVER_ITERATES, {2, 3}, label="fab")
    assert found == [("fab", 4)]


def test_an_executed_assertion_is_not_reported():
    assert avl.find_never_executed_asserts(_ITERATES, {2, 3, 4}, label="fab") == []


# ---------------------------------------------------------------------------
# The scan floor -- a detector that stopped matching must not read as clean
# ---------------------------------------------------------------------------
def test_the_detector_finds_loops_in_the_real_gate_sources():
    """Not a coverage assertion: this feeds the real gate files with EVERY line
    marked covered, so any loop it can parse is non-vacuous by construction and
    the result must be empty -- while the parse itself still has to succeed on
    all of them. A shape change that makes `find_vacuous` blind shows up here
    as an exception, not as a quiet zero."""
    gates = sorted(avl.GATES_DIR.glob("test_*.py"))
    assert len(gates) >= 40, f"only {len(gates)} gate files found"
    total_loops = 0
    for path in gates:
        source = path.read_text(encoding="utf-8")
        assert avl.find_vacuous(source, set(_lines(source)), label=path.name) == []
        total_loops += sum(
            1 for node in __import__("ast").walk(__import__("ast").parse(source))
            if isinstance(node, (__import__("ast").For, __import__("ast").ListComp))
        )
    assert total_loops >= 200, (
        f"the scan sees only {total_loops} for-loops/comprehensions across "
        f"{len(gates)} gate files, which is far below what this suite contains "
        f"-- `find_vacuous` has most likely stopped matching."
    )


def test_check_refuses_to_report_zero_from_an_absent_measurement(tmp_path, capsys):
    """The other way this class goes quiet: no coverage file, so nothing is
    found, so it looks clean. Exit 2, not 0."""
    rc = avl.main(["--coverage-file", str(tmp_path / "nope"), "--check"])
    assert rc == 2
    assert "Refusing to report zero findings" in capsys.readouterr().err
