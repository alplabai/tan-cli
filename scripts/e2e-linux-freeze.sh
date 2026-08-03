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
# Derived, never a hardcoded account: this repo is public and its history is
# permanent. Override with TAN_CHECKOUT when the clone lives elsewhere.
TAN_CHECKOUT="${TAN_CHECKOUT:-$HOME/tan-cli}"
cd "$TAN_CHECKOUT" || exit 2
git fetch --quiet origin feat/v06-batch || exit 2
git checkout --quiet -B v06 origin/feat/v06-batch || exit 2
echo "  linux tree @ $(git log --oneline -1)"
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
