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
    root = _repo(tmp_path, {"601.fixed.md": ""})
    assert ac.main(["--root", str(root)]) == 1
    assert (root / "changelog.d" / "601.fixed.md").is_file()


def test_a_whitespace_only_fragment_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"602.fixed.md": "   \n\t\n   "})
    assert ac.main(["--root", str(root)]) == 1
    assert (root / "changelog.d" / "602.fixed.md").is_file()


# --- tan-cli#930: content, not only filename ------------------------------
#
# `--check` (and every other mode, since they share `load_fragments`) used to
# exit 0 on the measured defect: a fragment whose entire body is a single
# line of prose with no Markdown bullet marker. Planted here verbatim.


def test_the_measured_930_defect_is_now_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact case from the issue: `not a bullet at all` must now be rc!=0,
    and the failure must NAME the file and say what is wrong with it -- a
    bare exit code is not enough for a human to act on."""
    root = _repo(tmp_path, {"778.changed.md": "not a bullet at all"})
    assert ac.main(["--check", "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "778.changed.md" in err
    assert "not a Markdown bullet" in err
    # And --check must be consistent with every other mode: it must not have
    # silently folded or deleted anything on the way to failing.
    assert (root / "changelog.d" / "778.changed.md").is_file()
    assert "not a bullet at all" not in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_930_defect_is_also_refused_by_the_real_fold_and_require_empty(
    tmp_path: Path,
) -> None:
    """The validation lives in `load_fragments`, shared by every mode --
    not bolted onto `--check` alone, or it would drift the moment another
    entry point is added."""
    root = _repo(tmp_path, {"778.changed.md": "not a bullet at all"})
    assert ac.main(["--root", str(root)]) == 1
    assert ac.main(["--root", str(root), "--require-empty"]) == 1


def test_a_fragment_missing_only_its_leading_bullet_marker_is_named_precisely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A more realistic slip than the planted case: the author forgot the
    `- ` on an otherwise normal entry. Must still be caught and named."""
    root = _repo(tmp_path, {"779.fixed.md": "**Forgot the leading dash.** Body text."})
    assert ac.main(["--check", "--root", str(root)]) == 1
    assert "779.fixed.md" in capsys.readouterr().err


def test_an_unbalanced_code_fence_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An odd number of ``` markers would swallow whatever splices in next
    into an unterminated code block -- caught even though every individual
    line is itself indented/bulleted correctly."""
    body = "- **Entry.** Example:\n\n  ```\n  unterminated fence"
    root = _repo(tmp_path, {"780.fixed.md": body})
    assert ac.main(["--check", "--root", str(root)]) == 1
    assert "780.fixed.md" in capsys.readouterr().err


def test_a_continuation_line_with_no_bullet_above_it_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An indented-only body (nothing to continue) is the other bulletless
    shape: no offending column-0 line to point at, just no list at all."""
    root = _repo(tmp_path, {"781.fixed.md": "  indented text with no bullet above it"})
    assert ac.main(["--check", "--root", str(root)]) == 1
    assert "781.fixed.md" in capsys.readouterr().err


def test_legitimate_multi_bullet_multi_paragraph_fragments_still_pass(
    tmp_path: Path,
) -> None:
    """The other half of non-vacuity: real, valid house-style content must
    NOT be rejected. Mirrors the actual shape used across changelog.d/ --
    multiple top-level bullets, wrapped continuation lines, a nested
    sub-bullet, and a fenced code block, all in one fragment."""
    body = (
        "- **First entry, wrapped across two\n"
        "  physical lines.** More text here, still the same bullet.\n\n"
        "  ```\n"
        "  a fenced example, indented under the bullet\n"
        "  ```\n\n"
        "  - a nested sub-bullet, also indented\n\n"
        "- **Second, independent top-level bullet.** Short body."
    )
    root = _repo(tmp_path, {"782.fixed.md": body})
    assert ac.main(["--check", "--root", str(root)]) == 0


def test_check_reports_a_visible_count_even_when_it_matches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty changelog.d/ must not pass SILENTLY -- the #919/#943 failure
    mode is a check that validates an empty set and says nothing about it."""
    root = _repo(tmp_path, {})
    assert ac.main(["--check", "--root", str(root)]) == 0
    assert "0 fragment(s) pending" in capsys.readouterr().out


def test_the_930_gate_is_not_load_bearing_on_the_word_bullet_alone(
    tmp_path: Path,
) -> None:
    """Mutation-adjacent sanity check exercised in-suite (the full mutation
    proof -- disabling `fragment_shape_errors` and watching this file go red
    -- is done by hand per tan-cli#930's verification checklist, not
    re-implemented as a permanent scaffold here): a fragment that merely
    CONTAINS the word "bullet" in prose, without being one, must still be
    refused -- proving the check inspects structure, not vocabulary."""
    root = _repo(tmp_path, {"783.fixed.md": "This text mentions bullet but has no marker."})
    assert ac.main(["--check", "--root", str(root)]) == 1


# --- real corpus: prove the check on THIS repo's actual pending fragments --
#
# Everything above is a synthetic fixture. tan-cli#930's own complaint is
# that a check exercised only on green fixtures proves nothing about the real
# corpus -- so this drives the real function, and the real CLI, over the
# actual `changelog.d/` in this checkout. Since `python -m pytest tests -q`
# runs in the required `python-tests-shard` CI job on every PR (parity.yml),
# this is also how a future malformed real fragment gets caught automatically,
# not just at release time via `--require-empty`.

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_real_changelog_d_fragments_are_shape_valid() -> None:
    frag_dir = REPO_ROOT / "changelog.d"
    paths = [p for p in sorted(frag_dir.glob("*.md")) if p.name != "README.md"]
    # Non-vacuity: if this ever finds zero fragments, the assertions below
    # would pass trivially and prove nothing -- exactly the #919/#943 shape.
    assert paths, "no pending changelog.d/ fragments found -- check would be vacuous"

    errors: list[str] = []
    for path in paths:
        body = path.read_text(encoding="utf-8").strip("\n")
        if not body.strip():
            continue  # load_fragments' own empty-body refusal covers this
        errors.extend(ac.fragment_shape_errors(path.name, body))
    assert not errors, "\n".join(errors)


def test_check_over_the_real_repo_passes() -> None:
    """End-to-end over the real tree, not `--root <tmp fixture>`."""
    assert ac.main(["--check"]) == 0


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
