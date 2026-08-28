# SPDX-License-Identifier: Apache-2.0
"""`tan debug-config` -- generate (or preview) a VS Code launch.json entry.

Port of `crates/tan-cli/src/commands/debug_config.rs`. Build a launch draft for
the target class + server, resolve what this project's own build already knows
(#66), then either preview it (`--preview`) or merge it into
`<workspace>/.vscode/launch.json`. A malformed existing launch.json -> exit 5;
a failed write -> exit 3.

A write also reads and (best-effort) rewrites one more file:
`<workspace>/.alp/debug-launch-provenance.json` (tan-cli#518) -- a
content-hash record of which `configFiles`/`setupCommands` list entries THIS
command itself wrote, so a LATER merge can tell its own prior output apart
from the customer's without guessing from position. See `tan.core.
launch_provenance`'s module docstring for the full design and
`debug_launch.create_launch_json_write_plan`'s for how it gates the merge.
Losing that file (deleted, corrupted, never written) never blocks a write and
never risks customer content -- it only makes the NEXT merge more
conservative.

Everything else this command refuses is the CALLER's own precondition or flag
value to fix, not a tan crash, so it exits `VALIDATION_FAILURE` (2)
(tan-cli#462, matching the distinction tan-cli#262 settled for `tan
validate`): a bad `--target-kind`/`--server`/target+server pairing/`--svd`/
`--gdbserver-address` value (tan-cli#477); a `--project` that does not exist
or is not a directory (tan-cli#476 half (a)); and an omitted `--target-kind`
this project cannot resolve to one target class -- pre-build hardware, an
explicit `--core` matching no slice, more than one target class with no
`--core` to narrow them, or no slice whose `os` maps to a target class at all.
Two more joined that list rather than continuing to succeed silently:

* an omitted `--target-kind` on a project offering NO signal at all -- no
  `build/system-manifest.yaml` and no `board.yaml` `som.sku` -- which used to
  fall through to `parse_target_kind(None)`'s `native-host` default and write
  a `native_sim` launch configuration into whatever directory `--project`
  named (tan-cli#476 half (b), `_target_kind_unresolved_failure`);
* an explicit `--core` naming a core this project's SoM does not have, on a
  project with no build manifest to check it against, which used to leave
  `device` as the literal `<resolved-device>` at exit 0 -- validated against
  the SDK's own published core list instead (tan-cli#477 major 2,
  `_sdk_published_cores`).

**The debug profile follows from the target CLASS; the customer never selects an
OS or a backend.** `--target-kind` names one of four classes, each of which
implies its adapter (cortex-debug / cppdbg / lldb), its artefact shape and its
legal server set. There is no `--os` and no `--backend`, and nothing here knows
a SKU, a device address, an I2C address or a pin name -- the hardware facts live
in alp-sdk `metadata/`, and the ones a launch config needs arrive through this
project's OWN build output (`build/system-manifest.yaml` +
`<build_dir>/zephyr/runners.yaml`, both written beside the build).

**Nothing here shells the SDK.** Every input is either an argument, a file
this project's build already wrote under the workspace, or -- since
alp-sdk#1026 -- a `metadata/**` file this command reads directly out of a
resolved SDK checkout (`_fill_debug_probe_identity_from_sdk`, below): a
best-effort, silent enrichment, exactly like the `board.yaml`/
`system-manifest.yaml` reads already were. No `alp_project.py`, no
`alp_orchestrate`, no subprocess of any kind ever runs, and no `sdk` envelope
key is emitted (tan-cli#111 follow-up -- see
`_resolve_project_reporting_fields`): the SDK root is resolved and read for
this one silent enrichment, never reported as a dependency this command
declares. That is the invariant the port spec records as I-32 and its
anti-pattern #22: giving a command an alp-sdk-checkout dependency it does not
declare is a silent regression that no gate catches -- READING metadata files
is not that; SHELLING the SDK would be.

Every failure path emits a coded envelope. A raw traceback on stdout is
indistinguishable, to the extension, from tan producing nothing at all -- it
renders an empty panel with no error -- so the outer guard in [`debug_config`]
converts any unexpected exception into `debug-config.internal-failure` at exit
5 rather than letting it escape.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_output import read_sdk_som_and_soc, resolve_project_context
from tan.core.atomic_write import atomic_write_text
from tan.core.debug_launch import (
    BAREMETAL_MCU,
    GDBSERVER,
    JLINK,
    MANIFEST_OS_BY_TARGET,
    NATIVE_HOST,
    OPENOCD,
    PYOCD,
    SERVER_NONE,
    TARGET_KINDS,
    YOCTO_USERSPACE,
    ZEPHYR_MCU,
    DebugConfigError,
    LaunchResolution,
    apply_launch_resolution,
    create_launch_draft,
    create_launch_json_write_plan,
    explicit_core_unknown_message,
    fill_debug_probe_identity_gaps,
    infer_target_kind,
    is_unresolved_placeholder,
    launch_preview_document,
    launch_preview_notes,
    manifest_slices,
    parse_server_kind,
    parse_target_kind,
    sdk_identity_overwrites,
    sdk_identity_stranded_appends,
)
from tan.core import launch_provenance
from tan.core.global_flags import accept_global_flags
from tan.core.jsonc_splice import pretty_json
from tan.core.run import is_native_sim_board, native_sim_exe_beside
from tan.core.size import resolve_variant
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The `runners.yaml` runner id a debug server reads its arguments from.
#: `gdbserver`/`none` have no runner: neither is a Zephyr probe runner.
_RUNNER_ID = {JLINK: "jlink", OPENOCD: "openocd", PYOCD: "pyocd"}

#: `--server`'s default when this run just INFERRED `--target-kind` itself
#: (tan-cli#456) -- never applied over an explicit target, so the pre-existing
#: "no --server given" refusal there is unchanged.
_DEFAULT_SERVER_FOR_TARGET = {
    ZEPHYR_MCU: JLINK,
    BAREMETAL_MCU: JLINK,
    YOCTO_USERSPACE: GDBSERVER,
    NATIVE_HOST: SERVER_NONE,
}

#: The launch-configuration JSON key the SDK's debug-probe identity
#: (`variants[].debug`) resolves for a given server -- absent for a server the
#: identity has no concept of at all (`gdbserver`/`none`, neither of which
#: `create_launch_draft` ever pairs with a `variants[].debug` field).
_SERVER_IDENTITY_FIELD = {JLINK: "device", OPENOCD: "configFiles", PYOCD: "targetId"}


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
    """`SOURCE_DATE_EPOCH` when set, else now -- `tan.core.timestamp`, which
    NEVER raises. That matters here specifically: this is also called from the
    recovery path of the exception guard in `_emit_outcome`, so a throw
    DOUBLE-FAULTS into a raw traceback with EMPTY stdout.

    Millisecond precision with a `Z` suffix, matching JavaScript's
    `toISOString()`, because the envelopes are byte-compared against the TS
    CLI's `generatedAt` (and against the committed goldens, which spell
    `1970-01-01T00:00:00.000Z`). A whole-second format would fail all four
    `debug-config` fixtures.
    """
    return generated_at_iso(millis=True)


def _normalise(path: str) -> str:
    """Rust's `normalize_path(cwd.join(p))`: cwd-anchored and lexically
    normalised, in the platform's OWN separators (this value becomes
    `data.launchJsonPath`, which the contract harness normalises itself).

    `os.path.abspath`, not `Path.resolve()`: abspath is purely lexical, so a
    project reached through a symlink keeps the name the user typed and a path
    that does not exist yet still resolves -- `resolve()` would rewrite both.
    """
    return os.path.abspath(path)


def _resolve_project_reporting_fields(
    project_arg: str, board_yaml_arg: str | None, sdk_root_arg: str | None
) -> tuple[str, str, str | None, str, str | None]:
    """`(project.root, project.boardYaml, sdk_root, sdk_source_tier,
    foreign_global_default_for)`. The first two, both posix, are the fields
    this command REPORTS; `sdk_root` is read for the alp-sdk#1026 metadata
    fallback (`_fill_debug_probe_identity_from_sdk`) below and is NEVER
    attached to the outgoing envelope.

    The last two are the SDK's PROVENANCE, and they are returned for exactly
    one consumer: [`_sdk_core_refusal_authority`], the tan-cli#477 review
    round. Reading a debug-probe identity out of whatever SDK happened to
    resolve is harmless -- the worst case is a placeholder that stays a
    placeholder. REFUSING the run at exit 2 on that same checkout's word is
    not, so the caller has to know which tier answered before it lets the
    answer decide anything.

    Delegates to the SAME shared resolver every other command uses
    (`build_output.resolve_project_context`, port of
    `util::resolve_cli_project_context`) instead of a hand-rolled duplicate of
    its workspace/board_yaml computation -- tan-cli#170: `debug-config` used to
    hardcode `board_yaml: None` on every path, even a success with a valid
    `board.yaml` sitting in the resolved root, while every other command took
    both from the shared resolver.

    Deliberately the `_no_sdk_report` half of the Rust resolver's contract:
    the shared resolver's `sdk` half IS read here (unlike before alp-sdk#1026),
    but the caller must never pass it into the `Envelope(...)` call --
    resolving one could only add an undeclared `sdk` envelope key as a side
    effect of a field this command otherwise merely reports (tan-cli#111
    follow-up), and `debug-config` still drives no SDK subprocess of any kind
    (I-32).

    `board.yaml`'s existence is NOT checked HERE, matching
    `project.rs::resolve_board_yaml_path`, which joins the configured relative
    path onto the workspace root unconditionally -- this function only names
    where a `board.yaml` WOULD live. [`_run`]'s caller is the seam that checks
    (tan-cli#236, `Project.resolved`): the four hermetic goldens run in a
    scratch directory holding no `board.yaml` at all and report a `null`
    `project.boardYaml`, not this joined path.
    """
    context = resolve_project_context(project_arg, board_yaml_arg, sdk_root_arg)
    sdk_root = context.sdk.root if context.sdk is not None else None
    return (
        context.workspace_root,
        context.board_yaml,
        sdk_root,
        context.sdk_source_tier,
        context.foreign_global_default_for,
    )


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
            (s for s in slices if is_native_sim_board(s.get("board"))),
            None,
        )
    manifest_os = MANIFEST_OS_BY_TARGET.get(target)
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
) -> tuple[LaunchResolution, list[str], str | None]:
    """Everything this project's own build knows about how to debug it: the
    per-core ELF from `build/system-manifest.yaml`, and the probe/tool paths from
    that slice's `runners.yaml` -- the same file `west flash` reads.

    Best-effort throughout. A missing manifest (pre-build), a missing slice, an
    unreadable or reshaped `runners.yaml` each leave the corresponding field
    unresolved instead of failing the command: `debug-config` must still emit its
    draft before the first build. Returns the resolution, the runner ids the
    board registered (for the "this build registers no such runner" note), and
    the `core_id` of the slice this run actually selected (`None` before a
    matching slice is found) -- the SAME id `--core` would have named
    explicitly. alp-sdk#1026's SDK-metadata fallback
    (`_fill_debug_probe_identity_from_sdk`) needs it to index `jlink_device`
    (keyed per core) even when the caller passed no `--core` of its own, so a
    single-core project's ALREADY-built slice still resolves without forcing
    the user to repeat a core id `tan` already knows.
    """
    resolution = LaunchResolution()
    manifest = _load_yaml(Path(workspace_root, "build", "system-manifest.yaml"))
    slice_ = _select_slice(manifest_slices(manifest), target, core)
    if slice_ is None:
        return resolution, [], None
    core_id = slice_.get("core_id")
    core_id = core_id if isinstance(core_id, str) else None

    artefact = _str_or_none(slice_.get("output_artefact"))
    if artefact is not None:
        # A host target needs the sibling swap; every other target kind
        # genuinely wants the artefact verbatim.
        if target == NATIVE_HOST:
            artefact = native_sim_exe_beside(artefact)
        resolution.executable = _workspace_relative(workspace_root, artefact)

    build_dir = _str_or_none(slice_.get("build_dir"))
    if build_dir is None:
        return resolution, [], core_id
    runners = _load_yaml(Path(build_dir, "zephyr", "runners.yaml"))
    if not isinstance(runners, dict):
        return resolution, [], core_id
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
    return resolution, _str_list(runners.get("runners")), core_id


def _sdk_variant_debug_block(
    sdk_root: str, sku: str, *, warnings: list[str] | None = None,
    skipped: list[str] | None = None,
) -> dict[str, Any] | None:
    """The resolved SoC-JSON `variants[].debug` block for `sku`'s SoM preset,
    or `None` when any step of the walk fails to resolve one.

    The metadata-layout walk itself is `build_output.read_sdk_som_and_soc` --
    the ONE reader `tan size` also drives, not a second one that could
    disagree (the exact drift alp-sdk#1026 itself is about).

    A forward-only `resolve_variant` match: `sku` is passed as `None`,
    deliberately disabling the reverse `sku in alp_module_skus` fallback
    `tan size` itself relies on -- a drifted/`TBD` preset must resolve NO
    identity rather than possibly a WRONG one that still connects a live debug
    session to the wrong part (alp-sdk#1026 review finding #7).

    *warnings* (tan-cli#964 review, major 5): threaded straight through to
    `read_sdk_som_and_soc`, which threads it into both leaf readers'
    `validate_document` calls -- the same `metadata_root`/`warnings`
    convention `tan size` already uses. Before this, `tan debug-config`
    called `read_sdk_som_and_soc` with no `warnings` at all, so it validated
    NOTHING despite the PR body's own claim that it inherited #964's WARN
    half "transitively" -- it did not, until this parameter existed.

    *skipped* (tan-cli#964 review, major 6): same threading, for the
    "skip-but-disclose" collector -- a note when the schema file itself is
    simply absent, rather than the silent `[]` that used to be.
    """
    metadata_root = os.path.join(sdk_root, "metadata")
    walked = read_sdk_som_and_soc(metadata_root, sku, warnings=warnings, skipped=skipped)
    if walked is None:
        return None
    _silicon, silicon_variant, variants, _soc_flash_mb, _soc_cores = walked
    variant = resolve_variant(silicon_variant, None, variants)
    if variant is None:
        return None
    debug = variant.get("debug")
    return debug if isinstance(debug, dict) else None


def _board_som_sku(board_yaml_path: str) -> str | None:
    """This project's `board.yaml` `som.sku`, or `None` for every failure --
    missing/unreadable file, no PyYAML, not a mapping, no `som.sku`, an empty
    one. Extracted so the tan-cli#477 `--core` guard and
    [`_fill_debug_probe_identity_from_sdk`] read the SAME field the same
    tolerant way rather than each spelling the four-step `isinstance` walk out
    again (they used to be one site; the guard needs it strictly earlier)."""
    board = _load_yaml(Path(board_yaml_path))
    som = board.get("som") if isinstance(board, dict) else None
    sku = som.get("sku") if isinstance(som, dict) else None
    return sku if isinstance(sku, str) and sku != "" else None


def _sdk_published_cores(
    sdk_root: str | None, board_yaml_path: str, *, warnings: list[str] | None = None,
    skipped: list[str] | None = None,
) -> frozenset[str]:
    """Every core id this project's SoM publishes, per the SDK -- the SoC
    JSON's own `cores[].id`, unioned with the `variants[].debug.jlink_device`
    keys. Empty whenever the walk resolves nothing, which the caller reads as
    "cannot be asked", never as "this SoM has no cores".

    *warnings*/*skipped*: see `_sdk_variant_debug_block`'s own doc -- same
    threading, same reason (tan-cli#964 review, majors 5/6).

    tan-cli#477 major 2. `--core` pre-build is NOT decoration: with no
    `build/system-manifest.yaml` to check against, it selects which core's
    SDK-published debug-probe identity to resolve (alp-sdk#1026), so a guard
    keyed only on "does a build manifest exist" cannot tell a typo apart from
    that legitimate use -- which is exactly why #508 left this half open. It
    CAN be told apart by consulting the SDK's own published core list, and
    that list is already reachable here from the same `--sdk-root` the
    identity fallback below reads.

    `cores[].id` is the authority; the `jlink_device` union only widens it for
    a SoC JSON that omits `cores` entirely. Measured across every SoC JSON in
    alp-sdk `metadata/socs/**` (alif e3-e8, deepx dx/m1, nxp imx9/imx93,
    renesas rzv2n/n44): the `jlink_device` keys are a SUBSET of `cores[].id`
    in all nine, so on real metadata the union IS `cores[].id`. It is kept
    because the frozen contract fixture
    `contract/envelopes/debug-config-preview-zephyr-mcu-sdk-identity/sdk` --
    which cannot be edited -- publishes `jlink_device` and no `cores` at all,
    and its `--core m55_hp` must not become a false refusal.

    Same vocabulary either side: a `build/system-manifest.yaml` slice's
    `core_id` is spelled with these same ids (`m55_hp`, `m33_sm`,
    `a55_cluster`), which is what lets this stand in for the manifest check
    when there is no manifest.
    """
    if sdk_root is None:
        return frozenset()
    sku = _board_som_sku(board_yaml_path)
    if sku is None:
        return frozenset()
    walked = read_sdk_som_and_soc(
        os.path.join(sdk_root, "metadata"), sku, warnings=warnings, skipped=skipped
    )
    if walked is None:
        return frozenset()
    _silicon, silicon_variant, variants, _soc_flash_mb, soc_cores = walked
    cores = {core_id for core_id, _tcm_kb in soc_cores}
    variant = resolve_variant(silicon_variant, None, variants)
    debug = variant.get("debug") if isinstance(variant, dict) else None
    jlink_device = debug.get("jlink_device") if isinstance(debug, dict) else None
    if isinstance(jlink_device, dict):
        cores |= {k for k in jlink_device if isinstance(k, str)}
    return frozenset(cores)


def _sdk_core_refusal_authority(
    sdk_root: str | None, sdk_source_tier: str, foreign_global_default_for: str | None
) -> str | None:
    """The SDK root that is allowed to REFUSE a `--core`, or `None`.

    tan-cli#477 major 2, REVIEW round. The refusal below is decided by an SDK
    checkout, and until this guard existed that could be a checkout the user
    never named: with no `--sdk-root` and no project pin, `resolve_sdk_tiered`
    falls through to the machine-global default (`~/.alp/sdk-default`), which
    `tan bootstrap` may have last pointed at an unrelated project. Measured on
    this box, from a project directory that names no SDK at all:

        resolve_project_context('.', None, None).sdk
        -> SdkInfo(root='.../alp-workspace/sdk-triage',
                   source_tier='globalDefault',
                   foreign_global_default_for='.../t477/p')

    and the refusal flips purely on which checkout answers -- measured, same
    project, same `--core m55_hp`, two SDK roots differing only in `e8.json`'s
    `cores[]`: `--sdk-root A` -> exit 0, `--sdk-root B` -> exit 2
    `debug-config.core-unknown`. `debug-config` reports no `sdk` block at all
    (deliberately -- see [`_resolve_project_reporting_fields`]) and does not
    emit the `sdk.global-default-foreign-project` warning `size`/`image` emit
    for this same situation, so a customer would have no way to see that a
    stranger's checkout had just refused their build.

    The guard's own rule -- "refuse only what you can PROVE unknown" -- settles
    it. A global default last pinned by ANOTHER project cannot prove anything
    about THIS project's SoM, so it declines to `None`, which the caller reads
    as "cannot be asked" and stays silent, exactly as it does with no SDK at
    all. That single flag is the whole discriminator on purpose: every other
    way to arrive here -- `--sdk-root`, a working project pin, or a global
    default THIS project's own bootstrap set (including after a broken pin fell
    through to it) -- is an SDK this project is entitled to be judged by. Those
    still refuse, and the refusal now names the checkout and the tier that
    decided it.
    """
    if sdk_root is None:
        return None
    if foreign_global_default_for is not None:
        return None
    return sdk_root


def _sdk_core_unknown_message(
    core: str, published: frozenset[str], sdk_root: str, sdk_source_tier: str
) -> str:
    """The tan-cli#477 counterpart of `debug_launch.explicit_core_unknown_
    message`, which names the cores a BUILD produced. There is no build here,
    so it names the cores the SDK publishes instead -- same refusal, same
    code, a different (and the only available) authority.

    It names WHICH checkout, and by which tier it was chosen (REVIEW round).
    The build-manifest arm needs no such line -- the manifest is inside the
    project the user pointed at -- but this arm's authority may be a checkout
    the argv never mentions (`--sdk-root` absent, a project pin, or a global
    default), and `debug-config` publishes no `sdk` envelope block for the
    reader to look it up in. A refusal a customer cannot attribute is a support
    ticket; naming the path makes `--sdk-root <the right one>` the obvious next
    move. See [`_sdk_core_refusal_authority`] for the tier that is NOT allowed
    to get this far."""
    cores = ", ".join(sorted(published))
    return (
        f"--core {core} names no core this project's SoM has (its cores, per "
        f"the SDK's published metadata for this board.yaml's som.sku: "
        f"{cores}), and this project has no build/system-manifest.yaml to "
        "check against instead. Pass a --core value this SoM actually has. "
        f"That core list came from the alp-sdk checkout at {sdk_root} "
        f"(resolved by: {sdk_source_tier}) -- if that is not the checkout this "
        "project should be judged against, pass --sdk-root explicitly. "
        "Left unrefused, this wrote a launch.json whose device stayed the "
        "literal <resolved-device> placeholder at exit 0 -- a file that looks "
        "valid and fails later in the debugger (tan-cli#477)."
    )


def _fill_debug_probe_identity_from_sdk(
    resolution: LaunchResolution,
    sdk_root: str | None,
    board_yaml_path: str,
    core_id: str | None,
    *,
    warnings: list[str] | None = None,
    skipped: list[str] | None = None,
) -> tuple[bool, frozenset[str]]:
    """alp-sdk#1026: fill `resolution`'s remaining `device`/`target_id`/
    `config_files` gaps from the SDK's published per-variant debug-probe
    identity (`variants[].debug`, alp-sdk#987), so `tan debug-config` resolves
    a real J-Link device / pyOCD target before the project has ever been built
    -- the case [`_resolve_from_build`]'s `runners.yaml` read structurally
    cannot cover. Port of
    `crates/tan-cli/src/commands/debug_config.rs::fill_debug_probe_identity_from_sdk`.

    Best-effort throughout, exactly like [`_resolve_from_build`]: a missing
    `board.yaml`/`som.sku`, no resolved SDK root, a missing/unreadable SoM
    preset or SoC-JSON file, or a SoC variant that resolves but declares no
    `debug` block each leave `resolution` exactly as it was -- the caller's
    existing placeholder note still applies, and nothing here can fail the
    command.

    Returns `(debug_block_found, known_jlink_cores)`. `debug_block_found` is
    whether a `variants[].debug` block was actually found for the resolved SoC
    variant -- distinct from whether every field this run wanted got filled
    from it. The caller uses this (alp-sdk#1026 review finding #4) to tell
    "the SDK publishes an identity for this part, but not a value for the
    specific field this server needs yet" apart from "no identity was
    resolvable at all". `known_jlink_cores` is `jlink_device`'s own key set --
    tan-cli#489 (4): `jlink_device` is the ONE field here keyed by core
    (`pyocd_target`/`openocd_config` are not), so a `device` placeholder that
    survives despite `debug_block_found` being true is either "no core id was
    resolved to index with" or "the given core id has no entry in this SoM's
    published map" -- two distinct, both-fixable-by-`--core` causes the caller
    cannot tell apart, or name the known cores for, without this set.

    *warnings*/*skipped*: see `_sdk_variant_debug_block`'s own doc -- same
    threading, same reason (tan-cli#964 review, majors 5/6).
    """
    if sdk_root is None:
        return False, frozenset()
    sku = _board_som_sku(board_yaml_path)
    if sku is None:
        return False, frozenset()
    debug = _sdk_variant_debug_block(sdk_root, sku, warnings=warnings, skipped=skipped)
    if debug is None:
        return False, frozenset()
    jlink_device = debug.get("jlink_device")
    jlink_device = (
        {k: v for k, v in jlink_device.items() if isinstance(v, str)}
        if isinstance(jlink_device, dict)
        else {}
    )
    pyocd_target = debug.get("pyocd_target")
    pyocd_target = pyocd_target if isinstance(pyocd_target, str) else None
    openocd_config = debug.get("openocd_config")
    openocd_config = openocd_config if isinstance(openocd_config, str) else None
    fill_debug_probe_identity_gaps(resolution, core_id, jlink_device, pyocd_target, openocd_config)
    return True, frozenset(jlink_device.keys())


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


def _resolve_gdbserver_address(arg: str) -> str:
    """Validate `--gdbserver-address` (tan-cli#321). Emitted verbatim into
    `miDebuggerServerAddress` -- cppdbg accepts a bare hostname, an IPv4 or
    bracketed-IPv6 literal, so there is no single `host:port` shape narrow
    enough to validate without rejecting a real one; the only input that can
    never be a real address is an empty string, the same floor `--svd` holds
    for its own path argument.
    """
    if arg.strip() == "":
        raise DebugConfigError("Alp: --gdbserver-address was given an empty value.")
    return arg


def _gdbserver_address_unresolved_issue() -> Issue:
    """tan-cli#321 direction 1: the yocto-userspace draft's
    `miDebuggerServerAddress` is still the unresolved `<host>:<port>`
    placeholder in what this run actually produced. Severity `info` -- this is
    not a failure, it is the one field on this target class that NO build and
    NO SDK-published metadata can ever resolve (it names where the board ends
    up after deploy, a fact that exists only at runtime), so surfacing it
    explicitly is the whole point of this issue rather than leaving F5 to fail
    silently at connect.

    tan-cli#138 vs #321: unlike the other three target classes, this profile's
    `preLaunchTask` carries NO restored default -- see
    `tan.core.debug_launch.DEFAULT_PRE_LAUNCH_TASK`'s own doc comment for why:
    alp-sdk-vscode registers no working task for yocto-userspace (the only one
    that exists exits 1 by design), so naming one here would put the
    "preLaunchTask terminated with exit code 1" dialog in front of every F5.
    Said here, alongside the address gap, rather than as a second issue: both
    point at the same manual deploy-and-start-gdbserver step, and a customer
    who wants a reminder can still opt one in explicitly.
    """
    return Issue(
        "debug-config.gdbserver-address-unresolved",
        "info",
        "This yocto-userspace configuration's `miDebuggerServerAddress` is "
        "still the placeholder `<host>:<port>` -- the host and gdbserver port "
        "are a runtime property of the deployed board that no build can "
        "resolve. Fill it in by hand in launch.json once you know it, or pass "
        "`--gdbserver-address host:port` next time you regenerate this "
        "profile. tan has no deploy mechanism of its own, so deploying the "
        "binary and starting gdbserver on the target before F5 is still a "
        "manual step; this profile carries no `preLaunchTask` reminder of "
        "that by default (tan-cli#138 vs #321 -- the extension's only "
        "registered task for this target exits 1 by design, so naming it "
        "would fail before every F5). Pass `--pre-launch-task '<name>'` to "
        "add a reminder of your own.",
    )


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


def _sdk_identity_key_absent_issue(field: str) -> Issue:
    """alp-sdk#1026 review finding #4: emitted when the SDK DID publish a
    debug-probe identity for this project's SoC variant, but that identity
    does not (yet) include a value for `field` -- distinct from, and more
    specific than, the generic "Placeholder fields..." note every unresolved
    field already gets regardless of why. Severity `info`: this is the
    schema's own documented stance that an unpopulated key is a published
    "unknown", not an error and not a bug."""
    return Issue(
        "debug-config.sdk-identity-key-absent",
        "info",
        f"This SoM's SDK-published debug-probe identity (alp-sdk#987) does not "
        f"include a value for `{field}` yet, so it stays the placeholder shown "
        "in `configuration` — an unpopulated key is the correct published "
        '"unknown" (alp-sdk#1026), never a guess.',
    )


def _sdk_identity_core_unresolved_issue(core: str | None, known_cores: frozenset[str]) -> Issue:
    """tan-cli#489 (4): distinct from [`_sdk_identity_key_absent_issue`] above,
    which is only correct when the field genuinely has no core dependency
    (`configFiles`/`targetId`) or a core WAS resolved and is simply not the
    one the SDK's map happens to key `device` under. On a never-built project
    with no `--core`, `jlink_device.get(None)` is structurally always `None`
    -- the SDK's map is never even consulted -- so `sdk-identity-key-absent`
    told the customer "the SDK publishes no `device` for this SoM" when
    `metadata/socs/.../*.json` may publish one for every core it has. Both
    "no core id was resolved" and "the given core id has no entry in the
    published map" share the SAME working remedy (pass `--core <id>` naming a
    core the SDK DOES publish), so this one code covers both; the message
    differs only in whether a core was named to look up."""
    if core is None:
        return Issue(
            "debug-config.sdk-identity-core-unresolved",
            "info",
            "This SoM's SDK-published debug-probe identity (alp-sdk#987) is "
            "keyed per core, and no core id was resolved for this run (no "
            "--core given, and this project has no prior build to infer one "
            "from) — so `device` stays the placeholder shown in "
            "`configuration`, even though the SDK may publish a value for "
            "one or more of this SoM's cores. Pass --core <id> to resolve it.",
        )
    cores = ", ".join(sorted(known_cores)) if known_cores else "none"
    return Issue(
        "debug-config.sdk-identity-core-unresolved",
        "info",
        f"This SoM's SDK-published debug-probe identity (alp-sdk#987) has no "
        f"`device` entry for core '{core}' — its published cores are: "
        f"{cores}. `device` stays the placeholder shown in `configuration`; "
        "pass --core with one of the cores above, if that is the core you "
        "meant.",
    )


def _sdk_identity_overwrite_issue(field: str, existing_value: str, incoming_value: str) -> Issue:
    """alp-sdk#1026 review finding #1: emitted whenever the SDK's published
    debug-probe identity (not a real build) just replaced a concrete existing
    value on the entry this run wrote. Severity `info`, same reasoning as its
    three siblings above: the overwrite itself is not new or wrong (a value
    resolved from a real build already overwrote unconditionally, by design --
    see `tan.core.debug_launch._merge_configuration`'s doc comment) but a tool
    that replaces a customer's own value at `exit 0` with `issues: []` has
    told them nothing happened."""
    return Issue(
        "debug-config.sdk-identity-overwrite",
        "info",
        f'This write replaced the existing `{field}` value "{existing_value}" with '
        f'"{incoming_value}", resolved from the SDK\'s published debug-probe identity '
        "(alp-sdk#987) rather than from a real build. If "
        f'"{existing_value}" was a value you filled in on purpose — e.g. a J-Link '
        "flash-unlock device profile more specific than the generic attach device "
        "the SDK publishes — restore it in .vscode/launch.json; a value tan itself "
        "resolves from a real build will overwrite it again the same way.",
    )


def _sdk_identity_appended_issue(field: str, existing_value: str, incoming_value: str) -> Issue:
    """tan-cli#982 review finding #2: the accepted degradation
    [`sdk_identity_stranded_appends`] exists to name -- an existing `field`
    entry `provenance` could not prove was tan's own prior output was left in
    place, and the value resolved this run from the SDK's published
    debug-probe identity (alp-sdk#987) was APPENDED beside it rather than
    replacing it. Severity `info`, same family as
    `debug-config.comments-dropped` / `debug-config.legacy-entry-untouched`:
    nothing failed, but a customer never told two `configFiles` entries now
    sit on the same launch configuration has no way to know one is stale --
    OpenOCD sources every `-f`, so two board configs on one TAP fail the
    debug session outright, the same failure class this write just created
    silently."""
    return Issue(
        "debug-config.sdk-identity-appended",
        "info",
        f'This write left the existing `{field}` value "{existing_value}" in place '
        f'and appended "{incoming_value}", resolved from the SDK\'s published '
        "debug-probe identity (alp-sdk#987), instead of replacing it -- tan could "
        f'not prove "{existing_value}" was its own prior output (no recorded '
        "`.alp/` provenance for it), so it left it rather than risk overwriting a "
        "value you filled in by hand. If it is stale, delete it from "
        ".vscode/launch.json yourself.",
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


def _build_manifest_missing_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#462: a real hardware project (`som.sku` set) has not been built
    yet, so `--target-kind` cannot be inferred from a `build/system-
    manifest.yaml` that does not exist -- a precondition the CALLER can fix
    (`tan build` first, or an explicit `--target-kind`), not a tan-side
    crash. Exit 2, matching the sibling distinction tan-cli#262 settled for
    `tan validate`: a verdict the command CAN produce about the caller's own
    input is `VALIDATION_FAILURE`, never `INTERNAL_FAILURE` -- that exit
    stays reserved for the `except Exception` backstop below and an
    unreadable/malformed existing launch.json."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="build-manifest-missing",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _project_not_found_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#476: `--project` names a directory that does not exist.

    Refused rather than created. `_write_launch_json` calls `mkdir(parents=
    True)`, so a typo'd `--project` (or a stale path in a script) silently
    MATERIALISED a project tree and dropped a `launch.json` in it, at exit 0
    with `issues: []`. Nothing downstream could tell that apart from writing
    into a real project.

    A deliberate divergence from the oracle, which does the same thing --
    measured, not assumed: `target/release/tan debug-config --project <ghost>`
    exits 0 and leaves `<ghost>/.vscode/launch.json` behind. No parity CASE
    pins it (all four frozen `debug-config` argvs run in an existing
    `work_dir`), so this changes no frozen comparison. `--project` names a
    project that EXISTS; a path that does not is the caller's own input error,
    hence `ValidationFailure` (2), the same class as tan-cli#462's four.

    A separate code from `_invalid_argument_failure` below, on purpose
    (tan-cli#508 review): this fires before target/server are even parsed --
    a precondition on the WORKSPACE, the same class as
    `_build_manifest_missing_failure` above, not a flag-value shape check."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="project-not-found",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _target_kind_unresolved_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#476 half (b): `--target-kind` was omitted and this project
    offers NO signal to infer one from -- no `build/system-manifest.yaml`, and
    no `board.yaml` declaring a `som.sku`. Refused rather than silently
    defaulting to `native-host`.

    The other half of the same report. Half (a) -- a `--project` that does not
    exist -- is [`_project_not_found_failure`] above, fixed in tan-cli#508;
    that PR's own body records this half as "deliberately left". The two share
    one symptom: a `native_sim` launch configuration materialising, at exit 0
    with `issues: []`, in a directory that is not a project. #508 stopped the
    directory being CREATED; a directory that already exists (the "ran it from
    the wrong cwd" case the issue names in its own words) still got the file.

    `parse_target_kind(None)` -> `NATIVE_HOST` is a REAL default for a
    project that says so -- `infer_target_kind` returns `NATIVE_HOST` outright
    when every slice a build produced is native_sim. This refusal fires only
    on the no-evidence case that default was standing in for, so a project
    with any signal at all is untouched, and `--target-kind native-host`
    remains the way to ask for exactly this draft on purpose.

    Exit 2, the same class as `_build_manifest_missing_failure` above and for
    the same reason: a precondition on the WORKSPACE that the caller can fix,
    never a tan-side crash. A deliberate divergence from the Rust oracle,
    which exits 0 with the native-host draft -- measured, not inferred
    (`target/debug/tan --format json debug-config --project <empty> --preview`
    -> exit 0, `targetKind: "native-host"`). No parity CASE and no frozen
    conformance golden pins it: all five frozen `debug-config` argvs pass
    `--target-kind` explicitly, so none of them reaches the inference path at
    all."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="target-kind-unresolved",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _invalid_argument_failure(
    generated_at: str,
    message: str,
    launch_json_path: str,
    target: str = ZEPHYR_MCU,
    server: str = SERVER_NONE,
) -> _Outcome:
    """tan-cli#477: a flag VALUE outside the set this command accepts.

    Covers `--target-kind`, `--server`, an unsupported target+server pairing,
    `--svd` and `--gdbserver-address` -- every `DebugConfigError` raised while
    turning the caller's own arguments into a draft. All five already produced
    a complete, actionable message; only the verdict was wrong:

        Unsupported --target-kind 'bogus'. Allowed values: zephyr-mcu,
        baremetal-mcu, yocto-userspace, native-host.

    Reporting that as `internal failure` / exit 5 tells the user tan crashed
    and tells CI to treat a typo as a tool defect. tan-cli#462 made exactly
    this argument for the four PRECONDITIONS and reclassified them; the
    argument-validation half was not part of that change, so `--core bogus` --
    quoted verbatim in #462's own body -- still exited 5 in v0.5.1.

    Exit 5 stays reserved for what `_build_manifest_missing_failure`'s
    docstring already names: the `except Exception` backstop, and an
    unreadable or malformed EXISTING launch.json. Neither is reachable from a
    flag value.

    A DELIBERATE DIVERGENCE from the retired Rust oracle, which exited 5
    here -- measured (`target/release/tan debug-config --target-kind bogus` ->
    5) while both implementations existed. `test_oracle_parity.py` pinned it
    ("Pins exit 5 ... across both implementations") until tan-cli#269 deleted
    that module with `crates/`, so there is no CASE table left to update in the
    same commit. The line is pinned tan-side instead, on both sides of it:
    `tests/commands/test_debug_config_command.py`'s
    `test_a_refused_selector_is_a_coded_envelope_at_exit_2` holds the 2, and
    `test_a_malformed_existing_launch_json_stays_an_internal_failure` holds the
    reserved 5."""
    return _failure(
        generated_at=generated_at,
        target=target,
        server=server,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="invalid-argument",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _core_unknown_failure(generated_at: str, message: str, launch_json_path: str) -> _Outcome:
    """tan-cli#462: an explicit `--core` (with `--target-kind` omitted) names
    no slice this project's own build produced -- the caller's own typo or a
    stale flag, not a tan-side crash. Exit 2, same reasoning as
    [`_build_manifest_missing_failure`] above."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="core-unknown",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _explicit_core_unknown_failure(
    generated_at: str, target: str, server: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#489 (5): the SAME `core-unknown` refusal as
    [`_core_unknown_failure`] above, reached from the OTHER side --
    `--target-kind` given EXPLICITLY, so `infer_target_kind` (and its own
    `--core`-vs-manifest guard) never runs at all, and an explicit `--core`
    naming no slice in this project's own build used to sail through in
    silence: exit 0, a placeholder `device`, and an `executable` pointing at
    a path that does not exist in this project. Same code, same exit, same
    caller-fixable cause -- only the TARGET/SERVER differ: both are already
    resolved here (unlike the omitted-`--target-kind` path, which fires
    before either is known), so this reports the REAL ones instead of
    `_core_unknown_failure`'s `zephyr-mcu`/`none` placeholder pair."""
    return _failure(
        generated_at=generated_at,
        target=target,
        server=server,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="core-unknown",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _target_kind_ambiguous_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#462 review round: `--target-kind` omitted, and this project's
    own `build/system-manifest.yaml` -- well-formed, fully built, tan's own
    output -- names more than one target class (a mixed-core board, e.g.
    E1M-V2N101/V2M101's `a55_cluster`+`m33_sm`) with no `--core` to narrow
    them. Worse than the two refusals above: it hits every run against a
    CORRECT project forever, not just a pre-build one, and the remedy the
    message itself names (`--target-kind` + `--core`) works. Still the
    caller's own precondition, not a tan crash -- exit 2, same reasoning as
    [`_build_manifest_missing_failure`] above."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="target-kind-ambiguous",
        message=message,
        text_lines=["debug-config: validation failure"],
    )


def _no_debuggable_target_class_failure(
    generated_at: str, message: str, launch_json_path: str
) -> _Outcome:
    """tan-cli#462 review round: `--target-kind` omitted, hardware slices
    exist, but none of their `os` values is one `MANIFEST_OS_BY_TARGET` maps
    (e.g. a lone `os: linux` slice) -- a knowledge/version skew between tan
    and the SDK, not a crash: no invariant was violated and the command still
    produced a coherent verdict with a working remedy (`--target-kind`
    explicit). Exit 2, same reasoning as [`_build_manifest_missing_failure`]
    above."""
    return _failure(
        generated_at=generated_at,
        target=ZEPHYR_MCU,
        server=SERVER_NONE,
        launch_json_path=launch_json_path,
        exit_code=ExitCode.VALIDATION_FAILURE,
        code="no-debuggable-target-class",
        message=message,
        text_lines=["debug-config: validation failure"],
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
    gdbserver_address: str | None,
    svd: str | None,
    preview: bool,
    project_arg: str,
    board_yaml_arg: str | None,
    sdk_root_arg: str | None,
    quiet: bool,
) -> _Outcome:
    """The whole command, as a pure-ish computation returning one outcome.

    Nothing here emits or exits; [`debug_config`] does both exactly once. That
    split is what lets the exception guard wrap this call without swallowing
    `typer.Exit`.
    """
    generated_at = _generated_at()
    # Errors before target/server are even known report a cwd-based launch.json
    # path and a zephyr-mcu/none placeholder (matches the TS catch block).
    cwd_launch_path = os.path.join(os.getcwd(), ".vscode", "launch.json")

    workspace_root = _normalise(project_arg)
    launch_json_path = os.path.join(workspace_root, ".vscode", "launch.json")
    # tan-cli#476, FIRST: a `--project` that does not exist is refused, not
    # created. Everything below this point can write, and the writer uses
    # `mkdir(parents=True)`.
    #
    # Review round: `os.path.isdir` alone cannot tell "missing" apart from
    # "exists but is a file", so the two get their own message rather than
    # both being told they "do not exist" -- a `--project` pointing at, say,
    # `board.yaml` genuinely exists, just not as a directory. And the
    # parenthetical resolved-path suffix is dropped when `project_arg` was
    # already absolute, since `_normalise` would otherwise repeat it verbatim.
    if not os.path.isdir(workspace_root):
        resolved_suffix = (
            "" if os.path.isabs(project_arg) else f" ({workspace_root})"
        )
        if os.path.exists(workspace_root):
            reason = "is not a directory"
        else:
            reason = "does not exist"
        return _project_not_found_failure(
            generated_at,
            f"--project {project_arg!r} {reason}{resolved_suffix}. "
            f"Name a project directory that already exists; debug-config "
            f"never creates one.",
            launch_json_path,
        )
    (
        project_root,
        board_yaml,
        sdk_root,
        sdk_source_tier,
        foreign_global_default_for,
    ) = _resolve_project_reporting_fields(project_arg, board_yaml_arg, sdk_root_arg)
    # tan-cli#236, the pair of #170 above: `boardYaml` reported only when a
    # file is really at the resolved path.
    project = Project.resolved(project_root, board_yaml)

    # tan-cli#456: an omitted --target-kind must never silently default to
    # native-host on a project whose own build cannot produce that binary --
    # infer it from the project instead, and pick a matching --server default
    # too (leaving it at the literal "none" default would only trade one
    # broken draft for `create_launch_draft`'s "Unsupported debug backend
    # 'none'" refusal). Never overrides a target/server the caller named
    # explicitly. `infer_target_kind` itself is pure and shared (`tan.core.
    # debug_launch`); this is just its IO, the same best-effort reads as
    # everything else in this module.
    effective_target_kind = target_kind
    effective_server_arg = server_arg
    inferred: str | None = None
    if target_kind is None:
        manifest = _load_yaml(Path(workspace_root, "build", "system-manifest.yaml"))
        inferred, reason_code, ambiguous = infer_target_kind(
            manifest, core, _board_som_sku(board_yaml)
        )
        if ambiguous is not None:
            # `launch_json_path` -- this project's own -- not `cwd_launch_path`:
            # unlike the pre-#456 catch-all below, it is already resolved by
            # this point, so a cwd-based path here would just name whatever
            # directory the shell happened to be in.
            #
            # tan-cli#462: `reason_code`, when set, names which of
            # `infer_target_kind`'s refusals the caller can fix themselves --
            # VALIDATION_FAILURE (2), not INTERNAL_FAILURE (5). Review round:
            # its other two ambiguity shapes (more than one target class, or
            # none at all) are the SAME defect, not "unclassified" -- both
            # now carry their own code too, so this dispatches all four.
            if reason_code == "build-manifest-missing":
                return _build_manifest_missing_failure(generated_at, ambiguous, launch_json_path)
            if reason_code == "core-unknown":
                return _core_unknown_failure(generated_at, ambiguous, launch_json_path)
            if reason_code == "target-kind-ambiguous":
                return _target_kind_ambiguous_failure(generated_at, ambiguous, launch_json_path)
            if reason_code == "no-debuggable-target-class":
                return _no_debuggable_target_class_failure(
                    generated_at, ambiguous, launch_json_path
                )
            return _internal_failure(generated_at, ambiguous, launch_json_path)
        if inferred is None:
            # tan-cli#476 half (b): NO signal at all -- `infer_target_kind`
            # returned `(None, None, None)`, its "nothing to go on" answer.
            # Falling through here reached `parse_target_kind(None)`, whose
            # `native-host` default then wrote an `Alp: Native Sim Debug`
            # entry into whatever directory `--project` happened to name.
            # That default is only defensible where the project SAYS it is a
            # native_sim project, which `infer_target_kind` answers with a
            # real `NATIVE_HOST` of its own, not with "no signal".
            return _target_kind_unresolved_failure(
                generated_at,
                f"--target-kind was not given, and {workspace_root} offers "
                "nothing to infer one from: it has no "
                "build/system-manifest.yaml, and no board.yaml declaring "
                "som.sku. Pass --target-kind explicitly (one of: "
                f"{', '.join(TARGET_KINDS)}) -- debug-config no longer "
                "defaults to native-host, which wrote a native_sim launch "
                "configuration into any directory it was pointed at "
                "(tan-cli#476). If this really is the project you meant, "
                "run tan build first, or check you are in the right "
                "directory.",
                launch_json_path,
            )
        effective_target_kind = inferred
        if server_arg is None:
            effective_server_arg = _DEFAULT_SERVER_FOR_TARGET.get(inferred)

    try:
        target = parse_target_kind(effective_target_kind)
        server = parse_server_kind(effective_server_arg)
    except DebugConfigError as err:
        # Neither local is bound yet -- the zephyr-mcu/none placeholder is the
        # honest answer here (matches the TS catch block).
        return _invalid_argument_failure(generated_at, str(err), cwd_launch_path)

    try:
        draft = create_launch_draft(target, server, pre_launch_task)
    except DebugConfigError as err:
        # #508 review, Major 4 follow-up: `target`/`server` ARE already bound
        # by this point (both parsed above) -- an unsupported PAIRING of two
        # individually-valid values must report the pairing it actually
        # refused, not the placeholder. Same reasoning as the `--svd` /
        # `--gdbserver-address` sites below, which already do this.
        return _invalid_argument_failure(
            generated_at, str(err), cwd_launch_path, target, server
        )

    # tan-cli#964 review (major 5): collects every `som-preset-v1`/
    # `soc-spec-v1` schema violation found while EITHER SDK-metadata walk
    # below reads this project's SoM preset/SoC JSON -- `_sdk_published_cores`
    # (the --core guard, just below) and `_fill_debug_probe_identity_from_sdk`
    # (the identity fallback, further down) share this one list rather than
    # each collecting -- and reporting -- its own, so a violation both walks
    # would hit is not folded into two `debug-config.metadata-schema-invalid`
    # issues.  Before this, `tan debug-config` passed no `warnings` to either
    # walk and validated NOTHING, despite #964's PR body claiming it inherited
    # the WARN half "transitively" through `read_sdk_som_and_soc`.
    schema_warnings: list[str] = []
    # tan-cli#964 review (major 6, "skip-but-disclose"): the same shared-list
    # convention as `schema_warnings` above, for the OTHER half -- a note
    # when the schema file itself is simply absent, rather than the silent
    # `[]` that used to be indistinguishable from "validated clean".
    schema_skipped: list[str] = []

    # tan-cli#489 (5): an EXPLICIT --target-kind bypasses `infer_target_kind`
    # entirely, so its own --core-vs-manifest guard (the `core-unknown` refusal
    # above) never runs for this path. Without this check, --core naming no
    # slice in this project's own build sailed through in silence: exit 0, a
    # placeholder `device`, and an `executable` pointing at a path this
    # project's build never produced. Checked against the WHOLE manifest (any
    # os), mirroring `infer_target_kind`'s own guard exactly.
    #
    # tan-cli#477 major 2 closes the half #489 left open: with NO manifest at
    # all this used to stay silent, so an unknown --core on a real hardware
    # project sailed through at exit 0 with `device` left as the literal
    # `<resolved-device>` -- a launch.json that looks valid and fails later in
    # the debugger with nothing connecting the failure back to this command.
    # The reason it was left open was that `--core` pre-build has a SECOND,
    # legitimate job -- selecting which core's SDK-published debug-probe
    # identity to resolve (alp-sdk#1026, `identity_core = core or
    # build_core_id` below) -- which a guard keyed only on "does a manifest
    # exist" cannot tell apart from a typo "without also consulting the SDK's
    # own published core list". So it consults it: `_sdk_published_cores`
    # reads the same `--sdk-root` the identity fallback already does.
    #
    # The two authorities are ordered, never merged: a real build is the truth
    # about which cores THIS project produced, so when a manifest exists it
    # alone decides (a SoM core the project does not build must still be
    # refused). The SDK list stands in only where there is no manifest.
    # BOTH arms refuse only what they can PROVE unknown -- no slices / no
    # resolvable SDK core list means "cannot be asked", and staying silent
    # there is the existing, correct "not built yet" behaviour every other
    # placeholder-carrying draft already has. `--core` is not a per-server
    # flag, so this is server-independent: measured pre-fix, the openocd arm
    # reported only `sdk-identity-key-absent` (silent about the core) and the
    # pyocd arm reported `issues: []`.
    if target_kind is not None and core is not None:
        build_manifest = _load_yaml(Path(workspace_root, "build", "system-manifest.yaml"))
        all_slices = manifest_slices(build_manifest)
        if all_slices:
            if not any(s.get("core_id") == core for s in all_slices):
                return _explicit_core_unknown_failure(
                    generated_at,
                    target,
                    server,
                    explicit_core_unknown_message(core, all_slices),
                    launch_json_path,
                )
        else:
            # NOT `sdk_root` directly: only a checkout this project is entitled
            # to be judged by may turn a `--core` into an exit-2 refusal. See
            # `_sdk_core_refusal_authority` (tan-cli#477 REVIEW round) -- the
            # identity fallback further down still reads `sdk_root` whatever
            # its provenance, because filling a placeholder from the wrong
            # checkout leaves a placeholder, while refusing on its word stops
            # the run.
            refusal_sdk = _sdk_core_refusal_authority(
                sdk_root, sdk_source_tier, foreign_global_default_for
            )
            published_cores = _sdk_published_cores(
                refusal_sdk, board_yaml, warnings=schema_warnings, skipped=schema_skipped
            )
            if published_cores and core not in published_cores:
                return _explicit_core_unknown_failure(
                    generated_at,
                    target,
                    server,
                    _sdk_core_unknown_message(
                        core, published_cores, str(refusal_sdk), sdk_source_tier
                    ),
                    launch_json_path,
                )

    # Fill the `<resolved-...>` placeholders from what this project's own build
    # recorded (#66). Nothing here fails the command: pre-build, or against a
    # Zephyr that reshaped `runners.yaml`, the draft keeps its placeholders.
    resolution, registered_runners, build_core_id = _resolve_from_build(
        workspace_root, target, server, core
    )

    # alp-sdk#1026: whatever the build did NOT already resolve, try the SDK's
    # published per-variant debug-probe identity next -- `--core` if given,
    # else the core id the build itself just resolved. See
    # `_fill_debug_probe_identity_from_sdk`'s own docstring for why `device`
    # stays the placeholder on a never-built project with no `--core`.
    identity_core = core or build_core_id
    # Snapshot of what the BUILD (not the SDK) resolved, taken before the SDK
    # fallback runs, so `sdk_filled_json_fields` below can name exactly the
    # fields the SDK fallback itself just populated -- never a field a real
    # build's `runners.yaml` already resolved (that overwrite is pre-existing,
    # intended behaviour per `_merge_configuration`'s own doc comment, not
    # something this disclosure is scoped to cover).
    device_before_identity = resolution.device
    target_id_before_identity = resolution.target_id
    config_files_empty_before_identity = not resolution.config_files
    identity_debug_block_found, known_jlink_cores = _fill_debug_probe_identity_from_sdk(
        resolution, sdk_root, board_yaml, identity_core,
        warnings=schema_warnings, skipped=schema_skipped,
    )
    # Which launch-configuration JSON keys the SDK fallback (not a real build)
    # just populated -- the ONLY fields `sdk_identity_overwrites` below is
    # allowed to flag (alp-sdk#1026 review finding #1).
    sdk_filled_json_fields: list[str] = []
    if device_before_identity is None and resolution.device is not None:
        sdk_filled_json_fields.append("device")
    if target_id_before_identity is None and resolution.target_id is not None:
        sdk_filled_json_fields.append("targetId")
    if config_files_empty_before_identity and resolution.config_files:
        sdk_filled_json_fields.append("configFiles")

    # `--svd` is the ONLY producer of `resolution.svd` (tan-cli#197): the SDK
    # ships no SVD, so without the flag the field is structurally always absent
    # and `apply_launch_resolution` drops both svd keys.
    if svd is not None:
        try:
            resolution.svd = _resolve_user_svd(workspace_root, svd)
        except DebugConfigError as err:
            # tan-cli#477 review: `target`/`server` ARE known here, unlike at
            # the parse site above -- reporting the zephyr-mcu/none
            # placeholder would misdescribe what the caller asked for, and a
            # validation verdict the extension may render must not.
            return _invalid_argument_failure(
                generated_at, str(err), launch_json_path, target, server
            )

    # `--gdbserver-address` is the ONLY producer of `resolution.gdbserver_address`
    # (tan-cli#321): a runtime property of the deployed board, so nothing else
    # -- not a build, not SDK-published metadata -- can ever fill it.
    if gdbserver_address is not None:
        try:
            resolution.gdbserver_address = _resolve_gdbserver_address(gdbserver_address)
        except DebugConfigError as err:
            # Same as `--svd` above: both are known by this point.
            return _invalid_argument_failure(
                generated_at, str(err), launch_json_path, target, server
            )

    apply_launch_resolution(draft, resolution)

    # alp-sdk#1026 review finding #4: which server-identity field the SDK
    # fallback found a block for but could not fill -- checked against the
    # FINAL draft (after `apply_launch_resolution`), not the pre-resolution
    # one, or a field the fallback itself just resolved would misreport as
    # still absent. Advisory about resolution state, so it fires on
    # `--preview` too, not just a write.
    identity_issues: list[Issue] = [
        # tan-cli#964 review (major 5): the WARN half of #964's decided rule,
        # same shape `presets_cmd.py`/`size_cmd.py` already use -- one issue
        # per violation, naming the file, the JSON pointer, and what was
        # found. `debug-config` already degrades a schema-invalid preset/SoC
        # JSON to its existing placeholder behaviour (nothing above refuses
        # on `schema_warnings`); this makes that degrade visible instead of
        # silent.
        Issue("debug-config.metadata-schema-invalid", "warning", w)
        for w in schema_warnings
    ]
    # tan-cli#964 review (major 6, "skip-but-disclose"): `info`, not
    # `warning` -- nothing here says a document is wrong, only that a schema
    # to check it against is absent. Deduplicated: both walks above can each
    # find the same missing schema.
    identity_issues.extend(
        Issue("debug-config.metadata-schema-unchecked", "info", w)
        for w in dict.fromkeys(schema_skipped)
    )
    if identity_debug_block_found:
        field = _SERVER_IDENTITY_FIELD.get(server)
        if field is not None and _has_placeholder(draft.get(field)):
            # tan-cli#489 (4): `jlink_device` is the ONE field this identity
            # keys by core (`configFiles`/`targetId` are not core-dependent at
            # all -- `sdk-identity-key-absent` is always the right call for
            # those). For `device`, a placeholder surviving despite a found
            # debug block means the lookup itself never had a usable key: no
            # core was resolved, or the one resolved has no entry in the
            # published map -- never "the SDK publishes no value for this
            # SoM", which is what `sdk-identity-key-absent` would have said.
            #
            # Review round: `bool(known_jlink_cores)` is required in front of
            # the core-mismatch half. A SoM whose published `variants[].debug`
            # carries NO `jlink_device` key at all (real shape: today's Alif
            # entries publish `openocd_config` but not `jlink_device`) makes
            # `known_jlink_cores` the empty set regardless of `identity_core`
            # -- `core_unindexed` was unconditionally True, so even a VALID
            # `--core m55_hp` reported "has no `device` entry for core
            # 'm55_hp' -- its published cores are: none ... pass --core with
            # one of the cores above", advice that contradicts itself. That
            # SoM genuinely publishes no `device` value for ANY core --
            # `sdk-identity-key-absent` is the correct, and only correct,
            # code for it, exactly as it was before this split.
            core_unindexed = bool(known_jlink_cores) and (
                identity_core is None or identity_core not in known_jlink_cores
            )
            if server == JLINK and core_unindexed:
                identity_issues.append(
                    _sdk_identity_core_unresolved_issue(identity_core, known_jlink_cores)
                )
            else:
                identity_issues.append(_sdk_identity_key_absent_issue(field))
    notes = _preview_notes_for(draft, registered_runners, server)
    # tan-cli#456 review: say when target/server were DERIVED, not requested --
    # otherwise silent, unlike the --svd/--gdbserver-address no-op notes right
    # below. Never fires for the no-signal native-host default (no manifest to
    # name).
    if target_kind is None and inferred is not None:
        server_clause = f" and --server '{effective_server_arg}'" if server_arg is None else ""
        notes.append(
            f"--target-kind was not given; inferred '{inferred}'{server_clause} from "
            "this project's build/system-manifest.yaml."
        )
    # A non-MCU draft carries no `svdFile` key at all, and
    # `apply_launch_resolution` only replaces keys that already exist -- so a
    # `--svd` here is a no-op. Say so rather than accepting the flag in silence
    # and leaving the user to wonder why no peripheral view appeared.
    if svd is not None and "svdFile" not in draft:
        notes.append(
            # `target`, the RESOLVED target -- `target_kind` is `None` on
            # every tan-cli#456 inference path and would mis-name it.
            f"--svd was given, but target kind '{target}' emits "
            "no svdFile field, so it had no effect: the Cortex Peripherals view "
            "is a cortex-debug (MCU) feature."
        )
    # Same "no silent no-op" floor as `--svd` above: only a yocto-userspace
    # draft carries `miDebuggerServerAddress` at all.
    if gdbserver_address is not None and "miDebuggerServerAddress" not in draft:
        notes.append(
            f"--gdbserver-address was given, but target kind "
            f"'{target}' emits no miDebuggerServerAddress "
            "field, so it had no effect: that field is a yocto-userspace "
            "(cppdbg) feature."
        )

    def success(
        *, replaced: bool, configuration: Any, issues: list[Issue], is_preview: bool
    ) -> _Outcome:
        # tan-cli#321: checked against `configuration` -- the value ACTUALLY
        # going out (the fresh `draft` on `--preview`, the merged
        # `written_configuration` on a write) -- not the pre-merge `draft`
        # this closure captures from its enclosing scope. A write that merged
        # over a customer's own already-hand-filled address must not re-nag
        # them every run; checking the final value is what tells the two
        # apart, the same distinction `_has_placeholder` exists for.
        final_issues = list(issues)
        if target == YOCTO_USERSPACE and isinstance(configuration, dict):
            if _has_placeholder(configuration.get("miDebuggerServerAddress")):
                final_issues.append(_gdbserver_address_unresolved_issue())
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
            issues=final_issues,
            text=_success_text(
                target=target,
                server=server,
                launch_json_path=launch_json_path,
                replaced=replaced,
                preview=is_preview,
                notes=notes,
                configuration=configuration,
                quiet=quiet,
                issues=final_issues,
            ),
        )

    if preview:
        # `--preview` never merges anything (it returns before the customer's
        # file is even read), so it reports the fresh draft -- which is also all
        # there is. tan-cli#180's preview-side invariant, and what the four
        # `debug-config-preview-*` goldens pin.
        return success(
            replaced=False, configuration=draft, issues=identity_issues, is_preview=True
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

    # tan-cli#518: the `.alp/` provenance sidecar, read the SAME best-effort
    # way `_write_project_sdk_pointer`'s own reads are (`bootstrap_cmd.py`) --
    # ANY failure (absent file, a read error, unparsable/unrecognised JSON;
    # see `launch_provenance.load`'s own docstring) degrades to `empty()`,
    # never to "everything is ours". This is deliberately NOT held to the
    # same read-error-must-refuse-to-write bar as `launch.json` itself just
    # above: losing provenance only makes the NEXT merge more conservative
    # (append instead of overwrite), never destructive, so there is nothing
    # here worth refusing the write over.
    provenance_path = launch_provenance.sidecar_path(workspace_root)
    provenance_content: str | None = None
    if provenance_path.is_file():
        try:
            with open(provenance_path, encoding="utf-8") as handle:
                provenance_content = handle.read()
        except OSError:
            provenance_content = None
    provenance = launch_provenance.load(provenance_content)

    # alp-sdk#1026 review finding #1: compute this BEFORE the write, against
    # the file as it stood -- `create_launch_json_write_plan` below already
    # performs the same overwrite (that part of its behaviour is intentional,
    # see `_merge_configuration`'s own doc comment), this only detects it so
    # it can be disclosed. tan-cli#518: `provenance` is passed through so a
    # LIST field (`configFiles`) is only reported as overwritten when the
    # real merge below would actually do that -- see `sdk_identity_overwrites`'s
    # own doc comment on why an honest "would this actually happen" beats an
    # unconditional "the values differ".
    overwrites = sdk_identity_overwrites(
        existing, draft, sdk_filled_json_fields, provenance=provenance
    )
    # tan-cli#982 review finding #2: the OTHER outcome that same merge can
    # produce for a list field -- an existing value provenance could not
    # prove was tan's own is left in place and the new one is appended
    # beside it, rather than replaced. `sdk_identity_overwrites` above
    # correctly stays silent about this shape (nothing concrete was lost);
    # this discloses the append instead, so it is not silent everywhere.
    stranded_appends = sdk_identity_stranded_appends(
        existing, draft, sdk_filled_json_fields, provenance=provenance
    )

    # tan-cli#489 (6): `--pre-launch-task ''` opts OUT of a `preLaunchTask` key
    # entirely (`create_launch_draft` builds it, then deletes it), which is
    # indistinguishable, to the merge's own "only visit the draft's own keys"
    # rule, from this target simply having no default -- so the opt-out was a
    # silent no-op against an entry a PRIOR run already gave one. Named here,
    # explicitly, rather than left for `create_launch_json_write_plan` to
    # infer from the draft alone.
    explicit_omissions = frozenset({"preLaunchTask"}) if pre_launch_task == "" else frozenset()

    try:
        plan = create_launch_json_write_plan(
            existing, draft, explicit_omissions, provenance=provenance
        )
    except DebugConfigError as err:
        # A malformed existing launch.json surfaces as an internal failure in TS.
        return _internal_failure(generated_at, str(err), cwd_launch_path)

    # `open(launch_json_path, "w")` truncates the customer's file to zero
    # before a single byte of `plan.content` is written, and that content
    # exists only in memory -- a failure between the truncate and the flush
    # (ENOSPC, a quota/RLIMIT_FSIZE hit, an I/O error, or the process dying:
    # SIGKILL, power loss, the VS Code extension killing the child) destroys
    # every hand-written configuration with no way for tan to repair it: the
    # next run reads the wreckage, hits the malformed-JSON guard above, and
    # refuses at exit 5 forever. `atomic_write_text`'s own docstring
    # (`tan/core/atomic_write.py`) covers the rest: symlink-safe, fsync'd
    # before the rename, and mode-preserving -- shared with
    # `bootstrap_cmd.reconcile_west_manifest_path` (tan-cli#516) rather than
    # a second hand-synchronised copy living beside this call site.
    try:
        atomic_write_text(launch_json_path, plan.content)
    except OSError as err:
        return _write_failure(
            generated_at, target, server, launch_json_path, str(err)
        )

    # tan-cli#518: persist the updated provenance sidecar AFTER launch.json
    # itself is safely on disk, and best-effort -- a failure here never fails
    # the command (the launch.json write is the one that matters and it
    # already succeeded) and never leaves a half-written sidecar
    # (`atomic_write_text` again), it just means the NEXT run degrades back
    # to `launch_provenance.empty()`'s conservative default for whatever this
    # write recorded, exactly as if this run had never touched the sidecar
    # at all.
    try:
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(provenance_path), launch_provenance.render(plan.provenance))
    except OSError:
        pass

    issues: list[Issue] = list(identity_issues)
    if plan.migrated_from is not None:
        issues.append(_migrated_issue(plan.migrated_from, draft.get("name", "")))
    if plan.legacy_entry_present is not None:
        issues.append(_legacy_untouched_issue(plan.legacy_entry_present))
    if plan.comments_dropped:
        issues.append(_comments_dropped_issue())
    # alp-sdk#1026 review finding #1: this write just replaced a concrete
    # existing value with one resolved from the SDK's published debug-probe
    # identity rather than a real build -- say so, the same way a dropped
    # comment is disclosed rather than left for the customer to notice by
    # diffing the file themselves.
    for field, existing_value, incoming_value in overwrites:
        issues.append(
            _sdk_identity_overwrite_issue(field, existing_value, incoming_value)
        )
    for field, existing_value, incoming_value in stranded_appends:
        issues.append(
            _sdk_identity_appended_issue(field, existing_value, incoming_value)
        )

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
            "Emit preLaunchTask: <TASK> on the generated configuration. "
            "Defaults to the v0.3.1 task name for this target (tan-cli#138): "
            "'alp: build active target' (zephyr-mcu), 'alp: build baremetal "
            "target' (baremetal-mcu), 'alp: build native_sim target' "
            "(native-host). yocto-userspace carries no default (tan-cli#321: "
            "the extension's only registered task for it exits 1 by design) "
            "-- pass this flag explicitly to add a reminder. Pass an empty "
            "string to omit the key entirely."
        ),
    ),
    gdbserver_address: str = typer.Option(
        None,
        "--gdbserver-address",
        metavar="HOST:PORT",
        help=(
            "Fill miDebuggerServerAddress on a yocto-userspace configuration "
            "(tan-cli#321). This is a runtime property of the deployed board "
            "that no build can resolve; without it the field stays the "
            "<host>:<port> placeholder and F5 fails at connect."
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
    sdk_root: str = typer.Option(  # read for the alp-sdk#1026 metadata fallback; see below
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
) -> None:
    """Generate (or preview) a VS Code launch.json debug configuration.

    `--sdk-root` is read, but only as a best-effort, silent enrichment
    (alp-sdk#1026): a resolved checkout's `metadata/**` fills whatever the
    build itself did not already resolve (`_fill_debug_probe_identity_from_sdk`),
    but is never shelled and never reported as an `sdk` envelope dependency,
    matching the module docstring above. clap makes `--sdk-root` `global =
    true` in Rust regardless, so `tan --sdk-root X debug-config` must not be a
    parse error even when `X` names no real checkout -- an unresolvable value
    changes nothing in the envelope (the fallback silently finds nothing),
    same as the oracle.
    """
    # `--format` is accepted BEFORE the subcommand too (`tan --format json
    # debug-config ...`, which is what the committed goldens invoke and what
    # clap's `global = true` gives the Rust); the root callback records it and
    # this option overrides it when repeated after the command name.
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    try:
        outcome = _run(
            target_kind=target_kind,
            server_arg=server,
            core=core,
            pre_launch_task=pre_launch_task,
            gdbserver_address=gdbserver_address,
            svd=svd,
            preview=preview,
            project_arg=project or ".",
            board_yaml_arg=board_yaml,
            sdk_root_arg=sdk_root,
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


# tan-cli#261: adds the six oracle `GlobalArgs` flags this command was still
# missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--target`/
# `--verbose`) on top of `--quiet`, already declared and read above; see
# `tan.core.global_flags`. `ctx: typer.Context` (this command's own
# `_HONOURS_ROOT_FORMAT` seam) is untouched -- appended parameters are all
# keyword-only Options, never repositioned relative to it.
debug_config = accept_global_flags(debug_config)
