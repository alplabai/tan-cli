# SPDX-License-Identifier: Apache-2.0
"""Gate: `tests/conftest.py`'s `tan_under_test` fixture actually refuses to
run against a `tan` resolved from outside this repository (tan-cli#423,
tan-cli#665).

## Why this file exists

`tan_under_test` is the detection half of tan-cli#665: a bare
`pip install -e ./python` run without an active venv writes an editable
install into user site-packages, and from that moment every bare `python3`
process on the box resolves `import tan` to whichever checkout was installed
last -- regardless of which worktree it is actually running in. Measured on
the shared dev box: a full `pytest tests -q` on an unrelated branch reported
`681 failed, 3298 passed, ..., 17 errors` for reasons that had nothing to do
with the branch under test (tan-cli#665's own "Measured" section).

`tan_under_test` (`tests/conftest.py`) is supposed to convert that silent
wrong answer into a named refusal at session start. Nothing in this suite
previously exercised the refusal path itself -- every existing consumer of
this fixture is a NORMAL test run, where the fixture is expected to pass
silently, so a suite-wide green bar says nothing about whether the guard
would actually fire the day it needs to. A gate that has never been made to
fail is not a gate (the same argument `tests/gates/test_probe_tool_
inventory.py` makes for `PROBE_TOOLS`).

## How the mutant is applied

`tan_under_test` is decorated with `@pytest.fixture`; pytest exposes the raw
function via `.__wrapped__`, so it can be called directly like any other
function, outside of a pytest session, with `sys.path` rigged first. Run in a
SEPARATE `sys.executable` subprocess rather than in-process: the fixture
mutates `sys.path`/`os.environ["PYTHONPATH"]` and does a real `import tan`,
and doing that against a decoy package inside the CURRENTLY RUNNING test
process would corrupt `sys.modules['tan']` for every test that runs after it
in the same session.

The decoy is a real, on-disk `tan/__init__.py` outside this repository's
`python/` tree -- not a mock of the fixture's logic, but the exact condition
(`import tan` resolving to a path outside `repo_python`) the fixture's own
docstring says a hijacked editable install produces. Whether the real hijack
mechanism is a `sys.path` entry or a higher-priority `sys.meta_path` finder
(as pip's PEP 660 editable installs use) does not matter to the assertion
under test: `tan_under_test` only ever inspects the RESOLVED `tan.__file__`,
never how it was resolved, so a `sys.path`-based decoy exercises the exact
same code path a real hijack would.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

#: `tests/gates/` -> `tests` -> `python` (the same `repo_python` the fixture
#: itself computes as `Path(__file__).resolve().parents[1]` from
#: `tests/conftest.py`).
REPO_PYTHON = Path(__file__).resolve().parents[2]


def _invoke_guard(*, decoy_parent: str | None) -> subprocess.CompletedProcess[str]:
    """Run `tan_under_test.__wrapped__()` in a fresh interpreter, optionally
    with `decoy_parent` inserted onto `sys.path` ahead of this repo's own
    `python/`, and report what happened rather than asserting anything --
    the two tests below each read this differently."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_PYTHON)!r})
        decoy_parent = {decoy_parent!r}
        if decoy_parent is not None:
            sys.path.insert(0, decoy_parent)
        from tests.conftest import tan_under_test
        tan_under_test.__wrapped__()
        print("GUARD-DID-NOT-REFUSE")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_guard_refuses_a_tan_resolved_from_outside_this_repo(tmp_path):
    """The mutant: a decoy `tan` package planted outside `python/`, put
    ahead of the real one on `sys.path` -- the same externally observable
    shape a hijacked editable install produces. `tan_under_test` must FAIL
    LOUDLY, not skip and not silently pass."""
    decoy_pkg = tmp_path / "tan"
    decoy_pkg.mkdir()
    (decoy_pkg / "__init__.py").write_text(
        "# decoy tan for tests/gates/test_tan_under_test_guard.py\n",
        encoding="utf-8",
    )

    proc = _invoke_guard(decoy_parent=str(tmp_path))

    assert proc.returncode != 0, (
        "the guard did NOT refuse a `tan` resolved from outside this repo -- "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}. tan-cli#665's whole "
        "premise is that this refusal is what stands between a hijacked "
        "editable install and hundreds of misleading pytest failures; a "
        "guard that cannot fail here is not a guard."
    )
    assert "GUARD-DID-NOT-REFUSE" not in proc.stdout, proc.stdout
    assert "is NOT under this" in proc.stderr, (
        f"the guard failed, but not with its own named refusal message -- "
        f"stderr={proc.stderr!r}. A different, unrelated failure would hide "
        "the real one this test exists to prove."
    )
    assert "tan-cli#423" in proc.stderr, proc.stderr
    assert str(decoy_pkg.resolve()) in proc.stderr or str(tmp_path.resolve()) in proc.stderr, (
        f"the refusal message does not name the resolved (decoy) path -- stderr={proc.stderr!r}"
    )


def test_the_guard_passes_for_this_repos_own_tan():
    """The control: no decoy on `sys.path`, so `import tan` resolves the
    real `python/tan` under this checkout. The guard must pass silently --
    this is what every ordinary `pytest tests -q` run relies on."""
    proc = _invoke_guard(decoy_parent=None)

    assert proc.returncode == 0, (
        f"the guard refused a legitimate, unhijacked `tan` -- stdout="
        f"{proc.stdout!r} stderr={proc.stderr!r}. A guard that also fires on "
        "the correct setup is worse than no guard: it would refuse every "
        "developer's and every CI runner's ordinary `pip install -e ./python`."
    )
    assert "GUARD-DID-NOT-REFUSE" in proc.stdout, proc.stdout
