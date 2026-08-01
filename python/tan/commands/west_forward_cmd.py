# SPDX-License-Identifier: Apache-2.0
"""`tan migrate` / `tan lock` / `tan quality` -- the three `west`-delegating
extension commands that survive ADR-0020 Phase 4 (`west alp-build` itself was
retired; `tan build` is native now -- see `build_cmd.py`'s module doc). Port of
`crates/tan-cli/src/commands/build/workspace.rs::run` (the oracle's own,
now-narrowed, "legacy west forward" entry) and its `west_argv`/
`west_workspace_dir`/`west_program` helpers.

Every flag NOT declared on the command below (`--core`, `--sequential`, `-b
<board>`, `--port COM7`, `--cfsr 0x...`, ...) belongs to the underlying `west
alp-<verb>` extension command, not to `tan` -- it is forwarded verbatim,
mirroring the oracle's own `[ARGS]...` catch-all
(`crates/tan-cli/src/cli.rs`'s `WestForwardArgs`). `ignore_unknown_options` in
each command's `context_settings` is what lets an unrecognised flag fall
through to that catch-all instead of Click rejecting it outright.

Text mode inherits stdio -- `alp-migrate`/`alp-lock`/`alp-quality` may prompt
or stream their own progress, exactly like the oracle's `west`-delegating path
(its own module doc: "Text mode inherits stdio so the build streams live").
JSON mode captures the child's output and reports ONE envelope; per the
oracle's own `BuildData`, `data` names the `west` command run, its cwd, and the
forwarded args -- never the child's own stdout, which for an interactive tool
cannot be captured without breaking it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from tan.commands.bootstrap_cmd import _manifest_points_at
from tan.commands.build_output import resolve_project_context, to_posix
from tan.core.venv import west_program, with_venv_on_path
from tan.envelope import Envelope, Issue, emit
from tan.exit_codes import ExitCode

#: Passed to `app.command(...)` for each forwarder below: lets an unrecognised
#: flag (the underlying `west alp-*` command's own surface) fall through to the
#: `args` catch-all instead of Click's usual "no such option" usage error.
FORWARD_CONTEXT_SETTINGS = {"ignore_unknown_options": True}


def _west_workspace_dir(start: str, sdk_root: Path | None) -> Path | None:
    """Port of `workspace.rs::west_workspace_dir`: the directory holding
    `.west/` that a `west alp-*` extension command must run from -- these are
    discovered only from a workspace manifest, never the app dir alone.
    Checked in order: the project tree upward, `$ZEPHYR_BASE/..`
    (manifest-verified against `sdk_root` when one resolved), then the
    SDK-derived layouts (`<sdk-parent>` and the legacy
    `<sdk-parent>/zephyrproject`). `None` when nothing resolves -- the caller
    then keeps the old app-dir cwd, matching the oracle exactly.
    """

    def is_workspace(d: Path) -> bool:
        return (d / ".west").is_dir()

    directory: Path | None = Path(start)
    while directory is not None:
        if is_workspace(directory):
            return directory
        parent = directory.parent
        directory = parent if parent != directory else None

    zephyr_base = os.environ.get("ZEPHYR_BASE")
    if zephyr_base:
        found = _zephyr_base_workspace(Path(zephyr_base), sdk_root)
        if found is not None:
            return found

    if sdk_root is not None:
        parent = sdk_root.parent
        for workspace in (parent, parent / "zephyrproject"):
            if is_workspace(workspace):
                return workspace

    return None


def _zephyr_base_workspace(zephyr_base: Path, sdk_root: Path | None) -> Path | None:
    """Step 2 of `_west_workspace_dir`: port of `workspace.rs::zephyr_base_workspace`.
    `zephyr_base`'s PARENT, but only when it is both a west workspace AND (when
    `sdk_root` resolved) its manifest is verifiably alp-sdk's -- a stock/unrelated
    Zephyr checkout's parent is a west workspace by the bare `.west`-dir test
    alone, yet carries no alp-sdk extension commands at all. `sdk_root` absent
    means there is nothing to verify against, so the old unconditional accept
    stands rather than refusing a workspace this call can't check.
    """
    workspace = zephyr_base.parent
    if not (workspace / ".west").is_dir():
        return None
    if sdk_root is not None and not _manifest_points_at(workspace, sdk_root):
        return None
    return workspace


def _west_argv(subcommand: str, passthrough: list[str]) -> list[str]:
    """Port of `workspace.rs::west_argv`: `alp-<subcommand>` followed by the
    forwarded args, verbatim."""
    return [f"alp-{subcommand}", *passthrough]


def _launch_error(err: OSError) -> str:
    """Port of `workspace.rs::west_launch_error`."""
    if isinstance(err, FileNotFoundError):
        return "west not found on PATH — run `tan bootstrap` and ensure west is on PATH."
    return f"failed to launch west: {err}"


def _run_forward(
    subcommand: str,
    passthrough: list[str],
    project: str | None,
    board_yaml: str | None,
    sdk_root: str | None,
    output_format: str,
) -> None:
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    context = resolve_project_context(project, board_yaml, sdk_root)
    west_cwd = context.workspace_root or "."
    sdk_path = Path(context.sdk.root) if context.sdk is not None else None

    west_bin = west_program(west_cwd, str(sdk_path) if sdk_path is not None else None)
    workspace = _west_workspace_dir(west_cwd, sdk_path)
    # `to_posix`: `str(PurePath)` renders with the platform separator (backslash
    # on Windows), but Rust's `Path::to_string_lossy` merely slices the already
    # forward-slash `west_cwd` string -- never re-rendering it -- so the oracle's
    # `data.westCwd` stays forward-slash on every platform. Without this the
    # port's envelope breaks the "platform-identical path" handshake contract
    # (`tan_core::project::to_posix`) the very first time a workspace resolves.
    run_cwd = to_posix(str(workspace)) if workspace is not None else to_posix(west_cwd)

    argv = _west_argv(subcommand, passthrough)
    # Mirrors the oracle: when a workspace resolved and the caller gave no
    # positional of their own (every forwarded token starts with `-`), insert
    # the resolved app path as the `alp-*` command's required positional.
    if workspace is not None and not any(not a.startswith("-") for a in passthrough):
        app_path = str(Path(west_cwd).resolve())
        argv.insert(1, app_path)

    west_command = argv[0]
    data = {
        "schemaVersion": "1",
        "westCommand": west_command,
        "westCwd": run_cwd,
        "args": list(passthrough),
    }
    project_ = context.project()
    env = with_venv_on_path(dict(os.environ), west_bin)
    full_argv = [west_bin, *argv]

    if json_mode:
        try:
            out = subprocess.run(
                full_argv,
                cwd=run_cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as err:
            exit_code = ExitCode.RUNTIME_FAILURE
            issues = [Issue(f"{subcommand}.failed", "error", _launch_error(err))]
        else:
            if out.returncode == 0:
                exit_code = ExitCode.SUCCESS
                issues = []
            else:
                exit_code = ExitCode.RUNTIME_FAILURE
                issues = [
                    Issue(
                        f"{subcommand}.failed",
                        "error",
                        f"`west {west_command}` failed; re-run without --format json "
                        "to see the log.",
                    )
                ]
        emit(Envelope(subcommand, project_, data, issues, exit_code, sdk=context.sdk))
        raise typer.Exit(int(exit_code))

    try:
        result = subprocess.run(full_argv, cwd=run_cwd, env=env, check=False)
    except OSError as err:
        typer.echo(f"{subcommand}: {_launch_error(err)}", err=True)
        raise typer.Exit(int(ExitCode.RUNTIME_FAILURE)) from err
    if result.returncode == 0:
        typer.echo(f"{subcommand}: complete.", err=True)
        raise typer.Exit(0)
    typer.echo(f"{subcommand}: `west {west_command}` failed (see log above).", err=True)
    raise typer.Exit(int(ExitCode.RUNTIME_FAILURE))


def migrate(
    args: list[str] = typer.Argument(None, metavar="ARGS..."),
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
) -> None:
    """Migrate board.yaml to the current schema (`west alp-migrate`)."""
    _run_forward("migrate", list(args or []), project, board_yaml, sdk_root, output_format)


def lock(
    args: list[str] = typer.Argument(None, metavar="ARGS..."),
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
) -> None:
    """Pin/lock library dependencies (`west alp-lock`)."""
    _run_forward("lock", list(args or []), project, board_yaml, sdk_root, output_format)


def quality(
    args: list[str] = typer.Argument(None, metavar="ARGS..."),
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
) -> None:
    """Run the board.yaml quality checks (`west alp-quality`)."""
    _run_forward("quality", list(args or []), project, board_yaml, sdk_root, output_format)
