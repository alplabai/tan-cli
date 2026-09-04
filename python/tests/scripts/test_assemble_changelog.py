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

import fnmatch
import importlib.util
import os
import stat
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
    assert ac.main(["--root", str(root), "--write"]) == 0

    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for expected in ("Added one.", "Fixed one.", "Fixed two.", "Security one."):
        assert expected in text, f"{expected} was dropped"

    remaining = [p.name for p in (root / "changelog.d").glob("*.md")]
    assert remaining == [], f"fragments left behind: {remaining}"


def test_existing_entries_survive(tmp_path: Path) -> None:
    """A fragment must never overwrite hand-written text already in the section."""
    root = _repo(tmp_path, {"201.fixed.md": "- **New fixed.**"})
    assert ac.main(["--root", str(root), "--write"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Pre-existing fixed entry." in text
    assert "Pre-existing added entry." in text
    assert "New fixed." in text


def test_released_sections_are_untouched(tmp_path: Path) -> None:
    """Only the Unreleased section may be edited; shipped history is immutable."""
    root = _repo(tmp_path, {"301.fixed.md": "- **New fixed.**"})
    assert ac.main(["--root", str(root), "--write"]) == 0
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
    assert ac.main(["--root", str(root), "--write"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert body in text, "fragment body was altered in transit"


def test_a_bad_category_is_refused_not_dropped(tmp_path: Path) -> None:
    """The whole point: an unusable fragment must STOP the run, not vanish."""
    root = _repo(tmp_path, {"501.nonsense.md": "- **Would be lost.**"})
    assert ac.main(["--root", str(root), "--write"]) == 1
    # And nothing was consumed on the way to failing.
    assert (root / "changelog.d" / "501.nonsense.md").is_file()
    assert "Would be lost." not in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_an_empty_fragment_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"601.fixed.md": ""})
    assert ac.main(["--root", str(root), "--write"]) == 1
    assert (root / "changelog.d" / "601.fixed.md").is_file()


def test_a_whitespace_only_fragment_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"602.fixed.md": "   \n\t\n   "})
    assert ac.main(["--root", str(root), "--write"]) == 1
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
    assert ac.main(["--root", str(root), "--write"]) == 1
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


def test_a_bullet_marker_with_no_content_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`- ` (dash, space, nothing else) technically `startswith("- ")` and
    `.strip()`s to `-`, which is non-empty, so it slipped past both the
    empty-body guard and the old bullet check and folded into CHANGELOG.md as
    a bare, unreadable `- `. Must now be refused."""
    root = _repo(tmp_path, {"784.fixed.md": "- "})
    assert ac.main(["--check", "--root", str(root)]) == 1
    assert "784.fixed.md" in capsys.readouterr().err


def test_a_nested_fence_of_a_different_length_is_not_a_false_positive(
    tmp_path: Path,
) -> None:
    """A legitimate outer fence opened with four backticks may contain a
    literal three-backtick line (e.g. documenting Markdown fence syntax
    itself) without that inner line counting as a close -- CommonMark only
    closes a fence with a marker of at least the opening length. Counting
    every ``` regardless of length previously flagged this as unbalanced."""
    body = (
        "- **Example.**\n\n"
        "  ````\n"
        "  ```\n"
        "  a literal triple-backtick line, still inside the outer fence\n"
        "  ```\n"
        "  ````"
    )
    root = _repo(tmp_path, {"785.fixed.md": body})
    assert ac.main(["--check", "--root", str(root)]) == 0


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


# --- state-independent non-vacuity guard (PR #947 review) ------------------
#
# An earlier version of this file proved the checker isn't vacuous by
# asserting `changelog.d/` in THIS checkout is non-empty. That is exactly
# backwards: `changelog.d/` containing nothing but README.md is a REAL,
# REQUIRED repo state -- it is what the tree looks like right after a release
# fold (tan-cli#892 landed exactly that: `git ls-tree -r --name-only <that
# commit> changelog.d/` -> only README.md), and `release.yml`'s
# `--require-empty` step *demands* it at tag time. Asserting non-emptiness
# there reds the one state the release path is required to reach.
#
# The fix is to stop asking a property of the LIVE DIRECTORY and instead ask
# a property of the CHECKER: drive a small, fixed, in-test corpus (one valid
# fragment, one planted-invalid one) through `load_fragments()` -- the real
# production entry point, not a re-implemented glob, so a typo in its own
# `*.md` glob is caught here too (a broken glob finds neither fragment, so
# the invalid one is never inspected and no error is raised). This holds no
# matter what -- or whether anything -- is pending in the real
# `changelog.d/` at the time the suite runs.


def test_the_930_guard_holds_on_a_fixed_corpus_independent_of_repo_state(
    tmp_path: Path,
) -> None:
    """Non-vacuity, made state-independent: proves `fragment_shape_errors` (via
    the production `load_fragments()` loader) still catches a planted-bad
    fragment, using a corpus this test owns rather than whatever the real
    `changelog.d/` happens to contain right now.
    """
    root = _repo(tmp_path, {
        "9101.fixed.md": "- **A valid entry, shaped like the house style.**",
        "9102.changed.md": "not a bullet at all",
    })
    with pytest.raises(ac.AssembleError) as exc:
        ac.load_fragments(root / "changelog.d")
    assert "9102.changed.md" in str(exc.value)
    assert "9101.fixed.md" not in str(exc.value)


# --- real corpus: prove the check on THIS repo's actual pending fragments --
#
# Everything above is a synthetic fixture. tan-cli#930's own complaint is
# that a check exercised only on green fixtures proves nothing about the real
# corpus -- so this drives the real function, and the real CLI, over the
# actual `changelog.d/` in this checkout. Since `python -m pytest tests -q`
# runs in the required `python-tests-shard` CI job on every PR (parity.yml),
# this is also how a future malformed real fragment gets caught automatically,
# not just at release time via `--require-empty`.
#
# Deliberately NOT asserting `changelog.d/` is non-empty (see above) -- an
# empty-except-README.md tree is correct, not vacuous, and the mutation-proof
# non-vacuity guard lives in the fixed-corpus test above instead. This test's
# job is narrower: IF something real is pending, it must be shape-valid.

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_real_changelog_d_fragments_are_shape_valid() -> None:
    # Drives the production loader (not a re-implemented glob) so a typo in
    # load_fragments()'s own `*.md` glob is caught here too, exactly as it is
    # in the fixed-corpus test above.
    try:
        ac.load_fragments(REPO_ROOT / "changelog.d")
    except ac.AssembleError as exc:
        pytest.fail(str(exc))


def test_check_over_the_real_repo_passes() -> None:
    """End-to-end over the real tree, not `--root <tmp fixture>`."""
    assert ac.main(["--check"]) == 0


def test_missing_unreleased_header_refuses_rather_than_guessing(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"701.fixed.md": "- **Entry.**"},
        changelog="# Changelog\n\n## [0.9.8] — 2026-01-01\n\n### Fixed\n\n- Old.\n",
    )
    assert ac.main(["--root", str(root), "--write"]) == 1
    assert (root / "changelog.d" / "701.fixed.md").is_file()


def test_require_empty_is_a_real_gate(tmp_path: Path) -> None:
    """--require-empty must FAIL while fragments remain, or it gates nothing."""
    root = _repo(tmp_path, {"801.fixed.md": "- **Entry.**"})
    assert ac.main(["--root", str(root), "--require-empty"]) == 1
    # Fold them, then the same gate must pass -- proving it tracks real state
    # rather than always failing.
    assert ac.main(["--root", str(root), "--write"]) == 0
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
    assert ac.main(["--root", str(root), "--write"]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# tan-cli#1172 -- the fold is opt-in. These are data-loss controls, not
# behaviour tests: what they pin is that the IRREVERSIBLE half cannot be
# reached by typing the script's name.
# ---------------------------------------------------------------------------
def test_a_bare_invocation_folds_nothing_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    """The defect this closes: the fold used to be the DEFAULT.

    A bare run rewrote CHANGELOG.md and deleted every fragment, with no
    prompt, exit 0, and a summary that reads like success -- and the next
    `--check` then reported `0 fragment(s) pending`, which reads as "nothing
    to do" rather than "everything is gone". It cost 157 fragments once,
    recovered only because they happened to be tracked with a clean index at
    that moment. An untracked or staged-but-uncommitted fragment -- exactly
    the state you are in while DRAFTING one, which is exactly when you would
    reach for this script to see how it renders -- would have been gone.
    """
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    assert ac.main(["--root", str(root)]) == 0

    assert (root / "changelog.d" / "1.added.md").exists(), (
        "a bare invocation deleted a fragment"
    )
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before, (
        "a bare invocation rewrote CHANGELOG.md"
    )


def test_a_bare_invocation_still_renders_so_the_safe_intent_is_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Making the fold opt-in must not make INSPECTION harder: rendering is
    the common interactive reason to run this at all."""
    root = _repo(tmp_path, {"1.added.md": "- A distinctive fragment body.\n"})
    assert ac.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    assert "A distinctive fragment body." in captured.out
    assert "--write" in captured.err, (
        "a bare run must name the flag that would have folded, or the reader "
        "cannot tell inspection from a no-op"
    )


def test_write_still_folds_and_deletes(tmp_path: Path) -> None:
    """The other direction: `--write` must still do the whole job, or this
    change has traded a data-loss bug for a release-blocking one."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    assert ac.main(["--root", str(root), "--write"]) == 0
    assert not (root / "changelog.d" / "1.added.md").exists()
    assert "Added a thing." in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_dry_run_is_silent_where_a_bare_run_nudges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` is an EXPLICIT request to render, so it gets no nudge. The
    nudge exists for the person who typed the bare name expecting a fold."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    assert ac.main(["--root", str(root), "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "Added a thing." in captured.out
    assert captured.err == ""
    assert (root / "changelog.d" / "1.added.md").exists()


def test_the_require_empty_error_names_the_flag_that_actually_folds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message a release engineer sees on a red tag has to name a command
    that works. It used to say `assemble_changelog.py` with no flag, which
    after this change reports and exits 0 without folding -- leaving them to
    run it, see success, and find the fragments still pending."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    assert ac.main(["--root", str(root), "--require-empty"]) == 1
    assert "--write" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# tan-cli#1181 -- no FLAG COMBINATION can reach the irreversible half either.
#
# The controls above pin that the bare NAME cannot fold. Nothing pinned the
# combinations, and one of them was live: `--dry-run --write` performed the
# fold, exit 0, silently -- measured on the real tree at PR #1181's head, 163
# fragments down to 1 (README.md) with no warning -- from an invocation whose
# `--dry-run` half is documented as "write nothing" and is advertised by
# `changelog.d/README.md` as the safe look-first form. `--check --write` was
# safe, but only by accident of `--check` being handled first, so the
# precedence was inconsistent in exactly the direction that loses data.
#
# One test per refused pairing, deliberately not parametrised into one: each
# pairing is a separate way to lose 163 uncommitted files, and a parametrised
# id is easier to silently narrow than a named test is to delete.
# ---------------------------------------------------------------------------
def _assert_nothing_happened(root: Path, before: str) -> None:
    assert (root / "changelog.d" / "1.added.md").exists(), (
        "a refused invocation deleted a fragment anyway"
    )
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before, (
        "a refused invocation rewrote CHANGELOG.md anyway"
    )


def test_dry_run_plus_write_is_refused_not_folded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The measured defect: `--dry-run --write` folded and deleted, exit 0."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(["--root", str(root), "--dry-run", "--write"]) == 2
    _assert_nothing_happened(root, before)
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--write" in err


def test_check_plus_write_is_refused_not_silently_reduced_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--check --write` never folded, but it reported and exited 0 as if the
    `--write` had not been typed. That is the same inconsistency from the
    harmless side: an operator who learns `--check --write` is fine has learned
    the wrong lesson about `--dry-run --write`."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(["--root", str(root), "--check", "--write"]) == 2
    _assert_nothing_happened(root, before)
    assert "--check" in capsys.readouterr().err


def test_require_empty_plus_write_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--require-empty` is the release GATE. Pairing it with the fold would
    make the gate step itself destructive if a `--write` ever leaked into
    `release.yml`'s invocation."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(["--root", str(root), "--require-empty", "--write"]) == 2
    _assert_nothing_happened(root, before)
    assert "--require-empty" in capsys.readouterr().err


def test_all_three_at_once_is_refused_and_every_flag_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must name each conflicting flag it saw, not just the first
    one -- an operator who removes only the flag in the message and re-runs
    must not land on a second silently-destructive combination."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ac.main(
        ["--root", str(root), "--check", "--require-empty", "--dry-run", "--write"]
    ) == 2
    _assert_nothing_happened(root, before)
    err = capsys.readouterr().err
    for flag in ("--check", "--require-empty", "--dry-run"):
        assert flag in err, f"{flag} was not named in the refusal"


def test_the_refusal_names_a_command_that_actually_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same bar as `--require-empty`'s error (tan-cli#1172): a refusal that
    does not say what to run instead just gets the flag deleted at random."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    assert ac.main(["--root", str(root), "--dry-run", "--write"]) == 2
    err = capsys.readouterr().err
    assert "assemble_changelog.py --dry-run" in err
    assert "assemble_changelog.py --write" in err


def test_the_safe_flags_still_combine_with_each_other(tmp_path: Path) -> None:
    """The refusal must be about `--write` specifically, not about "more than
    one flag". `--check --dry-run` and `--check --require-empty` change
    nothing whichever wins, so refusing them would be a gratuitous break."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    assert ac.main(["--root", str(root), "--check", "--dry-run"]) == 0
    assert ac.main(["--root", str(root), "--check", "--require-empty"]) == 1
    assert (root / "changelog.d" / "1.added.md").exists()


# ---------------------------------------------------------------------------
# tan-cli#1181 -- the fold's two failure windows.
# ---------------------------------------------------------------------------
def test_a_failed_changelog_write_leaves_both_sides_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHANGELOG.md is written via a temp file + `os.replace`, so a failure
    mid-write cannot leave it truncated with the fragments already gone. The
    old truncate-then-write could: `write_text` opens with "w", which empties
    the file before a single byte of the new text lands."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ac.os, "replace", boom)
    assert ac.main(["--root", str(root), "--write"]) == 1

    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before, (
        "a failed write truncated CHANGELOG.md"
    )
    assert (root / "changelog.d" / "1.added.md").exists(), (
        "a fragment was deleted despite the write never landing"
    )
    assert not list(root.glob("*.tan-tmp")), "temp file left behind"
    assert not (root / "CHANGELOG.md.tmp").exists(), (
        "the old, un-gitignored temp name is back"
    )
    assert "No space left on device" in capsys.readouterr().err


def test_an_unlink_failure_reports_the_survivors_instead_of_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window that DOES survive: CHANGELOG.md folded, fragments still on
    disk. Re-running `--write` there splices every survivor a second time --
    forced by monkeypatching `Path.unlink` to raise, the measured result was
    the same entry appearing twice in CHANGELOG.md. The fold cannot undo the
    write, so the contract is that it must not report success: exit nonzero
    and name the survivors, so the operator deletes them by hand rather than
    re-running into a double fold."""
    root = _repo(tmp_path, {"1.added.md": "- Added a thing.\n"})
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent.name == "changelog.d":
            raise PermissionError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse)
    assert ac.main(["--root", str(root), "--write"]) == 1, (
        "a fold that could not delete its fragments reported success"
    )

    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Added a thing." in text, "the write itself should have landed"
    assert (root / "changelog.d" / "1.added.md").exists()
    err = capsys.readouterr().err
    assert "1.added.md" in err, "the survivor was not named"
    assert "second time" in err, "the double-fold hazard was not stated"


def test_a_partial_unlink_failure_names_exactly_the_survivors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test above refuses EVERY unlink, so it pins only all-or-nothing. A
    PARTIAL failure is the branch a re-run double-folds from, and its contract
    is stricter: the named set must equal the on-disk set exactly, each with
    its own errno, or the operator cleans up the wrong files. Measured on the
    real 162-fragment corpus via `LD_PRELOAD` (no monkeypatching), 1-of-162
    and 45-of-162 both behaved this way."""
    root = _repo(
        tmp_path,
        {
            "1.added.md": "- **First.**",
            "2.added.md": "- **Second.**",
            "3.fixed.md": "- **Third.**",
        },
    )
    real_unlink = Path.unlink
    refused = {"1.added.md", "3.fixed.md"}

    def partial(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent.name == "changelog.d" and self.name in refused:
            raise PermissionError(1, "Operation not permitted")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", partial)
    assert ac.main(["--root", str(root), "--write"]) == 1
    assert {q.name for q in (root / "changelog.d").glob("*.md")} == refused
    err = capsys.readouterr().err
    assert "2 of 3 fragment(s) could not be deleted" in err
    for name in sorted(refused):
        assert f"{name}: [Errno 1] Operation not permitted" in err, name
    assert "2.added.md" not in err, "a deleted fragment was named a survivor"


def test_a_re_run_on_a_folded_plus_survivors_tree_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running `--write` on the state the test above leaves behind used to
    splice each survivor a SECOND time and exit 0 -- measured on the real
    corpus, `folded 1 fragment(s) into CHANGELOG.md` while that entry's lead
    sentence went 1 -> 2. It must refuse, and change nothing while refusing."""
    root = _repo(tmp_path, {"1.added.md": "- **First.**", "2.added.md": "- **Second.**"})
    real_unlink = Path.unlink

    def partial(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "1.added.md":
            raise PermissionError(1, "Operation not permitted")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", partial)
    assert ac.main(["--root", str(root), "--write"]) == 1
    capsys.readouterr()
    monkeypatch.undo()

    folded = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert folded.count("**First.**") == 1
    assert ac.main(["--root", str(root), "--write"]) == 1, "the re-run double-folded"
    err = capsys.readouterr().err
    assert "1 fragment(s) are ALREADY present" in err
    assert "1.added.md" in err
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == folded, (
        "the refused re-run still rewrote CHANGELOG.md"
    )
    assert (root / "changelog.d" / "1.added.md").is_file(), (
        "the refused re-run deleted the survivor it declined to fold"
    )


def test_the_guard_also_refuses_the_render_not_only_the_fold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare run and `--dry-run` on that tree would otherwise print a
    CHANGELOG with the entry twice and call it the result."""
    root = _repo(tmp_path, {"1.added.md": "- **Pre-existing added entry.**"})
    assert ac.main(["--root", str(root)]) == 1
    assert ac.main(["--root", str(root), "--dry-run"]) == 1
    assert "ALREADY present" in capsys.readouterr().err


def test_the_guard_does_not_fire_on_this_repos_real_pending_fragments() -> None:
    """The false-positive control, over the real corpus, not a fixture.
    `splice()` copies bodies byte-for-byte, so `body in <section>` is an exact
    already-folded test, not a similarity heuristic -- 0 hits across all 162
    fragments pending here. A guard that fired would block every fold."""
    buckets = ac.load_fragments(REPO_ROOT / "changelog.d")
    lines = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start, end = ac.find_unreleased(lines)
    assert ac.already_folded(lines[start + 1:end], buckets) == []


# ---------------------------------------------------------------------------
# tan-cli#1181 -- the fold lands on the right inode, durably, in a temp file
# `.gitignore` covers.
# ---------------------------------------------------------------------------
def test_a_symlinked_changelog_is_written_through_not_clobbered(tmp_path: Path) -> None:
    """`os.replace` targeting the LINK replaces it with a regular file: the
    fold lands on the wrong inode, the real file keeps its old bytes, the
    fragments are deleted anyway, and the run exits 0 reporting success.
    Measured on a copy of this repo before the fix -- `real/CHANGELOG.md`
    unchanged at md5 4cc00bd3446d5718b8eabb27b50d1744, the repo-root path a
    new 1261455-byte regular file, 162 fragments gone. `Path.write_text`, the
    call the temp replaced, followed the link; so must this."""
    root = _repo(tmp_path, {"1.added.md": "- **Entry that must reach the real file.**"})
    real_dir = root / "real"
    real_dir.mkdir()
    real = real_dir / "CHANGELOG.md"
    (root / "CHANGELOG.md").rename(real)
    (root / "CHANGELOG.md").symlink_to(Path("real") / "CHANGELOG.md")

    assert ac.main(["--root", str(root), "--write"]) == 0
    assert (root / "CHANGELOG.md").is_symlink(), "the symlink was replaced by a file"
    assert "Entry that must reach the real file." in real.read_text(encoding="utf-8"), (
        "the fold landed on the wrong inode; the real file was never updated"
    )
    assert not list(root.glob("*.tan-tmp")), "a temp was left beside the LINK"
    assert not list(real_dir.glob("*.tan-tmp")), "a temp was left beside the real file"


def test_the_rename_is_fsynced_too_not_only_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.fsync` on the temp makes the BYTES durable, not the RENAME. A power
    cut in between leaves exactly the state the survivor report calls
    impossible: CHANGELOG.md at its old content with the fragments gone. So
    the parent directory is fsynced after the replace too, in that order."""
    if os.name == "nt":  # pragma: no cover - no directory handle to fsync
        pytest.skip("Windows journals the rename itself; there is no dir fd")
    root = _repo(tmp_path, {"1.added.md": "- **Entry.**"})
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def rec_fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        real_fsync(fd)

    def rec_replace(src: object, dst: object) -> None:
        events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(ac.os, "fsync", rec_fsync)
    monkeypatch.setattr(ac.os, "replace", rec_replace)
    assert ac.main(["--root", str(root), "--write"]) == 0
    assert events == ["fsync-file", "replace", "fsync-dir"], events


def test_the_temp_file_is_one_gitignore_already_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CHANGELOG.md.tmp` matched no `.gitignore` rule: a SIGKILL mid-fsync
    left a 1261455-byte untracked, committable file in the repo ROOT
    (`?? CHANGELOG.md.tmp`), which a later run then truncated without a word.
    `.gitignore:53-69` pins `*.tan-tmp` for the two other producers."""
    root = _repo(tmp_path, {"1.added.md": "- **Entry.**"})
    seen: list[str] = []
    real_replace = os.replace

    def rec(src: object, dst: object) -> None:
        seen.append(Path(str(src)).name)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(ac.os, "replace", rec)
    assert ac.main(["--root", str(root), "--write"]) == 0
    assert len(seen) == 1, seen
    patterns = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert any(fnmatch.fnmatch(seen[0], pat) for pat in patterns), (
        f"the fold's temp {seen[0]!r} is matched by no .gitignore rule"
    )


def test_a_root_missing_its_changelog_gets_the_scripts_own_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`repo_root` settles only on a directory holding BOTH CHANGELOG.md and
    changelog.d/; `--root` bypassed that, so this died with a raw
    FileNotFoundError traceback, not the script's `error: ...` contract."""
    root = tmp_path / "half"
    (root / "changelog.d").mkdir(parents=True)
    (root / "changelog.d" / "1.added.md").write_text("- **Entry.**", encoding="utf-8")
    assert ac.main(["--root", str(root), "--write"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: "), err
    assert "does not contain both CHANGELOG.md and changelog.d/" in err
    assert (root / "changelog.d" / "1.added.md").is_file()
