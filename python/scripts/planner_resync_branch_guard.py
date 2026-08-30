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

THE SIGNAL: (author name, author email), not "did the diff move"
------------------------------------------------------------------
The workflow sets a fixed identity before it ever commits
(`git config user.name "alp-sdk planner re-sync"` /
`user.email "noreply@alplab.ai"`, `planner-resync.yml`'s "Open or refresh the
proposal PR" step). Any commit reachable from the branch tip but not from
`dev` whose author is NOT that exact (name, email) pair is, by construction,
something a human (or a different tool) added on top of an automated
proposal -- and is protected. This is not cryptographic: anyone with push
access could set `user.email` to the same string locally and defeat the
check. It closes the actual failure mode observed on PR #996 (an ordinary
human commit under the committer's own identity), not a deliberately hostile
one -- the same trust boundary this workflow's own `GITHUB_TOKEN` already
sits inside.

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
    subject: str


@dataclass(frozen=True)
class BranchDecision:
    branch: str
    diverted: bool
    protected: ForeignCommit | None
    candidates_tried: tuple[str, ...]


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


#: `%x1f` (unit separator) rather than a space or comma: a commit subject can
#: legitimately contain either.
_LOG_SEP = "\x1f"


def _foreign_commits(
    root: pathlib.Path,
    base_ref: str,
    tracking_ref: str,
    automation_name: str,
    automation_email: str,
) -> list[ForeignCommit]:
    """Every commit reachable from `tracking_ref` but not from `base_ref`
    whose (author name, author email) pair is not the automation's own.

    Scans the FULL range, not just the tip -- see the module docstring's "THE
    FULL RANGE IS SCANNED, NOT JUST THE TIP".
    """
    proc = _git(
        root,
        "log",
        f"--format=%H{_LOG_SEP}%an{_LOG_SEP}%ae{_LOG_SEP}%s",
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
        sha, name, email, subject = line.split(_LOG_SEP, 3)
        if name != automation_name or email.lower() != automation_email.lower():
            foreign.append(ForeignCommit(sha, name, email, subject))
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
                candidate, candidate != primary_branch, protected, tuple(tried)
            )
        tracking_ref = _fetch_branch_tip(root, candidate)
        foreign = _foreign_commits(
            root, base_ref, tracking_ref, automation_name, automation_email
        )
        if not foreign:
            return BranchDecision(
                candidate, candidate != primary_branch, protected, tuple(tried)
            )
        if protected is None:
            protected = foreign[0]
        candidate = (
            f"{primary_branch}-{divert_suffix}"
            if attempt == 1
            else f"{primary_branch}-{divert_suffix}-{attempt}"
        )


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
        required=True,
        help="short alp-sdk sha used to name a diverted branch when "
        "--branch carries non-automation commits",
    )
    ap.add_argument("--automation-name", default="alp-sdk planner re-sync")
    ap.add_argument("--automation-email", default="noreply@alplab.ai")
    args = ap.parse_args(argv)

    root = args.repo_root.resolve()
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
            if decision.protected is not None:
                # A commit subject cannot contain a literal newline (`%s` is
                # one line by construction), so this is safe to write verbatim
                # into $GITHUB_OUTPUT without a heredoc delimiter.
                fh.write(f"protected_commit={decision.protected.sha}\n")
                fh.write(
                    f"protected_commit_subject={decision.protected.subject}\n"
                )
                fh.write(
                    "protected_commit_author="
                    f"{decision.protected.author_name} "
                    f"<{decision.protected.author_email}>\n"
                )

    if decision.diverted:
        assert decision.protected is not None
        sys.stderr.write(
            f"planner_resync_branch_guard: refusing to force-push "
            f"{args.branch!r} -- it carries {decision.protected.sha[:8]} "
            f"({decision.protected.subject!r}, by "
            f"{decision.protected.author_name} "
            f"<{decision.protected.author_email}>), which the automation "
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
