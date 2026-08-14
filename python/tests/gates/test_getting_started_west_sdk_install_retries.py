# SPDX-License-Identifier: Apache-2.0
"""`west sdk install` in getting-started.yml must retry, not fail on one shot.

tan-cli#689. The step already carries `GH_TOKEN` to survive the GitHub API
rate limit on `fetch_releases`, but that is only the FIRST network call `west
sdk install` makes -- `setup.sh -t arm-zephyr-eabi` then fetches the GNU
toolchain as a second, separate download no token affects, and it failed on
PR #688 for a reason that PR's diff could not have caused: PR #688 touches
only `python/tan/planner/{kconfig,libraries,validate}.py`, the `ci.yml` and
`parity.yml` SDK-ref pins, and `test_planner_relocation_freshness.py`'s
matching hash re-pin (`gh pr view 688 --repo alplabai/tan-cli --json files`)
-- nothing in that diff reaches a toolchain download. Same class as
alp-sdk#1410 and the `sdk list --online` retry this repo already carries in
`clean-host.yml` / `clean_host_smoke.py:assert_sdk_list_online`: "a gate that
goes red for a reason unrelated to its subject stops being read."

This does not re-run the workflow -- it reads `getting-started.yml` and checks
the SHAPE of the step, so a future edit that flattens the retry back to a bare
one-shot invocation (the exact regression #689 was filed against) fails here
before it ever reaches a runner.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "getting-started.yml"

#: The step's own name, unabridged, so a rename is visible here rather than
#: silently losing the step this gate is supposed to be checking.
STEP_NAME = "install the Zephyr SDK (west sdk install, the printed remedy)"

#: What `west sdk install` is actually testing -- the printed remedy string
#: `build_readiness.rs` emits. Deliberately loose (an unanchored `.*` across
#: the whole loop body): used only to confirm the invocation is reachable
#: SOMEWHERE inside the retry loop (test_west_sdk_install_is_wrapped_in_a_
#: retry_loop). Do not reuse this to COUNT invocations -- with re.S its `.*`
#: is greedy and swallows two duplicated invocations into a single match
#: (verified), which is exactly why the flag-carrying test below uses
#: _EXACT_INVOCATION instead.
_INVOCATION = re.compile(r"west\s+sdk\s+install\b.*-t\s+arm-zephyr-eabi", re.S)

#: A loop that runs the command at least 3 times, per the issue's own
#: precedent (`clean_host_smoke.py`'s 3-attempt retry).
_RETRY_LOOP = re.compile(r"for\s+attempt\s+in\s+((?:\d+\s*){3,});\s*do")

#: The exact two-line invocation this step must contain, anchored end to
#: end (`--version <x> -t arm-zephyr-eabi \` then a continuation line
#: `--personal-access-token <y>`) so that:
#:   * a DROPPED flag or invocation has nothing to match (0 hits);
#:   * a DUPLICATED invocation cannot be swallowed into one greedy match --
#:     each hit stops at its own `--personal-access-token` line, so two
#:     copies produce two non-overlapping hits, not one;
#:   * a MANGLED invocation (renamed flag, missing continuation, extra
#:     text on the flag line) breaks the anchored shape and matches nothing.
_EXACT_INVOCATION = re.compile(
    r"west\s+sdk\s+install\s+--version\s+\S+\s+-t\s+arm-zephyr-eabi\s*\\\s*\n"
    r"\s*--personal-access-token\s+\S+"
)


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@functools.cache
def _step() -> dict | None:
    steps = _workflow()["jobs"]["first-install"]["steps"]
    return next((s for s in steps if s.get("name") == STEP_NAME), None)


def test_the_step_still_exists():
    """Anti-vacuity: every assertion below is about this step's `run:` body --
    if the name drifted and the lookup silently returned nothing, the checks
    below would all vacuously pass having examined nothing."""
    assert _step() is not None, (
        f"no step named {STEP_NAME!r} found in getting-started.yml's "
        f"first-install job -- either it was renamed (update STEP_NAME here "
        f"too) or removed (drop this gate along with it), but this gate must "
        f"not silently stop checking anything"
    )
    assert isinstance(_step()["run"], str) and _step()["run"].strip(), _step()


def test_west_sdk_install_is_wrapped_in_a_retry_loop():
    run = _step()["run"]
    loop = _RETRY_LOOP.search(run)
    assert loop, (
        "west sdk install has no retry loop in getting-started.yml -- this is "
        "the tan-cli#689 regression: the step's second network call (the GNU "
        "toolchain fetch inside `setup.sh`) is not covered by GH_TOKEN and can "
        "fail transiently with no fault of the PR under test. Wrap it in a "
        "`for attempt in 1 2 3; do ... done` retry, matching the "
        "`sdk list --online` precedent in clean-host.yml.\n\n" + run
    )
    # The invocation must sit INSIDE the loop body, not merely appear
    # somewhere in the same script (e.g. left over above/below an
    # unconnected loop that retries something else).
    body_start = loop.end()
    done_at = run.find("\ndone", body_start)
    assert done_at != -1, f"retry loop has no matching 'done' in:\n{run}"
    body = run[body_start:done_at]
    assert _INVOCATION.search(body), (
        "found a retry loop, but `west sdk install ... -t arm-zephyr-eabi` is "
        "not inside its body -- the loop retries the wrong thing.\n\n"
        f"loop body:\n{body}"
    )


#: The `if [ "${attempt}" -eq 3 ]; then ... fi` exhaustion branch, body
#: captured non-greedily so it stops at ITS OWN closing `fi` rather than
#: running on past it -- there is no nested `if` inside this branch, so the
#: first `fi` following `-eq 3 ]; then` is always the matching one. Scoping
#: the body this tightly (rather than scanning the whole step) means an
#: `exit` or `|| true` living OUTSIDE this branch cannot satisfy or defeat
#: these assertions.
_EXHAUSTION_BRANCH = re.compile(
    r'"\$\{attempt\}"\s*-eq\s*3\s*\]\s*;\s*then(?P<body>.*?)\n\s*fi\b', re.S
)


def test_a_third_failure_still_reds_the_job():
    """Not a skip and not `|| true`: a genuinely unavailable network must still
    fail the job, because this step is testing the remedy `tan` itself
    prints (#689's own explicit requirement). The failing `exit` must sit
    INSIDE the `-eq 3` branch -- a hard exit anywhere else in the step (or a
    dead branch whose real exit was defanged, e.g. `break`) must not satisfy
    this check."""
    run = _step()["run"]
    branch = _EXHAUSTION_BRANCH.search(run)
    assert branch, (
        f"no final-attempt (`-eq 3`) branch found -- exhaustion must be "
        f"detected:\n{run}"
    )
    body = branch.group("body")
    # The final-attempt branch must exit non-zero and must not be muted by
    # `|| true`, which would let the step end green after a real failure.
    assert "|| true" not in body, (
        f"the final-attempt branch mutes its own exit with `|| true`, so "
        f"the step ends green after a real failure:\n{body}"
    )
    assert re.search(r"exit\s+[1-9]\d*", body), (
        f"the final-attempt branch does not exit non-zero -- exhaustion is "
        f"detected but not turned into a failing step:\n{body}"
    )


def test_the_invocation_still_carries_the_flag_under_test():
    """The retry wrap must not have dropped, duplicated, or mangled what the
    step is actually proving -- `-t arm-zephyr-eabi`, the printed remedy's own
    flag. See `_EXACT_INVOCATION` for why each of the three named defects
    (dropped / duplicated / mangled) is guaranteed to change the hit count
    away from exactly 1."""
    run = _step()["run"]
    hits = list(_EXACT_INVOCATION.finditer(run))
    assert len(hits) == 1, (
        f"expected exactly one west sdk install --version ... "
        f"-t arm-zephyr-eabi \\ / --personal-access-token ... invocation "
        f"(not dropped, not duplicated, not mangled), found {len(hits)}:\n"
        f"{run}"
    )
