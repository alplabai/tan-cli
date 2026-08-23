# SPDX-License-Identifier: Apache-2.0
"""`tan/planner/**` still emits what alp-sdk's planner emitted, byte for byte.

tan-cli#509. This is the layer that OUTLIVES tan-cli#270.

`test_planner_emit_parity.py` beside this file compares two live
implementations, alp-sdk's `scripts/alp_orchestrate/` against tan's relocated
`tan/planner/**`. tan-cli#270 deletes the alp-sdk side, and that module then
does not merely go advisory -- it becomes UNRUNNABLE. Its whole body sits under
`pytestmark = skipif(not HAS_UPSTREAM)`, so all 775 cases skip together;
`test_planner_parity_actually_ran.py` (tan-cli#500) reds the job rather than
letting that read as green, which is correct and also means CI is
red-until-rewritten from the moment #270 lands.

The cost of simply losing it, measured in that module's own docstring:
coverage of `tan/planner/**` (3998 statements) falls from 83% to 27%, with
`zephyr_board.py` (261 statements), `project_emit/dts.py` (219),
`west_libs.py` (95), `native_sim.py` (61) and `hw_info.py` (57) at 0%, and a
two-line mutation halving every emitted partition `base_kib` invisible.

WHAT THIS MODULE COMPARES INSTEAD
----------------------------------

Expected side: `tests/fixtures/planner_oracle/emits/**`, the frozen OUTPUT of
alp-sdk's planner, captured by `scripts/capture_planner_oracle.py`. Checked in,
so it survives alp-sdk deleting the source, rewriting history, or going away.

Input side: an alp-sdk checkout at the SAME ref the fixture was captured from,
bound through `ALP_PLANNER_ORACLE_ROOT`. NOT vendored -- ADR-0017 / I-26 keeps
hardware facts in alp-sdk ("the generators relocated, the facts did not", and
`test_planner_emit_parity.py::test_no_metadata_was_vendored_into_tan` enforces
it). Copying `metadata/**` in here would create a second, staling copy of
exactly the data whose late binding caused tan-cli#485.

So the gate stops answering "have the two implementations diverged" -- after
#270 there is only one -- and starts answering "does tan still emit what it was
relocated from, given the inputs that answer was measured against".

WHY THE INPUTS ARE PINNED TOO, AND NOT JUST TRACKED
----------------------------------------------------

Binding this against the tracked `PINNED_SDK_TAG` instead would look tempting:
a metadata change that moved tan's output would then show up here as a diff,
which is tan-cli#485's late-data mechanism made visible. It was rejected. After
#270 the fixture can only ever be regenerated from a PRE-#270 ref, because no
later alp-sdk ref carries a planner to capture. Tracked inputs against a
freezable-only fixture is a gate whose red has no repair path -- which is the
exact defect that ruled out pinning the alp-sdk SOURCE as the oracle.

Pinning both sides to one ref keeps the repair path: regenerate, and review the
bytes that changed.

WHAT THIS FIXTURE DOES **NOT** COVER, MEASURED
-----------------------------------------------

Stated because a 700-case green is easy to read as 700 cases of substance. It
is not. Bytes per mode across the 100 captured boards:

    build-plan          8,733 B/file    substantive
    system-manifest     1,231 B/file    substantive
    storage-mounts-c      400 B/file    substantive
    dts-reservations      353 B/file    substantive
    ipc-contract-h        367 B/file    stub on 99 of 100
    dts-partitions        310 B/file    stub on ALL 100
    tfm-sysbuild-conf       7 B/file    empty on 98 of 100

`build-plan` and `system-manifest` carry 996 KB of the fixture's 1.14 MB.
`dts-partitions` is a stub everywhere because NO example board declares
`storage:` -- `grep -rl fixed-partitions` over the fixture returns nothing --
so `tan/planner/headers.py:196-202`, the partition `reg = <base size>`
arithmetic, is exercised by no case here. Measured directly: halving
`base_kib` at `tan/planner/partition.py:447` changes 0 of 297 emits.

This is a property of alp-sdk's `examples/`, not of freezing them: the live
`test_planner_emit_parity.py` walks the SAME 100 boards and therefore has the
SAME blind spot, so nothing is lost relative to what this replaces. It is
recorded here so the next person does not mistake a mutation surviving for a
gate working. Closing it needs an example board that declares `storage:`, which
is alp-sdk's to add.

WHY A SUBPROCESS
-----------------

`bind_sdk_root()` is process-global. `test_planner_emit_parity.py` binds
`ALP_SDK_ROOT`; this module needs `ALP_PLANNER_ORACLE_ROOT`. Pre-#270 both
modules are collected into one pytest process, and on a developer host those
two roots are routinely DIFFERENT (a live checkout for one, the frozen ref for
the other). Whichever bound last would win, and the loser would silently
measure the wrong tree -- an import-order-dependent result, which is the worst
shape a parity gate can have.

Rendering in one child process removes the class rather than documenting it. It
costs one interpreter start for all 700 emits, and it has a second merit: it
proves tan can render from a cold process, not only from one another test has
already warmed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_PYTHON = Path(__file__).resolve().parents[2]
FIXTURE = REPO_PYTHON / "tests" / "fixtures" / "planner_oracle"
EMITS = FIXTURE / "emits"

#: Its OWN name, deliberately not `ALP_SDK_ROOT` / `ALP_SDK_PARITY_ROOT`. Those
#: two are consulted by fifteen other test modules whose job is to track live
#: metadata FORWARD; binding either to a frozen ref would silently repoint all
#: of them at a snapshot. Same reasoning, and the same shape, as
#: `test_planner_relocation_freshness.py`'s `ALP_SDK_HAND_PORT_ROOT`.
ORACLE_ROOT_VAR = "ALP_PLANNER_ORACLE_ROOT"

#: Mirrors `capture_planner_oracle.py`'s table. Duplicated on purpose: if the
#: capture tool's mapping changes, this module must fail rather than silently
#: follow it -- a fixture and its reader agreeing by import cannot catch a
#: capture that wrote the wrong thing.
_EXTENSION = {
    "build-plan": ".json",
    "system-manifest": ".yaml",
    "ipc-contract-h": ".h",
    "dts-reservations": ".dtsi",
    "dts-partitions": ".dtsi",
    "storage-mounts-c": ".c",
    "tfm-sysbuild-conf": ".conf",
}

#: The substitution `capture_planner_oracle.py` applied. A contract between the
#: two files; changing it on one side alone reds every case.
SDK_TOKEN = "<SDK>"


def _oracle_root() -> Path | None:
    """The frozen alp-sdk checkout, or `None` when nothing bound one.

    Read at import time, like `tests/conftest.py::sdk_root`, because
    `_scrub_sdk_discovery_env` strips SDK variables before each test body runs.
    """
    raw = os.environ.get(ORACLE_ROOT_VAR, "").strip()
    if not raw:
        return None
    root = Path(raw)
    return root if (root / "metadata").is_dir() else None


ORACLE = _oracle_root()

pytestmark = pytest.mark.skipif(
    ORACLE is None,
    reason=f"set {ORACLE_ROOT_VAR} to the alp-sdk checkout named in "
           f"tests/fixtures/planner_oracle/PROVENANCE.txt",
)


def _goldens() -> list[tuple[str, str, Path]]:
    """Every captured emit as `(board_rel, mode, path)`, sorted.

    Discovered from the FIXTURE, never from the bound checkout: a board the
    checkout has and the fixture does not is an uncaptured board, and it must
    surface as a missing golden rather than as a case nobody ran.
    """
    by_suffix = {}
    for mode, extension in _EXTENSION.items():
        by_suffix.setdefault(extension, []).append(mode)
    found: list[tuple[str, str, Path]] = []
    for path in sorted(EMITS.rglob("*")):
        if not path.is_file():
            continue
        board_rel = path.parent.relative_to(EMITS).as_posix()
        mode = path.name.rsplit(".", 1)[0]
        if mode in _EXTENSION:
            found.append((board_rel, mode, path))
    return found


GOLDENS = _goldens()

#: Rendering script for the child. Kept as source here rather than as a file
#: under `scripts/` because it is a test detail, and because a reader comparing
#: it against `capture_planner_oracle.py::render` should not have to open a
#: third file to do it.
_CHILD = r'''
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
modes = sys.argv[2].split(",")
from tan.planner_root import bind_sdk_root
bind_sdk_root(root)
import tan.planner as pkg

def render(board, mode):
    try:
        project = pkg.load_board_yaml(board)
    except Exception as err:
        return (f"load:{type(err).__name__}", str(err))
    try:
        if mode == "build-plan":
            return ("ok", pkg.emit_build_plan(project, board_yaml=board,
                                              build_root=Path("build")))
        emitter = {
            "system-manifest": pkg.emit_system_manifest,
            "ipc-contract-h": pkg.emit_ipc_contract_h,
            "dts-reservations": pkg.emit_dts_reservations,
            "dts-partitions": pkg.emit_dts_partitions,
            "storage-mounts-c": pkg.emit_storage_mounts_c,
            "tfm-sysbuild-conf": pkg.emit_tfm_sysbuild_conf,
        }[mode]
        return ("ok", emitter(project))
    except Exception as err:
        return (f"emit:{type(err).__name__}", str(err))

out = {}
for board in sorted((root / "examples").rglob("board.yaml")):
    rel = board.parent.relative_to(root).as_posix()
    for mode in modes:
        kind, text = render(board, mode)
        for spelling in (root.as_posix(), str(root)):
            text = text.replace(spelling, "<SDK>")
        out[f"{rel}::{mode}"] = [kind, text]
sys.stdout.write(json.dumps(out))
'''


@pytest.fixture(scope="module")
def rendered() -> dict[str, list[str]]:
    """Every `tan` emit over the frozen checkout, from one cold child process."""
    assert ORACLE is not None
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(ORACLE), ",".join(_EXTENSION)],
        capture_output=True, text=True, cwd=str(REPO_PYTHON),
        env={**os.environ, "PYTHONPATH": str(REPO_PYTHON)},
    )
    assert result.returncode == 0, (
        "the render child failed, so nothing was compared:\n"
        f"{result.stderr[-4000:]}"
    )
    return json.loads(result.stdout)


def test_the_fixture_is_present_and_was_captured_from_a_named_ref():
    # Non-vacuity, first: every assertion below is parametrised off GOLDENS, so
    # an empty or half-written fixture would collect zero cases and report a
    # green run that compared nothing -- the failure tan-cli#500 exists for.
    provenance = FIXTURE / "PROVENANCE.txt"
    assert provenance.is_file(), (
        f"{provenance} is missing -- a fixture with no recorded alp-sdk ref "
        "cannot be attributed to an implementation, and nothing downstream "
        "could tell what it froze."
    )
    assert "alp-sdk ref" in provenance.read_text(encoding="utf-8")
    assert len(GOLDENS) >= 700, (
        f"only {len(GOLDENS)} goldens under {EMITS} -- 700 were captured at "
        "94378a05 (100 boards x 7 modes). A shrunken fixture silently shrinks "
        "this gate; regenerate with scripts/capture_planner_oracle.py."
    )


def test_every_captured_board_still_exists_in_the_bound_checkout():
    assert ORACLE is not None
    captured = {board for board, _, _ in GOLDENS}
    live = {p.parent.relative_to(ORACLE).as_posix()
            for p in (ORACLE / "examples").rglob("board.yaml")}
    missing = sorted(captured - live)
    uncaptured = sorted(live - captured)
    assert not missing, (
        f"{ORACLE_ROOT_VAR} is bound to a checkout that no longer has "
        f"{missing} -- it is not the ref this fixture was captured from. "
        "Check tests/fixtures/planner_oracle/PROVENANCE.txt."
    )
    assert not uncaptured, (
        f"{uncaptured} exist in the bound checkout but have no golden, so "
        "they are covered by nothing. Regenerate the fixture."
    )


@pytest.mark.parametrize(
    "board_rel,mode,golden",
    GOLDENS,
    ids=[f"{b.split('/')[-1]}-{m}" for b, m, _ in GOLDENS],
)
def test_the_emit_matches_the_frozen_oracle(rendered, board_rel, mode, golden):
    key = f"{board_rel}::{mode}"
    assert key in rendered, (
        f"{key} has a golden but tan rendered nothing for it -- the child "
        "walked a different board set than the fixture holds."
    )
    got_kind, got = rendered[key]

    if golden.suffix == ".error":
        want_kind, _, want = golden.read_text(encoding="utf-8").partition("\n")
        assert got_kind == want_kind, (
            f"{board_rel} --emit {mode}: alp-sdk raised {want_kind}, tan "
            f"{got_kind!r}. Failing IDENTICALLY is part of parity -- a mode "
            "that quietly stopped refusing is not a pass."
        )
        return

    assert got_kind == "ok", (
        f"{board_rel} --emit {mode}: alp-sdk emitted this mode, tan raised "
        f"{got_kind} ({got})."
    )
    want = golden.read_text(encoding="utf-8")
    assert got == want, (
        f"{board_rel} --emit {mode} no longer matches the frozen oracle.\n"
        f"{_first_diff(want, got)}\n"
        "If this change is CORRECT, regenerate the fixture "
        "(scripts/capture_planner_oracle.py) and review the resulting diff -- "
        "that diff is the point of freezing output rather than a source ref, "
        "and it is the only place a reviewer sees which emitted bytes moved."
    )


def _first_diff(want: str, got: str) -> str:
    want_lines, got_lines = want.splitlines(), got.splitlines()
    for index, (a, b) in enumerate(zip(want_lines, got_lines), start=1):
        if a != b:
            return f"line {index}:\n  oracle: {a!r}\n  tan:    {b!r}"
    return f"line count: oracle={len(want_lines)} tan={len(got_lines)}"
