# SPDX-License-Identifier: Apache-2.0
"""`tan inspect` -- show resolved project/debug context values.

Port of `crates/tan-cli/src/commands/inspect.rs` + the `tan_core::debug`
model it reads (`crates/tan-core/src/debug/{context,inspect}.rs`). Builds the
same six-row "resolved debug context" -- `workspaceRoot`, `sdkRoot`,
`boardYamlPath`, `boardYamlExists`, `westCwd`, `pythonBinary` -- that `tan
trace`/`tan support-bundle` (#257) also need. [`resolve_debug_project_context`]
and [`collect_resolved_values`] below are this port's ONE copy of that model:
`trace_cmd.py` and `support_bundle_cmd.py` import both directly rather than
re-deriving them, mirroring how the Rust `tan_core::debug` module is the one
place all three commands read it from.

**Established by RUNNING the oracle (`target/debug/tan.exe`), not by reading
`crates/`** -- issues #258/#260/#261 all record source-reading alone producing
a wrong answer here twice already. Every shape below (the six rows, their
`source`/`detail` strings, the JSON key order, the mixed-separator `outputPath`
shape `trace`/`support-bundle` share) was measured against a freshly-built
oracle from THIS worktree's `crates/` (`cargo build -p alp-tan-cli --bin tan`),
not a possibly-stale `dev`-branch checkout's binary -- the two disagree on
`Project.boardYaml`'s existence-filtering (tan-cli#236, landed on this
worktree's branch, not yet on `dev`), which is exactly the kind of mismatch
RUNNING catches and reading `crates/` alone would not.

**Which SDK ladder.** `inspect`/`trace` are two of the thirteen commands
`build_cmd.resolve_sdk_root_ladder`'s own docstring names -- measured against
the oracle -- as resolving the LATERAL/narrow ladder (the same one
`doctor_cmd.doctor` uses), not the wide `init`/`generate`/`examples`/`renode`
one. This file calls `resolve_sdk_root_ladder` directly for that reason,
rather than `build_output.resolve_project_context`'s narrower
`resolve_sdk_tiered` (no positional-walk tail) that `size`/`image`/
`debug-config` use for their own, separately-documented reason.

**Always-populated fields.** No `--west-cwd`/`--python-path` flag exists on
this CLI surface at all (the oracle's own `GlobalArgs` carries neither), so
`westCwd` always equals the resolved `workspaceRoot` and `pythonBinary` is
always the per-platform default (`python` on Windows, `python3` elsewhere) --
verified against the oracle: every resolved-values row for these two keys
reports source `setting`/`default` respectively, in every argument shape
tried, never `unresolved`/`setting`-from-an-override. The Rust
`collect_resolved_values`'s "nothing resolved" branches for `workspaceRoot`/
`boardYamlPath`/`westCwd` are therefore dead code in THIS port's construction
(the workspace root and the joined board.yaml path are always non-null once a
command reaches this point) and are not reproduced here.

`tan inspect` itself has no failure exit in the oracle -- verified against
every argument shape tried (missing SDK, missing board.yaml, an empty
`--path`, `--path` matching nothing): always exit 0, with an `issues` entry
carrying the bad news instead.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import _abs_posix, resolve_sdk_root_ladder
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS, sdk_resolution_issues
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ResolvedDebugContext:
    """The resolved project/debug context `inspect`/`trace`/`support-bundle`
    all build from the same four CLI inputs (`--project`/`--board-yaml`/
    `--sdk-root`, plus the always-resolved host defaults). Mirrors the shape
    `tan_core::debug::DebugWorkspaceContext` carries, minus the two fields
    that only mean something inside an IDE extension host
    (`project_selected`/`debugger_extensions`) -- the standalone CLI has no
    reader for either (see `doctor_cmd`'s own note on the same gap)."""

    #: Posix, absolute -- always resolved (cwd, or `--project` joined onto it).
    workspace_root: str
    #: Posix, absolute, or `None` when no alp-sdk checkout resolved.
    sdk_root: str | None
    #: The `SdkSourceTier` string `resolve_sdk_root_ladder` answered with
    #: (`"sdkRootFlag"`/`"projectPin"`/`"globalDefault"`/`"discovery"`/`"none"`).
    sdk_tier: str
    #: Posix, absolute -- the resolved location, joined onto `workspace_root`
    #: unconditionally (mirrors `project.rs::resolve_board_yaml_path`): this is
    #: WHERE a board.yaml would live, whether or not one is actually there.
    board_yaml_path: str
    #: Whether a real file sits at `board_yaml_path` (probed once, here).
    board_yaml_exists: bool
    #: Always equals `workspace_root` -- see the module docstring.
    west_cwd: str
    #: Always the per-platform default -- see the module docstring.
    python_binary: str
    #: The envelope `project` block (`board_yaml` present only when the file
    #: really exists -- `Project.resolved` applies that filter).
    project: Project
    #: The envelope `sdk` block, or `None` when nothing resolved.
    sdk: SdkInfo | None
    #: tan-cli#478: the project `~/.alp/sdk-default` was written FOR, when the
    #: SDK resolved through `globalDefault` and that project is not this one.
    #: `resolve_sdk_root_ladder` has always answered with it; this context
    #: dropped it, so `inspect`/`trace`/`support-bundle` reported
    #: `sourceTier: "globalDefault"` pointing at ANOTHER project's checkout
    #: with `ok: true` and `issues: []` -- the exact silence tan-cli#464 closed
    #: for `doctor`/`generate`/`build`/`presets`/`examples` and no one else.
    #: `None` when the pointer is this project's own, or was never consulted.
    foreign_global_default_for: str | None = None
    #: The unreadable/unresolvable `.alp/sdk-path` pin, same carry-through.
    #: Paired with the field above because `sdk_resolution_issues` emits the
    #: two together, in that order, and a caller holding one but not the other
    #: can only re-open half the hole.
    broken_project_pin: str | None = None


def resolve_debug_project_context(
    project_arg: str | None, board_yaml_arg: str | None, sdk_root_arg: str | None
) -> ResolvedDebugContext:
    """Resolve `--project`/`--board-yaml`/`--sdk-root` into the shared debug
    context. Mirrors `doctor_cmd.doctor`'s own workspace-root/board-yaml/SDK
    preprocessing (same ladder, same `--board-yaml` anchoring rule) rather than
    `build_output.resolve_project_context`'s -- see the module docstring for why
    these two commands take the narrow ladder specifically.
    """
    cwd = Path.cwd()
    workspace_root_path = (
        cwd if project_arg is None else Path(os.path.join(str(cwd), project_arg))
    )
    workspace_root = _abs_posix(str(workspace_root_path))

    configured = board_yaml_arg or "board.yaml"
    if os.path.isabs(configured):
        board_yaml_path = _abs_posix(configured)
    else:
        board_yaml_path = _abs_posix(os.path.join(str(workspace_root_path), configured))
    board_yaml_exists = os.path.isfile(board_yaml_path)

    sdk_resolution = resolve_sdk_root_ladder(sdk_root_arg, workspace_root_path)
    resolved_sdk = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_root = _abs_posix(str(resolved_sdk)) if resolved_sdk is not None else None
    sdk = (
        SdkInfo.from_resolution(sdk_root, sdk_resolution) if sdk_root is not None else None
    )

    python_binary = "python" if os.name == "nt" else "python3"

    return ResolvedDebugContext(
        workspace_root=workspace_root,
        sdk_root=sdk_root,
        sdk_tier=sdk_tier,
        board_yaml_path=board_yaml_path,
        board_yaml_exists=board_yaml_exists,
        west_cwd=workspace_root,
        python_binary=python_binary,
        project=Project.resolved(workspace_root, board_yaml_path),
        sdk=sdk,
        # tan-cli#478: carried, not recomputed. The ladder already answered
        # with both; dropping them here is what left three commands silent
        # about resolving another project's SDK.
        foreign_global_default_for=sdk_resolution.foreign_global_default_for,
        broken_project_pin=sdk_resolution.broken_project_pin,
    )


def collect_resolved_values(context: ResolvedDebugContext) -> list[dict[str, Any]]:
    """The six resolved-value rows, in the oracle's fixed order. Port of
    `tan_core::debug::inspect::collect_resolved_values`, narrowed to the
    branches this port's [`ResolvedDebugContext`] can actually produce -- see
    the module docstring's "Always-populated fields" note."""
    return [
        {
            "key": "workspaceRoot",
            "value": context.workspace_root,
            "source": "workspace",
            "detail": "Resolved project directory (cwd, or --project <dir>).",
        },
        {
            "key": "sdkRoot",
            "value": context.sdk_root,
            "source": "workspace" if context.sdk_root is not None else "unresolved",
            "detail": (
                "Resolved alp-sdk root used for scripts and schemas."
                if context.sdk_root is not None
                # tan-cli#381: named `tan sdk switch <path>`, which refuses in
                # this build (`sdk_cmd._run_not_ported`) -- the #305 dead end,
                # re-introduced by a port written after that fix landed. The
                # unresolved arm is the only one a reader acts on, so it takes
                # the shared NO_SDK_NEXT_STEPS instead of its own wording.
                else f"Unresolved -- {NO_SDK_NEXT_STEPS}."
            ),
        },
        {
            "key": "boardYamlPath",
            "value": context.board_yaml_path,
            "source": "setting",
            "detail": "Resolved board.yaml path (default location, or --board-yaml <path>).",
        },
        {
            "key": "boardYamlExists",
            "value": context.board_yaml_exists,
            "source": "runtime",
            "detail": (
                "board.yaml exists at the resolved path."
                if context.board_yaml_exists
                else "board.yaml is missing at the resolved path."
            ),
        },
        {
            "key": "westCwd",
            "value": context.west_cwd,
            "source": "setting",
            "detail": "Working directory used for west commands.",
        },
        {
            "key": "pythonBinary",
            "value": context.python_binary,
            "source": "default",
            "detail": "Interpreter used for loader and validation scripts.",
        },
    ]


def filter_resolved_values(
    values: list[dict[str, Any]], focus: str | None
) -> list[dict[str, Any]]:
    """`focus is None` passes everything through; otherwise keep a value whose
    `key` equals `focus` or is nested under it (a `focus.` dotted or `focus[`
    indexed prefix) -- mirrors `inspect.rs::filter_resolved_values`."""
    if focus is None:
        return values
    dot = f"{focus}."
    bracket = f"{focus}["
    return [
        v
        for v in values
        if v["key"] == focus or v["key"].startswith(dot) or v["key"].startswith(bracket)
    ]


def _format_value_text(value: Any) -> str:
    """Strings JSON-quoted, everything else its compact JSON form -- mirrors
    Rust's `format_value` (`Value::String` -> `serde_json::to_string`, else
    `Value::to_string()`); `json.dumps` gives the identical rendering for a
    scalar (`true`/`false`/`null`/a bare number) in either language."""
    return json.dumps(value)


def _inspect_text_lines(
    values: list[dict[str, Any]], focus: str | None, show_origin: bool, quiet: bool
) -> list[str]:
    lines = [f"inspect: resolved values={len(values)}"]
    if focus is not None:
        lines.append(f"path={focus}")
    if not quiet:
        for v in values:
            rendered = _format_value_text(v["value"])
            if show_origin:
                lines.append(
                    f"{v['key']}={rendered} source={v['source']} "
                    f"detail={json.dumps(v['detail'])}"
                )
            else:
                lines.append(f"{v['key']}={rendered}")
    return lines


def inspect(
    ctx: typer.Context,
    path: str = typer.Option(
        None, "--path", metavar="PATH", help="Limit output to resolved values under this key path."
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
    show_origin: bool = typer.Option(
        False, "--show-origin", help="Include source + detail metadata for each value."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
) -> None:
    """Inspect resolved project/debug context values.

    `--verbose`/`--no-color`/`--non-interactive`/`--ci`/`--target`/`--all` are
    accepted and ignored: clap makes every one of them `global = true` in the
    oracle, so `tan inspect --ci` (etc.) must not be a Click usage error even
    though `inspect.rs` never reads any of them.
    """
    del verbose, no_color, non_interactive, ci, target, all_targets
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    generated_at = generated_at_iso(millis=True)
    context = resolve_debug_project_context(project, board_yaml, sdk_root)

    # tan-cli#478: FIRST, and unconditionally. `inspect`'s whole job is to say
    # which SDK a build would use, so resolving another project's checkout
    # through `globalDefault` is the single most load-bearing thing it can
    # report -- and it reported the root while saying nothing about whose it
    # was. Same helper, same order, as `size`/`image`/`flash`.
    issues: list[Issue] = sdk_resolution_issues(
        context.broken_project_pin, context.sdk_tier, context.foreign_global_default_for
    )
    if not context.board_yaml_exists:
        issues.append(
            Issue(
                "inspect.board-yaml-missing",
                "warning",
                "board.yaml path could not be resolved or the file does not exist.",
            )
        )

    focus = path
    values = filter_resolved_values(collect_resolved_values(context), focus)
    if focus is not None and not values:
        issues.append(
            Issue(
                "inspect.path-not-found",
                "warning",
                f"No resolved values match --path '{focus}'.",
            )
        )

    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "focusPath": focus,
        "showOrigin": show_origin,
        "resolvedValues": values,
    }

    if not json_mode:
        for line in _inspect_text_lines(values, focus, show_origin, quiet):
            typer.echo(line, err=True)

    if json_mode:
        emit(
            Envelope(
                "inspect", context.project, data, issues, ExitCode.SUCCESS, sdk=context.sdk
            )
        )
    raise typer.Exit(int(ExitCode.SUCCESS))
