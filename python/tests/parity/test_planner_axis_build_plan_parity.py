# SPDX-License-Identifier: Apache-2.0
"""THE PLANNER AXIS (ADR-0026 §G): `--emit build-plan` alone.

Split out of `test_planner_emit_parity.py` (tan-cli#1215): that file used to
carry `build-plan` alongside its seventeen renderers, under one shared
file/skip-mark/fixture/mode-tuple, which made ADR-0026 §G step 6
("delete the planner axis, keep the render gate") something that needed a
hand-edit of the render file rather than a plain `rm` of this one. `build-plan`
is the one mode ADR-0026's own Decision makes tan's own rather than alp-sdk's
canonical render, so it is the one mode this file -- not the render axis --
owns, and it is the whole reason this file exists at all: once `tan build`
plans with no alp-sdk counterpart left to compare against, deleting this file
is the entirety of that retirement step.

Everything reusable is IMPORTED from `test_planner_emit_parity.py` rather than
redefined -- `SDK`, `HAS_UPSTREAM`, the `planners` fixture, `_boards` and
`_first_diff` -- the same `import a fixture (or a helper) for its side effect`
idiom `tests/planner/_bound_sdk_fixture.py` and
`tests/planner/_baremetal_support.py` already use for the analogous
`_bound_sdk` / `bound_sdk_root`. That import runs in one direction only: THIS
file depends on the render-axis file, never the reverse, so deleting this file
touches zero lines there.

Requires an alp-sdk checkout: set `ALP_SDK_ROOT` (or `ALP_SDK_PARITY_ROOT`).
Skipped, loudly, without one -- via the SAME `HAS_UPSTREAM` boolean the render
axis reads, so both files agree on when there is nothing to compare.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.parity.test_planner_emit_parity import (
    HAS_UPSTREAM,
    SDK,
    _boards,
    _first_diff,
    planners,  # noqa: F401 -- pytest fixture, imported for its side effect
)

PYTHON_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not HAS_UPSTREAM,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout that still ships "
           "scripts/alp_orchestrate/ to run planner byte-parity",
)


def _render_build_plan(pkg, board: Path) -> tuple[str, str]:
    """`(kind, text)` for `--emit build-plan` alone -- the same load/emit split
    `test_planner_emit_parity._render` uses for its six renderers, kept as a
    local one-mode function rather than an import so this file has nothing
    left to lose from that helper when ADR-0026 §G step 6 deletes it."""
    try:
        project = pkg.load_board_yaml(board)
    except Exception as err:  # noqa: BLE001 -- comparing failures on purpose
        return (f"load:{type(err).__name__}", str(err))
    try:
        return ("ok", pkg.emit_build_plan(project, board_yaml=board,
                                          build_root=Path("build")))
    except Exception as err:  # noqa: BLE001
        return (f"emit:{type(err).__name__}", str(err))


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_the_build_plan_mode_is_byte_identical(planners, board):
    """`--emit build-plan`'s own case, split out of what used to be
    `test_every_mode_is_byte_identical`'s first iteration (tan-cli#1215).

    Both directions of failure matter here exactly as they did there: an emit
    that CHANGED, and an emit that stopped happening at all (an exception on
    one side only is a mismatch, not a skip).
    """
    upstream, relocated = planners
    want_kind, want = _render_build_plan(upstream, board)
    got_kind, got = _render_build_plan(relocated, board)
    assert got_kind == want_kind, (
        f"{board} --emit build-plan: sdk {want_kind} ({want}) vs "
        f"tan {got_kind} ({got})")
    if want_kind != "ok":
        return
    assert got == want, (
        f"{board} --emit build-plan differs -- {_first_diff(want, got)}")


def test_the_subprocess_entry_points_agree_too():
    """One end-to-end pair through argv, not just the library surface.

    In-process comparison shares interpreter state; this is the shape a customer
    actually runs. One board is enough -- this checks the entry point, and
    `test_the_build_plan_mode_is_byte_identical` checks the emit.
    """
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    common = ["--input", str(board), "--emit", "build-plan"]

    up = subprocess.run(
        [sys.executable, "-m", "alp_orchestrate", *common],
        capture_output=True, text=True, encoding="utf-8", check=True,
        env={**os.environ, "PYTHONPATH": str(SDK / "scripts")},
    )
    mine = subprocess.run(
        [sys.executable, "-m", "tan.planner_cli", "--sdk-root", str(SDK), *common],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=str(PYTHON_ROOT),
    )
    assert mine.stdout == up.stdout, _first_diff(up.stdout, mine.stdout)


# ==========================================================================
# `tan build`'s own reach -- loads AND spawns
#
# `build` planned in-process already, but kept a `python -m alp_orchestrate`
# fallback that fired on an ImportError and reported NOTHING when it did. That is
# retired, so `build` has no path to alp-sdk's Python at all -- and this is where
# that is measured rather than asserted.
#
# SPAWNS are measured too, with `sys.addaudithook`. `sys.modules` cannot see a
# subprocess, and a subprocess is precisely how the last two edges survived
# every previous "the closure is zero" measurement.
#
# Scope, stated honestly: this covers acquiring, substituting and materialising a
# plan -- everything `tan` itself does. DISPATCH runs the customer's toolchain,
# and `west build` spawns whatever Zephyr's own `EXTRA_KCONFIG_TARGET` hook is
# pointed at -- but that is no longer an alp-sdk file: `--emit kconfig` renders
# its own dumper (`tan.planner.kconfig_symbols._DUMPER_SOURCE`) into the run's
# own scratch tree and points the hook at that instead (see
# tests/core/test_kconfig_symbols.py).
# ==========================================================================

_BUILD_REACH_PROBE = '''
import json, os, sys, tempfile
from pathlib import Path

SDK = Path(os.environ["ALP_SDK_ROOT"]).resolve()
# Forward-slashed + lowercased, and so is every recorded argv: the audit hook
# sees whatever spelling the caller used, and comparing a backslash path against
# a forward-slash one is how a filter silently matches nothing.
NEEDLE = str(SDK / "scripts").replace("\\\\", "/").lower()
BOARD = Path(sys.argv[1])

spawns = []


def _flatten(event_args):
    """One argv as a flat list of strings.

    `subprocess.Popen`'s audit event carries `(executable, args, cwd, env)`, and
    `args` is a LIST on some platforms and a single command STRING on others.
    Iterating a string yields characters, which turns the `<sdk>/scripts` filter
    below into a test that can never fail -- so normalise first.
    """
    exe, argv = event_args[0], event_args[1]
    if isinstance(argv, (list, tuple)):
        parts = [str(a) for a in argv]
    elif argv is None:
        parts = []
    else:
        parts = [str(argv)]
    return [str(exe), *parts]


def _hook(event, args):
    if event == "subprocess.Popen":
        spawns.append(_flatten(args))


sys.addaudithook(_hook)

from tan.commands.build_cmd import _acquire_plan
from tan.commands.build.materialise import materialise_plan
from tan.commands.build.token_substitution import apply_plan_token_substitution

# The board.yaml is COPIED into a scratch project: a tokened plan refuses when
# ${PROJECT_ROOT} (the board.yaml's directory) and the exec base diverge, and
# materialising into the SDK's own examples/ tree would be a side effect on a
# read-only checkout.
project_root = Path(tempfile.mkdtemp()) / "project"
project_root.mkdir()
board = project_root / "board.yaml"
board.write_text(BOARD.read_text(encoding="utf-8"), encoding="utf-8")

# `_acquire_plan` returns `(source text, parsed plan)` -- the text comes back so
# `--plan` can echo it verbatim rather than re-serialising a `BuildPlan`, which
# would silently drop the forward-compat keys a newer SDK adds. This probe wants
# only the parsed plan.
_plan_text, plan = _acquire_plan(None, str(SDK), str(board))
plan, _demotions = apply_plan_token_substitution(
    plan, board_yaml_path=str(board), exec_base=str(project_root),
    sdk_root=str(SDK), python="python3", toolchain_root=None)
materialise_plan(plan, project_root)

loaded = []
for name, mod in list(sys.modules.items()):
    f = getattr(mod, "__file__", None)
    if not f:
        continue
    try:
        Path(f).resolve().relative_to(SDK / "scripts")
    except ValueError:
        continue
    loaded.append(name)


def _touches_sdk_scripts(argv):
    return NEEDLE in " ".join(argv).replace("\\\\", "/").lower()


print(json.dumps({
    "loaded": sorted(loaded),
    "spawns": [s for s in spawns if _touches_sdk_scripts(s)],
    "allSpawns": spawns,
    "slices": len(plan.slices),
    # Proof the filter can match at all. An empty `spawns` means nothing if the
    # predicate is broken -- and it silently was, when a string argv got iterated
    # character by character. The test asserts BOTH of these.
    "filterMatchesAPositive": _touches_sdk_scripts(
        ["python", str(SDK / "scripts" / "alp_project.py"), "--emit", "zephyr-conf"]),
    "filterRejectsANegative": _touches_sdk_scripts(["git", "rev-parse", "HEAD"]),
}))
'''


def test_tan_build_reaches_no_sdk_python(tmp_path):
    """Acquiring, substituting and materialising a plan touches no `<sdk>/scripts`.

    Both halves matter and neither implies the other: nothing under
    `<sdk>/scripts` is IMPORTED (the fallback is gone, so there is no import to
    fail over), and nothing under `<sdk>/scripts` is SPAWNED (which is what the
    retired fallback did, and what `sys.modules` could never have shown).

    `slices` is reported so a plan that resolved to nothing cannot pass this as a
    vacuous zero.
    """
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    probe = tmp_path / "build_reach_probe.py"
    probe.write_text(_BUILD_REACH_PROBE, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(probe), str(board)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "ALP_SDK_ROOT": str(SDK)},
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout.strip())

    assert result["slices"] > 0, "an empty plan proves nothing"
    # The predicate first: an empty `spawns` list is worthless if the filter
    # cannot match, which is exactly what happened when a string argv was
    # iterated character by character.
    assert result["filterMatchesAPositive"] is True, (
        "the <sdk>/scripts spawn filter cannot match even a literal "
        "`python <sdk>/scripts/alp_project.py` -- the measurement below is vacuous")
    assert result["filterRejectsANegative"] is False, (
        "the <sdk>/scripts spawn filter matches everything -- it is not measuring "
        "what it claims")
    assert result["loaded"] == [], (
        "`tan build` still IMPORTS alp-sdk Python to get its plan: "
        f"{result['loaded']}")
    assert result["spawns"] == [], (
        "`tan build` still SPAWNS something under <sdk>/scripts to get its plan: "
        f"{result['spawns']}")
    # Everything it DOES spawn, for the record: `git -C <sdk> rev-parse --short
    # HEAD`, which stamps the plan's `sdkCommit`. `git` is not alp-sdk's Python.
    assert all("git" in argv[0].lower() or "git" in " ".join(argv).lower()
               for argv in result["allSpawns"]), (
        f"an unexpected spawn on the plan path: {result['allSpawns']}")


def test_the_build_plan_fallback_is_gone_not_merely_unused():
    """The retired fallback must not be reachable code sitting one branch away.

    Asserted on the module rather than by behaviour, because a fallback that
    exists and is never taken is exactly what this port has already been burned
    by: it read as absent for as long as nothing happened to take it.
    """
    from tan.commands import build_cmd

    assert not hasattr(build_cmd, "_emit_plan_subprocess"), (
        "`_emit_plan_subprocess` is back -- planning must have exactly one path")
    source = Path(build_cmd.__file__).read_text(encoding="utf-8")
    # `subprocess` is not imported at all any more: `build` spawns slice
    # commands through `tan.commands.build.execute`, never from this module.
    assert "\nimport subprocess" not in source, (
        "build_cmd imports subprocess again -- planning spawns nothing")
    assert "alp_orchestrate\"" not in source and "alp_orchestrate'" not in source, (
        "build_cmd names `alp_orchestrate` in code again")
