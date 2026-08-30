# SPDX-License-Identifier: Apache-2.0
"""`tan trace` -- report the generation decisions a build would make.

Port of `crates/tan-cli/src/commands/trace.rs` + the `tan_core::loader`/
`tan_core::debug::trace` pieces it reads. For each of the four per-core
build-config emit targets (`zephyr-conf`, `dts-overlay`, `cmake-args`,
`yocto-conf` -- [`BUILD_CONFIG_EMIT_MODES`]), report the loader command line +
output path a `tan build` slice would run. Requires a resolved SDK root and an
existing `board.yaml`; refuses otherwise (exit 2) rather than reporting decisions
for a project that cannot actually build.

**Deliberately the narrower build-config set, not the full `tan generate`
surface (tan-cli#165 review finding 1).** `tan trace` reports "the generation
decisions a build would make" (this file's own name for the command); a build
only ever materialises these four -- `carrier-netlist`/`native-sim-overlay`/
`west-libraries`/`hw-info-h`/`os-topology` are real `tan generate --target`
outputs a build never runs. [`BUILD_CONFIG_EMIT_MODES`] is this port's own
copy of `tan_core::loader::BUILD_CONFIG_EMIT_MODES`, not
`generate_cmd.ALL_EMIT_MODES`.

**The output-path/command-line shape is measured, not guessed.** Rust's
`create_loader_plan` joins the workspace root onto the target's relative path
with exactly ONE `Path::join` call (never split component-wise), which on
Windows inserts a single native separator before an otherwise-untouched
forward-slash literal -- `C:/proj\\build/generated/alp.conf`, not
`C:\\proj\\build\\generated\\alp.conf`. Verified: `os.path.join(workspace_root,
"build/generated/alp.conf")` reproduces this byte-for-byte on Windows (and is a
no-op difference on POSIX, where `/` is already native), so [`_loader_plan`]
below uses a single `os.path.join` call per component the Rust also joins
separately (`sdk_root`, then `"scripts"`, then `"alp_project.py"` -- two
`.join()` calls, i.e. two inserted separators), rather than
`generate_cmd._output_path`'s fully-native, fully-split form -- the two
commands' oracle behaviour genuinely differs here, not just their code shape.

`resolve_debug_project_context`/the six-row debug context are `inspect_cmd`'s;
this file imports them rather than re-deriving -- both commands, and
`support-bundle`, must read one context, or the same project could resolve
three different ways across the three commands.
"""

from __future__ import annotations

import os
from typing import Any

import typer

from tan.commands.inspect_cmd import resolve_debug_project_context
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.sdk_discovery import sdk_resolution_issues
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The output path (relative to the workspace root, `/`-separated -- an SDK
#: convention, not a host path) for each build-config emit target. Mirrors
#: `tan_core::loader::GENERATION_TARGET_CATALOG`'s four `BUILD_CONFIG_EMIT_
#: MODES` entries verbatim; NOT re-derived from `generate_cmd._OUTPUT_
#: RELATIVE_PATH` because [`_loader_plan`]'s join semantics differ from
#: `generate_cmd._output_path`'s (see the module docstring) even though the
#: four literal strings are identical today.
_OUTPUT_RELATIVE_PATH: dict[str, str] = {
    "zephyr-conf": "build/generated/alp.conf",
    "dts-overlay": "build/generated/alp.overlay",
    "cmake-args": "build/generated/alp-cmake-args.txt",
    "yocto-conf": "build/generated/alp-yocto.conf",
}

#: The four per-core build-config targets a `tan build` slice actually
#: materialises -- the set `tan trace`/`tan support-bundle` enumerate by
#: default and validate an explicit `--target` against. Order is the oracle's
#: own catalog order, pinned: `data.decisions` reports in this sequence.
BUILD_CONFIG_EMIT_MODES: tuple[str, ...] = (
    "zephyr-conf",
    "dts-overlay",
    "cmake-args",
    "yocto-conf",
)


class TraceTargetError(Exception):
    """An unsupported `--target` value; `str()` is the user-facing message,
    verbatim from the oracle."""


def resolve_trace_targets(raw: str | None) -> tuple[str, ...]:
    """`None` (no `--target`) -> all four, in catalog order. A known target ->
    that one alone. `--all` is deliberately NOT a parameter here: the oracle's
    `resolve_targets` never reads it either -- verified (`--target X --all`
    still narrows to `X`; `--all` alone matches the bare-no-target default)."""
    if raw is None:
        return BUILD_CONFIG_EMIT_MODES
    if raw in BUILD_CONFIG_EMIT_MODES:
        return (raw,)
    raise TraceTargetError(
        f"Unsupported trace target '{raw}'. Allowed values: "
        f"{', '.join(BUILD_CONFIG_EMIT_MODES)}."
    )


def _loader_plan(
    workspace_root: str, sdk_root: str, board_yaml_path: str, python_binary: str, emit_target: str
) -> tuple[str, str]:
    """`(output_path, command_line)` for one emit target -- port of
    `tan_core::loader::create_loader_plan`. See the module docstring for why
    the two `os.path.join` calls below must stay exactly this shape."""
    output_path = os.path.join(workspace_root, _OUTPUT_RELATIVE_PATH[emit_target])
    script_path = os.path.join(sdk_root, "scripts", "alp_project.py")
    command_line = (
        f"{python_binary} {script_path} --input {board_yaml_path} --emit {emit_target} "
        f"--output {output_path}"
    )
    return output_path, command_line


def build_trace_decisions(
    workspace_root: str,
    sdk_root: str,
    board_yaml_path: str,
    python_binary: str,
    targets: tuple[str, ...],
    focus: str | None,
) -> list[dict[str, Any]]:
    """One `Planned` decision per target, plus one more when `focus` (`--path`)
    is set -- port of `trace.rs::run`'s decision-building loop. Shared with
    `support_bundle_cmd`, whose bundled trace section is this exact list."""
    decisions: list[dict[str, Any]] = []
    for emit_target in targets:
        output_path, command_line = _loader_plan(
            workspace_root, sdk_root, board_yaml_path, python_binary, emit_target
        )
        decisions.append(
            {
                "key": f"generation.target.{emit_target}",
                "outcome": "planned",
                "outputPath": output_path,
                "detail": f"Would run: {command_line}",
            }
        )
    if focus is not None:
        decisions.append(
            {
                "key": f"config.path.{focus}",
                "outcome": "planned",
                "detail": (
                    "Path-level tracing is currently static and reports planning "
                    "context only."
                ),
            }
        )
    return decisions


def _trace_text_lines(decisions: list[dict[str, Any]], quiet: bool) -> list[str]:
    lines = [f"trace: decisions={len(decisions)}"]
    if not quiet:
        for d in decisions:
            lines.append(f"[{d['outcome']}] {d['key']}: {d['detail']}")
    return lines


def trace(
    ctx: typer.Context,
    path: str = typer.Option(
        None, "--path", metavar="PATH", help="Limit tracing to this config key path."
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
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    target: str = typer.Option(
        None,
        "--target",
        metavar="EMIT",
        help="Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).",
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
) -> None:
    """Trace the generation decisions a build would make.

    `--all` is accepted and ignored -- see [`resolve_trace_targets`]. The other
    hidden flags are `global = true` clap options `trace.rs` never reads.
    """
    del verbose, no_color, non_interactive, ci, all_targets
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    generated_at = generated_at_iso(millis=True)
    focus = path
    context = resolve_debug_project_context(project, board_yaml, sdk_root)

    def empty_data(target_value: str | None) -> dict[str, Any]:
        return {
            "schemaVersion": DATA_SCHEMA_VERSION,
            "generatedAt": generated_at,
            "workflow": "cli.trace",
            "focusPath": focus,
            "target": target_value,
            "decisions": [],
        }

    def _resolution_issues() -> list[Issue]:
        """tan-cli#478, computed once and prepended on BOTH exits. `trace`'s
        output names the `alp_project.py` a build would run; when that path
        came from another project's `~/.alp/sdk-default` the envelope said so
        nowhere. A failure path needs it as much as the success one -- more,
        even: "no SDK resolved" and "resolved SOMEONE ELSE'S" read identically
        without it."""
        return sdk_resolution_issues(
            context.broken_project_pin, context.sdk_tier, context.foreign_global_default_for
        )

    def fail(exit_code: ExitCode, code: str, message: str, data: dict, text_lines: list[str]) -> None:
        resolution_issues = _resolution_issues()
        issues = [*resolution_issues, Issue(f"trace.{code}", "error", message)]
        if not json_mode:
            # tan-cli#478 review finding 6: `_resolution_issues()` fed only the
            # JSON envelope below -- the DEFAULT text path printed `text_lines`
            # and nothing else, so `tan trace` with no `--format` stayed silent
            # about resolving another project's checkout, the literal repro
            # #478 opened with ("prints the alp_project.py a build would run
            # from the *other* project's checkout, no warning").
            for issue in resolution_issues:
                typer.echo(issue.message, err=True)
            for line in text_lines:
                typer.echo(line, err=True)
        if json_mode:
            emit(
                Envelope(
                    "trace",
                    Project(root=None, board_yaml=None),
                    data,
                    issues,
                    exit_code,
                    sdk=context.sdk,
                )
            )
        raise typer.Exit(int(exit_code))

    if context.sdk_root is None:
        fail(
            ExitCode.VALIDATION_FAILURE,
            "sdk-root-unresolved",
            # tan-cli#381: this port landed AFTER #305 centralised the advice
            # and hardcoded `tan sdk switch` again -- a subcommand that refuses
            # outright here (`sdk_cmd._run_not_ported`, `sdk.not-ported`). Same
            # shape as `generate_cmd`/`model_cmd`: keep the two mechanisms that
            # do work (`--sdk-root`, a checkout beside the project), take the
            # third from NO_SDK_NEXT_STEPS so it cannot drift again.
            "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
            f"alp-sdk checkout, or {NO_SDK_NEXT_STEPS}.",
            empty_data(target),
            ["trace: alp-sdk root is unresolved."],
        )

    if not context.board_yaml_exists:
        fail(
            ExitCode.VALIDATION_FAILURE,
            "board-yaml-missing",
            "board.yaml path could not be resolved or the file does not exist.",
            empty_data(target),
            ["trace: board.yaml path is unresolved or missing."],
        )

    try:
        targets = resolve_trace_targets(target)
    except TraceTargetError as err:
        # Mirrors the oracle's catch-block data: `target: null`, unlike the two
        # guard failures above (which echo the raw `--target` back).
        fail(
            ExitCode.INTERNAL_FAILURE,
            "internal-failure",
            str(err),
            empty_data(None),
            ["trace: internal failure", str(err)],
        )
        return  # pragma: no cover -- fail() always raises typer.Exit

    decisions = build_trace_decisions(
        context.workspace_root,
        context.sdk_root,
        context.board_yaml_path,
        context.python_binary,
        targets,
        focus,
    )
    resolved_target = targets[0] if len(targets) == 1 else None

    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "workflow": "cli.trace",
        "focusPath": focus,
        "target": resolved_target,
        "decisions": decisions,
    }

    if not json_mode:
        # tan-cli#478 review finding 6, success path: same silence as `fail()`
        # above, unconditional (unlike the `--quiet`-suppressed decision
        # lines) and printed first.
        for issue in _resolution_issues():
            typer.echo(issue.message, err=True)
        for line in _trace_text_lines(decisions, quiet):
            typer.echo(line, err=True)

    if json_mode:
        emit(
            Envelope(
                "trace",
                context.project,
                data,
                _resolution_issues(),
                ExitCode.SUCCESS,
                sdk=context.sdk,
            )
        )
    raise typer.Exit(int(ExitCode.SUCCESS))
