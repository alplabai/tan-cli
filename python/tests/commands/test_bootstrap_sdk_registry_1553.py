# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1553: `west sdk install`'s `setup.sh -c` / `setup.cmd -c` registers
the `.tmp-*` directory it installed to in CMake's user package registry --
`~/.cmake/packages/Zephyr-sdk/<md5>` on POSIX, the
`HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\Zephyr-sdk` value
`<md5>` on Windows -- and tan then moves that directory into its store, so the
entry is dead. #1552's scratch `HOME` stops new POSIX entries; nothing
redirects HKCU, and entries from runs before #1552 are still on disk.

tan now prunes a registration only when its path is exactly one of ITS OWN
`<root>/zephyr-sdk-*.tmp-*/cmake` directories and its name is the MD5 of that
path -- the two facts `zephyr_sdk_export.cmake` leaves -- and nothing else.

The real `winreg` backend is exercised on `windows-latest` against a
throwaway key; everywhere else the POSIX backend runs against `tmp_path`.
"""
from __future__ import annotations

import hashlib
import os
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

#: Captured before the suite-wide autouse stub (tests/conftest.py) replaces it.
_REAL_HOST_REGISTRY = reg.host_registry


def _md5(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


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
    monkeypatch.setattr(reg, "host_registry", _REAL_HOST_REGISTRY)
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
        # `.tmp-` with nothing after it, or not after a `zephyr-sdk-` head.
        ("/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-/cmake", False),
        ("/r/mine.tmp-1/cmake", False),
        ("/r/backup-zephyr-sdk-1.0.1.tmp-1/cmake", False),
        ("", False),
    ],
)
def test_only_tans_own_tmp_dirs_count_as_its_registrations(data, expected):
    assert core_reg.is_tan_tmp_registration(
        _md5(data), data, "/r", case_insensitive=False
    ) is expected


def test_an_entry_whose_name_is_not_the_md5_of_its_data_is_never_tans():
    """`zephyr_sdk_export.cmake` names every entry MD5(path); anything else was
    not written by it, whatever its path says."""
    data = "/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1/cmake"

    assert not core_reg.is_tan_tmp_registration("0" * 32, data, "/r", case_insensitive=False)


def test_a_root_behind_a_symlink_matches_the_resolved_path_cmake_may_record(tmp_path):
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a directory symlink")
    data = str(Path(os.path.realpath(real)) / f"{LEAF}.tmp-3" / "cmake")

    assert core_reg.is_tan_tmp_registration(_md5(data), data, str(link), case_insensitive=False)


def test_a_windows_registration_matches_regardless_of_case_and_separator():
    data = "c:/tools/.alp/TOOLCHAINS/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-7/cmake"

    root = "C:\\tools\\.alp\\toolchains"

    assert core_reg.is_tan_tmp_registration(_md5(data).upper(), data, root, case_insensitive=True)
    assert not core_reg.is_tan_tmp_registration(_md5(data), data, root, case_insensitive=False)


def test_another_pins_tmp_dir_is_also_tans_own():
    """A `.tmp-*` left by an earlier pin is still a directory tan made under its
    root, and its registration is just as dead."""
    data = "/r/zephyr-sdk-0.17.4-arm-zephyr-eabi.tmp-5/cmake"

    assert core_reg.is_tan_tmp_registration(_md5(data), data, "/r", case_insensitive=False)


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
    fake = _FakeRegistry({_md5(ours): ours, _md5(theirs): theirs})

    outcome = reg.prune_tan_tmp_registrations(
        fake, Path("C:/tools/.alp/toolchains"), case_insensitive=True
    )

    assert fake.entries == {_md5(theirs): theirs}
    assert outcome.removed == (ours,)
    assert outcome.failed == ()


def test_a_removal_that_fails_is_reported_not_raised():
    ours = "/r/zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-1/cmake"
    fake = _FakeRegistry({_md5(ours): ours}, fail_remove=True)

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
    _write_entry(registry, _md5(dead), dead)
    _write_entry(registry, _md5(users), users)

    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )

    assert log.blocking() == []
    assert sorted(p.name for p in registry.iterdir()) == [_md5(users)]
    assert (registry / _md5(users)).read_text(encoding="utf-8") == users


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
            data = str(Path(argv[_install_dir_index()]) / "cmake")
            _write_entry(registry, _md5(data), data)
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
    data = str(_root(tmp_path) / f"{LEAF}.tmp-1" / "cmake")
    _write_entry(registry, _md5(data), data)

    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), bootstrap_cmd.Log(json_mode=True),
        bootstrap_cmd.Runner(json=True, dry_run=True), sdk_root, None, is_windows=False,
    )

    assert [p.name for p in registry.iterdir()] == [_md5(data)]


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
        winreg.SetValueEx(key, _md5(ours), 0, winreg.REG_SZ, ours)
        winreg.SetValueEx(key, _md5(theirs), 0, winreg.REG_SZ, theirs)
    try:
        backend = reg.WindowsRegistry(subkey)

        outcome = reg.prune_tan_tmp_registrations(backend, root, case_insensitive=True)

        assert outcome.removed == (ours,)
        assert backend.list_entries() == [(_md5(theirs), theirs)]
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)


@pytest.mark.skipif(sys.platform != "win32", reason="HKCU exists only on Windows")
def test_a_missing_hkcu_key_is_an_empty_registry():
    backend = reg.WindowsRegistry(f"Software\\TanTest\\{uuid.uuid4().hex}\\Zephyr-sdk")

    assert backend.list_entries() == []
