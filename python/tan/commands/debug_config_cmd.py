# SPDX-License-Identifier: Apache-2.0
"""`tan debug-config` -- generate (or preview) a VS Code launch.json entry.

Port of `crates/tan-cli/src/commands/debug_config.rs`. Build a launch draft for
the target class + server, resolve what this project's own build already knows
(#66), then either preview it (`--preview`) or merge it into
`<workspace>/.vscode/launch.json`. Invalid kind / unsupported backend /
malformed existing file -> exit 5; a failed write -> exit 3.

**The debug profile follows from the target CLASS; the customer never selects an
OS or a backend.** `--target-kind` names one of four classes, each of which
implies its adapter (cortex-debug / cppdbg / lldb), its artefact shape and its
legal server set. There is no `--os` and no `--backend`, and nothing here knows
a SKU, a device address, an I2C address or a pin name -- the hardware facts live
in alp-sdk `metadata/`, and the ones a launch config needs arrive through this
project's OWN build output (`build/system-manifest.yaml` +
`<build_dir>/zephyr/runners.yaml`, both written beside the build).

**Nothing here shells the SDK.** Every input is either an argument or a file
this project's build already wrote under the workspace; the alp-sdk checkout is
never invoked, never probed for a loader script by this command's own logic, and
no `sdk` envelope key is emitted (tan-cli#111 follow-up -- see
`_resolve_project_reporting_fields`). That is the invariant the port spec records
as I-32 and its anti-pattern #22: giving a command an alp-sdk-checkout
dependency it deliberately does not have is a silent regression that no gate
catches. `debug-config` needs nothing from the SDK, so it asks it nothing.

Every failure path emits a coded envelope. A raw traceback on stdout is
indistinguishable, to the extension, from tan producing nothing at all -- it
renders an empty panel with no error -- so the outer guard in [`debug_config`]
converts any unexpected exception into `debug-config.internal-failure` at exit
5 rather than letting it escape.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.core.debug_launch import (
    BAREMETAL_MCU,
    JLINK,
    NATIVE_HOST,
    OPENOCD,
    PYOCD,
    SERVER_NONE,
    YOCTO_USERSPACE,
    ZEPHYR_MCU,
    DebugConfigError,
    LaunchResolution,
    apply_launch_resolution,
    create_launch_draft,
    create_launch_json_write_plan,
    is_unresolved_placeholder,
    launch_preview_document,
    launch_preview_notes,
    parse_server_kind,
    parse_target_kind,
)
from tan.core.jsonc_splice import pretty_json
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The manifest `os` a debug target class runs on, or absent for a target with
#: no per-core build slice keyed by `os`. `native-host` is exactly that case --
#: its slice is selected by BOARD target instead, in [`_select_slice`].
_MANIFEST_OS = {
    ZEPHYR_MCU: "zephyr",
    BAREMETAL_MCU: "baremetal",
    YOCTO_USERSPACE: "yocto",
}

#: The `runners.yaml` runner id a debug server reads its arguments from.
#: `gdbserver`/`none` have no runner: neither is a Zephyr probe runner.
_RUNNER_ID = {JLINK: "jlink", OPENOCD: "openocd", PYOCD: "pyocd"}

#: The Zephyr board target naming the host simulator, and the runnable that
#: sits beside its `zephyr.elf`. Verbatim from `tan_core::run`.
_NATIVE_SIM_BOARD = "native_sim"
_NATIVE_SIM_EXE = "zephyr.exe"

#: The system-manifest schema major this command consumes. A different value is
#: ignored (nothing resolves) rather than silently mis-applied.
_SYSTEM_MANIFEST_SCHEMA_VERSION = 1


@dataclass
class _Outcome:
    """What one run produced: the envelope pieces plus the human lines. Built and
    returned, never emitted in place, so the exception guard in [`debug_config`]
    can wrap the whole computation without also catching `typer.Exit` (which is a
    `RuntimeError` subclass, not a `SystemExit`, and would otherwise be swallowed
    by a bare `except Exception`)."""

    exit_code: ExitCode
    data: dict[str, Any]
    project: Project
    issues: list[Issue]
    text: list[str]


def _generated_at() -> str:
    """`SOURCE_DATE_EPOCH` when set, else now -- mirrors
    `crate::util::generated_at_iso` + `tan_core::clock::format_iso8601_utc`.

    Millisecond precision with a `Z` suffix, matching JavaScript's
    `toISOString()`, because the envelopes are byte-compared against the TS
    CLI's `generatedAt` (and against the committed goldens, which spell
    `1970-01-01T00:00:00.000Z`). A whole-second format would fail all four
    `debug-config` fixtures.
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    seconds = time.time()
    if raw is not None:
        try:
            seconds = float(int(raw.strip()))
        except ValueError:
            pass
    # NEVER raise. This helper is also called from the recovery path of the
    # exception guard in `_emit_outcome`, so a throw here DOUBLE-FAULTS: the
    # first failure is caught, the recovery re-raises, and the process dies with
    # a raw traceback and EMPTY stdout -- precisely the break that guard exists
    # to prevent, and the only path in the port that could still do it.
    #
    # The realistic trigger is `SOURCE_DATE_EPOCH` in MILLISECONDS
    # (1700000000000 -> year 55838), and CI / reproducible-build environments are
    # exactly what set this variable. `time.gmtime` raises OverflowError or
    # OSError (Errno 22 on Windows) once past the platform's `time_t` range, and
    # that range differs per platform, so the value cannot be portably
    # pre-validated -- catch rather than predict. Rust does not fail here either:
    # `crates/tan-cli/src/util.rs` parses and falls back to the clock.
    for candidate in (seconds, time.time()):
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(candidate)))
        except (OverflowError, OSError, ValueError):
            continue
        millis = int((candidate - int(candidate)) * 1000)
        return f"{stamp}.{millis:03d}Z"
    # Supplied stamp AND wall clock both unusable: still no throw.
    return "1970-01-01T00:00:00.000Z"


def _normalise(path: str) -> str:
    """Rust's `normalize_path(cwd.join(p))`: cwd-anchored and lexically
    normalised, in the platform's OWN separators (this value becomes
    `data.launchJsonPath`, which the contract harness normalises itself).

    `os.path.abspath`, not `Path.resolve()`: abspath is purely lexical, so a
    project reached through a symlink keeps the name the user typed and a path
    that does not exist yet still resolves -- `resolve()` would rewrite both.
    """
    return os.path.abspath(path)


def _to_posix(path: str) -> str:
    """Forward slashes always. Mirrors `tan_core::project::to_posix`: every field
    in the extension/CLI handshake must be platform-identical, and #170's own
    follow-up was `project.root` and `project.boardYaml` shipping DIFFERENT
    separators inside the same object on Windows."""
    return path.replace("\\", "/")


def _resolve_project_reporting_fields(
    project_arg: str, board_yaml_arg: str | None
) -> tuple[str, str]:
    """`(project.root, project.boardYaml)`, both posix -- the two fields this
    command REPORTS.

    tan-cli#170: `debug-config` used to hardcode `board_yaml: None` on every
    path, even a success with a valid `board.yaml` sitting in the resolved root,
    while every other command took both from the shared resolver. Bound together
    here so they cannot disagree on separator style.

    Deliberately the `_no_sdk_report` half of the Rust resolver: the SDK-root
    resolution the shared helper also performs is skipped outright rather than
    performed-and-discarded. `debug-config` never drives an SDK, so resolving one
    could only add an undeclared `sdk` envelope key as a side effect of a field
    it merely reports (tan-cli#111 follow-up) -- and skipping it is also what
    keeps this command free of any SDK-checkout dependency (I-32).

    `board.yaml`'s existence is NOT checked, matching
    `project.rs::resolve_board_yaml_path`, which joins the configured relative
    path onto the workspace root unconditionally. That is why the four goldens
    report `__WORKDIR__/board.yaml` from a scratch directory holding no
    `board.yaml` at all: the field names where one WOULD live.
    """
    workspace_root = _normalise(project_arg)
    configured = board_yaml_arg or "board.yaml"
    board_yaml = (
        configured
        if os.path.isabs(configured)
        else os.path.join(workspace_root, configured)
    )
    return _to_posix(workspace_root), _to_posix(board_yaml)


def _workspace_relative(workspace_root: str, path: str) -> str:
    """Rewrite a path under `workspace_root` as `${workspaceFolder}/<rel>`, so a
    committed launch.json stays portable; an artefact outside the project (an
    out-of-tree build root) is left absolute rather than mangled.

    The tail is SLICED out of `path`'s own text, never re-rendered through
    `Path`. Rust's `strip_prefix` hands back a borrowed subslice, so the
    separators the manifest itself used survive; `Path.relative_to(...)` builds a
    fresh object that re-renders with the platform separator, which on Windows
    turns a `/`-authored manifest path into `${workspaceFolder}/build\\...`. That
    string is handed to a debug adapter verbatim, so the difference is visible.

    Rust's `strip_prefix` folds case on the Windows drive PREFIX only (`c:` ==
    `C:`) and compares every other component exactly; mirrored here. In practice
    both values come from the same `abspath`, so it only matters against a
    hand-written manifest.
    """
    root = workspace_root.replace("\\", "/").rstrip("/")
    probe = path.replace("\\", "/")
    if os.name == "nt":
        root_drive, root_rest = os.path.splitdrive(root)
        probe_drive, probe_rest = os.path.splitdrive(probe)
        matched = root_drive.lower() == probe_drive.lower() and probe_rest.startswith(
            f"{root_rest}/"
        )
    else:
        matched = probe.startswith(f"{root}/")
    if not matched:
        return path
    return "${workspaceFolder}/" + path[len(root) + 1 :]


def _load_yaml(path: Path) -> Any | None:
    """Parse a YAML file this project's build wrote, or `None`.

    `None` for every failure -- missing file, unreadable, non-UTF-8, malformed
    YAML, or PyYAML not installed. Both callers are best-effort by contract (see
    [`_resolve_from_build`]): the field simply stays unresolved and the draft
    keeps the placeholder it has always had.

    tan ships no YAML dependency of its own, so PyYAML is used when importable
    -- it always is in a Zephyr workspace, which is the only place a
    `system-manifest.yaml` can exist -- and its absence degrades to "nothing
    resolved" rather than failing a command whose whole point is to work before
    the first build.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- Zephyr's/the SDK's output, not ours
        return None


def _slices(manifest: Any) -> list[dict[str, Any]]:
    """The manifest's slices, or `[]` when the document is not a v1 manifest.

    The schema-major guard mirrors `parse_system_manifest`, which REFUSES a
    manifest whose `schema_version` is not 1 rather than reading it as if it
    were. A slice missing `core_id` or `os` also disqualifies the whole document:
    both fields are non-`Option` in the Rust struct, so serde fails the entire
    parse, and a partial read here would resolve against a manifest the oracle
    rejects.
    """
    if not isinstance(manifest, dict):
        return []
    if manifest.get("schema_version") != _SYSTEM_MANIFEST_SCHEMA_VERSION:
        return []
    raw = manifest.get("slices")
    if not isinstance(raw, list):
        return []
    slices = []
    for entry in raw:
        if not isinstance(entry, dict):
            return []
        if not isinstance(entry.get("core_id"), str) or not isinstance(
            entry.get("os"), str
        ):
            return []
        slices.append(entry)
    return slices


def _is_native_sim_board(board: Any) -> bool:
    """True for the bare `native_sim` board AND Zephyr's qualified board form
    (`native_sim/native/64`). Exact-matching only the bare name let a qualified
    board name defeat the host-vs-hardware discriminator on a perfectly fresh
    manifest; `"native_simulated_foo"` deliberately does NOT match -- the
    required `/` anchors it to Zephyr's actual board-qualifier syntax."""
    return isinstance(board, str) and (
        board == _NATIVE_SIM_BOARD or board.startswith(f"{_NATIVE_SIM_BOARD}/")
    )


def _native_sim_exe_beside(elf: str) -> str:
    """Swap a native_sim slice's `output_artefact` for the runnable `zephyr.exe`
    beside it.

    A manifest NEVER records the `.exe`: `resolve_zephyr_artefact` is tan's only
    writer of `output_artefact` and stores `<slice-cwd>/build/zephyr/zephyr.elf`
    unconditionally for every zephyr slice, native_sim included, and alp-sdk
    (planner-only) never writes the field at all. So every consumer wanting the
    host runnable must make this swap -- #83 took the artefact verbatim and
    pointed `Alp: Native Sim Debug` at an ELF CodeLLDB cannot launch.

    Splits on the separator the path itself uses rather than going through
    `Path.with_name`, which on Windows rejoins with `\\` and would turn a
    `/`-authored manifest path into a mixed `a/b\\zephyr.exe` -- visible, since
    this value ships to a debug adapter verbatim.
    """
    cut = max(elf.rfind("/"), elf.rfind("\\"))
    return _NATIVE_SIM_EXE if cut < 0 else elf[: cut + 1] + _NATIVE_SIM_EXE


def _select_slice(
    slices: list[dict[str, Any]], target: str, core: str | None
) -> dict[str, Any] | None:
    """The manifest slice a debug draft resolves against, for a target/`--core`.

    `native-host` is a special case: its runnable artefact is the project's
    `native_sim` slice, found by BOARD target -- the same discriminator `tan run`
    uses to pick the host binary -- not by `os`. A board that also builds a real
    Zephyr MCU slice still has one or more slices with `os: zephyr`; the old
    `os`-keyed match took the first of those, which on such a board is often the
    MCU slice, pointing `Alp: Native Sim Debug` at a Cortex-M ELF that CodeLLDB
    then cannot launch. `--core` is intentionally unused on this arm: a
    `native_sim` slice's `core_id` is not a hardware core selector.
    """
    if target == NATIVE_HOST:
        return next(
            (s for s in slices if _is_native_sim_board(s.get("board"))),
            None,
        )
    manifest_os = _MANIFEST_OS.get(target)
    if manifest_os is None:
        return None
    # `--core` names the slice outright; otherwise the first slice of this
    # target's OS wins, which is the whole manifest on a single-core project.
    return next(
        (
            s
            for s in slices
            if s.get("os") == manifest_os and (core is None or s.get("core_id") == core)
        ),
        None,
    )


def _runner_arg_values(argv: Any, flag: str) -> list[str]:
    """Every value a runner's argv gives for `flag`, in either form west emits:
    `--device=Cortex-M55` (inline) or `--config <path>` (separate token).

    A list, not a single value: OpenOCD takes more than one `--config` and
    cortex-debug's `configFiles` is an array -- collapsing to the first would
    silently drop a board's second config file and produce a session that
    attaches to half a target.
    """
    if not isinstance(argv, list):
        return []
    tokens = [a for a in argv if isinstance(a, str)]
    inline = f"{flag}="
    values: list[str] = []
    i = 0
    while i < len(tokens):
        arg = tokens[i]
        if arg.startswith(inline):
            value = arg[len(inline) :]
            if value:
                values.append(value)
        elif arg == flag:
            # The next token is the value only when it is not another flag -- a
            # trailing `--config` with nothing after it must not swallow the
            # following option as if it were a path.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                values.append(tokens[i + 1])
                i += 1
        i += 1
    return values


def _str_or_none(value: Any) -> str | None:
    """A non-empty string, else `None`. An empty string in Zephyr's own output
    means "absent", not a path (`runners.rs::non_empty`)."""
    return value if isinstance(value, str) and value != "" else None


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _resolve_from_build(
    workspace_root: str, target: str, server: str, core: str | None
) -> tuple[LaunchResolution, list[str]]:
    """Everything this project's own build knows about how to debug it: the
    per-core ELF from `build/system-manifest.yaml`, and the probe/tool paths from
    that slice's `runners.yaml` -- the same file `west flash` reads.

    Best-effort throughout. A missing manifest (pre-build), a missing slice, an
    unreadable or reshaped `runners.yaml` each leave the corresponding field
    unresolved instead of failing the command: `debug-config` must still emit its
    draft before the first build. Returns the resolution plus the runner ids the
    board registered, for the "this build registers no such runner" note.
    """
    resolution = LaunchResolution()
    manifest = _load_yaml(Path(workspace_root, "build", "system-manifest.yaml"))
    slice_ = _select_slice(_slices(manifest), target, core)
    if slice_ is None:
        return resolution, []

    artefact = _str_or_none(slice_.get("output_artefact"))
    if artefact is not None:
        # A host target needs the sibling swap; every other target kind
        # genuinely wants the artefact verbatim.
        if target == NATIVE_HOST:
            artefact = _native_sim_exe_beside(artefact)
        resolution.executable = _workspace_relative(workspace_root, artefact)

    build_dir = _str_or_none(slice_.get("build_dir"))
    if build_dir is None:
        return resolution, []
    runners = _load_yaml(Path(build_dir, "zephyr", "runners.yaml"))
    if not isinstance(runners, dict):
        return resolution, []
    config = runners.get("config")
    config = config if isinstance(config, dict) else {}
    args = runners.get("args")
    args = args if isinstance(args, dict) else {}

    resolution.gdb_path = _str_or_none(config.get("gdb"))
    runner = _RUNNER_ID.get(server)
    if runner is not None:
        if server == JLINK:
            values = _runner_arg_values(args.get(runner), "--device")
            resolution.device = values[0] if values else None
        elif server == OPENOCD:
            resolution.server_path = _str_or_none(config.get("openocd"))
            resolution.search_dirs = _str_list(config.get("openocd_search"))
            resolution.config_files = _runner_arg_values(args.get(runner), "--config")
        elif server == PYOCD:
            values = _runner_arg_values(args.get(runner), "--target")
            resolution.target_id = values[0] if values else None
    return resolution, _str_list(runners.get("runners"))


def _resolve_user_svd(workspace_root: str, arg: str) -> str:
    """Resolve `--svd` into the value the launch configuration should carry.

    **Anchor: the current directory, not the project root.** `--svd` is a
    per-invocation flag typed at a shell prompt, so a relative path means what
    the shell means by it. (A board-level `debug.svd` key, should one ever be
    added, travels with the project and must anchor on the project root instead
    -- the two have different lifetimes, so they get different anchors
    deliberately rather than by omission.) The emitted string then goes through
    the same [`_workspace_relative`] rewrite as `executable`: inside the project
    it becomes `${workspaceFolder}/...` so a committed launch.json stays
    portable, outside it stays absolute -- the normal case, since a vendor SVD
    lives in the vendor SDK the user installed.

    **A bad path is a HARD ERROR, never a silent drop back to "no SVD".**
    tan-cli#67 established that cortex-debug fails the whole session on an
    `svdFile` it cannot read, which is why the *unresolved* case drops the key.
    But the user explicitly named this file: falling back would make a typo
    indistinguishable from not passing the flag, and the failure would surface as
    an unexplained empty peripheral view.
    """
    if arg.strip() == "":
        raise DebugConfigError("Alp: --svd was given an empty path.")
    # `abspath` on an absolute `arg` returns it normalised, so this handles both.
    candidate = _normalise(arg)
    # `stat` then S_ISREG, mirroring the Rust's `metadata()`-then-`is_file()`
    # pair: the two failures get DIFFERENT messages, and a bare `is_file()`
    # (false for both a missing path and a directory) could not tell them apart.
    try:
        mode = os.stat(candidate).st_mode
    except OSError as err:
        raise DebugConfigError(
            f"Alp: --svd path cannot be read: {candidate} ({err}). "
            "Pass the path to the vendor's own .svd file; the SDK ships none "
            "(alp-sdk#948)."
        ) from err
    if not stat.S_ISREG(mode):
        raise DebugConfigError(f"Alp: --svd path is not a file: {candidate}")
    return _workspace_relative(workspace_root, candidate)


def _has_placeholder(value: Any) -> bool:
    """Whether any `<...>` placeholder survived resolution, anywhere in the draft
    -- including inside `configFiles`, which is an array.

    The string test is [`is_unresolved_placeholder`], the SAME predicate the
    launch.json merge uses, so "keep the still-needs-resolution note" and "do not
    overwrite this hand-filled value" can never disagree. It used to be
    `contains("<resolved-")`, which called the two-token `<host>:<port>` a real
    address: a yocto config whose `<resolved-gdb>` resolved then dropped the note
    while `miDebuggerServerAddress` was still unusable.
    """
    if isinstance(value, str):
        return is_unresolved_placeholder(value)
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    return False


def _preview_notes_for(
    draft: dict[str, Any], registered_runners: list[str], server: str
) -> list[str]:
    """The preview notes, minus the "still needs resolution" warning once nothing
    is left to resolve. Keyed off the FINAL draft rather than off "did anything
    resolve": a partly-resolved config (a board that registers no OpenOCD runner
    still has `<resolved-openocd-board-cfg>`) must keep the warning, and a fully
    resolved one must lose it -- otherwise the note is noise on configs that are
    fine and silence on configs that are not.
    """
    notes = [
        n
        for n in launch_preview_notes()
        if not n.startswith("Placeholder fields") or _has_placeholder(draft)
    ]
    # The most common reason a placeholder survives: the board never registered
    # this server. Say so, instead of leaving the user to wonder which
    # project-specific value they are supposed to invent.
    runner = _RUNNER_ID.get(server)
    if runner is not None and registered_runners and runner not in registered_runners:
        # `{:?}` on a Vec<String> renders `["jlink", "openocd"]` -- reproduced
        # exactly, because this string is the note a customer reads and diffs
        # against the Rust binary's own output.
        rendered = "[" + ", ".join(f'"{r}"' for r in registered_runners) + "]"
        notes.append(
            f"This build registers no '{runner}' runner (runners.yaml: {rendered}), "
            "so its fields could not be resolved."
        )
    return notes


def _migrated_issue(from_name: str, to_name: str) -> Issue:
    """The #133 migration report: a pre-#155 `"ALP: ..."` entry was found in
    place of the current `"Alp: ..."` name and adopted onto it. Severity `info`,
    not `warning` or `error` -- nothing failed and no action is required; this
    exists so a consumer (or a customer reading `--format json`) can tell WHY the
    file changed under them instead of only by diffing it."""
    return Issue(
        "debug-config.legacy-entry-migrated",
        "info",
        f'Migrated the legacy launch-configuration entry "{from_name}" into '
        f'"{to_name}". Any value you had hand-filled in on the old entry for an '
        "unresolved-placeholder field (device, miDebuggerServerAddress, "
        "configFiles, …) carried across; every other field tan owns was "
        "refreshed to this run's values, same as an ordinary re-run. The old "
        "entry is gone.",
    )


def _legacy_untouched_issue(legacy_name: str) -> Issue:
    """tan-cli#179: the ORDINARY same-name merge ran and a legacy `"ALP: ..."`
    counterpart of the SAME draft ALSO still sits in the file. Distinct from
    [`_migrated_issue`], which fires on the MISS path where the legacy entry is
    the one adopted -- here NEITHER entry was touched beyond the ordinary merge,
    so the customer's real hand-filled values may still be stranded on the
    leftover entry with nothing pointing at it."""
    return Issue(
        "debug-config.legacy-entry-untouched",
        "info",
        f'A leftover legacy launch-configuration entry "{legacy_name}" still '
        "sits in .vscode/launch.json alongside the entry this run updated. It "
        "was left untouched — nothing decides which of the two you may have "
        "hand-edited is authoritative — so if you filled in real values on "
        "the legacy entry, copy them onto the maintained one and remove the "
        "legacy entry yourself.",
    )


def _comments_dropped_issue() -> Issue:
    """tan-cli#182 review finding #2: this write dropped a comment (or trailing
    comma) sitting inside a span it rewrote. Severity `info` -- nothing failed
    and there is no action to take, but a tool that discarded user-authored
    content must never report unqualified success (#182's own floor)."""
    return Issue(
        "debug-config.comments-dropped",
        "info",
        "This write dropped a comment (or trailing comma) that sat inside the "
        "part of .vscode/launch.json it rewrote — either inside the one entry "
        "being updated, or, if the file's shape couldn't be confidently "
        "spliced, anywhere in the file. Everything outside that span is "
        "untouched.",
    )


def _data(
    *,
    generated_at: str,
    target: str,
    server: str,
    preview: bool,
    launch_json_path: str,
    replaced: bool,
    notes: list[str],
    configuration: Any,
) -> dict[str, Any]:
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "targetKind": target,
        "server": server,
        "preview": preview,
        "launchJsonPath": launch_json_path,
        "replaced": replaced,
        "notes": notes,
        # The launch configuration itself -- the very thing the command
        # produces. Additive: the envelope used to describe the write (path,
        # replaced, notes) without carrying the object, so an automated consumer
        # had to re-read launch.json or scrape the text preview to see what was
        # generated (alp-sdk-vscode#339).
        "configuration": configuration,
    }


def _failure(
    *,
    generated_at: str,
    target: str,
    server: str,
    launch_json_path: str,
    exit_code: ExitCode,
    code: str,
    message: str,
    text_lines: list[str],
) -> _Outcome:
    """The shared failure outcome: one `error` issue, a `configuration: null`
    payload, and a NULL project (TS `createFailureResult` reports no project)."""
    return _Outcome(
        exit_code=exit_code,
        data=_data(
            generated_at=generated_at,
            target=target,
            server=server,
            preview=False,
            launch_json_path=launch_json_path,
            replaced=False,
            notes=[],
            # No draft exists on this path -- the failure happened before (or
            # instead of) generating one. `null`, not an empty object, so a
            # consumer cannot mistake it for a configuration with no fields.
            configuration=None,
        ),
        project=Project(root=None, board_yaml=None),
        issues=[Issue(f"debug-config.{code}", "error", message)],
        text=[*text_lines, message],
    )


def _internal_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """Invalid kind / unsupported backend / unreadable or malformed existing
    launch.json: exit 5, with a `zephyr-mcu`/`none` placeholder target (matching
    the TS catch block, which never learned what was actually asked for)."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.INTERNAL_FAILURE,
        code="internal-failure",
        message=message,
        text_lines=["debug-config: internal failure"],
    )


def _write_failure(
    generated_at: str, target: str, server: str, launch_json_path: str, message: str
) -> _Outcome:
    """A filesystem error while creating `.vscode/` or writing launch.json: exit
    3, preserving the resolved target/server."""
    return _failure(
        generated_at=generated_at,
        target=target,
        server=server,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.WRITE_FAILURE,
        code="write-failure",
        message=message,
        text_lines=["debug-config: failed to write launch.json."],
    )


def _success_text(
    *,
    target: str,
    server: str,
    launch_json_path: str,
    replaced: bool,
    preview: bool,
    notes: list[str],
    configuration: Any,
    quiet: bool,
    issues: list[Issue],
) -> list[str]:
    """The human-readable lines for a successful preview or write."""
    lines: list[str] = []
    if preview:
        lines.append(f"debug-config: preview target={target} server={server}")
        lines.append(f"launch.json path: {launch_json_path}")
        if not quiet:
            lines.append("")
            lines.append(pretty_json(launch_preview_document(configuration)))
            lines.append("")
            lines.extend(f"note: {n}" for n in notes)
        return lines
    action = "updated" if replaced else "written"
    lines.append(f"debug-config: {action} target={target} server={server}")
    lines.append(f"launch.json: {launch_json_path}")
    # Always shown, even under --quiet: a one-time notice that the file just
    # lost a differently-named entry (folded into this one), or still holds a
    # stranded one, or lost a comment this run destroyed -- none of that is
    # routine noise like the resolution notes below it.
    for issue in issues:
        if issue.code in (
            "debug-config.legacy-entry-migrated",
            "debug-config.legacy-entry-untouched",
        ):
            lines.append(f"debug-config: {issue.message}")
    for issue in issues:
        if issue.code == "debug-config.comments-dropped":
            lines.append(f"note: {issue.message}")
    if not quiet:
        lines.extend(f"note: {n}" for n in notes)
    return lines


def _run(
    *,
    target_kind: str | None,
    server_arg: str | None,
    core: str | None,
    pre_launch_task: str | None,
    svd: str | None,
    preview: bool,
    project_arg: str,
    board_yaml_arg: str | None,
    quiet: bool,
) -> _Outcome:
    """The whole command, as a pure-ish computation returning one outcome.

    Nothing here emits or exits; [`debug_config`] does both exactly once. That
    split is what lets the exception guard wrap this call without swallowing
    `typer.Exit`.
    """
    generated_at = _generated_at()
    # Errors before workspace resolution report a cwd-based launch.json path and
    # a zephyr-mcu/none placeholder (matches the TS catch block).
    cwd_launch_path = os.path.join(os.getcwd(), ".vscode", "launch.json")

    try:
        target = parse_target_kind(target_kind)
        server = parse_server_kind(server_arg)
        draft = create_launch_draft(target, server, pre_launch_task)
    except DebugConfigError as err:
        return _internal_failure(generated_at, str(err), cwd_launch_path)

    workspace_root = _normalise(project_arg)
    launch_json_path = os.path.join(workspace_root, ".vscode", "launch.json")
    project_root, board_yaml = _resolve_project_reporting_fields(
        project_arg, board_yaml_arg
    )
    project = Project(root=project_root, board_yaml=board_yaml)

    # Fill the `<resolved-...>` placeholders from what this project's own build
    # recorded (#66). Nothing here fails the command: pre-build, or against a
    # Zephyr that reshaped `runners.yaml`, the draft keeps its placeholders.
    resolution, registered_runners = _resolve_from_build(
        workspace_root, target, server, core
    )

    # `--svd` is the ONLY producer of `resolution.svd` (tan-cli#197): the SDK
    # ships no SVD, so without the flag the field is structurally always absent
    # and `apply_launch_resolution` drops both svd keys.
    if svd is not None:
        try:
            resolution.svd = _resolve_user_svd(workspace_root, svd)
        except DebugConfigError as err:
            return _internal_failure(generated_at, str(err), launch_json_path)

    apply_launch_resolution(draft, resolution)
    notes = _preview_notes_for(draft, registered_runners, server)
    # A non-MCU draft carries no `svdFile` key at all, and
    # `apply_launch_resolution` only replaces keys that already exist -- so a
    # `--svd` here is a no-op. Say so rather than accepting the flag in silence
    # and leaving the user to wonder why no peripheral view appeared.
    if svd is not None and "svdFile" not in draft:
        notes.append(
            f"--svd was given, but target kind '{target_kind or ZEPHYR_MCU}' emits "
            "no svdFile field, so it had no effect: the Cortex Peripherals view "
            "is a cortex-debug (MCU) feature."
        )

    def success(
        *, replaced: bool, configuration: Any, issues: list[Issue], is_preview: bool
    ) -> _Outcome:
        return _Outcome(
            exit_code=ExitCode.SUCCESS,
            data=_data(
                generated_at=generated_at,
                target=target,
                server=server,
                preview=is_preview,
                launch_json_path=launch_json_path,
                replaced=replaced,
                notes=notes,
                configuration=configuration,
            ),
            project=project,
            issues=issues,
            text=_success_text(
                target=target,
                server=server,
                launch_json_path=launch_json_path,
                replaced=replaced,
                preview=is_preview,
                notes=notes,
                configuration=configuration,
                quiet=quiet,
                issues=issues,
            ),
        )

    if preview:
        # `--preview` never merges anything (it returns before the customer's
        # file is even read), so it reports the fresh draft -- which is also all
        # there is. tan-cli#180's preview-side invariant, and what the four
        # `debug-config-preview-*` goldens pin.
        return success(
            replaced=False, configuration=draft, issues=[], is_preview=True
        )

    # Write mode: merge into .vscode/launch.json.
    try:
        Path(launch_json_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return _write_failure(
            generated_at, target, server, launch_json_path, str(err)
        )

    # A READ error on an EXISTING launch.json (wrong encoding, e.g. UTF-16LE
    # from PowerShell `>` redirection; a denied ACL; a sharing violation) must
    # NOT collapse into the same "no file yet" as absence. That fed a fresh
    # document to the write plan and the write then overwrote the user's file
    # wholesale -- silently destroying every hand-written debug configuration at
    # exit 0. The malformed-JSON case below is deliberately guarded (no write);
    # a read error must refuse to write for the same reason.
    existing: str | None = None
    if Path(launch_json_path).exists():
        try:
            # `newline=""` on the READ as well as the write: the default
            # universal-newlines mode translates every `\r\n` to `\n` before the
            # splice ever sees the text, so `jsonc_splice`'s dominant-EOL check
            # found no CRLF and re-wrote a Windows-authored launch.json LF-only.
            # Rust's `read_to_string` does not translate; neither may this.
            with open(launch_json_path, encoding="utf-8", newline="") as handle:
                existing = handle.read()
        except (OSError, UnicodeDecodeError) as err:
            return _internal_failure(
                generated_at,
                f"Alp: failed to read existing .vscode/launch.json: {err}",
                cwd_launch_path,
            )

    try:
        plan = create_launch_json_write_plan(existing, draft)
    except DebugConfigError as err:
        # A malformed existing launch.json surfaces as an internal failure in TS.
        return _internal_failure(generated_at, str(err), cwd_launch_path)

    try:
        # `newline=""` so the splice's own CRLF survives: Python's text mode
        # would otherwise translate every `\n` it wrote, turning a CRLF-authored
        # file into `\r\r\n`. The content is already exactly the bytes intended.
        with open(launch_json_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(plan.content)
    except OSError as err:
        return _write_failure(
            generated_at, target, server, launch_json_path, str(err)
        )

    issues: list[Issue] = []
    if plan.migrated_from is not None:
        issues.append(_migrated_issue(plan.migrated_from, draft.get("name", "")))
    if plan.legacy_entry_present is not None:
        issues.append(_legacy_untouched_issue(plan.legacy_entry_present))
    if plan.comments_dropped:
        issues.append(_comments_dropped_issue())

    return success(
        replaced=plan.replaced,
        # tan-cli#180: report what this write actually put in the file -- the
        # merged/migrated result -- never the fresh `draft`, which still carries
        # its own `<resolved-...>` placeholders even after a merge resolved them
        # from the customer's real, hand-filled values.
        configuration=plan.written_configuration,
        issues=issues,
        is_preview=False,
    )


def debug_config(
    ctx: typer.Context,
    target_kind: str = typer.Option(
        None,
        "--target-kind",
        metavar="KIND",
        help="Debug target class (zephyr-mcu, baremetal-mcu, yocto-userspace, native-host).",
    ),
    server: str = typer.Option(
        None,
        "--server",
        metavar="SERVER",
        help="Debug server backend (jlink, openocd, pyocd, gdbserver, none).",
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help=(
            "Resolve against the build slice with this core_id. Defaults to the "
            "first slice matching the target class's OS."
        ),
    ),
    pre_launch_task: str = typer.Option(
        None,
        "--pre-launch-task",
        metavar="TASK",
        help=(
            "Emit preLaunchTask: <TASK> on the generated configuration. Off by "
            "default: VS Code aborts pre-launch on a task it cannot resolve."
        ),
    ),
    svd: str = typer.Option(
        None,
        "--svd",
        metavar="PATH",
        help=(
            "Point cortex-debug's Cortex Peripherals view at an SVD file you "
            "supply. The SDK ships none (alp-sdk#948). A relative path resolves "
            "against the current directory; an unreadable one fails the command."
        ),
    ),
    preview: bool = typer.Option(
        False, "--preview", help="Print the launch configuration without writing launch.json."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
) -> None:
    """Generate (or preview) a VS Code launch.json debug configuration."""
    # `--format` is accepted BEFORE the subcommand too (`tan --format json
    # debug-config ...`, which is what the committed goldens invoke and what
    # clap's `global = true` gives the Rust); the root callback records it and
    # this option overrides it when repeated after the command name.
    resolved_format = output_format or (ctx.obj or {}).get("format") or "text"
    if resolved_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{resolved_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = resolved_format == "json"

    try:
        outcome = _run(
            target_kind=target_kind,
            server_arg=server,
            core=core,
            pre_launch_task=pre_launch_task,
            svd=svd,
            preview=preview,
            project_arg=project or ".",
            board_yaml_arg=board_yaml,
            quiet=quiet,
        )
    except Exception as err:  # noqa: BLE001
        # The recurring break this guard exists for: an escaping traceback puts
        # nothing parseable on stdout, and the extension renders an empty panel
        # with no error at all. Any unforeseen failure becomes a coded envelope.
        # `typer.Exit` cannot reach here -- `_run` never raises it (it returns an
        # outcome) -- which is why the emit/exit pair lives outside this try:
        # `typer.Exit` subclasses RuntimeError, not SystemExit, so a bare
        # `except Exception` around it would swallow the process exit.
        outcome = _internal_failure(
            _generated_at(),
            f"debug-config failed unexpectedly: {err}",
            os.path.join(os.getcwd(), ".vscode", "launch.json"),
        )

    if json_mode:
        emit(
            Envelope(
                "debug-config",
                outcome.project,
                outcome.data,
                outcome.issues,
                outcome.exit_code,
            )
        )
    else:
        # stdout is the envelope channel and carries nothing else, in either
        # mode; stderr carries no contract of its own.
        stream = typer.get_text_stream("stderr")
        for line in outcome.text:
            stream.write(f"{line}\n")
    raise typer.Exit(int(outcome.exit_code))
