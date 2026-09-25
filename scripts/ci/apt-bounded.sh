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
# WHEN THIS STOPS WORKING: this flag only ever waives an EXPIRY. MEASURED
# 2026-09-25 (six checks, all today -- no comparison to any earlier date is
# claimed):
#
#   deb.debian.org/debian/dists/bullseye/InRelease                        200
#   deb.debian.org/debian/dists/bullseye-updates/InRelease                200
#   deb.debian.org/debian-security/dists/bullseye-security/InRelease     200
#   archive.debian.org/debian/dists/bullseye/InRelease                    200
#   archive.debian.org/debian/dists/bullseye-updates/InRelease            200
#   archive.debian.org/debian-security/dists/bullseye-security/InRelease 404
#
# If deb.debian.org ever stops serving bullseye entirely, THIS flag will not
# help -- a 404 is not an expiry -- but the separate, REACTIVE
# archive.debian.org fallback further down this file now handles exactly that
# case: it detects the 404-shaped failure and repoints sources itself, rather
# than needing this comment revisited again. See that section for what it
# does and does not cover (bullseye-security on the archive, specifically).
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

# --------------------------------------------------------------------------
# BULLSEYE ONLY, REACTIVE, PER-SOURCE: fall back to archive.debian.org once
# deb.debian.org has actually stopped serving a SPECIFIC suite, rather than
# merely being expired.
#
# tan-cli#1257 option 2, hardened after review (the review found two HIGH
# defects in the first cut of this section -- both are why the design below
# is per-source and line-scoped rather than the blanket, whole-output version
# that shipped first; that history is worth knowing if this is touched again,
# but the code below is what actually runs).
#
# Distinct from the Check-Valid-Until waiver just above, which only ever
# waives an EXPIRY -- a 404 is not an expiry, and no `Acquire::*` flag makes
# apt tolerate a repository that plainly does not answer. This section does
# nothing on every run where deb.debian.org still serves bullseye (every run
# as of this writing) and engages only once an `update` attempt reports the
# "gone" signature for a SPECIFIC repository URI.
#
# MEASURED 2026-09-25 (six checks, all today):
#
#   deb.debian.org/debian/dists/bullseye/InRelease                        200
#   deb.debian.org/debian/dists/bullseye-updates/InRelease                200
#   deb.debian.org/debian-security/dists/bullseye-security/InRelease     200
#   archive.debian.org/debian/dists/bullseye/InRelease                    200
#   archive.debian.org/debian/dists/bullseye-updates/InRelease            200
#   archive.debian.org/debian-security/dists/bullseye-security/InRelease 404
#
# So the archive carries bullseye's MAIN and UPDATES suites, but not yet
# SECURITY. Both of those facts are decisions this section encodes, not just
# measurements: bullseye-updates does NOT need dropping on the archive path
# (it's there), and bullseye-security's continued absence is handled by
# failing loudly, not by silently dropping that one suite -- see below.
#
# THE TRIGGER, precisely: an `update` attempt's own output containing apt's
# own, LINE-SCOPED
#
#   E: The repository '<URI> <suite> Release' does not have a Release file.
#
# -- the exact message apt emits, confirmed against a live `apt-get update`
# in `python:3.12-slim-bullseye` with deb.debian.org's http proxied to a
# 404-only stub, for a genuinely missing dists/<suite> Release file, and
# ONLY for that. Deliberately NOT a bare `404  Not Found` line: that also
# appears for a by-hash package-index fetch and for a pool `.deb` 404 during
# `install`, neither of which means the repository itself is gone -- an
# earlier version of this trigger matched those too and a reviewer caught it.
# Deliberately NOT rc alone either: apt reports both this and a plain
# timeout/transient failure as rc=100, and retrying a 404 forever is exactly
# the anonymous-exhaustion failure mode this wrapper exists to avoid
# attributing correctly. An expiry ("is expired") matches neither this line
# nor a bare 404, so it keeps going through the Check-Valid-Until waiver
# above untouched, and a real transient/DNS/timeout failure -- which does not
# print this line either -- keeps today's plain retry.
#
# `install` IS EXCLUDED, deliberately (`_APT_SUBCOMMAND` below): a rewrite
# triggered there cannot help the invocation that triggered it (`install`
# does not re-fetch Release files the way `update` does), and a step never
# re-runs `update` after its own `install` call, so it could not help any
# later invocation either. Gated for real, not just by the message shape.
#
# NOT DEBOUNCED to two consecutive occurrences before rewriting, though the
# review raised it as worth considering: apt's own "does not have a Release
# file" is not a byte-level flip-flop the way a raw connection timeout is --
# it is apt's own conclusion, reached only after `Acquire::Retries=3` (see
# `ACQ` above) already failed to fetch a Release file that byte-for-byte
# should not change attempt to attempt. Requiring a second occurrence before
# reacting would spend a whole extra attempt on every GENUINE case (and this
# wrapper only gets `APT_ATTEMPTS` of them, 3 by default) to buy protection
# against a failure mode (a one-off flicker in apt's structural verdict) that
# has not been observed and does not match how apt reaches this message.
#
# THE REWRITE IS PER-SOURCE, not blanket: only the sources-file field(s) that
# EXACTLY EQUAL the failing URI move to archive.debian.org. If only
# `http://deb.debian.org/debian` 404s while
# `http://deb.debian.org/debian-security` still serves, ONLY the former moves
# -- security is left completely alone on a host that is still answering for
# it. A blanket rewrite (the first cut of this fix) would have moved security
# too, onto an archive that does not carry it, turning a perfectly recoverable
# state into a guaranteed FATAL below; a reviewer caught that as the more
# serious of the two HIGH defects. The match is on the WHOLE URI FIELD, not a
# substring, so `http://deb.debian.org/debian` can never also catch
# `http://deb.debian.org/debian-security` the way a naive string replace
# would (`-security` is a literal suffix of the same host+prefix, not a
# separate host). Every distinct failing URI named in one attempt's output is
# rewritten in the same pass -- if all three suites 404 together (Debian's own
# usual pattern for retiring an EOL release, though that is not asserted as
# something this wrapper measured happening again), all three move in one
# attempt rather than needing one attempt per suite.
#
# ONE trailing "/" IS tolerated on either side of the comparison (tan-cli#1257
# review): a sources-file field authored as `.../debian/` and apt's own
# un-slashed `.../debian` in its error line name the same repository, and the
# reverse is just as possible. No OTHER normalisation is attempted --
# scheme/host case, a doubled slash, and so on all still require a byte-exact
# match, deliberately: this fix is for a repository that moved host, not a
# general fuzzy-URI matcher. The field being rewritten keeps ITS OWN trailing
# slash (or lack of one) in the result; only the host changes.
#
# A deb822 `URIs:` value FOLDED onto an indented continuation line (RFC 5322
# folding, which deb822 permits) is NOT joined back before matching -- an
# accepted, measured gap, not an oversight: `python:3.12-slim-bullseye`'s own
# sources are a flat one-line `sources.list` (measured, see below), so no real
# call site is affected today. A folded file that this fallback cannot
# resolve a URI in simply matches nothing, and the L2 "nothing was rewritten"
# NOTICE further down fires rather than a silent, incorrect edit.
#
# BULLSEYE-SECURITY, THE DECISION: fail loudly rather than silently drop it.
# After a rewrite, if a retry's own "does not have a Release file" line names
# a URI whose host is ALREADY `archive.debian.org`, the wrapper refuses to
# keep retrying a deterministically-dead mirror and exits immediately with a
# named, unambiguous error naming that exact URI. A CI failure that says
# outright "archive.debian.org/debian-security has no Release file either" is
# the loud failure the guiding principle (never silently lose the security
# index) asks for; quietly continuing with main and updates while security
# silently stopped being checked is exactly the outcome this refuses.
#
# THE REWRITE TARGET IS AN OVERRIDABLE ROOT, `APT_SOURCES_ROOT` (default
# `/etc/apt`), not a caller knob -- no call site sets it, same footing as
# `APT_OS_RELEASE_FILE` above. It lets tests point the rewrite at a throwaway
# tree instead of mutating a real `/etc/apt`. THIS SEAM ALONE cannot enable
# the fallback on a non-bullseye host -- it only says WHERE the rewrite looks,
# never WHETHER `APT_OS_CODENAME` reads as `bullseye` in the first place. That
# said, "no ambient-variable path to this at all" would overstate it: the
# INHERITED `APT_OS_RELEASE_FILE` seam (landed with the Check-Valid-Until
# waiver in #1273, unchanged here) is exactly such a path -- an environment
# that exported it pointing at a bullseye-shaped file would make a
# non-bullseye host read as bullseye for BOTH the waiver and this fallback.
# That exposure is pre-existing and not widened by this change; it is not
# eliminated by it either.
#
# IDEMPOTENT BY CONSTRUCTION: `archive.debian.org` does not itself contain the
# substring `deb.debian.org`, so a URI already rewritten no longer equals any
# `deb.debian.org`-hosted target and a second pass (an earlier attempt in this
# same invocation, or a PRIOR invocation of this wrapper in the same container
# -- a step typically calls this script twice: `update` then `install`, and
# the rewrite lands on the real, persistent `/etc/apt` those share) has
# nothing left to match for that URI. A later `install` invocation reads the
# already-rewritten file directly; per the exclusion above it never attempts
# to detect or rewrite anything itself regardless.
: "${APT_SOURCES_ROOT:=/etc/apt}"

# tan-cli#1257 review: the fallback only ever engages for `update` (see
# above). `$1` is the apt-get subcommand every call site passes as this
# script's own first argument (`apt-bounded.sh update ...` /
# `apt-bounded.sh install ...`).
_APT_SUBCOMMAND="${1:-}"

_apt_release_gone_uris() {
  # Prints, one per line, the DISTINCT URIs from every occurrence of apt's
  # own, line-scoped
  #   E: The repository '<URI> <suite> Release' does not have a Release file.
  # in $1 -- see the trigger discussion above for why this exact line, and
  # only this line, is what "gone" means here.
  printf '%s\n' "$1" | sed -n \
    "s/^E: The repository '\([^ ]*\) .*does not have a Release file\.\$/\1/p" \
    | sort -u
}

_bullseye_rewrite_one_source() {
  # Rewrites ONLY sources-file fields that EXACTLY EQUAL $1 (the failing URI,
  # taken verbatim from `_apt_release_gone_uris`) to its archive.debian.org
  # equivalent -- see THE REWRITE IS PER-SOURCE above. Appends every file it
  # actually changed to the global `_bullseye_rewritten_files` array; the
  # caller resets that array once before calling this per gone URI, not here,
  # so a multi-URI event reports every touched file together.
  local target="$1" repl f tmp line line_no_cr changed_line field target_stripped repl_stripped
  repl="${target/deb.debian.org/archive.debian.org}"
  # tan-cli#1257 review: strip a single trailing "/" for the COMPARISON only
  # (see THE REWRITE IS PER-SOURCE above) -- `repl` above still carries
  # whatever slash `target` had, so `repl_stripped` needs its own strip, not
  # a copy of `target_stripped`'s.
  target_stripped="${target%/}"
  repl_stripped="${repl%/}"
  local candidates=()
  [ -f "${APT_SOURCES_ROOT}/sources.list" ] && candidates+=("${APT_SOURCES_ROOT}/sources.list")
  if [ -d "${APT_SOURCES_ROOT}/sources.list.d" ]; then
    for f in "${APT_SOURCES_ROOT}/sources.list.d"/*.list "${APT_SOURCES_ROOT}/sources.list.d"/*.sources; do
      [ -f "$f" ] && candidates+=("$f")
    done
  fi
  # `${candidates[@]+"${candidates[@]}"}`, NOT a bare `"${candidates[@]}"`:
  # bash <4.4 (this repo's own macOS test host ships 3.2.57, and CI's Ubuntu
  # runners have shipped far newer bash for years, but this script does not
  # get to assume that) raises `unbound variable` under `set -u` when
  # expanding an EMPTY array's `[@]` VALUES -- measured. `${#candidates[@]}`
  # and `${!candidates[@]}` (length/keys) are NOT affected, only this form.
  # An empty `candidates` here (no `sources.list`, no `sources.list.d` entry)
  # must fall through to the loop simply never running -- the L2 "nothing was
  # rewritten" NOTICE downstream, not a raw shell crash.
  for f in ${candidates[@]+"${candidates[@]}"}; do
    # Cheap pre-filter: skip a file that does not contain the target URI as a
    # substring anywhere at all, so a file wholly unrelated to this URI is
    # never opened, CRLF-normalised, or counted as touched. `-F`: the target
    # is a literal URI (its `.` and `/` must match literally), not a pattern.
    # The STRIPPED target, so a file spelling the URI with a trailing slash
    # this fallback would otherwise treat as equal is not skipped here either.
    grep -qF -- "$target_stripped" "$f" 2>/dev/null || continue

    tmp="$(mktemp)" || {
      echo "apt-bounded: FATAL tan-cli#1257 -- mktemp failed while preparing to rewrite $f from $target to $repl" >&2
      exit 1
    }
    changed_line=0
    # A tmp-file-and-copy, deliberately NOT `sed -i`: GNU sed accepts
    # `-i -e '...'` with the suffix argument omitted; BSD/macOS sed (this
    # repo's own test host) requires `-i` to take one, so the identical
    # invocation there reads the following `-e` as the backup suffix and
    # mangles the script instead of editing in place. Reading the file here
    # needs no root; `$SUDO` is reserved for the one step that actually does,
    # the write-back below.
    while IFS= read -r line || [ -n "$line" ]; do
      # Trim a trailing CR before splitting into fields, not after: an
      # untrimmed CRLF `.sources` line's LAST field (very often the URI
      # itself, on a bare `URIs: <uri>` line) would otherwise carry a
      # trailing `\r` baked into the value, so a byte-exact `[ "$field" =
      # "$target" ]` comparison would silently never match -- the fix would
      # become a silent no-op on a CRLF-authored deb822 file, exactly the
      # class of bug this script's os-release CRLF trim above already exists
      # to avoid. Measured with a CRLF `.sources` fixture.
      line_no_cr="${line%$'\r'}"
      local -a fields=()
      read -ra fields <<< "$line_no_cr"
      local rewrote_this_line=0
      if [ "${#fields[@]}" -ge 2 ] && { [ "${fields[0]}" = "deb" ] || [ "${fields[0]}" = "deb-src" ]; }; then
        # One-line sources.list syntax: `deb [opts] URI suite comp...`. The
        # URI is whichever FIELD equals $target exactly, wherever an optional
        # `[opts]` token puts it -- not assumed to be a fixed position.
        for i in "${!fields[@]}"; do
          [ "$i" -eq 0 ] && continue
          field="${fields[$i]}"
          if [ "${field%/}" = "$target_stripped" ]; then
            # Keep THIS FIELD's own trailing slash (or lack of one); only the
            # host changes. `target`'s own slash-ness plays no part here.
            case "$field" in
              */) fields[i]="${repl_stripped}/" ;;
              *) fields[i]="$repl_stripped" ;;
            esac
            rewrote_this_line=1
          fi
        done
      elif [ "${fields[0]:-}" = "URIs:" ]; then
        # deb822 syntax: `URIs: uri1 uri2 ...` -- rewrite only the field(s)
        # that match; a stanza naming more than one URI keeps the others. A
        # value FOLDED onto its own indented continuation line is not joined
        # back before this match runs -- see the accepted-gap note above.
        for i in "${!fields[@]}"; do
          [ "$i" -eq 0 ] && continue
          field="${fields[$i]}"
          if [ "${field%/}" = "$target_stripped" ]; then
            case "$field" in
              */) fields[i]="${repl_stripped}/" ;;
              *) fields[i]="$repl_stripped" ;;
            esac
            rewrote_this_line=1
          fi
        done
      fi
      if [ "$rewrote_this_line" -eq 1 ]; then
        # Rejoined with single spaces, cosmetic only: any original tabs or
        # multi-space alignment on a REWRITTEN line are lost, but apt parses
        # `sources.list`/deb822 fields on plain whitespace and treats every
        # run of it identically, so this changes nothing apt-visible.
        printf '%s\n' "${fields[*]}" >> "$tmp"
        changed_line=1
      else
        printf '%s\n' "$line_no_cr" >> "$tmp"
      fi
    done < "$f"

    if [ "$changed_line" -eq 0 ] || cmp -s "$f" "$tmp"; then
      # The pre-filter's substring hit did not actually land on a real field
      # match (e.g. the URI appeared only inside a comment) -- leave the file
      # untouched and unreported.
      rm -f "$tmp"
      continue
    fi
    if $SUDO cp "$tmp" "$f"; then
      rm -f "$tmp"
      _bullseye_rewritten_files+=("$f")
    else
      # `$?` here, not after a `!`-negated condition: bash preserves the
      # NEGATED construct's own 0/1 status across `!`, not the wrapped
      # command's real exit code -- measured, and the reason this branch is
      # written as a plain `if cmd; then ... else ...` rather than
      # `if ! cmd; then ...`.
      _cp_rc=$?
      echo "apt-bounded: FATAL tan-cli#1257 -- writing the rewritten $f back failed (sudo cp exit ${_cp_rc}); sources.list may now be inconsistent -- fix it by hand before retrying" >&2
      rm -f "$tmp"
      exit 1
    fi
  done
}

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

  # On bullseye, apt-get's own output has to be inspected for the archive
  # fallback's trigger (see above), so it is captured instead of being left to
  # inherit this script's stdout/stderr directly, then printed verbatim
  # afterward so nothing is lost from the CI log -- just batched instead of
  # streamed live. Every other codename keeps the original direct passthrough,
  # unchanged.
  attempt_output=""
  if [ "$APT_OS_CODENAME" = "bullseye" ]; then
    set +e
    attempt_output=$($SUDO timeout --signal=TERM --kill-after=30 "$slice" apt-get "${ACQ[@]}" "$@" 2>&1)
    rc=$?
    set -e
    printf '%s\n' "$attempt_output"
  else
    set +e
    $SUDO timeout --signal=TERM --kill-after=30 "$slice" apt-get "${ACQ[@]}" "$@"
    rc=$?
    set -e
  fi
  [ "$rc" -eq 0 ] && exit 0

  if [ "$APT_OS_CODENAME" = "bullseye" ] && [ "$_APT_SUBCOMMAND" = "update" ] && [ "$rc" -eq 100 ]; then
    _gone_uris="$(_apt_release_gone_uris "$attempt_output")"
    if [ -n "$_gone_uris" ]; then
      _fatal_uri=""
      _rewrite_uris=""
      while IFS= read -r _uri; do
        [ -z "$_uri" ] && continue
        case "$_uri" in
          http://archive.debian.org/*|https://archive.debian.org/*)
            _fatal_uri="$_uri"
            ;;
          http://deb.debian.org/*|https://deb.debian.org/*)
            _rewrite_uris="${_rewrite_uris}${_uri}"$'\n'
            ;;
          *)
            # Some third host apt was configured to fetch from -- not this
            # fallback's concern; leave it to the normal retry/exhaustion
            # path below.
            ;;
        esac
      done <<< "$_gone_uris"

      if [ -n "$_fatal_uri" ]; then
        # Already rewritten (this attempt's or an earlier invocation's) and
        # archive.debian.org ALSO has no Release file for what apt just asked
        # for -- deterministic, not transient. Retrying changes nothing here;
        # continuing to loop would either burn the rest of the step budget on
        # a mirror that is not going to answer, or worse, present as ordinary
        # retry exhaustion instead of naming the real cause. Fail loudly and
        # immediately instead: never silently drop a suite, security least of
        # all. The apt output naming this URI was already printed above
        # (`printf '%s\n' "$attempt_output"`) -- not repeated here.
        echo "apt-bounded: FATAL tan-cli#1257 -- ${_fatal_uri} has no Release file on archive.debian.org either (see the apt output above for apt's own error). Not retrying a dead mirror. Measured 2026-09-25, this is the expected outcome for bullseye-security specifically -- a different URI here is a NEW gap and needs its own investigation. The fix is upstream (Debian archiving the missing suite) or an explicit, reviewed change to this script, never another retry." >&2
        exit "$rc"
      fi

      if [ -n "$_rewrite_uris" ]; then
        _bullseye_rewritten_files=()
        while IFS= read -r _uri; do
          [ -z "$_uri" ] && continue
          _bullseye_rewrite_one_source "$_uri"
        done <<< "$_rewrite_uris"
        _gone_uris_oneline="$(printf '%s' "$_rewrite_uris" | tr '\n' ' ')"
        if [ "${#_bullseye_rewritten_files[@]}" -gt 0 ]; then
          # De-duplicated: the SAME sources file is legitimately appended once
          # per gone URI it contained (e.g. one file naming both `bullseye`
          # and `bullseye-updates`), which would otherwise repeat its own path
          # in the announcement below.
          _rewritten_files_deduped="$(printf '%s\n' "${_bullseye_rewritten_files[@]}" | sort -u | tr '\n' ' ')"
          _msg="tan-cli#1257: archive.debian.org fallback -- deb.debian.org no longer serves ${_gone_uris_oneline}(no Release file); repointed ${_rewritten_files_deduped}to the archive.debian.org equivalent and retrying. Measured 2026-09-25: archive.debian.org carries bullseye main + bullseye-updates; bullseye-security is not there yet, and a retry that finds it still missing there will fail loudly rather than move on silently."
          echo "apt-bounded: NOTICE ${_msg}" >&2
          echo "::warning::apt-bounded ${_msg}"
        else
          echo "apt-bounded: NOTICE tan-cli#1257 -- apt reported ${_gone_uris_oneline}gone, but no file under APT_SOURCES_ROOT=${APT_SOURCES_ROOT} names that URI, so nothing was rewritten. The next attempt will hit the same failure." >&2
        fi
        # Fall through to the retry classification below with sources now
        # rewritten -- rc is already 100, already in the retryable set, so the
        # loop's existing accounting (attempt count, dpkg recovery, shared
        # deadline) needs no special case to pick this up on the next pass.
      fi
    fi
  fi

  if [ "$rc" -ne 124 ] && [ "$rc" -ne 100 ]; then
    echo "apt-bounded: apt-get exited $rc (not a timeout/transient) -- not retrying" >&2
    exit "$rc"
  fi
done
echo "apt-bounded: all ${APT_ATTEMPTS} attempts failed (last rc=$rc)" >&2
exit "$rc"
