# SPDX-License-Identifier: Apache-2.0
"""Tests for `python/scripts/assemble_changelog.py`.

The thing under test is a release-time step that MOVES text between files and
then DELETES the source. The failure that actually costs something is not a
crash -- it is an entry that quietly does not arrive: the release ships, the
fragment is gone, and nobody notices the changelog is missing a line until a
user asks why an advertised fix is undocumented. Every test here is aimed at
that, not at pretty formatting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assemble_changelog.py"
_spec = importlib.util.spec_from_file_location("assemble_changelog", _SCRIPT)
assert _spec and _spec.loader
ac = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog"] = ac
_spec.loader.exec_module(ac)


CHANGELOG = """\
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

## [0.9.9] — Unreleased

### Added

- **Pre-existing added entry.**

### Fixed

- **Pre-existing fixed entry.**

## [0.9.8] — 2026-01-01

### Fixed

- **Shipped entry that must not move.**
"""


def _repo(tmp_path: Path, fragments: dict[str, str], changelog: str = CHANGELOG) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    frag = tmp_path / "changelog.d"
    frag.mkdir()
    for name, body in fragments.items():
        (frag / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_every_fragment_reaches_the_changelog_and_is_deleted(tmp_path: Path) -> None:
    """The core contract: nothing is dropped, and the source is cleaned up."""
    root = _repo(tmp_path, {
        "101.added.md": "- **Added one.**",
        "102.fixed.md": "- **Fixed one.**",
        "103.fixed.md": "- **Fixed two.**",
        "104.security.md": "- **Security one.**",
    })
    assert ac.main(["--root", str(root)]) == 0

    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for expected in ("Added one.", "Fixed one.", "Fixed two.", "Security one."):
        assert expected in text, f"{expected} was dropped"

    remaining = [p.name for p in (root / "changelog.d").glob("*.md")]
    assert remaining == [], f"fragments left behind: {remaining}"


def test_existing_entries_survive(tmp_path: Path) -> None:
    """A fragment must never overwrite hand-written text already in the section."""
    root = _repo(tmp_path, {"201.fixed.md": "- **New fixed.**"})
    assert ac.main(["--root", str(root)]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Pre-existing fixed entry." in text
    assert "Pre-existing added entry." in text
    assert "New fixed." in text


def test_released_sections_are_untouched(tmp_path: Path) -> None:
    """Only the Unreleased section may be edited; shipped history is immutable."""
    root = _repo(tmp_path, {"301.fixed.md": "- **New fixed.**"})
    assert ac.main(["--root", str(root)]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    released = text.split("## [0.9.8]", 1)[1]
    assert "New fixed." not in released, "an entry leaked into a released section"
    assert "Shipped entry that must not move." in released


def test_verbatim_technical_strings_are_not_reformatted(tmp_path: Path) -> None:
    """Register/hex/SKU strings must survive byte-for-byte -- a rewrap corrupts them."""
    body = (
        "- **Probe check.** DPIDR `0x4C013477`, device "
        "`AE822FA0E5597LS0_M55_HE`, I2C `0x1E`, code `flash.serial-unsupported`."
    )
    root = _repo(tmp_path, {"401.fixed.md": body})
    assert ac.main(["--root", str(root)]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert body in text, "fragment body was altered in transit"


def test_a_bad_category_is_refused_not_dropped(tmp_path: Path) -> None:
    """The whole point: an unusable fragment must STOP the run, not vanish."""
    root = _repo(tmp_path, {"501.nonsense.md": "- **Would be lost.**"})
    assert ac.main(["--root", str(root)]) == 1
    # And nothing was consumed on the way to failing.
    assert (root / "changelog.d" / "501.nonsense.md").is_file()
    assert "Would be lost." not in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_an_empty_fragment_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"601.fixed.md": "   \n"})
    assert ac.main(["--root", str(root)]) == 1
    assert (root / "changelog.d" / "601.fixed.md").is_file()


def test_missing_unreleased_header_refuses_rather_than_guessing(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"701.fixed.md": "- **Entry.**"},
        changelog="# Changelog\n\n## [0.9.8] — 2026-01-01\n\n### Fixed\n\n- Old.\n",
    )
    assert ac.main(["--root", str(root)]) == 1
    assert (root / "changelog.d" / "701.fixed.md").is_file()


def test_require_empty_is_a_real_gate(tmp_path: Path) -> None:
    """--require-empty must FAIL while fragments remain, or it gates nothing."""
    root = _repo(tmp_path, {"801.fixed.md": "- **Entry.**"})
    assert ac.main(["--root", str(root), "--require-empty"]) == 1
    # Fold them, then the same gate must pass -- proving it tracks real state
    # rather than always failing.
    assert ac.main(["--root", str(root)]) == 0
    assert ac.main(["--root", str(root), "--require-empty"]) == 0


def test_assembly_is_deterministic(tmp_path: Path) -> None:
    """Same fragments -> same bytes, or every release diff is noise."""
    frags = {"901.fixed.md": "- **A.**", "902.fixed.md": "- **B.**", "903.added.md": "- **C.**"}
    a = _repo(tmp_path / "one", frags)
    b = _repo(tmp_path / "two", frags)
    assert ac.main(["--root", str(a)]) == 0
    assert ac.main(["--root", str(b)]) == 0
    assert (a / "CHANGELOG.md").read_text(encoding="utf-8") == (
        b / "CHANGELOG.md"
    ).read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path, {"1001.fixed.md": "- **Entry.**"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(["--root", str(root), "--dry-run"]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert (root / "changelog.d" / "1001.fixed.md").is_file()
    assert "Entry." in capsys.readouterr().out


def test_no_fragments_is_a_clean_no_op(tmp_path: Path) -> None:
    root = _repo(tmp_path, {})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(["--root", str(root)]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
