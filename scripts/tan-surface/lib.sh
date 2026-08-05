#!/usr/bin/env bash
# Primitives for the tan command-surface harness. Sourced by run.sh; running
# this file directly does nothing.
#
# WHY THIS EXISTS NEXT TO scripts/e2e-full.sh RATHER THAN INSIDE IT.
# e2e-full.sh is a RELEASE REGRESSION harness: it hijacks $HOME, wipes its tree
# on every run, and drives seven commands deeply (--version, bootstrap, init,
# doctor, build, generate, examples) to prove a specific set of already-fixed
# bugs stays fixed. Measured 2026-08-05: those seven are the whole surface it
# touches, out of the 32 `tan --help` lists.
#
# This harness is the other axis -- every command, in order, against a REAL
# project, repeatably, with no $HOME hijack. Merging the two would make both
# worse: the regression suite needs a hermetic home to be meaningful, and this
# one needs the operator's actual machine to be meaningful.
#
# `tan flash` is NOT driven here and there is no flag to enable it. Programming
# a board is not something a surface walk should do as a side effect.

# Counters. Deliberately five buckets, not two: an expected failure is not a
# pass (it would hide the day it starts passing) and not a failure (it would
# make every run red until the upstream issue is fixed).
PASS=0; FAIL=0; XFAIL=0; XPASS=0; SKIP=0
FAILED_LABELS=(); XPASSED_LABELS=()

: "${TAN:=tan}"
: "${STEP_TIMEOUT:=900}"
: "${LEDGER:=}"
: "${CURRENT_PHASE:=-}"

# `timeout` is coreutils; macOS ships none by default and Homebrew installs it
# as `gtimeout`. Degrade to running unguarded rather than refusing: a missing
# timeout should cost a hang the operator can Ctrl-C, not a harness that will
# not start.
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN=timeout
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN=gtimeout
fi

hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
note() { printf '        %s\n' "$1"; }
warn() { printf '\033[33m        %s\033[0m\n' "$1"; }

_result() {  # _result <status> <label> <detail>
  local colour reset='\033[0m'
  case "$1" in
    PASS)  colour='\033[32m' ;;
    FAIL)  colour='\033[31m' ;;
    XFAIL) colour='\033[33m' ;;
    XPASS) colour='\033[35m' ;;
    SKIP)  colour='\033[90m' ;;
    *)     colour='' ; reset='' ;;
  esac
  printf "  ${colour}%-5s${reset}  %-58s %s\n" "$1" "$2" "$3"
}

_ledger() {  # _ledger <status> <label> <expected> <exit> <issue> <cmd...>
  [ -n "$LEDGER" ] || return 0
  local status=$1 label=$2 expected=$3 got=$4 issue=$5; shift 5
  STATUS="$status" LABEL="$label" EXPECTED="$expected" GOT="$got" \
  ISSUE="$issue" PHASE="$CURRENT_PHASE" CMD="$*" python3 - >>"$LEDGER" <<'PY'
import json, os
print(json.dumps({
    "phase":    os.environ["PHASE"],
    "label":    os.environ["LABEL"],
    "command":  os.environ["CMD"],
    "expected": os.environ["EXPECTED"],
    "exit":     os.environ["GOT"],
    "status":   os.environ["STATUS"],
    "issue":    os.environ["ISSUE"] or None,
}))
PY
}

# Echo the command the way the operator would have typed it, then run it.
# The echoed line is copy-pasteable ON THIS MACHINE -- that is the whole point
# of the harness, so paths are shown resolved and NOT scrubbed. Nothing here is
# ever committed; only tracked files must stay placeholder-only.
_invoke() {  # _invoke <timeout> <args...>; sets RC, OUT, ERR
  local t=$1; shift
  printf '\033[90m$ %s %s\033[0m\n' "$(basename "$TAN")" "$*"
  OUT="$WORK/.out.$$"; ERR="$WORK/.err.$$"
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "$t" "$TAN" "$@" >"$OUT" 2>"$ERR"; RC=$?
  else
    "$TAN" "$@" >"$OUT" 2>"$ERR"; RC=$?
  fi
  # 124 is coreutils' "the timeout fired". Surface it as itself rather than
  # letting it masquerade as the command's own exit code.
  [ "$RC" = 124 ] && warn "timed out after ${t}s"
  return 0
}

_tail() {  # first line of stderr, else stdout -- enough to identify a failure
  local line
  line=$(head -c 400 "$ERR" 2>/dev/null | head -1)
  [ -n "$line" ] || line=$(head -c 400 "$OUT" 2>/dev/null | head -1)
  printf '%s' "$line"
}

# step <label> <expected-exit|any> [--timeout N] -- <tan args...>
step() {
  local label=$1 expect=$2 t=$STEP_TIMEOUT; shift 2
  [ "${1:-}" = "--timeout" ] && { t=$2; shift 2; }
  [ "${1:-}" = "--" ] && shift
  _invoke "$t" "$@"
  if [ "$expect" = "any" ] || [ "$RC" = "$expect" ]; then
    PASS=$((PASS+1)); _result PASS "$label" "exit $RC"
    _ledger PASS "$label" "$expect" "$RC" "" "$@"
  else
    FAIL=$((FAIL+1)); FAILED_LABELS+=("$label")
    _result FAIL "$label" "exit $RC, expected $expect"
    note "$(_tail)"
    _ledger FAIL "$label" "$expect" "$RC" "" "$@"
  fi
}

# xstep <issue> <label> <exit-while-broken> [--timeout N] -- <tan args...>
#
# A KNOWN defect, pinned to its issue. While the bug stands this is XFAIL and
# the run stays green. The day it is fixed the harness reports XPASS and exits
# non-zero -- that is the signal to delete the entry, not to silence it.
xstep() {
  local issue=$1 label=$2 broken=$3 t=$STEP_TIMEOUT; shift 3
  [ "${1:-}" = "--timeout" ] && { t=$2; shift 2; }
  [ "${1:-}" = "--" ] && shift
  _invoke "$t" "$@"
  if [ "$RC" = "$broken" ]; then
    XFAIL=$((XFAIL+1)); _result XFAIL "$label" "exit $RC -- known, #$issue"
    _ledger XFAIL "$label" "$broken" "$RC" "$issue" "$@"
  else
    XPASS=$((XPASS+1)); XPASSED_LABELS+=("#$issue $label")
    _result XPASS "$label" "exit $RC, was $broken -- #$issue looks FIXED"
    _ledger XPASS "$label" "$broken" "$RC" "$issue" "$@"
  fi
}

# envelope <label> -- <tan args...>
#
# Runs the command with --format json and checks the contract alp-sdk-vscode
# consumes. The load-bearing assertion is `ok == (exitCode == 0)`: a command
# that reports ok:true alongside a non-zero exitCode breaks every consumer that
# branches on one of the two.
envelope() {
  local label=$1; shift
  [ "${1:-}" = "--" ] && shift
  _invoke "$STEP_TIMEOUT" "$@" --format json
  local verdict
  verdict=$(tail -1 "$OUT" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print(f"BAD not JSON: {exc}"); raise SystemExit
missing = [k for k in ("command", "ok", "exitCode", "data") if k not in d]
if missing:
    print("BAD missing " + ",".join(missing)); raise SystemExit
if d["ok"] != (d["exitCode"] == 0):
    print("BAD ok=%r but exitCode=%r" % (d["ok"], d["exitCode"])); raise SystemExit
soft = [k for k in ("project", "issues", "sdk") if k not in d]
print("OK" + (" (no " + ",".join(soft) + ")" if soft else ""))
' 2>/dev/null)
  case "$verdict" in
    OK*) PASS=$((PASS+1)); _result PASS "$label" "$verdict"
         _ledger PASS "$label" envelope "$RC" "" "$@" ;;
    *)   FAIL=$((FAIL+1)); FAILED_LABELS+=("$label")
         _result FAIL "$label" "${verdict:-BAD no output}"
         _ledger FAIL "$label" envelope "$RC" "" "$@" ;;
  esac
}

# step_out <label> <extended-regex> [--timeout N] -- <tan args...>
#
# For defects the exit code cannot express. Several real ones are exactly that
# shape: a command that exits 2 while printing no error, or writes a launch.json
# naming a binary the build never produces. Asserting only on $? would call
# both of those a pass.
step_out() {
  local label=$1 pattern=$2 t=$STEP_TIMEOUT; shift 2
  [ "${1:-}" = "--timeout" ] && { t=$2; shift 2; }
  [ "${1:-}" = "--" ] && shift
  _invoke "$t" "$@"
  if grep -Eq -- "$pattern" "$OUT" "$ERR" 2>/dev/null; then
    PASS=$((PASS+1)); _result PASS "$label" "matched /$pattern/"
    _ledger PASS "$label" "/$pattern/" "$RC" "" "$@"
  else
    FAIL=$((FAIL+1)); FAILED_LABELS+=("$label")
    _result FAIL "$label" "no /$pattern/ in output (exit $RC)"
    note "$(_tail)"
    _ledger FAIL "$label" "/$pattern/" "$RC" "" "$@"
  fi
}

# xstep_out <issue> <label> <regex-that-proves-the-bug> [--timeout N] -- <args...>
#
# Inverted on purpose: the pattern describes the BROKEN output, so a match is
# XFAIL. When the fix lands the pattern stops matching and the run reports
# XPASS -- same retire-me signal as xstep.
xstep_out() {
  local issue=$1 label=$2 pattern=$3 t=$STEP_TIMEOUT; shift 3
  [ "${1:-}" = "--timeout" ] && { t=$2; shift 2; }
  [ "${1:-}" = "--" ] && shift
  _invoke "$t" "$@"
  if grep -Eq -- "$pattern" "$OUT" "$ERR" 2>/dev/null; then
    XFAIL=$((XFAIL+1)); _result XFAIL "$label" "still /$pattern/ -- known, #$issue"
    _ledger XFAIL "$label" "/$pattern/" "$RC" "$issue" "$@"
  else
    XPASS=$((XPASS+1)); XPASSED_LABELS+=("#$issue $label")
    _result XPASS "$label" "/$pattern/ gone -- #$issue looks FIXED"
    _ledger XPASS "$label" "/$pattern/" "$RC" "$issue" "$@"
  fi
}

skip() { SKIP=$((SKIP+1)); _result SKIP "$1" "$2"; _ledger SKIP "$1" - - "" "$1"; }

summary() {
  printf '\n\033[1m== summary ==\033[0m\n'
  printf '  pass %d   fail %d   xfail %d   xpass %d   skip %d\n' \
    "$PASS" "$FAIL" "$XFAIL" "$XPASS" "$SKIP"
  if [ "${#FAILED_LABELS[@]}" -gt 0 ]; then
    printf '\n\033[31m  failures:\033[0m\n'
    printf '    - %s\n' "${FAILED_LABELS[@]}"
  fi
  if [ "${#XPASSED_LABELS[@]}" -gt 0 ]; then
    printf '\n\033[35m  now passing -- retire these from cases.sh:\033[0m\n'
    printf '    - %s\n' "${XPASSED_LABELS[@]}"
  fi
  [ -n "$LEDGER" ] && printf '\n  ledger: %s\n' "$LEDGER"
  # An XPASS is a real result that needs action, so it fails the run too.
  # A harness that stays green while its own expectations rot is the failure
  # mode this whole file exists to avoid.
  [ "$FAIL" -eq 0 ] && [ "$XPASS" -eq 0 ]
}
