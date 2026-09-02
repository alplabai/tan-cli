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

FOUR measured `pathlib`/`ntpath` behaviour differences now exist across the
supported range (`_IGNORED_ERRNOS`; `Path.resolve(strict=False)` on a symlink
loop; `ntpath.isabs`; and `tan/core/bootstrap.py`'s `resolve_workspace_target`,
where a rooted-but-driveless `--workspace` relocates a checkout into a literal
`\\proj\\ws` on 3.12.3 and raises `ValueError` from 3.13.15 on -- found in PR
#1137's review, real and user-visible, filed separately). None of the four is
guessable from the others; each needed a real interpreter to settle. Pinning
the ceiling away would not make them stop existing; it would only move the
discovery to a customer.

The fourth also marks this gate's standing limit, so it is recorded here rather
than left to be rediscovered: an interpreter leg reports only what some test
asserts. `resolve_workspace_target`'s divergence has no test pinning it, so no
amount of interpreter coverage would have surfaced it -- a reviewer running the
function by hand did.

The COST is stated where it is paid (`ci.yml`'s `python-newest` comment): a new
CPython minor release moves `"3.x"` under an unrelated PR and can red it. That
is the report doing its job.

That cost is not hypothetical here, and the counter-example is already in the
repo: `getting-started.yml` USED to float `"3.x"` and pinned `"3.12"` after the
float resolved to 3.14.6 and Zephyr's own requirements install failed on it,
leaving `patoolib` absent and killing `west sdk install` two steps later
(`getting-started.yml:133-138`). Nothing there was a tan defect -- an upstream
project simply did not support the newest CPython yet. So a floating leg can
red for a reason no change to this repo can fix, which is (a) why `python-newest`
is kept off the release path, where that red would spend a tag, and (b) why a
pinned job's `"3.12"` is not always "the floor because floor": that one is
pinned because Zephyr v4.4.1's requirements are built against it, a reason of
its own that happens to coincide. This gate checks the VALUE, never the reason;
the reason belongs in the job that holds the pin.

## What this gate actually enforces, and why each half exists

The spread on its own was NOT enough, and that is the defect tan-cli#1126
found. `seam1-plan-shape` was the ONLY job floating `"3.x"`, and its selection
is `tests/gates` plus two `tests/parity` seam files and the SDK emit parity
steps -- not `tests/core`, where the failures were -- so the full suite sat red
on 3.14.7 for two tests while all five required contexts stayed green. A check that runs, is required, and
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
   still float, still be green. "Whole" here is the SELECTION, not the
   executed set: with no fixture roots bound, 1365 of 7671 collected tests
   skip on that job (measured on 3.14.7 -- 696 name `ALP_PLANNER_ORACLE_ROOT`,
   572 name `ALP_SDK_ROOT`, 97 are ordinary host/toolchain gates every leg
   skips alike). The bigger half is the ORACLE root, not the SDK root, which
   is why "just bind `ALP_SDK_ROOT`" closes 7.5% rather than the 18% a merged
   count suggests. That gap is stated in the job's own comment and is not this
   gate's to close;
5. the ceiling job stays OFF the release path. `release.yml`'s `gates` job
   calls `ci.yml`, so without a guard a CPython minor shipping between the
   last PR run and a tag reds `python-newest`, skips `build`, and spends an
   immutable tag -- the `v0.5.0-rc3` / #319 shape, with CPython in place of
   alp-sdk `dev`. Deleting the `if:` or the call-site opt-out is one line in
   either of two files, and neither would fail anything else.

## Why the ENDPOINTS only, and what that costs

`>=3.12` promises 3.13 as much as it promises 3.14, and 3.13 is precisely where
the `ntpath.isabs` boundary moved -- a 3.13 leg would have caught tan-cli#1126 a
release earlier. Measured on the interior at this commit: the full suite on
3.13.15 is `6305 passed, 1365 skipped, 1 xfailed` -- identical to 3.14.7's, and
to 3.12.3's once its four no-controlling-terminal xdist artefacts are run
serially. Green, but by luck rather than by gate, and once the ceiling advances
to 3.15 both 3.13 and 3.14 become permanently unmeasured.

The endpoints are still what the JOBS run, because a third full-suite leg is
~10 minutes of runner time per PR (measured: `python-newest` took 10m20s on
PR #1137's own run) for an interior whose behaviour is bracketed by the two
legs either side of it. But the POLICY no longer FORBIDS an interior pin the
way an earlier draft of this file did -- `INTERIOR_PINS` below is the declared,
currently-empty extension point, and the moment a divergence is found INSIDE
the range (rather than at an endpoint) that is where the answer goes.

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

#: The `workflow_call` input `release.yml`'s `gates` call passes to keep the
#: ceiling job off the tag path. Named once, asserted in three places (the
#: declaration, the job's `if:`, the call site) by
#: [`test_the_ceiling_job_is_kept_off_the_release_path`].
RELEASE_OPT_OUT_INPUT = "skip_ceiling_interpreter"

#: The ceiling job's `if:` expression, compared EXACTLY -- not searched for the
#: input's name.
#:
#: The first version of this gate asserted `RELEASE_OPT_OUT_INPUT in guard`, a
#: substring test, and PR #1137's re-review showed what that buys: deleting the
#: `!` (`${{ !inputs.skip_ceiling_interpreter }}` ->
#: `${{ inputs.skip_ceiling_interpreter }}`) left the whole file at `32 passed`.
#: One character, and it inverts the guard completely -- the job disappears from
#: every PR AND runs on the tag path, which is both failures this gate was
#: written to prevent, at once. A gate that pins the presence of a mechanism
#: while its POLARITY is free is not a gate; it is a comment with an assert
#: around it, and this repo has now shipped that shape four times
#: (tan-cli#1059, #1062, #1070).
#:
#: Exact, therefore. A semantically-equivalent rewrite (`${{ ! inputs.x }}`,
#: `${{ inputs.x != true }}`) reds here deliberately: rewriting a guard whose
#: polarity is this consequential should be a decision someone records in this
#: constant, not a diff that slips through on "it means the same thing".
RELEASE_GUARD_EXPRESSION = "${{ !inputs." + RELEASE_OPT_OUT_INPUT + " }}"

FULL_SUITE_COMMAND = "python -m pytest tests -q"

#: `python-version` values allowed BETWEEN the floor and the ceiling, each one
#: a deliberate decision recorded here rather than a pin that appeared in a
#: workflow. Empty today, and that emptiness is the claim: no interior
#: interpreter is measured by any job. See "Why the ENDPOINTS only" in this
#: module's docstring for the cost of that -- 3.13 is where the tan-cli#1126
#: boundary actually moved, and nothing tests it.
#:
#: An entry here is a `"X.Y"` string strictly above the floor. Adding one is
#: half the work: the pin also has to exist in a job that runs something, or
#: this set becomes a list of interpreters the repo believes it tests -- and
#: that second half is ASSERTED below
#: ([`test_a_declared_interior_pin_is_actually_pinned_by_a_job`]), not left as
#: advice, because a claim about coverage that nothing checks is the shape
#: this whole file exists to remove.
INTERIOR_PINS: frozenset[str] = frozenset()

#: The marker each floating pin's own file must carry near it. The acceptance
#: criterion tan-cli#1126 wrote was not only "decide the policy" but "make it
#: legible where someone editing parity.yml will see it" -- before that change
#: the difference between `"3.x"` and `"3.12"` was a one-character detail in
#: one file with nothing saying it was load-bearing. A prose comment nothing
#: checks is a comment that gets deleted in a tidy-up, so its presence is
#: asserted here.
POLICY_MARKER = "INTERPRETER POLICY"

#: How far above a floating pin the marker may sit. Measured distances today:
#: 35 lines in `parity.yml` (marker 976, pin 1011) and 5 in `ci.yml`. 45 left
#: only ten lines of headroom above a comment block this policy had just
#: written at 34 lines -- the next person extending that prose would have hit a
#: failure saying the marker is missing when it is in fact present, which is a
#: worse outcome than a slightly loose window. 90 is comfortably wider than any
#: plausible block and still far narrower than either file (962 and 3012
#: lines, measured at this commit), so a marker elsewhere in the workflow
#: cannot satisfy it.
POLICY_MARKER_WINDOW = 90


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


def test_every_declared_version_is_the_floor_the_ceiling_or_a_declared_interior():
    """The floor, the ceiling, or an interior version DECLARED in
    `INTERIOR_PINS` -- never a value that simply appeared.

    An earlier draft of this test allowed exactly two values and said a
    `"3.13"` pin would be "a version this repo neither promises nor measures".
    Half of that is wrong: `>=3.12` DOES promise 3.13, and 3.13 is exactly
    where the boundary tan-cli#1126 is about actually moved. Forbidding the
    one version most worth adding a leg for is not a policy, it is an
    accident. The set is empty today (see the docstring section on why the
    endpoints are what the jobs run), so this behaves identically to the
    two-value rule until someone makes the decision explicitly.
    """
    allowed = {_floor(), CEILING} | set(INTERIOR_PINS)
    wrong = [(wf, job, version) for wf, job, version in _pins() if version not in allowed]
    assert not wrong, (
        f"python-version values outside the policy {sorted(allowed)}: {wrong}. "
        "The floor is derived from python/pyproject.toml's requires-python; the "
        f"ceiling is {CEILING!r}. An interior version is allowed but must be "
        "declared in INTERIOR_PINS in this file, with the reason, not appear as "
        "a quiet pin in a workflow."
    )


def test_the_floor_pin_tracks_requires_python():
    """The floor is a STRING in 20-odd workflow files and a spec in one
    `pyproject.toml`, and nothing but this test couples them. Raising
    `requires-python` to `>=3.13` without moving the pins would leave every job
    testing an interpreter the package no longer claims to support -- green,
    and measuring the wrong thing.
    """
    floor = _floor()
    pinned = {
        version
        for _, _, version in _pins()
        if version != CEILING and version not in INTERIOR_PINS
    }
    assert pinned == {floor}, (
        f"workflows pin {sorted(pinned)} as the floor; python/pyproject.toml "
        f"declares {floor!r}. Move them in the same change."
    )


def test_a_declared_interior_pin_sits_above_the_floor():
    """`INTERIOR_PINS` is "between the endpoints", and the floor end of that is
    checkable here (the ceiling end is not -- `"3.x"` has no number until a
    runner resolves it). An entry at or below the floor is not an interior
    version at all; it is either a duplicate of the floor or an interpreter
    this package does not support. Vacuous while the set is empty, and that is
    fine: it exists so the first entry cannot be wrong quietly.
    """
    floor = tuple(int(part) for part in _floor().split("."))
    for pin in sorted(INTERIOR_PINS):
        parts = pin.split(".")
        assert len(parts) == 2 and all(part.isdigit() for part in parts), (
            f"INTERIOR_PINS entry {pin!r} is not an `X.Y` version string"
        )
        assert tuple(int(part) for part in parts) > floor, (
            f"INTERIOR_PINS entry {pin!r} is at or below the requires-python "
            f"floor {_floor()!r} -- that is the floor leg, not an interior one"
        )


def test_a_declared_interior_pin_is_actually_pinned_by_a_job():
    """The half `INTERIOR_PINS`' own comment claims and nothing checked.

    A version listed there but pinned by no job is worse than no entry at all:
    it reads as "this repo measures 3.13" to anyone auditing the policy, while
    no runner ever resolves it. Vacuous while the set is empty -- deliberately,
    so that the first entry cannot be half-added quietly.
    """
    pinned = {version for _, _, version in _pins()}
    unused = sorted(set(INTERIOR_PINS) - pinned)
    assert not unused, (
        f"INTERIOR_PINS declares {unused} but no actions/setup-python step pins "
        "them, so nothing runs on them. Either wire a job to the version or "
        "drop it from the set -- a declared interpreter nothing executes is a "
        "coverage claim with no coverage behind it."
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


def test_the_ceiling_job_is_kept_off_the_release_path():
    """A floating interpreter must not be able to spend a tag.

    `release.yml`'s `gates` job is `uses: ./.github/workflows/ci.yml`, and
    `build` (`needs: [verify-version, gates, python-gates]`) does not run if
    `gates` fails -- so an unguarded `python-newest` puts "whatever CPython
    ships this week" on the release path. A minor release landing between the
    last PR run and the tag would red it, skip `build`, and spend an immutable
    tag with no Release behind it, for a reason no change to this repo could
    have prevented and no re-run can fix. That is not a hypothesis: it is
    `v0.5.0-rc3` (#319), which died in exactly that shape against alp-sdk
    `dev` rather than against CPython, and it is why the alp-sdk `ref:` in
    `ci.yml`'s `python` job is pinned.

    Three things have to hold together, in two files, and each is one line to
    undo: the input exists and defaults to NOT skipping (so PRs keep the job),
    the job is guarded by it, and the release call passes `true`. PR #1137's
    review found this; nothing else in the repo would have.

    `github.event_name` is deliberately NOT the discriminator: in a called
    workflow the `github` context is the CALLER's, so a tag run reads `push`,
    never `workflow_call`, and such a guard would never fire.
    """
    ci = _load(WORKFLOWS / "ci.yml")
    triggers = ci.get("on", ci.get(True))
    declared = triggers["workflow_call"]["inputs"][RELEASE_OPT_OUT_INPUT]
    assert declared["type"] == "boolean" and declared["default"] is False, (
        f"ci.yml's {RELEASE_OPT_OUT_INPUT} must default to False -- an input "
        "that defaults to 'run it' reads null (falsy) on pull_request/push/"
        "merge_group, where `inputs` does not exist, and the job would vanish "
        "from every run it exists for"
    )

    guard = str(ci["jobs"][FULL_SUITE_JOB].get("if", "")).strip()
    assert guard == RELEASE_GUARD_EXPRESSION, (
        f"ci.yml:{FULL_SUITE_JOB}'s `if:` is {guard!r}, expected "
        f"{RELEASE_GUARD_EXPRESSION!r}. Compared EXACTLY, and the reason is the "
        "polarity: dropping the `!` reads as a tidy-up, passes a substring "
        "check, and simultaneously deletes this job from every PR and puts the "
        "floating interpreter back on the tag path. If the rewrite is "
        "deliberate, move RELEASE_GUARD_EXPRESSION with it."
    )

    release = _load(WORKFLOWS / "release.yml")
    gates = release["jobs"]["gates"]
    assert str(gates.get("uses", "")).endswith("ci.yml"), (
        "release.yml's `gates` job no longer calls ci.yml -- this gate's whole "
        "premise moved and needs re-deriving, not deleting"
    )
    assert (gates.get("with") or {}).get(RELEASE_OPT_OUT_INPUT) is True, (
        f"release.yml's `gates` call must pass {RELEASE_OPT_OUT_INPUT}: true. "
        "Without it the guard above is inert and the tag path floats again."
    )


@pytest.mark.parametrize("workflow", sorted({workflow for workflow, _ in FLOATING_JOBS}))
def test_each_floating_pin_carries_the_policy_note(workflow: str):
    """"Make it legible" was an acceptance criterion in its own right, because
    before this change the entire difference between a job that tests the
    ceiling and one that tests the floor was one character, in one file, with
    nothing next to it saying so. Prose nothing checks is prose a tidy-up
    deletes.

    Parametrized by FILE, not by `(file, job)` as a first version was: the body
    scans the whole file for `python-version: "3.x"` literals and asserts a
    marker above each, so a `job` parameter advertised a per-job assertion this
    makes no attempt at (PR #1137 review) -- and if both floating jobs ever
    landed in one file, the identical check would have run twice under two
    names. The per-JOB half is
    [`test_exactly_the_named_jobs_float_the_ceiling`]'s, which is where it
    belongs.
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
