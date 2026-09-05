# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1116: three PRs in one night narrowed a `try`/`except` clause and
silently broke a function's own "never raises" / quiet-return contract --
`document_guards.read_catalog_document` (PR #1096, `except OSError` let a
`UnicodeDecodeError` escape), a `Path.exists()` pre-flight (PR #1110,
swallows `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` but not `EACCES`), and
`perf.read_perf_point`'s neighbour `perf_apply.py`'s two sites (PR #1114's
own delta review, `except (OSError, yaml.YAMLError)` missing
`UnicodeDecodeError`). Every one was caught by a human-directed review; none
by a test. This file is that test.

WHAT THIS ASSERTS, and why it is name-based and opt-in (`_SEEDED_CONTRACTS`
below, mirroring `test_shared_helpers_have_one_definition.py`'s
`_SHARED_HELPERS` precedent, tan-cli#1083/#1091): for each function named
there, driving it against a REAL broken filesystem fixture for every shape
that function's own read can meet produces the outcome its own docstring
declares -- either a specific quiet value (`None`, `[]`, `set()`, ...) on
EVERY failure, or (for the two catalog-style readers, `document_guards.
read_catalog_document` and `error_catalog.load_codes`) exactly the ONE
curated exception type its docstring names and never a different one. Both
are "the declared contract," not two different gates: a function that
raises a curated `CatalogUnreadable` on every failure has exactly as firm a
contract as one that returns `None` on every failure, and PR #1096's own
defect was a curated-raise function (`read_catalog_document` raises
`self.error`) leaking a RAW `UnicodeDecodeError` instead -- the same
"declared boundary breached" shape as a quiet-return function raising
instead of returning.

WHAT THIS DOES NOT ASSERT, deliberately. Nothing about a function not named
in `_SEEDED_CONTRACTS`. The tree has far more candidates than this seeds --
see THE SHAPE SWEEP below for the measured count -- and this file protects
only the seeded TWENTY-FIVE (twelve at tan-cli#1116, then three, then
#1132's two, then #1133's three, then #1134's one and #1162's four;
re-count the dict rather than trusting this sentence) until someone adds
the next one, the same "opt-in, not a blanket
walk" reasoning `_SHARED_HELPERS`'s own docstring gives: a
blanket version of this check is red on the day it lands (again, see THE
SHAPE SWEEP), and a gate that is red on landing gets disabled, which
protects nothing.

THE ALTERNATIVE REJECTED, ON PURPOSE: a lint rule banning a narrowed
`except` clause. That would be noisy and wrong -- narrowing an `except`
clause is very often the CORRECT edit (`test_no_unreachable_except_handler.
py` exists for the opposite mistake, an except clause too WIDE to ever need
its second arm). The invariant worth enforcing is the DECLARED CONTRACT --
what the function's own docstring promises about its outcome on failure --
never the shape of the clause that currently happens to hold it. A rule
keyed on clause shape cannot tell a correct narrowing from a regression;
this file asks the function itself, by running it.

THE VERSION-INDEPENDENCE LESSON (review round 2 BLOCKER, its own section
because it is the one mistake this file's FIRST version made and shipped
red): a pre-flight `is_file()`/`is_dir()`/`exists()` guard wrapped in
`except OSError` is not a fix, it is a bet on which errnos `pathlib`
happens to swallow THIS interpreter. The RAISING BEHAVIOUR that reasoning
depends on -- `is_file()`/`is_dir()` swallow `ENOENT`/`ENOTDIR`/`EBADF`/
`ELOOP` but re-raise `EACCES` -- holds on 3.12.3 AND 3.13.15 and stops
holding only in 3.14.7: there those calls swallow EVERY `OSError`,
`EACCES` included, and return `False` instead -- so a guard written to
catch the ONE errno that used to escape catches nothing there, because
nothing escapes the stat call for the guard to catch. `metadata_schema.
validate_document` and `scaffold.read_example_tree` shipped exactly that
guard in this gate's first version, and CI's `seam1` leg (`parity.yml`,
`python-version: "3.x"`, resolved to CPython 3.14.7) turned it red on
`TestValidateDocument::test_permission_denied` -- measured `assert 0 == 1`,
the function silently answering "everything is valid" for a schema it could
not even read.

**The boundary is not where the reasoning's own citation lives, and that
gap is worth naming precisely -- it is why the first fix got it wrong.**
`pathlib`'s `_IGNORED_ERRNOS` CONSTANT, the one the reasoning names, is
gone a full release EARLIER than the behaviour it once described: absent
from `pathlib` since 3.13 (`AttributeError` probing for it there), while
`is_file()` on a permission-denied ancestor keeps RAISING through 3.13.15,
identically to 3.12.3, and only starts returning `False` in 3.14.7.
Measured directly against all three real interpreters
(`python-build-standalone` portable builds for 3.13.15/3.14.7, this
project's own pinned 3.12.3), `errno=13` throughout:

    3.12.3   is_file() -> raises PermissionError   _IGNORED_ERRNOS: (2, 20, 9, 40)
    3.13.15  is_file() -> raises PermissionError   _IGNORED_ERRNOS: gone (AttributeError)
    3.14.7   is_file() -> False                    _IGNORED_ERRNOS: gone (AttributeError)

So a guard reasoning "the constant still exists, so the old errno set
still applies" is already wrong reading `pathlib` itself in 3.13; a guard
reasoning "I measured `is_file()` raise on every interpreter I have" is
wrong the moment 3.14 ships. Both are version-dependent facts a
version-independent contract cannot rest on -- which is exactly why the
fix below asks neither question.

The fix used everywhere in this file now is the one `_resolve_hw_rev`
(`perf_apply.py`) and `example_catalog.catalog_unreadable` already used:
NO stat call at all. Read straight through and classify by the REAL
exception the read raises -- `FileNotFoundError` for genuinely absent,
everything else for "there but broken" -- which is correct on every
interpreter because it never depends on what a stat-based guard happens to
swallow this Python version. `TestReadExampleTree` additionally asserts the
`not_found` VALUE, not just the exception type, because a type-only
assertion could not see this class of bug at all: the old code still raised
`ExampleReadError` on 3.14+, just with the wrong `not_found`. Re-driven
directly against all three interpreters as part of THIS round's fix (not
merely asserted): `tests/gates` is `1027 passed, 34 skipped` on 3.12.3,
3.13.15 and 3.14.7 alike.

THE SHAPE SWEEP (tan-cli#1116's own instruction: seed from the SHAPE, not
from the four names the issue body already listed -- issue comment 2 found
`perf_apply.py`'s two sites exactly because a reviewer went looking in a
NEIGHBOURING module a name-only seed would have missed -- and review round
2 widened it again: an 18-of-79 hand-read triage was not sound either,
finding two more confirmed live defects in "the first handful" of the 61 it
had left unread).

`python/scripts/audit_narrow_except_contracts.py` is the walk, committed
(review round 2 MINOR: the first version's count was not reproducible --
"a one-off audit script, not a second gate" is not the same as "not
committed"). Re-running it: **65** candidates under `tan/core`/`tan/
commands`/`tan/model` (excluding `tan/planner/**`, whose import order needs
`bind_sdk_root` first -- the script's own `--planner` flag widens the walk
there, reported but not executed) whose `try` wraps a `.read_text(`/`.
read_bytes(`/`open(`/`yaml.safe_load(`/`json.load(`/`json.loads(` call with
a non-broad `except`. **79** including `tan/planner/**`.

By the end of review round 2 ALL 65 non-planner candidates have been
individually inspected -- 19 confirmed already-correct by the script's own
best-effort execution (a real non-UTF-8 file substituted for a path-shaped
first parameter), the remaining 46 read by hand because the script's
signature-guessing heuristic cannot safely call them (a multi-argument
signature, a parameter that takes already-decoded TEXT rather than a path,
a bound method). Driving the 46 by hand -- real broken files, not just
reading source and guessing, review round 2's own explicit instruction --
found, beyond the four issue-named sites:

  * TWO more `except (OSError, yaml.YAMLError)` sites missing
    `UnicodeDecodeError` exactly like `perf_apply.py`'s pair --
    `tan/model/adapters/drpai.py::_compiler_version` (round 1, FIXED) and
    `tan/planner/kconfig.py::_emit_extra_library_profile` (round 1, found,
    **deferral reversed at tan-cli#1122 -- now FIXED and seeded**, the same
    way tan-cli#1162 reversed the `_load_board_symbols` half; the upstream
    half is tracked as alp-sdk#1961, since alp-sdk still carries the defect
    and tan's planner-fallback path still executes it. READ THE NEXT
    PARAGRAPH BEFORE REUSING THE ORIGINAL
    REASON. As written at tan-cli#1116 it says `kconfig.py` is a hash-pinned
    verbatim mirror of alp-sdk's `scripts/alp_orchestrate/kconfig.py` and
    that editing tan's copy would break `test_planner_relocation_freshness.
    py`'s `PINNED_HASHES`. That mechanical claim is FALSE, measured at
    tan-cli#1122: `PINNED_HASHES`' VALUES are sha256 of
    `<ALP_SDK_ROOT>/scripts/alp_orchestrate/*.py` and its KEYS are SDK-side
    paths (`test_planner_relocation_freshness.py:43`, `:84`, `:771`), and
    nothing in that gate hashes any `tan/planner/**` file at all -- so no
    edit here can move a pin. Whatever keeps this site deferred is a
    MAINTENANCE-COST decision -- keeping tan's diff against upstream small,
    and not handing `planner_resync.py`'s three-way merge another divergence
    to carry forward -- not a gate constraint.).
  * THREE `is_file()`/`is_dir()` pre-flight checks that raised a raw
    `PermissionError` on a permission-denied ANCESTOR directory (round 1) --
    `perf_apply._resolve_hw_rev`, `metadata_schema.validate_document`,
    `scaffold.read_example_tree`, all FIXED -- the last two rewritten AGAIN
    in review round 2 once the fix itself turned out to be version-dependent
    (see THE VERSION-INDEPENDENCE LESSON above).
  * `som_buildability._safe_load_mapping` (review round 2, found by the
    reviewer): `except OSError` alone, missing `UnicodeDecodeError`, on the
    private helper behind `hw_rev_not_buildable`'s own "Never raises: a
    scaffold must not fail because a metadata file could not be read."
    FIXED.
  * `tan/planner/template.py::_docs_ref` (review round 2, found by the
    reviewer): `except OSError` alone around a `yaml.safe_load(read_text(...))`
    pair, missing BOTH `UnicodeDecodeError` and `yaml.YAMLError` -- a
    malformed OR non-UTF-8 `sdk_version.yaml` used to raise raw past this
    "cost a stale-but-safe link, not the whole scaffold" contract. FIXED --
    `template.py` is NOT in `PINNED_HASHES` (confirmed against the gate's
    own list; it is a HAND-PORT the freshness gate tracks by hashing the
    alp-sdk SOURCE it was ported from, `HAND_PORT_HASHES`, not by hashing
    this file), so the mirror argument does not apply here.
  * `tan/planner/template.py::render_to_envelope`'s example `board.yaml`
    read (review round 2 MAJOR, found by the reviewer): `except OSError`
    alone, missing `UnicodeDecodeError`, raising a curated `TemplateError`.
    FIXED, same file, same non-pinned reasoning.
  * `tan/commands/debug_config_cmd.py::_run`'s `.alp/debug-launch-
    provenance.json` sidecar read (found in review round 2's re-triage, NOT
    named by the reviewer): an `is_file()` pre-flight AND a bare
    `except OSError`, missing `UnicodeDecodeError` -- past its own
    documented "ANY failure ... degrades to `empty()`" contract. FIXED.
  * `tan/commands/new_som_cmd.py::_known_board_names` and
    `_family_hw_revisions` (found in review round 2's re-triage, NOT named
    by the reviewer): BOTH missing `OSError` entirely in their per-file
    except clause (a per-file `chmod 000`, distinct from the directory-level
    guard each already had), AND both directory/file-level guards
    (`boards_dir.is_dir()` / `path.is_file()`) had their OWN EACCES-on-
    ancestor trap, the same class as the three round-1 sites. FOUR
    confirmed escapes across the two functions, all FIXED.
  * `tan/planner/slugs.py::peripheral_kconfig` and `tan/planner/
    kconfig_symbols.py::_load_board_symbols` (found opportunistically while
    checking the pinned planner files review round 2's MINOR finding named
    as an incomplete deferral list): the same missing-`UnicodeDecodeError`
    shape. NOT fixed at tan-cli#1116 -- "both in `PINNED_HASHES`, same
    reasoning as `kconfig.py`". **The `_load_board_symbols` half of that
    deferral was reversed at tan-cli#1162 and is now seeded**, because the
    reasoning behind it does not survive `test_planner_relocation_
    freshness.py`'s own later correction: `PINNED_HASHES` pins only the
    UPSTREAM side of the comparison, `tan/planner/` "is not the verbatim
    mirror it is sometimes called" (that gate's own words -- measured at
    `7d58ef32`, 16 of 20 relocated modules already differ from upstream,
    `kconfig_symbols.py` by 329 lines), and this very file already carries a
    documented tan-only divergence (`env=spawn_env()`, tan-cli#992). The
    freshness gate was run with `ALP_SDK_ROOT` bound after the #1162 change
    and is green.

    **AND THAT REFUTATION IS NOT SPECIFIC TO `kconfig_symbols.py`.**
    tan-cli#1122 measured the mechanism directly rather than by analogy:
    `PINNED_HASHES`' values are sha256 of `<ALP_SDK_ROOT>/scripts/
    alp_orchestrate/*.py`, its keys are SDK-side paths, and nothing in
    `test_planner_relocation_freshness.py` hashes any `tan/planner/**` file
    -- so editing tan's copy of ANY relocated module cannot break a pin.
    That kills the stated reason for all five deferrals recorded above and
    below, not one of them: `slugs.py::peripheral_kconfig`,
    `kconfig.py::_emit_extra_library_profile`,
    `sdk_compat.py::read_sdk_version`, `sdk_compat.py::_hw_revision_table`
    and `buildplan.py::_sdk_version`. They stay deferred here -- fixing five
    more sites is not tan-cli#1162's job -- but they stay deferred on a
    MAINTENANCE-COST judgement (keep the diff against upstream small; do not
    hand `planner_resync.py`'s three-way merge more divergence to carry),
    which is a decision someone may reverse, and NOT on a gate constraint,
    which would be a fact nobody could. See tan-cli#1122 for the
    measurement; it is not restated here.

So: **65** non-planner candidates, reproducible; **all 65** read; **46**
of those hand-driven with real broken files (the other 19 already
confirmed safe by the script's own execution); **9** live defects found
this way in `tan/core`/`tan/commands` plus **2** in the non-pinned half of
`tan/planner`, **11 fixed in total** (the issue's own two named
`perf_apply.py` sites are two of the eleven); **6 more sites across 5
files** found in `PINNED_HASHES`-protected planner files -- `kconfig.py`
(one site), `sdk_compat.py` (two: `read_sdk_version`, `_hw_revision_table`),
`buildplan.py` (`_sdk_version`), `slugs.py` (`peripheral_kconfig`),
`kconfig_symbols.py` (`_load_board_symbols`) -- named individually above and
in `_SEEDED_CONTRACTS`' neighbouring comments, all deferred at that round on
the `PINNED_HASHES` reasoning tan-cli#1122 has since refuted (see the
`slugs.py` bullet above; FOUR of the six deferrals stand, on maintenance
cost rather than on the gate -- `kconfig_symbols.py`'s was reversed at
tan-cli#1162 and `kconfig.py`'s at tan-cli#1122), all reported rather than
silently skipped. **15** seeded
into `_SEEDED_CONTRACTS` below AT THAT ROUND (twenty-six today; every
count in this paragraph is tan-cli#1116's own measurement, kept as the
record of what that sweep found rather than silently re-stated) -- the
twelve from review round 1 plus `som_buildability.hw_rev_not_buildable`,
`new_som_cmd._known_board_names` and `new_som_cmd._family_hw_revisions`.
`template.py::_docs_ref`/`render_to_envelope` are fixed but NOT seeded here
(deliberately, not an oversight): seeding either needs `bind_sdk_root`,
global mutable state this gate's other 15 seeds do not touch and this file
declines to be the first to introduce. Both are instead covered by a
COMMITTED test (review round 3 minor: an earlier draft of this fix shipped
neither fix with a test at all -- `tests/planner/test_docs_ref_and_
render_to_envelope_encoding.py`, SDK-gated the same way `tests/planner/
test_render_to_envelope_malformed_example_board.py` already is), mutation-
proved the same way as this file's own seeds (byte-copy restore,
`__pycache__` cleared before and after, each reds specifically on its own
narrowed `except`).

**THAT DECISION WAS REVERSED at tan-cli#1133, and what reversed it is what
it cost.** Two MORE sites in that same `template.py` -- `_load_som_doc` and
`_board_route_entries`, one call away from the read #1116 fixed and reached
in the SAME `emit_scaffold` invocation -- were left unseeded on the same
reasoning, then went unfound by `scripts/audit_narrow_except_contracts.py`
(they had no `try` at all, so the too-narrow-a-`try` sweep was structurally
blind to them), and shipped: a non-UTF-8 byte, a malformed YAML document
and a `chmod 000` file each escaped `emit_scaffold` raw, past a caller that
catches `TemplateError` and nothing else, on 3.12.3, 3.13.15 and 3.14.7
alike. A committed test next door is not the same protection as a seed
here, because only the seed list is walked for completeness. Both are
seeded now, at the bottom of this file, and the `bind_sdk_root` cost is
paid in the narrowest available way: NO autouse fixture (importing
`_bound_sdk` would bind for all twenty seeds and error outright when
`SDK is None`), a per-class `skipif`, and the bind inside each call. The
older two (`_docs_ref`, `render_to_envelope`) stay where they are -- both
already have committed tests, and #1133 does not re-open a decision it did
not need to.

PR #1160's review then found a THIRD planner site the same way, and it is
the one that settles the question: `template._rendered_bytes`, whose bare
per-file `read_bytes()` is on the hottest read path in the module (4-7
files per scaffold). The first cut of #1133 had it in hand -- its own new
shape-3 detector printed it -- and filed it as "a candidate to read"
rather than driving it. Seeded here, driven, and the lesson is the same one
the reversal above records: a candidate this list does not hold is a
candidate that ships.

The gap between 65 and 15 is real and recorded here rather than implied
clean: 19 are already-correct functions this file has not been asked to
seed, 6 are found-and-fixed-but-not-yet-seeded (the template.py pair) or
found-and-deferred (the six pinned files), and the remaining are the
already-broad-enough `except` clauses (`except Exception`/`BaseException`,
excluded from the walk itself) plus curated-raise functions correctly
converting to a project exception type (`model_cmd._load_board`,
`kconfig_symbols._run_kconfig`'s `_CoreResolutionError` ladder, and
similar) that this file has likewise not been asked to seed.

THE ROOT/CI CAVEAT (tan-cli#1105's own lesson, named explicitly in this
issue so it is not silently repeated -- corrected in review round 2, whose
BLOCKER named this section wrong on BOTH the job and the axis, and the half
it got wrong was the half that actually broke): every permission-denied
shape below is `@_skip_as_root`, which SKIPS -- loudly, by name, with a
reason string a CI log shows -- under `os.geteuid() == 0` or on Windows.

**Two jobs run `tests/gates`, not one** -- `ci.yml`'s own comment says so
verbatim ("this job is one of the two `tests/gates` legs in CI; the other
is `parity.yml`'s `seam1-plan-shape`"). `ci.yml`'s `python` job is
DELIBERATELY NOT a required context (its own header says so); `parity.yml`'s
`seam1` IS required, and `seam1` is the leg this gate actually broke on.
Both run on `ubuntu-latest` with no `container:` key, so both run as the
unprivileged `runner` account, not root -- the chmod-000 shape genuinely
executes on both, and `_skip_as_root` is not a silent gap on either. That
much of the ORIGINAL caveat held.

**What the original caveat missed is the axis that actually decided
`seam1`'s outcome: the INTERPRETER VERSION, not root-vs-non-root.**
`ci.yml`'s `python` job pins `python-version: "3.12"`; `seam1` uses
`python-version: "3.x"`, which resolved to CPython 3.14.7 the run this gate
broke on. `chmod 000` worked identically on both -- the bug was never a
skipped shape, it was `pathlib` behaving differently across interpreters
underneath a shape that DID run (see THE VERSION-INDEPENDENCE LESSON above
for exactly which release changes what -- the constant `pathlib.
_IGNORED_ERRNOS` and the raising behaviour it once named do not vanish in
the same release, and neither citation alone pins the real boundary).
Driven and confirmed on all THREE real interpreters as part of fixing
this: portable `python-build-standalone` builds of 3.13.15 and 3.14.7
(the exact patch release `seam1` reported) alongside the repo's pinned
3.12.3, side by side -- every seeded shape green on all three, not
asserted here, run (`tests/gates`: `1027 passed, 34 skipped` on each).

TAN-CLI#1127 FOLLOW-UP: `new_som_cmd._known_board_names`'s own primitive
choice -- `boards_dir.glob("*.yaml")` wrapped in `except OSError` -- turned
out to have the SAME version-skew shape THE VERSION-INDEPENDENCE LESSON
describes for `is_file()`/`is_dir()`, just one level further down the
`pathlib` stack: `Path.glob` raises for a permission-denied ancestor on
3.12.3, but returns an empty iterator SILENTLY for the identical shape on
3.13.15 and 3.14.7 (measured). Its `except OSError` was dead code on those
two interpreters; the function stayed correct only because `return names or
None` already treated "found nothing" the same as "permission denied" --
an accident, not a contract. `_known_board_names` now uses `os.listdir`
instead, which raises on all three measured interpreters for the same
shape, making the `except OSError` load-bearing everywhere. Its neighbour
`_family_hw_revisions` was re-checked against the same question and does
NOT share the defect: it never called `Path.glob` (or any stat-based
pre-flight) to begin with, only `Path.read_text()`, which raises
`PermissionError` for a permission-denied ancestor identically on all
three interpreters -- confirmed by measurement, not left as an inference
from the `_known_board_names` finding next door. `TestKnownBoardNames.
test_permission_denied_ancestor` seeds the new shape;
`TestFamilyHwRevisions.test_permission_denied` (unchanged) already covered
`_family_hw_revisions`'s equivalent case correctly before this issue.

TAN-CLI#1132 FOLLOW-UP -- THE OTHER HALF OF THE SAME CLASS. #1127 above is
the SWALLOW half: the exception never exists, because `Path.glob` answers an
empty iterator instead of raising. #1132 is the ESCAPE half: the exception
DOES exist, and the `try` written to catch it is not around the code that
raises. `Path.glob` is lazy -- it returns a generator and does the
filesystem work on ITERATION -- so

    try:
        matches = app_dir.glob(pattern)   # cannot fail: builds a generator
    except OSError:
        continue
    for path in matches:                  # the real work, outside the try

has an `except OSError` that is dead on EVERY interpreter. `_known_board_
names` escaped that only because it wrote `sorted(boards_dir.glob(...))`,
and `sorted()` forced iteration inside its `try`. A sweep for one half of
this class does not find the other, which is why
`scripts/audit_narrow_except_contracts.py` now detects the lazy-iterator
shape explicitly (tan-cli#1132) rather than only the too-narrow-`except`
one.

Two live sites, both seeded below. `configure_inputs.discover_configure_
inputs` had BOTH faults in one function -- the dead lazy-glob `except` and
an `is_dir()` pre-flight of exactly the kind THE VERSION-INDEPENDENCE
LESSON describes -- and, measured against a `chmod 000` PARENT, raised
`PermissionError` on 3.12.3 and 3.13.15 while returning `frozenset()` on
3.14.7, against a docstring promising the empty set. `analyze._resolve_
table` had the same `is_dir()` pre-flight plus a completely UNGUARDED
`sorted(table_dir.glob("*.json"))`, and raised on the same two
interpreters against a docstring promising `None`. Both now list through
`os.scandir`/`os.listdir`, the primitive that raises identically on all
three; both were re-measured, all four cells green on all three
interpreters, and both mutation-proved by deleting their `except` clause
one at a time -- each reds on 3.12.3, 3.13.15 AND 3.14.7, which is the
property neither had before (deleting the old handlers changed nothing
anywhere, because they never fired).
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from tan.commands import new_som_cmd
from tan.commands.build import configure_inputs
from tan.core import document_guards, error_catalog, example_catalog, example_facets
from tan.core import metadata_schema, scaffold, sdk_discovery, shapes, som_buildability
from tan.model import analyze, perf, perf_apply
from tan.model.adapters import drpai
# `SDK` ONLY -- deliberately NOT the `_bound_sdk` fixture next to it, which
# is autouse and would bind for every seed in this file. See the tan-cli#1133
# section at the bottom, which is the only part of this module that uses it.
# The name is captured at that module's import time, before `tests/conftest.
# py`'s autouse `_scrub_sdk_discovery_env` deletes `ALP_SDK_ROOT`.
from tests.planner._bound_sdk_fixture import SDK

#: Opt-in seed list -- `{"module.qualname": "why this function's contract
#: matters"}`. See the module docstring for what is and is not asserted.
#: `test_every_seed_has_a_test` (bottom of file) keeps this in lockstep with
#: the test functions below: a name added here with no matching test, or a
#: test added below for a name not here, reds that check.
_SEEDED_CONTRACTS: dict[str, str] = {
    "document_guards.DocumentGuards.read_catalog_document": (
        "PR #1096's own site: raises ONLY the injected curated error, never "
        "a raw OSError/UnicodeDecodeError/JSONDecodeError"
    ),
    "example_catalog.catalog_unreadable": (
        "tan-cli#1101's own site (PR #1110's `Path.exists()` pre-flight "
        "trap): a genuine read failure returns a message string, never "
        "raises and never silently returns None"
    ),
    "example_catalog.unsupported_som": (
        "the catalog-unreadable ancestor of catalog_unreadable above: folds "
        "EVERY failure to None, its own written 'Never raises' contract"
    ),
    "perf.read_perf_point": (
        "PR #1114's sibling site: None on every failure shape, never raises"
    ),
    "perf_apply._resolve_hw_rev": (
        "issue comment 2's live site (perf_apply.py:149): None on every "
        "failure; also had the EACCES-through-is_file() pre-flight trap, "
        "fixed in the same change"
    ),
    "perf_apply._topology_core_ids": (
        "issue comment 2's live site (perf_apply.py:245): set() on every "
        "failure shape, never raises"
    ),
    "metadata_schema.validate_document": (
        "named by PR #1096's review as a prior instance (metadata_schema."
        "py:209); had the EACCES-through-is_file() pre-flight trap on its "
        "own ABSENT check, fixed in the same change"
    ),
    "example_facets.load_example_facets": (
        "same catalog-degrades-silently family as example_catalog and "
        "error_catalog, its own written 'Never raises' contract"
    ),
    "sdk_discovery._read_file": (
        "named by PR #1096's review as a prior instance (sdk_discovery.py:"
        "189); already correctly widened, kept honest by this seed"
    ),
    "error_catalog.load_codes": (
        "named by PR #1096's review as a prior instance (error_catalog.py:"
        "104); a curated-raise contract like read_catalog_document -- "
        "raises ONLY CatalogUnreadable, never a raw stdlib exception"
    ),
    "drpai._compiler_version": (
        "found by this issue's own shape sweep, not named in the issue "
        "body: 'Never raises' but only caught OSError, missing "
        "UnicodeDecodeError; fixed in the same change"
    ),
    "scaffold.read_example_tree": (
        "named by PR #1096's review as a prior instance (scaffold.py:2034); "
        "a curated-raise contract (ExampleReadError); had the EACCES-"
        "through-is_dir() pre-flight trap, fixed in the same change"
    ),
    "som_buildability.hw_rev_not_buildable": (
        "found in the round-2 re-triage after review round 2 (tan-cli#1116): "
        "its own docstring says 'Never raises: a scaffold must not fail "
        "because a metadata file could not be read', and its private "
        "_safe_load_mapping caught OSError alone, missing UnicodeDecodeError "
        "-- measured escaping through this real caller; fixed in the same "
        "change"
    ),
    "new_som_cmd._known_board_names": (
        "found in the round-2 re-triage: 'None when the directory is "
        "missing' is its whole contract, but its per-file except clause "
        "(yaml.YAMLError, UnicodeDecodeError) dropped OSError entirely, so "
        "a per-FILE chmod 000 (distinct from the containing-directory "
        "listing failure, originally gated by a boards_dir.is_dir() "
        "pre-flight and now by the except OSError around the os.listdir "
        "call tan-cli#1127 replaced it with) escaped raw; fixed in the "
        "same change"
    ),
    "new_som_cmd._family_hw_revisions": (
        "found in the round-2 re-triage: same shape and same fix as "
        "_known_board_names next door -- 'None' on 'not resolvable at "
        "scaffold time' is the whole contract, and the same per-file "
        "chmod 000 escaped raw past the same missing OSError; fixed in the "
        "same change"
    ),
    "configure_inputs.discover_configure_inputs": (
        "tan-cli#1132's first live site: 'Returns the empty set for a "
        "missing/unreadable app_dir' is its written contract, and it "
        "raised PermissionError on 3.12.3/3.13.15 for a chmod-000 PARENT "
        "-- an is_dir() pre-flight plus a lazy Path.glob iterated OUTSIDE "
        "its own try, so the except OSError was dead on all three "
        "interpreters; os.scandir + one live handler now"
    ),
    "analyze._resolve_table": (
        "tan-cli#1132's second live site: 'None when the backend has no "
        "table directory' is the whole contract behind the caller's "
        "`undetermined` verdict, and it raised PermissionError on "
        "3.12.3/3.13.15 for the same shape past an is_dir() pre-flight and "
        "a completely unguarded sorted(table_dir.glob(...)); os.listdir + "
        "one live handler now"
    ),
    "template._load_som_doc": (
        "tan-cli#1133's first live site, and the first PLANNER seed here "
        "(see the module docstring's own reversal): an is_file() pre-flight "
        "then a wholly UNGUARDED read+parse -- no `try` at any point, which "
        "is why the too-narrow-a-`try` sweep could not see it -- so a "
        "non-UTF-8 byte, a malformed YAML document and a chmod-000 file "
        "each escaped raw past its own curated-raise TemplateError contract "
        "and out through `cli._emit_scaffold`'s `except TemplateError` and "
        "nothing else; measured on 3.12.3, 3.13.15 and 3.14.7 alike"
    ),
    "template._board_route_entries": (
        "tan-cli#1133's second live site, one document over and the same "
        "shape line for line: same is_file() pre-flight, same bare "
        "read+parse, same three raw exceptions measured escaping the same "
        "curated contract on the same three interpreters"
    ),
    "uri_reference.cwd_base_uri_or_none": (
        "tan-cli#1134 part 1, and the archetype the gate had somehow never "
        "seeded: its ENTIRE reason for existing is 'returns None instead of "
        "raising'. PR #1125 added it because `cwd_base_uri()`'s `Path.cwd()` "
        "raises FileNotFoundError when the process CWD has been removed, and "
        "that raise DOUBLE-FAULTED inside `validate_cmd.py`'s own `except "
        "Exception as err:  # never a bare traceback; the envelope is the "
        "contract` handler -- the handler calls `_emit` -> `_sarif_document`, "
        "which raised again. A textbook member of this class, added in the "
        "same week the gate landed, unguarded until now. Not a fix: the "
        "function is correct, and this is the pin that keeps it correct"
    ),
    "libraries.load_manifest": (
        "tan-cli#1162's first site: an is_file() pre-flight then a wholly "
        "unguarded `yaml.safe_load(path.read_text(...))`, past its own "
        "curated OrchestratorError contract. Measured escaping raw on "
        "3.12.3/3.13.15/3.14.7 alike -- UnicodeDecodeError for a non-UTF-8 "
        "manifest, yaml.parser.ParserError for a malformed one, "
        "PermissionError for a chmod-000 one -- and on the chmod-000 PARENT "
        "shape the pre-flight ALSO produced a curated message that was FALSE "
        "on 3.14.7 (`unknown library ... Available: <none>`, for a manifest "
        "that is there). Reached from `tan build` through "
        "`kconfig.py:1002`'s resolve_selection, where `build_cmd.py:505` "
        "absorbed it into a build.plan-unavailable envelope naming the "
        "exception TYPE rather than the file"
    ),
    "zephyr_board._load_soc_spec": (
        "tan-cli#1162's second site, same shape one serialisation over: "
        "is_file() pre-flight then a bare `json.loads(soc_path.read_text"
        "(...))` past a curated ZephyrBoardEmitError contract. NOT on the "
        "`tan build` path (the issue body said it was; its only caller is "
        "emit_zephyr_board, so `tan generate --emit zephyr-board` and "
        "`generate_cmd.py:890`'s except BaseException) -- the severity "
        "differs from load_manifest's and the seed says so"
    ),
    "kconfig_symbols._load_board_symbols": (
        "tan-cli#1162's third site, and the one whose broken input is not "
        "hypothetical: `alp_kconfig.json` is written by an EXTERNAL `west "
        "build -t` subprocess, so a directory in its place, a non-UTF-8 dump "
        "or an unreadable mode are all things a third-party build step can "
        "really leave. Its parse and shape halves were already guarded; the "
        "READ was bare behind an is_file() pre-flight. Deferred at "
        "tan-cli#1116 as PINNED_HASHES-protected -- superseded, see this "
        "module's own note: only the UPSTREAM side of that comparison is "
        "pinned and this file already carries a documented tan-only "
        "divergence (tan-cli#992)"
    ),
    "topology._core_os_choices": (
        "tan-cli#1162's fourth site, and the only one whose pre-flight was a "
        "SELECTOR rather than a guard: `is_file()` chose between the "
        "project's own board schema and the in-tree BOARD_SCHEMA, and "
        "answered False to `denied` and `is a directory` exactly as it did "
        "to `not there` -- so an unreadable project schema silently fell "
        "back onto a DIFFERENT document and the caller was told an OS set "
        "the project never declared (measured: NO RAISE, wrong answer, on "
        "3.14.7; raw PermissionError on 3.12.3/3.13.15). On the `tan build`, "
        "`tan validate`, `tan generate` and `tan kconfig` paths alike, "
        "through loader.py:1009 -> validate.py:343"
    ),
    "template._rendered_bytes": (
        "the FOURTH site, found by PR #1160's review after the first cut of "
        "the fix filed it as 'a candidate to read' instead of driving it -- "
        "and the highest-traffic one, since every catalog template lists "
        "5-8 files.user_owned entries and 4-7 of them are read here per "
        "scaffold. Its per-file read_bytes() was bare: chmod 000 escaped as "
        "raw PermissionError, a deleted file as raw FileNotFoundError, a "
        "directory as raw IsADirectoryError, on 3.12.3/3.13.15/3.14.7 alike"
    ),
    "kconfig._emit_extra_library_profile": (
        "tan-cli#1122: the sixth PINNED_HASHES-protected site the #1116 "
        "sweep found and deferred on a since-refuted mechanical claim (see "
        "this file's own module docstring) -- 'never fails the whole "
        "build' was the contract, and `except (OSError, yaml.YAMLError)` "
        "missed UnicodeDecodeError for a non-UTF-8 profile, the same shape "
        "as drpai._compiler_version above. Also had its own version-skew "
        "trap one level further in: the unguarded `.resolve()` this fix "
        "removed raised a bare RuntimeError for a symlink loop on 3.12.3 "
        "(the same shape template.py::_safe_join documents), which no "
        "widening of the except tuple alone would have caught"
    ),
}

_covered: set[str] = set()


def _covers(name: str):
    """Decorator recording that a test group below exercises @name, the
    other half of the `_SEEDED_CONTRACTS` <-> test-group lockstep
    `test_every_seed_has_a_test` checks."""
    assert name in _SEEDED_CONTRACTS, f"{name!r} is not in _SEEDED_CONTRACTS"

    def _wrap(fn):
        _covered.add(name)
        return fn

    return _wrap


# ---------------------------------------------------------------------------
# Shape builders -- the seven real failure shapes issue #1116 names, each a
# plain function that leaves a broken path on disk (or a context manager for
# the one shape that must be undone). Every seeded function below is driven
# against whichever of these its own read can actually meet.
# ---------------------------------------------------------------------------


def _non_utf8(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00not-utf8")


def _malformed(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _as_directory(path: Path) -> None:
    path.mkdir(parents=True)


def _parent_is_a_file(path: Path) -> None:
    """Makes @path's own PARENT a regular file, so @path itself cannot
    exist -- "the parent path is a file, not a directory" shape."""
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    path.parent.write_text("not a directory", encoding="utf-8")


def _symlink_loop(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(path)


_skip_as_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root: chmod 0o000 has no effect for root and "
    "Windows ACLs don't honour POSIX mode bits. See the module docstring's "
    "ROOT/CI CAVEAT -- ci.yml's ubuntu-latest python job (no `container:`) "
    "runs as the unprivileged `runner` account, so this shape DOES execute "
    "there; it skips only for a human running as root locally or a runner "
    "that overrides the default user.",
)


class _OsWithName:
    """The real `os` module with ONE attribute overridden: `name`.

    tan-cli#1132: the obvious way to ask a platform-casing question is
    `monkeypatch.setattr(new_som_cmd.os, "name", "nt")` -- what
    `TestIsYamlBoardFile` (line 1304) and `TestKnownBoardNames` above
    spell, and correctly so for them. But `new_som_cmd.os` IS the real `os`
    module object, so that mutates `os.name` process-wide, and `pathlib`
    reads `os.name` at construction time to choose between `PosixPath` and
    `WindowsPath`. Any function that builds a FRESH `Path` under that patch
    therefore dies with `NotImplementedError: cannot instantiate
    'WindowsPath' on your system` instead of exercising the rule under
    test. `_known_board_names` never does (`boards_dir / entry` reuses the
    existing class), which is why the older spelling is fine there; both
    tan-cli#1132 sites DO (`analyze._resolve_table`'s `Path(metadata_root)`,
    the walk's `Path(entry.path)`) -- measured, both reds were exactly that
    `NotImplementedError`.

    So the two #1132 groups rebind the `os` NAME inside `tan.core.shapes`
    to this object instead of mutating the module: `shapes.
    matches_glob_suffix` is the only reader of `os.name` on either path,
    and `os.path` and everything else still resolve to the real module
    through `__getattr__`.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __getattr__(self, attr: str):
        return getattr(os, attr)


@contextlib.contextmanager
def _permission_denied(dir_path: Path):
    """chmod 0o000 on @dir_path (which must already contain whatever file
    the caller is about to try reading) for the duration of the block."""
    dir_path.mkdir(parents=True, exist_ok=True)
    original_mode = dir_path.stat().st_mode
    dir_path.chmod(0o000)
    try:
        yield
    finally:
        dir_path.chmod(original_mode)


@contextlib.contextmanager
def _permission_denied_file(file_path: Path):
    """chmod 0o000 on @file_path ITSELF (which must already exist), for the
    duration of the block -- the distinct "per-file, containing directory
    stays listable" shape `new_som_cmd._known_board_names` needed
    (tan-cli#1116 review round 2): `stat()` only needs the CONTAINING
    directory to be searchable, so this shape is not reachable through
    `_permission_denied` above, which denies the directory."""
    original_mode = file_path.stat().st_mode
    file_path.chmod(0o000)
    try:
        yield
    finally:
        file_path.chmod(original_mode)


# ---------------------------------------------------------------------------
# document_guards.DocumentGuards.read_catalog_document -- curated-raise.
# ---------------------------------------------------------------------------


class _SeedCatalogError(Exception):
    """The injected curated error `DocumentGuards` is constructed with in
    these tests -- standing in for `example_catalog.MalformedCatalogError`/
    `tan.planner.template.TemplateError`, the two real callers use. The
    CONTRACT under test is "only this class, or a subclass, ever escapes" --
    a raw `UnicodeDecodeError`/`OSError`/`JSONDecodeError` fails every one of
    these via `pytest.raises`' own strict-type match, which is the point."""


@_covers("document_guards.DocumentGuards.read_catalog_document")
class TestReadCatalogDocument:
    def _guards(self) -> document_guards.DocumentGuards:
        return document_guards.DocumentGuards(_SeedCatalogError)

    def test_absent(self, tmp_path):
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(tmp_path / "catalog.json")

    def test_non_utf8(self, tmp_path):
        path = tmp_path / "catalog.json"
        _non_utf8(path)
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(path)

    def test_directory_where_file_expected(self, tmp_path):
        path = tmp_path / "catalog.json"
        _as_directory(path)
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(path)

    def test_parent_is_a_file(self, tmp_path):
        path = tmp_path / "parent" / "catalog.json"
        _parent_is_a_file(path)
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(path)

    def test_symlink_loop(self, tmp_path):
        path = tmp_path / "catalog.json"
        _symlink_loop(path)
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(path)

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        sub = tmp_path / "sub"
        path = sub / "catalog.json"
        sub.mkdir()
        path.write_text("{}")
        with _permission_denied(sub):
            with pytest.raises(_SeedCatalogError):
                self._guards().read_catalog_document(path)

    def test_malformed_document(self, tmp_path):
        path = tmp_path / "catalog.json"
        _malformed(path, "{")
        with pytest.raises(_SeedCatalogError):
            self._guards().read_catalog_document(path)


# ---------------------------------------------------------------------------
# example_catalog.catalog_unreadable -- quiet-return, TWO declared shapes:
# None for "absent", a non-None string for every other read failure.
# ---------------------------------------------------------------------------


def _catalog_path(sdk_root: Path) -> Path:
    return sdk_root / example_catalog.CATALOG_RELATIVE


_EXAMPLE_SRC = "peripheral/some-example"


@_covers("example_catalog.catalog_unreadable")
class TestCatalogUnreadable:
    def test_absent(self, tmp_path):
        assert example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC) is None

    def test_non_utf8(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)

    def test_directory_where_file_expected(self, tmp_path):
        path = _catalog_path(tmp_path)
        _as_directory(path)
        result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_catalog_path(tmp_path))
        result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_catalog_path(tmp_path))
        result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"templates": []}')
        with _permission_denied(path.parent):
            result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)

    def test_malformed_document(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "{")
        result = example_catalog.catalog_unreadable(tmp_path, _EXAMPLE_SRC)
        assert result is not None and isinstance(result, str)


# ---------------------------------------------------------------------------
# example_catalog.unsupported_som -- quiet-return, ONE declared shape: None
# on absolutely every failure (unlike catalog_unreadable's two).
# ---------------------------------------------------------------------------


@_covers("example_catalog.unsupported_som")
class TestUnsupportedSom:
    def test_absent(self, tmp_path):
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None

    def test_non_utf8(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(_catalog_path(tmp_path))
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_catalog_path(tmp_path))
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_catalog_path(tmp_path))
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"templates": []}')
        with _permission_denied(path.parent):
            result = example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301")
        assert result is None

    def test_malformed_document(self, tmp_path):
        path = _catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "{")
        assert example_catalog.unsupported_som(tmp_path, _EXAMPLE_SRC, "E1M-AEN301") is None


# ---------------------------------------------------------------------------
# perf.read_perf_point -- quiet-return, None on every failure.
# ---------------------------------------------------------------------------


@_covers("perf.read_perf_point")
class TestReadPerfPoint:
    def test_absent(self, tmp_path):
        assert perf.read_perf_point(tmp_path / "point.json") is None

    def test_non_utf8(self, tmp_path):
        path = tmp_path / "point.json"
        _non_utf8(path)
        assert perf.read_perf_point(path) is None

    def test_directory_where_file_expected(self, tmp_path):
        path = tmp_path / "point.json"
        _as_directory(path)
        assert perf.read_perf_point(path) is None

    def test_parent_is_a_file(self, tmp_path):
        path = tmp_path / "parent" / "point.json"
        _parent_is_a_file(path)
        assert perf.read_perf_point(path) is None

    def test_symlink_loop(self, tmp_path):
        path = tmp_path / "point.json"
        _symlink_loop(path)
        assert perf.read_perf_point(path) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        sub = tmp_path / "sub"
        path = sub / "point.json"
        sub.mkdir()
        path.write_text("{}")
        with _permission_denied(sub):
            assert perf.read_perf_point(path) is None

    def test_malformed_document(self, tmp_path):
        path = tmp_path / "point.json"
        _malformed(path, "{")
        assert perf.read_perf_point(path) is None


# ---------------------------------------------------------------------------
# perf_apply._resolve_hw_rev / perf_apply._topology_core_ids -- issue
# comment 2's own two live sites (perf_apply.py:149, :245). Same preset
# file location, different quiet value on failure (None vs set()).
# ---------------------------------------------------------------------------


def _preset_path(metadata_root: Path, sku: str = "E1M-TEST") -> Path:
    return metadata_root / "e1m_modules" / f"{sku}.yaml"


@_covers("perf_apply._resolve_hw_rev")
class TestResolveHwRev:
    def test_absent(self, tmp_path):
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    def test_non_utf8(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(_preset_path(tmp_path))
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_preset_path(tmp_path))
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_preset_path(tmp_path))
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("default_hw_rev: r2\n")
        with _permission_denied(path.parent):
            assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None

    def test_malformed_document(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "a: [1, 2")
        assert perf_apply._resolve_hw_rev("E1M-TEST", tmp_path, None) is None


@_covers("perf_apply._topology_core_ids")
class TestTopologyCoreIds:
    def test_absent(self, tmp_path):
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    def test_non_utf8(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(_preset_path(tmp_path))
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_preset_path(tmp_path))
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_preset_path(tmp_path))
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("topology: {m55_hp: zephyr}\n")
        with _permission_denied(path.parent):
            assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()

    def test_malformed_document(self, tmp_path):
        path = _preset_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "a: [1, 2")
        assert perf_apply._topology_core_ids("E1M-TEST", tmp_path) == set()


# ---------------------------------------------------------------------------
# metadata_schema.validate_document -- quiet-return: [] ONLY for a
# `FileNotFoundError` (the ABSENT half of its own two-shape docstring), a
# one-message list for every OTHER way the read can fail -- including a
# directory, a file parent, or an ELOOP loop, none of which are "absent"
# (tan-cli#1116 review round 2: a first version classified those three as
# ABSENT via a since-deleted `is_file()` pre-flight, which was wrong even on
# the interpreter it worked on -- a directory at the schema path is not
# "nothing to disclose", it is exactly the second bullet's "exists but
# cannot be read"; `FileNotFoundError` is now the ONLY absent signal, on
# every interpreter, because nothing here ever calls `is_file()`).
# ---------------------------------------------------------------------------


@_covers("metadata_schema.validate_document")
class TestValidateDocument:
    def test_absent(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        assert metadata_schema.validate_document({}, schema_path, source="src") == []

    def test_non_utf8(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        _non_utf8(schema_path)
        result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1

    def test_directory_where_file_expected(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        _as_directory(schema_path)
        # NOT absent: `_cached_validator`'s `stat()` succeeds on a directory,
        # and the read past it raises `IsADirectoryError` -- the UNREADABLE
        # half, a one-message list, version-independently (tan-cli#1116
        # review round 2 -- see the class comment above).
        result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1

    def test_parent_is_a_file(self, tmp_path):
        schema_path = tmp_path / "parent" / "schema.json"
        _parent_is_a_file(schema_path)
        # NOT absent: `stat()` raises `NotADirectoryError`, not
        # `FileNotFoundError` -- the UNREADABLE half.
        result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1

    def test_symlink_loop(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        _symlink_loop(schema_path)
        # NOT absent: `stat()` raises `OSError` (ELOOP) -- the UNREADABLE
        # half.
        result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # tan-cli#1116 review round 2 BLOCKER, re-derived: the first version
        # of this fix guarded a pre-flight `is_file()` with `except OSError:
        # exists = True`, which only helped through Python 3.13.15 --
        # `is_file()` there still raises `PermissionError` for a
        # permission-denied ancestor, identically to 3.12.3, even though
        # `pathlib`'s `_IGNORED_ERRNOS` CONSTANT the reasoning cites is
        # already gone by 3.13. Only in 3.14.7 does `is_file()` itself start
        # swallowing EVERY `OSError` including `EACCES` and returning
        # `False` -- the guard never fired there, and this test was measured
        # RED in CI's seam1 leg (`python-version: "3.x"`, resolved to
        # CPython 3.14.7) with `len(0) == 1`: the fix silently returned
        # `[]`, "everything is valid," on a schema this could not even
        # read. The rewrite removes `is_file()` entirely, so this shape is
        # now driven correctly on every supported interpreter -- this test
        # is what proves that, not just a single local run (re-run against
        # 3.12.3, 3.13.15 and 3.14.7 alike).
        sub = tmp_path / "sub"
        schema_path = sub / "schema.json"
        sub.mkdir()
        schema_path.write_text('{"type": "object"}')
        with _permission_denied(sub):
            result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1

    def test_malformed_document(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        _malformed(schema_path, "{")
        result = metadata_schema.validate_document({}, schema_path, source="src")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# example_facets.load_example_facets -- quiet-return, {} on every failure.
# ---------------------------------------------------------------------------


def _facets_catalog_path(sdk_root: Path) -> Path:
    return sdk_root / example_facets.CATALOG_RELATIVE


@_covers("example_facets.load_example_facets")
class TestLoadExampleFacets:
    def test_absent(self, tmp_path):
        assert example_facets.load_example_facets(tmp_path) == {}

    def test_non_utf8(self, tmp_path):
        path = _facets_catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        assert example_facets.load_example_facets(tmp_path) == {}

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(_facets_catalog_path(tmp_path))
        assert example_facets.load_example_facets(tmp_path) == {}

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_facets_catalog_path(tmp_path))
        assert example_facets.load_example_facets(tmp_path) == {}

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_facets_catalog_path(tmp_path))
        assert example_facets.load_example_facets(tmp_path) == {}

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _facets_catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        with _permission_denied(path.parent):
            assert example_facets.load_example_facets(tmp_path) == {}

    def test_malformed_document(self, tmp_path):
        path = _facets_catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "{")
        assert example_facets.load_example_facets(tmp_path) == {}


# ---------------------------------------------------------------------------
# sdk_discovery._read_file -- quiet-return, None on every failure.
# ---------------------------------------------------------------------------


@_covers("sdk_discovery._read_file")
class TestSdkDiscoveryReadFile:
    def test_absent(self, tmp_path):
        assert sdk_discovery._read_file(tmp_path / "f.txt") is None

    def test_non_utf8(self, tmp_path):
        path = tmp_path / "f.txt"
        _non_utf8(path)
        assert sdk_discovery._read_file(path) is None

    def test_directory_where_file_expected(self, tmp_path):
        path = tmp_path / "f.txt"
        _as_directory(path)
        assert sdk_discovery._read_file(path) is None

    def test_parent_is_a_file(self, tmp_path):
        path = tmp_path / "parent" / "f.txt"
        _parent_is_a_file(path)
        assert sdk_discovery._read_file(path) is None

    def test_symlink_loop(self, tmp_path):
        path = tmp_path / "f.txt"
        _symlink_loop(path)
        assert sdk_discovery._read_file(path) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        sub = tmp_path / "sub"
        path = sub / "f.txt"
        sub.mkdir()
        path.write_text("x")
        with _permission_denied(sub):
            assert sdk_discovery._read_file(path) is None

    def test_malformed_document(self, tmp_path):
        # `_read_file` has no document shape of its own to be malformed --
        # it returns raw text, verbatim, to a caller that parses it. Nothing
        # to drive here beyond the six shapes above; recorded rather than
        # silently omitted.
        pytest.skip("_read_file has no parse step of its own to malform")


# ---------------------------------------------------------------------------
# error_catalog.load_codes -- curated-raise: CatalogUnreadable, never a raw
# stdlib exception, on every failure including "absent" (unlike the
# quiet-return functions above, this one always raises).
# ---------------------------------------------------------------------------


@_covers("error_catalog.load_codes")
class TestLoadCodes:
    def test_absent(self, tmp_path):
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)

    def test_non_utf8(self, tmp_path):
        path = error_catalog.catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(error_catalog.catalog_path(tmp_path))
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(error_catalog.catalog_path(tmp_path))
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(error_catalog.catalog_path(tmp_path))
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = error_catalog.catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"codes": {}}')
        with _permission_denied(path.parent):
            with pytest.raises(error_catalog.CatalogUnreadable):
                error_catalog.load_codes(tmp_path)

    def test_malformed_document(self, tmp_path):
        path = error_catalog.catalog_path(tmp_path)
        path.parent.mkdir(parents=True)
        _malformed(path, "{")
        with pytest.raises(error_catalog.CatalogUnreadable):
            error_catalog.load_codes(tmp_path)


# ---------------------------------------------------------------------------
# drpai._compiler_version -- quiet-return: always a str (the fallback tag
# when nothing parses), never raises. Found by this issue's own shape sweep.
# ---------------------------------------------------------------------------


@_covers("drpai._compiler_version")
class TestDrpaiCompilerVersion:
    def test_absent(self, tmp_path):
        assert drpai._compiler_version(tmp_path) == "drp-ai_tvm"

    def test_non_utf8(self, tmp_path):
        (tmp_path / "VERSION").write_bytes(b"\xff\xfe\x00bad")
        assert isinstance(drpai._compiler_version(tmp_path), str)

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(tmp_path / "VERSION")
        assert isinstance(drpai._compiler_version(tmp_path), str)

    def test_parent_is_a_file(self, tmp_path):
        # tan-cli#1116 review round 2 MINOR: a first version of this test
        # passed a nonexistent `tvm_home`, which hits the identical
        # `FileNotFoundError` arm `test_absent` already drives -- measured,
        # both returned the same fallback, so this shape was a silent no-op
        # duplicate rather than a real drive of "parent is a file," exactly
        # the #1105 pattern this whole gate exists to catch, just inside the
        # gate itself. `tvm_home` ITSELF is now a regular file, so every one
        # of the three candidate paths (`tvm_home / rel`) has a FILE as its
        # own parent -- `NotADirectoryError`, genuinely distinct from
        # `test_absent`'s `FileNotFoundError`.
        (tmp_path / "tvm_home").write_text("not a directory")
        assert drpai._compiler_version(tmp_path / "tvm_home") == "drp-ai_tvm"

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(tmp_path / "VERSION")
        assert isinstance(drpai._compiler_version(tmp_path), str)

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        with _permission_denied(tmp_path):
            assert isinstance(drpai._compiler_version(tmp_path), str)

    def test_malformed_document(self, tmp_path):
        # No parse step -- a version file with no digits in it is simply
        # "no version found," the fallback tag, not a parse failure. Same
        # "nothing to drive" note as sdk_discovery._read_file above.
        pytest.skip("_compiler_version has no parse step of its own to malform")


# ---------------------------------------------------------------------------
# scaffold.read_example_tree -- curated-raise: ExampleReadError, never a raw
# stdlib exception, on every failure. `not_found` is asserted explicitly,
# not just the exception TYPE (tan-cli#1116 review round 2 BLOCKER): a first
# version of this fix's own `is_dir()` guard was dead on Python >= 3.14 and
# silently answered `not_found=True` (the user's-typo arm) for a
# permission-denied parent, where this class's own docstring says
# `not_found=False` -- a type-only assertion could not see that, only a
# value assertion can, which is what every test below now makes.
# ---------------------------------------------------------------------------


def _read_example_tree_error(source_dir):
    with pytest.raises(scaffold.ExampleReadError) as excinfo:
        scaffold.read_example_tree(source_dir)
    return excinfo.value


@_covers("scaffold.read_example_tree")
class TestReadExampleTree:
    def test_absent(self, tmp_path):
        # Genuinely missing: FileNotFoundError -> the "typo" arm.
        err = _read_example_tree_error(tmp_path / "example")
        assert err.not_found is True

    def test_non_utf8(self, tmp_path):
        source_dir = tmp_path / "example"
        source_dir.mkdir()
        _non_utf8(source_dir / "main.c")
        err = _read_example_tree_error(source_dir)
        assert err.not_found is False

    def test_directory_where_file_expected(self, tmp_path):
        # `source_dir` itself is a plain FILE, not a directory --
        # `NotADirectoryError` from `os.scandir`, the OTHER "not a real
        # example directory" cause, also the "typo" arm.
        source_dir = tmp_path / "example"
        source_dir.write_text("not a directory")
        err = _read_example_tree_error(source_dir)
        assert err.not_found is True

    def test_parent_is_a_file(self, tmp_path):
        # Also `NotADirectoryError` (a non-final path component) -- same
        # bucket as the file-itself shape above, indistinguishable by
        # errno, and both were indistinguishable through the OLD `is_dir()`
        # pre-flight too, so this is not a behaviour change, just a
        # version-independent path to the same answer.
        source_dir = tmp_path / "parent" / "example"
        _parent_is_a_file(source_dir)
        err = _read_example_tree_error(source_dir)
        assert err.not_found is True

    def test_symlink_loop(self, tmp_path):
        # Not via an INNER file -- `_example_source_files` skips symlinks
        # structurally (never follows one, matching the oracle's
        # `DirEntry::file_type()`), so an inner loop cannot reach the read
        # path at all. The top-level `source_dir` itself as the loop is the
        # shape that actually exercises this function: a real anomaly, not
        # a typo, so `not_found=False`. UNLIKE the permission-denied shape
        # below, this one was already wrong under the OLD `is_dir()`
        # pre-flight on EVERY interpreter, not just 3.14+: `is_dir()` on a
        # symlink loop returns `False` (never raises) on 3.12.3 and 3.13.15
        # alike, so the old code answered `not_found=True` there too --
        # ELOOP was never gated on the EACCES boundary at all.
        source_dir = tmp_path / "example"
        _symlink_loop(source_dir)
        err = _read_example_tree_error(source_dir)
        assert err.not_found is False

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # tan-cli#1116 review round 2 BLOCKER, re-derived: the first
        # version's `is_dir()` pre-flight was dead only on Python 3.14+ --
        # there `is_dir()` swallows EVERY `OSError` including `EACCES` and
        # returns `False`, where 3.12.3 AND 3.13.15 alike still raise
        # `PermissionError` for it (the `_IGNORED_ERRNOS` constant the
        # reasoning cites is gone from `pathlib` a release earlier, in
        # 3.13, without changing this raise until 3.14). So a
        # permission-denied PARENT fell through to `is_source_dir is False`
        # -> `not_found=True`, the wrong arm, on the exact interpreter
        # seam1's CI leg runs (CPython 3.14.7). The rewrite calls no stat at
        # all, so this is version-independent: `not_found` MUST be `False`
        # here, on every supported interpreter -- the assertion this test
        # makes, not merely "raises something," re-driven against 3.12.3,
        # 3.13.15 and 3.14.7 alike.
        outer = tmp_path / "outer"
        source_dir = outer / "example"
        source_dir.mkdir(parents=True)
        (source_dir / "main.c").write_text("int main(void) { return 0; }")
        with _permission_denied(outer):
            err = _read_example_tree_error(source_dir)
        assert err.not_found is False

    def test_malformed_document(self, tmp_path):
        # No document shape of its own -- a text file is either valid UTF-8
        # (copied verbatim) or it is the non_utf8 shape above. Nothing
        # further to malform.
        pytest.skip("read_example_tree has no parse step of its own to malform")


# ---------------------------------------------------------------------------
# som_buildability.hw_rev_not_buildable -- quiet-return, None on every
# failure ("Never raises: a scaffold must not fail because a metadata file
# could not be read", its own docstring). Found in the tan-cli#1116 review
# round 2 re-triage, not in the original 12: the private `_safe_load_mapping`
# it calls caught `OSError` alone, missing `UnicodeDecodeError`.
# ---------------------------------------------------------------------------


def _hw_rev_preset_path(sdk_root: Path, sku: str = "E1M-AEN301") -> Path:
    return sdk_root / "metadata" / "e1m_modules" / f"{sku}.yaml"


@_covers("som_buildability.hw_rev_not_buildable")
class TestHwRevNotBuildable:
    _SKU = "E1M-AEN301"

    def test_absent(self, tmp_path):
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    def test_non_utf8(self, tmp_path):
        path = _hw_rev_preset_path(tmp_path, self._SKU)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    def test_directory_where_file_expected(self, tmp_path):
        _as_directory(_hw_rev_preset_path(tmp_path, self._SKU))
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(_hw_rev_preset_path(tmp_path, self._SKU))
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    def test_symlink_loop(self, tmp_path):
        _symlink_loop(_hw_rev_preset_path(tmp_path, self._SKU))
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        path = _hw_rev_preset_path(tmp_path, self._SKU)
        path.parent.mkdir(parents=True)
        path.write_text("default_hw_rev: r2\n")
        with _permission_denied(path.parent):
            assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None

    def test_malformed_document(self, tmp_path):
        path = _hw_rev_preset_path(tmp_path, self._SKU)
        path.parent.mkdir(parents=True)
        _malformed(path, "a: [1, 2")
        assert som_buildability.hw_rev_not_buildable(tmp_path, self._SKU) is None


# ---------------------------------------------------------------------------
# new_som_cmd._known_board_names / _family_hw_revisions -- both quiet-return
# (None on every failure), both found in the same round-2 re-triage, both
# missing `OSError` entirely in their per-file except clause -- a per-file
# `chmod 000` (the CONTAINING directory stays listable) escaped raw past
# both, distinct from the directory-level `is_dir()`/`is_file()` pre-flight
# trap the round-2 fixes elsewhere in this file describe (and which turned
# out to ALSO be present here, on the directory side, fixed in the same
# change).
# ---------------------------------------------------------------------------


class TestIsYamlBoardFile:
    """tan-cli#1127 review round 2: `_is_yaml_board_file` must match what
    `boards_dir.glob("*.yaml")` used to match before the `os.listdir` swap
    -- case-sensitively on POSIX, case-INSENSITIVELY on Windows (`Path.
    glob`'s own `case_sensitive=None` default) -- not a narrower,
    always-case-sensitive suffix compare that happens to agree with `glob`
    only on POSIX. Not a `_SEEDED_CONTRACTS` entry (it is a platform-casing
    predicate, not a quiet-return/curated-raise contract), so no `@_covers`.
    Every expectation below is a literal, independently-stated boolean --
    never a second call into `Path.glob` or any other re-derivation of the
    function under test.
    """

    def test_posix_is_case_sensitive(self, monkeypatch):
        monkeypatch.setattr(new_som_cmd.os, "name", "posix")
        assert new_som_cmd._is_yaml_board_file("lower.yaml") is True
        assert new_som_cmd._is_yaml_board_file("upper.YAML") is False
        assert new_som_cmd._is_yaml_board_file("Mixed.YaML") is False
        assert new_som_cmd._is_yaml_board_file("not-yaml.txt") is False
        assert new_som_cmd._is_yaml_board_file("no-suffix") is False

    def test_windows_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(new_som_cmd.os, "name", "nt")
        assert new_som_cmd._is_yaml_board_file("lower.yaml") is True
        assert new_som_cmd._is_yaml_board_file("upper.YAML") is True
        assert new_som_cmd._is_yaml_board_file("Mixed.YaML") is True
        assert new_som_cmd._is_yaml_board_file("not-yaml.txt") is False
        assert new_som_cmd._is_yaml_board_file("no-suffix") is False


@_covers("new_som_cmd._known_board_names")
class TestKnownBoardNames:
    def _boards_dir(self, sdk_root: Path) -> Path:
        return sdk_root / "metadata" / "boards"

    def test_absent(self, tmp_path):
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_non_utf8(self, tmp_path):
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        _non_utf8(boards / "x.yaml")
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_directory_where_file_expected(self, tmp_path):
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        _as_directory(boards / "x.yaml")
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_parent_is_a_file(self, tmp_path):
        # `metadata` itself is a FILE, so `boards_dir`'s own parent cannot
        # be traversed -- the directory-level shape, not the per-file one.
        _parent_is_a_file(tmp_path / "metadata" / "boards")
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_symlink_loop(self, tmp_path):
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        _symlink_loop(boards / "x.yaml")
        assert new_som_cmd._known_board_names(tmp_path) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # Per-FILE chmod, containing directory stays listable -- the exact
        # shape that escaped raw past the per-file `except` before this fix
        # (measured).
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        path = boards / "x.yaml"
        path.write_text("name: FOO\n")
        with _permission_denied_file(path):
            assert new_som_cmd._known_board_names(tmp_path) is None

    @_skip_as_root
    def test_permission_denied_ancestor(self, tmp_path):
        # tan-cli#1127: the directory-LISTING's own `except OSError`, not
        # the per-file one above -- deny `boards_dir`'s PARENT (`metadata/`)
        # so the listing call itself fails. This is the exact shape
        # `Path.glob` answered SILENTLY with an empty iterator on 3.13.15
        # and 3.14.7 instead of raising (measured; tan-cli#1127), which
        # left the top-level `except OSError` dead code on those two
        # interpreters -- `os.listdir` raises here on all three measured
        # interpreters instead, so this must go red on all three if the
        # `except OSError` around it is ever removed.
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        (boards / "x.yaml").write_text("name: FOO\n")
        with _permission_denied(boards.parent):
            assert new_som_cmd._known_board_names(tmp_path) is None

    def test_empty_directory(self, tmp_path):
        # `return names or None`'s remaining job (tan-cli#1127): a listable
        # directory with nothing usable in it is "not resolvable", same as
        # a missing one -- not an empty-but-real `set()` the caller would
        # otherwise hard-fail every `--default-board` against.
        self._boards_dir(tmp_path).mkdir(parents=True)
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_malformed_document(self, tmp_path):
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        _malformed(boards / "x.yaml", "a: [1, 2")
        assert new_som_cmd._known_board_names(tmp_path) is None

    def test_uppercase_yaml_suffix_matches_only_on_windows(self, tmp_path, monkeypatch):
        # tan-cli#1127 review round 2: pins the CASING DECISION end-to-end,
        # not just `_is_yaml_board_file` in isolation. `Path.glob`'s own
        # `case_sensitive=None` default matched a `Foo.YAML` board file on
        # Windows before the `os.listdir` swap; this proves the swap did
        # not silently narrow that to a case-sensitive-everywhere match.
        boards = self._boards_dir(tmp_path)
        boards.mkdir(parents=True)
        (boards / "Upper.YAML").write_text("name: UPPER\n")
        monkeypatch.setattr(new_som_cmd.os, "name", "posix")
        assert new_som_cmd._known_board_names(tmp_path) is None
        monkeypatch.setattr(new_som_cmd.os, "name", "nt")
        assert new_som_cmd._known_board_names(tmp_path) == {"UPPER"}


@_covers("new_som_cmd._family_hw_revisions")
class TestFamilyHwRevisions:
    _SKU = "E1M-AEN301"

    def _patch_family(self, monkeypatch):
        monkeypatch.setattr(
            new_som_cmd, "_resolve_sku_family", lambda sku, sdk_root: "aen")

    def _hw_rev_path(self, sdk_root: Path) -> Path:
        return sdk_root / "metadata" / "e1m_modules" / "aen" / "hw-revisions.yaml"

    def test_absent(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        result = new_som_cmd._family_hw_revisions(
            self._SKU, tmp_path / "out", tmp_path / "sdk")
        assert result is None

    def test_non_utf8(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        path = self._hw_rev_path(sdk_root)
        path.parent.mkdir(parents=True)
        _non_utf8(path)
        result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

    def test_directory_where_file_expected(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        _as_directory(self._hw_rev_path(sdk_root))
        result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

    def test_parent_is_a_file(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        _parent_is_a_file(self._hw_rev_path(sdk_root))
        result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

    def test_symlink_loop(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        _symlink_loop(self._hw_rev_path(sdk_root))
        result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path, monkeypatch):
        # tan-cli#1116 review round 2: the CONTAINING directory denied --
        # the `path.is_file()` pre-flight this fix removed raised raw
        # `PermissionError` for exactly this shape (measured).
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        path = self._hw_rev_path(sdk_root)
        path.parent.mkdir(parents=True)
        path.write_text("hw_revisions: {r1: {}}\n")
        with _permission_denied(path.parent):
            result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

    def test_malformed_document(self, tmp_path, monkeypatch):
        self._patch_family(monkeypatch)
        sdk_root = tmp_path / "sdk"
        path = self._hw_rev_path(sdk_root)
        path.parent.mkdir(parents=True)
        _malformed(path, "a: [1, 2")
        result = new_som_cmd._family_hw_revisions(self._SKU, tmp_path / "out", sdk_root)
        assert result is None

# ---------------------------------------------------------------------------
# Anti-drift: every seed has a test, every test group is a declared seed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# tan-cli#1132 -- the ESCAPE half of the #1116 class: a `try` that cannot
# catch, because `Path.glob` is lazy and the iteration happened outside it.
# Both functions below now list through `os.scandir`/`os.listdir`, the only
# primitive measured to raise identically on 3.12.3, 3.13.15 and 3.14.7 for
# BOTH denied shapes (the directory itself chmod 000, and an ANCESTOR of it
# chmod 000). `_permission_denied` below covers the second; the first needs
# the directory under test to be the denied one, which these build inline.
# ---------------------------------------------------------------------------


class TestMatchesGlobSuffix:
    """`shapes.matches_glob_suffix` is the ONE spelling of the casing rule
    every `Path.glob("*.<ext>")` -> `os.listdir`/`os.scandir` swap in this
    tree needs (tan-cli#1127 review round 2, generalised by tan-cli#1132 when
    two more swaps wanted the identical predicate). Not a `_SEEDED_CONTRACTS`
    entry -- it is a platform-casing predicate, not a quiet-return contract --
    so no `@_covers`, exactly like `TestIsYamlBoardFile` above.

    Every expectation is a literal, independently-stated boolean; nothing
    here re-derives an answer by calling `Path.glob` a second time."""

    def test_posix_is_case_sensitive(self, monkeypatch):
        monkeypatch.setattr(shapes, "os", _OsWithName("posix"))
        assert shapes.matches_glob_suffix("prj.conf", ".conf", ".overlay") is True
        assert shapes.matches_glob_suffix("app.overlay", ".conf", ".overlay") is True
        assert shapes.matches_glob_suffix("prj.CONF", ".conf", ".overlay") is False
        assert shapes.matches_glob_suffix("App.OverLay", ".conf", ".overlay") is False
        assert shapes.matches_glob_suffix("readme.txt", ".conf", ".overlay") is False
        assert shapes.matches_glob_suffix("no-suffix", ".conf", ".overlay") is False

    def test_windows_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(shapes, "os", _OsWithName("nt"))
        assert shapes.matches_glob_suffix("prj.conf", ".conf", ".overlay") is True
        assert shapes.matches_glob_suffix("prj.CONF", ".conf", ".overlay") is True
        assert shapes.matches_glob_suffix("App.OverLay", ".conf", ".overlay") is True
        assert shapes.matches_glob_suffix("readme.txt", ".conf", ".overlay") is False
        assert shapes.matches_glob_suffix("no-suffix", ".conf", ".overlay") is False

    def test_the_suffix_argument_may_be_spelled_either_case(self, monkeypatch):
        # The caller's own spelling must not decide the answer -- only the
        # platform does. Both call sites pass lowercase today; a future one
        # passing ".JSON" must not silently start matching nothing on POSIX
        # and everything on Windows.
        monkeypatch.setattr(shapes, "os", _OsWithName("nt"))
        assert shapes.matches_glob_suffix("table.json", ".JSON") is True
        monkeypatch.setattr(shapes, "os", _OsWithName("posix"))
        assert shapes.matches_glob_suffix("table.JSON", ".JSON") is True
        assert shapes.matches_glob_suffix("table.json", ".JSON") is False

    def test_no_suffixes_matches_nothing(self, monkeypatch):
        # `str.endswith(())` is False, and the Windows arm builds its tuple
        # separately -- pinned so the two arms cannot drift on the edge case.
        for platform in ("posix", "nt"):
            monkeypatch.setattr(shapes, "os", _OsWithName(platform))
            assert shapes.matches_glob_suffix("prj.conf") is False


@_covers("configure_inputs.discover_configure_inputs")
class TestDiscoverConfigureInputs:
    """Contract: `frozenset()` for a missing or unreadable `app_dir`, and for
    an unreadable `boards/`/`socs/` subtree -- never a raise."""

    def _app(self, tmp_path: Path) -> Path:
        app = tmp_path / "outer" / "app"
        app.mkdir(parents=True)
        (app / "prj.conf").write_text("", encoding="utf-8")
        return app

    def test_absent(self, tmp_path):
        assert configure_inputs.discover_configure_inputs(tmp_path / "nope") == frozenset()

    def test_app_dir_is_a_file(self, tmp_path):
        path = tmp_path / "app"
        path.write_text("not a directory", encoding="utf-8")
        assert configure_inputs.discover_configure_inputs(path) == frozenset()

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(tmp_path / "outer" / "app")
        assert configure_inputs.discover_configure_inputs(tmp_path / "outer" / "app") == frozenset()

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # `app_dir` ITSELF chmod 000. `Path.glob` never raised for this shape
        # on ANY of the three interpreters (measured), so the old `except
        # OSError` was dead here even on 3.12.3; `os.scandir` raises on all
        # three, which is what this pins.
        app = self._app(tmp_path)
        with _permission_denied(app):
            assert configure_inputs.discover_configure_inputs(app) == frozenset()

    @_skip_as_root
    def test_permission_denied_ancestor(self, tmp_path):
        # THE mutation-proof shape (tan-cli#1132): deny `app_dir`'s PARENT.
        # Before the fix this raised PermissionError on 3.12.3 and 3.13.15
        # -- straight out of the `is_dir()` pre-flight, with the lazy-glob
        # `except OSError` never reached -- and returned frozenset() on
        # 3.14.7. Deleting the `except OSError` in
        # `discover_configure_inputs` must red this on all three now.
        app = self._app(tmp_path)
        with _permission_denied(app.parent):
            assert configure_inputs.discover_configure_inputs(app) == frozenset()

    @_skip_as_root
    def test_permission_denied_qualifier_subtree(self, tmp_path):
        # A denied `boards/` is `unreadable`, not `absent`: it must NOT slip
        # through the inner (FileNotFoundError, NotADirectoryError) handler
        # and leave a half-answer behind. The whole set folds to frozenset().
        app = self._app(tmp_path)
        boards = app / "boards"
        boards.mkdir()
        (boards / "native_sim.conf").write_text("", encoding="utf-8")
        with _permission_denied(boards):
            assert configure_inputs.discover_configure_inputs(app) == frozenset()

    def test_absent_qualifier_subtrees_are_not_a_failure(self, tmp_path):
        # The inner handler's own mutation proof: `boards/` and `socs/` are
        # OPTIONAL and usually absent. Removing `except (FileNotFoundError,
        # NotADirectoryError)` sends their FileNotFoundError to the outer
        # `except OSError`, which reds this by answering frozenset() instead.
        app = self._app(tmp_path)
        assert configure_inputs.discover_configure_inputs(app) == frozenset({"prj.conf"})

    def test_a_qualifier_subtree_that_is_a_file_is_not_a_failure_either(self, tmp_path):
        # `Path.glob("boards/**/*.conf")` matched nothing for a `boards`
        # that is a regular file, without raising; `os.scandir` raises
        # NotADirectoryError for it, which is why that name is in the inner
        # handler alongside FileNotFoundError.
        app = self._app(tmp_path)
        (app / "boards").write_text("not a directory", encoding="utf-8")
        assert configure_inputs.discover_configure_inputs(app) == frozenset({"prj.conf"})

    def test_symlink_loop_skips_only_that_entry(self, tmp_path):
        # Behaviour preserved across the primitive swap: `Path.is_file()`
        # answers False for an ELOOP symlink, but `os.DirEntry.is_file()`
        # RAISES OSError for it on all three interpreters (measured). The
        # walk uses `shapes.is_file`, so one bad entry costs one entry, not
        # the whole set -- exactly what the `glob` version did.
        app = self._app(tmp_path)
        _symlink_loop(app / "loop.conf")
        assert configure_inputs.discover_configure_inputs(app) == frozenset({"prj.conf"})

    def test_nested_qualifier_fragments_still_resolve(self, tmp_path):
        # `boards/**/*.conf` matched zero-or-more directories, so both a file
        # directly in `boards/` and one nested below it counted. Pinned here
        # because the `os.scandir` walk reproduces that by hand.
        app = self._app(tmp_path)
        (app / "boards" / "deep").mkdir(parents=True)
        (app / "boards" / "flat.conf").write_text("", encoding="utf-8")
        (app / "boards" / "deep" / "nested.overlay").write_text("", encoding="utf-8")
        (app / "socs").mkdir()
        (app / "socs" / "posix.conf").write_text("", encoding="utf-8")
        assert configure_inputs.discover_configure_inputs(app) == frozenset({
            "prj.conf", "boards/flat.conf", "boards/deep/nested.overlay",
            "socs/posix.conf",
        })

    def test_a_directory_named_like_a_fragment_is_not_a_fragment(self, tmp_path):
        # The old code's `path.is_file()` filter, preserved.
        app = self._app(tmp_path)
        (app / "notafile.conf").mkdir()
        assert configure_inputs.discover_configure_inputs(app) == frozenset({"prj.conf"})

    def test_uppercase_suffix_matches_only_on_windows(self, tmp_path, monkeypatch):
        # tan-cli#1127 review round 2's casing question, asked end-to-end of
        # this function: `Path.glob("*.conf")` enumerated a `Prj.CONF` on
        # Windows before the swap, and must still.
        app = self._app(tmp_path)
        (app / "Extra.CONF").write_text("", encoding="utf-8")
        monkeypatch.setattr(shapes, "os", _OsWithName("posix"))
        assert configure_inputs.discover_configure_inputs(app) == frozenset({"prj.conf"})
        monkeypatch.setattr(shapes, "os", _OsWithName("nt"))
        assert configure_inputs.discover_configure_inputs(app) == frozenset(
            {"prj.conf", "Extra.CONF"})


@_covers("analyze._resolve_table")
class TestResolveTable:
    """Contract: `None` when the backend's table directory is missing,
    unreadable or not a directory, and when no table in it covers the
    variant -- never a raise, because every one of those is the caller's
    `undetermined`."""

    _BACKEND = "ethos_u"
    _VARIANT = "u55"

    def _table_dir(self, metadata_root: Path) -> Path:
        return metadata_root / "npu_ops" / self._BACKEND

    def _seeded(self, tmp_path: Path) -> Path:
        metadata_root = tmp_path / "metadata"
        table_dir = self._table_dir(metadata_root)
        table_dir.mkdir(parents=True)
        (table_dir / "u55@vela-4.1.0.json").write_text(
            '{"applies_to": {"variant": "u55"}, "supported_ops": ["CONV_2D"]}',
            encoding="utf-8")
        return metadata_root

    def _resolve(self, metadata_root: Path):
        return analyze._resolve_table(metadata_root, self._BACKEND, self._VARIANT)

    def test_the_fixture_itself_resolves(self, tmp_path):
        # Without this, every "is None" below would pass for a fixture that
        # never had a resolvable table in the first place.
        metadata_root = self._seeded(tmp_path)
        resolved = self._resolve(metadata_root)
        assert resolved is not None
        assert resolved[0].name == "u55@vela-4.1.0.json"

    def test_absent(self, tmp_path):
        assert self._resolve(tmp_path / "metadata") is None

    def test_table_dir_is_a_file(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        table_dir = self._table_dir(metadata_root)
        table_dir.parent.mkdir(parents=True)
        table_dir.write_text("not a directory", encoding="utf-8")
        assert self._resolve(metadata_root) is None

    def test_parent_is_a_file(self, tmp_path):
        _parent_is_a_file(self._table_dir(tmp_path / "metadata"))
        assert self._resolve(tmp_path / "metadata") is None

    def test_empty_directory(self, tmp_path):
        self._table_dir(tmp_path / "metadata").mkdir(parents=True)
        assert self._resolve(tmp_path / "metadata") is None

    def test_non_utf8(self, tmp_path):
        metadata_root = self._seeded(tmp_path)
        _non_utf8(self._table_dir(metadata_root) / "u55@vela-4.1.0.json")
        assert self._resolve(metadata_root) is None

    def test_malformed_document(self, tmp_path):
        metadata_root = self._seeded(tmp_path)
        _malformed(self._table_dir(metadata_root) / "u55@vela-4.1.0.json", '{"a": [1, 2')
        assert self._resolve(metadata_root) is None

    def test_directory_where_table_expected(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _as_directory(self._table_dir(metadata_root) / "u55@vela-4.1.0.json")
        assert self._resolve(metadata_root) is None

    def test_symlink_loop(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _symlink_loop(self._table_dir(metadata_root) / "u55@vela-4.1.0.json")
        assert self._resolve(metadata_root) is None

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # The table directory ITSELF chmod 000.
        metadata_root = self._seeded(tmp_path)
        with _permission_denied(self._table_dir(metadata_root)):
            assert self._resolve(metadata_root) is None

    @_skip_as_root
    def test_permission_denied_ancestor(self, tmp_path):
        # THE mutation-proof shape (tan-cli#1132): deny the table directory's
        # ANCESTOR. Before the fix this raised PermissionError on 3.12.3 and
        # 3.13.15 out of the `is_dir()` pre-flight -- and would have raised
        # out of the unguarded `sorted(table_dir.glob(...))` behind it on
        # 3.12.3 anyway -- while returning None on 3.14.7. Deleting the
        # `except OSError` in `_resolve_table` must red this on all three.
        metadata_root = self._seeded(tmp_path)
        with _permission_denied(self._table_dir(metadata_root).parent):
            assert self._resolve(metadata_root) is None

    def test_uppercase_json_suffix_matches_only_on_windows(self, tmp_path, monkeypatch):
        # `table_dir.glob("*.json")` enumerated a `U55.JSON` on Windows
        # before the `os.listdir` swap; the swap must not have narrowed it.
        metadata_root = tmp_path / "metadata"
        table_dir = self._table_dir(metadata_root)
        table_dir.mkdir(parents=True)
        (table_dir / "U55@VELA-4.1.0.JSON").write_text(
            '{"applies_to": {"variant": "u55"}, "supported_ops": ["CONV_2D"]}',
            encoding="utf-8")
        monkeypatch.setattr(shapes, "os", _OsWithName("posix"))
        assert self._resolve(metadata_root) is None
        monkeypatch.setattr(shapes, "os", _OsWithName("nt"))
        resolved = self._resolve(metadata_root)
        assert resolved is not None
        assert resolved[0].name == "U55@VELA-4.1.0.JSON"


# ---------------------------------------------------------------------------
# tan-cli#1133: the two `tan/planner/template.py` reads -- curated-raise,
# `TemplateError` and nothing else, on every failure shape either can meet.
#
# THE REVERSAL, stated where it happened. An earlier version of this file's
# docstring said `template.py`'s fixed sites were "fixed but NOT seeded here
# (deliberately, not an oversight)", because seeding one needs
# `bind_sdk_root` -- global mutable state no other seed touches. tan-cli#1133
# is what that decision cost: two MORE sites in the same file, one call away
# from the read #1116 fixed and reached in the same invocation, went
# unseeded, then unfound by the sweep (no `try` to call too narrow), and
# shipped. The seed list is the only thing in this repo that would have held
# them, so they are in it now.
#
# The price is paid in the narrowest way available. There is NO autouse
# fixture here: importing `tests.planner._bound_sdk_fixture._bound_sdk` would
# register it as autouse for this WHOLE module, binding for the other
# seventeen seeds that neither need nor want it (and erroring outright when
# `SDK is None`, which for this un-gated module is the common case). Instead
# these two groups skip as a unit when no real checkout is bound, and each
# binds inside its own call -- the same "import inside the call so the module
# is not imported before `bind_sdk_root` has run" step every module under
# `tests/planner/` already takes.
# ---------------------------------------------------------------------------

_needs_sdk = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass. CI binds it on the sdk-parity legs.",
)


def _planner_template():
    """`tan.planner.template`, bound and imported INSIDE the call.

    `tan/planner/paths.py` evaluates `REPO = sdk_root()` at module scope, so
    an import at this file's top level would freeze (or refuse) the planner's
    root at collection time for the whole session. Every consumer under
    `tests/planner/` defers the import the same way.
    """
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    import tan.planner.template as m
    return m


class _PlannerDocumentCases:
    """The shapes both `template.py` reads meet, driven identically.

    Both functions take `(name, metadata_root)` and read exactly one file
    under it, so one set of cases covers both -- subclasses supply only
    `_path` (where the document lives) and `_call` (how it is reached).
    Written as a shared base rather than a parametrised pair so each seeded
    name keeps its own `@_covers` class, which is what
    `test_every_seed_has_a_test` counts.
    """

    #: The `_GOOD` document is not asserted on here -- these cases are about
    #: failure -- but each subclass writes it for the shapes that need a
    #: file to exist before being made unreadable.
    _GOOD = "{}\n"

    def _path(self, metadata_root: Path) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    def _call(self, metadata_root: Path):  # pragma: no cover - abstract
        raise NotImplementedError

    def _raises(self, metadata_root: Path) -> str:
        m = _planner_template()
        with pytest.raises(m.TemplateError) as excinfo:
            self._call(metadata_root)
        return str(excinfo.value)

    def _prepared(self, tmp_path: Path) -> Path:
        metadata_root = tmp_path / "metadata"
        path = self._path(metadata_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._GOOD, encoding="utf-8")
        return metadata_root

    def test_absent(self, tmp_path):
        # The one shape the deleted `is_file()` pre-flight DID answer. Its
        # message is preserved byte for byte -- `require_readable_text`'s
        # `absent=` argument exists for exactly that, and this asserts the
        # preservation rather than trusting it.
        msg = self._raises(tmp_path / "metadata")
        assert msg.startswith("no metadata/")

    def test_non_utf8(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _non_utf8(self._path(metadata_root))
        assert "cannot read" in self._raises(metadata_root)

    def test_malformed_document(self, tmp_path):
        # Two different failures wear this name for a YAML document, and
        # both used to escape raw. Syntactically invalid (`yaml.
        # ParserError`, neither an OSError nor a ValueError, so nothing in
        # this module's ladder had ever caught it) ...
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), "a: [1, 2\nb: }{\n")
        assert "not valid YAML" in self._raises(metadata_root)

    def test_malformed_shape(self, tmp_path):
        # ... and legal YAML that is not a mapping, the tan-cli#1025 half
        # this file's reads have guarded since long before #1133.
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), "- one\n- two\n")
        assert "expected a YAML mapping" in self._raises(metadata_root)

    def test_directory_where_file_expected(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _as_directory(self._path(metadata_root))
        assert "cannot read" in self._raises(metadata_root)

    def test_parent_is_a_file(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _parent_is_a_file(self._path(metadata_root))
        assert "cannot read" in self._raises(metadata_root)

    def test_symlink_loop(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _symlink_loop(self._path(metadata_root))
        assert "cannot read" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        with _permission_denied_file(self._path(metadata_root)):
            assert "cannot read" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_parent(self, tmp_path):
        # THE tan-cli#1127 CELL, and the reason the pre-flight had to go
        # rather than be widened: `Path.is_file()` raises `PermissionError`
        # here on 3.12.3 and 3.13.15, and returns `False` on 3.14.7 -- so
        # before this fix the SAME unreadable file produced a raw traceback
        # on two interpreters and, on the third, the curated but FALSE
        # `no metadata/...` message. Both halves measured. Asserting
        # `cannot read` (not merely "raises TemplateError") is what pins the
        # 3.14.7 half: a curated-but-untrue "no such file" would satisfy the
        # weaker assertion.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(self._path(metadata_root).parent):
            assert "cannot read" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_metadata_root(self, tmp_path):
        # ONE DIRECTORY FURTHER OUT than the cell above, and a distinct
        # shape rather than a weaker restatement of it (tan-cli#1171
        # review). Denying the document's own parent puts the guarded read
        # first in line; denying `metadata/` ALSO denies anything the
        # caller does with the root BEFORE that read -- and a caller can
        # have such a step without owning a pre-flight, because building
        # an `absent=` message is allowed to touch the filesystem too.
        # `libraries.load_manifest` did: its `Available: ...` list is a
        # `glob` of `metadata/libraries/`, and while it was built eagerly
        # the whole guarded read was unreachable behind an unguarded
        # `is_dir()`. That put a raw `PermissionError` on 3.12.3 and
        # 3.13.15 against a curated message on 3.14.7 -- the tan-cli#1127
        # split, alive inside the change that removes it, and invisible to
        # the cell above. All three measured, before and after.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(metadata_root):
            assert "cannot read" in self._raises(metadata_root)


@_needs_sdk
@_covers("template._load_som_doc")
class TestLoadSomDoc(_PlannerDocumentCases):
    _SKU = "E1M-FAKE1133"
    _GOOD = "default_board: TARGET-BOARD\n"

    def _path(self, metadata_root: Path) -> Path:
        return metadata_root / "e1m_modules" / f"{self._SKU}.yaml"

    def _call(self, metadata_root: Path):
        return _planner_template()._load_som_doc(self._SKU, metadata_root)


@_needs_sdk
@_covers("template._board_route_entries")
class TestBoardRouteEntries(_PlannerDocumentCases):
    _BOARD = "fake1133-board"
    _GOOD = "e1m_routes:\n  gpio: []\n"

    def _path(self, metadata_root: Path) -> Path:
        return metadata_root / "boards" / f"{self._BOARD}.yaml"

    def _call(self, metadata_root: Path):
        return _planner_template()._board_route_entries(
            self._BOARD, metadata_root)


@_needs_sdk
@_covers("template._rendered_bytes")
class TestRenderedBytes:
    """The fourth site (PR #1160 review, MAJOR 1). Not a subclass of
    `_PlannerDocumentCases`: this read takes no `(name, metadata_root)` pair,
    and -- the reason it is worth its own class rather than a parametrised
    case -- its contract on a non-UTF-8 file is the OPPOSITE. A template
    asset is not required to be text; `render()` copies whatever it read, so
    the bytes half must HAND BACK arbitrary bytes where the three document
    reads must refuse them."""

    _RECORD = {"id": "seed1133", "example": "examples/peripheral-io/seed1133",
               "supported": {"som_skus": ["E1M-SEED1133"]},
               "files": {"user_owned": ["src/main.c"]}, "cores": []}

    def _tree(self, tmp_path: Path) -> Path:
        source = tmp_path / self._RECORD["example"] / "src"
        source.mkdir(parents=True)
        (source / "main.c").write_text("int main(void){return 0;}\n",
                                       encoding="utf-8")
        return tmp_path

    def _path(self, base: Path) -> Path:
        return base / self._RECORD["example"] / "src" / "main.c"

    def _call(self, base: Path):
        m = _planner_template()
        return m._rendered_bytes(
            "seed1133", self._RECORD, ("src/main.c",), {}, base,
            doc="catalog", field="templates[0]")

    def _raises(self, base: Path) -> str:
        m = _planner_template()
        with pytest.raises(m.TemplateError) as excinfo:
            self._call(base)
        return str(excinfo.value)

    def test_absent(self, tmp_path):
        base = self._tree(tmp_path)
        self._path(base).unlink()
        assert "cannot read template source file at" in self._raises(base)

    def test_directory_where_file_expected(self, tmp_path):
        base = self._tree(tmp_path)
        self._path(base).unlink()
        self._path(base).mkdir()
        assert "cannot read template source file at" in self._raises(base)

    def test_parent_is_a_file(self, tmp_path):
        base = self._tree(tmp_path)
        source = self._path(base).parent
        (source / "main.c").unlink()
        source.rmdir()
        source.write_text("not a directory", encoding="utf-8")
        assert "cannot read template source file at" in self._raises(base)

    def test_symlink_loop(self, tmp_path):
        # The one shape whose curated MESSAGE is interpreter-dependent, and
        # deliberately so (`_safe_join`'s own docstring carries the
        # measurement): `Path.resolve()` raises `RuntimeError("Symlink loop
        # ...")` on 3.12.3 and returns the path unchanged on 3.13.15/3.14.7,
        # so the failure is caught at the resolve on one and at the read on
        # the other two. Asserting the CLASS plus the path is what holds on
        # all three; asserting one message would pass on two interpreters
        # and be a lie on the third.
        base = self._tree(tmp_path)
        self._path(base).unlink()
        _symlink_loop(self._path(base))
        message = self._raises(base)
        assert "template source file" in message
        assert "cannot read" in message or "cannot resolve" in message

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        base = self._tree(tmp_path)
        with _permission_denied_file(self._path(base)):
            assert "cannot read template source file at" in self._raises(base)

    @_skip_as_root
    def test_permission_denied_parent(self, tmp_path):
        base = self._tree(tmp_path)
        with _permission_denied(self._path(base).parent):
            assert "cannot read template source file at" in self._raises(base)

    def test_non_utf8(self, tmp_path):
        # NOT a failure here, and this asserting so is the point: the guard
        # must not have quietly narrowed what a template may ship. `render()`
        # writes these bytes back out verbatim; only `--emit scaffold`'s
        # JSON envelope needs text, and it has its own curated refusal one
        # frame up (`render_to_envelope`).
        base = self._tree(tmp_path)
        self._path(base).write_bytes(b"\xff\xfe\x00binary-asset")
        assert self._call(base) == [("src/main.c", b"\xff\xfe\x00binary-asset")]

    # No `test_malformed_document` here, and deliberately not an
    # unconditional `pytest.skip` standing in for one: `_rendered_bytes`
    # parses nothing, this class is not a `_PlannerDocumentCases` subclass,
    # and no shared shape list requires the name -- so a skip that can never
    # fail would be one more permanent skip in a suite where a skip is
    # already indistinguishable from a pass at a glance (PR #1160 review
    # round 2). The class docstring above says the contract differs.


# ---------------------------------------------------------------------------
# uri_reference.cwd_base_uri_or_none -- quiet-return (tan-cli#1134 part 1).
# ---------------------------------------------------------------------------


@_covers("uri_reference.cwd_base_uri_or_none")
class TestCwdBaseUriOrNone:
    """The one seeded function that is NOT reached through a path argument,
    and the reason it needed seeding at all.

    Every other member of this dict fails because of a document SOMEONE
    ELSE handed it. This one fails because of the process's own working
    directory -- `cwd_base_uri()` calls `Path.cwd()`, `Path.cwd()` calls
    `os.getcwd()`, and `os.getcwd()` raises when the CWD has been removed
    out from under the process. So the seven on-disk shape builders above
    have nothing to offer here; the fixture is `os.getcwd` itself.

    `os.getcwd` is patched rather than the real CWD removed, and that is
    tan-cli#1117 review round 4's own measurement, not a shortcut: POSIX
    lets a process `rmdir` its own working directory and Windows does not
    (`PermissionError [WinError 32]`, measured on `windows-latest` CI,
    where the removed-directory spelling of this test failed for real).
    Patching the call `Path.cwd()` actually makes reproduces the same
    contract identically on all three platforms this suite runs on.

    WHY IT IS SEEDED (tan-cli#1134). This function is not a fix and was
    never broken -- it is the GUARD PR #1125 added, and its whole reason
    for existing is the sentence "returns `None` instead of raising".
    `cwd_base_uri()`'s raise reached `_sarif_document` from INSIDE
    `validate_cmd.py`'s own `except Exception as err:  # never a bare
    traceback; the envelope is the contract` handler, and double-faulted
    there: the handler that exists to guarantee an envelope was itself the
    frame that could not produce one. A guard on that path is exactly what
    this gate is for, and it sat unseeded from the week the gate landed
    until #1134.

    THE ERRNOS ARE DRIVEN, not reasoned. `os.getcwd()` raises more than
    `ENOENT`: `EACCES` when a parent of the CWD loses execute permission,
    and `ENOTDIR`/`ENAMETOOLONG` in the shapes glibc can return. Each is
    driven below as its own case rather than trusting that "they are all
    `OSError` subclasses" -- which is true, and is precisely the kind of
    true-by-inspection claim tan-cli#1116 exists because nobody tested."""

    def _guarded(self, monkeypatch, exc: BaseException):
        def _raise() -> str:
            raise exc
        monkeypatch.setattr(os, "getcwd", _raise)
        from tan.core.uri_reference import cwd_base_uri_or_none
        return cwd_base_uri_or_none()

    @pytest.mark.parametrize("exc", [
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        NotADirectoryError(20, "Not a directory"),
        OSError(36, "File name too long"),
    ], ids=["ENOENT", "EACCES", "ENOTDIR", "ENAMETOOLONG"])
    def test_every_getcwd_failure_returns_none(self, monkeypatch, exc):
        assert self._guarded(monkeypatch, exc) is None

    def test_the_unguarded_call_really_does_raise(self, monkeypatch):
        # The other half of the pin: without this, a mutation that made
        # `cwd_base_uri()` itself stop raising would leave the cases above
        # green while asserting nothing.
        from tan.core.uri_reference import cwd_base_uri

        def _raise() -> str:
            raise FileNotFoundError(2, "No such file or directory")
        monkeypatch.setattr(os, "getcwd", _raise)
        with pytest.raises(OSError):
            cwd_base_uri()

    def test_the_happy_path_is_not_swallowed(self, tmp_path, monkeypatch):
        # A guard that returned None unconditionally would satisfy every
        # case above. This is what stops it.
        from tan.core.uri_reference import cwd_base_uri, cwd_base_uri_or_none
        monkeypatch.chdir(tmp_path)
        value = cwd_base_uri_or_none()
        assert value is not None
        assert value == cwd_base_uri()
        assert value.startswith("file:") and value.endswith("/")


# ---------------------------------------------------------------------------
# The tan-cli#1162 planner sites. Four functions, three documents and one
# subprocess artefact, all now read through `tan/core/document_guards.py`.
# ---------------------------------------------------------------------------


def _planner_module(name: str):
    """`tan.planner.<name>`, bound and imported INSIDE the call -- the same
    deferral `_planner_template` above documents, for the same
    module-scope `REPO = sdk_root()` reason."""
    import importlib
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    return importlib.import_module(f"tan.planner.{name}")


@_needs_sdk
@_covers("libraries.load_manifest")
class TestLoadManifest(_PlannerDocumentCases):
    """tan-cli#1162 site 1. A `_PlannerDocumentCases` subclass because the
    shapes and the curated vocabulary are identical to `template.py`'s two
    YAML document reads -- the only differences are the curated class
    (`OrchestratorError`, not `TemplateError`) and the `absent` message,
    which is this function's typo-correcting `unknown library ...
    Available: ...` rather than a `no metadata/...` line."""

    _GOOD = "tier: 1\nlicense: Apache-2.0\n"
    _NAME = "seed1162"

    def _path(self, metadata_root: Path) -> Path:
        return metadata_root / "libraries" / f"{self._NAME}.yaml"

    def _call(self, metadata_root: Path):
        return _planner_module("libraries").load_manifest(
            self._NAME, metadata_root)

    def _raises(self, metadata_root: Path) -> str:
        m = _planner_module("models")
        with pytest.raises(m.OrchestratorError) as excinfo:
            self._call(metadata_root)
        return str(excinfo.value)

    def test_absent(self, tmp_path):
        # This function's `absent=` message is not the base class's `no
        # metadata/...`: the typo-correcting option list is the whole point
        # of the message and is preserved byte for byte across the fix.
        msg = self._raises(tmp_path / "metadata")
        assert msg.startswith(f"unknown library `{self._NAME}`")
        assert "Available: <none>" in msg

    def test_a_directory_is_no_longer_reported_as_an_unknown_library(self, tmp_path):
        # THE MEASURED LIE, and the reason the pre-flight had to go rather
        # than be widened. Before the fix, on 3.12.3, 3.13.15 AND 3.14.7
        # alike, a DIRECTORY named `<lib>.yaml` made `is_file()` answer
        # False, so the user was told the library was unknown -- and the
        # option list, built from the same `glob("*.yaml")`, listed that
        # very directory as an available library in the same sentence.
        metadata_root = tmp_path / "metadata"
        _as_directory(self._path(metadata_root))
        msg = self._raises(metadata_root)
        assert "cannot read" in msg
        assert "unknown library" not in msg

    @_skip_as_root
    def test_the_option_list_is_only_built_on_a_real_miss(self, tmp_path):
        # The tan-cli#1171 review cell. What each assertion pins, stated
        # exactly, because review round 2 caught an earlier version of this
        # comment claiming more than the code can enforce:
        #
        #   * `"cannot read" in msg` is the whole load-bearing half. It
        #     needs `self._raises` to have caught an `OrchestratorError` at
        #     all, and with the listing built EAGERLY there is no
        #     `OrchestratorError` to catch on 3.12.3 and 3.13.15 -- the
        #     `is_dir()` in `available_libraries` raises a raw
        #     `PermissionError` before the register is ever reached
        #     (`pytest.raises` fails on the wrong type). Measured: that is
        #     what reds the eager mutant, and it reds only on those two.
        #   * `"Available:" not in msg` does NOT pin the ordering, and an
        #     earlier draft of this comment wrongly said it did.
        #     `Available:` lives only inside the `absent` message, and
        #     `_unreadable` resolves `absent` on `FileNotFoundError` and
        #     nothing else, so it cannot appear on a `PermissionError` path
        #     whether the listing is built eagerly or lazily. What it pins
        #     is that `absent=` STAYS `FileNotFoundError`-only -- widen
        #     that branch to any other `OSError` and this reds.
        #
        # The tail of the test then pins that the listing is not merely
        # gone: on a genuine miss it is built, with the sibling in it.
        metadata_root = self._prepared(tmp_path)
        (metadata_root / "libraries" / "sibling.yaml").write_text(
            "tier: 1\n", encoding="utf-8")
        with _permission_denied(metadata_root):
            msg = self._raises(metadata_root)
        assert "cannot read" in msg
        assert "Available:" not in msg
        # ... and it IS built, with the sibling in it, once the file really
        # is missing -- so neither assertion above can pass by the listing
        # having been dropped altogether.
        self._path(metadata_root).unlink()
        assert "Available: sibling" in self._raises(metadata_root)

    @_skip_as_root
    def test_available_libraries_answers_rather_than_raising(self, tmp_path):
        # Driven DIRECTLY, not through `load_manifest`: dropping the
        # `is_dir()` pre-flight is not on its own enough, because
        # `Path.glob` is not interpreter-uniform on this shape either.
        # Measured with `metadata/` denied: `glob("*.yaml")` raises
        # `PermissionError` on 3.12.3 and returns `[]` on 3.13.15 and
        # 3.14.7 -- so the `except OSError` is what makes the three agree,
        # and only 3.12.3 actually executes it. Answering `[]` is the right
        # contract here and only here: this is a HINT list inside another
        # function's message, not a document read, and its one caller is
        # reached only once a guarded read has already raised.
        metadata_root = self._prepared(tmp_path)
        module = _planner_module("libraries")
        assert module.available_libraries(metadata_root) == [self._NAME]
        with _permission_denied(metadata_root):
            assert module.available_libraries(metadata_root) == []


@_needs_sdk
@_covers("zephyr_board._load_soc_spec")
class TestLoadSocSpec:
    """tan-cli#1162 site 2. NOT a `_PlannerDocumentCases` subclass: the
    document is JSON, so "malformed" and "not a mapping" are two different
    fixtures (`{` and `[]`) where YAML's are `a: [1, 2` and `- one`, and the
    curated noun is `a JSON object`, not `a YAML mapping`."""

    _PRESET = {"sku": "E1M-SEED1162", "silicon": "alif:ensemble:e8"}
    _GOOD = '{"variants": [], "cores": []}'

    def _path(self, metadata_root: Path) -> Path:
        from tan.soc_ref import resolve_soc_path
        path = resolve_soc_path(self._PRESET["silicon"], metadata_root)
        assert path is not None, "test assumption: a triple-colon silicon ref resolves"
        return path

    def _call(self, metadata_root: Path):
        m = _planner_module("zephyr_board")
        return m._load_soc_spec(self._PRESET, metadata_root)

    def _raises(self, metadata_root: Path) -> str:
        m = _planner_module("zephyr_board")
        with pytest.raises(m.ZephyrBoardEmitError) as excinfo:
            self._call(metadata_root)
        return str(excinfo.value)

    def _prepared(self, tmp_path: Path) -> Path:
        metadata_root = tmp_path / "metadata"
        path = self._path(metadata_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._GOOD, encoding="utf-8")
        return metadata_root

    def test_the_good_document_still_loads(self, tmp_path):
        assert self._call(self._prepared(tmp_path)) == {"variants": [], "cores": []}

    def test_absent(self, tmp_path):
        assert self._raises(tmp_path / "metadata").startswith("no SoC spec at ")

    def test_non_utf8(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _non_utf8(self._path(metadata_root))
        assert "cannot read SoC spec at" in self._raises(metadata_root)

    def test_malformed_document(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), '{"variants": [')
        assert "not valid JSON" in self._raises(metadata_root)

    def test_malformed_shape(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), "[]")
        assert "expected a JSON object, got list" in self._raises(metadata_root)

    def test_directory_where_file_expected(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _as_directory(self._path(metadata_root))
        assert "cannot read SoC spec at" in self._raises(metadata_root)

    def test_parent_is_a_file(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _parent_is_a_file(self._path(metadata_root))
        assert "cannot read SoC spec at" in self._raises(metadata_root)

    def test_symlink_loop(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _symlink_loop(self._path(metadata_root))
        assert "cannot read SoC spec at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        with _permission_denied_file(self._path(metadata_root)):
            assert "cannot read SoC spec at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_parent(self, tmp_path):
        # The tan-cli#1127 cell for this site. Before the fix: raw
        # `PermissionError` on 3.12.3 and 3.13.15, and on 3.14.7 the curated
        # but FALSE `no SoC spec at <path>` for a spec that is there.
        # Asserting `cannot read` is what pins the 3.14.7 half.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(self._path(metadata_root).parent):
            assert "cannot read SoC spec at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_metadata_root(self, tmp_path):
        # The grandparent cell `_PlannerDocumentCases` documents, driven
        # here too so the isolation claim is a measurement rather than an
        # assumption: this site was already curated on all three
        # interpreters with `metadata/` denied, and stays so.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(metadata_root):
            assert "cannot read SoC spec at" in self._raises(metadata_root)


@_needs_sdk
@_covers("kconfig_symbols._load_board_symbols")
class TestLoadBoardSymbols:
    """tan-cli#1162 site 3, and the only seeded site whose broken input
    comes from a SUBPROCESS rather than from the SDK checkout.

    `_load_board_symbols` runs `west build --cmake-only` and then `west
    build -t alpkconfigjson` in a scratch tree of its own making, and reads
    the `alp_kconfig.json` the second one is supposed to leave behind. So
    the fixture cannot be a file placed beforehand -- the directory does not
    exist yet when the test starts. `subprocess.run` is substituted with a
    stub that reports success and leaves whichever broken shape the case
    wants at exactly the path the real dumper would have written, which is
    the real body of the function running against a real broken file, not a
    unit test of the guard expression.

    `west_program` is stubbed too, and only because resolving it needs a
    real west workspace on disk; nothing about which program name comes
    back is under test here."""

    def _run(self, tmp_path, shape, *, good=b'[{"name": "CONFIG_X"}]'):
        m = _planner_module("kconfig_symbols")
        state: dict[str, Path] = {}

        def _fake_run(cmd, **kwargs):
            # `-d <build_dir>` is how both invocations name the tree; the
            # SECOND (the `-t` one) is where the artefact appears.
            build_dir = Path(cmd[cmd.index("-d") + 1])
            state["output"] = build_dir / "alp_kconfig.json"
            if "-t" in cmd:
                build_dir.mkdir(parents=True, exist_ok=True)
                shape(state["output"], good)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(m.subprocess, "run", _fake_run)
            monkeypatch.setattr(m, "west_program", lambda *a, **k: "west")
            with pytest.raises(m.OrchestratorError) as excinfo:
                m._load_board_symbols(tmp_path / "zephyr", "alp_seed1162")
            return str(excinfo.value)
        finally:
            monkeypatch.undo()

    def _ok(self, tmp_path, shape, *, good=b'[{"name": "CONFIG_X"}]'):
        m = _planner_module("kconfig_symbols")

        def _fake_run(cmd, **kwargs):
            build_dir = Path(cmd[cmd.index("-d") + 1])
            if "-t" in cmd:
                build_dir.mkdir(parents=True, exist_ok=True)
                shape(build_dir / "alp_kconfig.json", good)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(m.subprocess, "run", _fake_run)
            monkeypatch.setattr(m, "west_program", lambda *a, **k: "west")
            return m._load_board_symbols(tmp_path / "zephyr", "alp_seed1162")
        finally:
            monkeypatch.undo()

    @staticmethod
    def _write(path, data):
        path.write_bytes(data)

    def test_the_good_artefact_still_loads(self, tmp_path):
        assert self._ok(tmp_path, self._write) == [{"name": "CONFIG_X"}]

    def test_absent(self, tmp_path):
        # `never wrote` is preserved byte for byte through `absent=`: it is
        # a better message than `cannot read` for the one shape it is true
        # of, and it is what tells the reader the DUMPER misbehaved.
        msg = self._run(tmp_path, lambda p, d: None)
        assert "completed but never wrote" in msg
        assert "never emit a partial/empty menu" in msg

    def test_non_utf8(self, tmp_path):
        msg = self._run(tmp_path, lambda p, d: p.write_bytes(b"\xff\xfe\x00x"))
        assert "cannot read the `alpkconfigjson` dumper's output at" in msg

    def test_directory_where_file_expected(self, tmp_path):
        msg = self._run(tmp_path, lambda p, d: p.mkdir())
        assert "cannot read the `alpkconfigjson` dumper's output at" in msg

    def test_empty_artefact_is_still_refused_by_its_own_guard(self, tmp_path):
        # Pre-existing and untouched by tan-cli#1162, asserted here so the
        # read guard cannot be widened later in a way that swallows it.
        msg = self._run(tmp_path, self._write, good=b"   \n")
        assert "reported success but wrote an empty" in msg

    def test_malformed_json_is_still_refused_by_its_own_guard(self, tmp_path):
        msg = self._run(tmp_path, self._write, good=b"[{")
        assert "is not valid JSON" in msg

    def test_a_non_list_artefact_is_still_refused_by_its_own_guard(self, tmp_path):
        msg = self._run(tmp_path, self._write, good=b'{"a": 1}')
        # The product message wraps across a source line, so compare on
        # whitespace-collapsed text rather than reproducing the wrap here.
        assert "is not a list of symbols (got dict)" in " ".join(msg.split())

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        def _shape(path, data):
            path.write_bytes(data)
            path.chmod(0o000)
        msg = self._run(tmp_path, _shape)
        assert "cannot read the `alpkconfigjson` dumper's output at" in msg

    @_skip_as_root
    def test_permission_denied_parent(self, tmp_path):
        # The tan-cli#1127 cell: raw `PermissionError` on 3.12.3/3.13.15
        # before the fix, and the curated but FALSE `never wrote` on 3.14.7
        # for an artefact the dumper DID write.
        modes: list = []

        def _shape(path, data):
            path.write_bytes(data)
            modes.append((path.parent, path.parent.stat().st_mode))
            path.parent.chmod(0o000)
        try:
            msg = self._run(tmp_path, _shape)
        finally:
            for d, mode in modes:
                d.chmod(mode)
        assert "cannot read the `alpkconfigjson` dumper's output at" in msg
        assert "never wrote" not in msg


@_needs_sdk
@_covers("topology._core_os_choices")
class TestCoreOsChoices:
    """tan-cli#1162 site 4. The odd one out twice over: its `absent` shape
    is NOT a failure (an absent project schema is the documented fallback
    onto the in-tree `BOARD_SCHEMA`), and its pre-flight was a SELECTOR
    between two documents rather than a guard on one -- so the shape that
    matters most here is the one where the project's OWN schema is present
    and unreadable, which used to fall back SILENTLY and answer with the
    wrong document's OS set."""

    _GOOD = ('{"$defs": {"core_entry": {"properties": {"os": '
             '{"enum": ["zephyr", "yocto", "baremetal", "off"]}}}}}')

    def _path(self, metadata_root: Path) -> Path:
        return metadata_root / "schemas" / "board.schema.json"

    def _module(self):
        m = _planner_module("topology")
        m._core_os_choices.cache_clear()
        return m

    def _call(self, metadata_root: Path):
        return self._module()._core_os_choices(metadata_root)

    def _raises(self, metadata_root: Path) -> str:
        m = self._module()
        models = _planner_module("models")
        with pytest.raises(models.OrchestratorError) as excinfo:
            m._core_os_choices(metadata_root)
        return str(excinfo.value)

    def _prepared(self, tmp_path: Path) -> Path:
        metadata_root = tmp_path / "metadata"
        path = self._path(metadata_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._GOOD, encoding="utf-8")
        return metadata_root

    def test_the_projects_own_schema_wins_when_it_is_readable(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root),
                   '{"$defs": {"core_entry": {"properties": {"os": '
                   '{"enum": ["only-this"]}}}}}')
        assert self._call(metadata_root) == ("only-this",)

    def test_absent_falls_back_to_the_in_tree_schema(self, tmp_path):
        # NOT a failure: the fallback is the function's own contract. It is
        # asserted positively (a non-empty tuple carrying the real enum)
        # rather than as "does not raise", so a guard that started refusing
        # a synthetic metadata root would red here.
        choices = self._call(tmp_path / "metadata")
        assert "zephyr" in choices and "off" in choices

    def test_a_missing_fallback_schema_is_a_curated_refusal(self, tmp_path):
        # The other arm, driven rather than assumed: with BOTH the project
        # schema and the in-tree one gone there is nothing to read, and the
        # `board schema not found` message is the one that must appear.
        m = self._module()
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(m, "BOARD_SCHEMA", tmp_path / "nowhere.json")
            models = _planner_module("models")
            with pytest.raises(models.OrchestratorError) as excinfo:
                m._core_os_choices(tmp_path / "metadata")
            assert str(excinfo.value).startswith("board schema not found: ")
        finally:
            monkeypatch.undo()
            m._core_os_choices.cache_clear()

    def test_non_utf8(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _non_utf8(self._path(metadata_root))
        assert "cannot read board schema at" in self._raises(metadata_root)

    def test_malformed_document(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), '{"$defs": ')
        assert "not valid JSON" in self._raises(metadata_root)

    def test_malformed_shape(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), "[]")
        assert "expected a JSON object, got list" in self._raises(metadata_root)

    @pytest.mark.parametrize("doc,missing", [
        ("{}", "'$defs'"),
        ('{"$defs": {}}', "'core_entry'"),
        ('{"$defs": {"core_entry": {}}}', "'properties'"),
        ('{"$defs": {"core_entry": {"properties": {}}}}', "'os'"),
        ('{"$defs": {"core_entry": {"properties": {"os": {}}}}}', "'enum'"),
    ], ids=["defs", "core_entry", "properties", "os", "enum"])
    def test_each_missing_link_in_the_defs_chain_is_named(self, tmp_path, doc, missing):
        # The sibling one level in from the read this issue named: before
        # the fix these five were a raw `KeyError` out of a bare subscript
        # chain, on every interpreter.
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root), doc)
        assert f"is missing required key {missing}" in self._raises(metadata_root)

    def test_a_non_list_enum_is_named_rather_than_iterated(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        _malformed(self._path(metadata_root),
                   '{"$defs": {"core_entry": {"properties": {"os": '
                   '{"enum": "zephyr"}}}}}')
        assert "must be a list, got str" in self._raises(metadata_root)

    def test_directory_where_file_expected(self, tmp_path):
        # THE SILENT-FALLBACK CELL. Before the fix this returned the
        # in-tree schema's enum with no diagnostic at all, on all three
        # interpreters -- `is_file()` answered False for a directory
        # exactly as it did for an absent file, so an unreadable project
        # schema was indistinguishable from no project schema.
        metadata_root = tmp_path / "metadata"
        _as_directory(self._path(metadata_root))
        assert "cannot read board schema at" in self._raises(metadata_root)

    def test_parent_is_a_file(self, tmp_path):
        # ENOTDIR, and the one shape that is DELIBERATELY still a fallback:
        # a parent that is a regular file means this root has no `schemas/`
        # directory at all, which is the same "carries no schema of its
        # own" fact ENOENT reports. `read_optional_text`'s docstring says
        # so; this drives it.
        metadata_root = tmp_path / "metadata"
        _parent_is_a_file(self._path(metadata_root))
        choices = self._call(metadata_root)
        assert "zephyr" in choices and "off" in choices

    def test_symlink_loop(self, tmp_path):
        metadata_root = tmp_path / "metadata"
        _symlink_loop(self._path(metadata_root))
        assert "cannot read board schema at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        metadata_root = self._prepared(tmp_path)
        with _permission_denied_file(self._path(metadata_root)):
            assert "cannot read board schema at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_parent(self, tmp_path):
        # The tan-cli#1127 cell: raw `PermissionError` on 3.12.3/3.13.15
        # before the fix, and a SILENT wrong-document fallback on 3.14.7.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(self._path(metadata_root).parent):
            assert "cannot read board schema at" in self._raises(metadata_root)

    @_skip_as_root
    def test_permission_denied_metadata_root(self, tmp_path):
        # The grandparent cell, driven here for the same reason as on
        # `TestLoadSocSpec`: already curated on all three with `metadata/`
        # denied, and pinned so it stays that way.
        metadata_root = self._prepared(tmp_path)
        with _permission_denied(metadata_root):
            assert "cannot read board schema at" in self._raises(metadata_root)


@_needs_sdk
@_covers("kconfig._emit_extra_library_profile")
class TestEmitExtraLibraryProfile:
    """tan-cli#1122, the sixth `PINNED_HASHES`-protected site (see this
    file's own module docstring for why editing it is safe: the freshness
    gate hashes only the alp-sdk SOURCE side of the comparison, never this
    file).

    `profile_rel` is joined onto the bound `REPO` inside the function
    (`REPO / profile_rel`) -- but pathlib lets an ABSOLUTE `rel` replace
    the left side outright (`Path("a") / "/etc/passwd" ==
    Path("/etc/passwd")`, the same fact `template.py::_safe_join`'s own
    docstring names for the identical join), so every case here passes an
    absolute `tmp_path`-rooted string and reaches the real read without
    writing anything into the bound SDK checkout. `project` is never
    touched on any of these paths -- the function returns before its first
    reference to it -- so `None` stands in for a real `BoardProject`.
    """

    _NAME = "seed1122"

    def _call(self, path: Path, project=None):
        m = _planner_module("kconfig")
        return m._emit_extra_library_profile(self._NAME, str(path), project)

    def _assert_parse_failed(self, lines: list[str]) -> None:
        # One quiet-return message for every read/parse failure -- the
        # function's own contract makes no distinction between "absent"
        # and "there but broken", unlike the curated-raise family above.
        assert len(lines) == 1
        assert lines[0].startswith(
            f"# extra_libraries[{self._NAME}] profile parse failed:")

    def test_absent(self, tmp_path):
        self._assert_parse_failed(self._call(tmp_path / "profile.yaml"))

    def test_non_utf8(self, tmp_path):
        path = tmp_path / "profile.yaml"
        _non_utf8(path)
        self._assert_parse_failed(self._call(path))

    def test_directory_where_file_expected(self, tmp_path):
        path = tmp_path / "profile.yaml"
        _as_directory(path)
        self._assert_parse_failed(self._call(path))

    def test_parent_is_a_file(self, tmp_path):
        path = tmp_path / "parent" / "profile.yaml"
        _parent_is_a_file(path)
        self._assert_parse_failed(self._call(path))

    def test_symlink_loop(self, tmp_path):
        # tan-cli#1122's own extra find: before this fix, the unguarded
        # `.resolve()` this call used to run raised a bare `RuntimeError`
        # for exactly this shape on 3.12.3 -- outside any `try` in the
        # function, so no widening of the `except` tuple alone would have
        # caught it. Removing the unneeded `.resolve()` (rather than
        # adding a `RecursionError`-reraise dance around it, the fix
        # `template.py::_safe_join` needed for the same trap) closes it by
        # construction: `open()`'s own ELOOP is a plain `OSError` on every
        # interpreter this repo supports.
        path = tmp_path / "profile.yaml"
        _symlink_loop(path)
        self._assert_parse_failed(self._call(path))

    @_skip_as_root
    def test_permission_denied_file(self, tmp_path):
        path = tmp_path / "profile.yaml"
        path.write_text("accelerators: []\n", encoding="utf-8")
        with _permission_denied_file(path):
            self._assert_parse_failed(self._call(path))

    def test_malformed_document(self, tmp_path):
        path = tmp_path / "profile.yaml"
        _malformed(path, "a: [1, 2\nb: }{\n")
        self._assert_parse_failed(self._call(path))


def test_every_seed_has_a_test():
    missing = sorted(set(_SEEDED_CONTRACTS) - _covered)
    assert not missing, (
        "these _SEEDED_CONTRACTS entries have no test group exercising them "
        f"(add a class decorated @_covers(name)):\n  " + "\n  ".join(missing)
    )
    extra = sorted(_covered - set(_SEEDED_CONTRACTS))
    assert not extra, (
        "these test groups cover a name _SEEDED_CONTRACTS does not declare "
        f"(the @_covers decorator itself would have caught this earlier -- "
        f"if you see this, something bypassed it):\n  " + "\n  ".join(extra)
    )
