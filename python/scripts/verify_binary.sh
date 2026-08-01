#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Prove a FROZEN `tan` binary actually works. Usage:
#
#   sh scripts/verify_binary.sh <path-to-binary> <path-to-alp-sdk-checkout>
#
# A binary that starts is not a binary that works. Each check below is here
# because it is a real failure mode of a PyInstaller onefile build, and every
# one of them was hit while establishing this path:
#
#   1. --version            -- import graph resolves at all. `python/tan/cli.py`
#                              imports `click.testing`, which typer 0.27 no
#                              longer pulls in, so a clean-venv build dies here
#                              with ModuleNotFoundError before printing a byte.
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

# STRUCTURAL, not a live network call. PyInstaller's onefile archive stores
# each embedded file/module's ORIGINAL NAME as plain ASCII in its TOC, right
# next to the (possibly zlib-compressed) entry it names -- grepping the raw
# executable for these names is a real proof of what got bundled, verified
# against a real freeze while writing this check (`grep -c cacert.pem
# dist/tan.exe` -> 1; `grep -o 'truststore[a-z._]*' dist/tan.exe` -> all four
# platform backends, on every OS this is built on -- `truststore/__init__.py`
# imports them unconditionally and lets `ssl` pick the live one).
#
# Chosen over `"$BIN" sdk list --online` against the real endpoint (the fix
# the #304 issue itself suggested) because it does NOT discriminate on every
# platform: measured on a real Windows freeze with NEITHER `truststore` nor
# `certifi` bundled, `sdk list --online` still SUCCEEDED -- CPython's `ssl` on
# win32 already calls `_load_windows_store_certs()` inside
# `create_default_context()`, a fallback macOS and Linux do not have, which is
# exactly why the shipped defect was a macOS asset. A live-network check on
# this platform would pass green on a build missing the fix entirely. This
# check has no such blind spot: it looks for the SAME bundled names on every
# OS, so it goes red the moment either mechanism drops out of the freeze,
# consistently, and needs no network to do it.
#
# Proves: the CA bundle `certifi.where()` resolves at runtime, and
# `truststore`'s platform backends, are physically in this archive. Does NOT
# prove: that `ssl.create_default_context()` actually verifies a real
# certificate chain at runtime, or that the endpoint is reachable -- #304 was
# reachable-but-untrusted, not unreachable, so only a live call proves THAT,
# and this check trades it for one that cannot be masked by which OS built it.
echo "== 5/5 CA trust anchors are bundled (tan-cli#304)"
grep -q "cacert.pem" "$BIN" ||
  fail "no certifi cacert.pem embedded -- check --collect-data certifi in build_binary.sh (tan-cli#304 would recur)"
grep -q "truststore" "$BIN" ||
  fail "no truststore module embedded -- tan/net.py's preferred CA mechanism is missing from this freeze (tan-cli#304 would recur)"

echo "OK: $BIN starts, carries --output, scaffolds, emits, and bundles its own CA trust anchors"
