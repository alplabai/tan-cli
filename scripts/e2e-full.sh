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
mkdir -p "$WORK/home" "$WORK/proj"
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
ZEPHYR_SDK_VERSION="${ZEPHYR_SDK_VERSION:-1.0.1}"
for cand in \
  "${ZEPHYR_SDK_INSTALL_DIR:-}" \
  "$HOME/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "$HOME/../zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/opt/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/usr/local/zephyr-sdk-$ZEPHYR_SDK_VERSION" \
  "/c/zephyr-sdk-$ZEPHYR_SDK_VERSION"
do
  if [ -f "$cand/sdk_version" ]; then export ZEPHYR_SDK_INSTALL_DIR="$cand"; break; fi
done
# `west sdk install` records the location in ~/.cmake/packages/Zephyr-sdk; on
# Windows that is the only place it lands, so a hardcoded path list misses it.
if [ -z "${ZEPHYR_SDK_INSTALL_DIR:-}" ]; then
  for reg in "$HOME/.cmake/packages/Zephyr-sdk"/*; do
    [ -f "$reg" ] || continue
    cand=$(tr -d '\r\n' < "$reg")
    [ -f "$cand/sdk_version" ] && { export ZEPHYR_SDK_INSTALL_DIR="$cand"; break; }
  done
fi
[ -n "${ZEPHYR_SDK_INSTALL_DIR:-}" ] && echo "  sdk:  $ZEPHYR_SDK_INSTALL_DIR" || echo "  sdk:  none found (build leg will be reported, not silently skipped)"

cd "$WORK/proj"

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
    note "$(head -c 300 "$o")"
    [ "$esz" -eq 0 ] || note "$(head -c 200 "$e")"
    return 1
  else
    ok "$label: one envelope, 0-byte stderr, ok/exitCode agree (exit $RC)"
    return 0
  fi
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

hdr "#322 doctor and bootstrap resolve the SAME root"
jrun doc2 any doctor --format json
jrun bs2 any bootstrap --dry-run --format json
D=$(jget "$WORK/doc2.out" sdk.root); B=$(jget "$WORK/bs2.out" data.sdkRoot)
note "doctor=$D"; note "bootstrap=$B"
# tan-cli#358: this compared the two values for NON-EMPTINESS, which is not what
# #322 is about. A real run printed doctor=.../proj/alp-sdk against
# bootstrap=.../proj/alp-workspace/alp-sdk -- two DIFFERENT checkouts, the exact
# disagreement #322 exists to catch -- and scored `PASS #322: both resolve an
# SDK`. The assertion is equality; anything weaker cannot fail for the reason it
# is named after.
if [ "$D" = "NONE" ] || [ "$B" = "NONE" ] || [ -z "$B" ]; then
  bad "#322: one side resolved nothing -- doctor='$D' bootstrap='$B'"
elif [ "$D" != "$B" ]; then
  bad "#322: doctor and bootstrap resolved DIFFERENT roots"
  note "doctor    = $D"
  note "bootstrap = $B"
else
  ok "#322: doctor and bootstrap resolve the same root ($D)"
fi

hdr "#323 --dry-run MUTATES NOTHING"
"$TAN" bootstrap --dry-run --sdk-root ./alp-sdk --format json >"$WORK/bsdry.out" 2>"$WORK/bsdry.err"
[ -d alp-sdk ]        && ok "#323: checkout not moved"        || bad "#323: checkout was MOVED by a dry run"
[ ! -d alp-workspace ] && ok "#323: no alp-workspace/ created" || bad "#323: dry run created alp-workspace/"
[ ! -e "$HOME/.alp" ]  && ok "#323: no ~/.alp written"         || bad "#323: dry run wrote the global pointer"
grep -q "would move\|would set" "$WORK/bsdry.out" && ok "#323: conditional wording (\"would\")" || note "no 'would' verb (no relocation planned)"

hdr "prerequisite scan (cmake, ninja, xz, wget)"
have_prereqs() {
  command -v cmake >/dev/null 2>&1 && command -v ninja >/dev/null 2>&1 \
    && command -v xz >/dev/null 2>&1 && command -v wget >/dev/null 2>&1
}
CAN_APT=0
if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && command -v apt-get >/dev/null 2>&1; then
  CAN_APT=1
fi
have_prereqs && note "cmake/ninja/xz/wget already present" || note "cmake/ninja/xz/wget NOT present"
note "root + apt-get available (can self-provision): $CAN_APT"

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
    note "$(head -c 300 "$out")"
    return
  fi
  [ "$esz" -eq 0 ] && oka "$label: 0-byte stderr" || bada "$label: stderr not empty (${esz}B)"
  local verdict
  verdict=$(python3 - "$out" "$rc" "bootstrap.$issue_code" "$@" <<'PY'
import json, sys
path, rc, code = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tools = sys.argv[4:]
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
      *)              bada "$label: unrecognised verdict '$verdict'" ;;
    esac
    note "$(head -c 300 "$out")"
  fi
}

########################  SCENARIO A: bare host refuses precisely  ########################
echo; echo "############ SCENARIO A: bare host (no build toolchain) ############"
if have_prereqs; then
  note "A: not applicable -- host already has cmake/ninja/xz/wget, nothing to refuse"
else
  check_bootstrap_refusal bsA1 prerequisites-missing cmake ninja xz wget
  if [ "$CAN_APT" -eq 1 ]; then
    note "A: installing exactly what tan named: cmake ninja-build xz-utils wget"
    apt-get install -y -qq --no-install-recommends cmake ninja-build xz-utils wget \
      >"$WORK/apt-a.log" 2>&1
    check_bootstrap_refusal bsA2 venv-unusable python3-venv
  else
    note "A: no root apt-get in this environment -- cannot install the four named"
    note "   tools to progress to the venv-unusable stage; that half of Scenario A"
    note "   is NOT RUN (not scored as a pass)."
  fi
fi
echo "--- scenario A: $PASS_A passed, $FAIL_A failed ---"

########################  SCENARIO B: provisioned host, real ARM ELF  ########################
echo; echo "############ SCENARIO B: provisioned host ############"
HAVE_PROJECT=0
SDK_OK=0
if ! have_prereqs && [ "$CAN_APT" -ne 1 ]; then
  note "B: NOT RUN -- host lacks cmake/ninja/xz/wget and cannot self-provision (no root apt-get)"
else
  if [ "$CAN_APT" -eq 1 ]; then
    note "B: installing cmake ninja-build xz-utils wget python3-venv"
    apt-get install -y -qq --no-install-recommends \
      cmake ninja-build xz-utils wget python3-venv >"$WORK/apt-b.log" 2>&1
  fi

  hdr "B: bootstrap succeeds on a provisioned host"
  T0=$(date +%s)
  "$TAN" bootstrap --sdk-root ./alp-sdk --non-interactive --format json \
    >"$WORK/bsB.out" 2>"$WORK/bsB.err"; RC=$?
  T1=$(date +%s)
  ESZ=$(wc -c <"$WORK/bsB.err" | tr -d ' ')
  OKVAL=$(jget "$WORK/bsB.out" ok)
  note "bootstrap: exit=$RC ok=$OKVAL stderr=${ESZ}B took $((T1-T0))s"
  if [ "$RC" -eq 0 ] && [ "$OKVAL" = "True" ] && [ "$ESZ" -eq 0 ]; then
    okb "B: bootstrap ok:true, exit 0, 0-byte stderr"
  else
    badb "B: bootstrap failed (exit $RC ok=$OKVAL stderr=${ESZ}B)"
    note "$(head -c 400 "$WORK/bsB.out")"
  fi
  WS=$(jget "$WORK/bsB.out" data.workspaceDir); note "workspace=$WS"
  WEST_BIN="$WS/.venv/bin/west"
  if [ "$WS" != "NONE" ] && [ -x "$WEST_BIN" ]; then
    okb "B: west exists at \$workspaceDir/.venv/bin/west"
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
    note "$(head -c 300 "$WORK/initB.out")"
  fi

  hdr "B: Zephyr SDK (west sdk install --version $ZEPHYR_SDK_VERSION -t arm-zephyr-eabi)"
  if [ "$HAVE_PROJECT" -eq 1 ] && [ -x "$WEST_BIN" ]; then
    "$TAN" doctor --build --format json >"$WORK/doctorPre.out" 2>/dev/null
    PRE=$(python3 - "$WORK/doctorPre.out" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("fail"); raise SystemExit
z=[c for c in (d.get("data") or {}).get("checks") or [] if c.get("name")=="zephyrSdk"]
print(z[0]["status"] if z else "fail")
PY
)
    if [ "$PRE" = "pass" ]; then
      SDK_OK=1
      note "B: Zephyr SDK already present -- no download needed"
    else
      SDK_TIMEOUT="${ZEPHYR_SDK_INSTALL_TIMEOUT:-1200}"
      T0=$(date +%s)
      if ( cd "$WS" && timeout "$SDK_TIMEOUT" "$WEST_BIN" sdk install \
             --version "$ZEPHYR_SDK_VERSION" -t arm-zephyr-eabi \
             >"$WORK/sdkinstall.out" 2>"$WORK/sdkinstall.err" ); then
        SDK_OK=1
      fi
      T1=$(date +%s)
      note "west sdk install: $((T1-T0))s (timeout ${SDK_TIMEOUT}s), exit-ok=$SDK_OK"
      [ "$SDK_OK" -eq 1 ] || note "$(tail -c 400 "$WORK/sdkinstall.err" 2>/dev/null)"
    fi
  else
    note "B: no west / no project -- cannot attempt the SDK install"
  fi

  hdr "B: build"
  if [ "$HAVE_PROJECT" -ne 1 ]; then
    badb "B: build not attempted -- init did not produce a project"
  else
    "$TAN" build --project blinky-e2e --format json >"$WORK/buildB.out" 2>"$WORK/buildB.err"; RC=$?
    if [ "$SDK_OK" -eq 1 ]; then
      [ "$RC" -eq 0 ] && okb "B: build exit 0" || { badb "B: build exit $RC"; note "$(head -c 500 "$WORK/buildB.out")"; }
      ELF=$(find . -name "zephyr.elf" 2>/dev/null | head -1)
      if [ -n "$ELF" ]; then
        DESC=$(file "$ELF" 2>/dev/null || echo "file(1) unavailable")
        note "$ELF: $DESC"
        echo "$DESC" | grep -qi "ELF.*ARM" && okb "B: build produced a real ARM ELF" \
                                            || badb "B: artefact is not an ARM ELF"
      else
        badb "B: no zephyr.elf produced despite a working Zephyr SDK"
      fi
    else
      # SDK install failed or was impractical (measured above, not assumed):
      # the build is EXPECTED to fail, and must name the missing Zephyr SDK --
      # an unnamed failure, or a build that unexpectedly succeeds anyway, is
      # itself a finding.
      if [ "$RC" -eq 0 ]; then
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
# The remedy not naming the stale pointer is tracked as #344 (v0.6.0), not
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
  slices_of() { python3 - "$1" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("UNREADABLE"); raise SystemExit
print(",".join(f"{s.get('coreId')}={s.get('status')}"
                for s in sorted((d.get("data") or {}).get("slices") or [],
                                key=lambda x: x.get("coreId") or "")))
PY
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
    [ "$RC" -eq 0 ] && ok "dirty build: exit 0" || { bad "dirty build: exit $RC"; note "$(head -c 400 "$WORK/dbuild.out")"; }
  else
    note "dirty build: exit $RC (no Zephyr SDK in this container -- consistent with Scenario B, not scored again)"
  fi
fi

echo
echo "=== $(uname -s): $PASS passed, $FAIL failed ==="
echo "    scenario A (bare host prerequisite refusal): $PASS_A passed, $FAIL_A failed"
echo "    scenario B (provisioned host, real ARM ELF):  $PASS_B passed, $FAIL_B failed"
[ "$FAIL" -gt 0 ] && { echo "failed:"; echo "$FAILED_NAMES" | tr '|' '\n' | grep -v '^$' | sed 's/^/  - /'; }
[ "$FAIL" -eq 0 ]
