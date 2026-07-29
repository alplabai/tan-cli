# SPDX-License-Identifier: Apache-2.0
"""`tan generate` -- run the SDK's board-derived emitters and report what landed.

**tan generates nothing itself.** Mirroring
`crates/tan-cli/src/commands/generate.rs`, every target is produced by spawning
the SDK's own planner front door once per emit mode:

    <python> <sdk>/scripts/alp_project.py --input <board.yaml> \
             --emit <mode> --output <path> [--core <id>]

So this module owns exactly three decisions and no more: WHICH modes to run,
WHERE each one's output goes, and how the outcome becomes an envelope. The
emitted bytes, the schemas behind them, and every hardware fact they encode
stay in alp-sdk (ADR-0017; I-26). There is no template here, no SKU list, no
address, no pin name -- `som.sku` is read from the customer's own `board.yaml`
purely to spell one directory name, exactly as the oracle does.

Shelling the SDK is the CONTRACT for this command, not a shortcut: I-31 pins
`scripts/alp_project.py` as tan's SDK-root marker and names `generate.rs:249`
as one of the six places that hardcode it. The invariant that forbids shelling
the SDK -- I-32, and anti-pattern 22 beside it -- is scoped to
`tan init`/`tan scaffold`, which read a VENDORED `--emit scaffold` tree so they
work with no checkout at all. `generate` has the opposite shape: without a
resolvable SDK checkout it refuses (`generate.sdk-root-unresolved`), because
there is nothing for it to delegate to.

**Every failure path is an envelope, never a traceback.** A missing or
unreadable `board.yaml`, an unresolved SDK, a bad `--target`/`--core` pairing, a
`native_sim` overlay that would be truncated, an interpreter too old to run the
SDK scripts, a spawn that dies or returns garbage -- each has a
`generate.<code>` issue and an exit code below, and the command carries a
catch-all backstop for anything unforeseen. The extension parses stdout whole:
a traceback there renders as nothing at all, with no error.

Exit codes, verbatim from the oracle: missing board / unresolved SDK / bad
target / unresolvable SKU are ValidationFailure (2); the overlay guard is
WriteFailure (3), as is any target whose emit failed; a too-old interpreter is
RuntimeFailure (1).
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
from tan.commands.build_cmd import _planner_python, discover_sdk_root
from tan.commands.doctor_cmd import probe
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

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
}

#: The default/`--all` target set. Derived, so it cannot disagree with the path
#: table above.
ALL_EMIT_MODES = tuple(_OUTPUT_RELATIVE_PATH)

#: The one target that is NOT defaultable: it hard-requires `--core` and writes
#: a DIRECTORY of files named per SKU+core, so a bare `tan generate` has no path
#: to give it. Reachable only via an explicit `--target zephyr-board --core <id>`.
ZEPHYR_BOARD = "zephyr-board"

#: Targets `--core` optionally SCOPES, beyond `zephyr-board` which requires it.
#: Verbatim from `alp_project.py`'s own `--core` help. `carrier-netlist` and
#: `native-sim-overlay` are excluded because the SDK never reads `--core` for
#: them at all, and `os-topology` because it accepts the flag and then prints
#: `--core is ignored for --emit os-topology (project-level emit)` and ignores
#: it -- the same "does nothing" shape, so all three are refused rather than
#: silently accepted.
CORE_SCOPABLE_TARGETS = (
    "zephyr-conf",
    "yocto-conf",
    "cmake-args",
    "dts-overlay",
    "west-libraries",
    "hw-info-h",
)

#: The floor the SDK's own scripts need -- they use `@dataclass(slots=True)`.
#: I-24: the same 3.10 that `metadata/bootstrap.json` and
#: `crate::util::MIN_PYTHON` declare, and the same number
#: `doctor_cmd.FALLBACK_PYTHON_FLOOR` carries -- bump one, grep for the other.
#: Kept separate rather than shared because they answer different questions:
#: that one is the floor assumed when no bootstrap manifest resolves, this one
#: is the floor the SPAWNED interpreter must clear. Refusing here turns the
#: SDK's cryptic `dataclass()` TypeError into something actionable.
MIN_PYTHON = (3, 10)

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


def _overlay_would_overwrite(
    workspace_root: Path, targets: tuple[str, ...], force: bool
) -> bool:
    """True when this run would truncate an existing hand-edited
    `boards/native_sim_native_64.overlay`.

    `native-sim-overlay` is the only target writing into the app's own source
    tree. Every other writer into a user tree (`init`, `scaffold`) diffs against
    disk and refuses without `--force`; a bare `tan generate` used to truncate a
    developer's tuned overlay with no check at all.
    """
    return (
        not force
        and "native-sim-overlay" in targets
        and _output_path(workspace_root, "native-sim-overlay", None).exists()
    )


def _python_too_old(python: str) -> str | None:
    """A message when `python` is below the SDK's floor, else `None`.

    `None` also covers "could not tell" -- a missing or broken interpreter is a
    different failure the real spawn surfaces on its own, and blocking on an
    unknown would refuse a perfectly good host. Bounded by `probe`'s timeout,
    which is why this reuses it rather than spawning by hand.
    """
    out = probe([python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
    if out is None:
        return None
    try:
        major, minor = (int(part) for part in out.strip().splitlines()[-1].split(".")[:2])
    except (IndexError, ValueError):
        return None
    if (major, minor) >= MIN_PYTHON:
        return None
    return (
        f"Python {major}.{minor} found at `{python}`, but alp-sdk requires Python "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+. Put a newer `python` first on PATH "
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


def _resolve_board_path(board_yaml: str | None, workspace_root: Path) -> Path:
    """`--board-yaml` (absolute, or workspace-relative), else
    `<workspace_root>/board.yaml`."""
    if board_yaml:
        candidate = Path(board_yaml)
        return candidate if candidate.is_absolute() else workspace_root / candidate
    return workspace_root / "board.yaml"


def _resolve_sdk_root(sdk_root: str | None, workspace_root: Path) -> Path | None:
    """`--sdk-root` is TERMINAL: an explicit path that is not an SDK checkout
    fails loudly here rather than silently falling through to discovery and
    generating against a different checkout than the caller named (I-31)."""
    if sdk_root:
        candidate = Path(sdk_root)
        return candidate if (candidate / "scripts" / "alp_project.py").is_file() else None
    return discover_sdk_root(workspace_root)


def _finish(
    *,
    json_mode: bool,
    verbose: bool,
    project: Project,
    targets: tuple[str, ...],
    written: list[str],
    failed: list[str],
    issues: list[Issue],
    exit_code: ExitCode,
) -> None:
    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "targets": list(targets),
        "written": written,
        "failed": failed,
    }
    if json_mode:
        emit(Envelope("generate", project, data, issues, exit_code))
    else:
        # stdout is the envelope channel in both modes; human text never
        # touches it.
        #
        # No summary line on a refusal (`targets` empty): "wrote 0/0 targets"
        # above a refusal message reads like a successful no-op run.
        if targets:
            tail = f"; failed: {', '.join(failed)}" if failed else " targets"
            print(
                f"generate: wrote {len(written)}/{len(targets)}{tail}",
                file=sys.stderr,
            )
        for issue in issues:
            print(f"generate: {issue.message}", file=sys.stderr)
        if verbose:
            for target in targets:
                print(f"target: {target}", file=sys.stderr)
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
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="List each target in text output."
    ),
) -> None:
    """Generate board-derived output files via the SDK's emitters."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    # The as-GIVEN strings, never the resolved absolute paths: the envelope
    # reflects back what the caller typed, which is what keeps the conformance
    # golden reproducible on any machine (`project.root == "."`,
    # `boardYaml == "board.yaml"`).
    reported = Project(
        root=project if project else ".",
        board_yaml=board_yaml if board_yaml else "board.yaml",
    )

    def finish(**kwargs) -> None:
        _finish(json_mode=json_mode, verbose=verbose, project=reported, **kwargs)

    def refuse(err: GenerateError) -> None:
        finish(
            targets=(),
            written=[],
            failed=[],
            issues=[Issue(err.code, "error", err.message)],
            exit_code=err.exit_code,
        )

    try:
        workspace_root = Path(os.path.abspath(project)) if project else Path.cwd()
        board_path = _resolve_board_path(board_yaml, workspace_root)

        if not board_path.exists():
            raise GenerateError(
                "generate.board-yaml-missing",
                "board.yaml path could not be resolved or the file does not exist.",
                ExitCode.VALIDATION_FAILURE,
            )

        resolved_sdk = _resolve_sdk_root(sdk_root, workspace_root)
        if resolved_sdk is None:
            raise GenerateError(
                "generate.sdk-root-unresolved",
                "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk "
                "switch <version|path>`, or place the project near an alp-sdk checkout.",
                ExitCode.VALIDATION_FAILURE,
            )

        targets = resolve_targets(target, all_targets, core)

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

        if _overlay_would_overwrite(workspace_root, targets, force):
            raise GenerateError(
                "generate.would-overwrite",
                "boards/native_sim_native_64.overlay already exists. Use --force to "
                "overwrite.",
                ExitCode.WRITE_FAILURE,
            )

        python = _planner_python()
        if (too_old := _python_too_old(python)) is not None:
            raise GenerateError(
                "generate.python-too-old", too_old, ExitCode.RUNTIME_FAILURE
            )

        script = resolved_sdk / "scripts" / "alp_project.py"
        written: list[str] = []
        failed: list[str] = []
        issues: list[Issue] = []
        for mode in targets:
            output = _output_path(workspace_root, mode, board_dir_name)
            message = _emit_one(python, script, board_path, mode, output, core)
            if message is None:
                written.append(_relative_or_full(workspace_root, output))
            else:
                failed.append(mode)
                issues.append(Issue("generate.emit-failed", "error", message))
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
        exit_code=ExitCode.SUCCESS if not failed else ExitCode.WRITE_FAILURE,
    )
