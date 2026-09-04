# SPDX-License-Identifier: Apache-2.0
"""tan-cli#992: the one shared LD_LIBRARY_PATH-restore rule every spawn site
routes through -- see `tan.core.subprocess_env`'s module docstring for the
mechanism. These are the same three cases `bootstrap_cmd`'s own tests proved
for `Runner._env` (tan-cli#990), now proved once against the lifted primitive
so every OTHER caller inherits the same guarantee."""
from __future__ import annotations

import os
import sys

from tan.core.subprocess_env import (
    ld_library_path_needs_restore,
    restore_ld_library_path,
    spawn_env,
)


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


def test_restore_ld_library_path_is_a_noop_without_orig_when_not_frozen(monkeypatch):
    """Absent `LD_LIBRARY_PATH_ORIG` on an UNFROZEN run -- every dev/CI/test
    run of `python -m tan` from source, and every macOS/Windows host -- must
    leave `env` completely untouched, not invent a value.

    Renamed for tan-cli#1189: the old name said "without orig" full stop,
    which read as a guarantee about EVERY no-`ORIG` host. It was only ever
    true of the unfrozen ones, and the frozen no-`ORIG` host is the leak this
    module now also has to close (see the pair of tests below)."""
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    env = {"LD_LIBRARY_PATH": "/whatever/was/already/there"}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == "/whatever/was/already/there"


#: The bundle dir a PyInstaller ONEDIR freeze reports as `sys._MEIPASS`,
#: spelled the way the tan-cli#1189 CI failure spelled it verbatim.
_BUNDLE = "/work/proj/tan-cli-lib/_internal"


def _sep(*entries: str) -> str:
    """Join search-path entries with `os.pathsep`, NOT a hardcoded `":"`.

    The first cut of these tests hardcoded `:` and went green on macOS while
    every one of them failed on `windows-latest` (`python -- pytest shard
    (windows-latest 2/4)`), because `os.pathsep` is `";"` there: the whole
    string parsed as ONE entry, the bundle never matched, and the assertions
    that the bundle gets stripped could not hold. The module under test was
    right all along -- it has always split on `os.pathsep` -- so the fixture,
    not the code, is what had to become platform-correct."""
    return os.pathsep.join(entries)


def _frozen_onedir(monkeypatch, ld_library_path):
    """A frozen ONEDIR process on a host that had NO `LD_LIBRARY_PATH` of its
    own, so the bootloader recorded no `LD_LIBRARY_PATH_ORIG` to undo."""
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", _BUNDLE, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", ld_library_path)


def test_restore_drops_the_bundle_when_frozen_with_no_orig(monkeypatch):
    """tan-cli#1189, the shape no existing case could see. Every other test
    in this file hands the restore an `ORIG` to work from; this is the host
    where there is none AND an override happened anyway -- a bare
    `ubuntu:24.04`, where the bundle is the ONLY entry on the path.

    Popping (not setting `""`) is the same rule the `orig == ""` branch
    follows: an explicit empty value is read by some loaders as "search the
    current directory"."""
    _frozen_onedir(monkeypatch, _BUNDLE)

    env = {"LD_LIBRARY_PATH": _BUNDLE}
    restore_ld_library_path(env)

    assert "LD_LIBRARY_PATH" not in env


def test_restore_keeps_the_host_entries_around_the_bundle(monkeypatch):
    """The bundle is removed; everything the host legitimately had is kept,
    in order. Removal must not become "wipe the search path"."""
    _frozen_onedir(monkeypatch, _sep(_BUNDLE, "/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib"))

    env = {"LD_LIBRARY_PATH": _sep(_BUNDLE, "/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib")}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == _sep("/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib")


def test_restore_matches_the_bundle_through_a_trailing_slash(monkeypatch):
    """A trailing slash is the same directory. Matching the raw string would
    let the exact leak this fixes walk straight past the check."""
    _frozen_onedir(monkeypatch, _sep(f"{_BUNDLE}/", "/usr/lib/x86_64-linux-gnu"))

    env = {"LD_LIBRARY_PATH": _sep(f"{_BUNDLE}/", "/usr/lib/x86_64-linux-gnu")}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"


def test_restore_leaves_a_frozen_run_whose_path_lacks_the_bundle_alone(monkeypatch):
    """Anti-overreach: frozen, no `ORIG`, but the bundle is NOT on the path,
    so nothing was overridden and nothing may be stripped. Without this the
    fallback would start editing a search path the bootloader never touched."""
    _frozen_onedir(monkeypatch, _sep("/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib"))

    env = {"LD_LIBRARY_PATH": _sep("/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib")}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == _sep("/usr/lib/x86_64-linux-gnu", "/opt/vendor/lib")


def test_orig_still_wins_over_the_bundle_fallback(monkeypatch):
    """When the bootloader DID record a marker, that exact value is restored
    -- the fallback must not preempt it or second-guess it."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", _BUNDLE, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", _sep(_BUNDLE, "/usr/lib/x86_64-linux-gnu"))
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")

    env = {"LD_LIBRARY_PATH": _sep(_BUNDLE, "/usr/lib/x86_64-linux-gnu")}
    restore_ld_library_path(env)

    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"


def test_needs_restore_is_false_on_an_unfrozen_host(monkeypatch):
    """`Runner._env`'s fast path (`env=None`, inherit unrestored) is only
    safe when this is False. It must stay False for source runs, or every
    spawn in the repo's own test suite starts copying `os.environ`."""
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/lib/x86_64-linux-gnu")

    assert ld_library_path_needs_restore() is False


def test_needs_restore_is_true_for_the_frozen_no_orig_host(monkeypatch):
    """The whole point of tan-cli#1189: on this host the old guard answered
    "nothing to do" and handed the child `env=None`."""
    _frozen_onedir(monkeypatch, _BUNDLE)

    assert ld_library_path_needs_restore() is True


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
