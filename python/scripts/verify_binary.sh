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
#
# POSIX sh, no bashisms: this runs under busybox in `alpine:3.20` (where the
# musl asset must be proven Python-free), under Debian's dash, under macOS bash
# 3.2 and under Git Bash on Windows.
set -eu

BIN=${1:?usage: verify_binary.sh <binary> <alp-sdk-root>}
SDK=${2:?usage: verify_binary.sh <binary> <alp-sdk-root>}

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "== 1/4 $BIN --version"
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
echo "== 2/4 generate --help carries --output"
NO_COLOR=1 "$BIN" generate --help >"$work/help.txt" || fail "generate --help exited non-zero"
esc=$(printf '\033')
if ! sed "s/${esc}\[[0-9;]*[a-zA-Z]//g" "$work/help.txt" | tr -cd 'A-Za-z0-9-' | grep -q -- "--output"; then
  echo "---- generate --help, as rendered ----" >&2
  cat "$work/help.txt" >&2
  echo "--------------------------------------" >&2
  fail "generate --help has no --output (pre-0.5 binary?)"
fi

cd "$work"

echo "== 3/4 init writes the vendored template tree"
"$BIN" init --template zephyr-app --format json >init.json || fail "init exited non-zero"
grep -q '"ok":true' init.json || fail "init envelope not ok: $(cat init.json)"
[ -f board.yaml ] || fail "init wrote no board.yaml (missing --add-data for tan/templates/vendored?)"
[ -f src/main.c ] || fail "init wrote no src/main.c"

echo "== 4/4 generate --output emits against that board.yaml"
"$BIN" generate --target zephyr-conf --output ./out/alp.conf \
  --sdk-root "$SDK" --format json >gen.json || fail "generate exited non-zero: $(cat gen.json)"
grep -q '"ok":true' gen.json || fail "generate envelope not ok: $(cat gen.json)"
[ -s ./out/alp.conf ] || fail "generate wrote no --output file"
grep -q "^CONFIG_" ./out/alp.conf || fail "emitted file carries no CONFIG_ lines"

echo "OK: $BIN starts, carries --output, scaffolds and emits"
