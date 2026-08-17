# SPDX-License-Identifier: Apache-2.0
"""parity.yml must supersede superseded PR runs, and no job may run unbounded.

tan-cli#812. `parity.yml` is the heaviest PR-triggered workflow in the repo (six
jobs, one of them a 15-leg `os` x `shard` matrix) and it produced FOUR of the
five required contexts while carrying neither a `concurrency:` group nor a
single `timeout-minutes:` -- the only PR-triggered workflow with neither. A
superseded push therefore ran its previous suite to completion while ci.yml,
clean-host.yml and getting-started.yml cancelled theirs, and a wedged leg on a
required context would have held the PR for GitHub's 360-minute job default.

Two things are pinned here, and the second is the one that is easy to undo by
accident:

1. Every workflow that runs `on: pull_request` carries a top-level
   `concurrency:` whose `cancel-in-progress` is enabled for `pull_request`.
   This is the house rule ci.yml:78 and clean-host.yml:170 write out in prose;
   nothing enforced it, which is how parity.yml went without one from birth.

2. `parity.yml`'s group key does NOT collapse every `repository_dispatch` run
   into one group. This is the subtle half. GitHub raises
   `repository_dispatch` from the DEFAULT branch, so a flat
   `parity-${{ github.ref }}` key -- the obvious "just match ci.yml" edit --
   gives every dispatched run the same `refs/heads/dev` key however different
   the `client_payload.sdk_ref` under test. Grouping is not only about
   cancellation: a run arriving while another in its group is in progress
   waits, and a third arrival cancels the one that was waiting. Three alp-sdk
   contract pushes in quick succession would leave the middle `sdk_ref` with no
   parity run at all, silently dropping drift coverage for exactly the SHA that
   asked for it -- and tan-cli#485 already records what happens when this
   signal goes unwatched. A dropped run is strictly worse than an unwatched
   one: there is no red left to find.

Nothing here runs a workflow. It reads the YAML and asserts structure, so a
future tidy-up that flattens the group key or drops a `timeout-minutes` fails
in `tests/gates` rather than on a runner months later.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PARITY = WORKFLOWS / "parity.yml"

#: Upper bound on any single `timeout-minutes`. GitHub's own job default is
#: 360; anything at or above that is not a hang catcher, it is the default
#: wearing a number. seam2/first-blink sit at 60, the widest this file needs.
_MAX_SANE_TIMEOUT_MINUTES = 120

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


def _load(path: Path) -> dict:
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
    jobs = _load(PARITY)["jobs"]
    assert job_id in jobs, (
        f"parity.yml has no job {job_id!r} (present: {sorted(jobs)}) -- if it "
        f"was renamed, rename it in _PARITY_JOBS too; if it was removed, drop "
        f"it from that tuple deliberately rather than leaving this gate "
        f"asserting nothing about it"
    )
    timeout = jobs[job_id].get("timeout-minutes")
    assert timeout is not None, (
        f"parity.yml's {job_id!r} job declares no `timeout-minutes`, so it "
        f"inherits GitHub's 360-minute default. Four of the five required "
        f"contexts come from this file: a wedged leg holds the PR for six "
        f"hours instead of failing in bounded time (tan-cli#812)."
    )
    assert isinstance(timeout, int) and 0 < timeout <= _MAX_SANE_TIMEOUT_MINUTES, (
        f"parity.yml's {job_id!r} job has `timeout-minutes: {timeout!r}`, "
        f"outside 1..{_MAX_SANE_TIMEOUT_MINUTES}. A bound at or near "
        f"GitHub's own 360 default is not a hang catcher. If a job genuinely "
        f"needs longer, raise _MAX_SANE_TIMEOUT_MINUTES in the same change "
        f"and say what got slower."
    )
