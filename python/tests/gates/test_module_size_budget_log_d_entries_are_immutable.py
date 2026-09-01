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
`git ls-tree`'s listing of HEAD's own tree AND a direct blob compare, for
EVERY entry-shaped path in that tree, between the content it was introduced
with and the content at HEAD (see `entry_violations`'s own docstring for
why the name-status walk alone is not enough: a merge commit that drops one
side's already-added entry while resolving a conflict emits no diff record
for that path at all, and neither does a merge commit that instead REWRITES
the entry's content, nor one that INTRODUCES it -- `git log --name-status`
with none of `-m`/`-c`/`--cc` given never emits a diff for a merge commit,
period, so the tree-membership check and the blob compare are each
load-bearing for a different half of "a merge commit touched this path",
not redundant with each other). Driving the blob compare off HEAD's tree
rather than off the walk's add-set is tan-cli#1065: an entry whose only
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
merge drops -- see "What is deliberately excluded" below and
`entry_violations`'s own docstring for why no git-log-based walk can close
either of them.)

## What is deliberately excluded

`MODULE_SIZE_BUDGET_LOG.d/README.md` is not an entry (`_module_size_budget_
core.LOG_DIR`'s own README, not something `regen_module_size_budget.py`
writes) and is free to be edited like any other doc file -- `_entry_path`
below only matches the `<date>-<8 hex chars>.md` shape the script actually
generates, so README.md (and anything else that does not match) is never
even considered.

A squash-merge that never actually commits an entry to this branch's
history is also excluded, but not by a filter the way README.md is --
excluded because there is nothing here to check against. If a feature
branch adds an entry and the merge into this branch is a GitHub "Squash and
merge" (`dev`'s own real merge strategy) that omits that file from the
squash commit's diff, this branch's history never contains a commit that
added the path at all: squash discards the branch's own per-commit history,
so there is no "A" record anywhere reachable from HEAD, and the path is not
in HEAD's tree either, so neither of `entry_violations`' two inputs (the
walk's records, HEAD's tree) ever mentions it. This is a real, unclosed gap
in what a git-log walk can see -- not a design choice this file is making
on purpose the way the README.md filter is -- see `entry_violations`'s own
docstring for the full reasoning on why no addition to the walk closes it.
Note that an ordinary squash-merge that DOES carry the entry is fully
covered and always was: a squash commit is an ordinary single-parent commit,
so it emits a normal "A" record like any other add.

One narrower relative of that gap is excluded for the same structural
reason, and is named here rather than left implicit (tan-cli#1065's own
scope note: a third UNDOCUMENTED blind spot in a file called
`..._entries_are_immutable` is the thing to avoid): an entry that a merge
commit INTRODUCED -- so it has no "A" record, the tan-cli#1065 case fixed
below -- and that a later merge commit then DROPPED, so it is not in HEAD's
tree either. Neither of `entry_violations`' two inputs mentions it, exactly
as for the squash case, so there is no path for it to check. What IS covered
as of tan-cli#1065 is the same merge-introduced entry while it remains in
HEAD's tree: its content is compared against the merge that introduced it,
so a rewrite by any later commit, merge or not, is caught. Closing the
dropped variant too would mean enumerating every tree in the history instead
of walking records plus HEAD's tree -- a materially more expensive check for
a shape whose ingredients are two separate conflict-resolution amends.
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

    `--full-history` is what makes this work for the merge case: it keeps a
    merge commit in a path-limited walk instead of simplifying it away, and
    a merge that introduces a path present in NEITHER parent is TREESAME to
    neither, so it is listed. `--reverse` puts the oldest listed commit
    first; the loop still checks tree membership rather than trusting the
    first line, so a history whose oldest listed commit is a delete (an
    add/drop/re-add cycle) resolves to the right commit instead of raising.
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
    sees the "A" to cross-check against -- only the pair does.

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
    its "D" record. So the blob compare is driven by HEAD's own TREE rather
    than by the walk's add-set: every entry-shaped path in HEAD's tree is
    compared against the content it was introduced with, whether or not the
    walk recorded an add for it. For the paths that do have an "A" record
    that is the add commit, unchanged; for a path with none, the introducing
    commit is resolved by `_introducing_commit` -- the earliest commit
    reachable from HEAD whose tree actually contains the path, which for a
    merge-introduced entry IS the introducing merge (`git rev-list
    --full-history` does list a merge that is TREESAME to no parent for the
    path, and a merge that introduces a path present in neither parent is by
    definition TREESAME to neither).

    This tree-driven pass cannot fire on the ordinary squash-merge shape
    `dev` actually uses -- the thing tan-cli#1065's scope note is emphatic it
    must not be turned into a false-positive machine. A squash commit is an
    ordinary SINGLE-parent commit, so it emits a normal "A" record for every
    entry it carries; a squash-merged entry therefore has an add record and
    goes down exactly the add-commit compare it always did. The tree-driven
    branch engages only for a path in HEAD's tree with ZERO name-status
    records, which no single-parent commit can produce -- only a merge can.
    Proven hermetically, not argued:
    `test_a_squash_merged_entry_passes_clean` and
    `test_an_entry_introduced_by_a_merge_commit_and_never_touched_again_
    passes_clean` are the negative half of
    `test_an_entry_introduced_by_a_merge_commit_and_rewritten_by_a_second_
    merge_is_caught`.

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

    One narrower relative of that gap survives the tree-driven pass above
    and is excluded for the same structural reason, named here rather than
    left implicit: an entry that a merge commit INTRODUCED and a later merge
    commit then DROPPED. It has no "A" record (the introducing merge emits
    none) and it is not in HEAD's tree (the dropping merge removed it,
    emitting none either), so it appears in NEITHER of this function's two
    inputs -- there is no path for it to check, the same way there is none
    for the squash case. Closing it would mean enumerating every tree in the
    history rather than walking records plus HEAD's tree, a materially more
    expensive check for a shape that needs two separate conflict-resolution
    amends where the tan-cli#1065 case fixed above needs only the
    introducing one. Everything that IS in HEAD's tree keeps the full
    content promise.
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
    for path, seen in statuses.items():
        if path in violations or path in present:
            continue
        # Every path here was matched by `_is_entry_path` and has at least
        # one "A" record, yet is absent from HEAD's own tree -- a merge
        # commit dropped it without ever recording a "D" for it (see the
        # docstring above). Flag it the same way a directly-observed delete
        # is flagged.
        violations[path] = [
            f"{status} at {commit}" for status, commit in seen
        ] + ["missing from HEAD's tree despite an add record (dropped during a merge)"]

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

    # Fourth check (tan-cli#1065): the three above all start from the walk's
    # own records, so an entry with NO record at all -- one a MERGE commit
    # introduced, since a merge emits no --name-status diff for anything --
    # is invisible to every one of them, and a second merge can then rewrite
    # it for free. Drive the same content promise off HEAD's TREE instead:
    # every entry-shaped path present at HEAD that the walk never recorded
    # is compared against the commit that introduced it. A squash commit is
    # single-parent and emits a normal "A" record, so a squash-merged entry
    # never reaches this branch (see the docstring above).
    for path in sorted(present):
        if path in statuses or path in violations:
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
            violations[path] = [
                f"introduced at {intro_commit} with no --name-status record "
                "(a merge commit introduced it)",
                f"content at HEAD ({head_oid}) differs from content at the "
                f"introducing commit ({intro_oid}) -- an entry a merge "
                "commit introduced was rewritten later, and neither commit "
                "left a --name-status record",
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
