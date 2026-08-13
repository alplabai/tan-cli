# SPDX-License-Identifier: Apache-2.0
"""The one containment guard every ACTIVE project writer trusts.

`tan init` (`tan.core.scaffold.write_files`) and `tan generate`
(`tan.commands.generate_cmd`, its default -- non-`--output` -- targets) both
join a caller-controlled relative path onto a project root and write there.
Neither used to check the RESOLVED target after following symlinks, so a
pre-existing directory symlink under the project (`<project>/src` ->
somewhere else) or a symlinked existing leaf silently redirected the write
outside the project while the envelope still reported the logical in-project
path as written (tan-cli#325).

`confine_to_build_root` (`tan.commands.build.materialise`) is the same
question for a DIFFERENT root (a build root, reused by `tan clean`) with its
own coded error (`build.path-escape`) and its own message shape; this module
is `resolve_confined` for the other two writers, not a third hand-rolled copy
of the same predicate (tan-cli#322 is exactly this drift: bootstrap and doctor
each grew their own resolver).

`Path.is_relative_to()` on TWO resolved paths, not a string prefix test:
resolving both sides is what makes a symlinked PROJECT ROOT itself still work
(`is_relative_to` only needs the root to be a prefix of the target's own
resolved components, so a project opened through a symlink still confines
correctly -- the escape this guards against is a write LANDING outside the
root, not the root being reached through one), and `is_relative_to` is
correct on Windows where a naive `str(target).startswith(str(root))` is not:
path comparison there folds case (`C:\\Foo` vs `c:\\foo`) and a bare
`startswith` would also wrongly accept a sibling directory whose name happens
to share the root's as a prefix (`C:\\proj` vs `C:\\project2`).

That case-folding is a `PureWindowsPath` property, NOT a filesystem one, and
those two came apart on macOS (tan-cli#587). `PurePosixPath.is_relative_to`
compares case-SENSITIVELY, and `Path.resolve()` does not canonicalise case on
a POSIX flavour to hide the difference -- `posixpath.realpath` walks the path
with `os.lstat`/`os.readlink` only and never asks the filesystem for a
component's true on-disk name, so `<tmp>/PROJECT` stays `<tmp>/PROJECT` even
where it names the very directory created as `<tmp>/Project`. On APFS (the
macOS default, case-INSENSITIVE and case-preserving) that made the guard
refuse a target genuinely inside the root. The failure direction was
over-restriction, never a bypass, but a `PathEscapeError` naming a path the
user can plainly see inside their project is a refusal that cannot be
trusted, and this module exists to be trusted.

The fallback below is INODE IDENTITY (`os.path.samestat`), not case folding.
Folding would be wrong on a case-sensitive volume, where `<tmp>/Project` and
`<tmp>/PROJECT` are two different directories and accepting the second as
inside the first is exactly the escape this guards against; asking the
filesystem whether the two names reach the same directory is right on both
kinds of volume, and needs no guess about which kind is underfoot.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathEscapeError(Exception):
    """`target` resolved outside `root` after symlink resolution. Carries both
    resolved paths so a caller can phrase its own coded issue message rather
    than parsing this one."""

    def __init__(self, root: Path, target: Path) -> None:
        super().__init__(f"'{target}' resolves outside '{root}'")
        self.root = root
        self.target = target


def _reaches_the_same_root(resolved_root: Path, resolved_target: Path) -> bool:
    """True when `resolved_target`'s first `len(resolved_root.parts)` components
    name the SAME directory as `resolved_root`, spelled differently.

    Only the prefix at the root's own depth is compared, and only by
    `os.stat` identity: if that prefix IS the root directory, everything after
    it lands inside the root by construction -- `resolved_target` has already
    had every symlink followed by `Path.resolve()`, so the remaining
    components are literal, not another indirection waiting to redirect the
    write.

    A shorter target than the root cannot be inside it, and an `OSError` (the
    prefix does not exist, or is unreadable) answers the question `False`:
    this runs only after `is_relative_to` already said no, so `False` simply
    keeps the pre-existing refusal.

    Not restricted to case: a bind mount or any other alias that makes two
    spellings reach one directory answers `True` here, which is the same
    answer for the same reason -- the write lands inside the root either way,
    and "the write LANDS outside the root" is the escape being guarded.
    """
    root_parts = resolved_root.parts
    target_parts = resolved_target.parts
    if len(target_parts) < len(root_parts):
        return False
    prefix = Path(*target_parts[: len(root_parts)])
    try:
        return os.path.samestat(os.stat(prefix), os.stat(resolved_root))
    except OSError:
        return False


def resolve_confined(root: Path, target: Path) -> Path:
    """Resolve `target` and require it stays inside the resolved `root`.

    Returns the resolved target. Raises `PathEscapeError` when it does not --
    including when a symlinked (or junctioned) PARENT directory of `target`
    points outside `root`, and when `target` itself is a symlink to a file
    outside `root`; `Path.resolve()` follows both. `target` need not exist:
    `resolve(strict=False)` (the default) resolves every symlink it finds and
    appends any remaining, not-yet-created components literally, so a `new`
    file under a symlinked parent is caught exactly like an existing one.

    Callers should also catch `OSError`/`ValueError` around this call: a path
    shape the host rejects outright (a Windows device-namespace path, one
    embedding a NUL) raises there, not here, and is a refusal either way.

    The returned path keeps the CALLER's spelling of the components it was
    given (`<tmp>/PROJECT/board.yaml`, not `<tmp>/Project/board.yaml`) on the
    identity path: on a case-insensitive volume both spellings open the same
    file, and re-spelling a caller's path from the on-disk casing would need
    a `readdir` per component that `Path.resolve()` never performs. Compare
    two returned paths with `os.path.samefile`, not `==`, if a test needs to
    tie one back to a differently-spelled root (tan-cli#587).
    """
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root) and not _reaches_the_same_root(
        resolved_root, resolved_target
    ):
        raise PathEscapeError(resolved_root, resolved_target)
    return resolved_target
