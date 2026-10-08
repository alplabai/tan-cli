# SPDX-License-Identifier: Apache-2.0
"""`clean-host.yml` reports ONE context branch protection can require (tan-cli#1392).

After tan-cli#1390 the Windows and both macOS `freeze-and-smoke` legs run only
on `merge_group`/push/dispatch, and none of those per-leg contexts is required,
so a non-Linux freeze or smoke regression in a queued merge went red without
blocking. `freeze-and-smoke-all` is the fix, same shape as parity.yml's
`python-tests`: stub-pass on `pull_request`, real check everywhere else.

What this pins, each easy to undo by accident:

1. The job always runs (`always()`), on both events a merge-queue-required
   context must report on, and depends on BOTH `matrix-plan` and
   `freeze-and-smoke` -- a `matrix-plan` failure skips the whole matrix, and a
   check that only read `freeze-and-smoke` would still see that (`skipped`),
   but naming both keeps the error message honest about which one broke.
2. The stub and the real check split on exactly `pull_request`: a stub that
   also fired on `merge_group` would make the required context vacuous.
3. The real check's shell body EXECUTES to the right exit code for every
   result GitHub can hand it -- run here through `bash`, not grepped, since
   `!= "success"` vs `= "failure"` reads alike and only the first one fails a
   skipped or cancelled matrix.
4. No `freeze-and-smoke` step is `continue-on-error`, which would turn a red
   leg into a `success` rollup and the aggregator green with it.
5. No `run:` body in the file interpolates `${{ matrix.* }}` inline (the
   issue's zizmor future-proofing ask); values reach the shell through `env:`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clean-host.yml"
AGGREGATOR = "freeze-and-smoke-all"
REQUIRED_NAME = "clean host (freeze): all legs"
AGGREGATOR_IF = "${{ always() && github.event_name != 'release' }}"
GITHUB_JOB_RESULTS = ("success", "failure", "cancelled", "skipped")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    return workflow.get("on", workflow.get(True))


def _aggregator() -> dict:
    return _workflow()["jobs"][AGGREGATOR]


def _step(name_prefix: str) -> dict:
    matches = [s for s in _aggregator()["steps"] if s.get("name", "").startswith(name_prefix)]
    assert len(matches) == 1, f"expected one step named {name_prefix!r}, found {len(matches)}"
    return matches[0]


def test_the_aggregator_always_reports_under_its_required_name():
    workflow = _workflow()
    job = workflow["jobs"][AGGREGATOR]

    assert job["name"] == REQUIRED_NAME
    assert set(job["needs"]) == {"matrix-plan", "freeze-and-smoke"}
    # always(): without it a failed leg SKIPS the aggregator, and a skipped
    # required check never reports.
    assert job["if"] == AGGREGATOR_IF
    triggers = _triggers(workflow)
    assert "pull_request" in triggers and "merge_group" in triggers


def test_the_stub_fires_on_pull_request_only_and_the_check_everywhere_else():
    assert _step("stub pass")["if"] == "${{ github.event_name == 'pull_request' }}"
    assert _step("verify every freeze-and-smoke leg")["if"] == "${{ github.event_name != 'pull_request' }}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to execute the step body")
@pytest.mark.parametrize("plan", GITHUB_JOB_RESULTS)
@pytest.mark.parametrize("freeze", GITHUB_JOB_RESULTS)
def test_the_real_check_passes_only_when_both_needs_succeeded(plan, freeze):
    body = _step("verify every freeze-and-smoke leg")["run"]
    env = {**os.environ, "PLAN_RESULT": plan, "FREEZE_RESULT": freeze}

    proc = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, check=False)

    expected_ok = plan == "success" and freeze == "success"
    assert (proc.returncode == 0) is expected_ok, proc.stdout + proc.stderr
    if not expected_ok:
        assert f"freeze-and-smoke: {freeze}" in proc.stdout


def test_no_freeze_leg_step_can_mask_its_own_failure():
    job = _workflow()["jobs"]["freeze-and-smoke"]
    masked = [s.get("name", s.get("uses")) for s in job["steps"] if s.get("continue-on-error")]
    assert masked == []
    assert not job.get("continue-on-error")


def test_no_run_body_interpolates_a_matrix_value_inline():
    offenders = [
        f"{job_id}/{step.get('name', '?')}"
        for job_id, job in _workflow()["jobs"].items()
        for step in job.get("steps", [])
        if re.search(r"\$\{\{\s*matrix\.", step.get("run", ""))
    ]
    assert offenders == []
