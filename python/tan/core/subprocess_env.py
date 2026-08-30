# SPDX-License-Identifier: Apache-2.0
"""The one shared rule for building a subprocess child's environment.

**Every spawn site under `python/tan/` must route its `env=` through
[`spawn_env`] (or, for a caller that already owns its own mutable base dict,
[`restore_ld_library_path`] directly) rather than build one by hand.**
tan-cli#992 is what happens when that is left to 25 independent call sites
instead: one gets it right and the other 24 leak.

## The rule

PyInstaller's Linux ONEDIR bootloader points `LD_LIBRARY_PATH` at the frozen
app's own bundled `_internal/` lib dir -- so the FROZEN `tan` binary finds its
OWN bundled shared libraries -- and stashes the caller's original value in
`LD_LIBRARY_PATH_ORIG` before doing so. Every child process `tan` spawns
(`west`, `git`, `dxcom`, a J-Link tool, and everything THOSE spawn in turn) is
a SYSTEM program, never a bundled one, so unless the spawn site restores
`LD_LIBRARY_PATH_ORIG` before spawning, that child inherits tan's bundled path
ahead of the system one and can silently resolve one of tan's own bundled
shared libraries instead of the host's -- only surfacing when the two
versions actually disagree in a way that breaks something.

Measured, tan-cli#990/#992, on real CI: `tan bootstrap` spawned `west sdk
install`, which shelled `tar --xz`, which loaded tan's bundled
`liblzma.so.5` (older than the host's) ahead of the system one and failed
outright:

    xz: /home/runner/.local/bin/tan-cli-lib/_internal/liblzma.so.5:
    version `XZ_5.4' not found (required by xz)
    /usr/bin/tar: Child returned status 1

`LD_LIBRARY_PATH_ORIG` (not "am I frozen") is the trigger: it is
PyInstaller's OWN marker that its bootloader modified `LD_LIBRARY_PATH` for
THIS process, and the exact value to restore -- present only on a real
PyInstaller-onedir Linux run, never on `python -m tan` from source (this
repo's own dev/CI/test runs), and never on macOS/Windows (`LD_LIBRARY_PATH`
is not their loader variable) -- so this never touches a host that never had
the problem, and never guesses at a fallback value.

Lifted out of `tan.commands.bootstrap_cmd.Runner._env`, which worked out this
rule first (tan-cli#990) but was exactly one spawn site out of ~25 under
`python/tan/` -- this module is the ONE place the rule is written down,
per tan-cli#992. `Runner._env` itself now calls [`restore_ld_library_path`]
rather than carrying its own copy of the restore logic.
"""
from __future__ import annotations

import os
from collections.abc import Mapping


def restore_ld_library_path(env: dict[str, str]) -> None:
    """Mutate `env` IN PLACE: undo the frozen bootloader's `LD_LIBRARY_PATH`
    override, if `LD_LIBRARY_PATH_ORIG` (read from the REAL process
    environment, `os.environ` -- never from `env` itself, which may already
    be a partially-built child env with no such key) says one happened.

    A no-op on every host that never had the problem: a source run, macOS,
    Windows, or a frozen run whose `LD_LIBRARY_PATH_ORIG` is absent.

    Sets `LD_LIBRARY_PATH` to the exact preserved value when it was
    non-empty; POPS the key entirely (never sets `""`) when the host had no
    `LD_LIBRARY_PATH` before the bootloader touched it -- some dynamic
    loaders treat an explicit empty value as "search the current directory",
    which is a worse outcome than the key being absent altogether."""
    orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig is None:
        return
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)


def spawn_env(
    overrides: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The `env=` every subprocess spawn in `tan` hands its child: a fresh
    copy of `base` (`os.environ` when not given) with
    [`restore_ld_library_path`] applied, then `overrides` layered on top.

    `base`, when given, is typically an already-adjusted mapping a caller
    built for its own reasons (e.g. `with_venv_on_path`'s PATH prepend) --
    `spawn_env` copies it rather than mutating it, then applies the SAME
    restore on top, so a caller can freely compose
    `with_venv_on_path(spawn_env(), tool)` or
    `spawn_env(base=with_venv_on_path(dict(os.environ), tool))`
    interchangeably; the two touch disjoint keys (`PATH` vs
    `LD_LIBRARY_PATH`) so the order never matters.

    Always returns an explicit `dict`, never `None`:
    `subprocess.run(env=None)` means "inherit this process's own
    (unrestored) environment", which is exactly the leak this module exists
    to close -- no call site may spawn with an implicit/`None` environment.
    """
    env = dict(base) if base is not None else dict(os.environ)
    restore_ld_library_path(env)
    if overrides:
        env.update(overrides)
    return env
