# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1483: `core.longpaths` is read from the system/global scopes only,
never from the local config of whatever repository tan's cwd sits in -- fresh
`west update` clones do not inherit a project repo's local setting."""
import shutil
import subprocess

import pytest

from tan.core.doctor_git import _git_core_longpaths

git = shutil.which("git")
pytestmark = pytest.mark.skipif(git is None, reason="needs git")


def _isolate_git(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))


def test_a_local_only_setting_does_not_give_a_false_pass(tmp_path, monkeypatch):
    _isolate_git(monkeypatch, tmp_path)
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    subprocess.run([git, "-C", str(repo), "config", "core.longpaths", "true"], check=True)
    monkeypatch.chdir(repo)
    assert _git_core_longpaths(git) is not True


def test_a_global_setting_is_still_seen(tmp_path, monkeypatch):
    _isolate_git(monkeypatch, tmp_path)
    subprocess.run([git, "config", "--global", "core.longpaths", "true"], check=True)
    monkeypatch.chdir(tmp_path)
    assert _git_core_longpaths(git) is True
