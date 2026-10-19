# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1552 review follow-ups: the isolation is measured against the REAL
`listsdk.cmake` (skipped when cmake or a Zephyr tree is absent), not only
against the code's own model of CMake's search order."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tan.commands import bootstrap_sdk_isolation as iso

VERSION_CMAKE = """\
file(READ ${CMAKE_CURRENT_LIST_DIR}/../sdk_version SDK_VERSION)
string(STRIP ${SDK_VERSION} PACKAGE_VERSION)
if(PACKAGE_VERSION VERSION_LESS PACKAGE_FIND_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
endif()
"""


def _make_sdk(parent: Path, name: str = "zephyr-sdk-1.0.1", cmake_dir: str = "cmake") -> Path:
    sdk = parent / name
    (sdk / cmake_dir).mkdir(parents=True)
    (sdk / "bin").mkdir()
    (sdk / "sdk_version").write_text("1.0.1\n", encoding="utf-8")
    (sdk / cmake_dir / "Zephyr-sdkConfig.cmake").write_text("", encoding="utf-8")
    (sdk / cmake_dir / "Zephyr-sdkConfigVersion.cmake").write_text(VERSION_CMAKE, encoding="utf-8")
    return sdk


# ---- unit: the layouts CMake's config search accepts ----------------------------


@pytest.mark.parametrize("sdk_name", ["zephyr-sdk-1.0.1", "Zephyr-SDK-1.0.1"])
@pytest.mark.parametrize("cmake_dir", ["cmake", "CMake"])
def test_a_path_bin_whose_prefix_holds_an_sdk_dir_is_dropped(tmp_path, sdk_name, cmake_dir):
    _make_sdk(tmp_path / "prefix", sdk_name, cmake_dir)
    (tmp_path / "prefix" / "bin").mkdir(exist_ok=True)
    keep = tmp_path / "tools" / "bin"
    keep.mkdir(parents=True)
    path = os.pathsep.join([str(tmp_path / "prefix" / "bin"), str(keep)])

    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={"PATH": path})

    assert env["PATH"] == str(keep)


def test_an_uppercase_BIN_is_stripped_like_bin(tmp_path):
    _make_sdk(tmp_path / "prefix")
    (tmp_path / "prefix" / "BIN").mkdir()
    path = str(tmp_path / "prefix" / "BIN")
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={"PATH": path})
    assert env["PATH"] == ""


def test_cmake_prefix_path_naming_the_parent_of_an_sdk_is_dropped(tmp_path):
    _make_sdk(tmp_path / "prefix")
    env = iso.isolated_env(
        tmp_path / "h", None, real_home=None,
        environ={"CMAKE_PREFIX_PATH": str(tmp_path / "prefix")},
    )
    assert env["CMAKE_PREFIX_PATH"] == ""


def test_a_prefix_entry_that_carries_a_needed_tool_is_kept_not_dropped(tmp_path):
    """`~/.local/bin` often holds the user's `west` (pip --user) next to an
    SDK; dropping it would break the very command we are isolating."""
    _make_sdk(tmp_path / "prefix")
    (tmp_path / "prefix" / "bin").mkdir()
    tool = tmp_path / "prefix" / "bin" / "west"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    path = str(tmp_path / "prefix" / "bin")
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={"PATH": path})
    assert "PATH" not in env  # unchanged: the reuse warning is the backstop


def test_an_sdks_own_bin_is_dropped_even_when_it_has_tools(tmp_path):
    sdk = _make_sdk(tmp_path)
    tool = sdk / "bin" / "cmake"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={"PATH": str(sdk / "bin")})
    assert env["PATH"] == ""


def test_relative_entries_are_left_alone(tmp_path):
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={"PATH": "rel/bin"})
    assert "PATH" not in env


def test_scrubbing_still_applies_when_the_scratch_home_could_not_be_made(tmp_path):
    sdk = _make_sdk(tmp_path)
    env = iso.isolated_env(
        None, {"NETRC": "/n"}, real_home=None,
        environ={"ZEPHYR_SDK_INSTALL_DIR": str(sdk), "PATH": str(sdk / "bin")},
    )
    assert env["ZEPHYR_SDK_INSTALL_DIR"] == "" and env["PATH"] == "" and env["NETRC"] == "/n"
    assert "HOME" not in env


def test_a_degraded_scratch_home_says_so(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    said: list[str] = []
    with iso.scratch_home(blocker / "sub", "leaf", 1, enabled=True, on_degraded=said.append) as home:
        assert home is None
    assert len(said) == 1


def test_the_real_west_config_survives_the_scratch_home(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / ".westconfig").write_text("[x]\n", encoding="utf-8")
    env = iso.isolated_env(tmp_path / "h", None, real_home=str(real), environ={})
    assert env["WEST_CONFIG_GLOBAL"] == str(real / ".westconfig")
    assert "PYTHONUSERBASE" not in env


def test_a_given_user_base_is_passed_through_and_none_sets_nothing(tmp_path):
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={}, user_base="/ub")
    assert env["PYTHONUSERBASE"] == "/ub"
    env = iso.isolated_env(tmp_path / "h", None, real_home=None, environ={}, user_base=None)
    assert "PYTHONUSERBASE" not in env


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


posix_only = pytest.mark.skipif(os.name != "posix", reason="shebang launchers")


def test_a_workspace_venv_west_needs_no_user_base_and_spawns_nothing(tmp_path):
    said: list[str] = []
    west = str(tmp_path / ".venv" / "bin" / "west")
    assert iso.user_base_for(west, {}, said.append, enabled=True) is None
    assert said == []


@posix_only
def test_a_bare_path_west_gets_the_user_base_of_its_own_interpreter(tmp_path):
    py = _script(tmp_path / "py", "#!/bin/sh\necho /fake/user/base\n")
    _script(tmp_path / "west", f"#!{py}\n")
    said: list[str] = []
    base = iso.user_base_for("west", {"PATH": str(tmp_path)}, said.append, enabled=True)
    assert base == "/fake/user/base"
    assert said == []


@posix_only
def test_an_env_shebang_is_resolved_through_path(tmp_path):
    _script(tmp_path / "pyx", "#!/bin/sh\necho /via/env\n")
    _script(tmp_path / "west", "#!/usr/bin/env pyx\n")
    assert iso.user_base_for("west", {"PATH": str(tmp_path)}, lambda _m: None, enabled=True) == "/via/env"


def test_an_undeterminable_west_interpreter_sets_nothing_and_says_so(tmp_path):
    said: list[str] = []
    assert iso.user_base_for("west", {"PATH": str(tmp_path)}, said.append, enabled=True) is None
    assert len(said) == 1 and "PYTHONUSERBASE" in said[0]


def test_a_dry_run_resolves_nothing(tmp_path):
    said: list[str] = []
    assert iso.user_base_for("west", {"PATH": str(tmp_path)}, said.append, enabled=False) is None
    assert said == []


def test_the_sdk_pointer_variables_are_blanked_in_the_env(tmp_path):
    env = iso.isolated_env(
        tmp_path / "h", None, real_home=None,
        environ={"ZEPHYR_SDK_INSTALL_DIR": "/a", "Zephyr-sdk_DIR": "/b", "Zephyr-sdk_ROOT": "/c"},
    )
    assert (env["ZEPHYR_SDK_INSTALL_DIR"], env["Zephyr-sdk_DIR"], env["Zephyr-sdk_ROOT"]) == ("", "", "")


def test_the_reuse_warning_names_the_prefix_the_sdk_was_found_under(tmp_path):
    _make_sdk(tmp_path / "elsewhere")
    msg = iso.reuse_warning(
        "1.0.1", tmp_path / "t", None, {"PATH": str(tmp_path / "elsewhere" / "bin")}
    )
    assert f"out of {tmp_path / 'elsewhere'}" in msg


def test_the_reuse_warning_names_an_sdk_found_through_path_and_prefix_path(tmp_path):
    sdk = _make_sdk(tmp_path / "elsewhere")
    msg = iso.reuse_warning(
        "1.0.1", tmp_path / "t", None, {"PATH": str(tmp_path / "elsewhere" / "bin")}
    )
    assert str(sdk) in msg
    msg = iso.reuse_warning(
        "1.0.1", tmp_path / "t", None, {"CMAKE_PREFIX_PATH": str(tmp_path / "elsewhere")}
    )
    assert str(sdk) in msg


# ---- integration: the real listsdk.cmake -----------------------------------------


def _find_zephyr_base() -> Path | None:
    for cand in (os.environ.get("ZEPHYR_BASE"), str(Path.home() / ".alp" / "sdk" / "zephyr")):
        if cand and (Path(cand) / "scripts" / "west_commands" / "sdk" / "listsdk.cmake").is_file():
            return Path(cand)
    return None


#: Resolved at import: an autouse fixture rewrites HOME/ZEPHYR_BASE per test.
ZEPHYR_BASE = _find_zephyr_base()


def _zephyr_base() -> Path | None:
    return ZEPHYR_BASE


def _listsdk(zephyr: Path, env: dict[str, str]) -> str:
    out = subprocess.run(
        [shutil.which("cmake"), "-P", str(zephyr / "scripts/west_commands/sdk/listsdk.cmake")],
        env=env, capture_output=True, text=True, check=False, cwd=str(zephyr),
    )
    return out.stdout + out.stderr


needs_cmake = pytest.mark.skipif(
    shutil.which("cmake") is None or _zephyr_base() is None,
    reason="needs cmake and a Zephyr tree (ZEPHYR_BASE or ~/.alp/sdk/zephyr)",
)


@needs_cmake
@pytest.mark.parametrize(
    "scenario",
    ["home-prefix", "home-bin-on-path", "prefix-path-parent", "install-dir-env", "sdk-bin-on-path"],
)
def test_real_listsdk_does_not_see_the_users_sdk_under_the_isolated_env(tmp_path, scenario):
    zephyr = _zephyr_base()
    real = tmp_path / "realhome"
    sdk = _make_sdk(real)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    base_path = os.path.dirname(shutil.which("cmake"))
    environ = {"PATH": base_path, "HOME": str(real), "ZEPHYR_BASE": str(zephyr)}
    if scenario == "home-bin-on-path":
        (real / "bin").mkdir(exist_ok=True)
        environ["PATH"] = os.pathsep.join([str(real / "bin"), base_path])
    elif scenario == "prefix-path-parent":
        environ["CMAKE_PREFIX_PATH"] = str(real)
        environ["HOME"] = str(tmp_path / "elsewhere")
    elif scenario == "install-dir-env":
        environ["ZEPHYR_SDK_INSTALL_DIR"] = str(sdk)
    elif scenario == "sdk-bin-on-path":
        environ["PATH"] = os.pathsep.join([str(sdk / "bin"), base_path])
    if scenario != "prefix-path-parent":
        environ.setdefault("HOME", str(real))

    # Without isolation the SDK is visible in EVERY scenario (the install-dir one
    # included, via HOME=real) -- so the assertion below is not vacuous. listsdk
    # in LIST mode never reads ZEPHYR_SDK_INSTALL_DIR; west does, and the
    # blanking of that variable is covered by a unit test on the env instead.
    assert "zephyr-sdk-1.0.1" in _listsdk(zephyr, environ)

    overlay = iso.isolated_env(scratch, None, real_home=str(real), environ=environ)
    isolated = {**environ, **overlay}
    assert "zephyr-sdk-1.0.1" not in _listsdk(zephyr, isolated)
