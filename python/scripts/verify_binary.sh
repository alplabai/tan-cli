#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Prove a FROZEN `tan` binary actually works. Usage:
#
#   sh scripts/verify_binary.sh <path-to-binary> <path-to-alp-sdk-checkout>
#
# A binary that starts is not a binary that works. Each check below is here
# because it is a real failure mode of a PyInstaller freeze, and every one of
# them was hit while establishing this path. From tan-cli#349 the freeze is
# --onedir, not --onefile: $BIN is the executable inside the onedir tree
# (e.g. dist/tan/tan or dist/tan/tan.exe), with a `_internal/` sibling
# directory that check 5/5 below depends on.
#
#   1. --version            -- import graph resolves at all.
#                              tan-cli#810 NARROWED what this proves: `cli.py`
#                              used to import `click.testing` at module scope
#                              (typer 0.27 stopped pulling it in, so a
#                              clean-venv build died right here with
#                              ModuleNotFoundError before printing a byte).
#                              That import is now deferred into the
#                              `--help --format json` path, so `--version` no
#                              longer touches it and no check below does
#                              either. `tests/conformance/
#                              test_packaged_binary.py::
#                              test_the_artifact_carries_click_testing` is what
#                              covers it against a real artifact now.
#   2. generate --help      -- the flag the extension's contract needs exists.
#                              Released v0.4.1 has no `--output`; a binary that
#                              runs but lacks it is the wrong binary.
#   3. init --template      -- `tan/templates/vendored/**` is DATA. PyInstaller
#                              follows the import graph, which reaches no
#                              non-.py file, so without the `--add-data` in
#                              build_binary.sh five of six templates report
#                              `init.template-unreadable` (exit 5). Nothing in
#                              --version or --help touches them.
#   4. generate --output    -- a real emit against a real board.yaml. The planner
#                              is the deepest import path in the package
#                              (PyYAML + jsonschema + the metadata loaders); a
#                              missing hidden import surfaces HERE and nowhere
#                              earlier.
#   5. CA trust anchors     -- tan-cli#304: a PyInstaller freeze bundles its own
#                              `ssl` but no CA bundle and does not fall back to
#                              the platform trust store, so the shipped
#                              v0.5.0-rc2 macOS asset had NO trust anchors at
#                              all -- every `urllib` HTTPS call in `sdk list
#                              --online` failed CERTIFICATE_VERIFY_FAILED on a
#                              host where curl/a browser/system python3 all
#                              verified the same endpoint fine. Nothing above
#                              this check would have caught it: 1-4 never touch
#                              `tan.net`. See the check itself for why this is a
#                              structural proof, not a live network call.
#
# POSIX sh, no bashisms: this runs under busybox in `alpine:3.20` (where the
# musl asset must be proven Python-free), under Debian's dash, under macOS bash
# 3.2 and under Git Bash on Windows.
set -eu

BIN=${1:?usage: verify_binary.sh <binary> <alp-sdk-root>}
SDK=${2:?usage: verify_binary.sh <binary> <alp-sdk-root>}

fail() { echo "FAIL: $1" >&2; exit 1; }

# tan-cli#361: checks 3-5 run from a throwaway project directory (`cd "$work"`
# below), so a RELATIVE argument -- which is exactly what the usage line above
# documents (`dist/tan/tan`) -- stops resolving the instant that cd happens.
# `sh scripts/verify_binary.sh ./dist/tan/tan /tmp/sdk` passed 1/5 and 2/5 and
# then died with `./dist/tan/tan: not found` at 3/5. BOTH arguments carry the
# bug, not just the one the issue named: $BIN is re-invoked in 3/5 and 4/5 and
# read from disk in 5/5, and $SDK is handed to the binary as `--sdk-root` in
# 4/5, where the binary resolves it against ITS cwd -- $work, not the caller's.
# Every CI call site passed `$PWD/...`, which is why CI could never see it.
#
# Resolved HERE, once, before the first cd -- not lazily at each use site, so
# whoever adds a check 6 does not have to remember. cd-into-dirname then `pwd`,
# recombined with `basename`, because the one-liners do not exist everywhere
# this runs: `readlink -f` is GNU-only (BSD readlink has no -f at all) and
# `realpath` is absent from a stock macOS, while this script must also survive
# busybox ash, dash, macOS bash 3.2 and Git Bash. `${bindir%/}` so a binary
# sitting at the filesystem root comes back as `/tan` and not `//tan` -- a
# leading `//` is implementation-defined in POSIX and Git Bash reads it as a
# UNC share.
[ -f "$BIN" ] || fail "no such binary: $BIN"
[ -d "$SDK" ] || fail "no such alp-sdk checkout: $SDK"
bindir=$(cd -- "$(dirname -- "$BIN")" && pwd)
BIN="${bindir%/}/$(basename -- "$BIN")"
SDK=$(cd -- "$SDK" && pwd)

echo "== 1/5 $BIN --version"
"$BIN" --version || fail "--version exited non-zero"

work=$(mktemp -d 2>/dev/null || mktemp -d -t tanverify)
trap 'rm -rf "$work"' EXIT

# Through a FILE, not a pipe into `grep -q`: grep exits at the first match and
# closes the pipe, and the frozen binary then trips over the half-written rich
# help text -- on Windows that surfaces as `OSError: [Errno 22] Invalid
# argument` from the cp1252 stdout wrapper, printed after the check already
# passed. Harmless to the check, indistinguishable from a real crash in a log.
#
# The question is whether the FLAG EXISTS, not how rich chose to draw it. Both
# macOS assets failed this check while the same build shape passed on Windows and
# Linux, and the binaries were fine (`tan 0.5.0-dev`, every other proof green) --
# the macOS runners give rich a colour-capable terminal, and rich styles a flag's
# leading dash SEPARATELY from the rest:
#
#   ESC[1;36m-ESC[0mESC[1;36m-outputESC[0m
#
# so the literal string `--output` is not in the bytes at all. `NO_COLOR=1` asks
# rich not to do that; the escapes are then stripped anyway, because a proof that
# depends on a library honouring an environment variable is a proof with a
# dependency. What survives is reduced to letters/digits/hyphens, which also
# removes box borders, column padding and line breaks -- so an option name
# wrapped across two lines rejoins as well. On failure the raw help is dumped:
# a proof that fails without showing what it saw sends the next person to the
# wrong place (this one sent me to terminal width).
echo "== 2/5 generate --help carries --output"
NO_COLOR=1 "$BIN" generate --help >"$work/help.txt" || fail "generate --help exited non-zero"
esc=$(printf '\033')
if ! sed "s/${esc}\[[0-9;]*[a-zA-Z]//g" "$work/help.txt" | tr -cd 'A-Za-z0-9-' | grep -q -- "--output"; then
  echo "---- generate --help, as rendered ----" >&2
  cat "$work/help.txt" >&2
  echo "--------------------------------------" >&2
  fail "generate --help has no --output (pre-0.5 binary?)"
fi

cd "$work"

echo "== 3/5 init writes the vendored template tree"
"$BIN" init --template zephyr-app --format json >init.json || fail "init exited non-zero"
grep -q '"ok":true' init.json || fail "init envelope not ok: $(cat init.json)"
[ -f board.yaml ] || fail "init wrote no board.yaml (missing --add-data for tan/templates/vendored?)"
[ -f src/main.c ] || fail "init wrote no src/main.c"

echo "== 4/5 generate --output emits against that board.yaml"
"$BIN" generate --target zephyr-conf --output ./out/alp.conf \
  --sdk-root "$SDK" --format json >gen.json || fail "generate exited non-zero: $(cat gen.json)"
grep -q '"ok":true' gen.json || fail "generate envelope not ok: $(cat gen.json)"
[ -s ./out/alp.conf ] || fail "generate wrote no --output file"
grep -q "^CONFIG_" ./out/alp.conf || fail "emitted file carries no CONFIG_ lines"

# STRUCTURAL, not a live network call. Two different proofs for two different
# kinds of bundled thing, because tan-cli#349 (--onedir) split them apart:
#
#   * `truststore` is pure-Python module CODE, no data files. PyInstaller
#     still compresses pure-Python module code into a PYZ archive embedded
#     INSIDE the executable itself, under --onedir exactly as it did under
#     --onefile -- externalising to `_internal/` only applies to DATA/BINARY/
#     EXTENSION entries, not the PYZ. The PYZ's directory table stores each
#     module's ORIGINAL NAME as plain ASCII right next to its (possibly
#     zlib-compressed) entry, so grepping the executable for the name is a
#     real proof of what got bundled -- verified against a real --onedir
#     freeze while fixing this check for #349: `grep -o
#     'truststore[a-z._]*' dist/tan/tan.exe` still finds all four platform
#     backends (`truststore/__init__.py` imports them unconditionally and
#     lets `ssl` pick the live one). UNCHANGED by #349.
#
#   * `certifi`'s `cacert.pem` is a DATA file (collected via `--collect-data
#     certifi` in build_binary.sh, not import-graph-reachable code), and DATA
#     files are exactly what --onedir stops embedding in the executable:
#     externalising them to disk ONCE at build time, instead of re-extracting
#     them from inside the exe on every launch, is the entire point of
#     --onedir. So `grep cacert.pem` on the executable now finds NOTHING --
#     measured on a real --onedir freeze, `grep -c cacert.pem dist/tan/tan.exe`
#     -> 0 -- while the same freeze passed under --onefile. Grepping the exe
#     here would pass or fail for the wrong reason: not proof of a real defect,
#     proof of the wrong file. The real bundled bytes are a loose file at
#     `_internal/certifi/cacert.pem`, a sibling of $BIN, so this check now
#     asserts on THAT path directly (existence + non-empty, not a byte-content
#     grep -- a PEM bundle's own content has no reason to contain the literal
#     string "cacert.pem").
#
# Chosen over `"$BIN" sdk list --online` against the real endpoint (the fix
# the #304 issue itself suggested) because it does NOT discriminate on every
# platform: measured on a real Windows freeze with NEITHER `truststore` nor
# `certifi` bundled, `sdk list --online` still SUCCEEDED -- CPython's `ssl` on
# win32 already calls `_load_windows_store_certs()` inside
# `create_default_context()`, a fallback macOS and Linux do not have, which is
# exactly why the shipped defect was a macOS asset. A live-network check on
# this platform would pass green on a build missing the fix entirely. This
# check has no such blind spot: it looks for the SAME bundled things on every
# OS, so it goes red the moment either mechanism drops out of the freeze,
# consistently, and needs no network to do it.
#
# Proves: the CA bundle `certifi.where()` resolves at runtime, and
# `truststore`'s platform backends, are physically in this freeze. Does NOT
# prove: that `ssl.create_default_context()` actually verifies a real
# certificate chain at runtime, or that the endpoint is reachable -- #304 was
# reachable-but-untrusted, not unreachable, so only a live call proves THAT,
# and this check trades it for one that cannot be masked by which OS built it.
echo "== 5/5 CA trust anchors are bundled (tan-cli#304)"
CA_BUNDLE="$(dirname "$BIN")/_internal/certifi/cacert.pem"
[ -s "$CA_BUNDLE" ] ||
  fail "no certifi CA bundle at $CA_BUNDLE -- check --collect-data certifi in build_binary.sh (tan-cli#304 would recur)"
grep -q "truststore" "$BIN" ||
  fail "no truststore module embedded -- tan/net.py's preferred CA mechanism is missing from this freeze (tan-cli#304 would recur)"

echo "OK: $BIN starts, carries --output, scaffolds, emits, and bundles its own CA trust anchors"
