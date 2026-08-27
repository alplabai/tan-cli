# SPDX-License-Identifier: Apache-2.0
"""Gate: the tan-cli#903 collection-time HOME pre-flight (`tests/conftest.py`)
actually does what its own 181 lines claim (PR #916 review, Major 2).

## Why this file exists

Nothing exercised `_home_scrubbed_environ`, `_run_version_probe`,
`_home_preflight_failure`, `_skip_home_preflight_requested` or the
`pytest_configure` hook itself before this file -- the whole check ran on
every OS, on every test, with zero automated coverage. The PR's own thesis is
that `tan_under_test`'s probe silently stopped covering the tan-cli#903 case
because nothing pinned WHEN it ran relative to the scrub fixture; the same rot
vector -- a helper that quietly stops doing what its docstring says -- is
exactly what is unguarded here. "A gate that cannot fail is not a gate" (the
same argument `test_probe_tool_inventory.py` and `test_tan_under_test_guard.py`
make) applies to this file's own subject as much as to either of theirs.

## What is stubbed, and what is real

`_home_scrubbed_environ` and `_skip_home_preflight_requested` are pure
functions of their inputs -- tested directly, no stub needed.

`_run_version_probe` and `_home_preflight_failure` spawn a REAL subprocess by
default; that subprocess's behaviour is exactly what Major 1's fix (this PR)
depends on being able to control independently for its two probes (scrubbed
vs control), so `subprocess.run` is monkeypatched to a stub that returns a
scripted `CompletedProcess`/raises a scripted exception, and the returned
diagnostic text is asserted against. This is what makes "the SyntaxError
case" and "the missing-deps case" both reproducible in a hermetic unit test
without actually breaking an interpreter.

The `pytest_configure` WIRING -- does a non-`None` diagnostic really abort the
session with `pytest.UsageError`, does the escape hatch really bypass it, is
a worker process really skipped -- is proven with `pytester.runpytest_
subprocess`, a REAL nested pytest session, rather than calling the hook
function directly: `pytest.UsageError` raised from `pytest_configure` is
pytest's OWN machinery for turning that into "collect nothing, exit 4, print
this message", and only running a real session proves that machinery is still
wired up the way this file assumes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    REAL_ENVIRON,
    TAN_TEST_SKIP_HOME_PREFLIGHT,
    _home_preflight_failure,
    _home_scrubbed_environ,
    _run_version_probe,
    _skip_home_preflight_requested,
    _with_repo_pythonpath,
)

# ---------------------------------------------------------------------------
# `_home_scrubbed_environ`
# ---------------------------------------------------------------------------


def test_home_scrubbed_environ_repoints_home_and_userprofile(tmp_path):
    """The one thing the whole pre-flight depends on: both names a scrubbed-
    HOME subprocess could read are repointed at the throwaway directory, on
    EITHER platform's spelling, regardless of which OS runs this test."""
    scrubbed = _home_scrubbed_environ(tmp_path)

    assert scrubbed["HOME"] == str(tmp_path)
    assert scrubbed["USERPROFILE"] == str(tmp_path)


def test_home_scrubbed_environ_removes_alp_sdk_root(tmp_path, monkeypatch):
    """If this deletion regressed, the probe would resolve a DIFFERENT SDK
    than a clean CI runner ever would -- the exact non-hermetic hazard the
    module docstring opens with, now reproduced inside the pre-flight itself."""
    monkeypatch.setitem(REAL_ENVIRON, "ALP_SDK_ROOT", "/somewhere/unrelated")

    scrubbed = _home_scrubbed_environ(tmp_path)

    assert "ALP_SDK_ROOT" not in scrubbed


def test_home_scrubbed_environ_does_not_mutate_real_environ(tmp_path):
    """Built from a COPY of `REAL_ENVIRON` -- if this ever became an in-place
    mutation, the second call in the same session (the control probe, Major
    1) would observe the FIRST call's throwaway HOME instead of the real
    one, silently breaking the very comparison Major 1 exists to make."""
    before = dict(REAL_ENVIRON)

    _home_scrubbed_environ(tmp_path)

    assert REAL_ENVIRON == before


# ---------------------------------------------------------------------------
# `_skip_home_preflight_requested` -- the minor: bare truthiness let `=0`
# DISABLE the check, the opposite of what a developer setting `=0` means.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        ("on", True),
        (" 1 ", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("2", False),
        ("", False),
    ],
)
def test_skip_home_preflight_requested_is_not_bare_truthiness(monkeypatch, value, expected):
    """The regression this pins: `os.environ.get(VAR)` alone treats ANY
    non-empty string as truthy, so `TAN_TEST_SKIP_HOME_PREFLIGHT=0` would
    SKIP the pre-flight under that reading -- exactly backwards from what a
    developer setting `=0` means. `"2"` is in here too: only the four named
    words count, not "any digit"."""
    monkeypatch.setenv(TAN_TEST_SKIP_HOME_PREFLIGHT, value)

    assert _skip_home_preflight_requested() is expected


def test_skip_home_preflight_requested_defaults_to_false_when_unset(monkeypatch):
    monkeypatch.delenv(TAN_TEST_SKIP_HOME_PREFLIGHT, raising=False)

    assert _skip_home_preflight_requested() is False


# ---------------------------------------------------------------------------
# `_run_version_probe` -- the minor: a hang and a failed spawn are DIFFERENT
# claims and must not share one message, and both must name the hatch.
# ---------------------------------------------------------------------------


def test_run_version_probe_reports_a_hang_distinctly_from_a_failed_spawn(monkeypatch):
    """A `TimeoutExpired` means the child DID spawn and just never returned --
    conflating that with "could not even SPAWN" tells the reader the wrong
    thing happened."""

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["tan"], timeout=60)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    message = _run_version_probe({}, "under a scrubbed HOME")

    assert isinstance(message, str)
    assert "HUNG" in message
    assert "did not spawn" not in message.lower()
    assert "DID spawn" in message
    assert TAN_TEST_SKIP_HOME_PREFLIGHT in message


def test_run_version_probe_reports_a_failed_spawn_distinctly_from_a_hang(monkeypatch):
    def _raise_oserror(*_args, **_kwargs):
        raise OSError("no such file or directory: python3")

    monkeypatch.setattr(subprocess, "run", _raise_oserror)

    message = _run_version_probe({}, "under a scrubbed HOME")

    assert isinstance(message, str)
    assert "could not even SPAWN" in message
    assert "HUNG" not in message
    assert TAN_TEST_SKIP_HOME_PREFLIGHT in message


def test_run_version_probe_passes_through_a_completed_process(monkeypatch):
    sentinel = subprocess.CompletedProcess(args=["tan"], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: sentinel)

    result = _run_version_probe({}, "under a scrubbed HOME")

    assert result is sentinel


# ---------------------------------------------------------------------------
# `_home_preflight_failure` -- Major 1's fix. Each stub below plays the role
# of a specific `sys.executable -m tan --version` outcome; the assertions are
# what MUTATION-prove the fix, per the PR review:
#
#   * "the SyntaxError-in-a-clean-venv case" == BOTH probes fail identically
#     -> must say "broken in this tree regardless of HOME", must NOT print
#     the venv recipe.
#   * "the genuine missing-deps case" == only the scrubbed probe fails, the
#     control succeeds -> must still print the venv recipe.
#
# `subprocess.run` is stubbed to answer differently depending on whether the
# call's `env` carries the throwaway HOME (the scrubbed probe) or
# `REAL_ENVIRON`'s own HOME (the control) -- the same signal
# `_home_preflight_failure` itself uses to build the two environments, so the
# stub cannot accidentally answer the wrong probe for the wrong reason.
# ---------------------------------------------------------------------------


def _stub_run(*, scrubbed_returncode: int, control_returncode: int):
    """A `subprocess.run` replacement that tells the scrubbed probe and the
    control probe apart by whether `env["HOME"]` still matches
    `REAL_ENVIRON["HOME"]`, and answers each with its own returncode.

    `HOME` only, deliberately -- `_home_preflight_failure`'s control call
    passes `REAL_ENVIRON` through with `HOME` untouched (only `ALP_SDK_ROOT`
    also popped, matching the scrubbed probe), and on POSIX that dict has no
    `USERPROFILE` key at all (`_home_scrubbed_environ` is what INTRODUCES
    `USERPROFILE`, only for the scrubbed probe); requiring both to match
    would make the control probe indistinguishable from itself.
    """
    real_home = REAL_ENVIRON.get("HOME")

    def _run(cmd, *, env, capture_output, text, timeout):  # noqa: ARG001
        is_control = env.get("HOME") == real_home
        returncode = control_returncode if is_control else scrubbed_returncode
        stderr = "control stderr\n" if is_control else "scrubbed stderr\n"
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout="", stderr=stderr
        )

    return _run


def test_home_preflight_failure_is_none_when_the_scrubbed_probe_succeeds(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _stub_run(scrubbed_returncode=0, control_returncode=0)
    )

    assert _home_preflight_failure() is None


def test_home_preflight_failure_blames_home_when_only_the_scrubbed_probe_fails(monkeypatch):
    """The genuine missing-deps case (tan-cli#903's original incident): the
    control (real HOME) succeeds, so HOME really is the cause -- the venv
    recipe MUST still appear."""
    monkeypatch.setattr(
        subprocess, "run", _stub_run(scrubbed_returncode=1, control_returncode=0)
    )

    problem = _home_preflight_failure()

    assert problem is not None
    assert "broken in this tree regardless of HOME" not in problem
    assert "python3.12 -m venv .venv" in problem, (
        f"the venv-repair recipe is missing from a genuinely HOME-caused "
        f"failure: {problem!r}"
    )
    assert "scrubbed stderr" in problem
    assert TAN_TEST_SKIP_HOME_PREFLIGHT in problem


def test_home_preflight_failure_does_not_blame_home_when_both_probes_fail(monkeypatch):
    """Major 1's mutant: `tan --version` is simply broken (mutation-proves
    the SyntaxError-in-a-clean-venv scenario from the review) -- BOTH the
    scrubbed probe and the real-HOME control fail. The diagnostic must say
    so plainly and must NOT hand out the venv recipe, which would send a
    developer chasing a HOME cause that is not there."""
    monkeypatch.setattr(
        subprocess, "run", _stub_run(scrubbed_returncode=1, control_returncode=1)
    )

    problem = _home_preflight_failure()

    assert problem is not None
    assert "broken in this tree regardless of HOME" in problem
    assert "python3.12 -m venv .venv" not in problem, (
        f"the venv-repair recipe was printed for a failure that reproduces "
        f"under the developer's own REAL HOME too: {problem!r}"
    )
    assert "scrubbed stderr" in problem
    assert "control stderr" in problem
    assert TAN_TEST_SKIP_HOME_PREFLIGHT in problem


def test_home_preflight_failure_reports_when_the_control_itself_cannot_run(monkeypatch):
    """The control probe can fail to even SPAWN too (a transient fork
    failure, say) -- that must not be silently swallowed or misreported as
    either of the two ordinary outcomes above."""
    real_env = _stub_run(scrubbed_returncode=1, control_returncode=0)

    def _run(cmd, *, env, capture_output, text, timeout):  # noqa: ARG001
        if env.get("HOME") == REAL_ENVIRON.get("HOME"):
            raise OSError("fork failed")
        return real_env(cmd, env=env, capture_output=capture_output, text=text, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", _run)

    problem = _home_preflight_failure()

    assert problem is not None
    assert "could not be run either" in problem or "could not even SPAWN" in problem, problem


def test_home_preflight_failure_uses_repo_pythonpath_for_both_probes(monkeypatch):
    """Both probes must be able to find `tan` at all -- without the
    `python/` prepend (`_with_repo_pythonpath`), the probe fails with an
    unrelated `ModuleNotFoundError` before it can say anything about HOME."""
    seen_pythonpaths: list[str] = []

    def _run(cmd, *, env, capture_output, text, timeout):  # noqa: ARG001
        seen_pythonpaths.append(env.get("PYTHONPATH", ""))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    assert _home_preflight_failure() is None
    assert seen_pythonpaths, "no probe ran at all"
    for pythonpath in seen_pythonpaths:
        assert pythonpath, "a probe ran with no PYTHONPATH set"


def test_with_repo_pythonpath_prepends_without_dropping_an_existing_value():
    result = _with_repo_pythonpath({"PYTHONPATH": "/existing/path"})

    parts = result["PYTHONPATH"].split(os.pathsep)
    assert "/existing/path" in parts
    assert parts[-1] == "/existing/path" or "/existing/path" in parts[1:]


# ---------------------------------------------------------------------------
# `pytest_configure` WIRING -- a REAL nested pytest session via `pytester`.
# ---------------------------------------------------------------------------

#: The forwarding conftest every case below writes into the pytester
#: project. It reuses the REAL `tests.conftest` module -- not a re-
#: implementation of it -- and forces `_home_preflight_failure` to a
#: deterministic, host-independent answer so this test does not depend on
#: whether the ACTUAL interpreter running it happens to survive a scrubbed
#: HOME (that question is already covered, hermetically, by the stubbed-
#: `subprocess.run` tests above).
_FORWARDING_CONFTEST = """
import sys
sys.path.insert(0, {repo_python!r})

import tests.conftest as _tan_conftest

_tan_conftest._home_preflight_failure = lambda: {answer!r}

def pytest_configure(config):
    _tan_conftest.pytest_configure(config)
"""


def _repo_python() -> str:
    import tests.conftest as conftest_module

    return str(Path(conftest_module.__file__).resolve().parents[1])


def test_pytest_configure_aborts_the_session_with_a_usage_error(pytester, monkeypatch):
    """The core of tan-cli#903: a non-`None` diagnostic must stop collection
    ENTIRELY, before any test runs -- not warn, not run a degraded suite.

    Neutralises an ambient `TAN_TEST_SKIP_HOME_PREFLIGHT` before spawning the
    nested session: `pytester.runpytest_subprocess` inherits the parent
    process's environment, so a developer who followed this very PR's own
    failure-message advice and exported the escape hatch to work around an
    unrelated failure would have it silently bypass the forced failure here
    too, and this test would pass for the wrong reason (the abort never
    fires, but nothing runs to prove it either) -- exactly the shape Major 1
    of the #916 review found. The two sibling tests below don't need this:
    they `setenv` the same var themselves, which already wins over anything
    ambient.
    """
    monkeypatch.delenv(TAN_TEST_SKIP_HOME_PREFLIGHT, raising=False)
    pytester.makeconftest(
        _FORWARDING_CONFTEST.format(
            repo_python=_repo_python(), answer="FORCED PROBLEM (tan-cli#903 gate)"
        )
    )
    pytester.makepyfile(test_should_never_run="def test_never_runs():\n    assert False\n")

    result = pytester.runpytest_subprocess()

    assert result.ret != 0, (
        "a forced pre-flight failure did not abort the session at all -- "
        f"outcome: {result.outlines}"
    )
    result.stderr.fnmatch_lines(["*FORCED PROBLEM (tan-cli#903 gate)*"])
    # UsageError must stop pytest before it ever executes the planted test --
    # if it ran (and failed on its own `assert False`), that is a DIFFERENT,
    # much weaker guarantee than "collection never began".
    assert "1 failed" not in "\n".join(result.outlines), result.outlines


def test_pytest_configure_bypass_runs_the_suite_anyway(pytester, monkeypatch):
    """`TAN_TEST_SKIP_HOME_PREFLIGHT=1` must make the SAME forced failure a
    no-op -- proving the escape hatch actually reaches `pytest_configure`,
    not just that `_skip_home_preflight_requested` parses in isolation."""
    pytester.makeconftest(
        _FORWARDING_CONFTEST.format(
            repo_python=_repo_python(), answer="FORCED PROBLEM (should be bypassed)"
        )
    )
    pytester.makepyfile(test_runs_fine="def test_runs_fine():\n    assert True\n")
    monkeypatch.setenv(TAN_TEST_SKIP_HOME_PREFLIGHT, "1")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    assert "FORCED PROBLEM" not in "\n".join(result.outlines), result.outlines


def test_pytest_configure_bypass_rejects_zero(pytester, monkeypatch):
    """The minor this pins end-to-end: `=0` must NOT bypass the check --
    the forced failure must still abort the session."""
    pytester.makeconftest(
        _FORWARDING_CONFTEST.format(
            repo_python=_repo_python(), answer="FORCED PROBLEM (=0 must not bypass)"
        )
    )
    pytester.makepyfile(test_should_never_run="def test_never_runs():\n    assert False\n")
    monkeypatch.setenv(TAN_TEST_SKIP_HOME_PREFLIGHT, "0")

    result = pytester.runpytest_subprocess()

    assert result.ret != 0, (
        f"TAN_TEST_SKIP_HOME_PREFLIGHT=0 bypassed the pre-flight -- outcome: {result.outlines}"
    )
    result.stderr.fnmatch_lines(["*FORCED PROBLEM (=0 must not bypass)*"])


# ---------------------------------------------------------------------------
# `pytest_configure` on an xdist WORKER -- the module docstring's claim that
# this is "proven", made true (#916 review, Major 2). `config.workerinput` is
# only set inside a worker process, so the ordinary forced-failure conftest
# above cannot exercise this branch in isolation: forcing a non-`None` answer
# aborts the CONTROLLER before it ever spawns a worker (the controller's own
# `config.workerinput` is `None`, so it runs the real check first), and the
# session never gets far enough to prove anything about the worker's copy of
# the hook at all.
#
# So this conftest instead RECORDS every call to `_home_preflight_failure`
# (into a file both the controller and any worker process can see) and always
# answers `None` -- no abort, in either process, so both get a chance to run.
# Under `-n 1` that is one controller process plus one worker process, each
# with its OWN `pytest_configure` invocation: if the `workerinput` guard is
# doing its job, only the controller's call is ever recorded. If it were
# disabled (`if False:` in place of the guard), the worker would call it too
# and the file would carry two records instead of one.
# ---------------------------------------------------------------------------

_RECORDING_CONFTEST = """
import sys
sys.path.insert(0, {repo_python!r})

from pathlib import Path

import tests.conftest as _tan_conftest

_calls_path = Path({calls_path!r})

def _recording_preflight():
    with _calls_path.open("a", encoding="utf-8") as fh:
        fh.write("call\\n")
    return None

_tan_conftest._home_preflight_failure = _recording_preflight

def pytest_configure(config):
    _tan_conftest.pytest_configure(config)
"""


def test_pytest_configure_skips_the_check_on_an_xdist_worker(pytester, tmp_path, monkeypatch):
    """A worker process must not re-run the probe: only the CONTROLLER's
    invocation of `pytest_configure` should ever reach `_home_preflight_
    failure` -- the worker's own call must be skipped via `config.workerinput`
    being set. Mutation-proved: replacing that guard with `if False:` in
    `tests/conftest.py::pytest_configure` turns this from `1` recorded call
    into `2` (controller AND worker both call through).

    Neutralises an ambient `TAN_TEST_SKIP_HOME_PREFLIGHT` before spawning the
    nested session, same reason as `test_pytest_configure_aborts_the_session_
    with_a_usage_error` above: `pytester.runpytest_subprocess` inherits the
    parent process's environment, and the recording conftest's own
    `pytest_configure` forwards straight into the REAL `tests.conftest.
    pytest_configure`, which still honours the escape hatch. Left ambient, a
    developer (or CI) with the hatch exported would make BOTH the controller's
    and the worker's `pytest_configure` return before ever reaching the
    `workerinput` guard this test exists to prove, so `calls` would come back
    empty instead of `["call"]` and this test would fail for a reason that has
    nothing to do with xdist.

    Requires `pytest-xdist` on the interpreter running THIS test (it is what
    the nested `-n 1` session below needs to spawn a worker at all) --
    installed alongside `pytest` itself in the two `tests/gates` legs
    (`ci.yml`'s `python` job, `parity.yml`'s `seam1-plan-shape` job)
    specifically so this test RUNS rather than skips there (#916 review,
    Major 2: neither leg installed it before, and no other CI leg runs
    `tests/gates` at all -- `parity.yml`'s cross-OS `python-tests` job
    `--ignore=tests/gates` outright, and none of the sharded `python-tests`
    matrix legs use `pytest-xdist`/`-n` either; they shard via `pytest-shard
    --shard-id=N --num-shards=4`, a different mechanism this test does not
    exercise). `importorskip` stays rather than a hard dependency: a developer
    running `python -m pytest tests/gates` from a bare `pip install pytest`
    venv (no `-e .[dev]`, no xdist) should still get a skip here, not a
    collection error, for the one test in the suite that specifically needs
    xdist PRESENT to prove anything.
    """
    monkeypatch.delenv(TAN_TEST_SKIP_HOME_PREFLIGHT, raising=False)
    pytest.importorskip("xdist")
    calls_path = tmp_path / "calls.log"
    pytester.makeconftest(
        _RECORDING_CONFTEST.format(repo_python=_repo_python(), calls_path=str(calls_path))
    )
    pytester.makepyfile(test_runs_fine="def test_runs_fine():\n    assert True\n")

    result = pytester.runpytest_subprocess("-n", "1", "-p", "xdist")

    result.assert_outcomes(passed=1)
    calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
    assert calls == ["call"], (
        "expected exactly ONE recorded call to _home_preflight_failure (the "
        "controller's) -- a worker process that also called through means "
        "the `config.workerinput` skip in pytest_configure is not doing its "
        f"job: {calls!r}"
    )
