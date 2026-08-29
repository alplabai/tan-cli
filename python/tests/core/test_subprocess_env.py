# SPDX-License-Identifier: Apache-2.0
"""tan-cli#992: the one shared LD_LIBRARY_PATH-restore rule every spawn site
routes through -- see `tan.core.subprocess_env`'s module docstring for the
mechanism. These are the same three cases `bootstrap_cmd`'s own tests proved
for `Runner._env` (tan-cli#990), now proved once against the lifted primitive
so every OTHER caller inherits the same guarantee."""
from __future__ import annotations

from tan.core.subprocess_env import restore_ld_library_path, spawn_env


def test_restore_ld_library_path_sets_the_preserved_value(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/runner/.local/bin/tan-cli-lib/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")

    env = {"LD_LIBRARY_PATH": "/home/runner/.local/bin/tan-cli-lib/_internal"}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"


def test_restore_ld_library_path_drops_the_key_when_orig_was_empty(monkeypatch):
    """PyInstaller sets `LD_LIBRARY_PATH_ORIG` to the EMPTY string, not
    unset, when the host had no `LD_LIBRARY_PATH` before the bootloader
    touched it -- the restore must drop the var entirely, not set it to `""`
    (some loaders treat an explicit empty value as "search the current
    directory")."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/runner/.local/bin/tan-cli-lib/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "")

    env = {"LD_LIBRARY_PATH": "/home/runner/.local/bin/tan-cli-lib/_internal"}
    restore_ld_library_path(env)

    assert "LD_LIBRARY_PATH" not in env


def test_restore_ld_library_path_is_a_noop_without_orig(monkeypatch):
    """Absent `LD_LIBRARY_PATH_ORIG` -- every dev/CI/test run of `python -m
    tan` from source, and every macOS/Windows host -- must leave `env`
    completely untouched, not invent a value."""
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    env = {"LD_LIBRARY_PATH": "/whatever/was/already/there"}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == "/whatever/was/already/there"


def test_spawn_env_defaults_to_a_restored_copy_of_os_environ(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/runner/.local/bin/tan-cli-lib/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")
    monkeypatch.setenv("A_MARKER_VAR", "still-here")

    env = spawn_env()

    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert env["A_MARKER_VAR"] == "still-here"


def test_spawn_env_never_returns_none(monkeypatch):
    """`subprocess.run(env=None)` means "inherit the unrestored process
    environment" -- exactly the leak this module exists to close. Every
    caller must get an explicit dict back, even on the ordinary
    unfrozen/no-override fast case."""
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    env = spawn_env()

    assert isinstance(env, dict)


def test_spawn_env_layers_overrides_on_top_of_the_restore(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/runner/.local/bin/tan-cli-lib/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")

    env = spawn_env({"PRODUCT": "v2n"})

    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert env["PRODUCT"] == "v2n"


def test_spawn_env_copies_a_given_base_rather_than_mutating_it(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/runner/.local/bin/tan-cli-lib/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")

    base = {"PATH": "/venv/bin:/usr/bin", "LD_LIBRARY_PATH": "/home/runner/.local/bin/tan-cli-lib/_internal"}
    env = spawn_env(base=base)

    assert env["PATH"] == "/venv/bin:/usr/bin"
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    # the caller's own dict is untouched
    assert base["LD_LIBRARY_PATH"] == "/home/runner/.local/bin/tan-cli-lib/_internal"
