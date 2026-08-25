# SPDX-License-Identifier: Apache-2.0
"""Tests for `python/scripts/bump_dev_version.py` (tan-cli#770).

The workflow this script feeds -- checking out `dev`, committing, pushing a
branch, opening a PR -- is not exercised here for the same reason
`test_pin_move_verify.py` does not shell `git`/`gh` either: there is nothing
to unit-test about a network call, and the arithmetic + the four-file edit is
exactly the part a CI round trip is the wrong place to discover a bug in. Two
of the cases below are the REAL, MEASURED precedents this script's rule was
read off of (`python/tan/version.py`'s own history / CHANGELOG.md), not
invented examples.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import bump_dev_version as bdv  # noqa: E402
import version_check as vc  # noqa: E402


# ---------------------------------------------------------------------------
# next_dev_version -- the pure arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("released", "expected"),
    [
        # The two REAL precedents this rule was read off of (tan-cli#479,
        # tan-cli#768) -- see python/tan/version.py's own comment history.
        ("0.5.1", "0.5.2-rc1.dev0"),
        ("0.6.0-rc1", "0.6.0-rc2.dev0"),
        # A final release with a two-digit patch, so "+1" is exercised past
        # a single digit.
        ("1.2.9", "1.2.10-rc1.dev0"),
        # rc without an explicit number reads as rc0 (matching
        # version_check.py's own SemVer grammar), so the next one is rc1.
        ("0.4.0-rc", "0.4.0-rc1.dev0"),
        # alpha/beta are admitted by the same SemVer grammar even though no
        # tag in this repo has ever used them; the rule generalises the same
        # way rc does.
        ("0.5.0-alpha.3", "0.5.0-alpha4.dev0"),
        ("0.5.0-beta2", "0.5.0-beta3.dev0"),
    ],
)
def test_next_dev_version_arithmetic(released: str, expected: str) -> None:
    assert bdv.next_dev_version(released) == expected


@pytest.mark.parametrize(
    "released",
    [
        "0.6.0-rc5.dev0",  # already a dev version -- nothing to bump FROM
        "0.5.0-dev1",
        "0.5.0-dev",
        "not-a-version",
        "v0.5.0",  # a tag string, not the released version (leading v)
    ],
)
def test_next_dev_version_refuses_a_non_release_input(released: str) -> None:
    with pytest.raises(vc.VersionError):
        bdv.next_dev_version(released)


# ---------------------------------------------------------------------------
# build_plan / apply_plan -- the four-file edit, against a synthetic tree
# ---------------------------------------------------------------------------

_VERSION_PY = """\
# SPDX-License-Identifier: Apache-2.0
# A synthetic fixture, not the real file.
TAN_VERSION = "0.6.0-rc1"
"""

_PYPROJECT = """\
[project]
name = "alp-tan"
version = "0.6.0rc1"
description = "fixture"
"""

_NPM_SHIM = """\
{
  "name": "@alplabai/tan",
  "version": "0.6.0-rc1",
  "description": "fixture — an em dash, matching the real file's own"
}
"""

_CHANGELOG = """\
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

All notable changes to `tan` are documented here.

## [0.6.0-rc1] — 2026-08-14

### Added

- something that shipped in rc1
"""


def _synthetic_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal tree shaped like the real repo (`python/tan/version.py`,
    `python/pyproject.toml`, `npm-shim/package.json`, `CHANGELOG.md`), with
    `version_check`'s `PYTHON_ROOT`/`REPO_ROOT` rebound onto it -- the same
    technique `test_version_check_refuses_an_empty_changelog_section.py`
    uses on `version_check` itself. `bump_dev_version.vc` IS `version_check`
    (imported, not copied), so rebinding it here is visible to every function
    in the module under test without any of them taking a root argument.
    """
    python_root = tmp_path / "python"
    (python_root / "tan").mkdir(parents=True)
    (python_root / "tan" / "version.py").write_text(_VERSION_PY, encoding="utf-8")
    (python_root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / "npm-shim").mkdir()
    (tmp_path / "npm-shim" / "package.json").write_text(_NPM_SHIM, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")

    monkeypatch.setattr(vc, "PYTHON_ROOT", python_root)
    monkeypatch.setattr(vc, "REPO_ROOT", tmp_path)
    return tmp_path


def test_build_plan_computes_all_four_edits(tmp_path, monkeypatch) -> None:
    _synthetic_repo(tmp_path, monkeypatch)
    plan = bdv.build_plan("0.6.0-rc1", today="2026-08-20")

    assert plan.next_version == "0.6.0-rc2.dev0"
    assert plan.target == "0.6.0"
    assert 'TAN_VERSION = "0.6.0-rc2.dev0"' in plan.version_py_text
    assert 'version = "0.6.0rc2.dev0"' in plan.pyproject_text
    assert json.loads(plan.npm_shim_text)["version"] == "0.6.0-rc2.dev0"
    assert "## [0.6.0] — Unreleased" in plan.changelog_text
    # The new heading lands AHEAD of the existing (now-dated) one, matching
    # where a hand-written bump has put it both times before.
    assert plan.changelog_text.index("## [0.6.0] — Unreleased") < (
        plan.changelog_text.index("## [0.6.0-rc1]")
    )
    # The bullet lands under a `### Fixed` heading, not bare under `## [...]`
    # -- `release.yml` slices a published release's notes on an exact
    # `^## [<version>]` match, so a heading-less bullet here would open the
    # NEXT release's body above its own `### Added`/`### Fixed` sections.
    assert "## [0.6.0] — Unreleased\n\n### Fixed\n\n- `TAN_VERSION`" in (
        plan.changelog_text
    )
    # `_bump_npm_shim`'s `ensure_ascii=False` is load-bearing (tan-cli#880):
    # the real npm-shim/package.json's `description` carries a literal em
    # dash, and the default `ensure_ascii=True` would silently rewrite it as
    # a `\uXXXX` escape on every bump -- a diff on a field this script has
    # no business touching. The em dash in `_NPM_SHIM` above exists so this
    # assertion can catch that regression; it must appear literally, not
    # escaped.
    assert "fixture — an em dash" in plan.npm_shim_text
    assert "\\u2014" not in plan.npm_shim_text


def test_build_plan_leaves_files_untouched_until_apply(tmp_path, monkeypatch) -> None:
    root = _synthetic_repo(tmp_path, monkeypatch)
    bdv.build_plan("0.6.0-rc1")
    assert (root / "python" / "tan" / "version.py").read_text(encoding="utf-8") == _VERSION_PY


def test_apply_plan_writes_the_four_files_and_version_check_agrees(
    tmp_path, monkeypatch
) -> None:
    """The strongest regression test available here: apply the bump to a
    synthetic tree, then run `version_check`'s OWN self-check against the
    result. If the bump this script computes ever stopped satisfying the
    invariants `version_check.py` polices, this is where it would show up --
    without needing the real repository's files to exercise it."""
    root = _synthetic_repo(tmp_path, monkeypatch)
    plan = bdv.build_plan("0.6.0-rc1", today="2026-08-20")
    bdv.apply_plan(plan)

    assert (root / "python" / "tan" / "version.py").read_text(encoding="utf-8") != _VERSION_PY
    assert vc.check(None) == []


def test_build_plan_refuses_a_changelog_that_already_has_the_target_heading(
    tmp_path, monkeypatch
) -> None:
    changelog_with_target_already_present = _CHANGELOG.replace(
        "## [0.6.0-rc1] — 2026-08-14",
        "## [0.6.0] — Unreleased\n\n## [0.6.0-rc1] — 2026-08-14",
    )
    _synthetic_repo(tmp_path, monkeypatch)
    (tmp_path / "CHANGELOG.md").write_text(
        changelog_with_target_already_present, encoding="utf-8"
    )
    with pytest.raises(vc.VersionError, match=r"already has a `## \[0\.6\.0\]`"):
        bdv.build_plan("0.6.0-rc1")


# ---------------------------------------------------------------------------
# main() -- the CLI, including the must-not-silently-no-op skip path
# ---------------------------------------------------------------------------


def test_main_applies_and_reports_status_applied(
    tmp_path, monkeypatch, capsys
) -> None:
    root = _synthetic_repo(tmp_path, monkeypatch)
    github_output = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    rc = bdv.main(["--tag", "v0.6.0-rc1", "--apply"])

    assert rc == 0
    assert github_output.read_text(encoding="utf-8").strip() == "status=applied"
    assert 'TAN_VERSION = "0.6.0-rc2.dev0"' in (
        root / "python" / "tan" / "version.py"
    ).read_text(encoding="utf-8")


def test_main_skips_when_dev_has_already_moved_off_the_tag(
    tmp_path, monkeypatch
) -> None:
    """The one legitimate no-op: `TAN_VERSION` on `dev` no longer equals the
    tag just published (a previous run already bumped it, or a human beat the
    job to it). Must exit 0 AND leave the four files untouched -- and must
    say so via `status=skip` rather than `status=applied`, so a workflow step
    reading that output cannot mistake this for the bump having happened."""
    root = _synthetic_repo(tmp_path, monkeypatch)
    github_output = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    # version.py's fixture TAN_VERSION is "0.6.0-rc1"; ask about a
    # DIFFERENT tag, simulating dev having already moved on.
    rc = bdv.main(["--tag", "v0.6.0-rc2", "--apply"])

    assert rc == 0
    assert github_output.read_text(encoding="utf-8").strip() == "status=skip"
    assert (root / "python" / "tan" / "version.py").read_text(encoding="utf-8") == _VERSION_PY


def test_main_without_apply_is_a_dry_run(tmp_path, monkeypatch) -> None:
    root = _synthetic_repo(tmp_path, monkeypatch)
    github_output = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    rc = bdv.main(["--tag", "v0.6.0-rc1"])  # no --apply

    assert rc == 0
    assert github_output.read_text(encoding="utf-8").strip() == "status=would-apply"
    assert (root / "python" / "tan" / "version.py").read_text(encoding="utf-8") == _VERSION_PY
