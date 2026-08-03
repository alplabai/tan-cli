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
for cand in \
  /home/caner/zephyr-sdk-1.0.1 \
  /opt/zephyr-sdk-1.0.1 \
  "/c/Users/Caner/zephyr-sdk-1.0.1" \
  "/c/zephyr-sdk-1.0.1" \
  "$HOME/zephyr-sdk-1.0.1" \
  "$HOME/../zephyr-sdk-1.0.1"
do
  if [ -f "$cand/sdk_version" ]; then export ZEPHYR_SDK_INSTALL_DIR="$cand"; break; fi
done
# `west sdk install` records the location in ~/.cmake/packages/Zephyr-sdk; on
# Windows that is the only place it lands, so a hardcoded path list misses it.
if [ -z "${ZEPHYR_SDK_INSTALL_DIR:-}" ]; then
  for reg in "$HOME/.cmake/packages/Zephyr-sdk"/* "/c/Users/Caner/.cmake/packages/Zephyr-sdk"/*; do
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

# One parseable envelope on stdout, zero bytes on stderr. RC is exported.
jrun() {
  local label="$1"; shift
  local o="$WORK/$label.out" e="$WORK/$label.err"
  "$TAN" "$@" >"$o" 2>"$e"; RC=$?
  local esz; esz=$(wc -c <"$e" | tr -d ' ')
  [ "$esz" -eq 0 ] || { bad "$label: stderr $esz bytes"; note "$(head -c 200 "$e")"; }
  python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$o" 2>/dev/null \
    || { bad "$label: stdout not a single JSON envelope"; note "$(head -c 200 "$o")"; }
  [ "$esz" -eq 0 ] && python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$o" 2>/dev/null \
    && ok "$label: one envelope, 0-byte stderr (exit $RC)"
}
jget() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));
import functools;
p=sys.argv[2].split('.');v=d
for k in p:
    v = (v or {}).get(k) if isinstance(v,dict) else None
print(v if v is not None else 'NONE')" "$1" "$2" 2>/dev/null || echo NONE; }

########################  FRESH HOST  ########################
echo; echo "############ FRESH HOST ############"

hdr "version"
"$TAN" --version >"$WORK/v.out" 2>"$WORK/v.err"
[ "$(wc -c <"$WORK/v.err"|tr -d ' ')" -eq 0 ] && ok "version: 0-byte stderr" || bad "version: stderr not empty"
note "$(tr -d '\r\n' <"$WORK/v.out")"

hdr "doctor, nothing configured"
jrun doctor doctor --format json

hdr "sdk list --online (real HTTPS, the #304 CA canary)"
jrun sdklist sdk list --online --format json
[ "$RC" -eq 0 ] && ok "sdk list --online: exit 0 over real TLS" || bad "sdk list --online: exit $RC"

hdr "clone alp-sdk (quickstart layout)"
git clone --quiet --depth 1 https://github.com/alplabai/alp-sdk alp-sdk 2>"$WORK/clone.err" \
  && ok "alp-sdk cloned ($(find alp-sdk -type f | wc -l | tr -d ' ') files)" \
  || { bad "alp-sdk clone failed"; note "$(head -c 200 "$WORK/clone.err")"; }

hdr "#322 doctor and bootstrap resolve the SAME root"
jrun doc2 doctor --format json
jrun bs2 bootstrap --dry-run --format json
D=$(jget "$WORK/doc2.out" sdk.root); B=$(jget "$WORK/bs2.out" data.sdkRoot)
note "doctor=$D"; note "bootstrap=$B"
if [ "$D" != "NONE" ] && [ "$B" != "NONE" ] && [ "$B" != "" ]; then ok "#322: both resolve an SDK"; else bad "#322: doctor='$D' bootstrap='$B'"; fi

hdr "#323 --dry-run MUTATES NOTHING"
"$TAN" bootstrap --dry-run --sdk-root ./alp-sdk --format json >"$WORK/bsdry.out" 2>"$WORK/bsdry.err"
[ -d alp-sdk ]        && ok "#323: checkout not moved"        || bad "#323: checkout was MOVED by a dry run"
[ ! -d alp-workspace ] && ok "#323: no alp-workspace/ created" || bad "#323: dry run created alp-workspace/"
[ ! -e "$HOME/.alp" ]  && ok "#323: no ~/.alp written"         || bad "#323: dry run wrote the global pointer"
grep -q "would move\|would set" "$WORK/bsdry.out" && ok "#323: conditional wording (\"would\")" || note "no 'would' verb (no relocation planned)"

hdr "real bootstrap"
"$TAN" bootstrap --sdk-root ./alp-sdk --non-interactive --format json >"$WORK/bs.out" 2>"$WORK/bs.err"; RC=$?
note "exit=$RC stderr=$(wc -c <"$WORK/bs.err"|tr -d ' ')B"
[ "$RC" -eq 0 ] && ok "bootstrap: exit 0" || { bad "bootstrap: exit $RC"; note "$(head -c 400 "$WORK/bs.out")"; }
WS=$(jget "$WORK/bs.out" data.workspaceDir); note "workspace=$WS"

hdr "#299 doctor AFTER bootstrap: west must not be the reason it is unhappy"
jrun doc3 doctor --format json
# #299 is NOT "exit 4 never happens after bootstrap". Exit 4 is CORRECT when a
# genuinely required toolchain is absent -- e.g. zephyrSdk fail with
# ZEPHYR_SDK_INSTALL_DIR unset, which is the honest state of an isolated HOME.
# #299 was specifically that `west`/`westResolved` were misreported for a host
# where west lives in the workspace venv and off bare PATH. Assert THAT, or the
# check reports a false regression on any host missing an unrelated tool.
WESTBAD=$(python3 - "$WORK/doc3.out" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
bad=[c["name"] for c in d["data"]["checks"]
     if c["name"].lower().startswith("west") and c["status"]=="fail"]
print(",".join(bad) if bad else "NONE")
PY
)
[ "$WESTBAD" = "NONE" ] && ok "#299: no west* check fails after bootstrap (exit $RC)" \
                        || bad "#299: west check(s) failing after a successful bootstrap: $WESTBAD"
FAILING=$(python3 - "$WORK/doc3.out" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(",".join(c["name"] for c in d["data"]["checks"] if c["status"]=="fail") or "none")
PY
)
note "failing checks: $FAILING"

hdr "init + build to a real ARM ELF"
# `tan init` takes OPTIONS ONLY -- no positional name. --name is the
# subdirectory, --destination the parent. Passing a positional gives a usage
# envelope with exit 2, which is tan behaving correctly and the caller being
# wrong; do not read that as a product defect.
"$TAN" init --from-example peripheral-io/hello-world --name blinky-e2e --destination . --format json >"$WORK/init.out" 2>"$WORK/init.err"; RC=$?
[ "$RC" -eq 0 ] && ok "init: exit 0" || { bad "init: exit $RC"; note "$(head -c 300 "$WORK/init.out")"; }
"$TAN" build --project blinky-e2e --format json >"$WORK/build.out" 2>"$WORK/build.err"; RC=$?
[ "$RC" -eq 0 ] && ok "build: exit 0" || { bad "build: exit $RC"; note "$(head -c 500 "$WORK/build.out")"; }
ELF=$(find . -name "zephyr.elf" 2>/dev/null | head -1)
if [ -n "$ELF" ]; then
  DESC=$(file "$ELF" 2>/dev/null || echo "file(1) unavailable")
  note "$ELF: $DESC"
  echo "$DESC" | grep -qi "ELF.*ARM" && ok "build produced a real ARM ELF" || bad "artefact is not an ARM ELF"
else bad "no zephyr.elf produced"; fi

hdr "flash --dry-run"
jrun flash flash --dry-run --format json

########################  DIRTY HOST  ########################
echo; echo "############ DIRTY HOST ############"
# Stale global pointer at a deleted path + stale ZEPHYR_BASE + west off PATH.
mkdir -p "$HOME/.alp"
printf '{"sdkPath": "%s/ghost-sdk", "updatedAt": "2026-01-01T00:00:00Z"}' "$WORK" > "$HOME/.alp/sdk-default"
export ZEPHYR_BASE="$WORK/ghost-zephyr"
note "stale ~/.alp/sdk-default -> $WORK/ghost-sdk (does not exist)"
note "stale ZEPHYR_BASE -> $ZEPHYR_BASE (does not exist)"

hdr "doctor survives a dangling global default"
jrun ddoc doctor --format json
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
# Equality alone is NOT sufficient, and this assertion already reported a false
# PASS on exactly that: after the tan-cli#349 onedir change broke the launcher,
# BOTH runs produced UNREADABLE, compared equal, and this printed PASS while
# nothing had built at all. Two runs being equally broken is not the property
# under test. Require a real, parseable slice list on both sides first.
if [ "$DIRTY_SLICES" = "UNREADABLE" ] || [ "$CLEAN_SLICES" = "UNREADABLE" ]; then
  bad "#336: slice outcomes UNREADABLE on at least one side -- nothing was compared"
elif [ "$DIRTY_SLICES" = "$CLEAN_SLICES" ]; then
  ok "#336: slice outcomes identical with and without a dangling ZEPHYR_BASE"
else
  bad "#336: a dangling ZEPHYR_BASE changed slice outcomes"
fi
[ "$RC" -eq 0 ] && ok "dirty build: exit 0" || { bad "dirty build: exit $RC"; note "$(head -c 400 "$WORK/dbuild.out")"; }

echo
echo "=== $(uname -s): $PASS passed, $FAIL failed ==="
[ "$FAIL" -gt 0 ] && { echo "failed:"; echo "$FAILED_NAMES" | tr '|' '\n' | grep -v '^$' | sed 's/^/  - /'; }
[ "$FAIL" -eq 0 ]
