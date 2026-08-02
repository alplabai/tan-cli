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
"""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(Exception):
    """`target` resolved outside `root` after symlink resolution. Carries both
    resolved paths so a caller can phrase its own coded issue message rather
    than parsing this one."""

    def __init__(self, root: Path, target: Path) -> None:
        super().__init__(f"'{target}' resolves outside '{root}'")
        self.root = root
        self.target = target


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
    """
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise PathEscapeError(resolved_root, resolved_target)
    return resolved_target
