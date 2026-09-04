# SPDX-License-Identifier: Apache-2.0
"""tan-cli#655: the file-existence side of Zephyr's own devicetree-overlay /
Kconfig-fragment auto-discovery, tracked across `tan build` invocations so
`execute.py` can tell when the discoverable SET changed (a new `app.overlay`
appeared, an existing one was removed, a new `boards/<board>.conf` showed up)
-- the one case Zephyr's own `CMAKE_CONFIGURE_DEPENDS` machinery does not
self-heal, because `configuration_files.cmake` caches its auto-discovery
result the first time it runs and only re-discovers `if(NOT DEFINED ...)`.
See `execute._maybe_reset_stale_configure_cache` for the fix this supports
and the measurement behind it.

Existence-only, not content: an EDIT to an ALREADY-discovered file is already
handled by Zephyr's own `CMAKE_CONFIGURE_DEPENDS` (the tracked file's mtime
bump alone triggers a reconfigure) -- this module exists only for the set-
membership change that mechanism cannot see."""
from __future__ import annotations

import os
from pathlib import Path, PurePath

from tan.core.shapes import is_file, matches_glob_suffix

__all__ = [
    "configure_inputs_stamp_path",
    "discover_configure_inputs",
    "read_configure_inputs_stamp",
    "relative_key",
    "resolve_zephyr_discovery_dir",
    "write_configure_inputs_stamp",
]

#: Lives inside west's own nested build dir (mirrors `manifest.py`'s
#: `_SDK_STAMP_FILE`/`sdk_stamp_path`), so a pristine wipe of that dir retires
#: this stamp atomically along with the CMake cache it describes -- it can
#: never outlive or misdescribe a build dir that no longer exists.
_CONFIGURE_INPUTS_STAMP_FILE = ".tan-configure-inputs"


def resolve_zephyr_discovery_dir(app_dir: str, build_root: Path) -> Path:
    """Anchor a slice's plan-supplied `appDir` on `build_root`, then apply
    Zephyr's own `app: ./src`-vs-self-contained convention, so a caller
    working from the CONFIGURED app dir (`appDir` -- see
    `tan.commands.build_cmd`'s "Note `appDir` is NOT the substituted value"
    docstring) lands on the directory Zephyr actually auto-discovers
    overlays/Kconfig fragments from -- the same one `west build` is pointed
    at (tan-cli#798).

    Anchoring mirrors both existing `appDir` probes,
    `build_cmd._missing_app_dirs` and `build_cmd._substituted_app_dirs`: a
    relative `appDir` resolves against `build_root`, never the `tan`
    process's own CWD, so the result does not depend on where `tan` was
    invoked from. The fallback mirrors `tan.planner.orchestrator`'s
    `_zephyr_app_dir` (hash-audited, tan-cli#655's relocation gate -- not
    importable here without reproducing its `_resolve_app_path` machinery
    for a value, `appDir`, that is already resolved): when `app_dir` itself
    carries no `CMakeLists.txt` but its parent does, Zephyr auto-discovers
    from the parent instead (the shipped `app: ./src` sources-only
    convention -- 96 of 105 alp-sdk example core entries use it). Neither
    directory existing is reported as-is; the caller's own existence checks
    (`discover_configure_inputs` returns the empty set for a missing dir)
    already handle that case correctly without this function raising.

    Unlike `build_cmd._substituted_app_dirs`, this does NOT carry an
    `app_dir_path.is_dir()` precondition before falling back to the parent --
    a nonexistent `app_dir` whose parent happens to carry a `CMakeLists.txt`
    globs the parent here. Deliberately left unguarded: this function's only
    caller runs on slices that already survived `_missing_app_dirs`
    (`build.app-dir-missing` already failed a nonexistent `appDir` before
    `execute_slices` dispatches it), so the divergence is unreachable in
    practice, not merely tolerated."""
    app_dir_path = Path(app_dir)
    if not app_dir_path.is_absolute():
        app_dir_path = build_root / app_dir_path
    if (app_dir_path / "CMakeLists.txt").is_file():
        return app_dir_path
    if (app_dir_path.parent / "CMakeLists.txt").is_file():
        return app_dir_path.parent
    return app_dir_path


#: The two auto-discovery families `cmake/modules/configuration_files.cmake`
#: resolves `if(NOT DEFINED ...)`: Kconfig fragments (`CONF_FILE`, `NAMES
#: "prj.conf"` at the app root plus `boards/`/`socs/` qualifiers) and
#: devicetree overlays (`DTC_OVERLAY_FILE`, `NAMES "app.overlay"` plus the
#: same `boards/`/`socs/` qualifiers). Not an attempt to reproduce Zephyr's
#: exact per-qualifier candidate-name algorithm (board revision, `SUFFIX`,
#: sysbuild image scoping -- all of which drift across Zephyr versions);
#: tracking every `*.conf`/`*.overlay` at these locations is a superset that
#: stays correct even when it over-tracks a file Zephyr itself would not have
#: picked for THIS exact board target -- an extra reset is harmless, a missed
#: one is the bug this module exists to close.
_CANDIDATE_SUFFIXES = (".conf", ".overlay")

#: The two qualifier subtrees, walked to ANY depth below themselves -- the
#: `boards/**/*.conf` half of what this module used to express as six
#: `Path.glob` patterns. `**` matched zero or more directories, so a file
#: directly in `boards/` counted too; the walk below reproduces that by
#: starting at the subtree root itself, not at its children.
_CANDIDATE_SUBTREES = ("boards", "socs")

# WHY `os.scandir` AND NOT `Path.glob` (tan-cli#1132, following tan-cli#1127's
# identical swap in `new_som_cmd._known_board_names`).
#
# This module used to iterate `app_dir.glob(pattern)` OUTSIDE the `try` that
# was supposed to guard it:
#
#     try:
#         matches = app_dir.glob(pattern)   # lazy: builds a generator
#     except OSError:
#         continue
#     for path in matches:                  # the filesystem work happens HERE
#
# `Path.glob` is lazy, so the guarded statement could not fail and the
# `except OSError` was dead on every interpreter -- it wrapped the
# construction of a generator. Forcing iteration inside the `try` would have
# fixed the escape but not the contract, because `Path.glob` does not raise
# uniformly. Measured on this tree, non-root, for the two denied shapes
# (`app_dir` itself `chmod 000`, and `app_dir`'s PARENT `chmod 000`):
#
#   primitive                      self-denied            ancestor-denied
#   Path.is_dir()                  True   / True  / True  RAISE / RAISE / False
#   list(Path.glob("*.conf"))      []     / []    / []    RAISE / []    / []
#   os.listdir() / os.scandir()    RAISE  / RAISE / RAISE RAISE / RAISE / RAISE
#                                  (3.12.3 / 3.13.15 / 3.14.7)
#
# So `Path.glob` never raises at all for a self-denied directory, and raises
# for a denied ancestor only on 3.12.3 -- an `except OSError` around it is
# dead code on 3.13.15 and 3.14.7. `os.scandir` raises `PermissionError` for
# both shapes on all three, which is what makes the single `except OSError`
# in `discover_configure_inputs` load-bearing everywhere rather than on one
# interpreter by accident. The `if not app_dir.is_dir()` pre-flight is gone
# for the same reason (tan-cli#1127, and PR #1110/#1121 before it): it
# raised `PermissionError` on 3.12.3/3.13.15 for a denied ancestor and
# answered `False` on 3.14.7, so it could not be the thing that decides this
# function's contract. The real exception classifies it now.


def relative_key(path: PurePath, base: PurePath) -> str:
    """@path relative to @base, spelled with forward slashes -- THE stamp
    file's key, and the one expression `_candidate_files` uses to build one.

    A separate function so the Windows spelling can be DRIVEN rather than
    restated (tan-cli#1132 review): `os.scandir` hands back a
    backslash-separated `entry.path` on Windows, so the key must be proven to
    normalise. Taking `PurePath` (not `Path`) is what makes that provable on
    ANY host without touching a filesystem, which is the point -- CI does run
    this suite on `windows-latest` (`parity.yml`'s `python-tests-shard`, whose
    matrix `os:` carries all three platforms; tan-cli#1153 corrects the "no
    Windows host" claim that stood here), but only after a push, whereas a
    pure-string oracle answers while the code is being written. A test passes
    real `PureWindowsPath` values through THIS function, so a future edit here
    (say, to `os.path.relpath`, which would leave backslashes in) reds the
    oracle instead of sailing past a restatement of the old expression. The
    keys are compared across runs, so a changed spelling would report every
    tracked file as new on the first build after an upgrade."""
    return path.relative_to(base).as_posix()


def _candidate_files(directory: Path, base: Path, *, recurse: bool) -> set[str]:
    """Relative-POSIX paths (to @base) of the regular files directly in
    @directory -- and, when @recurse, at any depth below it -- whose name
    matches `_CANDIDATE_SUFFIXES`.

    Raises `OSError` (`PermissionError`, `FileNotFoundError`,
    `NotADirectoryError`, ...) for any directory it cannot list; the single
    caller classifies. See the block comment above for why that is
    `os.scandir` and not `Path.glob`.

    Three `Path.glob` behaviours this deliberately reproduces rather than
    quietly improving on:

    * A symlinked directory is NOT descended into -- `**` does not follow
      symlinks on any supported interpreter (3.13's own
      `recurse_symlinks=False` default), which is what
      `entry.is_dir(follow_symlinks=False)` preserves.
    * A name matching a candidate suffix that is a DIRECTORY is not a match,
      the old code's `path.is_file()` filter.
    * A per-entry stat failure SKIPS that entry rather than failing the
      walk. That is why the file test is `shapes.is_file` and not
      `entry.is_file()`: measured on 3.12.3, 3.13.15 and 3.14.7 alike, a
      self-referential `loop.conf -> loop.conf` symlink makes
      `os.DirEntry.is_file()` raise `OSError(ELOOP)` where `Path.is_file()`
      -- what the `glob` version called -- returns `False` and keeps the
      rest of the set. Only the DIRECTORY LISTING is allowed to fail the
      walk, and `os.scandir` above is the call that does it.

    Symlinks are otherwise followed for the file test, so a symlink TO a
    fragment still counts, exactly as it did through `glob` + `is_file()`."""
    found: set[str] = set()
    pending = [directory]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if recurse and entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif matches_glob_suffix(entry.name, *_CANDIDATE_SUFFIXES) and is_file(entry.path):
                    found.add(relative_key(Path(entry.path), base))
    return found


def discover_configure_inputs(app_dir: Path) -> frozenset[str]:
    """The set of configure-time Kconfig-fragment/devicetree-overlay paths
    (relative POSIX, to `app_dir`) currently present under `app_dir` at one
    of Zephyr's own auto-discovery locations. Returns the empty set for a
    missing or unreadable `app_dir`, and for an unreadable `boards/`/`socs/`
    subtree under it -- "nothing discoverable" is the same honest answer a
    real Zephyr configure would reach, not an error this best-effort tracker
    should raise.

    An ABSENT `boards/`/`socs/` is the ordinary case, not a failure: it
    contributes nothing and the app-root fragments still come back. That is
    the inner handler's whole job, and it is why the outer one cannot simply
    swallow every `OSError` from the whole walk."""
    found: set[str] = set()
    try:
        found |= _candidate_files(app_dir, app_dir, recurse=False)
        for subtree in _CANDIDATE_SUBTREES:
            try:
                found |= _candidate_files(app_dir / subtree, app_dir, recurse=True)
            except (FileNotFoundError, NotADirectoryError):
                continue
    except OSError:
        return frozenset()
    return frozenset(found)


def configure_inputs_stamp_path(slice_cwd: Path) -> Path:
    """Path of the tan-owned configure-inputs stamp for one slice."""
    return slice_cwd / "build" / _CONFIGURE_INPUTS_STAMP_FILE


def read_configure_inputs_stamp(slice_cwd: Path) -> frozenset[str] | None:
    """The configure-input file set recorded at the last stamp write, or
    `None` for "never stamped", "unreadable", or "unreadable as UTF-8" --
    all three read as "no signal", which the caller treats conservatively
    (as a change, forcing one reset) rather than trusting a cache it cannot
    actually verify. An empty stamped file (no candidate files at all) reads
    back as the real empty set, `frozenset()`, not `None`."""
    try:
        text = configure_inputs_stamp_path(slice_cwd).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return frozenset(line for line in text.splitlines() if line)


def write_configure_inputs_stamp(slice_cwd: Path, inputs: frozenset[str]) -> None:
    """Write the stamp BEFORE the tool is spawned (see the caller): a
    mid-configure failure still stamped correctly, since the discoverable
    set on disk really was what `inputs` records regardless of whether the
    build finishes. One relative path per line, sorted for a stable diff.
    Raises `OSError` on failure -- the caller treats the write as
    best-effort (a write failure just means the next run may treat this dir
    as unstamped: fails toward a spurious extra reset, never toward trusting
    a stale cache)."""
    path = configure_inputs_stamp_path(slice_cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(inputs)) + ("\n" if inputs else ""), encoding="utf-8")
