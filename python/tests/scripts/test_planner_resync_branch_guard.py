# SPDX-License-Identifier: Apache-2.0
"""`python/scripts/planner_resync_branch_guard.py` -- tan-cli#1002.

Every scenario below is built out of a REAL bare "origin" and a real working
clone, exercised with real `git` subprocesses -- not mocks -- because the
defect this guard exists to close (PR #996's destroyed hand-port work) was
itself a real-git-plumbing bug (`git checkout -B` + `git push -f` discarding
history a mock would never have modelled faithfully). The four cases mirror
tan-cli#1002's own required probes verbatim:

* a branch with only automation commits -> force-push proceeds (exit 0)
* a branch with one human commit on top -> diverts, non-zero, naming the
  commit it protected (exit 1)
* a branch where the human commit is *behind* the automation tip -> still
  protected (exit 1, full-range scan, not tip-only)
* the branch-per-ref diversion path actually produces a distinct branch and
  names the protected commit accurately

Plus the two failure-mode guards the module's own docstring calls out:
refusing to guess when a branch's existence can't be determined, and
refusing to run forever hunting for a free name.

PR #1006's review added a fifth probe (a `major` finding, not read off the
existing four): the identity check keyed on AUTHOR alone, so a human who
folds hand-port work into the automation's own commit -- `git commit
--amend`, or an `--autosquash` `fixup!` rebase -- left the resulting
commit's author as the automation and its committer as the human, and the
pre-fix guard called it safe. `test_probe_amend_onto_automation_commit_is_protected`
and `test_probe_autosquash_fixup_onto_automation_commit_is_protected` drive
both real shapes end to end; `test_probe_automation_committed_replay_of_human_authored_commit_is_protected`
is the mirror image, proving the AUTHOR half is still load-bearing on its
own (author=human, committer=automation) so neither half of the check can be
dropped without a probe going red.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "python" / "scripts" / "planner_resync_branch_guard.py"

AUTOMATION_NAME = "alp-sdk planner re-sync"
AUTOMATION_EMAIL = "noreply@alplab.ai"
HUMAN_NAME = "A Human Reviewer"
HUMAN_EMAIL = "human@example.com"


def _load():
    spec = importlib.util.spec_from_file_location("planner_resync_branch_guard", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load()


# --------------------------------------------------------------- git helpers


def _run(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True
    )
    return proc.stdout.decode("utf-8", "replace")


def _commit(root: pathlib.Path, name: str, email: str, message: str, filename: str) -> str:
    (root / filename).write_text(f"{message}\n", encoding="utf-8")
    _run(root, "add", filename)
    _run(
        root,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _run(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A working clone with `origin` pointed at a real bare repo, `dev`
    checked out and pushed -- the same shape `planner-resync.yml`'s own
    "Check out tan-cli dev" step leaves behind."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q")
    _run(work, "remote", "add", "origin", str(bare))
    _run(work, "checkout", "-q", "-b", "dev")
    _commit(work, "seed", "seed@example.com", "seed dev", "seed.txt")
    _run(work, "push", "-q", "origin", "dev")
    return work


def _push_branch_from_dev(repo: pathlib.Path, branch: str) -> None:
    _run(repo, "checkout", "-q", "-B", branch, "dev")


# ------------------------------------------------------------------- probes


def test_no_remote_branch_yet_is_safe_to_push(repo: pathlib.Path):
    """First-ever run: `auto/planner-resync` does not exist on origin at
    all. Must proceed with the primary name, not invent a diversion."""
    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "deadbeef", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.branch == "auto/planner-resync"
    assert decision.diverted is False
    assert decision.protected is None
    # PR #1006 review (minor, force-with-lease): empty when the branch
    # doesn't exist on origin yet -- `--force-with-lease=<branch>:` (empty
    # expect) is the documented spelling for "the ref must not already
    # exist".
    assert decision.observed_tip == ""


def test_observed_tip_names_the_exact_sha_the_guard_approved(repo: pathlib.Path):
    """PR #1006 review (minor): the guard must hand back the sha it actually
    inspected on `origin/<branch>`, so the caller's `git push
    --force-with-lease` is atomic with this check rather than merely
    advisory -- a commit landing on `origin/<branch>` between this call and
    the push must be caught by `git push` itself, not silently overwritten."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    tip = _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "deadbeef", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is False
    assert decision.observed_tip == tip

    # A commit landing on origin AFTER the guard's read (the TOCTOU window
    # this fix closes) must not be silently reflected in an already-returned
    # decision -- `observed_tip` is a snapshot, not a live query.
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "raced in after the guard's read", "race.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")
    assert decision.observed_tip == tip, "the earlier decision must not mutate"


def test_probe_only_automation_commits_force_push_proceeds(repo: pathlib.Path):
    """tan-cli#1002 probe 1: a branch with only automation commits ->
    force-push proceeds."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "deadbeef", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.branch == "auto/planner-resync"
    assert decision.diverted is False
    assert decision.protected is None

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "deadbeef",
        ]
    )
    assert rc == 0


def test_probe_human_commit_on_top_diverts_naming_the_protected_commit(repo: pathlib.Path):
    """tan-cli#1002 probe 2: a branch with one human commit on top ->
    refuses/diverts, non-zero, naming the commit it protected. This is the
    EXACT shape of what destroyed PR #996's hand-port work."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    human_sha = _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "hand-port find_template_by_cores", "handport.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is True
    assert decision.branch == "auto/planner-resync-cafef00d"
    assert decision.protected is not None
    assert decision.protected.sha == human_sha
    assert decision.protected.author_email == HUMAN_EMAIL
    assert "hand-port find_template_by_cores" == decision.protected.subject

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "cafef00d",
        ]
    )
    assert rc == 1, "a protected branch must fail the step, not pass it"


def test_probe_human_commit_behind_automation_tip_still_protected(repo: pathlib.Path):
    """tan-cli#1002 probe 3: a branch where the human commit is *behind* the
    automation tip -> still protected. A tip-only check would see the
    automation-authored tip and wave the whole branch through, silently
    discarding the buried human commit -- exactly what a naive fix would
    still get wrong."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    human_sha = _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "hand-port find_template_by_cores", "handport.txt")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose newer re-sync", "resync2.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "0ff1ce00", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is True
    assert decision.protected is not None
    assert decision.protected.sha == human_sha
    assert decision.protected.author_email == HUMAN_EMAIL


def test_probe_amend_onto_automation_commit_is_protected(repo: pathlib.Path):
    """PR #1006 review (major): `git commit --amend` folds a human's
    hand-port work into the automation's own commit -- the resulting commit
    KEEPS the automation's author identity (amend does not change it unless
    `--reset-author` is passed) and changes only the COMMITTER to whoever
    ran the amend. Reproduces the exact repro from the review comment
    (a 1096-line hand-port amended onto the machine commit), driven with a
    real `git commit --amend`, not constructed by hand.

    An author-only check (the pre-fix shape) reads this commit's author as
    the automation and calls it safe -- this is the shape that let PR #996's
    force-push destroy 1096 insertions across 15 files."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    (repo / "handport.txt").write_text("hand-ported line\n" * 1096, encoding="utf-8")
    _run(repo, "add", "handport.txt")
    _run(
        repo,
        "-c",
        f"user.name={HUMAN_NAME}",
        "-c",
        f"user.email={HUMAN_EMAIL}",
        "commit",
        "-q",
        "--amend",
        "--no-edit",
    )
    amended_sha = _run(repo, "rev-parse", "HEAD").strip()
    # Confirm the shape actually landed as intended before trusting the
    # guard's verdict on it -- author stays the automation, committer is the
    # human who ran the amend.
    author = _run(repo, "log", "-1", "--format=%an%x1f%ae").strip().split("\x1f")
    committer = _run(repo, "log", "-1", "--format=%cn%x1f%ce").strip().split("\x1f")
    assert author == [AUTOMATION_NAME, AUTOMATION_EMAIL]
    assert committer == [HUMAN_NAME, HUMAN_EMAIL]
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is True
    assert decision.protected is not None
    assert decision.protected.sha == amended_sha
    assert decision.protected.committer_email == HUMAN_EMAIL

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "cafef00d",
        ]
    )
    assert rc == 1, "an amended-in human commit must fail the step, not pass it"


def test_probe_autosquash_fixup_onto_automation_commit_is_protected(repo: pathlib.Path):
    """PR #1006 review (major): a human's `fixup!` commit, folded into the
    automation's commit by `git rebase --autosquash`, produces the identical
    shape as `--amend` -- author stays the automation's, committer becomes
    whoever ran the rebase. Driven with a real `git rebase --autosquash`."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "fixup! propose re-sync", "handport.txt")
    _run(
        repo,
        "-c",
        f"user.name={HUMAN_NAME}",
        "-c",
        f"user.email={HUMAN_EMAIL}",
        "-c",
        "sequence.editor=true",
        "rebase",
        "-q",
        "-i",
        "--autosquash",
        "dev",
    )
    fixed_sha = _run(repo, "rev-parse", "HEAD").strip()
    author = _run(repo, "log", "-1", "--format=%an%x1f%ae").strip().split("\x1f")
    committer = _run(repo, "log", "-1", "--format=%cn%x1f%ce").strip().split("\x1f")
    assert author == [AUTOMATION_NAME, AUTOMATION_EMAIL]
    assert committer == [HUMAN_NAME, HUMAN_EMAIL]
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is True
    assert decision.protected is not None
    assert decision.protected.sha == fixed_sha


def test_probe_automation_committed_replay_of_human_authored_commit_is_protected(
    repo: pathlib.Path,
):
    """Mirror of the two probes above, proving the AUTHOR half of the check
    still matters and was not made redundant by adding the committer half:
    author=human, committer=automation (e.g. the automation's own machinery
    replaying a human-authored change under its own committer identity).
    Dropping the author check -- checking committer only -- would read this
    commit as safe."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _run(
        repo,
        "-c",
        f"user.name={AUTOMATION_NAME}",
        "-c",
        f"user.email={AUTOMATION_EMAIL}",
        "commit",
        "-q",
        "--allow-empty",
        "--author",
        f"{HUMAN_NAME} <{HUMAN_EMAIL}>",
        "-m",
        "hand-port replayed under the automation's committer identity",
    )
    replayed_sha = _run(repo, "rev-parse", "HEAD").strip()
    author = _run(repo, "log", "-1", "--format=%an%x1f%ae").strip().split("\x1f")
    committer = _run(repo, "log", "-1", "--format=%cn%x1f%ce").strip().split("\x1f")
    assert author == [HUMAN_NAME, HUMAN_EMAIL]
    assert committer == [AUTOMATION_NAME, AUTOMATION_EMAIL]
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.diverted is True
    assert decision.protected is not None
    assert decision.protected.sha == replayed_sha


def test_diversion_target_is_itself_reprotected_if_also_occupied(repo: pathlib.Path):
    """The escape hatch is not trusted blindly: if `<branch>-<suffix>` is
    ALSO occupied by someone else's work, the guard must keep looking rather
    than force-push over that too."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "hand-port on the primary branch", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    _push_branch_from_dev(repo, "auto/planner-resync-cafef00d")
    _commit(repo, "Another Human", "other@example.com", "unrelated work on the divert name", "b.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync-cafef00d")

    decision = guard.decide_branch(
        repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert decision.branch == "auto/planner-resync-cafef00d-2"
    assert decision.diverted is True
    # The reported protected commit is the one on the PRIMARY branch -- the
    # finding worth surfacing, regardless of how many names it took to land
    # somewhere safe.
    assert decision.protected is not None
    assert decision.protected.author_email == HUMAN_EMAIL
    assert decision.candidates_tried == (
        "auto/planner-resync",
        "auto/planner-resync-cafef00d",
        "auto/planner-resync-cafef00d-2",
    )
    # PR #1006 review (minor, cascaded diversion): BOTH occupied candidates
    # must be reported, not just the primary one `protected` already names --
    # a reader told about only the first has no way to know the diversion
    # target it's about to be pointed at is ALSO holding someone else's work.
    assert [oc.branch for oc in decision.occupied] == [
        "auto/planner-resync",
        "auto/planner-resync-cafef00d",
    ]
    assert decision.occupied[0].commit.author_email == HUMAN_EMAIL
    assert decision.occupied[1].commit.author_email == "other@example.com"


def test_cascaded_diversion_reports_every_occupied_candidate_in_github_output(
    repo: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Driven end to end against the CLI, matching PR #1006's review repro:
    a hand-port left on `auto/planner-resync` by one run, and a second,
    unrelated hand-port left on that run's own diversion target by a later
    run. A THIRD run must report both occupied branches in $GITHUB_OUTPUT --
    reporting only `protected_commit` (the primary branch's finding) would
    leave `auto/planner-resync-cafef00d`'s own foreign commit unmentioned."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    primary_sha = _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "hand-port on the primary branch", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    _push_branch_from_dev(repo, "auto/planner-resync-cafef00d")
    diverted_sha = _commit(
        repo, "Another Human", "other@example.com", "unrelated work on the divert name", "b.txt"
    )
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync-cafef00d")

    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "cafef00d",
        ]
    )
    assert rc == 1
    text = out.read_text(encoding="utf-8")
    assert "branch=auto/planner-resync-cafef00d-2" in text
    assert "occupied_count=2" in text
    assert "occupied_1_branch=auto/planner-resync" in text
    assert f"occupied_1_commit={primary_sha}" in text
    assert f"occupied_1_author={HUMAN_NAME} <{HUMAN_EMAIL}>" in text
    assert "occupied_2_branch=auto/planner-resync-cafef00d" in text
    assert f"occupied_2_commit={diverted_sha}" in text
    assert "occupied_2_author=Another Human <other@example.com>" in text
    # tan-cli#1109 review round 2 (major): the consolidated list
    # `planner-resync.yml`'s close-superseded step actually reads --
    # both occupied branches, space-separated, in the SAME order as the
    # numbered keys above.
    assert (
        "occupied_branches=auto/planner-resync auto/planner-resync-cafef00d\n"
        in text
    )


def test_cli_writes_github_output(repo: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "hand-port work", "handport.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "abc12345",
        ]
    )
    assert rc == 1
    text = out.read_text(encoding="utf-8")
    assert "branch=auto/planner-resync-abc12345" in text
    assert "diverted=true" in text
    assert "protected_commit_subject=hand-port work" in text
    assert f"protected_commit_author={HUMAN_NAME} <{HUMAN_EMAIL}>" in text
    assert f"protected_commit_committer={HUMAN_NAME} <{HUMAN_EMAIL}>" in text
    # The diverted name doesn't exist on origin yet -- empty, matching
    # `--force-with-lease=<branch>:` (empty <expect>) for "must not exist".
    assert "observed_tip=\n" in text
    # tan-cli#1109 review round 2 (major): the single occupied branch,
    # by itself (no trailing siblings), same reason as the cascaded case
    # above.
    assert "occupied_branches=auto/planner-resync\n" in text


def test_occupied_branches_is_empty_and_unconditional_when_nothing_is_occupied(
    repo: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """tan-cli#1109 review round 2 (major): the everyday, non-diverted run --
    `occupied_branches=` must still be WRITTEN (empty), same shape as
    `observed_tip=`/`occupied_count=` already use, so a caller's `grep '^…='`
    under `set -euo pipefail` never dies on a missing key (tan-cli#1006's own
    `protected_commit=` incident, one level up)."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "propose re-sync", "resync.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "abc12345",
        ]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "occupied_count=0\n" in text
    assert "occupied_branches=\n" in text


def test_forged_commit_with_separator_embedded_in_author_name_is_refused(
    repo: pathlib.Path,
):
    """PR #1006 review (nit): `_LOG_SEP` (`\\x1f`) trusts a real git porcelain
    guarantee that it never appears inside a field -- true of everything real
    `git commit`/`--amend`/rebase produce, but not of a hand-forged commit
    object (`git hash-object -t commit -w --stdin`, exactly as driven in the
    review). Plant `\\x1f` inside the author name so the bounded
    `split(_LOG_SEP, 5)` this used to be would shift every field after it
    (crafted right, both identity pairs read as the automation and the
    pre-fix guard returns rc 0) and confirm the unbounded split + field-count
    check instead REFUSES the record -- fail-closed, the same posture this
    module already takes for an ambiguous `git log`/`ls-remote` failure,
    rather than silently misparsing it into a false "safe"."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    tree = _run(repo, "rev-parse", "HEAD^{tree}").strip()
    parent = _run(repo, "rev-parse", "HEAD").strip()
    poisoned_name = f"{AUTOMATION_NAME}\x1f{HUMAN_NAME}"
    commit_text = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        f"author {poisoned_name} <{AUTOMATION_EMAIL}> 1700000000 +0000\n"
        f"committer {poisoned_name} <{AUTOMATION_EMAIL}> 1700000000 +0000\n"
        "\n"
        "forged: separator planted inside the author/committer name\n"
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=commit_text.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    forged_sha = proc.stdout.decode("utf-8").strip()
    _run(repo, "update-ref", "refs/heads/auto/planner-resync", forged_sha)
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    with pytest.raises(guard.BranchGuardError):
        guard.decide_branch(
            repo, "dev", "auto/planner-resync", "cafef00d", AUTOMATION_NAME, AUTOMATION_EMAIL
        )

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "cafef00d",
        ]
    )
    assert rc == 2, "an unparseable commit record must be refused, not read as safe"


def test_refuses_rather_than_guessing_when_existence_cannot_be_determined(
    repo: pathlib.Path, tmp_path: pathlib.Path
):
    """A `git ls-remote` failure that is NOT "no matching refs" (here: no
    `origin` remote configured at all) must not be read as "the branch does
    not exist" -- that misread is exactly how a transient failure would
    reintroduce tan-cli#1002 under a different name."""
    orphan = tmp_path / "no_origin"
    orphan.mkdir()
    _run(orphan, "init", "-q")
    with pytest.raises(guard.BranchGuardError):
        guard.decide_branch(
            orphan, "dev", "auto/planner-resync", "deadbeef", AUTOMATION_NAME, AUTOMATION_EMAIL
        )


def test_main_returns_2_when_existence_cannot_be_determined(tmp_path: pathlib.Path):
    orphan = tmp_path / "no_origin"
    orphan.mkdir()
    _run(orphan, "init", "-q")
    rc = guard.main(
        [
            "--repo-root",
            str(orphan),
            "--branch",
            "auto/planner-resync",
            "--divert-suffix",
            "deadbeef",
        ]
    )
    assert rc == 2


def test_gives_up_after_max_attempts_rather_than_looping_forever(repo: pathlib.Path):
    """Bounded refusal (module docstring: "WHAT 'SAFE' MEANS FOR THE ESCAPE
    HATCH ITSELF"), probed with `max_attempts=1` so the test does not need to
    actually occupy fifty branch names to exercise it."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "occupies the only branch this test permits", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    with pytest.raises(guard.BranchGuardError):
        guard.decide_branch(
            repo,
            "dev",
            "auto/planner-resync",
            "deadbeef",
            AUTOMATION_NAME,
            AUTOMATION_EMAIL,
            max_attempts=1,
        )


# -------------------------------------------- tan-cli#1119: --check-branch


def test_branch_currently_occupied_reports_existed_false_when_the_branch_does_not_exist(
    repo: pathlib.Path,
):
    """Mirrors what `decide_branch` treats as "safe" for a candidate it
    tries itself: a branch that has never been pushed at all is not
    occupied -- nothing to protect. tan-cli#1119 review (minor):
    `existed=False` distinctly, not collapsed into the same shape a genuinely
    clean existing branch reports."""
    occupancy = guard.branch_currently_occupied(
        repo, "dev", "auto/planner-resync-neverexisted", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert occupancy.existed is False
    assert occupancy.foreign is None
    assert occupancy.occupied is False


def test_branch_currently_occupied_reports_existed_true_for_an_automation_only_branch(
    repo: pathlib.Path,
):
    """A branch that exists but carries only the automation's own commits
    (a prior run's own push) is not occupied -- and, unlike the
    never-existed case above, `existed` says so is True: this guard actually
    looked and found it clean, not merely absent."""
    _push_branch_from_dev(repo, "auto/planner-resync")
    _commit(repo, AUTOMATION_NAME, AUTOMATION_EMAIL, "prior run's own commit", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync")

    occupancy = guard.branch_currently_occupied(
        repo, "dev", "auto/planner-resync", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert occupancy.existed is True
    assert occupancy.foreign is None
    assert occupancy.occupied is False


def test_branch_currently_occupied_finds_a_foreign_commit_on_a_branch_this_run_never_named(
    repo: pathlib.Path,
):
    """tan-cli#1119's whole point: `decide_branch` never tries
    `auto/planner-resync-eaa79695` in a run whose own divert suffix is
    something else entirely -- `branch_currently_occupied` still answers
    correctly about it, because it does not depend on `decide_branch` having
    tried the name."""
    _push_branch_from_dev(repo, "auto/planner-resync-eaa79695")
    sha = _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "a human adopted this old name", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync-eaa79695")

    occupancy = guard.branch_currently_occupied(
        repo, "dev", "auto/planner-resync-eaa79695", AUTOMATION_NAME, AUTOMATION_EMAIL
    )
    assert occupancy.existed is True
    assert occupancy.occupied is True
    commit = occupancy.foreign
    assert commit is not None
    assert commit.sha == sha
    assert commit.author_name == HUMAN_NAME


def test_branch_currently_occupied_raises_rather_than_guessing_when_it_cannot_tell(
    tmp_path: pathlib.Path,
):
    """Same fail-closed contract as `decide_branch`'s own existence check --
    an ambiguous `git ls-remote` failure (no `origin` configured) must not
    be read as "not occupied"."""
    orphan = tmp_path / "no_origin"
    orphan.mkdir()
    _run(orphan, "init", "-q")
    with pytest.raises(guard.BranchGuardError):
        guard.branch_currently_occupied(
            orphan, "dev", "auto/planner-resync", AUTOMATION_NAME, AUTOMATION_EMAIL
        )


def test_cli_check_branch_returns_0_when_not_occupied(repo: pathlib.Path):
    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--base-ref",
            "dev",
            "--check-branch",
            "auto/planner-resync-never-pushed",
        ]
    )
    assert rc == 0


def test_cli_check_branch_returns_1_and_names_the_commit_when_occupied(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
):
    _push_branch_from_dev(repo, "auto/planner-resync-eaa79695")
    _commit(repo, HUMAN_NAME, HUMAN_EMAIL, "adopted a prior run's diversion", "a.txt")
    _run(repo, "push", "-q", "-f", "origin", "auto/planner-resync-eaa79695")

    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--base-ref",
            "dev",
            "--check-branch",
            "auto/planner-resync-eaa79695",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert HUMAN_NAME in out
    assert HUMAN_EMAIL in out


def test_cli_check_branch_returns_2_when_existence_cannot_be_determined(
    tmp_path: pathlib.Path,
):
    orphan = tmp_path / "no_origin"
    orphan.mkdir()
    _run(orphan, "init", "-q")
    rc = guard.main(
        [
            "--repo-root",
            str(orphan),
            "--base-ref",
            "dev",
            "--check-branch",
            "auto/planner-resync",
        ]
    )
    assert rc == 2


def test_cli_check_branch_does_not_require_divert_suffix(repo: pathlib.Path):
    """`--divert-suffix` is only meaningful for the force-push-target mode
    (`decide_branch`) -- `--check-branch` must not force a caller to invent
    one it will never use."""
    rc = guard.main(
        [
            "--repo-root",
            str(repo),
            "--base-ref",
            "dev",
            "--check-branch",
            "auto/planner-resync-never-pushed",
        ]
    )
    assert rc == 0


def test_cli_requires_divert_suffix_when_not_checking_a_branch(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
):
    """The force-push-target mode still needs `--divert-suffix` -- dropping
    the requirement for `--check-branch` must not silently drop it for the
    everyday `decide_branch` path too."""
    with pytest.raises(SystemExit) as excinfo:
        guard.main(["--repo-root", str(repo), "--branch", "auto/planner-resync"])
    assert excinfo.value.code == 2
    assert "--divert-suffix is required" in capsys.readouterr().err
