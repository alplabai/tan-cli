#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Per-session lease for `tan flash --raw` (tan-cli#1457).
#
# labgrid names the holder of a place `<host>/<user>`, and every session of one user on a
# shared host has the same name, so "the place is acquired by me" does not say that THIS
# session acquired it. This helper acquires the place and records a random nonce in a
# 0600 lease file; the session keeps the same nonce in TAN_LEASE_NONCE. `tan flash --raw`
# refuses unless the two match, so a session that never ran `acquire` here (but shares the
# user) is refused.
#
#   eval "$(scripts/bench/tan-lease.sh acquire e1m-aen-evk-02)"   # sets TAN_LEASE_NONCE in THIS shell
#   JLINK_RUN_PLACE=e1m-aen-evk-02 tan flash --raw ... --confirm
#   scripts/bench/tan-lease.sh release e1m-aen-evk-02
#
# Needs labgrid-client on PATH (it is the acquiring user's own tool; tan itself runs a
# labgrid-client resolved from fixed directories). LG_COORDINATOR must be set for it.
set -euo pipefail

cmd="${1:-}"; place="${2:-}"
case "$cmd" in acquire|release) ;; *) echo "usage: $0 acquire|release <place>" >&2; exit 2 ;; esac
[[ "$place" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "tan-lease: bad place name '$place'" >&2; exit 2; }

dir="${TAN_LEASE_DIR:-$HOME/.cache/alplab-leases}"
file="$dir/$place.lease"

if [ "$cmd" = acquire ]; then
    [ ! -e "$file" ] || { echo "tan-lease: $file exists -- another session holds $place, or run release first" >&2; exit 1; }
    umask 077
    mkdir -p "$dir"
    chmod 700 "$dir"
    # Acquire first: a refused acquire must not leave a lease behind.
    labgrid-client -p "$place" acquire >&2
    nonce="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf 'place=%s\nnonce=%s\n' "$place" "$nonce" > "$file"
    chmod 600 "$file"
    echo "export TAN_LEASE_NONCE=$nonce"
else
    [ -f "$file" ] || { echo "tan-lease: no lease for $place" >&2; exit 1; }
    grep -qx "nonce=${TAN_LEASE_NONCE:-no-nonce}" "$file" \
        || { echo "tan-lease: TAN_LEASE_NONCE in this shell is not the nonce of $place's lease -- not yours to release" >&2; exit 1; }
    rm -f "$file"
    labgrid-client -p "$place" release >&2
fi
