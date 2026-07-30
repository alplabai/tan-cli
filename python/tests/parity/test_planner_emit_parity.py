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

Three further layers sit below that library comparison, because `tan generate`
now renders the relocated modes IN-PROCESS instead of spawning
`scripts/alp_project.py`, and a library-level match does not prove the command
produces the same file:

* `tan generate --output <path>` spawned for real, once per `board.yaml`, with
  the FILE it writes compared byte for byte against the SDK's own front door.
  That is the shape `cmake/alp.cmake` drives on every Zephyr configure, and it
  covers what a library comparison cannot -- newline translation, a wrong
  destination, an artefact leaking onto stdout beside the envelope. The engine
  is PINNED via `TAN_GENERATE_EXECUTOR` and then asserted from `data.engine`,
  so a fallback cannot let this layer measure the subprocess path and report it
  as proof of the in-process one.
* the breadth layer, driving both sides in-process so all 99 boards x all five
  relocated modes x both `--core` forms is affordable. Its oracle is
  `alp_project.py`'s OWN dispatch functions -- `--emit zephyr-conf` is not
  `_slice_alp_conf`, it is that slice inside a per-core wrapper.
* the error contract, because in-process execution widens it: every planner
  exception that used to die inside a subprocess and arrive as an exit code now
  propagates into the CLI, where an escaped traceback renders as nothing at all.

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
    bytes. Run twice through the real CLI, once per engine, and diffed."""
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
    ):
        common = ["--target", mode, "--board-yaml", str(board), *args]
        spawned = _tan_generate(common, tmp_path / f"{mode}.sub", "subprocess")
        native = _tan_generate(common, tmp_path / f"{mode}.native", "in-process")
        assert native == spawned, f"{mode}: " + _first_diff(
            spawned.decode("utf-8", "replace"), native.decode("utf-8", "replace"))


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

#: `(mode, core_os_classes_or_None)`. `None` means the mode takes no `--core`;
#: a tuple means the scoped form is exercised against the first core whose `os`
#: the mode accepts (per `compatible.os` in the emit registry). The unscoped
#: form -- the one a bare `tan generate` uses -- is exercised for all five.
GENERATE_MODES = (
    ("zephyr-conf", ("zephyr",)),
    ("cmake-args", ("baremetal", "zephyr")),
    ("yocto-conf", ("yocto",)),
    ("os-topology", None),
    ("ipc-contract-h", None),
)


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
    runner = (alp_project._run_v2_emit if mode in project_level
              else alp_project._run_v2_per_core_emit)
    try:
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
        if core_classes is not None:
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


def test_an_incomplete_checkout_is_a_coded_envelope(tmp_path):
    """A checkout that passes the engine chooser's probe and then cannot serve
    the emit: the planner's own loader raises mid-render, and the CLI must still
    produce exactly one envelope."""
    assert SDK is not None
    board = SDK / "examples" / "blinky" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    broken = tmp_path / "half-a-checkout"
    (broken / "scripts").mkdir(parents=True)
    (broken / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schemas = broken / "metadata" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "board.schema.json").write_text("{}", encoding="utf-8")
    (broken / "metadata" / "emit-registry-v1.json").write_text(
        json.dumps({"schemaVersion": 1, "modes": []}), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "tan", "generate", "--sdk-root", str(broken),
         "--target", "ipc-contract-h", "--board-yaml", str(board),
         "--output", str(tmp_path / "system_ipc.h"), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PYTHON_ROOT),
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT),
             "TAN_GENERATE_EXECUTOR": "auto"},
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout.strip())
    assert envelope["issues"], envelope
    assert all(i["code"].startswith("generate.") for i in envelope["issues"]), envelope
