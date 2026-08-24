# SPDX-License-Identifier: Apache-2.0
"""The packaging path must stay exercised by a pull request, not only by a tag.

tan-cli#450. `release.yml` triggers on `v*` and nothing else, so for a long
while its container leg was the only place two checks ran: the packaged-binary
conformance test and the glibc floor scan. A break in either could be found
only by pushing a tag, and a tag cannot be un-pushed -- `v0.5.0` published zero
assets for exactly that reason, after tan-cli#349 switched the freeze to
`--onedir` and the floor scan kept reading `.build/tan/PKG-00.toc`.

The fix is not a new workflow, and it is Linux-only by scope decision (the
issue's own words: "One platform is enough to catch this class; the four-way
matrix can stay tag-only"). `clean-host.yml`'s `freeze-and-smoke` job already
freezes the same artifact in the same container on every pull request, so its
container step now runs the same two commands. windows/macOS packaging
conformance still first surfaces at tag time, on `release.yml`'s non-container
freeze step. This file pins the properties that make that true, each easy to
undo by accident:

1. Both legs invoke the SAME extracted scan, `scripts/glibc_floor_scan.py`, and
   neither carries an inline copy of it. A heredoc is what made the scan
   untestable off a runner in the first place.
2. `clean-host.yml` is genuinely PR-triggered and `release.yml` genuinely is
   not. That asymmetry IS the issue; a test that only checked "both files
   mention the script" would pass if someone moved the exercise back behind a
   tag.
3. The packaged-binary conformance test runs on the RIGHT step of each leg --
   both of `release.yml`'s freeze steps at tag time, only the container step
   of `clean-host.yml` at PR time -- not merely somewhere in the job.
4. Every `docker run ... bash -euc '...'` body in EVERY workflow parses as
   shell, not just the two tan-cli#450 legs: `getting-started.yml` and
   `e2e-container.yml` build the same freeze the same way and carry the same
   latent defect.

Property 4 needs saying, because it is the one nothing else in this repo can
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
#: workdir, which is `python/` in both (`-v "$PWD:/src" -w /src/python` in
#: both release.yml and clean-host.yml).
_SCAN = "scripts/glibc_floor_scan.py"

#: The conformance test that was equally tag-only.
_CONFORMANCE = "tests/conformance/test_packaged_binary.py"

#: The two legs, as (workflow, job). Named rather than discovered so that
#: DELETING one is visible here instead of quietly shrinking this gate.
_TAG_LEG = ("release.yml", "build")
_PR_LEG = ("clean-host.yml", "freeze-and-smoke")

#: A GitHub expression is not shell. Neutralised to a bare word before
#: `bash -n` sees the body, the same way the runner substitutes it.
#: Non-greedy to `}}`, not `[^}]*` -- a `[^}]*` class cannot match an
#: expression that itself contains a `}`, e.g. `${{ fromJSON('{"a":1}') }}`,
#: because the character class excludes every `}` including the closing pair.
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


@functools.lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_bodies(name: str, job: str) -> list[str]:
    return [step["run"] for step in _load(name)["jobs"][job]["steps"] if "run" in step]


def _run_body_for_condition(name: str, job: str, condition: str) -> str:
    """The `run:` body of the one step in ``job`` whose `if:` is exactly
    ``condition`` -- e.g. ``"${{ matrix.container }}"`` selects the container
    freeze step, ``"${{ !matrix.container }}"`` the non-container one. Both
    workflows fork their freeze step on this exact pair of conditions.
    """
    for step in _load(name)["jobs"][job]["steps"]:
        if step.get("if") == condition and "run" in step:
            return step["run"]
    return ""


def _docker_shell_bodies() -> list[tuple[str, str, str]]:
    """``(workflow, job, body)`` for every `run:` body that opens a
    single-quoted `bash -euc '` docker shell, across EVERY workflow file --
    not just `_TAG_LEG`/`_PR_LEG`. `getting-started.yml` and
    `e2e-container.yml` build the same freeze in the same container with the
    same `bash -euc '...'` shape, so they carry the same latent
    apostrophe-closes-the-string defect this gate exists to catch; limiting
    the sweep to the two tan-cli#450 legs would leave those two uncovered.
    """
    found: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps", []):
                body = step.get("run")
                if body and "bash -euc '" in body:
                    found.append((path.name, job_name, body))
    return found


_DOCKER_SHELL_BODIES = _docker_shell_bodies()

#: Every workflow known, at authoring time, to carry a `bash -euc '` docker
#: body with this exact single-quote shape. If the sweep stops covering one
#: of these, that must fail loudly rather than silently shrink -- the same
#: failure mode `test_no_workflow_carries_an_inline_copy_of_the_scan` guards
#: against for the scan itself.
_EXPECTED_DOCKER_SHELL_WORKFLOWS = {
    "release.yml",
    "clean-host.yml",
    "getting-started.yml",
    "e2e-container.yml",
}


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


def test_the_tag_leg_runs_conformance_on_every_freeze_step():
    # release.yml's tag-time matrix is genuinely four-way -- windows-latest,
    # both macOS runners (the non-container step) and the Linux container --
    # so the conformance test must run on BOTH of its freeze steps, not just
    # the container one.
    workflow, job = _TAG_LEG
    container_body = _run_body_for_condition(workflow, job, "${{ matrix.container }}")
    noncontainer_body = _run_body_for_condition(workflow, job, "${{ !matrix.container }}")
    assert _CONFORMANCE in container_body, (
        f"{workflow}:{job}'s container freeze step no longer runs {_CONFORMANCE}."
    )
    assert _CONFORMANCE in noncontainer_body, (
        f"{workflow}:{job}'s non-container freeze step (windows-latest / macOS) "
        f"no longer runs {_CONFORMANCE}. windows/macOS packaging conformance "
        "still only surfaces at tag time, so this is its only coverage."
    )


def test_the_pr_leg_runs_conformance_only_on_its_container_step():
    # tan-cli#450 is explicit that the PR-time exercise is Linux-only by
    # scope decision ("One platform is enough ... the four-way matrix can
    # stay tag-only"), so this must assert PER STEP, not merely that
    # _CONFORMANCE appears somewhere in the job -- a job-wide `any()` is
    # satisfied by the container-only step today and would keep passing
    # forever even if the reference were moved to the wrong step, or
    # silently duplicated into the non-container "clean venv" step (which
    # would widen PR-time coverage without anyone deciding that on purpose).
    workflow, job = _PR_LEG
    container_body = _run_body_for_condition(workflow, job, "${{ matrix.container }}")
    noncontainer_body = _run_body_for_condition(workflow, job, "${{ !matrix.container }}")
    assert _CONFORMANCE in container_body, (
        f"{workflow}:{job}'s container freeze step no longer runs {_CONFORMANCE}. "
        "It was tag-only before tan-cli#450 and must run on this PR-time leg."
    )
    assert _CONFORMANCE not in noncontainer_body, (
        f"{workflow}:{job}'s non-container freeze step now also runs "
        f"{_CONFORMANCE}. tan-cli#450 scoped the PR-time exercise to Linux "
        "only -- widening it to the clean-venv step needs a deliberate "
        "decision (and a changelog fragment), not a silent duplication."
    )


def test_no_workflow_carries_an_inline_copy_of_the_scan():
    # `GNUVerNeedSection` is the import the heredoc opened with. Its presence
    # in a workflow means the logic was pasted back in rather than called.
    #
    # `*.y*ml`, not `*.yml`: a `.yaml`-suffixed workflow would silently escape
    # a `*.yml`-only glob.
    offenders = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
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
    #
    # Asserts `"pull_request" not in triggers`, not `triggers == {"push"}`:
    # release.yml gaining `workflow_dispatch` (a normal way to re-run a failed
    # release by hand) would redden the stricter equality for a reason that
    # has nothing to do with this gate's actual concern, which is only
    # whether `pull_request` -- the thing that would make the PR leg
    # redundant -- has crept in.
    triggers = _triggers(_TAG_LEG[0])
    assert "pull_request" not in triggers, (
        f"{_TAG_LEG[0]} triggers now include pull_request ({sorted(triggers)}). "
        "This gate assumes the release workflow is not PR-triggered; if that "
        "changed deliberately, re-read whether the clean-host exercise is "
        "still what gives PRs coverage."
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to parse with")
@pytest.mark.parametrize(
    "workflow,job,body",
    _DOCKER_SHELL_BODIES,
    ids=[f"{w}:{j}" for w, j, _ in _DOCKER_SHELL_BODIES],
)
def test_every_single_quoted_docker_body_parses_as_shell(workflow, job, body):
    # All required legs ship a bash (Git Bash on windows-latest), so the
    # skipif above is a courtesy for an odd developer host, not a leg this
    # silently stops covering.
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


def test_the_docker_shell_sweep_covers_every_known_workflow():
    # Guards the sweep itself: if _docker_shell_bodies() stopped finding a
    # `bash -euc '` body in one of these files -- glob miss, a rename, the
    # shape changing -- the parametrize above would just quietly run fewer
    # cases rather than fail. That is the same silent-shrink shape
    # `test_no_workflow_carries_an_inline_copy_of_the_scan` guards against for
    # the extracted scan.
    covered = {w for w, _, _ in _DOCKER_SHELL_BODIES}
    missing = _EXPECTED_DOCKER_SHELL_WORKFLOWS - covered
    assert not missing, (
        f"{sorted(missing)} no longer carry a `bash -euc '` docker body. If "
        "that shape was removed on purpose, shrink "
        "_EXPECTED_DOCKER_SHELL_WORKFLOWS to match; if not, the sweep just "
        "lost coverage silently."
    )
