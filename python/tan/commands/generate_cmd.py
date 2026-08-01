# SPDX-License-Identifier: Apache-2.0
"""`tan generate` -- run the board-derived emitters and report what landed.

**tan invents no output of its own**, but it no longer spawns alp-sdk to produce
it either. All TWELVE targets render in-process from `tan.planner`
(`tan.planner_emit.IN_PROCESS_MODES`) -- the five modes the planner relocation
brought over first, the six whose renderers moved with it (`dts-overlay`,
`native-sim-overlay`, `hw-info-h`, `west-libraries`, `carrier-netlist` and
`zephyr-board`), plus `composed-route-table` (the older debug view of
`carrier-netlist`'s own route rows, ported alongside it). No interpreter on
PATH, no PYTHONPATH, no process.

The spawned engine survives as an ESCAPE HATCH, not a code path anything reaches
by default:

      <python> <sdk>/scripts/alp_project.py --input <board.yaml> \
               --emit <mode> --output <path> [--core <id>]

It runs only when `TAN_GENERATE_EXECUTOR=subprocess` pins it, or when the
in-process path cannot serve the checkout at all. Keeping it is what lets the
parity suite render the same target both ways and diff the bytes.

`tan.planner_emit` documents how `metadata/emit-registry-v1.json` decides which
renderer owns which mode. Either way the emitted bytes, the schemas behind them,
and every hardware fact they encode stay in alp-sdk (ADR-0017; I-26). There is no
template here, no SKU list, no address, no pin name -- `som.sku` is read from the
customer's own `board.yaml` purely to spell one directory name, exactly as the
oracle does.

**A fall back to spawning is never silent.** When the in-process path cannot
serve a checkout (not a usable checkout, unreadable emit registry, or a `tan`
build without `PyYAML`/`jsonschema`), the run still succeeds via the subprocess
-- but it says so, with a `generate.in-process-unavailable` warning naming the
reason and a `data.engine` that reports `subprocess` for every target.
`TAN_GENERATE_EXECUTOR=subprocess` pins the old path deliberately;
`=in-process` refuses rather than falling back, which is what makes the parity
suite able to assert the engine it thinks it measured.

A resolvable SDK checkout is still REQUIRED on both engines -- `metadata/**`
never relocated -- so without one this command refuses
(`generate.sdk-root-unresolved`). I-31 pins `scripts/alp_project.py` as tan's
SDK-root marker; the invariant that forbids shelling the SDK (I-32, and
anti-pattern 22 beside it) is scoped to `tan init`/`tan scaffold`, which read a
VENDORED `--emit scaffold` tree so they work with no checkout at all.

**Every failure path is an envelope, never a traceback.** A missing or
unreadable `board.yaml`, an unresolved SDK, a bad `--target`/`--core` pairing, a
`native_sim` overlay that would be truncated, an interpreter too old to run the
SDK scripts, a spawn that dies or returns garbage -- each has a
`generate.<code>` issue and an exit code below, and the command carries a
catch-all backstop for anything unforeseen. The extension parses stdout whole:
a traceback there renders as nothing at all, with no error.

The in-process engine WIDENS that surface, which is why `_emit_one_in_process`
catches as broadly as it does: a planner blow-up that used to die inside a
subprocess and arrive as an exit code now propagates into this process, and
`tan.planner.loader` reports a missing dependency by calling `sys.exit` -- a
`SystemExit`, which no `except Exception` would stop.

Exit codes, verbatim from the oracle: missing board / unresolved SDK / bad
target / unresolvable SKU are ValidationFailure (2); the overlay guard is
WriteFailure (3), as is any target whose emit failed; a too-old interpreter is
RuntimeFailure (1).

**`--output` exists because CMake, not tan, decides where a Zephyr build reads
from.** alp-sdk's `cmake/alp.cmake` -- the one helper all 96 Zephyr examples
call at configure time -- writes to `${CMAKE_BINARY_DIR}/generated/alp.conf`,
and `CMAKE_BINARY_DIR` is NOT the project directory (an in-tree `west build
examples/<...>` puts it under the repo root). Without `--output`, `tan` would
emit into `<project>/build/generated/` and that build would read nothing; the
helper accordingly PROBES `generate --help` for `--output` and falls back to
shelling `scripts/alp_project.py` when it is absent. So the flag is what makes
`tan` the emitter for every example, and its default is exactly the previous
hardcoded path -- nothing that already worked changes.

`--output` writes a FILE. It never puts the file's contents on stdout: stdout
carries the envelope and nothing else, in both formats.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

# One home each for two decisions this command shares with the rest of the CLI:
# where an alp-sdk checkout is (I-31's marker, spelled once in `build_cmd`), and
# which interpreter name the SDK's own scripts run under -- a PATH name, never
# `sys.executable`, which is `tan` itself once PyInstaller has frozen it.
from tan import planner_emit
from tan.commands.build_cmd import _planner_python, resolve_sdk_root_wide
from tan.commands.sdk_cmd import project_pin_issue
from tan.commands.doctor_cmd import probe, resolve_manifest_python_floor
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: Pin the engine instead of letting `auto` decide. An env var, not a flag:
#: `cmake/alp.cmake` builds one argv that must stay accepted verbatim, and this
#: is an escape hatch for a host where the in-process path misbehaves plus the
#: seam the parity suite uses to compare both engines against the same oracle.
#:
#: `subprocess` spawns alp-sdk for every target (the pre-relocation behaviour).
#: `in-process` refuses instead of falling back, so a run cannot quietly measure
#: the engine it was not testing.
EXECUTOR_ENV = "TAN_GENERATE_EXECUTOR"
EXECUTORS = ("auto", "in-process", "subprocess")

#: Every emit mode a bare `tan generate` / `--all` runs, in the order
#: `data.targets` reports them, mapped to its output path relative to the
#: workspace root. ONE structure, deliberately: the Rust carries the mode list
#: and the path table separately (`tan_core::ALL_EMIT_MODES` plus
#: `GENERATION_TARGET_CATALOG`) and tan-cli#165 exists because those two drifted
#: apart silently, the catalog having grown only four of the nine.
#:
#: The literals are `/`-separated because they are SDK conventions, not host
#: paths -- `_output_path` folds them component-wise so a Windows run cannot
#: emit `build/generated/alp.conf` beside `build\boards\<dir>` in one envelope
#: (tan-cli#165 review finding 4).
#:
#: `native-sim-overlay` is the one entry outside `build/generated/`: Zephyr
#: auto-discovers `boards/<board>.overlay` in the app's own SOURCE tree, so
#: `west build -b native_sim/native/64` only finds it there. That it writes into
#: a hand-editable tree is why `_overlay_would_overwrite` exists.
_OUTPUT_RELATIVE_PATH = {
    "zephyr-conf": "build/generated/alp.conf",
    "dts-overlay": "build/generated/alp.overlay",
    "native-sim-overlay": "boards/native_sim_native_64.overlay",
    "cmake-args": "build/generated/alp-cmake-args.txt",
    "yocto-conf": "build/generated/alp-yocto.conf",
    "carrier-netlist": "build/generated/carrier-netlist.json",
    "west-libraries": "build/generated/alp-west-libs.yml",
    "hw-info-h": "build/generated/alp_hw_info_build.h",
    "os-topology": "build/generated/os-topology.json",
    # Explicit-only (see EXPLICIT_ONLY_TARGETS) -- still in this table because
    # each has one canonical path.
    # `composed-route-table` is the older debug/demonstrator view of
    # `carrier-netlist`'s own route rows -- alp-sdk's docs/cli.md lists it as
    # "JSON route-table dump (demonstrator)", a maintainer-only tool with no
    # product consumer. The Rust oracle's `tan_core::ALL_EMIT_MODES`
    # (crates/tan-core/src/loader.rs) excludes it from the default/`--all`
    # set for the same reason, so a bare `tan generate` never runs a full
    # pad-route composition.
    "composed-route-table": "build/generated/composed-route-table.json",
    # `alp_sdk_ipc_contract_header()` in alp-sdk's `cmake/alp.cmake` names
    # exactly this one: `${CMAKE_BINARY_DIR}/generated/alp/system_ipc.h`.
    "ipc-contract-h": "build/generated/alp/system_ipc.h",
}

#: The one target that is NOT defaultable: it hard-requires `--core` and writes
#: a DIRECTORY of files named per SKU+core, so a bare `tan generate` has no path
#: to give it. Reachable only via an explicit `--target zephyr-board --core <id>`.
ZEPHYR_BOARD = "zephyr-board"

#: The cross-core IPC contract header (`<alp/system_ipc.h>`), the second target
#: reachable only by an explicit `--target`. Not core-scoped -- the contract IS
#: the agreement between cores -- and not defaultable either: it renders a
#: board.yaml `ipc:` block, which four multicore examples have and the other 95
#: do not, so folding it into the default set would make a bare `tan generate`
#: report a failed target on almost every project.
IPC_CONTRACT_H = "ipc-contract-h"

#: The pad-route debug/demonstrator view (see `_OUTPUT_RELATIVE_PATH`'s
#: comment). Not defaultable: it is a maintainer-only tool with no product
#: consumer, so a bare `tan generate` must not pay for a full pad-route
#: composition on every project -- a prior revision listed this in
#: `_OUTPUT_RELATIVE_PATH` without also listing it here, which silently grew
#: the default set from 9 targets to 10.
COMPOSED_ROUTE_TABLE = "composed-route-table"

#: Targets an explicit `--target` can reach but the default/`--all` set never
#: does. `zephyr-board` is absent from `_OUTPUT_RELATIVE_PATH` (it writes a
#: directory, named per SKU+core); `composed-route-table` and
#: `ipc-contract-h` are both in it.
EXPLICIT_ONLY_TARGETS = frozenset(
    {ZEPHYR_BOARD, COMPOSED_ROUTE_TABLE, IPC_CONTRACT_H}
)

#: The default/`--all` target set. Derived, so it cannot disagree with the path
#: table above.
ALL_EMIT_MODES = tuple(
    mode for mode in _OUTPUT_RELATIVE_PATH if mode not in EXPLICIT_ONLY_TARGETS
)

#: Targets `--core` optionally SCOPES, beyond `zephyr-board` which requires it.
#: Verbatim from `alp_project.py`'s own `--core` help. `carrier-netlist`,
#: `composed-route-table` and `native-sim-overlay` are excluded because the SDK
#: never reads `--core` for them at all, and `os-topology` because it accepts
#: the flag and then prints `--core is ignored for --emit os-topology
#: (project-level emit)` and ignores it -- the same "does nothing" shape, so all
#: four are refused rather than silently accepted.
CORE_SCOPABLE_TARGETS = (
    "zephyr-conf",
    "yocto-conf",
    "cmake-args",
    "dts-overlay",
    "west-libraries",
    "hw-info-h",
)

#: Seconds a single `alp_project.py` emit may take. Generous: a cold emit
#: imports the whole orchestrator package and reads every metadata file for the
#: SKU. Bounded regardless, so a planner that wedges cannot hang a `--format
#: json` consumer with no envelope and no error -- the failure mode a missing
#: timeout produces is indistinguishable from a slow build.
EMIT_TIMEOUT_S = 300


class GenerateError(Exception):
    """A refusal whose issue code and exit code are already decided."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def zephyr_board_dir_name(sku: str, core_id: str) -> str | None:
    """`som.sku` + core id -> the SDK's `--emit zephyr-board` directory name:
    `E1M-AEN801` + `m55_hp` -> `alp_e1m_aen801_m55_hp`.

    `alp_project.py` keys every generated file under exactly this name and
    strips it before joining onto `--output`, so `--output` must BE this
    directory. A bare core id would collide across two SoMs that share one
    (tan-cli#116 review finding 1) -- a project retargeted from E1M-AEN901 to
    E1M-AEN801 would land each run's files beside the other SoM's stale ones.

    `None` on any other prefix, mirroring the SDK's own `unrecognised SKU
    prefix` guard. This is a naming convention, not a SKU list: no SKU is
    enumerated anywhere in tan.
    """
    if not sku.startswith("E1M-"):
        return None
    return f"alp_e1m_{sku[len('E1M-'):].lower()}_{core_id}"


def _board_sku(path: Path) -> str | None:
    """`som.sku` out of `board.yaml`, or `None` for every way that can fail.

    `None` is not an error here -- the caller turns it into
    `generate.board-sku-unresolved`, which is exactly what the oracle's
    `read_to_string(..).ok().and_then(..)` chain produces for an absent,
    unreadable, non-UTF-8 or wrong-shaped file. So a bad board.yaml is a
    refusal with a code, never a traceback.

    PyYAML when it is importable (a Zephyr workspace needs it anyway), else a
    two-line scan for the one nested key this needs. tan ships no YAML
    dependency, and the same reasoning as `validate_cmd._load_yaml` applies:
    answer only the question actually asked.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return _scan_som_sku(text)
    try:
        doc = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        return None
    if not isinstance(doc, dict):
        return None
    som = doc.get("som")
    if not isinstance(som, dict):
        return None
    sku = som.get("sku")
    return sku if isinstance(sku, str) and sku else None


def _scan_som_sku(text: str) -> str | None:
    """The no-PyYAML fallback: the `sku:` scalar inside the top-level `som:`
    block. Deliberately not a YAML parser -- it answers one question."""
    inside = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[:1].isspace():
            inside = stripped.split(":", 1)[0].strip() == "som"
            continue
        if inside:
            key, sep, value = stripped.partition(":")
            if sep and key.strip() == "sku":
                return value.strip().strip("'\"") or None
    return None


def resolve_targets(
    target: str | None, all_targets: bool, core: str | None
) -> tuple[str, ...]:
    """Which emit modes to run. Raises `GenerateError` for every user-supplied
    argument-shape mistake.

    ValidationFailure, never InternalFailure: an unknown `--target` or a
    `--core` paired with a target that does not consume it is an ordinary usage
    mistake, and it used to surface identically to a genuine tan bug
    (tan-cli#117 review finding 3). `--core` is refused rather than silently
    ignored -- silently ignoring it lets a user believe it scoped something.
    """

    def invalid(message: str) -> GenerateError:
        return GenerateError(
            "generate.invalid-target", message, ExitCode.VALIDATION_FAILURE
        )

    if core is not None:
        core_is_valid_here = target == ZEPHYR_BOARD or (
            not all_targets and target in CORE_SCOPABLE_TARGETS
        )
        if not core_is_valid_here:
            raise invalid(
                "`--core` is only valid with `--target zephyr-board` (required), or "
                f"one of {', '.join(CORE_SCOPABLE_TARGETS)} (optional scoping); it "
                "does nothing for the default/--all target set."
            )

    if all_targets or target is None:
        return ALL_EMIT_MODES

    if target == ZEPHYR_BOARD:
        if core is None:
            raise invalid(
                "`--target zephyr-board` requires `--core <id>` (it generates one "
                "core's Zephyr board tree)."
            )
        return (ZEPHYR_BOARD,)

    if target in _OUTPUT_RELATIVE_PATH:
        return (target,)

    raise invalid(f"Unsupported generate target '{target}'.")


def _output_path(
    workspace_root: Path, emit: str, board_dir_name: str | None
) -> Path:
    """Where `emit`'s output goes, with native separators throughout.

    `zephyr-board` is the one target with no literal path: it writes a
    directory per SKU+core under `build/boards/`, so
    `west build --board-root build/boards` finds every generated board tree
    without them colliding.
    """
    if emit == ZEPHYR_BOARD:
        # `run` resolves the name before reaching this target (refusing
        # otherwise), so the fallback is only for a direct call.
        return workspace_root / "build" / "boards" / (board_dir_name or "board")
    path = workspace_root
    for part in _OUTPUT_RELATIVE_PATH[emit].split("/"):
        path = path / part
    return path


def _resolve_output_override(
    output: str, targets: tuple[str, ...], workspace_root: Path
) -> Path:
    """`--output` as an absolute path. Touches nothing on disk.

    `--output` names ONE destination, so it needs exactly one target: paired
    with the default set (or `--all`) it would silently mean "overwrite this
    file eight times", the last emit winning. That is a usage mistake, so it is
    ValidationFailure with the same `generate.invalid-target` code the other
    argument-shape refusals carry.
    """
    if len(targets) != 1:
        raise GenerateError(
            "generate.invalid-target",
            "`--output` names a single destination, so it requires exactly one "
            "`--target <mode>`; it cannot serve the default/--all target set.",
            ExitCode.VALIDATION_FAILURE,
        )
    candidate = Path(output)
    return candidate if candidate.is_absolute() else workspace_root / candidate


def _ensure_writable(output: Path, target: str) -> None:
    """Create `output`'s parent and prove the destination is writable.

    Done HERE rather than discovered when the spawned emitter trips over it, for
    two reasons: a `PermissionError` traceback on the SDK's stderr is a terrible
    issue message, and the parent directory has to be created either way --
    `alp_sdk_zephyr_conf()` asks for `${CMAKE_BINARY_DIR}/generated/alp.conf` in
    a build tree where `generated/` does not exist yet.

    `ValueError` is caught beside `OSError` deliberately: a path carrying a
    surrogate or an embedded NUL raises `UnicodeEncodeError`/`ValueError`, not
    `OSError`, and letting one reach the command's catch-all backstop would
    report an unwritable path as InternalFailure (5) instead of WriteFailure (3).
    """
    try:
        if target == ZEPHYR_BOARD:
            # `--emit zephyr-board`'s `--output` IS the board directory.
            output.mkdir(parents=True, exist_ok=True)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            # Append mode, not write: proving writability must not truncate a
            # file whose emit could still be refused further down.
            with output.open("ab"):
                pass
    except (OSError, ValueError) as err:
        raise GenerateError(
            "generate.output-unwritable",
            f"--output path `{output}` cannot be written: {err}",
            ExitCode.WRITE_FAILURE,
        ) from err


def _overlay_would_overwrite(
    workspace_root: Path,
    targets: tuple[str, ...],
    force: bool,
    output_override: Path | None = None,
) -> bool:
    """True when this run would truncate an existing hand-edited
    `boards/native_sim_native_64.overlay`.

    `native-sim-overlay` is the only target writing into the app's own source
    tree. Every other writer into a user tree (`init`, `scaffold`) diffs against
    disk and refuses without `--force`; a bare `tan generate` used to truncate a
    developer's tuned overlay with no check at all.

    `output_override` is checked in place of the default path when `--output`
    redirected the emit: the guard has to name the file this run would actually
    truncate, not the one it would have without the flag.
    """
    if force or "native-sim-overlay" not in targets:
        return False
    destination = output_override or _output_path(
        workspace_root, "native-sim-overlay", None
    )
    return destination.exists()


def _python_too_old(python: str, floor: tuple[int, int]) -> str | None:
    """A message when `python` is below the SDK's floor, else `None`.

    `floor` is the resolved SDK's OWN declared floor -- they use
    `@dataclass(slots=True)`, so `resolve_manifest_python_floor` (I-24) reads
    `<sdk>/metadata/bootstrap.json`'s `pythonMinVersion` live rather than a
    second hardcoded 3.10 that could drift from the manifest's, or from
    `model_cmd`'s own copy. `None` also covers "could not tell" -- a missing or
    broken interpreter is a different failure the real spawn surfaces on its
    own, and blocking on an unknown would refuse a perfectly good host. Bounded
    by `probe`'s timeout, which is why this reuses it rather than spawning by
    hand.
    """
    out = probe([python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
    if out is None:
        return None
    try:
        major, minor = (int(part) for part in out.strip().splitlines()[-1].split(".")[:2])
    except (IndexError, ValueError):
        return None
    if (major, minor) >= floor:
        return None
    return (
        f"Python {major}.{minor} found at `{python}`, but alp-sdk requires Python "
        f"{floor[0]}.{floor[1]}+. Put a newer `python` first on PATH "
        "(VS Code users can instead set alpSdk.pythonPath)."
    )


def _relative_or_full(workspace_root: Path, output_path: Path) -> str:
    try:
        return str(output_path.relative_to(workspace_root))
    except ValueError:
        return str(output_path)


def _emit_one(
    python: str,
    script: Path,
    board_path: Path,
    emit: str,
    output: Path,
    core: str | None,
) -> str | None:
    """Run one emit. `None` on success, else the message for its
    `generate.emit-failed` issue.

    Every way a subprocess can fail is a message, not an exception: the binary
    is absent or not executable (`OSError`), it wedges (`TimeoutExpired`), or it
    exits non-zero (the SDK's own stderr is the most useful message there, so it
    is forwarded verbatim when present).
    """
    argv = [
        python,
        str(script),
        "--input",
        str(board_path),
        "--emit",
        emit,
        "--output",
        str(output),
    ]
    # zephyr-board requires --core (already validated); the scopable targets
    # accept it optionally, and forwarding it is what makes `--target
    # zephyr-conf --core m55_hp` byte-identical to what a build materialises for
    # that core (tan-cli#117 review finding 2).
    if core is not None and (emit == ZEPHYR_BOARD or emit in CORE_SCOPABLE_TARGETS):
        argv += ["--core", core]

    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=EMIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return f"Generation failed for target '{emit}': {err}"
    if out.returncode == 0:
        return None
    stderr = (out.stderr or "").strip()
    return stderr or f"Generation failed for target '{emit}'."


def _emit_one_in_process(
    sdk_root: Path,
    board_path: Path,
    emit: str,
    output: Path,
    core: str | None,
) -> str | None:
    """Render one relocated emit with `tan.planner` and write it. `None` on
    success, else the message for its `generate.emit-failed` issue.

    The exception funnel this command's contract depends on. In a subprocess
    every planner blow-up -- a malformed `board.yaml`, a missing SoM preset, an
    unreadable `metadata/**`, an `OrchestratorError`, a bare `ValueError` from
    the Ethos-U sizing path (I-48) -- arrived as a non-zero exit code and a
    string. In-process it propagates into THIS process instead, and an escaped
    traceback puts nothing parseable on stdout: the extension renders no error
    at all. So the catch is broad on purpose.

    `SystemExit` is caught separately because it is not an `Exception`:
    `tan.planner.loader` and `kconfig_symbols` call `sys.exit()` on a missing
    dependency, which in-process would tear down the CLI past every envelope
    guarantee.

    `zephyr-board` splits off to `render_tree`/`write_tree`: it writes a
    DIRECTORY of files named per SKU+core, and `output` for that target IS that
    directory (the same contract `alp_project.py --emit zephyr-board --output
    <dir>` had). One `if`, inside the same funnel, so its failures are coded
    exactly like every other target's.
    """
    try:
        if emit in planner_emit.TREE_MODES:
            planner_emit.write_tree(
                planner_emit.render_tree(
                    emit, sdk_root=sdk_root, board_yaml=board_path, core=core
                ),
                output,
            )
        else:
            text = planner_emit.render(
                emit, sdk_root=sdk_root, board_yaml=board_path, core=core
            )
            planner_emit.write(text, output)
    except SystemExit as err:
        return (
            f"Generation failed for target '{emit}': the planner exited early "
            f"(code {err.code})."
        )
    except planner_emit.PlannerEmitError as err:
        # Ours, and already phrased for a human.
        return f"Generation failed for target '{emit}': {err}"
    except KeyboardInterrupt:
        raise
    except BaseException as err:  # noqa: BLE001 -- see the docstring
        return f"Generation failed for target '{emit}': {type(err).__name__}: {err}"
    return None


def _resolve_engine(
    targets: tuple[str, ...], sdk_root: Path
) -> tuple[dict[str, str], str | None]:
    """`({mode: engine}, fallback_reason)` -- decided once, before any write.

    Up front rather than per target so `data.engine` describes the whole run and
    a half-in-process run cannot exist. `fallback_reason` is non-`None` only when
    the in-process path was WANTED and refused; a deliberate
    `TAN_GENERATE_EXECUTOR=subprocess` is a choice, not a fallback, and reports
    itself through `data.engine` alone.
    """
    requested = os.environ.get(EXECUTOR_ENV, "auto").strip().lower() or "auto"
    if requested not in EXECUTORS:
        raise GenerateError(
            "generate.invalid-executor",
            f"{EXECUTOR_ENV}='{requested}' is not one of {', '.join(EXECUTORS)}.",
            ExitCode.VALIDATION_FAILURE,
        )

    eligible = [mode for mode in targets if mode in planner_emit.IN_PROCESS_MODES]
    reason: str | None = None
    if requested == "subprocess":
        eligible = []
    elif eligible:
        reason = planner_emit.unavailable(sdk_root)
        if reason is not None:
            if requested == "in-process":
                raise GenerateError(
                    "generate.in-process-unavailable",
                    f"{EXECUTOR_ENV}=in-process was requested but the in-process "
                    f"planner cannot serve this run: {reason}",
                    ExitCode.RUNTIME_FAILURE,
                )
            eligible = []
    return (
        {
            mode: "in-process" if mode in eligible else "subprocess"
            for mode in targets
        },
        reason,
    )


def _resolve_board_path(board_yaml: str | None, workspace_root: Path) -> Path:
    """`--board-yaml` (absolute, or workspace-relative), else
    `<workspace_root>/board.yaml`."""
    if board_yaml:
        candidate = Path(board_yaml)
        return candidate if candidate.is_absolute() else workspace_root / candidate
    return workspace_root / "board.yaml"


def _resolve_sdk_root(
    sdk_root: str | None, workspace_root: Path
) -> tuple[Path, str, str, str | None] | None:
    """`(path, sourceTier, reported, brokenProjectPin)` -- or `None` when
    unresolved. `--sdk-root`
    is TERMINAL: an explicit path that is not an SDK checkout fails loudly here
    rather than silently falling through to discovery and generating against a
    different checkout than the caller named (I-31); its tier is always
    `sdkRootFlag`, matching the oracle's closed `SdkSourceTier`.

    Without `--sdk-root`: `.alp/sdk-path` project pin > the machine-global
    default (`~/.alp/sdk-default`) > the WIDE positional walk
    (`resolve_sdk_root_wide`, which reports its own tier) -- the wide ladder,
    not the narrow one thirteen other commands take, because the oracle's
    `generate` prefers the child `<ws>/alp-sdk` over a competing `../alp-sdk`
    (tan-cli#263). No `ALP_SDK_ROOT` tier (tried and reverted -- see
    `resolve_sdk_root_ladder`'s own docstring).

    `reported` is the string to put in the envelope's `sdk.root`: for
    `sdkRootFlag` it is the flag's raw text, NOT `str(path)` -- pathlib
    silently drops a trailing separator (`/srv/alp-sdk/` in, `/srv/alp-sdk`
    out), which the oracle does not do. Every other tier reports `str(path)`;
    those come from a pinned/discovered path, not user-typed argv, so there is
    no trailing-separator input to preserve.
    """
    if sdk_root:
        candidate = Path(sdk_root)
        if (candidate / "scripts" / "alp_project.py").is_file():
            return candidate, "sdkRootFlag", sdk_root, None
        return None
    found, tier, broken_pin = resolve_sdk_root_wide(None, workspace_root)
    return (found, tier, str(found), broken_pin) if found is not None else None


def _finish(
    *,
    json_mode: bool,
    verbose: bool,
    quiet: bool,
    project: Project,
    targets: tuple[str, ...],
    written: list[str],
    failed: list[str],
    issues: list[Issue],
    exit_code: ExitCode,
    engine: dict[str, str] | None = None,
    sdk: SdkInfo | None = None,
) -> None:
    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "targets": list(targets),
        "written": written,
        "failed": failed,
    }
    # Present only when there is a run to describe. Every refusal resolves zero
    # targets, and `contract/envelopes/generate-board-yaml-missing` freezes that
    # shape for the Rust and Python harnesses alike -- an empty `engine: {}`
    # there would be a contract break reporting nothing.
    if engine:
        data["engine"] = engine
    if json_mode:
        emit(Envelope("generate", project, data, issues, exit_code, sdk=sdk))
    else:
        # stdout is the envelope channel in both modes; human text never
        # touches it.
        #
        # No summary line on a refusal (`targets` empty): "wrote 0/0 targets"
        # above a refusal message reads like a successful no-op run.
        #
        # `--quiet` silences the summary only -- never the issues. A CMake
        # `execute_process` shows this stderr verbatim when the emit fails, and
        # a quiet flag that swallowed the reason would leave the caller with a
        # bare `rv=1` again, which is the exact defect `cmake/alp.cmake` was
        # written to fix.
        if targets and not quiet:
            tail = f"; failed: {', '.join(failed)}" if failed else " targets"
            print(
                f"generate: wrote {len(written)}/{len(targets)}{tail}",
                file=sys.stderr,
            )
        for issue in issues:
            print(f"generate: {issue.message}", file=sys.stderr)
        if verbose:
            for target in targets:
                where = (engine or {}).get(target)
                print(
                    f"target: {target}" + (f" ({where})" if where else ""),
                    file=sys.stderr,
                )
    raise typer.Exit(int(exit_code))


def generate(
    target: str = typer.Option(
        None,
        "--target",
        metavar="EMIT",
        help="Single generation target (e.g. zephyr-conf, dts-overlay, cmake-args).",
    ),
    all_targets: bool = typer.Option(
        False, "--all", help="Run every default generation target."
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="Core id: required by --target zephyr-board, optional scoping for some others.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Allow overwriting an existing native_sim overlay."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output: str = typer.Option(
        None,
        "--output",
        metavar="PATH",
        help="Destination for a single --target (default: the SDK's own layout "
             "under the project root).",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress the non-essential summary line."
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", hidden=True
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="List each target in text output."
    ),
) -> None:
    """Generate board-derived output files via the SDK's emitters."""
    # `generate` never prompts, so `--non-interactive` is accepted and ignored:
    # it exists because alp-sdk's `cmake/alp.cmake` passes it on every configure
    # (as it does to every `tan` invocation), and an unknown flag there is a
    # Click usage error that fails the whole CMake configure.
    del non_interactive
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    # The as-GIVEN strings, never the resolved absolute paths: the envelope
    # reflects back what the caller typed, which is what keeps the conformance
    # golden reproducible on any machine (`project.root == "."`,
    # `boardYaml == "board.yaml"`).
    #
    # tan-cli#236: NOT built via `Project.resolved` -- that would existence-check
    # this as-given string against the real cwd, which is wrong the moment
    # `--project` differs from it (the resolved `board_path` below, not this
    # string, is what must be checked). `reported` is corrected in place, right
    # below, at the one guard where the two can disagree.
    reported = Project(
        root=project if project else ".",
        board_yaml=board_yaml if board_yaml else "board.yaml",
    )

    def finish(**kwargs) -> None:
        _finish(
            json_mode=json_mode,
            verbose=verbose,
            quiet=quiet,
            project=reported,
            sdk=sdk_info,
            **kwargs,
        )

    def refuse(err: GenerateError) -> None:
        # No `engine`: a refusal resolved no targets, so there is no run to
        # describe (and the committed conformance golden pins that data shape).
        # `sdk` still reports (via `finish`'s default) once it is resolved --
        # only a refusal that precedes/IS the SDK resolution itself leaves it
        # unset, matching the oracle (`generate.board-yaml-missing` and
        # `generate.sdk-root-unresolved` both carry no `sdk`; every guard past
        # a successful resolution does).
        finish(
            targets=(),
            written=[],
            failed=[],
            issues=[Issue(err.code, "error", err.message)],
            exit_code=err.exit_code,
        )

    engine: dict[str, str] = {}
    sdk_info: SdkInfo | None = None
    try:
        workspace_root = Path(os.path.abspath(project)) if project else Path.cwd()
        board_path = _resolve_board_path(board_yaml, workspace_root)

        if not board_path.exists():
            # tan-cli#236: the refusal below says the file does not exist, so
            # the SAME envelope's `project.boardYaml` must not still name it
            # (Rust's own starkest instance of this bug -- see
            # `crates/tan-cli/src/commands/generate.rs`).
            reported = Project(root=reported.root, board_yaml=None)
            raise GenerateError(
                "generate.board-yaml-missing",
                "board.yaml path could not be resolved or the file does not exist.",
                ExitCode.VALIDATION_FAILURE,
            )

        resolved = _resolve_sdk_root(sdk_root, workspace_root)
        if resolved is None:
            raise GenerateError(
                "generate.sdk-root-unresolved",
                "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk "
                "switch <version|path>`, or place the project near an alp-sdk checkout.",
                ExitCode.VALIDATION_FAILURE,
            )
        resolved_sdk, sdk_tier, sdk_reported, sdk_broken_pin = resolved
        # Set only now: every guard from here on runs with a resolved SDK, so
        # the envelope carries `sdk` on both success and a later refusal
        # (`--target`/`--core` mistakes, an overlay guard, ...), matching the
        # oracle. The two guards above (`board.yaml` missing, this one) raise
        # before `sdk_info` exists, so their envelopes carry no `sdk` --
        # exactly the oracle's shape for both.
        sdk_info = SdkInfo(sdk_reported, sdk_tier)

        targets = resolve_targets(target, all_targets, core)

        # Which engine runs what -- before any guard that writes, so a bad
        # `TAN_GENERATE_EXECUTOR` refuses without leaving a directory behind.
        engine, fallback_reason = _resolve_engine(targets, resolved_sdk)

        # zephyr-board always resolves alone, so its directory name is derived
        # once here rather than inside the emit loop.
        board_dir_name: str | None = None
        if ZEPHYR_BOARD in targets:
            sku = _board_sku(board_path)
            board_dir_name = (
                zephyr_board_dir_name(sku, core) if sku and core else None
            )
            if board_dir_name is None:
                raise GenerateError(
                    "generate.board-sku-unresolved",
                    "board.yaml's som.sku is missing or is not an E1M-* SKU; "
                    "`--target zephyr-board` needs it to name the generated board "
                    "directory (alp-sdk's `alp_e1m_<sku-slug>_<core>` convention).",
                    ExitCode.VALIDATION_FAILURE,
                )

        output_override = (
            _resolve_output_override(output, targets, workspace_root)
            if output
            else None
        )

        if _overlay_would_overwrite(workspace_root, targets, force, output_override):
            raise GenerateError(
                "generate.would-overwrite",
                "boards/native_sim_native_64.overlay already exists. Use --force to "
                "overwrite.",
                ExitCode.WRITE_FAILURE,
            )

        # LAST of the guards, because it is the only one that WRITES: it creates
        # the parent directory (and, for `zephyr-board`, the output directory
        # itself). Running it earlier would leave strays behind for a run the
        # `som.sku` or overlay guard above was about to refuse.
        if output_override is not None:
            _ensure_writable(output_override, targets[0])

        # Only the spawned engine cares which interpreter PATH offers -- the
        # in-process one IS the interpreter. Probing regardless would refuse a
        # fully in-process run on a host whose `python` is old and unused, and
        # cost it a subprocess to find out.
        python = ""
        if "subprocess" in engine.values():
            python = _planner_python(
                str(workspace_root), str(resolved_sdk) if resolved_sdk else None
            )
            floor, _floor_source = resolve_manifest_python_floor(str(resolved_sdk))
            if (too_old := _python_too_old(python, floor)) is not None:
                raise GenerateError(
                    "generate.python-too-old", too_old, ExitCode.RUNTIME_FAILURE
                )

        script = resolved_sdk / "scripts" / "alp_project.py"
        written: list[str] = []
        failed: list[str] = []
        issues: list[Issue] = []
        for mode in targets:
            target_output = output_override or _output_path(
                workspace_root, mode, board_dir_name
            )
            if engine[mode] == "in-process":
                message = _emit_one_in_process(
                    resolved_sdk, board_path, mode, target_output, core
                )
            else:
                message = _emit_one(
                    python, script, board_path, mode, target_output, core
                )
            if message is None:
                written.append(_relative_or_full(workspace_root, target_output))
            else:
                failed.append(mode)
                issues.append(Issue("generate.emit-failed", "error", message))

        # APPENDED, after the per-target issues: what the user asked for comes
        # first and this is context about how it ran. Never omitted, though --
        # a fallback nobody can see is how the port got believed before it
        # happened.
        if fallback_reason is not None:
            issues.append(
                Issue(
                    "generate.in-process-unavailable",
                    "warning",
                    "generated by spawning the SDK's own emitter instead of the "
                    f"in-process planner: {fallback_reason}",
                )
            )
        pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier)
        if pin_issue is not None:
            issues.append(pin_issue)
    except GenerateError as err:
        refuse(err)
        return
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # `typer.Exit` cannot reach here: it is raised only from `_finish`,
        # which runs outside this try.
        refuse(
            GenerateError(
                "generate.internal-failure",
                f"generate failed unexpectedly: {err.__class__.__name__}: {err}",
                ExitCode.INTERNAL_FAILURE,
            )
        )
        return

    finish(
        targets=targets,
        written=written,
        failed=failed,
        issues=issues,
        engine=engine,
        exit_code=ExitCode.SUCCESS if not failed else ExitCode.WRITE_FAILURE,
    )
