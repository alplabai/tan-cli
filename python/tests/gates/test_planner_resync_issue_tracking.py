# SPDX-License-Identifier: Apache-2.0
"""planner-resync.yml's issue-tracking pair must not drift or crash unfed.

tan-cli#920 (second-round review of tan-cli#911): "Surface the owed work..."
and "Close the tracking issue..." used to each declare their own copy of
`RESYNC_ISSUE_TITLE`, with a comment claiming there was no job-level `env:`
to share it from -- a claim disproven in the same fix that replaced it with a
real shared `env:` on the `propose` job. Nothing enforced the two copies
staying identical before that, and nothing stops a future edit from
reintroducing a step-level copy that drifts. Separately, the "Surface the
owed work..." step's `cat "${RUNNER_TEMP}/resync.md"` had no fallback, so an
unhandled `planner_resync.py` traceback (rc=1, no markdown written) killed
the step under `set -euo pipefail` before it could open anything -- the
house answer (`test_planner_resync_workflow_errexit.py`) is to read the YAML
and assert the shape, not to run the workflow.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "planner-resync.yml"

_ISSUE_STEP = "Surface the owed work when there is nothing to propose"
_CLOSE_STEP = "Close the tracking issue once nothing is owed or a PR now covers it"

_CAT_RESYNC_MD = 'cat "${RUNNER_TEMP}/resync.md"'


def _logical_lines(run: str) -> list[str]:
    """Join `\\`-continued physical lines so a same-statement `||` fallback
    on the next line is visible on the line the `cat` itself is on."""
    joined = run.replace("\\\n", " ")
    return joined.split("\n")


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@functools.cache
def _job() -> dict:
    return _workflow()["jobs"]["propose"]


@functools.cache
def _step(name: str) -> dict | None:
    return next((s for s in _job()["steps"] if s.get("name") == name), None)


def test_the_issue_title_and_marker_are_shared_job_level_env_not_duplicated():
    job_env = _job().get("env") or {}
    for key in ("RESYNC_ISSUE_TITLE", "RESYNC_ISSUE_MARKER"):
        assert key in job_env, (
            f"{key!r} must be declared once, in the `propose` job's own "
            f"`env:` -- not per-step, where two hand-kept copies can drift "
            f"apart silently (tan-cli#920)."
        )
    for name in (_ISSUE_STEP, _CLOSE_STEP):
        step = _step(name)
        assert step is not None, f"step {name!r} not found -- update this gate"
        step_env = step.get("env") or {}
        for key in ("RESYNC_ISSUE_TITLE", "RESYNC_ISSUE_MARKER"):
            assert key not in step_env, (
                f"{name!r} redeclares {key!r} in its own `env:` -- this "
                f"reintroduces the two-copies-can-drift defect tan-cli#920 "
                f"fixed by sharing the job-level env var instead."
            )


def test_every_cat_of_resync_md_has_a_fallback():
    """Every `run:` step in this job that reads `resync.md` must tolerate it
    being absent -- `planner_resync.py` writes markdown on every *handled*
    path (including its own `Refused` branch); a plain rc=1 with none
    written means an unhandled traceback, and any bare `cat` of it dies
    under `set -euo pipefail` before the step can report anything."""
    found_any = False
    for step in _job()["steps"]:
        run = step.get("run")
        if not isinstance(run, str) or _CAT_RESYNC_MD not in run:
            continue
        found_any = True
        for line in _logical_lines(run):
            if _CAT_RESYNC_MD in line:
                assert "||" in line, (
                    f"{step.get('name')!r} has a bare "
                    f"`cat \"${{RUNNER_TEMP}}/resync.md\"` with no `||` "
                    f"fallback on the same statement -- see this test's "
                    f"docstring (tan-cli#920, tan-cli#911's own 'red X with "
                    f"no explanation' reintroduced on this path):\n{line}"
                )
    assert found_any, (
        "expected at least one `cat \"${RUNNER_TEMP}/resync.md\"` somewhere "
        "in the propose job -- either the shape changed (update this gate) "
        "or the read was removed (drop this gate), but it must not "
        "silently pass having found nothing"
    )
