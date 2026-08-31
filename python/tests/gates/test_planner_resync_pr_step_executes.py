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


def _run_step(
    workspace: pathlib.Path, fake_bin: pathlib.Path, tmp_path: pathlib.Path, sdk_sha: str
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
    env["RC"] = "0"
    env["GATE_VERDICT"] = "PASS"
    env["GH_TOKEN"] = "fake-token"

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
) -> str:
    values = {
        "steps.pr.outputs.diverted": "true" if diverted else "false",
        "steps.pr.outputs.occupied_count || 1": "1",
        "steps.pr.outputs.url || 'the job summary'": url,
        "steps.pr.outputs.url || steps.issue.outputs.url || 'the job summary'": url,
        "steps.resync.outputs.rc": rc,
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
