# SPDX-License-Identifier: Apache-2.0
"""Every workflow job must be bounded, and every PR run must supersede itself.

tan-cli#812. `parity.yml` is the heaviest PR-triggered workflow in the repo (six
jobs, one of them a 15-leg `os` x `shard` matrix) and it produced FOUR of the
five required contexts while carrying neither a `concurrency:` group nor a
single `timeout-minutes:` -- the only PR-triggered workflow with neither. A
superseded push therefore ran its previous suite to completion while ci.yml,
clean-host.yml and getting-started.yml cancelled theirs, and a wedged leg on a
required context would have held the PR for GitHub's 360-minute job default.

Two invariants are pinned here, at two DIFFERENT scopes -- and that difference
is deliberate, not an oversight (tan-cli#854/#855 review):

1. CONCURRENCY, scoped to `on: pull_request` workflows only. Every workflow
   that runs `on: pull_request` carries a top-level `concurrency:` whose
   `cancel-in-progress` is enabled for `pull_request`. This is the house rule
   ci.yml:78 and clean-host.yml:170 write out in prose; nothing enforced it,
   which is how parity.yml went without one from birth. "Supersede a
   superseded run" is a PR-specific concept: a `push` tag, a `schedule` cron,
   or a `workflow_dispatch` proof-run has no earlier run of the SAME logical
   change to cancel, so this half stays scoped to `_pull_request_workflows()`
   on purpose -- widening it would either assert nothing meaningful about
   those triggers or invent a semantics ci.yml/clean-host.yml never had.

   `parity.yml`'s group key additionally does NOT collapse every
   `repository_dispatch` run into one group -- the subtle half, covered by its
   own test below. GitHub raises `repository_dispatch` from the DEFAULT
   branch, so a flat `parity-${{ github.ref }}` key -- the obvious "just match
   ci.yml" edit -- gives every dispatched run the same `refs/heads/dev` key
   however different the `client_payload.sdk_ref` under test. Grouping is not
   only about cancellation: a run arriving while another in its group is in
   progress waits, and a third arrival cancels the one that was waiting.
   Three alp-sdk contract pushes in quick succession would leave the middle
   `sdk_ref` with no parity run at all, silently dropping drift coverage for
   exactly the SHA that asked for it -- and tan-cli#485 already records what
   happens when this signal goes unwatched. A dropped run is strictly worse
   than an unwatched one: there is no red left to find.

2. TIMEOUTS, scoped to EVERY workflow file under `.github/workflows/`, not
   just the `pull_request` set. This is the tan-cli#854/#855 widening. #841
   added `test_every_parity_job_is_bounded` below, hardcoded to `parity.yml`'s
   own six jobs; #845 then bounded five more jobs elsewhere (`ci.yml`,
   `version-identity.yml`) that nothing generalised caught. #855 measured the
   OTHER gap this left: eleven jobs outside the `pull_request` set at all
   (`planner-resync.yml`, `python-binaries.yml`, `release-combination.yml`,
   `release.yml`) still inherited GitHub's 360-minute default -- five of them
   on `release.yml`'s TAG path, where a wedge holds an immutable, already-cut
   version number for six hours with nothing re-runnable the way a PR push is.
   A job that never supersedes a sibling run can still hang, so the CONCURRENCY
   restriction above has no bearing on whether the TIMEOUT check should apply
   to it -- unlike invariant 1, there is no PR-specific concept a `schedule` or
   `push: tags` job would be asserting nothing about. The decision #855 asked
   to be recorded explicitly: **timeouts widen to every workflow; concurrency
   stays pull_request-scoped.** `parity.yml`'s own named-tuple check
   (`test_every_parity_job_is_bounded` / `_PARITY_JOBS`) is kept ALONGSIDE the
   generalised one, per #854's own ask -- deleting a job from `parity.yml`
   must still be visible here as a job needing deliberate removal from that
   tuple, not just a silent shrink of the wider net's coverage.

   A `uses:` job (a reusable-workflow caller, e.g. `release.yml`'s `gates` /
   `python-gates`) is excluded from the widened check: GitHub does not accept
   `timeout-minutes` on a job whose body is `uses: ./.github/workflows/x.yml`
   -- the bound lives on THAT workflow's own jobs, covered when this same test
   walks `x.yml` in its own right. Demanding the key there would fail every
   caller job for a key that does nothing (tan-cli#854's own issue text).

Nothing here runs a workflow. It reads the YAML and asserts structure, so a
future tidy-up that flattens the group key or drops a `timeout-minutes` fails
in `tests/gates` rather than on a runner months later.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PARITY = WORKFLOWS / "parity.yml"

#: Upper bound on any single `timeout-minutes`. GitHub's own job default is
#: 360; anything at or above that is not a hang catcher, it is the default
#: wearing a number. pin-move-verify.yml's `verify` sits at 100, the widest
#: value in the repo as of the tan-cli#854/#855 widening -- still comfortably
#: under this cap, so raising it was not needed to land that change. Raise it
#: in the same change that needs it, and say what got slower (this constant's
#: whole reason for being a name instead of a bare 120 in six places).
_MAX_SANE_TIMEOUT_MINUTES = 120

#: Matches a bare integer inside a GitHub Actions expression string, e.g. the
#: `60` and `30` in `${{ inputs.sdk_parity && 60 || 30 }}`. Used only by
#: `_assert_timeout_is_bounded` below, for the one job in this repo that
#: spells `timeout-minutes` as a computed expression rather than a literal
#: int (`ci.yml`'s `python` job -- see that function's docstring).
_TIMEOUT_EXPR_INT_RE = re.compile(r"\d+")

#: Jobs that must carry a timeout, named rather than derived, so that DELETING
#: a job from parity.yml is visible here instead of quietly shrinking what this
#: gate covers. tan-cli#812's own suggested fix listed only the first four --
#: `python-tests` was missing from it, and `python-tests` is the job whose
#: three matrix legs ARE three of the five required contexts.
_PARITY_JOBS = (
    "seam1-plan-shape",
    "seam2",
    "first-blink",
    "python-tests-shard",
    "python-tests",
    "notify-planner-drift",
)


@functools.cache
def _load(path: Path) -> dict:
    # Cached: the generalised job-bounding test below re-reads every workflow
    # file once per JOB it declares (29 cases across 11 files as of this
    # writing), not once per file -- caching keeps that an 11-file parse
    # instead of 29 re-parses of the same handful of files.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> list[str]:
    """The `on:` keys. PyYAML resolves a bare `on:` to the BOOLEAN True (YAML
    1.1 treats `on` as a truthy scalar), so the real key is `True`, not the
    string `"on"` -- read both rather than silently finding nothing."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, dict):
        return list(on)
    if isinstance(on, str):
        return [on]
    return list(on or [])


@functools.cache
def _pull_request_workflows() -> tuple[tuple[str, Path], ...]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if "pull_request" in _triggers(_load(path)):
            found.append((path.name, path))
    return tuple(found)


@functools.cache
def _all_workflows() -> tuple[tuple[str, Path], ...]:
    """Every workflow file under `.github/workflows/`, regardless of trigger.

    tan-cli#855: `planner-resync.yml`, `python-binaries.yml`,
    `release-combination.yml` and `release.yml` carry no `pull_request`
    trigger at all, so `_pull_request_workflows()` above never sees them --
    and eleven of their jobs inherited GitHub's 360-minute default as a
    result, five of them on `release.yml`'s `push: tags` path. The timeout
    half of this gate walks THIS set, not the pull_request one; see the module
    docstring for why that widening is deliberate and the concurrency half is
    not.
    """
    return tuple((path.name, path) for path in sorted(WORKFLOWS.glob("*.yml")))


def _bounded_jobs(workflow: dict) -> dict[str, dict]:
    """Jobs that COULD carry `timeout-minutes` -- i.e. every job except a
    reusable-workflow caller (`uses: ./.github/workflows/x.yml`).

    GitHub does not accept `timeout-minutes` on a `uses:` job: the bound lives
    on the CALLED workflow's own jobs, which this same test asserts on when it
    walks that file in its own right (`release.yml`'s `gates` calls `ci.yml`,
    whose `python`/`shim`/`wheel-floor`/`workflow-security` jobs are already
    covered; `python-gates` calls `parity.yml`, likewise already covered).
    Demanding the key on the CALLER would fail every release for a key that
    changes nothing -- tan-cli#854's own issue text flagged this exact shape
    before any code was written, which is why it is handled here rather than
    discovered as a false positive later.
    """
    jobs = workflow.get("jobs") or {}
    return {job_id: job for job_id, job in jobs.items() if "uses" not in job}


@functools.cache
def _every_workflow_job_cases() -> tuple[tuple[str, str], ...]:
    """Every (workflow filename, job id) pair this repo can be asked to bound,
    across every workflow file. Computed once (not per parametrised case) so
    that collection reads each YAML file exactly once via `_load`'s cache."""
    cases = []
    for name, path in _all_workflows():
        for job_id in _bounded_jobs(_load(path)):
            cases.append((name, job_id))
    return tuple(cases)


def _assert_timeout_is_bounded(workflow_name: str, job_id: str, timeout: object) -> None:
    """Shared assertion for both the generalised test and (implicitly, via the
    same shape) `test_every_parity_job_is_bounded` below.

    Handles one wrinkle a plain `isinstance(timeout, int)` check would get
    wrong: `ci.yml`'s `python` job spells its bound as a GitHub Actions
    expression, `${{ inputs.sdk_parity && 60 || 30 }}`, because the SAME job
    runs two different workloads -- a `pull_request`/`merge_group` run
    measures 6-11 minutes (p100 11m) while the `release.yml` `workflow_call`
    with `sdk_parity: true` measures up to 23.7 minutes (tan-cli#844's own
    changelog fragment, `changelog.d/844.fixed.md`, left a note for exactly
    this generalisation: "this value is a string expression, not an int").
    PyYAML parses that value as the plain string
    `"${{ inputs.sdk_parity && 60 || 30 }}"`, not as either branch's number,
    and this test has no GitHub Actions runtime to evaluate `inputs.*`
    against -- so instead of evaluating the expression, every bare integer
    literal INSIDE it is extracted and bounded individually. That is strictly
    MORE conservative than picking one branch: a future third branch adding an
    out-of-range number fails here even though this test can never know which
    branch a real run takes.
    """
    assert timeout is not None, (
        f"{workflow_name}'s {job_id!r} job declares no `timeout-minutes`, so "
        f"it inherits GitHub's 360-minute default. A wedged leg holds "
        f"whatever this job gates (a PR, a tag, a nightly schedule) for six "
        f"hours instead of failing in bounded time."
    )
    if isinstance(timeout, str):
        literals = [int(n) for n in _TIMEOUT_EXPR_INT_RE.findall(timeout)]
        assert literals, (
            f"{workflow_name}'s {job_id!r} job's timeout-minutes is the "
            f"string {timeout!r} with no integer literal in it at all -- "
            f"this looks like a typo, not a GitHub Actions expression."
        )
        for literal in literals:
            assert 0 < literal <= _MAX_SANE_TIMEOUT_MINUTES, (
                f"{workflow_name}'s {job_id!r} job's timeout-minutes "
                f"expression {timeout!r} contains {literal}, outside "
                f"1..{_MAX_SANE_TIMEOUT_MINUTES}."
            )
        return
    assert isinstance(timeout, int) and 0 < timeout <= _MAX_SANE_TIMEOUT_MINUTES, (
        f"{workflow_name}'s {job_id!r} job has `timeout-minutes: {timeout!r}`, "
        f"outside 1..{_MAX_SANE_TIMEOUT_MINUTES}. A bound at or near GitHub's "
        f"own 360 default is not a hang catcher. If a job genuinely needs "
        f"longer, raise _MAX_SANE_TIMEOUT_MINUTES in the same change and say "
        f"what got slower."
    )


def test_the_every_workflow_job_set_is_not_empty():
    """Anti-vacuity for the generalised test, mirroring the pull_request-only
    version below. If `_bounded_jobs` or the `jobs:` parse ever stopped
    matching, every parametrised case would vanish and the checks would pass
    having examined nothing."""
    cases = _every_workflow_job_cases()
    assert cases, "no (workflow, job) pairs were collected across .github/workflows/ at all"
    names = {name for name, _ in cases}
    for expected in ("release.yml", "python-binaries.yml", "planner-resync.yml"):
        assert expected in names, (
            f"{expected} contributed no bounded job to this gate ({sorted(names)}) "
            f"-- it is one of the four non-pull_request workflows tan-cli#855 "
            f"named as carrying unbounded jobs; if it now has zero bounded "
            f"jobs, `_bounded_jobs`/`_load` stopped parsing it correctly."
        )


@pytest.mark.parametrize(
    "name,job_id",
    _every_workflow_job_cases(),
    ids=[f"{n}::{j}" for n, j in _every_workflow_job_cases()],
)
def test_every_job_in_every_workflow_is_bounded(name, job_id):
    """tan-cli#854/#855: the generalised widening. Walks every job in every
    workflow file (not just the `pull_request` set `_PARITY_JOBS` and
    `test_every_parity_job_is_bounded` below stay scoped to), skipping `uses:`
    callers, and asserts each carries a sane `timeout-minutes`."""
    path = dict(_all_workflows())[name]
    jobs = _bounded_jobs(_load(path))
    assert job_id in jobs, (
        f"{name} has no bounded job {job_id!r} any more (present: "
        f"{sorted(jobs)}) -- this case was collected at collection time and "
        f"the file changed since; re-run pytest to regenerate the case list."
    )
    _assert_timeout_is_bounded(name, job_id, jobs[job_id].get("timeout-minutes"))


def test_the_pull_request_workflow_set_is_not_empty():
    """Anti-vacuity. Every parametrised case below draws from this set; if the
    `on:`-parsing above ever stopped matching (the boolean-True quirk is
    exactly the kind of thing a YAML library change moves), the checks would
    all pass having examined nothing at all."""
    names = [name for name, _ in _pull_request_workflows()]
    assert names, (
        "no workflow under .github/workflows/ parses as running on "
        "`pull_request` -- the `on:` key is not being read correctly (see "
        "_triggers), not that the repo stopped testing pull requests"
    )
    assert "parity.yml" in names, (
        f"parity.yml is not among the `pull_request` workflows ({names}) -- "
        f"it is the subject of this whole gate"
    )


@pytest.mark.parametrize(
    "name", [n for n, _ in _pull_request_workflows()], ids=lambda n: n
)
def test_every_pull_request_workflow_supersedes_its_own_superseded_runs(name):
    workflow = _load(dict(_pull_request_workflows())[name])
    concurrency = workflow.get("concurrency")
    assert concurrency, (
        f"{name} runs on `pull_request` and declares no top-level "
        f"`concurrency:` -- a newer push to a PR leaves the previous run "
        f"going to completion. ci.yml:78 and clean-host.yml:170 state the "
        f"house rule; copy it rather than inventing a new one (and read "
        f"parity.yml's own block first if this workflow has a "
        f"`repository_dispatch` trigger, which changes the group key)."
    )
    cancel = str(concurrency.get("cancel-in-progress", "")).strip()
    assert "pull_request" in cancel or cancel.lower() == "true", (
        f"{name}'s `cancel-in-progress` is {cancel!r}: it neither reads "
        f"`true` nor keys off `github.event_name == 'pull_request'`, so a "
        f"superseded PR run is still not cancelled. The group alone does not "
        f"cancel anything -- it only queues."
    )


def test_parity_never_collapses_dispatched_runs_into_one_group():
    """The regression this gate exists for. A flat `parity-${{ github.ref }}`
    is the tempting "match ci.yml" simplification and it is wrong HERE,
    because `repository_dispatch` fires from the default branch and every
    dispatched run would share that one ref."""
    concurrency = _load(PARITY).get("concurrency")
    assert concurrency and concurrency.get("group"), (
        "parity.yml declares no top-level `concurrency.group` at all, so "
        "there is no key for this test to judge. The sibling test above says "
        "what to add; read parity.yml's own block comment before copying "
        "ci.yml's, because the `repository_dispatch` trigger changes the key."
    )
    group = concurrency["group"]
    assert "github.run_id" in group, (
        f"parity.yml's concurrency group is {group!r} and does not include "
        f"`github.run_id`. Every `repository_dispatch` run is raised from the "
        f"default branch, so a `github.ref`-only key puts them all in one "
        f"group: with one run in progress and two more arriving, the middle "
        f"`client_payload.sdk_ref` gets cancelled while it waits and never "
        f"runs at all. See tan-cli#485 for the cost of an unwatched drift "
        f"signal; an ABSENT one cannot even be watched."
    )
    assert "client_payload" not in group, (
        f"parity.yml's concurrency group is {group!r} and interpolates a "
        f"`client_payload` field. A `repository_dispatch` payload is "
        f"attacker-supplied and a group key is a workflow-level expression "
        f"evaluated before any job starts. `github.run_id` separates the runs "
        f"just as well and puts no untrusted string in that position."
    )
    assert "pull_request" in group, (
        f"parity.yml's concurrency group is {group!r}: it no longer "
        f"distinguishes `pull_request` runs, so PR pushes stopped "
        f"superseding each other -- the original tan-cli#812 defect, back."
    )


@pytest.mark.parametrize("job_id", _PARITY_JOBS)
def test_every_parity_job_is_bounded(job_id):
    """The anti-shrink check tan-cli#854 asked to keep ALONGSIDE the
    generalised `test_every_job_in_every_workflow_is_bounded` above, not
    replaced by it: `_PARITY_JOBS` is a literal tuple precisely so that
    DELETING a job from `parity.yml` is visible here as a job someone must
    deliberately drop from the tuple, rather than a silent shrink of what a
    derived/generalised set happens to cover this run. Four of `parity.yml`'s
    jobs ARE required contexts (tan-cli#812); a wedged leg on one of them
    holds every PR for GitHub's 360-minute default.
    """
    jobs = _load(PARITY)["jobs"]
    assert job_id in jobs, (
        f"parity.yml has no job {job_id!r} (present: {sorted(jobs)}) -- if it "
        f"was renamed, rename it in _PARITY_JOBS too; if it was removed, drop "
        f"it from that tuple deliberately rather than leaving this gate "
        f"asserting nothing about it"
    )
    _assert_timeout_is_bounded("parity.yml", job_id, jobs[job_id].get("timeout-minutes"))
