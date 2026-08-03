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
PYTHON="$PY" VIRTUAL_ENV="$TAN_CHECKOUT/python/.venv-build" \
  bash scripts/build_binary.sh 2>&1 | tail -3
if [ -x dist/tan/tan ]; then
  echo "  freeze OK: $(dist/tan/tan --version)"
else
  echo "  ABORT: dist/tan/tan missing after build"; exit 2
fi
