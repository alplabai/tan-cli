#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Submit a signed onedir tree to Apple notarization and wait (tan-cli#1506).
#
# usage: macos_notarize.sh <onedir-root>
# env:   APPLE_NOTARY_KEY_PATH   path to the App Store Connect API key (.p8)
#        APPLE_NOTARY_KEY_ID, APPLE_NOTARY_ISSUER_ID
#
# notarytool takes a zip/dmg/pkg, so the tree is zipped with ditto purely as
# the upload payload; the shipped asset is still the tar.gz built from the same
# signed bytes. There is no stapling: a ticket cannot be attached to a bare
# Mach-O or a tarball. Gatekeeper finds the ticket by the code-directory hash
# via an online lookup on first launch, which is what removes the slow
# per-Mach-O scan -- an offline first launch still falls back to the slow path.
set -euo pipefail

root=${1:?usage: macos_notarize.sh <onedir-root>}
: "${APPLE_NOTARY_KEY_PATH:?}" "${APPLE_NOTARY_KEY_ID:?}" "${APPLE_NOTARY_ISSUER_ID:?}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
zip_path="$tmp/tan-notarize.zip"
ditto -c -k --keepParent "$root" "$zip_path"

# A bounded wait: the build leg's timeout-minutes covers this step too, and
# a runner kill mid-wait would leave no diagnostic at all. `|| rc=$?` keeps
# set -e from exiting before the output and the log below are printed.
rc=0
out=$(xcrun notarytool submit "$zip_path" \
  --key "$APPLE_NOTARY_KEY_PATH" --key-id "$APPLE_NOTARY_KEY_ID" \
  --issuer "$APPLE_NOTARY_ISSUER_ID" --wait --timeout 20m --output-format json) || rc=$?
echo "$out"

field() {
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except ValueError:
    print("")
' "$1"
}
id=$(field id)
status=$(field status)
if [ "$status" != "Accepted" ]; then
  echo "ERROR: notarization status '$status' (submission $id, notarytool exit $rc)" >&2
  if [ -n "$id" ]; then
    xcrun notarytool log "$id" \
      --key "$APPLE_NOTARY_KEY_PATH" --key-id "$APPLE_NOTARY_KEY_ID" \
      --issuer "$APPLE_NOTARY_ISSUER_ID" >&2 || true
  fi
  exit 1
fi
echo "OK: notarization Accepted (submission $id)"
