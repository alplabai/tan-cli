<!-- SPDX-License-Identifier: Apache-2.0 -->
# Who updates a PR branch, and when

PRs into `dev` merge through the merge queue. This page says when a PR branch
needs `dev` merged into it, and who does that, so two people do not update the
same branch at the same time.

## The rules

1. **Update a PR branch from `dev` only when it has a conflict.** A branch
   that is only behind `dev` does not need updating. Leave it.
2. **The PR's author updates the branch.** That covers merging `dev` in and
   resolving the conflicts.
3. **Anyone else who pushes to a PR branch says so first.** Post a comment on
   the PR ("pushing a review fix"), push, then post "done". The author does not
   push to the branch between those two comments.
4. **Never force-push a PR branch someone else may have touched.** If a normal
   push is rejected, someone else pushed first. Fetch, merge their commits,
   and push again.

## Why

- `dev` does not require a branch to be up to date before it merges
  (`required_status_checks.strict` is `false`). The merge queue builds every
  entry on top of `dev` and the entries ahead of it, so a branch that is
  behind but has no conflict can be queued as it is.
- Every push to a PR branch restarts its CI. An update that was not needed
  costs a full CI run, and it can collide with someone else's push.
- On 2026-10-09 #1431 and #1433 were each updated from `dev` by two people
  independently, and #1428 had `dev` merged into it twice in half an hour
  (tan-cli#1459). On #1433 the two merges were pushed a minute apart, and
  joining them took a third merge commit. Nothing was lost, because nobody
  force-pushed, but the work was done twice.

## In practice

1. Check whether the branch actually conflicts:

   ```sh
   gh pr view <N> --repo alplabai/tan-cli --json mergeable,mergeStateStatus
   ```

   `CONFLICTING` / `DIRTY` means it needs an update. Any other state does not
   call for one.

2. If you are not the author and the update cannot wait, comment on the PR
   before you start, and again when you have pushed.
3. A branch that is in the merge queue rejects pushes. Take the PR out of the
   queue first, or wait for the queue to eject it.
4. While a planner re-sync PR is open, `docs/planner-resync.md` decides the
   merge order for planner PRs; these rules still decide who updates a branch.
