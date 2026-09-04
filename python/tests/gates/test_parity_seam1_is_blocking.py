# SPDX-License-Identifier: Apache-2.0
"""`parity.yml`'s `seam1-plan-shape` is a REQUIRED context; it must stay one.

tan-cli#1196. `seam1 -- plan-shape parity` is in `dev`'s required status checks
(measured 2026-09-04 against
`GET /repos/alplabai/tan-cli/branches/dev/protection`, alongside the three
`python -- pytest across python/` legs and `zizmor`), so the merge queue will
not land a PR until this job reports green. That is exactly what makes it worth
neutering: a required context that reports green while asserting nothing is
strictly worse than no context, because the queue now trusts it.

tan-cli#1182 fixed the same defect one workflow over, in
`test_interpreter_policy.py`'s ceiling-job check, and PR #1193's scope stops at
`ci.yml`. Nothing on `dev` asserts anything about `parity.yml` -- measured:
`grep -rn "continue-on-error" python/tests/` finds no assertion that reads it.

## Why this could not be a copy of the #1182 fix

`seam1-plan-shape` legitimately carries THREE soft steps -- the three upstream
drift alarms in [`SOFT_STEPS`], each labelled "(alarm, warn only)" in the
workflow itself. They fire when live alp-sdk moves out from under
`tan/planner/`, which is a fact about the OTHER repo that no tan-cli PR author
caused or can fix in their own branch; failing the build on it would red every
`repository_dispatch` run for somebody else's commit. A blanket
`continue-on-error in (None, False)` over this job reds those three on the
pristine tree.

So the allowlist is the design, and it is keyed by step `name` with the reason
written beside the key rather than in a comment somewhere else: a justification
that lives away from the thing it justifies is the shape that decays.
[`test_every_allowlisted_step_is_real_and_still_soft`] is what stops it decaying
into a permanent exemption -- an entry that no longer names a live soft step is
a failure, not a leftover.

## `in (None, False)`, never `is not True`

`continue-on-error` takes an EXPRESSION, and `yaml.safe_load` returns a `str`
for `${{ ... }}` and for every quoted or `!!str`-tagged spelling. `"..." is not
True` is `True`, so the old comparison waved through every spelling that
matters. Measured here with `actionlint` 1.7.7 against the real `parity.yml`
(`-shellcheck= -pyflakes=`, so the unmutated baseline is rc=0; with shellcheck
on, the baseline is already rc=1 from a pre-existing `SC2016:info`), inserting
each spelling on the load-bearing step `seam1 field diff (live emit vs. frozen
oracle)` and, separately, at job level:

| spelling | `safe_load` gives | actionlint | `is not True` |
| --- | --- | --- | --- |
| `${{ true }}` | `'${{ true }}'` | rc=0 step, rc=0 job -- LANDABLE | passes |
| `${{ github.event_name == 'pull_request' }}` | `str` | rc=0 step, rc=0 job -- LANDABLE | passes |
| `true` | `True` | rc=0 step, rc=0 job -- LANDABLE | catches |
| `"true"` | `'true'` | rc=1 | passes |
| `!!str true` | `'true'` | rc=1 | passes |
| `"true "` | `'true '` | rc=1 | passes |
| `${{ env.SOFT }}` | `str` | rc=1 | passes |
| `${{ matrix.experimental }}` | `str` | rc=1 | passes |
| anchor/alias to `"true"` | `'true'` | rc=1 (`expected bool value but found alias node`) | passes |
| `yes` | `True` | rc=1 | catches |
| `on` | `True` | rc=1 | catches |

Three spellings are lint-clean, and TWO of those three (`${{ true }}` and
`${{ github.event_name == 'pull_request' }}`) are also silent under
`is not True` -- landable and silent at once, which is the real attack. The
rest are "merely" silent, and actionlint is not what the merge queue requires
(`dev`'s required contexts are seam1, the three pytest legs and zizmor), so
"actionlint would have caught it" is not a defence this gate may lean on. All
eleven are covered by [`test_a_neutering_spelling_reds_this_gate`].

## The job-level `if:`

A job whose `if:` is false renders **"skipped", not "failed"**, so a guard on
the job silences a required context with no red anywhere. Checked by VALUE, not
by absence-of-key search: PR #1193's naive `assert "if" not in job` failed on a
pristine `ci.yml` because `python-newest` legitimately carries a release
opt-out. Measured on `parity.yml` at `dev` `b432409`, `seam1-plan-shape`
carries NO job-level `if:` -- unlike its four siblings `seam2`, `first-blink`,
`python-tests` and `release-sdk-parity`, which all do -- so the pinned value
here is `None`. If this job ever earns one, pin it in [`SEAM1_JOB_IF`] with the
reason, the way `test_interpreter_policy.py` pins `python-newest`'s.

## What this does NOT cover, said out loud

**Step-level `if:`.** Ten of this job's 24 steps legitimately carry one
(`if: github.event_name == 'repository_dispatch'` and friends), so a blanket
"no step `if:`" reds the pristine tree exactly the way a blanket
`continue-on-error` check does, and a second allowlist keyed on step guards is
its own design with its own justifications -- not a rider on this one. A step
guarded into never running is a real hole and it is filed, not forgotten
(tan-cli#1196's own acceptance is scoped to `continue-on-error` and the job
`if:`).

**Whether the context is still REQUIRED.** Branch protection lives on GitHub,
not in this tree, so no test here can see it. [`SEAM1_DISPLAY_NAME`] pins the
job's `name:` -- the string protection keys on -- which is the most a file in
this repo can do; renaming it is at least fail-CLOSED (an unreported required
context blocks the queue rather than passing it).
"""

from __future__ import annotations

import copy
import functools
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PARITY = WORKFLOWS / "parity.yml"

#: The job id, as `parity.yml` spells it under `jobs:`.
SEAM1_JOB = "seam1-plan-shape"

#: The job's `name:`, which is the string `dev`'s required-status-check list
#: keys on -- measured 2026-09-04:
#: `["seam1 -- plan-shape parity", "python -- pytest across python/
#: (ubuntu-latest)", ..., "zizmor · workflow security"]`. Pinned so a rename
#: cannot drift the workflow away from the protection rule silently. It is the
#: fail-closed half of the pair (an unreported required context blocks the
#: queue); `continue-on-error` and `if:` are the fail-OPEN half, which is why
#: they get the rest of this file.
SEAM1_DISPLAY_NAME = "seam1 -- plan-shape parity"

#: The ONLY steps of [`SEAM1_JOB`] permitted to carry `continue-on-error`, each
#: with the reason it may be soft, here rather than in a comment elsewhere
#: (tan-cli#1196). Keyed on the step's `name`, which is what a reader of the
#: checks list sees; an UNNAMED step can never be allowlisted, and that is
#: deliberate -- softening a step nobody can name is not a thing this repo
#: should make easy.
#:
#: All three run ONLY on `repository_dispatch`, all three compare tan's
#: `tan/planner/` hand-port against LIVE alp-sdk, and all three are labelled
#: "(alarm, warn only)" in `parity.yml` itself. The shared reason is in
#: [`SOFT_STEP_SHARED_REASON`]; what differs per entry is which drift it
#: watches.
SOFT_STEPS: dict[str, str] = {
    "planner relocation freshness vs live alp-sdk (alarm, warn only)": (
        "alarms when alp-sdk's `scripts/alp_orchestrate/**` has moved since the "
        "commit `test_planner_relocation_freshness.py` pins its hashes at. "
        "Fires hardest on a `repository_dispatch`, whose whole premise is "
        "'alp-sdk just moved' -- hard-failing it would red the dispatch for a "
        "commit nobody in this repo authored. Whether it should ever block is "
        "tan-cli#509's decision, not this step's."
    ),
    "hand-port freshness vs live alp-sdk (alarm, warn only)": (
        "same alarm for the hand-ported (rather than relocated) planner "
        "modules, via `ALP_SDK_HAND_PORT_ROOT`. Not a replacement for the "
        "always-run internal-consistency half of the same test, which stays "
        "blocking in the `python/tests/gates` step above it."
    ),
    "strict-loaders freshness vs live alp-sdk (alarm, warn only)": (
        "alarms when alp-sdk's `scripts/strict_loaders.py` diverges from the "
        "hand-port wired into `tan/planner/loader.py`. The pin is FROZEN at the "
        "commit that introduced alp-sdk#1127, so a difference means 'upstream "
        "moved, re-audit', never 'this PR is broken'."
    ),
}

#: Why all three may be soft at all, in one place so the per-entry reasons say
#: only what is specific to them. An alarm that reds a run its author did not
#: cause and cannot fix in their own branch trains everyone to ignore it, which
#: is worse than not having it -- `parity.yml`'s own comments make that argument
#: for each of the three.
SOFT_STEP_SHARED_REASON = (
    "warn-only upstream-drift alarm: `repository_dispatch`-only, reports "
    "through an explicit `::warning::`, never a red X"
)

#: The job-level `if:` [`SEAM1_JOB`] is permitted to carry. `None` means NONE,
#: and that is a MEASURED value, not an assumption: on `dev` `b432409` this job
#: has no `if:` while `seam2`, `first-blink`, `python-tests` and
#: `release-sdk-parity` all do. Checked by value rather than by
#: `assert "if" not in job` so that, the day this job earns a legitimate guard,
#: the fix is to pin it here WITH its reason -- not to delete the check.
SEAM1_JOB_IF: str | None = None

#: A floor on the walk, so a green bar cannot mean "the job has no steps".
#: 24 on `dev` `b432409`; the floor is deliberately well below that, because
#: this is an anti-vacuity check and not a second size ratchet.
MIN_STEPS = 15

#: Every `continue-on-error` spelling that NEUTERS what it is written on,
#: spelled as it would appear in the YAML. Driven through the real
#: `yaml.safe_load` by [`_loaded`] rather than hand-typed as Python values,
#: because the SPELLING is the defect: tan-cli#1182 was a gate comparing the
#: loaded value with `is not True`, which is `True` for every entry here that
#: loads as a `str`.
#:
#: Each entry is `(label, YAML document defining continue-on-error)`. The
#: document form exists for the anchor/alias case, which cannot be expressed as
#: a bare scalar -- an alias needs its anchor earlier in the same document.
NEUTERING_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("${{ true }}", "continue-on-error: ${{ true }}"),
    ('"true"', 'continue-on-error: "true"'),
    ("!!str true", "continue-on-error: !!str true"),
    ('"true "', 'continue-on-error: "true "'),
    (
        "${{ github.event_name == 'pull_request' }}",
        "continue-on-error: ${{ github.event_name == 'pull_request' }}",
    ),
    ("${{ env.SOFT }}", "continue-on-error: ${{ env.SOFT }}"),
    ("${{ matrix.experimental }}", "continue-on-error: ${{ matrix.experimental }}"),
    ("anchor/alias", 'anchored: &soft "true"\ncontinue-on-error: *soft'),
    ("yes", "continue-on-error: yes"),
    ("on", "continue-on-error: on"),
    ("true", "continue-on-error: true"),
)

#: The spellings that leave a step BLOCKING and must therefore keep this gate
#: GREEN. `false` is the explicit opt-out somebody may legitimately write, and
#: an absent key and an explicit `false` mean the same thing to the runner -- a
#: gate that red on `false` would push the next person to delete the key
#: instead, which is strictly less legible.
#:
#: LITERAL falsy scalars only. `continue-on-error: ${{ false }}` reds, and that
#: is deliberate rather than an oversight: this check is static and no static
#: check can evaluate a GitHub expression, which is precisely why
#: `${{ env.SOFT }}` and `${{ matrix.experimental }}` sit above. Every
#: expression form fails CLOSED; the fix a workflow author needs is one word.
BLOCKING_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("false", "continue-on-error: false"),
    ("False", "continue-on-error: False"),
    ("FALSE", "continue-on-error: FALSE"),
    ("no", "continue-on-error: no"),
    ("off", "continue-on-error: off"),
    ("null", "continue-on-error: null"),
    ("~", "continue-on-error: ~"),
    ("<empty>", "continue-on-error:"),
)

#: Job-level `if:` expressions that must red. Every one of them leaves the job
#: present, named and reporting "skipped" -- which the merge queue does not
#: treat as a failure -- while it runs zero of its 24 steps. Blast radius wider
#: than any `continue-on-error`, which at least still runs the steps.
NEUTERING_JOB_GUARDS: tuple[str, ...] = (
    "github.event_name == 'schedule'",
    "${{ false }}",
    "${{ github.event_name != 'pull_request' }}",
    "${{ !inputs.python_only }}",
)


@functools.cache
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _seam1() -> dict:
    return _load(PARITY)["jobs"][SEAM1_JOB]


def _label(index: int, step: dict) -> str:
    return step.get("name") or step.get("uses") or f"step {index}"


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_seam1_plan_shape_cannot_be_neutered():
    """The required context stays blocking: no truthy `continue-on-error` on
    the job or on any step outside [`SOFT_STEPS`], and no job-level `if:`."""
    job = _seam1()

    assert job.get("name") == SEAM1_DISPLAY_NAME, (
        f"parity.yml:{SEAM1_JOB}'s `name:` is {job.get('name')!r}, but `dev`'s "
        f"required-status-check list keys on {SEAM1_DISPLAY_NAME!r}. Renaming "
        "the job without editing branch protection leaves a required context "
        "that is never reported. Update both, then update SEAM1_DISPLAY_NAME."
    )

    assert job.get("continue-on-error") in (None, False), (
        f"parity.yml:{SEAM1_JOB} carries "
        f"`continue-on-error: {job.get('continue-on-error')!r}` at JOB level -- "
        "it now reports success whatever the seam-1 comparison does, and it is "
        "a REQUIRED merge-queue context, so the queue lands PRs on its say-so. "
        "Compared with `in (None, False)` and not `is not True`: an expression "
        "or any quoted spelling loads as a `str`, which `is not True` waves "
        "straight through (tan-cli#1182)."
    )

    guard = job.get("if")
    assert guard == SEAM1_JOB_IF, (
        f"parity.yml:{SEAM1_JOB}'s JOB-level `if:` is {guard!r}; the pinned "
        f"value is {SEAM1_JOB_IF!r}. A job whose `if:` is false renders "
        "SKIPPED, not failed, so this required context would report no red "
        "anywhere while running zero of its steps -- a wider silencing than "
        "any `continue-on-error`. Checked by VALUE, not by absence: if this job "
        "has earned a guard, pin it in SEAM1_JOB_IF with the reason."
    )

    steps = job["steps"]
    for index, step in enumerate(steps):
        label = _label(index, step)
        value = step.get("continue-on-error")
        if step.get("name") in SOFT_STEPS:
            assert value is True, (
                f"parity.yml:{SEAM1_JOB}'s allowlisted step {label!r} carries "
                f"`continue-on-error: {value!r}`. An allowlisted step may be "
                "soft only as the literal `true`: an expression there is not "
                "'warn only', it is a value nothing in this tree can evaluate."
            )
            continue
        assert value in (None, False), (
            f"parity.yml:{SEAM1_JOB}'s step {label!r} carries "
            f"`continue-on-error: {value!r}`, and it is not in SOFT_STEPS. A "
            "failure there is swallowed and this REQUIRED context still "
            "reports green. If the step genuinely belongs in the warn-only "
            "set, add it to SOFT_STEPS with its reason -- the point of the "
            "allowlist is that widening it is a visible edit."
        )


def test_every_allowlisted_step_is_real_and_still_soft():
    """The allowlist cannot decay into a permanent exemption.

    An entry that names a step which no longer exists, or one that is no longer
    soft, is a claim about the workflow that stopped being true -- and an
    allowlist nobody has to keep true is the comment-with-an-assert-around-it
    shape this repo has shipped before.
    """
    steps = _seam1()["steps"]
    by_name = {step["name"]: step for step in steps if "name" in step}

    for name, reason in SOFT_STEPS.items():
        assert name in by_name, (
            f"SOFT_STEPS names {name!r}, which is not a step of "
            f"parity.yml:{SEAM1_JOB} any more. Delete the entry; a stale "
            "allowlist key is an exemption waiting for a step to be renamed "
            "back into it."
        )
        assert by_name[name].get("continue-on-error") is True, (
            f"SOFT_STEPS names {name!r} as permitted to be soft, but the step "
            "carries `continue-on-error: "
            f"{by_name[name].get('continue-on-error')!r}`. It is blocking now, "
            "so drop it from the allowlist rather than leaving an entry that "
            "would silently re-permit softening later."
        )
        assert reason.strip(), f"SOFT_STEPS[{name!r}] has no justification"

    soft_in_workflow = {
        step["name"]
        for step in steps
        if "name" in step and step.get("continue-on-error") is not None
    }
    assert soft_in_workflow == set(SOFT_STEPS), (
        "SOFT_STEPS and the workflow disagree about which steps carry "
        f"`continue-on-error`: workflow={sorted(soft_in_workflow)}, "
        f"allowlist={sorted(SOFT_STEPS)}."
    )


def test_the_job_was_actually_read():
    """Anti-vacuity. Every assertion above walks `job["steps"]`; an empty or
    absent list would make all of them pass while checking nothing."""
    assert PARITY.is_file(), f"{PARITY} is gone -- the walk below reads nothing"
    job = _seam1()
    assert len(job["steps"]) >= MIN_STEPS, (
        f"parity.yml:{SEAM1_JOB} has {len(job['steps'])} steps, below the "
        f"MIN_STEPS floor of {MIN_STEPS}. Either the job was gutted or this "
        "file is now walking the wrong thing."
    )
    assert SOFT_STEPS, "the allowlist is empty -- the allowlist branch is dead"


# --------------------------------------------------------------------------
# Negative controls. Without these the assertions above are a one-line edit
# nothing stands behind -- reverting `in (None, False)` to `is not True` would
# leave every test above green, which is exactly how tan-cli#1182 survived
# from the day its assertion was written.
# --------------------------------------------------------------------------


def _loaded(document: str):
    """The Python value `yaml.safe_load` produces for one spelling.

    The real loader, so a control cannot encode a belief about YAML that YAML
    does not share (`yes` is `True`, `!!str true` is a `str`, an alias resolves
    to its anchor's value, an empty value is `None`).
    """
    return yaml.safe_load(document)["continue-on-error"]


def _patched_parity(monkeypatch, mutate) -> None:
    """Run the gate above against a doctored `parity.yml`.

    A deep copy behind a patched `_load`, rather than writing the workflow into
    a temporary tree: editing the real file to measure a gate is how a
    measurement gets left behind in a commit.

    The fallback is CAPTURED, not looked up. `real_load` is bound HERE, before
    `monkeypatch.setattr` runs, because a `_load.__wrapped__` written inside the
    lambda resolves the module global `_load` at call time -- which by then is
    the lambda itself, and a plain function has no `__wrapped__`. That is not a
    hypothetical: it is the exact bug PR #1193 hit writing the same helper for
    `ci.yml`, where it stayed invisible because the test under control read one
    workflow and never took the other arm. It would surface here as an
    `AttributeError` raised INSIDE `pytest.raises(AssertionError,
    match="continue-on-error")`, so every neutering case would stop asserting
    anything about `continue-on-error` at all.
    [`test_the_patched_loader_falls_back_to_the_real_file`] keeps that arm
    alive.

    `__wrapped__` rather than `_load` itself, so the fallback bypasses
    `functools.cache` and genuinely reads the file.
    """
    real_load = _load.__wrapped__
    parity = copy.deepcopy(_load(PARITY))
    mutate(parity["jobs"][SEAM1_JOB])
    monkeypatch.setattr(
        sys.modules[__name__],
        "_load",
        lambda path: parity if path.name == "parity.yml" else real_load(path),
    )


def _load_bearing_step(job: dict) -> dict:
    """A step of [`SEAM1_JOB`] that is NOT allowlisted -- the thing an attacker
    would soften. Chosen by walking rather than by index, so a reordering of
    the workflow cannot silently point the controls at an allowlisted step and
    turn them all green."""
    for step in job["steps"]:
        if step.get("name") not in SOFT_STEPS and "name" in step:
            return step
    raise AssertionError("every named step is allowlisted -- nothing left to protect")


def _set_on_job(value):
    def mutate(job):
        job["continue-on-error"] = value

    return mutate


def _set_on_load_bearing_step(value):
    def mutate(job):
        _load_bearing_step(job)["continue-on-error"] = value

    return mutate


@pytest.mark.parametrize("label,document", NEUTERING_SPELLINGS, ids=[s[0] for s in NEUTERING_SPELLINGS])
@pytest.mark.parametrize("level", ("job", "step"))
def test_a_neutering_spelling_reds_this_gate(monkeypatch, level: str, label: str, document: str):
    """One case per spelling in [`NEUTERING_SPELLINGS`], at both levels."""
    value = _loaded(document)
    mutate = _set_on_job(value) if level == "job" else _set_on_load_bearing_step(value)
    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match="continue-on-error"):
        test_seam1_plan_shape_cannot_be_neutered()


@pytest.mark.parametrize("label,document", BLOCKING_SPELLINGS, ids=[s[0] for s in BLOCKING_SPELLINGS])
@pytest.mark.parametrize("level", ("job", "step"))
def test_a_blocking_spelling_keeps_this_gate_green(monkeypatch, level: str, label: str, document: str):
    """The other half of the control: the falsy spellings must NOT red, or the
    next person deletes the key rather than writing the explicit opt-out."""
    value = _loaded(document)
    mutate = _set_on_job(value) if level == "job" else _set_on_load_bearing_step(value)
    _patched_parity(monkeypatch, mutate)
    test_seam1_plan_shape_cannot_be_neutered()


@pytest.mark.parametrize("label,document", NEUTERING_SPELLINGS, ids=[s[0] for s in NEUTERING_SPELLINGS])
def test_an_allowlisted_step_stays_soft_only_as_the_literal_true(monkeypatch, label: str, document: str):
    """The allowlist permits softness, not any value at all.

    `true` on an allowlisted step is the pristine tree and must stay green;
    every other spelling on the SAME step must red, so the allowlist cannot be
    used as a doorway for an expression nothing static can evaluate.
    """
    value = _loaded(document)

    def mutate(job):
        soft = next(s for s in job["steps"] if s.get("name") in SOFT_STEPS)
        soft["continue-on-error"] = value

    _patched_parity(monkeypatch, mutate)
    if value is True:
        test_seam1_plan_shape_cannot_be_neutered()
        return
    with pytest.raises(AssertionError, match="continue-on-error"):
        test_seam1_plan_shape_cannot_be_neutered()


@pytest.mark.parametrize("guard", NEUTERING_JOB_GUARDS)
def test_a_job_level_if_reds_this_gate(monkeypatch, guard: str):
    """The job-`if:` control. Every guard here leaves the required context
    reporting "skipped" -- which is not "failed" -- while it runs nothing."""

    def mutate(job):
        job["if"] = guard

    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match=r"`if:`"):
        test_seam1_plan_shape_cannot_be_neutered()


def test_a_renamed_job_reds_this_gate(monkeypatch):
    """Renaming the job breaks its link to `dev`'s required-context list."""

    def mutate(job):
        job["name"] = "seam1 -- plan shape parity"

    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match="required-status-check"):
        test_seam1_plan_shape_cannot_be_neutered()


def test_a_stale_allowlist_entry_reds_the_decay_check(monkeypatch):
    """An entry naming a step that is not in the workflow."""
    monkeypatch.setitem(SOFT_STEPS, "a step that does not exist", "no reason at all")
    with pytest.raises(AssertionError, match="not a step of"):
        test_every_allowlisted_step_is_real_and_still_soft()


def test_an_allowlisted_step_that_went_blocking_reds_the_decay_check(monkeypatch):
    """An entry that is still real but no longer soft: the allowlist would be
    holding a permission nothing uses, ready to re-permit softening later."""
    name = next(iter(SOFT_STEPS))

    def mutate(job):
        next(s for s in job["steps"] if s.get("name") == name).pop("continue-on-error")

    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match="drop it from the allowlist"):
        test_every_allowlisted_step_is_real_and_still_soft()


def test_a_newly_softened_step_reds_the_decay_check(monkeypatch):
    """The other direction: a soft step the allowlist does not name. Caught by
    the main gate too -- asserted here as well so narrowing either one leaves
    the hole covered by the other."""

    def mutate(job):
        _load_bearing_step(job)["continue-on-error"] = True

    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match="disagree about which steps"):
        test_every_allowlisted_step_is_real_and_still_soft()


def test_an_emptied_job_reds_the_anti_vacuity_check(monkeypatch):
    """The floor is a real floor, not a comment."""

    def mutate(job):
        job["steps"] = job["steps"][:1]

    _patched_parity(monkeypatch, mutate)
    with pytest.raises(AssertionError, match="MIN_STEPS floor"):
        test_the_job_was_actually_read()


@pytest.mark.parametrize("workflow", ("ci.yml", "release.yml"))
def test_the_patched_loader_falls_back_to_the_real_file(monkeypatch, workflow: str):
    """The control on [`_patched_parity`]'s OTHER arm: everything that is not
    `parity.yml` must still reach the real loader and the real file.

    Unreachable from the controls above today, because
    [`test_seam1_plan_shape_cannot_be_neutered`] reads `parity.yml` and nothing
    else -- which is exactly the condition under which PR #1193's first version
    of this helper was DEAD and no existing test could show it. The moment
    either function grows a second `_load(...)`, this is what says whether the
    fallback still works.
    """
    path = WORKFLOWS / workflow
    assert path.is_file(), f"{workflow} is not in {WORKFLOWS} any more"
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))

    _patched_parity(monkeypatch, lambda job: None)

    # The parity.yml arm still answers with the doctored deep copy...
    assert SEAM1_JOB in _load(PARITY)["jobs"]
    # ...and the other arm reaches the real file. An equality check, not a
    # truthiness one: `assert _load(path)` would also pass on a stale cache
    # entry or on some other workflow entirely.
    assert _load(path) == expected


def test_the_spelling_tables_load_the_way_this_file_claims():
    """The table in the module docstring, asserted rather than believed.

    Every neutering spelling must load to something OUTSIDE `(None, False)`,
    and every blocking one INSIDE it -- otherwise the two lists are prose and
    the controls above are testing a belief about PyYAML rather than PyYAML.
    """
    for label, document in NEUTERING_SPELLINGS:
        value = _loaded(document)
        assert value not in (None, False), f"{label!r} loads as {value!r}, which is blocking"
    for label, document in BLOCKING_SPELLINGS:
        value = _loaded(document)
        assert value in (None, False), f"{label!r} loads as {value!r}, which is not blocking"

    # The half tan-cli#1182 was actually about: eight of the eleven neutering
    # spellings load as a `str`, and `<str> is not True` is `True`, so the old
    # comparison passed every one of them.
    silent_before = [
        label for label, document in NEUTERING_SPELLINGS if _loaded(document) is not True
    ]
    assert len(silent_before) == 8, silent_before
