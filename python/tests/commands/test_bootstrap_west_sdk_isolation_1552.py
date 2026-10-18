# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1552: `west sdk install` honours `--install-dir` only when no SDK of
the pinned version is already discoverable, and its `setup.sh -c` registers
the `.tmp-*` path it installed to in the CALLER's CMake package registry.

The fix runs that one child under a scratch `HOME` (hiding `~/.cmake/packages`
and `FindZephyr-sdk.cmake`'s `$HOME` prefix search, and catching the POSIX
registry write), with the SDK location variables blanked and SDK roots dropped
from `PATH` / `CMAKE_PREFIX_PATH`; a reuse that still happens is reported as
what it is instead of as a version mismatch whose remedy loops forever.

Hermetic: `Runner.run` is faked exactly as in test_bootstrap_toolchain_phase.py,
and `PATH` / `CMAKE_PREFIX_PATH` point only into `tmp_path`.
"""
from __future__ import annotations

import os
from pathlib import Path

from tan.commands import bootstrap_cmd
from tan.commands import bootstrap_west_sdk_isolation as iso
from tan.core import toolchain_provision as tp
from tan.core import west_sdk_isolation as core_iso
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


def _make_sdk_root(directory: Path, version: str) -> Path:
    (directory / "cmake").mkdir(parents=True)
    (directory / "cmake" / "Zephyr-sdkConfig.cmake").write_text("", encoding="utf-8")
    (directory / "sdk_version").write_text(f"{version}\n", encoding="utf-8")
    return directory


def _linux(monkeypatch, tmp_path: Path) -> None:
    path = [str(tmp_path / "usr" / "bin"), str(tmp_path / "bin")]
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    monkeypatch.delenv("CMAKE_PREFIX_PATH", raising=False)
    monkeypatch.setattr(iso, "SYSTEM_SDK_PREFIXES", ())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    for var in tp.SDK_TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(tp.NETRC_ENV_VAR, raising=False)


def _recording_install(seen: dict, *, writes: bool = True, register: bool = False):
    """The usual fake west, plus: record the env it was handed and, with
    `register`, do what `setup.sh -c` does on POSIX -- write a registry entry
    for the install dir under `$HOME/.cmake/packages/Zephyr-sdk/`."""
    inner = _fake_west_sdk_install_writes(_install_dir_index())

    def fake_run(self, argv, cwd=None, extra_env=None, tail_lines=4, *, isolated=False):
        if "install" in argv and "sdk" in argv:
            env = dict(extra_env or {})
            seen.setdefault("extra_env", []).append(env)
            if register:
                registry = Path(env["HOME"]) / ".cmake" / "packages" / "Zephyr-sdk"
                registry.mkdir(parents=True, exist_ok=True)
                (registry / "deadbeef").write_text(
                    str(Path(argv[_install_dir_index()]) / "cmake"), encoding="utf-8"
                )
        if not writes:
            return None
        return inner(self, argv, cwd=cwd, extra_env=extra_env, tail_lines=tail_lines)

    return fake_run


def _run_phase(tmp_path, monkeypatch, fake_run) -> bootstrap_cmd.Log:
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_run)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    return log


def _toolchain_root(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".alp" / "toolchains"


# ---------------------------------------------------------------------------
# Isolation: the child never sees, and never writes, the user's discovery state
# ---------------------------------------------------------------------------


def test_west_sdk_install_runs_under_a_scratch_home_with_sdk_location_vars_blanked(
    tmp_path, monkeypatch
):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    for var in core_iso.SDK_LOCATION_ENV_VARS:
        monkeypatch.setenv(var, str(tmp_path / "home" / "zephyr-sdk-1.0.1"))
    seen: dict = {}

    log = _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    assert log.blocking() == []
    env = seen["extra_env"][0]
    scratch = Path(env["HOME"])
    assert scratch != tmp_path / "home"
    assert scratch.parent == _toolchain_root(tmp_path)
    # The `.tmp-*` naming is what lets the next run's wreckage sweep reclaim a
    # scratch home a SIGKILL left behind.
    assert scratch.name.startswith(f"{tp.store_dir_name('1.0.1')}{tp.TMP_SUFFIX_PREFIX}")
    assert {var: env[var] for var in core_iso.SDK_LOCATION_ENV_VARS} == dict.fromkeys(
        core_iso.SDK_LOCATION_ENV_VARS, ""
    )


def test_the_scratch_home_is_deleted_once_the_install_returns(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    seen: dict = {}

    _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    assert not Path(seen["extra_env"][0]["HOME"]).exists()


def test_the_registry_entry_setup_sh_writes_never_reaches_the_users_home(tmp_path, monkeypatch):
    """Defect 2 of #1552 (POSIX): the entry pointed at the `.tmp-*` dir tan then
    moved, and it replaced whatever the user had registered for that version."""
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    users_registry = tmp_path / "home" / ".cmake" / "packages" / "Zephyr-sdk"
    users_registry.mkdir(parents=True)
    (users_registry / "e2605f1c").write_text("/home/u/zephyr-sdk-1.0.1/cmake", encoding="utf-8")
    seen: dict = {}

    log = _run_phase(tmp_path, monkeypatch, _recording_install(seen, register=True))

    assert log.blocking() == []
    assert [p.name for p in users_registry.iterdir()] == ["e2605f1c"]
    assert (users_registry / "e2605f1c").read_text(encoding="utf-8") == (
        "/home/u/zephyr-sdk-1.0.1/cmake"
    )


def test_a_dry_run_creates_no_scratch_home(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True, dry_run=True), sdk_root, None,
        is_windows=False,
    )

    assert not _toolchain_root(tmp_path).exists()


# ---------------------------------------------------------------------------
# netrc: moving HOME takes no credential away, and tan's staged one still wins
# ---------------------------------------------------------------------------


def test_the_users_own_netrc_still_reaches_an_unauthenticated_install(tmp_path, monkeypatch):
    """`requests` falls back to `~/.netrc`, and `~` is now the scratch home --
    point `$NETRC` at the real one so moving `HOME` takes nothing away."""
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    users_netrc = tmp_path / "home" / ".netrc"
    users_netrc.write_text("machine example.invalid login x password y\n", encoding="utf-8")
    seen: dict = {}

    _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    assert seen["extra_env"][0][tp.NETRC_ENV_VAR] == str(users_netrc)


def test_an_underscore_netrc_is_forwarded_when_there_is_no_dot_netrc(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    users_netrc = tmp_path / "home" / "_netrc"
    users_netrc.write_text("machine example.invalid login x password y\n", encoding="utf-8")

    assert iso.isolated_env(tmp_path / "scratch")[tp.NETRC_ENV_VAR] == str(users_netrc)


def test_an_inherited_netrc_variable_is_left_to_pass_through(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    (tmp_path / "home" / ".netrc").write_text("machine a login b password c\n", encoding="utf-8")
    monkeypatch.setenv(tp.NETRC_ENV_VAR, str(tmp_path / "elsewhere" / "netrc"))

    assert tp.NETRC_ENV_VAR not in iso.isolated_env(tmp_path / "scratch")


def test_a_staged_token_wins_over_the_users_netrc(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    users_netrc = tmp_path / "home" / ".netrc"
    users_netrc.write_text("machine example.invalid login x password y\n", encoding="utf-8")
    monkeypatch.setenv("TAN_GITHUB_TOKEN", "ghp_" + "x" * 36)
    seen: dict = {}

    _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    netrc = seen["extra_env"][0][tp.NETRC_ENV_VAR]
    assert netrc != str(users_netrc)
    assert Path(netrc).parent.parent == _toolchain_root(tmp_path)


# ---------------------------------------------------------------------------
# PATH / CMAKE_PREFIX_PATH: CMake turns `<sdk>/bin` on PATH into the prefix `<sdk>`
# ---------------------------------------------------------------------------


def test_path_prefix_drops_one_trailing_bin_or_sbin_component():
    assert core_iso.path_prefix_of("/home/u/zephyr-sdk-1.0.1/bin") == "/home/u/zephyr-sdk-1.0.1"
    assert core_iso.path_prefix_of("/home/u/zephyr-sdk-1.0.1/bin/") == "/home/u/zephyr-sdk-1.0.1"
    assert core_iso.path_prefix_of("/usr/sbin") == "/usr"
    assert core_iso.path_prefix_of("C:\\sdk\\Bin") == "C:\\sdk"
    assert core_iso.path_prefix_of("C:\\bin") == "C:\\"
    assert core_iso.path_prefix_of("/opt/tools") == "/opt/tools"
    assert core_iso.path_prefix_of("/bin") == "/bin"


def test_only_entries_inside_an_sdk_root_are_dropped_and_order_is_kept():
    roots = {"/home/u/zephyr-sdk-1.0.1"}
    path = ":".join(["/usr/local/bin", "/home/u/zephyr-sdk-1.0.1/bin", "", "/usr/bin"])

    filtered = core_iso.without_sdk_roots(
        path, sep=":", is_sdk_root=roots.__contains__, strip_bin=True
    )

    assert filtered == ":".join(["/usr/local/bin", "", "/usr/bin"])


def test_a_cmake_prefix_path_entry_is_a_prefix_as_it_stands():
    roots = {"/home/u/zephyr-sdk-1.0.1"}
    value = ":".join(["/home/u/zephyr-sdk-1.0.1", "/home/u/zephyr-sdk-1.0.1/bin", "/usr"])

    filtered = core_iso.without_sdk_roots(
        value, sep=":", is_sdk_root=roots.__contains__, strip_bin=False
    )

    assert filtered == ":".join(["/home/u/zephyr-sdk-1.0.1/bin", "/usr"])


def test_sdk_roots_on_path_and_cmake_prefix_path_are_hidden_from_the_west_child(
    tmp_path, monkeypatch
):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    sdk = _make_sdk_root(tmp_path / "home" / "zephyr-sdk-1.0.1", "1.0.1")
    keep = str(tmp_path / "usr" / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([keep, str(sdk / "bin")]))
    monkeypatch.setenv("CMAKE_PREFIX_PATH", os.pathsep.join([str(sdk), keep]))
    seen: dict = {}

    _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    env = seen["extra_env"][0]
    assert env["PATH"] == keep
    assert env["CMAKE_PREFIX_PATH"] == keep


def test_lists_with_no_sdk_entry_are_inherited_untouched(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    seen: dict = {}

    _run_phase(tmp_path, monkeypatch, _recording_install(seen))

    assert "PATH" not in seen["extra_env"][0]
    assert "CMAKE_PREFIX_PATH" not in seen["extra_env"][0]


# ---------------------------------------------------------------------------
# A reuse that isolation cannot prevent is named, not looped on
# ---------------------------------------------------------------------------


def test_a_reused_sdk_is_reported_as_reuse_and_the_toolchain_is_never_downloaded(
    tmp_path, monkeypatch
):
    _point_home_at(monkeypatch, tmp_path)
    _linux(monkeypatch, tmp_path)
    opt_sdk = _make_sdk_root(tmp_path / "opt" / "zephyr-sdk-1.0.1", "1.0.1")
    monkeypatch.setattr(iso, "SYSTEM_SDK_PREFIXES", (str(tmp_path / "opt"),))
    from tan.commands import bootstrap_toolchain_fetch

    def must_not_download(*a, **k):
        raise AssertionError("the toolchain archive was fetched for an install west never made")

    monkeypatch.setattr(bootstrap_toolchain_fetch, "install_pinned_toolchain", must_not_download)
    seen: dict = {}

    log = _run_phase(tmp_path, monkeypatch, _recording_install(seen, writes=False))

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "installed nothing" in msg and "already installed" in msg
    assert str(opt_sdk) in msg
    # The old remedy ("remove that directory and re-run") cannot change the
    # outcome, so it must not be offered.
    assert "Remove that directory" not in msg
    assert "SDK version None" not in msg


def test_a_reuse_with_no_visible_candidate_names_what_tan_cannot_see():
    msg = core_iso.reused_sdk_message("1.0.1", "/root/.alp/toolchains/x.tmp-1", [])

    assert "/usr" in msg and "/opt" in msg
    assert "cmake's own install prefix" in msg
    assert "HKEY_CURRENT_USER" in msg
    assert "west sdk list" not in msg


def test_candidates_are_zephyr_sdk_roots_at_the_pinned_version_directly_under_a_prefix(tmp_path):
    prefix = tmp_path / "opt"
    for name, version in (
        ("zephyr-sdk-1.0.1", "1.0.1"),
        ("zephyr-sdk-0.17.4", "0.17.4"),
        ("other-1.0.1", "1.0.1"),
    ):
        _make_sdk_root(prefix / name, version)
    _make_sdk_root(prefix / "deeper" / "zephyr-sdk-1.0.1", "1.0.1")
    (prefix / "zephyr-sdk-no-config").mkdir()
    (prefix / "zephyr-sdk-no-config" / "sdk_version").write_text("1.0.1\n", encoding="utf-8")

    found = iso.reusable_sdk_candidates(
        "1.0.1", prefixes=(str(prefix), str(tmp_path / "absent"), str(prefix))
    )

    assert found == [str(prefix / "zephyr-sdk-1.0.1")]


def test_a_path_sdk_the_child_never_saw_is_not_named_but_a_sibling_under_a_path_prefix_is(
    tmp_path, monkeypatch
):
    """Review of #1552: naming an SDK whose `bin` was filtered off the child's
    PATH would blame a copy west could not have reused."""
    monkeypatch.setattr(iso, "SYSTEM_SDK_PREFIXES", ())
    monkeypatch.delenv("CMAKE_PREFIX_PATH", raising=False)
    hidden = _make_sdk_root(tmp_path / "a" / "zephyr-sdk-1.0.1", "1.0.1")
    visible = _make_sdk_root(tmp_path / "b" / "zephyr-sdk-1.0.1", "1.0.1")
    (tmp_path / "b" / "bin").mkdir()
    path = [str(hidden / "bin"), str(tmp_path / "b" / "bin")]
    monkeypatch.setenv("PATH", os.pathsep.join(path))

    assert iso.reusable_sdk_candidates("1.0.1") == [str(visible)]
