# SPDX-License-Identifier: Apache-2.0
"""`version_check.py` must refuse a CHANGELOG section that is only a heading
(tan-cli#500).

`changelog_problems()` checked that `## [<target>]` EXISTS and nothing more, so
a heading with nothing under it satisfied every pre-tag gate. The cost lands
late and cannot be undone: `release.yml` slices the release body by an exact
`^## [<target>]` match, finds an empty slice, and fails the release job AFTER
four PyInstaller freezes have run -- with the tag already pushed and immutable.
That is #212's failure mode surviving #212's fix, one condition over.

These tests drive the real function against a synthetic CHANGELOG rather than
the repository's own, so they keep meaning after 0.5.2 ships and cannot be
satisfied by whatever the tree happens to contain today.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "python" / "scripts" / "version_check.py"


def _load(changelog_text: str, root: pathlib.Path):
    """`version_check` bound to a throwaway repo root holding `changelog_text`.

    Loaded from the file rather than imported: `python/scripts/` is not on
    `sys.path`, which is why `test_planner_resync.py` loads its subject the
    same way. `REPO_ROOT` is rebound after import because the module resolves
    it from its own `__file__` at import time.
    """
    (root / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("version_check_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.REPO_ROOT = root
    return module


HEADING_ONLY = """\
# Changelog

## [9.9.9] — Unreleased

## [9.9.8] — 2026-01-01

- a real entry, so the file is not degenerate
"""

POPULATED = """\
# Changelog

## [9.9.9] — Unreleased

### Fixed

- something worth publishing

## [9.9.8] — 2026-01-01

- a real entry
"""


def test_a_heading_with_nothing_under_it_is_refused(tmp_path: pathlib.Path) -> None:
    """The defect itself: heading present, section empty, every gate green."""
    module = _load(HEADING_ONLY, tmp_path)
    problems = module.changelog_problems("9.9.9-rc1.dev0")
    assert problems, (
        "`## [9.9.9]` has nothing under it and changelog_problems() reported "
        "no problem -- which is how a tag reaches release.yml, burns four "
        "PyInstaller freezes and then fails on an empty release body, with "
        "the tag already immutable"
    )
    assert "EMPTY" in problems[0], problems


def test_a_populated_section_is_accepted(tmp_path: pathlib.Path) -> None:
    """The other half, so the check above cannot be satisfied by refusing
    everything. Without this, `return ['...']` unconditionally would pass."""
    module = _load(POPULATED, tmp_path)
    assert module.changelog_problems("9.9.9-rc1.dev0") == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [("9.9.9", ""), ("9.9.8", "- a real entry, so the file is not degenerate")],
)
def test_the_section_reader_stops_at_the_next_heading(
    tmp_path: pathlib.Path, target: str, expected: str
) -> None:
    """The slice must end where release.yml's does.

    A reader that ran past the next `## [` would find the FOLLOWING release's
    notes under an empty heading and call it populated -- the empty-section
    check would then be unfalsifiable for every version except the last one in
    the file, which is exactly the shape of bug this gate exists to catch.
    """
    module = _load(HEADING_ONLY, tmp_path)
    assert module.changelog_section_body(target) == expected


MALFORMED_NEXT_HEADING = """\
# Changelog

## [9.9.9] — Unreleased

## [broken

- text that belongs to no released version
"""


def test_the_slice_terminates_where_release_yml_terminates(
    tmp_path: pathlib.Path,
) -> None:
    """A malformed `## [broken` heading must end the slice here too.

    `release.yml` ends on `line.startswith("## [")`; a slice built on
    `read_changelog_headings()`'s regex would not, because that regex requires
    a CLOSING bracket. The two would then disagree about the very case this
    check exists for: this function would call `## [9.9.9]` populated, every
    pre-tag gate would pass, and `release.yml` would slice `''` and fail after
    four freezes with the tag already immutable.

    Found in review of the first version of this fix, which had exactly that
    divergence while its own docstring claimed the two "cannot disagree".
    """
    module = _load(MALFORMED_NEXT_HEADING, tmp_path)
    assert module.changelog_section_body("9.9.9") == ""
    problems = module.changelog_problems("9.9.9-rc1.dev0")
    assert problems and "EMPTY" in problems[0], problems


def test_an_absent_heading_is_still_reported_as_absent(tmp_path: pathlib.Path) -> None:
    """The pre-existing #212 message must survive: an absent heading and an
    empty one are different failures and must not collapse into one wording."""
    module = _load(POPULATED, tmp_path)
    problems = module.changelog_problems("9.9.7-rc1.dev0")
    assert problems and "no `## [9.9.7]` section" in problems[0], problems
    assert "EMPTY" not in problems[0], problems
