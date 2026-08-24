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
   untestable off a runner in the first place. Matched against COMMAND lines
   only, comments stripped first -- `release.yml` also mentions both the scan
   and the conformance test in its own prose comments, which would satisfy a
   naive substring check even after the real invocation was deleted.
2. `clean-host.yml` is genuinely PR-triggered and `release.yml` genuinely is
   not. That asymmetry IS the issue; a test that only checked "both files
   mention the script" would pass if someone moved the exercise back behind a
   tag.
3. The packaged-binary conformance test runs on the RIGHT step of each leg --
   both of `release.yml`'s freeze steps at tag time, only the container step
   of `clean-host.yml` at PR time -- not merely somewhere in the job, and not
   satisfied by a step that no longer exists (a missing step fails loudly here
   rather than passing vacuously).
4. Every single-quoted docker shell argument in EVERY workflow parses as
   shell, not just the two tan-cli#450 legs: `getting-started.yml`,
   `e2e-container.yml` and `python-binaries.yml` build the same freeze the
   same way (`bash -euc '...'`, `sh -c '...'`, `bash -c '...'`) and carry the
   same latent defect. Each occurrence is sliced down to just the quoted
   argument itself before being handed to `bash -n` -- checking the WHOLE
   `run:` body cannot tell a genuinely unbalanced apostrophe planted inside
   the docker string from one that gets accidentally rebalanced by unrelated
   quoting later in the same step. Measured: getting-started.yml's step
   continues past its docker body into a `cat >... <<'LAUNCHER'` heredoc that
   supplies 21 more apostrophes, enough to make the WHOLE-body quote count
   come out even regardless of what is broken inside the docker argument, so
   `bash -n` on the whole body missed a planted defect there that it caught
   in the other three files.

Property 4 needs saying, because it is the one nothing else in this repo can
see. Those bodies are single-quoted shell inside a YAML scalar: an apostrophe
in a comment closes the string, `yaml.safe_load` still parses the file, every
linter still passes, and the job dies at runtime -- on a tag, for `release.yml`.
That is the same "only a spent tag finds this" shape the issue is about, so it
belongs in the same gate. One apostrophe IS escaped, the hard way, in
`release.yml`'s binutils comment (`'"'"'binutils'"'"'`, the close/reopen trick
for embedding a literal apostrophe in a single-quoted string) and
python-binaries.yml splices a variable the same way (`'"$BUILD_DEPS"'`);
`bash -n` accepts both and rejects a bare one, which is exactly the
discrimination wanted.

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

    :raises LookupError: if no step in ``job`` carries that `if:` -- a
        MISSING step must fail this gate loudly, not hand back an empty
        string that a subsequent ``not in`` assertion would then pass
        vacuously.
    """
    for step in _load(name)["jobs"][job]["steps"]:
        if step.get("if") == condition and "run" in step:
            return step["run"]
    raise LookupError(
        f"{name}:{job} has no step with `if: {condition}` -- either that step "
        "was deleted/renamed or its condition changed. Either way this gate "
        "cannot verify what it exists to verify without it."
    )


def _strip_comment_lines(body: str) -> str:
    """Drop every line that is only a `#` shell comment (after leading
    whitespace), so a reference that exists ONLY in prose cannot satisfy a
    substring check meant to prove a real command still runs. `release.yml`
    talks about both `_SCAN` and `_CONFORMANCE` at length in comments right
    next to the commands that invoke them -- deleting the real invocation
    while leaving the comment behind must not look like coverage.
    """
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


#: Matches the shell invocation that opens a single-quoted docker script
#: argument: `bash -euc '` (release.yml, clean-host.yml, getting-started.yml,
#: e2e-container.yml) as well as `sh -c '` / `bash -c '`
#: (python-binaries.yml's musl and glibc legs). All of these carry the
#: identical single-quote-inside-a-YAML-scalar apostrophe defect.
_DOCKER_MARKER = re.compile(r"\b(?:ba)?sh -[a-z]*c '")

#: A line that is JUST a closing quote (whitespace either side). This repo's
#: `bash -euc '...'` bodies always place their closing quote alone on its own
#: line -- the structural signal `_docker_shell_snippet` uses to find where
#: the argument really ends, deliberately NOT by counting/toggling quote
#: characters forward from the opening one. Counting forward cannot tell a
#: genuinely broken apostrophe from a legitimate one: bash itself would treat
#: EITHER as ending the string right there, so a scanner that just asks "what
#: would bash consider the next real terminator" always finds *a* valid
#: answer and can never observe the defect. A structural anchor -- a fact
#: about physical layout, independent of whether the quoting inside is
#: correct -- does not have that blind spot: a `# don't`-shaped apostrophe
#: never lands alone on its own line, so it can never be mistaken for this.
_STANDALONE_QUOTE_LINE = re.compile(r"^[ \t]*'[ \t]*$", re.MULTILINE)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _docker_shell_snippet(body: str, match: re.Match[str]) -> str | None:
    """The text of the single-quoted docker shell argument ``match`` opens,
    from `sh -c '`/`bash -c '`/`bash -euc '` through its OWN closing quote --
    not the whole `run:` body, and not merely "the next quote bash would
    honour" (see `_STANDALONE_QUOTE_LINE`'s docstring for why that always
    finds a spuriously-valid answer).

    Two structural signals, tried in order:

    1. The FIRST standalone closing-quote line after the marker. Covers
       `bash -euc '...'` (release.yml, clean-host.yml, getting-started.yml,
       e2e-container.yml), where the closing quote always sits alone on its
       own line by this repo's convention -- and is immune to a `'` planted
       mid-line elsewhere in the block, and to the trailing content some of
       these steps carry AFTER the closing quote (getting-started.yml's
       `cat >... <<'LAUNCHER'` heredoc, whose own apostrophes are excluded by
       construction since this never looks past the first standalone line).
    2. A fallback for python-binaries.yml's shape, which has no standalone
       quote line -- its closing quote is the LAST character of its LAST
       content line, immediately followed by a dedent (`else`/`fi`). Bounded
       by INDENTATION: every line more deeply indented than the marker's own
       line belongs to this docker block; the first line back at the
       marker's indentation or shallower ends it. Within that block, the
       closing quote is the LAST line (scanned forward, so the true final
       line wins over the close/reopen splice trick's own `'`-ending line,
       e.g. `'"$BUILD_DEPS"'`) whose content ends with `'`. A `'` planted
       mid-line (`# don't`) does not itself END its line, so it is not
       mistaken for this either.
    """
    start = match.end()
    close = _STANDALONE_QUOTE_LINE.search(body, start)
    if close:
        return body[match.start() : close.end()]

    lines = body.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    marker_line_idx = next(
        idx for idx, off in enumerate(offsets) if off <= match.start() < off + len(lines[idx])
    )
    marker_indent = _indent(lines[marker_line_idx])
    close_line_idx = None
    for idx in range(marker_line_idx + 1, len(lines)):
        line = lines[idx]
        if line.strip() == "":
            continue
        if _indent(line) <= marker_indent:
            break
        if line.rstrip("\n").rstrip().endswith("'"):
            close_line_idx = idx
    if close_line_idx is None:
        return None
    end = offsets[close_line_idx] + len(lines[close_line_idx].rstrip("\n").rstrip())
    return body[match.start() : end]


def _docker_shell_snippets() -> list[tuple[str, str, str]]:
    """``(workflow, job, snippet)`` for every single-quoted docker shell
    argument across EVERY workflow file, sliced down to just the argument
    itself -- not the whole `run:` body. See `_docker_shell_snippet`'s
    docstring for why the whole body is the wrong thing to check and how the
    boundary is found instead.

    A `run:` body may open more than one such argument (python-binaries.yml's
    `build` step has both a musl `sh -c '...'` leg and a glibc
    `bash -c '...'` leg in one step) -- every occurrence is its own entry.
    """
    found: list[tuple[str, str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps", []):
                body = step.get("run")
                if not body:
                    continue
                for match in _DOCKER_MARKER.finditer(body):
                    snippet = _docker_shell_snippet(body, match)
                    if snippet is not None:
                        found.append((path.name, job_name, snippet))
    return found


_DOCKER_SHELL_SNIPPETS = _docker_shell_snippets()

#: Every workflow known, at authoring time, to carry a single-quoted docker
#: shell argument with this shape. If the sweep stops covering one of these,
#: that must fail loudly rather than silently shrink -- the same failure
#: mode `test_no_workflow_carries_an_inline_copy_of_the_scan` guards against
#: for the scan itself.
_EXPECTED_DOCKER_SHELL_WORKFLOWS = {
    "release.yml",
    "clean-host.yml",
    "getting-started.yml",
    "e2e-container.yml",
    "python-binaries.yml",
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
    # Comments stripped first: release.yml's container step names `_SCAN` in
    # a prose comment ("tan-cli#450: the scan is scripts/glibc_floor_scan.py,
    # not a heredoc...") right next to the real invocation -- deleting the
    # real `python scripts/glibc_floor_scan.py` line and leaving that comment
    # behind must not still look like coverage.
    bodies = [_strip_comment_lines(b) for b in _run_bodies(workflow, job)]
    assert any(_SCAN in body for body in bodies), (
        f"{workflow}:{job} no longer invokes {_SCAN}. Both the tag leg and the "
        "PR leg must run the SAME scan -- that is the only thing keeping them "
        "from drifting (tan-cli#450)."
    )


def test_the_tag_leg_runs_conformance_on_every_freeze_step():
    # release.yml's tag-time matrix is genuinely four-way -- windows-latest,
    # both macOS runners (the non-container step) and the Linux container --
    # so the conformance test must run on BOTH of its freeze steps, not just
    # the container one. Comments stripped first: the non-container step's
    # own prose ("pytest AFTER the freeze ... tests/conformance/
    # test_packaged_binary.py is the extension's own acceptance test...")
    # names _CONFORMANCE right next to the real `pytest` invocation, so an
    # unstripped substring check is satisfied by the comment alone even after
    # the real command is deleted.
    workflow, job = _TAG_LEG
    container_body = _strip_comment_lines(
        _run_body_for_condition(workflow, job, "${{ matrix.container }}")
    )
    noncontainer_body = _strip_comment_lines(
        _run_body_for_condition(workflow, job, "${{ !matrix.container }}")
    )
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
    container_body = _strip_comment_lines(
        _run_body_for_condition(workflow, job, "${{ matrix.container }}")
    )
    noncontainer_body = _strip_comment_lines(
        _run_body_for_condition(workflow, job, "${{ !matrix.container }}")
    )
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
    "workflow,job,snippet",
    _DOCKER_SHELL_SNIPPETS,
    ids=[f"{w}:{j}:{i}" for i, (w, j, _) in enumerate(_DOCKER_SHELL_SNIPPETS)],
)
def test_every_single_quoted_docker_body_parses_as_shell(workflow, job, snippet):
    # All required legs ship a bash (Git Bash on windows-latest), so the
    # skipif above is a courtesy for an odd developer host, not a leg this
    # silently stops covering.
    #
    # `snippet` is already sliced to just the docker shell argument -- see
    # `_docker_shell_snippets`'s docstring for why the whole `run:` body is
    # the wrong thing to feed `bash -n`.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(_EXPRESSION.sub("EXPRESSION", snippet))
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
        f"{workflow}:{job} has a docker shell argument that does not parse:\n"
        f"{result.stderr}\n"
        "Most likely an apostrophe in a comment inside the single-quoted "
        "string. YAML parses it and every linter passes; the job dies at "
        "runtime, on a tag for release.yml."
    )


def test_the_docker_shell_sweep_covers_every_known_workflow():
    # Guards the sweep itself: if _docker_shell_snippets() stopped finding a
    # single-quoted docker argument in one of these files -- glob miss, a
    # rename, the shape changing -- the parametrize above would just quietly
    # run fewer cases rather than fail. That is the same silent-shrink shape
    # `test_no_workflow_carries_an_inline_copy_of_the_scan` guards against for
    # the extracted scan.
    covered = {w for w, _, _ in _DOCKER_SHELL_SNIPPETS}
    missing = _EXPECTED_DOCKER_SHELL_WORKFLOWS - covered
    assert not missing, (
        f"{sorted(missing)} no longer carry a single-quoted docker shell "
        "argument. If that shape was removed on purpose, shrink "
        "_EXPECTED_DOCKER_SHELL_WORKFLOWS to match; if not, the sweep just "
        "lost coverage silently."
    )
