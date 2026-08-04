# SPDX-License-Identifier: Apache-2.0
"""`tan kconfig` -- the board-scoped Kconfig symbol menu for one core (the
vscode `prj.conf` LSP's live feed).

Port of `crates/tan-cli/src/commands/kconfig.rs`. **Not a second Kconfig
path**: the actual solve is `tan.planner.kconfig_symbols.emit_kconfig`, the
SAME in-process renderer `tan.planner_root.emit("kconfig", ...)` already
serves for `west alp-emit kconfig` parity (`tan.planner.cli.emit_artefact`'s
`mode == "kconfig"` branch) -- this module is composition only: resolve the
project/SDK/core, call that ONE renderer, and fold its JSON into the standard
envelope. Reimplementing the Kconfig solve here would be exactly the drift
the oracle's own module doc warns against (two copies that can disagree).

**`--core` defaulting is CLI-side**, mirroring `tan_core::kconfig::
resolve_default_kconfig_core`: when omitted, this reads board.yaml's own
`cores:` block DIRECTLY (a lenient, un-validated read -- `preset:`
cross-file cores are not expanded, matching the oracle's `parse_board_model`,
which does the same single-file read) and picks the one `os: zephyr` core;
zero or more than one is a named-candidates error, never a guess.
`emit_kconfig` itself always requires an explicit core.

**ZEPHYR_BASE resolution** mirrors
`crates/tan-cli/src/commands/build/workspace.rs::resolve_zephyr_base` /
`west_workspace_dir`'s three tiers: (1) the project tree walked upward for a
`.west/` dir, (2) an already-exported `ZEPHYR_BASE` (the same check
`kconfig_symbols._require_workspace` makes -- kept as-is here rather than
re-derived with full manifest verification, since it already honours the
oracle's tier 2 in practice), (3) the SDK-derived layouts (`<sdk_root>/..`
and `<sdk_root>/../zephyrproject`). Tiers 1 and 3 close the gap that made
`tan kconfig` unable to succeed after an ordinary `tan bootstrap`, which
creates the workspace under the SDK's parent and does not export the
variable into the caller's shell. `build_cmd.py`'s own `_dispatch` still
carries a `NOT YET PORTED` note for the identical gap on the build path;
this module does not share code with it (no third file to touch for a
kconfig-scoped fix) but follows the exact same tier order so the two do not
drift in behaviour, only in whether they are wired up yet.

The resolved base is INJECTED into `os.environ["ZEPHYR_BASE"]` around the
`_plan_emit` call (temporarily, restored after) rather than relied on
ambiently -- `tan.planner.kconfig_symbols` reads `os.environ["ZEPHYR_BASE"]`
directly (it renders in-process, so there is no child env to set it on the
way the Rust oracle sets it on a spawned command), so a tree- or
SDK-derived workspace would otherwise never reach the planner at all.
"""

from __future__ import annotations

import json as _json
import os
import sys
from pathlib import Path

import typer
import yaml

from tan.commands.presets_cmd import resolve_project_paths, resolve_sdk
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.global_flags import accept_global_flags
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `tan.planner.kconfig_symbols.SCHEMA_VERSION`, verbatim -- not imported
#: directly: every `tan.planner.*` submodule import runs the package's own
#: `__init__.py`, which needs `tan.planner_root.bind_sdk_root` to have
#: already happened (its `paths.py` binds `REPO = sdk_root()` at module
#: scope) -- pulling the constant in at THIS module's import time, before
#: any SDK is resolved, would be exactly that ordering trap.
KCONFIG_SCHEMA_VERSION = 1


class _CoreResolutionError(Exception):
    """`--core` could not be resolved from board.yaml. Carries the envelope
    issue code and message the caller reports verbatim."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _yaml_type_name(value: object) -> str:
    """serde_yaml-flavoured type name for a board-yaml-invalid message
    (`invalid type: sequence, expected struct BoardModel`, verified against
    `target/debug/tan.exe`)."""
    if isinstance(value, list):
        return "sequence"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _resolve_core(core_arg: str | None, board_yaml: str) -> str:
    """The explicit `--core`, or the board's one declared `os: zephyr` core.

    Mirrors `tan_core::kconfig::resolve_core` (Rust `kconfig.rs`): a raw,
    single-file read of board.yaml's `cores:` block -- no schema validation,
    no preset expansion -- so this works even for a board.yaml the full
    planner would refuse for an unrelated reason, exactly like the oracle.
    """
    if core_arg:
        return core_arg
    try:
        text = Path(board_yaml).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        # tan-cli#396: `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`,
        # so an `except OSError` alone could not fire for a board.yaml saved in
        # cp1252/latin-1 (or with one stray byte pasted into a comment) -- it
        # escaped as an unhandled traceback, rc 1 with ZERO bytes on stdout.
        # That is the worst shape a machine interface has: the extension's two
        # string matches against the envelope both fail open, so the `prj.conf`
        # symbol menu renders empty or stale and never says why.
        #
        # Deliberately the SAME code and message an unreadable file gets,
        # because the oracle reaches this one arm for both: Rust's
        # `std::fs::read_to_string` returns an `io::Error` (kind `InvalidData`,
        # "stream did not contain valid UTF-8") for non-UTF-8 bytes, and
        # `crates/tan-cli/src/commands/kconfig.rs:156-161` maps every
        # `read_to_string` error to `kconfig.board-yaml-missing`. Not
        # `errors="replace"`: that would silently mangle the user's bytes and
        # then resolve a core out of the mangled result.
        raise _CoreResolutionError(
            "kconfig.board-yaml-missing",
            f"failed to read board.yaml at `{board_yaml}`: {err}",
        ) from err
    except UnicodeDecodeError as err:
        # tan-cli#396: `UnicodeDecodeError` is a `ValueError`, NOT an
        # `OSError`, so the clause above could never fire on it -- one
        # undecodable byte in the customer's own board.yaml (a cp1252/latin-1
        # editor on Windows, a stray byte pasted into a comment) escaped this
        # whole command as a traceback: rc 1, ZERO bytes on stdout, no
        # envelope. That is the worst possible shape for this particular
        # command, which exists to feed the alp-sdk-vscode `prj.conf` LSP
        # symbol menu: per `contract/README.md:33-37` the extension's two
        # string matches both fail OPEN on empty stdout, so it renders an
        # empty or stale menu and never tells the user why. `scaffold.py`
        # already learned this exact lesson (`except (OSError,
        # UnicodeDecodeError)`); this file had not.
        #
        # `board-yaml-invalid`, not `board-yaml-missing`: the file is there
        # and readable, so "missing" would send the customer looking for the
        # wrong problem. Same class -- and same remedy -- as the list-shaped
        # board.yaml below.
        raise _CoreResolutionError(
            "kconfig.board-yaml-invalid",
            f"failed to read board.yaml at `{board_yaml}`: not valid UTF-8: {err}",
        ) from err
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise _CoreResolutionError(
            "kconfig.board-yaml-invalid",
            f"failed to parse board.yaml at `{board_yaml}`: {err}",
        ) from err
    # `doc` not a mapping (a list-shaped board.yaml, e.g.) is a structural
    # parse failure, not "no cores declared" -- the oracle's `parse_board_model`
    # deserializes the WHOLE document into a typed struct, so a non-mapping
    # top level (or a `cores:` block that isn't itself a mapping) fails there,
    # never reaches core resolution at all. Falling through to the ambiguous
    # branch here would tell the customer to pass `--core` for a file that
    # will not parse, full stop -- same exit code, wrong issue code and wrong
    # remedy. `doc is None` (an empty/`null` file) is legitimate -- the oracle's
    # `Option<BoardModel>` parses that to the default model -- and keeps
    # falling through to "no cores declared" below.
    if doc is not None and not isinstance(doc, dict):
        raise _CoreResolutionError(
            "kconfig.board-yaml-invalid",
            f"failed to parse board.yaml at `{board_yaml}`: board.yaml is not valid "
            f"YAML: invalid type: {_yaml_type_name(doc)}, expected a mapping",
        )
    cores = doc.get("cores") if isinstance(doc, dict) else None
    if cores is not None and not isinstance(cores, dict):
        raise _CoreResolutionError(
            "kconfig.board-yaml-invalid",
            f"failed to parse board.yaml at `{board_yaml}`: board.yaml is not valid "
            f"YAML: cores: invalid type: {_yaml_type_name(cores)}, expected a map",
        )
    cores = cores or {}
    zephyr_ids = sorted(
        cid
        for cid, entry in cores.items()
        if isinstance(entry, dict) and entry.get("os") == "zephyr"
    )
    if len(zephyr_ids) == 1:
        return zephyr_ids[0]
    hint = (
        "board.yaml declares no cores"
        if not cores
        else f"declared cores: {', '.join(sorted(str(c) for c in cores))}"
    )
    raise _CoreResolutionError(
        "kconfig.core-ambiguous",
        f"--core <id> is required (board.yaml doesn't declare exactly one Zephyr "
        f"core); {hint}",
    )


def _resolve_zephyr_base(root: str, sdk_root: str | None) -> str | None:
    """The bootstrapped `ZEPHYR_BASE`, or `None`. Port of the oracle's
    `resolve_zephyr_base` / `west_workspace_dir` (see the module docstring):
    tries, IN ORDER, (1) the project tree walked upward for a `.west/` dir,
    (2) an already-exported `$ZEPHYR_BASE`, (3) the SDK-derived layouts. Per
    the oracle, once a tier finds a *workspace* that is the end of the
    search -- it does not fall through to a later tier just because that
    workspace turns out to hold no `zephyr/` checkout.
    """

    def workspace_zephyr(workspace: Path) -> str | None:
        zephyr = workspace / "zephyr"
        return str(zephyr) if zephyr.is_dir() else None

    # Tier 1: the project tree, walked upward.
    directory: Path | None = Path(root)
    while directory is not None:
        if (directory / ".west").is_dir():
            return workspace_zephyr(directory)
        parent = directory.parent
        directory = parent if parent != directory else None

    # Tier 2: an already-exported `$ZEPHYR_BASE`.
    raw = os.environ.get("ZEPHYR_BASE")
    if raw and Path(raw, "scripts", "kconfig", "kconfiglib.py").is_file():
        return raw

    # Tier 3: SDK-derived layouts.
    if sdk_root:
        parent = Path(sdk_root).parent
        for workspace in (parent, parent / "zephyrproject"):
            if (workspace / ".west").is_dir():
                return workspace_zephyr(workspace)
    return None


def _empty_data(core: str | None) -> dict:
    return {
        "schemaVersion": KCONFIG_SCHEMA_VERSION,
        "board": "",
        "core": core or "",
        "symbols": [],
    }


def _fail(
    *,
    root: str,
    board_path: str,
    exit_code: ExitCode,
    code: str,
    message: str,
    core: str | None,
    json_mode: bool,
    sdk: SdkInfo | None = None,
) -> None:
    if json_mode:
        emit(
            Envelope(
                "kconfig",
                Project.resolved(root, board_path),
                _empty_data(core),
                [Issue(code, "error", message)],
                exit_code,
                sdk=sdk,
            )
        )
    else:
        print(f"kconfig: {message}", file=sys.stderr)
    raise typer.Exit(int(exit_code))


class _KconfigShapeError(Exception):
    """A `--emit kconfig` document decoded as JSON but does not match
    `tan_core::KconfigData`/`KconfigSymbol` (`crates/tan-core/src/kconfig.rs`).

    The oracle's `parse_kconfig` deserializes straight into those typed
    structs, so a MISSING `board`/`core`/`symbols` key -- or a symbol missing
    `name`/`type`/`prompt`/`depends`/`default`/`help` -- is a `serde_json`
    deserialize error wrapped in the SAME `KconfigError::Json` variant a raw
    JSON syntax error gets (`kconfig.rs`'s `deserialize_present_option` doc
    comment: "a missing key is never legitimate input -- it means the SDK
    renamed the field"). This carries only the `serde`-shaped detail text;
    the caller wraps it in the identical `kconfig.parse-failed` code and
    `"kconfig emit is not valid JSON: {0}"` message prefix a syntax error
    gets, rather than inventing a second message shape the oracle has none
    of.
    """


def _validate_kconfig_data(data: dict) -> dict:
    """Structurally validate an already-JSON-decoded emit body and rebuild
    `data` from exactly the modelled fields (dropping any unmodelled key --
    the oracle's typed deserialize does the same), so `data` is exactly the
    shape `Envelope<KconfigData>` serializes. Raises `_KconfigShapeError` on
    the first missing/mistyped key, mirroring the oracle's required (never
    `#[serde(default)]`) `board`/`core`/`symbols`/per-symbol fields --
    `default` is the one field that is REQUIRED-but-nullable.
    """

    def require(mapping: dict, key: str) -> object:
        if key not in mapping:
            raise _KconfigShapeError(f"missing field `{key}`")
        return mapping[key]

    def require_str(mapping: dict, key: str) -> str:
        value = require(mapping, key)
        if not isinstance(value, str):
            raise _KconfigShapeError(
                f"invalid type: {_yaml_type_name(value)} for `{key}`, expected a string"
            )
        return value

    board = require_str(data, "board")
    core = require_str(data, "core")
    symbols = require(data, "symbols")
    if not isinstance(symbols, list):
        raise _KconfigShapeError(
            f"invalid type: {_yaml_type_name(symbols)} for `symbols`, expected a list"
        )

    rebuilt_symbols = []
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            raise _KconfigShapeError(
                f"invalid type: {_yaml_type_name(symbol)} for `symbols[{index}]`, "
                f"expected an object"
            )
        name = require_str(symbol, "name")
        sym_type = require_str(symbol, "type")
        prompt = require_str(symbol, "prompt")
        depends = require_str(symbol, "depends")
        default = require(symbol, "default")  # present, but nullable
        if default is not None and not isinstance(default, str):
            raise _KconfigShapeError(
                f"invalid type: {_yaml_type_name(default)} for `symbols[{index}].default`, "
                f"expected a string or null"
            )
        help_text = require_str(symbol, "help")
        rebuilt_symbols.append(
            {
                "name": name,
                "type": sym_type,
                "prompt": prompt,
                "depends": depends,
                "default": default,
                "help": help_text,
            }
        )

    return {
        "schemaVersion": data["schemaVersion"],
        "board": board,
        "core": core,
        "symbols": rebuilt_symbols,
    }


def _text_lines(data: dict, verbose: bool) -> list[str]:
    symbols = data.get("symbols") or []
    lines = [
        f"kconfig: {len(symbols)} symbol(s) for core '{data.get('core', '')}' "
        f"({data.get('board', '')})"
    ]
    if verbose:
        for sym in symbols:
            lines.append(f"  {sym.get('name')} [{sym.get('type')}] — {sym.get('prompt')}")
    return lines


def _run_kconfig(
    *,
    root: str,
    board_path: str,
    core: str | None,
    sdk_root: str | None,
    verbose: bool,
    json_mode: bool,
) -> None:
    """The whole setup-class ladder plus the emit, split out of `kconfig`
    below so that command can wrap it in ONE catch-all (tan-cli#396) without
    indenting 130 lines under a `try:`.

    Every failure exits through `_fail`, which raises `typer.Exit` after
    writing the envelope -- so this function's only control-flow exception is
    `typer.Exit`, which is exactly what the caller re-raises untouched.
    """
    # Setup-class check #1: no SDK checkout resolved -- checked before core
    # resolution so every setup-class failure here is uniformly one shape,
    # never a spawn attempt with half-resolved inputs (mirrors kconfig.rs).
    sdk = resolve_sdk(sdk_root, root)
    if sdk is None:
        # No `sdk=` here -- there is nothing resolved to report, matching the
        # oracle (which likewise omits the `sdk` envelope key on this one
        # failure; every OTHER `_fail` below runs after `sdk` resolved and
        # threads it through).
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.VALIDATION_FAILURE,
            code="kconfig.no-sdk-root",
            # `tan sdk switch` refuses in this build (tan-cli#305). Dropped
            # "run `tan bootstrap` first" too -- bootstrap resolves an SDK
            # through this exact same ladder, so with none resolved it
            # refuses right back with tan-cli#305's own fix text; it is not a
            # remedy for THIS failure, just a second site that needs one.
            message=f"no alp-sdk checkout found — pass `--sdk-root <PATH>`, or "
            f"{NO_SDK_NEXT_STEPS}.",
            core=None,
            json_mode=json_mode,
        )
        return
    sdk_info = SdkInfo(sdk[0], sdk[1])

    try:
        resolved_core = _resolve_core(core, board_path)
    except _CoreResolutionError as err:
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.VALIDATION_FAILURE,
            code=err.code,
            message=err.message,
            core=None,
            json_mode=json_mode,
            sdk=sdk_info,
        )
        return

    # Setup-class check #2: no bootstrapped Zephyr workspace (needs
    # ZEPHYR_BASE for the real Kconfig solver).
    zephyr_base = _resolve_zephyr_base(root, sdk[0])
    if zephyr_base is None:
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.VALIDATION_FAILURE,
            code="kconfig.no-workspace",
            message="no bootstrapped Zephyr workspace found for `--emit kconfig` "
            "(needs ZEPHYR_BASE) — run `tan bootstrap` first.",
            core=resolved_core,
            json_mode=json_mode,
            sdk=sdk_info,
        )
        return

    from tan.planner_root import emit as _plan_emit

    # Inject the RESOLVED base, not the ambient one -- `kconfig_symbols`
    # renders in-process and reads `os.environ["ZEPHYR_BASE"]` directly, so a
    # tree- or SDK-derived workspace (tiers 1/3 above) would never reach the
    # planner without this. Restored afterwards so this call has no lasting
    # effect on the process env (other commands, and other tests in the same
    # process, must not see it).
    prev_zephyr_base = os.environ.get("ZEPHYR_BASE")
    os.environ["ZEPHYR_BASE"] = zephyr_base
    try:
        try:
            stdout_text = _plan_emit(
                "kconfig", root=sdk[0], board_yaml=Path(board_path), core=resolved_core
            )
        except SystemExit as err:
            _fail(
                root=root,
                board_path=board_path,
                exit_code=ExitCode.RUNTIME_FAILURE,
                code="kconfig.emit-failed",
                message=f"the kconfig emit exited early (code {err.code}).",
                core=resolved_core,
                json_mode=json_mode,
                sdk=sdk_info,
            )
            return
        except Exception as err:  # noqa: BLE001 -- every planner failure is an
            # envelope, never a traceback (mirrors build_cmd._emit_plan's own
            # backstop); includes `OrchestratorError` for an unknown/non-Zephyr
            # `--core`.
            _fail(
                root=root,
                board_path=board_path,
                exit_code=ExitCode.RUNTIME_FAILURE,
                code="kconfig.emit-failed",
                message=f"the kconfig emit failed: {type(err).__name__}: {err}",
                core=resolved_core,
                json_mode=json_mode,
                sdk=sdk_info,
            )
            return
    finally:
        if prev_zephyr_base is None:
            os.environ.pop("ZEPHYR_BASE", None)
        else:
            os.environ["ZEPHYR_BASE"] = prev_zephyr_base

    try:
        data = _json.loads(stdout_text)
    except _json.JSONDecodeError as err:
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.RUNTIME_FAILURE,
            code="kconfig.parse-failed",
            message=f"kconfig emit is not valid JSON: {err}",
            core=resolved_core,
            json_mode=json_mode,
            sdk=sdk_info,
        )
        return
    if not isinstance(data, dict) or data.get("schemaVersion") != KCONFIG_SCHEMA_VERSION:
        found = data.get("schemaVersion") if isinstance(data, dict) else None
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.RUNTIME_FAILURE,
            code="kconfig.parse-failed",
            message=f"unsupported kconfig schemaVersion {found!r} (this CLI "
            f"consumes v{KCONFIG_SCHEMA_VERSION}); upgrade the CLI or the SDK so "
            f"the versions match",
            core=resolved_core,
            json_mode=json_mode,
            sdk=sdk_info,
        )
        return

    try:
        data = _validate_kconfig_data(data)
    except _KconfigShapeError as err:
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.RUNTIME_FAILURE,
            code="kconfig.parse-failed",
            message=f"kconfig emit is not valid JSON: {err}",
            core=resolved_core,
            json_mode=json_mode,
            sdk=sdk_info,
        )
        return

    if json_mode:
        emit(
            Envelope(
                "kconfig",
                Project.resolved(root, board_path),
                data,
                [],
                ExitCode.SUCCESS,
                sdk=sdk_info,
            )
        )
    else:
        for line in _text_lines(data, verbose):
            print(line, file=sys.stderr)
    raise typer.Exit(int(ExitCode.SUCCESS))


def kconfig(
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="Core id to scope the Kconfig symbol menu to (default: the board's "
        "one declared Zephyr core, when unambiguous).",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to current directory)."
    ),
    board_yaml: str = typer.Option(
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path (overrides project resolution).",
    ),
    sdk_root: str = typer.Option(None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."),
    verbose: bool = typer.Option(False, "--verbose", help="Emit additional diagnostic detail."),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Show the board-scoped Kconfig symbol menu for one core (the vscode
    `prj.conf` LSP's live feed). Needs a bootstrapped Zephyr workspace."""
    json_mode = output_format == "json"

    # The same backstop `presets_cmd.py` has had all along and this command
    # did not (tan-cli#396). Nothing enumerated reaches it -- every IO/parse
    # failure below is already coded -- and that is precisely the point: the
    # ONE nobody thought of must arrive as an envelope rather than as a
    # traceback on stderr with zero bytes on stdout, which the vscode
    # extension renders as an empty or stale `prj.conf` symbol menu with no
    # error of any kind (`contract/README.md:33-37`).
    #
    # `typer.Exit` is re-raised untouched: it is a `RuntimeError` subclass, so
    # a bare `except Exception` swallows it -- and every SUCCESSFUL run, plus
    # every already-coded refusal, leaves through exactly that exception.
    # Catching it here would have turned `ok: true` into an internal failure.
    #
    # The pre-resolution defaults mirror `presets_cmd`'s: `resolve_project_
    # paths` is itself inside the guard, so the failing envelope needs a
    # `project` block even when resolution is what blew up.
    root, board_path = ".", "./board.yaml"
    try:
        root, board_path = resolve_project_paths(project, board_yaml)
        _run_kconfig(
            root=root,
            board_path=board_path,
            core=core,
            sdk_root=sdk_root,
            verbose=verbose,
            json_mode=json_mode,
        )
    except typer.Exit:
        raise
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        _fail(
            root=root,
            board_path=board_path,
            exit_code=ExitCode.INTERNAL_FAILURE,
            code="kconfig.internal-failure",
            message=f"kconfig failed unexpectedly: {type(err).__name__}: {err}",
            core=core,
            json_mode=json_mode,
        )


# tan-cli#261: adds the six oracle `GlobalArgs` flags this command was still
# missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`) on top of `--verbose`, already declared and read above; see
# `tan.core.global_flags`.
kconfig = accept_global_flags(kconfig)
