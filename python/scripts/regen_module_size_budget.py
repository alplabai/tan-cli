#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regenerate `tests/gates/module_size_budget.d/` (tan-cli#668, tan-cli#1057).

`test_module_size_budget.py`'s ratchet used to be a hand-maintained Python
dict: one entry per over-budget module, its line count, and a paragraph
explaining why. It conflicted on SEVEN separate merges in one day, because it
stored ABSOLUTE measurements of one tree and nearly every PR perturbed at
least one entry -- and both naive conflict resolutions (`--ours`, `--theirs`)
shipped a red gate, because neither side's numbers describe the MERGED tree.

This script is the fix: the numbers are no longer typed by hand, they are
MEASURED, by this script, from the tree it is run against. A merge conflict on
a record file is resolved by throwing either side away (the content does not
matter -- see below) and running this script again against the already-merged
tree; the numbers it writes are correct by construction because they come from
`ast`-walking the real merged source, not from reconciling two committed
opinions about a tree that no longer exists.

Growth is still gated, on purpose (tan-cli#668's hard constraint: a padded or
guessed value must not pass silently, and this ratchet has caught real growth
before). A plain `regen` with no flag NEVER raises a ceiling -- it only ever
lowers one (a module shrank, or dropped below the cap entirely) or leaves the
records untouched. Raising one requires an explicit flag, and the two flags
are deliberately different so the reason a number moved stays visible in the
`git log` and, as of tan-cli#907, in MODULE_SIZE_BUDGET_LOG.d/ (one file per
entry -- see that directory's README.md):

    --reason "text"     a DELIBERATE budget raise -- some module or function
                         genuinely grew and that growth is not being split out
                         this time. Writes a new, dated, reasoned entry file
                         under MODULE_SIZE_BUDGET_LOG.d/ (tan-cli#907; see
                         that directory's README.md).

    --merge-resync       re-measuring a tree that is the union of two already-
                         reviewed branches, each of which already justified
                         its own growth (with its own --reason) before it
                         merged. No new judgement call is being made here, so
                         the ledger entry it writes carries no reason of its
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

## Where the records live, and what is DERIVED (tan-cli#1057)

Through 2026-08-31 all of this lived in one file,
`tests/gates/module_size_budget.generated.json`. Every branch that changed any
tracked module rewrote it, so it conflicted on most long-lived branches -- the
friction half of tan-cli#907, deliberately deferred there and closed here.

It is now one record file per measured module under
`tests/gates/module_size_budget.d/`, named after the module itself
(`module_size_budget.d/tan/commands/build_cmd.py.json`) -- the same structural
fix tan-cli#907 applied to the ledger, for the same reason: two branches that
touch different modules are never writing the same path, so there is nothing
to conflict over, with no merge driver needed local or GitHub-side.

The issue proposed splitting per TOP-LEVEL PACKAGE and asked for a larger
sample before committing to a schema change. Measured over 93 value-changing
commits to the old single file (4278 commit pairs):

    single file (today)                  4278/4278  = 100.0%
    per-package (the issue's proposal)   2621/4278  =  61.3%
    per-module, scalars still stored      958/4278  =  22.4%
    per-module + derived scalars          553/4278  =  12.9%

Per-package loses most of its value to one bucket: 69% of those commits touch
`tan/commands/` and 58% touch more than one package at all. And 34% of them
moved a WHOLE-TREE scalar -- which the issue's four-PR sample happened to
find none of -- so leaving the two scalars stored anywhere shared costs 22.4%
instead of 12.9%.

So they are not stored. `function_count_budget` was always exactly a SUM over
modules and `function_worst_budget` exactly a MAX over modules (see
`measure_current` in `tests/gates/_module_size_budget_core.py`: `len(found)`
and `max(span for span, _ in found)` over a list accumulated per module), so
each record carries its own module's `long_functions` -- since tan-cli#1173
the actual sorted `[span, name]` list, not a count with a sibling
`worst_function` max (see that section below) -- and `MeasuredState` exposes
the two whole-tree numbers as computed properties. The ratchet still compares
whole-tree totals and still means exactly what it meant; there is simply no
longer a stored number two branches can both write.

The residual 12.9% is real and is not claimed away: two branches that both
change the SAME module still write the same record file and still conflict.
Nothing short of not committing the measurement at all removes that, and the
resolution is the cheap one it has always been -- delete either side, re-run
this script.

`observed_tests` (tan-cli#817) got the same treatment, for the same measured
reason: those entries collide by the same mechanism and are inside the sample
above. They stay a RECORD, not a budget -- refreshed by every plain run,
never requiring `--reason`, never writing a ledger line. The distinction used
to be positional (a separate section of one file); it is now explicit and
machine-checked, `"kind": "observed"` on every such record, rejected by
`_load_records` if it disagrees with the tree the record's path sits in.

Two consequences of the observed side being refreshed UNCONDITIONALLY, on
every write path, worth knowing before you run this (review of #875):

* A contributor raising a `tan/**` ceiling with `--reason` also commits
  whatever `tests/**` drift has accumulated on `dev` since the last refresh.
  As of tan-cli#1057 that lands in the drifted files' OWN records rather than
  in the same file as the ceiling, so it no longer widens the raise's
  conflict surface -- but it is still committed in the same PR.
* `--check` and the pytest gate do not agree on what "stale" means, on
  purpose. `--check` compares EXACTLY and reds on a one-line drift;
  `tests/gates/test_module_size_budget.py` tolerates `max(200, 10%)` of a
  recorded `tests/**` count so an ordinary PR is not taxed, and only demands
  agreement on WHICH files are over the cap. A tree can therefore satisfy
  that tolerance and still be `--check`-stale -- do not read a green `pytest`
  as a green `--check`. (The gated `tan/**` side is tighter: its function
  facts are compared exactly by the gate too, because `--check` is a required
  CI step and would demand the same regen anyway -- better to fail at the
  local bar.)

As of tan-cli#907, `--check` is not merely a local convenience: it runs as
its own early step in both `.github/workflows/ci.yml`'s `python` job and
`.github/workflows/parity.yml`'s `seam1-plan-shape` job (the only two CI legs
that run `tests/gates` at all). That closed a real gap, not a hypothetical
one -- the old single JSON file was deliberately NOT `merge=union`'d, so a
real `git merge` on it either conflicted visibly or -- measured directly --
stitched two disjoint edits into one syntactically valid, semantically STALE
JSON object with no conflict marker at all. The split narrows that shape (two
branches editing different modules now edit different files) but does not
retire the step: the same silent-staleness is still reachable whenever a
merge brings in a tree change without the matching record, which is every
merge where one side edited a module the other side's records describe.

## Growth is judged per FUNCTION, not per whole-tree scalar (tan-cli#1173)

The paragraph above ("they still mean exactly what they meant") stopped being
true the moment the whole-tree scalars were the only thing `--reason` was
judged against: a module could have one function cross `FUNCTION_CAP` while a
different function in the SAME module dropped below it, and the sum and the
max would both read unchanged, so this script would see no growth at all. PR
#1170 is the real instance, not a hypothetical one: `_sdk_credential` grew
`50 -> 63 -> 69` while `_data` fell `51 -> 47` in the same diff, and
`bootstrap_cmd.py.json`'s `long_functions` read `19` before and after.

`ModuleFunctions.entries` (`_module_size_budget_core.py`) now stores the
actual sorted `(span, name)` list per module instead of a count and a max, and
`_function_deltas` below compares it per `module:name` -- a function newly
over the cap, or an already-over one growing further, is growth in its own
right and forces `--reason` even when nothing else in the tree moved. A
function dropping below the cap is reported as SHRUNK, the same as a module
shrinking: it never forces a flag, but (unlike a module-line shrink) it IS
included in the `--reason`/`--merge-resync` ledger entry, because the shape
this issue closes is exactly a shrink sitting right next to the growth that
forced the entry to exist -- leaving it out of the entry would still tell only
half of what moved.
"""
from __future__ import annotations

import argparse
import datetime
import secrets
import sys
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "gates"))
import _module_size_budget_core as core  # noqa: E402


def _deltas(
    old: dict[str, int], new: dict[str, int], still_exists: set[str]
) -> tuple[list[str], list[str]]:
    """(grown, shrunk-or-removed) description lines, module ceilings only.

    `still_exists` (every key `measure_current`/`measure_observed_tests`
    would produce a KEY for, over the cap or not) is what tells "shrank
    under the cap" apart from "the module is gone" for the `after is None`
    case -- a module deleted outright, renamed, or moved is not IN the cap
    any more either, but "dropped (now under the cap)" claims it is, which
    is not true of a module that no longer exists at all (the same false
    claim tan-cli#1173's review found on the function side; one wording
    fixes both, see `_function_deltas` below)."""
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
            if name in still_exists:
                shrunk.append(f"{name}: {before} -> dropped (now under the cap)")
            else:
                shrunk.append(
                    f"{name}: {before} -> gone (renamed, moved, or deleted -- not a shrink)"
                )
        elif after > before:
            grown.append(f"{name}: {before} -> {after}")
        else:
            shrunk.append(f"{name}: {before} -> {after}")
    return grown, shrunk


def _function_deltas(
    old: dict[str, core.ModuleFunctions],
    new: dict[str, core.ModuleFunctions],
    new_names: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    """(grown, shrunk) description lines for INDIVIDUAL functions crossing
    `FUNCTION_CAP` (tan-cli#1173) -- judged per `module:name`, never per
    module and never on the two whole-tree derived scalars alone.

    Before this, growth was judged only on `function_count`/`function_worst`
    -- a sum and a max over the tree -- so one function crossing the cap
    while a different one in the SAME module dropped below it left both
    numbers unchanged and needed no `--reason`. That is not a hypothetical:
    PR #1170 grew `_sdk_credential` `50 -> 63 -> 69` while `_data` fell
    `51 -> 47` in the same diff, and `bootstrap_cmd.py.json`'s
    `long_functions` read `19` before and after. Comparing the actual
    per-function list closes that -- a function newly over the cap, or an
    already-over one growing further, is its own growth event regardless of
    what any other function in the tree did.

    A function that drops below the cap (or shrinks while staying over it)
    is reported as SHRUNK, not folded into `grown` -- deliberately, and for
    the same reason a module shrinking never needs `--reason` (see `_deltas`
    above): getting smaller is never the thing this ratchet exists to catch.
    `main` still surfaces these lines in a `--reason`/`--merge-resync`
    ledger entry (unlike a module-line shrink) so the entry names the
    function that dropped, not only the one that grew -- otherwise an
    offsetting pair's ledger line would tell only half of what moved.

    A dropped entry is only reported as a SHRINK when `name` still names a
    function somewhere in that module per `new_names` (`core.
    all_function_names_by_module()`) -- i.e. it is still there, just under
    `FUNCTION_CAP` now. If it is not, the function did not shrink: it was
    renamed, moved to a different module, or deleted, and saying "dropped
    (now under the cap)" would put an untrue statement about a cap crossing
    into the append-only ledger. Nothing here can say WHERE a renamed or
    moved function went -- that identity is not tracked -- only that this
    name, in this module, stopped meaning anything.

    A name that goes away and a DIFFERENT name that appears, in the SAME
    module, at the exact same span, is a pure RENAME -- nothing about the
    ceiling moved (same module, same size), so it must not force `--reason`
    on its own: a review of tan-cli#1173 measured `f` -> `g` (both 63 lines,
    same module) producing `grown == ["mod:g: new entry at 63"]`, refusing
    the regen for what is not a ceiling raise at all. Those are paired off
    per module, by span, before either bucket is built -- a genuine new
    over-cap function still counts as growth (a DIFFERENT span, or no
    same-span disappearance in that module to pair it with -- e.g. the move
    to a different module `test_a_whole_tree_neutral_function_move_now_needs_a_reason`
    covers, which this pairing deliberately leaves alone: a different module
    now holds an over-cap function it did not before, which is exactly the
    growth tan-cli#1173 exists to catch)."""
    grown: list[str] = []
    shrunk: list[str] = []
    empty = core.ModuleFunctions(entries=())
    for module in sorted(set(old) | set(new)):
        old_by_name = core.spans_by_name(old.get(module, empty))
        new_by_name = core.spans_by_name(new.get(module, empty))
        still_named = new_names.get(module, set())
        module_grown: list[str] = []
        module_shrunk: list[str] = []
        appeared: dict[int, list[str]] = {}  # span -> names newly over the cap
        vanished: dict[int, list[str]] = {}  # span -> names gone (not still-named)
        for name in sorted(set(old_by_name) | set(new_by_name)):
            before_spans = old_by_name.get(name, [])
            after_spans = new_by_name.get(name, [])
            label = f"{module}:{name}"
            for before, after in zip_longest(before_spans, after_spans):
                if before == after:
                    continue
                if before is None:
                    appeared.setdefault(after, []).append(name)
                elif after is None:
                    if name in still_named:
                        module_shrunk.append(f"{label}: {before} -> dropped (now under the cap)")
                    else:
                        vanished.setdefault(before, []).append(name)
                elif after > before:
                    module_grown.append(f"{label}: {before} -> {after}")
                else:
                    module_shrunk.append(f"{label}: {before} -> {after}")

        # Pair same-span appear/vanish within this module as renames: neither
        # side is reported (see the docstring paragraph above).
        for span, gone_names in vanished.items():
            new_at_span = appeared.pop(span, [])
            paired = min(len(gone_names), len(new_at_span))
            for name in gone_names[paired:]:
                module_shrunk.append(
                    f"{module}:{name}: {span} -> gone (renamed, moved, or "
                    "deleted -- not a shrink)"
                )
            for name in new_at_span[paired:]:
                module_grown.append(f"{module}:{name}: new entry at {span}")
        for span, names in appeared.items():
            for name in names:
                module_grown.append(f"{module}:{name}: new entry at {span}")

        grown.extend(sorted(module_grown))
        shrunk.extend(sorted(module_shrunk))
    return grown, shrunk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reason", metavar="TEXT", help="justify a deliberate budget raise")
    mode.add_argument(
        "--merge-resync",
        action="store_true",
        help="re-measure after resolving a conflict on a record file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed records do not match a fresh measurement; write nothing",
    )
    args = parser.parse_args(argv)

    current = core.measure_current()
    observed = core.measure_observed_tests()
    try:
        committed = core.load_generated()
        committed_observed = core.load_observed_tests()
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    current_modules = {core.rel(path) for path in core.modules()}
    module_grown, module_shrunk = _deltas(committed.modules, current.modules, current_modules)
    # Judged per FUNCTION, not on the derived whole-tree numbers (tan-cli#1173)
    # -- see `_function_deltas`'s own docstring for why the derived pair alone
    # let an offsetting pair of per-function moves hide a cap crossing.
    current_names = core.all_function_names_by_module()
    function_grown, function_shrunk = _function_deltas(
        committed.functions, current.functions, current_names
    )
    grown = module_grown + function_grown
    shrunk = module_shrunk + function_shrunk

    # tan-cli#817: the observed `tests/**` deltas are computed and REPORTED,
    # and deliberately kept out of `grown`/`shrunk` above. Those two lists are
    # what the `--reason` refusal below reads, so folding the observed side
    # into them would silently convert this record into the ratchet the scope
    # decision rejected -- see `TEST_ROOT` in _module_size_budget_core.py.
    current_test_tree = {core.rel(path) for path in core.test_tree_modules()}
    observed_moved, observed_settled = _deltas(committed_observed, observed, current_test_tree)

    stale_caps = not core.CAPS_PATH.exists() or core.CAPS_PATH.read_text(
        encoding="utf-8"
    ) != core.dump_caps()

    if not (grown or shrunk or observed_moved or observed_settled or stale_caps):
        print("module_size_budget.d/ already matches the measured tree.")
        return 0

    if args.check:
        print("module_size_budget.d/ is stale:")
        for line in grown + shrunk:
            print(f"  {line}")
        for line in observed_moved + observed_settled:
            print(f"  observed: {line}")
        if stale_caps:
            print("  _caps.json does not match MODULE_CAP/FUNCTION_CAP")
        print("Run `python scripts/regen_module_size_budget.py` to refresh it.")
        return 1

    if grown and not (args.reason or args.merge_resync):
        print("Refusing to raise a ceiling without --reason or --merge-resync:", file=sys.stderr)
        for line in grown:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nA real growth needs `--reason \"...\"` (logged as a new file under\n"
            "MODULE_SIZE_BUDGET_LOG.d/).\n"
            "Re-deriving after a merge conflict needs `--merge-resync` (the growth was\n"
            "already reasoned on the branches this merge combined).",
            file=sys.stderr,
        )
        return 1

    core.write_records(current, observed)
    print(f"wrote {core.RECORD_DIR}")
    for line in shrunk:
        print(f"  shrunk: {line}")
    for line in grown:
        print(f"  grown:  {line}")
    for line in observed_settled + observed_moved:
        print(f"  observed (tests/, not gated): {line}")

    if grown and args.reason:
        _append_log(args.reason, grown, function_shrunk)
    elif grown and args.merge_resync:
        _append_log(
            "merge-resync (growth already reasoned on the merged branches)",
            grown,
            function_shrunk,
        )

    return 0


def _append_log(reason: str, grown: list[str], function_shrunk: list[str]) -> None:
    """tan-cli#907: writes one NEW file per entry under `core.LOG_DIR`,
    never an append to `core.LOG_PATH` (frozen -- see this script's module
    docstring). The filename carries the date for a human scanning the
    directory chronologically, plus a random 8-hex-char token so two
    concurrent branches -- or two runs the same day -- can never be made to
    pick the same path; a sequential counter could not promise that, since
    two branches unaware of each other could both compute the same next
    number. `"x"` (exclusive create) mode both proves that and refuses to
    silently clobber an existing file on the astronomically unlikely token
    collision -- it raises `FileExistsError`, which is retried with a fresh
    token rather than swallowed.

    `function_shrunk` (tan-cli#1173) is logged alongside `grown`, not
    dropped: for the exact shape this issue reports -- one function crosses
    `FUNCTION_CAP` while a different one in the SAME module drops below it --
    the growth line alone names only half of what moved. A module-line
    shrink is still never logged here (unchanged from before #1173); only the
    per-function half gets this treatment, because it is the half that can
    sit right next to the growth line that forced this entry to exist."""
    date = datetime.date.today().isoformat()
    entry = f"- {date} -- {reason}\n"
    for line in grown:
        entry += f"    - {line}\n"
    for line in function_shrunk:
        entry += f"    - shrunk: {line}\n"
    core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        path = core.LOG_DIR / f"{date}-{secrets.token_hex(4)}.md"
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(entry)
        except FileExistsError:
            continue
        break
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
