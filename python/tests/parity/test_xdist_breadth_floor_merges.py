# SPDX-License-Identifier: Apache-2.0
"""Regression test for tan-cli#1256 BLOCKER 1: the merged xdist breadth
floor must fire for ANY invocation path, not only one that happens to name
`tests/parity` -- see `tests/parity/_xdist_breadth_floor.py`'s own module
docstring for the full mechanism this proves.

Spawns a REAL, nested `pytest -n 2 <path> -k <tiny selection>` subprocess
against THIS actual repo tree -- not a `pytester` sandbox. The bug this
guards is specifically about `python/conftest.py` + `tests/parity/`'s real
layout (a subdirectory `conftest.py` loading, or not, on the xdist
CONTROLLER depending on the invocation path); a throwaway synthetic
`pytester`-built project has neither file and cannot reproduce it.
`path="tests"` is the exact shape that silently passed before this file's
own fix (BLOCKER 1): the controller never loaded a `tests/parity/
conftest.py` there, so nothing ever enforced the floor while every worker
skipped its own judgement -- rc 0, nothing printed.

Kept small deliberately -- this must NOT run the whole ~100-board parity
suite twice per subprocess. The `-k` selection below picks exactly ONE
board (`aen-analog-validate`) plus the breadth test itself, and two
`pytest-xdist` workers rather than the usual six; each nested subprocess
finishes in single-digit-to-low-teens seconds (measured 5-17s locally).

## Where this runs for real, and where it only skips

RUNS for real (both `pytest-xdist` present and `ALP_SDK_ROOT` bound) in
`ci.yml`'s `python` job: it installs `pytest pytest-xdist coverage`
alongside the runner (for an unrelated `tests/gates` case) and binds
`ALP_SDK_ROOT` for the same `python -m pytest tests -q` invocation that
collects this file. That outer invocation passes no `-n`/`--dist` itself,
so the NESTED `-n 2` subprocess this test spawns is not itself running
inside a worker -- nesting is safe.

SKIPS (`pytest.importorskip("xdist")`) in `parity.yml`'s
`python-tests-shard` "parity, whole" leg and its `release-sdk-parity` job:
neither installs `pytest-xdist` (both shard via `pytest-shard` instead, an
entirely different mechanism this test does not exercise), even though
both bind `ALP_SDK_ROOT` and do collect `tests/parity`.

SKIPS (`ALP_SDK_ROOT` unbound) in `ci.yml`'s `python-newest` job, which
binds no alp-sdk checkout at all.

So today this is a REAL, running regression test in exactly one CI leg
(`ci.yml`'s `python` job) and an explicit, named skip everywhere else --
never a silent no-op that merely looks collected.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import sdk_root

PYTHON_ROOT = Path(__file__).resolve().parents[2]

SDK = sdk_root()

#: One real board (`aen-analog-validate`) plus the breadth-floor test
#: itself -- see the module docstring for why this stays this small.
#:
#: `and not merged_floor_fires` is NOT decorative: pytest's `-k` matches by
#: SUBSTRING against every ancestor keyword, including the file stem --
#: `"breadth"` alone is also a substring of THIS FILE's own name
#: (`test_xdist_breadth_floor_merges.py`), so the bare three-term
#: expression self-selects this module's own two parametrized items too.
#: Selected, each spawns its OWN nested `-n 2` subprocess reusing this very
#: selection -- an unbounded recursive spawn, measured directly as an
#: apparent "hang" (a 120s `subprocess.run(timeout=...)` firing with only a
#: couple of dots of progress captured) before this exclusion was added.
#: `--collect-only` with the exclusion present: exactly the 3 intended
#: items, `1681 deselected` -- worth re-checking with `--collect-only` if
#: this string, or this file's own name, ever changes.
_K_SELECTION = (
    "(breadth or (for_every_mode and aen-analog-validate) or "
    "(for_every_board_tree and aen-analog-validate)) and not "
    "merged_floor_fires"
)


def _run_nested(path: str, sdk: Path) -> subprocess.CompletedProcess[str]:
    """A real `pytest -n 2 <path> -k <_K_SELECTION>` subprocess against
    THIS repo tree, `ALP_SDK_ROOT` bound explicitly (not merely inherited)
    so this reproduces regardless of what the outer session's own
    environment happens to carry.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", path, "-n", "2",
         "-k", _K_SELECTION, "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "ALP_SDK_ROOT": str(sdk)},
        check=False,
        # 180s, not the ~15s this measures at when the host is idle: a
        # shared/loaded CI runner (or a busy dev machine) can starve this
        # nested subprocess for a while without the fix under test being
        # wrong -- measured directly, a `-n 8` unrelated session on the
        # same host stretched an idle-15s run past 150s. A hang from an
        # actual regression still fails this test; it just takes longer
        # to say so under contention instead of reporting a false red.
        timeout=180,
    )


@pytest.mark.parametrize("path", ["tests", "tests/parity"],
                         ids=["path-tests", "path-tests-parity"])
def test_the_merged_floor_fires_for_any_invocation_path(path):
    """Both invocation paths must fail identically -- rc 1, the
    `tan-cli#1256` message -- now that the merge hooks are registered from
    `python/conftest.py` (loaded unconditionally for every invocation)
    instead of a `tests/parity/conftest.py` (loaded by the CONTROLLER only
    when the path descends into that directory). Before that fix,
    `path="tests"` exited 0 with nothing printed: the ONE board this `-k`
    selection admits is far under the real floor, so a correctly-firing
    check MUST fail here -- a passing nested run would mean the merge
    silently stopped enforcing anything again, not that the fix still
    works.
    """
    pytest.importorskip("xdist")
    if SDK is None:
        pytest.skip(
            "set ALP_SDK_ROOT to an alp-sdk checkout to run this "
            "regression test")
    result = _run_nested(path, SDK)
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"path={path!r}: expected the merged floor to fail this nested "
        f"session (one board is far under the real floor) -- "
        f"rc={result.returncode}\n{combined}"
    )
    assert "tan-cli#1256" in combined, (
        f"path={path!r}: expected the merged-floor failure message naming "
        f"tan-cli#1256\n{combined}"
    )
