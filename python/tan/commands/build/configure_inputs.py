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

from pathlib import Path

__all__ = [
    "configure_inputs_stamp_path",
    "discover_configure_inputs",
    "read_configure_inputs_stamp",
    "resolve_zephyr_discovery_dir",
    "write_configure_inputs_stamp",
]

#: Lives inside west's own nested build dir (mirrors `manifest.py`'s
#: `_SDK_STAMP_FILE`/`sdk_stamp_path`), so a pristine wipe of that dir retires
#: this stamp atomically along with the CMake cache it describes -- it can
#: never outlive or misdescribe a build dir that no longer exists.
_CONFIGURE_INPUTS_STAMP_FILE = ".tan-configure-inputs"

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
#: one is the bug this module exists to close. `**` matches zero or more
#: directories, so `boards/**/*.conf` also matches a file directly in
#: `boards/`, not only nested ones.
_CANDIDATE_GLOBS = (
    "*.conf",
    "*.overlay",
    "boards/**/*.conf",
    "boards/**/*.overlay",
    "socs/**/*.conf",
    "socs/**/*.overlay",
)


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
    already handle that case correctly without this function raising."""
    app_dir_path = Path(app_dir)
    if not app_dir_path.is_absolute():
        app_dir_path = build_root / app_dir_path
    if (app_dir_path / "CMakeLists.txt").is_file():
        return app_dir_path
    if (app_dir_path.parent / "CMakeLists.txt").is_file():
        return app_dir_path.parent
    return app_dir_path


def discover_configure_inputs(app_dir: Path) -> frozenset[str]:
    """The set of configure-time Kconfig-fragment/devicetree-overlay paths
    (relative POSIX, to `app_dir`) currently present under `app_dir` at one
    of Zephyr's own auto-discovery locations. Returns the empty set for a
    missing/unreadable `app_dir` -- "nothing discoverable" is the same
    honest answer a real Zephyr configure would reach, not an error this
    best-effort tracker should raise."""
    if not app_dir.is_dir():
        return frozenset()
    found: set[str] = set()
    for pattern in _CANDIDATE_GLOBS:
        try:
            matches = app_dir.glob(pattern)
        except OSError:
            continue
        for path in matches:
            try:
                if path.is_file():
                    found.add(path.relative_to(app_dir).as_posix())
            except OSError:
                continue
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
