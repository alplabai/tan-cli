# SPDX-License-Identifier: Apache-2.0
"""Makes `test_the_breadth_layer_still_covers_every_board`'s floor survive
`pytest-xdist` (tan-cli#1256), from a location the CONTROLLER always loads.

## The bug

`test_planner_emit_parity.py`'s `_ARTEFACTS_COMPARED` is a plain
module-level `dict`, filled in as a side effect of two parametrized tests
(one per board) and read once, at the end, by
`test_the_breadth_layer_still_covers_every_board`, which asserts floors of
`>= 90` boards and `>= 2900` artefacts (`_BREADTH_MIN_BOARDS` /
`_BREADTH_MIN_ARTEFACTS` in that module). Serially that is exactly one
process, so the dict the floor-check reads is the dict every board test
wrote to.

Under `pytest-xdist` each worker is a SEPARATE process with its OWN import
of the test module, hence its OWN `_ARTEFACTS_COMPARED`. Whichever worker
happens to be assigned the breadth-floor test item only ever sees the share
of boards *that worker itself* ran -- typically a fraction of the full
~100 -- so the in-test assertion reds on a partial count that has nothing
to do with the SDK's actual breadth ("only 54 boards were measured").
Lowering the floor per shard was rejected (tan-cli#1256): CI's `parity.yml`
runs the whole module unsharded specifically so the floor means something,
and a per-shard floor would stop catching a whole `GENERATE_MODES` entry
disappearing (the exact regression the floor exists to catch, per that
test's own docstring).

## The fix

Each xdist WORKER publishes its own `_ARTEFACTS_COMPARED` share (plus its
copy of the floor constants, and whether it collected the breadth-floor
test item itself) via `config.workeroutput` in `pytest_sessionfinish` --
one of xdist's two own documented per-worker-state mechanisms
(`config.workerinput` is the other; `tests/conftest.py::pytest_configure`
already reads `workerinput`, for an unrelated purpose, to tell a worker
process apart from the controller). The CONTROLLER merges every worker's
share as each one shuts down (`pytest_testnodedown`, `node.workeroutput`),
boards as a set union with per-board artefact counts SUMMED (matching
`_ARTEFACTS_COMPARED[name] += compared`'s own accumulation semantics -- the
same board can be measured by the stream-mode test on one worker and the
board-tree test on another), and enforces the exact same floors on the
merged total from the controller's own `pytest_sessionfinish`, once all
workers are down.

Two things keep this from being vacuous:

- If no worker ever reports having collected the breadth-floor test item
  (it was `-k`-deselected, or `tests/parity` was not selected at all this
  session), the controller does nothing -- same as the in-test
  `pytest.skip` does serially for the same situation.
- If xdist is not even running (plain `pytest`, or `pytest-xdist` not
  installed), `config.workerinput`/`workeroutput` are never set and
  `pytest_testnodedown`/`pytest_configure_node` are never called (both
  guarded with `optionalhook=True` below so pytest does not error on an
  unregistered hookspec when the plugin is absent) -- so serial behaviour
  is untouched, byte for byte: the in-test assertion is still the only
  thing deciding the result.

## Why this lives here and not in `tests/parity/conftest.py`

The first cut of this fix (tan-cli#1256) put all of the above in
`tests/parity/conftest.py`. That is exactly wrong for the CONTROLLER half:
pytest only loads a subdirectory's `conftest.py` on a given process when
that process actually collects into the subdirectory, and the xdist
CONTROLLER does not always do that -- measured directly: `pytest -n 2
tests/parity -k ...` merges and fails correctly (rc 1, this file's message),
but `pytest -n 2 tests -k ...` (a broader path -- same items selected,
same workers doing the same work) exits 0 with NOTHING printed, because the
controller process never loaded `tests/parity/conftest.py` and so never
registered its `pytest_testnodedown`/merged `pytest_sessionfinish` at all,
while every WORKER still happily skips its own in-test assertion (it *does*
collect into `tests/parity/`, so it loads fine there). The floor ends up
enforced NOWHERE -- worse than the pre-fix state, which was at least loudly
wrong. `python/conftest.py` (the ROOTDIR conftest, next to `pyproject.toml`)
is the one file pytest loads unconditionally for every invocation
regardless of which path was given -- that is the whole reason
`pytest_plugins` is restricted to living there. Registering THIS module
from that list (`pytest_plugins = [..., "tests.parity._xdist_breadth_floor"]`)
makes the controller-side hooks exist in every process, every time,
independent of invocation path.

## The handshake: never silent, even if this file is somehow not loaded

Loading location fixes the *known* vacuous path. To also refuse a FUTURE
regression in the same shape silently, the worker-side skip in
`test_the_breadth_layer_still_covers_every_board` does not trust "am I an
xdist worker" (an `os.environ` check cannot tell whether the merge check
that is supposed to replace its own judgement is actually loaded) -- it
trusts a HANDSHAKE. `pytest_configure_node` below runs on the CONTROLLER,
once per worker, before that worker's very first message is sent
(`WorkerController.setup()` calls it immediately before
`channel.send((self.workerinput, ...))` -- verified against
`xdist/workermanage.py`), and stamps `HANDSHAKE_KEY` into that worker's own
`workerinput`. Only a worker that actually SEES that stamp skips its own
judgement; a worker running under xdist without this module loaded (this
module missing from `pytest_plugins`, or the whole file deleted) sees no
stamp and falls back to asserting exactly as it did before tan-cli#1256 --
loud and possibly wrong on a partial share, but never silent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: Stamped into every worker's own `config.workerinput` by
#: `pytest_configure_node` below, so the worker-side skip in
#: `test_the_breadth_layer_still_covers_every_board` can tell "the
#: controller-side merge check is loaded and will judge my share" apart
#: from "it silently is not" -- see the handshake section above.
HANDSHAKE_KEY = "tan_cli_1256_merged_floor"

#: `{board dir name: artefacts compared}`, summed across every worker that
#: reported a share. Populated by `pytest_testnodedown`, read by this
#: module's own `pytest_sessionfinish` once every worker is down.
_MERGED_ARTEFACTS_COMPARED: dict[str, int] = {}

#: Whether ANY worker reported having actually collected
#: `test_the_breadth_layer_still_covers_every_board` this session -- the
#: non-vacuousness guard: with no worker reporting this, the breadth test
#: was never collected/run (`-k`, or `tests/parity` not selected) and the
#: merged-floor check below must stay silent, exactly like the in-test
#: `pytest.skip` does serially for the same case.
_ANY_WORKER_RAN_THE_BREADTH_TEST = False

#: `{"boards": int, "artefacts": int}` -- the floors as the workers read
#: them straight off the test module's own `_BREADTH_MIN_BOARDS` /
#: `_BREADTH_MIN_ARTEFACTS`, never a second copy kept here, so the two
#: checks cannot silently drift apart.
_MERGED_FLOORS: dict[str, int] = {}

#: Workers that went down without a usable report -- a crash, or a
#: `pytest_testnodedown(error=...)` -- surfaced in the failure message
#: (only once the check actually fires; see `_enforce_merged_breadth_floor`).
_WORKER_ERRORS: list[str] = []

_BREADTH_TEST_NODEID_SUFFIX = (
    "::test_the_breadth_layer_still_covers_every_board")

_WORKEROUTPUT_KEY = "tan_cli_1256_breadth_floor"

_TARGET_MODULE_FILE = (
    Path(__file__).parent / "test_planner_emit_parity.py").resolve()


def _emit_parity_module_from_items(session: pytest.Session):
    """The `test_planner_emit_parity` module object exactly as PYTEST
    ITSELF collected and ran it in this process -- read off any item this
    worker actually holds in `session.items` whose `__file__` matches, never
    a `sys.modules` scan.

    `sys.modules` is not safe here: `test_planner_axis_build_plan_parity.py`
    (alphabetically before `test_planner_emit_parity.py`, so collected
    FIRST when the whole `tests/parity` directory is collected) imports
    `tests.parity.test_planner_emit_parity` at its own module level -- since
    `tests/__init__.py` does not exist, Python's namespace-package
    machinery satisfies that import by creating a SECOND, never-mutated
    module object for the identical physical file, and inserts it into
    `sys.modules` before pytest's own `parity.test_planner_emit_parity`
    import does. A `sys.modules` lookup by file identity alone can return
    that empty duplicate (measured: "only 0 boards were measured" on a
    session where every board test genuinely ran). `item.module` has no such
    ambiguity -- it is the literal object pytest instantiated the test
    function from, so there is nothing else it could be.

    `None` if this worker collected no item from that file at all.
    """
    for item in session.items:
        module = getattr(item, "module", None)
        module_file = getattr(module, "__file__", None) if module else None
        if not module_file:
            continue
        try:
            resolved = Path(module_file).resolve()
        except (OSError, ValueError):
            continue  # an exotic __file__ (frozen/zip-imported) -- not a match
        if resolved == _TARGET_MODULE_FILE:
            return module
    return None


def _ran_breadth_test(session: pytest.Session) -> bool:
    return any(
        item.nodeid.endswith(_BREADTH_TEST_NODEID_SUFFIX)
        for item in session.items
    )


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:  # noqa: ANN001 -- xdist's own type
    """CONTROLLER-side only (see the module docstring's handshake section).
    `optionalhook=True` for the same reason as `pytest_testnodedown` below:
    this hookIMPL only matters when the xdist plugin actually registered
    the matching hookSPEC, and must not error when it did not.

    Runs once per worker, before that worker's first message is sent --
    stamps `HANDSHAKE_KEY` into `node.workerinput`, the dict xdist is about
    to hand that worker, so it can tell "the merge check is loaded" apart
    from "it silently is not" the moment it starts.
    """
    node.workerinput[HANDSHAKE_KEY] = True


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Runs in EVERY process -- each worker AND the controller (or the one
    lone process of a serial run) -- because that is how `pytest_sessionfinish`
    works; the two roles are told apart by `config.workerinput`, the same
    guard `tests/conftest.py::pytest_configure` already uses for the same
    distinction (that file reads `workerinput` only, never `workeroutput`).

    WORKER branch: publish this worker's own `_ARTEFACTS_COMPARED` share,
    its copy of the floor constants, and whether this worker's own
    `session.items` (post `-k`/`-m` deselection) included the breadth-floor
    test, into `config.workeroutput` -- read back by the controller's
    `pytest_testnodedown` below. Writing here, not in a hookwrapper, is
    fine: xdist's OWN `pytest_sessionfinish` hookwrapper (`WorkerInteractor`,
    `xdist/remote.py`) starts before and resumes after every plain
    `pytest_sessionfinish` hookimpl including this one, so `workeroutput` is
    still mutable when this runs and is not sent (`workerfinished` event)
    until after every hookimpl, this one included, has returned.

    CONTROLLER (or serial) branch: skipped entirely on an interrupted,
    internal-error, or usage-error session (MINOR 2 -- this file's own
    verdict has no business overriding a session that never really ran).
    If no worker ever reported collecting the breadth test, there is
    nothing to enforce -- return quietly. Otherwise check the exact same
    floors the in-test assertion checks, against the MERGED totals, and
    fail the whole session if either is short.
    """
    config = session.config
    workeroutput = getattr(config, "workeroutput", None)
    if workeroutput is not None:
        module = _emit_parity_module_from_items(session)
        artefacts = dict(getattr(module, "_ARTEFACTS_COMPARED", {})) if module else {}
        floors = None
        if module is not None:
            boards = getattr(module, "_BREADTH_MIN_BOARDS", None)
            min_artefacts = getattr(module, "_BREADTH_MIN_ARTEFACTS", None)
            if isinstance(boards, int) and isinstance(min_artefacts, int):
                floors = {"boards": boards, "artefacts": min_artefacts}
        workeroutput[_WORKEROUTPUT_KEY] = {
            "artefacts": artefacts,
            "ran_breadth_test": _ran_breadth_test(session),
            "floors": floors,
        }
        return

    if exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return  # interrupted / internal error / usage error -- not our verdict to add to
    if not _ANY_WORKER_RAN_THE_BREADTH_TEST:
        # Either this is a serial run (no workers ever existed, so this
        # global can never have been set -- the in-test assertion is the
        # whole story) or xdist ran but no worker collected the breadth
        # test this session. Either way: silence, matching the in-test
        # `pytest.skip`.
        return
    if not _MERGED_ARTEFACTS_COMPARED and not _WORKER_ERRORS:
        # The breadth test itself ran on some worker, but no worker ever
        # populated `_ARTEFACTS_COMPARED` (e.g. a `-k` that keeps the
        # breadth test but drops both accumulating tests), and no worker
        # went down unreported either -- the in-test
        # `if not _ARTEFACTS_COMPARED: pytest.skip(...)` guard covers this
        # exact shape serially; mirror it here rather than reporting "0
        # boards were measured" as though that were a real regression.
        return
    _enforce_merged_breadth_floor(session)


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

    A crashed worker, or one whose `workeroutput` never got this file's own
    key written (this plugin missing on THAT worker somehow, or a crash
    before its `pytest_sessionfinish` ran), is recorded rather than merged
    as zero -- MINOR 4: silently treating "no report" as "measured nothing"
    would hide the exact failure mode this issue is about.
    """
    global _ANY_WORKER_RAN_THE_BREADTH_TEST
    worker_id = None
    workerinput = getattr(node, "workerinput", None)
    if workerinput:
        worker_id = workerinput.get("workerid")
    payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
    if error or not payload:
        detail = f" (error: {error})" if error else ""
        _WORKER_ERRORS.append(
            f"worker {worker_id or '?'} went down without reporting its "
            f"share{detail}")
        return
    for board, count in payload["artefacts"].items():
        _MERGED_ARTEFACTS_COMPARED[board] = (
            _MERGED_ARTEFACTS_COMPARED.get(board, 0) + count)
    if payload["ran_breadth_test"]:
        _ANY_WORKER_RAN_THE_BREADTH_TEST = True
    if payload.get("floors"):
        _MERGED_FLOORS.update(payload["floors"])


def _report(session: pytest.Session, lines: list[str], *, failed: bool) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        for line in lines:
            print(line, file=sys.stderr)
        return
    reporter.ensure_newline()
    reporter.write_sep(
        "=", "tan-cli#1256 -- merged xdist breadth floor",
        red=failed, green=not failed, bold=True)
    for line in lines:
        reporter.write_line(line, red=failed, bold=failed)


def _enforce_merged_breadth_floor(session: pytest.Session) -> None:
    """The exact two assertions
    `test_the_breadth_layer_still_covers_every_board` makes serially,
    against the cross-worker MERGED dict instead of one worker's partial
    share. Mirrors that test's own semantics deliberately, including the
    "thin board" detail, rather than re-deriving a similar-looking check.
    """
    artefacts = _MERGED_ARTEFACTS_COMPARED
    thin = {name: n for name, n in artefacts.items() if n < 2}
    total = sum(artefacts.values())
    problems = list(_WORKER_ERRORS)
    if not {"boards", "artefacts"} <= _MERGED_FLOORS.keys():
        # The breadth test ran, so its module was imported on that worker
        # and the floors were published -- absent means the constants were
        # renamed or removed. Fail loudly rather than enforce nothing.
        problems.append(
            "no worker published the floors (_BREADTH_MIN_BOARDS / "
            "_BREADTH_MIN_ARTEFACTS in test_planner_emit_parity.py)")
    else:
        if len(artefacts) < _MERGED_FLOORS["boards"]:
            problems.append(f"only {len(artefacts)} boards were measured")
        if total < _MERGED_FLOORS["artefacts"]:
            problems.append(
                f"only {total} artefacts were compared byte for byte")
    if thin:
        problems.append(f"boards that compared almost nothing: {thin}")

    if not problems:
        _report(
            session,
            [f"merged breadth floor OK: {len(artefacts)} boards / "
             f"{total} artefacts (tan-cli#1256)"],
            failed=False)
        return

    message = (
        "tan-cli#1256: test_the_breadth_layer_still_covers_every_board's "
        "floor failed on the pytest-xdist MERGED total across every "
        "worker (the in-test assertion itself is skipped under xdist, "
        "since one worker only ever sees its own partial share) -- "
        + "; ".join(problems)
    )
    _report(session, [message], failed=True)
    session.testsfailed += 1
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
