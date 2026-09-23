# SPDX-License-Identifier: Apache-2.0
"""The Release assets block must survive a CHANGELOG section over 125000 chars.

tan-cli#1276. `softprops/action-gh-release@efb35369` (v3.0.3) silently
truncates any release body to at most 124999 UTF-16 code units
(`truncateReleaseNotes`, `src/github.ts:254-257`:
`input.substring(0, githubNotesMaxCharLength - 1)`) before publishing. The
old `release.yml` step wrote the CHANGELOG slice, then unconditionally
appended the `## Release assets` block after it -- so once the slice alone
passed that limit (0.7.0's section measures 770642 characters), the block
was silently cut away and the job still went green.

The load-bearing assertion in every "oversized" test below is not "the job
exits 0" -- it is that `## Release assets` (and the glibc floor inside it)
literally appears, unmangled, in the final body, AND that the body's own
UTF-16 length is under the action's hard limit so the action's truncation is
never reached at all. `test_a_naive_concatenation_is_what_broke_0_7_0`
actually RUNS the OLD "Slice CHANGELOG section for the release notes" step
(the real `run:` bash, extracted verbatim out of `release.yml` as it read at
`_OLD_RELEASE_YML_COMMIT`, the commit immediately before this fix), against
the measured 0.7.0 numbers from the issue, so it documents the actual
failure mode this module exists to prevent, not a hypothetical one -- and no
change to `build_release_notes.py` or `release.yml` can make it pass for the
wrong reason, since it re-implements neither.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_release_notes.py"
_spec = importlib.util.spec_from_file_location("build_release_notes", _SCRIPT)
assert _spec and _spec.loader
brn = importlib.util.module_from_spec(_spec)
sys.modules["build_release_notes"] = brn
_spec.loader.exec_module(brn)

#: `origin/dev`'s tip immediately before tan-cli#1276 landed -- the commit
#: right before `## Release assets` was moved to `build_release_notes.py`.
#: Pinned to this exact SHA, never to `origin/dev` itself (that ref keeps
#: moving; a later re-sync would silently repoint what this test proves
#: against): `git -C tan-cli log --oneline -1 <this SHA>` ->
#: "chore(release): fold 204 changelog fragments and bump to 0.6.1 (#1274)".
_OLD_RELEASE_YML_COMMIT = "03dbd38a50b06f5e3529477f3d1ce45d8b91c20c"


def _has_gnu_sed() -> bool:
    """The old step's own `sed -i "s/.../.../" release_notes.md` is GNU
    syntax (no backup-suffix argument) -- it only ever ran on `ubuntu-latest`
    (GNU coreutils `sed`). BSD/macOS `sed -i` requires an explicit suffix
    argument and misparses this exact invocation, unrelated to anything this
    fix touches. `sed --version` exits 0 and prints "GNU sed" only under GNU
    sed; BSD `sed` doesn't recognise the flag at all and exits non-zero."""
    try:
        proc = subprocess.run(["sed", "--version"], capture_output=True)
    except OSError:
        return False
    return proc.returncode == 0 and b"GNU sed" in proc.stdout


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

    `truncateReleaseNotes` is `input.substring(0, githubNotesMaxCharLength -
    1)` (v3.0.3 `src/github.ts:254-257`) -- it keeps `limit - 1` units, not
    `limit`. Node's `String.prototype.substring` operates on UTF-16 code
    units, so the equivalent Python operation is: encode UTF-16LE, slice by
    2-byte units up to `(limit - 1) * 2` bytes, decode back. `errors="ignore"`
    matches a slice landing inside a surrogate pair being dropped rather than
    raising, which is what a raw JS slice does too (it would produce a lone
    surrogate; decoding drops it here instead of round-tripping it, close
    enough for this test's purpose of proving presence/absence of ASCII
    markers like `## Release assets`).
    """
    encoded = body.encode("utf-16-le")
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore")


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
    assert brn.utf16_len(body) < brn.RELEASE_BODY_UTF16_HARD_LIMIT


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
    assert brn.utf16_len(body) < brn.RELEASE_BODY_UTF16_HARD_LIMIT
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
    assert brn.utf16_len(body) < brn.RELEASE_BODY_UTF16_HARD_LIMIT


def test_a_body_of_exactly_the_hard_limit_is_refused_not_accepted():
    """tan-cli#1276 review (low): the action's own ceiling is
    `RELEASE_BODY_UTF16_HARD_LIMIT - 1` units (`truncateReleaseNotes`'s own
    `- 1`, see that constant's docstring), so a body landing at EXACTLY the
    limit must still refuse -- accepting it would let `action-gh-release`
    silently drop its last character, the exact hazard this module exists
    to never rely on. Crafted directly with `assemble_body`, per the
    review's own repro: a block sized to make the notice-plus-block total
    come out to precisely `RELEASE_BODY_UTF16_HARD_LIMIT`.
    """
    version = "0.7.0"
    notice = brn.too_long_notice(version)
    separator = "\n\n"
    prefix_len = brn.utf16_len(notice) + brn.utf16_len(separator)
    pad_len = brn.RELEASE_BODY_UTF16_HARD_LIMIT - prefix_len
    block = "x" * pad_len
    assert brn.utf16_len(f"{notice}{separator}{block}") == brn.RELEASE_BODY_UTF16_HARD_LIMIT

    with pytest.raises(SystemExit, match="at or over action-gh-release's"):
        brn.assemble_body("- did a thing", block, version)


def test_a_body_still_over_the_hard_limit_after_the_notice_is_refused():
    # Arrange -- force the "always present" block itself to be enormous, so
    # even the notice-plus-block assembly cannot fit. This is the last-resort
    # guard: never rely on the action's own silent truncation.
    huge_block = "## Release assets\n\n" + ("x" * 200_000)
    # Act / Assert
    with pytest.raises(SystemExit, match="over action-gh-release's"):
        brn.assemble_body("- did a thing", huge_block, "0.7.0")


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to parse with")
# On windows-latest, `bash` on PATH can resolve to System32's WSL launcher,
# which has no distribution installed and exits 1 before running a line (tan-cli#1276's
# parity windows 0/4 shard). The step only ever runs on the
# release job's `ubuntu-latest`, and the ubuntu/macos legs still exercise it
# -- the same restriction `tests/test_e2e_linux_freeze_script.py` makes.
@pytest.mark.skipif(sys.platform == "win32", reason="the release step only runs on ubuntu-latest")
@pytest.mark.skipif(
    not _has_gnu_sed(),
    reason="the old step's `sed -i` invocation assumes GNU sed (it only ever ran on "
    "ubuntu-latest); no GNU-compatible sed on this host",
)
def test_a_naive_concatenation_is_what_broke_0_7_0(tmp_path):
    """Pins the OLD, unfixed behaviour this module replaces -- by actually
    RUNNING the old step's own shell text, not by re-implementing its
    concatenation here.

    tan-cli#1276 review (low): an earlier version of this test rebuilt the
    old logic inline (`f"{oversized.strip()}\\n\\n{block}"`, checked only
    against this file's own `_gh_action_truncate`) and called neither
    `build_release_notes.py` nor `release.yml` -- no change to either could
    ever make it fail. This version extracts the OLD "Slice CHANGELOG
    section for the release notes" step's `run:` text verbatim out of
    `release.yml` AS IT READ at `_OLD_RELEASE_YML_COMMIT` (the commit
    immediately before this fix), via `git show <sha>:<path>` (never
    `origin/dev`, which keeps moving -- see that constant), and runs it for
    real under `bash -eo pipefail` against an oversized CHANGELOG section.
    The resulting `release_notes.md` is the step's REAL output; only the
    truncation simulating `action-gh-release` itself is a stand-in, since
    reaching the actual GitHub API is out of scope for a hermetic test.
    """
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{_OLD_RELEASE_YML_COMMIT}^{{commit}}"],
        capture_output=True,
    )
    if probe.returncode != 0:
        # Skip ONLY on a genuinely shallow checkout. On a full-history
        # checkout (ci.yml's `fetch-depth: 0` `python` job) an unreachable pin
        # means the pin itself is wrong, and a skip there would pass silently
        # -- the tan-cli#970 shape.
        shallow = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
        )
        if shallow.stdout.strip() == "true":
            pytest.skip(
                f"{_OLD_RELEASE_YML_COMMIT} is unreachable from this shallow "
                f"checkout (e.g. a `fetch-depth: 1` python-tests-shard leg), "
                f"which has no history to `git show` a pre-#1276 commit out of"
            )
        pytest.fail(
            f"{_OLD_RELEASE_YML_COMMIT} is unreachable from a full-history "
            f"checkout -- the pinned pre-#1276 commit is wrong or was rewritten"
        )

    workflow_text = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{_OLD_RELEASE_YML_COMMIT}:.github/workflows/release.yml"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    steps = yaml.safe_load(workflow_text)["jobs"]["release"]["steps"]
    step = next(
        s for s in steps if s.get("name") == "Slice CHANGELOG section for the release notes"
    )
    run = step["run"]
    assert isinstance(run, str) and run.strip()

    version = "0.7.0"
    oversized = "- an entry\n" * 70_000  # mirrors 0.7.0's measured 770642 chars
    changelog = (
        f"# Changelog\n\n"
        f"## [{version}] -- 2026-09-19\n\n"
        f"{oversized}\n\n"
        f"## [0.6.0] -- 2026-08-04\n\n"
        f"- an older entry\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "glibc-floor.txt").write_text("GLIBC_2.30\n", encoding="utf-8")

    env = dict(os.environ)
    env["GITHUB_REF_NAME"] = f"v{version}"
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, (
        f"STEP EXIT={proc.returncode}\n--- stdout ---\n"
        f"{proc.stdout.decode('utf-8', 'replace')}\n--- stderr ---\n"
        f"{proc.stderr.decode('utf-8', 'replace')}"
    )

    naive_body = (tmp_path / "release_notes.md").read_text(encoding="utf-8")
    # The old step DID write the block into the file -- the defect is not in
    # what it wrote, it is in what survives `action-gh-release`'s own
    # truncation on publish.
    assert "## Release assets" in naive_body
    assert brn.utf16_len(naive_body) > brn.RELEASE_BODY_UTF16_HARD_LIMIT
    truncated = _gh_action_truncate(naive_body)
    assert "## Release assets" not in truncated
