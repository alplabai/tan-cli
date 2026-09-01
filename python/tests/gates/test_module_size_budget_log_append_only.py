# SPDX-License-Identifier: Apache-2.0
"""`MODULE_SIZE_BUDGET_LOG.md` declares itself append-only but nothing
enforced that (tan-cli#906) until this file.

## tan-cli#907: this file is now FROZEN

As of tan-cli#907, `scripts/regen_module_size_budget.py` no longer writes
into `MODULE_SIZE_BUDGET_LOG.md` at all -- every new ledger entry is a new
file under the sibling `MODULE_SIZE_BUDGET_LOG.d/` directory (mirroring
`changelog.d/`; see that directory's `README.md`). This gate is UNCHANGED and
keeps running: the `.md` file's 2026-08-11 through 2026-08-30 history still
needs the same append-only protection it always did, it just never gains a
new line to protect going forward. `MODULE_SIZE_BUDGET_LOG.d/`'s own
immutability is a separate, simpler gate --
`test_module_size_budget_log_d_entries_are_immutable.py` -- because a
directory of one-file-per-entry does not need this file's base-vs-HEAD
machinery at all (see that file's own docstring for why "compare against a
resolved PR base" was never quite right even here, and why the new design
sidesteps the question rather than fixing it).

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
PR's own branch added and later dropped by an ORDINARY, single-parent commit
within its own unmerged history (not inherited from the base) is deliberately
not this gate's concern -- that is exactly the `tan-cli#971` shape, a PR
correcting its own draft content before it ever reaches `dev`, and the
ledger's promise to `dev` is about `dev`'s own lines, not a branch's private
editing history. This sentence used to carry no "by an ordinary, single-parent
commit" clause; without it, it was too broad -- see the tan-cli#1065 section
below, which narrows it.

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

## tan-cli#1065 -- the branch's OWN committed lines, when a MERGE drops them

The paragraph above is right about a branch revising its own draft, and was
wrong as a blanket rule. `ledger_violations` only ever asserts that the
ANCHOR's lines survive; on a `pull_request` run the anchor is the PR's base
(`dev`), and a line the BRANCH committed is by construction never one of the
base's lines -- so discarding it costs that check nothing to detect. Measured
against PR #1062's real pre-merge head `f77f4818` merged with `1f06a426`
(`dev` as it stood then), the `MODULE_SIZE_BUDGET_LOG.md` conflict resolved
with `git checkout --theirs`: four committed lines gone,
`ledger_violations(base='1f06a426')` -> `[]`, whole file `14 passed in 0.59s`
under `GITHUB_EVENT_NAME=pull_request`. That is the tan-cli#902 incident, in
the one context where it matters.

`merge_loss_violations` closes it WITHOUT re-opening the tan-cli#971
false-positive, by splitting on which commit dropped the line: an ordinary,
single-parent commit is a deliberate revision (allowed); a MERGE commit is a
conflict resolution silently discarding an already-committed entry the other
parent never had to weigh against (never allowed). See that function's own
docstring for why both of its conditions -- missing from the merge's own
content AND still missing at HEAD -- are load-bearing, and for the one respect
in which it is deliberately narrower than `ledger_violations`.

Still reachable, measured, not assumed: `MODULE_SIZE_BUDGET_LOG.md` is frozen
against `regen_module_size_budget.py`, not against people. Two of the four
`dev` commits after tan-cli#907 landed appended to it by hand -- `1f06a426`
(#1060, +7) and `b3c40619` (#1062, +4), both correction notes in the same tail
region -- and tan-cli#907 also removed this file's `merge=union` attribute, so
those appends now conflict outright rather than union-merging. The conflict
whose RESOLUTION this gate guards is more likely today, not less.
(`MODULE_SIZE_BUDGET_LOG.d/` needs none of this: a merge that drops an entry
FILE is caught by `test_module_size_budget_log_d_entries_are_immutable.py`'s
tree cross-check, which is not anchored on a base ref at all.)

## Two shapes this deliberately does NOT try to catch

`.gitattributes` used to document two ways `merge=union` itself gets a
conflict wrong (two branches editing the SAME existing entry land both
variants with no conflict markers; a delete racing an adjacent append
silently reverts the deletion). Both were pre-existing, documented
limitations of the union driver, not a gap in this gate -- fighting them here
would have meant rejecting the union driver's own legitimate output, i.e.
regressing the merge-clean case this file also has to prove. tan-cli#906
scoped this gate to "an entry edited or removed"; tan-cli#907 closed the
underlying question a different way -- `MODULE_SIZE_BUDGET_LOG.md` is now
frozen (no future entry lands in it, so `merge=union` is no longer needed or
applied to it at all) and every new entry goes to
`MODULE_SIZE_BUDGET_LOG.d/`, whose one-file-per-entry shape makes both of
these union-specific failure modes structurally unreachable rather than
merely undetected: there is no shared file for two entries to collide inside
of any more.
"""
from __future__ import annotations

import ast
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


def merge_loss_violations(
    cwd: Path,
    rel_path: str,
    current_lines: list[str],
    commits: list[str],
) -> list[tuple[str, str, list[str]]]:
    """THE SECOND decision (tan-cli#1065). `ledger_violations` above only ever
    asserts that the ANCHOR's lines survive, and on a `pull_request` run the
    anchor is the PR's base (`dev`). A line the BRANCH itself committed is,
    by construction, never one of the base's lines -- so discarding it costs
    that check nothing to detect. Measured against PR #1062's real pre-merge
    head `f77f4818` merged with `1f06a426` (`dev` as it stood at the time),
    the `MODULE_SIZE_BUDGET_LOG.md` conflict resolved with
    `git checkout --theirs`: four lines the branch had committed (two
    tan-cli#427 entries and their sub-bullets) are absent at HEAD, and
    `ledger_violations(base='1f06a426')` returns `[]` -- the whole gate file
    `14 passed in 0.59s` under `GITHUB_EVENT_NAME=pull_request`. That is the
    tan-cli#902 incident shape, exactly, in the CI context that matters.

    Closing it needs a check that does NOT re-open the tan-cli#971
    false-positive the round-3 major fix exists to prevent. The two shapes
    look superficially alike -- in both, a line this branch committed is gone
    at HEAD -- but they differ in WHICH commit dropped it, and that
    difference is the whole distinction:

    * a branch revising its own draft entry in an ORDINARY, single-parent
      commit. Allowed (tan-cli#971); the ledger's promise to `dev` is about
      `dev`'s own lines, and a branch may correct its own not-yet-merged
      prose.
    * a committed entry dropped by a MERGE commit -- `git checkout --theirs`
      takes the other side's whole file, and the loss lands in the merge
      commit's own tree. Flagged.

    The discriminator is COMMIT SHAPE, and commit shape does not carry
    intent. That is a deliberate, load-bearing choice, not an approximation
    that happens to work, and the counterexample is real -- measured against
    this function (tan-cli#1065 review round 2):

        S1 merge keeps BOTH sides                                -> [] CLEAN
        S5 merge REORDERS both sides, loses nothing              -> [] CLEAN
        S3 drops its own superseded entry in an ORDINARY commit  -> [] CLEAN
        S2 drops its own superseded entry IN the merge           -> FLAGGED
        S4 rewords its OWN draft WHILE resolving the merge       -> FLAGGED

    S4 is the tan-cli#971 shape verbatim, and it is FLAGGED: it differs from
    the allowed S3 only in which commit the edit landed in. That is not an
    oversight. There is no git signal that separates S4 from tan-cli#902 --
    in both, a line the branch committed is dropped by a merge and is absent
    at HEAD -- so any rule that lets S4 through lets the incident through,
    and the incident is the one that costs a reasoned entry nobody notices.
    Strictness is chosen. S2 and S4 are pinned by
    `test_a_branch_dropping_its_own_superseded_entry_inside_the_merge_is_
    flagged_by_choice` and
    `test_a_branch_rewording_its_own_draft_while_resolving_the_merge_is_
    flagged_by_choice` so the boundary stays visible.

    The cost of that strictness is paid in the REMEDY, which is why
    `_enforce`'s failure text prints two of them rather than one. Telling an
    S4 author to "add the missing line back verbatim" -- the only remedy the
    first version of that message named -- would park a superseded draft in
    an append-only ledger next to its own replacement, permanently. The
    right fix for S2/S4 is the ORDERING: make the merge keep both parents'
    lines, then redo the revision in its own ordinary follow-up commit
    (shape S3). Both pinning tests above execute that remedy and assert it
    is clean, so it is proven, not merely described.

    So this check only ever looks at MERGE commits in the given range, and
    flags a parent's line only when it is missing from the MERGE COMMIT'S
    OWN content AND still missing from `current_lines`. Both conditions are
    load-bearing:

    * "missing from the merge's own content" is what excludes the tan-cli#971
      shape: a later ordinary commit rewording an entry leaves the merge
      itself intact, so nothing is flagged.
    * "still missing at HEAD" preserves round 3's NET-STATE principle: a
      merge that dropped a line which a later commit restored is clean, so
      the prescribed fix (restore the line in a normal follow-up commit)
      actually clears the failure and no history rewrite is ever required.

    Deliberately narrower than `ledger_violations` in one respect: membership,
    not ordered subsequence. A merge that REORDERS lines without losing any is
    not flagged here. What this exists to catch is a committed entry silently
    discarded; a pure reorder inside a merge loses no reasoning, and requiring
    order across two independently-appending parents would red legitimate
    resolutions that interleave both sides. Order relative to the BASE is
    still enforced, unchanged, by `ledger_violations`.

    Returns `(merge commit, parent, lost lines)` triples -- empty when clean.
    """
    violations: list[tuple[str, str, list[str]]] = []
    for commit in commits:
        parents = _parents_of(cwd, commit)
        if len(parents) < 2:
            # Not a merge: an ordinary commit's edit of a line this branch
            # itself added is the tan-cli#971 case, deliberately allowed.
            continue
        commit_lines = _content_at(cwd, commit, rel_path)
        if commit_lines is None:
            continue
        for parent in parents:
            parent_lines = _content_at(cwd, parent, rel_path)
            if parent_lines is None:
                continue
            lost = [
                line
                for line in parent_lines
                if line.strip() and line not in commit_lines and line not in current_lines
            ]
            if lost:
                violations.append((commit, parent, lost))
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


def _enforce(
    cwd: Path, rel_path: str, current_lines: list[str], base_ref: str | None
) -> None:
    """BOTH decisions plus their assertions, factored out of the real
    enforcement test below so a hermetic repo can drive the enforcement PATH
    end-to-end and not just the two decision functions in isolation
    (tan-cli#1065 review round 2, major 2).

    Why this exists at all: the three hermetic tests added for
    `merge_loss_violations` all called it DIRECTLY, so deleting its call
    from the real enforcement test left the whole suite green -- measured,
    `34 passed in 1.87s` across both gate files with the round-3 half
    disconnected from the only test that enforces anything. The
    `f77f4818` repro proved the function WORKS; nothing proved it was
    CALLED, and those are different claims. `test_the_enforcement_path_
    itself_rejects_a_theirs_resolution` now drives this function on a
    throwaway repo, so removing either assertion BELOW, or the
    `merge_loss_violations`/`ledger_violations` call feeding it, reds.

    That covers everything inside this function and nothing outside it. The
    remaining link -- the one CALL that connects this function to the real
    repository's own ledger, in
    `test_the_ledger_only_ever_appends_since_the_prs_base` -- is out of reach
    of any runtime test here, because this repo's ledger is clean, so the
    call passing and the call being absent look identical. Measured
    (tan-cli#1065 review round 3, mutant W1b): replacing that single line with
    `pass` left the whole of `tests/gates/` at `875 passed, 6 skipped`, its
    exact baseline. It is pinned instead by
    `test_the_real_enforcement_test_calls_enforce_on_this_repos_own_ledger`,
    a source-level assertion in this repo's own established idiom
    (`test_subprocess_env_routes_through_the_helper.py`). The regress stops
    there for the same reason it stops in
    `test_module_size_budget_check_is_wired_into_ci.py`: a source-level
    "is it wired in" assertion cannot itself be satisfied vacuously -- delete
    it and the deletion is the diff. (The sibling
    `..._log_d_entries_are_immutable.py` never had the inner half of this
    gap: its equivalent check lives INSIDE `entry_violations`, which every
    hermetic test drives.)"""
    if base_ref is not None:
        violations = ledger_violations(cwd, rel_path, current_lines, base=base_ref)
        diagnostic_commits = _commits_since_base(cwd, base_ref)
        window_desc = f"relative to its base ref ({base_ref})"
    else:
        violations = ledger_violations(cwd, rel_path, current_lines)
        diagnostic_commits = ["HEAD"]
        window_desc = "relative to its parent commit(s) (no PR/merge-queue base ref applies to this run)"

    # tan-cli#1065: the check above protects the ANCHOR's lines only, so a
    # merge commit that discarded lines THIS BRANCH committed passes it
    # clean on a `pull_request` run (`dev` never had those lines). Scanned
    # separately, over the same window -- see `merge_loss_violations`.
    merge_losses = merge_loss_violations(cwd, rel_path, current_lines, diagnostic_commits)
    assert not merge_losses, (
        f"a MERGE commit in this run's window dropped {rel_path} line(s) that "
        "one of its own parents had already committed, and nothing since has "
        f"restored them: {merge_losses}. This is the tan-cli#902 shape -- a "
        "conflict resolution (classically `git checkout --theirs`) taking one "
        "side of the file wholesale and silently discarding the other side's "
        "own reasoned entries. The ledger is append-only across a merge too: "
        "a resolution must keep BOTH parents' lines.\n"
        "\n"
        "TWO DIFFERENT FIXES -- pick by what the missing line actually is:\n"
        "\n"
        "1. Somebody ELSE's entry (or your own, and you still want it): add "
        "the missing line(s) back verbatim in a normal follow-up commit. "
        "This check only asks whether they are present at HEAD, so no "
        "history rewrite is needed.\n"
        "\n"
        "2. YOUR OWN entry that you deliberately reworded or superseded "
        "WHILE resolving this merge: do NOT paste the old line back -- that "
        "would park a superseded entry in an append-only ledger next to its "
        "own replacement, permanently. Redo it in the other order instead: "
        "make the merge keep BOTH parents' lines, then make your revision in "
        "its own ORDINARY, single-parent commit after the merge. That "
        "ordering is deliberately clean here (tan-cli#971) -- see "
        "`merge_loss_violations`'s docstring for why the same edit is "
        "flagged inside the merge and allowed after it, which is a chosen "
        "strictness and not an oversight."
    )

    if not violations:
        return

    culprit = _locate_dropping_commit(cwd, rel_path, diagnostic_commits)
    culprit_desc = f" -- first observed dropped at commit {culprit[0]} (its own parent {culprit[1]})" if culprit else ""
    assert not violations, (
        f"{rel_path} lost or reordered content {window_desc}{culprit_desc}. "
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


def test_the_ledger_only_ever_appends_since_the_prs_base():
    """The real enforcement. See the module docstring's "The major fix"
    section for the full reasoning. Two-line version: the ledger's content
    at its PR/merge-queue base (or, outside a PR/merge-queue run, at each of
    HEAD's own immediate parents) must still be present, in order, in the
    ledger's CURRENT content -- nothing in between is ever consulted -- and
    no MERGE commit in the same window may have dropped a line one of its
    own parents committed (tan-cli#1065). Both live in `_enforce`, which a
    hermetic test drives directly so neither can be silently unwired."""
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

    _enforce(REPO, LOG_REL, current, base_ref)


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


def _theirs_resolution_repo(repo: Path) -> None:
    """The tan-cli#902 shape as a PR would produce it: `feature` commits its
    own reasoned entry, `main` (standing in for `dev`) appends a different
    one, and the conflict is resolved with `git checkout --theirs` -- taking
    `dev`'s file wholesale and discarding `feature`'s own committed entry.
    Leaves HEAD on `feature`, at the damaging merge commit."""
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- tan-cli#427 feature's own reasoned entry"])
    _commit(repo, "feature appends its own reasoned entry")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends its own entry")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode != 0, "expected a real conflict with no merge=union driver configured"
    _git(repo, "checkout", "--theirs", "LOG.md")
    _git(repo, "add", "LOG.md")
    _git(repo, "commit", "-q", "--no-edit")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- tan-cli#427 feature's own reasoned entry" not in current, (
        "the --theirs resolution should have dropped the branch's own entry"
    )


def test_a_pull_request_run_still_catches_a_merge_that_dropped_the_branchs_own_entry(tmp_path):
    """tan-cli#1065's third blind spot. The sibling test above proves the
    same `--theirs` loss is caught on the anchor-is-HEAD's-parents path -- but
    that path is exactly the one a `pull_request` run does NOT take. With a
    base ref resolved (`dev`), `ledger_violations` compares only the BASE's
    lines against HEAD, and the branch's own entry is by construction not one
    of them, so it returns `[]` and the whole gate file passed `14 passed`
    green with the entry gone. Measured against PR #1062's real pre-merge head
    as well as here. `merge_loss_violations` is what closes it."""
    repo = tmp_path / "repo"
    _theirs_resolution_repo(repo)
    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()

    assert ledger_violations(repo, "LOG.md", current, base="main") == [], (
        "this test is only meaningful while the base-anchored check is blind "
        "to the loss -- if this ever fails, the boundary moved and this "
        "test's premise needs re-deriving, not silently satisfying"
    )

    losses = merge_loss_violations(repo, "LOG.md", current, _commits_since_base(repo, "main"))
    assert losses, (
        "a merge commit discarded the branch's own already-committed entry "
        "and the pull_request-shaped check did not flag it -- this is the "
        "exact tan-cli#902 incident in the CI context that matters"
    )
    assert any(
        "- tan-cli#427 feature's own reasoned entry" in lost
        for _commit_sha, _parent, lost in losses
    ), f"the flagged lines must name the entry that was actually lost, got: {losses}"


def test_a_branch_revising_its_own_entry_in_an_ordinary_commit_after_a_merge_is_not_flagged(tmp_path):
    """The tan-cli#971 shape, which the round-3 major fix deliberately allows
    and which this widening must not re-break: the branch commits an entry,
    merges `dev` cleanly (nothing lost in the merge), and only THEN rewords
    its own not-yet-merged entry in an ordinary, single-parent commit. The
    reworded line is gone from HEAD and was present at the merge's own parent,
    so a naive "every merge parent's lines must survive to HEAD" rule would
    red it. The merge's own content is what is compared instead, precisely to
    keep this legitimate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", "- feature's own DRAFT entry"])
    _commit(repo, "feature appends a draft entry")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", "- dev's own entry"])
    _commit(repo, "dev appends its own entry")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    if merge.returncode != 0:
        # Resolved the RIGHT way -- both sides kept. The loss this gate hunts
        # is the resolution that drops one side, not the conflict itself.
        _write(repo, "LOG.md", ["- base entry", "- feature's own DRAFT entry", "- dev's own entry"])
        _git(repo, "add", "LOG.md")
        _git(repo, "commit", "-q", "--no-edit")

    _write(repo, "LOG.md", ["- base entry", "- feature's own REWORDED entry", "- dev's own entry"])
    _commit(repo, "feature rewords its own not-yet-merged entry")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    assert "- feature's own DRAFT entry" not in current

    losses = merge_loss_violations(repo, "LOG.md", current, _commits_since_base(repo, "main"))
    assert losses == [], (
        "a branch revising its OWN not-yet-merged entry in an ordinary "
        "commit is the tan-cli#971 case and must stay legitimate, but got: "
        f"{losses}"
    )

    # The same shape through the whole enforcement path, so `_enforce`'s
    # CLEAN direction is pinned too and a mutant that made it always raise
    # would red here rather than passing unnoticed.
    _enforce(repo, "LOG.md", current, "main")


def test_a_merge_dropped_line_restored_by_a_later_commit_is_not_flagged(tmp_path):
    """Round 3's NET-STATE principle, preserved by the new check: a merge
    that dropped a line which a later commit put back verbatim is clean, so
    the failure message's prescribed fix ("add the missing line back in a
    normal follow-up commit") actually clears the failure. A per-merge-commit
    rule with no HEAD condition would stay red forever and force a history
    rewrite -- the exact defect round 3 removed from this gate."""
    repo = tmp_path / "repo"
    _theirs_resolution_repo(repo)

    _write(repo, "LOG.md", ["- base entry", "- dev's own entry", "- tan-cli#427 feature's own reasoned entry"])
    _commit(repo, "restore the entry the merge resolution dropped")

    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    losses = merge_loss_violations(repo, "LOG.md", current, _commits_since_base(repo, "main"))
    assert losses == [], (
        "a merge-dropped line restored verbatim by a later commit must clear "
        f"the check without a history rewrite, but got: {losses}"
    )


def _conflicting_merge(repo: Path, feature_lines: list[str], dev_lines: list[str]) -> None:
    """`feature` and `main` (standing in for `dev`) each append to the ledger
    tail, then `feature` merges `main` and hits the real content conflict
    tan-cli#907 made possible again by removing this file's `merge=union`.
    Leaves the merge UNRESOLVED, for the caller to resolve however the shape
    under test requires. HEAD is `feature`."""
    repo.mkdir()
    _init_repo(repo)

    _write(repo, "LOG.md", ["- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "LOG.md", ["- base entry", *feature_lines])
    _commit(repo, "feature appends its own entries")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "LOG.md", ["- base entry", *dev_lines])
    _commit(repo, "dev appends its own entry")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode != 0, (
        "these two tail appends must conflict outright -- tan-cli#907 removed "
        "this file's merge=union attribute, which is exactly why resolving "
        "this conflict by hand is now routine"
    )


def _resolve_as(repo: Path, lines: list[str]) -> None:
    """Finish the conflicted merge with `lines` as the resolved content."""
    _write(repo, "LOG.md", lines)
    _git(repo, "add", "LOG.md")
    _git(repo, "commit", "-q", "--no-edit")


def _losses(repo: Path) -> list[tuple[str, str, list[str]]]:
    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()
    return merge_loss_violations(repo, "LOG.md", current, _commits_since_base(repo, "main"))


def test_a_branch_dropping_its_own_superseded_entry_inside_the_merge_is_flagged_by_choice(tmp_path):
    """Shape S2 (tan-cli#1065 review round 2). The branch drops its OWN, now
    superseded entry as part of resolving the merge. Intent-wise this is the
    tan-cli#971 case; commit-shape-wise it is indistinguishable from
    tan-cli#902, because in both a line the branch committed is dropped by a
    merge and is absent at HEAD. There is no git signal that separates them,
    so this is FLAGGED -- deliberately, and pinned here so the boundary is
    visible rather than reading as unconsidered.

    The escape hatch is the ORDERING, and it is proven executable below, not
    just described in the failure text: make the merge keep both parents'
    lines, then drop the superseded entry in an ordinary, single-parent
    follow-up commit (shape S3). That is clean."""
    flagged = tmp_path / "flagged"
    _conflicting_merge(
        flagged,
        ["- feature SUPERSEDED entry #A", "- feature entry #B"],
        ["- dev's own entry"],
    )
    # Resolution drops the branch's own superseded #A.
    _resolve_as(flagged, ["- base entry", "- feature entry #B", "- dev's own entry"])

    losses = _losses(flagged)
    assert any("- feature SUPERSEDED entry #A" in lost for _c, _p, lost in losses), (
        "a branch's own superseded entry dropped INSIDE the merge is flagged "
        f"by choice -- no git signal separates it from tan-cli#902; got: {losses}"
    )

    # The remedy the failure text prescribes for exactly this case (S3).
    clean = tmp_path / "clean"
    _conflicting_merge(
        clean,
        ["- feature SUPERSEDED entry #A", "- feature entry #B"],
        ["- dev's own entry"],
    )
    _resolve_as(
        clean,
        ["- base entry", "- feature SUPERSEDED entry #A", "- feature entry #B", "- dev's own entry"],
    )
    _write(clean, "LOG.md", ["- base entry", "- feature entry #B", "- dev's own entry"])
    _commit(clean, "drop the superseded entry, in its own follow-up commit")

    assert _losses(clean) == [], (
        "the prescribed remedy must actually work: the SAME edit, made in an "
        "ordinary single-parent commit after a both-sides merge, is clean -- "
        f"got: {_losses(clean)}"
    )


def test_a_branch_rewording_its_own_draft_while_resolving_the_merge_is_flagged_by_choice(tmp_path):
    """Shape S4 (tan-cli#1065 review round 2) -- the tan-cli#971 shape
    verbatim, differing from the allowed S3 only in WHICH commit the edit
    landed in. The author has their own draft entry open while resolving the
    ledger-tail conflict and rewords it there. Flagged, for the same reason
    as S2, and pinned for the same reason.

    This is the shape the failure text's second remedy exists for: pasting
    the old DRAFT line back would park a superseded entry in an append-only
    ledger next to its own replacement. The right fix is the S3 ordering,
    proven clean below."""
    flagged = tmp_path / "flagged"
    _conflicting_merge(flagged, ["- feature DRAFT entry"], ["- dev's own entry"])
    _resolve_as(flagged, ["- base entry", "- feature REWORDED entry", "- dev's own entry"])

    losses = _losses(flagged)
    assert any("- feature DRAFT entry" in lost for _c, _p, lost in losses), (
        "a reword of the branch's own draft made INSIDE the merge is flagged "
        f"by choice, not by oversight; got: {losses}"
    )

    clean = tmp_path / "clean"
    _conflicting_merge(clean, ["- feature DRAFT entry"], ["- dev's own entry"])
    _resolve_as(clean, ["- base entry", "- feature DRAFT entry", "- dev's own entry"])
    _write(clean, "LOG.md", ["- base entry", "- feature REWORDED entry", "- dev's own entry"])
    _commit(clean, "reword the draft entry, in its own follow-up commit")

    assert _losses(clean) == [], (
        "the same reword, made in an ordinary single-parent commit after a "
        f"both-sides merge, must stay clean (tan-cli#971) -- got: {_losses(clean)}"
    )


def test_a_merge_that_only_reorders_both_sides_is_not_flagged(tmp_path):
    """Shape S5. `merge_loss_violations` is deliberately membership-based,
    not ordered-subsequence: a resolution that interleaves the two parents'
    appends in a different order loses no reasoning, and demanding order
    across two independently-appending parents would red legitimate
    resolutions. Order relative to the BASE is still enforced, unchanged, by
    `ledger_violations` -- this test pins only the merge-loss half."""
    repo = tmp_path / "repo"
    _conflicting_merge(repo, ["- feature's own entry"], ["- dev's own entry"])
    _resolve_as(repo, ["- base entry", "- dev's own entry", "- feature's own entry"])

    assert _losses(repo) == [], (
        "a merge that keeps both parents' lines but interleaves them "
        f"differently loses nothing and must not be flagged; got: {_losses(repo)}"
    )


def test_the_enforcement_path_itself_rejects_a_theirs_resolution(tmp_path):
    """tan-cli#1065 review round 2, major 2: the WIRING, not the decision.
    Every other test here calls `merge_loss_violations` directly, so deleting
    its call from the real enforcement test left the suite fully green
    (measured: `34 passed in 1.87s` across both gate files). This test drives
    `_enforce` -- the whole enforcement path the real test runs -- against the
    `--theirs` repo, so unwiring either decision reds here."""
    repo = tmp_path / "repo"
    _theirs_resolution_repo(repo)
    current = (repo / "LOG.md").read_text(encoding="utf-8").splitlines()

    with pytest.raises(AssertionError) as excinfo:
        _enforce(repo, "LOG.md", current, "main")

    message = str(excinfo.value)
    assert "MERGE commit" in message and "tan-cli#902" in message, (
        "the enforcement path must reject this through the merge-loss "
        f"assertion specifically, not some other one -- got: {message}"
    )
    assert "- tan-cli#427 feature's own reasoned entry" in message, (
        f"the failure must name the line that was actually lost -- got: {message}"
    )
    # The remedies, pinned as a case -> remedy MAPPING rather than as a bag
    # of strings. Two earlier versions of this were too weak, both measured
    # GREEN under a mutant before being replaced:
    #   * W2 (round 2): only one fragment of remedy 2 was asserted, so
    #     deleting the other clause left the suite at `38 passed`.
    #   * W4/W5 (round 3): both fragments were asserted but nothing tied
    #     either to its own case, so deleting remedy 1 outright (`38 passed`)
    #     and SWAPPING which case each remedy attached to (`38 passed`) both
    #     stayed green -- the swap being the worse of the two, since it tells
    #     an S4 author to paste the superseded draft back and tells a
    #     tan-cli#902 victim NOT to restore their colleague's destroyed
    #     entry. Two correct strings in the wrong order is worse advice than
    #     one missing string.
    # So: each case label must appear, its own remedy must follow it, and
    # that remedy must come BEFORE the next case label -- i.e. each remedy
    # sits inside its own case's block. This test states the expected
    # mapping itself, in its own literals, rather than importing it from the
    # message it is checking.
    expected_mapping = [
        ("Somebody ELSE's entry", ["add the missing line(s) back verbatim"]),
        (
            "YOUR OWN entry that you deliberately reworded or superseded",
            [
                "do NOT paste the old line back",
                "ORDINARY, single-parent commit after the merge",
            ],
        ),
    ]
    for position, (case_label, remedies) in enumerate(expected_mapping):
        assert case_label in message, (
            f"the failure must name the case {case_label!r} -- an author "
            "cannot pick the right remedy from a message that does not say "
            f"which situation it is for. Got: {message}"
        )
        block_start = message.index(case_label)
        following = expected_mapping[position + 1 :]
        block_end = (
            message.index(following[0][0]) if following and following[0][0] in message
            else len(message)
        )
        for remedy in remedies:
            assert remedy in message, (
                f"the remedy {remedy!r} for case {case_label!r} is missing "
                f"entirely from the failure text. Got: {message}"
            )
            assert block_start < message.index(remedy) < block_end, (
                f"the remedy {remedy!r} must sit inside the block for case "
                f"{case_label!r} (offsets {block_start}..{block_end}), not "
                "attached to a different case -- both strings being present "
                "somewhere is not enough, the MAPPING is what carries the "
                f"advice. Got: {message}"
            )


#: The one test whose job is to run the enforcement against THIS repository's
#: real ledger. Named as a constant because the source-level gate below has to
#: find it by name, and a rename that silently orphaned that gate would be
#: exactly the drift the gate exists to catch.
_ENFORCEMENT_TEST = "test_the_ledger_only_ever_appends_since_the_prs_base"


def test_the_real_enforcement_test_calls_enforce_on_this_repos_own_ledger():
    """tan-cli#1065 review round 3, major 1: the LAST unpinned link.

    Every decision this file makes lives in `ledger_violations`,
    `merge_loss_violations` and `_enforce`, and all three are driven by
    hermetic tests, so a mutant inside any of them reds. The one thing no
    runtime test here can reach is the single line that connects `_enforce`
    to the real repository -- because this repo's own ledger is CLEAN, an
    `_enforce(REPO, LOG_REL, ...)` that runs and an `_enforce` that is never
    called are indistinguishable at runtime. Measured (mutant W1b): replacing
    that one line with `pass` left the whole of `tests/gates/` at
    `875 passed, 6 skipped`, byte-identical to its baseline, with
    `test_the_ledger_only_ever_appends_since_the_prs_base` reduced to "the
    checkout is not shallow and a base ref resolves" while keeping its name.

    So it is pinned at the SOURCE level, in this repo's own established idiom
    for exactly this question -- `test_subprocess_env_routes_through_the_
    helper.py` (AST introspection over code that must call a specific
    helper) and `test_module_size_budget_check_is_wired_into_ci.py` (an
    "is it actually wired in" gate for this very budget system). The regress
    stops here and does not need a gate-for-the-gate: an assertion whose only
    failure mode is being deleted outright cannot be satisfied vacuously --
    deleting it IS the diff, which is what code review reads.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
    defs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _ENFORCEMENT_TEST
    ]
    assert len(defs) == 1, (
        f"expected exactly one `def {_ENFORCEMENT_TEST}` in {Path(__file__).name}, "
        f"found {len(defs)} -- if it was renamed, `_ENFORCEMENT_TEST` must be "
        "renamed with it, or this gate silently stops watching anything."
    )

    calls = [
        node
        for node in ast.walk(defs[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_enforce"
    ]
    assert calls, (
        f"`{_ENFORCEMENT_TEST}` no longer calls `_enforce` at all, so nothing "
        "in this suite checks THIS repository's own MODULE_SIZE_BUDGET_LOG.md "
        "any more -- every other test here builds a throwaway repo. The test "
        "would still pass its name, its shallow-checkout guard and its base-"
        "ref resolution, and the suite would stay green with the ledger "
        "entirely unenforced (measured: 875 passed, 6 skipped, the exact "
        "baseline)."
    )

    positional = {
        arg.id for call in calls for arg in call.args if isinstance(arg, ast.Name)
    }
    assert {"REPO", "LOG_REL"} <= positional, (
        f"`{_ENFORCEMENT_TEST}` calls `_enforce`, but not on this repository's "
        f"own ledger: expected REPO and LOG_REL among its positional "
        f"arguments, got {sorted(positional)}. An `_enforce` aimed at a "
        "throwaway repo here would leave the real ledger unchecked just as "
        "completely as no call at all."
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
