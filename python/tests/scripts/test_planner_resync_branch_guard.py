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
