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
check below therefore compares content against EACH relevant commit's own
git parents directly (`<rev>^@`), not their common ancestor.

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
against `dev`'s own history (`ce854cc6`, a single squash-merge parent, no
merge commit involved at all): its immediate parent's ledger content is not a
literal prefix of its own (a later PR's branch had regenerated its own ledger
entry against an earlier snapshot of `dev`, so by the time it squash-merged,
its one new line landed ahead of two lines another, already-merged PR had
appended in between); it IS an ordered subsequence. Requiring a literal
prefix everywhere would have reproduced the false positive this file exists
to avoid on real, ordinary history -- so every parent, of every commit this
gate looks at, is checked the same way: its own lines must still all be
present, in order, as a subsequence of the child's content, not necessarily
contiguous. An edit or deletion inside a parent's history still breaks this
(the line simply will not appear at all); only *interleaving* with content
added elsewhere is permitted to move.

## Blocker 1 (tan-cli#970 review) -- a shallow checkout must RED, not skip

Every CI job that runs this suite used the default `actions/checkout`
`fetch-depth: 1`. On a depth-1 clone the shallow graft makes the checked-out
commit look parentless -- `git rev-parse HEAD^@` exits 0 with EMPTY stdout,
not an error -- so the original version of the real test below read that as
"HEAD has no parents (repo root commit)" and `pytest.skip`ped. That message
is only true on an actual repository ROOT commit; on every real CI run it
was actually describing a shallow graft, and a skip is indistinguishable
from a pass in a green pytest summary, so this gate had ZERO effective CI
enforcement from the day it was written. `test_the_repo_is_a_git_checkout`
below was written to guard against exactly this class of vacuity (a
tarball, a stripped cache) and did not, because a depth-1 clone still
answers `--is-inside-work-tree` with `true` -- it is a real checkout, just
not a deep enough one. `test_the_checkout_has_full_history` below is the
fix: `git rev-parse --is-shallow-repository` is checked explicitly and
`assert`ed false, the same hard-failure shape `--is-inside-work-tree`
already got, never `pytest.skip`. The other half of the fix is outside this
file, in `.github/workflows/ci.yml`'s `python` job and `parity.yml`'s
`seam1-plan-shape` job, both of which now clone with `fetch-depth: 0`.

## Blocker 2 (tan-cli#970 review) -- HEAD's own parents are too narrow a window

Comparing only against HEAD's own git parents gives a one-commit detection
window that misses the shape tan-cli#906 was filed about, even with blocker
1 fixed. Two ways it goes invisible, both measured against hermetic repos
reproducing tan-cli#902 (see `test_a_follow_up_commit_does_not_hide_an_
earlier_dropped_entry` and `test_the_github_pull_request_merge_ref_shape_is_
still_caught` below):

  B) One ordinary commit lands on top of the damaging merge, touching
     anything else. HEAD's own parent is now that follow-up commit, whose
     ledger content already reflects the loss -- the check never looks far
     enough back to see the merge where the loss actually happened.
  B) is permanent, not a one-run miss: from the moment the follow-up commit
     merges, no future CI run -- on that PR, on `dev`, ever -- compares
     against the commit where the entry was actually dropped again.

  C) `pull_request` CI does not check out the PR branch tip at all; it
     checks out GitHub's own synthetic `refs/pull/N/merge`, i.e.
     `merge(base_tip, head_tip)`. THAT commit's two parents are base_tip and
     head_tip themselves, and content lost somewhere INSIDE head_tip's own
     line of commits is trivially still "a subsequence of the merge result"
     when compared only at the top -- both parents pass individually even
     though one line's own history silently dropped a line.

The issue asked for enforcement "against the PR's base ref". The fix is not
"look at more of HEAD's own parents" -- it is comparing every commit the
current run's branch introduced since diverging from that base ref, each
against ITS OWN git parents (`git rev-list <base>..HEAD`, oldest first),
using the exact same per-commit ordered-subsequence rule as before. That
range includes the damaging commit itself in both B and C, so it is caught
regardless of how many ordinary commits pile on afterward, and regardless of
which synthetic shape CI happens to check out.

`_resolve_base_ref` below is how "the PR's base ref" is actually found in
each context this gate can run in:

  - `pull_request`: `GITHUB_BASE_REF`, which GitHub Actions sets
    automatically for this event only, resolved to `origin/<value>`.
  - `merge_group`: GitHub Actions does NOT populate `GITHUB_BASE_REF` for
    this event (only `pull_request`/`pull_request_target`), so
    `ci.yml`/`parity.yml` export `TAN_MERGE_GROUP_BASE_REF` from
    `github.event.merge_group.base_ref` into the step that runs this suite,
    resolved to `origin/<value>` the same way.
  - Anything else (a direct `push` to `main`, a `workflow_call` release run,
    `repository_dispatch`, or a plain local `pytest` run with no CI env at
    all): there is no separate PR/merge-queue base to speak of -- these fall
    back to the original, narrower "HEAD's own parents" check, which is
    still correct for an ordinary, non-PR commit and is the best available
    answer with no base ref in play.

A `pull_request` or `merge_group` run ALWAYS has a base ref -- if
`_resolve_base_ref` cannot resolve one in those two contexts (the env var is
unset, or the resolved `origin/<ref>` is not present in this checkout), that
is `BaseRefUnresolved`, and the real test below turns it into a hard
`pytest.fail`, never a fallback and never a skip -- a silent fallback here
would recreate blocker 1's exact vacuity one layer up.

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

import os
import subprocess
from pathlib import Path

import pytest

from tests.gates import _module_size_budget_core as core

REPO = Path(__file__).resolve().parents[3]
LOG_REL = core.LOG_PATH.relative_to(REPO).as_posix()


def _git_ok(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def _parents_of(cwd: Path, rev: str) -> list[str]:
    """SHAs of `rev`'s own git parents -- `[]` for a root commit, one entry
    for an ordinary commit, two (or more, for an octopus merge) for a merge
    commit. `rev` may be a real sha or the literal `"HEAD"`. `<rev>^@` is
    git's own "all parents of <rev>" syntax."""
    result = _git_ok("rev-parse", f"{rev}^@", cwd=cwd)
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


def ledger_violations(
    cwd: Path,
    rel_path: str,
    current_lines: list[str],
    *,
    commits: list[str] | None = None,
) -> list[tuple[str, str]]:
    """The check itself, factored out so both the real gate below and the
    hermetic scenario tests exercise the identical logic. Returns
    `(commit, parent)` pairs for every commit whose own git parent's lines
    were not preserved, in order, as a subsequence of that commit's content.

    `commits` defaults to `["HEAD"]` -- the original, single-commit check
    every hermetic test below still exercises unmodified. Blocker 2's
    range-based real gate passes its own explicit list: every commit the
    current run introduced since its PR/merge-queue base, oldest first, with
    the LAST entry swapped for the literal string `"HEAD"` so its content is
    read from `current_lines` (the working tree) rather than `git show` --
    matching the original code path's ability to also catch an uncommitted
    edit before it lands, not just a committed one.
    """
    violations: list[tuple[str, str]] = []
    for commit in commits if commits is not None else ["HEAD"]:
        commit_lines = current_lines if commit == "HEAD" else _content_at(cwd, commit, rel_path)
        if commit_lines is None:
            continue
        for parent in _parents_of(cwd, commit):
            parent_lines = _content_at(cwd, parent, rel_path)
            if parent_lines is None:
                continue
            if not _is_ordered_subsequence(parent_lines, commit_lines):
                violations.append((commit, parent))
    return violations


def _commits_since_base(cwd: Path, base_ref: str) -> list[str]:
    """SHAs of every commit reachable from HEAD but not from `base_ref`,
    oldest first -- i.e. everything the current run's branch introduced
    since diverging from its base. On a real `pull_request` run this
    already includes GitHub's own synthetic `refs/pull/N/merge` commit
    (HEAD itself) as the last entry: that commit's `base_ref` parent does
    not make IT reachable from `base_ref`."""
    result = _git_ok("rev-list", "--reverse", f"{base_ref}..HEAD", cwd=cwd)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


class BaseRefUnresolved(RuntimeError):
    """Raised when a run whose event type ALWAYS carries a PR/merge-queue
    base ref (`pull_request`, `merge_group`) does not yield one this
    checkout can resolve. The caller must turn this into a hard failure, not
    a fallback or a skip -- see the module docstring, tan-cli#970 review
    blocker 2."""


def _resolve_base_ref(cwd: Path) -> str | None:
    """The ref to diff HEAD against for the range-based check, or `None`
    when this run has no PR/merge-queue base to enforce against at all --
    see the module docstring's "Blocker 2" section for the full reasoning
    and the exact env var each CI context relies on."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")

    if event == "pull_request":
        base = os.environ.get("GITHUB_BASE_REF", "").strip()
        if not base:
            raise BaseRefUnresolved(
                "GITHUB_EVENT_NAME=pull_request but GITHUB_BASE_REF is unset -- "
                "GitHub Actions sets this automatically for every pull_request "
                "run, so its absence means something upstream of this test is "
                "not what it claims to be."
            )
        ref = f"origin/{base}"
    elif event == "merge_group":
        base = os.environ.get("TAN_MERGE_GROUP_BASE_REF", "").strip()
        if not base:
            raise BaseRefUnresolved(
                "GITHUB_EVENT_NAME=merge_group but TAN_MERGE_GROUP_BASE_REF is "
                "unset -- GitHub Actions does not populate GITHUB_BASE_REF for "
                "merge_group runs (only pull_request/pull_request_target), so "
                "the workflow step that runs this suite must export "
                "TAN_MERGE_GROUP_BASE_REF: ${{ github.event.merge_group.base_ref }} "
                "for a base ref to be resolvable here at all."
            )
        ref = f"origin/{base.removeprefix('refs/heads/')}"
    else:
        return None

    if _git_ok("rev-parse", "--verify", "--quiet", ref, cwd=cwd).returncode != 0:
        raise BaseRefUnresolved(
            f"resolved base ref {ref!r} for a {event!r} run, but `git "
            f"rev-parse` cannot see it in {cwd} -- the checkout must fetch "
            "every branch for this to work (this repo's jobs use "
            "`fetch-depth: 0`, which actions/checkout documents as fetching "
            "all history for all branches; a checkout missing that ref "
            "regressed away from that, or fetched a different remote name "
            "than 'origin')."
        )
    return ref


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


def _assert_not_shallow(cwd: Path) -> None:
    """Blocker 1 (tan-cli#970 review): a shallow checkout must RED, never
    `pytest.skip` -- a skip is indistinguishable from a pass in a green
    summary, and a depth-1 clone is exactly what made this gate skip in
    every CI job that ran it. See the module docstring's "Blocker 1"
    section."""
    result = _git_ok("rev-parse", "--is-shallow-repository", cwd=cwd)
    assert result.returncode == 0 and result.stdout.strip() == "false", (
        f"{cwd} is a shallow git checkout (`git rev-parse "
        "--is-shallow-repository` said so), so this gate cannot see far "
        "enough back to compare the ledger against its own history. Every "
        "CI job that runs `tests/gates` must clone with `fetch-depth: 0` "
        "(see .github/workflows/ci.yml's `python` job and parity.yml's "
        "`seam1-plan-shape` job) -- this is a hard failure, not "
        "`pytest.skip`, because a skip here is indistinguishable from a "
        "pass (tan-cli#970 review, blocker 1)."
    )


def test_the_checkout_has_full_history():
    """See `_assert_not_shallow`'s docstring. A dedicated test, not folded
    silently into the real enforcement test below, so a regression here
    reads as its own clearly named failure rather than as noise inside a
    different assertion's message."""
    _assert_not_shallow(REPO)


def test_the_ledger_only_ever_appends_since_the_prs_base():
    """The real enforcement. See the module docstring for the full
    reasoning. Two-line version: for every commit the current run's branch
    introduced since its PR/merge-queue base (or, outside a PR/merge-queue
    run, just HEAD's own parents), that commit's own git parent's lines must
    all still be present, in order, in the commit's content."""
    _assert_not_shallow(REPO)

    current = core.LOG_PATH.read_text(encoding="utf-8").splitlines()

    try:
        base_ref = _resolve_base_ref(REPO)
    except BaseRefUnresolved as exc:
        pytest.fail(
            f"could not determine this run's PR/merge-queue base ref: {exc} "
            "-- this must fail loudly rather than silently fall back, "
            "because a silent fallback here would recreate the tan-cli#970 "
            "review's blocker 1 vacuity one layer up."
        )

    if base_ref is not None:
        commits = _commits_since_base(REPO, base_ref)
        if not commits:
            return  # this run's branch adds nothing new relative to its base
        commits[-1] = "HEAD"
        violations = ledger_violations(REPO, LOG_REL, current, commits=commits)
        window_desc = f"relative to its base ref ({base_ref})"
    else:
        parents = _parents_of(REPO, "HEAD")
        if not parents:
            pytest.skip("HEAD has no parents (repo root commit) -- nothing to compare against")
        violations = ledger_violations(REPO, LOG_REL, current)
        window_desc = "relative to its parent commit (no PR/merge-queue base ref applies to this run)"

    assert not violations, (
        f"{LOG_REL} lost or reordered content {window_desc} -- an existing "
        f"ledger entry was edited or deleted at (commit, parent) pair(s) "
        f"{violations} (tan-cli#906, the tan-cli#902 shape). The ledger is "
        "append-only: every prior line must survive unchanged, with new "
        "lines added only at the tail. If this fired on a real merge, do "
        "not hand-edit the result -- restore the missing entry from the "
        "parent named above."
    )


# ---------------------------------------------------------------------------
# Hermetic proof, both directions. Neither test touches this repo's own
# history; each builds a throwaway git repo under `tmp_path` so the scenario
# is reproduced exactly rather than hoped for from a real commit that may or
# may not still exist by the time this runs.


#: `-c` overrides, not repository config WRITES: a host with
#: `commit.gpgsign=true` globally (a real setup on the bench box) would
#: otherwise make every commit-creating call below prompt or fail for a
#: reason that has nothing to do with what is under test. Applied to every
#: git call in this section, not just `commit` -- `merge` creates a commit
#: too, and needs the identical override. The identical guarded site is one
#: directory over, `tests/gates/test_sdk_pin_disagreement_warning.py`'s
#: `_git` helper, whose docstring names the reason verbatim (tan-cli#970
#: review, Major 3).
_HERMETIC_GIT_CONFIG = (
    "-c",
    "user.email=tests@example.invalid",
    "-c",
    "user.name=tan-cli tests",
    "-c",
    "commit.gpgsign=false",
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """git for the hermetic scenario repos below, identity + signing
    isolated on every call. Refuses loudly (`check=True` by default) rather
    than leaving a setup step's failure to surface later as a confusing
    assertion on the wrong line."""
    proc = subprocess.run(
        ["git", *_HERMETIC_GIT_CONFIG, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check:
        assert proc.returncode == 0, f"git {args} failed: {proc.stdout!r} {proc.stderr!r}"
    return proc


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")


def _write(path: Path, name: str, lines: list[str]) -> None:
    (path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _commit(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)


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


def test_a_reorder_of_existing_entries_with_nothing_deleted_is_caught(tmp_path):
    """Minor 5 (tan-cli#970 review): the ordering half of the check had no
    test of its own. Swaps two already-committed entries -- both survive,
    just in a different relative order, nothing deleted -- proving
    `_is_ordered_subsequence` is doing real work here and this isn't
    secretly `set(base) <= set(target)`, which would pass this exact case."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- first entry", "- second entry", "- third entry"])
    _commit(repo, "base")

    _write(repo, "LOG.md", ["- second entry", "- first entry", "- third entry"])
    _commit(repo, "reorders the first two entries, deletes nothing")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, (
        "two existing entries were reordered and must be flagged, even "
        "though neither was deleted and both still individually appear"
    )


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

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own reasoned entry"])
    _commit(repo, "feature appends")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode != 0, "expected a real conflict with no merge=union driver configured"

    _git(repo, "checkout", "--theirs", "LOG.md")
    _git(repo, "add", "LOG.md")
    _git(repo, "commit", "-q", "--no-edit")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- feature's own reasoned entry" not in current, "the resolution should have dropped it"

    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, (
        "the checkout --theirs resolution silently dropped feature's own "
        "entry, and the check did not flag it -- this is the exact "
        "tan-cli#902 incident, and this gate must catch it"
    )


def test_a_follow_up_commit_does_not_hide_an_earlier_dropped_entry(tmp_path):
    """Blocker 2 (tan-cli#970 review), shape B: the damaging `checkout
    --theirs` merge lands, then one ORDINARY commit follows with the ledger
    left untouched. Proves both halves: the pre-fix, HEAD-only check
    (`ledger_violations` with no `commits=`) sees nothing once that
    follow-up commit is HEAD -- the follow-up's own single parent IS the
    damaging merge, and by then the loss is already baked into both sides --
    while the range-based check (`commits=`, blocker 2's fix) still catches
    it because the damaging merge commit itself remains inside
    `base..HEAD`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own reasoned entry"])
    _commit(repo, "feature appends")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode != 0, "expected a real conflict with no merge=union driver configured"
    _git(repo, "checkout", "--theirs", "LOG.md")
    _git(repo, "add", "LOG.md")
    _git(repo, "commit", "-q", "--no-edit")

    # One ordinary follow-up commit that never touches the ledger.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _commit(repo, "ordinary follow-up, ledger untouched")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()

    pre_fix = ledger_violations(repo, "LOG.md", current)
    assert pre_fix == [], (
        "sanity: the HEAD-only check must NOT see this one commit later -- "
        "that gap is exactly what blocker 2 exists to close"
    )

    commits = _commits_since_base(repo, base_sha)
    assert commits, "the follow-up commit must be reachable from HEAD but not from base"
    commits[-1] = "HEAD"
    violations = ledger_violations(repo, "LOG.md", current, commits=commits)
    assert violations, (
        "the checkout --theirs merge, now one ordinary commit back, must "
        "still be caught by the range-based check over base..HEAD"
    )


def test_the_github_pull_request_merge_ref_shape_is_still_caught(tmp_path):
    """Blocker 2 (tan-cli#970 review), shape C: on a real `pull_request` run,
    the checked-out HEAD is GitHub's OWN synthetic `merge(base_tip,
    head_tip)` commit, not the PR branch tip. Configures the same
    `merge=union` driver as the real ledger so that top-level merge unions
    cleanly -- base_tip's OWN lines and feature_tip's OWN (accumulated)
    lines are BOTH, individually, trivially still a subsequence of the
    union result, exactly the "both are subsequences of the merge result"
    false negative measured in the tan-cli#970 review. The loss is buried
    ONE LEVEL DEEPER than either of HEAD's immediate parents: feature's own
    internal history dropped the base entry between two of ITS OWN commits,
    which only the range-based check (walking every commit since base_tip,
    not just HEAD's two immediate parents) ever inspects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, ".gitattributes", ["LOG.md merge=union"])
    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base + union attribute")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own entry"])
    _commit(repo, "feature appends")

    # The damaging commit lives INSIDE feature's own branch history: drops
    # the base entry while still legitimately appending, so the append
    # alone must not mask it. This is what the range-based check has to
    # reach -- neither of HEAD's two immediate parents below is this commit.
    _write(repo, "LOG.md", ["- feature's own entry", "- feature's follow-up entry"])
    _commit(repo, "drops the base entry while also appending")
    feature_tip = _git(repo, "rev-parse", "feature").stdout.strip()

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")
    base_tip = _git(repo, "rev-parse", "main").stdout.strip()

    # GitHub's own refs/pull/N/merge shape: a synthetic 2-parent merge of
    # (base_tip, feature_tip). merge=union auto-resolves it clean, same as
    # `test_a_legitimate_union_merge_of_two_divergent_appends_passes_clean`.
    _git(repo, "checkout", "-q", "-b", "pr-merge", "main")
    merge = _git(repo, "merge", "-q", "--no-edit", "feature", check=False)
    assert merge.returncode == 0, merge.stderr

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()

    pre_fix = ledger_violations(repo, "LOG.md", current)
    assert pre_fix == [], (
        "sanity: a HEAD-only check must NOT see this -- both of the "
        "synthetic merge's own parents (base_tip, feature_tip) pass "
        "individually against the union result, which IS the blocker 2 gap "
        "this test proves the range-based check closes"
    )

    commits = _commits_since_base(repo, base_tip)
    assert feature_tip in commits, "the damaging commit inside feature's own line must be in range"
    commits[-1] = "HEAD"
    violations = ledger_violations(repo, "LOG.md", current, commits=commits)
    assert violations, (
        "the base entry was dropped inside feature's own branch history, "
        "and the range-based check over base_tip..HEAD must still catch it "
        "even though the synthetic GitHub merge commit's own two immediate "
        "parents look clean individually"
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

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own entry"])
    _commit(repo, "feature appends")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
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
