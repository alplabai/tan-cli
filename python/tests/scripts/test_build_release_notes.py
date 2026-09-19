# SPDX-License-Identifier: Apache-2.0
"""The Release assets block must survive a CHANGELOG section over 125000 chars.

tan-cli#1276. `softprops/action-gh-release@efb35369` (v3.0.3) silently
truncates any release body to 125000 UTF-16 code units
(`truncateReleaseNotes`, `src/github.ts:254-256`) before publishing. The old
`release.yml` step wrote the CHANGELOG slice, then unconditionally appended
the `## Release assets` block after it -- so once the slice alone passed that
limit (0.7.0's section measures 770642 characters), the block was silently cut
away and the job still went green.

The load-bearing assertion in every "oversized" test below is not "the job
exits 0" -- it is that `## Release assets` (and the glibc floor inside it)
literally appears, unmangled, in the final body, AND that the body's own
UTF-16 length is under the action's hard limit so the action's truncation is
never reached at all. `test_a_naive_concatenation_is_what_broke_0_7_0` pins
the OLD, unfixed shape (slice-then-append, no budget) directly against the
measured 0.7.0 numbers from the issue, so it documents the actual failure
mode this module exists to prevent, not a hypothetical one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_release_notes.py"
_spec = importlib.util.spec_from_file_location("build_release_notes", _SCRIPT)
assert _spec and _spec.loader
brn = importlib.util.module_from_spec(_spec)
sys.modules["build_release_notes"] = brn
_spec.loader.exec_module(brn)


def _changelog(version: str, section_body: str) -> str:
    return (
        f"# Changelog\n\n"
        f"## [{version}] -- 2026-09-19\n\n"
        f"{section_body}\n\n"
        f"## [0.6.0] -- 2026-08-04\n\n"
        f"- an older entry\n"
    )


def _gh_action_truncate(body: str, limit: int = brn.RELEASE_BODY_UTF16_HARD_LIMIT) -> str:
    """Reproduce `truncateReleaseNotes`'s own slicing, in UTF-16 units.

    Node's `String.prototype.slice` operates on UTF-16 code units, so the
    equivalent Python operation is: encode UTF-16LE, slice by 2-byte units,
    decode back. `errors="ignore"` matches a slice landing inside a surrogate
    pair being dropped rather than raising, which is what a raw JS slice does
    too (it would produce a lone surrogate; decoding drops it here instead of
    round-tripping it, close enough for this test's purpose of proving
    presence/absence of ASCII markers like `## Release assets`).
    """
    encoded = body.encode("utf-16-le")
    return encoded[: limit * 2].decode("utf-16-le", errors="ignore")


# ---------------------------------------------------------------------------
# utf16_len: the counting primitive the whole fix depends on
# ---------------------------------------------------------------------------


def test_utf16_len_matches_python_len_for_plain_ascii():
    assert brn.utf16_len("hello") == len("hello") == 5


def test_utf16_len_counts_a_surrogate_pair_as_two_units_not_one_code_point():
    # Arrange -- U+1F389 PARTY POPPER is outside the Basic Multilingual
    # Plane: one Python code point, a UTF-16 surrogate PAIR.
    emoji = "\U0001f389"
    assert len(emoji) == 1
    # Act / Assert
    assert brn.utf16_len(emoji) == 2


def test_a_body_that_fits_by_python_len_can_still_overflow_by_utf16_len():
    # Arrange -- enough astral characters that Python's len() stays well
    # under the hard limit while the UTF-16 count does not.
    body = "\U0001f389" * 70_000
    # Assert: this is the exact gap a len()-based budget would miss.
    assert len(body) == 70_000
    assert brn.utf16_len(body) == 140_000 > brn.RELEASE_BODY_UTF16_HARD_LIMIT


# ---------------------------------------------------------------------------
# slice_changelog_section -- #212 refusals, unchanged
# ---------------------------------------------------------------------------


def test_a_missing_section_is_refused():
    text = "# Changelog\n\n## [0.6.0] -- 2026-08-04\n\n- old\n"
    with pytest.raises(SystemExit, match=r"has no `## \[0\.7\.0\]` section"):
        brn.slice_changelog_section(text, "0.7.0")


def test_an_empty_section_is_refused():
    text = _changelog("0.7.0", "")
    with pytest.raises(SystemExit, match=r"section is empty"):
        brn.slice_changelog_section(text, "0.7.0")


def test_a_normal_section_is_returned_verbatim():
    text = _changelog("0.7.0", "- did a thing")
    assert brn.slice_changelog_section(text, "0.7.0") == "- did a thing"


# ---------------------------------------------------------------------------
# read_glibc_floor -- refusal unchanged
# ---------------------------------------------------------------------------


def test_a_missing_glibc_floor_file_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="missing or empty"):
        brn.read_glibc_floor(tmp_path / "does-not-exist.txt")


def test_an_empty_glibc_floor_file_is_refused(tmp_path):
    path = tmp_path / "glibc-floor.txt"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing or empty"):
        brn.read_glibc_floor(path)


def test_a_real_glibc_floor_file_is_read_and_stripped(tmp_path):
    path = tmp_path / "glibc-floor.txt"
    path.write_text("GLIBC_2.30\n", encoding="utf-8")
    assert brn.read_glibc_floor(path) == "GLIBC_2.30"


# ---------------------------------------------------------------------------
# assemble_body -- the actual fix
# ---------------------------------------------------------------------------


def test_a_normal_sized_slice_is_kept_verbatim_and_the_block_follows():
    block = brn.assets_block("GLIBC_2.30")
    body = brn.assemble_body("- did a thing", block, "0.6.0")
    assert body.startswith("- did a thing")
    assert "## Release assets" in body
    assert "GLIBC_2.30" in body
    assert brn.utf16_len(body) <= brn.RELEASE_BODY_UTF16_HARD_LIMIT


def test_an_oversized_slice_is_replaced_by_a_pointer_notice_and_the_block_survives():
    # Arrange -- mirrors 0.7.0's measured 770642-character section.
    oversized = "- an entry\n" * 70_000
    block = brn.assets_block("GLIBC_2.30")
    # Act
    body = brn.assemble_body(oversized, block, "0.7.0")
    # Assert: the block is present and intact -- the entire point of #1276.
    assert "## Release assets" in body
    assert "GLIBC_2.30" in body
    assert "gh attestation verify" in body
    # Assert: the slice itself was not published in full.
    assert "- an entry" not in body
    # Assert: an absolute URL, not a relative one (a relative link in a
    # release body resolves under /blob/ and 404s).
    assert "https://github.com/alplabai/tan-cli/blob/v0.7.0/CHANGELOG.md" in body
    # Assert: honest about why.
    assert "too long" in body
    # Assert: fits comfortably, so the action's own truncation never fires.
    assert brn.utf16_len(body) <= brn.RELEASE_BODY_UTF16_HARD_LIMIT
    assert _gh_action_truncate(body) == body


def test_astral_characters_alone_can_push_a_slice_into_the_notice_path():
    # Arrange -- proves the budget decision is made in UTF-16 units, not
    # Python code points: this slice's `len()` is under SLICE_BUDGET_UTF16,
    # but its UTF-16 length is not.
    astral_slice = "\U0001f389" * 60_000
    assert len(astral_slice) < brn.SLICE_BUDGET_UTF16
    assert brn.utf16_len(astral_slice) > brn.SLICE_BUDGET_UTF16
    block = brn.assets_block("GLIBC_2.30")
    # Act
    body = brn.assemble_body(astral_slice, block, "0.9.9")
    # Assert: the notice path was taken, not the verbatim one.
    assert "too long" in body
    assert "## Release assets" in body
    assert brn.utf16_len(body) <= brn.RELEASE_BODY_UTF16_HARD_LIMIT


def test_a_body_still_over_the_hard_limit_after_the_notice_is_refused(monkeypatch):
    # Arrange -- force the "always present" block itself to be enormous, so
    # even the notice-plus-block assembly cannot fit. This is the last-resort
    # guard: never rely on the action's own silent truncation.
    huge_block = "## Release assets\n\n" + ("x" * 200_000)
    # Act / Assert
    with pytest.raises(SystemExit, match="over action-gh-release's"):
        brn.assemble_body("- did a thing", huge_block, "0.7.0")


def test_a_naive_concatenation_is_what_broke_0_7_0():
    """Pins the OLD, unfixed behaviour this module replaces.

    This is not a test of `build_release_notes`; it is a fixed record of the
    defect from the issue, measured: a bare slice-then-append with no budget
    check publishes a body whose `## Release assets` block does not survive
    the action's own 125000-unit truncation. If this test ever starts
    failing, the *issue*, not the fix, has changed shape.
    """
    oversized = "- an entry\n" * 70_000  # mirrors 0.7.0's 770642 chars
    block = brn.assets_block("GLIBC_2.30")
    naive_body = f"{oversized.strip()}\n\n{block}"
    assert brn.utf16_len(naive_body) > brn.RELEASE_BODY_UTF16_HARD_LIMIT
    truncated = _gh_action_truncate(naive_body)
    assert "## Release assets" not in truncated
