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

#: The exact invocation this step must contain, anchored end to end
#: (`--version <x> -t arm-zephyr-eabi` terminated by the retry loop's own
#: `; then`) so that:
#:   * a DROPPED invocation has nothing to match (0 hits);
#:   * a DUPLICATED invocation cannot be swallowed into one greedy match --
#:     each hit stops at its own `; then`, so two copies produce two
#:     non-overlapping hits, not one;
#:   * a MANGLED invocation (an extra flag, a missing target, a stray
#:     continuation) breaks the anchored shape and matches nothing.
#:
#: **tan-cli#1185 removed the `--personal-access-token <y>` continuation line
#: this used to anchor on**, so the shape is one line now. That flag put the
#: token in `west`'s argv -- and so in the host process table -- for the whole
#: multi-minute download; the credential reaches the same `west` through a
#: netrc the step stages instead (`NETRC`), which is what `tan bootstrap`
#: already did (tan-cli#1143). A side effect worth keeping: the command under
#: test is now LITERALLY the remedy `tan doctor` prints, with no
#: runner-specific authentication detail bolted onto it.
#:
#: Dropping the flag alone would satisfy this regex while leaving the download
#: anonymous, which is the tan-cli#689/#1163 rate-limit flake coming back. So
#: the two tests at the bottom of this file hold both halves: the flag must
#: not return, AND the netrc that replaced it must not be dropped.
_EXACT_INVOCATION = re.compile(
    r"west\s+sdk\s+install\s+--version\s+\S+\s+-t\s+arm-zephyr-eabi\s*;\s*then"
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
        f"expected exactly one `west sdk install --version ... "
        f"-t arm-zephyr-eabi; then` invocation (not dropped, not duplicated, "
        f"not mangled), found {len(hits)}. If a credential flag was added "
        f"back onto this line, that is tan-cli#1185 returning -- see "
        f"test_the_credential_is_not_passed_in_argv below:\n{run}"
    )


def test_the_credential_is_not_passed_in_argv():
    """tan-cli#1185. A credential on a CLI flag lands in the host process
    table for the whole multi-minute download, readable by anything else
    executing in the job -- the invariant
    `toolchain_provision.SDK_TOKEN_ENV_VARS` states in its own words and
    tan-cli#1143 designed `tan bootstrap` around. This step was the last site
    in the repo contradicting it.

    Scoped to THIS step; `test_no_credential_in_workflow_argv.py` is the
    repo-wide gate. Comment lines are exempt for the same reason they are
    there: the block right above the invocation explains why the netrc
    exists, and it has to be able to name the flag it replaced."""
    offenders = [
        line.strip()
        for line in _step()["run"].splitlines()
        if "--personal-access-token" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "the SDK-install step passes a credential on a CLI flag again "
        "(tan-cli#1185/#1143). Stage it into a netrc instead -- `mktemp -d`, "
        "`trap ... EXIT`, `chmod 0600`, `export NETRC`:\n  "
        + "\n  ".join(offenders)
    )


def test_the_credential_still_reaches_west_through_a_netrc():
    """The other half, and the reason the test above cannot stand alone:
    deleting the flag AND the netrc satisfies it perfectly while leaving the
    download anonymous. That is not a neutral state -- it is tan-cli#689 /
    tan-cli#1163 returning, a `403 API rate limit exceeded` on a quota counted
    per source IP and shared across every hosted runner in the region, reddening
    this job for traffic that is not ours.

    Each fact below is checked separately so a partial regression names itself
    rather than reporting as one opaque failure."""
    run = _step()["run"]
    assert re.search(r"^\s*export NETRC=", run, re.M), (
        "the step no longer exports NETRC, so `requests` -- the client `west "
        "sdk install`'s GitHub call goes through -- has no netrc path to read "
        "and the download is anonymous (tan-cli#1143 "
        "`toolchain_provision.NETRC_ENV_VAR`)"
    )
    assert "machine api.github.com" in run and "login x-access-token" in run, (
        "the staged netrc is no longer the one-machine document tan writes "
        "(`toolchain_provision.netrc_text`: api.github.com / x-access-token) "
        "-- a mismatched machine line means `requests` finds no entry and "
        "authenticates nothing"
    )
    trap_lines = [
        line for line in run.splitlines()
        if line.lstrip().startswith("trap ") and "EXIT" in line and "netrc" in line
    ]
    assert trap_lines, (
        "no `trap ... EXIT` discarding the netrc: a credential file must not "
        "survive the step on ANY exit path, including the `exit 1` the "
        "exhausted retry takes (tan-cli#1185 acceptance)"
    )
    assert re.search(r"chmod 0600 .*netrc", run), (
        "the staged netrc is no longer chmod 0600 (tan-cli#1185 acceptance)"
    )
    assert "api.github.com/rate_limit" in run, (
        "the step no longer checks that the staged netrc actually "
        "AUTHENTICATES. Without it a green run stops distinguishing 'the token "
        "reached api.github.com' from 'the shared anonymous quota happened to "
        "be free', which is the wrong-reason pass every other assertion here "
        "exists to prevent (tan-cli#1185)"
    )
