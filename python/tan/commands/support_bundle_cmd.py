# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` -- export a diagnostic bundle (inspect + trace +
doctor) to a JSON file for attaching to a bug report.

Port of `crates/tan-cli/src/commands/support_bundle.rs`. Composes three
sections into one written file: the resolved debug context + its resolved
values (`inspect_cmd`'s own model), the generation-trace decisions
(`trace_cmd`'s own model), and a doctor report -- then returns a stdout
envelope naming the written path + a decision count. Exit follows the doctor
summary: any `fail` check -> `DOCTOR_FAILURE` (4); an unsupported
target/server pairing -> `DOCTOR_FAILURE` too; a bad `--target-kind`/`--server`
value -> `INTERNAL_FAILURE` (5).

**The doctor section is this port's `tan doctor` verdict, not the oracle's.**
The Rust `support_bundle.rs` embeds `build_doctor_report` -- a SEPARATE,
debug-flavoured check list (`workspaceRoot`/`sdkRoot`/`boardYaml`/
`codeLLDBExtension`/`lldb`/`hostPrerequisites`(old flavour)/
`zephyrSdkAvailableForHost`/`longPaths`/`homePath`) that ONLY `support-bundle`
and the not-yet-ported debug half of `tan doctor` itself produce; it is
distinct from the build/flash-readiness check list `doctor_cmd._collect`
implements (`sdk`/`boardYaml`/`workspace`/`westResolved`/`zephyrSdk`/
`hostPrerequisites`(this port's flavour)/`setools`/`jlink`/...), which is what
THIS port's `tan doctor` produces and the only doctor logic this port owns.
Per this unit's own instructions, that logic is reused here verbatim via
`doctor_cmd._collect`/`summarise`/`exit_code_for`/`next_steps` -- never
copied or re-implemented -- so this bundle's `doctor` section reports the
SAME facts a `tan doctor` run against the same project would, under
`support-bundle.<name>`-coded issues instead of `doctor.<name>`-coded ones.
This is a deliberate, known divergence from the oracle's own bundled doctor
section, not an oversight: building a THIRD, debug-flavoured check list here
would duplicate checks doctor_cmd.py already owns in a different shape --
exactly the drift this whole architecture exists to avoid -- and
`doctor_cmd.py`'s own module docstring already names the debug half as
"needs context this port has no command to produce yet" (written before this
unit existed). `--target-kind`/`--server` are still parsed and validated
exactly like the oracle (`is_server_supported_for_target`); they simply do not
change which checks the bundled doctor section runs, because this port's
`_collect` never branches on either.

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
from dataclasses import dataclass
from typing import Any

import typer

from tan.commands import doctor_cmd
from tan.commands.inspect_cmd import (
    ResolvedDebugContext,
    collect_resolved_values,
    resolve_debug_project_context,
)
from tan.commands.trace_cmd import (
    TraceTargetError,
    build_trace_decisions,
    resolve_trace_targets,
)
from tan.core.debug_launch import (
    DebugConfigError,
    is_server_supported_for_target,
    parse_server_kind,
    parse_target_kind,
)
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

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


def _doctor_section(
    context: ResolvedDebugContext,
    project_arg: str | None,
    board_yaml_arg: str | None,
    target: str,
    server: str,
) -> tuple[dict[str, Any], list[doctor_cmd.Check]]:
    """This bundle's doctor section, reusing `doctor_cmd`'s own build/flash-
    readiness checks verbatim -- see the module docstring for why this
    diverges from the oracle's debug-flavoured one.

    `board_yaml` is passed to `_collect` only when it was either explicitly
    given (`--board-yaml`) or really exists -- mirroring `doctor_cmd.doctor`'s
    own preprocessing rule (`_collect`'s docstring: "the only way it is
    non-`None` while its file does NOT exist is an explicitly-given
    `--board-yaml`"), so `boardYaml`'s `project_selected` severity split reads
    the same signal a real `tan doctor` invocation would.
    """
    board_yaml_for_doctor = (
        context.board_yaml_path
        if (board_yaml_arg is not None or context.board_yaml_exists)
        else None
    )
    checks = doctor_cmd._collect(
        context.sdk_root,
        board_yaml=board_yaml_for_doctor,
        project_scope=project_arg,
        workspace_root=context.workspace_root,
        sdk_tier=context.sdk_tier,
    )
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


def _doctor_issues(checks: list[doctor_cmd.Check]) -> list[Issue]:
    """Warn/fail checks become `support-bundle.<name>` issues -- port of
    `support_bundle.rs::doctor_checks_to_issues`, with THIS port's own
    `doctor_cmd.Check` shape (`name`/`status`/`detail`) instead of Rust's
    `DoctorCheck`. `unknown` raises nothing: the question was not askable,
    not a problem."""
    return [
        Issue(
            f"support-bundle.{c.name}",
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
            generated_at, str(err), target_kind_arg or "native-host", server_arg or "none", context.sdk
        )

    if not is_server_supported_for_target(target, server):
        return _server_incompatible(generated_at, target, server, context.sdk)

    try:
        decisions = _create_bundle_trace_decisions(context, target_arg, path_arg)
    except TraceTargetError as err:
        return _internal_failure(generated_at, str(err), target, server, context.sdk)

    doctor_report, checks = _doctor_section(
        context, project_arg, board_yaml_arg, target, server
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
            # A reduced form of the oracle's `DebugWorkspaceContext`: the six
            # fields this port actually resolves. `projectSelected`/
            # `debuggerExtensions` are omitted -- both are IDE-extension-host
            # concepts (`tan_core::debug::DebuggerExtensionsState`) with no
            # standalone-CLI reader anywhere in this port (see
            # `inspect_cmd.ResolvedDebugContext`'s own docstring), so
            # fabricating a value for either would be an invented, not a
            # resolved, fact.
            "context": {
                "generatedAt": generated_at,
                "workspaceRoot": context.workspace_root,
                "sdkRoot": context.sdk_root,
                "boardYamlPath": context.board_yaml_path,
                "westCwd": context.west_cwd,
                "pythonBinary": context.python_binary,
                "boardYamlExists": context.board_yaml_exists,
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
    }

    try:
        output_path = _write_bundle(destination_arg, context.workspace_root, generated_at, payload)
    except OSError as err:
        return _internal_failure(generated_at, str(err), target, server, context.sdk)

    issues = _doctor_issues(checks)
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
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
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
    resolved_format = (
        output_format if output_format is not None else (ctx.obj or {}).get("format") or "text"
    )
    if resolved_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{resolved_format}' (choose from 'text', 'json')", param_hint="--format"
        )
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
            target_kind or "native-host",
            server or "none",
            None,
        )

    if not json_mode:
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
