# SPDX-License-Identifier: Apache-2.0
"""IO half of tan-cli#1553: list and prune `Zephyr-sdk` entries in CMake's
user package registry -- files under `~/.cmake/packages/Zephyr-sdk/` on POSIX,
REG_SZ values under HKCU on Windows. Which entries are tan's own dead ones is
decided by `tan.core.sdk_registry`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tan.core.sdk_registry import is_tan_tmp_registration

#: `zephyr_sdk_export.cmake`'s own key, under HKEY_CURRENT_USER.
WINDOWS_SUBKEY = r"Software\Kitware\CMake\Packages\Zephyr-sdk"

#: Which registry this host has. Module-level so a test can pin it.
HOST_IS_WINDOWS = os.name == "nt"

#: The same registry on POSIX, relative to the home directory.
POSIX_RELPATH = (".cmake", "packages", "Zephyr-sdk")


class Registry(Protocol):
    def list_entries(self) -> list[tuple[str, str]]:
        """`(name, data)` per entry. Raises `OSError` when unreadable."""
        ...

    def remove(self, name: str) -> None:
        """Raises `OSError` when the entry cannot be removed."""
        ...


@dataclass(frozen=True)
class PosixRegistry:
    directory: Path

    def list_entries(self) -> list[tuple[str, str]]:
        if not self.directory.is_dir():
            return []
        entries = []
        for path in sorted(self.directory.iterdir()):
            try:
                entries.append((path.name, path.read_text(encoding="utf-8").strip()))
            except (OSError, UnicodeDecodeError):
                continue
        return entries

    def remove(self, name: str) -> None:
        (self.directory / name).unlink()


@dataclass(frozen=True)
class WindowsRegistry:
    subkey: str = WINDOWS_SUBKEY

    def list_entries(self) -> list[tuple[str, str]]:
        import winreg  # noqa: PLC0415 -- Windows-only module

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey)
        except FileNotFoundError:
            return []
        entries = []
        with key:
            index = 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(key, index)
                except OSError:  # ERROR_NO_MORE_ITEMS
                    break
                index += 1
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str):
                    entries.append((name, data))
        return entries

    def remove(self, name: str) -> None:
        import winreg  # noqa: PLC0415 -- Windows-only module

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.subkey, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)


def host_registry() -> Registry:
    if HOST_IS_WINDOWS:
        return WindowsRegistry()
    return PosixRegistry(Path.home().joinpath(*POSIX_RELPATH))


@dataclass(frozen=True)
class PruneOutcome:
    #: The `data` of every entry removed.
    removed: tuple[str, ...]
    #: One line per entry (or listing) that could not be handled.
    failed: tuple[str, ...]


def prune_tan_tmp_registrations(
    registry: Registry, root: Path, *, case_insensitive: bool
) -> PruneOutcome:
    """Remove every entry pointing at a tan `.tmp-*` directory under `root`.
    Never raises: every failure is returned in `failed` for the caller to
    report."""
    try:
        entries = registry.list_entries()
    except OSError as err:
        return PruneOutcome((), (f"cannot read the CMake package registry: {err}",))
    removed: list[str] = []
    failed: list[str] = []
    for name, data in entries:
        if not is_tan_tmp_registration(data, str(root), case_insensitive=case_insensitive):
            continue
        try:
            registry.remove(name)
        except OSError as err:
            failed.append(f"{data}: {err}")
        else:
            removed.append(data)
    return PruneOutcome(tuple(removed), tuple(failed))
