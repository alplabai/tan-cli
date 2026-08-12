# SPDX-License-Identifier: Apache-2.0
"""A 3000-line module and a 679-line function must not arrive unnoticed
(tan-cli#408).

The house guideline is 800 lines per module and 50 per function. Nothing else
enforces either: there is no `[tool.ruff]`, flake8 or pylint section in
`python/pyproject.toml` and no Python lint job in `.github/workflows/`. This
is a RATCHET, not a cap: it records what is true today and fails on growth. It
deliberately does not fail every module and function that is already over --
a gate that is red on the day it lands gets disabled, and then it guards
nothing at all.

## Where the numbers live (tan-cli#668)

Until tan-cli#668, the per-module ceilings, the function-count budget and the
worst-function budget were a hand-maintained Python dict IN THIS FILE, one
entry per over-budget module with a paragraph of prose recording why it grew.
That dict conflicted on seven separate merges in a single day, because it
stored ABSOLUTE measurements of one tree and nearly every PR perturbed at
least one entry -- and both naive conflict resolutions (`--ours`, `--theirs`)
shipped a red gate, because neither side's numbers describe the tree the
merge actually produced. Only running the gate on the merged tree does.

The numbers now live in `module_size_budget.generated.json`, produced by
`scripts/regen_module_size_budget.py`, never hand-edited. A merge conflict on
that file is resolved by throwing either side away and re-running the script
against the merged tree -- it re-measures from source, so the result is
correct by construction rather than reconciled. `MODULE_SIZE_BUDGET_LOG.md`
carries the append-only, one-line-per-change record of WHY a ceiling moved,
in the same "keep both sides" shape that is already correct for
`CHANGELOG.md` -- unlike the old dict, nothing in that log encodes absolute
state, so two branches that each grow a module produce two non-conflicting
lines instead of one contested number.

## Why a pytest gate and not a ruff job

`python -- pytest across python/` is ALREADY a required context on `main` and
`dev`. A new CI job would have to be added to the required list to matter, and
adding a required context blocks every open PR until it has run on each of
them. This runs inside a gate that is already required, so it enforces on the
next PR with no protection change.

## How to change these numbers

Do not hand-edit `module_size_budget.generated.json`. Run
`python scripts/regen_module_size_budget.py` -- see its own module docstring
for the `--reason` / `--merge-resync` distinction. Lowering a ceiling never
needs either flag: a module shrinking, or dropping under the cap, is always
safe and the script applies it without asking.
"""
from __future__ import annotations

import json

import pytest

from tests.gates import _module_size_budget_core as core


def test_no_module_grows_past_its_recorded_budget():
    """The ratchet. A budgeted module may shrink freely; growing past its
    recorded size fails and must be answered by regenerating the budget file
    with a reason."""
    budget = core.load_generated()
    grew = []
    for path in core.modules():
        rel = core.rel(path)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        ceiling = budget.modules.get(rel, core.MODULE_CAP)
        if lines > ceiling:
            grew.append(f"{rel}: {lines} lines, budget {ceiling}")

    assert grew == [], (
        "these modules are over budget:\n  "
        + "\n  ".join(grew)
        + f"\n\nA module not in module_size_budget.generated.json is capped at "
        f"{core.MODULE_CAP}. Either extract from it, or run "
        "`python scripts/regen_module_size_budget.py --reason \"...\"` to raise "
        "its entry."
    )


def test_the_module_budget_has_not_gone_stale():
    """The other direction: a budget entry for a file that has SHRUNK well
    under its ceiling is a ratchet that stopped ratcheting. Lower it (rerun
    the regen script -- shrinking never needs a flag) so the next growth is
    caught at the new level rather than the old one.

    The slack allowed is deliberately generous (50 lines). This gate exists
    to catch a module doubling, not to make every ordinary edit regenerate a
    number."""
    budget = core.load_generated()
    slack = []
    for rel, ceiling in sorted(budget.modules.items()):
        path = core.PACKAGE.parent / rel
        if not path.exists():
            slack.append(f"{rel}: budgeted but no longer exists -- drop the entry")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines <= core.MODULE_CAP:
            slack.append(f"{rel}: {lines} lines, now under {core.MODULE_CAP} -- drop the entry")
        elif ceiling - lines > 50:
            slack.append(f"{rel}: {lines} lines, budget {ceiling} -- lower it")

    assert slack == [], (
        "the generated module budget no longer describes the tree (run "
        "`python scripts/regen_module_size_budget.py`):\n  " + "\n  ".join(slack)
    )


def test_no_new_long_function_and_none_of_them_grows():
    """Hundreds of functions are already over 50 lines, so enumerating them
    would be a table nobody reads. The COUNT and the WORST are ratcheted
    instead: a new long function moves the count, and an existing one growing
    moves the worst."""
    budget = core.load_generated()
    found: list[tuple[int, str]] = []
    for path in core.modules():
        try:
            tree = __import__("ast").parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:  # pragma: no cover -- a broken tree fails elsewhere first
            pytest.fail(f"{core.rel(path)} does not parse: {err}")
        found.extend((span, f"{core.rel(path)}:{name}") for span, name in core.long_functions(tree))

    worst = max(found, default=(0, "<none>"))
    assert len(found) <= budget.function_count, (
        f"{len(found)} functions are over {core.FUNCTION_CAP} lines, budget "
        f"{budget.function_count}. Extract from the one you just grew, or "
        "regenerate the budget file with a reason. Longest: "
        f"{worst[1]} at {worst[0]} lines."
    )
    assert worst[0] <= budget.function_worst, (
        f"{worst[1]} is {worst[0]} lines, past the recorded worst "
        f"({budget.function_worst}). The longest function in the package "
        "getting longer is the exact drift tan-cli#408 reports."
    )


def test_the_mirrored_planner_is_named_as_out_of_scope():
    """`tan/planner/**` is a hash-audited relocation of alp-sdk's
    `scripts/alp_orchestrate/**`. Splitting a mirror file here would make it
    diverge in SHAPE from upstream, which
    `test_planner_relocation_freshness.py` exists to prevent -- so any
    oversized module under it is upstream's to fix, and this records that
    rather than leaving the next reader to rediscover it from a failing
    hash."""
    budget = core.load_generated()
    mirrored = [rel for rel in budget.modules if rel.startswith(core.MIRRORED_PREFIX)]
    assert mirrored, "no mirrored planner module is budgeted -- has the mirror moved?"
    for rel in mirrored:
        assert (core.PACKAGE.parent / rel).exists(), f"{rel} is budgeted but missing"


def test_the_generated_budget_has_no_duplicate_module_keys():
    """A duplicate key in `modules` is invisible to every other test in this
    file, because JSON (like the Python dict literal it replaced) collapses a
    duplicate on parse -- the LAST spelling wins and the earlier one is dead
    text (tan-cli#586's class of defect, which this design does not get for
    free just by moving to JSON)."""
    try:
        core.load_generated()
    except ValueError as err:
        pytest.fail(str(err))


def test_the_generated_file_declares_the_caps_this_gate_uses():
    """`module_size_budget.generated.json` carries its own `module_cap`/
    `function_cap` fields alongside the measured data, so the file is
    self-describing without a second source. They must agree with the
    constants this gate actually enforces -- a drift here would mean the
    committed file and the running gate silently disagree about the policy,
    not just the measurements."""
    data = json.loads(core.GENERATED_PATH.read_text(encoding="utf-8"))
    assert data["module_cap"] == core.MODULE_CAP
    assert data["function_cap"] == core.FUNCTION_CAP
