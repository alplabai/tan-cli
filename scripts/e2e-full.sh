#!/usr/bin/env bash
# Full cross-platform e2e for tan: fresh host AND dirty host, to a real ARM ELF.
#
# ONE script, run identically on Windows (Git Bash) and Linux (WSL). Anything
# that differs is a finding, not something to paper over with a second script.
#
# Usage: e2e-full.sh <tan-binary> <workdir>
#
# Every regression assertion here was validated against the KNOWN-BAD
# v0.5.0-rc3 asset first and observed to FAIL. A check that has never seen its
# bug is not a check.
set -uo pipefail

SRC_BIN="${1:?usage: e2e-full.sh <tan-binary> <workdir>}"
WORK="${2:?usage: e2e-full.sh <tan-binary> <workdir>}"

# Captured BEFORE `export HOME="$WORK/home"` sandboxes it below. Some later
# assertions need to tell "this host already had it" apart from "this run's
# own debris" -- a candidate found under the sandboxed $HOME could just be
# something THIS run extracted a moment earlier, so they must scan the REAL
# home, never the sandbox.
REAL_HOME="${HOME:-}"

PASS=0; FAIL=0; FAILED_NAMES=""
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES="$FAILED_NAMES|$1"; printf '  FAIL  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }
hdr()  { printf '\n-- %s --\n' "$1"; }

# A previous run's tree MUST be gone before this one starts, and a plain
# `rm -rf` is not enough to guarantee that here: a `west update` checkout on
# Windows leaves read-only files, so `rm -rf` reports
# `Permission denied` / `Directory not empty`, exits non-zero, and -- because
# this script is `set -uo pipefail` without `-e` -- the run CONTINUES on a
# half-deleted tree. That happened, and it turned a clean 23/0 into 3/26 whose
# every failure was leftover state rather than a defect. A harness that runs on
# a dirty tree reports fiction, so this aborts instead.
chmod -R +w "$WORK" 2>/dev/null || true
rm -rf "$WORK" 2>/dev/null || true
if [ -e "$WORK" ]; then
  echo "ABORT: could not fully remove the previous run's tree at $WORK" >&2
  echo "       survivors:" >&2
  find "$WORK" -mindepth 1 2>/dev/null | head -5 | sed 's/^/         /' >&2
  echo "       re-run after removing it; continuing would measure stale state." >&2
  exit 2
fi
# Guarded (tan-cli#506, the other half of the `cd "$WORK/proj"` guard below):
# `set -e` is deliberately off in this harness, so an unguarded failure here
# -- a read-only or full filesystem, a quota, `$WORK` on a path this user
# cannot write -- sails past silently, and `export HOME` on the next line then
# points at a directory that was never created.
#
# MEASURED against current `dev`, which already has the `cd "$WORK/proj"`
# guard 90 lines below: an unwritable `$WORK` does NOT run all the way into a
# misattributed tan failure any more -- that guard already catches it and
# exits 2. But the diagnosis it prints names the wrong site: "could not cd
# into $WORK/proj", when the real cause is THIS `mkdir` failing 90 lines
# earlier and being silently outlived. For an unattended nightly run
# (tan-cli#754), the Actions tab should name the actual failing operation at
# the actual line, not rely on a guard further down the script to catch the
# same root cause under someone else's name.
mkdir -p "$WORK/home" "$WORK/proj" || {
  echo "ABORT: could not create the sandbox under $WORK" >&2
  echo "       (read-only filesystem, quota, or an unwritable parent)" >&2
  exit 2
}
export HOME="$WORK/home"; export USERPROFILE="$WORK/home"
unset ALP_SDK_ROOT ZEPHYR_BASE ALP_FLASH_FORCE 2>/dev/null || true
git config --global core.longpaths true 2>/dev/null || true

# A real `build` needs a toolchain. If a Zephyr SDK exists on this machine,
# bind it -- otherwise `zephyrSdk` legitimately fails, doctor legitimately
# exits 4, and the ARM-ELF leg cannot run at all. Binding it is what a real
# user has; NOT binding it would make the build leg untestable rather than
# rigorous.
# Candidates are DERIVED, never a hardcoded account. This repo is public and
# its history is permanent, so `/home/<someone>` in a tracked file is a leak --
# tests/gates/test_no_leaked_host_paths.py caught exactly that here. Export
# ZEPHYR_SDK_INSTALL_DIR to skip the search entirely.

# One toolchain-presence test, shared by EVERY "does a usable Zephyr SDK live
# here" scan below (this candidate list, the CMake dot-file/registry reads,
# RUN_SDK's post-install scan, and HOST_ZSDK's host scan) so none of them can
# be satisfied by a bare `sdk_version` file alone. `sdk_version` sits at the
# SDK root but the compiler ships in a SEPARATE tarball under it
# (`gnu/arm-zephyr-eabi` on SDK 1.0.x, `arm-zephyr-eabi` on 0.16.x -- both
# accepted). Testing for the GCC binary itself, not just the directory,
# matters: a `timeout` mid-extract leaves an EMPTY toolchain directory that a
# bare `-d` test would wave through.
_sdk_has_toolchain() {
  # `${1:-}`, not a bare `$1`: under `set -u` a call with NO argument aborts
  # the whole harness (`$1: unbound variable`, rc=1, nothing after it runs)
  # instead of returning a status. Unreachable today -- every call site quotes
  # an argument -- but this is the shared choke point for every SDK scan in
  # this script, so a future caller's typo would take the whole run down.
  [ -f "${1:-}/sdk_version" ] || return 1
  local _g
  for _g in "$1"/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc* "$1"/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc*; do
    [ -f "$_g" ] && return 0
  done
  return 1
}

ZEPHYR_SDK_VERSION="${ZEPHYR_SDK_VERSION:-1.0.1}"
# REAL_HOME, never the sandboxed $HOME (already redirected above at :48) -- a
# candidate under the sandbox could only be this run's own debris, and a dev
# box whose only SDK lives under its real ~/zephyr-sdk-<ver> would otherwise
# bind nothing and silently degrade the ARM-ELF leg to the refusal path.
for cand in \
  "${ZEPHYR_SDK_INSTALL_DIR:-}" \
  "$REAL_HOME/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "$REAL_HOME/../zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/opt/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/usr/local/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/c/zephyr-sdk-$ZEPHYR_SDK_VERSION"
do
  _sdk_has_toolchain "$cand" && { export ZEPHYR_SDK_INSTALL_DIR="$cand"; break; }
done
# `west sdk install` records the location via CMake's user package registry.
# On POSIX that channel is a dot-file tree, ~/.cmake/packages/Zephyr-sdk; on
# Windows CMake does NOT use that path at all -- the registry is the channel,
# HKCU\Software\Kitware\CMake\Packages\Zephyr-sdk, probed separately below
# where a Scenario-B build's success is judged against a pre-existing SDK.
if [ -z "${ZEPHYR_SDK_INSTALL_DIR:-}" ]; then
  for reg in "$REAL_HOME/.cmake/packages/Zephyr-sdk"/*; do
    [ -f "$reg" ] || continue
    # The dot-file records the directory holding Zephyr-sdkConfig.cmake, i.e.
    # <sdk>/cmake -- NOT <sdk> itself (same shape as the HKCU registry probed
    # further below, Scenario B's Zephyr SDK check) -- but a value that
    # ALREADY points at the SDK root must not be forced through dirname too,
    # or it resolves to the root's PARENT and this never binds a real SDK.
    # Test both the raw value and its dirname.
    _z=$(tr -d '\r\n' < "$reg")
    cand=$(dirname "$_z" 2>/dev/null)
    if [ -n "$_z" ] && _sdk_has_toolchain "$_z"; then
      export ZEPHYR_SDK_INSTALL_DIR="$_z"; break
    elif [ -n "$_z" ] && _sdk_has_toolchain "$cand"; then
      export ZEPHYR_SDK_INSTALL_DIR="$cand"; break
    fi
  done
fi
[ -n "${ZEPHYR_SDK_INSTALL_DIR:-}" ] && echo "  sdk:  $ZEPHYR_SDK_INSTALL_DIR" || echo "  sdk:  none found (build leg will be reported, not silently skipped)"

# GUARDED, and it has to be: line 12 sets `-uo pipefail` deliberately WITHOUT
# `-e`, so the harness keeps scoring after an individual assertion fails. That
# choice makes an unchecked `cd` far more dangerous here than in an ordinary
# script -- a failed `cd` does not stop the run, it relocates the REST of it.
# Every relative path below (the alp-sdk clone, `./alp-sdk`, the scaffolded
# project, the build outputs) would then resolve against whatever directory the
# shell happened to be in, and the harness would report a full set of results
# about a tree that is not the one under test -- the worst failure mode this
# harness has, because it looks exactly like a real run. tan-cli#506.
cd "$WORK/proj" || {
	echo "ABORT: could not cd into $WORK/proj -- every path below is relative to it," >&2
	echo "       so continuing would measure a different tree and report it as this one." >&2
	exit 2
}

# tan must sit BESIDE alp-sdk/ -- the documented quickstart layout, and
# load-bearing for #323 (bootstrap only plans a relocation when the directory
# "holds more than this checkout"; tan itself is what makes it hold more).
#
# tan-cli#349 made this NOT a single-file copy. The freeze is --onedir now: the
# launcher needs its `_internal/` sibling, and copying the executable alone
# gets `[PYI-...:ERROR] Failed to load Python DLL ... LoadLibrary: The
# specified module could not be found.` -- which is exactly what this harness
# did on its first post-#349 run, turning 23 checks red for one reason.
#
# So install it the way install.sh / install.ps1 actually do: the whole tree
# into a lib dir, plus a thin launcher beside alp-sdk/. That keeps the
# quickstart layout the assertions depend on AND exercises the real shipped
# shape rather than a copy no installer ever produces.
_bin_name=$(basename "$SRC_BIN")
_src_dir=$(cd "$(dirname "$SRC_BIN")" && pwd)
if [ -d "$_src_dir/_internal" ]; then
  # `cp -r SRC DST` is NOT idempotent: when DST already exists it copies INTO
  # it, giving `tan-cli-lib/tan/tan.exe` instead of `tan-cli-lib/tan.exe`. That
  # is silent -- the launcher then points at a path that does not exist and
  # every command fails with cmd.exe's opaque
  # `'"...\tan-cli-lib\tan.exe"' is not recognized`. Copy the CONTENTS into a
  # freshly created dir so the layout cannot depend on what was there before.
  rm -rf ./tan-cli-lib
  mkdir -p ./tan-cli-lib
  cp -r "$_src_dir"/. ./tan-cli-lib/
  # Built line by line with `echo`, never a multi-line `printf` format string:
  # a CRLF-normalising pass turns escapes embedded in such a format into REAL
  # newlines, which is how this block broke once already.
  case "$_bin_name" in
    *.exe)
      # TWO DISTINCT PATHS, and conflating them destroyed the real binary once:
      # `_launcher` is what install.ps1 ships for a cmd.exe user, `TAN` is what
      # THIS POSIX harness drives. An earlier revision pointed `TAN` into the
      # lib dir but left the launcher `echo`s writing to `$TAN`, so it
      # overwrote `tan-cli-lib/tan.exe` with a 40-byte `@echo off` script --
      # every call then failed `line 1: @echo: command not found`, which reads
      # nothing like "the harness clobbered the binary".
      _launcher="$WORK/proj/tan.cmd"
      TAN="$WORK/proj/tan-cli-lib/$_bin_name"
      # `_bs` holds the separator rather than inlining a backslash: inside
      # double quotes bash parses `\\$` as an escaped `$`, so the obvious
      # `...tan-cli-lib\\${_bin_name}...` emits a LITERAL `${_bin_name}`.
      _q='"'; _bs='\'
      echo "@echo off"                                       >  "$_launcher"
      echo "${_q}%~dp0tan-cli-lib${_bs}${_bin_name}${_q} %*"  >> "$_launcher"
      # Git Bash cannot exec a `.cmd` by absolute path (exit 127), so the
      # harness drives the onedir exe directly. Same shape either way: the exe
      # still needs its `_internal/` sibling.
      ;;
    *)
      TAN="$WORK/proj/tan"
      echo '#!/bin/sh'                                             >  "$TAN"
      echo 'exec "$(dirname "$0")/tan-cli-lib/'"${_bin_name}"'" "$@"' >> "$TAN"
      chmod +x "$TAN"
      ;;
  esac
  echo "  shape: --onedir tree + launcher (tan-cli#349)"
  # PROVE the installed binary actually runs before asserting anything about
  # tan's behaviour. Every red run in this harness's history -- the missing
  # `_internal/`, the nested `tan-cli-lib/tan/`, the clobbered exe -- produced
  # a wall of failures whose real cause was that `$TAN` was not a working
  # binary. Failing HERE names it in one line instead of 20+ misattributed
  # assertion failures.
  if ! "$TAN" --version >/dev/null 2>&1; then
    echo "ABORT: the installed tan does not run: $TAN" >&2
    echo "       size: $(wc -c <"$TAN" 2>/dev/null) bytes" >&2
    echo "       error: $("$TAN" --version 2>&1 | head -2)" >&2
    exit 2
  fi
else
  # Pre-#349 single-file freeze, and any published asset up to v0.5.0-rc4.
  cp "$SRC_BIN" "./$_bin_name"
  TAN="$WORK/proj/$_bin_name"
  echo "  shape: single-file binary"
fi

echo "=== tan e2e: $(uname -s) $(uname -m) ==="
echo "  tan:  $TAN"
echo "  HOME: $HOME"

jget() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));
import functools;
p=sys.argv[2].split('.');v=d
for k in p:
    v = (v or {}).get(k) if isinstance(v,dict) else None
print(v if v is not None else 'NONE')" "$1" "$2" 2>/dev/null || echo NONE; }

# One parseable envelope on stdout, zero bytes on stderr, and -- the part
# tan-cli#358 was missing -- a verdict that is actually CHECKED.
#
# `jrun` used to score a call PASS on "stdout parsed as JSON and stderr was
# empty", never reading RC, `ok` or `exitCode`. So a `flash` that returned
# ok:false / exitCode 1 with `flash.manifest-not-found` printed
# `PASS flash: one envelope, 0-byte stderr (exit 1)` and counted toward
# "23 passed, 0 failed". A harness that cannot fail is not evidence, and this
# one had been reporting a green line for a broken command for two rounds.
#
# Usage: jrun <label> <expect> <tan args...>
#
# <expect> is the required process exit code, or `any` where the correct answer
# legitimately depends on what the host has installed -- a bare container's
# `doctor` exits 4 and a fully provisioned one exits 0, and pinning either
# number would make this harness lie on the other host. `any` waives the exact
# NUMBER and nothing else: every invariant below is enforced on every call,
# `any` included.
#
#   * stderr is empty and stdout is exactly one JSON envelope;
#   * envelope.exitCode EQUALS the process exit code -- the CLI-wide invariant.
#     Checking it here is what makes a silent divergence impossible to score as
#     a pass, whatever the expectation;
#   * envelope.ok is true if and only if the process exited 0.
#
# Every failure reason is accumulated and reported together rather than the
# first one winning: "expected exit 0, got 1" alone sends you looking in the
# wrong place when the envelope also disagreed with itself. RC is exported.
jrun() {
  local label="$1" expect="$2"; shift 2
  local o="$WORK/$label.out" e="$WORK/$label.err"
  "$TAN" "$@" >"$o" 2>"$e"; RC=$?
  local esz; esz=$(wc -c <"$e" | tr -d ' ')
  local why="" env_rc env_ok
  [ "$esz" -eq 0 ] || why="stderr $esz bytes"
  env_rc=$(jget "$o" exitCode); env_ok=$(jget "$o" ok)
  if [ "$env_rc" = "NONE" ]; then
    why="${why:+$why; }stdout is not a single JSON envelope"
  else
    [ "$env_rc" = "$RC" ] ||
      why="${why:+$why; }envelope.exitCode=$env_rc but the process exited $RC"
    if [ "$RC" -eq 0 ]; then
      [ "$env_ok" = "True" ] || why="${why:+$why; }exit 0 but ok=$env_ok"
    else
      [ "$env_ok" = "False" ] || why="${why:+$why; }exit $RC but ok=$env_ok"
    fi
  fi
  [ "$expect" = "any" ] || [ "$RC" -eq "$expect" ] ||
    why="${why:+$why; }expected exit $expect, got $RC"
  if [ -n "$why" ]; then
    bad "$label: $why"
    envelope_issues "$o"
    [ "$esz" -eq 0 ] || note "$(head -c 200 "$e")"
    return 1
  else
    ok "$label: one envelope, 0-byte stderr, ok/exitCode agree (exit $RC)"
    return 0
  fi
}

# A captured-output excerpt for `note`, safe against tan-cli#758: a
# tail-only truncation severs `Traceback (most recent call last):` -- the
# ALWAYS-first line of a Python traceback -- so a `tail -c N` excerpt of a
# real crash silently reads as clean. Silent in the dangerous direction: a
# traceback that DID occur then reads, in the report, as one that did not.
# Keeps BOTH ends (a `head` as well as the `tail`) rather than choosing one,
# and calls the fact out explicitly whenever the header is present, so it
# survives even on a capture so large neither excerpt alone would reach it.
excerpt() {
  # `${1:-}`, not a bare `$1`: under `set -u` a no-arg call would abort the
  # whole harness (`$1: unbound variable`) instead of returning a status --
  # the same convention `_sdk_has_toolchain` documents at the top of this
  # file (tan-cli#757 review MINOR 4). `excerpt` now has a dozen call sites
  # across this file; a future one calling it wrong must not take the whole
  # run down.
  local f="${1:-}"
  if grep -qF 'Traceback (most recent call last):' "$f" 2>/dev/null; then
    printf 'TRACEBACK PRESENT -- excerpt is head+tail, not the whole capture:\n'
    head -c 200 "$f" 2>/dev/null
    printf '\n...\n'
    tail -c 400 "$f" 2>/dev/null
  else
    tail -c 400 "$f" 2>/dev/null
  fi
}

# Every `issues[]` entry of a JSON envelope -- code, severity and the WHOLE
# message -- for a `note`-style report of why a `tan` command failed.
#
# tan-cli#1187: `e2e-container` was red on every `dev` run for six consecutive
# days and the log never once said why. The failure paths printed a
# `head -c 400` excerpt of the raw envelope, and an envelope's scalar/`data`
# prefix alone is already longer than that, so what reached the reader was
#
#   {"command":"bootstrap","ok":false,"exitCode":1,...,"factsFromManifest":tr
#
# cut mid-token, with `issues[]` -- the only field that names a cause -- never
# appearing at all (measured on run 33855586144, `dev`, 2026-09-04). A
# byte-count excerpt is the wrong tool for an envelope specifically: it
# truncates by POSITION, and the causal field is last, so the excerpt is
# guaranteed to spend its whole budget on fields nobody is reading.
#
# Prints the issues and nothing else, so it stays short on the envelopes that
# carry one or two. The raw head survives only where there is no parseable
# envelope to read -- there the bytes themselves are the evidence, and a
# `tan` that printed something other than an envelope is its own finding.
envelope_issues() {
  # `${1:-}`, not a bare `$1`, for the same reason `excerpt` above uses it:
  # under `set -u` a no-arg call must return a report, not abort the run.
  local f="${1:-}"
  # stderr deliberately NOT suppressed: a python3 that cannot run here must
  # say so rather than leave a failure path printing nothing at all, which is
  # the exact shape (a diagnostic that silently explains nothing) this
  # function exists to remove.
  python3 - "$f" <<'PY' | sed 's/^/        /'
import json, sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        envelope = json.load(handle)
except Exception as exc:
    print(f"NOT a parseable envelope ({type(exc).__name__}: {exc}).")
    print(f"First 400 bytes of {path} verbatim:")
    try:
        with open(path, "rb") as handle:
            print(handle.read(400).decode("utf-8", "replace"))
    except OSError as read_exc:
        print(f"  ...and the capture could not be read either: {read_exc}")
    raise SystemExit

if not isinstance(envelope, dict):
    print(f"envelope is a {type(envelope).__name__}, not an object: {envelope!r}")
    raise SystemExit

issues = envelope.get("issues")
if not issues:
    # Not an error here: `ok:false` with an empty `issues[]` is itself a
    # reportable defect (a command that failed and named no reason), and
    # saying so is more use than printing 400 bytes of `data`.
    print(f"envelope carries NO issues[] (ok={envelope.get('ok')!r}, "
          f"exitCode={envelope.get('exitCode')!r}) -- nothing on it names a cause")
    raise SystemExit

print(f"envelope issues[] ({len(issues)}):")
for index, issue in enumerate(issues):
    if not isinstance(issue, dict):
        print(f"  issues[{index}] is not an object: {issue!r}")
        continue
    print(f"  issues[{index}] {issue.get('severity', '?')} "
          f"{issue.get('code', '?')}: {issue.get('message', '')}")
    # Anything else the issue carries (`remediation`, `context`, ...) printed
    # rather than dropped: this runs on failure paths only, where an unread
    # field is exactly what cost six days.
    for key, value in issue.items():
        if key not in ("severity", "code", "message"):
            print(f"    {key}: {value}")
PY
}

########################  FRESH HOST  ########################
echo; echo "############ FRESH HOST ############"

hdr "version"
"$TAN" --version >"$WORK/v.out" 2>"$WORK/v.err"
[ "$(wc -c <"$WORK/v.err"|tr -d ' ')" -eq 0 ] && ok "version: 0-byte stderr" || bad "version: stderr not empty"
note "$(tr -d '\r\n' <"$WORK/v.out")"

hdr "doctor, nothing configured"
jrun doctor any doctor --format json

hdr "sdk list --online (real HTTPS, the #304 CA canary)"
# Hard 0: reaching the release index over real TLS is the #304 CA canary and
# has exactly one correct answer on every host. `jrun` now owns the assertion.
jrun sdklist 0 sdk list --online --format json

hdr "clone alp-sdk (quickstart layout)"
# tan-cli#358: the SDK revision is EXPLICIT and RECORDED. An unpinned shallow
# clone of whatever the default branch happened to be that hour makes a result
# unreproducible from the tan SHA alone -- a red leg six weeks later cannot be
# told apart from an SDK that moved under it. `ALP_SDK_REF` pins a tag/branch/SHA
# (a bare SHA needs the unshallow fetch below, since `clone --branch` will not
# take one); either way the resolved SHA is printed with the result.
ALP_SDK_REF="${ALP_SDK_REF:-dev}"
if git clone --quiet --depth 1 --branch "$ALP_SDK_REF" \
     https://github.com/alplabai/alp-sdk alp-sdk 2>"$WORK/clone.err" ||
   { git clone --quiet https://github.com/alplabai/alp-sdk alp-sdk 2>>"$WORK/clone.err" &&
     git -C alp-sdk checkout --quiet "$ALP_SDK_REF" 2>>"$WORK/clone.err"; }; then
  ALP_SDK_SHA=$(git -C alp-sdk rev-parse HEAD 2>/dev/null || echo UNKNOWN)
  ok "alp-sdk cloned ($(find alp-sdk -type f | wc -l | tr -d ' ') files)"
  note "alp-sdk ref=$ALP_SDK_REF sha=$ALP_SDK_SHA"
else
  # ABORT, never continue. Every check below needs this checkout, so carrying on
  # turns ONE environmental failure into eight misattributed ones -- measured in
  # a CA-less container, where the clone failed and the harness then reported
  # "#323: checkout was MOVED by a dry run" about a checkout that had never
  # existed. Same misattribution the $TAN guard above exists to prevent, and
  # worse here because it accuses the product for git's problem.
  bad "alp-sdk clone failed -- ABORTING; every check below needs the checkout"
  note "$(head -c 300 "$WORK/clone.err")"
  note "on a host with no CA store this is GIT's own trust, not tan's --"
  note "tan's HTTPS is self-sufficient since tan-cli#354; git needs ca-certificates."
  echo
  echo "=== $(uname -s): $PASS passed, $FAIL failed (ABORTED at the clone) ==="
  exit 1
fi

hdr "#322 doctor's CURRENT root vs bootstrap's PLANNED root"
jrun doc2 any doctor --format json
jrun bs2 any bootstrap --dry-run --format json
D=$(jget "$WORK/doc2.out" sdk.root); B=$(jget "$WORK/bs2.out" data.sdkRoot)
W=$(jget "$WORK/bs2.out" data.workspaceDir)
note "doctor=$D"; note "bootstrap=$B"; note "workspaceDir=$W"
# tan-cli#358 tightened this from non-emptiness to equality, which was right to
# reject, but the PAIR it compares is still wrong -- tan-cli#741.
#
# `doctor.sdk.root` is where the checkout IS. `bootstrap.data.sdkRoot` under
# --dry-run is where it WOULD BE. Those answer different questions, and
# `tan bootstrap` relocating a checkout into the workspace is deliberate and
# announced (tan-cli#185). So a flat equality assertion fails on every host
# where bootstrap can actually succeed, and passes only where it refuses for
# missing prerequisites and therefore plans no move at all.
#
# Measured, same script and same tan build: a pristine ubuntu:24.04 container
# scored PASS with bs2 exiting 1 ("no 'would' verb (no relocation planned)"),
# while a provisioned host scored FAIL with bs2 exiting 0 and
# doctor=.../proj/alp-sdk against bootstrap=.../proj/alp-workspace/alp-sdk.
# The container's PASS was not agreement -- it was one side not running.
#
# The invariant that holds on BOTH shapes: if no relocation is planned the two
# must be identical, and if one IS planned the destination must sit inside the
# workspace bootstrap just reported. Post-bootstrap agreement is a different
# assertion and is already covered by "#299 doctor AFTER a successful
# bootstrap", which passes on both hosts.
if [ "$D" = "NONE" ] || [ "$B" = "NONE" ] || [ -z "$B" ]; then
  bad "#322: one side resolved nothing -- doctor='$D' bootstrap='$B'"
elif [ "$D" = "$B" ]; then
  ok "#322: no relocation planned; doctor and bootstrap agree ($D)"
elif [ -z "$W" ] || [ "$W" = "NONE" ]; then
  bad "#322: bootstrap plans a move to '$B' but reported no workspaceDir to move into"
elif [ "${B#"$W"}" = "$B" ]; then
  bad "#322: bootstrap's planned root is OUTSIDE the workspace it reported"
  note "planned      = $B"
  note "workspaceDir = $W"
else
  ok "#322: relocation planned into the reported workspace ($B)"
  note "doctor still reports the pre-move root, which is why a move is planned"
fi

hdr "#323 --dry-run MUTATES NOTHING"
"$TAN" bootstrap --dry-run --sdk-root ./alp-sdk --format json >"$WORK/bsdry.out" 2>"$WORK/bsdry.err"
[ -d alp-sdk ]        && ok "#323: checkout not moved"        || bad "#323: checkout was MOVED by a dry run"
[ ! -d alp-workspace ] && ok "#323: no alp-workspace/ created" || bad "#323: dry run created alp-workspace/"
[ ! -e "$HOME/.alp" ]  && ok "#323: no ~/.alp written"         || bad "#323: dry run wrote the global pointer"
grep -q "would move\|would set" "$WORK/bsdry.out" && ok "#323: conditional wording (\"would\")" || note "no 'would' verb (no relocation planned)"

# The prerequisite set is PER HOST, exactly as metadata/bootstrap.json declares
# it -- posix: git cmake python3 ninja xz wget; windows: git cmake python ninja.
# `xz` and `wget` are POSIX-only entries; tan never asks a Windows host for them.
#
# Checking the POSIX four on every platform is what made SCENARIO B unrunnable
# on Windows: a fully provisioned Windows box (cmake, ninja, git, python all on
# PATH, and `xz` shipped by Git Bash at /mingw64/bin/xz) was scored "bare"
# purely because `wget` was absent, so the harness skipped the real-ARM-ELF leg
# and reported `scenario B: 0 passed, 0 failed`. Measured on
# MINGW64_NT-10.0-26200. No workflow invokes this script, so that meant the
# Windows provisioned-host path had never been exercised by anyone.
#
# Scenario A stays honest: on a Windows host that really does have its set, A
# reports "not applicable" rather than inventing a refusal to score.
#
# The set had ALSO been wrong on non-Windows: it checked only `cmake ninja xz
# wget` (four), silently dropping `git` and `python3` -- so a Linux host
# missing everything but `git` scored Scenario A "not applicable", the same
# false-bare-becomes-skipped shape the Windows fix above exists to catch. And
# macOS fell into that same `*)` arm and inherited `xz wget`, which tan's own
# macOS tuple never asks for and a stock Mac ships neither of (see the note at
# ~line 490): a fully provisioned Mac scored "bare" and silently skipped
# Scenario B. macOS is a shipped target (install.sh, apple-darwin), so it gets
# its own arm rather than falling through to the POSIX-with-xz/wget default.
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) HOST_KIND=windows ; HOST_IS_WINDOWS=1 ;;
  Darwin)                HOST_KIND=macos   ; HOST_IS_WINDOWS=0 ;;
  *)                     HOST_KIND=posix   ; HOST_IS_WINDOWS=0 ;;
esac
# READ from the manifest this run already cloned at :268
# (./alp-sdk/metadata/bootstrap.json) instead of hand-copying its tuples --
# a hand-copied tuple drifting from tan's own idea of the prerequisite list
# is the root cause of the whole bsA1 class of bugs this harness keeps
# re-filing. Mirrors tan/core/bootstrap.py's own `prerequisites(host)`
# fallback: windows -> prerequisites.windows; macos -> prerequisites.macos if
# non-empty else prerequisites.posix; else -> prerequisites.posix. ALP_SDK_REF
# (:267) is a documented knob, so the manifest content can legitimately vary
# by ref -- an argument FOR reading it live, not against.
HOST_PREREQS_RESULT=$(python3 - "$HOST_KIND" ./alp-sdk/metadata/bootstrap.json <<'PY' 2>/dev/null
import json, os, sys

host, path = sys.argv[1], sys.argv[2]
EXPECTED_SCHEMA = 1  # mirrors BOOTSTRAP_MANIFEST_SCHEMA_VERSION (bootstrap.py:89)


def reason(kind, detail):
    print(f"REASON:{kind}:{detail}")
    raise SystemExit(0)


# ABSENT is a first-class SUPPORTED path, not a defect: metadata/bootstrap.json
# was only added to alp-sdk on 2026-07-25 (alp-sdk#928 / v0.13.0) -- v0.12.0,
# v0.11.1, v0.11.0, v0.10.1, v0.10.0 all lack it -- and tan's own
# load_facts() (bootstrap_cmd.py:1660-1666) falls back to fallback_facts()
# for exactly this case, no error, no issue emitted. ALP_SDK_REF is a
# documented knob (used above at the clone), so a run legitimately pins a
# pre-#928 SDK on purpose.
if not os.path.exists(path):
    reason("absent", f"{path} does not exist")
try:
    doc = json.load(open(path, encoding="utf-8"))
except Exception as e:
    reason("unparseable", f"{path}: {e}")
# Refuse in this SAME try block, exactly as tan's own parse_bootstrap_manifest
# does (tan/core/bootstrap.py:513-535): schemaVersion is read FIRST there and
# a mismatch is a hard refusal, not "fall through and hope the shape still
# matches". Unlike ABSENT above, a manifest that EXISTS but is unusable is the
# shape tan itself hard-errors on (BootstrapManifestError,
# bootstrap_cmd.py:1662) rather than silently falling back for -- so this
# harness scores it too, instead of blaming tan for a manifest-shape mismatch
# that was never tan's fault.
version = doc.get("schemaVersion") if isinstance(doc, dict) else None
if not isinstance(version, int) or isinstance(version, bool) or version != EXPECTED_SCHEMA:
    reason("schema", f"schemaVersion {version!r} unsupported (tan needs {EXPECTED_SCHEMA})")
try:
    p = doc["prerequisites"]
    if host == "windows":
        lst = p["windows"]
    elif host == "macos":
        lst = p.get("macos") or p["posix"]
    else:
        lst = p["posix"]
except Exception as e:
    reason("schema", f"prerequisites.{host}: {e}")
if not isinstance(lst, list):
    reason("schema", f"prerequisites.{host} is not a list")
# Reject any entry CONTAINING whitespace (leading, trailing, OR internal)
# rather than only trimming it: `[t.strip() for t in lst if t.strip()]` let an
# entry like "cmake ninja" (one JSON string) survive as-is -- schema-valid,
# no minLength -- and `" ".join` below plus the unquoted `for _t in
# $HOST_PREREQS` in have_prereqs/missing_prereqs then WORD-SPLITS it into two
# bogus tool names, producing exactly the
# "provisioned host scored bare, Scenario B silently skipped" outcome
# documented at :350-360.
clean = [t for t in lst if isinstance(t, str) and t and not any(c.isspace() for c in t)]
if not clean:
    reason("empty", f"prerequisites.{host} has no usable (non-whitespace) tool name")
print(" ".join(clean))
PY
)
case "$HOST_PREREQS_RESULT" in
  REASON:absent:*)
    # NOT scored: see the python block above -- an absent manifest is the
    # documented legacy path, and ALP_SDK_REF is how an operator legitimately
    # selects a pre-#928 SDK. Scoring this `bad` would fail a run like
    # `ALP_SDK_REF=v0.13.0 scripts/e2e-full.sh <bin> <work>` for an input tan
    # fully supports.
    note "prereq-manifest: ${HOST_PREREQS_RESULT#REASON:absent:} -- using hand-ported FALLBACK tuples (legacy-SDK path, not a defect)"
    HOST_PREREQS=""
    ;;
  REASON:*)
    _reason_kind="${HOST_PREREQS_RESULT#REASON:}"; _reason_kind="${_reason_kind%%:*}"
    _reason_detail="${HOST_PREREQS_RESULT#REASON:$_reason_kind:}"
    # SCORED: the manifest EXISTS and is a shape tan itself refuses to use
    # (unsupported schemaVersion, unparseable JSON, or no usable tool name) --
    # never a silent fallback for load_facts() either. May drift from the
    # live SDK; the block above is preferred.
    bad "prereq-manifest: alp-sdk/metadata/bootstrap.json $_reason_kind -- $_reason_detail -- using hand-ported FALLBACK tuples, may drift from the live SDK"
    HOST_PREREQS=""
    ;;
  "")
    bad "prereq-manifest: alp-sdk/metadata/bootstrap.json read failed with no diagnosable reason -- using hand-ported FALLBACK tuples, may drift from the live SDK"
    HOST_PREREQS=""
    ;;
  *)
    HOST_PREREQS="$HOST_PREREQS_RESULT"
    ;;
esac
if [ -z "$HOST_PREREQS" ]; then
  case "$HOST_KIND" in
    windows) HOST_PREREQS="git cmake python ninja" ;;
    macos)   HOST_PREREQS="git cmake python3 ninja" ;;
    *)       HOST_PREREQS="git cmake python3 ninja xz wget" ;;
  esac
fi
hdr "prerequisite scan ($(echo "$HOST_PREREQS" | tr ' ' ','))"
# Windows accepts the interpreter as `python` OR the `py` launcher, matching
# tan's own _prereq_present() (bootstrap_cmd.py:415). Shared by have_prereqs
# (yes/no gate) and missing_prereqs (the exact absent subset -- what Scenario
# A's refusal check must assert tan named, never the whole host set: tan only
# ever names tools `check_prerequisites` found missing, bootstrap_cmd.py:473-478).
_prereq_ok() {
  if [ "$HOST_IS_WINDOWS" -eq 1 ] && [ "$1" = "python" ]; then
    command -v python >/dev/null 2>&1 || command -v py >/dev/null 2>&1
  else
    command -v "$1" >/dev/null 2>&1
  fi
}
have_prereqs() {
  for _t in $HOST_PREREQS; do
    _prereq_ok "$_t" || return 1
  done
  return 0
}
missing_prereqs() {
  local _m=""
  for _t in $HOST_PREREQS; do
    _prereq_ok "$_t" || _m="$_m $_t"
  done
  printf '%s' "${_m# }"
}
# Probe rather than assume, the same shape e2e-container.sh:52-65 already
# uses for the Docker daemon (tan-cli#759): try the real operation directly,
# then again under `sudo -n`, and RECORD which path (if any) actually worked.
# The OLD gate matched literal root only and never attempted sudo, so a
# non-root host with passwordless sudo -- `NOPASSWD:ALL`, the shape a real
# customer running behind sudo actually has -- scored byte-identical to a
# non-root host with no escalation path at all, and Scenario B (the only leg
# that reaches a real ARM ELF) was reachable ONLY as root.
#
# `apt-get check` is the probe operation, not `apt-get install`/`update`: it
# needs the SAME dpkg/apt lock a real install needs (so it genuinely fails
# Permission-denied for a user who cannot self-provision), but -- unlike
# `update` -- touches neither the network nor the package-list cache, so the
# probe itself has no side effect worth naming, the same role `docker info`
# plays in the pattern this copies.
#
# `check` tests a STRICTER property than "can install", though (tan-cli#759
# review MINOR 3): it returns 100 on unmet/broken dpkg dependencies even as
# root, which is a genuinely different finding from "cannot escalate at
# all". A root (or working-sudo) host with a half-configured package would
# otherwise score identically to a host with no privilege whatsoever and
# Scenario B would go NOT RUN where the OLD `id -u = 0` gate ran it -- so
# that state gets its own APT_MODE (`unhealthy`) rather than collapsing into
# `none`, even though CAN_APT stays 0 either way (a genuinely broken package
# state means a real install would likely fail too, same as `none`).
CAN_APT=0
APT_MODE=none
APT="apt-get"
if command -v apt-get >/dev/null 2>&1; then
  if apt-get check >/dev/null 2>&1; then
    CAN_APT=1; APT_MODE=root; APT="apt-get"
  elif sudo -n apt-get check >/dev/null 2>&1; then
    # `-n` carried into the INSTALL prefix too (tan-cli#759 review MINOR 1):
    # the probe already used `sudo -n`, but a plain `sudo apt-get` here would
    # let the real install at the call sites below block on a password
    # prompt (or die "no tty present and no askpass program specified") if a
    # cached, non-`NOPASSWD` credential's 15-minute `timestamp_timeout`
    # expires between this probe and that install.
    #
    # `env DEBIAN_FRONTEND=noninteractive` inside the sudo invocation, not a
    # bare exported var (MINOR 2): sudo's default `env_reset` (plus
    # `secure_path`) strips a caller-exported DEBIAN_FRONTEND on the sudo
    # path ONLY -- root and no-sudo hosts never lose it -- so `apt-get
    # install -y -qq` under sudo could hit a debconf prompt unattended.
    # `env` sets it fresh for the child AFTER sudo's reset. Not `sudo -E`: a
    # sudoers policy without `SETENV` refuses that outright.
    CAN_APT=1; APT_MODE=sudo; APT="sudo -n env DEBIAN_FRONTEND=noninteractive apt-get"
  elif [ "$(id -u 2>/dev/null || echo 1)" = "0" ] || sudo -n true >/dev/null 2>&1; then
    APT_MODE=unhealthy
  fi
fi
# Interpolated, not a hardcoded "cmake/ninja/xz/wget": that literal is only
# true for the old always-POSIX-four set this section replaces, and would
# misreport what was actually probed on Windows or macOS.
HOST_PREREQS_LABEL="$(echo "$HOST_PREREQS" | tr ' ' '/')"
have_prereqs && note "$HOST_PREREQS_LABEL already present" || note "$HOST_PREREQS_LABEL NOT present"
note "apt-get available for self-provisioning: $CAN_APT (mode=$APT_MODE: root/sudo/unhealthy/none)"

# What follows used to be one unconditional "real bootstrap -> #299 doctor ->
# init + build" block that ASSUMED a toolchain and scored a correct refusal on
# a bare host as five failures, while counting checks that never ran as part
# of "N passed". That is the exact defect class this repo keeps re-filing: a
# skip inflating the pass count. Two scenarios instead, each with its own
# tally and its own POSITIVE assertion of what SHOULD happen on that host
# shape; a scenario that cannot run says so and scores nothing.
PASS_A=0; FAIL_A=0
oka()  { PASS_A=$((PASS_A+1)); ok "$1"; }
bada() { FAIL_A=$((FAIL_A+1)); bad "$1"; }
PASS_B=0; FAIL_B=0
okb()  { PASS_B=$((PASS_B+1)); ok "$1"; }
badb() { FAIL_B=$((FAIL_B+1)); bad "$1"; }

# One bootstrap attempt, checked for a PRECISE refusal: non-zero exit, 0-byte
# stderr, envelope.exitCode/ok self-consistent with the process exit, and the
# named issue code's message naming every tool passed after it. A non-zero
# exit alone is not enough -- a wrong-but-non-zero exit must fail this.
check_bootstrap_refusal() {
  local label="$1" issue_code="$2"; shift 2
  local out="$WORK/$label.out" err="$WORK/$label.err"
  "$TAN" bootstrap --sdk-root ./alp-sdk --non-interactive --format json >"$out" 2>"$err"
  local rc=$?
  local esz; esz=$(wc -c <"$err" | tr -d ' ')
  note "$label: exit=$rc stderr=${esz}B"
  if [ "$rc" -eq 0 ]; then
    bada "$label: bootstrap unexpectedly exited 0"
    envelope_issues "$out"
    [ "$esz" -eq 0 ] || note "$(excerpt "$err")"
    return
  fi
  [ "$esz" -eq 0 ] && oka "$label: 0-byte stderr" || bada "$label: stderr not empty (${esz}B)"
  local verdict
  verdict=$(python3 - "$out" "$rc" "bootstrap.$issue_code" "$@" <<'PY'
import json, sys
path, rc, code = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tools = sys.argv[4:]
if not tools:
    # An empty tool list can never fail the `missing` check below (nothing to
    # find absent from the message), which made this helper a cannot-fail
    # assertion whenever "$@" is empty. Unreachable from bsA1 today, but a
    # loaded gun in a file whose entire history is this bug class.
    print("NOTOOLS"); raise SystemExit(1)
try:
    d = json.load(open(path))
except Exception as e:
    print("PARSEFAIL:" + str(e)); raise SystemExit
if d.get("exitCode") != rc:
    print(f"EXITMISMATCH:{d.get('exitCode')}"); raise SystemExit
if d.get("ok") is not False:
    print(f"OKMISMATCH:{d.get('ok')}"); raise SystemExit
matches = [i for i in (d.get("issues") or []) if i.get("code") == code]
if not matches:
    print("NOCODE"); raise SystemExit
msg = matches[0].get("message", "")
missing = [t for t in tools if t not in msg]
print("OK" if not missing else "MISSING:" + ",".join(missing))
PY
)
  if [ "$verdict" = "OK" ]; then
    oka "$label: envelope carries bootstrap.$issue_code naming $*"
  else
    case "$verdict" in
      PARSEFAIL:*)    bada "$label: stdout is not a single JSON envelope" ;;
      EXITMISMATCH:*) bada "$label: envelope.exitCode=${verdict#EXITMISMATCH:} but process exited $rc" ;;
      OKMISMATCH:*)   bada "$label: exit $rc but envelope ok=${verdict#OKMISMATCH:}" ;;
      NOCODE)         bada "$label: envelope missing issue bootstrap.$issue_code" ;;
      MISSING:*)      bada "$label: bootstrap.$issue_code message did not name: ${verdict#MISSING:}" ;;
      NOTOOLS)        bada "$label: check_bootstrap_refusal called with an empty tool list -- cannot verify anything" ;;
      *)              bada "$label: unrecognised verdict '$verdict'" ;;
    esac
    envelope_issues "$out"
    # A frozen tan spilling a stack trace on this path (never expected, since
    # this leg only exercises envelope-shape defects) would otherwise report
    # as a bare byte count -- tan-cli#758 review. `esz` is already known from
    # the stderr check above.
    [ "$esz" -eq 0 ] || note "$(excerpt "$err")"
  fi
}

# bsA2's ONE bootstrap attempt after the bare-host tool set is installed can
# CORRECTLY land on either of two refusals, not one (tan-cli#757). Once
# cmake/ninja/xz/wget are present, `tan bootstrap` next probes the resolved
# Python against the EFFECTIVE floor (the higher of alp-sdk's manifest and
# Zephyr's own `PYTHON_MINIMUM_REQUIRED`, `python/tan/commands/bootstrap_cmd.py`
# `resolve_python_floor`) BEFORE it ever gets to the venv check:
#
#   * below the floor              -> bootstrap.python-too-old (tool-less)
#   * clears the floor, no venv    -> bootstrap.venv-unusable (names python3-venv)
#
# Which one is correct is a property of the HOST's system Python, not of tan --
# measured identical on ubuntu:22.04, debian:12 and debian:11 (all below the
# 3.12 effective floor) against a tan build already proven correct there. A
# single hardcoded expectation can therefore only ever pass on a host at or
# above the floor (in practice, only ubuntu:24.04), and the old bsA2 assertion
# did exactly that -- reddening every Debian-family host below it for a
# refusal tan was RIGHT to make.
#
# Accepting "either" does not mean accepting "anything": each branch still
# demands its own positive evidence -- venv-unusable must name python3-venv
# (unchanged from before), and python-too-old must carry the floor-comparison
# wording the refusal always emits ("needs >="), not just a bare code with no
# message. A bootstrap that exits non-zero for some THIRD, unrelated reason
# (env/exit/ok mismatch, or neither code present) still fails this exactly as
# it did before.
check_bootstrap_python_gate() {
  local label="$1"
  local out="$WORK/$label.out" err="$WORK/$label.err"
  "$TAN" bootstrap --sdk-root ./alp-sdk --non-interactive --format json >"$out" 2>"$err"
  local rc=$?
  local esz; esz=$(wc -c <"$err" | tr -d ' ')
  note "$label: exit=$rc stderr=${esz}B"
  if [ "$rc" -eq 0 ]; then
    bada "$label: bootstrap unexpectedly exited 0"
    envelope_issues "$out"
    [ "$esz" -eq 0 ] || note "$(excerpt "$err")"
    return
  fi
  [ "$esz" -eq 0 ] && oka "$label: 0-byte stderr" || bada "$label: stderr not empty (${esz}B)"
  local verdict
  verdict=$(python3 - "$out" "$rc" <<'PY'
import json, sys
path, rc = sys.argv[1], int(sys.argv[2])
try:
    d = json.load(open(path))
except Exception as e:
    print("PARSEFAIL:" + str(e)); raise SystemExit
if d.get("exitCode") != rc:
    print(f"EXITMISMATCH:{d.get('exitCode')}"); raise SystemExit
if d.get("ok") is not False:
    print(f"OKMISMATCH:{d.get('ok')}"); raise SystemExit
issues = d.get("issues") or []
venv = next((i for i in issues if i.get("code") == "bootstrap.venv-unusable"), None)
old = next((i for i in issues if i.get("code") == "bootstrap.python-too-old"), None)
if venv is not None:
    msg = venv.get("message", "")
    print("OK:venv-unusable" if "python3-venv" in msg else "MISSING:venv-unusable:python3-venv")
elif old is not None:
    msg = old.get("message", "")
    print("OK:python-too-old" if "needs >=" in msg else "MISSING:python-too-old:floor-wording")
else:
    print("NOCODE")
PY
)
  case "$verdict" in
    OK:*)
      oka "$label: envelope carries bootstrap.${verdict#OK:} (the refusal this host's Python actually earns)"
      return
      ;;
    PARSEFAIL:*)    bada "$label: stdout is not a single JSON envelope" ;;
    EXITMISMATCH:*) bada "$label: envelope.exitCode=${verdict#EXITMISMATCH:} but process exited $rc" ;;
    OKMISMATCH:*)   bada "$label: exit $rc but envelope ok=${verdict#OKMISMATCH:}" ;;
    NOCODE)         bada "$label: envelope carries neither bootstrap.venv-unusable nor bootstrap.python-too-old" ;;
    MISSING:*)      bada "$label: $verdict" ;;
    *)              bada "$label: unrecognised verdict '$verdict'" ;;
  esac
  envelope_issues "$out"
  # A frozen tan spilling a stack trace on this path would otherwise report
  # as a bare byte count -- tan-cli#758 review.
  [ "$esz" -eq 0 ] || note "$(excerpt "$err")"
}

########################  SCENARIO A: bare host refuses precisely  ########################
echo; echo "############ SCENARIO A: bare host (no build toolchain) ############"
if have_prereqs; then
  note "A: not applicable -- host already has $HOST_PREREQS_LABEL, nothing to refuse"
else
  # The tools asserted here MUST be the ones ACTUALLY MISSING, not the whole
  # $HOST_PREREQS set: tan's refusal message names only what
  # `check_prerequisites` found absent (bootstrap_cmd.py:473-478), never a
  # tool it found present. A host that REACHES this branch necessarily has
  # `git` (the clone above would have ABORTed otherwise) and `python3`/`python`
  # (this very script's `jget` and the verdict check below need it), so tan
  # can never name either -- asserting the full host set demanded exactly that
  # and failed 100% of bare-POSIX-host runs for a reason unrelated to any gap.
  MISSING_PREREQS=$(missing_prereqs)
  check_bootstrap_refusal bsA1 prerequisites-missing $MISSING_PREREQS
  if [ "$CAN_APT" -eq 1 ]; then
    note "A: installing cmake ninja-build xz-utils wget via '$APT' (mode=$APT_MODE; missing: $MISSING_PREREQS)"
    # shellcheck disable=SC2086  # $APT is deliberately word-split (may be "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get")
    $APT install -y -qq --no-install-recommends cmake ninja-build xz-utils wget \
      >"$WORK/apt-a.log" 2>&1
    # Either bootstrap.venv-unusable or bootstrap.python-too-old is a correct
    # refusal here -- see check_bootstrap_python_gate's own comment (tan-cli#757).
    check_bootstrap_python_gate bsA2
  else
    note "A: no apt-get privilege in this environment (mode=$APT_MODE) -- cannot install the"
    note "   missing tools to progress past prerequisites-missing; that half of Scenario A"
    note "   is NOT RUN (not scored as a pass)."
  fi
fi
echo "--- scenario A: $PASS_A passed, $FAIL_A failed ---"

########################  SCENARIO B: provisioned host, real ARM ELF  ########################
echo; echo "############ SCENARIO B: provisioned host ############"
HAVE_PROJECT=0
SDK_OK=0
RUN_SDK=no
if ! have_prereqs && [ "$CAN_APT" -ne 1 ]; then
  note "B: NOT RUN -- host lacks $HOST_PREREQS_LABEL and cannot self-provision (apt-get privilege: $APT_MODE)"
else
  if [ "$CAN_APT" -eq 1 ]; then
    # `file` is NOT one tan names, and it is required anyway: the Zephyr SDK's
    # host-tools installer (`setup.sh -t <target> -h`, which `west sdk install`
    # invokes) needs file(1). Isolated to that single variable in a container --
    # identical bootstrapped workspace, identical HOME, `file` the only
    # difference:
    #
    #   WITH    file -> west rc=0, "All done."
    #   WITHOUT file -> west rc=1, "ERROR: Host tools installation failed"
    #                             "FATAL ERROR: command
    #                              `<sdk>/setup.sh -t arm-zephyr-eabi -h` failed"
    #
    # Installed here so scenario B can reach a real ARM ELF. That tan's own
    # prerequisite list omits it -- and that the SDK's failure never names it --
    # is a separate customer-facing gap, filed as tan-cli#424.
    note "B: installing cmake ninja-build xz-utils wget python3-venv file via '$APT' (mode=$APT_MODE)"
    # shellcheck disable=SC2086  # $APT is deliberately word-split (may be "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get")
    $APT install -y -qq --no-install-recommends \
      cmake ninja-build xz-utils wget python3-venv file >"$WORK/apt-b.log" 2>&1
  fi

  # tan-cli#1189: the pair that decides whether tan's tan-cli#992
  # `LD_LIBRARY_PATH` restore can fire at all on this host. `tan` is a
  # PyInstaller ONEDIR freeze here, and its bootloader points
  # `LD_LIBRARY_PATH` at the bundled `_internal/` before running -- stashing
  # the caller's value in `LD_LIBRARY_PATH_ORIG` so children can be given it
  # back. `subprocess_env.restore_ld_library_path` is a documented NO-OP when
  # `LD_LIBRARY_PATH_ORIG` is absent, on the reading that an absent marker
  # means "this host never had the problem".
  #
  # That reading holds only if the bootloader records the marker
  # unconditionally. If it records it ONLY when the caller already had an
  # `LD_LIBRARY_PATH`, a bare container -- which has none -- gets the bundled
  # path with nothing to undo it, and every hosted runner is immune. So this is
  # the PRE-LAUNCH value, deliberately: what the bootloader had to work with.
  # An `<unset>` on the first line is the whole finding.
  #
  # Not an assertion. The harness must not fail on a fact it is here to
  # MEASURE (tan-cli#1186's own "measure it before you fix it"), and the
  # failure this informs is already scored by the bootstrap check below.
  note "host LD_LIBRARY_PATH before tan (pre-launch): ${LD_LIBRARY_PATH:-<unset>}"
  note "host LD_LIBRARY_PATH_ORIG before tan (pre-launch): ${LD_LIBRARY_PATH_ORIG:-<unset>}"

  hdr "B: bootstrap succeeds on a provisioned host"
  T0=$(date +%s)
  "$TAN" bootstrap --sdk-root ./alp-sdk --non-interactive --format json \
    >"$WORK/bsB.out" 2>"$WORK/bsB.err"; RC=$?
  T1=$(date +%s)
  ESZ=$(wc -c <"$WORK/bsB.err" | tr -d ' ')
  OKVAL=$(jget "$WORK/bsB.out" ok)
  note "bootstrap: exit=$RC ok=$OKVAL stderr=${ESZ}B took $((T1-T0))s"
  # tan-cli#1169: WHICH quota the toolchain phase just spent, printed as a
  # fact rather than left to be inferred. A 403 on the anonymous per-IP quota
  # breaks all three assertions below at once -- exit, `ok`, and 0-byte
  # stderr -- so without this line the rate limit presents as three
  # unrelated-looking failures rather than one cause, which is a large part of
  # why it went unfixed.
  #
  # tan itself gives the reader NOTHING here. Its `Authenticating the Zephyr
  # SDK download with the token in $<var>` line is a `Log.line`, and
  # `Log.line` prints nothing at all under `--format json`
  # (`bootstrap_cmd.py:265-268`), while the envelope is deliberately
  # byte-identical with and without a token. So the harness has to say it.
  #
  # The NAME of the variable, never the value -- the rule tan's own messages
  # follow. The verdict is sound because a token that is present and does NOT
  # end up authenticating the download always leaves a
  # `bootstrap.sdk-credential-*` issue on the envelope: `-unstaged` when it
  # was refused or could not be written into the private netrc, `-unverified`
  # when `west sdk install` no longer matches the shape that netrc route
  # depends on (tan-cli#1148, tan-cli#1154). Absence of both, with the
  # variable set AND the envelope actually read, is the staged-and-passed
  # path and nothing else -- the `PARSEFAIL:` branch below separates out the
  # case where it was not read, which is not evidence of anything. `${!v}` is
  # bash indirect expansion, which this harness already requires elsewhere.
  SDK_CRED_VAR=NONE
  for v in TAN_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN; do
    [ -n "${!v:-}" ] || continue
    SDK_CRED_VAR="$v"; break
  done
  #
  # `PARSEFAIL:` rather than a swallowed `d = {}` (tan-cli#1184 review). An
  # absent or unparseable `$WORK/bsB.out` -- `tan bootstrap` dying with a
  # traceback before it writes one, which is exactly what the `ok=NONE` on
  # the `note` line above is the symptom of -- would otherwise be
  # INDISTINGUISHABLE from an envelope carrying no credential issue, and the
  # branch below would print the positive "staged for west sdk install"
  # assurance from an absence of evidence. The sentinel shape is this file's
  # own, from `check_bootstrap_refusal` above. The `|| echo` fallback covers
  # a host with no `python3` at all: an empty capture is neither the codes
  # nor `NONE`, so it would reach the alarming branch below as a false alarm
  # with an empty code list.
  SDK_CRED_ISSUES=$(python3 - "$WORK/bsB.out" <<'PY' || echo "PARSEFAIL:python3 unavailable"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("PARSEFAIL:" + str(e)); raise SystemExit
codes = [str(i.get("code")) for i in (d.get("issues") or [])
         if str(i.get("code") or "").startswith("bootstrap.sdk-credential-")]
print(",".join(codes) if codes else "NONE")
PY
)
  if [ "$SDK_CRED_VAR" = NONE ]; then
    note "SDK download credential: none of TAN_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN is set -- the release listing went out on the anonymous per-IP quota"
  elif [ "${SDK_CRED_ISSUES#PARSEFAIL:}" != "$SDK_CRED_ISSUES" ]; then
    note "SDK download credential: \$$SDK_CRED_VAR was set, but the bootstrap envelope could not be read -- whether it was staged is UNKNOWN (${SDK_CRED_ISSUES})"
  elif [ "$SDK_CRED_ISSUES" = NONE ]; then
    note "SDK download credential: \$$SDK_CRED_VAR, staged for west sdk install (no bootstrap.sdk-credential-* issue on the envelope)"
  else
    note "SDK download credential: \$$SDK_CRED_VAR did NOT authenticate the download -- $SDK_CRED_ISSUES"
  fi
  # A host below the EFFECTIVE Python floor is refused CORRECTLY right here
  # (bootstrap.python-too-old) -- tan-cli#757. That is not a defect: measured
  # identical on ubuntu:22.04, debian:12 and debian:11 (all below the 3.12
  # floor) against a tan build already proven correct there, and the "no
  # west" assertion further down cannot hold once the floor legitimately
  # refuses first.
  #
  # The CODE alone is not enough evidence, though (tan-cli#757 review): a
  # regression in `resolve_python_floor` or the tuple comparison in
  # `python/tan/core/bootstrap.py` that wrongly refused a host which DOES
  # clear the floor would disable the "no west" assertion below and report
  # green -- the false-pass direction, the single most valuable regression
  # this harness could catch. So the refusal's own message is parsed for the
  # `found`/`floor` numbers it always carries verbatim ("Python 3.10 found;
  # the SDK tooling needs >= 3.12") and the skip is honoured ONLY when
  # `found < floor` actually holds -- nothing hand-ported from
  # `resolve_python_floor` itself, no coupling to its internals, just
  # checking tan's own arithmetic against tan's own claim. Still not
  # re-derived from a host python3 --version probe: re-deriving the floor
  # COMPUTATION (manifest vs Zephyr's PYTHON_MINIMUM_REQUIRED) in bash is the
  # hand-copied-tuple drift the HOST_PREREQS block above already had to fix
  # once; verifying the arithmetic in a message tan already printed is not
  # that.
  # WHICH of the three invariants below actually broke, and ONLY those
  # (tan-cli#1187, second defect). The failure line used to print all three
  # values unconditionally -- `exit 1 ok=False stderr=0B` -- in which
  # `stderr=0B` is the assertion PASSING. Three facts at equal weight, one of
  # them good news, reads as three unrelated problems rather than the one that
  # happened, and it is what made six days of an identical failure look like a
  # three-part mystery. Naming only the violations leaves the reader the real
  # count.
  B_WHY=""
  [ "$RC" -eq 0 ]       || B_WHY="exit $RC (wanted 0)"
  [ "$OKVAL" = "True" ] || B_WHY="${B_WHY:+$B_WHY; }envelope ok=$OKVAL (wanted True)"
  [ "$ESZ" -eq 0 ]      || B_WHY="${B_WHY:+$B_WHY; }stderr ${ESZ}B (wanted 0)"
  # Anti-vacuity: the `else` arm below is reached only when at least one of the
  # three failed, so an empty $B_WHY there means this block and that condition
  # have drifted apart -- say so rather than print `failed -- `.
  B_WHY="${B_WHY:-NO invariant violated; this failure branch and the condition guarding it have drifted apart}"
  B_FLOOR_REFUSAL=0
  if [ "$RC" -eq 0 ] && [ "$OKVAL" = "True" ] && [ "$ESZ" -eq 0 ]; then
    okb "B: bootstrap ok:true, exit 0, 0-byte stderr"
  else
    FLOOR_CHECK=$(python3 - "$WORK/bsB.out" <<'PY'
import json, re, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("ABSENT"); raise SystemExit
issue = next((i for i in (d.get("issues") or [])
              if i.get("code") == "bootstrap.python-too-old"), None)
if issue is None:
    print("ABSENT"); raise SystemExit
msg = issue.get("message", "")
m = re.search(r"Python (\d+)\.(\d+) found.*needs >= (\d+)\.(\d+)", msg)
if not m:
    print("NOPARSE"); raise SystemExit
found = (int(m.group(1)), int(m.group(2)))
floor = (int(m.group(3)), int(m.group(4)))
verdict = "EARNED" if found < floor else "UNEARNED"
print(f"{verdict} {found[0]}.{found[1]} {floor[0]}.{floor[1]}")
PY
)
    read -r FLOOR_VERDICT FOUND_VER EFFECTIVE_FLOOR <<<"$FLOOR_CHECK"
    case "$FLOOR_VERDICT" in
      EARNED)
        B_FLOOR_REFUSAL=1
        note "B: bootstrap correctly refused -- host Python $FOUND_VER is below the effective"
        note "   floor $EFFECTIVE_FLOOR (bootstrap.python-too-old, numbers verified against its own message)"
        ;;
      UNEARNED)
        badb "B: bootstrap refused bootstrap.python-too-old naming Python $FOUND_VER against floor $EFFECTIVE_FLOOR, but $FOUND_VER is NOT below $EFFECTIVE_FLOOR -- an unearned refusal"
        envelope_issues "$WORK/bsB.out"
        [ "$ESZ" -eq 0 ] || note "$(excerpt "$WORK/bsB.err")"
        ;;
      NOPARSE)
        badb "B: bootstrap carries bootstrap.python-too-old but its message could not be parsed for found/floor -- cannot verify the refusal was earned"
        envelope_issues "$WORK/bsB.out"
        [ "$ESZ" -eq 0 ] || note "$(excerpt "$WORK/bsB.err")"
        ;;
      *)
        # Not a floor refusal at all -- a bare crash trace lands here.
        badb "B: bootstrap failed -- $B_WHY"
        envelope_issues "$WORK/bsB.out"
        [ "$ESZ" -eq 0 ] || note "$(excerpt "$WORK/bsB.err")"
        ;;
    esac
  fi

  WS=$(jget "$WORK/bsB.out" data.workspaceDir); note "workspace=$WS"
  # The venv layout is per-platform: POSIX puts console scripts in `bin/`,
  # Windows in `Scripts/` with a `.exe` suffix. tan itself gets this right --
  # facts.venv_bin_dir(is_windows), bootstrap_cmd.py:649 -- but this check
  # hardcoded the POSIX shape, so on Windows it reported
  # `no west at <ws>/.venv/bin/west` while bootstrap had correctly produced
  # <ws>\.venv\Scripts\west.exe. Measured on MINGW64_NT-10.0-26200.
  if [ "$HOST_IS_WINDOWS" -eq 1 ]; then
    WEST_REL=".venv/Scripts/west.exe"
  else
    WEST_REL=".venv/bin/west"
  fi
  WEST_BIN="$WS/$WEST_REL"
  # ONLY this assertion is conditional on B_FLOOR_REFUSAL -- it is the sole
  # line a legitimate floor refusal turns into a false failure (tan-cli#757
  # review). init/build/flash/#336 below need no bootstrapped workspace at
  # all and keep running and scoring normally; scoping the skip any wider
  # than this deletes real, passing assertions (six of them, per the review's
  # own #757-table replay) for no reason tied to the actual defect.
  if [ "$WS" != "NONE" ] && [ -x "$WEST_BIN" ]; then
    okb "B: west exists at \$workspaceDir/$WEST_REL"
  elif [ "$B_FLOOR_REFUSAL" -eq 1 ]; then
    note "B: no west -- bootstrap correctly refused before a workspace existed (bootstrap.python-too-old); not scored"
  else
    badb "B: no west at $WEST_BIN"
  fi

  if [ "$RC" -eq 0 ]; then
    hdr "B: #299 doctor AFTER a successful bootstrap: west must not be the reason it is unhappy"
    # jrun already prints+scores the PASS/FAIL line via the global ok()/bad();
    # only the scenario tally is added here, not a second assertion.
    if jrun doc3 any doctor --format json; then PASS_B=$((PASS_B+1)); else FAIL_B=$((FAIL_B+1)); fi
    WESTBAD=$(python3 - "$WORK/doc3.out" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
bad=[c["name"] for c in d["data"]["checks"]
     if c["name"].lower().startswith("west") and c["status"]=="fail"]
print(",".join(bad) if bad else "NONE")
PY
)
    [ "$WESTBAD" = "NONE" ] && okb "B: #299 no west* check fails after bootstrap" \
                            || badb "B: #299 west check(s) failing after a successful bootstrap: $WESTBAD"
  else
    note "B: #299 skipped -- bootstrap did not succeed, nothing to check it against"
  fi

  hdr "B: init"
  "$TAN" init --from-example peripheral-io/hello-world --name blinky-e2e --destination . \
    --format json >"$WORK/initB.out" 2>"$WORK/initB.err"; RC=$?
  ESZ=$(wc -c <"$WORK/initB.err" | tr -d ' ')
  if [ "$RC" -eq 0 ] && [ "$ESZ" -eq 0 ]; then
    okb "B: init exit 0, 0-byte stderr"; HAVE_PROJECT=1
  else
    badb "B: init failed (exit $RC, stderr ${ESZ}B)"
    envelope_issues "$WORK/initB.out"
    [ "$ESZ" -eq 0 ] || note "$(excerpt "$WORK/initB.err")"
  fi

  hdr "B: Zephyr SDK (west sdk install --version $ZEPHYR_SDK_VERSION -t arm-zephyr-eabi)"
  if [ "$HAVE_PROJECT" -eq 1 ] && [ -x "$WEST_BIN" ]; then
    "$TAN" doctor --build --format json >"$WORK/doctorPre.out" 2>/dev/null
    # tan-cli#1186 widened `tan doctor`'s zephyrSdk/toolchain checks
    # (`python/tan/commands/doctor_cmd.py`) to also see the ADR 0021
    # artifact-keyed store (`~/.alp/toolchains/...`, or `$ALP_TOOLCHAIN_ROOT`)
    # `tan bootstrap`'s own toolchain phase fills -- correctly: a real,
    # verified `arm-zephyr-eabi` compiler genuinely does live there once
    # bootstrap has run. That doctor-side fix stays.
    #
    # tan-cli#1206 review: trusting that verdict HERE, to skip this step's
    # OWN `west sdk install`, does not follow from it. `tan doctor` answers
    # "does a verified toolchain exist on this host"; this step needs "will
    # the `west build`/CMake configure the next step runs actually locate
    # it" -- and for a toolchain that lives only in tan's own store those are
    # different questions. Measured on a pristine `ubuntu:24.04` (PR #1206,
    # job run 33969119991): right after `tan bootstrap` acquired one into
    # that store, `tan doctor --build` reported `zephyrSdk=pass
    # toolchain=pass`; the (now-removed) guard skipped this step's install on
    # that verdict; the very next `tan build` then failed cmake's configure
    # at a `find_package` call ("Configuring incomplete, errors occurred!").
    # UPDATE (tan-cli#1209): at the time of the #1206 measurement above,
    # `tan build` never exported `ZEPHYR_SDK_INSTALL_DIR` for a toolchain
    # living only in that store -- the exact gap `build.toolchain`'s own
    # docstring named for `${TOOLCHAIN_ROOT}` substitution ("a customer who
    # ran nothing but tan bootstrap still could not get it to resolve ...
    # without ALSO hand-exporting ZEPHYR_SDK_INSTALL_DIR"). #1209 closes
    # that gap: `execute_slices` now resolves `build.toolchain.
    # verified_store_dir(sdk_root)` once per run and fills
    # `ZEPHYR_SDK_INSTALL_DIR` into every spawned west/CMake child from it
    # (tan-cli#1209 review MINOR: only when no OTHER scan-visible SDK
    # already lives on the host, and only when no inherited
    # `ZEPHYR_SDK_INSTALL_DIR` names a path that still exists -- see
    # `zephyr_env.py`/`build/toolchain.py`'s own docstrings). So the
    # PREMISE that made this step's own install unconditional -- "a
    # toolchain doctor correctly sees present is not, on its own, proof
    # this step can skip its own verification install" -- no longer holds
    # for the specific failure #1206 measured.
    #
    # This step still does NOT skip its own install, on purpose, for a
    # narrower reason than the one above: this harness's OWN "B: build"
    # scoring below (`SDK_OK`/`RUN_SDK`/`HOST_ZSDK`) is built around this
    # step genuinely attempting (or refusing) its own `west sdk install`,
    # and flipping the guard here would also have to thread a "doctor
    # already verified it" outcome through that bash bookkeeping -- a
    # harness-only change with no pytest coverage in this repo, and #1206
    # is the recorded lesson for shipping exactly that shape unverified
    # against a real clean container. Left as a named follow-up on #1209
    # rather than repeated here: flip this guard, retire the now-redundant
    # PRE_DETAIL commentary below it, and re-thread SDK_OK/RUN_SDK/HOST_ZSDK
    # accordingly -- proven the same way #1206 was, against a real
    # clean-container run, not by reading this diff. `doctorPre.out` stays
    # captured, and its verdict printed, purely so a reader of this log can
    # see what doctor reported at this point in the run.
    PRE_DETAIL=$(python3 - "$WORK/doctorPre.out" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("zephyrSdk=? toolchain=?"); raise SystemExit
checks = (d.get("data") or {}).get("checks") or []
statuses = {c.get("name"): c.get("status") for c in checks}
print(f"zephyrSdk={statuses.get('zephyrSdk')} toolchain={statuses.get('toolchain')}")
PY
)
    note "B: doctor reports $PRE_DETAIL -- re-verifying with this step's own install regardless"
    # tan-cli#1169: this one stays ANONYMOUS, deliberately, and the token
    # forwarded into the container for `tan bootstrap` above does not reach
    # it. `west sdk install` takes a credential from
    # `--personal-access-token` and from NOTHING else -- it reads no
    # environment variable of its own (measured at Zephyr v4.4.1,
    # `scripts/west_commands/sdk.py:473`; `grep environ` over that file
    # finds only ZEPHYR_BASE and ZEPHYR_SDK_INSTALL_DIR, recorded as
    # `toolchain_provision.WEST_SDK_KNOWN_ENV_READS`). So "the environment
    # now carries a token" covers `tan bootstrap` and covers nothing here.
    #
    # Three reasons not to close that gap in this change:
    #
    #  1. The flag is the one shape tan-cli#1143 exists to forbid. It would
    #     put the secret in this process's argv -- readable in the process
    #     table of whatever box this runs on, and this harness runs on
    #     developer machines (Windows Git Bash and WSL) as well as in an
    #     ephemeral `--rm` container.
    #  2. The netrc route tan uses instead is not a one-liner: a 0600 file
    #     in a 0700 scratch directory, discarded in a `finally` on every
    #     exit path, plus a sweep for a previous crash's leftovers. Hand-
    #     rolling that in bash is a second place a secret touches disk, with
    #     none of those guarantees.
    #  3. This step exists to run the command `tan doctor` PRINTS, verbatim,
    #     the way a customer would (`zephyr_sdk_install_command`,
    #     `doctor_cmd.py:1158`). A customer on a home IP has their own
    #     unauthenticated quota; authenticating it here would stop measuring
    #     the shape under test. `getting-started.yml`'s own manual
    #     `west sdk install` makes the opposite trade and says so beside the
    #     flag -- that step is a retry loop whose subject is the toolchain
    #     arriving, not the customer's command.
    #
    # What that leaves standing, named rather than glossed: this download can
    # still 403 on the shared per-IP quota, and -- now that this step is
    # unconditional again -- it runs on EVERY host, including one where
    # `tan bootstrap` already left a verified toolchain in its own store,
    # which is the ~1.9 GiB redundant-download cost tan-cli#1186 set out to
    # remove. Reintroduced here on purpose (tan-cli#1206 review): the guard
    # that removed it could not tell "doctor sees a toolchain" apart from
    # "this step's own build can use one", and shipping a green harness that
    # asserts the wrong thing is worse than a known, named inefficiency.
    SDK_TIMEOUT="${ZEPHYR_SDK_INSTALL_TIMEOUT:-1200}"
    T0=$(date +%s)
    if ( cd "$WS" && timeout "$SDK_TIMEOUT" "$WEST_BIN" sdk install \
           --version "$ZEPHYR_SDK_VERSION" -t arm-zephyr-eabi \
           >"$WORK/sdkinstall.out" 2>"$WORK/sdkinstall.err" ); then
      SDK_OK=1
    fi
    T1=$(date +%s)
    note "west sdk install: $((T1-T0))s (timeout ${SDK_TIMEOUT}s), exit-ok=$SDK_OK"
    [ "$SDK_OK" -eq 1 ] || note "$(excerpt "$WORK/sdkinstall.err")"
    # `west sdk install` can extract a full, usable SDK into the sandbox
    # $HOME and THEN fail at a later step -- missing file(1) (the host-tools
    # gap documented above) or the `timeout "$SDK_TIMEOUT"` mid-extract --
    # leaving SDK_OK=0 even though a real, buildable SDK is sitting right
    # there. Recorded as ITS OWN fact: distinct from "a pre-existing host
    # SDK" (HOST_ZSDK, scanned from $REAL_HOME below, outside the sandbox)
    # and from "genuinely no SDK" (neither is true). The SANDBOXED $HOME is
    # the right place to look HERE -- this asks "did THIS run's own install
    # leave something usable", the opposite of what HOST_ZSDK asks.
    #
    # `_sdk_has_toolchain` (defined near the top of this script), not a bare
    # `sdk_version` check: a `timeout` mid-install is exactly the shape that
    # leaves `sdk_version` at the root with no toolchain under it.
    #
    # Pinned to $ZEPHYR_SDK_VERSION, not a `zephyr-sdk-*` glob: THIS run's
    # own install just above always requested exactly that version, so the
    # sandboxed $HOME can never legitimately hold any other -- pinning says
    # so directly instead of matching-then-hoping. Contrast with HOST_ZSDK
    # below, which stays deliberately version-agnostic for a different
    # reason (see its own comment): RUN_SDK only ever asks "did the install
    # THIS RUN JUST RAN leave something usable", never "does any SDK exist".
    if _sdk_has_toolchain "$HOME/zephyr-sdk-$ZEPHYR_SDK_VERSION"; then
      RUN_SDK=yes
    fi
  else
    note "B: no west / no project -- cannot attempt the SDK install"
  fi

  hdr "B: build"
  if [ "$HAVE_PROJECT" -ne 1 ]; then
    badb "B: build not attempted -- init did not produce a project"
  else
    "$TAN" build --project blinky-e2e --format json >"$WORK/buildB.out" 2>"$WORK/buildB.err"; RC=$?
    BESZ=$(wc -c <"$WORK/buildB.err" | tr -d ' ')

    # Hoisted OUT of the arms below and run unconditionally whenever RC=0, so
    # no arm here -- present or future -- can return a build "success"
    # without the artefact itself being checked and scored. Before this, the
    # HOST_ZSDK arm further down was the exact unfixed twin of the RUN_SDK
    # arm: tested FIRST among the SDK_OK=0 arms, `SDK_OK=0, HOST_ZSDK=yes,
    # RC=0` scored nothing and never looked at the artefact at all. A
    # Zephyr-slice-skipped PARTIAL build still exits 0
    # (build_cmd.py:1097-1102 refuses only when NO slice succeeded), so
    # "exit 0" alone is never proof of a real ELF on ANY arm.
    ELF=""
    ELF_FOUND=no
    ELF_IS_ARM=no
    if [ "$RC" -eq 0 ]; then
      ELF=$(find . -name "zephyr.elf" 2>/dev/null | head -1)
      if [ -n "$ELF" ]; then
        ELF_FOUND=yes
        if command -v file >/dev/null 2>&1; then
          # `file -b`: the BARE description, no leading filename. Without `-b`
          # the match below is unanchored against `file`'s path-prefixed
          # output, so path text alone can satisfy it --
          # `./self-test/arm-zephyr-eabi/build/zephyr.elf` would score "a real
          # ARM ELF" even while holding a genuine x86-64 binary.
          DESC=$(file -b "$ELF" 2>/dev/null)
          note "$ELF: $DESC"
          if echo "$DESC" | grep -qi "ELF.*ARM"; then
            okb "B: build produced a real ARM ELF"; ELF_IS_ARM=yes
          else
            badb "B: artefact is not an ARM ELF"
          fi
        else
          # file(1) absent is a HOST gap, not an artefact defect -- reachable
          # on a non-Debian/minimal POSIX host (CAN_APT=0, since the apt
          # install above only runs when CAN_APT gates it, tan-cli#759) that
          # still carries a working toolchain of its own. The old code
          # fed "file(1) unavailable" into the same `grep -qi "ELF.*ARM"`,
          # which can never match, scoring a genuine ARM ELF as
          # `badb "artefact is not an ARM ELF"`.
          note "$ELF: exists, but file(1) is unavailable -- artefact type not checked"
        fi
      else
        badb "B: build exited 0 but no zephyr.elf was produced"
      fi
    fi

    if [ "$SDK_OK" -eq 1 ]; then
      [ "$RC" -eq 0 ] && okb "B: build exit 0" || {
        badb "B: build exit $RC"
        envelope_issues "$WORK/buildB.out"
        [ "$BESZ" -eq 0 ] || note "$(excerpt "$WORK/buildB.err")"
      }
    else
      # SDK install failed or was impractical (measured above, not assumed):
      # the build is EXPECTED to fail, and must name the missing Zephyr SDK --
      # an unnamed failure, or a build that unexpectedly succeeds anyway, is
      # itself a finding.
      #
      # UNLESS the host already carries a Zephyr SDK of its own. This harness
      # not attempting an install does NOT mean the host has none: a developer
      # box commonly has ~/zephyr-sdk-<ver> from unrelated work (measured:
      # /c/Users/<user>/zephyr-sdk-1.0.1 on the Windows box where this fired),
      # and `tan build` finding it and succeeding is CORRECT, not a defect.
      # Scoring that as "build unexpectedly succeeded" is the harness asserting
      # its own assumption over the observed host.
      #
      # The candidate set is REAL_HOME (captured at the very top of the script,
      # BEFORE `export HOME=` sandboxed it) plus system-wide install dirs --
      # NEVER the sandboxed $HOME. A `west sdk install` that times out mid-
      # extract (the `timeout "$SDK_TIMEOUT"` above) leaves a PARTIAL
      # `$HOME/zephyr-sdk-<ver>/` behind as THIS run's own debris; scanning the
      # sandbox would treat that empty directory as "pre-existing" and
      # permanently disarm the very assertion below for the rest of the run.
      # `_sdk_has_toolchain`, not a bare `-f sdk_version`, matching the
      # top-of-script search -- a directory (or file) alone proves nothing, a
      # half-extracted SDK has one too.
      #
      # Deliberately version-AGNOSTIC (`zephyr-sdk-*`, not
      # `zephyr-sdk-$ZEPHYR_SDK_VERSION`): unlike RUN_SDK above, this asks
      # "does the REAL host already carry a usable Zephyr SDK OF ITS OWN",
      # which is exactly as legitimate an explanation for a successful build
      # at ANY version as at this run's configured one -- a dev box's SDK
      # from unrelated prior work does not stop being real because it is
      # 0.16.8 and $ZEPHYR_SDK_VERSION defaults to 1.0.1. Pinning this scan
      # would turn a genuinely-working, differently-versioned host SDK into a
      # false "unexpectedly succeeded" failure below -- worse for a
      # regression harness than under-filtering here.
      HOST_ZSDK=no
      for _z in "$REAL_HOME"/zephyr-sdk-* /opt/zephyr-sdk-* /usr/local/zephyr-sdk-* "${ZEPHYR_SDK_INSTALL_DIR:-}"; do
        [ -n "$_z" ] && _sdk_has_toolchain "$_z" && { HOST_ZSDK=yes; break; }
      done
      # `west sdk install` also registers via CMake's user package registry,
      # and on Windows that channel IS the registry -- HKCU\Software\Kitware\
      # CMake\Packages\Zephyr-sdk -- not a dot-file the $HOME redirect could
      # neutralise. A build that resolves an SDK through THAT channel is just
      # as legitimate as one found by path, so it has to be probed too before
      # concluding "no host SDK". `reg` is absent on POSIX and the key is
      # absent on a Windows host that never ran `west sdk install`; both are
      # tolerated, not treated as a script error.
      if [ "$HOST_ZSDK" = no ] && command -v reg >/dev/null 2>&1; then
        while IFS= read -r _z; do
          _z=$(printf '%s' "$_z" | tr -d '\r')
          # The registry records the directory holding Zephyr-sdkConfig.cmake,
          # i.e. <sdk>/cmake -- NOT <sdk> itself. Measured on a real box: the
          # value ends in /cmake, and <sdk>/cmake/sdk_version does not exist
          # while <sdk>/sdk_version does -- but a value that already points at
          # the SDK root must not be forced through dirname too, or it
          # resolves to the root's PARENT and this channel never binds a real
          # SDK. Test both the raw value and its dirname; guard on the RAW
          # value ($_z) being non-empty -- dirname "" prints "." (measured), so
          # a blank reg-query line would otherwise probe ./sdk_version relative
          # to $WORK/proj instead of being skipped, since dirname can never
          # itself leave $_zdir empty.
          _zdir=$(dirname "$_z" 2>/dev/null)
          if [ -n "$_z" ] && { _sdk_has_toolchain "$_z" || _sdk_has_toolchain "$_zdir"; }; then
            HOST_ZSDK=yes; break
          fi
        done < <(reg query "HKCU\Software\Kitware\CMake\Packages\Zephyr-sdk" 2>/dev/null \
                    | grep REG_SZ | sed -E 's/^.*REG_SZ[[:space:]]+//')
      fi
      if [ "$RC" -eq 0 ] && [ "$HOST_ZSDK" = yes ]; then
        note "B: build succeeded using a Zephyr SDK already present on this host"
        note "   (not installed by this run) -- the no-SDK refusal path is NOT"
        note "   exercised here; the artefact check above already scored the ELF."
      elif [ "$RC" -eq 0 ] && [ "$RUN_SDK" = yes ]; then
        note "B: build succeeded using the SDK THIS RUN's own west install left"
        note "   in place, even though the install itself did not finish clean"
        note "   (host-tools step / timeout) -- the no-SDK refusal path is NOT"
        note "   exercised here; the artefact check above already scored the ELF."
      elif [ "$RC" -eq 0 ] && [ "$ELF_IS_ARM" = yes ]; then
        # Neither scan above found an SDK, but the hoisted block already
        # confirmed a real ARM ELF -- that IS positive evidence a toolchain
        # was found (e.g. an inherited ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb +
        # GNUARMEMB_TOOLCHAIN_PATH, or a toolchain living only in the ADR
        # 0021 artifact-keyed store under $HOME/.alp/toolchains -- unlike the
        # Scenario-B `PRE` guard above, tan-cli#1186, neither scan in THIS
        # section (the top-of-script discovery or HOST_ZSDK) knows about
        # that store), not a contradiction of it. Deferring to the artefact is
        # what keeps this from scoring BOTH "produced a real ARM ELF" AND
        # "unexpectedly succeeded with no Zephyr SDK" for the same build.
        note "B: build succeeded and produced a real ARM ELF (already scored above)"
        note "   despite no Zephyr SDK found by this harness's scans -- the ELF"
        note "   itself is the stronger evidence; not scored as unexpected."
      elif [ "$RC" -eq 0 ] && [ "$ELF_FOUND" = no ]; then
        # Already scored above as "build exited 0 but no zephyr.elf was
        # produced" -- a partial build where every Zephyr slice was skipped
        # but some other slice succeeded (exit 0 either way). Scoring
        # "unexpectedly succeeded with no Zephyr SDK" too would double-file
        # the same underlying defect (no SDK -> no Zephyr artefact).
        note "B: no ARM ELF and no Zephyr SDK found -- already scored above, not double-scored here"
      elif [ "$RC" -eq 0 ]; then
        badb "B: build unexpectedly succeeded with no Zephyr SDK installed"
      else
        NAMED_BUILD=no
        grep -qi "zephyr-sdk\|zephyr sdk" "$WORK/buildB.out" "$WORK/buildB.err" 2>/dev/null && NAMED_BUILD=yes
        "$TAN" doctor --build --format json >"$WORK/doctorB.out" 2>/dev/null
        NAMED_DOCTOR=$(python3 - "$WORK/doctorB.out" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("no"); raise SystemExit
z=[c for c in (d.get("data") or {}).get("checks") or [] if c.get("name")=="zephyrSdk"]
ok = bool(z) and z[0].get("status")=="fail" and "west sdk install" in (z[0].get("fix") or "")
print("yes" if ok else "no")
PY
)
        if [ "$NAMED_BUILD" = "yes" ] || [ "$NAMED_DOCTOR" = "yes" ]; then
          okb "B: build failed (exit $RC) and the Zephyr SDK is named as the cause (build:$NAMED_BUILD doctor:$NAMED_DOCTOR)"
        else
          badb "B: build failed (exit $RC) but nothing names the missing Zephyr SDK (build:$NAMED_BUILD doctor:$NAMED_DOCTOR)"
          [ "$BESZ" -eq 0 ] || note "$(excerpt "$WORK/buildB.err")"
        fi
        note "NO ARM ELF produced -- no Zephyr SDK toolchain in this container"
        note "(tan-cli#419: the build envelope itself doesn't name the gap; 'tan doctor --build' does -- not this task's fix)"
      fi
    fi
  fi

  hdr "B: flash --dry-run"
  if [ "$HAVE_PROJECT" -ne 1 ]; then
    note "B: flash --dry-run NOT RUN -- no project (init did not succeed)"
  else
    # tan-cli#358: this ran from the PARENT directory with no `--project`, so it
    # planned against `$WORK/proj` -- which has no board.yaml -- and returned
    # ok:false / exitCode 1 / `flash.manifest-not-found`. The old jrun scored
    # that PASS. It has to target the project that was actually built, or it
    # measures nothing about flash.
    # Same as the #299 doc3 call above: jrun already prints+scores the line.
    if jrun flash any flash --project blinky-e2e --dry-run --format json
    then PASS_B=$((PASS_B+1)); else FAIL_B=$((FAIL_B+1)); fi
    FP=$(jget "$WORK/flash.out" project.root); FB=$(jget "$WORK/flash.out" project.boardYaml)
    case "$FP" in
      */blinky-e2e) okb "B: flash planned against the project that was built ($FP)" ;;
      *)            badb "B: flash planned against '$FP', not the built blinky-e2e" ;;
    esac
    [ "$FB" != "NONE" ] && okb "B: flash resolved the project's board.yaml" \
      || badb "B: flash resolved no board.yaml -- it is not looking at the built project"
  fi
fi
echo "--- scenario B: $PASS_B passed, $FAIL_B failed ---"

########################  DIRTY HOST  ########################
echo; echo "############ DIRTY HOST ############"
# Stale global pointer at a deleted path + stale ZEPHYR_BASE + west off PATH.
mkdir -p "$HOME/.alp"
printf '{"sdkPath": "%s/ghost-sdk", "updatedAt": "2026-01-01T00:00:00Z"}' "$WORK" > "$HOME/.alp/sdk-default"
export ZEPHYR_BASE="$WORK/ghost-zephyr"
note "stale ~/.alp/sdk-default -> $WORK/ghost-sdk (does not exist)"
note "stale ZEPHYR_BASE -> $ZEPHYR_BASE (does not exist)"

hdr "doctor survives a dangling global default"
jrun ddoc any doctor --format json
DR=$(jget "$WORK/ddoc.out" sdk.root); DT=$(jget "$WORK/ddoc.out" sdk.sourceTier)
note "resolved=$DR tier=$DT"
# NOT "must still resolve an SDK". Discovery is deliberately BOUNDED -- the only
# checkout here is two levels down at proj/alp-workspace/alp-sdk, and walking
# arbitrary depth to find it is exactly what #292 exists to prevent (adopting an
# unrelated checkout). So reporting no SDK is the correct verdict, and the
# earlier assertion was wrong rather than the product.
# What MUST hold: the dangling pointer does not crash, does not resolve to the
# dead path, and the envelope stays well formed.
if [ "$DR" = "$WORK/ghost-sdk" ]; then
  bad "dirty: resolved to the DEAD path from the stale pointer"
else
  ok "dirty: dangling pointer not resolved (tier=$DT) -- fell through cleanly"
fi
# The remedy not naming the stale pointer is tracked as #344, not
# asserted here: it is a message-quality gap, not a behavioural one.

if [ "$HAVE_PROJECT" -ne 1 ]; then
  note "#336 slice comparison NOT RUN -- no project was built in Scenario B"
else
  hdr "#336 a dangling ZEPHYR_BASE must not change ANY slice's outcome"
  # The control that found #336. Asserting only the exit code is too weak: the
  # bug dropped ONE slice (m55_hp) while the other still built, so a run can be
  # non-zero for unrelated reasons and still hide it. Compare slice-by-slice
  # against the same build with ZEPHYR_BASE unset.
  "$TAN" build --project blinky-e2e --format json >"$WORK/dbuild.out" 2>"$WORK/dbuild.err"; RC=$?
  DESZ=$(wc -c <"$WORK/dbuild.err" | tr -d ' ')
  slices_of() { python3 - "$1" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("UNREADABLE"); raise SystemExit
print(",".join(f"{s.get('coreId')}={s.get('status')}"
                for s in sorted((d.get("data") or {}).get("slices") or [],
                                key=lambda x: x.get("coreId") or "")))
PY
}
  # Wire vocabulary is `ok` / `skipped` / `failed` (build_cmd.py's
  # _WIRE_STATUS -- `succeeded` internally, `ok` on the wire), matching what
  # slices_of() prints above verbatim, no translation needed here.
  slice_list_has_ok() {
    printf '%s' "$1" | grep -qE '(^|,)[^,=]+=ok(,|$)'
  }
  DIRTY_SLICES=$(slices_of "$WORK/dbuild.out")
  ( unset ZEPHYR_BASE; "$TAN" build --project blinky-e2e --format json >"$WORK/cleanbuild.out" 2>/dev/null )
  CLEAN_SLICES=$(slices_of "$WORK/cleanbuild.out")
  note "with dangling ZEPHYR_BASE: $DIRTY_SLICES"
  note "with ZEPHYR_BASE unset:    $CLEAN_SLICES"
  # Equality alone is NOT sufficient, and this assertion already reported a
  # false PASS on exactly that: after the tan-cli#349 onedir change broke the
  # launcher, BOTH runs produced UNREADABLE, compared equal, and this printed
  # PASS while nothing had built at all. Two runs being equally broken is not
  # the property under test. Require a real, parseable slice list on both
  # sides first.
  if [ "$DIRTY_SLICES" = "UNREADABLE" ] || [ "$CLEAN_SLICES" = "UNREADABLE" ]; then
    bad "#336: slice outcomes UNREADABLE on at least one side -- nothing was compared"
  elif [ "$DIRTY_SLICES" = "$CLEAN_SLICES" ]; then
    ok "#336: slice outcomes identical with and without a dangling ZEPHYR_BASE"
  else
    bad "#336: a dangling ZEPHYR_BASE changed slice outcomes"
  fi
  # A Zephyr-SDK-less container legitimately fails this build (no toolchain)
  # -- that is Scenario B's own already-scored finding, not a NEW defect here.
  # Only assert exit 0 when the SDK was actually usable, mirroring the same
  # fix applied to the main build leg above.
  if [ "$SDK_OK" -eq 1 ]; then
    if [ "$RC" -ne 0 ]; then
      bad "dirty build: exit $RC"
      envelope_issues "$WORK/dbuild.out"
      [ "$DESZ" -eq 0 ] || note "$(excerpt "$WORK/dbuild.err")"
    elif [ "$DIRTY_SLICES" = "UNREADABLE" ] || [ "$CLEAN_SLICES" = "UNREADABLE" ]; then
      # Already scored above (#336) -- an unreadable slice list means nothing
      # was compared, so asserting slice success again here would double-score
      # the same defect.
      note "dirty build: exit 0, but slice success not checked -- slices already scored UNREADABLE above (#336)"
    elif slice_list_has_ok "$DIRTY_SLICES" && slice_list_has_ok "$CLEAN_SLICES"; then
      ok "dirty build: exit 0 and at least one slice succeeded on both sides"
    else
      # #336 above only requires the two slice lists to be EQUAL, never that
      # either one contains a success -- a run where every Zephyr slice is
      # SKIPPED on both sides (e.g. this container's Zephyr toolchain vanished
      # mid-run) still passes that equality check and can still exit 0 (a
      # partial build, some other slice succeeding), scoring two PASSes for a
      # run with no real ARM ELF on either side. The comment above this build
      # ("exit 0 alone is never proof of a real ELF on ANY arm") applies here
      # too; NOT re-running the hoisted ELF-file block, since the ELF it would
      # find is stale from the earlier :727 Scenario-B build -- slice status
      # is the check that actually asks about THIS build.
      bad "dirty build: exit 0 but no slice succeeded on both sides (dirty=$DIRTY_SLICES clean=$CLEAN_SLICES)"
    fi
  else
    note "dirty build: exit $RC (no Zephyr SDK in this container -- consistent with Scenario B, not scored again)"
  fi
fi


############ AMBIGUOUS AND REFUSED-WRITE SURFACES ############
#
# Two behaviours that only a FROZEN binary can really answer, and that no
# scenario above touches.
#
# tan-cli#407's warning is attached in `envelope.py` behind a LAZY import of
# `build_cmd`, inside a `try/except Exception` that deliberately swallows
# failures so an advisory can never break a command's real result. In a
# PyInstaller freeze that is exactly the shape that hides an ImportError: the
# warning would silently never fire and every unit test would still pass,
# because they import from source. This is the check that says otherwise.
#
# tan-cli#420 is a filesystem effect -- the writability probe's own empty file
# being taken back off disk when the emit is refused -- so it is only observable
# by looking at the disk after a real refused run.
echo
echo "############ AMBIGUOUS SDK LAYOUT + REFUSED WRITES ############"

D407="$WORK/two-checkouts"
mkdir -p "$D407/alp-sdk/scripts" "$D407/ws/alp-sdk/scripts"
: > "$D407/alp-sdk/scripts/alp_project.py"
: > "$D407/ws/alp-sdk/scripts/alp_project.py"

# `doctor` takes the narrow ladder (the lateral checkout), `examples` the wide
# one (the child). Both must SAY so; before #407 both reported `discovery` and
# named only their own root.
# cd FIRST: these commands answer from the cwd, and running the loop before
# the cd measured a directory with no checkouts at all (`root=-`,
# `divergent=False`) -- a green-looking probe of the wrong place.
cd "$D407/ws" || { bad "#407: cannot cd into the two-checkout workspace"; }

DIV_MISSING=""
for C in doctor examples; do
  "$TAN" $C --format json >"$WORK/div-$C.json" 2>/dev/null
  HAS=$(tail -1 "$WORK/div-$C.json" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('UNREADABLE'); raise SystemExit
print('sdk.discovery-divergent' in [i.get('code') for i in d.get('issues',[])])
" 2>/dev/null)
  ROOT=$(tail -1 "$WORK/div-$C.json" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('UNREADABLE'); raise SystemExit
print((d.get('sdk') or {}).get('root','-'))
" 2>/dev/null)
  note "$C: root=$ROOT divergent=$HAS"
  [ "$HAS" = "True" ] || DIV_MISSING="$DIV_MISSING $C"
done
if [ -z "$DIV_MISSING" ]; then
  ok "#407: both ladders report sdk.discovery-divergent from one directory"
else
  bad "#407: no divergence warning from:$DIV_MISSING (a lazy import swallowed in the freeze?)"
fi

# The human surface, not just the wire.
#
# Keyed on what the READER sees -- the sentence naming the other checkout --
# not on a check NAME. This first asserted `sdkDiscoveryDivergent`, a
# separate check that a later reconciliation deleted in favour of tan-cli#428's
# treatment (the `sdk` check itself moves pass -> warn and carries the code).
# The behaviour never regressed; the assertion had simply outlived the
# internal name it was written against, and reported a silent doctor while
# doctor was in fact naming both roots. An e2e assertion has to survive that.
"$TAN" doctor >"$WORK/div-doctor.txt" 2>&1
# doctor renders this path through `_abs_posix()` (build_cmd.py:333):
# `os.path.abspath().replace("\\","/")`, which on Windows is always the
# drive-letter form `C:/...` -- even under Git Bash, where THIS script's own
# $D407 is the MSYS mount form `/c/...`. Same directory, different spelling:
# a literal grep for "$D407/ws/alp-sdk" therefore never matches on Windows and
# always misreports the divergence warning as silent. cygpath -m renders the
# same drive-letter/forward-slash form doctor does; on a host with no cygpath
# (plain POSIX), $D407 is already in that form, so the fallback is a no-op.
divpath() { command -v cygpath >/dev/null 2>&1 && cygpath -m "$1" || printf '%s\n' "$1"; }
D407_WS_ALP_SDK="$(divpath "$D407/ws/alp-sdk")"
# NEVER grep doctor's PROSE line by line (tan-cli#500). doctor's text renderer
# wraps every check block at `shutil.get_terminal_size(fallback=(100,24)).columns`
# UNCONDITIONALLY -- unlike `tan.env.wrap_width` it does not consult isatty, so
# a redirected stream still wraps at 100 -- and the two absolute paths
# interpolated ahead of this sentence move the wrap point with the harness's own
# $WORK length. Measured: at len($WORK) >= 40 the sentence straddles a line
# break (non-monotonic -- 53-55 happens to re-match, 56+ fails again), `grep -q`
# misses, and this assertion scored a FABRICATED product defect against a report
# that named both checkouts plainly, then exited 1. Flatten the wrapping first.
#
# The PATH half deliberately keeps grepping the unflattened file: `wrap_block`
# passes `break_long_words=False`, so a path token always survives intact on its
# own line, and `grep -qF` on the raw text is the stricter check of the two.
flatprose() { tr '\n' ' ' < "$1" | tr -s ' '; }
if flatprose "$WORK/div-doctor.txt" | grep -q "resolve a DIFFERENT checkout"    && grep -qF "$D407_WS_ALP_SDK" "$WORK/div-doctor.txt"; then
  ok "#407: doctor's text report names the second checkout"
  # `[^[]*`, not `.*`: flattening puts the whole report on ONE line, where a
  # greedy `.*` runs past the `sdk` check into every later one that happens to
  # contain the phrase. Measured on a three-check sample: 210 chars greedy vs
  # 107 bounded, the greedy match swallowing an unrelated `[   ok] zephyr`
  # block. Stopping at the next `[` keeps the note inside its own check.
  note "$(flatprose "$WORK/div-doctor.txt" | grep -o "warn\] sdk: [^[]*resolve a DIFFERENT checkout" | head -1)"
else
  bad "#407: doctor's text report is silent about the second checkout"
  note "$(grep -E "^\[.*\] sdk:" "$WORK/div-doctor.txt" | head -1)"
fi

# Negative control. A warning that also fires on the ordinary single-checkout
# host is worse than none -- it trains the reader to ignore the real case.
D407B="$WORK/one-checkout"
mkdir -p "$D407B/alp-sdk/scripts" "$D407B/ws"
: > "$D407B/alp-sdk/scripts/alp_project.py"
cd "$D407B/ws" >/dev/null 2>&1 || true
"$TAN" doctor >"$WORK/div-single.txt" 2>&1
# Keyed on the SAME sentence the positive assertion above uses, and for the same
# reason its comment gives. This control was still grepping `sdkDiscoveryDivergent`
# -- the check name that reconciliation deleted -- so it matched nothing on any
# host and scored a PASS unconditionally (tan-cli#500). Measured: the string
# exists nowhere in `python/` or `contract/`, only in this file. A negative
# control that cannot fire is not a control; it is the thing it was written to
# catch, one assertion later.
if flatprose "$WORK/div-single.txt" | grep -q "resolve a DIFFERENT checkout"; then
  bad "#407: divergence reported on a host with ONE checkout"
else
  ok "#407: silent on a host with one checkout"
fi

# tan-cli#420. `--sdk-root` names nothing, so the emit is refused after the
# writability probe has already created the destination.
D420="$WORK/refused-emit"
mkdir -p "$D420" && cd "$D420" >/dev/null 2>&1 || true
DEST="$D420/build/generated/alp.conf"
"$TAN" generate --emit zephyr-conf --output "$DEST" --sdk-root "$WORK/no-such-sdk" \
  --format json >"$WORK/gen420.json" 2>/dev/null
if [ -e "$DEST" ]; then
  SZ=$(wc -c <"$DEST" | tr -d ' ')
  if [ "$SZ" -eq 0 ]; then
    bad "#420: a zero-byte $DEST survived a refused emit -- Zephyr would take it as an empty EXTRA_CONF_FILE"
  else
    ok "#420: a non-empty artefact is kept ($SZ B)"
  fi
else
  ok "#420: no stray destination file after a refused emit"
fi

# The other half: a file the user ALREADY had must never be removed. Empty on
# purpose -- that is the one case size alone cannot distinguish.
mkdir -p "$D420/keep/generated" && : > "$D420/keep/generated/alp.conf"
"$TAN" generate --emit zephyr-conf --output "$D420/keep/generated/alp.conf" \
  --sdk-root "$WORK/no-such-sdk" --format json >/dev/null 2>&1
if [ -e "$D420/keep/generated/alp.conf" ]; then
  ok "#420: a pre-existing destination is never deleted"
else
  bad "#420: DELETED a destination file the user already had"
fi

echo
echo "=== $(uname -s): $PASS passed, $FAIL failed ==="
echo "    scenario A (bare host prerequisite refusal): $PASS_A passed, $FAIL_A failed"
echo "    scenario B (provisioned host, real ARM ELF):  $PASS_B passed, $FAIL_B failed"
[ "$FAIL" -gt 0 ] && { echo "failed:"; echo "$FAILED_NAMES" | tr '|' '\n' | grep -v '^$' | sed 's/^/  - /'; }
[ "$FAIL" -eq 0 ]
