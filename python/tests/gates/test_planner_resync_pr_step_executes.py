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


#: tan-cli#1119 review round 3 (nit): read straight out of the real
#: `jobs.propose.env` block (`_workflow()`, already defined above) instead
#: of a hand-copied literal -- a copy here could disagree with the workflow
#: without the workflow ever disagreeing with ITSELF, which is exactly the
#: kind of drift `AUTOMATION_NAME`/`AUTOMATION_EMAIL` being hoisted to
#: job-level `env:` (this same PR) was supposed to make impossible. This is
#: the last copy; every other reader in this module (`git config
#: user.name`/`user.email`, `planner_resync_branch_guard.py
#: --automation-name`/`--automation-email`) already gets it from these two
#: constants, and this makes THEM trace back to the workflow file itself
#: rather than to a second literal living beside it.
_JOB_ENV = _workflow()["jobs"]["propose"]["env"]
_AUTOMATION_NAME = _JOB_ENV["AUTOMATION_NAME"]
_AUTOMATION_EMAIL = _JOB_ENV["AUTOMATION_EMAIL"]


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
    # tan-cli#1119 review (minor): `AUTOMATION_NAME`/`AUTOMATION_EMAIL` moved
    # to job-level `env:` in the real workflow -- this step's `run:` text no
    # longer sets them as local literals, it reads them from the
    # environment. A real GHA job would supply them from the job's own
    # `env:` block; this harness must too, or `set -u` kills the step on an
    # unbound variable.
    env["AUTOMATION_NAME"] = _AUTOMATION_NAME
    env["AUTOMATION_EMAIL"] = _AUTOMATION_EMAIL
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
    workspace: pathlib.Path,
    tmp_path: pathlib.Path,
    gh_script: str,
    *,
    keep_branch: str,
    keep_url: str,
    pr_list_fixture: str = "[]",
    no_jq: bool = False,
    no_python: bool = False,
    check_branch_exit_code: int | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], pathlib.Path]:
    """Run the close step's real `run:` body against a real `gh` stub AND
    (tan-cli#1119) a real `workspace` checkout -- the step now shells out to
    `planner_resync_branch_guard.py --check-branch` per candidate, live
    `git ls-remote`/`git log` and all, so it needs the same real bare
    `origin` + working clone the PR-step tests already build (`workspace`
    fixture), not just a `gh` stub. Runs with `workspace` as cwd and
    `GITHUB_WORKSPACE` pointed at it, matching what the real step sees
    (`actions/checkout`'s default target directory).

    `check_branch_exit_code`, when given, replaces the `python` stub with
    one that ignores its arguments and exits with that code unconditionally
    -- tan-cli#1119 review (major 1): the ONLY way to drive the close step's
    own `check_rc` handling through every code the real
    `planner_resync_branch_guard.py --check-branch` invocation can produce
    (0, 1, 2, or a subprocess failure like 127/137) without needing a
    different real git shape for each one."""
    bindir = tmp_path / "close-bin"
    bindir.mkdir()
    gh_stub = bindir / "gh"
    gh_stub.write_text(gh_script, encoding="utf-8")
    gh_stub.chmod(0o755)
    # tan-cli#1119: the step now runs `python python/scripts/...
    # --check-branch` itself -- same reasoning as `fake_bin`'s own python
    # stub above (a plain `python` on PATH might not be the interpreter this
    # test suite is running under).
    if no_python:
        python_stub = None
    elif check_branch_exit_code is not None:
        python_stub = bindir / "python"
        python_stub.write_text(
            "#!/usr/bin/env bash\n"
            f"echo 'stub python: forcing --check-branch to exit "
            f"{check_branch_exit_code}' >&2\n"
            f"exit {check_branch_exit_code}\n",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
    else:
        python_stub = bindir / "python"
        python_stub.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        python_stub.chmod(0o755)
    # tan-cli#1109 review round 3 / tan-cli#1119 review (blocker): `no_jq`
    # restricts PATH to `bindir` alone so `command -v jq` genuinely finds
    # nothing -- but `bindir` must still carry a REAL `jq` in that case
    # (copied in below) whenever the test is not ALSO exercising
    # `no_python`, or the step would die at the `jq` check before ever
    # reaching the `python` one this test wants to isolate. Symmetric for
    # `no_python`: PATH-restricted, but `jq` still present unless `no_jq`
    # too.
    if no_jq:
        jq_stub = None
    else:
        real_jq = shutil.which("jq")
        if real_jq is not None:
            jq_stub = bindir / "jq"
            jq_stub.write_text(f'#!/usr/bin/env bash\nexec "{real_jq}" "$@"\n', encoding="utf-8")
            jq_stub.chmod(0o755)
        else:
            jq_stub = None
    call_log = tmp_path / "gh_calls.log"
    call_log.write_text("", encoding="utf-8")
    fixture = tmp_path / "pr_list_fixture.json"
    fixture.write_text(pr_list_fixture, encoding="utf-8")

    env = dict(os.environ)
    # `no_jq`/`no_python` both restrict PATH to `bindir` ALONE (not prepend
    # to the real PATH) so the missing tool is genuinely missing -- `jq`/
    # `python` live under the real PATH this process inherited, so
    # prepending would leave them reachable regardless. `printf`/`command`
    # are shell builtins, so the step's own presence checks (`command -v
    # jq`, `command -v python`) still run either way.
    env["PATH"] = (
        str(bindir)
        if (no_jq or no_python)
        else f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    )
    env["GH_TOKEN"] = "fake-token"
    env["GITHUB_REPOSITORY"] = "example/tan-cli"
    env["GITHUB_WORKSPACE"] = str(workspace)
    env["KEEP_BRANCH"] = keep_branch
    env["KEEP_URL"] = keep_url
    env["GH_CALL_LOG"] = str(call_log)
    env["GH_PR_LIST_FIXTURE"] = str(fixture)
    # tan-cli#1119 review (minor): job-level `env:` in the real workflow --
    # see `_run_step`'s own copy of this comment.
    env["AUTOMATION_NAME"] = _AUTOMATION_NAME
    env["AUTOMATION_EMAIL"] = _AUTOMATION_EMAIL

    # tan-cli#1109 review round 3 / tan-cli#1119 review (blocker): `no_jq`'s
    # (and `no_python`'s) restricted PATH (bindir only, so `command -v jq`/
    # `command -v python` genuinely find nothing) would also hide `bash`
    # itself if bash were looked up BY NAME through that same PATH --
    # `jq`/`python` and `bash` live in the same directory on this box
    # (`/usr/bin`), so excluding jq's/python's directory would exclude
    # bash's too. Resolved to its absolute path from the UNRESTRICTED, real
    # environment before `env` is built, so launching bash itself never
    # needs a PATH lookup at all -- only what runs INSIDE the script (the
    # step's own `command -v jq`/`command -v python` checks) is affected.
    bash_exe = shutil.which("bash") or "/bin/bash"

    proc = subprocess.run(
        [bash_exe, "-x", "-c", _close_step_run()],
        cwd=workspace,
        env=env,
        capture_output=True,
    )
    return proc, call_log


#: tan-cli#1109 review round 2: the close step's `gh pr list` call itself no
#: longer carries a `--jq` -- the filtering (prefix/self/author/cross-repo)
#: moved to a SEPARATE local `jq -r ... | @tsv` invocation over the raw
#: `--json` output. This fake's "pr list" case therefore just returns the
#: fixture VERBATIM (`cat`), matching what a real `gh pr list --json ...`
#: (no `--jq`) returns -- the step's own subsequent `jq` call is the REAL
#: system `jq` binary either way, so the actual filter expression (prefix,
#: self, author, cross-repo) is exercised character-for-character regardless
#: of which `gh` answered the raw listing. tan-cli#1119: the occupied-branch
#: exclusion is no longer part of that `jq` filter at all -- it is now a
#: live `planner_resync_branch_guard.py --check-branch` call per surviving
#: candidate, asked against the REAL git state in `workspace` (not a value
#: this fake `gh` or the fixture below can answer).
_FAKE_GH_RAW_JSON = """#!/usr/bin/env bash
case "$1 $2" in
  "pr list") cat "$GH_PR_LIST_FIXTURE" ;;
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
#: "app/github-actions"}`, measured tan-cli#1109 review) plus every shape
#: the close step must refuse:
#:   * #1103 -- the literal incident the review round-2 finding names: BOT-
#:     authored (opened by `app/github-actions`), on the PRIMARY branch
#:     `auto/planner-resync`, but a HUMAN later pushed a hand-port commit
#:     onto that same branch -- which is exactly what makes the guard divert
#:     and is exactly why this PR must be EXCLUDED (the test that uses this
#:     fixture pushes that hand-port commit for real, onto `workspace`'s
#:     origin, so the live `--check-branch` call this step now makes finds
#:     it) even though it passes the author/cross-repo checks on its own.
#:     This is real PR #1103's own measured field values, not a fixture
#:     invented for the test.
#:   * #1106/#1107 -- bot-authored, same-repo, diverted proposals on
#:     branches that carry no foreign commit right now: MUST close.
#:   * #2001 -- a HUMAN'S PR on a branch that also happens to start with
#:     `auto/planner-resync` (the literal #1002/#996 "hand-port work parked
#:     on this branch name" shape): MUST NOT close.
#:   * #2002 -- author claims to be the bot but `isCrossRepository` is true (a
#:     fork cannot really forge the author identity, but the check is free):
#:     MUST NOT close.
#:   * #1108 -- the branch this run just opened/refreshed (`KEEP_BRANCH`
#:     below): MUST NOT close itself.
_PR_LIST_FIXTURE_MIXED = """[
  {"number": 1103, "headRefName": "auto/planner-resync",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false},
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


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_closes_bot_prs_but_never_a_human_cross_repo_or_occupied_one(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 fault 3 + review rounds 1 and 2 + tan-cli#1119: once one
    proposal is open/refreshed, every OTHER open, BOT-AUTHORED, same-repo,
    NOT-OCCUPIED PR whose head starts with `auto/planner-resync` is
    superseded and closed; a PR that merely shares the branch prefix but is
    NOT the bot's own -- a human's hand-port work (#2001) or a cross-repo PR
    (#2002) -- is never touched, and NEITHER is a bot-authored PR sitting on
    a branch that CURRENTLY carries a foreign commit (#1103 -- the literal
    incident: opened by the bot, then a human's hand-port commit landed on
    the same branch -- AND #1106, occupied here too so this also proves the
    exclusion holds for MORE THAN ONE occupied branch at once, not just the
    single-occupied case). All exclusion reasons plus the "still closes a
    legitimate sibling" control (#1107) asserted in one pass.

    tan-cli#1119: unlike the pre-fix version of this test, "occupied" is not
    a symbolic string passed alongside the fixture -- #1103's and #1106's
    hand-port commits are pushed for REAL onto `workspace`'s origin, and the
    close step's own live `--check-branch` call is what finds them."""
    bare = tmp_path / "origin.git"
    _push_foreign_commit_on(bare, "auto/planner-resync", tmp_path)
    _push_foreign_commit_on(bare, "auto/planner-resync-eaa79695", tmp_path)

    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_MIXED,
    )
    assert proc.returncode == 0, _fmt(proc)
    calls = call_log.read_text(encoding="utf-8")

    # Direction 1: the bot's own OTHER, NOT-occupied proposal is superseded
    # and closed -- the cap still works even with two occupied exclusions in
    # place.
    assert "pr comment 1107" in calls, calls
    assert "pr close 1107" in calls, calls

    # Direction 2: #1103 AND #1106 -- both bot-authored, same-repo, on a
    # branch that currently carries a foreign commit -- must never be closed
    # or commented on. This is the review round-2 finding (author/cross-repo
    # filters alone are NOT enough, since both pass them) proved for TWO
    # simultaneously occupied branches, not just one.
    assert "pr comment 1103" not in calls, calls
    assert "pr close 1103" not in calls, calls
    assert "pr comment 1106" not in calls, calls
    assert "pr close 1106" not in calls, calls
    # A human's PR (same branch prefix, foreign author) must never be
    # touched, no matter how it got that branch name.
    assert "pr comment 2001" not in calls, calls
    assert "pr close 2001" not in calls, calls
    # ...and neither must a cross-repository PR, even one claiming the bot's
    # own author identity.
    assert "pr comment 2002" not in calls, calls
    assert "pr close 2002" not in calls, calls
    # ...nor the branch this very run just opened/refreshed.
    assert "pr comment 1108" not in calls, calls
    assert "pr close 1108" not in calls, calls


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_leaves_an_unoccupied_bot_sibling_untouched_when_nothing_is_occupied(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """The other half of the occupied-exclusion, isolated. Two DIFFERENT
    "not occupied" shapes, both proven, not just one:

    * #1103 and #1107's branches are never pushed at all -- proves
      `_remote_branch_exists` returning False (a branch this run's fixture
      names but never actually pushed) degrades safely to "not occupied".
    * #1106's branch (tan-cli#1119 review, major 2) carries a REAL commit
      under the automation's own identity, pushed for real -- the everyday
      shape this cap actually closes in production (a genuine prior-run
      proposal, not an absent name), and the only one of the three that
      exercises `_identity_is_foreign` returning False end-to-end on the
      close path. A prior version of this test left every sibling branch
      absent, so the cap was only ever proven against "the branch does not
      exist", never against "the branch exists and is genuinely clean"."""
    bare = tmp_path / "origin.git"
    _push_automation_commit_on(bare, "auto/planner-resync-eaa79695", tmp_path)

    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_MIXED,
    )
    assert proc.returncode == 0, _fmt(proc)
    calls = call_log.read_text(encoding="utf-8")
    # #1103 is bot-authored and same-repo -- with nothing occupied this run,
    # it is NOT protected and closes like any other bot sibling. (This is
    # the realistic shape too: #1103 would only ever be occupied on a run
    # where the guard actually found it foreign-occupied; a run where
    # nothing is occupied has no reason to protect it.)
    assert "pr close 1103" in calls, calls
    # #1106's branch exists and carries a genuine automation commit -- still
    # closes, proving the "clean, not absent" shape works too.
    assert "pr close 1106" in calls, calls
    assert "pr close 1107" in calls, calls


# ----------------------------------------------- tan-cli#1119: probe E + TOCTOU

#: tan-cli#1119 probe E, verbatim from the issue: a PRIOR run's diverted
#: branch (`auto/planner-resync-eaa79695` -- NOT `auto/planner-resync`
#: itself, and NOT a candidate name THIS run's own branch-clobber guard
#: invocation ever tries) that a human has since adopted, sitting under an
#: OLDER bot-opened PR (#2300). #1107 is a legitimate, unoccupied sibling --
#: the cap must still close it.
_PR_LIST_FIXTURE_PRIOR_RUN_DIVERTED = """[
  {"number": 2300, "headRefName": "auto/planner-resync-9f10cba2",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false},
  {"number": 1107, "headRefName": "auto/planner-resync-bbfc6c5a",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false}
]"""


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_excludes_a_prior_run_diverted_branch_a_human_has_adopted(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1119 probe E: `occupied_branches` (tan-cli#1109's fix) only
    ever covers THIS run's own candidate walk (`decide_branch` tries
    `auto/planner-resync`, then `<primary>-<this run's own suffix>`, then
    `-2`, ...) -- a PREVIOUS run's diverted branch name
    (`auto/planner-resync-9f10cba2`, say, picked by a run that targeted a
    DIFFERENT alp-sdk sha) is never one of those candidates, so a human who
    adopts THAT branch is invisible to a close step that only consults a
    snapshot built from this run's own guard invocation.

    Measured pre-fix (real YAML step body, real `jq`, this exact fixture):
    `closed=['2300', '1107']` -- #2300 must not be in that list. Post-fix,
    the close step asks the guard's own authorship test about
    `auto/planner-resync-9f10cba2` LIVE, at close time, rather than
    consulting any run's snapshot -- so it excludes #2300 regardless of
    which run (this one, or some earlier one) the branch name came from.
    #1107 (a genuinely unoccupied sibling) still closes -- the cap keeps
    working."""
    bare = tmp_path / "origin.git"
    # The human's adoption of the prior run's diverted branch -- pushed for
    # real, so the close step's live `--check-branch` call is what finds it,
    # not a value this test hands it directly.
    _push_foreign_commit_on(bare, "auto/planner-resync-9f10cba2", tmp_path)

    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_PRIOR_RUN_DIVERTED,
    )
    assert proc.returncode == 0, _fmt(proc)
    calls = call_log.read_text(encoding="utf-8")

    # The prior-run diverted branch a human adopted: excluded.
    assert "pr comment 2300" not in calls, calls
    assert "pr close 2300" not in calls, calls
    # The cap still works: an unrelated, unoccupied bot sibling still closes.
    assert "pr comment 1107" in calls, calls
    assert "pr close 1107" in calls, calls


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_catches_a_branch_adopted_in_the_toctou_window(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1119: the TOCTOU half of the same defect. `occupied_branches`
    (tan-cli#1109) is a SNAPSHOT computed at the guard step, consulted here,
    LATER -- a branch a human adopts in the window between those two moments
    was invisible to a snapshot-based check no matter how fresh the snapshot
    was when it was taken.

    tan-cli#1119 review (minor): an earlier version of this test only
    simulated "the guard step already ran" as a comment -- nothing actually
    ran before the human's push, which made it mechanically identical to
    `test_close_step_excludes_a_prior_run_diverted_branch_a_human_has_adopted`
    with a different branch name. This version executes the REAL "Open or
    refresh the proposal PR" step first (`_run_step`, the same helper the
    PR-step tests above use), for real, against `workspace` -- capturing the
    genuine `occupied_branches` snapshot it produces (empty: nothing is
    occupied at that moment) as proof of what a pre-tan-cli#1119 close step
    would have trusted. ONLY AFTER that real step completes does the human
    adopt a wholly separate SIBLING branch (an earlier run's own proposal,
    never a candidate in THIS guard-step invocation's own walk either) --
    exactly inside the TOCTOU window between "the guard step ran" and "the
    close step runs". The close step must still exclude it, DESPITE a
    snapshot that says (truthfully, for the moment it was taken) nothing was
    occupied."""
    # "The guard step already ran" -- for real. A clean, non-diverted run:
    # nothing occupies `auto/planner-resync` yet, so `decide_branch` picks
    # it outright and the step pushes under the automation's own identity.
    guard_sdk_sha = "cafef00d12345678"
    guard_proc = _run_step(workspace, fake_bin, tmp_path, guard_sdk_sha)
    assert guard_proc.returncode == 0, _fmt(guard_proc)
    guard_outputs = (tmp_path / "github_output").read_text(encoding="utf-8")
    assert "diverted=false\n" in guard_outputs, guard_outputs
    # The snapshot a pre-tan-cli#1119 close step would have consumed: empty,
    # because nothing was occupied at the moment the guard step looked --
    # correct for its own moment, and exactly why a snapshot-based check
    # cannot see what happens next.
    assert "occupied_branches=\n" in guard_outputs, guard_outputs
    keep_branch = "auto/planner-resync"
    keep_url = "https://github.com/example/tan-cli/pull/1"

    # THE TOCTOU WINDOW: strictly AFTER the guard step above completed, a
    # human adopts a SEPARATE, pre-existing sibling branch -- an earlier
    # run's own proposal, never a candidate this guard-step invocation's own
    # cascade touched (it never diverted, so it never even looked at
    # `auto/planner-resync-toctou01`).
    bare = tmp_path / "origin.git"
    _push_foreign_commit_on(bare, "auto/planner-resync-toctou01", tmp_path)

    pr_list_fixture = """[
      {"number": 4200, "headRefName": "auto/planner-resync-toctou01",
       "author": {"login": "app/github-actions", "is_bot": true},
       "isCrossRepository": false}
    ]"""

    # "The close step runs" -- later still, against the now-adopted branch,
    # and (unlike the pre-fix mechanism) without ever consuming the
    # `occupied_branches` snapshot captured above at all.
    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch=keep_branch,
        keep_url=keep_url,
        pr_list_fixture=pr_list_fixture,
    )
    assert proc.returncode == 0, _fmt(proc)
    calls = call_log.read_text(encoding="utf-8")
    assert "pr comment 4200" not in calls, (
        "a branch adopted in the TOCTOU window between the guard step and "
        "this step must not be closed:\n" + calls
    )
    assert "pr close 4200" not in calls, (
        "a branch adopted in the TOCTOU window between the guard step and "
        "this step must not be closed:\n" + calls
    )


# --------------------------- tan-cli#1119 review (blocker + major 1): check_rc

_CHECK_RC_FIXTURE = """[
  {"number": 9000, "headRefName": "auto/planner-resync-checkrc",
   "author": {"login": "app/github-actions", "is_bot": true},
   "isCrossRepository": false}
]"""


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
@pytest.mark.parametrize(
    "check_rc,expect_closed,expect_step_ok",
    [
        # 0 = "not occupied" (the real guard's own answer for a genuinely
        # clean or nonexistent branch) -- the ONLY code that may close.
        (0, True, True),
        # 1 = "occupied" -- a routine, expected skip; the step keeps
        # running and stays green.
        (1, False, True),
        # 2 = the real guard's own "could not determine" refusal --
        # (BranchGuardError OR argparse's ap.error, see the guard's own
        # module docstring) must abort the whole step, not skip just this
        # candidate.
        (2, False, False),
        # tan-cli#1119 review (blocker): 127 = "command not found" -- the
        # single most likely real-world shape of "`python` resolved on
        # PATH but the interpreter it points to could not run the script"
        # (a broken venv, a bad shebang inside a container image). Measured
        # PRE-fix: this fell through the old `-eq 2`/`-eq 1` allow-list
        # straight to `gh pr close`, closing #9000 silently, step rc 0, no
        # `::error::` -- exactly the failure this parametrization exists to
        # pin shut.
        (127, False, False),
        # tan-cli#1119 review (blocker): 137 = 128+SIGKILL -- an OOM-killed
        # subprocess, the other most likely real-world failure for a
        # short-lived `python` invocation on a loaded runner. Same pre-fix
        # silent-close shape as 127.
        (137, False, False),
    ],
)
def test_close_step_treats_every_check_branch_exit_code_correctly(
    workspace: pathlib.Path,
    tmp_path: pathlib.Path,
    check_rc: int,
    expect_closed: bool,
    expect_step_ok: bool,
):
    """tan-cli#1119 review (major 1): nothing exercised `check_rc` at all --
    `grep -n "check_rc" python/tests/gates/test_planner_resync_pr_step_executes.py`
    returned nothing
    before this test existed, which is exactly why the blocker (every exit
    code outside {1,2} fell through to `gh pr close`) survived a suite that
    is otherwise mutation-proved. A `--check-branch` stub parametrised over
    every code the real guard can produce (0, 1, 2) plus the two realistic
    subprocess-failure codes the review measured directly (127, 137):
    `gh pr close` must fire for 0 and ONLY 0; every other code must leave
    #9000 untouched, and only 0/1 may leave the step itself green (rc 0) --
    2/127/137 must abort the whole step with an `::error::`."""
    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_CHECK_RC_FIXTURE,
        check_branch_exit_code=check_rc,
    )
    calls = call_log.read_text(encoding="utf-8")

    if expect_closed:
        assert "pr comment 9000" in calls, _fmt(proc) + "\n" + calls
        assert "pr close 9000" in calls, _fmt(proc) + "\n" + calls
    else:
        assert "pr comment 9000" not in calls, (
            f"check_rc={check_rc} must not close #9000:\n"
            + _fmt(proc)
            + "\n"
            + calls
        )
        assert "pr close 9000" not in calls, (
            f"check_rc={check_rc} must not close #9000:\n"
            + _fmt(proc)
            + "\n"
            + calls
        )

    if expect_step_ok:
        assert proc.returncode == 0, _fmt(proc)
    else:
        assert proc.returncode != 0, _fmt(proc)
        assert b"::error::" in proc.stdout, _fmt(proc)


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_errors_loudly_when_python_is_missing_instead_of_proceeding_silently(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1119 review (blocker): the same `command -v jq` presence-check
    shape, now also for `python` -- the step's per-branch loop shells out to
    it directly (`python python/scripts/planner_resync_branch_guard.py
    --check-branch ...`), so a `python`-less runner must refuse loudly
    before ever reaching a `gh pr close`, not fall through to whatever a
    bare "command not found" happens to do inside the loop."""
    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_MIXED,
        no_python=True,
    )
    assert proc.returncode != 0, _fmt(proc)
    assert b"::error::" in proc.stdout, _fmt(proc)
    assert b"python is not on PATH" in proc.stdout, _fmt(proc)
    assert call_log.read_text(encoding="utf-8") == "", (
        "no pr comment/close call may happen when python itself is missing:\n"
        + _fmt(proc)
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_warns_when_the_open_pr_page_is_full(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 review round 2 (nit): `--limit 100` is the page, not a
    guaranteed complete list -- past 100 total open PRs (of ANY kind, not
    just planner-resync ones) an older proposal this cap is meant to
    supersede can age off page 1 with no signal. A full page (exactly 100
    returned) must warn, loudly, rather than silently under-deliver."""
    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture='[{"number": 3000, "headRefName": "unrelated/branch-0", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3001, "headRefName": "unrelated/branch-1", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3002, "headRefName": "unrelated/branch-2", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3003, "headRefName": "unrelated/branch-3", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3004, "headRefName": "unrelated/branch-4", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3005, "headRefName": "unrelated/branch-5", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3006, "headRefName": "unrelated/branch-6", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3007, "headRefName": "unrelated/branch-7", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3008, "headRefName": "unrelated/branch-8", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3009, "headRefName": "unrelated/branch-9", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3010, "headRefName": "unrelated/branch-10", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3011, "headRefName": "unrelated/branch-11", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3012, "headRefName": "unrelated/branch-12", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3013, "headRefName": "unrelated/branch-13", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3014, "headRefName": "unrelated/branch-14", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3015, "headRefName": "unrelated/branch-15", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3016, "headRefName": "unrelated/branch-16", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3017, "headRefName": "unrelated/branch-17", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3018, "headRefName": "unrelated/branch-18", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3019, "headRefName": "unrelated/branch-19", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3020, "headRefName": "unrelated/branch-20", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3021, "headRefName": "unrelated/branch-21", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3022, "headRefName": "unrelated/branch-22", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3023, "headRefName": "unrelated/branch-23", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3024, "headRefName": "unrelated/branch-24", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3025, "headRefName": "unrelated/branch-25", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3026, "headRefName": "unrelated/branch-26", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3027, "headRefName": "unrelated/branch-27", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3028, "headRefName": "unrelated/branch-28", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3029, "headRefName": "unrelated/branch-29", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3030, "headRefName": "unrelated/branch-30", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3031, "headRefName": "unrelated/branch-31", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3032, "headRefName": "unrelated/branch-32", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3033, "headRefName": "unrelated/branch-33", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3034, "headRefName": "unrelated/branch-34", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3035, "headRefName": "unrelated/branch-35", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3036, "headRefName": "unrelated/branch-36", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3037, "headRefName": "unrelated/branch-37", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3038, "headRefName": "unrelated/branch-38", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3039, "headRefName": "unrelated/branch-39", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3040, "headRefName": "unrelated/branch-40", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3041, "headRefName": "unrelated/branch-41", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3042, "headRefName": "unrelated/branch-42", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3043, "headRefName": "unrelated/branch-43", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3044, "headRefName": "unrelated/branch-44", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3045, "headRefName": "unrelated/branch-45", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3046, "headRefName": "unrelated/branch-46", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3047, "headRefName": "unrelated/branch-47", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3048, "headRefName": "unrelated/branch-48", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3049, "headRefName": "unrelated/branch-49", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3050, "headRefName": "unrelated/branch-50", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3051, "headRefName": "unrelated/branch-51", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3052, "headRefName": "unrelated/branch-52", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3053, "headRefName": "unrelated/branch-53", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3054, "headRefName": "unrelated/branch-54", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3055, "headRefName": "unrelated/branch-55", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3056, "headRefName": "unrelated/branch-56", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3057, "headRefName": "unrelated/branch-57", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3058, "headRefName": "unrelated/branch-58", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3059, "headRefName": "unrelated/branch-59", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3060, "headRefName": "unrelated/branch-60", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3061, "headRefName": "unrelated/branch-61", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3062, "headRefName": "unrelated/branch-62", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3063, "headRefName": "unrelated/branch-63", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3064, "headRefName": "unrelated/branch-64", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3065, "headRefName": "unrelated/branch-65", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3066, "headRefName": "unrelated/branch-66", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3067, "headRefName": "unrelated/branch-67", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3068, "headRefName": "unrelated/branch-68", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3069, "headRefName": "unrelated/branch-69", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3070, "headRefName": "unrelated/branch-70", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3071, "headRefName": "unrelated/branch-71", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3072, "headRefName": "unrelated/branch-72", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3073, "headRefName": "unrelated/branch-73", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3074, "headRefName": "unrelated/branch-74", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3075, "headRefName": "unrelated/branch-75", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3076, "headRefName": "unrelated/branch-76", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3077, "headRefName": "unrelated/branch-77", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3078, "headRefName": "unrelated/branch-78", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3079, "headRefName": "unrelated/branch-79", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3080, "headRefName": "unrelated/branch-80", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3081, "headRefName": "unrelated/branch-81", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3082, "headRefName": "unrelated/branch-82", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3083, "headRefName": "unrelated/branch-83", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3084, "headRefName": "unrelated/branch-84", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3085, "headRefName": "unrelated/branch-85", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3086, "headRefName": "unrelated/branch-86", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3087, "headRefName": "unrelated/branch-87", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3088, "headRefName": "unrelated/branch-88", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3089, "headRefName": "unrelated/branch-89", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3090, "headRefName": "unrelated/branch-90", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3091, "headRefName": "unrelated/branch-91", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3092, "headRefName": "unrelated/branch-92", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3093, "headRefName": "unrelated/branch-93", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3094, "headRefName": "unrelated/branch-94", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3095, "headRefName": "unrelated/branch-95", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3096, "headRefName": "unrelated/branch-96", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3097, "headRefName": "unrelated/branch-97", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3098, "headRefName": "unrelated/branch-98", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}, {"number": 3099, "headRefName": "unrelated/branch-99", "author": {"login": "someone", "is_bot": false}, "isCrossRepository": false}]',
    )
    assert proc.returncode == 0, _fmt(proc)
    assert b"::warning::" in proc.stdout, _fmt(proc)
    assert b"exactly 100 open PRs" in proc.stdout, _fmt(proc)
    # None of the 100 unrelated PRs match the auto/planner-resync prefix, so
    # the warning must not be accompanied by any close call.
    assert call_log.read_text(encoding="utf-8") == "", call_log.read_text(
        encoding="utf-8"
    )


def test_close_step_errors_loudly_when_jq_is_missing_instead_of_proceeding_silently(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """tan-cli#1109 review round 3 (nit): without the presence check, a
    missing `jq` dies inside the FIRST `$(... | jq ...)` assignment under
    `set -e` -- fail-closed (the step never reaches a `gh pr close`) but
    SILENT: no `::error::` naming what happened, just a bare nonzero exit,
    exactly the shape this file's own `gh pr list` failure already guards
    against with an explicit message. This test does NOT need real `jq`
    to be meaningful (it is asserting what happens in its absence), so it
    carries no jq skipif -- it is the one close-step test that must pass
    on a box WITHOUT jq, not skip."""
    proc, call_log = _run_close_step(
        workspace,
        tmp_path,
        _FAKE_GH_RAW_JSON,
        keep_branch="auto/planner-resync-5c33ef04",
        keep_url="https://github.com/example/tan-cli/pull/1108",
        pr_list_fixture=_PR_LIST_FIXTURE_MIXED,
        no_jq=True,
    )
    assert proc.returncode != 0, _fmt(proc)
    assert b"::error::" in proc.stdout, _fmt(proc)
    assert b"jq is not on PATH" in proc.stdout, _fmt(proc)
    assert call_log.read_text(encoding="utf-8") == "", (
        "no pr comment/close call may happen when jq itself is missing:\n"
        + _fmt(proc)
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq to filter with")
def test_close_step_refuses_to_guess_when_gh_pr_list_fails(
    workspace: pathlib.Path, tmp_path: pathlib.Path
):
    """Same fail-closed shape as every other `gh ... | jq` lookup in this
    job (tan-cli#920): a transient `gh` error must not be read as "no other
    proposal exists" and silently leave a stale one open.

    tan-cli#1109 review round 3 (nit): missed in round 2's sweep -- the step
    now runs `jq` before it ever reaches `gh pr list` (the presence check),
    so this test needs `jq` on PATH too, same as the others, or it fails
    outright instead of skipping on a dev box without one."""
    proc, call_log = _run_close_step(
        workspace,
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
