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

WINDOWS = os.name == "nt"


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


@pytest.mark.skipif(not WINDOWS, reason="Windows-only path shape")
def test_mixed_case_is_not_an_escape_on_windows(tmp_path):
    root = tmp_path / "Project"
    root.mkdir()
    # Same real path, different case -- Windows path comparison folds case.
    mixed_case_target = Path(str(tmp_path).upper()) / "PROJECT" / "board.yaml"

    resolved = resolve_confined(root, mixed_case_target)

    assert resolved == (root / "board.yaml").resolve()
