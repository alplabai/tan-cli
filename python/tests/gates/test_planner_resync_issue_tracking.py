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

tan-cli#920 round 3: the round-2 gate above guarded the `cat` fallback and
the shared `env:`, but not the thing round 2 was actually FOR -- the
lookup's own exit-code check. Mutation-proved (patching both lookups back to
`--jq "..." || true` left the round-2 gate at "2 passed" while the same
mutant, driven under a stubbed `gh`, reproduced the fail-open `gh issue
create` duplicate tan-cli#911 exists to prevent). `test_the_final_attempt...`
in `test_getting_started_west_sdk_install_retries.py` is the prior art for
this shape: `assert "|| true" not in body`. Round 3 also found `gh issue
list` was called with no `--limit`, capping the page at 30 items,
newest-created-first -- an old tracking issue can age off page 1 and the
lookup returns empty with rc=0, indistinguishable from "no match", which
re-opens the exact "must not create 40 issues" hazard through pagination
instead of a `gh` error. All three properties are asserted below so none of
the three can silently regress again.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "planner-resync.yml"

_ISSUE_STEP = "Surface the owed work when there is nothing to propose"
_CLOSE_STEP = "Close the tracking issue once nothing is owed or a PR now covers it"

_CAT_RESYNC_MD = 'cat "${RUNNER_TEMP}/resync.md"'

#: Floor for `gh issue list --limit`. 100 is what the job carries and what
#: this module's docstring names; `gh` defaults to 30 and the repo measured
#: 49 open issues, so the floor must sit above that default to mean anything.
_MIN_ISSUE_LIST_LIMIT = 100


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
    # The two steps that read these vars must still exist -- if either is
    # renamed this gate is looking at the wrong workflow and says so.
    for name in (_ISSUE_STEP, _CLOSE_STEP):
        assert _step(name) is not None, (
            f"step {name!r} not found -- update this gate")

    # ...but the redeclaration check sweeps EVERY step, not just those two
    # (tan-cli#937). Checking only the two named steps was the hole: the
    # `propose` job has ten steps, and a copy of `RESYNC_ISSUE_TITLE` added
    # to any of the other eight would drift from the job-level value with
    # nothing to catch it -- which is the whole defect tan-cli#920 closed,
    # re-openable in eight places the gate never looked at.
    for step in _job()["steps"]:
        step_env = step.get("env") or {}
        for key in ("RESYNC_ISSUE_TITLE", "RESYNC_ISSUE_MARKER"):
            assert key not in step_env, (
                f"{step.get('name')!r} redeclares {key!r} in its own `env:` "
                f"-- this reintroduces the two-copies-can-drift defect "
                f"tan-cli#920 fixed by sharing the job-level env var instead."
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


def _gh_issue_list_lines(run: str) -> list[str]:
    """The actual invocation lines only -- not a comment or an `::error::`
    string that happens to mention "gh issue list" in prose."""
    return [line for line in _logical_lines(run) if "gh issue list --repo" in line]


def test_gh_issue_list_lookups_fail_closed_with_a_limit_and_a_checked_exit_code():
    """The de-dup lookup in both "Surface..." and "Close..." must, all three:

    (a) never be muted with `|| true` -- tan-cli#920's headline fix.
        A lookup failure must not look identical to "no match", or the step
        falls through to `gh issue create` and opens a silent duplicate --
        the exact "must not create 40 issues" guarantee tan-cli#911 was filed
        for. (Prior art for this exact assertion shape:
        `test_getting_started_west_sdk_install_retries.py`'s
        `assert "|| true" not in body`.)
    (b) capture the lookup's own exit code in `lookup_rc` and test it (`-ne
        0`) before deciding "no match" -- the mechanism (a) actually relies
        on; `set +e; existing=$(...); lookup_rc=$?; set -e` is required
        because a `var=$(cmd)` assignment does not reliably trip `set -e`
        the way a bare command does.
    (c) carry `--limit 100` -- `gh issue list` with no `--limit` caps at 30
        items, newest-created-first (measured live against this repo: `gh
        issue list --state open --json number --jq length` -> 30, `--limit
        500` -> 49). Past 30 open issues the tracking issue this lookup
        exists to find ages off page 1 and `existing` comes back empty with
        rc=0 -- the same "indistinguishable from no match" fail-open as (a)
        and (b) guard, arriving through pagination instead of a `gh` error.
    """
    for name in (_ISSUE_STEP, _CLOSE_STEP):
        step = _step(name)
        assert step is not None, f"step {name!r} not found -- update this gate"
        run = step["run"]

        assert "|| true" not in run, (
            f"{name!r} mutes a command with `|| true` -- a lookup failure "
            f"must not look identical to \"no match\" (tan-cli#920):\n{run}"
        )

        assert re.search(r"lookup_rc\s*=\s*\$\?", run), (
            f"{name!r} does not capture its `gh issue list` lookup's own "
            f"exit code into `lookup_rc` -- without this a failed lookup "
            f"can silently fall through to `gh issue create` and open a "
            f"duplicate (tan-cli#920's headline fix):\n{run}"
        )
        assert re.search(r'"\$\{lookup_rc\}"\s*-ne\s*0', run), (
            f"{name!r} captures `lookup_rc` but never tests it (`-ne 0`) "
            f"before deciding \"no match\" -- capturing the exit code alone "
            f"does nothing without the check that acts on it "
            f"(tan-cli#920):\n{run}"
        )

        list_lines = _gh_issue_list_lines(run)
        assert list_lines, (
            f"{name!r} no longer calls `gh issue list` -- either the shape "
            f"changed (update this gate) or the read was removed (drop "
            f"this gate), but it must not silently pass having found "
            f"nothing"
        )
        for line in list_lines:
            limit = re.search(r"--limit\s+(\d+)", line)
            assert limit is not None, (
                f"{name!r}'s `gh issue list` lookup has no `--limit <N>` -- "
                f"it defaults to a 30-item, newest-created-first page, so an "
                f"older tracking issue can age off page 1 and look "
                f"identical to \"no match\", opening a silent duplicate "
                f"(tan-cli#920 round 3, measured live: `gh issue list "
                f"--state open --json number --jq length` -> 30, `--limit "
                f"500` -> 49):\n{line}"
            )
            # Assert the VALUE, not merely the flag (tan-cli#937). Checking
            # `"--limit" in line` was the second hole: `--limit 1` satisfies
            # it and reinstates the exact pagination fail-open (c) exists to
            # prevent, only harder -- a one-item page ages the tracking issue
            # off immediately. The floor is the 100 this job actually carries
            # and this module's own docstring names; the repo measured 49
            # open issues against a 30-item default, so anything at or below
            # that default is not a fix at all.
            assert int(limit.group(1)) >= _MIN_ISSUE_LIST_LIMIT, (
                f"{name!r}'s `gh issue list` uses `--limit "
                f"{limit.group(1)}`, below the {_MIN_ISSUE_LIST_LIMIT} floor "
                f"-- `gh issue list` defaults to 30 items and this repo "
                f"already has more open issues than that, so a small limit "
                f"lets the tracking issue age off the page and read as \"no "
                f"match\", which is the fail-open (a) and (b) also guard "
                f"(tan-cli#920 round 3, tan-cli#937):\n{line}"
            )
