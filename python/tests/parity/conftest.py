# SPDX-License-Identifier: Apache-2.0
"""Makes `test_the_breadth_layer_still_covers_every_board`'s floor survive
`pytest-xdist` (tan-cli#1256).

## The bug

`test_planner_emit_parity.py`'s `_ARTEFACTS_COMPARED` is a plain
module-level `dict`, filled in as a side effect of two parametrized tests
(one per board) and read once, at the end, by
`test_the_breadth_layer_still_covers_every_board`, which asserts floors of
`>= 90` boards and `>= 2900` artefacts. Serially that is exactly one
process, so the dict the floor-check reads is the dict every board test
wrote to.

Under `pytest-xdist` each worker is a SEPARATE process with its OWN import
of the test module, hence its OWN `_ARTEFACTS_COMPARED`. Whichever worker
happens to draw the breadth-floor test item only ever sees the share of
boards *that worker itself* ran -- typically a fraction of the full ~100 --
so the in-test assertion reds on a partial count that has nothing to do with
the SDK's actual breadth ("only 54 boards were measured"). Lowering the
floor per shard was rejected (tan-cli#1256): CI's `parity.yml` runs the
whole module unsharded specifically so the floor means something, and a
per-shard floor would stop catching a whole `GENERATE_MODES` entry
disappearing (the exact regression the floor exists to catch, per that
test's own docstring).

## The fix

Each xdist WORKER publishes its own `_ARTEFACTS_COMPARED` share (plus
whether it drew the breadth-floor test itself) via `config.workeroutput` in
`pytest_sessionfinish` -- xdist's own documented mechanism, ALREADY used
this way by `tests/conftest.py`'s `workerinput` guard. The CONTROLLER
merges every worker's share as each one shuts down (`pytest_testnodedown`,
`node.workeroutput`), boards as a set union with per-board artefact counts
SUMMED (matching `_ARTEFACTS_COMPARED[name] += compared`'s own accumulation
semantics -- the same board can be measured by the stream-mode test on one
worker and the board-tree test on another), and enforces the exact same
floors on the merged total from the controller's own `pytest_sessionfinish`,
once all workers are down.

Two things keep this from being vacuous:

- If no worker ever reports having drawn the breadth-floor test item (it was
  `-k`-deselected, or `tests/parity` was not selected at all this session),
  the controller does nothing -- same as the in-test `pytest.skip` does
  serially for the same situation.
- If xdist is not even running (plain `pytest`, or `pytest-xdist` not
  installed), `config.workerinput`/`workeroutput` are never set and
  `pytest_testnodedown` is never called (guarded with `optionalhook=True`
  below so pytest does not error on an unregistered hookspec when the
  plugin is absent) -- so serial behaviour is untouched, byte for byte: the
  in-test assertion is still the only thing deciding the result.

The in-test assertion itself gains one new early skip -- "running as an
xdist WORKER" -- so it never reds on its own partial share; the controller
check above is what actually enforces the floor in that case.

## The module-identity trap this file had to dodge

`tests/parity/__init__.py` exists but `tests/__init__.py` does not, so
pytest's default ("prepend") import mode collects this directory's test
files under the dotted name `parity.<module>`, NOT `tests.parity.<module>`
-- measured directly (a throwaway `pytest_collectstart` probe printed
`parity.test_planner_emit_parity`). `from tests.parity.test_planner_emit_parity
import _ARTEFACTS_COMPARED` -- the natural-looking spelling, and the one
`test_planner_axis_build_plan_parity.py` itself already uses for its
stateless imports (`SDK`, `_boards`, `_first_diff`) -- resolves through
Python's OWN namespace-package machinery (`tests` has no `__init__.py`, so
`import tests.parity...` is satisfied as an implicit PEP 420 namespace
package) and imports a SECOND, otherwise-identical module object that no
test ever touches. For a stateless import that is invisible; for
`_ARTEFACTS_COMPARED` specifically it is fatal -- collecting the WHOLE
`tests/parity` directory collects `test_planner_axis_build_plan_parity.py`
alphabetically BEFORE `test_planner_emit_parity.py`, so its own top-level
`tests.parity.test_planner_emit_parity` import runs first and lands in
`sys.modules` before pytest's own `parity.test_planner_emit_parity` import
does -- so even picking "the first `sys.modules` entry whose `__file__`
matches" (measured) silently returns the never-mutated duplicate, and the
merged floor read "only 0 boards were measured" even on a session where
every board test genuinely ran. `_worker_artefacts_compared()` below sums
`_ARTEFACTS_COMPARED` across EVERY `sys.modules` entry matching this file's
identity, rather than trusting whichever one import order happens to return
first -- since only the real, pytest-run copy is ever mutated, the
never-touched duplicate contributes `{}` to the sum and the answer comes out
right regardless of which module "wins" the identity lookup.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: `{board dir name: artefacts compared}`, summed across every worker that
#: reported a share. Populated by `pytest_testnodedown`, read by this
#: module's own `pytest_sessionfinish` once every worker is down.
_MERGED_ARTEFACTS_COMPARED: dict[str, int] = {}

#: Whether ANY worker reported having actually drawn
#: `test_the_breadth_layer_still_covers_every_board` this session -- the
#: non-vacuousness guard: with no worker reporting this, the breadth test
#: was never collected/run (`-k`, or `tests/parity` not selected) and the
#: merged-floor check below must stay silent, exactly like the in-test
#: `pytest.skip` does serially for the same case.
_ANY_WORKER_RAN_THE_BREADTH_TEST = False

_BREADTH_TEST_NODEID_SUFFIX = (
    "::test_the_breadth_layer_still_covers_every_board")

_WORKEROUTPUT_KEY = "tan_cli_1256_breadth_floor"

_TARGET_MODULE_FILE = (Path(__file__).parent / "test_planner_emit_parity.py").resolve()


def _worker_artefacts_compared() -> dict[str, int]:
    """This worker's `_ARTEFACTS_COMPARED` share -- found by file identity,
    not by importing a dotted name, for the reason the module docstring above
    spells out (a guessed name resolves to a second, empty copy) -- MERGED
    across every `sys.modules` entry whose `__file__` is
    `test_planner_emit_parity.py`, not just the first one found.

    That merge matters even within ONE worker process: collecting the WHOLE
    `tests/parity` directory also collects `test_planner_axis_build_plan_parity.py`,
    which (alphabetically before `test_planner_emit_parity.py`, so imported
    FIRST) does `from tests.parity.test_planner_emit_parity import (...)` at
    its own module level -- through Python's OWN namespace-package machinery,
    since `tests/__init__.py` does not exist, exactly like the module
    docstring's trap. That import inserts a SECOND module object under the
    dotted name `tests.parity.test_planner_emit_parity` -- pointing at the
    SAME physical file -- into `sys.modules` BEFORE pytest's own collection
    later imports the file again as `parity.test_planner_emit_parity`, which
    is the copy every parametrized test actually mutates. Returning "the
    first `sys.modules` value matching this file" (measured) silently picked
    the FIRST-inserted, NEVER-mutated duplicate and reported "only 0 boards
    were measured" on a session where every board test genuinely ran. Only
    one of any such pair of modules is ever mutated by real test execution,
    so summing every match's dict -- rather than trusting whichever "wins"
    dict identity -- gives the right answer regardless of import order.
    """
    merged: dict[str, int] = {}
    for module in list(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            resolved = Path(module_file).resolve()
        except (OSError, ValueError):
            continue  # an exotic __file__ (frozen/zip-imported) -- not a match
        if resolved != _TARGET_MODULE_FILE:
            continue
        for board, count in getattr(module, "_ARTEFACTS_COMPARED", {}).items():
            merged[board] = merged.get(board, 0) + count
    return merged


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Runs in EVERY process -- each worker AND the controller (or the one
    lone process of a serial run) -- because that is how `pytest_sessionfinish`
    works; the two roles are told apart by `config.workerinput`, the same
    guard `tests/conftest.py::pytest_configure` already uses for the same
    distinction.

    WORKER branch: publish this worker's own `_ARTEFACTS_COMPARED` share,
    plus whether this worker's own `session.items` (post `-k`/`-m`
    deselection) included the breadth-floor test, into
    `config.workeroutput` -- read back by the controller's
    `pytest_testnodedown` below. Writing here, not in a hookwrapper, is
    fine: xdist's OWN `pytest_sessionfinish` hookwrapper (`WorkerInteractor`,
    `xdist/remote.py`) starts before and resumes after every plain
    `pytest_sessionfinish` hookimpl including this one, so `workeroutput` is
    still mutable when this runs and is not sent (`workerfinished` event)
    until after every hookimpl, this one included, has returned.

    CONTROLLER (or serial) branch: if no worker ever reported drawing the
    breadth test, there is nothing to enforce -- return quietly. Otherwise
    check the exact same two floors the in-test assertion checks, against
    the MERGED totals, and fail the whole session if either is short.
    """
    config = session.config
    workeroutput = getattr(config, "workeroutput", None)
    if workeroutput is not None:
        artefacts = _worker_artefacts_compared()
        ran_breadth_test = any(
            item.nodeid.endswith(_BREADTH_TEST_NODEID_SUFFIX)
            for item in session.items
        )
        workeroutput[_WORKEROUTPUT_KEY] = {
            "artefacts": artefacts,
            "ran_breadth_test": ran_breadth_test,
        }
        return

    if not _ANY_WORKER_RAN_THE_BREADTH_TEST:
        # Either this is a serial run (no workers ever existed, so this
        # global can never have been set -- the in-test assertion is the
        # whole story) or xdist ran but no worker drew the breadth test this
        # session. Either way: silence, matching the in-test `pytest.skip`.
        return
    if not _MERGED_ARTEFACTS_COMPARED:
        # The breadth test itself ran on some worker, but no worker ever
        # populated `_ARTEFACTS_COMPARED` (e.g. a `-k` that keeps the
        # breadth test but drops both accumulating tests) -- the in-test
        # `if not _ARTEFACTS_COMPARED: pytest.skip(...)` guard covers this
        # exact shape serially; mirror it here rather than reporting "0
        # boards were measured" as though that were a real regression.
        return
    _enforce_merged_breadth_floor(_MERGED_ARTEFACTS_COMPARED, session)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:  # noqa: ANN001 -- xdist's own type
    """CONTROLLER-side only: `pytest_testnodedown` is an xdist hookSPEC, so
    this hookIMPL only ever fires when the xdist plugin registered that
    spec. `optionalhook=True` is what keeps a plain, xdist-less `pytest` run
    from erroring with "unknown hook" at hookimpl-validation time over a
    hookimpl for a hookspec that, in that run, does not exist.

    Fires once per worker as it shuts down, well before this session's own
    `pytest_sessionfinish` -- xdist tears every worker down before the
    controller's run loop ends (verified against `xdist/dsession.py`:
    `DSession.worker_workerfinished` calls `pytest_testnodedown` directly,
    synchronously, from the same event handling that runs while
    `pytest_runtestloop` is still executing).
    """
    global _ANY_WORKER_RAN_THE_BREADTH_TEST
    payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
    if not payload:
        return
    for board, count in payload["artefacts"].items():
        _MERGED_ARTEFACTS_COMPARED[board] = (
            _MERGED_ARTEFACTS_COMPARED.get(board, 0) + count)
    if payload["ran_breadth_test"]:
        _ANY_WORKER_RAN_THE_BREADTH_TEST = True


def _enforce_merged_breadth_floor(
    artefacts: dict[str, int], session: pytest.Session
) -> None:
    """The exact two assertions
    `test_the_breadth_layer_still_covers_every_board` makes serially,
    against the cross-worker MERGED dict instead of one worker's partial
    share. Mirrors that test's own semantics deliberately, including the
    "thin board" detail, rather than re-deriving a similar-looking check.
    """
    thin = {name: n for name, n in artefacts.items() if n < 2}
    total = sum(artefacts.values())
    problems = []
    if thin:
        problems.append(f"boards that compared almost nothing: {thin}")
    if len(artefacts) < 90:
        problems.append(f"only {len(artefacts)} boards were measured")
    if total < 2900:
        problems.append(f"only {total} artefacts were compared byte for byte")
    if not problems:
        return

    message = (
        "tan-cli#1256: test_the_breadth_layer_still_covers_every_board's "
        "floor failed on the pytest-xdist MERGED total across every "
        "worker (the in-test assertion itself is skipped under xdist, "
        "since one worker only ever sees its own partial share) -- "
        + "; ".join(problems)
    )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, red=True, bold=True)
    else:
        print(message, file=sys.stderr)
    session.testsfailed += 1
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
