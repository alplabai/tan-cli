# SPDX-License-Identifier: Apache-2.0
"""`regen_module_size_budget.py --check` used to run in NO workflow at all --
its own module docstring said so, verbatim, until tan-cli#907. The only thing
that ever exercised the sidecar's exact-match check was a human running it by
hand.

## Why that was a real gap, not a hypothetical one

`module_size_budget.generated.json` was a single JSON object, deliberately NOT
`merge=union`'d in `.gitattributes` -- unioning two JSON documents that both
add a trailing key can leave two sibling entries with no comma between them,
which is invalid JSON. (Its former sibling `MODULE_SIZE_BUDGET_LOG.md` DID
carry `merge=union` from tan-cli#939 through tan-cli#907, when it was retired:
that file is now frozen, superseded by the one-file-per-entry
`MODULE_SIZE_BUDGET_LOG.d/`, which needs no merge attribute at all.) So a real
`git merge` on it either conflicted visibly, which a human resolves, or --
measured directly, a plain `git merge` of two branches editing different keys
of a shared JSON object -- stitched both DISJOINT edits into one syntactically
valid, semantically STALE JSON object with NO conflict marker at all. There is
nothing for `test_no_conflict_markers.py` to catch and nothing for a
conflict-resolving human to notice, because there is no conflict to resolve.

tan-cli#1057 replaced that file with one record per measured module under
`module_size_budget.d/`, which removes the COMMON case (two branches touching
different modules now write different paths) but not this step's reason to
exist. The silent-staleness shape survives the split: a merge that takes one
side's edit to a module together with the other side's record for it produces
exactly the same marker-free, semantically stale result, and only a
re-measurement can see it. The step's `run:` line is unchanged; only its
`name:` moved off the retired filename.

The existing ratchet tests in `test_module_size_budget.py` do eventually
catch that drift -- but as a cluster of unrelated-looking failures spread
across several of that file's own tests, in whichever of the two CI legs
happens to run `tests/gates`, rather than one targeted diagnostic. `--check`
catches the SAME drift directly, in one step, with one message ("... is
stale: ... Run `python scripts/regen_module_size_budget.py` to refresh it"),
naming the module whose record moved
-- before either leg's `tests/gates` collection even starts.

## What this file pins

This file reads the workflow YAML; it runs nothing. It exists so a future
edit that drops the `--check` step (or moves it out of the one job in each
workflow that actually runs `tests/gates`) fails HERE, in review, instead of
being rediscovered the next time a stale sidecar slips through and someone
has to re-diagnose the same cluster of unrelated-looking failures from
scratch.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PARITY = REPO_ROOT / ".github" / "workflows" / "parity.yml"

#: The two -- and only two -- CI legs that run `python/tests/gates` at all
#: (see `ci.yml`'s `python` job comment and `parity.yml`'s `seam1-plan-shape`
#: job comment, both of which say so explicitly). A `--check` step anywhere
#: else would not be running ahead of the leg it exists to pre-empt.
_LEGS = {CI: "python", PARITY: "seam1-plan-shape"}


def _steps(workflow: Path, job_id: str) -> list[dict]:
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert job_id in jobs, f"{workflow.name} has no job {job_id!r} -- has it been renamed?"
    return jobs[job_id]["steps"]


def _check_step_index(steps: list[dict]) -> int | None:
    for i, step in enumerate(steps):
        run = step.get("run", "")
        if "regen_module_size_budget.py" in run and "--check" in run:
            return i
    return None


def _named_step_index(steps: list[dict], *, name_contains: str) -> int:
    for i, step in enumerate(steps):
        if name_contains in step.get("name", ""):
            return i
    raise AssertionError(f"no step with {name_contains!r} in its name")


def test_every_tests_gates_leg_runs_regen_check():
    for workflow, job_id in _LEGS.items():
        steps = _steps(workflow, job_id)
        idx = _check_step_index(steps)
        assert idx is not None, (
            f"{workflow.name}'s {job_id!r} job must run "
            "`regen_module_size_budget.py --check` (tan-cli#907) -- this is "
            "one of the only two CI legs that runs python/tests/gates, and "
            "without this step a git-merge-stale module_size_budget.d/ record "
            "surfaces only as a cluster of unrelated-looking ratchet-test "
            "failures instead of one targeted diagnostic"
        )


def test_regen_check_runs_before_the_slow_ratchet_it_pre_empts():
    """Order is the entire value proposition here: a step that runs AFTER
    the pytest collection it is meant to short-circuit buys nothing but
    extra CI minutes."""
    ci_steps = _steps(CI, "python")
    check_idx = _check_step_index(ci_steps)
    pytest_idx = _named_step_index(ci_steps, name_contains="pytest")
    assert check_idx < pytest_idx, (
        "ci.yml's regen --check step must run BEFORE the `pytest` step, or "
        "a stale sidecar is caught no earlier than it already was"
    )

    parity_steps = _steps(PARITY, "seam1-plan-shape")
    check_idx = _check_step_index(parity_steps)
    gates_idx = _named_step_index(parity_steps, name_contains="python/tests/gates")
    assert check_idx < gates_idx, (
        "parity.yml's regen --check step must run BEFORE the "
        "python/tests/gates step, or a stale sidecar is caught no earlier "
        "than it already was"
    )


def test_regen_check_needs_no_alp_sdk_checkout_so_it_can_run_first():
    """The whole point is a FAST, targeted failure. If this step ever grows
    a dependency on the alp-sdk clones parity.yml's other steps need, it can
    no longer run ahead of them -- so pin that it stays free of that, rather
    than let a future edit silently reintroduce the slow path this exists to
    skip."""
    steps = _steps(PARITY, "seam1-plan-shape")
    idx = _check_step_index(steps)
    assert idx is not None
    step = steps[idx]
    env = step.get("env", {})
    assert "ALP_SDK_ROOT" not in env, (
        "the regen --check step must not depend on an alp-sdk checkout -- "
        "that is what lets it run before parity.yml's alp-sdk clone steps"
    )
