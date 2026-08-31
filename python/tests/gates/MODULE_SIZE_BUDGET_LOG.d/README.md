<!-- SPDX-License-Identifier: Apache-2.0 -->
# `MODULE_SIZE_BUDGET_LOG.d/` — one file per ledger entry

Mirrors `changelog.d/` (see that directory's own `README.md` for the fuller
version of this reasoning; this file states the parts specific to this
ledger).

## Why

Through 2026-08-30 the module/function size ratchet's ledger,
`../MODULE_SIZE_BUDGET_LOG.md`, was a single file every raised-ceiling regen
run **appended** to. Two branches that each raised a ceiling wrote into the
same tail region of the same file, so a large fraction of long-lived branches
conflicted on it — measured on tan-cli#907, four of five sampled v0.7.0
PRs — and the standard resolution (`git checkout --theirs`, or similar) took
one side wholesale, silently discarding the other side's own reasoned entry.
That happened for real on tan-cli#902.

`.gitattributes`' `merge=union` mitigation (tan-cli#939) fixed the LOCAL `git
merge` case but not the one that actually blocks a PR: GitHub computes a
pull request's own mergeable status without applying custom merge drivers —
measured on PR #971 (tan-cli#907 comment, 2026-08-28): a real `git merge
origin/dev` at that PR's exact head resolved clean, zero markers, while
GitHub itself, polled three times over eight minutes, reported `CONFLICTING`
every time — so a union-attributed single file can still show a PR as
CONFLICTING in the GitHub UI even though a plain local merge would resolve it
clean.

Disjoint files cannot conflict, under any merge strategy, local or
GitHub-side, with no driver needed at all. One file per entry removes the
conflict class structurally rather than mitigating it after the fact.

## How

Every entry here is **written by `scripts/regen_module_size_budget.py`**,
never hand-authored — the same `--reason "..."` / `--merge-resync` flags that
used to append a line to `MODULE_SIZE_BUDGET_LOG.md` now create a new file
here instead, named `<date>-<8 hex chars>.md` (the trailing token exists only
to make the filename unique against a concurrent branch or same-day run; it
carries no other meaning). Its content is the same dated `- <date> --
<reason>` bullet, with one `- <module>: <before> -> <after>` sub-bullet per
ceiling that moved, that used to be one line appended to the old file.

Do not hand-edit an existing entry here, and do not delete one — once a file
in this directory reaches `dev` it is exactly as immutable as a line in the
old ledger was supposed to be, and
`test_module_size_budget_log_d_entries_are_immutable.py` enforces that
directly (a modified or removed file under this directory, at any point in
this checkout's history, is a hard failure — not the old ledger's
base-vs-HEAD comparison, which this simpler design does not need at all: a
file that is only ever added can be checked by walking its own history in
isolation, with no PR/merge-queue base ref to resolve).

## `MODULE_SIZE_BUDGET_LOG.md` itself

The old single-file ledger, one directory up, is frozen as of tan-cli#907 —
its historical entries (2026-08-11 through 2026-08-30) stay exactly where
they are, read-only, and `test_module_size_budget_log_append_only.py` keeps
enforcing that. Nothing writes into it any more; read it for the ratchet's
history before this migration, and this directory for everything after.
