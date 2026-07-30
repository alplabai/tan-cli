#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the single-file `tan` executable.
#
# --onefile is REQUIRED, not a preference: the VS Code extension downloads a raw
# binary straight to ONE cached path and has no unpack step anywhere in it
# (alp-sdk-vscode/src/alpCli/service.ts:295 "tan-cli ships a RAW binary per
# target (not an archive)"; download.ts:159-162 writes the response body to the
# destination file; download.ts:124-129 chmods it 0o755). A --onedir artifact
# cannot be consumed by the extension at all.
#
# PyInstaller is a BUILD-TIME tool only -- deliberately absent from the runtime
# dependencies in pyproject.toml. Build from a CLEAN environment holding nothing
# but the runtime deps: PyInstaller bundles whatever its hooks can see, so a
# shared interpreter that happens to have numpy/Pillow/pywin32 installed inflates
# the artifact (measured: 34349423 B vs 10238030 B) and with it the unpack cost
# each invocation pays -- which the extension's 3 s --version probe must fit
# inside (alp-sdk-vscode/src/alpCli/vscodeAdapter.ts:288-290).
#
#   python -m venv .venv-build
#   .venv-build/bin/pip install typer rich pyyaml "pyinstaller>=6.10"
#   PYTHON=.venv-build/bin/python scripts/build_binary.sh
#
# The artifact is named `tan` / `tan.exe` here. Release assets carry the Rust
# target triple the extension already hardcodes (service.ts:34-46) -- rename on
# upload, e.g. tan.exe -> tan-x86_64-pc-windows-msvc.exe. PyInstaller cannot
# cross-compile: each of the six targets must be built on its own host/runner.
set -euo pipefail
cd "$(dirname "$0")/.."

# `tan init`'s scaffold trees are DATA, and PyInstaller follows the static IMPORT
# graph, which reaches no non-.py file. Without this line the frozen binary is
# missing five of its six templates -- which reports `init.template-unreadable`
# (exit 5) rather than a traceback precisely because this is an easy line to
# lose. The destination mirrors the source layout's package-relative path, which
# is what lets `tan/templates/__init__.py` resolve it off `__file__` in both
# modes (PyInstaller points a frozen module's `__file__` under `sys._MEIPASS`).
# PyInstaller splits SOURCE from DEST on os.pathsep: `;` on Windows.
#
# The leading `../` is not a typo: a relative SOURCE is resolved against the
# generated spec file's directory, which `--specpath .build` puts one level below
# this script's cwd (proven -- without it the build dies with "Unable to find
# .../python/.build/tan/templates/vendored"). An absolute path would avoid the
# question but not portably: under Git Bash on Windows `$PWD` is an MSYS path
# (`/e/...`) that the native-Windows PyInstaller cannot open.
ADD_DATA_SEP=':'
case "${OS:-}" in Windows_NT) ADD_DATA_SEP=';' ;; esac

# --paths . pins the package root explicitly. PyInstaller 6.21 happens to
# resolve `tan` without it (it puts the CWD on the analysis path), but the dir
# it derives from the script is tan/, not the package root, so the implicit
# resolution is an accident of running from python/ -- state it instead.
"${PYTHON:-python}" -m PyInstaller --onefile --name tan --clean --noconfirm \
  --console --distpath dist --workpath .build --specpath .build \
  --add-data "../tan/templates/vendored${ADD_DATA_SEP}tan/templates/vendored" \
  --paths . tan/__main__.py

# Fail the BUILD, not merely the test suite, on a dirty interpreter. $PYTHON
# stays optional on purpose: an already-activated clean venv should not need
# ceremony, and demanding the variable would only prove someone set it, never
# that what it points at is clean. Size is the actual invariant -- assert that.
# The 3 s --version probe cannot stand in for this check: the dirty 34349423 B
# build answered in ~1.00 s and passed every timing assertion.
MAX_ARTIFACT_BYTES=15000000  # keep in step with tests/conformance/test_packaged_binary.py
artifact=dist/tan
[ -f dist/tan.exe ] && artifact=dist/tan.exe
size=$(wc -c <"$artifact")
if [ "$size" -ge "$MAX_ARTIFACT_BYTES" ]; then
  echo "ERROR: $artifact is $size B (ceiling $MAX_ARTIFACT_BYTES)." >&2
  echo "       Built from a dirty interpreter -- PyInstaller bundled modules" >&2
  echo "       tan never imports. Rebuild from a clean venv (see header)." >&2
  exit 1
fi
echo "OK: $artifact is $size B"
