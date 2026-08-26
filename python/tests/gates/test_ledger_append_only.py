# SPDX-License-Identifier: Apache-2.0
"""Gate: `MODULE_SIZE_BUDGET_LOG.md` is append-only with respect to its
merge-base (tan-cli#906).

See `_ledger_append_only_core.py`'s module docstring for the full incident
history and the design this replays: a naive "current starts with dev's
content" check passes tan-cli#902 by construction (a merge makes `dev`
itself an ancestor of the merge commit, collapsing the comparison to
identity), so a merge commit gets a two-sided check instead -- each parent's
own unique tail must survive as an order-preserving subsequence of whatever
follows the merge's true common ancestor.

The live gate below (`test_the_real_ledger_is_append_only`) runs the check
against THIS repository, exactly as an ordinary `pytest tests -q` would. The
rest of this file proves, with real `git` repositories built and merged
under `tmp_path` (not hand-typed strings standing in for a merge), that the
design actually does what tan-cli#906 asked for: it must both (1) fail on a
reconstruction of tan-cli#902's shape, (2) fail on a reconstruction of
tan-cli#878's shape, (3) pass a legitimate interleaved-by-date merge, and
(4) pass a plain append. A design that only catches one of the two real
incidents is half a gate -- so all four are asserted here, not just the two
failure cases.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.gates import _ledger_append_only_core as core

LEDGER_NAME = "LEDGER.md"


# ---------------------------------------------------------------------------
# the live gate
# ---------------------------------------------------------------------------


def test_the_real_ledger_is_append_only():
    """The actual gate.

    A local developer run `pytest.skip`s -- loudly, naming what is missing --
    when no base is determinable (a detached checkout, a clone with no `dev`
    fetched); a skip is visible in `-rs` output and is never mistaken for a
    pass, unlike the silent "nothing checked" this gate exists to replace.

    A CI run (`GITHUB_ACTIONS` set, per GitHub's own documented convention)
    does NOT get that leniency: it is a hard `pytest.fail`, not a skip.
    tan-cli#906 fix round, blocker 1 -- a skip is invisible in the default
    `pytest tests -q` summary line CI actually reads (no `-rs`), so "no base
    determinable" and "the ledger is fine" were indistinguishable from the CI
    log alone. Worse, every CI checkout at the time this was found WAS
    undeterminable (the default `actions/checkout` depth-1 shallow clone
    cannot resolve `dev`/`HEAD^1`/`HEAD^2` at all), so this gate had never
    once actually run in CI -- a `pytest.skip` here is not a fallback for a
    rare local edge case, it is the everyday CI shape, and a gate that
    degrades to green on its own everyday shape is not armed. The checkout
    steps that feed this test now fetch full history precisely so this
    branch should never be reached in CI; if it is, that regressed and must
    fail loudly, not quietly resume skipping."""
    verdict = core.find_problems(core.REPO, core.LEDGER_REL_PATH)
    if not verdict.determinable:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(
                "base not determinable while running in CI (GITHUB_ACTIONS "
                f"is set) -- this must never silently pass or skip: {verdict.skip_reason}"
            )
        pytest.skip(verdict.skip_reason)
    assert verdict.problems == [], "\n".join(verdict.problems)


def test_the_ledger_file_still_exists_at_its_pinned_path():
    """Guard against `LEDGER_REL_PATH` drifting from a real file -- e.g. a
    rename that forgot to update the constant. `_file_lines_at` and
    `_working_tree_lines` both return `[]` for a path that resolves nowhere,
    so a stale `LEDGER_REL_PATH` makes `find_problems` compare `[] == []` on
    every side, forever: `determinable=True, problems=[]`, indistinguishable
    from a real pass. This assertion is what turns that into a real failure
    instead of a silently-green gate that has stopped checking anything."""
    assert (core.REPO / core.LEDGER_REL_PATH).is_file()


def test_the_repo_root_is_a_git_checkout():
    """Guard against `core.REPO` pointing somewhere with no `.git` at all,
    which would make every other assertion in this module vacuous -- the
    exact failure mode `test_no_conflict_markers.py` guards the same way."""
    assert (core.REPO / ".git").exists()


# ---------------------------------------------------------------------------
# hermetic reconstructions -- real git repos under tmp_path
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "dev"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    return repo


def _write_ledger(repo: Path, text: str) -> None:
    (repo / LEDGER_NAME).write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    add = _git(["add", "-A"], repo)
    assert add.returncode == 0, add.stderr
    commit = _git(["commit", "-q", "-m", message], repo)
    assert commit.returncode == 0, commit.stderr
    sha = _git(["rev-parse", "HEAD"], repo)
    return sha.stdout.strip()


_SEED = "- 2026-08-01 -- seed entry\n- 2026-08-05 -- pre-existing tail entry\n"


def _seed_repo_with_dev_and_feature(tmp_path: Path) -> Path:
    """`dev` and `feature` both start at the same commit, carrying `_SEED`.
    Callers diverge them from here."""
    repo = _init_repo(tmp_path)
    _write_ledger(repo, _SEED)
    _commit(repo, "seed")
    _git(["checkout", "-q", "-b", "feature"], repo)
    return repo


# --- (1) tan-cli#902's shape: a wholesale `--theirs` merge resolution -----


def test_a_wholesale_theirs_merge_resolution_drops_our_entry_and_is_caught(tmp_path):
    repo = _seed_repo_with_dev_and_feature(tmp_path)

    # `feature` (ours) adds its own reasoned entry.
    _write_ledger(repo, _SEED + "- 2026-08-10 -- tan-cli#896: our reasoned entry\n")
    _commit(repo, "feature: reasoned entry")

    # `dev` (theirs) advances independently, in parallel.
    _git(["checkout", "-q", "dev"], repo)
    _write_ledger(repo, _SEED + "- 2026-08-11 -- dev: an unrelated entry\n")
    _commit(repo, "dev: unrelated entry")

    # Merge dev into feature -- both sides appended at the same tail, so
    # this is a genuine conflict, exactly like the real tan-cli#902 shape.
    _git(["checkout", "-q", "feature"], repo)
    merge = _git(["merge", "--no-ff", "--no-commit", "dev"], repo)
    assert merge.returncode != 0, "expected a real tail-append conflict"

    # Resolve with `git checkout --theirs`, wholesale -- the exact incident.
    theirs = _git(["checkout", "--theirs", "--", LEDGER_NAME], repo)
    assert theirs.returncode == 0, theirs.stderr
    _git(["add", LEDGER_NAME], repo)
    commit = _git(["commit", "-q", "--no-edit"], repo)
    assert commit.returncode == 0, commit.stderr

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems != [], (
        "a wholesale --theirs resolution silently dropped `feature`'s own "
        "entry and the gate did not notice -- this is the tan-cli#902 "
        "incident recurring"
    )
    assert "tan-cli#902" in verdict.problems[0] or "did not survive" in verdict.problems[0]


# --- (2) tan-cli#878's shape: an insertion above the existing tail --------


def test_an_insertion_above_the_existing_tail_is_caught(tmp_path):
    repo = _seed_repo_with_dev_and_feature(tmp_path)

    lines = _SEED.splitlines()
    # Insert BEFORE the pre-existing tail entry instead of after it -- the
    # exact `@@ -210,6 +210,8 @@` shape from the real incident.
    lines.insert(len(lines) - 1, "- 2026-08-10 -- misplaced insertion")
    _write_ledger(repo, "\n".join(lines) + "\n")
    _commit(repo, "feature: misplaced insertion")

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems != [], (
        "an entry landed above the file's existing tail instead of after it, "
        "and the gate did not notice -- this is the tan-cli#878 incident "
        "recurring"
    )


# --- (3) the legitimate case: an interleaved-by-date "keep both" merge ----


def test_a_keep_both_sides_interleaved_merge_passes(tmp_path):
    """The formulation's whole point, verified rather than assumed: a merge
    that brings in another branch's entries interleaved by date -- not
    stacked as two separate blocks -- is still an append at each side's
    tail, and must pass."""
    repo = _seed_repo_with_dev_and_feature(tmp_path)

    # `feature` adds two entries of its own, in order.
    _write_ledger(
        repo,
        _SEED
        + "- 2026-08-09 -- feature entry one\n"
        + "- 2026-08-12 -- feature entry two\n",
    )
    _commit(repo, "feature: two entries")

    # `dev` adds two entries of its own, in order, independently.
    _git(["checkout", "-q", "dev"], repo)
    _write_ledger(
        repo,
        _SEED + "- 2026-08-10 -- dev entry one\n- 2026-08-11 -- dev entry two\n",
    )
    _commit(repo, "dev: two entries")

    _git(["checkout", "-q", "feature"], repo)
    merge = _git(["merge", "--no-ff", "--no-commit", "dev"], repo)
    assert merge.returncode != 0, "expected a real tail-append conflict"

    # Resolve by hand, keeping BOTH sides, interleaved strictly by date --
    # not feature's block then dev's block, which would be the easy case.
    resolved = (
        _SEED
        + "- 2026-08-09 -- feature entry one\n"
        + "- 2026-08-10 -- dev entry one\n"
        + "- 2026-08-11 -- dev entry two\n"
        + "- 2026-08-12 -- feature entry two\n"
    )
    _write_ledger(repo, resolved)
    _git(["add", LEDGER_NAME], repo)
    commit = _git(["commit", "-q", "--no-edit"], repo)
    assert commit.returncode == 0, commit.stderr

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems == [], (
        "a correct keep-both-sides, interleaved-by-date merge resolution was "
        "rejected:\n" + "\n".join(verdict.problems)
    )


# --- (4) the baseline: a plain append passes ------------------------------


def test_a_plain_append_passes(tmp_path):
    repo = _seed_repo_with_dev_and_feature(tmp_path)
    _write_ledger(repo, _SEED + "- 2026-08-09 -- a new, ordinary entry\n")
    _commit(repo, "feature: ordinary append")

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems == []


# --- (5) tan-cli#906 fix round, blocker 3: a commit ON TOP of the merge ---


def test_a_commit_on_top_of_an_already_committed_merge_still_gets_the_two_sided_check(
    tmp_path,
):
    """The real tan-cli#902 history did NOT fix the drop in the merge commit
    itself: the restore (`1a2c9970`) is a CHILD of the merge (`303ba5533`),
    i.e. the ordinary "merge, keep working, push" shape. Before the
    blocker-3 fix, `_merge_parents(repo)` only ever looked at HEAD's own
    parents -- one commit on top of the merge makes `HEAD^2` stop resolving,
    `_merge_parents` returns `[]`, and `find_problems` fell back to the
    single-base check against `dev`. That check's ancestor collapses to
    `dev`'s own tip (dev is an ancestor of the merge, hence of HEAD too), so
    the working tree (still the wholesale `--theirs` content) reads as an
    exact match of "the base" -- `determinable=True, problems=[]`, a
    confident false pass on the very incident this gate exists to catch."""
    repo = _seed_repo_with_dev_and_feature(tmp_path)

    _write_ledger(repo, _SEED + "- 2026-08-10 -- tan-cli#896: our reasoned entry\n")
    _commit(repo, "feature: reasoned entry")

    _git(["checkout", "-q", "dev"], repo)
    _write_ledger(repo, _SEED + "- 2026-08-11 -- dev: an unrelated entry\n")
    _commit(repo, "dev: unrelated entry")

    _git(["checkout", "-q", "feature"], repo)
    merge = _git(["merge", "--no-ff", "--no-commit", "dev"], repo)
    assert merge.returncode != 0, "expected a real tail-append conflict"

    theirs = _git(["checkout", "--theirs", "--", LEDGER_NAME], repo)
    assert theirs.returncode == 0, theirs.stderr
    _git(["add", LEDGER_NAME], repo)
    commit = _git(["commit", "-q", "--no-edit"], repo)
    assert commit.returncode == 0, commit.stderr

    # The one line this test adds over the #902 reconstruction above: keep
    # working AFTER the merge, without touching the ledger, exactly like the
    # real history's `1a2c9970` sits one commit downstream of `303ba5533`.
    on_top = _git(
        ["commit", "-q", "--allow-empty", "-m", "feature: unrelated follow-up"],
        repo,
    )
    assert on_top.returncode == 0, on_top.stderr

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems != [], (
        "a commit sitting on top of an already-committed --theirs merge "
        "resolution hid the dropped entry from the gate -- HEAD is not the "
        "merge itself, so the two-sided check must still find that merge "
        "further back in HEAD's history, not silently fall back to the "
        "single-base form"
    )


# ---------------------------------------------------------------------------
# degradation -- no PR, no determinable base
# ---------------------------------------------------------------------------


def test_running_on_dev_itself_is_a_real_pass_not_a_vacuous_one():
    """Checked out exactly on `dev` (HEAD == the resolved base): the
    comparison still RUNS -- merge-base(HEAD, dev) is HEAD itself, so the
    ancestor is the file's own current content and the check is a genuine
    (if trivial) identity match, not a skip. An uncommitted, ledger-breaking
    edit sitting in the working tree at that point must still be caught."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = _init_repo(tmp_path)
        _write_ledger(repo, _SEED)
        _commit(repo, "seed")
        # HEAD is `dev`, no divergence at all.
        verdict = core.find_problems(repo, LEDGER_NAME)
        assert verdict.determinable, verdict.skip_reason
        assert verdict.problems == []

        # Now break it in the working tree without committing.
        lines = _SEED.splitlines()
        lines.insert(0, "- 2026-07-01 -- inserted before everything")
        _write_ledger(repo, "\n".join(lines) + "\n")
        verdict = core.find_problems(repo, LEDGER_NAME)
        assert verdict.determinable, verdict.skip_reason
        assert verdict.problems != [], (
            "an uncommitted edit that rewrites the file's own head was not "
            "caught while sitting on dev itself -- that would be exactly "
            "the vacuous pass this gate exists to avoid"
        )


def test_a_detached_checkout_with_no_dev_ref_skips_loudly_rather_than_passing(
    tmp_path,
):
    """No `dev`, no `origin/dev`, no upstream, no merge in progress: the base
    genuinely cannot be determined. `determinable` must be `False` with a
    reason naming what was tried -- never a silent, vacuous pass, which is
    the worse failure per tan-cli#906 (a gate that cannot fail is not a
    gate)."""
    repo = tmp_path / "lonely"
    repo.mkdir()
    _git(["init", "-q", "-b", "solo"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    _write_ledger(repo, _SEED)
    _commit(repo, "seed")

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable is False
    assert verdict.problems == []
    assert verdict.skip_reason
    assert "dev" in verdict.skip_reason


def test_at_u_self_tracking_after_push_does_not_produce_a_false_pass(tmp_path):
    """tan-cli#906 fix round, blocker 2: for a branch pushed with `git push
    -u` and no `dev`/`origin/dev` ref present at all, `@{u}` is that
    branch's OWN remote-tracking ref -- `merge-base(HEAD, @{u})` collapses to
    HEAD itself, so the ledger's content is compared against its own current
    content. Reconstructed with a REAL `git remote` (a local bare repo, not a
    hand-typed stand-in for one -- `_init_repo`'s other hermetic tests never
    add one, which is why this hole shipped uncaught), carrying a genuine
    commit shaped exactly like tan-cli#878 (an entry inserted above the
    existing tail). The base-ref candidate order tries
    `origin/$GITHUB_BASE_REF`, `origin/dev`, and `dev` before `@{u}`, and
    none of those exist here, so this isolates `@{u}` alone."""
    remote = tmp_path / "remote.git"
    bare_init = _git(["init", "-q", "--bare", str(remote)], tmp_path)
    assert bare_init.returncode == 0, bare_init.stderr

    repo = tmp_path / "pushed"
    repo.mkdir()
    _git(["init", "-q", "-b", "feature"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)

    _write_ledger(repo, _SEED)
    _commit(repo, "seed")

    lines = _SEED.splitlines()
    lines.insert(len(lines) - 1, "- 2026-08-10 -- misplaced insertion")
    _write_ledger(repo, "\n".join(lines) + "\n")
    _commit(repo, "feature: misplaced insertion")

    push = _git(["push", "-q", "-u", "origin", "feature"], repo)
    assert push.returncode == 0, push.stderr

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert not (verdict.determinable and verdict.problems == []), (
        "@{u} -- this branch's own remote-tracking ref, right after `git "
        "push -u` -- let the gate compare the ledger to its own current "
        "content and report a confident PASS on a real tan-cli#878-shaped "
        "violation, with no dev ref anywhere that could have caught it "
        "honestly"
    )
    # The correct outcome is a loud, honest skip -- not a fabricated
    # problem report either.
    assert verdict.determinable is False
    assert verdict.problems == []


def test_a_committed_merge_with_no_conflict_still_gets_the_two_sided_check(tmp_path):
    """A merge that auto-resolves cleanly (no conflict at all, e.g. the two
    sides touched different files) still produces a HEAD with two parents,
    and must still be checked -- this is the ordinary, everyday shape of
    `merge dev into <feature>` when the ledger itself was not touched on
    both sides, and it must pass without any special-casing."""
    repo = _seed_repo_with_dev_and_feature(tmp_path)

    (repo / "unrelated.txt").write_text("feature work\n", encoding="utf-8")
    _commit(repo, "feature: unrelated change")

    _git(["checkout", "-q", "dev"], repo)
    _write_ledger(repo, _SEED + "- 2026-08-09 -- dev: ordinary append\n")
    _commit(repo, "dev: ordinary append")

    _git(["checkout", "-q", "feature"], repo)
    merge = _git(["merge", "--no-edit", "dev"], repo)
    assert merge.returncode == 0, merge.stderr  # no conflict expected

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems == []


def test_a_brand_new_ledger_file_is_trivially_append_only(tmp_path):
    """The file did not exist at the merge-base at all -- there is nothing
    to preserve, so the whole thing is legitimately new."""
    repo = _init_repo(tmp_path)
    (repo / "placeholder.txt").write_text("x\n", encoding="utf-8")
    _commit(repo, "seed, no ledger yet")
    _git(["checkout", "-q", "-b", "feature"], repo)

    _write_ledger(repo, "- 2026-08-01 -- the very first entry\n")
    _commit(repo, "feature: introduce the ledger")

    verdict = core.find_problems(repo, LEDGER_NAME)
    assert verdict.determinable, verdict.skip_reason
    assert verdict.problems == []
