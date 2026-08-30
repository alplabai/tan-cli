# SPDX-License-Identifier: Apache-2.0
"""planner-resync.yml's `rc=$?` / pin-sha captures must run with errexit cleared.

tan-cli#793. GitHub wraps every `run:` block in `bash --noprofile --norc -e -o
pipefail {0}` -- errexit ON by default, and neither step below opens with an
explicit `set -e` of its own (`set -uo pipefail` does not set it). `python
python/scripts/planner_resync.py` documents exit 1 ("a re-sync is owed") and
exit 2 ("Refused") as ROUTINE outcomes the "Propose the re-sync" step is
supposed to route through `rc=$?` -- but with errexit still on, either exit
kills the step before that assignment ever runs. The "Run the freshness gate
against the re-synced tree" step has the same shape twice: `sha="$(pin
"$name")"` is a `grep | grep` pipeline that exits nonzero under `pipefail` on
a no-match, which would kill the step before the `[ -z "$sha" ]` guard below
it ever runs; and its own `rc=$?` capturing the pytest run has the identical
exposure. This is exactly how the workflow shipped broken from birth and
stayed broken for 29 of its first 31 runs -- nothing in CI ever exercised the
"a re-sync is owed" or "Refused" exit paths before this gate existed.

Nothing here re-runs the workflow -- it reads `planner-resync.yml`, walks each
`run:` script tracking the `-e`/`+e` toggle state through `set` lines (`set
-uo pipefail` does not toggle errexit; `set -eu...`/`set -e` turns it on; `set
+e` turns it off), and asserts errexit is OFF at the moment of every `rc=$?`
capture and at the `sha="$(pin "$name")"` assignment -- the two exact shapes
tan-cli#793 fixed. A future edit that drops either `set +e` guard, or that
reorders a `set -e` restore ahead of the capture it is meant to protect, fails
here before it ever reaches a runner.

tan-cli#1002 added a THIRD `rc=$?` capture, in the "Open or refresh the
proposal PR" step: `planner_resync_branch_guard.py`'s exit 1 ("diverted, use
the branch it named") is exactly as routine as `planner_resync.py`'s own exit
1/2 above, and the same tan-cli#793 shape would kill the step on it if a
future edit ever let errexit stay ON across that call. Covered here rather
than in a second file, since it is the identical defect class this gate
already exists to catch.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "planner-resync.yml"

#: The three steps this gate is about, named exactly as they appear in the
#: workflow so a rename is visible here rather than silently losing coverage.
_PROPOSE_STEP = "Propose the re-sync"
_GATE_STEP = "Run the freshness gate against the re-synced tree"
_PR_STEP = "Open or refresh the proposal PR"

#: `set -eu...` / `set -e` (errexit ON) vs `set +e` (errexit OFF). Only a
#: `set` whose option string actually contains `e` toggles errexit -- `set
#: -uo pipefail` does not, and must not be mistaken for one.
_SET_TOGGLE = re.compile(r"^\s*set\s+([+-])(\S*)", re.M)

#: `rc=$?` -- the exit-code capture this whole gate exists to protect.
_RC_CAPTURE = re.compile(r"^\s*rc=\$\?", re.M)

#: `sha="$(pin "$name")"` -- the second capture tan-cli#793 names by name; a
#: `pin | grep` pipeline exits nonzero under `pipefail` on a no-match, and
#: plain errexit would kill the step before the `[ -z "$sha" ]` guard below
#: it ever runs.
_PIN_CAPTURE = re.compile(r'^\s*sha="\$\(pin "\$name"\)"', re.M)

#: GitHub's own wrapper (`bash --noprofile --norc -e -o pipefail {0}`,
#: documented in this workflow's own comments) starts every `run:` block with
#: errexit ON. Neither step below opens with an explicit `set -e`, so the
#: tracked state must start True to match what actually executes.
_ERREXIT_ON_AT_STEP_START = True


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@functools.cache
def _steps() -> list[dict]:
    return _workflow()["jobs"]["propose"]["steps"]


@functools.cache
def _step(name: str) -> dict | None:
    return next((s for s in _steps() if s.get("name") == name), None)


def _errexit_state_before(run: str, pos: int) -> bool:
    """Errexit state in effect immediately before byte offset `pos` in `run`,
    replaying every `set` toggle that appears earlier in the script."""
    state = _ERREXIT_ON_AT_STEP_START
    for m in _SET_TOGGLE.finditer(run):
        if m.start() >= pos:
            break
        opts = m.group(2)
        if "e" in opts:
            state = m.group(1) == "-"
    return state


def _line_no(run: str, pos: int) -> int:
    return run[:pos].count("\n") + 1


def _assert_errexit_cleared_at_every(
    run: str, pattern: re.Pattern, label: str, step_name: str
) -> None:
    hits = list(pattern.finditer(run))
    assert hits, (
        f"expected at least one {label} in the {step_name!r} step's `run:` "
        f"body -- either the shape changed (update this gate's regex too) or "
        f"the capture was removed (this gate is now vacuous), but it must "
        f"not silently pass having found nothing:\n{run}"
    )
    for m in hits:
        state = _errexit_state_before(run, m.start())
        assert state is False, (
            f"errexit is still ON at the {label} capture on line "
            f"{_line_no(run, m.start())} of the {step_name!r} step -- a "
            f"nonzero exit immediately before this line kills the step "
            f"before it runs (tan-cli#793's exact defect: 29 of the "
            f"workflow's first 31 runs failed this way). Add a `set +e` "
            f"before the command whose exit this line captures.\n{run}"
        )


def test_the_steps_still_exist():
    """Anti-vacuity: every assertion below is about these three steps' `run:`
    bodies -- if any name drifted and the lookup silently returned nothing,
    the checks below would all vacuously pass having examined nothing."""
    for name in (_PROPOSE_STEP, _GATE_STEP, _PR_STEP):
        step = _step(name)
        assert step is not None, (
            f"no step named {name!r} found in planner-resync.yml's "
            f"`propose` job -- either it was renamed (update this gate too) "
            f"or removed (drop this gate along with it), but this gate must "
            f"not silently stop checking anything"
        )
        assert isinstance(step["run"], str) and step["run"].strip(), step


def test_the_propose_steps_rc_capture_runs_with_errexit_cleared():
    run = _step(_PROPOSE_STEP)["run"]
    _assert_errexit_cleared_at_every(run, _RC_CAPTURE, "rc=$?", _PROPOSE_STEP)


def test_the_propose_step_restores_errexit_after_the_capture():
    """The `set +e` above must not leak past this step's own risky command --
    everything after it in the step (writing the job summary, `exit 0`) is
    meant to run under normal errexit semantics again."""
    run = _step(_PROPOSE_STEP)["run"]
    hit = _RC_CAPTURE.search(run)
    assert hit, run
    next_toggle = _SET_TOGGLE.search(run, hit.end())
    assert next_toggle is not None and "e" in next_toggle.group(2) and next_toggle.group(1) == "-", (
        f"errexit is not restored (`set -e`) after `rc=$?` in the "
        f"{_PROPOSE_STEP!r} step -- the rest of the step (writing the job "
        f"summary, `exit 0`) is meant to run under normal errexit again:\n{run}"
    )


def test_the_gate_steps_pin_capture_runs_with_errexit_cleared():
    run = _step(_GATE_STEP)["run"]
    _assert_errexit_cleared_at_every(run, _PIN_CAPTURE, 'sha="$(pin "$name")"', _GATE_STEP)


def test_the_gate_steps_rc_capture_runs_with_errexit_cleared():
    run = _step(_GATE_STEP)["run"]
    _assert_errexit_cleared_at_every(run, _RC_CAPTURE, "rc=$?", _GATE_STEP)


def test_the_pr_steps_branch_guard_rc_capture_runs_with_errexit_cleared():
    """tan-cli#1002: `planner_resync_branch_guard.py`'s exit 1 (diverted) is a
    routine outcome this step must keep running past, same shape as the two
    steps above."""
    run = _step(_PR_STEP)["run"]
    _assert_errexit_cleared_at_every(run, _RC_CAPTURE, "rc=$?", _PR_STEP)


def test_the_pr_step_restores_errexit_after_the_branch_guard_capture():
    run = _step(_PR_STEP)["run"]
    hit = _RC_CAPTURE.search(run)
    assert hit, run
    next_toggle = _SET_TOGGLE.search(run, hit.end())
    assert next_toggle is not None and "e" in next_toggle.group(2) and next_toggle.group(1) == "-", (
        f"errexit is not restored (`set -e`) after `rc=$?` in the "
        f"{_PR_STEP!r} step -- everything after it (reading the guard's "
        f"decision back out of $GITHUB_OUTPUT, the git commit/push, opening "
        f"the PR) is meant to run under normal errexit again:\n{run}"
    )
