# SPDX-License-Identifier: Apache-2.0
"""`tan.core.venv` -- the workspace-venv resolution family (tan-cli#289),
covering the parts of the module `test_build_planner_python.py` (via
`_planner_python`/`venv_python`) and `test_execute.py`/`test_flash_command.py`
(via `west_program`/`tool_in_venv` end-to-end) do not already exercise
directly: `tool_in_venv`'s own file-existence + `.exe` behaviour, and
`with_venv_on_path`'s bare-tool no-op. Mirrors the shape of Rust's own
`venv.rs` unit tests.
"""
import os
from pathlib import Path

from tan.core.venv import tool_in_venv, west_program, with_venv_on_path


def _venv_parts() -> tuple[str, str]:
    return ("Scripts", "west.exe") if os.name == "nt" else ("bin", "west")


def test_tool_in_venv_resolves_only_files_that_exist(tmp_path):
    bin_dir, west_exe = _venv_parts()
    venv_bin = tmp_path / bin_dir
    venv_bin.mkdir()
    (venv_bin / west_exe).write_text("", encoding="utf-8")

    resolved = tool_in_venv(venv_bin, "west")
    assert resolved == str(venv_bin / west_exe)
    assert tool_in_venv(venv_bin, "openocd") is None


def test_tool_in_venv_appends_exe_only_on_windows_and_only_once(tmp_path):
    """A caller that already spelled `.exe` must not become `west.exe.exe`."""
    bin_dir, west_exe = _venv_parts()
    venv_bin = tmp_path / bin_dir
    venv_bin.mkdir()
    (venv_bin / west_exe).write_text("", encoding="utf-8")

    assert tool_in_venv(venv_bin, "west") == str(venv_bin / west_exe)
    if os.name == "nt":
        assert tool_in_venv(venv_bin, "west.exe") == str(venv_bin / west_exe)


def test_west_program_falls_back_to_the_bare_path_name(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    empty = tmp_path / "no-venv-here"
    empty.mkdir()
    assert west_program(str(empty), None) == "west"


def test_with_venv_on_path_prepends_onto_an_already_staged_path():
    venv_west = str(Path("/ws/.venv/bin/west")) if os.name != "nt" else r"C:\ws\.venv\Scripts\west.exe"
    env = with_venv_on_path({"PATH": "/plan/toolchain/bin"}, venv_west)
    venv_dir = str(Path(venv_west).parent)
    assert env["PATH"].startswith(venv_dir)
    assert "/plan/toolchain/bin" in env["PATH"]


def test_with_venv_on_path_leaves_a_bare_tool_name_untouched():
    env = with_venv_on_path({"PATH": "/plan/toolchain/bin"}, "west")
    assert env == {"PATH": "/plan/toolchain/bin"}


def test_with_venv_on_path_does_not_mutate_the_caller_s_dict():
    original = {"PATH": "/plan/toolchain/bin"}
    venv_west = r"C:\ws\.venv\Scripts\west.exe" if os.name == "nt" else "/ws/.venv/bin/west"
    with_venv_on_path(original, venv_west)
    assert original == {"PATH": "/plan/toolchain/bin"}
