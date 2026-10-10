#!/usr/bin/env bash
# Usage: docker-pull.sh IMAGE
# Pulls an official Docker Hub library IMAGE (e.g. python:3.12-slim-bullseye,
# or python@sha256:...) through a registry mirror first and prints the ref it
# was pulled from on stdout, for the caller's `docker run`.
#
# Anonymous Docker Hub pulls from shared GitHub runners hit "toomanyrequests"
# and ejected merge-queue entries on 2026-10-09. mirror.gcr.io and
# public.ecr.aws serve the same library images without that limit; Docker Hub
# itself stays the last resort. A digest-pinned IMAGE yields identical bytes
# from whichever registry serves it.
# DOCKER_PULL_BACKOFF_S scales the retry sleep (tests set it to 0).
set -euo pipefail
image="${1:?usage: docker-pull.sh IMAGE}"
for ref in "mirror.gcr.io/library/${image}" \
           "public.ecr.aws/docker/library/${image}" \
           "${image}"; do
  for attempt in 1 2 3; do
    if docker pull --quiet "$ref" >&2; then
      echo "pulled ${ref}" >&2
      printf '%s\n' "$ref"
      exit 0
    fi
    sleep $((attempt * ${DOCKER_PULL_BACKOFF_S:-15}))
  done
done
echo "::error::could not pull ${image} from any registry" >&2
exit 1
