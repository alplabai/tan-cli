<!-- SPDX-License-Identifier: Apache-2.0 -->
# Planner re-sync PRs merge first

`python/tan/planner/` mirrors alp-sdk's `scripts/alp_orchestrate/`, and
`python/tests/gates/test_planner_relocation_freshness.py` checks the mirror
by hash. A planner re-sync PR moves the pinned alp-sdk commit, the mirrored
planner files and the gate's hash and audit tables in one change.

## The rule

**While a planner re-sync PR is open, it merges before any other PR that
touches `python/tan/planner/**` or
`python/tests/gates/test_planner_relocation_freshness.py`. Those other PRs
rebase onto `dev` after the re-sync lands.**

A planner re-sync PR is one whose title starts with `chore(planner): re-sync`.
The bot's re-sync PRs and hand-finished ones both use that prefix.

## Why

- A re-sync is checked as a whole against one bound alp-sdk commit: the
  freshness hashes, the hand-port tables, the planner oracle and the parity
  fixtures. Rebasing it onto another planner change means doing that whole
  check again.
- A feature PR that touches the planner usually adds a few lines to a file
  the re-sync also changes. Rebasing those few lines onto the re-sync is the
  smaller job.
- Notes that a feature PR leaves in the freshness gate for "the next
  re-sync" are written against the pin in effect once the open re-sync has
  landed. If the feature PR merges first, those notes point at the wrong
  re-sync.

## In practice

1. Before you queue a PR that touches the paths above, check for an open
   re-sync PR:

   ```sh
   gh pr list --repo alplabai/tan-cli --state open --search 'in:title "chore(planner): re-sync"'
   ```

2. If one is open and is not a bot PR waiting for someone to pick it up,
   hold your PR, say so in a comment on it, and rebase once the re-sync has
   merged.
3. A bot re-sync PR that nobody has picked up does not block other PRs.
   Whoever picks it up re-syncs on top of the `dev` they find.

Who updates a branch that has to rebase after a re-sync is covered in
`docs/pr-branch-updates.md`.
