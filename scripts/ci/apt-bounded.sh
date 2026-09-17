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

# BULLSEYE ONLY: waive the Release file's FRESHNESS WINDOW, and nothing else.
#
# tan-cli#1257. Debian 11 "bullseye" left LTS on 2026-08-31. On 2026-09-07 the
# `Valid-Until` on bullseye-security's InRelease lapsed, and every job that
# freezes tan inside `python:3.12-slim-bullseye` died at its FIRST update with
#
#   E: Release file for http://deb.debian.org/debian-security/dists/bullseye-security/InRelease
#      is expired (invalid since 2d 13h 56min 35s).
#
# apt reports that as rc=100, which this wrapper classes as transient, so all
# three attempts burned on a condition no retry can change. release.yml's `-gnu`
# freeze is one of the five call sites, which is what made #1257 a release
# blocker.
#
# STATE AS MEASURED 2026-09-17 -- this is HARDENING, not the repair of a live
# red. Debian re-signed the suite (`Suite: oldoldstable-security`, `Date: Sat,
# 12 Sep 2026 09:27:08 UTC`) carrying NO `Valid-Until:` field at all, so apt
# has nothing left to expire and the symptom is currently GONE on its own. The
# flag is here against recurrence: a re-added `Valid-Until`, or a mirror
# serving a stale index, reds the release freeze again on a date, with nothing
# in the diff under test to explain it.
#
# WHICH CHECK THIS DROPS (verified against apt.conf(5) and sources.list(5),
# bookworm, not asserted from memory): `Acquire::Check-Valid-Until` is the
# replay-attack / expiry check and only that -- "a repository creator can
# declare a time until which the data provided in the repository should be
# considered valid". The InRelease GPG signature is STILL verified, and package
# hashes are STILL checked against that signed index. This is NOT
# `--allow-unauthenticated` and NOT `[trusted=yes]` -- sources.list(5)'s
# `Trusted`, of which it says "The value `yes` tells APT always to consider
# this source as trusted, even if it doesn't pass authentication checks. It
# disables parts of apt-secure(8)". Signature checking is not downgraded.
#
# HOW WIDE IT IS, which is a separate question from which check it drops: this
# is apt's GLOBAL override, so it applies to every source configured in the
# container -- `bullseye`, `bullseye-updates` and `bullseye-security` alike --
# not only to the suite that expired. apt.conf(5) is explicit that the
# per-source `Check-Valid-Until` in sources.list(5) "should be preferred to
# disable the check selectively instead of using this global override". Editing
# sources.list per suite is not taken here: it means rewriting a file whose
# layout differs across base images (`/etc/apt/sources.list` vs a
# `.sources` deb822 file) from a wrapper that is shared by every call site,
# to narrow an exposure that is already bounded by the paragraph below.
#
# THE RISK, NOT UNDERSTATED: a stale security index means a known-vulnerable
# `binutils` could be installed without apt objecting. Accepted, with the blast
# radius stated honestly -- this is not a scratch container. release.yml:517-527
# is where the PUBLISHED `tan-x86_64-unknown-linux-gnu` /
# `tan-aarch64-unknown-linux-gnu` assets are frozen, and `objdump` (from
# `binutils`, the one package installed) is what PyInstaller walks the
# artifact's shared-library graph with. The container is discarded when the
# freeze finishes; its OUTPUT ships to customers.
#
# SCOPED TO THE CODENAME so no other call site loses the check: the others run
# on `ubuntu-latest` or inside `ubuntu:24.04`, and keep it in full.
#
# WHEN THIS STOPS WORKING: bullseye is still served from `deb.debian.org`
# (measured 2026-09-17: `dists/bullseye/InRelease` -> HTTP 200). Once it moves
# to `archive.debian.org`, `deb.debian.org` stops serving it and this flag will
# NOT help -- a 404 is not an expiry. THAT is the point to revisit repointing
# sources.list at `archive.debian.org`, tracked on the still-open #1257.
# Deliberately not implemented now: it would be untested speculation against a
# URL that currently answers 200.
#
# THE TEST SEAM IS THE FILE PATH, NOT THE CODENAME, deliberately. An
# `APT_OS_CODENAME` override would also be the one way a NON-bullseye host
# could acquire the waiver from an ambient environment variable; overriding
# which file is read gives the tests the same reach with no such path, and
# lets them exercise this probe's own guards (an absent file, one with no
# `VERSION_CODENAME`, a malformed one) instead of bypassing it. It is not a
# caller knob like APT_STEP_BUDGET/APT_ATTEMPTS above -- no call site sets it.
: "${APT_OS_RELEASE_FILE:=/etc/os-release}"
APT_OS_CODENAME=""
if [ -r "$APT_OS_RELEASE_FILE" ]; then
  # SOURCED IN A SUBSHELL, deliberately: os-release defines NAME/ID/VERSION/...
  # which would otherwise land in this script's own scope.
  #
  # `|| APT_OS_CODENAME=""` keeps `set -euo pipefail` from turning a hostile
  # os-release into a hard failure of the WRAPPER. Precisely what it catches,
  # measured rather than assumed: a top-level `exit` INSIDE the sourced file.
  # Without it, an os-release ending in `exit 7` makes THIS SCRIPT exit 7.
  # A merely malformed file does NOT need it -- `printf` is the last command in
  # the subshell, so the substitution's status is printf's 0 however badly the
  # `.` went (measured: an unterminated quote gives `.` rc=1 and the
  # substitution rc=0). That is also why no separate `|| true` on the `.` is
  # carried: measured, adding one changes no outcome. An unknown codename must
  # fall through to "not bullseye", never to an exit.
  #
  # `${VERSION_CODENAME:-}` is its own guard: os-release need not carry the key
  # at all (Alpine's does not), and a bare `$VERSION_CODENAME` would trip `-u`.
  # shellcheck source=/dev/null  # a runtime host file, not a repo source
  APT_OS_CODENAME="$( . "$APT_OS_RELEASE_FILE" 2>/dev/null; printf '%s' "${VERSION_CODENAME:-}" )" || APT_OS_CODENAME=""
  # A CRLF os-release yields `bullseye<CR>`, which matches no `case` arm below
  # -- the fix would silently become a no-op on a real bullseye host, with no
  # diagnostic. Measured, and trimmed.
  APT_OS_CODENAME="${APT_OS_CODENAME%$'\r'}"
fi
case "$APT_OS_CODENAME" in
  bullseye)
    ACQ+=(-o Acquire::Check-Valid-Until=false)
    # Named, on stderr: a reader seeing bullseye behave differently from every
    # other call site should find the reason in the log rather than bisect for
    # it.
    echo "apt-bounded: NOTICE bullseye detected -- adding -o Acquire::Check-Valid-Until=false (tan-cli#1257, Debian 11 left LTS 2026-08-31). Drops the Release file freshness check ONLY, and does so for every source in this container; the InRelease signature and package hashes are still verified." >&2
    ;;
esac

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
