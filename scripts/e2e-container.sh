#!/usr/bin/env bash
# Run the full e2e (scripts/e2e-full.sh) inside a PRISTINE Linux container.
#
# Usage: e2e-container.sh <frozen-onedir-tree-or-tarball> [image]
#
# WHY a container when e2e-full.sh already runs on Linux: a developer's Linux
# box is not a customer's. It has west, cmake, ninja, dtc, gperf, a Zephyr SDK
# and a populated CA store, all installed months ago for other reasons, and
# every one of them silently supplies something `tan` is supposed to provide or
# to complain about. Two release-blocking defects had survived every green
# developer-host run and showed up in the first minute inside a bare
# `ubuntu:24.04`:
#
#   * tan-cli#354 -- every HTTPS call died `CERTIFICATE_VERIFY_FAILED`. The
#     frozen binary shipped `certifi` and never used it, because `truststore`
#     CONSTRUCTS fine on a host with an empty OS trust store and only fails
#     later, at verify time, where no `except` around construction can see it.
#     Reproduced identically on the published v0.5.0-rc4 asset, so it had
#     shipped in every RC.
#   * tan-cli#355 -- a clean host was told which tools were missing and not
#     that `tan doctor --build --fix` installs them.
#
# The image gets ONLY what the quickstart tells a customer to install:
# ca-certificates, git, python3. Deliberately NO west, NO cmake/ninja/dtc/gperf,
# NO Zephyr SDK -- providing those is `tan bootstrap`'s job, and whether it does
# is the thing under test. (`python3` is here for the HARNESS, which parses
# envelopes with it; `tan` itself is a freeze and needs no system Python. Do not
# read its presence as a tan prerequisite.)
#
# The harness is BIND-MOUNTED from this checkout rather than copied in, so the
# container runs the same file CI and the developer host run -- tan-cli#358 was
# partly a second, drifted copy of it.
set -uo pipefail

FROZEN="${1:?usage: e2e-container.sh <frozen-onedir-tree-or-tarball> [image]}"
IMAGE="${2:-ubuntu:24.04}"

HERE=$(cd "$(dirname "$0")" && pwd)
HARNESS="$HERE/e2e-full.sh"
[ -f "$HARNESS" ] || { echo "ABORT: no harness at $HARNESS" >&2; exit 2; }

# Docker may need sudo, may not. Probe rather than assume: a hardcoded `sudo`
# fails on Docker Desktop and a hardcoded bare `docker` fails on a stock Linux
# install where the user is not in the `docker` group.
DOCKER="${DOCKER:-docker}"
if ! $DOCKER info >/dev/null 2>&1; then
  if sudo -n $DOCKER info >/dev/null 2>&1; then
    DOCKER="sudo $DOCKER"
  else
    echo "ABORT: cannot talk to the Docker daemon as this user, and passwordless" >&2
    echo "       sudo is unavailable. Start Docker, or add this user to the" >&2
    echo "       docker group, or set DOCKER='sudo docker' and re-run." >&2
    exit 2
  fi
fi

# Accept either shape the freeze is handed around in: the `dist/tan/` directory
# PyInstaller writes, or the `.tar.gz` the release publishes. Both end up at
# /opt/tan/lib/tan inside the container.
if [ -d "$FROZEN" ]; then
  MOUNT_SRC=$(cd "$FROZEN" && pwd)
  MOUNT_ARGS="-v $MOUNT_SRC:/frozen:ro"
  UNPACK='mkdir -p /opt/tan/lib && cp -r /frozen/. /opt/tan/lib/'
elif [ -f "$FROZEN" ]; then
  MOUNT_SRC=$(cd "$(dirname "$FROZEN")" && pwd)/$(basename "$FROZEN")
  MOUNT_ARGS="-v $MOUNT_SRC:/frozen.tar.gz:ro"
  # The published tarball has a single top-level `tan/` directory; strip it so
  # the launcher lands at a fixed path either way.
  UNPACK='mkdir -p /opt/tan/lib && tar -xzf /frozen.tar.gz -C /opt/tan/lib --strip-components=1'
else
  echo "ABORT: $FROZEN is neither a directory nor a file" >&2
  exit 2
fi

echo "=== isolated e2e: $IMAGE ==="
echo "  frozen:  $MOUNT_SRC"
echo "  harness: $HARNESS"
echo

# shellcheck disable=SC2086  # MOUNT_ARGS is deliberately word-split
$DOCKER run --rm \
  $MOUNT_ARGS \
  -v "$HARNESS:/e2e-full.sh:ro" \
  -e "ALP_SDK_REF=${ALP_SDK_REF:-dev}" \
  "$IMAGE" bash -c '
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends ca-certificates git python3 >/dev/null 2>&1

echo "=== host shape (what a customer actually has) ==="
for t in git python3 west cmake ninja dtc gperf; do
  printf "  %-8s %s\n" "$t" "$(command -v "$t" 2>/dev/null || echo ABSENT)"
done
echo "  CA store: $(ls /etc/ssl/certs/ca-certificates.crt 2>/dev/null || echo ABSENT)"
echo
'"$UNPACK"'
[ -x /opt/tan/lib/tan ] || { echo "ABORT: no launcher at /opt/tan/lib/tan after unpack" >&2; ls -la /opt/tan/lib >&2; exit 2; }
bash /e2e-full.sh /opt/tan/lib/tan /work 2>&1
'
