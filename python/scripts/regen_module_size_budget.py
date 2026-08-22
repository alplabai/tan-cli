#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regenerate `tests/gates/module_size_budget.generated.json` (tan-cli#668).

`test_module_size_budget.py`'s ratchet used to be a hand-maintained Python
dict: one entry per over-budget module, its line count, and a paragraph
explaining why. It conflicted on SEVEN separate merges in one day, because it
stored ABSOLUTE measurements of one tree and nearly every PR perturbed at
least one entry -- and both naive conflict resolutions (`--ours`, `--theirs`)
shipped a red gate, because neither side's numbers describe the MERGED tree.

This script is the fix: the numbers are no longer typed by hand, they are
MEASURED, by this script, from the tree it is run against. A merge conflict on
the generated file is resolved by throwing either side away (the content does
not matter -- see below) and running this script again against the already-
merged tree; the numbers it writes are correct by construction because they
come from `ast`-walking the real merged source, not from reconciling two
committed opinions about a tree that no longer exists.

Growth is still gated, on purpose (tan-cli#668's hard constraint: a padded or
guessed value must not pass silently, and this ratchet has caught real growth
before). A plain `regen` with no flag NEVER raises a ceiling -- it only ever
lowers one (a module shrank, or dropped below the cap entirely) or leaves the
file untouched. Raising one requires an explicit flag, and the two flags are
deliberately different so the reason a number moved stays visible in the
`git log` of this file and of MODULE_SIZE_BUDGET_LOG.md:

    --reason "text"     a DELIBERATE budget raise -- some module or function
                         genuinely grew and that growth is not being split out
                         this time. Appends a dated, reasoned line to
                         MODULE_SIZE_BUDGET_LOG.md.

    --merge-resync       re-measuring a tree that is the union of two already-
                         reviewed branches, each of which already justified
                         its own growth (with its own --reason) before it
                         merged. No new judgement call is being made here, so
                         the ledger line it writes carries no reason of its
                         own -- it records the re-measurement and points at
                         the two commits this merge combined, which is where
                         the reasons already are. (This used to say "no new
                         ledger entry is written". The code has always written
                         one; the sentence was wrong, not the behaviour.)

Usage:

    python scripts/regen_module_size_budget.py                 # shrink-only
    python scripts/regen_module_size_budget.py --reason "..."   # a real raise
    python scripts/regen_module_size_budget.py --merge-resync   # after a
                                                                  conflict
    python scripts/regen_module_size_budget.py --check          # exit 1 if
                                                                  stale, write
                                                                  nothing

The file also carries an `observed_tests` section (tan-cli#817): line counts
for `python/tests/**` over the cap. Those are a RECORD, not a budget. They are
refreshed by every plain run above, they never require `--reason`, and they
never write a ledger line -- growing a test file is not a ceiling raise. See
`TEST_ROOT` in `tests/gates/_module_size_budget_core.py` for the scope
decision and the measurements behind it.

Two consequences of that section being refreshed UNCONDITIONALLY, on every
write path, worth knowing before you run this (review of #875):

* A contributor raising a `tan/**` ceiling with `--reason` also commits
  whatever `tests/**` drift has accumulated on `dev` since the last refresh,
  in the same file. That is live, not hypothetical -- and it is the same
  absolute-measurement conflict surface tan-cli#668 created this file to
  remove. `--merge-resync` resolves it for the observed section exactly as it
  does for the budgeted one: re-measure the merged tree and record it, no new
  judgement call.
* `--check` and the pytest gate do not agree on what "stale" means, on
  purpose. `--check` compares EXACTLY and reds on a one-line drift;
  `tests/gates/test_module_size_budget.py` tolerates `max(200, 10%)` of the
  recorded count so an ordinary PR is not taxed, and only demands agreement
  on WHICH files are over the cap. A tree can therefore satisfy the gate and
  still be `--check`-stale. Only the gate runs in CI -- this script is in no
  workflow -- so that asymmetry costs nothing today, but do not read a green
  `pytest` as a green `--check`.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "gates"))
import _module_size_budget_core as core  # noqa: E402


def _deltas(old: dict[str, int], new: dict[str, int]) -> tuple[list[str], list[str]]:
    """(grown, shrunk-or-removed) description lines, module map only."""
    grown: list[str] = []
    shrunk: list[str] = []
    for name in sorted(set(old) | set(new)):
        before = old.get(name)
        after = new.get(name)
        if before == after:
            continue
        if before is None:
            grown.append(f"{name}: new entry at {after}")
        elif after is None:
            shrunk.append(f"{name}: {before} -> dropped (now under the cap)")
        elif after > before:
            grown.append(f"{name}: {before} -> {after}")
        else:
            shrunk.append(f"{name}: {before} -> {after}")
    return grown, shrunk


def _scalar_delta(name: str, before: int, after: int) -> tuple[list[str], list[str]]:
    if after > before:
        return [f"{name}: {before} -> {after}"], []
    if after < before:
        return [], [f"{name}: {before} -> {after}"]
    return [], []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reason", metavar="TEXT", help="justify a deliberate budget raise")
    mode.add_argument(
        "--merge-resync",
        action="store_true",
        help="re-measure after resolving a conflict on the generated file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed file does not match a fresh measurement; write nothing",
    )
    args = parser.parse_args(argv)

    current = core.measure_current()
    observed = core.measure_observed_tests()
    if core.GENERATED_PATH.exists():
        try:
            committed = core.load_generated()
            committed_observed = core.load_observed_tests()
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
    else:
        committed = core.MeasuredState(modules={}, function_count=0, function_worst=0)
        committed_observed = {}

    module_grown, module_shrunk = _deltas(committed.modules, current.modules)
    count_grown, count_shrunk = _scalar_delta(
        "function_count_budget", committed.function_count, current.function_count
    )
    worst_grown, worst_shrunk = _scalar_delta(
        "function_worst_budget", committed.function_worst, current.function_worst
    )
    grown = module_grown + count_grown + worst_grown
    shrunk = module_shrunk + count_shrunk + worst_shrunk

    # tan-cli#817: the observed `tests/**` deltas are computed and REPORTED,
    # and deliberately kept out of `grown`/`shrunk` above. Those two lists are
    # what the `--reason` refusal below reads, so folding the observed side
    # into them would silently convert this record into the ratchet the scope
    # decision rejected -- see `TEST_ROOT` in _module_size_budget_core.py.
    observed_moved, observed_settled = _deltas(committed_observed, observed)

    if not grown and not shrunk and not observed_moved and not observed_settled:
        print("module_size_budget.generated.json already matches the measured tree.")
        return 0

    if args.check:
        print("module_size_budget.generated.json is stale:")
        for line in grown + shrunk:
            print(f"  {line}")
        for line in observed_moved + observed_settled:
            print(f"  observed: {line}")
        print("Run `python scripts/regen_module_size_budget.py` to refresh it.")
        return 1

    if grown and not (args.reason or args.merge_resync):
        print("Refusing to raise a ceiling without --reason or --merge-resync:", file=sys.stderr)
        for line in grown:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nA real growth needs `--reason \"...\"` (logged to MODULE_SIZE_BUDGET_LOG.md).\n"
            "Re-deriving after a merge conflict needs `--merge-resync` (the growth was\n"
            "already reasoned on the branches this merge combined).",
            file=sys.stderr,
        )
        return 1

    core.GENERATED_PATH.write_text(core.dump_generated(current, observed), encoding="utf-8")
    print(f"wrote {core.GENERATED_PATH}")
    for line in shrunk:
        print(f"  shrunk: {line}")
    for line in grown:
        print(f"  grown:  {line}")
    for line in observed_settled + observed_moved:
        print(f"  observed (tests/, not gated): {line}")

    if grown and args.reason:
        _append_log(args.reason, grown)
    elif grown and args.merge_resync:
        _append_log("merge-resync (growth already reasoned on the merged branches)", grown)

    return 0


def _append_log(reason: str, grown: list[str]) -> None:
    date = datetime.date.today().isoformat()
    entry = f"- {date} -- {reason}\n"
    for line in grown:
        entry += f"    - {line}\n"
    with core.LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"appended to {core.LOG_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())
