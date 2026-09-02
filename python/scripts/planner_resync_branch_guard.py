#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Refuse to force-push `auto/planner-resync` over a commit the automation
did not write.

tan-cli#1002. `planner-resync.yml`'s "Open or refresh the proposal PR" step
used to do this unconditionally:

    git checkout -q -B "${branch}"
    git add -A python/
    git commit -q -m "..."
    git push -q -f origin "${branch}"

The PR that same workflow opens tells a human, in so many words, "NEEDS A
HUMAN -- part of this re-sync could not be applied" and lists the hand-ports
to do BY HAND ON THAT BRANCH. Someone did exactly that on PR #996. The next
scheduled run then force-pushed `dev` + one fresh machine commit over it,
silently destroying 1096 insertions across 15 files -- six verified re-pins,
seven retired `HAND_PORT_HASHES` entries, a `HAND_PORT_PINNED_SDK_COMMIT`
advance, and a hand-ported function with its tests. Recovery worked only
because GitHub had not yet garbage-collected the unreachable commit; that
window is not guaranteed.

WHY A GUARD, AND NOT (ONLY) "NEVER REUSE THE BRANCH"
-----------------------------------------------------
tan-cli#1002 names two candidate fixes: refuse to overwrite foreign work
before pushing, or never reuse `auto/planner-resync` at all (one branch per
target alp-sdk ref). The second sounds structural, but it does not close the
actual gap: this workflow's own `schedule` trigger is explicitly a BACKSTOP
that fires whether or not alp-sdk moved (see `planner-resync.yml`'s own
"TRIGGERS" comment -- most days it finds nothing to propose), and a
`workflow_dispatch` can re-target the exact ref it already ran against. A
branch named only after the target ref collides with itself on a same-ref
rerun -- precisely the case that destroyed PR #996's work, since nothing
about "which alp-sdk commit" changed between the human's hand-port and the
run that clobbered it. A content-based guard is the only mechanism that is
correct regardless of how the branch got named, so it is the one this module
implements. Per-ref naming still earns its keep as the ESCAPE HATCH a tripped
guard diverts to (see `decide_branch`) -- it keeps `auto/planner-resync`
itself the everyday, reused name (the common case: nothing to protect, no
branch churn) while guaranteeing a diversion target that does not collide
with the branch the guard just protected.

THE SIGNAL: (author name, author email) OR (committer name, committer email),
not "did the diff move"
------------------------------------------------------------------
The workflow sets a fixed identity before it ever commits
(`git config user.name "alp-sdk planner re-sync"` /
`user.email "noreply@alplab.ai"`, `planner-resync.yml`'s "Open or refresh the
proposal PR" step). Any commit reachable from the branch tip but not from
`dev` whose AUTHOR pair is not that exact (name, email) pair, OR whose
COMMITTER pair is not, is, by construction, something a human (or a
different tool) added on top of an automated proposal -- and is protected.
Author alone is not enough: a human who folds hand-port work into the
automation's own commit -- `git commit --amend`, or an `--autosquash`
`fixup!` rebase -- keeps the automation's author identity on the resulting
commit and only changes the committer to their own. PR #1006's review
reproduced this end to end: amending 1096 lines onto the automation's commit
left the guard reading rc 0 ("safe to force-push") on a commit that, a
force-push later, was destroyed exactly like PR #996's. Checking both pairs
closes that gap; the workflow's own commits still pass, since its `git
config` sets both `user.name`/`user.email` before it commits, which fixes
BOTH author and committer to the automation identity in the same commit.
This is not cryptographic: anyone with push access could set `user.email` to
the same string locally (for either identity) and defeat the check. It
closes the actual failure mode observed on PR #996 and reproduced for
PR #1006 (an ordinary human identity on one half of a commit), not a
deliberately hostile one -- the same trust boundary this workflow's own
`GITHUB_TOKEN` already sits inside.

THE FULL RANGE IS SCANNED, NOT JUST THE TIP
--------------------------------------------
tan-cli#1002's third probe is a branch where the human commit sits BEHIND a
later automation commit (`dev -> human -> automation`, tip authored by the
automation). A tip-only check (`git log -1`) would see an automation-authored
tip and wave the whole branch through, silently discarding the buried human
commit anyway. `_foreign_commits` therefore walks every commit in
`base_ref..branch_tip`, not just the last one.

WHAT "SAFE" MEANS FOR THE ESCAPE HATCH ITSELF
------------------------------------------------
A tripped guard does not just append a suffix and trust it: `decide_branch`
re-runs the same check against the suffixed candidate (`<branch>-<suffix>`,
then `-2`, `-3`, ... up to `max_attempts`) before handing it back, because
"the diversion target happens to be occupied by someone else's unrelated
work" is the same class of bug this module exists to close, just one name
over. `max_attempts` exists only to turn a pathological run into a bounded,
loud refusal (exit 2) instead of an unbounded loop -- reaching it in practice
would mean dozens of independently human-occupied branch names in a row,
which is not a shape this workflow's own history has ever produced.

WHEN THE GUARD ITSELF CANNOT ANSWER
-------------------------------------
`git ls-remote`/`git fetch` failing for a reason OTHER THAN "the branch does
not exist" (a network blip, an auth failure) is treated as UNKNOWN, never as
"assume safe" -- `BranchGuardError` propagates and the caller must not force-
push anything. Guessing "not found" from an ambiguous git failure is exactly
how a transient error would reintroduce tan-cli#1002 under a different name.

Exit codes (`main`)
--------------------
0  `--branch` carries no commit outside the automation identity (or does not
   exist yet on `origin`) -- safe to force-push as named.
1  `--branch` is protected; force-push the DIVERTED name in the `branch=`
   line this prints to `$GITHUB_OUTPUT` instead, and leave `--branch` alone.
2  refused: could not determine whether some candidate branch is safe (a git
   failure other than "ref does not exist"), or ran out of candidate names.

Both 0 and 1 also write `observed_tip=` -- the sha this guard actually
inspected on `origin/<branch>` (empty string if the branch did not exist
there yet). This exists so the caller's own `git push` can be made atomic
with the check, rather than merely advisory: `git checkout -B`/`add
-A`/`commit` all run between this guard's read and the push, so a commit
landing on `origin/<branch>` in that window is invisible to a bare `git push
-f` no matter how careful this guard was. Pass it straight through to `git
push --force-with-lease="${branch}":"${observed_tip}"` (the documented way to
require "still exactly this sha", or "still does not exist" for an empty
value) instead of `-f`.

`--check-branch BRANCH` (tan-cli#1119) is a SEPARATE mode that answers a
different question -- "does BRANCH carry a commit the automation did not
write, RIGHT NOW" -- for a caller that is not choosing a force-push target
at all (`planner-resync.yml`'s "Close superseded planner-resync proposals"
step, deciding per open PR whether its head branch is safe to close). It
does not force-push anything and writes no `$GITHUB_OUTPUT` keys of its own.
Exit codes in this mode: 0 = not occupied (safe to close -- whether the
branch does not exist at all, or exists and is genuinely clean; see
`BranchOccupancy` for why a caller that cares can still tell the two apart),
1 = occupied (protected; must not be closed), 2 = refused -- a `git`
failure this guard could not interpret, OR argparse's own `ap.error` for a
bad invocation (both exit via `SystemExit`/`return` with code 2; a caller
consuming this exit code must treat BOTH as "could not determine", not
assume 2 always means the former). Any exit code OTHER than 0 or 1 --
including one this module never deliberately produces, like a Python
interpreter crash or an OOM kill (137) -- must be treated by a caller the
same way: refused, not "assume clean". `--check-branch` is deliberately NOT
served by `--branch`/`decide_branch`: that function only
ever evaluates candidates ITS OWN cascade generates in THIS invocation
(`primary_branch`, `<primary_branch>-<divert_suffix>`, `-2`, ...) -- a
PREVIOUS run's diverted branch name is never one of those candidates, so a
caller that only consulted a `decide_branch` result (or, worse, a snapshot
of one computed earlier in the same run) can neither see a human's takeover
of that older name nor react to one that happens after the snapshot was
taken. Asking `--check-branch` fresh, immediately before the destructive
action, answers both: it is not limited to a fixed candidate set, and there
is no snapshot to go stale.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass

#: `git ls-remote --exit-code` reserves this code for "no matching refs" --
#: distinct from 0 (matched) and everything else (a real failure this module
#: must not interpret as "the branch does not exist").
_LS_REMOTE_NO_MATCH = 2


class BranchGuardError(RuntimeError):
    """The guard could not determine whether it is safe to force-push.

    Never caught to mean "assume safe" -- see the module docstring's "WHEN
    THE GUARD ITSELF CANNOT ANSWER".
    """


@dataclass(frozen=True)
class ForeignCommit:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    subject: str


@dataclass(frozen=True)
class OccupiedCandidate:
    """One candidate branch `decide_branch` tried and found occupied by a
    foreign commit before moving on to the next name.

    tan-cli#1006 review (minor): on a cascaded diversion, EVERY occupied
    candidate needs reporting, not just the first -- a reader told about only
    `candidates_tried[0]`'s foreign commit has no way to know
    `candidates_tried[1]` (say, a prior run's own diversion target) is also
    sitting on someone else's work. `protected` on `BranchDecision` keeps
    naming only the first (it is still "the finding", for the common
    single-diversion case); `occupied` below is the full list."""

    branch: str
    commit: ForeignCommit


@dataclass(frozen=True)
class BranchDecision:
    branch: str
    diverted: bool
    protected: ForeignCommit | None
    candidates_tried: tuple[str, ...]
    #: Every candidate in `candidates_tried` that was found occupied by a
    #: foreign commit (i.e. every entry except the last, which is always
    #: `branch` itself) paired with the commit that occupied it -- see
    #: `OccupiedCandidate`.
    occupied: tuple[OccupiedCandidate, ...]
    #: The sha this guard actually inspected on `origin/<branch>` at the
    #: moment it approved the force-push -- empty string when the branch did
    #: not exist there yet. Callers pass it to `git push
    #: --force-with-lease=<branch>:<observed_tip>` so the approval is
    #: atomic with the write: a commit landing on `origin/<branch>` in the
    #: window between this check and the push (this step also runs `git
    #: config` x2, `checkout -B`, `add -A`, `commit` first) is caught by
    #: `git push` itself rather than silently force-pushed away, closing the
    #: TOCTOU gap a bare `git push -f` leaves open.
    observed_tip: str


def _git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )


def _remote_branch_exists(root: pathlib.Path, branch: str) -> bool:
    proc = _git(root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}")
    if proc.returncode == 0:
        return True
    if proc.returncode == _LS_REMOTE_NO_MATCH:
        return False
    raise BranchGuardError(
        f"git ls-remote could not determine whether origin/{branch} exists "
        f"(exit {proc.returncode}): "
        + proc.stderr.decode("utf-8", "replace").strip()
    )


def _fetch_branch_tip(root: pathlib.Path, branch: str) -> str:
    """Fetch `branch`'s current tip from origin into a local tracking ref and
    return that ref's name.

    Callers must already have confirmed the branch exists
    (`_remote_branch_exists`) -- a failure here is therefore a real error,
    not "the branch is gone", and is raised rather than swallowed into a
    false "safe".
    """
    tracking_ref = f"refs/remotes/origin/{branch}"
    proc = _git(
        root, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{tracking_ref}"
    )
    if proc.returncode != 0:
        raise BranchGuardError(
            f"origin/{branch} exists but could not be fetched: "
            + proc.stderr.decode("utf-8", "replace").strip()
        )
    return tracking_ref


def _rev_parse(root: pathlib.Path, ref: str) -> str:
    proc = _git(root, "rev-parse", ref)
    if proc.returncode != 0:
        raise BranchGuardError(
            f"git rev-parse {ref} failed: "
            + proc.stderr.decode("utf-8", "replace").strip()
        )
    return proc.stdout.decode("utf-8", "replace").strip()


#: `%x1f` (unit separator) rather than a space or comma: a commit subject can
#: legitimately contain either.
_LOG_SEP = "\x1f"


def _identity_is_foreign(
    name: str, email: str, automation_name: str, automation_email: str
) -> bool:
    # tan-cli#1006 review (nit): name is compared case-SENSITIVELY, email
    # case-INSENSITIVELY -- deliberate, not an oversight. There is no
    # case-folding convention for a git commit's free-text display name, so
    # a name that differs only by case (`Alp-SDK Planner Re-Sync` vs this
    # module's own `alp-sdk planner re-sync`) is exactly as likely to be a
    # distinct human as a retyped automation identity, and this guard's bias
    # throughout is to treat ambiguity as foreign (over-protect) rather than
    # wave it through. Email addresses DO have a real case-folding
    # convention: the domain is case-insensitive by RFC 5321/5322, and
    # GitHub itself folds address case when matching a committer to an
    # account -- so a same-address, different-case email is the SAME
    # identity, not a distinct one, and comparing it case-sensitively would
    # risk misreading the automation's own commits as foreign if some git
    # client ever presented `noreply@alplab.ai` back with different casing.
    return name != automation_name or email.lower() != automation_email.lower()


def _foreign_commits(
    root: pathlib.Path,
    base_ref: str,
    tracking_ref: str,
    automation_name: str,
    automation_email: str,
) -> list[ForeignCommit]:
    """Every commit reachable from `tracking_ref` but not from `base_ref`
    whose AUTHOR pair is not the automation's own, OR whose COMMITTER pair
    is not -- see the module docstring's "THE SIGNAL" for why both halves
    are checked: `git commit --amend` and an `--autosquash` `fixup!` rebase
    both fold a human's work onto the automation's own commit while leaving
    its author identity untouched, changing only the committer.

    Scans the FULL range, not just the tip -- see the module docstring's "THE
    FULL RANGE IS SCANNED, NOT JUST THE TIP".
    """
    proc = _git(
        root,
        "log",
        f"--format=%H{_LOG_SEP}%an{_LOG_SEP}%ae{_LOG_SEP}%cn{_LOG_SEP}%ce{_LOG_SEP}%s",
        f"{base_ref}..{tracking_ref}",
    )
    if proc.returncode != 0:
        raise BranchGuardError(
            f"git log {base_ref}..{tracking_ref} failed: "
            + proc.stderr.decode("utf-8", "replace").strip()
        )
    foreign: list[ForeignCommit] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if not line:
            continue
        # tan-cli#1006 review (nit): `split(_LOG_SEP, 5)` (a bounded
        # maxsplit) trusts `_LOG_SEP` never appears INSIDE a field. Real git
        # porcelain guarantees that -- but a hand-forged commit object
        # (`git hash-object -t commit -w`) is not real porcelain, and one
        # with `\x1f` planted inside `%an` shifts every field after it
        # (driven: the forged author name reads as a truncated prefix, the
        # forged content spills into what should be the next field, and the
        # trailing remainder rides along inside `subject`) -- crafted right,
        # both identity pairs parse as the automation and this function
        # reports the commit as NOT foreign. A plain (unbounded) split turns
        # that shift into an extra field instead of a silent misparse: this
        # module's own trust boundary is "not cryptographic" (module
        # docstring, "THE SIGNAL"), but refusing an unparseable record is the
        # same fail-closed posture `BranchGuardError` already takes for a
        # `git log`/`ls-remote` failure -- guessing "probably fine" here
        # would be exactly the kind of ambiguity-read-as-safe this guard
        # exists to reject.
        fields = line.split(_LOG_SEP)
        if len(fields) != 6:
            raise BranchGuardError(
                f"a commit log entry in {base_ref}..{tracking_ref} did not "
                f"split into exactly 6 {_LOG_SEP!r}-separated fields (got "
                f"{len(fields)}) -- refusing to guess which field is which "
                f"rather than risk misreading a foreign identity as the "
                f"automation's own: {line!r}"
            )
        sha, author_name, author_email, committer_name, committer_email, subject = fields
        author_foreign = _identity_is_foreign(
            author_name, author_email, automation_name, automation_email
        )
        committer_foreign = _identity_is_foreign(
            committer_name, committer_email, automation_name, automation_email
        )
        if author_foreign or committer_foreign:
            foreign.append(
                ForeignCommit(
                    sha,
                    author_name,
                    author_email,
                    committer_name,
                    committer_email,
                    subject,
                )
            )
    return foreign


def decide_branch(
    root: pathlib.Path,
    base_ref: str,
    primary_branch: str,
    divert_suffix: str,
    automation_name: str,
    automation_email: str,
    max_attempts: int = 50,
) -> BranchDecision:
    """Pick the branch `auto/planner-resync` (or a diverted stand-in) should
    be force-pushed to.

    Tries `primary_branch` first -- the everyday, reused name; the common
    case (nothing to protect) costs zero extra branches. If it carries a
    commit the automation did not author, `primary_branch` is left
    completely untouched and `<primary_branch>-<divert_suffix>` is tried
    instead, then `-2`, `-3`, ... if even those turn out to be occupied by
    someone else's work (see the module docstring's "WHAT 'SAFE' MEANS FOR
    THE ESCAPE HATCH ITSELF").

    `protected`, when set, always names the FIRST foreign commit found on
    `primary_branch` -- the finding worth telling a human about, regardless
    of how many diversion attempts it took to land somewhere safe.
    """
    tried: list[str] = []
    occupied: list[OccupiedCandidate] = []
    protected: ForeignCommit | None = None
    candidate = primary_branch
    attempt = 0
    while True:
        attempt += 1
        if attempt > max_attempts:
            raise BranchGuardError(
                f"could not find a branch name safe to force-push after "
                f"{max_attempts} attempts starting from {primary_branch!r}: "
                f"{tried}"
            )
        tried.append(candidate)
        if not _remote_branch_exists(root, candidate):
            return BranchDecision(
                candidate,
                candidate != primary_branch,
                protected,
                tuple(tried),
                tuple(occupied),
                "",
            )
        tracking_ref = _fetch_branch_tip(root, candidate)
        foreign = _foreign_commits(
            root, base_ref, tracking_ref, automation_name, automation_email
        )
        if not foreign:
            return BranchDecision(
                candidate,
                candidate != primary_branch,
                protected,
                tuple(tried),
                tuple(occupied),
                _rev_parse(root, tracking_ref),
            )
        if protected is None:
            protected = foreign[0]
        occupied.append(OccupiedCandidate(candidate, foreign[0]))
        candidate = (
            f"{primary_branch}-{divert_suffix}"
            if attempt == 1
            else f"{primary_branch}-{divert_suffix}-{attempt}"
        )


@dataclass(frozen=True)
class BranchOccupancy:
    """What `branch_currently_occupied` observed about ONE branch.

    tan-cli#1119 review (minor): `existed` and `foreign` are reported
    separately on purpose. "The branch does not exist on `origin` at all"
    and "the branch exists and carries no commit outside the automation
    identity" are the SAME safe answer for `decide_branch`'s question ("is
    this candidate name safe to force-push to") -- a name nobody has ever
    used is exactly as safe as one that is genuinely clean. They are not the
    same OBSERVATION for the close decision `branch_currently_occupied`
    exists for, though: one means "I looked, and there is nothing there";
    the other means "I looked, and it is clean". Collapsing both into a
    single `None` (as an earlier version of this function did) left an
    operator reading the close step's log after a PR closed with no way to
    tell which happened. `occupied` is the one question most callers
    actually need to branch on.
    """

    existed: bool
    foreign: ForeignCommit | None

    @property
    def occupied(self) -> bool:
        return self.foreign is not None


def branch_currently_occupied(
    root: pathlib.Path,
    base_ref: str,
    branch: str,
    automation_name: str,
    automation_email: str,
) -> BranchOccupancy:
    """Ask this guard's own authorship test about an ARBITRARY existing
    branch (e.g. an open PR's head), right now, rather than trusting a list
    built earlier in the run.

    tan-cli#1119: `decide_branch` only ever answers for the handful of
    candidate names IT tries THIS run (`primary_branch`, then
    `<primary_branch>-<divert_suffix>`, then `-2`, ...) -- a PREVIOUS run's
    diverted branch name is never one of those candidates, so a human who
    adopts THAT branch (pushes a commit onto it between runs) is invisible
    to a caller that only consults `decide_branch`'s own `occupied` list.
    This function instead re-runs the identical `_foreign_commits` check
    against `branch` directly, at the moment it is called -- the same
    question `decide_branch` asks of each candidate it tries, just asked of
    a caller-supplied name instead of a name this module generated itself.
    Calling it immediately before a destructive action (rather than
    consulting a snapshot computed earlier in the run) also closes the
    TOCTOU window a snapshot leaves open: a branch adopted in the interval
    between an earlier snapshot and the destructive action is caught here,
    because there is no snapshot to go stale.

    Returns a `BranchOccupancy` -- `existed=False` if `branch` does not
    exist on `origin` at all (nothing to protect), `existed=True,
    foreign=None` if it exists and carries no commit outside the
    automation's own identity (genuinely clean), or `existed=True,
    foreign=<commit>` if it carries one. `.occupied` mirrors exactly what
    `decide_branch` treats as "safe to force-push to" for a candidate it
    tries itself (`not occupied` in both of the first two cases) while still
    letting a caller that cares -- like `--check-branch`'s own log line --
    tell the two apart.
    """
    if not _remote_branch_exists(root, branch):
        return BranchOccupancy(existed=False, foreign=None)
    tracking_ref = _fetch_branch_tip(root, branch)
    foreign = _foreign_commits(
        root, base_ref, tracking_ref, automation_name, automation_email
    )
    return BranchOccupancy(existed=True, foreign=foreign[0] if foreign else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Refuse to force-push auto/planner-resync over a "
        "commit the automation did not write."
    )
    ap.add_argument("--repo-root", required=True, type=pathlib.Path)
    ap.add_argument("--base-ref", default="dev")
    ap.add_argument("--branch", default="auto/planner-resync")
    ap.add_argument(
        "--divert-suffix",
        default=None,
        help="short alp-sdk sha used to name a diverted branch when "
        "--branch carries non-automation commits -- required unless "
        "--check-branch is given",
    )
    ap.add_argument("--automation-name", default="alp-sdk planner re-sync")
    ap.add_argument("--automation-email", default="noreply@alplab.ai")
    # tan-cli#1119: a second, independent mode -- ask whether ONE existing
    # branch (an open PR's head, say) carries a foreign commit RIGHT NOW,
    # rather than picking a branch to force-push to. See
    # `branch_currently_occupied`'s docstring for why this must be a live
    # per-branch question, not a snapshot `decide_branch` built earlier in
    # the run: `decide_branch`'s own candidate walk never tries a PREVIOUS
    # run's diverted name, so a caller that only ever consulted that walk's
    # `occupied` list would miss a human who adopted such a branch, and
    # would also be reading a stale snapshot rather than the live truth by
    # the time a caller acts on it (the TOCTOU window).
    ap.add_argument(
        "--check-branch",
        default=None,
        metavar="BRANCH",
        help="instead of deciding where to force-push, report (via exit "
        "code) whether BRANCH currently carries a commit the automation "
        "did not write; does not force-push or write force-push-related "
        "$GITHUB_OUTPUT keys",
    )
    args = ap.parse_args(argv)

    root = args.repo_root.resolve()

    if args.check_branch is not None:
        try:
            occupancy = branch_currently_occupied(
                root,
                args.base_ref,
                args.check_branch,
                args.automation_name,
                args.automation_email,
            )
        except BranchGuardError as exc:
            sys.stderr.write(f"planner_resync_branch_guard: REFUSED: {exc}\n")
            return 2
        commit = occupancy.foreign
        if commit is not None:
            identity = f"{commit.author_name} <{commit.author_email}>"
            if (commit.author_name, commit.author_email) != (
                commit.committer_name,
                commit.committer_email,
            ):
                identity += (
                    f", committed by {commit.committer_name} "
                    f"<{commit.committer_email}>"
                )
            sys.stdout.write(
                f"planner_resync_branch_guard: {args.check_branch!r} "
                f"carries {commit.sha[:8]} ({commit.subject!r}, by "
                f"{identity}), which the automation did not write -- "
                f"occupied.\n"
            )
            return 1
        # tan-cli#1119 review (minor): "never existed" and "exists and is
        # clean" are the same SAFE answer (`occupied` is False either way)
        # but not the same OBSERVATION -- see `BranchOccupancy`'s own
        # docstring. Reported distinctly so an operator reading this log
        # after a close can tell which happened.
        if occupancy.existed:
            sys.stdout.write(
                f"planner_resync_branch_guard: {args.check_branch!r} exists "
                f"and carries no commit outside the automation identity -- "
                f"clean, not occupied.\n"
            )
        else:
            sys.stdout.write(
                f"planner_resync_branch_guard: {args.check_branch!r} does "
                f"not exist on origin -- nothing to protect, not "
                f"occupied.\n"
            )
        return 0

    if args.divert_suffix is None:
        ap.error("--divert-suffix is required unless --check-branch is given")

    try:
        decision = decide_branch(
            root,
            args.base_ref,
            args.branch,
            args.divert_suffix,
            args.automation_name,
            args.automation_email,
        )
    except BranchGuardError as exc:
        sys.stderr.write(f"planner_resync_branch_guard: REFUSED: {exc}\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write("decision=refused\n")
        return 2

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"branch={decision.branch}\n")
            fh.write(f"diverted={'true' if decision.diverted else 'false'}\n")
            # Empty when `decision.branch` did not exist on origin at the
            # moment this guard checked it -- `git push
            # --force-with-lease=<branch>:` (empty <expect>) is the documented
            # way to say "the ref must not already exist" for that case.
            fh.write(f"observed_tip={decision.observed_tip}\n")
            # tan-cli#1006 review (minor): every candidate found occupied
            # on the way to `decision.branch`, not just the first -- a
            # cascaded diversion (this run's own primary AND its predecessor
            # run's diversion target both carrying a foreign commit) must
            # name all of them, not just `decision.protected`'s one. Numbered
            # keys (`occupied_N_*`), not a single delimited blob: the caller
            # reads $GITHUB_OUTPUT with plain `grep`/`cut` (no `jq` in this
            # step), same convention as every other key here.
            fh.write(f"occupied_count={len(decision.occupied)}\n")
            # tan-cli#1109 review round 2 (major): a SEPARATE consumer
            # (`planner-resync.yml`'s "Close superseded planner-resync
            # proposals" step) needs the full occupied set as a group -- to
            # exclude every one of them from what it closes, not to name any
            # one of them -- and reading N numbered keys back out means that
            # caller has to know N ahead of time, which it does not (`gh pr
            # list` finds it, not this guard). A single space-separated list
            # is safe here specifically: `occupied_{i}_branch` values are git
            # ref names, which cannot contain a space, so splitting on
            # whitespace never misparses one. Written UNCONDITIONALLY, empty
            # when there is nothing occupied -- same shape as `observed_tip`/
            # `occupied_count` already use, not a key a reader has to guard
            # for absence.
            fh.write(
                f"occupied_branches={' '.join(oc.branch for oc in decision.occupied)}\n"
            )
            for i, oc in enumerate(decision.occupied, start=1):
                fh.write(f"occupied_{i}_branch={oc.branch}\n")
                fh.write(f"occupied_{i}_commit={oc.commit.sha}\n")
                fh.write(f"occupied_{i}_subject={oc.commit.subject}\n")
                fh.write(
                    f"occupied_{i}_author={oc.commit.author_name} "
                    f"<{oc.commit.author_email}>\n"
                )
                fh.write(
                    f"occupied_{i}_committer={oc.commit.committer_name} "
                    f"<{oc.commit.committer_email}>\n"
                )
            # tan-cli#1006 review (blocker): written UNCONDITIONALLY, empty
            # when `decision.protected is None` -- same shape as
            # `observed_tip` above, not the `if decision.protected is not
            # None:` guard this used to sit behind. The workflow's own read
            # (`planner-resync.yml`'s "Open or refresh the proposal PR" step)
            # does `grep '^protected_commit=' "$GITHUB_OUTPUT" | tail -1 | cut
            # -d= -f2-` under `set -euo pipefail`; a KEY THAT IS ABSENT makes
            # `grep` exit 1, `pipefail` propagates that into the assignment,
            # and the step dies right there -- on every clean, non-diverted
            # run, which is the everyday case. Writing the key on every path
            # (empty string when there is nothing protected) makes that read
            # symmetric with `branch=`/`diverted=`/`observed_tip=`/
            # `occupied_count=`, which is what keeps THEM safe already, and
            # closes the whole class of "conditional key, unconditional grep"
            # rather than patching this one instance.
            #
            # A commit subject cannot contain a literal newline (`%s` is
            # one line by construction), so this is safe to write verbatim
            # into $GITHUB_OUTPUT without a heredoc delimiter.
            protected = decision.protected
            fh.write(f"protected_commit={protected.sha if protected else ''}\n")
            fh.write(
                "protected_commit_subject="
                f"{protected.subject if protected else ''}\n"
            )
            fh.write(
                "protected_commit_author="
                f"{f'{protected.author_name} <{protected.author_email}>' if protected else ''}\n"
            )
            # Reported separately from author (tan-cli#1006): an
            # `--amend`/`--autosquash fixup!` shape keeps the
            # automation's own author identity and only the committer is
            # foreign, so author alone can misleadingly point back at
            # the automation.
            fh.write(
                "protected_commit_committer="
                f"{f'{protected.committer_name} <{protected.committer_email}>' if protected else ''}\n"
            )

    if decision.diverted:
        assert decision.protected is not None
        protected = decision.protected
        # An `--amend`/`--autosquash fixup!` shape keeps the automation's
        # own author identity and changes only the committer, so naming
        # author alone would misleadingly read as "the automation wrote
        # this" -- name the committer too whenever it differs.
        identity = f"{protected.author_name} <{protected.author_email}>"
        if (protected.author_name, protected.author_email) != (
            protected.committer_name,
            protected.committer_email,
        ):
            identity += (
                f", committed by {protected.committer_name} "
                f"<{protected.committer_email}>"
            )
        sys.stderr.write(
            f"planner_resync_branch_guard: refusing to force-push "
            f"{args.branch!r} -- it carries {protected.sha[:8]} "
            f"({protected.subject!r}, by {identity}), which the automation "
            f"did not write. Diverting to {decision.branch!r} instead; "
            f"{args.branch!r} is left untouched.\n"
        )
        return 1

    sys.stdout.write(
        f"planner_resync_branch_guard: {decision.branch!r} carries no "
        f"commit outside the automation identity (or does not exist yet) "
        f"-- safe to force-push.\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
