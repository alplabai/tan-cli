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
stored anywhere: each record carries its own module's `long_functions` /
`worst_function`, and `core.MeasuredState` exposes the two whole-tree numbers
as a SUM and a MAX over those -- exactly how `measure_current` always
computed them. The ratchet below still compares whole-tree totals and means
what it always meant.

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

`tan/planner/**` is a third case, and only PART of it is exempted from the
GROWTH ratchet's practical remedy -- not from the walk or the ratchet itself,
which still apply to every `.py` under it the same as anywhere else in
`tan/**` (`core.modules()` is `PACKAGE.rglob("*.py")`, no prefix filter: a
`PINNED_HASHES` module that grows past its recorded budget still fails
`test_no_module_grows_past_its_recorded_budget` and still needs a `--reason`
regen like any other). What IS different for its `PINNED_HASHES` subset (a
hash-audited, 3-way-merged mirror of upstream) is that SPLITTING one -- the
usual remedy for an over-budget module -- would turn every future upstream
hunk into a merge conflict, so
`test_the_mirrored_planner_is_named_as_out_of_scope` records that an
oversized one of them is upstream's to split, not this repo's, rather than
demanding a split this repo cannot safely make. Its
`HAND_PORT_SOURCES` modules (e.g. `template.py`) are NOT mirrored the same
way -- they are flagged against upstream, never merged -- so that remedy is
available and a split is expected like any other oversized module
(tan-cli#1142). See `test_the_mirrored_planner_is_named_as_out_of_scope`.

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
from tests.gates.test_planner_relocation_freshness import PINNED_HASHES


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
    measurement EXACTLY and names the module, which is precisely what the old
    single file's `function_count_budget: 300 -> 301` could never do.

    Exact rather than tolerant, unlike the `tests/**` drift window below.
    That is a real, small tax rather than a free one: measured against 68
    non-merge `origin/dev` commits touching `python/tan/**.py`, 39 needed a
    regen under both the old single-file scheme and this one, 15 under the old
    scheme only, 11 under neither, and **3** are newly taxed here
    (`8866d7fb5`, `dcf37ae45`, `3ff889093`) -- a whole-tree-neutral per-module
    function growth, which the old file structurally could not see. The regen
    those 3 force needs no `--reason` and writes no ledger entry.

    Worth the tax, because it is exactly what makes a padded function record
    visible: an inflated budget still bounds the tree, so the `<=` ratchet
    alone cannot catch one. Failing at the local bar instead of on the runner
    is the whole point of keeping this gate local-first (tan-cli#895)."""
    measured = core.measure_current().functions
    recorded = core.load_generated().functions
    empty = core.ModuleFunctions(count=0, worst=0)

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
            wrong.append(
                f"{rel}: recorded long_functions {was.count} / worst_function "
                f"{was.worst}, measured {now.count} / {now.worst}"
            )

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
    """Only `tan/planner/**`'s `PINNED_HASHES` modules are a hash-audited,
    3-way-MERGED relocation of alp-sdk's `scripts/alp_orchestrate/**`.
    Splitting one of THOSE would make it diverge in SHAPE from a moving
    upstream file, turning every future upstream hunk into a merge conflict --
    which `test_planner_relocation_freshness.py` exists to prevent -- so an
    oversized one of them is upstream's to fix, and this records that rather
    than leaving the next reader to rediscover it from a failing hash.

    Deliberately narrower than "every budgeted module under `MIRRORED_PREFIX`"
    (tan-cli#1142 review): a `HAND_PORT_SOURCES` module living under the same
    prefix (e.g. `template.py`) is FLAGGED against upstream, never merged, so
    it has no base/theirs/ours triple for a split to break, and this repo may
    split one like any other oversized module -- `template.py` itself did, in
    the same change that narrowed this assertion. Widening it back to the
    whole prefix would re-assert the false "cannot be split" claim tan-cli#1142
    corrected in three other places (`_module_size_budget_core.MIRRORED_PREFIX`,
    `template.py`'s former `_GUARDS` comment, `document_guards.py`)."""
    budget = core.load_generated()
    mirrored = [
        rel
        for rel in budget.modules
        if rel.startswith(core.MIRRORED_PREFIX)
        and rel[len(core.MIRRORED_PREFIX):] in PINNED_HASHES
    ]
    assert mirrored, "no PINNED_HASHES planner module is budgeted -- has the mirror moved?"
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


def test_a_whole_tree_neutral_function_move_needs_no_reason(tmp_path, monkeypatch):
    """tan-cli#1057's meaning-preservation, pinned rather than asserted in
    prose. The ratchet has always been WHOLE-TREE: one module gaining a long
    function while another loses one moved neither `function_count_budget`
    nor `function_worst_budget`, so it needed no `--reason`. Storing those
    facts per module makes it trivially easy to start judging growth per
    RECORD instead, which would quietly convert a whole-tree ratchet into a
    per-module one -- the exact design change tan-cli#1057's issue text said
    deserves its own review. This is what says no.
    """
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
    # span. Per record that is a rise in `b.py`; whole-tree it is a no-op.
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    (package / "b.py").write_text(long_fn("g", 80), encoding="utf-8")
    rc = regen.main([])
    assert rc == 0, (
        "a whole-tree-neutral function move was refused without --reason -- "
        "the ratchet has been narrowed to per-module, which is a change to "
        "what its numbers MEAN, not a storage change (tan-cli#1057)"
    )
    assert sorted(ledger_dir.glob("*.md")) == seeded_entries, (
        "a whole-tree-neutral move must not write a ledger entry either"
    )
    after = target.load_generated()
    assert after.function_count == before.function_count
    assert after.function_worst == before.function_worst
    assert set(after.functions) == {"tan/b.py"}


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
    produced. Padding one record's `long_functions` inflates the DERIVED
    `function_count_budget` -- which the `<=` ratchet alone cannot see,
    because a too-generous budget still bounds the tree -- so this pins that
    both `--check` and `test_the_recorded_function_facts_match_the_measurement`
    catch it, and that the message names the module rather than a whole-tree
    scalar."""
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
    data["long_functions"] = 99
    record.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    assert regen.main(["--check"]) == 1, "a padded record must RED --check"
    assert target.load_generated().function_count == 99
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
