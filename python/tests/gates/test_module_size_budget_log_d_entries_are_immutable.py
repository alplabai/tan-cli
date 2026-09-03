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
commit reachable from here" -- so the check is a walk of `git log
--name-status` over `MODULE_SIZE_BUDGET_LOG.d/`, cross-checked against both
`git ls-tree`'s listing of HEAD's own tree AND a blob compare of every
entry-shaped path in that tree against BOTH the commit that introduced it
and the commit that recorded its add (two anchors that catch different
things -- see `entry_violations`'s own docstring for
why the name-status walk alone is not enough: a merge commit that drops one
side's already-added entry while resolving a conflict emits no diff record
for that path at all, and neither does a merge commit that instead REWRITES
the entry's content, nor one that INTRODUCES it -- `git log --name-status`
with none of `-m`/`-c`/`--cc` given never emits a diff for a merge commit,
period, so the tree-membership check and the blob compare are each
load-bearing for a different half of "a merge commit touched this path",
not redundant with each other). Anchoring one blob compare on HEAD's tree
rather than on the walk's add-set is tan-cli#1065: an entry whose only
introducing commit is a merge has no "A" record for the add-set to hold, so
an add-set-driven compare never looked at it at all. All of these checks are
entirely self-contained, with the same answer locally and in CI, on a
`pull_request` run, a `merge_group` run, or a bare `push`. There
is no base-ref resolution to get wrong, so there is no equivalent gap: a
same-branch rewrite of a file that branch itself just added is caught the
same way a same-branch rewrite of a line already on `dev` is -- both are
just "a later commit changed a path that already existed" -- and that is
deliberately a STRICTER promise than the old file made for its own
within-branch edits. A per-entry file is meant to be finished the moment it
is written; nothing about this ledger's purpose argues for tolerating a
self-correction the way free-form prose might. (A squash-merge that drops
an entry before it is ever committed to this branch's history is a
different, NOT-covered gap, as is a merge-introduced entry that a later
merge drops and nothing ever re-adds -- see "What is deliberately excluded"
below and `entry_violations`'s own docstring for why no git-log-based walk
can close either of them.)

## What is deliberately excluded

`MODULE_SIZE_BUDGET_LOG.d/README.md` is not an entry (`_module_size_budget_
core.LOG_DIR`'s own README, not something `regen_module_size_budget.py`
writes) and is free to be edited like any other doc file -- `_entry_path`
below only matches the `<date>-<8 hex chars>.md` shape the script actually
generates, so README.md (and anything else that does not match) is never
even considered.

Two shapes are also excluded, but not by a filter the way README.md is --
excluded because there is nothing here to check against. `entry_violations`
has exactly two inputs, the walk's own records and HEAD's tree, and each of
these appears in NEITHER:

1. A squash-merge that never actually commits the entry to this branch's
   history. If a feature branch adds an entry and the merge into this branch
   is a GitHub "Squash and merge" (`dev`'s own real merge strategy) that
   omits that file from the squash commit's diff, this branch's history
   never contains a commit that added the path at all -- squash discards the
   branch's own per-commit history -- and the path is not in HEAD's tree
   either. (An ordinary squash-merge that DOES carry the entry is fully
   covered, and always was.)

2. An entry a merge commit INTRODUCED, a later merge commit then DROPPED,
   and nothing ever re-added (tan-cli#1065). No merge emits a `--name-status`
   record, so there is no "A" to find, and the drop leaves nothing in HEAD's
   tree. The "never re-added" clause is load-bearing: a re-add puts the path
   back in HEAD's tree, where it is measured against the merge that
   introduced it -- that recovery shape is CAUGHT, and was the one finding
   of tan-cli#1065's own review.

Both are real, unclosed gaps in what a git-log walk can see -- not design
choices this file is making on purpose the way the README.md filter is. The
full reasoning for each (and why no addition to the walk closes either) is
written out ONCE, in `entry_violations`'s own docstring, rather than
restated here.
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


def _blob_oid(cwd: Path, rev: str, path: str) -> str | None:
    """The blob sha `path` resolves to at `rev`, or `None` when `rev`'s tree
    does not contain `path` at all -- a legitimate answer here (the commit
    predates the entry), not an error."""
    result = _git_ok("rev-parse", f"{rev}:{path}", cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _introducing_commit(cwd: Path, path: str) -> tuple[str, str] | None:
    """`(commit, blob sha)` for the earliest commit reachable from HEAD whose
    own tree actually contains `path` -- i.e. the commit that INTRODUCED it,
    resolved without needing a `--name-status` "A" record for it (tan-cli#1065:
    a merge commit introducing an entry emits no such record, so
    `entry_violations`' walk never sees one). `None` only when no reachable
    commit contains the path, which cannot happen for a path read out of
    HEAD's own tree and which the caller therefore treats as a hard git
    failure rather than a pass.

    `--reverse` puts the oldest listed commit first; the loop still checks
    tree membership rather than trusting the first line, so a history whose
    oldest listed commit is a delete (an add/drop/re-add cycle) resolves to
    the right commit instead of raising.

    `--full-history` buys exactly one thing here, and it is NOT keeping the
    introducing merge in the listing (tan-cli#1144). Measured on raw git
    2.43.0, against the shape `_entry_introduced_by_a_merge_commit` builds
    and again against tan-cli#1070's own introduce-then-rewrite shape, both
    walks list the same commits:

        introducing merge alone   --full-history: 823bf29a
                                  default       : 823bf29a
        + rewritten by a 2nd merge --full-history: 823bf29a cfd155ae
                                  default       : 823bf29a cfd155ae

    DEFAULT simplification already keeps a merge that is TREESAME to NO
    parent, which a merge introducing a path present in neither parent
    always is. So the listing half is free -- in every shape this file
    constructs, including the one tan-cli#1070 was filed for. An earlier
    version of this docstring claimed the flag was what got that merge
    listed; it is not, and a reader who believes it will delete the flag and
    measure no difference on the shapes nearest to hand.

    What is NOT free is REACHABILITY, and it is the whole of what the flag
    does here. Default simplification follows only ONE parent of a merge it
    IS TREESAME to. A later merge that drops the path is TREESAME to any
    parent whose tree also lacks it, so that one parent alone is followed and
    the entire side the introducing merge lives on is pruned out of the walk
    before it can be listed at all. `_introducing_commit` then resolves the
    "introduction" to whatever ordinary commit re-added the path, whose blob
    IS HEAD's own, and `entry_violations`' check 4 reads a rewritten entry
    clean. `--full-history` follows every parent regardless.

    Deleting it here left the ENTIRE gates suite green -- `1142 passed, 34
    skipped` on tan-cli `1fc18bb1`, Python 3.12.3, git 2.43.0 -- until
    `test_a_merge_introduced_entry_is_caught_even_when_default_simplification_would_prune_the_introducing_merge`
    was added to pin it. That test is this argument's only guard, so do not
    weaken it; its helper reaches the pruning condition by forking the
    dropping merge's other side BEFORE the entry existed, which is one
    sufficient way for that parent's tree to lack the path, not the only one.
    """
    result = _git_ok(
        "rev-list", "--full-history", "--reverse", "HEAD", "--", path, cwd=cwd
    )
    if result.returncode != 0:
        raise GitCommandFailed(
            f"git rev-list --full-history -- {path} failed in {cwd}: "
            f"{result.stderr!r}"
        )
    for commit in result.stdout.splitlines():
        oid = _blob_oid(cwd, commit, path)
        if oid is not None:
            return commit, oid
    return None


def entry_violations(cwd: Path, dir_rel: str) -> dict[str, list[str]]:
    """path -> what is wrong with it -- empty when every entry under
    `dir_rel` was added exactly once (or introduced by a merge commit, which
    records nothing), is still present in HEAD's own tree, and still holds
    byte-for-byte the content it was introduced with.

    Uses `--name-status` (not `--follow`, not `-M` rename detection): an
    entry is content-addressed by nothing but its own filename, and this
    gate's promise is about that PATH specifically, not about the flow of
    similar-looking content across paths. `git log`'s default DOES detect
    renames for `--name-status` in newer versions on some configs; forcing
    `--no-renames` keeps this deterministic across environments and reads
    a rename as exactly what it is here -- a delete of one path plus an add
    of another, i.e. two violations, not a clean move.

    The `git log --name-status` walk alone is NOT enough, even with
    `--full-history` added below. `git log`'s DEFAULT history simplification
    is two separate mechanisms and both bite here. First, a merge commit's
    diff for a path-limited `--name-status` walk is empty whenever the merge
    is TREESAME to a parent for that path -- which a dropped-during-resolution
    entry always is, since the merge's tree then equals the surviving
    parent's tree for that path -- so the merge itself never produces a `D`
    record no matter what. Second, and worse, DEFAULT simplification does not
    just skip that commit's own diff, it can rewrite the walked DAG to treat
    the merge as a pass-through of the TREESAME parent, which prunes the
    OTHER parent's lineage -- including the commit that originally ADDED the
    now-dropped path -- out of the walk entirely. `--full-history` defeats
    the second mechanism (the "A" record survives), but not the first (the
    merge still records no "D") -- so a bare record-count check
    (`len(seen) == 1 and seen[0].startswith("A ")`) still reads the dropped
    path as untouched even with `--full-history` on. Measured directly
    against this function, both ways: merge two branches that each add a
    different entry, then resolve as if by conflict (amend the merge commit
    to drop one side's file) -- `entry_violations` returned `{}` for the
    dropped path with neither `--full-history` alone nor the original walk,
    even though the path no longer exists in HEAD.

    So every path this function ever saw an "A" record for (which needs
    `--full-history` to survive the DAG-rewrite above) is cross-checked
    against `git ls-tree`'s listing of `HEAD`'s own tree: an added path that
    is not part of HEAD's tree is a violation regardless of what the
    name-status walk did or didn't record for it. Neither half alone closes
    the gap -- `--full-history` without the tree check still misses the
    merge's missing "D"; the tree check without `--full-history` never even
    sees the "A" to cross-check against -- only the pair does. This tree
    check runs for every path missing from HEAD's tree, including one check
    1 already flagged for an unrelated reason (an ordinary modify, say,
    later dropped by a separate merge) -- it APPENDS the "missing from
    HEAD's tree" reason to that path's existing record list rather than
    skipping it, so a caller reading `violations[path]` sees every reason a
    path is broken, not just whichever check happened to notice first
    (tan-cli#1099 review: an earlier version of this loop skipped a path
    already in `violations`, so a path that was both modified and later
    merge-dropped carried only the modify's records and looked, from the
    outside, like it was never removed at all).

    Path membership is still not the whole promise, though: a merge commit
    can rewrite an already-added entry's *content* while leaving its path in
    place, and that is invisible to both halves above -- the path never
    leaves HEAD's tree, so the tree cross-check has nothing to flag, and
    `git log --name-status` with none of `-m`/`-c`/`--cc` given emits NO diff
    record for a merge commit at all, full stop, regardless of TREESAME --
    measured directly: two branches that diverge without either one ever
    touching the entry again after the base commit that added it, merged,
    then the merge commit amended to a THIRD content matching neither
    parent, produces a `git log --full-history --name-status` walk with only
    the original "A" record for that path -- the merge commit's own header
    line appears (so `--full-history` did keep it in the walk) but carries
    no diff body whatsoever, for either parent. So a merge is the one commit
    shape whose content mutation the name-status walk can NEVER see, by
    design of the flag itself, not as a corner case of TREESAME pruning.
    Closing this needs an actual content compare, not another git-log flag:
    for every path whose name-status + tree-membership history reads clean
    (exactly one "A" record, still present in HEAD's tree), the blob at
    `<add-commit>:<path>` is compared against the blob at `HEAD:<path>` via
    `git rev-parse`; a mismatch is flagged even though nothing upstream of it
    recorded a modify. This is the only one of the three checks that opens a
    subprocess per surviving path rather than one call for the whole
    directory -- deliberate, since `git log`/`git ls-tree` have no verb for
    "diff an arbitrary pair of revisions for a single path" cheaper than
    `rev-parse`'s O(1) blob lookup at each of the two revisions.

    That blob compare originally ran only for paths the walk had seen an
    "A" record for, which left one more door into the same "a merge commit
    emits no --name-status record" root cause open (tan-cli#1065): an entry
    whose ONLY introducing commit is a MERGE commit has no "A" record
    anywhere -- the introducing merge emits no diff for it, exactly like the
    dropping and rewriting merges above -- so the path never enters
    `statuses` at all and none of the three checks so far ever looks at it.
    Measured directly in a throwaway repo, both halves of the pair:

        entry introduced by a merge commit, then rewritten by a second merge
          tree_has_entry = True
          violations     = {}          <- not caught
        same shape, but followed by a NORMAL delete
          tree_has_entry = False
          violations     = {'LOG.d/2026-08-27-22222222.md': ['D at 8d659323']}

    -- the second row being caught only because a NON-merge commit does emit
    its "D" record. So a FOURTH check is anchored on HEAD's own TREE rather
    than on the walk's records: every entry-shaped path present at HEAD is
    compared against the content the commit that INTRODUCED it gave it,
    where the introducing commit is resolved by `_introducing_commit` -- the
    earliest commit reachable from HEAD whose tree actually contains the
    path, which for a merge-introduced entry IS the introducing merge
    (`git rev-list --full-history` does list a merge that is TREESAME to no
    parent for the path, and a merge that introduces a path present in
    neither parent is by definition TREESAME to neither).

    That pass runs for EVERY entry path in HEAD's tree, not only the
    record-less ones. Restricting it to record-less paths is what tan-cli#1065's
    own review found still open, one door further along: compose the two
    merge shapes with the obvious human recovery -- a merge INTRODUCES the
    entry (no record), a later merge DROPS it (no record), an ordinary commit
    RE-ADDS it with different content -- and the path arrives with exactly
    one "A" record and IS in HEAD's tree, so the record-anchored check 3
    compares it against the RE-ADD instead of against the introduction and
    the loss goes unseen. Measured against this function before the widening,
    verbatim: `walk records: ['A\tLOG.d/2026-08-27-22222222.md']`,
    `in HEAD tree: True`, `content at HEAD: - REWRITTEN content Y`,
    `VIOLATIONS: {}` -- with `- ORIGINAL content X` gone.

    Checks 3 and 4 are two different ANCHORS for the same content promise,
    and neither subsumes the other -- also measured, not argued. Check 4
    anchors on the introduction and so catches the compose-three-steps shape
    above, which check 3 misses. Check 3 anchors on the recorded add and
    catches its mirror image, which check 4 misses: a merge introduces X, a
    merge drops it, an ordinary commit re-adds Z, and a THIRD merge rewrites
    Z back to X. There the walk records a lone "A" (content Z) while
    `_introducing_commit` and HEAD both read X, so check 4 sees nothing and
    check 3 flags `content at HEAD (dbf1d1f4) differs from content at the add
    commit (aab7d83f)` -- content Z was committed and silently rewritten.
    `test_the_add_commit_anchor_still_catches_what_the_introduction_anchor_
    cannot` pins that, so check 3 is not dead code behind the widening.

    None of this can fire on the ordinary squash-merge shape `dev` actually
    uses -- the thing tan-cli#1065's scope note is emphatic it must not be
    turned into a false-positive machine. A squash commit is an ordinary
    SINGLE-parent commit: it emits a normal "A" record for every entry it
    carries, and it is itself the earliest commit on this branch containing
    that path, so BOTH anchors resolve to the same commit and agree. What a
    widened pass cannot do is invent a disagreement where the content never
    moved. Proven hermetically, not argued, by
    `test_a_squash_merged_entry_passes_clean` (which asserts the
    single-parent shape and the "A" record directly, not just the clean
    result), with
    `test_an_entry_introduced_by_a_merge_commit_and_never_touched_again_
    passes_clean` as the other negative half.

    Squash-merges are a DIFFERENT, unfixable-by-any-git-log-walk gap, called
    out here rather than left implicit: if a branch adds an entry and the
    merge into the branch this gate walks is a GitHub "Squash and merge"
    (the strategy `dev` actually uses) that never staged that file into the
    squash commit -- or, equivalently, `git merge --squash` locally with that
    path unstaged before the commit -- the receiving branch's history has NO
    commit, anywhere, that ever added the path: squash collapses the whole
    branch into one new commit with no parent link to the original per-file
    commits, so there is no "A" record to find and nothing under HEAD's tree
    to be missing. Every mechanism above (`--full-history`, the tree
    cross-check, the blob compare added here) depends on the ADDING commit
    being reachable from HEAD; a squash-merge that drops a file makes that
    commit not exist in this history at all, which is a strictly different,
    more fundamental gap than a merge that keeps the path but changes or
    loses its content. There is no git-log-based fix for it -- the
    information needed (what the feature branch's own per-commit history
    looked like) is exactly what squash-merge discards on purpose. This is a
    known, accepted limitation of a purely git-history-based check, not an
    oversight: catching it would need reviewing the squash's staged diff
    BEFORE the commit is made (a PR-time check, not a post-hoc history walk),
    which is out of scope for a gate that -- by design (see "Why this needs
    no PR/merge-queue base ref at all" above) -- only ever looks at commits
    already reachable from HEAD.

    One narrower relative of that gap survives both anchors and is excluded
    for the same structural reason, named here rather than left implicit: an
    entry that a merge commit INTRODUCED, that a later merge commit then
    DROPPED, and that nothing ever re-added. It has no "A" record (the
    introducing merge emits none, and the dropping merge emits none either)
    and it is not in HEAD's tree, so it appears in NEITHER of this function's
    two inputs -- there is no path for it to check, exactly as for the squash
    case. Note the "never re-added" clause is load-bearing: if any commit
    re-adds the path, it is back in HEAD's tree and check 4 measures it
    against the introducing merge again. Closing the never-re-added variant
    would mean enumerating every tree in the history rather than walking
    records plus HEAD's tree, a materially more expensive check for a shape
    that needs two separate conflict-resolution amends and no recovery.
    """
    result = _git_ok(
        "log",
        "--reverse",
        "--full-history",
        "--no-renames",
        "--name-status",
        "--pretty=format:%x01%H",
        "--",
        dir_rel,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise GitCommandFailed(
            f"git log --full-history --name-status -- {dir_rel} failed in "
            f"{cwd}: {result.stderr!r}"
        )

    # Each record keeps its raw (status, commit) pair, not just a formatted
    # string, so a later pass can still `git rev-parse` against the exact
    # add-commit for a path that survives the first two checks below.
    statuses: dict[str, list[tuple[str, str]]] = {}
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
        assert current_commit is not None
        statuses.setdefault(path, []).append((status, current_commit))

    violations: dict[str, list[str]] = {}
    for path, seen in statuses.items():
        # Exactly one "A"-status record is the only clean history. Anything
        # else -- a second record of any kind (a re-add after delete, a
        # modify, ...), or a first record that is not "A" at all (should be
        # unreachable given git's own model, guarded anyway rather than
        # assumed) -- is a violation.
        if len(seen) != 1 or seen[0][0] != "A":
            violations[path] = [f"{status} at {commit}" for status, commit in seen]

    tree_result = _git_ok(
        "ls-tree", "-r", "--name-only", "HEAD", "--", dir_rel, cwd=cwd
    )
    if tree_result.returncode != 0:
        raise GitCommandFailed(
            f"git ls-tree -r --name-only HEAD -- {dir_rel} failed in {cwd}: "
            f"{tree_result.stderr!r}"
        )
    present = set(tree_result.stdout.splitlines())
    # tan-cli#1099 review round 3: this string used to say "despite an add
    # record (dropped during a merge)", which was true only of the ONE shape
    # this check was originally written for -- a single "A" record, then a
    # merge silently drops the path with no diff of its own. Once this loop
    # started appending to a path check 1 already flagged (below), it also
    # started reaching two shapes where that wording is FALSE: a plain `git
    # rm` (an ordinary, non-merge commit records an explicit "D" -- there is
    # no merge here at all), and a merge-introduced entry an ordinary commit
    # later modifies with no "A" record ever recorded for it (there is no
    # add record to be "despite"). The string below claims only what is true
    # in all three: the path has SOME recorded history for it (an "A", "M",
    # or "D" record already sits in `violations[path]` or is about to), and
    # it is, right now, missing from HEAD's tree. It does not claim HOW it
    # left, or that an "A" record exists -- `_is_delete_shaped` only ever
    # matches the `"missing from HEAD's tree"` prefix, so this rewording
    # changes nothing mechanically (re-proved by this file's classifier
    # tests, not assumed).
    _MISSING_FROM_TREE = (
        "missing from HEAD's tree despite this path's own recorded history "
        "showing it as tracked at some point"
    )
    for path, seen in statuses.items():
        if path in present:
            continue
        # Every path here was matched by `_is_entry_path` and has at least
        # one recorded status, yet is absent from HEAD's own tree. Flag it
        # the same way a directly-observed delete is flagged -- and APPEND,
        # not skip, when check 1 already flagged this path for an unrelated
        # reason (an ordinary modify, say): a path can be both modified AND
        # later dropped, and a caller reading `violations[path]` needs the
        # removal signal too, not just whichever check happened to run first
        # (tan-cli#1099 review: an add/modify/merge-drop history read as
        # MODIFY-only because this loop used to `continue` on `path in
        # violations` before ever looking at tree membership, so the one
        # signal that says "this was removed" never reached a path check 1
        # had already touched).
        if path in violations:
            if _MISSING_FROM_TREE not in violations[path]:
                violations[path].append(_MISSING_FROM_TREE)
            continue
        violations[path] = [
            f"{status} at {commit}" for status, commit in seen
        ] + [_MISSING_FROM_TREE]

    # Third check: a path can pass BOTH checks above (exactly one "A"
    # record, still present in HEAD's tree) and still have had its content
    # rewritten by a merge commit -- `git log --name-status` never records a
    # diff for a merge commit at all without `-m`/`-c`/`--cc` (see the
    # docstring above), so a merge that changes an already-added entry's
    # content leaves no trace for either of the first two checks to catch.
    # Close it directly: compare the blob actually at the add-commit against
    # the blob at HEAD for every path that made it this far clean.
    for path, seen in statuses.items():
        if path in violations:
            continue
        add_status, add_commit = seen[0]
        add_oid_result = _git_ok("rev-parse", f"{add_commit}:{path}", cwd=cwd)
        if add_oid_result.returncode != 0:
            raise GitCommandFailed(
                f"git rev-parse {add_commit}:{path} failed in {cwd}: "
                f"{add_oid_result.stderr!r}"
            )
        head_oid_result = _git_ok("rev-parse", f"HEAD:{path}", cwd=cwd)
        if head_oid_result.returncode != 0:
            raise GitCommandFailed(
                f"git rev-parse HEAD:{path} failed in {cwd}: "
                f"{head_oid_result.stderr!r}"
            )
        add_oid = add_oid_result.stdout.strip()
        head_oid = head_oid_result.stdout.strip()
        if add_oid != head_oid:
            violations[path] = [
                f"{add_status} at {add_commit}",
                f"content at HEAD ({head_oid}) differs from content at the "
                f"add commit ({add_oid}) despite no recorded modify -- a "
                "merge commit rewrote it without leaving a --name-status "
                "record",
            ]

    # Fourth check (tan-cli#1065): the three above are all anchored on the
    # walk's own records, which a merge commit never produces. This one is
    # anchored on HEAD's TREE instead -- every entry-shaped path present at
    # HEAD, records or not, must still hold the content the commit that
    # INTRODUCED it gave it. See `entry_violations`'s docstring above for
    # the full argument (why it is not redundant with check 3, why it cannot
    # fire on the squash-merge shape `dev` lands, and what remains out of
    # reach); it is written out once, there, not restated here.
    for path in sorted(present):
        if path in violations:
            continue
        if not _is_entry_path(path, dir_rel):
            continue
        introduced = _introducing_commit(cwd, path)
        if introduced is None:
            raise GitCommandFailed(
                f"{path} is present in HEAD's tree but no commit reachable "
                f"from HEAD contains it, in {cwd} -- git history is "
                "inconsistent; this must not be read as a clean entry"
            )
        intro_commit, intro_oid = introduced
        head_oid = _blob_oid(cwd, "HEAD", path)
        if head_oid is None:
            raise GitCommandFailed(
                f"git rev-parse HEAD:{path} failed in {cwd} for a path "
                "`git ls-tree HEAD` just listed"
            )
        if intro_oid != head_oid:
            # The walk's own first record for the path, when it has one, is
            # reported alongside the introducing commit precisely because the
            # two can differ -- that difference IS the tan-cli#1065 review's
            # fourth door (introduced by a merge, dropped by a merge, re-added
            # by an ordinary commit: the "A" record is the RE-ADD, not the
            # introduction). Never claim "a merge commit introduced it"
            # unconditionally here; a single-parent add reaches this pass too.
            if path in statuses:
                first_status, first_commit = statuses[path][0]
                origin = f"introduced at {intro_commit} ({first_status} record)"
                if first_commit != intro_commit:
                    origin = (
                        f"introduced at {intro_commit}, but the walk's own "
                        f"first record for it is {first_status} at "
                        f"{first_commit} -- the recorded add is not the "
                        "introduction"
                    )
            else:
                origin = (
                    f"introduced at {intro_commit} with no --name-status "
                    "record at all (a merge commit introduced it)"
                )
            violations[path] = [
                origin,
                f"content at HEAD ({head_oid}) differs from content at the "
                f"introducing commit ({intro_oid}) -- the entry does not "
                "still hold the content it was introduced with",
            ]
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


#: tan-cli#1093 (review round 3). A violating path's own record list (the
#: strings `entry_violations` attaches to it) says DELETE when the path was
#: ever actually removed: an explicit `D` `git log --name-status` record
#: (check 1), or check 2's "missing from HEAD's tree" marker -- appended
#: whenever the path is absent from HEAD's own tree, EVEN when check 1
#: already flagged the same path for an unrelated reason (a modify, say,
#: later removed some other way) -- see `entry_violations`'s check 2. Before
#: that fix this classifier read a since-dropped, previously-modified path
#: as MODIFY-only (its records were `['A ...', 'M ...']` with no removal
#: signal at all, because check 2 used to skip any path check 1 had already
#: flagged), which is why the fix lives in `entry_violations` itself and not
#: just in the string match below.
#:
#: What this test does NOT catch, named rather than left implicit: a path
#: that was genuinely removed at some point and later RE-ADDED by an
#: ordinary commit reads as `['A ...', 'A ...']` -- two "A" records, no `D`,
#: and present in HEAD's tree again, so check 2 never touches it and this
#: test reads it as a plain MODIFY (its content, if it changed, "just
#: changed"; the removal in between is invisible from the record strings
#: alone). Check 1 still flags the path either way (`len(seen) != 1`), so
#: nothing here is a MISSED violation -- only a shape this classifier routes
#: to the wrong remedy, printing "the path was never removed" for a path
#: that, briefly, was. This is a narrower relative of the "excluded" shapes
#: the module docstring already names, not a new one: `['A','A']` itself is
#: CAUGHT (tan-cli#1065's own review finding), only the remedy text picked
#: for it here is the wrong one.
#:
#: Everything else that is NOT delete-shaped -- a lone `M` record, or
#: checks 3/4's "content ... differs" sentences, or the `['A','A']` gap just
#: named -- reaches this classifier only because the path is CURRENTLY
#: present in HEAD's tree with no `D` record anywhere in its history; it is
#: not a promise that the path was never touched by a removal at any point.
#: The two shapes get different remediation text below because the obvious
#: recovery attempt differs (re-add the file vs. restore its bytes), not
#: because either one actually clears the check -- neither does; see
#: `_violation_failure_message`'s own docstring.
def _is_delete_shaped(records: list[str]) -> bool:
    return any(
        record.startswith("D ") or "missing from HEAD's tree" in record
        for record in records
    )


_MODIFY_REMEDY = (
    "MODIFY-shaped violation(s) above (the path was never removed, only its "
    "content changed after being committed): a forward commit that restores "
    "the original bytes does NOT clear this -- check 1 counts every record "
    "the path has ever had, and the restoring commit is itself a new one, "
    "so 'A', 'M', 'M' stays flagged exactly like 'A', 'M' already was. The "
    "fix is `git rebase -i`: `drop` the commit(s) that changed it "
    "(discarding the change, leaving the entry exactly as it was first "
    "added), or `fixup`/`squash` them into the add commit (keeping the "
    "correction, folded into that one commit) -- never `reword`, which "
    "edits only the commit message and leaves the tree change, and the "
    "extra record, in place. Either way history must end with a single "
    "clean 'A' and nothing else."
)

_DELETE_REMEDY = (
    "DELETE-shaped violation(s) above (the path was removed, or dropped "
    "during a merge, after being committed): restoring the file forward "
    "does NOT clear this either -- it adds a THIRD record ('A', 'D', 'A'), "
    "not a clean one, because check 1 counts records, not current content. "
    "The fix is `git rebase -i`: `drop` the commit that deleted it (and any "
    "later commit that tried to re-add it) -- do not add a new commit that "
    "recreates the file."
)


def _violation_failure_message(violations: dict[str, list[str]]) -> str:
    """The failure text for `test_every_entry_under_module_size_budget_log_d_
    was_only_ever_added` -- tan-cli#1093. The gate's original text prescribed
    one remedy ("a normal follow-up commit that restores the original
    content") for every shape. That is wrong for a delete: PR #1089 hit it
    for real -- a branch-local delete restored forward walked 'A', 'D', 'A',
    still flagged, with no remaining forward move, because check 1 wants
    EXACTLY one 'A' record and a re-add after a delete is a second violating
    record, not a clean one.

    It turns out to be equally wrong for a modify, which the original text
    was actually written for and which tan-cli#1093 initially assumed still
    worked. It does not: measured directly against this file's own
    `entry_violations`, restoring a modified entry's original bytes in a
    THIRD commit leaves the record trail at 'A', 'M', 'M' -- still two
    records past the one clean 'A' the check wants, for the identical
    reason a delete's re-add is. So this message does not tell either shape
    "add a follow-up commit and you are done" -- neither shape has that
    escape. What differs between the two branches below is only which
    commit a `git rebase -i` needs to drop, fixup, or squash; both funnel
    through the same explanation of why, appended once regardless of which
    shape(s) are present.

    An alternative to this whole design was considered and rejected, and is
    recorded HERE -- not only in the runtime string this function returns --
    so it survives a refactor of that string: evaluate the immutability
    check against the squash-equivalent tree (the diff `dev` would actually
    receive) instead of walking full branch history. That would make a
    branch agree with `dev` without ever needing a rebase. It is rejected
    because it would also make a genuine delete invisible until the PR
    merges -- the opposite failure from what this gate exists to catch. See
    `test_the_squash_equivalent_tree_alternative_is_recorded_and_rejected`
    for the pin.
    """
    shapes = [_is_delete_shaped(records) for records in violations.values()]
    modify_shaped = any(not shape for shape in shapes)
    delete_shaped = any(shapes)
    remedies = []
    if modify_shaped:
        remedies.append(_MODIFY_REMEDY)
    if delete_shaped:
        remedies.append(_DELETE_REMEDY)
    return (
        f"{LOG_DIR_REL} entries must only ever be ADDED, never modified or "
        f"removed once committed. Violating path(s): {violations}.\n\n"
        + "\n\n".join(remedies) + "\n\n"
        "Why neither shape can be fixed by adding a commit, and why a "
        "rebase does not cost dev anything it would have kept: this check "
        "walks every commit reachable from THIS branch's own HEAD, so a "
        "second record for a path is permanent as long as the commit that "
        "made it stays in history -- but dev never walks a branch's "
        "per-commit history at all. A GitHub squash-merge folds this whole "
        "branch into ONE new commit on dev, so a rebase that drops the "
        "offending commit(s) and leaves a single clean 'A' reaches dev "
        "exactly the way a branch that never made the mistake would have. "
        "(An alternative was considered and rejected here: evaluating this "
        "check against the squash-equivalent tree instead of full branch "
        "history would make branch and dev agree without any rebase -- but "
        "it would also make a genuine delete invisible until the PR "
        "merges, which is the opposite failure from what this gate exists "
        "to catch.)"
    )


def test_every_entry_under_module_size_budget_log_d_was_only_ever_added():
    """The real enforcement. See the module docstring for why this needs no
    base ref: every entry's own git history, walked in isolation, is enough."""
    violations = entry_violations(REPO, LOG_DIR_REL)
    assert not violations, _violation_failure_message(violations)


# ---------------------------------------------------------------------------
# tan-cli#1093 -- mutation-proof that the message actually branches. These
# drive `_violation_failure_message` directly against synthetic violation
# dicts (the exact shape `entry_violations` returns), not through a real git
# repo -- the branching is pure string logic and does not need one. Deleting
# either `if` arm in `_violation_failure_message`, or the `_is_delete_shaped`
# classification it depends on, must fail exactly one of the two tests below
# and leave the other passing -- proving neither arm is decorative.


def test_the_failure_message_names_the_rebase_remedy_for_a_delete_shaped_violation():
    violations = {"LOG.d/2026-01-01-aaaaaaaa.md": ["A at abc1234", "D at def5678"]}
    message = _violation_failure_message(violations)
    assert "`drop` the commit that deleted it" in message, (
        f"a delete-shaped violation must name the rebase-drop remedy, got: {message}"
    )
    assert "`drop` the commit(s) that changed it" not in message, (
        "a purely delete-shaped violation must not also carry the modify "
        f"arm's remedy text, got: {message}"
    )


def test_the_failure_message_names_the_rebase_remedy_for_a_modify_shaped_violation():
    violations = {"LOG.d/2026-01-01-aaaaaaaa.md": ["A at abc1234", "M at def5678"]}
    message = _violation_failure_message(violations)
    assert "`drop` the commit(s) that changed it" in message, (
        f"a modify-shaped violation must name the rebase-drop/fixup remedy, never `reword`, got: {message}"
    )
    assert "`drop` the commit that deleted it" not in message, (
        "a purely modify-shaped violation must not also carry the delete "
        f"arm's remedy text, got: {message}"
    )


def test_the_failure_message_treats_a_merge_dropped_entry_as_delete_shaped():
    """Check 2's synthetic record (a merge dropped the path without ever
    emitting a `D`) must route to the same remedy as an explicit delete --
    it is a removal either way, just one `git log --name-status` cannot see
    directly (see `entry_violations`'s own docstring)."""
    violations = {
        "LOG.d/2026-01-01-aaaaaaaa.md": [
            "A at abc1234",
            "missing from HEAD's tree despite this path's own recorded "
            "history showing it as tracked at some point",
        ]
    }
    message = _violation_failure_message(violations)
    assert "`drop` the commit that deleted it" in message
    assert "`drop` the commit(s) that changed it" not in message


def test_the_failure_message_treats_a_merge_rewritten_content_mismatch_as_modify_shaped():
    """Checks 3/4's synthetic "content ... differs" sentences (a merge
    silently rewrote an entry while its path never left HEAD's tree) must
    route to the modify remedy, not the delete one -- the path was never
    removed."""
    violations = {
        "LOG.d/2026-01-01-aaaaaaaa.md": [
            "A at abc1234",
            "content at HEAD (aaa) differs from content at the add commit "
            "(bbb) despite no recorded modify -- a merge commit rewrote it "
            "without leaving a --name-status record",
        ]
    }
    message = _violation_failure_message(violations)
    assert "`drop` the commit(s) that changed it" in message
    assert "`drop` the commit that deleted it" not in message


def test_the_failure_message_carries_both_remedies_exactly_once_when_both_shapes_are_present():
    """Three violating paths -- TWO delete-shaped, one modify-shaped -- in a
    single run. Both remedy paragraphs must appear, and neither is
    duplicated: the remedy is about the SHAPE, not the path count, so two
    delete-shaped paths must still yield exactly one copy of the delete
    remedy, not one per path.

    A fixture with only one path per shape (this test's original form)
    cannot tell "the remedy is per-shape" apart from "the remedy happens to
    be per-path, and there is only one path of each shape here" -- both
    read `count == 1`. Measured: rewriting `_violation_failure_message` to
    append a remedy once per violating path, instead of once per shape
    present, left the single-path-per-shape fixture at `23 passed`; it only
    reds once a shape has TWO paths, which is why this fixture needs one."""
    violations = {
        "LOG.d/2026-01-01-aaaaaaaa.md": ["A at abc1234", "D at def5678"],
        "LOG.d/2026-01-02-cccccccc.md": ["A at 3333333", "D at 4444444"],
        "LOG.d/2026-01-02-bbbbbbbb.md": ["A at 1111111", "M at 2222222"],
    }
    message = _violation_failure_message(violations)
    assert message.count("`drop` the commit that deleted it") == 1, message
    assert message.count("`drop` the commit(s) that changed it") == 1, message


def test_the_failure_message_names_the_reason_dev_never_sees_either_shape():
    """The `git rebase` remedy above has to be more than a bare command --
    the message must say WHY a forward commit cannot work and why the
    rebase costs nothing dev would have kept, for either shape."""
    violations = {"LOG.d/2026-01-01-aaaaaaaa.md": ["A at abc1234", "D at def5678"]}
    message = _violation_failure_message(violations)
    assert "squash-merge" in message and "dev" in message, message


def test_the_squash_equivalent_tree_alternative_is_recorded_and_rejected():
    """tan-cli#1093 review (minor 2): the rejected "check the squash-
    equivalent tree instead of branch history" alternative must be pinned
    in TWO places, not one -- `_violation_failure_message`'s own docstring
    (read by the next person before they re-litigate the design, not only
    on a red run) AND the runtime string it returns (what an author
    actually sees when the gate fires). Deleting either copy must fail
    exactly this test."""
    doc = _violation_failure_message.__doc__ or ""
    assert "squash-equivalent tree" in doc, (
        f"the rejected alternative is missing from the docstring: {doc!r}"
    )
    assert "opposite failure" in doc, (
        f"the docstring must say WHY it was rejected, not just name it: {doc!r}"
    )

    violations = {"LOG.d/2026-01-01-aaaaaaaa.md": ["A at abc1234", "D at def5678"]}
    message = _violation_failure_message(violations)
    assert "squash-equivalent tree" in message, (
        f"the rejected alternative is missing from the runtime message: {message!r}"
    )
    assert "opposite failure" in message, (
        f"the runtime message must say WHY it was rejected, not just name it: {message!r}"
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
    driver -- which is exactly the thing this design was chosen to avoid (a
    driver GitHub's own PR-mergeability computation does not apply to --
    measured on PR #971, tan-cli#907 comment, 2026-08-28)."""
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


def test_a_merge_commit_that_drops_one_sides_already_added_entry_is_caught(tmp_path):
    """The tan-cli#902 loss, reproduced one layer over the old single-file
    ledger: a merge commit whose conflict resolution drops one side's
    already-added entry outright, rather than a follow-up commit editing or
    deleting it. `git log --name-status` alone is fooled here -- a merge
    commit that is TREESAME to one parent for a path is pruned from that
    path's history and emits no diff record at all, so the dropped entry's
    only status record stays a single, lonely "A" -- which is exactly why
    `entry_violations` also cross-checks the add-set against `git ls-tree`
    of HEAD's own tree, not just the name-status walk."""
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
    assert merge.returncode == 0, f"setup merge must succeed clean -- stderr: {merge.stderr}"

    # Simulate the #902-shape conflict resolution: the merge commit's tree
    # drops feature's own entry (as if a lossy `git checkout --theirs` had
    # been applied while resolving), folded into the merge commit itself via
    # `--amend` rather than added as a separate follow-up commit -- the shape
    # that leaves no independent "D" record anywhere in the path's history.
    (entry_dir / "2026-08-27-22222222.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    assert not (entry_dir / "2026-08-27-22222222.md").exists()

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-27-22222222.md" in violations, (
        "a merge commit that drops one side's already-added entry must be "
        f"flagged, but got: {violations}"
    )


def test_an_entry_added_then_modified_then_dropped_by_a_merge_is_classified_as_delete_shaped(tmp_path):
    """tan-cli#1093 review round 2, major 2: the shape neither original arm
    was built for. An entry is added, then modified by an ORDINARY commit
    on this same branch (check 1 already flags it here -- `['A ...', 'M
    ...']`, no removal signal yet), and only THEN dropped by a separate
    merge commit that never emits its own "D" record. Before the
    `entry_violations` fix this shipped with, check 2's tree-membership pass
    skipped any path already in `violations`, so the merge-drop's "missing
    from HEAD's tree" marker never reached this path -- `_is_delete_shaped`
    saw only `['A ...', 'M ...']` and routed it to the MODIFY remedy, which
    told the author "the path was never removed", a false statement to
    their face for a path that, by construction, no longer exists."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-30-ffffffff.md", ["- 2026-08-30 -- first reason"])
    _commit(repo, "add an entry")

    _write(entry_dir, "2026-08-30-ffffffff.md", ["- 2026-08-30 -- REWRITTEN reason"])
    _commit(repo, "an ordinary commit modifies its own just-added entry")

    _git(repo, "checkout", "-q", "-b", "other")
    _write(repo, "unrelated.txt", ["noise"])
    _commit(repo, "unrelated commit on the other branch")

    _git(repo, "checkout", "-q", "main")
    # `--no-ff` is load-bearing: `other` is a pure fast-forward of `main`
    # here (nothing diverged on `main`'s own side), so a plain `git merge`
    # would just move the branch pointer with NO merge commit at all --
    # `--no-ff` forces a real 2-parent commit to amend below.
    merge = _git(repo, "merge", "--no-ff", "--no-edit", "other", check=False)
    assert merge.returncode == 0, f"setup merge must succeed clean -- stderr: {merge.stderr}"

    # Fold the drop into the merge commit itself (no independent "D" record
    # anywhere in the path's history), mirroring the sibling #902-shape test
    # above.
    (entry_dir / "2026-08-30-ffffffff.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    assert not (entry_dir / "2026-08-30-ffffffff.md").exists()

    violations = entry_violations(repo, "LOG.d")
    records = violations.get("LOG.d/2026-08-30-ffffffff.md")
    assert records is not None, (
        f"a modified-then-merge-dropped entry must still be flagged, got: {violations}"
    )
    assert _is_delete_shaped(records), (
        "an entry a merge later dropped must classify as DELETE-shaped even "
        f"though an earlier ordinary commit also modified it, got: {records}"
    )

    message = _violation_failure_message(violations)
    assert "`drop` the commit that deleted it" in message, message
    assert "the path was never removed" not in message, (
        f"a removed path must not be told it was never removed: {message}"
    )


def test_an_evil_merge_that_rewrites_an_already_added_entrys_content_is_caught(tmp_path):
    """Round 3's missed major: a merge commit that REWRITES an already-added
    entry's content, rather than dropping it, leaves no trace for either of
    the first two checks in `entry_violations` -- the path stays in HEAD's
    tree (so the tree-membership cross-check has nothing to flag), and
    `git log --name-status` with none of `-m`/`-c`/`--cc` given never emits a
    diff record for a merge commit at all, regardless of TREESAME, so the
    name-status walk's lone "A" record for this path reads as clean.
    Reproduced directly: base commit adds the entry; two branches diverge
    without either one ever touching that path again; the merge of the two
    is amended to a THIRD content matching neither parent (the shape a
    lossy manual conflict resolution -- 'just retype it' -- produces) --
    `entry_violations` must still flag it, via the blob compare against the
    add-commit added for exactly this gap."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- original reason"])
    _commit(repo, "base adds the entry")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "feature.txt", ["feature unrelated"])
    _commit(repo, "feature unrelated change")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "main.txt", ["main unrelated, diverges"])
    _commit(repo, "main unrelated change")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode == 0, f"setup merge must succeed clean -- stderr: {merge.stderr}"
    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2, (
        "setup must produce a real two-parent merge commit, not a "
        "fast-forward, or this reproduces nothing"
    )
    assert (entry_dir / "2026-08-27-22222222.md").read_text() == "- 2026-08-27 -- original reason\n", (
        "the merge must still be TREESAME to both parents for the entry "
        "before the amend below, or this isn't the gap being reproduced"
    )

    # The evil part: neither branch ever touched the entry, so the merge
    # commit's own tree, before this amend, is identical to both parents for
    # this path -- git log emits nothing for it either way. Rewriting it
    # here, in the merge commit itself, to content matching NEITHER parent
    # is exactly the shape `git log --name-status` (no -m/-c/--cc) can never
    # record for a merge, full stop.
    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- EVIL REWRITE, matches neither parent"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-27-22222222.md" in violations, (
        "a merge commit that rewrites an already-added entry's content "
        f"must be flagged, but got: {violations}"
    )


def _walk_records_for(repo: Path, path: str) -> list[str]:
    """Every `--name-status` record the gate's own walk emits for `path` --
    the shape assertions below use to prove a scenario really is the
    no-record blind spot (or, for the squash case, really is NOT)."""
    out = _git(
        repo,
        "log",
        "--reverse",
        "--full-history",
        "--no-renames",
        "--name-status",
        "--pretty=format:%x01%H",
        "--",
        "LOG.d",
    ).stdout
    return [line for line in out.splitlines() if line.endswith(f"\t{path}")]


def _entry_introduced_by_a_merge_commit(repo: Path) -> Path:
    """Builds the tan-cli#1065 setup: an entry whose ONLY introducing commit
    is a real two-parent MERGE commit, so the name-status walk has no "A"
    record for it anywhere. Two branches diverge on unrelated files, merge
    cleanly, and the merge commit is amended to carry an entry present in
    neither parent -- an "evil merge" that ADDS, which is what an
    over-eager conflict resolution that retypes a lost file produces.
    Returns the entry directory."""
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-25-11111111.md", ["- 2026-08-25 -- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "feature.txt", ["feature unrelated"])
    _commit(repo, "feature unrelated change")

    _git(repo, "checkout", "-q", "main")
    _write(repo, "main.txt", ["main unrelated, diverges"])
    _commit(repo, "main unrelated change")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "main", check=False)
    assert merge.returncode == 0, f"setup merge must succeed clean -- stderr: {merge.stderr}"
    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2, (
        "setup must produce a real two-parent merge commit, not a "
        "fast-forward, or this reproduces nothing"
    )

    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- introduced by the merge commit itself"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == [], (
        "the whole point of this setup is that the merge commit which "
        "introduced the entry emits NO --name-status record for it; if the "
        "walk sees one, the scenario is not the tan-cli#1065 blind spot"
    )
    return entry_dir


def test_an_entry_introduced_by_a_merge_commit_and_rewritten_by_a_second_merge_is_caught(tmp_path):
    """tan-cli#1065, the measured row: an entry whose only introducing commit
    is a merge has no "A" record, so the add-set-driven blob compare added in
    tan-cli#907 round 3 never looked at it, and a SECOND merge could then
    rewrite it for free -- `tree_has_entry = True, violations = {}`, while the
    same shape followed by a NORMAL (single-parent) delete was caught, because
    only a non-merge commit emits a record. Fixed by driving the blob compare
    off HEAD's tree rather than off the add-set."""
    repo = tmp_path / "repo"
    entry_dir = _entry_introduced_by_a_merge_commit(repo)

    # Diverge and merge a SECOND time, and rewrite the merge-introduced entry
    # in that merge commit -- again a shape no --name-status record exists for,
    # on either end.
    _git(repo, "checkout", "-q", "-b", "other")
    _write(repo, "other.txt", ["other unrelated"])
    _commit(repo, "other unrelated change")

    _git(repo, "checkout", "-q", "feature")
    _write(repo, "feature2.txt", ["feature diverges again"])
    _commit(repo, "feature unrelated change 2")

    merge = _git(repo, "merge", "--no-edit", "other", check=False)
    assert merge.returncode == 0, f"second setup merge must succeed clean -- stderr: {merge.stderr}"
    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2, (
        "the second setup merge must also be a real two-parent merge commit"
    )

    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- REWRITTEN by the second merge"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "LOG.d").stdout.splitlines()
    assert "LOG.d/2026-08-27-22222222.md" in tree, (
        "the rewritten entry must still be in HEAD's tree -- the tree "
        "cross-check must have nothing to flag, or this is the already-fixed "
        "merge-DROP case rather than tan-cli#1065"
    )
    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == [], (
        "neither the introducing merge nor the rewriting merge may emit a "
        "--name-status record, or this is not the blind spot being closed"
    )

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-27-22222222.md" in violations, (
        "an entry introduced by a merge commit and then rewritten by a "
        f"second merge must be flagged, but got: {violations}"
    )


def test_an_entry_introduced_by_a_merge_commit_and_never_touched_again_passes_clean(tmp_path):
    """The negative half of the test above, and the reason tan-cli#1065 is
    closed with a content compare rather than by flagging every record-less
    path outright: an entry a merge commit introduced and nothing touched
    again has kept its content, which is the whole promise. Flagging it would
    make the gate red on a shape that lost nothing."""
    repo = tmp_path / "repo"
    entry_dir = _entry_introduced_by_a_merge_commit(repo)

    _write(repo, "unrelated.txt", ["noise"])
    _commit(repo, "an unrelated follow-up commit")

    assert (entry_dir / "2026-08-27-22222222.md").read_text(encoding="utf-8") == (
        "- 2026-08-27 -- introduced by the merge commit itself\n"
    )
    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, (
        "a merge-introduced entry that was never touched again kept its "
        f"content and must not be flagged, but got: {violations}"
    )


def test_a_squash_merged_entry_passes_clean(tmp_path):
    """The shape `dev` actually lands, asserted rather than assumed: GitHub
    "Squash and merge" is what this repo's PRs use, so the tan-cli#1065 fix
    would be a false-positive machine if the tree-driven pass could fire on
    it. It cannot, and the mechanism is asserted directly here, not just in
    the docstring: a squash commit is an ordinary SINGLE-parent commit, so it
    emits a normal "A" record for the entry it carries, which sends that
    entry down the unchanged add-commit compare and never into the
    record-less branch at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    entry_dir = repo / "LOG.d"
    entry_dir.mkdir()
    _write(entry_dir, "2026-08-25-11111111.md", ["- 2026-08-25 -- base entry"])
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src.py", ["def a():", "    return 1"])
    _commit(repo, "feature: the code change that raised the ceiling")
    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- feature's own reasoned entry"])
    _commit(repo, "feature: regen the module size budget")

    _git(repo, "checkout", "-q", "main")
    squash = _git(repo, "merge", "--squash", "feature", check=False)
    assert squash.returncode == 0, f"squash merge must apply clean -- stderr: {squash.stderr}"
    _commit(repo, "feature: the code change that raised the ceiling (#1234)")

    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 1, (
        "a squash merge must produce a SINGLE-parent commit -- that is "
        "exactly why it still emits a --name-status record"
    )
    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == [
        "A\tLOG.d/2026-08-27-22222222.md"
    ], (
        "a squash-merged entry must arrive with a normal single 'A' record, "
        "so it goes down the add-commit compare and never reaches the "
        "tree-driven pass tan-cli#1065 added"
    )

    violations = entry_violations(repo, "LOG.d")
    assert violations == {}, (
        "the ordinary squash-merge shape `dev` uses must stay clean, but "
        f"got: {violations}"
    )


def _merge_introduced_then_merge_dropped(repo: Path) -> Path:
    """Builds on `_entry_introduced_by_a_merge_commit`: a SECOND real merge
    commit then drops the merge-introduced entry, again leaving no
    `--name-status` record anywhere. At this point the path has no record and
    is absent from HEAD's tree -- the never-re-added shape the module
    docstring lists as excluded. Returns the entry directory."""
    entry_dir = _entry_introduced_by_a_merge_commit(repo)

    _git(repo, "checkout", "-q", "-b", "dropside")
    _write(repo, "dropside.txt", ["dropside unrelated"])
    _commit(repo, "dropside unrelated change")

    _git(repo, "checkout", "-q", "feature")
    _write(repo, "feature2.txt", ["feature diverges again"])
    _commit(repo, "feature unrelated change 2")

    merge = _git(repo, "merge", "--no-edit", "dropside", check=False)
    assert merge.returncode == 0, f"the dropping merge must succeed clean -- stderr: {merge.stderr}"
    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2, (
        "the dropping merge must be a real two-parent merge commit"
    )

    (entry_dir / "2026-08-27-22222222.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "LOG.d").stdout.splitlines()
    assert "LOG.d/2026-08-27-22222222.md" not in tree, "the dropping merge must remove the entry from HEAD's tree"
    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == [], (
        "neither the introducing merge nor the dropping merge may emit a "
        "--name-status record, or this is not the shape being composed"
    )
    # The module docstring's exclusion 2, pinned exactly where it is built
    # rather than walked past by both callers: at THIS point the entry is in
    # neither of `entry_violations`' two inputs, so it is invisible -- the
    # documented never-re-added gap. A future widening that started flagging
    # (or raising on) it would surface here, in the helper, instead of
    # silently changing what the docstring promises.
    assert entry_violations(repo, "LOG.d") == {}, (
        "merge-introduced + merge-dropped + never re-added is a DOCUMENTED "
        "exclusion (module docstring, 'What is deliberately excluded' item "
        "2); if it is now caught, that is a real improvement -- but the "
        "docstring and this assertion must move with it"
    )
    return entry_dir


def test_a_merge_introduced_entry_dropped_by_a_merge_and_re_added_with_different_content_is_caught(tmp_path):
    """tan-cli#1065's own review round, the fourth door: compose the two merge
    shapes with the obvious human recovery. A merge INTRODUCES the entry (no
    record), a later merge DROPS it (no record), and an ordinary commit
    RE-ADDS it with different content. The path then arrives with exactly one
    "A" record AND in HEAD's tree, so the record-anchored blob compare
    (check 3) measures it against the RE-ADD instead of the introduction and
    reads clean -- measured `VIOLATIONS: {}` while `- ORIGINAL content X` was
    gone. Caught only because the tree-anchored pass runs for EVERY entry
    path at HEAD, not just the record-less ones."""
    repo = tmp_path / "repo"
    entry_dir = _merge_introduced_then_merge_dropped(repo)

    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- RE-ADDED by an ordinary commit, different content"])
    _commit(repo, "an ordinary commit re-adds the entry the merge dropped")

    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == ["A\tLOG.d/2026-08-27-22222222.md"], (
        "the re-add must emit exactly one ordinary 'A' record -- that record "
        "pointing at the RE-ADD rather than at the introduction is the whole "
        "defect being pinned here"
    )
    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "LOG.d").stdout.splitlines()
    assert "LOG.d/2026-08-27-22222222.md" in tree, (
        "the re-added entry must be back in HEAD's tree, or this is the "
        "already-documented never-re-added exclusion rather than the door "
        "this test exists for"
    )

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-27-22222222.md" in violations, (
        "an entry a merge introduced, a merge dropped and an ordinary commit "
        f"re-added with DIFFERENT content must be flagged, but got: {violations}"
    )


def _merge_introduced_then_dropped_against_a_pre_entry_branch(repo: Path) -> Path:
    """`_merge_introduced_then_merge_dropped`'s sibling, differing in exactly
    one structural detail: the branch the DROPPING merge merges forked BEFORE
    the entry ever existed, so that parent's tree and the dropping merge's
    own tree agree about the path (neither has it) and the merge is TREESAME
    to it for that path.

    That TREESAME-ness is the whole point. DEFAULT git history simplification
    follows only one parent of a merge it is TREESAME to, which prunes the
    entire side the INTRODUCING merge lives on out of a path-limited walk --
    the half of `--full-history` that `_introducing_commit`'s docstring calls
    load-bearing. The sibling helper's dropping merge is TREESAME to NEITHER
    parent (both sides carry the entry), so default simplification already
    follows both parents there and no test built on it can see this
    difference. Returns the entry directory.
    """
    entry_dir = _entry_introduced_by_a_merge_commit(repo)

    # `main` is a parent of the introducing merge and has never held the
    # entry -- branching from it is what makes this side "pre-entry". A new
    # commit on top keeps the merge below a real two-parent one rather than a
    # fast-forward.
    _git(repo, "checkout", "-q", "-b", "preentry", "main")
    _write(repo, "preentry.txt", ["unrelated, on a branch that forked before the entry existed"])
    _commit(repo, "a commit on a branch that predates the entry")

    _git(repo, "checkout", "-q", "feature")
    merge = _git(repo, "merge", "--no-edit", "preentry", check=False)
    assert merge.returncode == 0, f"the dropping merge must succeed clean -- stderr: {merge.stderr}"

    (entry_dir / "2026-08-27-22222222.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2, (
        "the dropping merge must be a real two-parent merge commit"
    )
    # The structural difference from the sibling helper, asserted rather than
    # described: the merge and its SECOND parent (the pre-entry branch) both
    # lack the entry, i.e. the merge is TREESAME to that parent for the path.
    assert _blob_oid(repo, "HEAD", "LOG.d/2026-08-27-22222222.md") is None, (
        "the dropping merge must remove the entry from HEAD's tree"
    )
    assert _blob_oid(repo, "HEAD^2", "LOG.d/2026-08-27-22222222.md") is None, (
        "the dropping merge's second parent must be a branch that never held "
        "the entry -- without that the merge is TREESAME to no parent, "
        "default simplification follows both, and nothing is pruned"
    )
    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == [], (
        "neither the introducing merge nor the dropping merge may emit a "
        "--name-status record, or this is not the shape being composed"
    )
    # Same documented exclusion the sibling helper pins, pinned again here
    # because this variant reaches it by a different route: at THIS point the
    # entry has no record and is not in HEAD's tree, so it is in neither of
    # `entry_violations`' two inputs (module docstring, "What is deliberately
    # excluded" item 2).
    assert entry_violations(repo, "LOG.d") == {}, (
        "merge-introduced + merge-dropped + never re-added is a DOCUMENTED "
        "exclusion; if it is now caught, the docstring and this assertion "
        "must move with it"
    )
    return entry_dir


def test_a_merge_introduced_entry_is_caught_even_when_default_simplification_would_prune_the_introducing_merge(tmp_path):
    """tan-cli#1144: the guard `_introducing_commit`'s `--full-history` never
    had. Same composed shape as
    `test_a_merge_introduced_entry_dropped_by_a_merge_and_re_added_with_different_content_is_caught`
    -- a merge INTRODUCES the entry, a merge DROPS it, an ordinary commit
    RE-ADDS it with different content -- but with the dropping merge TREESAME
    to its pre-entry parent, so DEFAULT history simplification prunes the
    introducing merge's whole side out of the path-limited walk instead of
    following both parents. The sibling test passes with `--full-history`
    deleted from the `rev-list` call; this one cannot, because without it the
    walk resolves the "introduction" to the re-add itself, whose blob IS
    HEAD's, and the rewritten entry reads clean.

    Deleting `--full-history` from the `git rev-list` call in
    `_introducing_commit` was measured green across the entire gates suite --
    `1142 passed, 34 skipped` on tan-cli `1fc18bb1`, Python 3.12.3, git
    2.43.0 -- before this test existed. It is the only thing standing between
    that call and a silent reopening of PR #1070's blind spot on an
    append-only ledger.
    """
    repo = tmp_path / "repo"
    entry_dir = _merge_introduced_then_dropped_against_a_pre_entry_branch(repo)
    path = "LOG.d/2026-08-27-22222222.md"

    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- RE-ADDED by an ordinary commit, different content"])
    _commit(repo, "an ordinary commit re-adds the entry the merge dropped")
    re_add = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Checks 1-3 are clean here BY CONSTRUCTION, so nothing but check 4's
    # introduction anchor can catch this: the re-add is the path's only
    # name-status record (check 1), the path is back in HEAD's tree
    # (check 2), and the record check 3 anchors on is that same re-add, whose
    # blob is therefore HEAD's own.
    assert _walk_records_for(repo, path) == [f"A\t{path}"], (
        "the re-add must be the path's ONLY --name-status record, or checks "
        "1-3 could be doing the catching and this test would not isolate the "
        "introduction walk"
    )
    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "LOG.d").stdout.splitlines()
    assert path in tree, "the re-added entry must be back in HEAD's tree"
    head_oid = _blob_oid(repo, "HEAD", path)
    assert _blob_oid(repo, re_add, path) == head_oid, (
        "the add-commit anchor must AGREE with HEAD here, or check 3 is doing "
        "the catching rather than check 4"
    )

    # The pruning itself, measured on raw git rather than argued: the same
    # walk WITHOUT `--full-history` lists nothing but the re-add -- both
    # merges, and with them the introduction, are simplified away.
    default_walk = _git(repo, "rev-list", "--reverse", "HEAD", "--", path).stdout.split()
    assert default_walk == [re_add], (
        "default history simplification must prune both merges out of this "
        f"path's walk, or the scenario is not the blind spot: {default_walk}"
    )

    intro = _introducing_commit(repo, path)
    assert intro is not None, "a path read out of HEAD's own tree must resolve an introducing commit"
    assert intro[0] != re_add, (
        "`_introducing_commit`'s `git rev-list --full-history` walk must "
        "reach the MERGE that introduced the entry; resolving to the re-add "
        f"({re_add}) means the walk was simplified down to the surviving "
        "parent -- i.e. `--full-history` is no longer pinned on that walk "
        "and PR #1070's merge-introduction blind spot is reopened"
    )
    assert intro[1] != head_oid, (
        "the introducing merge's blob must differ from HEAD's, or this test "
        "asserts nothing about content"
    )

    violations = entry_violations(repo, "LOG.d")
    assert path in violations, (
        "an entry a merge introduced, a merge dropped against a pre-entry "
        "branch, and an ordinary commit re-added with DIFFERENT content must "
        f"be flagged, but got: {violations}"
    )


def test_the_add_commit_anchor_still_catches_what_the_introduction_anchor_cannot(tmp_path):
    """Checks 3 and 4 are two ANCHORS for one promise and neither subsumes
    the other -- so widening check 4 to every path in HEAD's tree does not
    turn check 3 into dead code. The mirror image of the test above: a merge
    introduces X, a merge drops it, an ordinary commit re-adds Z, and a THIRD
    merge rewrites Z back to X. `_introducing_commit` and HEAD then both read
    X, so the introduction anchor sees nothing; only the add-commit anchor
    can tell that content Z was committed and silently rewritten.

    Guard this one carefully: it is check 3's SOLE remaining unique coverage.
    Since the widening, the older
    `test_an_evil_merge_that_rewrites_an_already_added_entrys_content_is_caught`
    passes under check 4 as well, so deleting check 3 reds exactly this test
    and nothing else (measured -- mutant N2: `1 failed, 16 passed`). Weaken
    or delete this test and check 3 becomes dead code that no mutant can
    detect."""
    repo = tmp_path / "repo"
    entry_dir = _merge_introduced_then_merge_dropped(repo)

    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- RE-ADDED content Z"])
    _commit(repo, "an ordinary commit re-adds the entry with content Z")

    _git(repo, "checkout", "-q", "-b", "fixside")
    _write(repo, "fixside.txt", ["fixside unrelated"])
    _commit(repo, "fixside unrelated change")

    _git(repo, "checkout", "-q", "feature")
    _write(repo, "feature3.txt", ["feature diverges a third time"])
    _commit(repo, "feature unrelated change 3")

    merge = _git(repo, "merge", "--no-edit", "fixside", check=False)
    assert merge.returncode == 0, f"the rewriting merge must succeed clean -- stderr: {merge.stderr}"
    assert len(_git(repo, "rev-parse", "HEAD^@").stdout.splitlines()) == 2

    # Back to the content the INTRODUCING merge gave it -- so the tree anchor
    # is satisfied while the recorded add (content Z) is not.
    _write(entry_dir, "2026-08-27-22222222.md", ["- 2026-08-27 -- introduced by the merge commit itself"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "--no-edit")

    intro = _introducing_commit(repo, "LOG.d/2026-08-27-22222222.md")
    assert intro is not None
    assert intro[1] == _blob_oid(repo, "HEAD", "LOG.d/2026-08-27-22222222.md"), (
        "the introduction anchor must AGREE with HEAD here, or this test is "
        "not isolating what only the add-commit anchor can see"
    )
    assert _walk_records_for(repo, "LOG.d/2026-08-27-22222222.md") == ["A\tLOG.d/2026-08-27-22222222.md"], (
        "the walk must still show exactly the re-add's lone 'A' record"
    )

    violations = entry_violations(repo, "LOG.d")
    assert "LOG.d/2026-08-27-22222222.md" in violations, (
        "content committed by the recorded add and then rewritten by a merge "
        "must still be flagged by the add-commit anchor, but got: "
        f"{violations}"
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
