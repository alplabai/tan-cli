# SPDX-License-Identifier: Apache-2.0
"""Actually EXECUTE release.yml's "Slice CHANGELOG section for the release
notes" step -- tan-cli#1276 review round 2 (medium).

`test_build_release_notes.py` only ever calls `build_release_notes.py`'s
functions directly, imported by path. Nothing there ties those tests to the
workflow half of the fix (`release.yml:849-860`): if the step regressed to
the old unconditional slice-then-append, or `--out` stopped matching the
`publish release` step's `body_path` (`release.yml:887`), or a flag were
mistyped, every one of those tests would still pass -- none of them run the
step, or even read the workflow at all.

This module does what `test_planner_resync_pr_step_executes.py` already
established as the house pattern for this exact gap (extract a step's `run:`
text out of the YAML with `yaml.safe_load`, same mechanism
`test_planner_resync_issue_tracking.py` uses to read the workflow, then hand
it to a real `bash` subprocess): it pulls the "Slice CHANGELOG section for
the release notes" step's `run:` text verbatim out of `release.yml` and runs
it for real, under `bash --noprofile --norc -eo pipefail` -- the exact
invocation GitHub Actions uses for a `shell: bash` step -- against a tmp tree
holding an oversized CHANGELOG section, a measured `meta/glibc-floor.txt`,
and a copy of the real `python/scripts/build_release_notes.py` at the
relative path the step invokes it from. If the workflow step's own text ever
drifts from what this asserts, this fails where `test_build_release_notes.py`
cannot.

It also asserts the file the slice step writes (`--out`) is the same file
the `publish release` step reads as `body_path` -- the other way this fix
could silently stop working: both sides agreeing does nothing if they name
different files.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
SCRIPT = REPO_ROOT / "python" / "scripts" / "build_release_notes.py"

#: tan-cli#1015's rationale applies verbatim here: every test in this module
#: drives a real `bash` subprocess, so skip the whole module (loudly) rather
#: than fail obscurely on a host with no `bash`. Windows is skipped too: there
#: `bash` on PATH can be System32's WSL launcher with no distribution installed,
#: which exits 1 before running a line, and the step it drives only ever runs
#: on the release job's `ubuntu-latest` (tan-cli#1276's parity windows shard).
pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to parse with"),
    pytest.mark.skipif(sys.platform == "win32", reason="the release step only runs on ubuntu-latest"),
]

_SLICE_STEP = "Slice CHANGELOG section for the release notes"
_PUBLISH_STEP = "publish release"

#: `action-gh-release`'s own hard ceiling on a release body, in UTF-16 code
#: units -- duplicated from `build_release_notes.RELEASE_BODY_UTF16_HARD_LIMIT`
#: deliberately: this module must not import the production script as a
#: module (it runs a COPY of it, at a different path, inside `bash`), so it
#: cannot share the constant without re-introducing the very "imported by
#: path, never executed" gap this module exists to close.
_RELEASE_BODY_UTF16_HARD_LIMIT = 125_000


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _release_steps() -> list[dict]:
    return _workflow()["jobs"]["release"]["steps"]


def _find_step(name: str) -> dict:
    step = next((s for s in _release_steps() if s.get("name") == name), None)
    assert step is not None, (
        f"no step named {name!r} found in release.yml's `release` job -- "
        f"either it was renamed (update this gate too) or removed (drop "
        f"this gate along with it)"
    )
    return step


def _slice_run() -> str:
    run = _find_step(_SLICE_STEP)["run"]
    assert isinstance(run, str) and run.strip()
    assert "${{" not in run, (
        f"the {_SLICE_STEP!r} step now contains a GHA `${{ }}` expression -- "
        f"bash cannot parse that syntax on its own (the runner resolves it "
        f"first); this test has no substitution for it yet, so a real run "
        f"here would not match what GitHub Actions actually executes"
    )
    return run


def _out_path(run: str) -> str:
    m = re.search(r"--out\s+(\S+)", run)
    assert m, f"no --out flag found in the {_SLICE_STEP!r} step's run text"
    return m.group(1)


def test_slice_step_out_matches_publish_step_body_path():
    """The file the slice step writes must be the one `publish release`
    reads -- both sides can be individually correct and the release still
    ship a stale or missing body if they ever name different files."""
    out_path = _out_path(_slice_run())
    body_path = _find_step(_PUBLISH_STEP)["with"]["body_path"]
    assert out_path == body_path, (
        f"the {_SLICE_STEP!r} step writes {out_path!r} but the "
        f"{_PUBLISH_STEP!r} step reads body_path {body_path!r} -- these "
        f"must name the same file"
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A tmp tree shaped like the step's `$GITHUB_WORKSPACE`: the real
    `build_release_notes.py` at the relative path the step invokes it from,
    plus a measured glibc floor file."""
    (tmp_path / "python" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "python" / "scripts" / "build_release_notes.py")
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "glibc-floor.txt").write_text("GLIBC_2.30\n", encoding="utf-8")
    return tmp_path


def _write_oversized_changelog(root: Path, version: str) -> None:
    # Mirrors 0.7.0's measured 770642-character section -- the actual
    # failure this fix exists to prevent, not a hypothetical one.
    oversized = "- an entry\n" * 70_000
    changelog = (
        f"# Changelog\n\n"
        f"## [{version}] -- 2026-09-19\n\n"
        f"{oversized}\n\n"
        f"## [0.6.0] -- 2026-08-04\n\n"
        f"- an older entry\n"
    )
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


def test_slice_step_keeps_the_assets_block_when_the_section_is_oversized(
    workspace: Path,
):
    """Run the REAL step body -- not a re-implementation of it -- against an
    oversized CHANGELOG section, and assert the notes it writes still carry
    the assets block, the measured glibc floor, and an absolute CHANGELOG
    URL, all under the action's hard limit."""
    version = "9.9.9"
    _write_oversized_changelog(workspace, version)
    run = _slice_run()

    env = dict(os.environ)
    env["GITHUB_REF_NAME"] = f"v{version}"

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=workspace,
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, (
        f"STEP EXIT={proc.returncode}\n--- stdout ---\n"
        f"{proc.stdout.decode('utf-8', 'replace')}\n--- stderr ---\n"
        f"{proc.stderr.decode('utf-8', 'replace')}"
    )

    body = (workspace / _out_path(run)).read_text(encoding="utf-8")

    assert "## Release assets" in body
    assert "GLIBC_2.30" in body
    assert f"https://github.com/alplabai/tan-cli/blob/v{version}/CHANGELOG.md" in body

    utf16_len = len(body.encode("utf-16-le")) // 2
    assert utf16_len < _RELEASE_BODY_UTF16_HARD_LIMIT
