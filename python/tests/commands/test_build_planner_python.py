# SPDX-License-Identifier: Apache-2.0
"""`_planner_python` -- the west-capable workspace venv preference.

Pins the documented-install-path bug: `${PYTHON}` token substitution used to
always return a bare PATH name (`python3` off Windows, `python` on it), so a
host whose PATH `python3` lacked the `west` module got the SDK planner's own
`ModuleNotFoundError: No module named 'west'` surfaced through CMake
configure (`-DPython3_EXECUTABLE=python3`, self-contradicting the correctly
resolved `-DWEST_PYTHON=`) instead of a working build. Mirrors Rust's own
`venv.rs` unit tests (`venv_python_finds_python_next_to_a_west_capable_venv` /
`venv_python_is_none_when_no_west_capable_venv_resolves`), which use the same
scratch-tmp-dir + `.venv/<bin>/<exe>` shape.
"""
import os
from pathlib import Path

from tan.commands import build_cmd
from tan.commands.build_cmd import _planner_python

_BARE_NAME = "python" if os.name == "nt" else "python3"


def _venv_parts() -> tuple[str, str, str]:
    """`(bin_dir, west_exe, python_exe)` for this host."""
    if os.name == "nt":
        return "Scripts", "west.exe", "python.exe"
    return "bin", "west", "python"


def _make_venv(root: Path) -> Path:
    bin_dir, west_exe, python_exe = _venv_parts()
    venv_bin = root / ".venv" / bin_dir
    venv_bin.mkdir(parents=True)
    (venv_bin / west_exe).write_text("", encoding="utf-8")
    python = venv_bin / python_exe
    python.write_text("", encoding="utf-8")
    return python


def test_prefers_the_west_capable_venv_python_over_the_bare_path_name(tmp_path, monkeypatch):
    """A west-capable `.venv` under `start` wins over the bare PATH name --
    the one `_planner_python` always returned before this fix, and the one a
    PATH `python3` lacking `west` would resolve to. The result is an ABSOLUTE
    path, so it wins regardless of what a decoy on PATH is named."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    venv_python = _make_venv(project)

    resolved = _planner_python(str(project), None)

    assert resolved == str(venv_python).replace("\\", "/")
    assert resolved != _BARE_NAME
    assert "\\" not in resolved


def test_searches_upward_from_a_nested_start_dir(tmp_path, monkeypatch):
    """`start` is the build root -- typically nested under the project tree
    the `.venv` sits at the top of -- so the search must walk UP, matching
    Rust's `find_workspace_venv`."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    venv_python = _make_venv(root)
    nested_build_root = root / "app" / "build"
    nested_build_root.mkdir(parents=True)

    resolved = _planner_python(str(nested_build_root), None)

    assert resolved == str(venv_python).replace("\\", "/")


def test_falls_back_to_the_bare_path_name_when_no_venv_resolves(tmp_path, monkeypatch):
    """No `.venv` anywhere reachable -- the pre-existing fallback survives
    untouched (CI, an activated venv, the contract harness).

    `_find_workspace_venv` is pinned to `None` rather than relying on
    `tmp_path`'s ancestors being `.venv`-free: the upward walk climbs to the
    filesystem root, and a developer machine with a `.venv` anywhere above
    the OS temp dir (e.g. `~/.venv`) would red this test for reasons
    unrelated to the code under test.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(build_cmd, "_find_workspace_venv", lambda *_args: None)
    empty = tmp_path / "no-venv-here"
    empty.mkdir()

    assert _planner_python(str(empty), None) == _BARE_NAME


def test_falls_back_when_the_venv_has_west_but_no_python(tmp_path, monkeypatch):
    """A west-capable venv resolves (the west-gate passes) but has no
    `python` binary -- must return the bare fallback name, not a path to a
    file that doesn't exist (else the planner spawn would fail outright)."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    bin_dir, west_exe, _python_exe = _venv_parts()
    project = tmp_path / "project"
    venv_bin = project / ".venv" / bin_dir
    venv_bin.mkdir(parents=True)
    (venv_bin / west_exe).write_text("", encoding="utf-8")

    assert _planner_python(str(project), None) == _BARE_NAME
