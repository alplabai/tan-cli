#!/usr/bin/env bash
# Walk the whole `tan` command surface, in dependency order, against a REAL
# project. Entry point -- see README.md, and cases.sh for the case list.
#
#   scripts/tan-surface/run.sh --sdk-root ~/src/alp-sdk
#   scripts/tan-surface/run.sh --sdk-root ~/src/alp-sdk --allow-bootstrap
#   scripts/tan-surface/run.sh --project ~/src/my-app --sdk-root ~/src/alp-sdk
#
# NOT `set -e`: a step that exits non-zero is a RESULT, not a reason to stop
# walking. The exit status of the whole run comes from summary().
#
# File-wide: SOM/TEMPLATE/CORE/EXPECT_MARKER/LEDGER/PROJ_NAME/MODE and friends
# read like dead stores in this file because they are consumed by cases.sh and
# lib.sh, which are sourced through the dynamic "$HERE" path. shellcheck cannot
# follow that, so it reports every one of them unused.
#
# SC1007 fires on every `CDPATH= cd -- ... && pwd` below. That is a one-command
# prefix assignment, not a typo'd empty assignment: it neuters a user's CDPATH,
# which would otherwise make `cd` land in an unrelated directory and echo it,
# corrupting the captured path.
# shellcheck disable=SC2034,SC1007
set -uo pipefail

# `CDPATH= cd` is a one-command prefix assignment that neuters a user's CDPATH,
# which would otherwise make `cd` land somewhere else and print the directory.
# shellcheck disable=SC1007
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

TAN="${TAN:-tan}"
SDK=""
MODE=sandbox
PROJ=""
PROJ_NAME="tan-surface-app"
TEMPLATE=sensor-starter
SOM=E1M-AEN801
CORE=m55_hp
EXPECT_MARKER="[i2c-master]"
WORK=""
PHASES=all
ALLOW_MUTATE=0
ALLOW_BOOTSTRAP=0
KEEP=0
LEDGER=""
BOOTSTRAPPED=0

usage() {
  cat <<'TXT'
Walk the whole `tan` command surface, in order, against a real project.

  scripts/tan-surface/run.sh --sdk-root <alp-sdk>
  scripts/tan-surface/run.sh --sdk-root <alp-sdk> --allow-bootstrap
  scripts/tan-surface/run.sh --project <my-app> --sdk-root <alp-sdk>

Options
  --tan PATH           tan binary to drive (default: $TAN, else `tan` on PATH)
  --sdk-root PATH      alp-sdk checkout (required)
  --project PATH       run against an EXISTING project instead of scaffolding
  --work PATH          sandbox root (default: a fresh mktemp -d)
  --phase NAME         discovery|project|generate|workspace|build|diag|teardown|all
  --som SKU            SoM for the scaffolded project (default: E1M-AEN801)
  --template ID        template for the scaffolded project (default: sensor-starter)
  --core ID            core for kconfig/renode (default: m55_hp)
  --allow-mutate       permit steps that WRITE into --project
  --allow-bootstrap    permit `tan bootstrap` (copies the SDK; slow, GBs of disk)
  --keep               keep the sandbox after the run
  --json FILE          append a machine-readable result ledger
  -h, --help           this text

`tan flash` is never run and there is no flag to enable it.

Exit status
  0  every step met its expectation
  1  a step failed, OR a known-broken step started passing (retire it)
  2  the harness could not start
TXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tan)             TAN=$2; shift 2 ;;
    --sdk-root)        SDK=$2; shift 2 ;;
    --project)         MODE=inplace; PROJ=$2; shift 2 ;;
    --work)            WORK=$2; shift 2 ;;
    --phase)           PHASES=$2; shift 2 ;;
    --som)             SOM=$2; shift 2 ;;
    --template)        TEMPLATE=$2; shift 2 ;;
    --core)            CORE=$2; shift 2 ;;
    --allow-mutate)    ALLOW_MUTATE=1; shift ;;
    --allow-bootstrap) ALLOW_BOOTSTRAP=1; shift ;;
    --keep)            KEEP=1; shift ;;
    --json)            LEDGER=$2; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v "$TAN" >/dev/null 2>&1 || [ -x "$TAN" ] || {
  echo "ABORT: no tan binary at '$TAN' (--tan PATH, or put tan on PATH)" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || {
  echo "ABORT: python3 is required for the envelope and ledger checks" >&2; exit 2; }
[ -n "$SDK" ] || { echo "ABORT: --sdk-root is required" >&2; usage >&2; exit 2; }
[ -f "$SDK/scripts/alp_project.py" ] || {
  echo "ABORT: '$SDK' does not look like an alp-sdk checkout (no scripts/alp_project.py)" >&2; exit 2; }
SDK=$(CDPATH= cd -- "$SDK" && pwd)

if [ -z "$WORK" ]; then
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/tan-surface.XXXXXX") || exit 2
else
  mkdir -p "$WORK" || exit 2
  WORK=$(CDPATH= cd -- "$WORK" && pwd)
fi

if [ "$MODE" = inplace ]; then
  [ -d "$PROJ" ] || { echo "ABORT: --project '$PROJ' is not a directory" >&2; exit 2; }
  PROJ=$(CDPATH= cd -- "$PROJ" && pwd)
else
  PROJ="$WORK/$PROJ_NAME"
fi

# shellcheck source=lib.sh
. "$HERE/lib.sh"
# shellcheck source=cases.sh
. "$HERE/cases.sh"

printf '\033[1mtan surface walk\033[0m\n'
printf '  tan       %s (%s)\n' "$TAN" "$("$TAN" --version 2>/dev/null | head -1)"
printf '  sdk-root  %s\n' "$SDK"
printf '  project   %s (%s)\n' "$PROJ" "$MODE"
printf '  work      %s\n' "$WORK"
printf '  phases    %s\n' "$PHASES"
if [ "$MODE" = inplace ] && [ "$ALLOW_MUTATE" != 1 ]; then
  warn "read-only against a real project; pass --allow-mutate to include the writing steps"
fi

# An ALREADY-bootstrapped checkout is the normal state on a working machine, and
# it is the one the workspace/build phases actually need. Detect it instead of
# demanding --allow-bootstrap, which would otherwise make those phases skip
# forever for anyone who had already run `tan bootstrap` by hand.
#
# The signature is west's own: bootstrap puts the checkout INSIDE the workspace
# and writes <ws>/.west/config with `[manifest] path = <checkout basename>`,
# alongside the <ws>/.venv it created.
SDK_PARENT=$(dirname "$SDK")
if [ -f "$SDK_PARENT/.west/config" ] && [ -d "$SDK_PARENT/.venv" ]; then
  BOOTSTRAPPED=1
  printf '  workspace %s (already bootstrapped)\n' "$SDK_PARENT"
else
  printf '  workspace none detected at %s\n' "$SDK_PARENT"
fi

# ---------------------------------------------------------------------------
# Bootstrap is opt-in and NEVER runs against the SDK checkout you passed in.
#
# `tan bootstrap` MOVES the checkout into its workspace (tan-cli#185) and writes
# a machine-global default at ~/.alp/sdk-default. A test harness that relocated
# the operator's working checkout would be indefensible, so this copies the SDK
# into the sandbox first and bootstraps the copy. The global default it writes
# is restored on exit.
# ---------------------------------------------------------------------------
GLOBAL_DEFAULT="$HOME/.alp/sdk-default"
GLOBAL_SAVED=""
GLOBAL_CREATED=0

restore_global() {
  if [ "$GLOBAL_CREATED" = 1 ] && [ -f "$GLOBAL_DEFAULT" ]; then
    rm -f "$GLOBAL_DEFAULT"
    printf '        removed the ~/.alp/sdk-default that bootstrap created\n'
  elif [ -n "$GLOBAL_SAVED" ] && [ -f "$GLOBAL_SAVED" ]; then
    cp "$GLOBAL_SAVED" "$GLOBAL_DEFAULT"
    printf '        restored your previous ~/.alp/sdk-default\n'
  fi
  if [ "$KEEP" != 1 ] && [ -n "${WORK:-}" ] && [ "$MODE" = sandbox ]; then
    chmod -R +w "$WORK" 2>/dev/null || true
    rm -rf "$WORK" 2>/dev/null || true
  fi
}
trap restore_global EXIT

do_bootstrap() {
  if [ "$BOOTSTRAPPED" = 1 ]; then
    note "workspace already bootstrapped -- not rebuilding one"
    return 0
  fi
  hdr "bootstrap (opt-in, on a COPY of the SDK)"
  if [ -f "$GLOBAL_DEFAULT" ]; then
    GLOBAL_SAVED="$WORK/.sdk-default.bak"; cp "$GLOBAL_DEFAULT" "$GLOBAL_SAVED"
  else
    GLOBAL_CREATED=1
  fi
  local base copy
  base=$(basename "$SDK")
  copy="$WORK/sdk-src"
  note "copying $SDK -> $copy/$base (your checkout is never moved)"
  mkdir -p "$copy" && cp -R "$SDK" "$copy/$base" || {
    warn "SDK copy failed; skipping bootstrap"; return 1; }
  step "bootstrap" 0 --timeout 3600 -- bootstrap --project "$PROJ" \
      --sdk-root "$copy/$base" --workspace "$WORK/ws"
  if [ "$RC" = 0 ]; then
    SDK="$WORK/ws/$base"   # bootstrap relocated it here
    BOOTSTRAPPED=1
    note "workspace-backed commands will use $SDK"
  else
    warn "bootstrap failed; the workspace and build phases will skip"
  fi
}

run_phase() {
  CURRENT_PHASE=$1
  case "$1" in
    discovery) phase_discovery ;;
    project)   phase_project ;;
    generate)  phase_generate ;;
    workspace) phase_workspace ;;
    build)     phase_build ;;
    diag)      phase_diag ;;
    teardown)  phase_teardown ;;
    *) echo "unknown phase: $1" >&2; exit 2 ;;
  esac
}

if [ "$PHASES" = all ]; then
  run_phase discovery
  run_phase project
  run_phase generate
  [ "$ALLOW_BOOTSTRAP" = 1 ] && do_bootstrap
  run_phase workspace
  run_phase build
  run_phase diag
  run_phase teardown
else
  # A single phase still needs its predecessors' state. `project` scaffolds on
  # demand; the workspace/build phases skip loudly rather than pretending.
  case "$PHASES" in
    generate|workspace|build|diag|teardown)
      if [ "$MODE" = sandbox ] && [ ! -f "$PROJ/board.yaml" ]; then
        CURRENT_PHASE=project; phase_project
      fi
      [ "$ALLOW_BOOTSTRAP" = 1 ] && do_bootstrap
      ;;
  esac
  run_phase "$PHASES"
fi

summary
