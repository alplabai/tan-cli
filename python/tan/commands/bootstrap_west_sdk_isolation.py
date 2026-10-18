# SPDX-License-Identifier: Apache-2.0
"""IO half of tan-cli#1552 for `bootstrap_cmd._acquire_toolchain`: the scratch
`HOME` and filtered search lists `west sdk install` runs under, and the scan
that names an SDK west could still reuse. The reasoning lives in
`tan.core.west_sdk_isolation`."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from tan.core import toolchain_provision
from tan.core import west_sdk_isolation as core

#: Module-level so a test can point the scan at a temp tree.
SYSTEM_SDK_PREFIXES: tuple[str, ...] = core.SYSTEM_SDK_PREFIXES

#: CMake CONFIG mode matches `<prefix>/<name>*/cmake/` for `<name>` =
#: `Zephyr-sdk`; the SDK's own directory name is `zephyr-sdk-<version>`.
_SDK_DIR_GLOB = "zephyr-sdk*"

#: What makes a directory an SDK root to `find_package(Zephyr-sdk)`.
_SDK_CONFIG_RELPATH = ("cmake", "Zephyr-sdkConfig.cmake")

#: `PATH` entries become prefixes after [`core.path_prefix_of`];
#: `CMAKE_PREFIX_PATH` entries are prefixes as they stand.
_FILTERED_LISTS: tuple[tuple[str, bool], ...] = (
    (core.PATH_ENV_VAR, True),
    (core.CMAKE_PREFIX_PATH_ENV_VAR, False),
)


def make_scratch_home(root: Path, leaf: str) -> Path:
    """A fresh empty directory under the toolchain root, named
    `<leaf>.tmp-home-*` so `_reclaim_toolchain_wreckage` sweeps one a SIGKILL
    left behind. Raises `OSError`; the caller deletes it with
    [`discard_scratch_home`]."""
    prefix = f"{leaf}{toolchain_provision.TMP_SUFFIX_PREFIX}{core.SCRATCH_HOME_INFIX}"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def discard_scratch_home(home: Path) -> None:
    shutil.rmtree(home, ignore_errors=True)


def _is_zephyr_sdk_root(prefix: str) -> bool:
    try:
        return Path(prefix).joinpath(*_SDK_CONFIG_RELPATH).is_file()
    except (OSError, ValueError):
        return False


def _user_netrc() -> str | None:
    """The netrc `requests` would have read from the real home -- `.netrc`
    then `_netrc` -- when `$NETRC` does not already point elsewhere (an
    inherited `$NETRC` reaches the child unchanged)."""
    if os.environ.get(core.NETRC_ENV_VAR):
        return None
    try:
        home = Path.home()
        for name in core.NETRC_FILENAMES:
            if (home / name).is_file():
                return str(home / name)
    except (OSError, RuntimeError):
        return None
    return None


def _filtered_lists() -> dict[str, str]:
    changed: dict[str, str] = {}
    for var, strip_bin in _FILTERED_LISTS:
        inherited = os.environ.get(var, "")
        filtered = core.without_sdk_roots(
            inherited, sep=os.pathsep, is_sdk_root=_is_zephyr_sdk_root, strip_bin=strip_bin
        )
        if filtered != inherited:
            changed[var] = filtered
    return changed


def isolated_env(home: Path) -> dict[str, str]:
    return core.isolated_west_sdk_env(
        str(home), user_netrc=_user_netrc(), filtered_lists=_filtered_lists()
    )


def _sdk_version_of(directory: Path) -> str | None:
    try:
        return (directory / "sdk_version").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _visible_prefixes() -> list[str]:
    """The system prefixes plus the prefixes of the `PATH` the child actually
    got -- an entry [`isolated_env`] dropped cannot be what west reused."""
    path = _filtered_lists().get(core.PATH_ENV_VAR, os.environ.get(core.PATH_ENV_VAR, ""))
    entries = [core.path_prefix_of(entry) for entry in path.split(os.pathsep) if entry]
    return [*SYSTEM_SDK_PREFIXES, *entries]


def reusable_sdk_candidates(version: str, *, prefixes: Sequence[str] | None = None) -> list[str]:
    """Every `zephyr-sdk*` SDK root at `version` directly under a prefix west
    still searches (default: [`_visible_prefixes`]). Sorted, de-duplicated,
    never raises."""
    found: set[str] = set()
    for prefix in dict.fromkeys(_visible_prefixes() if prefixes is None else prefixes):
        try:
            children = list(Path(prefix).glob(_SDK_DIR_GLOB))
        except (OSError, ValueError):
            continue
        for child in children:
            if _is_zephyr_sdk_root(str(child)) and _sdk_version_of(child) == version:
                found.add(str(child))
    return sorted(found)
