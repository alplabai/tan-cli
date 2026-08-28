# SPDX-License-Identifier: Apache-2.0
"""Every workflow job must be bounded, and every PR run must supersede itself.

tan-cli#812. `parity.yml` is the heaviest PR-triggered workflow in the repo (six
jobs, one of them a 15-leg `os` x `shard` matrix) and it produced FOUR of the
five required contexts while carrying neither a `concurrency:` group nor a
single `timeout-minutes:` -- the only PR-triggered workflow with neither. A
superseded push therefore ran its previous suite to completion while ci.yml,
clean-host.yml and getting-started.yml cancelled theirs, and a wedged leg on a
required context would have held the PR for GitHub's 360-minute job default.

Two invariants are pinned here, at two DIFFERENT scopes -- and that difference
is deliberate, not an oversight (tan-cli#854/#855 review):

1. CONCURRENCY, scoped to `on: pull_request` workflows only. Every workflow
   that runs `on: pull_request` carries a top-level `concurrency:` whose
   `cancel-in-progress` is enabled for `pull_request`. This is the house rule
   ci.yml:78 and clean-host.yml:170 write out in prose; nothing enforced it,
   which is how parity.yml went without one from birth. "Supersede a
   superseded run" is a PR-specific concept: a `push` tag, a `schedule` cron,
   or a `workflow_dispatch` proof-run has no earlier run of the SAME logical
   change to cancel, so this half stays scoped to `_pull_request_workflows()`
   on purpose -- widening it would either assert nothing meaningful about
   those triggers or invent a semantics ci.yml/clean-host.yml never had.

   `parity.yml`'s group key additionally does NOT collapse every
   `repository_dispatch` run into one group -- the subtle half, covered by its
   own test below. GitHub raises `repository_dispatch` from the DEFAULT
   branch, so a flat `parity-${{ github.ref }}` key -- the obvious "just match
   ci.yml" edit -- gives every dispatched run the same `refs/heads/dev` key
   however different the `client_payload.sdk_ref` under test. Grouping is not
   only about cancellation: a run arriving while another in its group is in
   progress waits, and a third arrival cancels the one that was waiting.
   Three alp-sdk contract pushes in quick succession would leave the middle
   `sdk_ref` with no parity run at all, silently dropping drift coverage for
   exactly the SHA that asked for it -- and tan-cli#485 already records what
   happens when this signal goes unwatched. A dropped run is strictly worse
   than an unwatched one: there is no red left to find.

2. TIMEOUTS, scoped to EVERY workflow file under `.github/workflows/`, not
   just the `pull_request` set. This is the tan-cli#854/#855 widening. #841
   added `test_every_parity_job_is_bounded` below, hardcoded to `parity.yml`'s
   own six jobs; #845 then bounded five more jobs elsewhere (`ci.yml`,
   `version-identity.yml`) that nothing generalised caught. #855 measured the
   OTHER gap this left: eleven jobs outside the `pull_request` set at all
   (`planner-resync.yml`, `python-binaries.yml`, `release-combination.yml`,
   `release.yml`) still inherited GitHub's 360-minute default -- five of them
   on `release.yml`'s TAG path, where a wedge holds an immutable, already-cut
   version number for six hours with nothing re-runnable the way a PR push is.
   A job that never supersedes a sibling run can still hang, so the CONCURRENCY
   restriction above has no bearing on whether the TIMEOUT check should apply
   to it -- unlike invariant 1, there is no PR-specific concept a `schedule` or
   `push: tags` job would be asserting nothing about. The decision #855 asked
   to be recorded explicitly: **timeouts widen to every workflow; concurrency
   stays pull_request-scoped.** `parity.yml`'s own named-tuple check
   (`test_every_parity_job_is_bounded` / `_PARITY_JOBS`) is kept ALONGSIDE the
   generalised one, per #854's own ask -- deleting a job from `parity.yml`
   must still be visible here as a job needing deliberate removal from that
   tuple, not just a silent shrink of the wider net's coverage.

   A `uses:` job (a reusable-workflow caller, e.g. `release.yml`'s `gates` /
   `python-gates`) is excluded from the widened check: GitHub does not accept
   `timeout-minutes` on a job whose body is `uses: ./.github/workflows/x.yml`
   -- the bound lives on THAT workflow's own jobs, covered when this same test
   walks `x.yml` in its own right. Demanding the key there would fail every
   caller job for a key that does nothing (tan-cli#854's own issue text).

Nothing here runs a workflow. It reads the YAML and asserts structure, so a
future tidy-up that flattens the group key or drops a `timeout-minutes` fails
in `tests/gates` rather than on a runner months later.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PARITY = WORKFLOWS / "parity.yml"

#: Upper bound on any single `timeout-minutes`. GitHub's own job default is
#: 360; anything at or above that is not a hang catcher, it is the default
#: wearing a number. pin-move-verify.yml's `verify` sits at 100, the widest
#: value in the repo as of the tan-cli#854/#855 widening -- still comfortably
#: under this cap, so raising it was not needed to land that change. Raise it
#: in the same change that needs it, and say what got slower (this constant's
#: whole reason for being a name instead of a bare 120 in six places).
_MAX_SANE_TIMEOUT_MINUTES = 120

#: Matches a STANDALONE integer inside a GitHub Actions expression string,
#: e.g. the `60` and `30` in `${{ inputs.sdk_parity && 60 || 30 }}`. Anchored
#: with lookaround (not `\b`, which sits between a word char and a non-word
#: char and so never separates two adjacent word chars like a letter and a
#: digit) so a digit run embedded in an IDENTIFIER is excluded: a future
#: expression like `${{ inputs.py311 && 60 || 30 }}` must extract only `60`
#: and `30`, not the `311` inside `py311`, or a perfectly fine 60/30 bound
#: would fail the 1..120 range check on a phantom 311. Used only by
#: `_assert_timeout_is_bounded` below, for the one job in this repo that
#: spells `timeout-minutes` as a computed expression rather than a literal
#: int (`ci.yml`'s `python` job -- see that function's docstring).
_TIMEOUT_EXPR_INT_RE = re.compile(r"(?<![A-Za-z0-9_.])\d+(?![A-Za-z0-9_.])")

#: Jobs that must carry a timeout, named rather than derived, so that DELETING
#: a job from parity.yml is visible here instead of quietly shrinking what this
#: gate covers. tan-cli#812's own suggested fix listed only the first four --
#: `python-tests` was missing from it, and `python-tests` is the job whose
#: three matrix legs ARE three of the five required contexts.
_PARITY_JOBS = (
    "seam1-plan-shape",
    "seam2",
    "first-blink",
    "python-tests-shard",
    "python-tests",
    "notify-planner-drift",
)


@functools.cache
def _load(path: Path) -> dict:
    # Cached: the generalised job-bounding test below re-reads every workflow
    # file once per JOB it declares (29 cases across 11 files as of this
    # writing), not once per file -- caching keeps that an 11-file parse
    # instead of 29 re-parses of the same handful of files.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> list[str]:
    """The `on:` keys. PyYAML resolves a bare `on:` to the BOOLEAN True (YAML
    1.1 treats `on` as a truthy scalar), so the real key is `True`, not the
    string `"on"` -- read both rather than silently finding nothing."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, dict):
        return list(on)
    if isinstance(on, str):
        return [on]
    return list(on or [])


def _workflow_files(directory: Path) -> list[Path]:
    """Every workflow file directly under `directory`, `.yml` AND `.yaml` --
    GitHub Actions accepts both extensions for `.github/workflows/*`, so a
    glob scoped to `.yml` alone gives a future `foo.yaml` zero coverage from
    a gate whose whole premise is "no blind spot left" (tan-cli#854/#855
    review)."""
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


@functools.cache
def _pull_request_workflows() -> tuple[tuple[str, Path], ...]:
    found = []
    for path in _workflow_files(WORKFLOWS):
        if "pull_request" in _triggers(_load(path)):
            found.append((path.name, path))
    return tuple(found)


@functools.cache
def _all_workflows() -> tuple[tuple[str, Path], ...]:
    """Every workflow file under `.github/workflows/`, regardless of trigger.

    tan-cli#855: `planner-resync.yml`, `python-binaries.yml`,
    `release-combination.yml` and `release.yml` carry no `pull_request`
    trigger at all, so `_pull_request_workflows()` above never sees them --
    and eleven of their jobs inherited GitHub's 360-minute default as a
    result, five of them on `release.yml`'s `push: tags` path. The timeout
    half of this gate walks THIS set, not the pull_request one; see the module
    docstring for why that widening is deliberate and the concurrency half is
    not.
    """
    return tuple((path.name, path) for path in _workflow_files(WORKFLOWS))


#: The ONLY `uses:` prefix that exempts a job below -- a LOCAL reusable
#: workflow, whose own jobs this same test walks and bounds when it reads that
#: file in its own right. `uses: some-org/repo/.github/workflows/x.yml@v1` (a
#: CROSS-repo caller) does not start with this prefix and is therefore NOT
#: exempt: nothing in this repo bounds a workflow it doesn't own, so a job
#: that called out to one would sail through with GitHub's 360-minute default
#: and nothing anywhere would catch it. Verified latent, not live -- no
#: cross-repo caller exists in this repo today (tan-cli#854/#855 review).
_LOCAL_REUSABLE_WORKFLOW_PREFIX = "./.github/workflows/"


def _bounded_jobs(workflow: dict) -> dict[str, dict]:
    """Jobs that COULD carry `timeout-minutes` -- i.e. every job except a
    LOCAL reusable-workflow caller (`uses: ./.github/workflows/x.yml`).

    GitHub does not accept `timeout-minutes` on a `uses:` job: the bound lives
    on the CALLED workflow's own jobs, which this same test asserts on when it
    walks that file in its own right (`release.yml`'s `gates` calls `ci.yml`,
    whose `python`/`shim`/`wheel-floor`/`workflow-security` jobs are already
    covered; `python-gates` calls `parity.yml`, likewise already covered).
    Demanding the key on the CALLER would fail every release for a key that
    changes nothing -- tan-cli#854's own issue text flagged this exact shape
    before any code was written, which is why it is handled here rather than
    discovered as a false positive later.

    A `uses:` job whose callee is NOT local (no `./.github/workflows/`
    prefix -- a cross-repo reusable workflow) is deliberately NOT exempted
    here: there is no local file for this test to walk and bound instead, so
    letting it through would leave the job's runtime entirely unbounded with
    nothing anywhere asserting on it.
    """
    jobs = workflow.get("jobs") or {}
    return {
        job_id: job
        for job_id, job in jobs.items()
        if not str(job.get("uses", "")).startswith(_LOCAL_REUSABLE_WORKFLOW_PREFIX)
    }


@functools.cache
def _every_workflow_job_cases() -> tuple[tuple[str, str], ...]:
    """Every (workflow filename, job id) pair this repo can be asked to bound,
    across every workflow file. Computed once (not per parametrised case) so
    that collection reads each YAML file exactly once via `_load`'s cache."""
    cases = []
    for name, path in _all_workflows():
        for job_id in _bounded_jobs(_load(path)):
            cases.append((name, job_id))
    return tuple(cases)


def _assert_timeout_is_bounded(workflow_name: str, job_id: str, timeout: object) -> None:
    """Shared assertion for both the generalised test and (implicitly, via the
    same shape) `test_every_parity_job_is_bounded` below.

    Handles one wrinkle a plain `isinstance(timeout, int)` check would get
    wrong: `ci.yml`'s `python` job spells its bound as a GitHub Actions
    expression, `${{ inputs.sdk_parity && 60 || 30 }}`, because the SAME job
    runs two different workloads -- a `pull_request`/`merge_group` run
    measures 6-11 minutes (p100 11m) while the `release.yml` `workflow_call`
    with `sdk_parity: true` measures up to 23.7 minutes (tan-cli#844's own
    changelog fragment, `changelog.d/844.fixed.md`, left a note for exactly
    this generalisation: "this value is a string expression, not an int").
    PyYAML parses that value as the plain string
    `"${{ inputs.sdk_parity && 60 || 30 }}"`, not as either branch's number,
    and this test has no GitHub Actions runtime to evaluate `inputs.*`
    against -- so instead of evaluating the expression, every bare integer
    literal INSIDE it is extracted and bounded individually. That is strictly
    MORE conservative than picking one branch: a future third branch adding an
    out-of-range number fails here even though this test can never know which
    branch a real run takes.
    """
    assert timeout is not None, (
        f"{workflow_name}'s {job_id!r} job declares no `timeout-minutes`, so "
        f"it inherits GitHub's 360-minute default. A wedged leg holds "
        f"whatever this job gates (a PR, a tag, a nightly schedule) for six "
        f"hours instead of failing in bounded time."
    )
    if isinstance(timeout, str):
        literals = [int(n) for n in _TIMEOUT_EXPR_INT_RE.findall(timeout)]
        assert literals, (
            f"{workflow_name}'s {job_id!r} job's timeout-minutes is the "
            f"string {timeout!r} with no integer literal in it at all -- "
            f"this looks like a typo, not a GitHub Actions expression."
        )
        for literal in literals:
            assert 0 < literal <= _MAX_SANE_TIMEOUT_MINUTES, (
                f"{workflow_name}'s {job_id!r} job's timeout-minutes "
                f"expression {timeout!r} contains {literal}, outside "
                f"1..{_MAX_SANE_TIMEOUT_MINUTES}."
            )
        return
    assert isinstance(timeout, int) and 0 < timeout <= _MAX_SANE_TIMEOUT_MINUTES, (
        f"{workflow_name}'s {job_id!r} job has `timeout-minutes: {timeout!r}`, "
        f"outside 1..{_MAX_SANE_TIMEOUT_MINUTES}. A bound at or near GitHub's "
        f"own 360 default is not a hang catcher. If a job genuinely needs "
        f"longer, raise _MAX_SANE_TIMEOUT_MINUTES in the same change and say "
        f"what got slower."
    )


def find_timeout_problems(root: Path) -> list[str]:
    """Scan every workflow under `root/.github/workflows/` and return a
    problem string per job that fails `_assert_timeout_is_bounded`, instead
    of raising -- the same `find_problems(root) -> list[str]` shape
    `test_apt_bounded.py` uses, so a synthetic `tmp_path` tree can prove the
    widened gate actually fires rather than only proving it passes on this
    repo's own clean tree.

    Deliberately reuses `_assert_timeout_is_bounded` itself (catching the
    `AssertionError`) rather than re-deriving the bound logic here: a second,
    parallel implementation could drift from the real one and pass its own
    tests while the real gate silently regressed.
    """
    problems: list[str] = []
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return problems
    for path in _workflow_files(workflows_dir):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in _bounded_jobs(workflow).items():
            try:
                _assert_timeout_is_bounded(path.name, job_id, job.get("timeout-minutes"))
            except AssertionError as exc:
                problems.append(str(exc))
    return problems


def _write_timeout_workflow(root: Path, name: str, jobs_body: str) -> None:
    workflows_dir = root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(
        f"""\
name: example
on: [push]
jobs:
{jobs_body}
""",
        encoding="utf-8",
    )


def test_find_timeout_problems_on_a_clean_tree_is_empty(tmp_path: Path) -> None:
    _write_timeout_workflow(
        tmp_path,
        "clean.yml",
        "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n"
        "    steps:\n      - run: echo hi\n",
    )
    assert find_timeout_problems(tmp_path) == []


def test_missing_bound_is_caught(tmp_path: Path) -> None:
    """The MAJOR review finding this whole block exists to fix: before this
    change, nothing proved the widened gate could fail at all -- only a
    hand-run recorded in the PR body ("11 failed, 34 passed") backed it."""
    _write_timeout_workflow(
        tmp_path,
        "missing.yml",
        "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "missing.yml" in problems[0] and "declares no" in problems[0]


def test_zero_bound_is_caught(tmp_path: Path) -> None:
    _write_timeout_workflow(
        tmp_path,
        "zero.yml",
        "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 0\n"
        "    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "zero.yml" in problems[0] and "outside 1.." in problems[0]


def test_out_of_range_bound_is_caught(tmp_path: Path) -> None:
    """360 -- GitHub's own job default -- is exactly the value #855 exists to
    reject: a bound AT the default is the default wearing a number, not a
    hang catcher."""
    _write_timeout_workflow(
        tmp_path,
        "toolong.yml",
        "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 360\n"
        "    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "toolong.yml" in problems[0] and "outside 1.." in problems[0]


def test_bare_string_bound_with_no_integer_is_caught(tmp_path: Path) -> None:
    _write_timeout_workflow(
        tmp_path,
        "barestring.yml",
        '  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: "soon"\n'
        "    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "barestring.yml" in problems[0] and "no integer literal" in problems[0]


def test_expression_with_out_of_range_literal_is_caught(tmp_path: Path) -> None:
    """The string-expression branch (`ci.yml`'s `python` job shape) had only
    ONE all-green real input (`inputs.sdk_parity`, which carries no digits at
    all) backing it -- this fixture forces the branch to actually reject
    something."""
    _write_timeout_workflow(
        tmp_path,
        "expr.yml",
        "  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: ${{ inputs.big && 400 || 30 }}\n"
        "    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "expr.yml" in problems[0] and "400" in problems[0]


def test_expression_digits_embedded_in_an_identifier_are_not_extracted(
    tmp_path: Path,
) -> None:
    """tan-cli#854/#855 review finding: `${{ inputs.py311 && 60 || 30 }}`
    must bound on 60/30, never on the phantom `311` inside the identifier
    `py311` -- proves `_TIMEOUT_EXPR_INT_RE`'s anchoring, not just its
    presence."""
    _write_timeout_workflow(
        tmp_path,
        "identdigits.yml",
        "  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: ${{ inputs.py311 && 60 || 30 }}\n"
        "    steps:\n      - run: echo hi\n",
    )
    assert find_timeout_problems(tmp_path) == []


def test_expression_digit_adjacent_to_a_dot_is_not_extracted(tmp_path: Path) -> None:
    """A plain `\\b\\d+\\b` is NOT enough: `\\b` sits between a word char and a
    non-word char, and `.` is a NON-word char, so `\\b\\d+\\b` still matches a
    digit run separated from its neighbour only by a dot -- e.g. the phantom
    `500` in `${{ matrix.v1.500 && 60 || 30 }}` (a decimal-looking literal,
    not a real int bound). `_TIMEOUT_EXPR_INT_RE`'s lookaround explicitly
    excludes `.` as well as identifier chars, so this fixture is clean here
    and would fail under a `\\b\\d+\\b` mutation (500 is outside 1..120)."""
    _write_timeout_workflow(
        tmp_path,
        "dotdigit.yml",
        "  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: ${{ matrix.v1.500 && 60 || 30 }}\n"
        "    steps:\n      - run: echo hi\n",
    )
    assert find_timeout_problems(tmp_path) == []


def test_local_reusable_workflow_caller_is_exempt(tmp_path: Path) -> None:
    """The `uses:`-caller exclusion (release.yml's `gates` / `python-gates`
    shape) had only two all-green real inputs backing it -- this fixture
    proves a LOCAL caller is actually skipped, not merely that it happens not
    to be flagged today."""
    _write_timeout_workflow(
        tmp_path,
        "localcaller.yml",
        "  gates:\n    uses: ./.github/workflows/other.yml\n",
    )
    assert find_timeout_problems(tmp_path) == []


def test_cross_repo_uses_caller_is_not_exempt(tmp_path: Path) -> None:
    """tan-cli#854/#855 review finding: a job that calls a workflow in
    ANOTHER repo has no local file for this gate to walk and bound instead,
    so it must NOT be swept into the same exemption as a local caller.
    Verified latent, not live -- no cross-repo caller exists in this repo
    today."""
    _write_timeout_workflow(
        tmp_path,
        "crosscaller.yml",
        "  gates:\n    uses: some-org/repo/.github/workflows/x.yml@v1\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "crosscaller.yml" in problems[0] and "declares no" in problems[0]


def test_a_dot_yaml_workflow_is_scanned_too(tmp_path: Path) -> None:
    """The `*.yaml` half of the widening (`_workflow_files`) had ZERO test
    coverage: every other fixture in this module writes a `.yml` file, so a
    mutation back to `sorted(directory.glob("*.yml"))` left every other test
    green. Write a `.yaml` file with an unbounded job and require it show up
    -- this is the one fixture that actually exercises the `*.yaml` glob."""
    _write_timeout_workflow(
        tmp_path,
        "zz.yaml",
        "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
    )
    problems = find_timeout_problems(tmp_path)
    assert len(problems) == 1
    assert "zz.yaml" in problems[0] and "declares no" in problems[0]


def test_this_repos_own_workflows_produce_no_timeout_problems() -> None:
    """The real thing this gate exists to guard, in the same
    `find_problems(REPO_ROOT) == []` shape `test_apt_bounded.py` closes on --
    kept alongside (not instead of) the parametrised
    `test_every_job_in_every_workflow_is_bounded` below, which gives a
    per-case failure name in a test run rather than one bundled assertion."""
    problems = find_timeout_problems(REPO_ROOT)
    assert problems == [], "\n".join(problems)


#: EVERY workflow file's bounded-job count, mapped to the MINIMUM it must
#: still contribute -- not just presence. Presence alone let a shrink from
#: `release.yml`'s real 5 bounded jobs (`verify-version`, `build`, `release`,
#: `publish_npm`, `release_gate` -- `gates`/`python-gates` stay excluded as
#: LOCAL `uses:` callers) down to 1 pass silently, since the old check only
#: asked "is release.yml in the set at all". Floored for all ELEVEN non-parity
#: files, not just the four tan-cli#855 named -- a review round on this same
#: gate pointed out the other six (`ci.yml`, `clean-host.yml`,
#: `e2e-container.yml`, `getting-started.yml`, `pin-move-verify.yml`,
#: `version-identity.yml`) could shrink silently too, and there is no reason
#: this floor should apply to only the four #855 happened to name.
#: `parity.yml` is deliberately absent: `_PARITY_JOBS` /
#: `test_every_parity_job_is_bounded` already give it the same anti-shrink
#: coverage, named per-job rather than as a bare count. Counts below are the
#: actual current counts; a future PR that drops a job updates this dict on
#: purpose, the same anti-shrink shape `_PARITY_JOBS` gives `parity.yml`.
_MIN_BOUNDED_JOBS = {
    "ci.yml": 4,
    "clean-host.yml": 2,
    "e2e-container.yml": 2,
    "getting-started.yml": 1,
    "pin-move-verify.yml": 1,
    "planner-resync.yml": 1,
    "python-binaries.yml": 4,
    "release-combination.yml": 2,
    "release.yml": 5,
    "unsharded-python-canary.yml": 1,
    "version-identity.yml": 1,
}


def test_the_every_workflow_job_set_is_not_empty():
    """Anti-vacuity for the generalised test, mirroring the pull_request-only
    version below. If `_bounded_jobs` or the `jobs:` parse ever stopped
    matching, every parametrised case would vanish and the checks would pass
    having examined nothing. Also guards against a silent SHRINK: presence of
    a workflow name in the collected set says nothing about how many of its
    jobs actually got counted, so every non-`parity.yml` workflow is
    additionally held to a minimum bounded-job count (`_MIN_BOUNDED_JOBS`);
    `parity.yml` gets the same coverage from `_PARITY_JOBS` instead. And a
    COMPLETENESS check: `_MIN_BOUNDED_JOBS` naming exactly eleven of today's
    files says nothing about tomorrow's twelfth -- a newly added workflow
    would carry no floor at all and could grow or shrink invisibly until
    someone remembered to add an entry for it by hand."""
    assert set(_MIN_BOUNDED_JOBS) | {"parity.yml"} == {
        p.name for p in _workflow_files(WORKFLOWS)
    }, (
        "_MIN_BOUNDED_JOBS (plus parity.yml, covered separately by "
        "_PARITY_JOBS) does not match the workflow files actually under "
        f"{WORKFLOWS} -- a workflow file was added or removed without "
        "updating _MIN_BOUNDED_JOBS, so it currently carries NO minimum-job "
        "floor at all."
    )
    cases = _every_workflow_job_cases()
    assert cases, "no (workflow, job) pairs were collected across .github/workflows/ at all"
    counts: dict[str, int] = {}
    for name, _ in cases:
        counts[name] = counts.get(name, 0) + 1
    for expected, minimum in _MIN_BOUNDED_JOBS.items():
        assert expected in counts, (
            f"{expected} contributed no bounded job to this gate ({sorted(counts)}) "
            f"-- it is named in `_MIN_BOUNDED_JOBS`; if it now has zero bounded "
            f"jobs, `_bounded_jobs`/`_load` stopped parsing it correctly."
        )
        assert counts[expected] >= minimum, (
            f"{expected} contributed only {counts[expected]} bounded job(s) to "
            f"this gate, below the {minimum} it had when this floor was set -- "
            f"a job disappeared from `_bounded_jobs`'s view of this file "
            f"(renamed, refactored into a `uses:` caller, or the parse broke) "
            f"without anyone updating `_MIN_BOUNDED_JOBS` to say that was "
            f"intentional."
        )


@pytest.mark.parametrize(
    "name,job_id",
    _every_workflow_job_cases(),
    ids=[f"{n}::{j}" for n, j in _every_workflow_job_cases()],
)
def test_every_job_in_every_workflow_is_bounded(name, job_id):
    """tan-cli#854/#855: the generalised widening. Walks every job in every
    workflow file (not just the `pull_request` set `_PARITY_JOBS` and
    `test_every_parity_job_is_bounded` below stay scoped to), skipping `uses:`
    callers, and asserts each carries a sane `timeout-minutes`."""
    path = dict(_all_workflows())[name]
    jobs = _bounded_jobs(_load(path))
    assert job_id in jobs, (
        f"{name} has no bounded job {job_id!r} any more (present: "
        f"{sorted(jobs)}) -- this case was collected at collection time and "
        f"the file changed since; re-run pytest to regenerate the case list."
    )
    _assert_timeout_is_bounded(name, job_id, jobs[job_id].get("timeout-minutes"))


def test_the_pull_request_workflow_set_is_not_empty():
    """Anti-vacuity. Every parametrised case below draws from this set; if the
    `on:`-parsing above ever stopped matching (the boolean-True quirk is
    exactly the kind of thing a YAML library change moves), the checks would
    all pass having examined nothing at all."""
    names = [name for name, _ in _pull_request_workflows()]
    assert names, (
        "no workflow under .github/workflows/ parses as running on "
        "`pull_request` -- the `on:` key is not being read correctly (see "
        "_triggers), not that the repo stopped testing pull requests"
    )
    assert "parity.yml" in names, (
        f"parity.yml is not among the `pull_request` workflows ({names}) -- "
        f"it is the subject of this whole gate"
    )


@pytest.mark.parametrize(
    "name", [n for n, _ in _pull_request_workflows()], ids=lambda n: n
)
def test_every_pull_request_workflow_supersedes_its_own_superseded_runs(name):
    workflow = _load(dict(_pull_request_workflows())[name])
    concurrency = workflow.get("concurrency")
    assert concurrency, (
        f"{name} runs on `pull_request` and declares no top-level "
        f"`concurrency:` -- a newer push to a PR leaves the previous run "
        f"going to completion. ci.yml:78 and clean-host.yml:170 state the "
        f"house rule; copy it rather than inventing a new one (and read "
        f"parity.yml's own block first if this workflow has a "
        f"`repository_dispatch` trigger, which changes the group key)."
    )
    cancel = str(concurrency.get("cancel-in-progress", "")).strip()
    assert "pull_request" in cancel or cancel.lower() == "true", (
        f"{name}'s `cancel-in-progress` is {cancel!r}: it neither reads "
        f"`true` nor keys off `github.event_name == 'pull_request'`, so a "
        f"superseded PR run is still not cancelled. The group alone does not "
        f"cancel anything -- it only queues."
    )


def test_parity_never_collapses_dispatched_runs_into_one_group():
    """The regression this gate exists for. A flat `parity-${{ github.ref }}`
    is the tempting "match ci.yml" simplification and it is wrong HERE,
    because `repository_dispatch` fires from the default branch and every
    dispatched run would share that one ref."""
    concurrency = _load(PARITY).get("concurrency")
    assert concurrency and concurrency.get("group"), (
        "parity.yml declares no top-level `concurrency.group` at all, so "
        "there is no key for this test to judge. The sibling test above says "
        "what to add; read parity.yml's own block comment before copying "
        "ci.yml's, because the `repository_dispatch` trigger changes the key."
    )
    group = concurrency["group"]
    assert "github.run_id" in group, (
        f"parity.yml's concurrency group is {group!r} and does not include "
        f"`github.run_id`. Every `repository_dispatch` run is raised from the "
        f"default branch, so a `github.ref`-only key puts them all in one "
        f"group: with one run in progress and two more arriving, the middle "
        f"`client_payload.sdk_ref` gets cancelled while it waits and never "
        f"runs at all. See tan-cli#485 for the cost of an unwatched drift "
        f"signal; an ABSENT one cannot even be watched."
    )
    assert "client_payload" not in group, (
        f"parity.yml's concurrency group is {group!r} and interpolates a "
        f"`client_payload` field. A `repository_dispatch` payload is "
        f"attacker-supplied and a group key is a workflow-level expression "
        f"evaluated before any job starts. `github.run_id` separates the runs "
        f"just as well and puts no untrusted string in that position."
    )
    assert "pull_request" in group, (
        f"parity.yml's concurrency group is {group!r}: it no longer "
        f"distinguishes `pull_request` runs, so PR pushes stopped "
        f"superseding each other -- the original tan-cli#812 defect, back."
    )


@pytest.mark.parametrize("job_id", _PARITY_JOBS)
def test_every_parity_job_is_bounded(job_id):
    """The anti-shrink check tan-cli#854 asked to keep ALONGSIDE the
    generalised `test_every_job_in_every_workflow_is_bounded` above, not
    replaced by it: `_PARITY_JOBS` is a literal tuple precisely so that
    DELETING a job from `parity.yml` is visible here as a job someone must
    deliberately drop from the tuple, rather than a silent shrink of what a
    derived/generalised set happens to cover this run. Four of `parity.yml`'s
    jobs ARE required contexts (tan-cli#812); a wedged leg on one of them
    holds every PR for GitHub's 360-minute default.
    """
    jobs = _load(PARITY)["jobs"]
    assert job_id in jobs, (
        f"parity.yml has no job {job_id!r} (present: {sorted(jobs)}) -- if it "
        f"was renamed, rename it in _PARITY_JOBS too; if it was removed, drop "
        f"it from that tuple deliberately rather than leaving this gate "
        f"asserting nothing about it"
    )
    _assert_timeout_is_bounded("parity.yml", job_id, jobs[job_id].get("timeout-minutes"))
