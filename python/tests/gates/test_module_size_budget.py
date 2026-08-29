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

## What this gates, and what it only watches (tan-cli#817)

GATED: `python/tan/**`. OBSERVED, never gated: `python/tests/**`.

That distinction is the whole of tan-cli#817, and it is stated here because
the issue's first complaint was that it was stated nowhere -- "a reader who
finds test_module_size_budget.py reasonably concludes the repo has file-size
drift under control", while half the Python in the repo, including its single
largest file, sat outside every number on this page. It no longer sits outside
the MEASUREMENT: `observed_tests` in the generated file records every
`tests/**` file over the cap, so growth shows up in a diff. It still sits
outside the ENFORCEMENT, deliberately and for a measured reason -- see
`TEST_ROOT` in `_module_size_budget_core.py`, and
`test_the_observed_test_tree_is_recorded_not_gated` below, which pins the
decision end-to-end rather than trusting this paragraph.

`tan/planner/**` is a third case: inside the walk, named as out of scope
because it is a hash-audited mirror of upstream.

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
from pathlib import Path

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


#: How far the `observed_tests` record may drift from the tree before it is
#: called rotten, and both halves are MEASURED rather than picked round
#: (tan-cli#817). Over 60 consecutive `dev` commits, 22 grew a `tests/**` file
#: already over the cap, by a median of 65 lines, p90 365, max 577.
#:
#: * The 10% keeps the record honest at the only resolution it claims: its job
#:   is "test_flash_command.py is about 8000 lines", so a proportional bound
#:   preserves that on a big file where a flat one would not.
#: * The 200 floor is ~3x the measured median per-commit growth and above its
#:   75th percentile, so no ordinary PR reds this on its own -- the record
#:   only goes stale after several PRs pile onto the same file.
#:
#: This is NOT the ratchet's growth check wearing a different hat. It fails
#: only on DRIFT, in either direction, and a plain `regen` clears it with no
#: `--reason` and no ledger entry. See `TEST_ROOT` in the core module.
OBSERVED_DRIFT_FRACTION = 0.10
OBSERVED_DRIFT_FLOOR = 200


def test_the_observed_test_record_has_not_rotted():
    """A record nobody refreshes is worse than no record: it reads as current
    and is not. The fix is always the same one line -- `python
    scripts/regen_module_size_budget.py`, no flag.

    Two ways to rot, and BOTH are checked. An earlier version of this test
    walked only the committed record, so it could notice a file already in
    the record drifting and was blind to a `tests/**` file that crossed
    `MODULE_CAP` after the record was written -- i.e. to every new oversized
    test file, on day one. Reproduced before the fix: a fresh 9000-line
    `tests/commands/test_zz_brand_new_huge.py`, larger than the current
    record-holder, left this file at `8 passed`. The only thing that caught
    it was `regen --check` (rc=1), and at the time `regen_module_size_budget.py`
    appeared in no workflow, so nothing in CI saw it at all (review of #875).
    As of tan-cli#907, `--check` runs as its own step in both `ci.yml`'s
    `python` job and `parity.yml`'s `seam1-plan-shape` job -- see
    `test_module_size_budget_check_is_wired_into_ci.py`, which pins that
    directly rather than trusting this paragraph.

    A membership gap is a RECORD gap, not a budget breach, so this stays
    inside tan-cli#817's decision: it fails, and a plain `regen` with no
    `--reason` and no ledger entry clears it. That is the whole difference
    from the `tan/**` ratchet above, which demands a written reason."""
    recorded = core.load_observed_tests()
    assert recorded, (
        "`observed_tests` is empty -- tan-cli#817 exists because nothing "
        "measured python/tests/**, and an empty section is that state again"
    )

    rotten = []
    for rel, count in sorted(recorded.items()):
        path = core.PACKAGE.parent / rel
        if not path.exists():
            rotten.append(f"{rel}: recorded at {count} but no longer exists")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        allowed = max(OBSERVED_DRIFT_FLOOR, int(count * OBSERVED_DRIFT_FRACTION))
        if abs(lines - count) > allowed:
            rotten.append(f"{rel}: recorded {count}, now {lines} (drift > {allowed})")

    # The membership half: measure the tree, and compare WHICH files are over
    # the cap -- not how big they are, which the drift bound above already
    # tolerates. Both directions, because both mean the same thing (the file
    # says something about `tests/**` that the tree does not).
    measured = core.measure_observed_tests()
    for rel in sorted(set(measured) - set(recorded)):
        rotten.append(
            f"{rel}: {measured[rel]} lines, over the {core.MODULE_CAP}-line "
            "cap and missing from the record entirely"
        )
    for rel in sorted(set(recorded) - set(measured)):
        path = core.PACKAGE.parent / rel
        if path.exists():
            rotten.append(
                f"{rel}: recorded, but no longer over the {core.MODULE_CAP}-"
                "line cap"
            )

    assert rotten == [], (
        "the observed python/tests/** record has drifted from the tree (run "
        "`python scripts/regen_module_size_budget.py` -- no flag, this is not "
        "a budget):\n  " + "\n  ".join(rotten)
    )


def test_the_observed_test_tree_is_recorded_not_gated(tmp_path, monkeypatch):
    """tan-cli#817's decision, pinned end-to-end rather than asserted in prose:
    a `tests/**` file growing past the cap must regenerate CLEANLY, with no
    `--reason`, and must write NOTHING to the append-only ledger. If someone
    later folds the observed deltas into `grown`, this is what says no.

    Hermetic: every path the script touches is redirected into `tmp_path`, so
    this neither reads nor writes the real tree.
    """
    import importlib.util

    regen_path = core.PACKAGE.parent / "scripts" / "regen_module_size_budget.py"
    spec = importlib.util.spec_from_file_location("_regen_under_test", regen_path)
    regen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regen)
    # The script reaches its measurement module through its OWN `sys.path`
    # insert, so `regen.core` is a second module object loaded from the same
    # file -- patching this gate's `core` would not reach the script. Verified
    # rather than assumed: the two `__file__`s match, the objects do not.
    # Both halves are asserted -- the comment claimed the distinctness and
    # only the `__file__` match was checked, so if the gate ever switched to
    # the script's bare module spelling the two would collapse into one
    # object and this would still have passed (review of #875).
    assert Path(regen.core.__file__) == Path(core.__file__)
    assert regen.core is not core
    target = regen.core

    package = tmp_path / "tan"
    (package / "sub").mkdir(parents=True)
    (package / "sub" / "small.py").write_text("x = 1\n", encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    big_test = tests_root / "test_big.py"
    big_test.write_text("# line\n" * 900, encoding="utf-8")

    generated = tmp_path / "module_size_budget.generated.json"
    ledger = tmp_path / "LOG.md"
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "GENERATED_PATH", generated)
    monkeypatch.setattr(target, "LOG_PATH", ledger)

    # 1. Seeding an observed entry: clean, no flag, no ledger.
    assert regen.main([]) == 0
    assert json.loads(generated.read_text())["observed_tests"] == {"tests/test_big.py": 900}
    assert not ledger.exists(), "seeding the observed record must not touch the ledger"

    # 2. The decision itself: that file GROWS past the cap, a lot. Still clean,
    #    still no flag, still nothing in the ledger.
    big_test.write_text("# line\n" * 2400, encoding="utf-8")
    assert regen.main([]) == 0, (
        "growing a tests/** file was refused -- the observed record has been "
        "turned into a ratchet, which is the decision tan-cli#817 rejected"
    )
    assert json.loads(generated.read_text())["observed_tests"] == {"tests/test_big.py": 2400}
    assert not ledger.exists(), "observed growth must never write a ledger entry"

    # 3. The contrast, so none of the above passes for the wrong reason: the
    #    SAME growth on the gated side IS refused without a reason.
    (package / "sub" / "small.py").write_text("# line\n" * 2400, encoding="utf-8")
    assert regen.main([]) == 1, (
        "a tan/** module grew past the cap and regen accepted it without "
        "--reason -- then the asymmetry proved above is not a decision, the "
        "ratchet is simply off"
    )
    assert not ledger.exists()
    assert regen.main(["--reason", "deliberate"]) == 0
    assert "deliberate" in ledger.read_text(), "the gated side still logs its reasons"


def test_check_mode_reds_on_a_stale_sidecar_and_passes_once_resynced(tmp_path, monkeypatch):
    """Direct probe for tan-cli#907's CI wiring: `--check` must RED on a
    generated file that no longer matches the measured tree, and PASS once
    the file is regenerated against it.

    The scenario this reproduces is deliberately the git-mechanics one, not
    a hand-edit: `.gitattributes` does not carry `merge=union` for
    `module_size_budget.generated.json` (unioning two JSON documents that
    both add a key can leave two entries with no comma between them), so a
    real `git merge` on it either conflicts visibly or -- measured directly,
    a plain `git merge` of two branches editing different keys of a shared
    JSON object -- stitches both DISJOINT edits into one syntactically valid,
    semantically stale JSON object with NO conflict marker at all. There is
    nothing for a human or `test_no_conflict_markers.py` to catch in that
    case; `--check` re-measuring and comparing exactly is the only thing
    that can. Hermetic, same redirection pattern as
    `test_the_observed_test_tree_is_recorded_not_gated` above -- this
    changes the OBSERVED side (a `tests/**` file), not the gated `modules`
    map, specifically so no `--reason`/`--merge-resync` flag is needed to
    make the fixture's own resync step (below) succeed.
    """
    import importlib.util

    regen_path = core.PACKAGE.parent / "scripts" / "regen_module_size_budget.py"
    spec = importlib.util.spec_from_file_location("_regen_under_test_check", regen_path)
    regen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regen)
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    (package / "small.py").write_text("x = 1\n", encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    drifting_test = tests_root / "test_drifts.py"
    drifting_test.write_text("# line\n" * 900, encoding="utf-8")

    generated = tmp_path / "module_size_budget.generated.json"
    ledger = tmp_path / "LOG.md"
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "GENERATED_PATH", generated)
    monkeypatch.setattr(target, "LOG_PATH", ledger)

    # Seed a generated file that matches the (fake) tree exactly.
    assert regen.main([]) == 0
    assert regen.main(["--check"]) == 0, "a freshly regenerated sidecar must pass --check"

    # A sibling branch's edit lands on the tree, but nobody reruns the
    # regen script for it -- exactly what a clean, marker-free text merge of
    # two disjoint JSON edits leaves behind: a committed sidecar that no
    # longer describes the tree it sits next to.
    drifting_test.write_text("# line\n" * 2400, encoding="utf-8")
    rc_stale = regen.main(["--check"])
    assert rc_stale == 1, f"a stale sidecar must RED --check, got rc={rc_stale}"

    # The merge-resync fix: re-run the regen script (no flag needed here --
    # only the OBSERVED side moved, see the docstring above).
    assert regen.main([]) == 0
    rc_fresh = regen.main(["--check"])
    assert rc_fresh == 0, f"a freshly-resynced sidecar must PASS --check, got rc={rc_fresh}"


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
