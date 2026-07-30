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
#   .venv-build/bin/pip install -e . "pyinstaller>=6.10"
#   PYTHON=.venv-build/bin/python scripts/build_binary.sh
#
# `-e .` rather than a hand-listed dependency set: the list drifts. It drifted --
# `tan/cli.py` imports `click.testing`, typer stopped depending on click, and a
# venv built from the hand-written list froze a binary that died at import with
# `ModuleNotFoundError: No module named 'click'`. Installing the PACKAGE means
# the runtime deps come from pyproject.toml, which is also what a customer's
# `pip install alp-tan` resolves, so a build environment can no longer be
# quietly richer than the declared one. Extras stay OUT (no `[monitor]`): an
# extra is optional at runtime by definition, and freezing one in would make the
# binary disagree with what the wheel promises.
#
# `jsonschema` is in that list because the PLANNER relocated in (`tan/planner/`,
# was alp-sdk `scripts/alp_orchestrate/`) and validates every board.yaml against
# `metadata/schemas/board.schema.json`. It costs ~2.1 MB frozen: measured
# 12377580 B against the 15000000 B ceiling below, so the headroom is now ~2.6 MB
# where it was ~4.8 MB. A frozen build that omits it still runs -- `tan build`
# falls back to the SDK's own `-m alp_orchestrate` subprocess and, failing that,
# reports a coded `build.plan-unavailable` -- but no release should ship so.
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
#
# Both ceilings, their measurements and the reason there are two of them live in
# artifact_ceilings.env -- sourced, not copied, because the copy in
# tests/conformance/test_packaged_binary.py drifted from this one and the pair
# then rejected a correct aarch64 musl build (15277408 B against a single
# 15000000 ceiling).
. "$(dirname "$0")/artifact_ceilings.env"

# musl links libc statically and is legitimately ~1.0-1.4 MB larger than its
# glibc twin, so the ceiling depends on which libc this host builds against --
# not on the arch. `ldd --version` names it on both (busybox prints "musl libc
# (aarch64)", glibc prints "ldd (Debian GLIBC 2.31-...)"); the ld-musl-* probe
# is the fallback for a stripped image with no ldd at all. Windows and macOS
# take DEFAULT.
max_bytes=$TAN_MAX_ARTIFACT_BYTES_DEFAULT
libc=default
if ldd --version 2>&1 | head -1 | grep -qi musl || ls /lib/ld-musl-* >/dev/null 2>&1; then
  max_bytes=$TAN_MAX_ARTIFACT_BYTES_MUSL
  libc=musl
fi

artifact=dist/tan
[ -f dist/tan.exe ] && artifact=dist/tan.exe
size=$(wc -c <"$artifact")
if [ "$size" -ge "$max_bytes" ]; then
  # QUARANTINE, do not merely complain. `exit 1` alone is defeatable by a pipe:
  # `bash scripts/build_binary.sh | tail -3` returns tail's status, so the
  # caller's `set -e` never fires and the next line happily `cp`s an artifact
  # this gate just rejected. (That is not hypothetical -- it is how the
  # oversized aarch64 musl binary got copied out during the freeze
  # investigation.) A gate that a pipe can defeat is not a gate, so the file
  # itself has to stop existing under the name every consumer copies from.
  mv -f "$artifact" "$artifact.oversized"
  echo "ERROR: $artifact was $size B (ceiling $max_bytes, libc=$libc)." >&2
  echo "       Quarantined as $artifact.oversized; nothing is left to ship." >&2
  echo "       Usually a dirty interpreter -- PyInstaller bundled modules tan" >&2
  echo "       never imports. Rebuild from a clean venv (see header). If the" >&2
  echo "       venv IS clean, the ceiling is the question: measure it and edit" >&2
  echo "       scripts/artifact_ceilings.env, which both readers share." >&2
  exit 1
fi
# A stale quarantine from an earlier failed build would otherwise fail the
# conformance suite forever after a green rebuild.
rm -f "$artifact.oversized"
echo "OK: $artifact is $size B (ceiling $max_bytes, libc=$libc)"
