# SPDX-License-Identifier: Apache-2.0
"""The interpreter contract: two ends of `requires-python`, both tested.

tan-cli#1126. `pyproject.toml` declares `requires-python = ">=3.12"` -- an
OPEN-ENDED promise -- and this repo keeps it by testing BOTH ends deliberately:

* the **floor** (`"3.12"`) is pinned by every job that is not named below. It
  is the oldest interpreter a customer may install `alp-tan` on, and it is what
  Zephyr's own `python.cmake` floor forces (`tan build` bakes the resolved
  interpreter into every slice it configures).
* the **ceiling** (`"3.x"` -- setup-python's newest available CPython, 3.14.7
  at the time of writing) is floated by exactly two jobs:
  `parity.yml`'s `seam1-plan-shape` and `ci.yml`'s `python-newest`.

## Why the spread stays, rather than pinning everything to the floor

It has caught two real defects that a uniform `"3.12"` would have shipped:

* **tan-cli#1116** -- a `boards_dir.is_dir()` pre-flight in
  `new_som_cmd._known_board_names` that was load-bearing on 3.12 (it raised
  `PermissionError`) and DEAD on 3.14 (`is_dir()` returns `False` there for the
  same shape), i.e. silently wrong in the opposite direction. Corrected in
  tan-cli#1127/#1129.
* **tan-cli#1126** -- two `tests/core/test_uri_reference.py` tests that had
  encoded `ntpath.isabs`'s pre-3.13 answer for a rooted-but-driveless Windows
  path as a universal stdlib fact.

Three measured `pathlib`/`ntpath` behaviour differences now exist across the
supported range (`_IGNORED_ERRNOS`, `Path.resolve(strict=False)` on a symlink
loop, `ntpath.isabs`), and none of the three is guessable from the others --
each needed a real interpreter to settle. Pinning the ceiling away would not
make them stop existing; it would only move the discovery to a customer.

The COST is stated where it is paid (`ci.yml`'s `python-newest` comment): a new
CPython minor release moves `"3.x"` under an unrelated PR and can red it. That
is the report doing its job.

## What this gate actually enforces, and why each half exists

The spread on its own was NOT enough, and that is the defect tan-cli#1126
found. `seam1-plan-shape` was the ONLY job floating `"3.x"`, and it runs
`tests/gates` alone -- so the full suite sat red on 3.14.7 for two tests while
all five required contexts stayed green. A check that runs, is required, and
cannot report the thing it would catch is exactly the shape this milestone
spent a week clearing. `ci.yml`'s `python-newest` job is the closure: same
floating `"3.x"`, the WHOLE suite.

So four invariants, each guarding a way that could quietly come undone:

1. every `actions/setup-python` step declares a `python-version` at all (an
   omitted one takes the runner image's default -- a third, undeclared,
   silently-moving interpreter);
2. every declared value is either the floor or the ceiling, and the floor
   STRING equals `requires-python`'s own floor (so raising `requires-python`
   cannot land without moving the workflows in the same change);
3. the set of jobs floating the ceiling is EXACTLY the two named below --
   catching both "someone pinned `python-newest` to make it quiet" and
   "someone floated a job that should be measuring the floor";
4. `python-newest` still selects the WHOLE suite. Narrowing it to a subset
   (`tests/gates`, a `-k`, an `--ignore`) reproduces the original defect
   precisely, and would otherwise be invisible -- the job would still exist,
   still float, still be green.

Nothing here runs a workflow or an interpreter: it reads the YAML and the
`pyproject.toml`, so a drift fails in `tests/gates` rather than on whichever
runner happens to notice months later.
"""

from __future__ import annotations

import functools
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "python" / "pyproject.toml"

#: setup-python's "newest CPython you can serve me" spec. A literal, not a
#: pattern: `"3"` and `"3.x"` and `">=3.12"` all mean subtly different things
#: to that action, and only one of them is this repo's ceiling.
CEILING = "3.x"

#: The `(workflow file, job id)` pairs allowed -- and required -- to float the
#: ceiling. Named, not derived, so that DELETING either one is a failure here
#: rather than a silent shrink of what the policy covers (the same reason
#: `test_parity_workflow_concurrency_and_timeouts.py` keeps `_PARITY_JOBS` as a
#: literal tuple).
#:
#: The two are not interchangeable and both are required:
#:   * `seam1-plan-shape` floats it against `tests/gates` plus the SDK emit
#:     parity steps -- it is where the ceiling first got exercised at all;
#:   * `python-newest` floats it against the FULL suite -- it is the half that
#:     can actually report a divergence (tan-cli#1126).
FLOATING_JOBS = {
    ("parity.yml", "seam1-plan-shape"),
    ("ci.yml", "python-newest"),
}

#: The job that must run the whole suite on the ceiling, and the exact command
#: it must run. Byte-compared against `ci.yml`, deliberately: every way of
#: narrowing this (`--ignore=`, `-k`, `tests/gates`, a shard flag) changes this
#: string, and every one of them restores the tan-cli#1126 blind spot.
FULL_SUITE_JOB = "python-newest"
FULL_SUITE_COMMAND = "python -m pytest tests -q"

#: The marker each floating pin's own file must carry near it. The acceptance
#: criterion tan-cli#1126 wrote was not only "decide the policy" but "make it
#: legible where someone editing parity.yml will see it" -- before that change
#: the difference between `"3.x"` and `"3.12"` was a one-character detail in
#: one file with nothing saying it was load-bearing. A prose comment nothing
#: checks is a comment that gets deleted in a tidy-up, so its presence is
#: asserted here.
POLICY_MARKER = "INTERPRETER POLICY"

#: How far above a floating pin the marker may sit. Wide enough for the real
#: comment block (~30 lines) plus the `uses:`/`with:` lines between them, tight
#: enough that a marker elsewhere in a 3000-line workflow does not count.
POLICY_MARKER_WINDOW = 45


@functools.cache
def _floor() -> str:
    """The `requires-python` floor as setup-python would spell it (`"3.12"`).

    DERIVED, never hardcoded -- a second copy of `3.12` in this file is a
    second place to update, and the pair going stale is the same defect class
    as the untested bound. `tomllib` rather than a grep so reformatting
    `pyproject.toml` cannot silently stop matching.
    """
    spec = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"]
    match = re.search(r">=\s*([0-9]+\.[0-9]+)", spec)
    assert match, f"requires-python is {spec!r} -- no `>=X.Y` floor to derive the workflow pin from"
    return match.group(1)


@functools.cache
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow_files() -> list[Path]:
    """`.yml` AND `.yaml` -- GitHub accepts both, so a glob scoped to one
    extension gives a future `foo.yaml` zero coverage from a gate whose whole
    premise is that no interpreter goes undeclared."""
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


@functools.cache
def _setup_python_steps() -> tuple[tuple[str, str, dict], ...]:
    """Every `actions/setup-python` step in the repo as
    `(workflow file name, job id, the step dict)`.

    A `uses:` reusable-workflow caller has no `steps:` of its own; its
    interpreter lives in the CALLED workflow, which this same walk reads in its
    own right.
    """
    found: list[tuple[str, str, dict]] = []
    for path in _workflow_files():
        workflow = _load(path)
        for job_id, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                uses = str(step.get("uses", ""))
                if uses.startswith("actions/setup-python"):
                    found.append((path.name, job_id, step))
    return tuple(found)


def _pins() -> list[tuple[str, str, str]]:
    return [
        (workflow, job, str((step.get("with") or {}).get("python-version")))
        for workflow, job, step in _setup_python_steps()
    ]


def test_the_walk_finds_the_interpreters_it_claims_to_police():
    """Non-vacuity. Every assertion below is a loop over `_setup_python_steps
    ()`, so a walk that silently found nothing -- a renamed action, a
    workflow directory that moved, a YAML shape this parse stopped
    understanding -- would pass all of them while checking no interpreter at
    all. Bounds, not an exact count: the number grows with the workflow set
    and pinning it would turn every new job into a red build here.
    """
    steps = _setup_python_steps()
    assert len(steps) >= 20, (
        f"only {len(steps)} actions/setup-python steps found across "
        f"{len(_workflow_files())} workflow files -- this gate walks that list to "
        "decide everything below, so finding (almost) none means it is asserting "
        "nothing, not that the repo is clean"
    )
    assert {workflow for workflow, _, _ in steps} >= {"ci.yml", "parity.yml"}


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(workflow, job) for workflow, job, _ in _setup_python_steps()],
)
def test_every_setup_python_declares_a_version(workflow: str, job: str):
    """An omitted `python-version` is not "the floor" -- it is the runner
    image's own default, which moves on GitHub's schedule and is declared
    nowhere. That is a third interpreter, silently, in a repo whose whole
    contract here is that there are exactly two.
    """
    steps = [step for wf, jb, step in _setup_python_steps() if (wf, jb) == (workflow, job)]
    for step in steps:
        version = (step.get("with") or {}).get("python-version")
        assert version, (
            f"{workflow}:{job} uses actions/setup-python with no python-version -- "
            "it would silently take the runner image's default. Pin the floor "
            f"({_floor()!r}) or, if this job is meant to measure the ceiling, "
            f"float {CEILING!r} and add it to FLOATING_JOBS in this file."
        )


def test_every_declared_version_is_the_floor_or_the_ceiling():
    """Two values, no third. A `"3.13"` pinned somewhere for a one-off reason
    is a version this repo neither promises nor measures anywhere else, and it
    would drift out of the policy the moment either end moved.
    """
    allowed = {_floor(), CEILING}
    wrong = [(wf, job, version) for wf, job, version in _pins() if version not in allowed]
    assert not wrong, (
        f"python-version values outside the policy {sorted(allowed)}: {wrong}. "
        "The floor is derived from python/pyproject.toml's requires-python; the "
        f"ceiling is {CEILING!r}. A third value needs a reason recorded in this "
        "file, not a quiet pin in a workflow."
    )


def test_the_floor_pin_tracks_requires_python():
    """The floor is a STRING in 20-odd workflow files and a spec in one
    `pyproject.toml`, and nothing but this test couples them. Raising
    `requires-python` to `>=3.13` without moving the pins would leave every job
    testing an interpreter the package no longer claims to support -- green,
    and measuring the wrong thing.
    """
    floor = _floor()
    pinned = {version for _, _, version in _pins() if version != CEILING}
    assert pinned == {floor}, (
        f"workflows pin {sorted(pinned)} as the floor; python/pyproject.toml "
        f"declares {floor!r}. Move them in the same change."
    )


def test_exactly_the_named_jobs_float_the_ceiling():
    """Both directions.

    A job floating the ceiling that is not named here is measuring something
    nobody decided it should measure. And a NAMED job that has stopped floating
    is the failure mode this whole issue is about: pinning `python-newest` to
    `"3.12"` is the single edit that makes the newest interpreter untested
    again while leaving every job, name and required context in place.
    """
    floating = {(wf, job) for wf, job, version in _pins() if version == CEILING}
    assert floating == FLOATING_JOBS, (
        f"jobs floating {CEILING!r} are {sorted(floating)}; the policy names "
        f"{sorted(FLOATING_JOBS)}. Missing means the ceiling lost coverage; extra "
        "means a job started measuring it without a decision. Either way, edit "
        "FLOATING_JOBS in this file and say why in the same change."
    )


def test_the_ceiling_job_still_runs_the_whole_suite():
    """The acceptance criterion, made mechanical.

    tan-cli#1126's defect was NOT "no job used 3.14" -- one did. It was that
    the only job using it ran `tests/gates`, a slice that could not contain the
    failures. Narrowing `python-newest`'s selection would restore that exactly,
    and would look like a harmless speed-up in review: the job still exists,
    still floats the ceiling, still reports green.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"][FULL_SUITE_JOB]
    pytest_steps = [
        step
        for step in job["steps"]
        if "pytest" in str(step.get("run", "")) and step.get("name") == "pytest"
    ]
    assert len(pytest_steps) == 1, (
        f"ci.yml:{FULL_SUITE_JOB} has {len(pytest_steps)} steps named 'pytest' -- "
        "this gate reads exactly one to check the selection"
    )
    command = pytest_steps[0]["run"].strip()
    assert command == FULL_SUITE_COMMAND, (
        f"ci.yml:{FULL_SUITE_JOB}'s pytest step runs {command!r}, not "
        f"{FULL_SUITE_COMMAND!r}. Any narrowing (--ignore, -k, a subdirectory, a "
        "shard flag) puts the newest interpreter back where tan-cli#1126 found "
        "it: exercised by a slice too small to contain the divergence."
    )
    assert pytest_steps[0].get("working-directory") == "python", (
        f"ci.yml:{FULL_SUITE_JOB}'s pytest step must run from `python/` -- "
        "`tests` resolves to nothing from the repo root, and pytest exits 4 "
        "(usage error), which is not the same as a green suite but is easy to "
        "mistake for one in a log"
    )


@pytest.mark.parametrize(("workflow", "job"), sorted(FLOATING_JOBS))
def test_each_floating_pin_carries_the_policy_note(workflow: str, job: str):
    """"Make it legible" was an acceptance criterion in its own right, because
    before this change the entire difference between a job that tests the
    ceiling and one that tests the floor was one character, in one file, with
    nothing next to it saying so. Prose nothing checks is prose a tidy-up
    deletes.
    """
    lines = (WORKFLOWS / workflow).read_text(encoding="utf-8").splitlines()
    pin_lines = [i for i, line in enumerate(lines) if f'python-version: "{CEILING}"' in line]
    assert pin_lines, f"{workflow} no longer spells the ceiling pin as a literal"
    for index in pin_lines:
        window = lines[max(0, index - POLICY_MARKER_WINDOW) : index]
        assert any(POLICY_MARKER in line for line in window), (
            f"{workflow}:{index + 1} floats {CEILING!r} with no {POLICY_MARKER!r} "
            f"comment in the {POLICY_MARKER_WINDOW} lines above it. The pin is "
            "load-bearing and reads like a typo; say so where it is written."
        )
