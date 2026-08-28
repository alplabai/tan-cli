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

## Why the check is an ordered SUBSEQUENCE, not a literal prefix

The obvious rule -- "the base's lines are literally the first N lines of the
current file, unchanged" -- is correct for a real 3-way merge commit
resolved by `merge=union` (`.gitattributes`, tan-cli#939): a genuine
two-branch append-append conflict resolves as "ours' new lines, then theirs'
new lines" (reproduced below in
`test_a_legitimate_union_merge_of_two_divergent_appends_passes_clean`), so
checking one side's tail for strict contiguity against the OTHER side's own
new lines would red on exactly the case `merge=union` exists to keep clean.
It turns out to ALSO be necessary for ordinary, single-parent commits on this
repo's real `dev` history -- measured directly against `dev`'s own history
(`ce854cc6`): its immediate parent's ledger content is not a literal prefix
of its own, but it IS an ordered subsequence. So every base checked below is
checked the same way: its own lines must still all be present, in order, as
a subsequence of the compared content, not necessarily contiguous. An edit
or deletion still breaks this (the line simply will not appear at all); only
*interleaving* with content added elsewhere is permitted to move.

## Blocker 1 (tan-cli#970 review, round 1) -- a shallow checkout must RED, not skip

Every CI job that runs this suite used the default `actions/checkout`
`fetch-depth: 1`. On a depth-1 clone the shallow graft makes the checked-out
commit look parentless -- `git rev-parse HEAD^@` exits 0 with EMPTY stdout,
not an error -- so an early version of the real test below read that as
"HEAD has no parents (repo root commit)" and `pytest.skip`ped, which is only
true on an actual repository ROOT commit; on every real CI run it was
describing a shallow graft, and a skip is indistinguishable from a pass in a
green pytest summary. `test_the_checkout_has_full_history` below is the fix:
`git rev-parse --is-shallow-repository` is checked explicitly and asserted
false -- a hard failure, never `pytest.skip`. The other half of the fix is
outside this file, in `.github/workflows/ci.yml`'s `python` job and
`parity.yml`'s `seam1-plan-shape` job, both of which clone with
`fetch-depth: 0`.

## Blocker 2 (tan-cli#970 review, round 1) -- HEAD's own parents are too narrow a window

Comparing only against HEAD's own git parents gives a one-commit detection
window: a follow-up commit that never touches the ledger, or GitHub's own
synthetic `refs/pull/N/merge` (`merge(base_tip, head_tip)`) checkout, both
push the actual point of loss further back than HEAD's own immediate
parent(s) reach. Round 2 answered this with a decision that walked every
commit `git rev-list <base>..HEAD` introduced, each checked against its OWN
parent -- which closed blocker 2, but see the next section for what it broke.

## The major fix (tan-cli#970 review, round 2 -> round 3) -- net state, not every intermediate commit

Round 2's per-commit walk decided a violation existed the moment ANY commit
in `base..HEAD` failed its own-parent check, and never revisited that
decision -- so a damaging commit that a LATER commit in the same range fixed
(the ledger restored byte-identical to its base) stayed flagged forever,
because the damaging commit itself never left `base..HEAD`. Measured live
against `tan-cli#971` (`origin/dev..pr/971` contains `6d7842f0d`, parent
`8db001c28`, which reworded/collapsed two of the PR's own just-added ledger
entries in a later commit): round 2's design reds that PR the moment this
gate lands, with no action by its author, and the fix it prescribes --
"restore the missing entry from the parent" -- does not clear it, because the
walk never stops re-checking `(6d7842f0d, 8db001c28)` against each other no
matter what HEAD ends up containing. A gate whose only escape is a history
rewrite is worse than the drift it prevents.

The append-only contract this file enforces is about the ledger's NET STATE
relative to its base, not about every intermediate commit: a line the base
had must still be present, in order, by the time HEAD is reached -- nothing
says it may never be ABSENT at some commit in between, only that it must be
back by the end. So the decision below (`ledger_violations`) compares
exactly two things: the content at a single anchor (the PR/merge-queue base
ref for a `pull_request`/`merge_group` run, or each of HEAD's own immediate
git parents individually for everything else, unchanged from before blocker
2 -- see `test_a_checkout_theirs_resolution_that_drops_a_branchs_own_entry_is_caught`,
which is that exact single-hop case and needed no change) and the CURRENT
content. Nothing in between is ever consulted for the decision. A line the
PR's own branch added and later dropped WITHIN ITS OWN unmerged history (not
inherited from the base) is deliberately not this gate's concern any more --
that is exactly the `tan-cli#971` shape, a PR correcting its own draft
content before it ever reaches `dev`, and the ledger's promise to `dev` is
about `dev`'s own lines, not a branch's private editing history.

The per-commit walk from round 2 is kept, but demoted: `_locate_dropping_commit`
runs ONLY after `ledger_violations` has already found a violation, purely to
name a specific (commit, parent) pair in the failure message. It never
decides whether a violation exists -- that would resurrect exactly the bug
this section describes.

Two probes prove this still catches what round 1's blocker 2 was filed
about, without round 2's false-positive:

- `test_a_base_entry_dropped_early_in_the_range_is_still_caught_even_with_an_untouched_follow_up_commit`
  -- a line the BASE (not the branch) had is dropped by an early PR commit
  and never restored; an ordinary, ledger-untouched commit lands on top.
  Still caught, because the comparison is base vs. CURRENT, and current
  still lacks it.
- `test_the_github_pull_request_merge_ref_shape_still_catches_a_genuine_loss`
  -- the same shape checked out as GitHub's own synthetic two-parent
  `refs/pull/N/merge` commit, where the loss is not restored by anything on
  either side (no `merge=union` recovery). Still caught.

And the complementary probe that motivates the whole redesign:

- `test_a_dropped_entry_restored_by_a_later_commit_passes_clean` -- the base
  line is dropped, then a later commit in the same range puts it back,
  verbatim, in order. Must PASS: the ledger is intact by the time HEAD is
  reached, which is what "append-only" actually promises.

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


class GitCommandFailed(RuntimeError):
    """A git subprocess this gate depends on failed for a reason that is
    not the legitimate "this path/rev does not exist yet" case (tan-cli#970
    round 2 review, minors on `_commits_since_base` and `_content_at`).
    Must propagate as a loud test error, never be swallowed into an empty
    result a caller could mistake for "nothing to check here" -- that is
    the same vacuity class `BaseRefUnresolved` exists to kill one layer up."""


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
    """Lines of `rel_path` as committed at `rev`, `None` if that path does
    not exist there yet (e.g. a rev that predates the ledger) -- a
    legitimate state, not an error. Anything else `git show` fails on (an
    unresolvable `rev`, a corrupt object, a permissions error) raises
    `GitCommandFailed` instead of being folded into the same `None` (tan-cli#970
    round 2 review): `ledger_violations` and its callers `continue` past a
    `None`, which is correct for "did not exist yet" and would be a silent
    vacuity for a real git failure."""
    result = _git_ok("show", f"{rev}:{rel_path}", cwd=cwd)
    if result.returncode == 0:
        return result.stdout.splitlines()
    stderr = result.stderr
    if "does not exist in" in stderr or "exists on disk, but not in" in stderr:
        return None
    raise GitCommandFailed(
        f"git show {rev}:{rel_path} failed in {cwd} for a reason other than "
        f"the path not existing yet at that rev -- stderr: {stderr!r}"
    )


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
    base: str | None = None,
) -> list[str]:
    """THE decision (tan-cli#970 round 2 -> round 3 review, the major fix).
    See the module docstring's "The major fix" section for the full
    reasoning. Two-line version: compare exactly two states -- the content
    at `base` (or, when `base` is `None`, EACH of HEAD's own immediate git
    parents individually -- unchanged from before blocker 2, and still what
    correctly resolves a real `merge=union` merge of two divergent appends,
    where each side's content must independently survive) -- against
    `current_lines`. Nothing in between is ever consulted: a line dropped by
    an intermediate commit and restored by a later one in the same range is
    therefore not a violation, because only the anchor and the final state
    are ever compared. Returns the anchor's own lines whenever any of them
    failed to survive, in order, into `current_lines` -- empty when clean.
    """
    anchors = [base] if base is not None else _parents_of(cwd, "HEAD")

    violations: list[str] = []
    for anchor in anchors:
        anchor_lines = _content_at(cwd, anchor, rel_path)
        if anchor_lines is None:
            continue
        if not _is_ordered_subsequence(anchor_lines, current_lines):
            violations.extend(anchor_lines)
    return violations


def _commits_since_base(cwd: Path, base_ref: str) -> list[str]:
    """SHAs of every commit reachable from HEAD but not from `base_ref`,
    oldest first -- i.e. everything the current run's branch introduced
    since diverging from its base. Used only for `_locate_dropping_commit`'s
    diagnostics now (tan-cli#970 round 3 review demotes the walk this once
    fed); the decision itself (`ledger_violations`) no longer consults it.
    A failed `git rev-list` raises `GitCommandFailed` rather than returning
    `[]` (tan-cli#970 round 2 review, minor): an earlier version of this
    helper returned `[]` on failure, which the diagnostic caller would have
    read as "nothing to walk" and silently produced no culprit name --
    survivable now that the decision does not depend on this list, but
    still a git failure that must not disappear quietly."""
    result = _git_ok("rev-list", "--reverse", f"{base_ref}..HEAD", cwd=cwd)
    if result.returncode != 0:
        raise GitCommandFailed(
            f"git rev-list --reverse {base_ref}..HEAD failed in {cwd}: "
            f"{result.stderr!r} -- this must fail loudly rather than "
            "silently return an empty range, which a caller could "
            "otherwise mistake for a legitimately empty one."
        )
    return [line for line in result.stdout.splitlines() if line]


def _locate_dropping_commit(cwd: Path, rel_path: str, commits: list[str]) -> tuple[str, str] | None:
    """Diagnostic only -- never the decision (tan-cli#970 round 3 review
    demotes this from what round 2 made it). Walks every commit the current
    run's branch introduced since its base, oldest first, each checked
    against its OWN git parent the same ordered-subsequence way
    `ledger_violations` checks a single anchor, and returns the FIRST
    (commit, parent) pair where a parent's line failed to survive into that
    commit -- purely to name a culprit in the failure message. A pair
    returned here is not necessarily still a violation by the time HEAD is
    reached (a later commit in `commits` may have restored it) --
    `ledger_violations` is what actually decided a violation exists; this
    only explains one plausible cause."""
    for commit in commits:
        commit_lines = _content_at(cwd, commit, rel_path)
        if commit_lines is None:
            continue
        for parent in _parents_of(cwd, commit):
            parent_lines = _content_at(cwd, parent, rel_path)
            if parent_lines is None:
                continue
            if not _is_ordered_subsequence(parent_lines, commit_lines):
                return (commit, parent)
    return None


class BaseRefUnresolved(RuntimeError):
    """Raised when a run whose event type ALWAYS carries a PR/merge-queue
    base ref (`pull_request`/`pull_request_target`, `merge_group`) does not
    yield one this checkout can resolve. The caller must turn this into a
    hard failure, not a fallback or a skip -- see the module docstring,
    tan-cli#970 review blocker 2."""


def _resolve_base_ref(cwd: Path) -> str | None:
    """The ref to diff HEAD against for the base-vs-HEAD check, or `None`
    when this run has no PR/merge-queue base to enforce against at all --
    see the module docstring's "Blocker 2" section for the full reasoning
    and the exact env var each CI context relies on."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")

    if event in ("pull_request", "pull_request_target"):
        base = os.environ.get("GITHUB_BASE_REF", "").strip()
        if not base:
            raise BaseRefUnresolved(
                f"GITHUB_EVENT_NAME={event!r} but GITHUB_BASE_REF is unset -- "
                "GitHub Actions sets this automatically for both events, so "
                "its absence means something upstream of this test is not "
                "what it claims to be."
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
    """The real enforcement. See the module docstring's "The major fix"
    section for the full reasoning. Two-line version: the ledger's content
    at its PR/merge-queue base (or, outside a PR/merge-queue run, at each of
    HEAD's own immediate parents) must still be present, in order, in the
    ledger's CURRENT content -- nothing in between is ever consulted."""
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
        violations = ledger_violations(REPO, LOG_REL, current, base=base_ref)
        diagnostic_commits = _commits_since_base(REPO, base_ref)
        window_desc = f"relative to its base ref ({base_ref})"
    else:
        violations = ledger_violations(REPO, LOG_REL, current)
        diagnostic_commits = ["HEAD"]
        window_desc = "relative to its parent commit(s) (no PR/merge-queue base ref applies to this run)"

    if not violations:
        return

    culprit = _locate_dropping_commit(REPO, LOG_REL, diagnostic_commits)
    culprit_desc = f" -- first observed dropped at commit {culprit[0]} (its own parent {culprit[1]})" if culprit else ""
    assert not violations, (
        f"{LOG_REL} lost or reordered content {window_desc}{culprit_desc}. "
        f"Missing/reordered line(s): {violations}. The ledger is append-only: "
        "every line present at the base must still be present, in order, at "
        "HEAD; new lines may be added anywhere. If this fired, add the "
        "missing entry back in a normal follow-up commit -- this check only "
        "looks at the CURRENT state, so restoring the line verbatim (in "
        "order) is enough; you do not need to rewrite history "
        "(tan-cli#970 round 3 review: an earlier version of this message "
        "prescribed a history rewrite that did not actually clear the "
        "failure -- it does now, because the decision compares base vs. "
        "CURRENT, not every intermediate commit)."
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
    must not be enough to mask the edit). No `base=` given -- the fallback
    path (HEAD's own immediate parent), unchanged by the major fix."""
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


def test_an_existing_entry_deleted_outright_with_no_replacement_is_caught(tmp_path):
    """Pure deletion, no edit and no append to obscure it -- the simplest
    possible loss (tan-cli#970 round 2 review, preserve probe 2, reproduced
    hermetically rather than only against the real ledger)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- first entry", "- second entry", "- third entry"])
    _commit(repo, "base")

    _write(repo, "LOG.md", ["- first entry", "- third entry"])
    _commit(repo, "deletes the middle entry outright")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, "an existing entry was deleted outright and must be flagged"


def test_a_reorder_of_existing_entries_with_nothing_deleted_is_caught(tmp_path):
    """Minor 5 (tan-cli#970 review, round 1): the ordering half of the check
    had no test of its own. Swaps two already-committed entries -- both
    survive, just in a different relative order, nothing deleted -- proving
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


def test_an_uncommitted_edit_of_an_existing_entry_is_caught(tmp_path):
    """The real gate reads `current` from the WORKING TREE
    (`core.LOG_PATH.read_text()`), never from a git rev, specifically so an
    uncommitted edit is caught before it ever lands -- there is no separate
    "swap the last entry for the working tree" step any more (tan-cli#970
    round 3 review, nit: the old `commits[-1] = "HEAD"` line this used to
    depend on is gone; the mechanism is now just "the caller always passes
    the working tree", which this test exercises directly rather than
    trusting the old line was load-bearing)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry", "- second entry"])
    _commit(repo, "root commit")  # HEAD's own parent -- the fallback anchor below

    _write(repo, "LOG.md", ["- base entry", "- second entry", "- third entry"])
    _commit(repo, "base")  # HEAD -- a legitimate append, nothing edited yet

    # Edited on disk, never committed: rewrites an entry HEAD's own parent
    # (the root commit above) already had.
    _write(repo, "LOG.md", ["- base entry", "- second entry (edited, not committed)", "- third entry"])

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    violations = ledger_violations(repo, "LOG.md", current)
    assert violations, "an uncommitted edit to an existing entry must still be caught"


def test_a_checkout_theirs_resolution_that_drops_a_branchs_own_entry_is_caught(tmp_path):
    """Reproduces the tan-cli#902 shape exactly: `feature` appends its own
    entry, `main` (standing in for `dev`) appends a different one, and the
    conflict is resolved with `git checkout --theirs` -- `main`'s content
    wins verbatim, discarding `feature`'s own entry. This is a single hop
    (the damaging merge IS HEAD), so the fallback path (HEAD's own immediate
    parents, unchanged by the major fix) already catches it without needing
    `base=`."""
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


def test_a_base_entry_dropped_early_in_the_range_is_still_caught_even_with_an_untouched_follow_up_commit(tmp_path):
    """Blocker 2 shape B (tan-cli#970 review, round 1), reconstructed for
    the major fix's redefinition (round 3): the entry lost here is one the
    PR's BASE already had -- not the branch's own private addition -- so it
    must still be caught however far back the damaging commit sits, and
    however many ordinary commits pile on afterward. If this test instead
    used an entry the branch itself invented and later discarded within its
    own history, it would legitimately pass clean under the major fix -- see
    the module docstring's "The major fix" section for why that is
    deliberate, not a gap (the tan-cli#971 case this fix exists for is
    exactly that shape)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "pr")
    _write(repo, "LOG.md", ["- reworded away from the base entry"])
    _commit(repo, "drops the base entry while rewording")

    # One ordinary follow-up commit that never touches the ledger.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _commit(repo, "ordinary follow-up, ledger untouched")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- base entry" not in current, "the base entry should still be missing at this point"

    violations = ledger_violations(repo, "LOG.md", current, base=base_sha)
    assert violations, (
        "the base's own entry was dropped by an early PR commit and never "
        "restored -- a later, ledger-untouched commit landing on top must "
        "not hide that from the base-vs-HEAD comparison"
    )


def test_the_github_pull_request_merge_ref_shape_still_catches_a_genuine_loss(tmp_path):
    """Blocker 2 shape C (tan-cli#970 review, round 1), reconstructed for
    the major fix (round 3): on a real `pull_request` run, the checked-out
    HEAD is GitHub's OWN synthetic `merge(base_tip, head_tip)` commit. Here
    the base's own entry is dropped on BOTH sides of the eventual merge (the
    PR branch drops it and never restores it; the base side never carried a
    duplicate to fall back on), so nothing -- not even a `merge=union`
    resolution -- puts it back. The two-parent shape must not hide that."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own entry"])
    _commit(repo, "feature appends")
    _write(repo, "LOG.md", ["- feature's own entry", "- feature's follow-up entry"])
    _commit(repo, "drops the base entry while also appending")

    # main never touches LOG.md again -- nothing on the base side carries
    # the entry forward for a merge to recover it from.
    _git(repo, "checkout", "-q", "main")
    _write(repo, "unrelated.txt", ["noise"])
    _commit(repo, "main: unrelated change, ledger untouched")

    _git(repo, "checkout", "-q", "-b", "pr-merge", "main")
    merge = _git(repo, "merge", "-q", "--no-edit", "feature", check=False)
    assert merge.returncode == 0, merge.stderr

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- base entry" not in current, "nothing should have restored it in this scenario"

    violations = ledger_violations(repo, "LOG.md", current, base=base_sha)
    assert violations, (
        "the base entry was dropped inside feature's own branch history and "
        "never restored by either side of the synthetic pull/merge commit -- "
        "the base-vs-HEAD comparison must still catch it"
    )


def test_a_dropped_entry_restored_by_a_later_commit_passes_clean(tmp_path):
    """The case the major fix exists for (tan-cli#970 round 2 -> round 3
    review): an early PR commit drops a base entry, and a LATER commit in
    the same range puts it back, verbatim and in order (a legitimate
    self-correction, exactly the tan-cli#971 shape). This must PASS -- the
    ledger is intact by the time HEAD is reached, which is what
    "append-only" actually promises. Under round 2's per-commit-range design
    this stayed flagged forever, because the damaging commit never left
    `base..HEAD`; that is the false positive this test guards against."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "pr")
    _write(repo, "LOG.md", [])
    _commit(repo, "drops the base entry")

    _write(repo, "LOG.md", ["- base entry", "- pr's own new entry"])
    _commit(repo, "restores the base entry and appends its own")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- base entry" in current, "sanity: the restoring commit must have put it back"

    violations = ledger_violations(repo, "LOG.md", current, base=base_sha)
    assert violations == [], (
        "the base entry was dropped and then restored, verbatim and in "
        f"order, within the same PR's range -- this must pass clean, but "
        f"was flagged: {violations}"
    )


def test_a_legitimate_union_merge_of_two_divergent_appends_passes_clean(tmp_path):
    """The half that will actually bite in practice. Configures the SAME
    `merge=union` driver `.gitattributes` uses for the real ledger, has both
    branches append a genuinely different entry to the same tail, and merges
    -- git resolves this with no conflict markers at all, ours-then-theirs.
    The check must see zero violations. No `base=` given -- the fallback
    path (each of HEAD's own immediate parents individually), unchanged by
    the major fix."""
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


def test_commits_since_base_raises_loudly_when_git_rev_list_fails(tmp_path):
    """tan-cli#970 round 2 review, minor: `_commits_since_base` used to
    return `[]` on a non-zero `git rev-list`, which a caller could read as
    "nothing new relative to base" -- the same vacuity class
    `BaseRefUnresolved` exists to kill one layer up. An unresolvable
    `base_ref` (never fetched, never existed) makes `git rev-list` fail for
    real; this must raise, not return an empty list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")

    with pytest.raises(GitCommandFailed):
        _commits_since_base(repo, "origin/no-such-ref-was-ever-fetched")


def test_content_at_distinguishes_a_missing_path_from_a_real_git_failure(tmp_path):
    """tan-cli#970 round 2 review, minor: `_content_at` used to collapse
    "path does not exist at this rev" (legitimate -- the ledger predates
    this commit) and "git failed for some other reason" (not legitimate)
    into the same `None`, which `ledger_violations` silently treats as
    "nothing to check" either way. A rev that predates the path must still
    return `None`; a rev that does not resolve at all must raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write(repo, "a.txt", ["hi"])
    _commit(repo, "init, no LOG.md yet")

    assert _content_at(repo, "HEAD", "LOG.md") is None, "the path legitimately does not exist yet at this rev"

    with pytest.raises(GitCommandFailed):
        _content_at(repo, "not-a-real-rev", "a.txt")
