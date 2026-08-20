#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Run apt-get under a WALL-CLOCK bound that is SHARED ACROSS EVERY INVOCATION IN
# THE STEP, with a dpkg-safe retry. Ported from alp-sdk's scripts/ci/apt-bounded.sh
# (alp-sdk#1592/#1575).
#
# Cross-platform scope: CI-only. Bash + apt-get + dpkg, so Debian/Ubuntu
# runners (or containers) by nature -- it is not part of the Windows / macOS
# developer-host surface tan itself ships to.
#
# WHY A BOUND AT ALL: `Acquire::http::Timeout` bounds an IDLE read, not a SLOW
# one. Every byte that arrives resets the timer, so a mirror that trickles
# defeats it forever, and apt has no minimum-transfer-rate option (no equivalent
# of curl's --speed-limit). Measured on alp-sdk against two local servers
# (alp-sdk#1575):
#
#   server                               result
#   -----------------------------------  -----------------------------------
#   accepts, sends headers, then silent   rc=100 after 127s -- Timeout=30 FIRED,
#                                         3 retries, apt gave up on its own
#   accepts, then 1 byte every 20s        NEVER returns; only an external kill
#                                         ended it. Unbounded.
#
# tan-cli#860: measured for real here too. PR #851, job 96014351754:
# `sudo apt-get update` started 09:14:51, printed its last output at 09:15:29,
# and sat silent until the JOB's own 60-minute cap killed it at 10:15:06 --
# with no `Acquire::*` flags at all, that step wasn't even bounded to the
# idle-read case above, just unbounded. Happened twice.
#
# WHY THE BUDGET IS SHARED, not per-invocation: a step calls this TWICE --
# `update` then `install`. A per-invocation budget of N therefore admits 2N per
# step, which can overrun the JOB's own cap and let it kill the wrapper before
# it could report its own attributed failure (alp-sdk#1592 measured exactly
# this: a step-scoped budget let one 20-minute step cap fire anonymously).
#
# So the deadline is computed ONCE per step and persisted in RUNNER_TEMP, keyed
# by GITHUB_ACTION (the step's own identifier). Every later invocation in the
# same step inherits it, and each attempt is clamped to the time actually
# remaining. Total wall time for the step is APT_STEP_BUDGET, plus at most a
# `--kill-after` grace period (<=30s on the apt-get call, <=10s on the dpkg
# recovery) if a single stubborn process ignores SIGTERM -- not APT_STEP_BUDGET
# exactly, but bounded regardless of how many times the wrapper is called;
# every command this script runs is under a `timeout`, with none left to hang
# forever the way an un-timed `dpkg --configure -a` used to.
#
# THE DEFAULT, and why it is NOT alp-sdk's 780s: alp-sdk sized 780s against a
# uniform 20-minute STEP cap. tan-cli has no step-level `timeout-minutes` at
# all -- only JOB caps, and every job that actually calls this wrapper is
# bounded (verified against `dev`, not assumed): clean-host.yml's
# freeze-and-smoke = 20 min (the smallest job that calls it), ci.yml's
# `python` = 30 min on a PR (`sdk_parity` false) / 60 on the release path,
# e2e-container.yml's `container` = 45 min, and getting-started.yml /
# parity.yml's seam2 & first-blink / release-combination.yml all = 60 min.
# python-binaries.yml's `linux` and release.yml's `build` carry no
# `timeout-minutes` at all (GitHub's 360-minute default). A blanket 780s
# default would eat the entire 20-minute freeze-and-smoke job before the
# wrapper ever got to report its own failure -- exactly the anonymous
# job-cap-fires-first outcome this script exists to prevent. 240s (4 min) is
# 20% of that real 20-minute floor, leaving 80% of the job for
# checkout/setup/the actual PyInstaller freeze that follows -- and clears
# even clean-host.yml's OTHER job, release-asset-smoke (10 min), which calls
# no apt-get today but would still have comfortable (60%) headroom if a
# future call site ever landed there. Every real call site here installs at
# most ~9 small, already-cached packages (`ninja-build device-tree-compiler
# gperf ...`, `binutils`, `zsh`, `ca-certificates git python3`), which
# historically finish in well under 30s -- none needs a per-call override of
# a bigger budget; set APT_STEP_BUDGET before calling if one ever does.
#
# dpkg safety: `timeout` can kill apt-get mid-unpack, leaving the database
# half-configured or the lock held. Every retry runs a BOUNDED
# `dpkg --configure -a` first -- the standard recovery, a no-op when nothing
# was interrupted, itself wrapped in `timeout` (an un-timed recovery command
# could block forever on the same dpkg lock apt-get just failed to get,
# which is precisely the unbounded wait this script exists to rule out) --
# and `slice` below is computed AFTER that recovery runs, from a freshly
# re-read clock, so a slow recovery can never leave the following apt-get
# attempt sized against a stale, too-generous budget.
#
# Usage:  scripts/ci/apt-bounded.sh update
#         scripts/ci/apt-bounded.sh install -y --no-install-recommends foo bar
set -euo pipefail

# Total wall clock for ALL invocations in this step. Must sit comfortably UNDER
# the JOB's own cap so this wrapper loses the race and reports a named failure
# instead of the job cap firing anonymously. See the big comment above for why
# this is 240s and not alp-sdk's 780s.
: "${APT_STEP_BUDGET:=240}"     # 4 min, 20% of the smallest job that calls this (20 min)
: "${APT_ATTEMPT_TIMEOUT:=60}"  # ceiling per attempt; clamped to what remains
: "${APT_ATTEMPTS:=3}"
: "${APT_DPKG_TIMEOUT:=30}"     # ceiling for the pre-retry `dpkg --configure -a` recovery

_now() { date +%s; }

# One deadline per step. GITHUB_ACTION identifies the step; fall back to the PID
# of our parent shell so a local run still gets a private, non-colliding file.
_state_dir="${RUNNER_TEMP:-/tmp}"
_key="${GITHUB_ACTION:-local-$PPID}"
_deadline_file="${_state_dir}/apt-bounded.${_key//[^A-Za-z0-9_.-]/_}.deadline"

if [ -s "$_deadline_file" ]; then
  DEADLINE="$(cat "$_deadline_file")"
else
  DEADLINE=$(( $(_now) + APT_STEP_BUDGET ))
  printf '%s' "$DEADLINE" > "$_deadline_file"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

ACQ=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=3)

rc=0
for attempt in $(seq 1 "$APT_ATTEMPTS"); do
  remaining=$(( DEADLINE - $(_now) ))
  if [ "$remaining" -le 10 ]; then
    echo "apt-bounded: step budget of ${APT_STEP_BUDGET}s exhausted before attempt ${attempt} -- giving up so the JOB cap does not fire anonymously (last rc=$rc)" >&2
    # NEVER exit 0 here.  rc is 0 when the budget was consumed by an EARLIER
    # invocation in this step, so `${rc:-124}` would report SUCCESS for an
    # apt-get that never ran -- a silent failure worse than the hang this
    # wrapper exists to bound.
    [ "$rc" -eq 0 ] && rc=124
    exit "$rc"
  fi
  if [ "$attempt" -gt 1 ]; then
    echo "apt-bounded: attempt ${attempt}/${APT_ATTEMPTS} (previous rc=$rc, ${remaining}s of step budget left)" >&2
    # BOUNDED: an un-timed `dpkg --configure -a` can block indefinitely on
    # the dpkg frontend lock -- exactly the unbounded-wait class this whole
    # wrapper exists to rule out, just one step earlier. `--kill-after=10`
    # matches the apt-get call below's own grace-period shape.
    $SUDO timeout --kill-after=10 "$APT_DPKG_TIMEOUT" dpkg --configure -a >/dev/null 2>&1 || true
    # Re-read the clock: dpkg's own bounded recovery can itself burn up to
    # ~40s of real wall time, and `slice` below must reflect what is ACTUALLY
    # left afterward, not what was left before it ran -- otherwise the
    # apt-get attempt's own timeout would be sized against a stale budget and
    # the step could run past DEADLINE by however long dpkg took.
    remaining=$(( DEADLINE - $(_now) ))
    if [ "$remaining" -le 10 ]; then
      echo "apt-bounded: step budget of ${APT_STEP_BUDGET}s exhausted during dpkg recovery before attempt ${attempt} -- giving up so the JOB cap does not fire anonymously (last rc=$rc)" >&2
      [ "$rc" -eq 0 ] && rc=124
      exit "$rc"
    fi
  fi

  # Cap this attempt at APT_ATTEMPT_TIMEOUT, but never past what is actually
  # left in the step budget -- the last attempt legitimately gets whatever
  # remains, however little, rather than a full APT_ATTEMPT_TIMEOUT slice.
  slice="$APT_ATTEMPT_TIMEOUT"
  [ "$slice" -gt "$remaining" ] && slice="$remaining"

  set +e
  $SUDO timeout --signal=TERM --kill-after=30 "$slice" apt-get "${ACQ[@]}" "$@"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] && exit 0
  if [ "$rc" -ne 124 ] && [ "$rc" -ne 100 ]; then
    echo "apt-bounded: apt-get exited $rc (not a timeout/transient) -- not retrying" >&2
    exit "$rc"
  fi
done
echo "apt-bounded: all ${APT_ATTEMPTS} attempts failed (last rc=$rc)" >&2
exit "$rc"
