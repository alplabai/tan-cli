<!-- SPDX-License-Identifier: Apache-2.0 -->
# `module_size_budget.d/` — one file per measured module

Mirrors `MODULE_SIZE_BUDGET_LOG.d/` and `changelog.d/` (see those directories'
own `README.md`s for the fuller version of this reasoning; this file states
the parts specific to the size ratchet's data).

## Why

Through 2026-08-31 all of this lived in one file,
`../module_size_budget.generated.json`. tan-cli#668 had already fixed the
worst of its predecessor — a hand-maintained Python dict nobody could merge —
by making every number MEASURED rather than typed. What it did not fix was the
conflict rate: every branch that changed any tracked module rewrote the same
region of the same file, so it collided on most long-lived branches.
tan-cli#907 removed that conflict class for the sibling *ledger*, structurally,
by giving every entry its own file; it deliberately left this data alone and
filed tan-cli#1057. This directory is that follow-up.

Measured over **93 value-changing commits** to the old single file (4278
commit pairs, `dev` history):

| storage | pairs that collide |
|---|---|
| single file (what this replaces) | 4278/4278 = **100.0%** |
| per top-level package (tan-cli#1057's proposal) | 2621/4278 = **61.3%** |
| per module, whole-tree scalars still stored | 958/4278 = **22.4%** |
| per module, whole-tree scalars **derived** | 553/4278 = **12.9%** |

The per-package split loses most of its value to one bucket: **69%** of those
commits touch `tan/commands/` and **58%** touch more than one package at all.
The issue's own four-PR sample found no commit that moved a whole-tree scalar;
over 93 commits, **34%** do — which is why storing those two numbers anywhere
shared costs 22.4% instead of 12.9%.

The residual **12.9%** is not claimed away: two branches that both change the
*same* module still write the same record file and still conflict. That is a
genuine same-module conflict, and its resolution is the cheap one it has
always been — delete either side, re-run the script.

## How

Every file here is **written by `scripts/regen_module_size_budget.py`**, never
hand-authored. A record's **path is its key**: the module's repo-relative path
plus `.json`, so `python/tan/commands/build_cmd.py` is recorded at
`module_size_budget.d/tan/commands/build_cmd.py.json`. Mirroring the source
tree makes the naming collision-free by construction (a fixed suffix appended
to an already-unique path is injective) and deterministic — no counter, no
hash, nothing a second branch could compute differently for the same module.

Two kinds of record, and the difference is load-bearing:

* **`"kind": "budget"`** — a `python/tan/**` module. `lines` is its ratcheted
  ceiling, or `null` when the module is under the 800-line cap and the record
  exists only for its function facts. Raising `lines`, or a function newly
  over `FUNCTION_CAP` (or growing further) in `long_functions` (tan-cli#1173,
  see below), needs `--reason` and writes a ledger entry under
  `../MODULE_SIZE_BUDGET_LOG.d/`; a later SHRINK of the same module is instead
  absorbed by a plain regen with no `--reason` and no new entry, because
  `_append_log` (`scripts/regen_module_size_budget.py`) fires only on growth,
  so a ledger entry's logged `-> Y` can end up above the module's current
  `lines` — the ledger is history, not state, and it is this record, never
  the ledger, that the gate enforces against.
* **`"kind": "observed"`** — a `python/tests/**` file over the cap. A
  MEASUREMENT, never a ceiling (tan-cli#817). Nothing compares it to a
  threshold, its growth needs no `--reason`, and it never writes a ledger
  entry. See `TEST_ROOT` in `../_module_size_budget_core.py` for the decision
  and the measurements behind it.

That distinction used to be positional (two sections of one file). It is now
explicit and machine-checked: `_load_records` refuses a record whose `kind`
disagrees with the tree its path sits in, so a `tests/**` measurement can
never be read as a `tan/**` ceiling.

## The two whole-tree scalars are DERIVED, not stored

`function_count_budget` and `function_worst_budget` — the count and worst span
of every over-50-line function anywhere in `python/tan/**` — are gone as
stored numbers. Each record's `long_functions` is now the actual sorted
`[span, name]` list for that module (see below), and `MeasuredState` exposes
the whole-tree pair as a **sum** and a **max** over those lists' lengths and
spans. That is exactly how `measure_current` always computed them (`len(found)`
and `max(span for span, _ in found)` over a list accumulated per module), so
**nothing about what those two whole-tree numbers mean changed** — they are
still a sum and a max over the tree.

## `long_functions` is a list, and growth is judged per FUNCTION (tan-cli#1173)

Through 2026-09-04, `long_functions` was itself a count (and a sibling
`worst_function` field the max), and `scripts/regen_module_size_budget.py`
judged growth only on the two DERIVED whole-tree numbers above. That let a
module have one function cross `FUNCTION_CAP` while a different function in
the SAME module dropped below it, with both the count and the whole-tree max
reading unchanged — not hypothetical: PR #1170 grew `_sdk_credential`
`50 -> 63 -> 69` while `_data` fell `51 -> 47` in the same diff, and
`bootstrap_cmd.py.json`'s `long_functions` read `19` before and after.

`long_functions` is now the sorted `[span, name]` list itself — every function
in the module over `FUNCTION_CAP`, not a count of them — and the regen script
compares it per `module:name`. A function newly over the cap, or an
already-over one growing further, is growth in its own right and needs
`--reason` even when nothing else in the tree moved; a function dropping below
the cap is reported as shrunk (never forces a flag, the same as a module
shrinking) but — unlike a module-line shrink — it IS included in the
`--reason`/`--merge-resync` ledger entry, so an offsetting pair's entry names
both the function that grew and the one that dropped rather than only half of
what moved.

This is a narrower decision than tan-cli#1057's: that issue's own
`test_a_whole_tree_neutral_function_move_needs_no_reason` used to pin that a
function move ACROSS modules needed no reason, on the reasoning that the
ratchet is whole-tree. tan-cli#1173 overturns that specifically for
`FUNCTION_CAP` crossings — a function newly over the cap is judged on its own,
regardless of whether the module (or the tree) it sits in grew, shrank, or
stayed flat.

## Resolving a conflict, and the one thing never to do

Same as it always was: **throw either side away and re-run
`python scripts/regen_module_size_budget.py --merge-resync`** against the
already-merged tree. Never interpolate between two committed numbers, and
never hand-edit a value here. `measure_current` is the only thing either the
gate or the script trusts for "what is true right now" — neither ever reads a
committed record to derive a new one (tan-cli#668's constraint), which is what
makes a padded value structurally impossible to arrive at honestly. A padded
one that is hand-written anyway reds `regen_module_size_budget.py --check` and
`test_the_recorded_function_facts_match_the_measurement`, naming the module.
