#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the `tan` executable as a PyInstaller --onedir freeze and archive it.
#
# --onedir, not --onefile, as of tan-cli#349. --onefile re-extracts its whole
# ~14 MB runtime into a FRESH temp dir on EVERY invocation, and on macOS each
# extracted .dylib is unsigned (the parent's ad-hoc signature does not cover
# extracted copies), so the OS re-verifies every one of them on every launch.
# Measured on the published v0.5.0-rc4 --onefile assets (5 runs of --version):
# macOS arm64 13.25/19.35/19.35/18.58/19.74 s, against git --version's 0.01 s
# on the same host. alp-sdk-vscode's own version probe times out at 3 s
# (vscodeAdapter.ts:1406) and its commandOnPath check at 5 s -- that asset's
# --version TIMED OUT under the extension's own probe, not merely "slow".
# Confirmed locally too (Windows, this host, Python 3.12.10/PyInstaller
# 6.21.0, mean of 5 --version runs): --onefile 0.880 s vs --onedir 0.369 s --
# a >2x win even on the platform that was never the emergency, since --onedir
# extracts ONCE, at install time, rather than per invocation.
#
# The old rationale here (an --onedir artifact "cannot be consumed by the
# extension at all", because the extension downloaded a raw binary straight to
# one cached path with no unpack step anywhere) is exactly the shape of the
# tan-cli#259 failure this comment used to warn about -- a stale comment
# asserting the opposite of the code. It stopped being true the moment this
# script started emitting an archive instead of a raw binary: the archive
# below IS meant to be unpacked, which install.sh (../install.sh, this repo)
# already does. Unpacking it on the alp-sdk-vscode side is a SEPARATE unit of
# #349, landing independently in that repo -- this script and the archive it
# produces are correct on their own regardless of when that lands.
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
#   .venv-build/bin/pip install -e ".[monitor]" "pyinstaller>=6.10"
#   PYTHON=.venv-build/bin/python scripts/build_binary.sh
#
# `-e .` rather than a hand-listed dependency set: the list drifts. It drifted --
# `tan/cli.py` imports `click.testing`, typer stopped depending on click, and a
# venv built from the hand-written list froze a binary that died at import with
# `ModuleNotFoundError: No module named 'click'`. Installing the PACKAGE means
# the runtime deps come from pyproject.toml, which is also what a customer's
# `pip install alp-tan` resolves, so a build environment can no longer be
# quietly richer than the declared one.
#
# `[monitor]` IS included, and this line used to say the opposite ("extras stay
# OUT ... freezing one in would make the binary disagree with what the wheel
# promises"). That reasoning inverts who can act. An extra is optional for a
# WHEEL because a wheel user can add it whenever they want; a customer holding a
# --onefile binary cannot, ever, and `tan monitor` is a command that binary
# advertises in its own `--help`. Leaving it out ships a dead command --
# `monitor.pyserial-missing`, whose text has always said "A frozen `tan` binary
# bundles it at build time, so a binary built without that extra cannot gain it
# here." Measured BOTH ways on real freezes, since a claim about what
# PyInstaller bundles is not something to reason about: with the extra,
# 13805270 B and `monitor.no-port`; without it, 13729615 B and
# `monitor.pyserial-missing` (Windows x86_64, Python 3.12.10, PyInstaller 6.x).
# So the lazy in-function `import serial` IS followed when installed and the
# module is NOT acquired otherwise -- no --hidden-import needed. +75655 B,
# against 2.69 MB of headroom under artifact_ceilings.env's default ceiling.
#
# The two release workflows had drifted apart on exactly this: python-binaries.yml
# froze `-e ".[monitor]"` while release.yml -- the workflow a `v*` tag actually
# runs -- froze a bare `.`. Every published asset would have carried the dead
# command; only the dispatch-only workflow was correct.
#
# `jsonschema` is in that list because the PLANNER relocated in (`tan/planner/`,
# was alp-sdk `scripts/alp_orchestrate/`) and validates every board.yaml against
# `metadata/schemas/board.schema.json`. It costs ~2.1 MB frozen: measured
# 12377580 B against the 15000000 B ceiling below, so the headroom is now ~2.6 MB
# where it was ~4.8 MB. A frozen build that omits it still runs -- `tan build`
# falls back to the SDK's own `-m alp_orchestrate` subprocess and, failing that,
# reports a coded `build.plan-unavailable` -- but no release should ship so.
#
# The onedir folder is named `tan/` (dist/tan/tan[.exe] + dist/tan/_internal/)
# and the ARCHIVE built from it below is named `tan.zip` / `tan.tar.gz` here.
# Release assets carry the Rust target triple the extension already hardcodes
# (service.ts:34-46) -- rename on upload, e.g. tan.zip ->
# tan-x86_64-pc-windows-msvc.zip. PyInstaller cannot cross-compile: each target
# must be built on its own host/runner.
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
#
# --collect-data certifi (tan-cli#304): `certifi.where()` returns a path to
# `certifi/cacert.pem`, a DATA file PyInstaller's static import-graph analysis
# does not reach on its own -- the same reason `--add-data` exists for
# `tan/templates/vendored` above. MEASURED redundant today:
# `pyinstaller-hooks-contrib` (a hard dependency of `pyinstaller>=6.10`, not
# something this repo controls) ships its own `hook-certifi.py` that collects
# the same file automatically -- a build with this flag removed still bundles
# `cacert.pem` (checked against a real freeze while writing this fix). Kept
# explicit anyway: the alternative is `tan/net.py`'s CA-floor path depending on
# a third-party hook nobody here declared, three packages removed from
# anything `pyproject.toml` states -- exactly the kind of implicit dependency
# that let `click` go undeclared until `tests/gates/test_declared_dependencies.py`
# existed (see that gate's own docstring). `truststore` needs no equivalent
# flag: it carries no data files, only Python + the OS's own verifier APIs.
"${PYTHON:-python}" -m PyInstaller --onedir --name tan --clean --noconfirm \
  --console --distpath dist --workpath .build --specpath .build \
  --add-data "../tan/templates/vendored${ADD_DATA_SEP}tan/templates/vendored" \
  --collect-data certifi \
  --paths . tan/__main__.py

# Archive the onedir folder into the actual release artefact -- one file, so
# `checksums.txt`/the attestation/install.sh all still deal with a single
# thing per target, matching the old raw-binary contract's shape even though
# the payload is now a directory (tan-cli#349). zip on Windows (installers on
# that platform reach for it natively); tar.gz elsewhere, matching every
# existing `.tar.gz`-based download path (curl | tar in getting-started.yml,
# install.sh). shutil.make_archive over `zip`/`tar` as external commands: it
# is stdlib, so it needs nothing this build venv doesn't already have, and it
# behaves identically across the three build OSes.
archive_ext=tar.gz
archive_format=gztar
case "${OS:-}" in Windows_NT) archive_ext=zip; archive_format=zip ;; esac
"${PYTHON:-python}" - "$archive_format" <<'PY'
import shutil
import sys

shutil.make_archive("dist/tan", sys.argv[1], root_dir="dist", base_dir="tan")
PY

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

# The ceiling now measures the ARCHIVE, not the onedir folder: that is the one
# file a consumer actually downloads, and a "size of a directory" number would
# depend on the filesystem's block size rather than on what shipped.
artifact="dist/tan.${archive_ext}"
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
