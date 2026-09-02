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
only the seeded twelve until someone adds the next one, the same "opt-in,
not a blanket walk" reasoning `_SHARED_HELPERS`'s own docstring gives: a
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
    NOT fixed -- `kconfig.py` is a hash-pinned verbatim mirror of alp-sdk's
    `scripts/alp_orchestrate/kconfig.py`, `test_planner_relocation_
    freshness.py`'s `PINNED_HASHES`; editing it here without first porting
    the fix upstream and re-pinning that hash is exactly the drift that gate
    exists to catch, and is out of this issue's scope).
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
    shape. NOT fixed -- both in `PINNED_HASHES`, same reasoning as
    `kconfig.py`.

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
in `_SEEDED_CONTRACTS`' neighbouring comments, all deliberately deferred
rather than fixed, all reported rather than silently skipped. **15** seeded
into `_SEEDED_CONTRACTS` below -- the
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
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from tan.commands import new_som_cmd
from tan.core import document_guards, error_catalog, example_catalog, example_facets
from tan.core import metadata_schema, scaffold, sdk_discovery, som_buildability
from tan.model import perf, perf_apply
from tan.model.adapters import drpai

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
