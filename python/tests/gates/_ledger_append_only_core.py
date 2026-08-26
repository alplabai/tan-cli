# SPDX-License-Identifier: Apache-2.0
"""Pure git-plumbing core for the ledger append-only gate (tan-cli#906).

`MODULE_SIZE_BUDGET_LOG.md`'s own header says every entry only ever APPENDS
at the end of the file. Nothing checked that until this module:
`test_module_size_budget.py` parses the `.json` beside it and never reads the
ledger's prior content at all. Two incidents on the same day proved the gap:

* **tan-cli#902** -- resolving a merge conflict on the ledger with
  `git checkout --theirs` took `dev`'s side WHOLESALE, silently deleting the
  branch's own reasoned entry (restored by hand in `1a2c9970`).
* **tan-cli#878** -- a hand-written entry landed ABOVE the file's existing
  tail instead of after it (`git diff` showed `@@ -210,6 +210,8 @@`, writing
  into the same hunk region as the entry below it -- fixed by `1bd65407`,
  "move a misplaced ledger insertion to the tail").

`find_problems` (the only function `test_ledger_append_only.py` calls) closes
both, and is deliberately shaped so it is sound for the THIRD case that must
keep passing: a legitimate merge that interleaves two branches' entries by
date, resolved by keeping both sides.

## Why a single "is `dev`'s content a prefix" check is not enough

The naive form -- "the working file must start with whatever
`merge-base(HEAD, dev)` looked like" -- passes tan-cli#902 by construction:
once a merge commit is made, `dev`'s own tip becomes an ANCESTOR of the new
HEAD, so `merge-base(HEAD, dev)` collapses to `dev`'s tip itself. A wholesale
`--theirs` resolution's content is then trivially IDENTICAL to that "base",
and the very defect this gate exists to catch reads as a clean pass. Verified
against the real incident, not assumed: replaying tan-cli#902's actual merge
commit (`303ba5533`, parents `cec4a40a1` or "ours" and `6b5681b72` or
"theirs") through that naive form passes it silently, the same as the real
gate-less history did.

The fix is to treat a merge specially. When HEAD is itself a merge (or a
merge is in progress -- `MERGE_HEAD` resolves), the "base" for this check is
the merge-base of the merge's OWN parents, and each parent's *own* unique
tail (its content beyond that base) must still appear, in order, somewhere
after that base in the resolved file -- not necessarily contiguously with
respect to the OTHER parent's unique tail, which is what makes an interleaved
"keep both sides" resolution pass:

    base   = merge-base(parent_1, parent_2, ...)
    delta_i = parent_i's lines, with `base`'s lines removed from the front
    require: `base` is an exact PREFIX of the resolved file
    require: every delta_i is an order-preserving SUBSEQUENCE of whatever
             follows that prefix

Outside a merge (the common case -- an ordinary commit or a dirty working
tree on a feature branch), there is only one side to protect, and the check
reduces to the simple form: the ledger's content at `merge-base(HEAD, <the
dev integration branch>)` must be an exact prefix of the working file. This
is what catches tan-cli#878's mid-file insertion: whatever the file looked
like at the branch's own fork point from `dev` must not get anything spliced
before its own tail.

## Where "the base" comes from, and how this degrades

tan-cli#895 is the precedent this repo already has for the shape a gate must
take: a check that only fires in specially-provisioned CI arrives after human
review, so the goal is a check that fires on an ordinary local
`pytest tests -q`, at author time. `merge-base` needs no network and no bound
external checkout -- only the LOCAL git history and refs already in a normal
clone -- so this reads `dev`/`origin/dev` (falling back to `@{u}` and, when
set, `origin/$GITHUB_BASE_REF`) directly via subprocess `git`, the same way
`test_no_conflict_markers.py` shells `git ls-files`.

A developer on a detached checkout with none of those refs present cannot
have this determined -- and a gate that cannot determine its own base must
say so LOUDLY rather than pass silently, the exact `test_planner_relocation_
freshness.py` shape ("without ALP_SDK_ROOT the gate SKIPS, loudly, naming the
missing var -- never a silent pass"). `find_problems` returns a `Verdict`
whose `determinable` flag distinguishes "nothing to report" (a real pass --
the working tree really does contain everything it must) from "could not
check" (a `pytest.skip`, never folded into a pass).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

#: `python/tests/gates`, found the same cwd-independent way as
#: `_module_size_budget_core.PACKAGE` -- identical however pytest was
#: started.
REPO = Path(__file__).resolve().parents[3]

#: The one file this gate protects. Deliberately not a generic "any ledger"
#: framework (tan-cli#906 scopes this to the one file the incidents hit);
#: `find_problems` takes a path so the hermetic tests can point it at a
#: throwaway repo without touching the real one.
LEDGER_REL_PATH = "python/tests/gates/MODULE_SIZE_BUDGET_LOG.md"

#: The integration branch every PR lands against (see the `tan-cli` skill:
#: "work lands by PR into `dev`"). `origin/<name>` is tried before the bare
#: local branch, since a stale local `dev` that a developer forgot to pull is
#: a worse base than the remote-tracking ref, when both exist.
_DEV_BRANCH_NAME = "dev"


class Verdict(NamedTuple):
    """`find_problems`'s return.

    `determinable=False` means the base could not be established at all --
    the gate must `pytest.skip(skip_reason)`, never assert. `determinable=
    True, problems=[]` is a REAL pass: the comparison ran and found nothing
    wrong.
    """

    determinable: bool
    skip_reason: str | None
    problems: list[str]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _ref_exists(repo: Path, ref: str) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo).returncode == 0


def _resolve(repo: Path, ref: str) -> str | None:
    """`ref`'s commit sha, or `None` if `ref` does not resolve in `repo`."""
    result = _run_git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _candidate_dev_refs(repo: Path) -> list[str]:
    """Ordered, most-authoritative first. `GITHUB_BASE_REF` is set
    automatically by GitHub Actions on a `pull_request` event to the PR's
    base branch NAME (not a ref) -- naming it here costs nothing (it is only
    consulted, never required) and gives the one CI context that could ever
    know the true PR base a shot at the exact ref before falling back to the
    repo's own always-true convention that PRs land against `dev`."""
    candidates: list[str] = []
    base_env = os.environ.get("GITHUB_BASE_REF")
    if base_env:
        candidates.append(f"origin/{base_env}")
    candidates.append(f"origin/{_DEV_BRANCH_NAME}")
    candidates.append(_DEV_BRANCH_NAME)
    # The branch's own configured upstream, if any -- covers a fork whose
    # integration branch is not literally named `dev`/`origin/dev`, or a
    # worktree tracking something else on purpose.
    candidates.append("@{u}")
    return candidates


def _file_lines_at(repo: Path, ref: str, rel_path: str) -> list[str]:
    """The file's lines at `ref`, or `[]` if it does not exist there (a
    brand-new file has nothing in its history to preserve -- the whole thing
    is legitimately "new")."""
    result = _run_git(["show", f"{ref}:{rel_path}"], repo)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _working_tree_lines(repo: Path, rel_path: str) -> list[str]:
    """Read from disk, not from git -- this must catch an uncommitted,
    working-tree-only edit before it is ever committed, the same as every
    other gate in this file's neighbourhood reads its subject."""
    path = repo / rel_path
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, UnicodeDecodeError):
        return []


def _merge_base(repo: Path, refs: list[str]) -> str | None:
    """`git merge-base [--octopus]` across two or more refs, or `None` if it
    cannot be computed (refs unrelated, or history too shallow to find a
    common ancestor -- both are real, both mean "cannot determine", neither
    is this gate's business to distinguish from the caller's standpoint)."""
    if len(refs) < 2:
        return None
    args = ["merge-base"]
    if len(refs) > 2:
        args.append("--octopus")
    args.extend(refs)
    result = _run_git(args, repo)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _first_missing(needle: list[str], haystack: list[str]) -> str | None:
    """`None` if `needle` is an order-preserving subsequence of `haystack`
    (not necessarily contiguous -- this is what lets two branches' entries
    land interleaved by date and still pass); otherwise the first `needle`
    line that could not be matched, for a message that names what is
    actually missing rather than just failing."""
    hi = 0
    for line in needle:
        while hi < len(haystack) and haystack[hi] != line:
            hi += 1
        if hi == len(haystack):
            return line
        hi += 1
    return None


def _merge_parents(repo: Path) -> list[str]:
    """The commit shas this check must protect as separate sides:

    * an IN-PROGRESS merge (`MERGE_HEAD` resolves, conflict just resolved
      but not yet committed) -- `HEAD` plus every `MERGE_HEAD` (usually one;
      an octopus merge can have more, `git` exposes each via `.git/
      MERGE_HEAD` newline-separated, but `rev-parse MERGE_HEAD` only ever
      gives the first -- rare enough in practice that this repo's own
      history (47 merge commits on `dev`, all two-parent) does not warrant
      more than the common two-parent shape).
    * an ALREADY-COMMITTED merge at HEAD (`HEAD^2` resolves) -- every parent,
      via `HEAD^1`, `HEAD^2`, ... until one stops resolving.

    Empty when HEAD is an ordinary, single-parent commit -- the caller falls
    back to the single-base form in that case."""
    merge_head = _resolve(repo, "MERGE_HEAD")
    if merge_head is not None:
        head = _resolve(repo, "HEAD")
        return [head, merge_head] if head else []

    parents: list[str] = []
    i = 1
    while True:
        parent = _resolve(repo, f"HEAD^{i}")
        if parent is None:
            break
        parents.append(parent)
        i += 1
    return parents if len(parents) >= 2 else []


def _check_single_base(repo: Path, rel_path: str, base_ref: str) -> Verdict:
    ancestor = _merge_base(repo, [_resolve(repo, "HEAD") or "HEAD", base_ref])
    if ancestor is None:
        return Verdict(
            False,
            f"could not compute a merge-base between HEAD and {base_ref!r} -- "
            "history too shallow, or the two share no common ancestor",
            [],
        )
    base_lines = _file_lines_at(repo, ancestor, rel_path)
    current_lines = _working_tree_lines(repo, rel_path)
    if current_lines[: len(base_lines)] != base_lines:
        return Verdict(
            True,
            None,
            [
                f"{rel_path} is not an append-only extension of its content at "
                f"merge-base(HEAD, {base_ref}) ({ancestor[:12]}) -- something "
                "before the file's tail was edited, reordered, or deleted "
                "instead of appended after it"
            ],
        )
    return Verdict(True, None, [])


def _check_merge_parents(repo: Path, rel_path: str, parents: list[str]) -> Verdict:
    ancestor = _merge_base(repo, parents)
    if ancestor is None:
        return Verdict(
            False,
            f"could not compute a merge-base across the {len(parents)} merge "
            f"parents ({', '.join(p[:12] for p in parents)}) -- history too "
            "shallow, or they share no common ancestor",
            [],
        )
    base_lines = _file_lines_at(repo, ancestor, rel_path)
    current_lines = _working_tree_lines(repo, rel_path)
    problems: list[str] = []

    if current_lines[: len(base_lines)] != base_lines:
        problems.append(
            f"{rel_path} is not an append-only extension of its content at the "
            f"merge-base of this merge's parents ({ancestor[:12]}) -- the "
            "resolution must keep every line from before the merge, in order, "
            "before any new content"
        )
        return Verdict(True, None, problems)

    remainder = current_lines[len(base_lines) :]
    for parent in parents:
        parent_lines = _file_lines_at(repo, parent, rel_path)
        if parent_lines[: len(base_lines)] != base_lines:
            # That parent's own history already violates append-only against
            # the true common ancestor -- not this merge's doing, but still
            # worth a clear message rather than a confusing missing-line
            # report below.
            problems.append(
                f"{rel_path} at {parent[:12]} is not itself an append-only "
                f"extension of the merge-base ({ancestor[:12]}) -- cannot "
                "verify this side's contribution survived the merge"
            )
            continue
        delta = parent_lines[len(base_lines) :]
        missing = _first_missing(delta, remainder)
        if missing is not None:
            problems.append(
                f"{rel_path}: a line unique to {parent[:12]} did not survive "
                f"this merge -- first missing: {missing!r}. This is the "
                "tan-cli#902 shape: a wholesale `git checkout --theirs`/"
                "`--ours` resolution silently drops one side's own entry "
                "instead of keeping both."
            )
    return Verdict(True, None, problems)


def find_problems(repo: Path, rel_path: str = LEDGER_REL_PATH) -> Verdict:
    """The one entry point `test_ledger_append_only.py` calls.

    Dispatches on whether HEAD is (part of) a merge: two or more parents
    (in progress or already committed) get the multi-sided check that
    protects each side's own unique tail; anything else gets the simple
    single-base check against the dev integration branch."""
    parents = _merge_parents(repo)
    if parents:
        return _check_merge_parents(repo, rel_path, parents)

    for ref in _candidate_dev_refs(repo):
        if _ref_exists(repo, ref):
            verdict = _check_single_base(repo, rel_path, ref)
            if verdict.determinable:
                return verdict
            # That candidate resolved as a ref but merge-base still failed
            # (e.g. unrelated histories) -- fall through to the next one
            # rather than giving up on the first attempt.

    return Verdict(
        False,
        "no usable base ref found (tried origin/$GITHUB_BASE_REF, "
        f"origin/{_DEV_BRANCH_NAME}, {_DEV_BRANCH_NAME}, @{{u}}) -- likely a "
        "detached checkout or a clone with no dev integration branch fetched; "
        "cannot verify the ledger's append-only property without a base to "
        "compare against",
        [],
    )
