# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1553: `west sdk install`'s `setup.sh -c` / `setup.cmd -c` registers
the `.tmp-*` directory it installed to in CMake's user package registry --
`~/.cmake/packages/Zephyr-sdk/<md5>` on POSIX, the
`HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\Zephyr-sdk` value
`<md5>` on Windows -- and tan then moves that directory into its store, so the
entry is dead. #1552's scratch `HOME` stops new POSIX entries; nothing
redirects HKCU, and entries from runs before #1552 are still on disk.

tan now prunes every registration whose path is one of ITS OWN `.tmp-*`
directories, and nothing else: the value name is the MD5 of the path, so the
user's own entries never collide with these and are never touched.

The real `winreg` backend is exercised on `windows-latest` against a
throwaway key; everywhere else the POSIX backend runs against `tmp_path`.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd
from tan.commands import bootstrap_sdk_registry as reg
from tan.core import sdk_registry as core_reg
from tan.core import toolchain_provision as tp
from tests.commands.test_bootstrap_toolchain_phase import (
    _fake_west_sdk_install_writes,
    _install_dir_index,
    _make_sdk_with_toolchains,
    _point_home_at,
    _small_manifest,
    _stub_compiler_probe,  # noqa: F401 -- autouse fixture
    _stub_release_sum_fetch,  # noqa: F401 -- autouse fixture
    _stub_toolchain_download,  # noqa: F401 -- autouse fixture
    _workspace,
)

LEAF = tp.store_dir_name("1.0.1")


def _root(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".alp" / "toolchains"


def _posix_registry(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".cmake" / "packages" / "Zephyr-sdk"


def _linux(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("CMAKE_PREFIX_PATH", raising=False)
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    # The POSIX backend under the test home on every host -- on windows-latest
    # the real one is HKCU, which the dedicated winreg tests below exercise.
    monkeypatch.setattr(reg, "HOST_IS_WINDOWS", False)
    for var in tp.SDK_TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# The predicate: only tan's own `.tmp-*` dirs, never anything else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-91228/cmake", True),
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-91228/cmake/", True),
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-home-abc/cmake", True),
        # The store itself, the user's own SDK, another root, a non-cmake leaf.
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi/cmake", False),
        ("/opt/zephyr-sdk-1.0.1/cmake", False),
        ("/elsewhere/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1/cmake", False),
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1", False),
        ("/r/sub/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1/cmake", False),
        ("", False),
    ],
)
def test_only_tans_own_tmp_dirs_count_as_its_registrations(data, expected):
    assert core_reg.is_tan_tmp_registration(data, "/r", case_insensitive=False) is expected


def test_a_windows_registration_matches_regardless_of_case_and_separator():
    data = "c:/tools/.alp/TOOLCHAINS/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-7/cmake"

    assert core_reg.is_tan_tmp_registration(data, "C:\\tools\\.alp\\toolchains", case_insensitive=True)
    assert not core_reg.is_tan_tmp_registration(
        data, "C:\\tools\\.alp\\toolchains", case_insensitive=False
    )


def test_another_pins_tmp_dir_is_also_tans_own():
    """A `.tmp-*` left by an earlier pin is still a directory tan made under its
    root, and its registration is just as dead."""
    data = "/r/zephyr-sdk-0.17.4-arm-zephyr-eabi.tmp-5/cmake"

    assert core_reg.is_tan_tmp_registration(data, "/r", case_insensitive=False)


# ---------------------------------------------------------------------------
# Pruning, against a fake backend (the shape the Windows one has)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entries: dict[str, str], *, fail_remove: bool = False):
        self.entries = dict(entries)
        self.fail_remove = fail_remove

    def list_entries(self) -> list[tuple[str, str]]:
        return list(self.entries.items())

    def remove(self, name: str) -> None:
        if self.fail_remove:
            raise OSError("access denied")
        del self.entries[name]


def test_pruning_removes_tans_entries_and_keeps_the_users():
    ours = "C:\\tools\\.alp\\toolchains\\zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-9576\\cmake"
    theirs = "C:\\tools\\zephyr-sdk-1.0.1\\cmake"
    fake = _FakeRegistry({"aaa": ours, "bbb": theirs})

    outcome = reg.prune_tan_tmp_registrations(
        fake, Path("C:/tools/.alp/toolchains"), case_insensitive=True
    )

    assert fake.entries == {"bbb": theirs}
    assert outcome.removed == (ours,)
    assert outcome.failed == ()


def test_a_removal_that_fails_is_reported_not_raised():
    fake = _FakeRegistry(
        {"aaa": "/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1/cmake"}, fail_remove=True
    )

    outcome = reg.prune_tan_tmp_registrations(fake, Path("/r"), case_insensitive=False)

    assert outcome.removed == ()
    assert outcome.failed and "access denied" in outcome.failed[0]


def test_a_registry_that_cannot_be_listed_is_reported_not_raised():
    class _Unreadable(_FakeRegistry):
        def list_entries(self):
            raise OSError("no access")

    outcome = reg.prune_tan_tmp_registrations(_Unreadable({}), Path("/r"), case_insensitive=False)

    assert outcome.removed == () and "no access" in outcome.failed[0]


# ---------------------------------------------------------------------------
# The POSIX backend and the phase wiring
# ---------------------------------------------------------------------------


def _write_entry(registry: Path, name: str, data: str) -> None:
    registry.mkdir(parents=True, exist_ok=True)
    (registry / name).write_text(data, encoding="utf-8")


def test_a_dead_entry_from_an_earlier_run_is_pruned_even_when_nothing_is_installed(
    tmp_path, monkeypatch
):
    """The #1552 host: an already-stamped store, plus the dead entry the run
    that installed it left behind. The skip path must still clean it."""
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(
        bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index())
    )
    ws = _workspace(tmp_path)
    bootstrap_cmd.toolchain_phase(
        ws, bootstrap_cmd.Log(json_mode=True), bootstrap_cmd.Runner(json=True), sdk_root, None,
        is_windows=False,
    )
    registry = _posix_registry(tmp_path)
    dead = str(_root(tmp_path) / f"{LEAF}.tmp-9576" / "cmake")
    users = str(tmp_path / "zephyr-sdk-1.0.1" / "cmake")
    _write_entry(registry, "53861e69", dead)
    _write_entry(registry, "e2605f1c", users)

    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )

    assert log.blocking() == []
    assert sorted(p.name for p in registry.iterdir()) == ["e2605f1c"]
    assert (registry / "e2605f1c").read_text(encoding="utf-8") == users


def test_an_entry_the_install_itself_writes_is_pruned_once_it_returns(tmp_path, monkeypatch):
    """What HKCU gets on Windows: a write the scratch `HOME` cannot redirect.
    Simulated by a fake west writing into the REAL (test) home's registry."""
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    inner = _fake_west_sdk_install_writes(_install_dir_index())
    registry = _posix_registry(tmp_path)

    def registering_west(self, argv, cwd=None, extra_env=None, tail_lines=4, *, isolated=False):
        if "--install-dir" in argv:
            _write_entry(registry, "deadbeef", str(Path(argv[_install_dir_index()]) / "cmake"))
        return inner(self, argv, cwd=cwd, extra_env=extra_env, tail_lines=tail_lines)

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", registering_west)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True), sdk_root, None,
        is_windows=False,
    )

    assert log.blocking() == []
    assert list(registry.iterdir()) == []


def test_a_dry_run_prunes_nothing(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    registry = _posix_registry(tmp_path)
    _write_entry(registry, "53861e69", str(_root(tmp_path) / f"{LEAF}.tmp-1" / "cmake"))

    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), bootstrap_cmd.Log(json_mode=True),
        bootstrap_cmd.Runner(json=True, dry_run=True), sdk_root, None, is_windows=False,
    )

    assert [p.name for p in registry.iterdir()] == ["53861e69"]


def test_a_missing_posix_registry_is_an_empty_one(tmp_path):
    backend = reg.PosixRegistry(tmp_path / "no" / "such" / "dir")

    assert backend.list_entries() == []


# ---------------------------------------------------------------------------
# The real winreg backend, on Windows only, against a throwaway key
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU exists only on Windows")
def test_the_windows_backend_prunes_a_real_hkcu_value(tmp_path):
    import winreg

    parent = f"Software\\TanTest\\{uuid.uuid4().hex}"
    subkey = f"{parent}\\Zephyr-sdk"
    root = tmp_path / "toolchains"
    ours = str(root / f"{LEAF}.tmp-42" / "cmake")
    theirs = str(tmp_path / "zephyr-sdk-1.0.1" / "cmake")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
        winreg.SetValueEx(key, "a" * 32, 0, winreg.REG_SZ, ours)
        winreg.SetValueEx(key, "b" * 32, 0, winreg.REG_SZ, theirs)
    try:
        backend = reg.WindowsRegistry(subkey)

        outcome = reg.prune_tan_tmp_registrations(backend, root, case_insensitive=True)

        assert outcome.removed == (ours,)
        assert backend.list_entries() == [("b" * 32, theirs)]
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU exists only on Windows")
def test_a_missing_hkcu_key_is_an_empty_registry():
    backend = reg.WindowsRegistry(f"Software\\TanTest\\{uuid.uuid4().hex}\\Zephyr-sdk")

    assert backend.list_entries() == []
