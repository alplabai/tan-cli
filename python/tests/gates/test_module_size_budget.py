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

## Where the numbers live (tan-cli#668, tan-cli#1057)

Until tan-cli#668, the per-module ceilings, the function-count budget and the
worst-function budget were a hand-maintained Python dict IN THIS FILE, one
entry per over-budget module with a paragraph of prose recording why it grew.
That dict conflicted on seven separate merges in a single day, because it
stored ABSOLUTE measurements of one tree and nearly every PR perturbed at
least one entry -- and both naive conflict resolutions (`--ours`, `--theirs`)
shipped a red gate, because neither side's numbers describe the tree the
merge actually produced. Only running the gate on the merged tree does.

tan-cli#668 moved them to a generated sidecar,
`module_size_budget.generated.json`, produced by
`scripts/regen_module_size_budget.py` and never hand-edited. That fixed the
"retyped by hand" half but not the conflict rate: every branch that changed
any tracked module rewrote the same file, so it still collided on most
long-lived branches. tan-cli#1057 split it one record file per module, under
`module_size_budget.d/`, named after the module itself -- the same structural
fix tan-cli#907 applied to the ledger, and for the same reason: two branches
touching different modules never write the same path, so there is nothing for
git or GitHub to call a conflict.

Measured over 93 value-changing commits to the old single file (4278 commit
pairs): 100% of pairs collided under one file, 61.3% would still collide
under the per-package split the issue proposed, 22.4% under a per-module
split that kept the two whole-tree scalars stored, and 12.9% with those
scalars DERIVED instead. The residual 12.9% is two branches editing the SAME
module, which is a genuine same-file conflict no split removes.

`function_count_budget` and `function_worst_budget` are therefore no longer
stored anywhere: each record carries its own module's `long_functions` --
since tan-cli#1173 the actual sorted `[span, name]` list, not a count -- and
`core.MeasuredState` exposes the two whole-tree numbers as a SUM and a MAX
over those lists. The ratchet below still compares whole-tree totals, but
`scripts/regen_module_size_budget.py` no longer decides `--reason` on those
two numbers alone: see `test_an_offsetting_pair_in_one_module_needs_a_reason_naming_both`
below for why a whole-tree-neutral pair still needs one.

A merge conflict on a record file is still resolved the same way: throw
either side away and re-run the script against the merged tree -- it
re-measures from source, so the result is correct by construction rather than
reconciled. `MODULE_SIZE_BUDGET_LOG.md` carried the append-only,
one-line-per-change record of WHY a ceiling moved from 2026-08-11 through
2026-08-30; that file is FROZEN (tan-cli#907) and every new entry is its own
file under `MODULE_SIZE_BUDGET_LOG.d/`.

## What this gates, and what it only watches (tan-cli#817)

GATED: `python/tan/**`. OBSERVED, never gated: `python/tests/**`.

That distinction is the whole of tan-cli#817, and it is stated here because
the issue's first complaint was that it was stated nowhere -- "a reader who
finds test_module_size_budget.py reasonably concludes the repo has file-size
drift under control", while half the Python in the repo, including its single
largest file, sat outside every number on this page. It no longer sits outside
the MEASUREMENT: every `tests/**` file over the cap has its own `"kind":
"observed"` record, so growth shows up in a diff. It still sits outside the
ENFORCEMENT, deliberately and for a measured reason -- see `TEST_ROOT` in
`_module_size_budget_core.py`, and
`test_the_observed_test_tree_is_recorded_not_gated` below, which pins the
decision end-to-end rather than trusting this paragraph. Since tan-cli#1057
that distinction is machine-checked rather than positional: a record's `kind`
must agree with the tree its path sits in, or `core` refuses to load it at
all.

`tan/planner/**` is a third case: inside the walk, named as out of scope
because it is a hash-audited mirror of upstream.

## Why a pytest gate and not a ruff job

`python -- pytest across python/` is ALREADY a required context on `main` and
`dev`. A new CI job would have to be added to the required list to matter, and
adding a required context blocks every open PR until it has run on each of
them. This runs inside a gate that is already required, so it enforces on the
next PR with no protection change.

## How to change these numbers

Do not hand-edit anything under `module_size_budget.d/`. Run
`python scripts/regen_module_size_budget.py` -- see its own module docstring
for the `--reason` / `--merge-resync` distinction. Lowering a ceiling never
needs either flag: a module shrinking, or dropping under the cap, is always
safe and the script applies it without asking.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.gates import _module_size_budget_core as core


def test_no_module_grows_past_its_recorded_budget():
    """The ratchet. A budgeted module may shrink freely; growing past its
    recorded size fails and must be answered by regenerating the records
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
        + f"\n\nA module with no `lines` ceiling under module_size_budget.d/ "
        f"is capped at {core.MODULE_CAP}. Either extract from it, or run "
        "`python scripts/regen_module_size_budget.py --reason \"...\"` to raise "
        "its record."
    )


def test_the_module_budget_has_not_gone_stale():
    """The other direction: a record for a file that has SHRUNK well under
    its ceiling is a ratchet that stopped ratcheting. Lower it (rerun the
    regen script -- shrinking never needs a flag) so the next growth is
    caught at the new level rather than the old one.

    The slack allowed is deliberately generous (50 lines). This gate exists
    to catch a module doubling, not to make every ordinary edit regenerate a
    number."""
    budget = core.load_generated()
    slack = []
    for rel, ceiling in sorted(budget.modules.items()):
        path = core.PACKAGE.parent / rel
        if not path.exists():
            slack.append(f"{rel}: budgeted but no longer exists -- drop the record")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines <= core.MODULE_CAP:
            slack.append(f"{rel}: {lines} lines, now under {core.MODULE_CAP} -- drop the record")
        elif ceiling - lines > 50:
            slack.append(f"{rel}: {lines} lines, budget {ceiling} -- lower it")

    assert slack == [], (
        "the committed module records no longer describe the tree (run "
        "`python scripts/regen_module_size_budget.py`):\n  " + "\n  ".join(slack)
    )


def test_no_new_long_function_and_none_of_them_grows():
    """Hundreds of functions are already over 50 lines, so enumerating them
    would be a table nobody reads. The COUNT and the WORST are ratcheted
    instead: a new long function moves the count, and an existing one growing
    moves the worst.

    Both budgets are DERIVED (tan-cli#1057) -- a sum and a max over the
    per-module records -- rather than stored scalars two branches could both
    write. The comparison is still whole-tree on both sides, so a module
    gaining a long function while another loses one still passes here,
    exactly as it did when the two numbers were stored."""
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
        "regenerate the records with a reason. Longest: "
        f"{worst[1]} at {worst[0]} lines."
    )
    assert worst[0] <= budget.function_worst, (
        f"{worst[1]} is {worst[0]} lines, past the recorded worst "
        f"({budget.function_worst}). The longest function in the package "
        "getting longer is the exact drift tan-cli#408 reports."
    )


def test_the_recorded_function_facts_match_the_measurement():
    """The staleness half of the function ratchet, and the thing that makes a
    DERIVED scalar's inputs auditable per module (tan-cli#1057).

    The test above only asks whether the whole-tree total still bounds the
    tree. That direction alone cannot see a record whose `long_functions` has
    been padded upward, or one that has gone stale downward, or one that is
    missing entirely -- all three leave the derived sum wrong while the `<=`
    still holds. This compares every module's stored facts to a fresh
    measurement EXACTLY and names the functions that appeared, disappeared,
    or changed span, which is precisely what the old single file's
    `function_count_budget: 300 -> 301` could never do.

    Exact rather than tolerant, unlike the `tests/**` drift window below.
    That is a real, small tax rather than a free one: measured against 68
    non-merge `origin/dev` commits touching `python/tan/**.py` (against the
    tan-cli#1057 scheme this paragraph describes, before tan-cli#1173 existed),
    39 needed a regen under both the old single-file scheme and this one, 15
    under the old scheme only, 11 under neither, and **3** are newly taxed
    here (`8866d7fb5`, `dcf37ae45`, `3ff889093`) -- a whole-tree-neutral
    per-module function growth, which the old file structurally could not
    see. Under tan-cli#1057, the regen those 3 forced needed no `--reason` and
    wrote no ledger entry. That consequence does NOT survive tan-cli#1173
    (see `test_an_offsetting_pair_in_one_module_needs_a_reason_naming_both`
    below): the whole point of that issue is that a per-module offsetting
    move like this is now judged per function, not on the flat whole-tree
    scalar, so it is no longer safe to assume a re-run of these 3 against
    today's code stays `--reason`-free -- that would have to be re-measured
    against the commits themselves, not assumed from this paragraph.

    Worth the tax, because it is exactly what makes a padded function record
    visible: an inflated budget still bounds the tree, so the `<=` ratchet
    alone cannot catch one. Failing at the local bar instead of on the runner
    is the whole point of keeping this gate local-first (tan-cli#895)."""
    measured = core.measure_current().functions
    recorded = core.load_generated().functions
    empty = core.ModuleFunctions(entries=())

    wrong = []
    for rel in sorted(set(measured) | set(recorded)):
        was = recorded.get(rel, empty)
        now = measured.get(rel, empty)
        if was == now:
            continue
        if rel not in recorded:
            wrong.append(
                f"{rel}: has {now.count} function(s) over {core.FUNCTION_CAP} "
                f"lines (worst {now.worst}) and no record at all"
            )
        elif rel not in measured:
            wrong.append(
                f"{rel}: recorded {was.count} over-cap function(s) but the "
                "module now has none"
            )
        else:
            appeared = sorted(set(now.entries) - set(was.entries))
            disappeared = sorted(set(was.entries) - set(now.entries))
            parts = []
            if appeared:
                parts.append(
                    "now over cap: " + ", ".join(f"{name} at {span}" for span, name in appeared)
                )
            if disappeared:
                parts.append(
                    "no longer over cap (or renamed/moved/deleted): "
                    + ", ".join(f"{name} at {span}" for span, name in disappeared)
                )
            wrong.append(f"{rel}: " + "; ".join(parts))

    assert wrong == [], (
        "the committed per-module function records disagree with the tree, so "
        "the derived function_count_budget "
        f"({sum(f.count for f in recorded.values())}) and function_worst_budget "
        f"({max((f.worst for f in recorded.values()), default=0)}) are not what "
        "the tree actually measures (run `python "
        "scripts/regen_module_size_budget.py`):\n  " + "\n  ".join(wrong)
    )


def test_every_record_describes_a_module_that_still_exists():
    """A record whose module was deleted or renamed is dead weight that still
    contributes to a derived scalar. `test_the_module_budget_has_not_gone_stale`
    catches this only for records that carry a `lines` ceiling; most records
    do not (60 of the 90 gated ones exist purely for their function facts),
    so without this a deleted module's record could inflate the function
    budget indefinitely."""
    orphans = [
        rel
        for rel in sorted(core.load_generated().functions)
        if not (core.PACKAGE.parent / rel).exists()
    ]
    assert orphans == [], (
        "these modules have a record under module_size_budget.d/ but no "
        "longer exist (run `python scripts/regen_module_size_budget.py`):\n  "
        + "\n  ".join(orphans)
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


#: How far an `observed` record may drift from the tree before it is called
#: rotten, and both halves are MEASURED rather than picked round
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
        "no `observed` record exists -- tan-cli#817 exists because nothing "
        "measured python/tests/**, and an empty record tree is that state again"
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
    # tolerates. Both directions, because both mean the same thing (a record
    # says something about `tests/**` that the tree does not).
    measured = core.measure_observed_tests()
    for rel in sorted(set(measured) - set(recorded)):
        rotten.append(
            f"{rel}: {measured[rel]} lines, over the {core.MODULE_CAP}-line "
            "cap and missing from the record tree entirely"
        )
    for rel in sorted(set(recorded) - set(measured)):
        path = core.PACKAGE.parent / rel
        if path.exists():
            rotten.append(
                f"{rel}: recorded, but no longer over the {core.MODULE_CAP}-"
                "line cap"
            )

    assert rotten == [], (
        "the observed python/tests/** records have drifted from the tree (run "
        "`python scripts/regen_module_size_budget.py` -- no flag, this is not "
        "a budget):\n  " + "\n  ".join(rotten)
    )


def _load_regen(name: str):
    """Load `scripts/regen_module_size_budget.py` by path -- it lives outside
    the package and is never otherwise imported by this suite."""
    import importlib.util

    regen_path = core.PACKAGE.parent / "scripts" / "regen_module_size_budget.py"
    spec = importlib.util.spec_from_file_location(name, regen_path)
    regen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regen)
    return regen


def _redirect(target, monkeypatch, tmp_path):
    """Point every path the script touches into `tmp_path`. `CAPS_PATH` is
    computed from `RECORD_DIR` at import time, so patching only the directory
    would leave the caps file pointing at the REAL tree -- and the script
    writes it on every run."""
    record_dir = tmp_path / "module_size_budget.d"
    monkeypatch.setattr(target, "RECORD_DIR", record_dir)
    monkeypatch.setattr(target, "CAPS_PATH", record_dir / "_caps.json")
    return record_dir


def test_the_observed_test_tree_is_recorded_not_gated(tmp_path, monkeypatch):
    """tan-cli#817's decision, pinned end-to-end rather than asserted in prose:
    a `tests/**` file growing past the cap must regenerate CLEANLY, with no
    `--reason`, and must write NOTHING to the append-only ledger. If someone
    later folds the observed deltas into `grown`, this is what says no.

    Hermetic: every path the script touches is redirected into `tmp_path`, so
    this neither reads nor writes the real tree.
    """
    regen = _load_regen("_regen_under_test")
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

    ledger = tmp_path / "LOG.md"
    ledger_dir = tmp_path / "LOG.d"
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", ledger)
    monkeypatch.setattr(target, "LOG_DIR", ledger_dir)
    record_dir = _redirect(target, monkeypatch, tmp_path)

    def _entry_files() -> list[Path]:
        return sorted(ledger_dir.glob("*.md")) if ledger_dir.exists() else []

    # 1. Seeding an observed record: clean, no flag, no ledger.
    assert regen.main([]) == 0
    assert target.load_observed_tests() == {"tests/test_big.py": 900}
    assert (record_dir / "tests" / "test_big.py.json").exists(), (
        "an observed record must land at the path its module names -- that "
        "path IS its key (tan-cli#1057)"
    )
    assert not _entry_files(), "seeding the observed record must not touch the ledger"

    # 2. The decision itself: that file GROWS past the cap, a lot. Still clean,
    #    still no flag, still nothing in the ledger.
    big_test.write_text("# line\n" * 2400, encoding="utf-8")
    assert regen.main([]) == 0, (
        "growing a tests/** file was refused -- the observed record has been "
        "turned into a ratchet, which is the decision tan-cli#817 rejected"
    )
    assert target.load_observed_tests() == {"tests/test_big.py": 2400}
    assert not _entry_files(), "observed growth must never write a ledger entry"

    # 3. The contrast, so none of the above passes for the wrong reason: the
    #    SAME growth on the gated side IS refused without a reason.
    (package / "sub" / "small.py").write_text("# line\n" * 2400, encoding="utf-8")
    assert regen.main([]) == 1, (
        "a tan/** module grew past the cap and regen accepted it without "
        "--reason -- then the asymmetry proved above is not a decision, the "
        "ratchet is simply off"
    )
    assert not _entry_files()
    assert regen.main(["--reason", "deliberate"]) == 0
    # tan-cli#907: a NEW file under LOG_DIR, never an append to LOG_PATH --
    # the old single-file ledger is frozen and gets nothing written to it any
    # more (asserted directly here, not just by omission).
    entries = _entry_files()
    assert len(entries) == 1, f"expected exactly one new ledger entry file, got {entries}"
    assert "deliberate" in entries[0].read_text(), "the gated side still logs its reasons"
    assert not ledger.exists(), "the frozen single-file ledger must never be written to again"


def test_a_whole_tree_neutral_function_move_now_needs_a_reason(tmp_path, monkeypatch):
    """tan-cli#1057 pinned the OPPOSITE of this outcome: one module gaining a
    long function while another loses one moved neither `function_count_budget`
    nor `function_worst_budget`, so it needed no `--reason`, and this test used
    to assert exactly that (`test_a_whole_tree_neutral_function_move_needs_no_reason`).
    tan-cli#1173 overturns it, deliberately and for a measured reason: the two
    whole-tree scalars staying flat is precisely how a function crossing
    `FUNCTION_CAP` can hide behind another one dropping below it, whether the
    two are in the same module (the exact PR #1170 shape) or, as here, two
    different ones. A function newly over the cap is now growth in its own
    right regardless of what any other function anywhere in the tree did --
    see `_function_deltas` in `scripts/regen_module_size_budget.py`.

    The whole-tree DERIVED numbers themselves are unaffected by this move (that
    half of tan-cli#1057 still holds -- `function_count`/`function_worst` read
    identically before and after), which is exactly why they were never enough
    on their own."""
    regen = _load_regen("_regen_under_test_neutral")
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    ledger_dir = tmp_path / "LOG.d"
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", ledger_dir)
    _redirect(target, monkeypatch, tmp_path)

    def long_fn(name: str, body_lines: int) -> str:
        return f"def {name}():\n" + "    pass\n" * body_lines + "\n"

    (package / "a.py").write_text(long_fn("f", 80), encoding="utf-8")
    (package / "b.py").write_text("x = 1\n", encoding="utf-8")
    # Seeding a tree that already holds a long function IS whole-tree growth
    # (0 -> 1), so it legitimately needs a reason; the move below is what
    # this test is about.
    assert regen.main(["--reason", "seed the fixture"]) == 0
    seeded_entries = sorted(ledger_dir.glob("*.md"))
    before = target.load_generated()
    assert before.function_count == 1

    # The move: `a.py` loses its long function, `b.py` gains one of the same
    # span. Whole-tree this nets to zero; per FUNCTION, `b.py:g` is a brand
    # new over-cap function tan-cli#1173 says must be its own growth event.
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    (package / "b.py").write_text(long_fn("g", 80), encoding="utf-8")
    rc = regen.main([])
    assert rc == 1, (
        "a function newly over FUNCTION_CAP was accepted without --reason just "
        "because a different function elsewhere dropped below it -- tan-cli#1173 "
        "exists to refuse exactly this"
    )
    assert sorted(ledger_dir.glob("*.md")) == seeded_entries, (
        "a refused regen must not write anything, committed or logged"
    )

    assert regen.main(["--reason", "move f to g"]) == 0
    new_entries = sorted(set(ledger_dir.glob("*.md")) - set(seeded_entries))
    assert len(new_entries) == 1
    text = new_entries[0].read_text(encoding="utf-8")
    assert "b.py:g" in text and "new entry" in text
    # `f` did not shrink below the cap -- `a.py` no longer defines it at all
    # (rewritten to `x = 1\n` above) -- so the ledger must say it is GONE, not
    # falsely claim it "dropped (now under the cap)" (tan-cli#1173 review).
    assert "a.py:f" in text and "gone" in text, (
        "the move's OTHER half -- f no longer existing in a.py at all -- must "
        "be named in the same entry, or the entry tells only half of what moved"
    )

    after = target.load_generated()
    assert after.function_count == before.function_count, (
        "the whole-tree DERIVED count is still unaffected by this move -- "
        "tan-cli#1057's half of the design is intact, tan-cli#1173 only adds "
        "the per-function check on top of it"
    )
    assert after.function_worst == before.function_worst
    assert set(after.functions) == {"tan/b.py"}


def test_an_offsetting_pair_in_one_module_needs_a_reason_naming_both(tmp_path, monkeypatch):
    """The literal tan-cli#1173 shape, constructed directly rather than
    inferred: PR #1170 grew `_sdk_credential` `50 -> 63 -> 69` past
    `FUNCTION_CAP` while `_data` fell `51 -> 47` below it, IN THE SAME MODULE,
    in the same diff -- and `bootstrap_cmd.py.json`'s `long_functions` read
    `19` before and after, because a count and a max cannot tell that apart
    from no movement at all.

    Mutation-proven in both directions, per the issue's acceptance text: the
    offsetting pair is refused without `--reason` (below), and an unrelated,
    non-crossing edit made afterwards is still silent (phase 3). The headline
    `assert rc == 1` below has to be the thing tan-cli#1173's growth rule
    (`_function_deltas`) actually earns: a review of this issue proved that
    with `_giant` living in `tan/creds.py` itself, reverting ONLY that growth
    rule back to the pre-tan-cli#1173 whole-tree `_scalar_delta` pair left
    `assert rc == 1` PASSING anyway, because `_giant`'s 818 lines pushed
    `tan/creds.py` itself (923 -> 938 lines) past the unrelated, pre-existing
    `MODULE_CAP` (800) module-LINE ratchet -- a second, incidental reason to
    refuse that has nothing to do with this issue. `_giant` now lives in its
    own module (`tan/giant.py`) so the only thing left that can make this
    assertion fail is the per-function growth rule under test."""
    regen = _load_regen("_regen_under_test_offsetting_pair")
    target = regen.core

    def long_fn(name: str, body_lines: int) -> str:
        return f"def {name}():\n" + "    pass\n" * body_lines + "\n"

    package = tmp_path / "tan"
    package.mkdir()
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    ledger_dir = tmp_path / "LOG.d"
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", ledger_dir)
    _redirect(target, monkeypatch, tmp_path)

    module = package / "creds.py"

    # A dominant function, unchanged throughout, in its OWN module --
    # `bootstrap_cmd.py`'s real `_run` at 819 lines plays exactly this role.
    # It has to dwarf both movers for the WHOLE-TREE derived pair
    # (`function_count`/`function_worst`) to read flat across the move below
    # -- the same way a bigger function elsewhere absorbed the max in PR
    # #1170 -- and it has to be a SEPARATE module from `_sdk_credential`/
    # `_data`, or its own 818-line body puts `tan/creds.py` itself over
    # `MODULE_CAP` and the unrelated module-LINE ratchet forces `--reason` on
    # its own (see this test's docstring). Whole tree, not per module, is
    # what has to stay flat, and `function_count`/`function_worst` are
    # summed/maxed across every module -- so keeping `_giant` in its own file
    # still keeps it dominant for the tree-wide max without inflating
    # `creds.py`'s own line count at all.
    (package / "giant.py").write_text(long_fn("_giant", 818), encoding="utf-8")

    # Seed: `_data` already over the cap (51 lines); `_sdk_credential` sits
    # right AT the cap (50 lines: `span > FUNCTION_CAP` is strict, so 50 is
    # not yet tracked) -- the pre-PR #1170 state.
    module.write_text(long_fn("_sdk_credential", 49) + long_fn("_data", 50), encoding="utf-8")
    assert regen.main(["--reason", "seed the fixture"]) == 0
    seeded_entries = sorted(ledger_dir.glob("*.md"))
    seeded = target.load_generated()
    before = seeded.functions["tan/creds.py"]
    assert dict((name, span) for span, name in before.entries) == {"_data": 51}
    assert seeded.function_count == 2 and seeded.function_worst == 819, (
        "the fixture is wrong unless the whole tree already reads 2 / 819 "
        "before the move below -- that is the baseline the move must leave "
        "unchanged"
    )

    # 1. The offsetting pair, same module, same diff: `_sdk_credential` grows
    #    past the cap, `_data` shrinks below it. Both DERIVED whole-tree
    #    numbers stay exactly flat (count 2 -> 2, worst 819 -> 819, `_giant`
    #    dwarfing both movers from its own module) -- the exact silence PR
    #    #1170 hit.
    module.write_text(long_fn("_sdk_credential", 68) + long_fn("_data", 46), encoding="utf-8")
    current = target.measure_current()
    assert (
        current.function_count == seeded.function_count
        and current.function_worst == seeded.function_worst
    ), (
        "the fixture must reproduce a whole-tree-neutral move, or this is not "
        "actually testing what PR #1170 hit"
    )
    rc = regen.main([])
    assert rc == 1, (
        "an offsetting per-function pair in one module was accepted without "
        "--reason -- this is the exact shape tan-cli#1173 exists to refuse"
    )
    assert sorted(ledger_dir.glob("*.md")) == seeded_entries, "a refusal must write nothing"

    # 2. With --reason, the regen succeeds and the ledger line names BOTH
    #    functions -- the one that crossed the cap AND the one that dropped.
    assert regen.main(["--reason", "PR #1170 shape"]) == 0
    new_entries = sorted(set(ledger_dir.glob("*.md")) - set(seeded_entries))
    assert len(new_entries) == 1
    text = new_entries[0].read_text(encoding="utf-8")
    assert "creds.py:_sdk_credential" in text and "new entry at 69" in text
    assert "creds.py:_data" in text and "dropped" in text, (
        "the ledger entry must name the function that dropped below the cap "
        "too, or it tells only half of what moved in this diff"
    )
    after = target.load_generated().functions["tan/creds.py"]
    assert dict((name, span) for span, name in after.entries) == {"_sdk_credential": 69}

    # 3. Mutation-proof, the other direction: an unrelated edit that does not
    #    cross FUNCTION_CAP anywhere stays silent, no flag needed.
    (package / "unrelated.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    assert regen.main([]) == 0, "an edit that crosses no cap must not demand --reason"
    assert sorted(ledger_dir.glob("*.md")) == sorted(seeded_entries + new_entries), (
        "an unrelated, non-crossing change must not write a ledger entry either"
    )


def test_a_dropped_entry_is_only_a_shrink_if_the_name_still_exists(tmp_path, monkeypatch):
    """A review of tan-cli#1173 measured `_function_deltas` directly and found
    a rename, a module move, or a plain deletion of an already-over-cap
    function all produce the same false claim: `grown` gets a "new entry"
    line (for a rename/move) and `shrunk` gets
    `"{name}: {span} -> dropped (now under the cap)"` -- which is not true.
    The function did not shrink under `FUNCTION_CAP`; it stopped existing
    under that name in that module. The ledger is append-only, so a false
    statement about a cap crossing lives there forever.

    `_function_deltas`'s third argument (`new_names`, from
    `core.all_function_names_by_module()`) is what tells the two apart: if
    the name still labels SOME function in that module right now, it really
    did shrink below the cap; if not, it is gone (renamed, moved elsewhere,
    or deleted), and nothing here claims to know which."""
    regen = _load_regen("_regen_under_test_disappearance")

    old = {"a.py": regen.core.ModuleFunctions(entries=((63, "f"),))}
    new = {"a.py": regen.core.ModuleFunctions(entries=())}

    # `f` is still there in `a.py`, just shorter now -- a real shrink.
    grown, shrunk = regen._function_deltas(old, new, {"a.py": {"f"}})
    assert grown == []
    assert shrunk == ["a.py:f: 63 -> dropped (now under the cap)"]

    # `f` no longer names anything in `a.py` (renamed to `g` in the same
    # module, moved to another module entirely, or just deleted) -- not a
    # shrink, and the ledger must not say it is one.
    for still_there in ({"a.py": {"g"}}, {"a.py": set()}, {}):
        grown, shrunk = regen._function_deltas(old, new, still_there)
        assert grown == []
        assert shrunk == ["a.py:f: 63 -> gone (renamed, moved, or deleted -- not a shrink)"]


def test_check_mode_reds_on_a_stale_record_and_passes_once_resynced(tmp_path, monkeypatch):
    """Direct probe for tan-cli#907's CI wiring: `--check` must RED on a
    record tree that no longer matches the measured tree, and PASS once it is
    regenerated against it.

    The scenario this reproduces is deliberately the git-mechanics one, not
    a hand-edit. Splitting per module (tan-cli#1057) narrows the shape but
    does not retire it: a merge that brings in one side's edit to a module
    together with the other side's record for it leaves a syntactically
    valid, semantically STALE record with no conflict marker for a human or
    `test_no_conflict_markers.py` to catch. `--check` re-measuring and
    comparing exactly is the only thing that can. Hermetic, same redirection
    pattern as `test_the_observed_test_tree_is_recorded_not_gated` above --
    this changes the OBSERVED side (a `tests/**` file), not a gated ceiling,
    specifically so no `--reason`/`--merge-resync` flag is needed to make the
    fixture's own resync step (below) succeed.
    """
    regen = _load_regen("_regen_under_test_check")
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    (package / "small.py").write_text("x = 1\n", encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    drifting_test = tests_root / "test_drifts.py"
    drifting_test.write_text("# line\n" * 900, encoding="utf-8")

    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", tmp_path / "LOG.d")
    _redirect(target, monkeypatch, tmp_path)

    # Seed records that match the (fake) tree exactly.
    assert regen.main([]) == 0
    assert regen.main(["--check"]) == 0, "freshly regenerated records must pass --check"

    # A sibling branch's edit lands on the tree, but nobody reruns the
    # regen script for it -- exactly what a clean, marker-free merge leaves
    # behind: a committed record that no longer describes the file it names.
    drifting_test.write_text("# line\n" * 2400, encoding="utf-8")
    rc_stale = regen.main(["--check"])
    assert rc_stale == 1, f"a stale record must RED --check, got rc={rc_stale}"

    # The merge-resync fix: re-run the regen script (no flag needed here --
    # only the OBSERVED side moved, see the docstring above).
    assert regen.main([]) == 0
    rc_fresh = regen.main(["--check"])
    assert rc_fresh == 0, f"freshly-resynced records must PASS --check, got rc={rc_fresh}"


def test_a_padded_record_reds_check_and_names_its_module(tmp_path, monkeypatch):
    """The defect tan-cli#668 exists to make structurally impossible, at the
    new storage's granularity: a hand-edited value that no real measurement
    produced. Appending a fake entry to one record's `long_functions` list
    inflates the DERIVED `function_count_budget` -- which the `<=` ratchet
    alone cannot see, because a too-generous budget still bounds the tree --
    so this pins that both `--check` and
    `test_the_recorded_function_facts_match_the_measurement` catch it, and
    that the message names the module rather than a whole-tree scalar."""
    import json

    regen = _load_regen("_regen_under_test_padded")
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    (package / "a.py").write_text("def f():\n" + "    pass\n" * 80, encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", tmp_path / "LOG.d")
    record_dir = _redirect(target, monkeypatch, tmp_path)

    assert regen.main(["--reason", "seed the fixture"]) == 0
    assert regen.main(["--check"]) == 0

    record = record_dir / "tan" / "a.py.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["long_functions"] == [[81, "f"]]
    data["long_functions"].append([55, "fake_padded_function"])
    record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    assert regen.main(["--check"]) == 1, "a padded record must RED --check"
    assert target.load_generated().function_count == 2
    measured = target.measure_current()
    assert measured.function_count == 1, (
        "the measurement is unaffected by the padded record -- measure_current "
        "never reads the committed records, which is tan-cli#668's constraint"
    )


def test_a_record_that_disagrees_with_its_own_path_or_kind_is_refused(tmp_path, monkeypatch):
    """The cross-file half of `_load_json`'s duplicate-key guard (tan-cli#586).

    With one file there was exactly one way to spell a module key twice, and
    `object_pairs_hook` caught it. With a file per module the same silent
    last-write-wins is reachable by copying a record to a second path, and by
    a record claiming the wrong `kind` -- which would let a `tests/**`
    MEASUREMENT be read as a `tan/**` ceiling, inverting tan-cli#817. Both
    raise; neither is coerced."""
    import json

    regen = _load_regen("_regen_under_test_identity")
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    (package / "a.py").write_text("def f():\n" + "    pass\n" * 80, encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", tmp_path / "LOG.d")
    record_dir = _redirect(target, monkeypatch, tmp_path)
    assert regen.main(["--reason", "seed the fixture"]) == 0

    record = record_dir / "tan" / "a.py.json"
    original = record.read_text(encoding="utf-8")

    # A copy at a second path still claiming the first path's module.
    copy = record_dir / "tan" / "b.py.json"
    copy.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="its path says"):
        target.load_generated()
    copy.unlink()

    # A record whose kind contradicts the tree its path sits in.
    data = json.loads(original)
    data["kind"] = target.KIND_OBSERVED
    record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be 'budget'"):
        target.load_generated()
    record.write_text(original, encoding="utf-8")

    # A stray file that is neither a record nor one of the two known
    # non-record files must not be silently skipped.
    (record_dir / "stray.txt").write_text("hello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="neither records nor"):
        target.load_generated()


def test_a_long_functions_entry_at_or_under_the_cap_is_refused(tmp_path, monkeypatch):
    """`long_functions` (tan-cli#1173) holds only over-cap functions by
    construction -- `measure_current` never puts a `span <= FUNCTION_CAP`
    entry in the list it builds. An entry at or under the cap could therefore
    only get into a record by a hand-edit, which is exactly the tan-cli#668
    class this whole file exists to make impossible; `load_generated` raises
    rather than silently accepting it (and, unlike a padded-upward entry,
    doing so would otherwise UNDER-count -- a hand-edit could hide a real
    over-cap function behind a bogus at-cap one with the same list length)."""
    import json

    regen = _load_regen("_regen_under_test_at_cap")
    target = regen.core

    package = tmp_path / "tan"
    package.mkdir()
    (package / "a.py").write_text("def f():\n" + "    pass\n" * 80, encoding="utf-8")
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    monkeypatch.setattr(target, "PACKAGE", package)
    monkeypatch.setattr(target, "TEST_ROOT", tests_root)
    monkeypatch.setattr(target, "LOG_PATH", tmp_path / "LOG.md")
    monkeypatch.setattr(target, "LOG_DIR", tmp_path / "LOG.d")
    record_dir = _redirect(target, monkeypatch, tmp_path)
    assert regen.main(["--reason", "seed the fixture"]) == 0

    record = record_dir / "tan" / "a.py.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["long_functions"][0][0] = target.FUNCTION_CAP  # exactly at the cap, not over it
    record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="at or under FUNCTION_CAP"):
        target.load_generated()


def test_the_records_have_no_duplicate_module_keys():
    """A duplicate key inside a record is invisible to every other test in
    this file, because JSON (like the Python dict literal it replaced)
    collapses a duplicate on parse -- the LAST spelling wins and the earlier
    one is dead text (tan-cli#586's class of defect, which this design does
    not get for free just by moving to JSON, nor by splitting the JSON into
    many files)."""
    try:
        core.load_generated()
        core.load_observed_tests()
    except ValueError as err:
        pytest.fail(str(err))


def test_the_record_tree_declares_the_caps_this_gate_uses():
    """`module_size_budget.d/_caps.json` carries the caps the records were
    measured against, so the record tree is self-describing without a second
    source. They must agree with the constants this gate actually enforces --
    a drift here would mean the committed records and the running gate
    silently disagree about the policy, not just the measurements."""
    caps = core.load_caps()
    assert caps["module_cap"] == core.MODULE_CAP
    assert caps["function_cap"] == core.FUNCTION_CAP
