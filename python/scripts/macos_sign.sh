#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Developer ID sign every Mach-O in a PyInstaller --onedir tree (tan-cli#1506).
#
# usage: macos_sign.sh <onedir-root> <entitlements.plist>
# env:   TAN_CODESIGN_IDENTITY  signing identity (SHA-1 or common name)
#        TAN_CODESIGN_KEYCHAIN  optional keychain path to search
#
# Order matters: nested code (dylibs, .so, helper executables) is signed
# first, the main `tan` executable last, so a signature never covers a
# component that is signed after it. --options runtime (hardened runtime) and
# --timestamp are required for notarization. Entitlements go on the main
# executable only: they are a property of the process, not of a library.
set -euo pipefail

root=${1:?usage: macos_sign.sh <onedir-root> <entitlements.plist>}
entitlements=${2:?usage: macos_sign.sh <onedir-root> <entitlements.plist>}
: "${TAN_CODESIGN_IDENTITY:?TAN_CODESIGN_IDENTITY is not set}"

# One codesign wrapper instead of an args array: "${arr[@]}" on an empty
# array is an unbound-variable error under set -u on macOS's bash 3.2.
sign() {
  if [ -n "${TAN_CODESIGN_KEYCHAIN:-}" ]; then
    codesign --force --options runtime --timestamp \
      --keychain "$TAN_CODESIGN_KEYCHAIN" --sign "$TAN_CODESIGN_IDENTITY" "$@"
  else
    codesign --force --options runtime --timestamp \
      --sign "$TAN_CODESIGN_IDENTITY" "$@"
  fi
}

main="$root/tan"
[ -f "$main" ] || { echo "ERROR: $main not found" >&2; exit 1; }

n=0
# Regular files only (a symlink is signed via its target); `file` identifies
# Mach-O regardless of extension. Deepest paths first.
while IFS= read -r f; do
  [ "$f" = "$main" ] && continue
  case "$(file -b "$f")" in
    *Mach-O*)
      sign "$f"
      # Fail here, naming the file, rather than at notarization.
      codesign --verify --strict "$f"
      n=$((n + 1))
      ;;
  esac
done < <(find "$root" -type f -print | awk -F/ '{print NF " " $0}' | sort -rn | cut -d' ' -f2-)

sign --entitlements "$entitlements" "$main"
n=$((n + 1))

# Fail the build on a bad signature rather than at notarization.
codesign --verify --strict --verbose=2 "$main"
echo "OK: Developer ID signed $n Mach-O file(s) under $root"
