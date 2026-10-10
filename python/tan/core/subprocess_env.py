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

`LD_LIBRARY_PATH_ORIG` is the PREFERRED trigger: it is PyInstaller's own
marker that its bootloader modified `LD_LIBRARY_PATH` for THIS process, and
it carries the exact value to restore.

**It is not the ONLY trigger, and tan-cli#1189 is what that cost.** Until
then this module treated an absent `ORIG` as proof that no override had
happened -- "a frozen run whose `LD_LIBRARY_PATH_ORIG` is absent" was listed
as a host that never had the problem. Measured on a bare `ubuntu:24.04`, the
identical `liblzma.so.5` failure came back with tan-cli#992's fix in place,
so on that host the restore did not fire. A host with no `LD_LIBRARY_PATH`
of its own is precisely the host where the bundle becomes the ONLY entry on
the child's search path -- the worst case, not the safe one.

So the second trigger is the fact that does not depend on the bootloader
having recorded anything: is the bundle dir ([`_frozen_bundle_dir`], i.e.
`sys._MEIPASS` on a frozen run) currently ON `LD_LIBRARY_PATH`? Both
triggers are still false on `python -m tan` from source (this repo's own
dev/CI/test runs, where `sys.frozen` is unset) and on macOS/Windows
(`LD_LIBRARY_PATH` is not their loader variable), so this still never
touches a host that never had the problem -- and it still never guesses at a
fallback value: with no `ORIG`, the only defensible restore is REMOVAL.

Lifted out of `tan.commands.bootstrap_cmd.Runner._env`, which worked out this
rule first (tan-cli#990) but was exactly one spawn site out of ~25 under
`python/tan/` -- this module is the ONE place the rule is written down,
per tan-cli#992. `Runner._env` itself now calls [`restore_ld_library_path`]
rather than carrying its own copy of the restore logic.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping


def _frozen_bundle_dir() -> str | None:
    """The directory a PyInstaller ONEDIR bootloader puts on
    `LD_LIBRARY_PATH` so the frozen binary finds its OWN shared libraries --
    `sys._MEIPASS`, which for a onedir freeze is the `_internal/` tree.
    `None` on a source run (this repo's own dev/CI/test runs), where
    `sys.frozen` is unset and there is no bundle to strip."""
    if not getattr(sys, "frozen", False):
        return None
    bundle = getattr(sys, "_MEIPASS", None)
    return bundle if isinstance(bundle, str) and bundle else None


def _without_bundle_dir(value: str, bundle: str) -> str | None:
    """`value` (an `os.pathsep`-joined search path) with every entry that IS
    `bundle` removed -- or `None` when `bundle` does not appear in it at all,
    which is the caller's signal that there is nothing to undo.

    Compared with `os.path.normpath`, so a trailing slash or a `./` segment
    cannot smuggle the bundle past the match. Empty entries are dropped from
    the result: stripping the bundle out of `"<bundle>"` or
    `"<bundle>:<bundle>"` must not leave a bare `":"`, which some loaders
    read as "search the current directory" -- the same hazard the
    `orig == ""` branch below is written to avoid."""
    entries = value.split(os.pathsep)
    target = os.path.normpath(bundle)
    if not any(entry and os.path.normpath(entry) == target for entry in entries):
        return None
    kept = [entry for entry in entries if entry and os.path.normpath(entry) != target]
    return os.pathsep.join(kept)


def ld_library_path_needs_restore() -> bool:
    """Would a child spawned from THIS process inherit the frozen bundle's
    lib dir on `LD_LIBRARY_PATH`?

    The predicate a caller uses to decide whether it may skip building a
    child environment at all. `bootstrap_cmd.Runner._env` is the one such
    caller: it returns `None` (meaning `subprocess.run(env=None)`, "inherit
    this process's unrestored environment") on its fast path, and that fast
    path is only safe when this returns `False`."""
    if os.environ.get("LD_LIBRARY_PATH_ORIG") is not None:
        return True
    bundle = _frozen_bundle_dir()
    if bundle is None:
        return False
    current = os.environ.get("LD_LIBRARY_PATH")
    if not current:
        return False
    return _without_bundle_dir(current, bundle) is not None


def _restore_from_orig(env: dict[str, str], orig: str) -> None:
    """The bootloader recorded a marker, so restore exactly what it saved.

    POPS the key rather than setting `""` when the host had no
    `LD_LIBRARY_PATH` before the bootloader touched it -- some dynamic
    loaders read an explicit empty value as "search the current directory",
    which is worse than the key being absent."""
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)


def _strip_bundle_dir(env: dict[str, str]) -> None:
    """No marker was recorded -- remove the bundle dir from `LD_LIBRARY_PATH`
    if it is on it (tan-cli#1189).

    Deliberately does NOT invent a replacement value: with no
    `LD_LIBRARY_PATH_ORIG` the only defensible restore is REMOVAL. A frozen
    run whose path does not carry the bundle is left completely alone -- the
    bootloader never touched it, so neither does this."""
    bundle = _frozen_bundle_dir()
    if bundle is None:
        return
    current = os.environ.get("LD_LIBRARY_PATH")
    if not current:
        return
    stripped = _without_bundle_dir(current, bundle)
    if stripped is None:
        return
    if stripped:
        env["LD_LIBRARY_PATH"] = stripped
    else:
        env.pop("LD_LIBRARY_PATH", None)


def restore_ld_library_path(env: dict[str, str]) -> None:
    """Mutate `env` IN PLACE: undo the frozen bootloader's `LD_LIBRARY_PATH`
    override, reading the REAL process environment (`os.environ`) -- never
    `env` itself, which may already be a partially-built child env with no
    such key.

    A no-op on every host that never had the problem: a source run, macOS,
    Windows, or a frozen run where the bundle is not on the search path.

    Two triggers, not one -- see this module's docstring for why an absent
    `LD_LIBRARY_PATH_ORIG` is NOT proof that no override happened
    (tan-cli#1189), and [`_restore_from_orig`] / [`_strip_bundle_dir`] for
    what each arm does."""
    orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig is not None:
        _restore_from_orig(env, orig)
        return
    _strip_bundle_dir(env)


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
