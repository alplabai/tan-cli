#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Find loops and assertions in `python/tests/gates/` that never execute.

tan-cli#1145, second of the two mechanical shapes. The first, a workflow `if:`
key searched instead of compared, is a pure AST rule and lives in
`tests/gates/test_workflow_if_keys_are_compared_not_searched.py`. This one
cannot be static: a loop over a collection that is EMPTY AT RUNTIME looks
identical to one that iterates. `_gating_variables()` returning `{}` is the
shape, and no parser sees it.

So this reads a coverage data file: a `For` or comprehension whose HEADER line
executed while NOT ONE line of its body did is a loop that never iterated, and
every assertion inside it is decoration. Four such gates have been found in
this repo, each one by accident during a review of something else.

WHY THE BODY SUBTREE, NOT `body[0].lineno`. A multi-line `if` inside a loop
body puts its condition on the first body line, and that line is covered
whenever the loop runs at all -- including zero times, if the condition line is
shared with the header on a one-liner. Keying on the first statement produced
this detector's only two false positives during tan-cli#1145's audit. The union
of every line in the body subtree has no such hole.

THE RECURSIVE HAZARD, which is the reason for `--check`'s floor. A meta-gate
for this defect class can BECOME the thing it detects: if the scan silently
stops matching -- a coverage file that measured the wrong source, an AST shape
that moved -- it reports zero candidates and goes green. `audit_narrow_except_contracts.py`
shipped 869 lines of AST logic with no test coverage of its own and had to be
retrofitted (PR #1138). This one ships with fabricated-input negative controls
and a candidate floor from the start, in `tests/scripts/test_audit_vacuous_loops.py`.

Usage:

    coverage run --source=tests/gates -m pytest tests/gates -q
    python scripts/audit_vacuous_loops.py --check

`--check` compares against the declared allow-list and exits 1 on anything new.
Without it, the script reports what it finds and exits 0 -- the human-driven
audit mode `audit_narrow_except_contracts.py` established.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import sys

REPO_PYTHON = pathlib.Path(__file__).resolve().parent.parent
GATES_DIR = REPO_PYTHON / "tests" / "gates"

#: Loops that measure nothing TODAY and are kept deliberately. One row per
#: site, each carrying the reason, in the shape this repo already uses for
#: `_SEEDED_CONTRACTS` and `test_no_new_hardware_facts.py`'s `ALLOWED`. A row
#: here is a REVIEWED DECISION, not an exemption: the point of the gate is that
#: a new empty loop must be argued for in writing rather than discovered by
#: accident two releases later.
#:
#: DELIBERATELY EMPTY as this ships, and `--check` is therefore NOT wired into
#: CI yet. A first run against real coverage data found TEN candidates
#: (`1151 passed, 113 skipped`, 63 measured files, 2026-09-04), so an armed
#: `--check` would red on all ten today. Populating this table is the
#: judgement, not the bookkeeping: coverage cannot distinguish "empty because
#: the gate PASSES" -- `for match in pattern.finditer(text)` finding no
#: offenders -- from "empty because the gate MEASURES NOTHING", and both look
#: identical here (header covered, body not). Only a human separates them, one
#: site at a time, and a row whose reason was guessed is what
#: `test_hand_port_tan_side.py` calls "an exemption wearing a comment".
#:
#: So this ships in REPORT mode, the same shape
#: `scripts/audit_narrow_except_contracts.py` has used since it was written:
#: committed, human-driven, no CI leg. tan-cli#1145 stays open for the
#: classification pass and the parallel `vacuity` job that follows it.
DECLARED_EMPTY: dict[str, str] = {}


def _body_lines(node: ast.AST) -> set[int]:
    """Every line number in a node's BODY subtree.

    Comprehensions have no `body` attribute -- their "body" is the element
    expression plus the condition of each generator, which is exactly what
    fails to run when the source iterable is empty.
    """
    if isinstance(node, (ast.For, ast.AsyncFor)):
        parts: list[ast.AST] = [*node.body, *node.orelse]
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        parts = [node.elt, *(cond for gen in node.generators for cond in gen.ifs)]
    elif isinstance(node, ast.DictComp):
        parts = [node.key, node.value, *(c for gen in node.generators for c in gen.ifs)]
    else:
        return set()
    lines: set[int] = set()
    for part in parts:
        for sub in ast.walk(part):
            line = getattr(sub, "lineno", None)
            if line is not None:
                lines.add(line)
            end = getattr(sub, "end_lineno", None)
            if end is not None:
                lines.update(range(line or end, end + 1))
    return lines


def _header_line(node: ast.AST) -> int:
    return node.lineno


def find_vacuous(
    source: str, covered: set[int], *, label: str
) -> list[tuple[str, int, str]]:
    """`(label, line, kind)` for every loop whose header ran and body did not."""
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(ast.parse(source)):
        kind = {
            ast.For: "for",
            ast.AsyncFor: "async-for",
            ast.ListComp: "listcomp",
            ast.SetComp: "setcomp",
            ast.DictComp: "dictcomp",
            ast.GeneratorExp: "genexp",
        }.get(type(node))
        if kind is None:
            continue
        header = _header_line(node)
        body = _body_lines(node) - {header}
        if not body:
            # UNDECIDABLE at line granularity, and skipped rather than
            # guessed. A one-line comprehension puts its `elt` and its
            # generator conditions on the SAME line as the header, so
            # "iterated" and "did not iterate" produce identical coverage.
            # Reporting it would be a false positive on every such
            # comprehension in the tree; not reporting it is a real blind
            # spot, and it is named here rather than left for someone to
            # rediscover. Multi-line comprehensions ARE decidable and are
            # checked normally.
            continue
        if header in covered and not (body & covered):
            out.append((label, header, kind))
    return out


def find_never_executed_asserts(
    source: str, covered: set[int], *, label: str
) -> list[tuple[str, int]]:
    """The same pass over `Assert` nodes, free. An assertion whose own line
    never executed asserts nothing; tan-cli#1145 measured 37 of 1188, 33 of
    them inside `ALP_SDK_ROOT`-gated skips, which is why this is REPORTED and
    not gated."""
    return [
        (label, node.lineno)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assert) and node.lineno not in covered
    ]


def _covered_lines(coverage_file: pathlib.Path) -> dict[str, set[int]]:
    import coverage  # imported here so `--help` works without it installed

    data = coverage.CoverageData(basename=str(coverage_file))
    data.read()
    return {
        pathlib.Path(measured).name: set(data.lines(measured) or ())
        for measured in data.measured_files()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coverage-file", default=".coverage", type=pathlib.Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 on any loop not in DECLARED_EMPTY (CI mode)",
    )
    args = parser.parse_args(argv)

    if not args.coverage_file.exists():
        print(
            f"no coverage data at {args.coverage_file} -- run\n"
            f"  coverage run --source=tests/gates -m pytest tests/gates -q\n"
            f"first. Refusing to report zero findings from an absent "
            f"measurement, which is how this class of gate goes quiet.",
            file=sys.stderr,
        )
        return 2

    per_file = _covered_lines(args.coverage_file)
    vacuous: list[tuple[str, int, str]] = []
    dead_asserts: list[tuple[str, int]] = []
    for path in sorted(GATES_DIR.glob("test_*.py")):
        covered = per_file.get(path.name, set())
        if not covered:
            continue
        source = path.read_text(encoding="utf-8")
        vacuous.extend(find_vacuous(source, covered, label=path.name))
        dead_asserts.extend(find_never_executed_asserts(source, covered, label=path.name))

    for label, line, kind in vacuous:
        key = f"{label}:{line}"
        note = DECLARED_EMPTY.get(key)
        mark = "declared" if note else "NEW"
        print(f"{mark:8s} {key:64s} {kind}" + (f"  -- {note}" if note else ""))
    print(f"\n{len(vacuous)} never-iterating loop(s), "
          f"{len(dead_asserts)} never-executed assertion(s), "
          f"across {len(per_file)} measured file(s)")

    if not args.check:
        return 0
    undeclared = [f"{label}:{line}" for label, line, _ in vacuous
                  if f"{label}:{line}" not in DECLARED_EMPTY]
    if undeclared:
        print(
            "\nThese loops never iterate, so every assertion inside them is "
            "decoration. Either make the collection non-empty, or add a row to "
            "DECLARED_EMPTY with the reason it is empty on purpose:\n  "
            + "\n  ".join(undeclared),
            file=sys.stderr,
        )
        return 1
    stale = sorted(set(DECLARED_EMPTY) - {f"{l}:{n}" for l, n, _ in vacuous})
    if stale:
        print(
            "\nDECLARED_EMPTY rows that no longer describe an empty loop -- the "
            "loop now iterates, moved, or was deleted. Drop them, in the change "
            "that made them dead:\n  " + "\n  ".join(stale),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
