#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Per-session lease for `tan flash --raw` (tan-cli#1457).
#
# labgrid names the holder of a place `<host>/<user>`, and every session of one user on a
# shared host has the same name, so "the place is acquired by me" does not say that THIS
# session acquired it. This helper acquires the place and records, in a 0600 lease file, a
# random nonce AND the place's labgrid `changed:` timestamp (labgrid 26.0 updates it on every
# acquire/release). The session keeps the same nonce in TAN_LEASE_NONCE. `tan flash --raw`
# refuses unless the nonce matches AND the recorded `changed:` still equals labgrid's, so a
# session that never ran `acquire` here (but shares the user), and a lease left over from an
# earlier acquisition, are both refused.
#
#   eval "$(scripts/bench/tan-lease.sh acquire e1m-aen-evk-02)"   # sets TAN_LEASE_NONCE in THIS shell
#   JLINK_RUN_PLACE=e1m-aen-evk-02 tan flash --raw ... --confirm
#   scripts/bench/tan-lease.sh release e1m-aen-evk-02
#
# The lease directory is fixed (~/.cache/alplab-leases): tan reads the same path, so an
# override here would only produce leases tan ignores.
#
# Needs labgrid-client on PATH (it is the acquiring user's own tool; tan itself runs a
# labgrid-client resolved from fixed directories). LG_COORDINATOR must be set for it.
set -euo pipefail

cmd="${1:-}"; place="${2:-}"
case "$cmd" in acquire|release) ;; *) echo "usage: $0 acquire|release <place>" >&2; exit 2 ;; esac
[[ "$place" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "tan-lease: bad place name '$place'" >&2; exit 2; }

dir="$HOME/.cache/alplab-leases"
file="$dir/$place.lease"

changed_of() { labgrid-client -p "$place" show | sed -n 's/^  changed: //p'; }

if [ "$cmd" = acquire ]; then
    [ ! -e "$file" ] || { echo "tan-lease: $file exists -- another session holds $place, or run release first" >&2; exit 1; }
    umask 077
    mkdir -p "$dir"
    chmod 700 "$dir"
    # Acquire first: a refused acquire must not leave a lease behind. If the place was
    # already acquired outside this helper, `acquire` fails here and no lease is written.
    labgrid-client -p "$place" acquire >&2
    changed="$(changed_of)"
    [ "$(printf '%s\n' "$changed" | wc -l)" = 1 ] && [ -n "$changed" ] || {
        echo "tan-lease: labgrid reports no single 'changed:' value for $place; not writing a lease tan could not bind to this acquisition" >&2
        labgrid-client -p "$place" release >&2 || true
        exit 1
    }
    nonce="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    set -o noclobber   # never overwrite a lease (also refuses a pre-planted symlink target)
    printf 'place=%s\nnonce=%s\nchanged=%s\n' "$place" "$nonce" "$changed" > "$file"
    set +o noclobber
    chmod 600 "$file"
    echo "export TAN_LEASE_NONCE=$nonce"
else
    [ -f "$file" ] || { echo "tan-lease: no lease for $place" >&2; exit 1; }
    grep -qxF "nonce=${TAN_LEASE_NONCE:-no-nonce}" "$file" \
        || { echo "tan-lease: TAN_LEASE_NONCE in this shell is not the nonce of $place's lease -- not yours to release" >&2; exit 1; }
    rm -f "$file"
    labgrid-client -p "$place" release >&2
fi
