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

import subprocess
from pathlib import Path

import pytest

from tests.gates import _ledger_append_only_core as core

LEDGER_NAME = "LEDGER.md"


# ---------------------------------------------------------------------------
# the live gate
# ---------------------------------------------------------------------------


def test_the_real_ledger_is_append_only():
    """The actual gate. `pytest.skip`s -- loudly, naming what is missing --
    when no base is determinable (a detached checkout, a clone with no `dev`
    fetched); a skip is visible in `-rs` output and is never mistaken for a
    pass, unlike the silent "nothing checked" this gate exists to replace."""
    verdict = core.find_problems(core.REPO, core.LEDGER_REL_PATH)
    if not verdict.determinable:
        pytest.skip(verdict.skip_reason)
    assert verdict.problems == [], "\n".join(verdict.problems)


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
