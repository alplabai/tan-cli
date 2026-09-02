# SPDX-License-Identifier: Apache-2.0
"""Actually EXECUTE `planner-resync.yml`'s "Open or refresh the proposal PR"
step body -- tan-cli#1006 review round 2 (major).

Every other gate that touches this workflow (`test_planner_resync_workflow_errexit.py`,
the branch-guard unit tests) reads the YAML text or calls the guard's Python
API directly. NOTHING executed the step's actual shell body end to end, which
is exactly why round 2's blocker survived two review rounds and a fully green
CI: `planner-resync.yml:412`'s bare
`protected_commit="$(grep '^protected_commit=' "$GITHUB_OUTPUT" | tail -1 | cut
-d= -f2-)"` killed this step on EVERY non-diverted run (the everyday case --
`planner_resync_branch_guard.py` only wrote that key when something was
actually protected), and nothing in the suite noticed because nothing ran the
`run:` text as a program.

This module does. It extracts the step's `run:` string verbatim out of the
YAML (same mechanism `test_planner_resync_workflow_errexit.py` already uses,
so this cannot drift from the workflow), substitutes the handful of
`${{ github.* }}` expressions the GitHub Actions runner would normally
resolve before bash ever sees the script (bash cannot parse `${{ }}` itself),
and runs the result with a real `bash` subprocess against a real bare
"origin" and a real working clone -- `git config`, `checkout -B`, `commit`,
and `git push --force-with-lease` all execute for real. `gh` is stubbed (a
tiny script on `PATH`) since actually opening a GitHub PR is out of scope for
a hermetic test; everything up to and including the push is not stubbed.

Four shapes, the first two matching the review's own repro exactly and the
other two added for tan-cli#1015 (the everyday shapes those two do not
reach):

* clean (`auto/planner-resync` does not exist yet) -- must reach `git push`
  and exit 0. Only ever the very first run in a repo's history.
* diverted (`auto/planner-resync` already carries a human commit) -- must
  divert to `auto/planner-resync-<suffix>`, leave the human commit on the
  primary branch untouched, and still exit 0.
* refresh (`auto/planner-resync` already exists and is automation-owned) --
  what EVERY run after the first actually hits; must reuse the branch and
  force-push with the REAL prior tip as the lease, not the empty one the
  clean shape always exercises.
* cascaded diversion (`auto/planner-resync` AND its first divert candidate
  are both occupied by foreign commits) -- must cascade to a third name and
  credit every occupied branch in the PR body, not just the first
  (`occupied_count -eq 1` vs. the `credit` loop PR #1014 added).

Mutation-tested against the reviewer's own instruction: re-introducing the
bare (non-`|| true`, conditionally-written) shape reds BOTH the clean-run
test and the refresh-shape test on their own assertion (`STEP EXIT` != 0),
not an incidental exception -- the refresh shape hits the exact same
conditional-write bug the clean shape does, just on the branch every run
after the first actually takes.

The other unpinned finding in the same review comment
(`planner-resync.yml:449,805-807`) gets the same treatment below: the
clean-run test also asserts `--force-with-lease` (not a bare `-f`) appears in
the traced `git push`, and a second class of tests executes the "Verdict"
step's own `run:` body (same extraction mechanism, with the load-bearing
`${{ steps.pr.outputs.diverted }}`/`${{ steps.resync.outputs.rc }}` etc.
tokens substituted to controlled values rather than blanked) to pin that the
`::warning::` fires exactly when `diverted=true`, and only then.
"""

from __future__ import annotations

import functools
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "planner-resync.yml"
GUARD_SCRIPT = REPO_ROOT / "python" / "scripts" / "planner_resync_branch_guard.py"

#: tan-cli#1015 review nit: every test in this module drives a real `bash`
#: subprocess (both the PR step and the Verdict step below), unlike every
#: other bash-driving gate in this suite (e.g.
#: `tests/gates/test_packaging_path_pr_exercise.py:461`), which already
#: skips this way. Module-scoped rather than per-test: there is no test here
#: that does NOT need `bash`.
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to parse with")

_PR_STEP = "Open or refresh the proposal PR"

#: The runner resolves `${{ ... }}` expressions BEFORE bash ever sees the
#: script -- bash has no idea what that syntax means. None of the handful of
#: expressions inside this step's `run:` text span a `}` (they're plain
#: `github.*` property reads), so a non-greedy single-line match is exact,
#: not an approximation.
_GHA_EXPR = re.compile(r"\$\{\{(.*?)\}\}")

#: tan-cli#1015 review nit: an earlier version of this substitution was a
#: bare `_GHA_EXPR.sub("TESTVAL", run)` -- blanking every `${{ }}` expression
#: indiscriminately with no allow-list, unlike `_render_verdict` below (which
#: asserts on an unrecognised token for exactly this reason). Exact today --
#: every expression inside this step's `run:` text is a cosmetic `github.*`
#: read embedded in an `echo` (a PR-body URL, a run link) -- but a FUTURE
#: load-bearing token (say `${{ steps.*.outputs.* }}`) would silently run as
#: the literal string `TESTVAL` and this gate would keep passing. Keyed on
#: the raw expression text so a rename/reword surfaces as an assertion
#: failure below, not a silent no-op substitution.
_PR_STEP_TOKEN_VALUES = {
    "github.repository": "example/tan-cli",
    "github.server_url": "https://github.com",
    "github.run_id": "999999",
}


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@functools.cache
def _pr_step_run() -> str:
    steps = _workflow()["jobs"]["propose"]["steps"]
    step = next((s for s in steps if s.get("name") == _PR_STEP), None)
    assert step is not None, (
        f"no step named {_PR_STEP!r} found in planner-resync.yml's `propose` "
        f"job -- either it was renamed (update this gate too) or removed "
        f"(drop this gate along with it)"
    )
    run = step["run"]
    assert isinstance(run, str) and run.strip(), step

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        assert inner in _PR_STEP_TOKEN_VALUES, (
            f"unhandled GHA expression in the {_PR_STEP!r} step: {inner!r} -- "
            f"this token is new (or reworded); add it to "
            f"{__name__}._PR_STEP_TOKEN_VALUES's substitution map rather "
            f"than let this test silently blank it to a dummy literal"
        )
        return _PR_STEP_TOKEN_VALUES[inner]

    return _GHA_EXPR.sub(repl, run)


def _git(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True
    )
    return proc.stdout.decode("utf-8", "replace")


def _commit(root: pathlib.Path, name: str, email: str, message: str, filename: str) -> str:
    (root / filename).write_text(f"{message}\n", encoding="utf-8")
    _git(root, "add", filename)
    _git(
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
    return _git(root, "rev-parse", "HEAD").strip()


#: The fake `gh` this test puts on `PATH` -- actually opening a GitHub PR is
#: out of scope for a hermetic test; everything up to and including `git
#: push` is real. `pr list` answers "no existing PR" (empty stdout, matching
#: `-q '.[0].number'` finding nothing) so the step always takes the `gh pr
#: create` branch; `pr create`/`pr view` each print a fake URL.
_FAKE_GH = """#!/usr/bin/env bash
case "$1 $2" in
  "pr list") exit 0 ;;
  "pr create") echo "https://github.com/example/tan-cli/pull/1"; exit 0 ;;
  "pr view") echo "https://github.com/example/tan-cli/pull/1"; exit 0 ;;
  "pr edit") exit 0 ;;
  *) echo "fake gh: unhandled invocation: $*" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def fake_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    """A `PATH` entry carrying a real `python` (so `python
    python/scripts/planner_resync_branch_guard.py` -- the literal command the
    step runs -- works no matter what `python` resolves to on the host) and
    the stub `gh` above."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python_stub = bindir / "python"
    python_stub.write_text(
        f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    python_stub.chmod(0o755)
    gh_stub = bindir / "gh"
    gh_stub.write_text(_FAKE_GH, encoding="utf-8")
    gh_stub.chmod(0o755)
    return bindir


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A real bare `origin` plus a real working clone playing the role of
    `GITHUB_WORKSPACE` -- `dev` checked out, the ACTUAL current
    `planner_resync_branch_guard.py` copied in at the exact path the step
    invokes it from (`python/scripts/...`, relative to the step's cwd), and
    an uncommitted change under `python/` so the step's own "nothing to
    propose" early-exit does not fire."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "checkout", "-q", "-b", "dev")
    (work / "python" / "scripts").mkdir(parents=True)
    shutil.copy(GUARD_SCRIPT, work / "python" / "scripts" / "planner_resync_branch_guard.py")
    _git(work, "add", "python")
    _git(
        work,
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@example.com",
        "commit",
        "-q",
        "-m",
        "seed dev",
    )
    _git(work, "push", "-q", "origin", "dev")

    # The re-sync's own diff -- untracked is enough, `git status --porcelain
    # -- python/` sees it either way.
    (work / "python" / "resynced_marker.txt").write_text("proposed delta\n", encoding="utf-8")
    return work


def _push_foreign_commit_on(bare: pathlib.Path, branch: str, tmp_path: pathlib.Path) -> str:
    """Seed `origin/<branch>` with a commit a human authored, from a THROWAWAY
    clone -- never through `workspace` itself, so the human commit predates
    anything the step under test does."""
    scratch = tmp_path / f"scratch-{branch.replace('/', '-')}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    _git(scratch, "checkout", "-q", "-B", branch, "origin/dev")
    sha = _commit(scratch, "A Human Reviewer", "human@example.com", "hand-port work", "handport.txt")
    _git(scratch, "push", "-q", "-f", "origin", branch)
    return sha


#: Must match `planner-resync.yml`'s own `AUTOMATION_NAME`/`AUTOMATION_EMAIL`
#: (the step's `git config user.name`/`user.email`, which is also what it
#: passes `planner_resync_branch_guard.py --automation-name`/
#: `--automation-email`) -- a prior run's own commit, for the refresh shape
#: below.
_AUTOMATION_NAME = "alp-sdk planner re-sync"
_AUTOMATION_EMAIL = "noreply@alplab.ai"


def _push_automation_commit_on(bare: pathlib.Path, branch: str, tmp_path: pathlib.Path) -> str:
    """Seed `origin/<branch>` with a commit carrying the AUTOMATION's own
    identity (both author and committer) -- i.e. what `auto/planner-resync`
    looks like after a PRIOR run of this exact step, which is the everyday
    shape every run after the first actually hits. From a THROWAWAY clone,
    same reasoning as `_push_foreign_commit_on`: the "prior run" must predate
    anything the step under test does."""
    scratch = tmp_path / f"scratch-{branch.replace('/', '-')}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    _git(scratch, "checkout", "-q", "-B", branch, "origin/dev")
    sha = _commit(
        scratch,
        _AUTOMATION_NAME,
        _AUTOMATION_EMAIL,
        "chore(planner): propose the re-sync owed to alp-sdk deadbeef1",
        "resynced_marker.txt",
    )
    _git(scratch, "push", "-q", "-f", "origin", branch)
    return sha


def _push_foreign_commit_with_distinct_committer_on(
    bare: pathlib.Path, branch: str, tmp_path: pathlib.Path
) -> str:
    """Seed `origin/<branch>` with a commit whose AUTHOR is a human but whose
    COMMITTER is the automation identity -- the `git commit --amend` /
    `--autosquash fixup!` shape `planner_resync_branch_guard.py`'s own
    docstring calls out ("THE SIGNAL" section): the commit is still foreign
    (author-foreign is enough), but a reader who only sees the author would
    misread it as the automation's own. Exercises the credit loop's
    `credit = oa, committed by ocommitter` branch (`planner-resync.yml`
    `:521-524`), not just its `credit = oa` one."""
    scratch = tmp_path / f"scratch-{branch.replace('/', '-')}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    _git(scratch, "checkout", "-q", "-B", branch, "origin/dev")
    (scratch / "handport.txt").write_text("amended hand-port work\n", encoding="utf-8")
    _git(scratch, "add", "handport.txt")
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "Another Human"
    env["GIT_AUTHOR_EMAIL"] = "another-human@example.com"
    subprocess.run(
        [
            "git",
            "-C",
            str(scratch),
            "-c",
            f"user.name={_AUTOMATION_NAME}",
            "-c",
            f"user.email={_AUTOMATION_EMAIL}",
            "commit",
            "-q",
            "-m",
            "amended hand-port work",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    sha = _git(scratch, "rev-parse", "HEAD").strip()
    _git(scratch, "push", "-q", "-f", "origin", branch)
    return sha


def _dev_tip(workspace: pathlib.Path) -> str:
    return _git(workspace, "rev-parse", "dev").strip()


def _run_step(
    workspace: pathlib.Path,
    fake_bin: pathlib.Path,
    tmp_path: pathlib.Path,
    sdk_sha: str,
    *,
    dev_sha: str | None = None,
    resync_verdict: str = "clean",
    rc: str = "0",
) -> subprocess.CompletedProcess[bytes]:
    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GITHUB_OUTPUT"] = str(github_output)
    env["GITHUB_WORKSPACE"] = str(workspace)
    env["RUNNER_TEMP"] = str(runner_temp)
    env["SDK_SHA"] = sdk_sha
    env["RC"] = rc
    env["GATE_VERDICT"] = "PASS"
    env["GH_TOKEN"] = "fake-token"
    # tan-cli#1109: the step now re-fetches `origin/dev` for itself and
    # compares to `DEV_SHA` (`steps.devtip.outputs.sha` on the real runner)
    # before doing anything else -- default to the workspace's OWN current
    # `dev` tip, i.e. "this run is fresh", so the four pre-existing shapes
    # below stay exactly what they were testing before this guard existed.
    env["DEV_SHA"] = dev_sha if dev_sha is not None else _dev_tip(workspace)
    env["RESYNC_VERDICT"] = resync_verdict

    proc = subprocess.run(
        ["bash", "-x", "-c", _pr_step_run()],
        cwd=workspace,
        env=env,
        capture_output=True,
    )
    return proc


def _fmt(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (
        f"STEP EXIT={proc.returncode}\n--- stdout ---\n"
        f"{proc.stdout.decode('utf-8', 'replace')}\n--- stderr (bash -x trace) ---\n"
        f"{proc.stderr.decode('utf-8', 'replace')}"
    )


def test_clean_run_reaches_git_push_and_exits_zero(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """The everyday case: nothing occupies `auto/planner-resync`. This is the
    exact shape the round-2 blocker broke -- pre-fix, this reds at `STEP
    EXIT=1` with `git push` never traced, `$GITHUB_OUTPUT` holding
    `protected_commit=` nowhere at all."""
    proc = _run_step(workspace, fake_bin, tmp_path, "cafef00d12345678")
    assert proc.returncode == 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" in trace, _fmt(proc)
    # tan-cli#1006 review (major, :449): pins the OTHER unpinned finding in
    # the same review comment -- a revert of `--force-with-lease` back to a
    # bare `-f` must also red here, not just at line 412.
    assert "--force-with-lease=auto/planner-resync:" in trace, _fmt(proc)

    tip = _git(workspace, "ls-remote", "origin", "refs/heads/auto/planner-resync").strip()
    assert tip, "auto/planner-resync was never pushed to origin:\n" + _fmt(proc)


def test_diverted_run_leaves_the_primary_branch_untouched_and_exits_zero(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """`auto/planner-resync` already carries a human commit -> the step must
    divert to `auto/planner-resync-<suffix>`, leave the human's commit on the
    primary branch exactly where it was, and still complete (exit 0) --
    tan-cli#1002's whole point, driven here through the real workflow step
    rather than only through the guard's own API."""
    bare = tmp_path / "origin.git"
    human_sha = _push_foreign_commit_on(bare, "auto/planner-resync", tmp_path)

    sdk_sha = "cafef00d12345678"
    proc = _run_step(workspace, fake_bin, tmp_path, sdk_sha)
    assert proc.returncode == 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" in trace, _fmt(proc)

    diverted_branch = f"auto/planner-resync-{sdk_sha[:8]}"
    diverted_tip = _git(workspace, "ls-remote", "origin", f"refs/heads/{diverted_branch}").strip()
    assert diverted_tip, f"{diverted_branch} was never pushed to origin:\n" + _fmt(proc)

    primary_tip = _git(workspace, "ls-remote", "origin", "refs/heads/auto/planner-resync").strip()
    assert human_sha in primary_tip, (
        "the human commit on auto/planner-resync must survive untouched:\n" + _fmt(proc)
    )


def test_refresh_run_reuses_the_branch_and_force_pushes_with_the_observed_tip(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1015: the shape `test_clean_run_reaches_git_push_and_exits_zero`
    does NOT cover -- `auto/planner-resync` already exists on origin and is
    automation-owned (a prior run's own commit sits there, not a human's).
    This is what EVERY run after the first hits; the "branch absent" clean
    shape above is only ever the very first run in a repo's history. Must
    still reach `git push`, must NOT divert (`planner_resync_branch_guard.py`
    finds no foreign commit), and -- the part the clean shape cannot exercise
    because there `observed_tip` is always empty -- must force-push with the
    REAL prior tip as the lease, not an empty one, or a concurrent write to
    `auto/planner-resync` in the TOCTOU window (tan-cli#1006 minor) would
    silently be force-pushed over instead of caught by `git push` itself."""
    bare = tmp_path / "origin.git"
    prior_sha = _push_automation_commit_on(bare, "auto/planner-resync", tmp_path)

    sdk_sha = "01234567deadbeef"
    proc = _run_step(workspace, fake_bin, tmp_path, sdk_sha)
    assert proc.returncode == 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" in trace, _fmt(proc)
    assert f"--force-with-lease=auto/planner-resync:{prior_sha}" in trace, (
        "the refresh must lease against the PRIOR run's real tip, not an "
        "empty one (that shape is already covered by the clean-run test):\n"
        + _fmt(proc)
    )

    outputs = (tmp_path / "github_output").read_text(encoding="utf-8")
    assert "branch=auto/planner-resync\n" in outputs, outputs
    assert "diverted=false\n" in outputs, outputs
    assert "occupied_count=0\n" in outputs, outputs
    assert "protected_commit=\n" in outputs, outputs

    new_tip = _git(workspace, "ls-remote", "origin", "refs/heads/auto/planner-resync").strip()
    assert new_tip, "auto/planner-resync vanished from origin:\n" + _fmt(proc)
    assert prior_sha not in new_tip, (
        "the refresh must actually move the branch tip (a fresh commit on "
        "top of the prior automation commit), not a no-op:\n" + _fmt(proc)
    )


def test_cascaded_diversion_credits_every_occupied_branch_and_exits_zero(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1015: `test_diverted_run_leaves_the_primary_branch_untouched_
    and_exits_zero` above only ever occupies `auto/planner-resync` itself,
    which takes the `occupied_count -eq 1` arm (`planner-resync.yml:488`) --
    it never enters the cascaded `credit` loop PR #1014 added
    (`:495-525`), reached only when a PRIOR run's own diversion target is
    ALSO occupied by foreign work by the time this run looks. Seeds BOTH
    `auto/planner-resync` and its first divert candidate
    (`auto/planner-resync-<suffix>`) with foreign commits -- the guard must
    cascade past both to a THIRD name (`-<suffix>-2`), and the PR body's
    credit loop must name both occupied branches, not just the first.

    The second occupied branch specifically uses a foreign AUTHOR with the
    AUTOMATION's own committer identity (the `--amend`/`fixup!` shape the
    guard's own docstring calls "THE SIGNAL") so this also exercises the
    loop's `credit = oa, committed by ocommitter` line
    (`planner-resync.yml:521-524`), not only its plain `credit = oa` one."""
    bare = tmp_path / "origin.git"
    sdk_sha = "deadbeef87654321"
    suffix = sdk_sha[:8]

    primary_sha = _push_foreign_commit_on(bare, "auto/planner-resync", tmp_path)
    divert_sha = _push_foreign_commit_with_distinct_committer_on(
        bare, f"auto/planner-resync-{suffix}", tmp_path
    )

    proc = _run_step(workspace, fake_bin, tmp_path, sdk_sha)
    assert proc.returncode == 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" in trace, _fmt(proc)

    outputs = (tmp_path / "github_output").read_text(encoding="utf-8")
    assert "diverted=true\n" in outputs, outputs
    assert "occupied_count=2\n" in outputs, (
        "expected a cascaded diversion (both auto/planner-resync and its "
        "first divert candidate occupied):\n" + outputs
    )

    final_branch = f"auto/planner-resync-{suffix}-2"
    final_tip = _git(workspace, "ls-remote", "origin", f"refs/heads/{final_branch}").strip()
    assert final_tip, f"{final_branch} was never pushed to origin:\n" + _fmt(proc)

    # Both occupied branches survive untouched -- the whole point of a
    # cascaded diversion is that NEITHER prior occupant is overwritten.
    primary_tip = _git(workspace, "ls-remote", "origin", "refs/heads/auto/planner-resync").strip()
    assert primary_sha in primary_tip, _fmt(proc)
    divert_tip = _git(
        workspace, "ls-remote", "origin", f"refs/heads/auto/planner-resync-{suffix}"
    ).strip()
    assert divert_sha in divert_tip, _fmt(proc)

    # The PR body's credit loop (`planner-resync.yml:495-527`) must name
    # BOTH occupied branches, with the second crediting author AND committer
    # since they differ on that one.
    body = (tmp_path / "runner_temp" / "body.md").read_text(encoding="utf-8")
    assert "found **2** branches already" in body, body
    assert f"`auto/planner-resync` carries `{primary_sha}`" in body, body
    assert f"`auto/planner-resync-{suffix}` carries `{divert_sha}`" in body, body
    assert "A Human Reviewer" in body, body
    assert "Another Human <another-human@example.com>, committed by " in body, body
    assert _AUTOMATION_NAME in body, body


# ------------------------------------ tan-cli#1109 faults 2 and 1 (logging)


def _advance_dev_with_a_landed_change(
    bare: pathlib.Path, tmp_path: pathlib.Path, rel_path: str, tag: str
) -> str:
    """Push a new commit directly onto `origin/dev` at `rel_path`, from a
    THROWAWAY clone -- simulates a merge landing on `dev` while this run was
    busy regenerating (PR #1103's own timeline: two of tonight's three junk
    PRs were computed while it sat open, unmerged, on exactly this path)."""
    scratch = tmp_path / f"scratch-dev-{tag}"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    _git(scratch, "checkout", "-q", "dev")
    (scratch / rel_path).parent.mkdir(parents=True, exist_ok=True)
    sha = _commit(
        scratch, "A Human Reviewer", "human@example.com", f"advance dev: {rel_path}", rel_path
    )
    _git(scratch, "push", "-q", "origin", "dev")
    return sha


#: tan-cli#1109 review (minor): the staleness check's path LIST is the
#: revert-risk surface, not just "some file under python/tan/planner" --
#: `apply()` writes exactly `python/tan/planner/<mod>` AND `GATE_REL`
#: (`planner_resync.py:738-786`), so a change confined to the GATE FILE alone
#: (a hand-authored pin move, say) is just as revert-risky as one confined to
#: a mirror module, and dropping it from the `git diff --quiet` path list
#: would leave this exact test green while only asserting half the surface.
#: Parametrized over both paths rather than duplicated, so the two cases
#: cannot drift apart the way two independently-hand-kept tests eventually do.
@pytest.mark.parametrize(
    "rel_path,tag",
    [
        ("python/tan/planner/validate.py", "mirror-touch"),
        (
            "python/tests/gates/test_planner_relocation_freshness.py",
            "gate-file-touch",
        ),
    ],
)
def test_stale_dev_between_regen_and_push_aborts_without_pushing_anything(
    workspace: pathlib.Path,
    fake_bin: pathlib.Path,
    tmp_path: pathlib.Path,
    rel_path: str,
    tag: str,
):
    """tan-cli#1109 fault 2, half two: `origin/dev`'s `tan/planner/` (or the
    freshness gate's own pin file) moved (a landed fix, exactly PR #1103's
    shape) between when this run regenerated (`DEV_SHA`, what
    `steps.devtip.outputs.sha` names on the real runner) and when this step
    is about to push -- must abort before EVER reaching `git push`, must
    leave `auto/planner-resync` completely unpushed, and must set both
    `opened=false` and `stale=true`."""
    bare = tmp_path / "origin.git"
    stale_dev_sha = _dev_tip(workspace)

    _advance_dev_with_a_landed_change(bare, tmp_path, rel_path, tag)

    proc = _run_step(
        workspace, fake_bin, tmp_path, "cafef00d12345678", dev_sha=stale_dev_sha
    )
    assert proc.returncode != 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" not in trace, (
        "a stale run must abort before ever pushing anything:\n" + _fmt(proc)
    )
    assert b"::error::" in proc.stdout, _fmt(proc)
    assert b"tan-cli#1109" in proc.stdout, _fmt(proc)

    outputs = (tmp_path / "github_output").read_text(encoding="utf-8")
    assert "opened=false\n" in outputs, outputs
    assert "stale=true\n" in outputs, outputs
    assert not _git(
        workspace, "ls-remote", "origin", "refs/heads/auto/planner-resync"
    ).strip(), "auto/planner-resync must never have been created on a stale run"


def test_dev_advancing_on_an_unrelated_file_does_not_abort(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """The staleness check is scoped to exactly the paths
    `planner_resync.py` reads as `ours`/writes -- an unrelated commit landing
    on `dev` in the same window (any other PR, any day) must not turn every
    ordinary run red."""
    bare = tmp_path / "origin.git"
    stale_dev_sha = _dev_tip(workspace)

    _advance_dev_with_a_landed_change(bare, tmp_path, "docs/unrelated.md", "unrelated")

    proc = _run_step(
        workspace, fake_bin, tmp_path, "cafef00d12345678", dev_sha=stale_dev_sha
    )
    assert proc.returncode == 0, _fmt(proc)
    trace = proc.stderr.decode("utf-8", "replace")
    assert "+ git push" in trace, (
        "an unrelated file changing on dev in the same window must not "
        "abort a run whose own diff does not touch the watched paths:\n"
        + _fmt(proc)
    )


def test_nothing_to_propose_logs_the_planner_resync_verdict(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 fault 1: the "nothing to propose" log line must name WHY
    (the actual verdict `planner_resync.py` reached), not just repeat that
    nothing was proposed -- so a silent run reads as a measurement."""
    (workspace / "python" / "resynced_marker.txt").unlink()

    proc = _run_step(
        workspace, fake_bin, tmp_path, "cafef00d12345678", resync_verdict="up-to-date"
    )
    assert proc.returncode == 0, _fmt(proc)
    assert b"Nothing to propose" in proc.stdout, _fmt(proc)
    assert b"up-to-date" in proc.stdout, _fmt(proc)

    outputs = (tmp_path / "github_output").read_text(encoding="utf-8")
    assert "opened=false\n" in outputs, outputs


def test_nothing_to_propose_on_a_refusal_does_not_assert_a_cause_it_never_measured(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 review (minor): `planner_resync.py` returns 2 (REFUSED)
    BEFORE it ever writes `verdict=` to $GITHUB_OUTPUT -- `RESYNC_VERDICT` is
    empty on this path, so the "no file ... changed enough" wording would
    assert a measurement the script never made. `RC == '2'` must instead say
    the script refused, not fabricate a verdict from an empty string."""
    (workspace / "python" / "resynced_marker.txt").unlink()

    proc = _run_step(
        workspace,
        fake_bin,
        tmp_path,
        "cafef00d12345678",
        resync_verdict="",
        rc="2",
    )
    assert proc.returncode == 0, _fmt(proc)
    assert b"Nothing to propose" in proc.stdout, _fmt(proc)
    assert b"REFUSED" in proc.stdout, _fmt(proc)
    assert b"changed enough to write anything" not in proc.stdout, (
        "a refusal must not claim it measured that nothing changed -- it "
        "never got far enough to classify a single file:\n" + _fmt(proc)
    )


def test_diff_against_dev_lists_a_path_with_a_space_untruncated(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 review (nit): `awk '{print $2}'` splits on whitespace, so
    a changed path containing a space prints truncated in the "Diff against
    `dev`" block -- `cut -c4-` takes the whole rest of each `git status
    --porcelain` line instead."""
    (workspace / "python" / "a file with spaces.txt").write_text(
        "proposed delta\n", encoding="utf-8"
    )

    proc = _run_step(workspace, fake_bin, tmp_path, "cafef00d12345678")
    assert proc.returncode == 0, _fmt(proc)

    body = (tmp_path / "runner_temp" / "body.md").read_text(encoding="utf-8")
    assert "python/a file with spaces.txt" in body, body


# ---------------------------------------------------------- the Verdict step


_VERDICT_STEP = "Verdict"

#: Unlike the PR step's `${{ github.* }}` tokens (cosmetic -- a URL in a PR
#: body), the Verdict step's `${{ steps.*.outputs.* }}` tokens are
#: LOAD-BEARING control flow (`if [ '${{ steps.pr.outputs.diverted }}' =
#: 'true' ]`), so they cannot be blanked to one dummy value the way the PR
#: step's were -- each is substituted to a value this test controls.
_VERDICT_TOKEN_ORDER = (
    "steps.pr.outputs.diverted",
    "steps.pr.outputs.occupied_count || 1",
    "steps.pr.outputs.url || 'the job summary'",
    "steps.pr.outputs.stale",
    "steps.resync.outputs.rc",
    "steps.pr.outputs.url || steps.issue.outputs.url || 'the job summary'",
    "steps.gate.outputs.verdict",
    "steps.pr.outputs.opened",
)


@functools.cache
def _verdict_step_run() -> str:
    steps = _workflow()["jobs"]["propose"]["steps"]
    step = next((s for s in steps if s.get("name") == _VERDICT_STEP), None)
    assert step is not None, (
        f"no step named {_VERDICT_STEP!r} found in planner-resync.yml's "
        f"`propose` job -- either it was renamed (update this gate too) or "
        f"removed (drop this gate along with it)"
    )
    run = step["run"]
    assert isinstance(run, str) and run.strip(), step
    return run


def _render_verdict(
    *,
    diverted: bool,
    rc: str = "0",
    verdict: str = "PASS",
    opened: str = "true",
    url: str = "https://github.com/example/tan-cli/pull/1",
    stale: bool = False,
) -> str:
    values = {
        "steps.pr.outputs.diverted": "true" if diverted else "false",
        "steps.pr.outputs.occupied_count || 1": "1",
        "steps.pr.outputs.url || 'the job summary'": url,
        "steps.pr.outputs.stale": "true" if stale else "false",
        "steps.resync.outputs.rc": rc,
        "steps.pr.outputs.url || steps.issue.outputs.url || 'the job summary'": url,
        "steps.gate.outputs.verdict": verdict,
        "steps.pr.outputs.opened": opened,
    }

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        assert inner in values, (
            f"unhandled GHA expression in the {_VERDICT_STEP!r} step: "
            f"{inner!r} -- this token is new (or reworded); add it to "
            f"{__name__}._render_verdict's substitution map rather than let "
            f"this test silently execute stale literal `${{{{ ... }}}}` text"
        )
        return values[inner]

    return re.sub(r"\$\{\{(.*?)\}\}", repl, _verdict_step_run())


def _run_bash(script: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["bash", "-x", "-c", script], capture_output=True)


def test_verdict_warns_exactly_when_diverted_is_true(tmp_path: pathlib.Path):
    """tan-cli#1006 review (major, :805-807): pins the `::warning::` firing
    on the right condition -- the OTHER unpinned finding named in the same
    review comment as the blocker. `rc=0` for both: this is specifically the
    "green job, but something needs attention" incident the warning exists
    to close, so a clean rc must not be what silences it."""
    diverted_proc = _run_bash(_render_verdict(diverted=True, rc="0"))
    assert diverted_proc.returncode == 0, diverted_proc.stderr.decode("utf-8", "replace")
    assert b"::warning::" in diverted_proc.stdout, diverted_proc.stdout.decode("utf-8", "replace")
    assert b"Re-sync clean, but this proposal was DIVERTED" in diverted_proc.stdout, (
        diverted_proc.stdout.decode("utf-8", "replace")
    )

    clean_proc = _run_bash(_render_verdict(diverted=False, rc="0"))
    assert clean_proc.returncode == 0, clean_proc.stderr.decode("utf-8", "replace")
    assert b"::warning::" not in clean_proc.stdout, clean_proc.stdout.decode("utf-8", "replace")
    assert b"Re-sync clean (or nothing owed)." in clean_proc.stdout, (
        clean_proc.stdout.decode("utf-8", "replace")
    )


def test_verdict_errors_on_a_stale_abort_instead_of_reporting_clean(
    tmp_path: pathlib.Path,
):
    """tan-cli#1109 review (minor): `steps.pr.outputs.stale` used to be
    written and never read -- with `rc` left at whatever the resync step
    reported (commonly `0`), a stale-aborted run fell straight into the `0)`
    arm and printed "Re-sync clean (or nothing owed)." on a run that pushed
    nothing. Checked before the `rc` case, same reason the diversion warning
    is: never lost regardless of which `rc` branch would otherwise fire."""
    proc = _run_bash(_render_verdict(diverted=False, rc="0", stale=True))
    assert proc.returncode != 0, proc.stderr.decode("utf-8", "replace")
    assert b"::error::" in proc.stdout, proc.stdout.decode("utf-8", "replace")
    assert b"tan-cli#1109" in proc.stdout, proc.stdout.decode("utf-8", "replace")
    assert b"Re-sync clean (or nothing owed)." not in proc.stdout, (
        "a stale-aborted run must never read as clean:\n"
        + proc.stdout.decode("utf-8", "replace")
    )


# ------------------------ tan-cli#1109 fault 3: cap at one open PR

_CLOSE_STEP = "Close superseded planner-resync proposals (cap at one open PR)"


@functools.cache
def _close_step_run() -> str:
    steps = _workflow()["jobs"]["propose"]["steps"]
    step = next((s for s in steps if s.get("name") == _CLOSE_STEP), None)
    assert step is not None, (
        f"no step named {_CLOSE_STEP!r} found in planner-resync.yml's "
        f"`propose` job -- either it was renamed (update this gate too) or "
        f"removed (drop this gate along with it)"
    )
    run = step["run"]
    assert isinstance(run, str) and run.strip(), step
    # Unlike the PR step, this one's `run:` text carries no `${{ ... }}`
    # expression at all -- everything it needs (KEEP_BRANCH, KEEP_URL,
    # GITHUB_REPOSITORY) arrives as a real env var, not a GHA-runner
    # substitution -- so, unlike `_pr_step_run()`/`_render_verdict`, there is
    # nothing here to blank or substitute.
    assert "${{" not in run, (
        "this step started referencing a `${{ }}` expression -- give it the "
        "same substitution treatment `_pr_step_run()` gets rather than let "
        "this test run stale literal `${{ ... }}` text verbatim"
    )
    return run


def _run_close_step(
    tmp_path: pathlib.Path,
    gh_script: str,
    *,
    keep_branch: str,
    keep_url: str,
    pr_list_fixture: str = "[]",
) -> tuple[subprocess.CompletedProcess[bytes], pathlib.Path]:
    bindir = tmp_path / "close-bin"
    bindir.mkdir()
    gh_stub = bindir / "gh"
    gh_stub.write_text(gh_script, encoding="utf-8")
    gh_stub.chmod(0o755)
    call_log = tmp_path / "gh_calls.log"
    call_log.write_text("", encoding="utf-8")
    fixture = tmp_path / "pr_list_fixture.json"
    fixture.write_text(pr_list_fixture, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_TOKEN"] = "fake-token"
    env["GITHUB_REPOSITORY"] = "example/tan-cli"
    env["KEEP_BRANCH"] = keep_branch
    env["KEEP_URL"] = keep_url
    env["GH_CALL_LOG"] = str(call_log)
    env["GH_PR_LIST_FIXTURE"] = str(fixture)

    proc = subprocess.run(
        ["bash", "-x", "-c", _close_step_run()], env=env, capture_output=True
    )
    return proc, call_log


#: tan-cli#1109 review (major): the earlier version of this fake answered
#: `pr list` with a hardcoded number list regardless of argv, so the real
#: `--jq` expression the step passes -- the `startswith("auto/planner-
#: resync")` prefix match, the `!= KEEP_BRANCH` self-exclusion, and (this
#: round) the `author.login == "app/github-actions"` + `isCrossRepository ==
#: false` filters that stop a HUMAN's PR from being closed -- never actually
#: ran. This fake instead extracts the real `--jq` argument out of `gh`'s own
#: argv and applies it with the REAL `jq` binary against a JSON fixture
#: (`GH_PR_LIST_FIXTURE`), the same shape `gh pr list --json ... --jq ...`
#: itself returns -- so a typo or a dropped `select()` in the step's own
#: string is caught here, not waved through by a fake that always answers
#: "correctly" no matter what was asked.
_FAKE_GH_ARGV_AWARE = """#!/usr/bin/env bash
case "$1 $2" in
  "pr list")
    jq_expr=""
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--jq" ]; then
        jq_expr="$arg"
      fi
      prev="$arg"
    done
    if [ -z "$jq_expr" ]; then
      echo "fake gh: pr list called with no --jq argument: $*" >&2
      exit 1
    fi
    jq -r "$jq_expr" "$GH_PR_LIST_FIXTURE"
    ;;
  "pr comment") echo "$*" >> "$GH_CALL_LOG"; exit 0 ;;
  "pr close") echo "$*" >> "$GH_CALL_LOG"; exit 0 ;;
  *) echo "fake gh: unhandled invocation: $*" >&2; exit 1 ;;
esac
"""

_FAKE_GH_LIST_FAILS = """#!/usr/bin/env bash
case "$1 $2" in
  "pr list") echo "gh: transient failure" >&2; exit 1 ;;
  *) echo "fake gh: unhandled invocation: $*" >&2; exit 1 ;;
esac
"""

#: A realistic `gh pr list --json number,headRefName,author,isCrossRepository`
#: page, mirroring the real shapes this repo has actually produced (`gh pr
#: view <n> --json author` on a genuine bot PR: `{"is_bot":true,"login":
#: "app/github-actions"}`, measured tan-cli#1109 review) plus the two shapes
#: this round's guard exists to refuse:
#:   * #1106/#1107 -- bot-authored, same-repo, diverted proposals: MUST close.
#:   * #2001 -- a HUMAN'S PR on a branch that also happens to start with
#:     `auto/planner-resync` (the literal #1002/#996 "hand-port work parked
#:     on this branch name" shape): MUST NOT close.
#:   * #2002 -- author claims to be the bot but `isCrossRepository` is true (a
#:     fork cannot really forge the author identity, but the check is free):
#:     MUST NOT close.
#:   * #1108 -- the branch this run just opened/refreshed (`KEEP_BRANCH`
#:     below): MUST NOT close itself.
_PR_LIST_FIXTURE_MIXED = """[
  {"number": 1106, "headRefName": "auto/planner-resync-eaa79695",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false},
  {"number": 1107, "headRefName": "auto/planner-resync-bbfc6c5a",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false},
  {"number": 2001, "headRefName": "auto/planner-resync-humanwork",
   "author": {"login": "a-human-reviewer", "is_bot": false},
   "isCrossRepository": false},
  {"number": 2002, "headRefName": "auto/planner-resync-forked",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": true},
  {"number": 1108, "headRefName": "auto/planner-resync-5c33ef04",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false}
]"""


def test_close_step_closes_bot_prs_but_never_a_human_or_cross_repo_one(
    tmp_path: pathlib.Path,
):
    """tan-cli#1109 fault 3 + review (major): once one proposal is
    open/refreshed, every OTHER open, BOT-AUTHORED, same-repo PR whose head
    starts with `auto/planner-resync` is superseded and closed (#1106/#1107 --
    the literal shape of tonight's #1106/#1107/#1108); a PR that merely
    shares the branch prefix but is NOT the bot's own -- a human's hand-port
    work (#2001, the #1002/#996 shape this whole guard exists to protect) or
    a cross-repo PR (#2002) -- is never touched. Both directions asserted."""
    proc, call_log = _run_close_step(
        tmp_path,
        _FAKE_GH_ARGV_AWARE,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_MIXED,
    )
    assert proc.returncode == 0, _fmt(proc)
    calls = call_log.read_text(encoding="utf-8")

    # Direction 1: the bot's own OTHER proposals are superseded and closed.
    assert "pr comment 1106" in calls, calls
    assert "pr comment 1107" in calls, calls
    assert "pr close 1106" in calls, calls
    assert "pr close 1107" in calls, calls

    # Direction 2: a human's PR (same branch prefix, foreign author) must
    # never be closed or commented on, no matter how it got that branch name.
    assert "pr comment 2001" not in calls, calls
    assert "pr close 2001" not in calls, calls
    # ...and neither must a cross-repository PR, even one claiming the bot's
    # own author identity.
    assert "pr comment 2002" not in calls, calls
    assert "pr close 2002" not in calls, calls
    # ...nor the branch this very run just opened/refreshed.
    assert "pr comment 1108" not in calls, calls
    assert "pr close 1108" not in calls, calls


def test_close_step_refuses_to_guess_when_gh_pr_list_fails(tmp_path: pathlib.Path):
    """Same fail-closed shape as every other `gh ... | jq` lookup in this
    job (tan-cli#920): a transient `gh` error must not be read as "no other
    proposal exists" and silently leave a stale one open."""
    proc, call_log = _run_close_step(
        tmp_path,
        _FAKE_GH_LIST_FAILS,
        keep_branch="auto/planner-resync",
        keep_url="https://github.com/example/tan-cli/pull/2",
    )
    assert proc.returncode != 0, _fmt(proc)
    assert b"::error::" in proc.stdout, _fmt(proc)
    assert b"refusing to guess" in proc.stdout, _fmt(proc)
    assert call_log.read_text(encoding="utf-8") == "", (
        "no pr comment/close call may happen once the lookup itself failed:\n"
        + _fmt(proc)
    )
