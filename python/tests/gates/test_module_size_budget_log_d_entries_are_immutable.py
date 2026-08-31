# SPDX-License-Identifier: Apache-2.0
"""`MODULE_SIZE_BUDGET_LOG.d/` (tan-cli#907) is the one-file-per-entry
replacement for the old single `MODULE_SIZE_BUDGET_LOG.md` ledger --
`test_module_size_budget_log_append_only.py` still protects that frozen
file's pre-migration history, this file protects the new directory going
forward.

## Why this needs no PR/merge-queue base ref at all

The old gate had to resolve a base ref (`GITHUB_BASE_REF` /
`TAN_MERGE_GROUP_BASE_REF`) because its unit of comparison was ONE growing
file's content, and "did a line survive" only means something relative to
some earlier snapshot of that same file -- which snapshot depends on what
"earlier" means for the run (a PR's base, a merge commit's parents, ...).
That is also where its blind spot lived: a `pull_request`/`merge_group` run
resolves its base to the PR's target branch (`dev`), so a line a branch added
and then rewrote WITHIN ITS OWN unmerged history never regresses relative to
`dev` (`dev` never had that line to begin with) and passes clean in CI, even
though the same rewrite is caught immediately by a local run comparing
against HEAD's own parent. That gap is by design for the old file (tan-cli#970
round 3's "the ledger's promise to `dev` is about `dev`'s own lines, not a
branch's private editing history") -- but it is still a real asymmetry
between what CI enforces and what a local run enforces, worth not carrying
into the replacement if it can be avoided for free.

It can: a directory of one-file-per-entry does not need a "snapshot vs.
snapshot" comparison at all. Each entry file's own promise is simpler and
does not depend on any base ref -- "once this exact path is added to the
repository, by any commit, it is never modified or removed by any later
commit reachable from here" -- so the check is just a walk of `git log
--name-status` over `MODULE_SIZE_BUDGET_LOG.d/`, entirely self-contained,
with the same answer locally and in CI, on a `pull_request` run, a
`merge_group` run, or a bare `push`. There is no base-ref resolution to get
wrong, so there is no equivalent gap: a same-branch rewrite of a file that
branch itself just added is caught the same way a same-branch rewrite of a
line already on `dev` is -- both are just "a later commit changed a path
that already existed" -- and that is deliberately a STRICTER promise than
the old file made for its own within-branch edits. A per-entry file is meant
to be finished the moment it is written; nothing about this ledger's purpose
argues for tolerating a self-correction the way free-form prose might.

## What is deliberately excluded

`MODULE_SIZE_BUDGET_LOG.d/README.md` is not an entry (`_module_size_budget_
core.LOG_DIR`'s own README, not something `regen_module_size_budget.py`
writes) and is free to be edited like any other doc file -- `_entry_path`
below only matches the `<date>-<8 hex chars>.md` shape the script actually
generates, so README.md (and anything else that does not match) is never
even considered.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.gates import _module_size_budget_core as core
from tests.gates.test_module_size_budget_log_append_only import (
    GitCommandFailed,
    _commit,
    _git,
    _git_ok,
    _init_repo,
    _write,
)

REPO = Path(__file__).resolve().parents[3]
LOG_DIR_REL = core.LOG_DIR.relative_to(REPO).as_posix()

#: Matches exactly the filenames `_append_log` (scripts/regen_module_size_
#: budget.py) generates -- `<YYYY-MM-DD>-<8 lowercase hex chars>.md`. Anything
#: else under the directory (README.md, a future non-entry file) is out of
#: scope for this gate on purpose -- see the module docstring's "What is
#: deliberately excluded" section.
_ENTRY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.md$")


def _is_entry_path(path: str, dir_rel: str) -> bool:
    if not path.startswith(f"{dir_rel}/"):
        return False
    return bool(_ENTRY_NAME.match(path.rsplit("/", 1)[-1]))


def entry_violations(cwd: Path, dir_rel: str) -> dict[str, list[str]]:
    """path -> every git status code (other than a single leading "A") ever
    recorded for it, reachable from HEAD -- empty when every entry under
    `dir_rel` was added exactly once and never touched again.

    Uses `--name-status` (not `--follow`, not `-M` rename detection): an
    entry is content-addressed by nothing but its own filename, and this
    gate's promise is about that PATH specifically, not about the flow of
    similar-looking content across paths. `git log`'s default DOES detect
    renames for `--name-status` in newer versions on some configs; forcing
    `--no-renames` keeps this deterministic across environments and reads
    a rename as exactly what it is here -- a delete of one path plus an add
    of another, i.e. two violations, not a clean move.
    """
    result = _git_ok(
        "log",
        "--reverse",
        "--no-renames",
        "--name-status",
        "--pretty=format:%x01%H",
        "--",
        dir_rel,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise GitCommandFailed(
            f"git log --name-status -- {dir_rel} failed in {cwd}: {result.stderr!r}"
        )

    statuses: dict[str, list[str]] = {}
    current_commit = None
    for raw_line in result.stdout.splitlines():
        if raw_line.startswith("\x01"):
            current_commit = raw_line[1:]
            continue
        if not raw_line.strip():
            continue
        status, _, path = raw_line.partition("\t")
        if not _is_entry_path(path, dir_rel):
            continue
        statuses.setdefault(path, []).append(f"{status} at {current_commit}")

    violations: dict[str, list[str]] = {}
    for path, seen in statuses.items():
        # Exactly one "A"-status record is the only clean history. Anything
        # else -- a second record of any kind (a re-add after delete, a
        # modify, ...), or a first record that is not "A" at all (should be
        # unreachable given git's own model, guarded anyway rather than
        # assumed) -- is a violation.
        if len(seen) != 1 or not seen[0].startswith("A "):
            violations[path] = seen
    return violations


def test_the_repo_is_a_git_checkout():
    """Same vacuity guard as the sibling append-only gate: this needs a real
    git checkout to walk history against, and a stripped/tarball checkout
    must fail loudly, not pass by having nothing to check."""
    result = _git_ok("rev-parse", "--is-inside-work-tree", cwd=REPO)
    assert result.returncode == 0 and result.stdout.strip() == "true", (
        "this gate needs a git checkout to walk MODULE_SIZE_BUDGET_LOG.d/'s "
        f"own history; `git rev-parse --is-inside-work-tree` failed in {REPO}"
    )


def test_the_checkout_has_full_history():
    """A shallow checkout truncates the history this gate walks, which could
    hide a real modify/delete that happened further back than the shallow
    graft -- same tan-cli#970 blocker-1 shape as the sibling append-only
    gate, and the same fix: hard-fail, never `pytest.skip` (a skip here is
    indistinguishable from a pass in a green summary)."""
    result = _git_ok("rev-parse", "--is-shallow-repository", cwd=REPO)
    assert result.returncode == 0 and result.stdout.strip() == "false", (
        f"{REPO} is a shallow git checkout, so this gate cannot see far "
        "enough back to know whether every MODULE_SIZE_BUDGET_LOG.d/ entry "
        "was only ever added. Every CI job that runs `tests/gates` clones "
        "with `fetch-depth: 0` (see .github/workflows/ci.yml's `python` job "
        "and parity.yml's `seam1-plan-shape` job) -- this must be a hard "
        "failure here, not a skip, for the same reason as "
        "test_module_size_budget_log_append_only.py's identical guard."
    )


def test_every_entry_under_module_size_budget_log_d_was_only_ever_added():
    """The real enforcement. See the module docstring for why this needs no
    base ref: every entry's own git history, walked in isolation, is enough."""
    violations = entry_violations(REPO, LOG_DIR_REL)
    assert not violations, (
        f"{LOG_DIR_REL} entries must only ever be ADDED, never modified or "
        f"removed once committed. Violating path(s): {violations}. If this "
        "fired, the fix is a normal follow-up commit that restores the "
        "original content of the affected path(s) -- do not rewrite history."
    )


# ---------------------------------------------------------------------------
# Hermetic proof, both directions -- reuses the sibling append-only gate's
# git plumbing rather than re-implementing it (tan-cli#907 follows the same
# "shared helper, not a second copy" principle _module_size_budget_core.py's
# own module docstring names for the measurement side).


def test_a_later_commit_modifying_an_already_added_entry_is_caught(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-aaaaaaaa.md", ["- 2026-08-30 -- first reason", "    - a.py: 10 -> 20"])
    _commit(repo, "add an entry")

    _write(entry_dir, "2026-08-30-aaaaaaaa.md", ["- 2026-08-30 -- REWRITTEN reason", "    - a.py: 10 -> 20"])
    _commit(repo, "rewrites the entry it just added")

    violations = entry_violations(repo, "LOG.d")
    assert violations, "a later commit modified an already-added entry and must be flagged"


def test_a_later_commit_deleting_an_already_added_entry_is_caught(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-bbbbbbbb.md", ["- 2026-08-30 -- a reason", "    - a.py: 10 -> 20"])
    _commit(repo, "add an entry")

    (entry_dir / "2026-08-30-bbbbbbbb.md").unlink()
    _commit(repo, "removes the entry")

    violations = entry_violations(repo, "LOG.d")
    assert violations, "a later commit deleted an already-added entry and must be flagged"


def test_an_entry_added_once_and_never_touched_again_passes_clean(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-cccccccc.md", ["- 2026-08-30 -- a reason", "    - a.py: 10 -> 20"])
    _commit(repo, "add an entry")

    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _commit(repo, "an unrelated follow-up commit")

    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, f"an untouched entry must not be flagged, but got: {violations}"


def test_readme_under_the_directory_is_not_treated_as_an_entry(tmp_path):
    """`_is_entry_path` must not match `README.md` -- it is documentation,
    not a generated entry, and is expected to be edited over time."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "README.md", ["# one file per entry"])
    _commit(repo, "add the README")

    _write(entry_dir, "README.md", ["# one file per entry", "", "edited later"])
    _commit(repo, "edit the README")

    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, f"README.md is not an entry and must never be flagged, but got: {violations}"


def test_two_branches_each_adding_a_different_entry_merge_with_zero_conflicts_and_no_driver(tmp_path):
    """The property the whole migration exists for (tan-cli#907), reproduced
    directly: unlike the old single-file ledger, this needs no `merge=union`
    (or any other custom merge driver) at all -- two branches adding two
    DIFFERENT new files under the same directory are trivially compatible
    under git's default recursive strategy. No `.gitattributes` is written in
    this repo; the merge below runs with none configured on purpose, so a
    conflict here would mean the directory shape itself still depends on a
    driver -- which is exactly the thing this design was chosen to avoid
    (a driver GitHub's own PR-mergeability computation does not apply)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-25-11111111.md", ["- 2026-08-25 -- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- feature's own reasoned entry"])
    _commit(repo, "feature adds its own entry")

    _git(repo, "checkout", "-q", "main")
    _write(entry_dir, "2026-08-26-33333333.md", ["- 2026-08-26 -- dev's own entry"])
    _commit(repo, "dev adds its own entry")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode == 0, (
        "two branches adding two different new files under the same "
        f"directory must merge with zero conflicts, no driver configured -- "
        f"stderr: {merge.stderr}"
    )

    assert (entry_dir / "2026-08-25-11111111.md").exists()
    assert (entry_dir / "2026-08-27-22222222.md").exists()
    assert (entry_dir / "2026-08-26-33333333.md").exists()

    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, f"a clean two-sided add-add merge must not be flagged, but got: {violations}"


def test_a_rename_is_caught_as_a_delete_plus_an_add(tmp_path):
    """`--no-renames` is asserted directly, not just claimed: without it, git
    could read a same-content rename as one `R100` record rather than a
    delete-of-the-old-path (which this gate must still flag, since the old
    path was, by definition, removed after being added)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-dddddddd.md", ["- 2026-08-30 -- a reason"])
    _commit(repo, "add an entry")

    _git(repo, "mv", "LOG.d/2026-08-30-dddddddd.md", "LOG.d/2026-08-30-eeeeeeee.md")
    _commit(repo, "renames the entry")

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-30-dddddddd.md" in violations, (
        f"the renamed-away path must be flagged as removed, but got: {violations}"
    )


def test_commits_outside_head_do_not_leak_in(tmp_path):
    """`git log` without `--all` walks only what HEAD can reach. A commit
    that exists in the repository but is not an ancestor of HEAD (an
    abandoned branch, a detached-HEAD experiment) must not be able to flag
    a path this checkout's own history never actually touched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-ffffffff.md", ["- 2026-08-30 -- a reason"])
    _commit(repo, "add an entry")

    _git(repo, "checkout", "-q", "-b", "abandoned")
    _write(entry_dir, "2026-08-30-ffffffff.md", ["- 2026-08-30 -- REWRITTEN on an abandoned branch"])
    _commit(repo, "rewrite on a branch HEAD (main) never merges")

    _git(repo, "checkout", "-q", "main")

    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, (
        "a rewrite on a branch that is not an ancestor of HEAD must not "
        f"leak into HEAD's own check, but got: {violations}"
    )
