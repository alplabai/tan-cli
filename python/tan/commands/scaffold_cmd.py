# SPDX-License-Identifier: Apache-2.0
"""`tan scaffold` -- scaffold one module (a source/header pair + README) into
an EXISTING tan project. Distinct from `tan init`, which scaffolds a whole new
project: this command never touches `board.yaml`, never resolves an SDK, and
its template id space (`tan.core.module_template.MODULE_TEMPLATE_IDS`) is a
different, smaller registry than `tan init`'s own six.

Composition, not logic: resolve the module name + template + destination
(`--name`/`--template`/`--destination`, and `tan.core.module_template`'s
registry), ask it for the planned three files, diff them against disk
(`tan.core.scaffold.collect_file_changes`), then preview / guard / write --
folding whatever comes back into exactly one envelope. Mirrors
`crates/tan-cli/src/commands/scaffold.rs`.

**`--name` is REQUIRED, with NO non-interactive default.** Unlike `tan init`'s
`--template` (defaults to `zephyr-app`) or `--name` (defaults to an empty
subdirectory), a module scaffold has no sane default name -- so the
non-interactive contract is a REFUSAL (`scaffold.name-required`, exit 2),
never a default (`crates/tan-cli/src/commands/scaffold.rs`'s own history,
CHANGELOG.md's #187-follow-up entry: "its non-interactive contract is a
refusal, not a default, since a module name has no sane one"). `--template`
DOES have a non-interactive default (`sensor-driver`, the registry's first
entry) when omitted.

**Interactivity mirrors the oracle's `GlobalArgs::can_prompt()`
(`crates/tan-cli/src/cli.rs`) exactly**: may only prompt when NOT
`--non-interactive`, NOT `--ci`, NOT `--format json`, AND both stdin and
stderr are real terminals. The last two matter more than they look: a
prompting library renders to stderr and reads through the controlling
terminal, so `stdin=tty, stderr=piped` -- every wrapper that captures output
while inheriting the terminal -- still cannot be prompted safely and must
refuse rather than hang. No CI runner, and no `pytest` subprocess, has a TTY,
so `--name`/`--template` are effectively always required in an automated run;
this is intentional (the module docstring for `can_prompt` documents the same
for the Rust binary: "no CI runner has a TTY"). The interactive fallback here
uses `click.prompt`/`click.Choice` rather than a `Select`/`Text` TUI widget,
the same simplification `tan.commands.new_som_cmd` already made and documents
(no arrow-key menu dependency for a path automated tests never exercise).

Every failure is a coded issue, never a traceback -- the backstop at the
bottom of [`scaffold`] converts any unexpected exception into
`scaffold.internal-failure` rather than letting it escape, matching
`init_cmd`/`debug_config_cmd`'s own catch-all.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import click
import typer

from tan.core.consent import can_prompt
from tan.core.module_template import (
    DEFAULT_MODULE_TEMPLATE_ID,
    MODULE_TEMPLATE_IDS,
    create_module_scaffold_plan,
)
from tan.core.scaffold import FileChange, PlannedFile, ScaffoldWriteError, collect_file_changes
from tan.core.scaffold import scaffold_tree_preview as _tree_preview
from tan.core.scaffold import write_files
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


class ScaffoldError(Exception):
    """A failure with its issue code and exit code already decided -- the
    ONE exception type every resolution/planning/write step in [`scaffold`]
    raises, mirrors `init_cmd.InitError`'s reason for existing: a single
    exception class lets the whole computation run inside ONE `try`, with the
    error-shaped envelope built exactly once, after it, in a SIBLING `except`
    clause. Calling an emit-and-`typer.Exit` helper from a handler that is
    itself still lexically nested INSIDE that same `try` does not work --
    `typer.Exit` subclasses `RuntimeError`, so raising it from a nested
    `except ScaffoldWriteError:` block is still within the outer try's
    dynamic extent and gets re-caught by the outer `except Exception:`
    backstop, turning a clean exit 3 into a misreported `scaffold.internal-
    failure` at exit 5 (caught by this port's own oracle-diff smoke test,
    not a golden -- there is no committed fixture for this shape).

    `partial` carries the files that DID land when a write failed part-way:
    `written: []` for a module half-written to disk would contradict the
    filesystem, the same reasoning `InitError.partial` documents.
    """

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: ExitCode,
        *,
        partial: tuple[list[str], list[str]] = ([], []),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.partial = partial


@dataclass
class _Outcome:
    """A completed (non-error) run: preview, overwrite-guard refusal, or
    write. Built and returned rather than emitted in place, so the exception
    guard in [`scaffold`] can wrap the whole computation without also
    catching `typer.Exit`."""

    template_id: str
    module_name: str
    normalized_name: str
    destination: str
    preview: bool
    file_changes: list[FileChange]
    files: list[PlannedFile]
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    exit_code: ExitCode = ExitCode.SUCCESS
    issue: Issue | None = None


# ---------------------------------------------------------------------------
# Interactivity
# ---------------------------------------------------------------------------


def _need_name() -> ScaffoldError:
    return ScaffoldError(
        "scaffold.name-required",
        "Module name is required. Use --name <name> or run interactively.",
        ExitCode.VALIDATION_FAILURE,
    )


def _cancelled() -> ScaffoldError:
    return ScaffoldError("scaffold.cancelled", "Cancelled.", ExitCode.RUNTIME_FAILURE)


def _resolve_module_name(name: str | None, interactive: bool) -> str:
    if name is not None:
        return name
    if not interactive:
        raise _need_name()
    try:
        # `err=True`: `click.prompt`'s own default (`err=False`) writes the
        # question to stdout, which corrupts whatever this run is piping or
        # redirecting there and leaves a real terminal on stderr showing
        # nothing to answer (tan-cli#496 defect 6, measured against the
        # frozen oracle: `inquire::Text` renders to stderr).
        raw = click.prompt("Module name", err=True)
    except click.exceptions.Abort as err:
        raise _cancelled() from err
    stripped = raw.strip()
    if not stripped:
        raise _need_name()
    return stripped


def _resolve_template(template: str | None, interactive: bool) -> str:
    if template is not None:
        if template not in MODULE_TEMPLATE_IDS:
            raise ScaffoldError(
                "scaffold.invalid-template",
                f"Unknown module template '{template}'.",
                ExitCode.VALIDATION_FAILURE,
            )
        return template
    if not interactive:
        return DEFAULT_MODULE_TEMPLATE_ID
    try:
        # See `_resolve_module_name`'s `err=True` note -- same defect.
        return click.prompt(
            "Select a module template", type=click.Choice(MODULE_TEMPLATE_IDS), err=True
        )
    except click.exceptions.Abort as err:
        raise _cancelled() from err


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


def _data(
    *,
    template_id: str,
    module_name: str,
    normalized_module_name: str,
    destination: str,
    preview: bool,
    file_changes: list[FileChange],
    written: list[str],
    unchanged: list[str],
) -> dict:
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "templateId": template_id,
        "moduleName": module_name,
        "normalizedModuleName": normalized_module_name,
        "destination": destination,
        "preview": preview,
        "fileChanges": [{"relativePath": c.relative_path, "kind": c.kind} for c in file_changes],
        "written": written,
        "unchanged": unchanged,
    }


_EMPTY_DATA_FIELDS = {
    "template_id": "",
    "module_name": "",
    "normalized_module_name": "",
    "destination": "",
    "preview": False,
    "file_changes": [],
}


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _emit_error(json_mode: bool, err: ScaffoldError) -> None:
    """An error before (or during) a write: `project.root: null`, every
    plan-shaped field empty. `written`/`unchanged` are usually empty too, EXCEPT
    on a part-way write failure (`err.partial`), where they carry whatever
    landed before it -- reporting `written: []` there would contradict the
    filesystem. Mirrors `error_run`/`write_error_run` in the Rust
    (`scaffold.rs`), which the wire shape is otherwise identical between.
    """
    written, unchanged = err.partial
    if json_mode:
        emit(
            Envelope(
                "scaffold",
                Project(root=None, board_yaml=None),
                _data(**_EMPTY_DATA_FIELDS, written=written, unchanged=unchanged),
                [Issue(err.code, "error", err.message)],
                err.exit_code,
            )
        )
    else:
        _stderr(f"scaffold: {err.message}")
    raise typer.Exit(int(err.exit_code))


def _emit_outcome(json_mode: bool, outcome: _Outcome) -> None:
    project = Project(root=outcome.destination, board_yaml=None)
    if json_mode:
        emit(
            Envelope(
                "scaffold",
                project,
                _data(
                    template_id=outcome.template_id,
                    module_name=outcome.module_name,
                    normalized_module_name=outcome.normalized_name,
                    destination=outcome.destination,
                    preview=outcome.preview,
                    file_changes=outcome.file_changes,
                    written=outcome.written,
                    unchanged=outcome.unchanged,
                ),
                [outcome.issue] if outcome.issue is not None else [],
                outcome.exit_code,
            )
        )
    elif outcome.preview:
        _stderr(
            f"scaffold: preview for module '{outcome.normalized_name}' "
            f"(template '{outcome.template_id}')"
        )
        # `_tree_preview` already ends in one `\n` (one per listed path); NOT
        # stripped -- `_stderr`'s own `print()` adds a second, matching the
        # oracle's own two-`CommandRun.text` -> `println!` shape byte-for-byte
        # (measured: `tan scaffold --preview` ends the tree with `\n\n`).
        _stderr(_tree_preview(outcome.files))
    elif outcome.exit_code != ExitCode.SUCCESS:
        # The overwrite guard. Deliberately NOT `outcome.issue.message` --
        # the Rust's text-mode line here is a separate, shorter, hardcoded
        # string (`scaffold.rs`'s guard block), not the JSON issue message
        # ("One or more files would be overwritten. Use --force to allow
        # updates.") rendered with a prefix; measured against the oracle.
        _stderr("scaffold: would overwrite existing files; use --force to proceed.")
    else:
        _stderr(
            f"scaffold: created module '{outcome.normalized_name}' "
            f"(template '{outcome.template_id}')"
        )
        _stderr(f"  written: {len(outcome.written)}, unchanged: {len(outcome.unchanged)}")
    raise typer.Exit(int(outcome.exit_code))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def scaffold(
    ctx: typer.Context,
    template: str = typer.Option(
        None,
        "--template",
        metavar="TEMPLATE",
        help=f"Module template id ({', '.join(MODULE_TEMPLATE_IDS)}).",
    ),
    name: str = typer.Option(
        None, "--name", metavar="NAME", help="Module name (required)."
    ),
    destination: str = typer.Option(
        None,
        "--destination",
        metavar="DESTINATION",
        help="Destination project root (default: current directory or --project).",
    ),
    preview: bool = typer.Option(
        False, "--preview", help="Show planned files without writing anything."
    ),
    force: bool = typer.Option(
        False, "--force", help="Allow overwriting existing files."
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
    all_targets: bool = typer.Option(
        False, "--all", help="Run command against all relevant targets."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    verbose: bool = typer.Option(False, "--verbose", help="Emit additional diagnostic detail."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable ANSI color in text output."
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; fail instead of asking when a required value is missing.",
    ),
    ci: bool = typer.Option(
        False, "--ci", help="CI mode: implies non-interactive and disables color."
    ),
) -> None:
    """Scaffold a module into an existing project."""
    # `--board-yaml`/`--sdk-root`/`--target`/`--all`/`--verbose`/`--quiet`/
    # `--no-color` are members of the oracle's clap `GlobalArgs` (`global =
    # true`), so the real `tan scaffold` parses and lists all of them in
    # `--help` -- but `crates/tan-cli/src/commands/scaffold.rs::run` reads
    # only `g.project` and `g.can_prompt()` (non_interactive/ci/format).
    # Declared (not `hidden=True`) so `tan scaffold --help` matches the
    # oracle's own listing; genuinely unread otherwise, matching `init_cmd`'s
    # identical block for its own five ignored globals.
    del board_yaml, sdk_root, target, all_targets, verbose, quiet, no_color

    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"
    interactive = can_prompt(non_interactive=non_interactive, ci=ci, json_mode=json_mode)

    try:
        module_name = _resolve_module_name(name, interactive)
        template_id = _resolve_template(template, interactive)

        dest = destination if destination else (project if project else ".")
        project_root = Path(dest)

        try:
            plan = create_module_scaffold_plan(template_id, module_name)
        except ValueError as err:
            # Re-raised as `ScaffoldError`, never emitted from here directly:
            # this `except` is still lexically INSIDE the outer `try` below,
            # so a `typer.Exit` raised from an emit helper called here would
            # be re-caught by this same function's own `except Exception`
            # backstop (`typer.Exit` subclasses `RuntimeError`) -- see
            # `ScaffoldError`'s own docstring for the mechanism and how this
            # was actually caught (an oracle-diff smoke test, not a golden).
            raise ScaffoldError(
                "scaffold.invalid-name", str(err), ExitCode.VALIDATION_FAILURE
            ) from err

        changes = collect_file_changes(project_root, plan.files)
        has_updates = any(c.kind == "update" for c in changes)

        if preview:
            # Before the overwrite guard, deliberately -- a preview touches no
            # disk, so it has nothing to be guarded against (same ordering
            # `tan init` learned the hard way; see `tan.core.scaffold`'s
            # `write_files` docstring for the sibling incident).
            outcome = _Outcome(
                template_id=template_id,
                module_name=module_name,
                normalized_name=plan.normalized_name,
                destination=dest,
                preview=True,
                file_changes=changes,
                files=plan.files,
            )
        elif has_updates and not force:
            outcome = _Outcome(
                template_id=template_id,
                module_name=module_name,
                normalized_name=plan.normalized_name,
                destination=dest,
                preview=False,
                file_changes=changes,
                files=plan.files,
                exit_code=ExitCode.WRITE_FAILURE,
                issue=Issue(
                    "scaffold.would-overwrite",
                    "error",
                    "One or more files would be overwritten. Use --force to allow updates.",
                ),
            )
        else:
            try:
                result = write_files(project_root, plan.files)
            except ScaffoldWriteError as err:
                raise ScaffoldError(
                    "scaffold.write-failed",
                    f"Failed to write files: {err}",
                    ExitCode.WRITE_FAILURE,
                    partial=(err.partial.written, err.partial.unchanged),
                ) from err
            outcome = _Outcome(
                template_id=template_id,
                module_name=module_name,
                normalized_name=plan.normalized_name,
                destination=dest,
                preview=False,
                file_changes=changes,
                files=plan.files,
                written=result.written,
                unchanged=result.unchanged,
            )
    except ScaffoldError as err:
        _emit_error(json_mode, err)
        return
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # `typer.Exit` cannot reach here: it is only ever raised from
        # `_emit_error`, called from the SIBLING `except ScaffoldError` clause
        # above -- outside this try's dynamic extent, so it propagates
        # straight out rather than looping back into this handler.
        _emit_error(
            json_mode,
            ScaffoldError(
                "scaffold.internal-failure",
                f"scaffold failed unexpectedly: {err.__class__.__name__}: {err}",
                ExitCode.INTERNAL_FAILURE,
            ),
        )
        return

    _emit_outcome(json_mode, outcome)
