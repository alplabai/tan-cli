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

THE SHAPE SWEEP (tan-cli#1116's own instruction: seed from the SHAPE, not
from the four names the issue body already listed -- issue comment 2 found
`perf_apply.py`'s two sites exactly because a reviewer went looking in a
NEIGHBOURING module a name-only seed would have missed).

An AST walk over every `python/tan/**` function whose `try` wraps a
`.read_text(`/`.read_bytes(`/`open(`/`yaml.safe_load(`/`json.load(`/`
json.loads(` call, with a `broad` catch (`except Exception`/`BaseException`)
excluded as safe-by-construction: **79** functions, reproducible by re-running
the same walk (not committed here -- a one-off audit script, not a second
gate). Most are false positives of the mechanical heuristic (a `try` matching
a `.load(`-shaped attribute name on already-decoded text, not a file read; a
name that shadows a stdlib name) or already hold a properly-widened contract
(`OSError` PLUS the decode/parse family the call actually raises). Reading
each of those 79 plus the four issue-named sites plus their explicitly-named
siblings (`error_catalog.py:104`, `sdk_discovery.py:189`, `metadata_schema.
py:209`, `scaffold.py:2034`), 18 were genuine candidates worth driving
against real broken files rather than reasoning about from the source; doing
that (the same seven shapes below, run against each) found:

  * TWO more `except (OSError, yaml.YAMLError)` sites missing
    `UnicodeDecodeError` exactly like `perf_apply.py`'s pair --
    `tan/model/adapters/drpai.py::_compiler_version` (FIXED here) and
    `tan/planner/kconfig.py::_emit_extra_library_profile` (found, NOT fixed
    here -- `kconfig.py` is a hash-pinned verbatim mirror of alp-sdk's
    `scripts/alp_orchestrate/kconfig.py`, `test_planner_relocation_
    freshness.py`'s `PINNED_HASHES`; editing it here without first porting
    the fix upstream and re-pinning that hash is exactly the drift that gate
    exists to catch, and is out of this issue's scope. NOT seeded below for
    the same reason a gate seeded on a currently-failing function is
    disabled on day one: it does not hold today).
  * THREE `is_file()`/`is_dir()` PRE-FLIGHT checks that raised a raw
    `PermissionError` on a permission-denied ANCESTOR directory -- the exact
    PR #1110 `Path.exists()` trap (`_IGNORED_ERRNOS` swallows `ENOENT`/
    `ENOTDIR`/`EBADF`/`ELOOP`, never `EACCES`), just via a sibling stdlib
    call instead of `Path.exists()` itself. All three measured escaping
    raw, all three FIXED here: `perf_apply._resolve_hw_rev`, `metadata_
    schema.validate_document`, `scaffold.read_example_tree`.

So: **79** mechanically-flagged, **18** hand-read and shape-driven, **5**
live defects found this way (3 fixed here, plus the issue's own two named
live sites in `perf_apply.py`, also fixed here -- 6 fixes total; 1 more
found-and-deferred as out of scope), **12** seeded into `_SEEDED_CONTRACTS`
below. The gap between 79 and 12 is real and recorded here rather than
implied clean: most of the 79 are either false positives of the mechanical
walk or already-correct functions this file simply has not been asked to
seed yet.

THE ROOT/CI CAVEAT (tan-cli#1105's own lesson, named explicitly in this
issue so it is not silently repeated): every permission-denied shape below
is `@_skip_as_root`, which SKIPS -- loudly, by name, with a reason string a
CI log shows -- under `os.geteuid() == 0` or on Windows. This is NOT a
silent gap: `.github/workflows/ci.yml`'s `python` job that runs this
directory (`gates` included) runs on `runs-on: ubuntu-latest` with no
`container:` key, and a GitHub-hosted `ubuntu-latest` runner with no
container override runs its job steps as the unprivileged `runner` account,
not root (measured: `id -u` on this very sandbox, itself non-root, reports
1000; GitHub's own hosted-runner documentation states the same for
`ubuntu-latest`). So the permission shape DOES execute, for real, on the one
CI job that runs this file. It skips only for a human running the suite as
root locally (`sudo pytest ...`) or a self-hosted/containerized runner that
overrides the default user -- a real, named gap, not the tan-cli#1105 shape
of "skips everywhere it matters and nobody noticed."
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from tan.core import document_guards, error_catalog, example_catalog, example_facets
from tan.core import metadata_schema, scaffold, sdk_discovery
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
# metadata_schema.validate_document -- quiet-return: [] for a schema that is
# genuinely absent (the ABSENT half of its own two-shape docstring), a
# one-message list for a schema that exists but cannot be read/parsed.
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
        # A directory is not `is_file()`: the ABSENT half, `[]`.
        assert metadata_schema.validate_document({}, schema_path, source="src") == []

    def test_parent_is_a_file(self, tmp_path):
        schema_path = tmp_path / "parent" / "schema.json"
        _parent_is_a_file(schema_path)
        # `is_file()` swallows ENOTDIR too: the ABSENT half, `[]`.
        assert metadata_schema.validate_document({}, schema_path, source="src") == []

    def test_symlink_loop(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        _symlink_loop(schema_path)
        # `is_file()` swallows ELOOP too: the ABSENT half, `[]`.
        assert metadata_schema.validate_document({}, schema_path, source="src") == []

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
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
        # `tvm_home` itself absent (its parent already exists as tmp_path,
        # a real directory) exercises the same FileNotFoundError arm every
        # candidate hits when tvm_home does not exist at all.
        assert drpai._compiler_version(tmp_path / "does-not-exist") == "drp-ai_tvm"

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
# stdlib exception, on every failure.
# ---------------------------------------------------------------------------


@_covers("scaffold.read_example_tree")
class TestReadExampleTree:
    def test_absent(self, tmp_path):
        with pytest.raises(scaffold.ExampleReadError):
            scaffold.read_example_tree(tmp_path / "example")

    def test_non_utf8(self, tmp_path):
        source_dir = tmp_path / "example"
        source_dir.mkdir()
        _non_utf8(source_dir / "main.c")
        with pytest.raises(scaffold.ExampleReadError):
            scaffold.read_example_tree(source_dir)

    def test_directory_where_file_expected(self, tmp_path):
        # `source_dir` itself is a plain FILE, not a directory -- the
        # `is_dir()` half of this function's own contract.
        source_dir = tmp_path / "example"
        source_dir.write_text("not a directory")
        with pytest.raises(scaffold.ExampleReadError):
            scaffold.read_example_tree(source_dir)

    def test_parent_is_a_file(self, tmp_path):
        source_dir = tmp_path / "parent" / "example"
        _parent_is_a_file(source_dir)
        with pytest.raises(scaffold.ExampleReadError):
            scaffold.read_example_tree(source_dir)

    def test_symlink_loop(self, tmp_path):
        # Not via an INNER file -- `_example_source_files` skips symlinks
        # structurally (never follows one, matching the oracle's
        # `DirEntry::file_type()`), so an inner loop cannot reach the read
        # path at all. The top-level `source_dir` itself as the loop is the
        # shape that actually exercises this function.
        source_dir = tmp_path / "example"
        _symlink_loop(source_dir)
        with pytest.raises(scaffold.ExampleReadError):
            scaffold.read_example_tree(source_dir)

    @_skip_as_root
    def test_permission_denied(self, tmp_path):
        # PARENT of source_dir denied, not source_dir itself (tan-cli#1116's
        # own fix here: `source_dir.is_dir()` raised raw `PermissionError`
        # for exactly this shape before the fix).
        outer = tmp_path / "outer"
        source_dir = outer / "example"
        source_dir.mkdir(parents=True)
        (source_dir / "main.c").write_text("int main(void) { return 0; }")
        with _permission_denied(outer):
            with pytest.raises(scaffold.ExampleReadError):
                scaffold.read_example_tree(source_dir)

    def test_malformed_document(self, tmp_path):
        # No document shape of its own -- a text file is either valid UTF-8
        # (copied verbatim) or it is the non_utf8 shape above. Nothing
        # further to malform.
        pytest.skip("read_example_tree has no parse step of its own to malform")


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
