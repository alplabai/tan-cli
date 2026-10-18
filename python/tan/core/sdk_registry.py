# SPDX-License-Identifier: Apache-2.0
"""Pure half of tan-cli#1553: which CMake package-registry entries for
`Zephyr-sdk` are tan's own dead registrations.

`west sdk install` ends with the SDK's `setup.sh -c` / `setup.cmd -c`, i.e.
`cmake -P zephyr_sdk_export.cmake`, which registers the directory it was run
from: on POSIX a file `~/.cmake/packages/Zephyr-sdk/<md5>`, on Windows the
value `<md5>` under `HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\
Zephyr-sdk`; the data is `<install dir>/cmake` and the name is the MD5 of that
path. tan installs into `<root>/<leaf>.tmp-<pid>` and then moves it into the
store, so the entry it caused always points at a directory that is gone.

The entry's NAME is derived from its path, so a user's own registration can
never share a name with one of these, and an entry is tan's exactly when its
path is a `<root>/zephyr-sdk-*.tmp-*/cmake` directory -- a name only tan
creates, under a root only tan writes. Nothing else is ever matched, so
pruning cannot remove a registration the user made.
"""
from __future__ import annotations

from tan.core.toolchain_provision import TMP_SUFFIX_PREFIX

#: Every store leaf tan names is `zephyr-sdk-<version>-<component>`
#: (`toolchain_provision.store_dir_name`); any pin's `.tmp-*` counts, since an
#: older pin's leftover registration is just as dead.
_TAN_LEAF_PREFIX = "zephyr-sdk-"

_SDK_CMAKE_DIR = "cmake"


def _parts(path: str, *, case_insensitive: bool) -> list[str]:
    text = path.replace("\\", "/").rstrip("/")
    if case_insensitive:
        text = text.lower()
    return [part for part in text.split("/") if part]


def is_tan_tmp_registration(data: str, root: str, *, case_insensitive: bool) -> bool:
    """Whether a registry entry's `data` is `<root>/zephyr-sdk-*.tmp-*/cmake`.
    `case_insensitive` for Windows, whose paths compare that way; separators
    are normalised on every host."""
    entry = _parts(data, case_insensitive=case_insensitive)
    base = _parts(root, case_insensitive=case_insensitive)
    if not entry or len(entry) != len(base) + 2 or entry[: len(base)] != base:
        return False
    leaf, last = entry[-2], entry[-1]
    return (
        last == _SDK_CMAKE_DIR
        and leaf.startswith(_TAN_LEAF_PREFIX)
        and TMP_SUFFIX_PREFIX in leaf
    )
