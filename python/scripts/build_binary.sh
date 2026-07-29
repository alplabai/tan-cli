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
#   .venv-build/bin/pip install typer rich "pyinstaller>=6.10"
#   PYTHON=.venv-build/bin/python scripts/build_binary.sh
#
# The artifact is named `tan` / `tan.exe` here. Release assets carry the Rust
# target triple the extension already hardcodes (service.ts:34-46) -- rename on
# upload, e.g. tan.exe -> tan-x86_64-pc-windows-msvc.exe. PyInstaller cannot
# cross-compile: each of the six targets must be built on its own host/runner.
set -euo pipefail
cd "$(dirname "$0")/.."

# --paths . pins the package root explicitly. PyInstaller 6.21 happens to
# resolve `tan` without it (it puts the CWD on the analysis path), but the dir
# it derives from the script is tan/, not the package root, so the implicit
# resolution is an accident of running from python/ -- state it instead.
"${PYTHON:-python}" -m PyInstaller --onefile --name tan --clean --noconfirm \
  --console --distpath dist --workpath .build --specpath .build \
  --paths . tan/__main__.py
