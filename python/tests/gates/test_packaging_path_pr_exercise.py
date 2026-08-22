# SPDX-License-Identifier: Apache-2.0
"""The packaging path must stay exercised by a pull request, not only by a tag.

tan-cli#450. `release.yml` triggers on `v*` and nothing else, so for a long
while its container leg was the only place two checks ran: the packaged-binary
conformance test and the glibc floor scan. A break in either could be found
only by pushing a tag, and a tag cannot be un-pushed -- `v0.5.0` published zero
assets for exactly that reason, after tan-cli#349 switched the freeze to
`--onedir` and the floor scan kept reading `.build/tan/PKG-00.toc`.

The fix is not a new workflow. `clean-host.yml`'s `freeze-and-smoke` job
already freezes the same artifact in the same container on every pull request,
so it now runs the same two commands. This file pins the three properties that
makes true, each easy to undo by accident:

1. Both legs invoke the SAME extracted scan, `scripts/glibc_floor_scan.py`, and
   neither carries an inline copy of it. A heredoc is what made the scan
   untestable off a runner in the first place.
2. `clean-host.yml` is genuinely PR-triggered and `release.yml` genuinely is
   not. That asymmetry IS the issue; a test that only checked "both files
   mention the script" would pass if someone moved the exercise back behind a
   tag.
3. Every `docker run ... bash -euc '...'` body in the two workflows parses as
   shell.

Property 3 needs saying, because it is the one nothing else in this repo can
see. Those bodies are single-quoted shell inside a YAML scalar: an apostrophe
in a comment closes the string, `yaml.safe_load` still parses the file, every
linter still passes, and the job dies at runtime -- on a tag, for `release.yml`.
That is the same "only a spent tag finds this" shape the issue is about, so it
belongs in the same gate. One apostrophe IS escaped, the hard way, in
`release.yml`'s binutils comment; `bash -n` accepts that and rejects a bare
one, which is exactly the discrimination wanted.

Nothing here runs a workflow, a container, or a freeze. It reads YAML and
shells `bash -n`, so a regression lands in `tests/gates` in seconds rather than
on a runner months later.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The extracted scan, as both legs must name it. Relative to the container
#: workdir, which is `python/` in both (`-w /src/python` in release.yml,
#: `-v "$PWD/python:/src" -w /src` in clean-host.yml).
_SCAN = "scripts/glibc_floor_scan.py"

#: The conformance test that was equally tag-only.
_CONFORMANCE = "tests/conformance/test_packaged_binary.py"

#: The two legs, as (workflow, job). Named rather than discovered so that
#: DELETING one is visible here instead of quietly shrinking this gate.
_TAG_LEG = ("release.yml", "build")
_PR_LEG = ("clean-host.yml", "freeze-and-smoke")

#: A GitHub expression is not shell. Neutralised to a bare word before
#: `bash -n` sees the body, the same way the runner substitutes it.
_EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")


@functools.lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_bodies(name: str, job: str) -> list[str]:
    return [step["run"] for step in _load(name)["jobs"][job]["steps"] if "run" in step]


def _triggers(name: str) -> set[str]:
    # `on` is YAML 1.1 truthy, so PyYAML hands back the bool True as the key.
    workflow = _load(name)
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on}
    return set(on)


@pytest.mark.parametrize("workflow,job", [_TAG_LEG, _PR_LEG])
def test_both_legs_invoke_the_extracted_scan_by_name(workflow, job):
    bodies = _run_bodies(workflow, job)
    assert any(_SCAN in body for body in bodies), (
        f"{workflow}:{job} no longer invokes {_SCAN}. Both the tag leg and the "
        "PR leg must run the SAME scan -- that is the only thing keeping them "
        "from drifting (tan-cli#450)."
    )


@pytest.mark.parametrize("workflow,job", [_TAG_LEG, _PR_LEG])
def test_both_legs_run_the_packaged_binary_conformance_test(workflow, job):
    bodies = _run_bodies(workflow, job)
    assert any(_CONFORMANCE in body for body in bodies), (
        f"{workflow}:{job} no longer runs {_CONFORMANCE}. It was tag-only "
        "before tan-cli#450 and must stay on both legs."
    )


def test_no_workflow_carries_an_inline_copy_of_the_scan():
    # `GNUVerNeedSection` is the import the heredoc opened with. Its presence
    # in a workflow means the logic was pasted back in rather than called.
    offenders = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if "GNUVerNeedSection" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} inline the floor scan again. It lives in python/{_SCAN} "
        "so the PR leg and the tag leg cannot diverge, and so its refusal is "
        "unit-testable off a runner (tan-cli#450)."
    )


def test_the_pr_leg_is_actually_pull_request_triggered():
    assert "pull_request" in _triggers(_PR_LEG[0]), (
        f"{_PR_LEG[0]} no longer runs on pull_request, so the packaging path is "
        "back to being tag-only -- the whole subject of tan-cli#450."
    )


def test_the_tag_leg_is_still_tag_only():
    # Not a nice-to-have: if release.yml ever gained `pull_request`, the PR leg
    # above would look redundant, someone would delete it, and the coverage
    # would then vanish the next time release.yml narrowed again.
    triggers = _triggers(_TAG_LEG[0])
    assert triggers == {"push"}, (
        f"{_TAG_LEG[0]} triggers changed to {sorted(triggers)}. This gate assumes "
        "the release workflow is tag-only; if that changed deliberately, re-read "
        "whether the clean-host exercise is still what gives PRs coverage."
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to parse with")
@pytest.mark.parametrize("workflow,job", [_TAG_LEG, _PR_LEG])
def test_every_single_quoted_docker_body_parses_as_shell(workflow, job):
    # All three required legs ship a bash (Git Bash on windows-latest), so the
    # skipif above is a courtesy for an odd developer host, not a leg this
    # silently stops covering.
    checked = 0
    for body in _run_bodies(workflow, job):
        if "bash -euc '" not in body:
            continue
        checked += 1
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(_EXPRESSION.sub("EXPRESSION", body))
            script = Path(handle.name)
        try:
            # `.as_posix()`, not `str()`: on windows-latest this is Git Bash,
            # and handing it a `C:\\Users\\...` path is the kind of thing that
            # is green on macOS and red on the one required leg that matters.
            result = subprocess.run(
                ["bash", "-n", script.as_posix()], capture_output=True, text=True
            )
        finally:
            script.unlink()
        assert result.returncode == 0, (
            f"{workflow}:{job} has a `bash -euc '...'` body that does not parse:\n"
            f"{result.stderr}\n"
            "Most likely an apostrophe in a comment inside the single-quoted "
            "string. YAML parses it and every linter passes; the job dies at "
            "runtime, on a tag for release.yml."
        )
    assert checked, (
        f"{workflow}:{job} no longer has a single-quoted docker body. If the "
        "container leg was restructured, re-point this gate rather than letting "
        "it pass vacuously."
    )
