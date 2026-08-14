# SPDX-License-Identifier: Apache-2.0
"""`tan.core.fs_confine.resolve_confined` -- the shared containment guard
`tan.core.scaffold.write_files` and `tan.commands.generate_cmd` both use
(tan-cli#325). Symlink-following behaviour itself is covered against real
links in `tests/core/test_scaffold.py` and `tests/commands/
test_generate_command.py`; this file covers the pure resolve/compare shape,
including the case a naive string-prefix test gets wrong on Windows."""
import os
from pathlib import Path

import pytest

from tan.core.fs_confine import PathEscapeError, resolve_confined


def _case_insensitive_root(parent: Path) -> Path:
    """Create `<parent>/Project` and hand it back if -- and only if -- this
    volume also reaches it as `<parent>/PROJECT`; raise otherwise.

    ONE function, used by both the capability probe and the test itself, so
    the probe cannot drift into probing a different operation than the one
    the test performs (the shape tan-cli#580 introduced in
    `tests/test_envelope_surrogate.py`).

    PROBED, not inferred from `os.name`. The guard here used to be
    `skipif(os.name != "nt")` with the reason "Windows-only path shape", while
    the assertion's own comment named the real requirement correctly -- *a
    filesystem whose path comparison folds case*. Case-insensitivity is a
    FILESYSTEM property, not a Windows one: APFS, the macOS default, is
    case-insensitive too, and macOS is a platform this repo ships and runs
    pytest on. So the one path shape where case matters went unexercised
    there, and being over-restrictive rather than under-, it could never turn
    CI red on its own (tan-cli#587).

    `samefile` rather than `exists()`: a FUSE or network mount can answer a
    differently-cased lookup with a DIFFERENT directory, and a probe that
    accepted that would let the assertion below pass while exercising nothing.
    """
    root = parent / "Project"
    root.mkdir()
    upper = parent / "PROJECT"
    if not upper.exists() or not os.path.samefile(upper, root):
        raise ValueError(
            f"'{upper}' does not name the directory created as '{root}'; this "
            f"volume's path comparison is case-sensitive"
        )
    return root


@pytest.fixture
def case_insensitive_root(tmp_path):
    """A `Project/` directory on a volume that also reaches it as `PROJECT/`,
    or a skip naming the filesystem behaviour that was missing.

    `return`, not `yield`: a `yield` inside the `try` would also swallow a
    `ValueError` raised by the TEST body and report it as a skip.
    """
    try:
        return _case_insensitive_root(tmp_path)
    except ValueError as err:
        pytest.skip(str(err))


def test_a_target_inside_root_resolves_and_returns(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "src" / "main.c"  # need not exist

    resolved = resolve_confined(root, target)

    assert resolved == target.resolve()


def test_the_root_itself_is_not_an_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert resolve_confined(root, root) == root.resolve()


def test_a_target_outside_root_raises(tmp_path):
    root = tmp_path / "project"
    sibling = tmp_path / "sibling"
    root.mkdir()

    with pytest.raises(PathEscapeError):
        resolve_confined(root, sibling / "file.txt")


def test_a_sibling_sharing_the_root_name_as_a_string_prefix_is_still_an_escape(
    tmp_path,
):
    """`C:\\proj` vs `C:\\project2`: a bare `str(target).startswith(str(root))`
    would wrongly accept this. `is_relative_to` on resolved paths does not."""
    root = tmp_path / "proj"
    decoy = tmp_path / "proj2"
    root.mkdir()

    with pytest.raises(PathEscapeError):
        resolve_confined(root, decoy / "file.txt")


def test_mixed_case_is_not_an_escape_on_a_case_insensitive_volume(
    case_insensitive_root,
):
    """Runs wherever the volume folds case -- NTFS, APFS, any case-insensitive
    mount -- not wherever `os.name == "nt"` (tan-cli#587)."""
    root = case_insensitive_root
    # Same real directory, different spelling. Only the component the probe
    # actually proved foldable is re-cased -- the Windows-only version
    # upper-cased the WHOLE of `tmp_path`, which a probe cannot vouch for:
    # case-insensitivity is per-VOLUME, and `tmp_path`'s ancestors can sit on
    # a different one (they do on a case-insensitive mount under a
    # case-sensitive root, which is how this was reproduced on Linux).
    mixed_case_target = root.parent / "PROJECT" / "board.yaml"

    resolved = resolve_confined(root, mixed_case_target)

    # `samefile` on the PARENT, not `==` on the leaf: the return keeps the
    # caller's spelling, and `PurePosixPath.__eq__` is case-sensitive, so an
    # `==` here would fail on APFS for a path that is provably inside `root`.
    # `board.yaml` itself is never created, hence the parent.
    assert resolved.name == "board.yaml"
    assert os.path.samefile(resolved.parent, root)


def test_a_differently_cased_sibling_is_still_an_escape(tmp_path):
    """The identity fallback must not degrade into case folding: on a
    case-SENSITIVE volume `Project` and `PROJECT` are two different
    directories and the second is an escape from the first. On a
    case-insensitive one this second `mkdir` is the one that cannot happen,
    so the case never arises there -- hence the skip rather than an
    inverted assertion."""
    root = tmp_path / "Project"
    root.mkdir()
    try:
        (tmp_path / "PROJECT").mkdir()
    except FileExistsError:
        pytest.skip(
            "this volume's path comparison folds case, so a distinct "
            "'PROJECT' sibling cannot exist alongside 'Project'"
        )

    with pytest.raises(PathEscapeError):
        resolve_confined(root, tmp_path / "PROJECT" / "board.yaml")
