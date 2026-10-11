# SPDX-License-Identifier: Apache-2.0
"""`scripts/ci/docker-pull.sh` must try the registry mirrors before Docker Hub.

Anonymous Docker Hub pulls from shared GitHub runners hit "toomanyrequests" and
ejected merge-queue entries on 2026-10-09 (#1454, #1469). Every CI `docker run`
of an official library image resolves its ref through this script, so these
tests pin the fallback order and the stdout contract (only the pulled ref)
against a fake `docker` on PATH -- no network, no daemon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "docker-pull.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="scripts/ci/docker-pull.sh is a Linux-CI artefact",
)

_IMAGE = "python@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db"


def _run(tmp_path: Path, succeed_on: str | None) -> subprocess.CompletedProcess[str]:
    """Run the script with a fake `docker` that only succeeds for refs starting with `succeed_on`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "pulls.log"
    fake = bin_dir / "docker"
    prefix = succeed_on or "\x00never"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$3" >> "{log}"\n'
        f'case "$3" in "{prefix}"*) echo pulled-ok; exit 0;; esac\n'
        'echo "toomanyrequests: You have reached your unauthenticated pull rate limit" >&2\n'
        "exit 1\n"
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_PULL_BACKOFF_S": "0"}
    return subprocess.run(
        ["bash", str(_SCRIPT), _IMAGE], capture_output=True, text=True, env=env, timeout=60, check=False
    )


def _pulls(tmp_path: Path) -> list[str]:
    return (tmp_path / "pulls.log").read_text().split()


def test_the_gcr_mirror_is_tried_first_and_its_ref_is_the_only_stdout(tmp_path: Path) -> None:
    proc = _run(tmp_path, "mirror.gcr.io/")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"mirror.gcr.io/library/{_IMAGE}\n"
    assert _pulls(tmp_path) == [f"mirror.gcr.io/library/{_IMAGE}"]


def test_a_rate_limited_mirror_falls_through_to_ecr_after_three_tries(tmp_path: Path) -> None:
    proc = _run(tmp_path, "public.ecr.aws/")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"public.ecr.aws/docker/library/{_IMAGE}\n"
    assert _pulls(tmp_path) == [f"mirror.gcr.io/library/{_IMAGE}"] * 3 + [f"public.ecr.aws/docker/library/{_IMAGE}"]


def test_docker_hub_is_the_last_resort(tmp_path: Path) -> None:
    proc = _run(tmp_path, "python@")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"{_IMAGE}\n"
    assert _pulls(tmp_path)[-1] == _IMAGE


def test_every_registry_failing_exits_non_zero_with_an_attributed_error(tmp_path: Path) -> None:
    proc = _run(tmp_path, None)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert f"::error::could not pull {_IMAGE} from any registry" in proc.stderr
    assert len(_pulls(tmp_path)) == 9


def test_the_e2e_container_harness_image_goes_through_the_mirror(tmp_path: Path) -> None:
    """tan-cli#1524: `e2e-container.sh` ran `docker run ubuntu:24.04` straight
    from Docker Hub; the image it runs must be the mirror ref docker-pull.sh
    resolved, pulled before the run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1 $2" in "image inspect") exit 1;; esac\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_PULL_BACKOFF_S": "0"}
    subprocess.run(
        ["bash", str(_REPO_ROOT / "scripts" / "e2e-container.sh"), str(frozen)],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    calls = log.read_text().splitlines()
    pull = next(i for i, c in enumerate(calls) if c.startswith("pull "))
    run = next(i for i, c in enumerate(calls) if c.startswith("run "))
    assert pull < run
    assert "mirror.gcr.io/library/ubuntu:24.04 bash -c" in calls[run]


def _fake_docker(tmp_path: Path, script: str) -> tuple[dict, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake = bin_dir / "docker"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n{script}\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_PULL_BACKOFF_S": "0"}
    return env, log


def _run_e2e(tmp_path: Path, env: dict) -> list[str]:
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    subprocess.run(
        ["bash", str(_REPO_ROOT / "scripts" / "e2e-container.sh"), str(frozen)],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    return (tmp_path / "docker.log").read_text().splitlines()


def test_the_e2e_container_uses_a_locally_cached_image_without_pulling(tmp_path: Path) -> None:
    env, _ = _fake_docker(tmp_path, "exit 0")
    calls = _run_e2e(tmp_path, env)
    assert not any(c.startswith("pull ") for c in calls), calls
    run = next(c for c in calls if c.startswith("run "))
    assert " ubuntu:24.04 bash -c" in run and "mirror.gcr.io" not in run


def test_the_e2e_container_falls_back_to_the_bare_ref_when_every_pull_fails(tmp_path: Path) -> None:
    env, _ = _fake_docker(tmp_path, 'case "$1" in image|pull) exit 1;; esac\nexit 0')
    calls = _run_e2e(tmp_path, env)
    run = next(c for c in calls if c.startswith("run "))
    assert " ubuntu:24.04 bash -c" in run and "mirror.gcr.io" not in run


def test_a_namespaced_image_skips_the_library_mirrors(tmp_path: Path) -> None:
    env, log = _fake_docker(tmp_path, "exit 0")
    proc = subprocess.run(
        ["bash", str(_REPO_ROOT / "scripts" / "ci" / "docker-pull.sh"), "ghcr.io/org/x:1"],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert proc.stdout.strip() == "ghcr.io/org/x:1"
    assert log.read_text().splitlines() == ["pull --quiet ghcr.io/org/x:1"]
