# SPDX-License-Identifier: Apache-2.0
"""Pure half of tan-cli#1553: which CMake package-registry entries for
`Zephyr-sdk` are tan's own dead registrations.

`west sdk install` ends with the SDK's `setup.sh -c` / `setup.cmd -c`, i.e.
`cmake -P zephyr_sdk_export.cmake`, which registers the directory it was run
from: on POSIX a file `~/.cmake/packages/Zephyr-sdk/<md5>`, on Windows the
value `<md5>` under `HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\
Zephyr-sdk`. The data is `<install dir>/cmake` and the name is the MD5 of that
exact string. tan installs into `<root>/<leaf>.tmp-<suffix>` and then moves it
into the store, so the entry it caused always points at a directory that is
gone.

An entry is pruned only when BOTH hold ([`is_tan_tmp_registration`]):

- its data is exactly `<root>/zephyr-sdk-*.tmp-<suffix>/cmake` -- one level
  under tan's toolchain root, in the shape only tan's own install dirs have
  (`toolchain_provision.TMP_SUFFIX_PREFIX`); and
- its name is the MD5 of its data, i.e. `zephyr_sdk_export.cmake` wrote it.

Nothing else is ever matched. Path variants that do not compare equal (an
unexpanded `%VAR%`, `..`, a UNC or 8.3 spelling) can only cause a missed
prune, never a false one; `root` is tried both as given and resolved
(`~` expanded, symlinks followed), since CMake may record either form.
"""
from __future__ import annotations

import hashlib
import os

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


def _is_tan_tmp_leaf(leaf: str) -> bool:
    head, sep, tail = leaf.partition(TMP_SUFFIX_PREFIX)
    return bool(sep) and bool(tail) and head.startswith(_TAN_LEAF_PREFIX)


def _root_forms(root: str) -> list[str]:
    resolved = os.path.realpath(os.path.expanduser(root))
    return [root] if resolved == root else [root, resolved]


def _under_root(data: str, root: str, *, case_insensitive: bool) -> bool:
    entry = _parts(data, case_insensitive=case_insensitive)
    base = _parts(root, case_insensitive=case_insensitive)
    if len(entry) != len(base) + 2 or entry[: len(base)] != base:
        return False
    return entry[-1] == _SDK_CMAKE_DIR and _is_tan_tmp_leaf(entry[-2])


def is_tan_tmp_registration(
    name: str, data: str, root: str, *, case_insensitive: bool
) -> bool:
    """Whether the registry entry `name` -> `data` is one tan's own `.tmp-*`
    install dir caused. `case_insensitive` for Windows paths (the MD5 name is
    compared case-insensitively too); separators are normalised on every host."""
    if not data or name.lower() != hashlib.md5(data.encode("utf-8")).hexdigest():
        return False
    return any(
        _under_root(data, form, case_insensitive=case_insensitive)
        for form in _root_forms(root)
    )
