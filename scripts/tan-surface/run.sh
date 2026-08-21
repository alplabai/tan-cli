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
# File-wide: SOM/TEMPLATE/CORE/LEDGER/PROJ_NAME/MODE and friends
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
WORK=""
WORK_CREATED=0
PHASES=all
ALLOW_MUTATE=0
ALLOW_BOOTSTRAP=0
KEEP=0
LEDGER=""
BOOTSTRAPPED=0
STRICT=0

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
  --work PATH          sandbox root (default: a fresh mktemp -d). An EXISTING
                        directory is used as the PARENT of a per-run sandbox
                        inside it, never as the sandbox itself
  --phase NAME         discovery|project|generate|workspace|build|diag|teardown|all
  --som SKU            SoM for the scaffolded project (default: E1M-AEN801)
  --template ID        template for the scaffolded project (default: sensor-starter)
  --core ID            core for kconfig/debug-config (default: m55_hp)
  --allow-mutate       permit steps that WRITE into --project
  --allow-bootstrap    permit `tan bootstrap` (copies the SDK; slow, GBs of disk)
  --keep               keep the sandbox after the run
  --json FILE          append a machine-readable result ledger
  --strict             also fail the run if anything was SKIPped (workspace,
                        toolchain, ... not detected) -- "green" then means "the
                        whole surface ran", not just "nothing that ran failed"
  -h, --help           this text

Only a directory THIS run created is ever deleted at exit: the mktemp -d
default, a --work path that did not already exist, or the per-run
`tan-surface.XXXXXX` subdirectory made inside an EXISTING --work. The
directory you pass to --work is never itself removed; --keep additionally
keeps the per-run sandbox inside it.

`tan flash` is never run and there is no flag to enable it.

Exit status
  0  every step met its expectation
  1  a step failed, OR a known-broken step started passing (retire it)
  2  the harness could not start
TXT
}

# A value-taking flag given with no value used to die on bash's own `$2:
# unbound variable` under `set -u` -- exit 1, no usage text, and a message that
# names a shell variable rather than the flag the operator mistyped. The usage
# block above documents exit 2 for "the harness could not start", so say that
# instead. `"$@"` here is the whole remaining argument list, so `$# -ge 2`
# is exactly "this flag has something after it".
need_value() {  # need_value <flag> <rest-of-argv...>
  [ "$#" -ge 2 ] || { echo "ABORT: $1 needs a value" >&2; usage >&2; exit 2; }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tan)             need_value "$@"; TAN=$2; shift 2 ;;
    --sdk-root)        need_value "$@"; SDK=$2; shift 2 ;;
    --project)         need_value "$@"; MODE=inplace; PROJ=$2; shift 2 ;;
    --work)            need_value "$@"; WORK=$2; shift 2 ;;
    --phase)           need_value "$@"; PHASES=$2; shift 2 ;;
    --som)             need_value "$@"; SOM=$2; shift 2 ;;
    --template)        need_value "$@"; TEMPLATE=$2; shift 2 ;;
    --core)            need_value "$@"; CORE=$2; shift 2 ;;
    --allow-mutate)    ALLOW_MUTATE=1; shift ;;
    --allow-bootstrap) ALLOW_BOOTSTRAP=1; shift ;;
    --keep)            KEEP=1; shift ;;
    --json)            need_value "$@"; LEDGER=$2; shift 2 ;;
    --strict)          STRICT=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v "$TAN" >/dev/null 2>&1 || [ -x "$TAN" ] || {
  echo "ABORT: no tan binary at '$TAN' (--tan PATH, or put tan on PATH)" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || {
  echo "ABORT: python3 is required for the envelope and ledger checks" >&2; exit 2; }

# Presence on PATH is not proof `$TAN` IS tan -- on Windows especially, a
# same-named shim, a stale shell function, or a launcher stub for an entirely
# different tool can sit on PATH and still pass `command -v`. Prove it by
# asking, before running ~90 steps against something that answers to the name
# but is not the tool. (The concrete trap this generalises from: on Windows,
# `bash.exe` on PATH is often the WSL launcher stub, not bash -- a real,
# executable, PATH-resolvable binary that answers `-c` with a UTF-16LE "no
# distributions installed" banner instead of running anything. If you are
# invoking THIS script as `bash scripts/tan-surface/run.sh` on a Windows host,
# confirm your `bash` is real -- `bash -c 'echo ok'` -- before trusting
# anything this script goes on to report.)
TAN_VERSION_LINE=$("$TAN" --version 2>/dev/null | head -1)
case "$TAN_VERSION_LINE" in
  tan\ *) ;;
  *) echo "ABORT: '$TAN --version' did not print 'tan <version>' (got: '${TAN_VERSION_LINE:-<empty output>}') -- '$TAN' is not the tan binary" >&2
     exit 2 ;;
esac

[ -n "$SDK" ] || { echo "ABORT: --sdk-root is required" >&2; usage >&2; exit 2; }
[ -f "$SDK/scripts/alp_project.py" ] || {
  echo "ABORT: '$SDK' does not look like an alp-sdk checkout (no scripts/alp_project.py)" >&2; exit 2; }
SDK=$(CDPATH= cd -- "$SDK" && pwd)

# WORK_CREATED gates the cleanup trap below: only a directory THIS run made
# is ever deleted. Passing an existing directory to --work is a request to
# use it, not permission to destroy it at exit.
#
# An EXISTING --work therefore becomes the PARENT of a per-run sandbox rather
# than the sandbox itself. Without that, "never delete what the operator gave
# us" turned into "never clean up at all": every run left its SDK copy
# (several GB under --allow-bootstrap), its new-som/invalid-board/model-build
# scratch trees and its .out/.err capture files behind, forever, in a
# directory the operator most likely passed precisely BECAUSE they intended to
# reuse it. The per-run subdirectory is this run's own creation, so it is
# cleaned like the mktemp default, while the directory that was passed in is
# still never removed.
if [ -z "$WORK" ]; then
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/tan-surface.XXXXXX") || exit 2
  WORK_CREATED=1
elif [ -d "$WORK" ]; then
  WORK=$(CDPATH= cd -- "$WORK" && pwd)
  WORK=$(mktemp -d "$WORK/tan-surface.XXXXXX") || exit 2
  WORK_CREATED=1
else
  mkdir -p "$WORK" || exit 2
  WORK=$(CDPATH= cd -- "$WORK" && pwd)
  WORK_CREATED=1
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
printf '  tan       %s (%s)\n' "$TAN" "$TAN_VERSION_LINE"
printf '  sdk-root  %s\n' "$SDK"
printf '  project   %s (%s)\n' "$PROJ" "$MODE"
printf '  work      %s\n' "$WORK"
printf '  phases    %s\n' "$PHASES"
if [ "$MODE" = inplace ] && [ "$ALLOW_MUTATE" != 1 ]; then
  warn "read-only against a real project; pass --allow-mutate to include the writing steps (build, run, lock, support-bundle, generate, clean)"
fi

# An ALREADY-bootstrapped checkout is the normal state on a working machine, and
# it is the one the workspace/build phases actually need. Detect it instead of
# demanding --allow-bootstrap, which would otherwise make those phases skip
# forever for anyone who had already run `tan bootstrap` by hand.
#
# The signature is west's own: bootstrap puts the checkout INSIDE the workspace
# and writes <ws>/.west/config with `[manifest] path = <checkout basename>`,
# alongside the <ws>/.venv it created. Checking only that the two files/dirs
# EXIST beside $SDK is not enough -- an unrelated west workspace (a Zephyr
# checkout, a different SDK) sitting next to this one would match on presence
# alone and turn every workspace/build step into a confusing FAIL against a
# workspace that was never built for this checkout. Read the `path` value back
# and require it to actually name this checkout's own directory.
_west_manifest_path() {  # _west_manifest_path <west/config path>
  awk '
    /^\[/  { insection = ($0 == "[manifest]"); next }
    insection && match($0, /^[ \t]*path[ \t]*=/) {
      v = substr($0, RSTART + RLENGTH)
      gsub(/^[ \t]+|[ \t]+$/, "", v)
      print v
      exit
    }
  ' "$1" 2>/dev/null
}

#
# The three ways this can be false are DIFFERENT reasons, and the diagnostic
# must name the one that actually applies. Collapsing them into one "manifest
# path does not name this checkout" line states a false cause whenever the
# manifest DOES name the checkout and it is the `.venv` that is absent --
# measured on a real host (`~/.west/config` carrying `path = alp-sdk` beside
# an `--sdk-root` whose basename IS `alp-sdk`, with no `~/.venv`): the verdict
# (unbootstrapped) was right and the printed reason was wrong, which sends the
# reader looking at west's manifest instead of at the missing venv.
SDK_PARENT=$(dirname "$SDK")
SDK_BASE=$(basename "$SDK")
WEST_CONFIG="$SDK_PARENT/.west/config"
MANIFEST_PATH=""
[ -f "$WEST_CONFIG" ] && MANIFEST_PATH=$(_west_manifest_path "$WEST_CONFIG")
MANIFEST_NAMES_SDK=0
[ -n "$MANIFEST_PATH" ] && [ "$MANIFEST_PATH" = "$SDK_BASE" ] && MANIFEST_NAMES_SDK=1

if [ "$MANIFEST_NAMES_SDK" = 1 ] && [ -d "$SDK_PARENT/.venv" ]; then
  BOOTSTRAPPED=1
  printf '  workspace %s (already bootstrapped)\n' "$SDK_PARENT"
elif [ "$MANIFEST_NAMES_SDK" = 1 ]; then
  printf '  workspace none detected at %s (.west/config there DOES name this checkout (path=%s), but %s/.venv is missing -- treating as unbootstrapped)\n' \
      "$SDK_PARENT" "$MANIFEST_PATH" "$SDK_PARENT"
elif [ -f "$WEST_CONFIG" ]; then
  printf '  workspace none detected at %s (.west/config there has manifest path=%s, which does not name this checkout (%s) -- treating as unbootstrapped)\n' \
      "$SDK_PARENT" "${MANIFEST_PATH:-<unset>}" "$SDK_BASE"
elif [ -d "$SDK_PARENT/.venv" ]; then
  printf '  workspace none detected at %s (found a .venv there but no .west/config -- treating as unbootstrapped)\n' \
      "$SDK_PARENT"
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
  # Only ever delete a $WORK this run itself created (see the option parsing
  # above) -- an operator-supplied EXISTING directory is never removed here,
  # in EITHER --project or sandbox mode; what gets removed in that case is the
  # per-run subdirectory made inside it. Un-gating this from `MODE = sandbox`
  # also closes the leak the old condition had in --project mode: a
  # --project run's own mktemp'd scratch dir (used for the invalid-board
  # fixture, a bootstrap SDK copy, ...) used to survive every run untouched.
  if [ "$KEEP" != 1 ] && [ "$WORK_CREATED" = 1 ] && [ -n "${WORK:-}" ]; then
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
