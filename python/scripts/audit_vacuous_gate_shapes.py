#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run `tests/gates` under `coverage` and report the gates that cannot fail
(tan-cli#1145).

Two things this reports, both needing execution data that no static walk can
supply:

  1. **A `for` loop that never iterated.** Its header line ran, no line of its
     body ever did, across the whole directory. Every assertion inside it is
     unreachable. Reviewed instances are declared in
     [`ALLOWED_EMPTY_LOOPS`] with a reason each; anything else exits 1.
  2. **A gate FILE that executed no `assert` at all.** The coarse
     skip-guard form, and deliberately no finer -- "this file asserted
     nothing today" is decidable, "this file's assertions are load-bearing"
     is not. Declared exemptions live in [`ALLOWED_ZERO_ASSERT_FILES`].

The cheap, static half -- the tautology walk, the swallowed-assert walk, and
every allow-list staleness check -- is NOT here. It needs no coverage run, so
it lives in `tests/gates/test_vacuous_gate_shapes.py` and runs in every leg
that runs the suite, including the required `seam1-plan-shape` one. This
script is only the part that costs a second execution of the directory.

tan-cli#1145's OTHER gate -- an `if:` key must be compared with `==`, never
searched with `in` -- ships in PR #1168 and is not here either.

## The recursive hazard, and what is done about it

A meta-gate for this defect class can BECOME the thing it detects: if the scan
silently stops matching, it finds no candidates and reports clean. Two
defences, and neither is optional:

  * **Floors.** `MIN_FOR_SITES`, `MIN_ASSERT_SITES` and
    `MAX_UNMEASURED_GATE_FILES` in `_vacuous_gate_shapes_core.py` refuse a
    collapse in the CANDIDATE population -- a zero that came from finding
    nothing to look at, rather than from finding nothing wrong. The third is
    derived against the number of gate modules on disk rather than typed as an
    absolute, because a file missing from the coverage mapping is skipped in
    silence by both checks below.
  * **Fabricated-input negative controls**, in
    `tests/gates/test_vacuous_gate_shapes.py`. They drive `never_iterating`
    and the `if:`-key walk with synthesised input that MUST be flagged, so a
    neutered detector reds in the suite rather than going quietly green here.
    `scripts/audit_narrow_except_contracts.py` shipped 290 lines of `ast`
    logic with no test coverage at all; this is that lesson applied.

## Direction, on purpose

Only ONE direction is enforced against the coverage run: an undeclared
never-iterating loop reds. An allow-list row whose loop DID iterate in this
run does NOT red -- because whether a given loop iterates depends on the
configuration (`ALP_SDK_ROOT` bound unskips tests, and more execution can
only make the never-iterated set smaller), and a gate that flips on the leg
it runs in is worse than no gate. Row staleness is checked STRUCTURALLY
instead, in the pytest file: every row must still name a real `for` header at
that exact `(file, source)`, so a deleted or reworded loop reds in review.

That same monotonicity is why this script is wired into ONE leg for reasons of
COST alone. A bound `ALP_SDK_ROOT` cannot make it report a loop that iterates;
it can only make it report fewer. Running it in `seam1-plan-shape` too would
be correct and would cost a second ~35s execution of the directory on a
required context, which is the only argument against it.

Usage:

    python scripts/audit_vacuous_gate_shapes.py              # run + report
    python scripts/audit_vacuous_gate_shapes.py --report     # never exit 1
    python scripts/audit_vacuous_gate_shapes.py \
        --coverage-data PATH                                 # reuse a run
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1]
GATES = PYTHON_ROOT / "tests" / "gates"

sys.path.insert(0, str(GATES))
import _vacuous_gate_shapes_core as core  # noqa: E402


def gate_modules() -> list[Path]:
    """Every `.py` under `tests/gates/`, in a stable order."""
    return sorted(GATES.rglob("*.py"))


def run_coverage(data_file: Path) -> tuple[int, str]:
    """Execute `tests/gates` under `coverage`, writing `data_file`."""
    cmd = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        f"--data-file={data_file}",
        "-m",
        "pytest",
        "tests/gates",
        "-q",
    ]
    proc = subprocess.run(
        cmd, cwd=PYTHON_ROOT, capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def read_coverage(data_file: Path) -> dict[str, frozenset[int]]:
    """`{path relative to python/: executed line numbers}`."""
    import coverage  # imported here so `--help` works without it installed

    data = coverage.CoverageData(basename=str(data_file))
    data.read()
    covered: dict[str, frozenset[int]] = {}
    for measured in data.measured_files():
        try:
            rel = Path(measured).relative_to(PYTHON_ROOT).as_posix()
        except ValueError:
            continue
        covered[rel] = frozenset(data.lines(measured) or ())
    return covered


def collect() -> tuple[list[core.ForSite], dict[str, frozenset[int]]]:
    """Every `for` site and every `assert` line under `tests/gates/`."""
    sites: list[core.ForSite] = []
    asserts: dict[str, frozenset[int]] = {}
    for path in gate_modules():
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        sites.extend(core.iter_for_sites(tree, rel, text))
        asserts[rel] = core.iter_assert_lines(tree)
    return sites, asserts


def check_floors(
    sites: list[core.ForSite],
    asserts: dict[str, frozenset[int]],
    covered: dict[str, frozenset[int]],
) -> list[str]:
    """The anti-collapse half: refuse a clean report built on nothing."""
    problems = []
    measured = sum(1 for rel in covered if rel.startswith("tests/gates/"))
    total_asserts = sum(len(lines) for lines in asserts.values())
    # DERIVED from the tree, not typed: the script knows exactly how many gate
    # modules exist, and both `never_iterating` and `check_zero_assert_files`
    # silently skip one that is absent from `covered`. A constant floor with
    # slack in it would let that many modules stop being measured -- every
    # loop and assertion in them exempted -- and still read green.
    measured_floor = len(gate_modules()) - core.MAX_UNMEASURED_GATE_FILES
    checks = (
        ("`for` sites walked", len(sites), core.MIN_FOR_SITES),
        ("`assert` sites walked", total_asserts, core.MIN_ASSERT_SITES),
        ("gate files measured", measured, measured_floor),
    )
    for label, found, floor in checks:
        if found < floor:
            problems.append(
                f"{label}: {found}, below the floor of {floor}. A clean report "
                "from a scan that reached almost nothing is the exact failure "
                "this gate exists to prevent -- fix the scan, or move the "
                "floor deliberately in the same change that shrank the tree."
            )
    return problems


def check_empty_loops(
    sites: list[core.ForSite], covered: dict[str, frozenset[int]]
) -> list[str]:
    """Never-iterating loops that carry no declared reason."""
    problems = []
    for site in core.never_iterating(sites, covered):
        if site.key in core.ALLOWED_EMPTY_LOOPS:
            continue
        problems.append(
            f"{site.rel}:{site.header_line}: {site.source}\n"
            "      never iterated across the whole run -- its header executed, "
            "no line of its body ever did, so every assertion inside it is "
            "unreachable. If zero iterations is CORRECT, add a row to "
            "`ALLOWED_EMPTY_LOOPS` in `tests/gates/"
            "_vacuous_gate_shapes_core.py` saying why."
        )
    return problems


def check_zero_assert_files(
    asserts: dict[str, frozenset[int]], covered: dict[str, frozenset[int]]
) -> list[str]:
    """Gate files that executed no assertion at all."""
    problems = []
    for rel, lines in sorted(asserts.items()):
        if not lines or rel in core.ALLOWED_ZERO_ASSERT_FILES:
            continue
        hit = covered.get(rel)
        if hit is None or (lines & hit):
            continue
        problems.append(
            f"{rel}: {len(lines)} `assert` statements, none of which "
            "executed. The file reports green having checked nothing. If "
            "that is correct (a skip guard this configuration cannot "
            "satisfy), add a row to `ALLOWED_ZERO_ASSERT_FILES` in "
            "`tests/gates/_vacuous_gate_shapes_core.py` with the reason."
        )
    return problems


def summarise(
    sites: list[core.ForSite],
    asserts: dict[str, frozenset[int]],
    covered: dict[str, frozenset[int]],
) -> str:
    total_asserts = sum(len(lines) for lines in asserts.values())
    never_asserts = sum(
        len(lines - covered[rel]) for rel, lines in asserts.items() if rel in covered
    )
    measured = sum(1 for rel in covered if rel.startswith("tests/gates/"))
    empty = core.never_iterating(sites, covered)
    return (
        f"measured {measured} gate modules; "
        f"{len(sites)} `for` sites, {len(empty)} of which never iterated "
        f"({len(core.ALLOWED_EMPTY_LOOPS)} declared); "
        f"{total_asserts} `assert` statements, {never_asserts} never executed"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--coverage-data",
        type=Path,
        help="reuse an existing coverage data file instead of running the suite",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the findings and exit 0 regardless (for a local look)",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="tan-vacuity-") as tmp:
        data_file = args.coverage_data
        if data_file is None:
            data_file = Path(tmp) / "coverage"
            code, output = run_coverage(data_file)
            if code != 0:
                print(output[-4000:], file=sys.stderr)
                print(
                    "\ntests/gates is RED under coverage (exit "
                    f"{code}). A vacuity measurement of a red run says "
                    "nothing -- fix the suite first.",
                    file=sys.stderr,
                )
                return 1
        covered = read_coverage(data_file)

    sites, asserts = collect()
    problems = (
        check_floors(sites, asserts, covered)
        + check_empty_loops(sites, covered)
        + check_zero_assert_files(asserts, covered)
    )
    print(summarise(sites, asserts, covered))
    if not problems:
        print("no undeclared vacuous gate shapes under tests/gates/")
        return 0
    print(f"\n{len(problems)} undeclared vacuous gate shape(s):", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 0 if args.report else 1


if __name__ == "__main__":
    raise SystemExit(main())
