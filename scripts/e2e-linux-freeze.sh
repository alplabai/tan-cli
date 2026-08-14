#!/usr/bin/env bash
# Build the Linux --onedir freeze for the cross-platform e2e, INSIDE WSL.
#
# This exists as a FILE for one reason, and it cost three full e2e rounds to
# learn: driving it as an inline `wsl -d Ubuntu-24.04 -- bash -c '...'` block
# from Git Bash does NOT work. Git Bash expands `$PWD`/`$PATH` in the OUTER
# shell before wsl ever sees the string -- even inside single quotes, and even
# with MSYS_NO_PATHCONV=1 on the call -- so the variables arrive EMPTY. Proven
# directly: an inline block that echoed `P="$PWD/.venv-build/bin/python"`
# printed `P=`. The visible symptom was
#   scripts/build_binary.sh: line 126: python: command not found
# which reads like a broken WSL, while `.venv-build/bin/python` was present,
# resolved to /usr/bin/python3.12, and had PyInstaller 6.21.0 installed.
#
# Everything here uses ABSOLUTE paths and an explicit PYTHON= for the same
# reason: `build_binary.sh` reads "${PYTHON:-python}", and Ubuntu 24.04 ships
# no bare `python`.
#
# Invoke as:  MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash <this file>

set -uo pipefail
# Operates on ITS OWN checkout by default -- the repo this file is in, resolved
# from its own location, never a hardcoded account path (this repo is public and
# its history is permanent). tan-cli#358: defaulting to a fixed `$HOME/tan-cli`
# meant that invoking the script from an isolated worktree silently froze a
# DIFFERENT tree than the one under test, and the version banner it printed gave
# no hint of it. Override with TAN_CHECKOUT to build a clone elsewhere.
TAN_CHECKOUT="${TAN_CHECKOUT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$TAN_CHECKOUT" || exit 2

# Freezes WHAT IS CHECKED OUT, including uncommitted work. It used to
# unconditionally `git fetch` + `git checkout -B v06 origin/feat/v06-batch`,
# which meant it measured what had been PUSHED rather than what was being
# tested -- so a local fix could pass the e2e it had not actually been built
# into. Set TAN_E2E_REF to opt back into fetching an explicit ref.
if [ -n "${TAN_E2E_REF:-}" ]; then
  git fetch --quiet origin "$TAN_E2E_REF" || exit 2
  git checkout --quiet --detach FETCH_HEAD || exit 2
fi
echo "  linux tree @ $TAN_CHECKOUT"
echo "  linux tree @ $(git log --oneline -1)$(git diff --quiet || echo '  [+ uncommitted changes]')"
cd python || exit 2
rm -rf dist .build
PY="$TAN_CHECKOUT/python/.venv-build/bin/python"
[ -x "$PY" ] || { echo "  ABORT: no venv interpreter at $PY"; exit 2; }
# UNPIPED, and the exit status is read (tan-cli#500). This was
# `bash scripts/build_binary.sh 2>&1 | tail -3` with nothing consulting the
# pipeline status, and success judged one line down on `[ -x dist/tan/tan ]`
# alone. build_binary.sh's over-ceiling path quarantines only the ARCHIVE
# (`mv -f dist/tan.tar.gz dist/tan.tar.gz.oversized`) and leaves the onedir tree
# in place, so a build that exited 1 printed `freeze OK` and this harness
# returned 0. `tail -3` made it worse rather than tidier: the ERROR block is six
# lines, so the `ERROR: dist/tan.tar.gz was ... B (ceiling ...)` and
# `Quarantined as ...` lines were truncated away and the operator saw only the
# three trailing advice lines, with no ERROR token anywhere. Every workflow that
# runs this script leaves it unpiped for exactly this reason
# (python-binaries.yml comments on it; release.yml, clean-host.yml and
# getting-started.yml all call it bare) -- this harness was the outlier.
PYTHON="$PY" VIRTUAL_ENV="$TAN_CHECKOUT/python/.venv-build" \
  bash scripts/build_binary.sh 2>&1 || {
    rc=$?
    echo "  ABORT: build_binary.sh exited $rc (the onedir tree may still exist -- an"
    echo "         over-ceiling build quarantines only dist/tan.tar.gz)"
    exit 2
  }
if [ ! -x dist/tan/tan ]; then
  echo "  ABORT: dist/tan/tan missing after build"; exit 2
fi
# `-x` alone is not a freeze that WORKS: it passes on the PyInstaller
# bootloader whatever state the app inside it is in. A `.venv-build` carrying
# PyInstaller but not tan's own runtime dependencies produces a tree that dies
# `ModuleNotFoundError: No module named 'typer'` on every invocation, and this
# gate used to read the version inside a command substitution -- whose non-zero
# status is discarded -- so it printed `freeze OK:` with an empty version and
# returned 0 (tan-cli#717). Same shape as tan-cli#500 three lines above: the
# fix there made build_binary.sh unpiped so its status is read; the --version
# call kept the defect. The tree then goes to e2e-container.sh as a green
# input.
FREEZE_VERSION=$(dist/tan/tan --version 2>&1); vrc=$?
if [ "$vrc" -ne 0 ] || [ -z "$FREEZE_VERSION" ]; then
  echo "  ABORT: dist/tan/tan --version exited $vrc -- the freeze is not runnable"
  printf '%s\n' "$FREEZE_VERSION" | sed 's/^/         /'
  exit 2
fi
echo "  freeze OK: $FREEZE_VERSION"
