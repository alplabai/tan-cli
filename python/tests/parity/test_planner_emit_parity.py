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
workspace (I-34) -- so it cannot run here. Its dumper is rendered onto disk
fresh per run rather than read off a fixed path (`kconfig_symbols
._DUMPER_SOURCE`, replacing the earlier `_DUMPER` path anchored on the bound
SDK root); `test_the_kconfig_dumper_no_longer_names_a_path_in_the_sdk`
checks that directly, and `tests/core/test_kconfig_symbols.py` covers the
render's own invariants hermetically.

Further layers sit below that library comparison, because `tan generate` renders
ALL ELEVEN of its targets IN-PROCESS instead of spawning
`scripts/alp_project.py`, and a library-level match does not prove the command
produces the same file:

* `tan generate --output <path>` spawned for real, once per `board.yaml`, with
  the FILE it writes compared byte for byte against the SDK's own front door.
  That is the shape `cmake/alp.cmake` drives on every Zephyr configure, and it
  covers what a library comparison cannot -- newline translation, a wrong
  destination, an artefact leaking onto stdout beside the envelope. The engine
  is PINNED via `TAN_GENERATE_EXECUTOR` and then asserted from `data.engine`,
  so a fallback cannot let this layer measure the subprocess path and report it
  as proof of the in-process one. `zephyr-board` joins that layer as the one
  target writing a DIRECTORY -- plumbing (`write_tree`, and an `--output` that
  IS the directory) nothing else exercises.
* the breadth layer, driving both sides in-process so all 99 boards x every
  relocated mode x every `--core` form is affordable. Its oracle is
  `alp_project.py`'s OWN dispatch functions -- `--emit zephyr-conf` is not
  `_slice_alp_conf`, it is that slice inside a per-core wrapper, and `--emit
  carrier-netlist` is not `_emit_carrier_netlist` either: it is that emitter
  behind `main()`'s own SoM-preset + inline-or-preset-board resolution.
* the error contract, because in-process execution widens it: every planner
  exception that used to die inside a subprocess and arrive as an exit code now
  propagates into the CLI, where an escaped traceback renders as nothing at all.
  Nine Criticals in this port were exactly that, so each newly relocated target
  gets its own probe.
* the IMPORT CLOSURE, the one layer none of the others can see. Byte-identical
  emit proves the planner renders from `tan`; it says nothing about what `tan`
  LOADED to do it. Before the fact-readers were relocated, an in-process
  `tan generate` still pulled 32 modules off `<sdk>/scripts` -- all 19 of
  `alp_orchestrate` among them -- because six `tan/planner/*` modules imported
  `alp_project` / `alp_registries` / `alp_cli.validator` at module scope. Every
  emit passed while the dependency the port exists to remove was fully intact.
  `test_the_in_process_path_loads_none_of_the_sdks_python` measures it PER MODE
  across all eleven, so a single target regressing names itself.
* and `tan build`'s own reach, measured the same way.  `build` acquired its plan
  in-process but kept a SILENT `python -m alp_orchestrate` fallback; that is
  retired, and `test_tan_build_reaches_no_sdk_python` proves the plan-acquire
  path neither imports NOR SPAWNS anything under `<sdk>/scripts`. Spawns are
  measured with an audit hook, because a subprocess is invisible to
  `sys.modules` -- which is how the last edge stayed hidden.

Requires an alp-sdk checkout: set `ALP_SDK_ROOT` (or `ALP_SDK_PARITY_ROOT`).
Skipped, loudly, without one -- a green run that compared nothing would be worse
than a red one.
"""

from __future__ import annotations

import json
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


def _single_core_board() -> Path | None:
    """Any example `board.yaml` with exactly one non-`off` core, found by SEARCH.

    The error-contract probes below used to name `examples/blinky/board.yaml`
    directly. That example no longer exists, so nine of them were SKIPPING --
    a green run measuring nothing, which this module's own docstring calls worse
    than a red one. Resolving by search means an example being renamed costs the
    suite nothing, and a checkout with no examples at all still skips honestly.

    Parsed with plain YAML rather than the loader: this runs at collection time,
    before any planner is bound.
    """
    import yaml

    for board in _boards():
        try:
            doc = yaml.safe_load(board.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- an unparseable example is not our probe
            continue
        if not isinstance(doc, dict):
            continue
        cores = doc.get("cores")
        if not isinstance(cores, dict):
            continue
        live = [c for c, s in cores.items()
                if isinstance(s, dict) and s.get("os") != "off"]
        if len(live) == 1:
            return board
    return None


#: One simple project, resolved once. `None` only when the checkout ships no
#: examples -- and then every probe using it skips WITH that reason.
SIMPLE_BOARD = _single_core_board()


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


def test_the_kconfig_dumper_no_longer_names_a_path_in_the_sdk(planners):
    """`--emit kconfig`'s dumper used to be a path into the bound alp-sdk
    checkout -- first a `__file__`-relative sibling walk (`../kconfig/`,
    which inside `tan` resolved to a `tan/kconfig/` that does not exist),
    then `_DUMPER = REPO / "scripts" / "kconfig" / "alp_kconfig_dump.py"`.
    Both coupled `--emit kconfig` to a file living outside `tan`; it is now
    `_DUMPER_SOURCE`, rendered fresh onto disk by every
    `_load_board_symbols` call (see `tests/core/test_kconfig_symbols.py`
    for the render's own invariants).
    """
    _, relocated = planners
    from tan.planner import kconfig_symbols
    assert SDK is not None
    assert not hasattr(kconfig_symbols, "_DUMPER")
    assert isinstance(kconfig_symbols._DUMPER_SOURCE, str)
    assert kconfig_symbols._DUMPER_SOURCE.strip()
    assert str(SDK) not in kconfig_symbols._DUMPER_SOURCE


def test_no_metadata_was_vendored_into_tan():
    """ADR-0017 / I-26: the generators relocated, the facts did not."""
    tan_pkg = Path(__file__).resolve().parents[2] / "tan"
    strays = [p for p in tan_pkg.rglob("*")
              if p.is_dir() and p.name == "metadata"]
    assert not strays, f"metadata/ must stay in alp-sdk; found {strays}"


# ==========================================================================
# `tan generate --output <path>`: the FILE it writes, byte for byte
#
# The tests above compare the two planners as libraries. These compare the
# artefact a customer actually ends up with -- the bytes on disk after
# `cmake/alp.cmake` has driven `tan generate --output`, against the same
# artefact from the SDK's own front door.
#
# This is a distinct failure surface, not a duplicate: everything between the
# renderer and the file can corrupt the result. The one that bites hardest is
# newline translation -- the SDK writes `newline=""` so an emit stays LF on
# every host, and `python -m alp_orchestrate > file` on Windows already produces
# CRLF for exactly that reason (its stdout is a text stream). A `--output` that
# reached the disk through any text-mode write would silently CRLF the whole
# tree, and every emit-snapshot golden in alp-sdk would flip.
# ==========================================================================

PYTHON_ROOT = Path(__file__).resolve().parents[2]


def _tan_generate(args: list[str], destination: Path,
                  engine: str = "in-process") -> bytes:
    """Run the real `tan generate` and return the bytes it wrote.

    The CLI is spawned, not called: `--output` is a promise about a FILE and
    about stdout carrying nothing but the envelope, and only a real process can
    show both. Any non-zero exit fails here with the envelope attached.

    `engine` is PINNED through `TAN_GENERATE_EXECUTOR` rather than left to
    `auto`, and `data.engine` is then asserted to agree. Without that this whole
    layer could silently measure the subprocess path and report it as proof the
    in-process one works -- which is the exact belief the port has to disprove.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate",
         "--sdk-root", str(SDK), "--output", str(destination),
         "--format", "json", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": engine},
        check=False,
    )
    assert proc.returncode == 0, f"tan generate {args} -> {proc.returncode}: {proc.stdout}{proc.stderr}"
    assert proc.stderr.strip() == "", f"stderr must stay empty under --format json: {proc.stderr}"
    envelope = json.loads(proc.stdout.strip())  # exactly one document
    assert envelope["issues"] == [], envelope["issues"]
    # `--output` writes a FILE; the artefact must never also be on stdout.
    assert envelope["data"]["written"] == [str(destination)]
    assert set(envelope["data"]["engine"].values()) == {engine}, (
        f"asked for {engine}, ran {envelope['data']['engine']}")
    return destination.read_bytes()


def _alp_project(board: Path, mode: str, core: str | None, destination: Path) -> bytes:
    """The SDK's own front door, writing a file. The oracle for the modes
    `scripts/alp_project.py` owns (`zephyr-conf` among them) -- comparing against
    its stdout instead would compare against a CRLF-translated stream."""
    assert SDK is not None
    argv = [sys.executable, str(SDK / "scripts" / "alp_project.py"),
            "--input", str(board), "--emit", mode, "--output", str(destination)]
    if core is not None:
        argv += ["--core", core]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, f"oracle {mode} -> {proc.returncode}: {proc.stderr}"
    return destination.read_bytes()


def _first_core_with_os(project, *allowed: str) -> str | None:
    """The first core this emit mode will accept, or `None`.

    `--emit zephyr-conf --core <id>` is a hard error for a core whose `os` is not
    `zephyr` (and `cmake-args` for one that is neither `zephyr` nor
    `baremetal`), so the core has to be picked, not guessed. The accepted set
    per mode is `compatible.os` in `metadata/emit-registry-v1.json`.
    """
    for core_id in sorted(project.cores):
        if project.cores[core_id].os in allowed:
            return core_id
    return None


def _first_zephyr_core(project) -> str | None:
    return _first_core_with_os(project, "zephyr")


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_tan_generate_writes_the_ipc_contract_header_byte_for_byte(
    planners, board, tmp_path
):
    """`alp_sdk_ipc_contract_header()`'s artefact, end to end.

    The oracle is the upstream planner's own return value, in-process: it is the
    string every other consumer of this mode gets, so any difference on disk is
    tan's plumbing and nothing else.
    """
    upstream, _ = planners
    try:
        want = upstream.emit_ipc_contract_h(
            upstream.load_board_yaml(board)).encode("utf-8")
    except Exception:  # noqa: BLE001 -- parity of the failure is asserted above
        pytest.skip("board does not render this mode; covered by the emit-parity test")

    got = _tan_generate(
        ["--target", "ipc-contract-h", "--board-yaml", str(board)],
        tmp_path / "generated" / "alp" / "system_ipc.h",
    )
    assert got == want, _first_diff(
        want.decode("utf-8"), got.decode("utf-8", "replace"))


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_tan_generate_writes_the_zephyr_conf_fragment_byte_for_byte(
    planners, board, tmp_path
):
    """`alp_sdk_zephyr_conf()`'s artefact, end to end -- the one the other 96
    examples depend on, and the one `scripts/check_zephyr_conf_parity.py` pins
    against the build plan's `configArtefacts`."""
    upstream, _ = planners
    try:
        core = _first_zephyr_core(upstream.load_board_yaml(board))
    except Exception:  # noqa: BLE001
        pytest.skip("board does not load; parity of the failure is asserted above")
    if core is None:
        pytest.skip("no zephyr core -- `--emit zephyr-conf --core` refuses one")

    want = _alp_project(board, "zephyr-conf", core, tmp_path / "oracle.conf")
    got = _tan_generate(
        ["--target", "zephyr-conf", "--core", core, "--board-yaml", str(board)],
        tmp_path / "generated" / "alp.conf",
    )
    assert got == want, _first_diff(
        want.decode("utf-8"), got.decode("utf-8", "replace"))


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_tan_generate_writes_the_cmake_args_fragment_byte_for_byte(
    planners, board, tmp_path
):
    """`alp_sdk_cmake_args()`'s artefact. Distinct from `zephyr-conf` in a way
    that has bitten: `cmake-args --core <id>` keeps the `# --- core: ... ---`
    marker that `zephyr-conf --core <id>` drops."""
    upstream, _ = planners
    try:
        core = _first_core_with_os(upstream.load_board_yaml(board),
                                   "zephyr", "baremetal")
    except Exception:  # noqa: BLE001
        pytest.skip("board does not load; parity of the failure is asserted above")
    if core is None:
        pytest.skip("no zephyr/baremetal core -- `--emit cmake-args --core` refuses one")

    want = _alp_project(board, "cmake-args", core, tmp_path / "oracle.txt")
    got = _tan_generate(
        ["--target", "cmake-args", "--core", core, "--board-yaml", str(board)],
        tmp_path / "generated" / "alp-cmake-args.txt",
    )
    assert got == want, _first_diff(
        want.decode("utf-8"), got.decode("utf-8", "replace"))


def test_the_two_engines_produce_the_same_bytes(planners, tmp_path):
    """The port's actual claim, pinned end to end: routing an emit through
    `tan.planner` in-process instead of spawning `alp_project.py` changes NO
    bytes. Run twice through the real CLI, once per engine, and diffed.

    Every STREAM target, not a sample. This is the only layer where both sides
    go through the real `tan generate` process, so it is the only one that can
    catch a difference introduced between the renderer and the file --
    `TAN_GENERATE_EXECUTOR=subprocess` still shells `alp_project.py`, which is
    exactly what makes the comparison meaningful rather than circular.
    `zephyr-board` has its own test below (it writes a directory).
    """
    upstream, _ = planners
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    core = _first_core_with_os(upstream.load_board_yaml(board), "zephyr")
    assert core is not None

    for mode, args in (
        ("zephyr-conf", ["--core", core]),
        ("cmake-args", ["--core", core]),
        ("os-topology", []),
        ("ipc-contract-h", []),
        # The five stream targets that relocated last.
        ("dts-overlay", ["--core", core]),
        ("dts-overlay", []),
        ("hw-info-h", ["--core", core]),
        ("west-libraries", ["--core", core]),
        ("native-sim-overlay", []),
        ("carrier-netlist", []),
    ):
        tag = f"{mode}-{'scoped' if args else 'unscoped'}"
        common = ["--target", mode, "--board-yaml", str(board), *args]
        spawned = _tan_generate(common, tmp_path / f"{tag}.sub", "subprocess")
        native = _tan_generate(common, tmp_path / f"{tag}.native", "in-process")
        assert native == spawned, f"{tag}: " + _first_diff(
            spawned.decode("utf-8", "replace"), native.decode("utf-8", "replace"))


def _tan_generate_tree(args: list[str], destination: Path,
                       engine: str = "in-process") -> dict[str, bytes]:
    """`tan generate --target zephyr-board --output <dir>`, spawned for real.

    Returns `{filename: bytes}` read back off disk. The directory case needs its
    own helper because `--output` here IS the board directory, and what the
    envelope reports in `data.written` is that directory rather than a file.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate",
         "--sdk-root", str(SDK), "--output", str(destination),
         "--format", "json", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": engine},
        check=False,
    )
    assert proc.returncode == 0, (
        f"tan generate {args} -> {proc.returncode}: {proc.stdout}{proc.stderr}")
    assert proc.stderr.strip() == "", f"stderr must stay empty: {proc.stderr}"
    envelope = json.loads(proc.stdout.strip())
    assert envelope["issues"] == [], envelope["issues"]
    assert envelope["data"]["written"] == [str(destination)]
    assert set(envelope["data"]["engine"].values()) == {engine}, (
        f"asked for {engine}, ran {envelope['data']['engine']}")
    return {p.name: p.read_bytes() for p in destination.iterdir()}


def test_tan_generate_writes_a_zephyr_board_tree_byte_for_byte(planners, tmp_path):
    """`--emit zephyr-board` end to end, both engines, through the real CLI.

    The one target whose `--output` is a DIRECTORY, so it is the only one
    exercising `planner_emit.write_tree` plus `_ensure_writable`'s `mkdir` branch
    -- and the only one where a wrong destination shows up as files landing
    beside the board directory instead of inside it. `west build --board-root
    build/boards` is what consumes the result, so a stray level of nesting makes
    the board undiscoverable rather than wrong.
    """
    upstream, _ = planners
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    core = _first_core_with_os(upstream.load_board_yaml(board), "zephyr")
    assert core is not None

    args = ["--target", "zephyr-board", "--core", core, "--board-yaml", str(board)]
    spawned = _tan_generate_tree(args, tmp_path / "sub", "subprocess")
    native = _tan_generate_tree(args, tmp_path / "native", "in-process")
    assert sorted(native) == sorted(spawned), (
        f"board-tree FILE SET differs -- alp_project {sorted(spawned)} vs "
        f"tan {sorted(native)}")
    assert native, "a board tree with no files is not a pass"
    for name in sorted(spawned):
        assert native[name] == spawned[name], f"{name}: " + _first_diff(
            spawned[name].decode("utf-8", "replace"),
            native[name].decode("utf-8", "replace"))
        assert b"\r\n" not in native[name], f"{name}: CRLF reached disk"


def test_an_emit_reaching_disk_through_tan_stays_lf(planners, tmp_path):
    """The newline trap, pinned on its own so a CRLF regression names itself
    rather than showing up as 99 unrelated diffs.

    `python -m alp_orchestrate --emit ipc-contract-h > file` on Windows produces
    CRLF -- that is the shape this must NOT have.
    """
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    got = _tan_generate(
        ["--target", "ipc-contract-h", "--board-yaml", str(board)],
        tmp_path / "system_ipc.h",
    )
    assert b"\r\n" not in got


# ==========================================================================
# `tan generate`'s in-process engine, over every board and every form
#
# The `tan generate --output` tests above spawn a real process per case, so they
# can only afford a couple of modes. This block is the breadth layer: it drives
# BOTH sides in-process, so all 99 boards x every relocated mode x both `--core`
# forms costs seconds rather than an hour.
#
# The oracle is `scripts/alp_project.py`'s OWN dispatch functions, called
# directly with the argparse Namespace `main()` would have built. Not a
# reimplementation of them, and not the upstream planner's renderer either --
# `--emit zephyr-conf` is not `_slice_alp_conf`: it is that slice wrapped in a
# per-core loop with an OS-class gate, an `os: off` skip, a section marker whose
# presence depends on `--core`, and a conditional trailing newline. Comparing
# against the slice alone would pass while the wrapper diverged.
#
# Both directions of failure are covered: where the oracle REFUSES (rc != 0),
# the in-process path must refuse too.
# ==========================================================================

#: Exercise EVERY core, not a representative one. For a mode whose registry
#: `compatible.os` is `["any"]` there is no core to reject, so "first accepted +
#: first rejected" would collapse to one form and never reach the branches that
#: differ per core -- `dts-overlay`'s yocto/off stub, `hw-info-h`'s per-core
#: `ALP_HW_BUILD_OS`, `west-libraries`' per-core library set.
ANY_CORE = object()

#: `(mode, core_form)`. `None` means the mode takes no `--core`; a tuple means
#: the scoped form is exercised against the first core whose `os` the mode
#: accepts (per `compatible.os` in the emit registry) plus the first it must
#: refuse; `ANY_CORE` means every core. The unscoped form -- the one a bare
#: `tan generate` uses -- is exercised for all of them.
#:
#: `zephyr-board` is absent deliberately: it writes a DIRECTORY, so its
#: `(rc, bytes)` shape does not fit here and
#: `test_the_in_process_engine_matches_alp_project_for_every_board_tree` covers
#: it instead, feeding the same artefact counter.
GENERATE_MODES = (
    ("zephyr-conf", ("zephyr",)),
    ("cmake-args", ("baremetal", "zephyr")),
    ("yocto-conf", ("yocto",)),
    ("os-topology", None),
    ("ipc-contract-h", None),
    # The five stream modes that relocated last.
    ("dts-overlay", ANY_CORE),
    ("hw-info-h", ANY_CORE),
    ("west-libraries", ANY_CORE),
    ("native-sim-overlay", None),
    ("carrier-netlist", None),
)


#: `{board dir name: artefacts compared byte for byte}`, filled in by the breadth
#: test below. A per-board assertion cannot notice the LAYER shrinking -- drop a
#: mode from `GENERATE_MODES` and every remaining board still passes -- so the
#: total is checked once, at the end, by
#: `test_the_breadth_layer_still_covers_every_board`.
_ARTEFACTS_COMPARED: dict[str, int] = {}


def _first_rejected_core(project, allowed: tuple[str, ...]) -> str | None:
    """The first core an OS-scoped mode must REFUSE, or `None`.

    `os: off` is excluded: both sides SKIP an off core rather than refusing it,
    which is a different (and already covered) branch.
    """
    for core_id in sorted(project.cores):
        os_ = project.cores[core_id].os
        if os_ != "off" and os_ not in allowed:
            return core_id
    return None


def _oracle_emit(board: Path, mode: str, core: str | None,
                 destination: Path) -> tuple[int, bytes]:
    """`alp_project.py`'s own dispatch, in-process. `(rc, bytes_written)`."""
    from types import SimpleNamespace

    import alp_project  # off <sdk>/scripts, put there by the `planners` fixture

    args = SimpleNamespace(input=board, emit=mode, core=core,
                           output=destination,
                           metadata_root=alp_project.METADATA_ROOT)
    project_level = ("os-topology", "ipc-contract-h", "system-manifest",
                     "dts-reservations")
    try:
        if mode == "carrier-netlist":
            # `main()`'s own branch, not `_run_v2_per_core_emit`: this mode is
            # dispatched before the per-core slice machinery is reached, off the
            # raw board.yaml dict plus the SoM preset and the inline-or-preset
            # board. Comparing against `_emit_carrier_netlist` alone would pass
            # while that resolution diverged.
            raw = alp_project._validate_and_load(board, args.metadata_root)
            sku_preset = alp_project._resolve_sku(
                raw["som"]["sku"], args.metadata_root)
            board_preset = alp_project._resolve_inline_or_preset_board(
                raw, args.metadata_root)
            rc = alp_project._write_or_print(
                alp_project._emit_carrier_netlist(
                    raw, sku_preset, board_preset, args.metadata_root),
                destination)
        else:
            runner = (alp_project._run_v2_emit if mode in project_level
                      else alp_project._run_v2_per_core_emit)
            rc = runner(args)
    except SystemExit as err:  # a dependency gate in the SDK's own loader
        return (int(err.code or 1), b"")
    return (rc, destination.read_bytes() if rc == 0 else b"")


def _tan_emit(board: Path, mode: str, core: str | None,
              destination: Path) -> tuple[int, bytes]:
    """`tan.planner_emit`, the in-process engine `tan generate` now uses."""
    from tan import planner_emit

    try:
        text = planner_emit.render(mode, sdk_root=SDK, board_yaml=board,
                                   core=core)
        planner_emit.write(text, destination)
    except SystemExit as err:
        return (int(err.code or 1), b"")
    except Exception:  # noqa: BLE001 -- a refusal is a RESULT here, compared below
        return (1, b"")
    return (0, destination.read_bytes())


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_the_in_process_engine_matches_alp_project_for_every_mode(
    planners, board, tmp_path
):
    upstream, _ = planners
    try:
        project = upstream.load_board_yaml(board)
    except Exception:  # noqa: BLE001 -- covered by test_every_mode_is_byte_identical
        pytest.skip("board does not load; parity of the failure is asserted above")

    compared = 0
    for mode, core_classes in GENERATE_MODES:
        forms: list[str | None] = [None]
        if core_classes is ANY_CORE:
            forms += sorted(project.cores)
        elif core_classes is not None:
            scoped = _first_core_with_os(project, *core_classes)
            if scoped is not None:
                forms.append(scoped)
            # And a core the mode REFUSES (#605 turned warn-and-emit-anyway
            # into a hard error). Without this form the OS-class gate is never
            # reached: every board's first matching core passes it by
            # construction, so the refusal branch would go unmeasured.
            rejected = _first_rejected_core(project, core_classes)
            if rejected is not None:
                forms.append(rejected)
        for core in forms:
            tag = f"{mode}-{core or 'unscoped'}"
            want_rc, want = _oracle_emit(board, mode, core, tmp_path / f"o-{tag}")
            got_rc, got = _tan_emit(board, mode, core, tmp_path / f"t-{tag}")
            assert (got_rc == 0) == (want_rc == 0), (
                f"{board} --emit {mode} --core {core}: alp_project rc={want_rc}, "
                f"tan rc={got_rc}")
            if want_rc != 0:
                continue
            assert got == want, (
                f"{board} --emit {mode} --core {core} differs -- "
                + _first_diff(want.decode("utf-8", "replace"),
                              got.decode("utf-8", "replace")))
            assert b"\r\n" not in got, f"{board} {tag}: CRLF reached disk"
            compared += 1
    assert compared, f"{board}: nothing was compared"
    # ACCUMULATED, not assigned: the board-tree layer below feeds the same
    # counter for the same board and nothing pins which of the two lands first --
    # an assignment would silently discard the other's count and drop the total
    # under its own floor.
    _ARTEFACTS_COMPARED[board.parent.name] = (
        _ARTEFACTS_COMPARED.get(board.parent.name, 0) + compared)


def _oracle_board_tree(board: Path, core: str, destination: Path) -> tuple[int, dict]:
    """`alp_project.py --emit zephyr-board --output <dir>`, in-process.

    `(rc, {filename: bytes})`. `--output` names the board DIRECTORY, so the
    generator's own `<board_dir>/` key prefix is stripped by `alp_project.py`
    before the join -- reading the directory back is what compares the result of
    that stripping rather than trusting it.
    """
    from types import SimpleNamespace

    import alp_project

    destination.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(input=board, emit="zephyr-board", core=core,
                           output=destination,
                           metadata_root=alp_project.METADATA_ROOT)
    try:
        rc = alp_project._run_v2_per_core_emit(args)
    except SystemExit as err:
        return (int(err.code or 1), {})
    except Exception:  # noqa: BLE001 -- a refusal is a RESULT, compared below
        return (1, {})
    if rc != 0:
        return (rc, {})
    return (0, {p.name: p.read_bytes() for p in destination.iterdir()})


def _tan_board_tree(board: Path, core: str, destination: Path) -> tuple[int, dict]:
    """`tan.planner_emit.render_tree` + `write_tree`, the in-process engine."""
    from tan import planner_emit

    try:
        planner_emit.write_tree(
            planner_emit.render_tree("zephyr-board", sdk_root=SDK,
                                     board_yaml=board, core=core),
            destination,
        )
    except SystemExit as err:
        return (int(err.code or 1), {})
    except Exception:  # noqa: BLE001
        return (1, {})
    return (0, {p.name: p.read_bytes() for p in destination.iterdir()})


@pytest.mark.parametrize("board", _boards(), ids=lambda p: p.parent.name)
def test_the_in_process_engine_matches_alp_project_for_every_board_tree(
    planners, board, tmp_path
):
    """`--emit zephyr-board`, the one target that writes a DIRECTORY.

    Every core of every board, because the generator's family split is real: the
    `aen` family emits seven files (including the `.dts` and pinctrl `.dtsi`
    derived from `metadata/pinmux/aen.yaml`) while the V2N families emit three,
    and a core with no `zephyr_cpucluster` or no `topology.<id>.zephyr_full_name`
    is REFUSED -- so refusal parity is asserted here too, not just content.

    The comparison is on the FILE SET and each file's bytes, so a name that
    changed (they carry the SKU, the SoC variant tag and the cpucluster) fails as
    loudly as a body that did.
    """
    upstream, _ = planners
    try:
        project = upstream.load_board_yaml(board)
    except Exception:  # noqa: BLE001 -- covered by test_every_mode_is_byte_identical
        pytest.skip("board does not load; parity of the failure is asserted above")

    compared = 0
    for core in sorted(project.cores):
        want_rc, want = _oracle_board_tree(board, core, tmp_path / f"o-zb-{core}")
        got_rc, got = _tan_board_tree(board, core, tmp_path / f"t-zb-{core}")
        assert (got_rc == 0) == (want_rc == 0), (
            f"{board} --emit zephyr-board --core {core}: alp_project "
            f"rc={want_rc}, tan rc={got_rc}")
        if want_rc != 0:
            continue
        assert sorted(got) == sorted(want), (
            f"{board} --core {core}: board-tree FILE SET differs -- "
            f"alp_project {sorted(want)} vs tan {sorted(got)}")
        for name in sorted(want):
            assert got[name] == want[name], (
                f"{board} --core {core} {name} differs -- "
                + _first_diff(want[name].decode("utf-8", "replace"),
                              got[name].decode("utf-8", "replace")))
            assert b"\r\n" not in got[name], f"{board} {core} {name}: CRLF on disk"
        compared += len(want)
    # NOT `assert compared`: a board whose every core legitimately lacks a
    # `zephyr_cpucluster` compares nothing here and that is correct. The refusal
    # parity above is what such a board contributes.
    if compared:
        _ARTEFACTS_COMPARED[board.parent.name] = (
            _ARTEFACTS_COMPARED.get(board.parent.name, 0) + compared)


def test_the_breadth_layer_still_covers_every_board():
    """The layer's own size, so it cannot quietly stop measuring.

    Every board that LOADS must have contributed at least the two `--core`-less
    artefacts a project-scoped mode always yields; the floor on the total is what
    catches a mode disappearing from `GENERATE_MODES` (each board would still
    pass its own assertion).

    Two tests feed this counter -- the stream modes and the board trees -- so the
    floor covers both. Selecting only one of them with `-k` trips the floor; that
    is what a size check is for, and `-k` that excludes this test skips it.
    """
    if not _ARTEFACTS_COMPARED:
        pytest.skip("the breadth test did not run in this session (-k selection)")
    thin = {name: n for name, n in _ARTEFACTS_COMPARED.items() if n < 2}
    assert not thin, f"boards that compared almost nothing: {thin}"
    total = sum(_ARTEFACTS_COMPARED.values())
    assert len(_ARTEFACTS_COMPARED) >= 90, (
        f"only {len(_ARTEFACTS_COMPARED)} boards were measured")
    # 99 boards / 3109 artefacts as measured once the last six modes relocated
    # (738 before). The floor keeps room for boards coming and going while still
    # tripping if a whole mode leaves `GENERATE_MODES` -- the cheapest mode there
    # is worth ~100 artefacts, and the board-tree layer ~450.
    assert total >= 2800, f"only {total} artefacts were compared byte for byte"


# ==========================================================================
# The error contract on the in-process path
#
# In a subprocess every planner blow-up arrived as a non-zero exit code. Running
# in-process, each one now propagates into the CLI, where an escaped traceback
# puts nothing parseable on stdout and the extension renders no error at all --
# the defect class this port produced eight Criticals of. So each probe below
# asserts a CODED ENVELOPE, not merely a non-zero exit.
# ==========================================================================

def _tan_generate_failing(args: list[str], destination: Path,
                          engine: str = "in-process") -> dict:
    """Run `tan generate` expecting a refusal; return the parsed envelope."""
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate",
         "--sdk-root", str(SDK), "--output", str(destination),
         "--format", "json", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": engine},
        check=False,
    )
    assert proc.returncode != 0, f"expected a refusal, got 0: {proc.stdout}"
    assert proc.stderr.strip() == "", f"stderr must stay empty: {proc.stderr}"
    envelope = json.loads(proc.stdout.strip())  # a traceback would not parse
    assert envelope["issues"], "a refusal with no issue is unreportable"
    return envelope


@pytest.mark.parametrize(
    "board_text,label",
    [
        ("som:\n  sku: E1M-AEN801\n  hw_rev: [unclosed\n", "malformed-yaml"),
        ("som:\n  sku: NOT-A-REAL-SKU\n  hw_rev: a\ncores: {}\n", "missing-preset"),
        ("", "empty"),
        ("[]\n", "wrong-root-type"),
    ],
)
def test_a_planner_refusal_is_a_coded_envelope_not_a_traceback(
    tmp_path, board_text, label
):
    board = tmp_path / f"{label}.yaml"
    board.write_text(board_text, encoding="utf-8")
    envelope = _tan_generate_failing(
        ["--target", "ipc-contract-h", "--board-yaml", str(board)],
        tmp_path / "system_ipc.h",
    )
    assert envelope["exitCode"] in (2, 3), envelope
    assert envelope["issues"][0]["code"].startswith("generate."), envelope["issues"]


def test_an_unknown_core_id_is_a_coded_envelope(tmp_path):
    """`--core` naming a core the board does not have. `alp_project.py` printed
    `--core <id> not present in board.yaml` and exited 1; in-process the same
    refusal has to become an issue rather than a KeyError traceback."""
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    envelope = _tan_generate_failing(
        ["--target", "zephyr-conf", "--core", "no_such_core",
         "--board-yaml", str(board)],
        tmp_path / "alp.conf",
    )
    assert envelope["issues"][0]["code"] == "generate.emit-failed"
    assert "no_such_core" in envelope["issues"][0]["message"]
    assert envelope["data"]["failed"] == ["zephyr-conf"]
    assert envelope["data"]["engine"] == {"zephyr-conf": "in-process"}


def _incomplete_checkout(root: Path) -> Path:
    """A tree that satisfies I-31's SDK-root marker and the engine chooser's
    `board.schema.json` probe, and nothing else.

    The planted `scripts/alp_project.py` is EMPTY on purpose: it is the marker
    `discover_sdk_root` looks for, and it is also what the subprocess engine would
    execute -- an empty script exits 0 and writes nothing, which is what makes the
    fallback's own reporting testable below.
    """
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schemas = root / "metadata" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "board.schema.json").write_text("{}", encoding="utf-8")
    (root / "metadata" / "emit-registry-v1.json").write_text(
        json.dumps({"schemaVersion": 1, "modes": []}), encoding="utf-8")
    return root


def _generate_against(checkout: Path, board: Path, target: str,
                      output: Path, engine: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tan", "generate", "--sdk-root", str(checkout),
         "--target", target, "--board-yaml", str(board),
         "--output", str(output), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": engine},
        check=False,
    )


def test_an_incomplete_checkout_refuses_rather_than_rendering_nothing(tmp_path):
    """`TAN_GENERATE_EXECUTOR=in-process` against a checkout the planner cannot
    serve: one coded envelope, non-zero, no traceback.

    This is the assertion the pinned engine buys. Under `auto` the same run
    SUCCEEDS by falling back (see the next test) -- so a probe that left the
    engine to `auto` would have been measuring the spawned path while claiming to
    measure the in-process one.
    """
    assert SDK is not None
    board = SIMPLE_BOARD
    if board is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    broken = _incomplete_checkout(tmp_path / "half-a-checkout")

    proc = _generate_against(broken, board, "ipc-contract-h",
                             tmp_path / "system_ipc.h", "in-process")
    assert proc.returncode != 0, proc.stdout
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())  # a traceback would not parse
    assert envelope["issues"], envelope
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope


def test_a_fallback_on_an_incomplete_checkout_says_so(tmp_path):
    """The same checkout under `auto`: the run may SUCCEED, but never silently.

    The empty `alp_project.py` the fixture plants exits 0 and writes nothing, so
    `auto` produces a zero exit and an empty artefact -- exactly the shape that,
    unreported, would look like a working emit. So what is asserted here is the
    REPORTING: a `generate.in-process-unavailable` warning naming the reason, and
    a `data.engine` that says `subprocess` for the target. A fallback nobody can
    see is how this port got believed before it happened.
    """
    assert SDK is not None
    board = SIMPLE_BOARD
    if board is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    broken = _incomplete_checkout(tmp_path / "half-a-checkout")

    proc = _generate_against(broken, board, "ipc-contract-h",
                             tmp_path / "system_ipc.h", "auto")
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["data"]["engine"] == {"ipc-contract-h": "subprocess"}
    fallbacks = [i for i in envelope["issues"]
                 if i["code"] == "generate.in-process-unavailable"]
    assert len(fallbacks) == 1, envelope["issues"]
    assert fallbacks[0]["severity"] == "warning", fallbacks[0]
    # And the reason names the file that made the planner unusable, so the
    # warning is actionable rather than merely present.
    assert "peripheral-kconfig.json" in fallbacks[0]["message"], fallbacks[0]


def test_a_missing_som_preset_is_a_coded_envelope(tmp_path):
    """A SKU the board schema ACCEPTS but `metadata/e1m_modules/` has no preset
    for. `NOT-A-REAL-SKU` above never reaches the loader -- the schema pattern
    rejects it first -- so the loader's own "no preset" refusal, which is what a
    customer on a brand-new SKU actually hits, went unmeasured.
    """
    board = tmp_path / "board.yaml"
    board.write_text(
        "schemaVersion: 2\n"
        "som:\n  sku: E1M-NX9999\n  hw_rev: a\n"
        "cores:\n  m33:\n    os: zephyr\n",
        encoding="utf-8",
    )
    envelope = _tan_generate_failing(
        ["--target", "zephyr-conf", "--board-yaml", str(board)],
        tmp_path / "alp.conf",
    )
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope
    assert "NX9999" in json.dumps(envelope["issues"]), envelope["issues"]


def test_a_planner_orchestrator_error_is_a_coded_envelope(tmp_path):
    """`OrchestratorError` raised mid-render, not at load.

    An unknown `libraries:` entry passes the schema and passes the loader; it is
    `tan.planner.libraries.resolve_selection` that refuses, from inside
    `planner_emit._render_per_core`. That is the deepest point in the relocated
    planner a refusal can come from, and in a subprocess it was an exit code.
    """
    assert SDK is not None
    source = SIMPLE_BOARD
    if source is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    board = tmp_path / "board.yaml"
    board.write_text(
        source.read_text(encoding="utf-8").rstrip("\n")
        + "\nlibraries: [no-such-curated-library]\n",
        encoding="utf-8",
    )
    envelope = _tan_generate_failing(
        ["--target", "zephyr-conf", "--board-yaml", str(board)],
        tmp_path / "alp.conf",
    )
    assert envelope["issues"][0]["code"] == "generate.emit-failed", envelope["issues"]
    assert "no-such-curated-library" in envelope["issues"][0]["message"]


def test_an_unreadable_metadata_tree_is_a_coded_envelope(tmp_path):
    """A checkout whose `metadata/**` is PRESENT but corrupt.

    Newly load-bearing: the peripheral-Kconfig registry used to be parsed by
    alp-sdk's `alp_registries` inside a spawned interpreter, where a JSON error
    was that interpreter's problem. `tan.planner.slugs` parses it now, at import,
    in tan's own process -- so a corrupt registry has to come back as an issue
    rather than a `JSONDecodeError` traceback with no envelope at all.
    """
    assert SDK is not None
    board = SIMPLE_BOARD
    if board is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    broken = tmp_path / "corrupt-registry"
    (broken / "scripts").mkdir(parents=True)
    (broken / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    metadata = broken / "metadata"
    (metadata / "schemas").mkdir(parents=True)
    for relpath in (("schemas", "board.schema.json"), ("emit-registry-v1.json",)):
        source = SDK / "metadata"
        target = metadata
        for part in relpath:
            source = source / part
            target = target / part
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (metadata / "registries").mkdir()
    (metadata / "registries" / "peripheral-kconfig.json").write_text(
        "{ this is not json", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate", "--sdk-root", str(broken),
         "--target", "zephyr-conf", "--board-yaml", str(board),
         "--output", str(tmp_path / "alp.conf"), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": "in-process"},
        check=False,
    )
    assert proc.returncode != 0, proc.stdout
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["issues"], envelope
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope


# --------------------------------------------------------------------------
# ...and the same contract for the six targets that relocated LAST
#
# Moving six more emitters in-process widens the same surface again, so each of
# them gets the same probes rather than inheriting confidence from
# `ipc-contract-h`'s. They fail in DIFFERENT places -- `zephyr-board` inside its
# own `ZephyrBoardEmitError`, `carrier-netlist` inside `_resolve_sku` before any
# per-core model exists, `dts-overlay` inside a guard that used to call
# `sys.exit` -- and a `SystemExit` from any of them would tear the CLI down with
# nothing parseable on stdout.
# --------------------------------------------------------------------------

#: `(target, extra argv)`. `zephyr-board` hard-requires `--core`, so every probe
#: board.yaml below declares a core spelled `m33` for it to name.
_LAST_RELOCATED = (
    ("dts-overlay", []),
    ("native-sim-overlay", []),
    ("hw-info-h", []),
    ("west-libraries", []),
    ("carrier-netlist", []),
    ("zephyr-board", ["--core", "m33"]),
)

_LAST_RELOCATED_IDS = [t for t, _ in _LAST_RELOCATED]


@pytest.mark.parametrize("target,extra", _LAST_RELOCATED, ids=_LAST_RELOCATED_IDS)
@pytest.mark.parametrize(
    "board_text,label",
    [
        ("som:\n  sku: E1M-AEN801\n  hw_rev: [unclosed\n", "malformed-yaml"),
        ("", "empty"),
        ("[]\n", "wrong-root-type"),
    ],
)
def test_a_malformed_board_is_a_coded_envelope_for_every_relocated_target(
    tmp_path, target, extra, board_text, label
):
    board = tmp_path / f"{label}.yaml"
    board.write_text(board_text, encoding="utf-8")
    envelope = _tan_generate_failing(
        ["--target", target, "--board-yaml", str(board), *extra],
        tmp_path / "out",
    )
    assert envelope["exitCode"] in (2, 3), envelope
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), (
        envelope["issues"])


@pytest.mark.parametrize("target,extra", _LAST_RELOCATED, ids=_LAST_RELOCATED_IDS)
def test_a_missing_som_preset_is_a_coded_envelope_for_every_relocated_target(
    tmp_path, target, extra
):
    """A SKU the board schema ACCEPTS but `metadata/e1m_modules/` has no preset
    for -- what a customer on a brand-new SKU actually hits.

    `carrier-netlist` and `zephyr-board` reach it through `project_loader.
    _resolve_sku`, which in alp-sdk called `sys.exit`; the other three reach it
    through `load_board_yaml`. Both routes have to arrive as an issue.
    """
    board = tmp_path / "board.yaml"
    board.write_text(
        "schemaVersion: 2\n"
        "som:\n  sku: E1M-NX9999\n  hw_rev: a\n"
        "cores:\n  m33:\n    os: zephyr\n",
        encoding="utf-8",
    )
    envelope = _tan_generate_failing(
        ["--target", target, "--board-yaml", str(board), *extra],
        tmp_path / "out",
    )
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope
    assert "NX9999" in json.dumps(envelope["issues"]), envelope["issues"]


@pytest.mark.parametrize(
    "target", ["dts-overlay", "hw-info-h", "west-libraries", "zephyr-board"])
def test_an_unknown_core_id_is_a_coded_envelope_for_every_scoped_target(
    tmp_path, target
):
    """`--core` naming a core the board does not have, for each newly relocated
    target that accepts one.

    `alp_project.py` printed `--core <id> not present in board.yaml` and exited
    1. In-process the same refusal has to become an issue rather than the
    `KeyError` that `project.cores[core]` raises one line later.
    """
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    envelope = _tan_generate_failing(
        ["--target", target, "--core", "no_such_core", "--board-yaml", str(board)],
        tmp_path / "out",
    )
    assert envelope["issues"][0]["code"] == "generate.emit-failed", envelope["issues"]
    assert "no_such_core" in envelope["issues"][0]["message"]
    assert envelope["data"]["failed"] == [target]
    assert envelope["data"]["engine"] == {target: "in-process"}


@pytest.mark.parametrize("target,extra", _LAST_RELOCATED, ids=_LAST_RELOCATED_IDS)
def test_an_unreadable_metadata_tree_is_a_coded_envelope_for_every_relocated_target(
    tmp_path, target, extra
):
    """A checkout that passes the engine chooser's probe and then cannot serve
    the emit, for each newly relocated target.

    The probe is `metadata/schemas/board.schema.json` plus a readable emit
    registry; a tree with those and nothing else is exactly the shape a partial
    clone or a stale `tan sdk switch` pin produces. Every one of these targets
    then fails somewhere different, and all of them must produce exactly ONE
    envelope on stdout.
    """
    assert SDK is not None
    board = SIMPLE_BOARD
    if board is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    broken = tmp_path / "half-a-checkout"
    (broken / "scripts").mkdir(parents=True)
    (broken / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schemas = broken / "metadata" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "board.schema.json").write_text(
        (SDK / "metadata" / "schemas" / "board.schema.json").read_text(
            encoding="utf-8"), encoding="utf-8")
    (broken / "metadata" / "emit-registry-v1.json").write_text(
        (SDK / "metadata" / "emit-registry-v1.json").read_text(encoding="utf-8"),
        encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate", "--sdk-root", str(broken),
         "--target", target, "--board-yaml", str(board),
         "--output", str(tmp_path / "out"), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": "in-process"},
        check=False,
    )
    assert proc.returncode != 0, proc.stdout
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())  # a traceback would not parse
    assert envelope["issues"], envelope
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope


def test_a_missing_board_header_is_a_coded_envelope(tmp_path):
    """`--emit dts-overlay`'s deepest refusal, and the one this move converted
    from `sys.exit` to a raise.

    A board.yaml naming a `preset:` whose `include/alp/boards/alp_<name>.h` is
    absent from the checkout. Inside a spawned `alp_project.py` the `sys.exit`
    there was just a non-zero rc; in `tan`'s own process it is a `SystemExit`,
    which no `except Exception` stops -- it would exit the CLI with an empty
    stdout and the extension would render no error at all.
    """
    assert SDK is not None
    board = SIMPLE_BOARD
    if board is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    # A checkout with real metadata but NO include/alp/boards/ tree: the SoM
    # preset and schemas resolve, the board header does not.
    broken = tmp_path / "no-headers"
    (broken / "scripts").mkdir(parents=True)
    (broken / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    _copytree(SDK / "metadata", broken / "metadata")

    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate", "--sdk-root", str(broken),
         "--target", "dts-overlay", "--board-yaml", str(board),
         "--output", str(tmp_path / "alp.overlay"), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": "in-process"},
        check=False,
    )
    assert proc.returncode != 0, proc.stdout
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["issues"][0]["code"] == "generate.emit-failed", envelope["issues"]
    assert "board header" in envelope["issues"][0]["message"], envelope["issues"]


def test_an_unresolvable_board_preset_is_a_coded_envelope(tmp_path):
    """`--emit carrier-netlist`'s own refusal path: `preset:` naming a board
    `metadata/boards/` does not have.

    This mode never builds a per-core model, so it is the one target whose
    failures come out of `project_loader` alone -- `_resolve_board`, which was a
    `sys.exit` in alp-sdk too.
    """
    assert SDK is not None
    source = SIMPLE_BOARD
    if source is None:
        pytest.skip("this checkout ships no examples/*/board.yaml")
    board = tmp_path / "board.yaml"
    board.write_text(
        source.read_text(encoding="utf-8").rstrip("\n")
        + "\npreset: no-such-carrier-board\n",
        encoding="utf-8",
    )
    envelope = _tan_generate_failing(
        ["--target", "carrier-netlist", "--board-yaml", str(board)],
        tmp_path / "carrier-netlist.json",
    )
    assert envelope["issues"][0]["code"] == "generate.emit-failed", envelope["issues"]
    assert "no-such-carrier-board" in envelope["issues"][0]["message"]


def _copytree(source: Path, target: Path) -> None:
    """`shutil.copytree` without importing shutil for one call site."""
    import shutil
    shutil.copytree(source, target)


# ==========================================================================
# The import closure: what `tan` LOADS, not what it emits
#
# The measurement the rest of this file cannot make. Byte-identical emit is
# satisfied by a planner that renders from `tan` and imports half of alp-sdk to
# do it -- which is exactly what the relocation shipped the first time round.
#
# Run in a CHILD interpreter on purpose: this module's own `planners` fixture
# imports `alp_orchestrate` to have something to compare against, so measuring
# in this process would always find it.
# ==========================================================================

_CLOSURE_PROBE = '''
import json, os, sys, tempfile
from pathlib import Path

SDK = Path(os.environ["ALP_SDK_ROOT"]).resolve()
SCRIPTS = SDK / "scripts"
BOARD = Path(sys.argv[1])

from tan.planner_root import bind_sdk_root

bind_sdk_root(SDK)
import tan.planner
from tan import planner_emit


def loaded():
    out = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            Path(f).resolve().relative_to(SCRIPTS)
        except ValueError:
            continue
        out.append(name)
    return sorted(out)


project = tan.planner.load_board_yaml(BOARD)
zephyr = next((c for c in sorted(project.cores)
               if project.cores[c].os == "zephyr"), None)
tmp = Path(tempfile.mkdtemp())

# CUMULATIVE per mode, and every mode `tan generate` can reach. Attributing the
# count to the mode that caused it is what makes a regression name itself: with
# one total, a single target quietly going back to importing alp-sdk reads as an
# unattributable non-zero.
per_mode = {}
for mode in sorted(planner_emit.IN_PROCESS_MODES):
    core = zephyr if mode in ("zephyr-conf", "cmake-args", "zephyr-board") else None
    if mode in planner_emit.TREE_MODES:
        planner_emit.write_tree(
            planner_emit.render_tree(mode, sdk_root=SDK, board_yaml=BOARD,
                                     core=core), tmp / mode)
    else:
        planner_emit.write(
            planner_emit.render(mode, sdk_root=SDK, board_yaml=BOARD, core=core),
            tmp / mode)
    per_mode[mode] = loaded()
print(json.dumps(per_mode))
'''


def test_the_in_process_path_loads_none_of_the_sdks_python(tmp_path):
    """Rendering EVERY reachable mode must load ZERO modules off `<sdk>/scripts`.

    Not "fewer" -- zero. A single surviving edge keeps the whole graph alive: one
    `from alp_project import resolve_memory_map` pulled in `alp_project_loader`,
    whose `_load_yaml` lazily imported `alp_orchestrate.loader`, whose package
    `__init__` imported all 19 of its siblings. That is how 6 import statements
    measured as 32 loaded modules, and why the number to assert is 0 rather than
    some smaller number.

    Measured per mode across all eleven, and the count is CUMULATIVE, so the
    failure message names the mode that first pulled something in rather than
    reporting one anonymous total. That matters here specifically: the last six
    modes reach `project_emit/`, `project_loader.py` and `zephyr_board.py`, whose
    alp-sdk originals imported `alp_project_loader` and `alp_registries` at module
    scope -- exactly the shape that measured as 32.
    """
    assert SDK is not None
    board = SDK / "examples" / "multicore" / "rpmsg-aen" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    probe = tmp_path / "closure_probe.py"
    probe.write_text(_CLOSURE_PROBE, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(probe), str(board)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "ALP_SDK_ROOT": str(SDK)},
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    per_mode = json.loads(proc.stdout.strip())

    # The probe iterates `IN_PROCESS_MODES`, so this is what pins the SET as well
    # as the counts -- a mode dropped from that frozenset would otherwise go
    # unmeasured and pass.
    assert len(per_mode) == 11, f"only {sorted(per_mode)} were measured"

    orchestrate = {
        mode: [n for n in loaded
               if n == "alp_orchestrate" or n.startswith("alp_orchestrate.")]
        for mode, loaded in per_mode.items()
    }
    still = {m: n for m, n in orchestrate.items() if n}
    assert not still, (
        "`alp_orchestrate` is STILL loaded on the in-process path -- the planner "
        f"relocation is not finished: {still}")
    nonzero = {m: loaded for m, loaded in per_mode.items() if loaded}
    assert not nonzero, (
        "the relocated planner still loads alp-sdk Python; every one of these has "
        f"to become a `tan` module or the SDK's Python cannot be deleted: {nonzero}")


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

plan = _acquire_plan(None, str(SDK), str(board))
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
