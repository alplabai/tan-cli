# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` -- export a diagnostic bundle (inspect + trace +
doctor) to a JSON file for attaching to a bug report.

Port of `crates/tan-cli/src/commands/support_bundle.rs`. Composes three
sections into one written file: the resolved debug context + its resolved
values (`inspect_cmd`'s own model), the generation-trace decisions
(`trace_cmd`'s own model), and a doctor report -- then returns a stdout
envelope naming the written path + a decision count. Exit follows the
oracle's rule verbatim: `DOCTOR_FAILURE` (4) when the bundled doctor
section's `summary.fail > 0` (`support_bundle.rs`: `let exit = if
doctor.summary.fail > 0 { ExitCode::DoctorFailure }`), else `SUCCESS` (0).
The bundle file is still written either way. An unsupported target/server
pairing -> `DOCTOR_FAILURE` (4) too; a bad `--target-kind`/`--server` value
-> `INTERNAL_FAILURE` (5).

**The doctor section is the DEBUG-focused report, not this port's `tan
doctor` checklist** (tan-cli#357). Until that issue this module substituted
`doctor_cmd._collect`'s build/flash-readiness list and pinned the exit at
`SUCCESS`, so a bundle attached to a debug failure carried no debugger state
at all while automation read `ok: true` next to error-severity issues.
Measured against the oracle (empty PATH, no SDK): rust rc=4 ok=false
issues=[`support-bundle.sdkRoot`, `support-bundle.hostPrerequisites`]; the
port answered rc=0 ok=true with six error issues.

[`_debug_doctor_report`] is therefore the port of `tan_core::debug::doctor::
build_doctor_report` + `tan-cli`'s two appends (`append_host_prerequisites`,
`append_host_environment`). The base/target-branch checks are written here
because nothing else in this port produces them -- `support-bundle` is this
port's ONLY debug-report consumer, the debug half of `tan doctor` still
being unported (`doctor_cmd.py`'s own module docstring). The four host
checks are NOT rewritten: they are harvested by name from
`doctor_cmd._collect`, which already owns each one, so a change there
reaches the bundle with no second copy to drift (see
[`_host_checks_from_doctor`]). `--target-kind`/`--server` now genuinely
change the report, exactly as they do in the oracle.

**One harvested check is capped, not passed through raw** (tan-cli#374
finding 1): `doctor_cmd`'s `longPaths` carries a Windows-only `fail` arm
(tan-cli#306) the oracle cannot reach, and feeding it straight into this
command's verdict made a first-run customer host (registry `LongPathsEnabled`
on, no global `.gitconfig`) exit 4 where the oracle exits 0 with no issues,
on every target/server shape. See [`_demote_long_paths_fail`].

REDACTION POLICY (this port's own decision -- the oracle does not redact at
all; verified: a fresh bundle from a freshly-built `target/debug/tan.exe`
writes the literal `Home directory has no spaces: C:\\Users\\<realname>`
straight into the file). Every string value in the WRITTEN FILE -- never the
stdout envelope, whose `data.outputPath` must stay a real, followable path for
the caller that just asked for it -- has every literal occurrence of the
resolved home directory (`%USERPROFILE%` on Windows, `$HOME` elsewhere, in
both its native and its posix-slash spelling) replaced with the placeholder
`<home>`. This is deliberately narrow, not a blanket path-scrubber: it targets
the one concrete PII class this bundle contains today -- the OS account name,
which rides on almost every absolute path the bundle reports (`workspaceRoot`,
`sdkRoot`, every trace `outputPath`/command line, and the doctor section's own
`homePath`-flavoured detail strings) -- while leaving a workspace/SDK root
OUTSIDE the home directory legible on purpose: a maintainer reading an
attached bundle needs the real project layout to diagnose a path problem, and
none of this command's own inputs (tool presence/versions, filesystem facts)
carry a token or credential to redact beyond the account name. See
[`_redact`]/[`_home_variants`].
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

import typer

from tan.commands import doctor_cmd
from tan.commands.inspect_cmd import (
    ResolvedDebugContext,
    collect_resolved_values,
    resolve_debug_project_context,
)
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS, sdk_resolution_issues
from tan.commands.trace_cmd import (
    TraceTargetError,
    build_trace_decisions,
    resolve_trace_targets,
)
from tan.core.debug_launch import (
    BAREMETAL_MCU,
    GDBSERVER,
    JLINK,
    NATIVE_HOST,
    OPENOCD,
    PYOCD,
    SERVER_NONE,
    YOCTO_USERSPACE,
    ZEPHYR_MCU,
    DebugConfigError,
    is_server_supported_for_target,
    parse_server_kind,
    parse_target_kind,
)
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: `data.schemaVersion` for this command's stdout payload, and the bundle
#: file's own top-level + per-section schema versions (all "1", like the
#: oracle).
DATA_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Redaction -- see the module docstring's "REDACTION POLICY".
# ---------------------------------------------------------------------------


def _home_variants() -> tuple[str, ...]:
    """The resolved home directory, in both its native and posix-slash
    spelling -- the bundle mixes both (native in doctor detail strings and
    `--destination`-normalised paths, posix in `workspaceRoot`/`sdkRoot`/
    `boardYamlPath`), so redaction has to look for both. Empty when the host
    has neither `USERPROFILE` nor `HOME` set -- redacts nothing rather than
    guessing."""
    home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    if not home:
        return ()
    return tuple({home, home.replace("\\", "/")})


def _redact(value: Any, home_variants: tuple[str, ...]) -> Any:
    """Recursively replace every literal `home_variants` occurrence in every
    string this bundle payload carries with `<home>`. Walks dicts/lists;
    every other JSON-safe type (bool/int/float/None) passes through
    unchanged."""
    if isinstance(value, str):
        for variant in home_variants:
            value = value.replace(variant, "<home>")
        return value
    if isinstance(value, dict):
        return {k: _redact(v, home_variants) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, home_variants) for v in value]
    return value


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _timestamp_for_file(generated_at: str) -> str:
    """Makes an ISO timestamp filename-safe: `:`/`.` -> `-`. Mirrors the
    oracle's `timestamp_for_file`."""
    return "".join("-" if c in ":." else c for c in generated_at)


def _create_bundle_trace_decisions(
    context: ResolvedDebugContext, target: str | None, focus: str | None
) -> list[dict[str, Any]]:
    """The bundle's own trace section: `Planned` decisions when an SDK root
    resolved, else one `Failed` placeholder -- port of `support_bundle.rs`'s
    `create_bundle_trace_decisions`.

    Deliberately checks ONLY `context.sdk_root is not None`, never
    `board_yaml_exists` -- measured against the oracle: a project with a
    resolved SDK but a MISSING board.yaml still gets four `Planned` decisions
    here (`decisionCount: 4`), unlike bare `tan trace`, which refuses outright
    on a missing board.yaml. `workspace_root`/`board_yaml_path` need no
    presence check of their own in this port: both are unconditionally
    resolved by [`resolve_debug_project_context`] (see that module's
    docstring), exactly mirroring why the oracle's own three-way `Option`
    match only ever turns on `sdk_root`.
    """
    if context.sdk_root is None:
        decisions: list[dict[str, Any]] = [
            {
                "key": "generation.targets",
                "outcome": "failed",
                "detail": (
                    "Generation targets were not traced because project context "
                    "is unresolved."
                ),
            }
        ]
    else:
        targets = resolve_trace_targets(target)  # may raise TraceTargetError
        decisions = build_trace_decisions(
            context.workspace_root,
            context.sdk_root,
            context.board_yaml_path,
            context.python_binary,
            targets,
            None,  # the focus-path decision is appended once, below
        )

    if focus is not None:
        decisions.append(
            {
                "key": f"config.path.{focus}",
                "outcome": "planned",
                "detail": (
                    "Path-level trace was requested and captured as part of "
                    "bundle metadata."
                ),
            }
        )
    return decisions


#: The `DebuggerExtensionsState` the standalone binary reports, verbatim from
#: `crates/tan-cli/src/commands/doctor.rs::standalone_debugger_extensions`.
#: The three flags are an inherited assumption from the VS Code extension --
#: only an extension host can enumerate its own marketplace extensions -- and
#: `observable: false` is what stops anything reading them as facts: every
#: derived check renders `unknown` instead of claiming "vadimcn.vscode-lldb is
#: installed." on a headless container (#102).
STANDALONE_DEBUGGER_EXTENSIONS = {
    "cortexDebug": True,
    "cppTools": True,
    "codeLLDB": True,
    "observable": False,
}

#: Per-server executable candidates, first hit on PATH wins -- port of
#: `tan_core::debug::context::collect_runtime_capabilities_from_commands`. The
#: reported value is the COMMAND NAME, not the resolved path: that is what the
#: oracle puts in the check detail (`.map(|c| (*c).to_string())`).
_RUNTIME_EXECUTABLES: dict[str, tuple[str, ...]] = {
    JLINK: ("JLinkGDBServerCL", "JLinkGDBServer"),
    OPENOCD: ("openocd",),
    PYOCD: ("pyocd",),
    GDBSERVER: ("gdb", "arm-none-eabi-gdb"),
    SERVER_NONE: ("lldb-dap", "lldb"),
}

#: The host checks the oracle appends to the pure report AFTER building it
#: (`append_host_prerequisites` then `append_host_environment`), in that
#: order. `bootstrapManifest` has no oracle counterpart as a CHECK -- Rust
#: folds a rejected `metadata/bootstrap.json` into `hostPrerequisites`'
#: own detail via `manifest_error`, this port reports it as a sibling warn --
#: so it rides here rather than being dropped: a bundle whose prerequisite
#: list came from tan's fallback instead of the SDK's manifest must say so.
#: `longPaths` is Windows-only and simply absent elsewhere. Its `fail` status
#: is demoted before it ever reaches this command's verdict -- see
#: [`_demote_long_paths_fail`].
_HOST_CHECK_ORDER = (
    "bootstrapManifest",
    "hostPrerequisites",
    "zephyrSdkAvailableForHost",
    "longPaths",
    "homePath",
)


def _demote_long_paths_fail(check: doctor_cmd.Check) -> doctor_cmd.Check:
    """tan-cli#374 finding 1. tan-cli#306 widened `doctor_cmd.long_paths_check`
    with a Windows-only `fail` arm (registry `LongPathsEnabled` on, git's own
    `core.longpaths` not) that the oracle cannot reach at all: Rust's
    `long_paths_check` (`crates/tan-core/src/host_env.rs:300-330`) takes only
    the registry axis and never returns worse than `Warn` -- confirmed by
    RUNNING the oracle, not by reading it. Every OTHER `longPaths` state
    (`warn`/`pass`) already matches the oracle's own verdict for that state
    and is left alone; only this one, oracle-unreachable combination is
    downgraded.

    Feeding the undemoted check into this command's verdict was exactly the
    bug: on a first-run customer host (registry on, no global `.gitconfig`,
    so git's own flag is unset) it made `support-bundle` exit 4 -- doctor
    failure -- where the oracle exits 0 with no issues at all, on every
    target/server shape. `tan doctor` KEEPS the fail arm unchanged (that is
    #306's real fix, for the command that actually gates a build and where
    "fail fast on a certain west-update break" is the right call); this
    command only ever attaches a debug snapshot, and its own acceptance bar
    is matching the oracle's axes, which do not include this one.

    Demoting here (not filtering the check out of the report) keeps the
    written bundle, `doctor.summary`, `nextSteps` and the wire verdict all
    reading the SAME check list -- a maintainer opening the file still sees
    the real git/registry mismatch (the `detail`/`fix` text is untouched),
    just at the severity this command can actually promise the oracle
    agrees with.
    """
    if check.name == "longPaths" and check.status == "fail":
        return replace(check, status="warn")
    return check


def _first_on_path(names: tuple[str, ...]) -> str | None:
    return next((name for name in names if doctor_cmd.on_path(name) is not None), None)


def _extension_check(name: str, extension_id: str) -> doctor_cmd.Check:
    """One VS Code extension-presence check, always `unknown` here -- port of
    `doctor.rs::extension_check`'s `None` arm, which is the only arm the
    standalone binary can reach (`observable: false`). Not `warn`: a warning
    says something is wrong, and nothing is -- the question was not askable.
    `unknown` is counted in no summary bucket and raises no issue, so it can
    never move this command's exit code.

    The dash in `detail` is a real U+2014 EM DASH, not `--` (tan-cli#374
    finding 6): the oracle's own literal
    (`crates/tan-core/src/debug/doctor.rs:132`) uses one, and this string is a
    verbatim port of it -- unlike this file's own prose/comments, which use
    `--` throughout, an emitted user-facing detail string copies its source of
    truth byte for byte."""
    return doctor_cmd.Check(
        name,
        "unknown",
        f"{extension_id}: unknown — the standalone tan binary cannot see "
        "VS Code's installed extensions.",
    )


def _host_checks_from_doctor(
    context: ResolvedDebugContext, project_arg: str | None, board_yaml_arg: str | None
) -> list[doctor_cmd.Check]:
    """The host half of the bundled report, harvested BY NAME from
    `doctor_cmd._collect` rather than re-probed here.

    The oracle appends these with `append_host_prerequisites` /
    `append_host_environment`, both `pub(crate)` in `tan-cli` precisely so
    `support_bundle.rs` can share `doctor.rs`'s copy. This port has the same
    four checks -- `prerequisites_check`, `zephyr_sdk_host_check`,
    `long_paths_check`, `home_path_check`, plus `bootstrapManifest` (see
    `_HOST_CHECK_ORDER`) -- but their IO (manifest load,
    effective-Python-floor arithmetic, registry read) is inline in `_collect`,
    so calling `_collect` and picking the checks out is what keeps ONE copy of
    each. Re-probing them here would be ~40 duplicated lines that drift the
    first time `tan doctor` changes a verdict.

    KNOWN CEILING: `_collect` runs the WHOLE build/flash-readiness checklist
    (west/JLink/SETOOLS probes included) to yield these five. It is the same
    call this command already made before tan-cli#357, so the cost is
    unchanged and the discarded checks are exactly the ones the oracle's
    bundle never carried -- but if the probe cost ever matters, the fix is a
    `doctor_cmd.host_environment_checks()` seam both callers share, not a
    second copy of the probes here.

    `board_yaml` is passed only when explicitly given (`--board-yaml`) or the
    file really exists, mirroring `doctor_cmd.doctor`'s own preprocessing rule
    -- irrelevant to the five checks kept here, but passing a path that does
    not exist would misreport `_collect`'s own `boardYaml`, and a future
    reader harvesting one more name should not inherit a lie.
    """
    board_yaml_for_doctor = (
        context.board_yaml_path
        if (board_yaml_arg is not None or context.board_yaml_exists)
        else None
    )
    collected = doctor_cmd._collect(
        context.sdk_root,
        board_yaml=board_yaml_for_doctor,
        project_scope=project_arg,
        workspace_root=context.workspace_root,
        sdk_tier=context.sdk_tier,
    )
    by_name = {check.name: check for check in collected}
    return [
        _demote_long_paths_fail(by_name[name])
        for name in _HOST_CHECK_ORDER
        if name in by_name
    ]


def _debug_doctor_report(
    context: ResolvedDebugContext,
    project_arg: str | None,
    board_yaml_arg: str | None,
    project_selected: bool,
    target: str,
    server: str,
) -> tuple[dict[str, Any], list[doctor_cmd.Check]]:
    """The bundle's DEBUG-focused doctor section (tan-cli#357) -- port of
    `tan_core::debug::doctor::build_doctor_report` plus `tan-cli`'s two host
    appends. See the module docstring for what it replaced and why.

    No `serverCompatibility` check: the oracle's builder emits one and then
    short-circuits, but `support_bundle.rs` refuses an unsupported pairing
    with its own `support-bundle.server-compatibility` issue BEFORE building
    any report (`_server_incompatible` here does the same), so that arm is
    unreachable from this command. Verified against the oracle:
    `--target-kind yocto-userspace --server jlink` writes no bundle at all.

    `workspaceRoot` has no fail arm for the same reason `inspect_cmd`'s
    resolved-values rows have none: `resolve_debug_project_context` always
    resolves a workspace root (cwd, or `--project` joined onto it), so Rust's
    `is_present(&context.workspace_root)` is always true in this port's
    construction. Left as a reported check because the oracle reports it and
    a bundle reader looks for it.
    """
    checks = [
        doctor_cmd.Check("workspaceRoot", "pass", context.workspace_root),
        doctor_cmd.Check(
            "sdkRoot",
            "pass" if context.sdk_root else "fail",
            context.sdk_root or "No alp-sdk checkout resolved.",
            None
            if context.sdk_root
            # tan-cli#381: the `fix` said "Run `tan sdk switch <path>`", which
            # refuses in this build (`sdk_cmd._run_not_ported`) -- and a support
            # bundle is read by whoever is already stuck, so a fix line naming a
            # command that exits 1 is the worst place for the #305 dead end to
            # come back. `doctor_cmd.sdk_check`'s own wording, shared verbatim.
            else f"Resolve an alp-sdk checkout: {NO_SDK_NEXT_STEPS}.",
        ),
        _board_yaml_check(context, project_selected),
        *_target_checks(target, server),
        *_host_checks_from_doctor(context, project_arg, board_yaml_arg),
    ]
    missing_prerequisites = next(
        (c.missing for c in checks if c.name == "hostPrerequisites"), None
    )
    report = {
        "generatedAt": None,  # filled by the caller, which already has the timestamp
        "targetKind": target,
        "server": server,
        "summary": doctor_cmd.summarise(checks),
        "checks": [c.as_dict() for c in checks],
        "nextSteps": doctor_cmd.next_steps(checks),
        "missingPrerequisites": missing_prerequisites,
    }
    return report, checks


def _board_yaml_check(
    context: ResolvedDebugContext, project_selected: bool
) -> doctor_cmd.Check:
    """Port of `doctor.rs::board_yaml_check` -- the DEBUG report's `boardYaml`,
    which reports the resolved PATH as its detail (this port's build-checklist
    `board_yaml_preflight_check` reports "board.yaml found" instead, and stays
    where it is).

    A missing `board.yaml` is a hard failure only once the user NAMED a
    project: with neither `--project` nor `--board-yaml` the path is a guess
    at the working directory, and `tan bootstrap` sends every new customer to
    run `tan doctor` from the SDK checkout root, which has no `board.yaml` and
    needs none (#100).
    """
    if context.board_yaml_exists:
        return doctor_cmd.Check("boardYaml", "pass", context.board_yaml_path)
    if project_selected:
        return doctor_cmd.Check(
            "boardYaml",
            "fail",
            context.board_yaml_path,
            "Create board.yaml or pass `--board-yaml <path>`.",
        )
    return doctor_cmd.Check(
        "boardYaml",
        "warn",
        f"no project selected -- no board.yaml at {context.board_yaml_path}",
        "Select a project with `--project <dir>` (or `--board-yaml <path>`) to check one.",
    )


def _target_checks(target: str, server: str) -> list[doctor_cmd.Check]:
    """The per-target-kind arm of `build_doctor_report`: one extension check
    plus one tool check, both driven by `--target-kind`/`--server`.

    `lldb` always passes, with no `fix` (#131): `vadimcn.vscode-lldb` ships
    its own complete LLDB inside the extension directory and never consults
    PATH, so warning that none was found and telling the user to install one
    is a remedy for a tool the product does not need. The resolved executable
    is still REPORTED when present -- that is real information; only the
    verdict and the advice were wrong.
    """
    if target in (ZEPHYR_MCU, BAREMETAL_MCU):
        found = _first_on_path(_RUNTIME_EXECUTABLES[server])
        return [
            _extension_check("cortexDebugExtension", "marus25.cortex-debug"),
            doctor_cmd.Check(
                f"{server}Backend",
                "pass" if found else "warn",
                found or f"No {server} executable was found on PATH.",
                None if found else f"Install {server} and make sure it is on PATH.",
            ),
        ]
    if target == YOCTO_USERSPACE:
        gdb = _first_on_path(_RUNTIME_EXECUTABLES[GDBSERVER])
        return [
            _extension_check("cppToolsExtension", "ms-vscode.cpptools"),
            doctor_cmd.Check(
                "gdb",
                "pass" if gdb else "warn",
                gdb or "No local gdb executable was found on PATH.",
                None if gdb else "Install gdb locally for symbolized remote debugging.",
            ),
        ]
    lldb = _first_on_path(_RUNTIME_EXECUTABLES[SERVER_NONE])
    return [
        _extension_check("codeLLDBExtension", "vadimcn.vscode-lldb"),
        doctor_cmd.Check(
            "lldb",
            "pass",
            lldb or "vadimcn.vscode-lldb ships its own LLDB, so none is needed on PATH.",
        ),
    ]


def _doctor_issues(checks: list[doctor_cmd.Check]) -> list[Issue]:
    """Warn/fail checks become `support-bundle.<kebab-name>` issues -- port of
    `support_bundle.rs::doctor_checks_to_issues`, with THIS port's own
    `doctor_cmd.Check` shape (`name`/`status`/`detail`) instead of Rust's
    `DoctorCheck`. `unknown` raises nothing: the question was not askable,
    not a problem."""
    return [
        Issue(
            f"support-bundle.{doctor_cmd.kebab_check_name(c.name)}",
            "error" if c.status == "fail" else "warning",
            c.detail,
        )
        for c in checks
        if c.status in ("warn", "fail")
    ]


def _write_bundle(
    destination: str | None,
    workspace_root: str,
    generated_at: str,
    payload: dict[str, Any],
) -> str:
    """Write the redacted `payload` to a timestamped
    `debug-support-bundle-*.json` file under `destination` (resolved against
    cwd, like the oracle's `normalize_path(cwd.join(dest))`) or
    `<workspace_root>/.alp-support`. Returns the written path."""
    file_name = f"debug-support-bundle-{_timestamp_for_file(generated_at)}.json"
    base_dir = (
        os.path.abspath(destination)
        if destination is not None
        else os.path.join(workspace_root, ".alp-support")
    )
    output_path = os.path.join(base_dir, file_name)
    os.makedirs(base_dir, exist_ok=True)
    redacted = _redact(payload, _home_variants())
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        json.dump(redacted, handle, indent=2)
        handle.write("\n")
    return output_path


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    exit_code: ExitCode
    data: dict[str, Any]
    project: Project
    sdk: SdkInfo | None
    issues: list[Issue]
    text: list[str]
    #: Whether `--verbose` may append its "use --format json" hint under text
    #: mode -- only the bundle-written path does (`support_bundle_text` in the
    #: oracle); the three failure shapes (`internal_failure`/
    #: `server_incompatible`, plus this port's outer exception guard) have
    #: their own fixed text and never grow a verbose-only line. Verified: `tan
    #: support-bundle --target-kind yocto-userspace --server jlink --verbose`
    #: prints only the one incompatibility line, no hint.
    verbose_hint_eligible: bool = False


def _empty_data(generated_at: str, target: str, server: str) -> dict[str, Any]:
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "outputPath": "",
        "targetKind": target,
        "server": server,
        "decisionCount": 0,
    }


def _internal_failure(
    generated_at: str, message: str, target: str, server: str, sdk: SdkInfo | None
) -> _Outcome:
    return _Outcome(
        exit_code=ExitCode.INTERNAL_FAILURE,
        data=_empty_data(generated_at, target, server),
        project=Project(root=None, board_yaml=None),
        sdk=sdk,
        issues=[Issue("support-bundle.internal-failure", "error", message)],
        text=["support-bundle: internal failure", message],
    )


def _server_incompatible(
    generated_at: str, target: str, server: str, sdk: SdkInfo | None
) -> _Outcome:
    message = f"Server '{server}' is not supported for target '{target}'."
    return _Outcome(
        exit_code=ExitCode.DOCTOR_FAILURE,
        data=_empty_data(generated_at, target, server),
        project=Project(root=None, board_yaml=None),
        sdk=sdk,
        issues=[Issue("support-bundle.server-compatibility", "error", message)],
        text=[f"support-bundle: server '{server}' is not supported for target '{target}'."],
    )


def _run(
    *,
    project_arg: str | None,
    board_yaml_arg: str | None,
    sdk_root_arg: str | None,
    target_kind_arg: str | None,
    server_arg: str | None,
    path_arg: str | None,
    target_arg: str | None,
    destination_arg: str | None,
) -> _Outcome:
    """The whole command as a pure-ish computation (project/SDK resolution and
    the bundle-file WRITE are the only IO) returning one outcome. Mirrors
    `debug_config_cmd._run`'s split: nothing here emits or exits, so the
    caller's exception guard can wrap this call without swallowing
    `typer.Exit`."""
    generated_at = generated_at_iso(millis=True)
    context = resolve_debug_project_context(project_arg, board_yaml_arg, sdk_root_arg)

    try:
        target = parse_target_kind(target_kind_arg)
        server = parse_server_kind(server_arg)
    except DebugConfigError as err:
        return _internal_failure(
            generated_at, str(err), NATIVE_HOST, SERVER_NONE, context.sdk
        )

    if not is_server_supported_for_target(target, server):
        return _server_incompatible(generated_at, target, server, context.sdk)

    try:
        decisions = _create_bundle_trace_decisions(context, target_arg, path_arg)
    except TraceTargetError as err:
        return _internal_failure(generated_at, str(err), target, server, context.sdk)

    # Port of `doctor.rs::project_selected`: `--project`/`--board-yaml` are the
    # whole selection surface, so with neither given the resolved board.yaml
    # path is a guess at the cwd, not a request.
    project_selected = any(
        (arg or "").strip() for arg in (project_arg, board_yaml_arg)
    )
    doctor_report, checks = _debug_doctor_report(
        context, project_arg, board_yaml_arg, project_selected, target, server
    )
    doctor_report["generatedAt"] = generated_at

    notes = [
        f"targetKind={target}",
        f"server={server}",
        f"workspaceRoot={context.workspace_root}",
    ]
    payload = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "inspect": {
            "schemaVersion": DATA_SCHEMA_VERSION,
            "generatedAt": generated_at,
            # The oracle's `DebugWorkspaceContext`, key for key (tan-cli#357).
            # `projectSelected`/`debuggerExtensions` are NOT fabricated: the
            # first is derived from this invocation's own flags (see
            # `project_selected` above, and `doctor.rs::project_selected`), the
            # second is the standalone binary's own declared, self-describing
            # state -- `observable: false` says in the file itself that the
            # three flags were never probed. Omitting them cost the bundle the
            # two facts that explain WHY its `boardYaml` and extension checks
            # read the way they do, in the artifact a debug failure gets
            # attached to.
            "context": {
                "generatedAt": generated_at,
                "workspaceRoot": context.workspace_root,
                "sdkRoot": context.sdk_root,
                "boardYamlPath": context.board_yaml_path,
                "westCwd": context.west_cwd,
                "pythonBinary": context.python_binary,
                "boardYamlExists": context.board_yaml_exists,
                "projectSelected": project_selected,
                "debuggerExtensions": STANDALONE_DEBUGGER_EXTENSIONS,
            },
            "resolvedValues": collect_resolved_values(context),
        },
        "trace": {
            "schemaVersion": DATA_SCHEMA_VERSION,
            "generatedAt": generated_at,
            "workflow": "cli.support-bundle",
            "decisions": decisions,
        },
        "doctor": doctor_report,
        "notes": notes,
        # tan-cli#478's headline requirement, and the item review called worth
        # fixing FIRST: the FILE has to carry it, not just the stdout envelope.
        # An earlier revision computed these AFTER `_write_bundle` below, so
        # the bundle a user attaches to a bug report -- the whole point of the
        # command -- still answered False to the issue's own repro:
        #   'global-default-foreign-project' in open(BUNDLE).read()
        # Its own `doctor` section could not have supplied it either: that set
        # keeps host checks only (tan-cli#441), 8 where a standalone doctor
        # emits ~17.
        "sdkResolution": [
            {"code": issue.code, "severity": issue.severity, "message": issue.message}
            for issue in sdk_resolution_issues(
                context.broken_project_pin,
                context.sdk_tier,
                context.foreign_global_default_for,
            )
        ],
    }

    try:
        output_path = _write_bundle(destination_arg, context.workspace_root, generated_at, payload)
    except OSError as err:
        return _internal_failure(generated_at, str(err), target, server, context.sdk)

    # tan-cli#357: the doctor summary IS this command's verdict, exactly as in
    # the oracle (`if doctor.summary.fail > 0 { ExitCode::DoctorFailure }`).
    # `exit_code_for` is `any(status == "fail")` over the same list, so it is
    # that rule spelled in this port's vocabulary -- and it keeps the CLI-wide
    # invariant `process exit code == envelope.exitCode == (not ok)` intact,
    # which the previous hardcoded SUCCESS broke: automation saw `ok: true`
    # and exit 0 beside error-severity issues in the same envelope. The bundle
    # file is still written on the failing path -- it is what the user
    # attaches, and a doctor failure is the reason they are attaching it.
    # tan-cli#478: the SDK-resolution pair FIRST, then the doctor rollup.
    # This bundle is what a user sends to explain a broken machine, and it
    # was the only place the foreign-default warning never appeared. The
    # embedded doctor set would not have covered it either -- it keeps host
    # checks only (tan-cli#441): 8, where a standalone doctor emitted ~17.
    issues = sdk_resolution_issues(
        context.broken_project_pin, context.sdk_tier, context.foreign_global_default_for
    )
    issues.extend(_doctor_issues(checks))
    exit_code = doctor_cmd.exit_code_for(checks)

    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "outputPath": output_path,
        "targetKind": target,
        "server": server,
        "decisionCount": len(decisions),
    }
    text = [
        f"support-bundle: exported {output_path}",
        f"support-bundle: trace decisions={len(decisions)}",
    ]
    return _Outcome(
        exit_code=exit_code,
        data=data,
        project=context.project,
        sdk=context.sdk,
        issues=issues,
        text=text,
        verbose_hint_eligible=True,
    )


def support_bundle(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to current directory)."
    ),
    target_kind: str = typer.Option(
        None,
        "--target-kind",
        metavar="KIND",
        help="Debug target class (zephyr-mcu, baremetal-mcu, yocto-userspace, native-host).",
    ),
    board_yaml: str = typer.Option(
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path (overrides project resolution).",
    ),
    server: str = typer.Option(
        None,
        "--server",
        metavar="SERVER",
        help="Debug server backend (jlink, openocd, pyocd, gdbserver, none).",
    ),
    path: str = typer.Option(
        None, "--path", metavar="PATH", help="Limit generation tracing to this config key path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    destination: str = typer.Option(
        None,
        "--destination",
        metavar="DESTINATION",
        help="Output directory for the bundle (default: <workspace>/.alp-support).",
    ),
    target: str = typer.Option(
        None,
        "--target",
        metavar="EMIT",
        help="Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).",
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    verbose: bool = typer.Option(
        False, "--verbose", help="Emit additional diagnostic detail."
    ),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
) -> None:
    """Export a diagnostic support bundle (inspect + trace + doctor).

    `--all` is accepted and ignored, same as `tan trace`. `--quiet`/
    `--no-color`/`--non-interactive`/`--ci` are `global = true` clap options
    `support_bundle.rs` never reads.
    """
    del quiet, no_color, non_interactive, ci, all_targets
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    try:
        outcome = _run(
            project_arg=project,
            board_yaml_arg=board_yaml,
            sdk_root_arg=sdk_root,
            target_kind_arg=target_kind,
            server_arg=server,
            path_arg=path,
            target_arg=target,
            destination_arg=destination,
        )
    except Exception as err:  # noqa: BLE001 -- see debug_config_cmd's identical guard
        outcome = _internal_failure(
            generated_at_iso(millis=True),
            f"support-bundle failed unexpectedly: {err.__class__.__name__}: {err}",
            NATIVE_HOST,
            SERVER_NONE,
            None,
        )

    if not json_mode:
        # tan-cli#478 review finding 6: the bundle FILE and the JSON envelope
        # both carry `sdkResolution`/the foreign-default pair now, but
        # `outcome.text` (every `_Outcome` constructor above) never did --
        # the default `tan support-bundle` with no `--format` stayed silent.
        # Read off `outcome.issues` rather than recomputed from `outcome.sdk`:
        # `resolve_debug_project_context`'s `context.sdk` is a bare
        # `SdkInfo(sdk_root, sdk_tier)` (the legacy site
        # `test_sdk_info_is_built_from_a_resolution.py` ledgers for
        # `inspect_cmd.py`, shared by every caller of that resolver), so it
        # carries neither `foreign_global_default_for` nor
        # `broken_project_pin` -- recomputing from it would silently print
        # nothing. `_run`'s success path already builds `issues` from
        # `context`'s own (correctly carried) fields; this only ever
        # re-prints what is already there.
        for issue in outcome.issues:
            if issue.code.startswith("sdk."):
                typer.echo(issue.message, err=True)
        for line in outcome.text:
            typer.echo(line, err=True)
        if verbose and outcome.verbose_hint_eligible:
            typer.echo(
                "support-bundle: include --format json for machine-readable envelopes.",
                err=True,
            )

    if json_mode:
        emit(
            Envelope(
                "support-bundle",
                outcome.project,
                outcome.data,
                outcome.issues,
                outcome.exit_code,
                sdk=outcome.sdk,
            )
        )
    raise typer.Exit(int(outcome.exit_code))
