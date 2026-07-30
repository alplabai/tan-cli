# SPDX-License-Identifier: Apache-2.0
"""Byte-parity: the relocated planner vs alp-sdk's `alp_orchestrate`.

The relocation of `scripts/alp_orchestrate/` into `tan/planner/` is a MOVE, not
a rewrite -- so the only acceptable result is byte-identical emit. This module
proves it the only way that means anything: it imports BOTH planners into one
process (`alp_orchestrate` off `<sdk>/scripts`, `tan.planner` bound to the same
checkout), renders every mode for every `board.yaml` in the SDK's `examples/`,
and compares the strings. Same metadata, same modes, same bytes -- or a failure
naming the file, the mode and the first differing line.

Both directions of failure matter and both are covered: an emit that CHANGED,
and an emit that stopped happening at all (an exception on one side only is a
mismatch, not a skip).

`--emit kconfig` is deliberately absent from the mode list. It is the one
non-hermetic emit -- it shells `west build` inside a bootstrapped Zephyr
workspace (I-34) -- so it cannot run here. The relocation touched its dumper
path (`kconfig_symbols._DUMPER`, now anchored on the bound SDK root), which
`test_the_kconfig_dumper_resolves_into_the_sdk` checks directly instead.

Requires an alp-sdk checkout: set `ALP_SDK_ROOT` (or `ALP_SDK_PARITY_ROOT`).
Skipped, loudly, without one -- a green run that compared nothing would be worse
than a red one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The project-scoped emit modes `tan.planner` owns. `kconfig` is excluded (see
# the module docstring); the other seven need nothing but `metadata/**`.
MODES = (
    "build-plan",
    "system-manifest",
    "ipc-contract-h",
    "dts-reservations",
    "dts-partitions",
    "storage-mounts-c",
    "tfm-sysbuild-conf",
)


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()
HAS_UPSTREAM = SDK is not None and (
    SDK / "scripts" / "alp_orchestrate" / "__init__.py"
).is_file()

pytestmark = pytest.mark.skipif(
    not HAS_UPSTREAM,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout that still ships "
           "scripts/alp_orchestrate/ to run planner byte-parity",
)


@pytest.fixture(scope="module")
def planners():
    """Both planners, in one process, bound to the same SDK checkout."""
    assert SDK is not None
    scripts = str(SDK / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import alp_orchestrate  # noqa: F401  -- upstream, off <sdk>/scripts

    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    import tan.planner  # noqa: F401

    from alp_orchestrate.cli import main as _upstream_main  # noqa: F401
    return alp_orchestrate, tan.planner


def _render(pkg, board: Path, mode: str) -> tuple[str, str]:
    """`(kind, text)` -- `kind` is 'ok' or the exception class name.

    An exception is a RESULT, not a skip: the two planners must fail
    identically too, or a relocation that quietly stopped emitting a mode would
    read as parity.
    """
    try:
        project = pkg.load_board_yaml(board)
    except Exception as err:  # noqa: BLE001 -- comparing failures on purpose
        return (f"load:{type(err).__name__}", str(err))
    try:
        if mode == "system-manifest":
            return ("ok", pkg.emit_system_manifest(project))
        if mode == "ipc-contract-h":
            return ("ok", pkg.emit_ipc_contract_h(project))
        if mode == "dts-reservations":
            return ("ok", pkg.emit_dts_reservations(project))
        if mode == "dts-partitions":
            return ("ok", pkg.emit_dts_partitions(project))
        if mode == "storage-mounts-c":
            return ("ok", pkg.emit_storage_mounts_c(project))
        if mode == "tfm-sysbuild-conf":
            return ("ok", pkg.emit_tfm_sysbuild_conf(project))
        if mode == "build-plan":
            return ("ok", pkg.emit_build_plan(project, board_yaml=board,
                                              build_root=Path("build")))
    except Exception as err:  # noqa: BLE001
        return (f"emit:{type(err).__name__}", str(err))
    raise AssertionError(f"unhandled mode {mode!r}")


def _boards() -> list[Path]:
    if SDK is None:
        return []
    return sorted((SDK / "examples").rglob("board.yaml"))


def _first_diff(a: str, b: str) -> str:
    al, bl = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(al, bl), start=1):
        if x != y:
            return f"line {i}:\n  sdk: {x!r}\n  tan: {y!r}"
    return f"line count: sdk={len(al)} tan={len(bl)}"


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_every_mode_is_byte_identical(planners, board):
    upstream, relocated = planners
    for mode in MODES:
        want_kind, want = _render(upstream, board, mode)
        got_kind, got = _render(relocated, board, mode)
        assert got_kind == want_kind, (
            f"{board} --emit {mode}: sdk {want_kind} ({want}) vs "
            f"tan {got_kind} ({got})")
        if want_kind != "ok":
            # Same failure class; the message is allowed to carry the SDK path
            # either planner resolved it from, so compare only the class here.
            continue
        assert got == want, (
            f"{board} --emit {mode} differs -- {_first_diff(want, got)}")


#: Renderers that MOVED but whose `--emit` front door stayed in alp-sdk
#: (`alp_project.py` owns 15 of the 20 registry modes). The emit-snapshot
#: goldens reach them as `proj-*.zephyr-conf` / `proj-*.os-topology` over three
#: boards; comparing the functions directly covers all 99.
_SLICE_RENDERERS = ("_slice_alp_conf", "_slice_local_conf", "_slice_cmake_args")


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_the_relocated_renderers_behind_alp_project_agree(planners, board):
    upstream, relocated = planners
    try:
        want_project = upstream.load_board_yaml(board)
    except Exception:  # noqa: BLE001 -- covered by the emit-parity test above
        pytest.skip("board does not load; parity of the failure is asserted elsewhere")
    got_project = relocated.load_board_yaml(board)

    assert (relocated.emit_os_topology(got_project)
            == upstream.emit_os_topology(want_project))
    assert (relocated.core_os_topology(got_project)
            == upstream.core_os_topology(want_project))
    assert (relocated.emit_sysbuild_conf(got_project)
            == upstream.emit_sysbuild_conf(want_project))

    want_slices = {s.core_id: s for s in want_project.cores.values()}
    for core_id, got_slice in got_project.cores.items():
        want_slice = want_slices[core_id]
        for name in _SLICE_RENDERERS:
            assert (getattr(relocated, name)(got_project, got_slice)
                    == getattr(upstream, name)(want_project, want_slice)), (
                f"{board} {core_id} {name} differs")


def test_the_subprocess_entry_points_agree_too():
    """One end-to-end pair through argv, not just the library surface.

    In-process comparison shares interpreter state; this is the shape a customer
    actually runs. One board is enough -- this checks the entry point, and
    `test_every_mode_is_byte_identical` checks the emit.
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
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert mine.stdout == up.stdout, _first_diff(up.stdout, mine.stdout)


def test_the_kconfig_dumper_resolves_into_the_sdk(planners):
    """`--emit kconfig`'s dumper stayed in alp-sdk; the path must follow it.

    It used to be a `__file__`-relative sibling walk (`../kconfig/`), which
    inside `tan` would resolve to a `tan/kconfig/` that does not exist -- and
    only the one merge-BLOCKING gate in alp-sdk would have caught it.
    """
    _, relocated = planners
    from tan.planner import kconfig_symbols
    assert SDK is not None
    assert kconfig_symbols._DUMPER == (
        SDK / "scripts" / "kconfig" / "alp_kconfig_dump.py")
    assert kconfig_symbols._DUMPER.is_file()


def test_no_metadata_was_vendored_into_tan():
    """ADR-0017 / I-26: the generators relocated, the facts did not."""
    tan_pkg = Path(__file__).resolve().parents[2] / "tan"
    strays = [p for p in tan_pkg.rglob("*")
              if p.is_dir() and p.name == "metadata"]
    assert not strays, f"metadata/ must stay in alp-sdk; found {strays}"
