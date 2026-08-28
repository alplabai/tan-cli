# SPDX-License-Identifier: Apache-2.0
"""`MODULE_SIZE_BUDGET_LOG.md` declares itself append-only but nothing
enforced that (tan-cli#906) until this file.

## The incident

On tan-cli#902 (`fix/896-hand-port-hash-drift`), resolving a merge conflict
on this ledger with `git checkout --theirs` took `dev`'s side of the file
wholesale, silently discarding the branch's own reasoned entry. Every other
local gate stayed green -- `test_module_size_budget.py` only parses the
sibling `.json`, never this `.md` -- and the deletion was found by reading
the diff by hand.

## Why "compare to the merge-base" is not quite the right check

A tempting first design is: find `git merge-base` of HEAD's two parents and
require its content to be a prefix of HEAD's. That is the wrong level -- the
merge-base predates BOTH branches' own new entries, so it cannot see that one
side's growth went missing; only the parent tips themselves carry that. The
check below therefore compares HEAD's own content against EACH of HEAD's own
git parents directly (`HEAD^@`), not their common ancestor.

## Why the check is an ordered SUBSEQUENCE, not a literal prefix

The obvious per-parent rule -- "the parent's lines are literally the first N
lines of the current file, unchanged" -- is correct for a real 3-way merge
commit resolved by `merge=union` (`.gitattributes`, tan-cli#939): a genuine
two-branch append-append conflict resolves as "ours' new lines, then theirs'
new lines" (reproduced below in
`test_a_legitimate_union_merge_of_two_divergent_appends_passes_clean`), so
checking one parent's tail for strict contiguity against the OTHER parent's
own new lines would red on exactly the case `merge=union` exists to keep
clean.

It turns out to ALSO be necessary for ordinary, single-parent commits on this
repo's real `dev` history, not just real merge commits -- measured directly
against `dev`'s own last commit at the time of writing (`ce854cc6`, a single
squash-merge parent, no merge commit involved at all): its immediate parent's
ledger content is not a literal prefix of its own (a later PR's branch had
regenerated its own ledger entry against an earlier snapshot of `dev`, so by
the time it squash-merged, its one new line landed ahead of two lines another,
already-merged PR had appended in between); it IS an ordered subsequence.
Requiring a literal prefix everywhere would have reproduced the false
positive this file exists to avoid on real, ordinary history -- so every
parent, whether HEAD has one or several, is checked the same way: its own
lines must still all be present, in order, as a subsequence of the current
file, not necessarily contiguous. An edit or deletion inside a parent's
history still breaks this (the line simply will not appear at all); only
*interleaving* with content added elsewhere is permitted to move.

## Two shapes this deliberately does NOT try to catch

`.gitattributes` documents two ways `merge=union` itself gets a conflict
wrong: two branches editing the SAME existing entry land both variants with
no conflict markers (a duplicate, not a loss), and a delete racing an
adjacent append silently reverts the deletion. Both are pre-existing,
documented limitations of the union driver, not a gap in this gate -- fighting
them here would mean rejecting the union driver's own legitimate output, i.e.
regressing the merge-clean case this file also has to prove. tan-cli#906
scopes this gate to "an entry edited or removed"; tan-cli#907 owns making
either of those two shapes safer at the git-mechanics layer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.gates import _module_size_budget_core as core

REPO = Path(__file__).resolve().parents[3]
LOG_REL = core.LOG_PATH.relative_to(REPO).as_posix()


def _git_ok(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _head_parents(cwd: Path) -> list[str]:
    """SHAs of the checked-out HEAD's own parents -- `[]` for a root commit,
    one entry for an ordinary commit, two (or more, for an octopus merge)
    for a merge commit. `HEAD^@` is git's own "all parents of HEAD" syntax,
    so this is exactly what committing `cwd`'s current state would record."""
    result = _git_ok("rev-parse", "HEAD^@", cwd=cwd)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _content_at(cwd: Path, rev: str, rel_path: str) -> list[str] | None:
    """Lines of `rel_path` as committed at `rev`, or `None` if that path
    does not exist there (e.g. a parent that predates the ledger)."""
    result = _git_ok("show", f"{rev}:{rel_path}", cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def _is_ordered_subsequence(base: list[str], target: list[str]) -> bool:
    """Every line of `base` appears in `target`, in the same relative order,
    not necessarily contiguously. This still catches an edited or deleted
    line -- it simply will not appear at all -- while tolerating OTHER
    content being interleaved in between, which both a legitimate
    `merge=union` resolution and, measured, this repo's ordinary squash-merge
    history both produce. See the module docstring for why a stricter
    literal-prefix check is not used here."""
    it = iter(target)
    for line in base:
        for candidate in it:
            if candidate == line:
                break
        else:
            return False
    return True


def ledger_violations(cwd: Path, rel_path: str, current_lines: list[str]) -> list[str]:
    """The check itself, factored out so both the real gate below and the
    hermetic scenario tests exercise the identical logic. Returns the SHAs
    of any of HEAD's own parents whose content was not preserved."""
    violations: list[str] = []
    for parent in _head_parents(cwd):
        parent_lines = _content_at(cwd, parent, rel_path)
        if parent_lines is None:
            continue
        if not _is_ordered_subsequence(parent_lines, current_lines):
            violations.append(parent)
    return violations


def test_the_repo_is_a_git_checkout():
    """Guard against every check below silently no-op'ing because this isn't
    a git checkout (a source tarball, a stripped CI cache) -- that must be a
    loud failure, not a vacuous pass, or the gate can never fail."""
    result = _git_ok("rev-parse", "--is-inside-work-tree", cwd=REPO)
    assert result.returncode == 0 and result.stdout.strip() == "true", (
        "this gate needs a git checkout to compare the ledger against its "
        "own history; `git rev-parse --is-inside-work-tree` failed in "
        f"{REPO}"
    )


def test_the_ledger_only_ever_appends_relative_to_head():
    """The real enforcement. See the module docstring for the full
    reasoning; this is the two-line version: compare the working copy of
    MODULE_SIZE_BUDGET_LOG.md against each of HEAD's own git parents, and
    fail if any parent's own lines did not all survive, in order."""
    parents = _head_parents(REPO)
    if not parents:
        pytest.skip("HEAD has no parents (repo root commit) -- nothing to compare against")

    current = core.LOG_PATH.read_text(encoding="utf-8").splitlines()
    violations = ledger_violations(REPO, LOG_REL, current)

    assert not violations, (
        f"{LOG_REL} lost or reordered content relative to "
        f"{'its parent commit' if len(parents) == 1 else 'a merge parent'} "
        f"{violations} -- an existing ledger entry was edited or deleted "
        "(tan-cli#906, the tan-cli#902 shape). The ledger is append-only: "
        "every prior line must survive unchanged, with new lines added only "
        "at the tail. If this fired on a real merge, do not hand-edit the "
        "result -- restore the missing entry from the parent named above."
    )


# ---------------------------------------------------------------------------
# Hermetic proof, both directions. Neither test touches this repo's own
# history; each builds a throwaway git repo under `tmp_path` so the scenario
# is reproduced exactly rather than hoped for from a real commit that may or
# may not still exist by the time this runs.


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _write(path: Path, name: str, lines: list[str]) -> None:
    (path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _commit(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


def test_a_direct_edit_of_an_existing_entry_with_no_merge_involved_is_caught(tmp_path):
    """The plainest violation: a single-parent commit that rewrites an
    existing line (here, alongside a legitimate append -- the append alone
    must not be enough to mask the edit)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry", "- second entry"])
    _commit(repo, "base")

    _write(repo, "LOG.md", ["- base entry (reworded after the fact)", "- second entry", "- third entry"])
    _commit(repo, "edits an existing line while also appending")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, "an existing line was rewritten in place and must be flagged"


def test_a_checkout_theirs_resolution_that_drops_a_branchs_own_entry_is_caught(tmp_path):
    """Reproduces the tan-cli#902 shape exactly: `feature` appends its own
    entry, `main` (standing in for `dev`) appends a different one, and the
    conflict is resolved with `git checkout --theirs` -- `main`'s content
    wins verbatim, discarding `feature`'s own entry. `feature`'s pre-merge
    commit must show up as a violated parent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    _write(repo, "LOG.md", ["- base entry", "- feature's own reasoned entry"])
    _commit(repo, "feature appends")

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")

    subprocess.run(["git", "checkout", "-q", "feature"], cwd=repo, check=True)
    merge = _git_ok("merge", "--no-edit", "main", cwd=repo)
    assert merge.returncode != 0, "expected a real conflict with no merge=union driver configured"

    subprocess.run(["git", "checkout", "--theirs", "LOG.md"], cwd=repo, check=True)
    subprocess.run(["git", "add", "LOG.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=repo, check=True)

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- feature's own reasoned entry" not in current, "the resolution should have dropped it"

    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, (
        "the checkout --theirs resolution silently dropped feature's own "
        "entry, and the check did not flag it -- this is the exact "
        "tan-cli#902 incident, and this gate must catch it"
    )


def test_a_legitimate_union_merge_of_two_divergent_appends_passes_clean(tmp_path):
    """The half that will actually bite in practice. Configures the SAME
    `merge=union` driver `.gitattributes` uses for the real ledger, has both
    branches append a genuinely different entry to the same tail, and merges
    -- git resolves this with no conflict markers at all, ours-then-theirs.
    The check must see zero violations."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, ".gitattributes", ["LOG.md merge=union"])
    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base + union attribute")

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    _write(repo, "LOG.md", ["- base entry", "- feature's own entry"])
    _commit(repo, "feature appends")

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")

    subprocess.run(["git", "checkout", "-q", "feature"], cwd=repo, check=True)
    merge = _git_ok("merge", "--no-edit", "main", cwd=repo)
    assert merge.returncode == 0, (
        "expected merge=union to auto-resolve this cleanly, no manual "
        f"intervention -- stderr: {merge.stderr}"
    )

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- feature's own entry" in current
    assert "- dev's own entry" in current
    assert "- base entry" in current

    violations = ledger_violations(repo, "LOG.md", current)
    assert violations == [], (
        "a legitimate union merge of two independent appends must pass "
        f"clean, but was flagged: {violations}"
    )
