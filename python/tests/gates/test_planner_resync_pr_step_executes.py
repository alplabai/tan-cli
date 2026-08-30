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

Two shapes, matching the review's own repro exactly:

* clean (nothing occupies `auto/planner-resync`) -- must reach `git push` and
  exit 0.
* diverted (`auto/planner-resync` already carries a human commit) -- must
  divert to `auto/planner-resync-<suffix>`, leave the human commit on the
  primary branch untouched, and still exit 0.

Mutation-tested against the reviewer's own instruction: re-introducing the
bare (non-`|| true`, conditionally-written) shape reds the clean-run test on
its own assertion (`STEP EXIT` != 0), not an incidental exception.

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

_PR_STEP = "Open or refresh the proposal PR"

#: The runner resolves `${{ ... }}` expressions BEFORE bash ever sees the
#: script -- bash has no idea what that syntax means. None of the handful of
#: expressions inside this step's `run:` text span a `}` (they're plain
#: `github.*` property reads), so a non-greedy single-line match is exact,
#: not an approximation.
_GHA_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


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
    return _GHA_EXPR.sub("TESTVAL", run)


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
    scratch = tmp_path / "scratch"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    _git(scratch, "checkout", "-q", "-B", branch, "origin/dev")
    sha = _commit(scratch, "A Human Reviewer", "human@example.com", "hand-port work", "handport.txt")
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
