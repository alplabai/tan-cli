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
JSON mode captures the child's output and reports ONE envelope, and that
envelope now CARRIES what it captured (`data.stdout`/`data.stderr`/
`data.westExitCode`) -- see `_run_forward` for the two DELIBERATE DIVERGENCES
from `crates/tan-cli/src/commands/build/workspace.rs` this module holds.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from tan.commands.build_output import resolve_project_context, to_posix
from tan.core.venv import west_program, west_workspace_dir, with_venv_on_path
from tan.envelope import Envelope, Issue, emit
from tan.exit_codes import ExitCode

#: Passed to `app.command(...)` for each forwarder below: lets an unrecognised
#: flag (the underlying `west alp-*` command's own surface) fall through to the
#: `args` catch-all instead of Click's usual "no such option" usage error.
FORWARD_CONTEXT_SETTINGS = {"ignore_unknown_options": True}

#: Kept as the underscore-named alias this module (and its own tests) always
#: called it -- the implementation moved to `tan.core.venv.west_workspace_dir`
#: (tan-cli#289 review) so `flash_cmd.py` shares it instead of importing a
#: private name across modules.
_west_workspace_dir = west_workspace_dir


def _west_argv(subcommand: str, passthrough: list[str]) -> list[str]:
    """Port of `workspace.rs::west_argv`: `alp-<subcommand>` followed by the
    forwarded args, verbatim."""
    return [f"alp-{subcommand}", *passthrough]


#: The two forwarded commands that declare a `--board <path>` of their own --
#: alp-sdk `scripts/west_commands/alp_migrate.py` (`--check`/`--preview`/
#: `--apply`/`--all`/`--board`/`--no-verify`) and `alp_lock.py`
#: (`--check`/`--workspace`/`--board`). `alp-quality` declares
#: `--profile`/`--json`/`--junit`/`--sarif` and NOTHING that names a project,
#: so it is deliberately absent: it needs no targeting and would reject the
#: flag outright (tan-cli#391).
_TAKES_BOARD = frozenset({"migrate", "lock"})

#: Forwarded tokens that mean the caller already chose the target, so this
#: module must not choose one for them. `--board` in either spelling is the
#: obvious one; `--all` matters because `alp_migrate.py` puts `--all` and
#: `--board` in ONE `add_mutually_exclusive_group()`, so adding `--board` on
#: top of an explicit `--all` turns a valid invocation into a usage error.
_SELF_TARGETING = frozenset({"--board", "--all"})


def _targets_itself(passthrough: list[str]) -> bool:
    return any(a in _SELF_TARGETING or a.startswith("--board=") for a in passthrough)


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
    # DELIBERATE DIVERGENCE 1 from `workspace.rs::run` (tan-cli#391). The
    # oracle inserts the resolved app path as `argv[1]` whenever every
    # forwarded token starts with `-` (`workspace.rs:292-298`). NOTHING
    # accepts that positional any more: `west alp-build` was its only valid
    # target and ADR-0020 Phase 4 retired it, so on the three surviving verbs
    # the child answers `west alp-migrate: error: unexpected arguments:
    # ['<project>']` and exits 1 -- on EVERY invocation of `tan migrate` and
    # `tan lock`, whose entire argument surface is flags. Parity here
    # preserved already-broken behaviour, so the fix has to DIVERGE:
    # `tests/parity/test_oracle_parity.py`'s `test_west_forward_*` case
    # compares whole envelopes against the frozen oracle and must be
    # re-declared as a known divergence, the shape its siblings there use
    # (`test_size_missing_manifest_is_a_known_divergence_from_the_oracle`).
    #
    # Deleting the injection is not enough on its own: `alp-*` are extension
    # commands discovered from the workspace manifest, so they run with cwd
    # set to the west TOPDIR, and `alp_migrate.py::_targets`' no-flag fallback
    # is `Path("board.yaml").resolve()` -- the workspace's board.yaml, not the
    # project's. Name the project through the flag those two actually declare.
    # `context.board_yaml` already folds in `--project`/`--board-yaml` and is
    # posix-absolute (the envelope's platform-identical-path contract), which
    # argparse and west take verbatim on Windows too.
    #
    # Inserted at argv[1] rather than appended so a caller's trailing
    # value-expecting flag (`tan lock --workspace` with the value forgotten)
    # cannot swallow `--board` and turn our injection into their bug.
    if subcommand in _TAKES_BOARD and not _targets_itself(passthrough):
        argv[1:1] = ["--board", context.board_yaml]

    west_command = argv[0]
    data = {
        "schemaVersion": "1",
        "westCommand": west_command,
        "westCwd": run_cwd,
        # The argv actually handed to `west`, injection included -- not the
        # caller's `passthrough` (tan-cli#391): an envelope that omits the
        # argument a failure was about cannot explain the failure.
        "args": argv[1:],
        # Seeded `None` (JSON mode only; text mode never emits `data`) so the
        # launch-error path -- where NO child ran at all, a different fact from
        # one that ran and produced nothing -- still reports the keys rather
        # than `""`/`0` or nothing. A wire consumer can read them
        # unconditionally. Overwritten below the moment a child does run.
        "stdout": None,
        "stderr": None,
        "westExitCode": None,
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
            # DELIBERATE DIVERGENCE 2 from `workspace.rs::run` (tan-cli#395).
            # The oracle calls `cmd.output()` and then reads neither
            # `out.stdout` nor `out.stderr` (`workspace.rs:312-328`) -- it
            # captures the child's entire product and throws it away, leaving
            # a JSON caller with `ok:true, issues:[]` and no report from
            # `alp-quality`, no diff from `alp-migrate --preview`, no manifest
            # from `alp-lock`. The capture already happened; discarding it was
            # the only decision being made. Both streams go in the envelope,
            # on the success branch as much as the failure one -- success is
            # where the report IS.
            data["stdout"] = out.stdout
            data["stderr"] = out.stderr
            # ...and the child's REAL code, which the envelope's own
            # `exitCode` deliberately does NOT propagate: `ExitCode` is tan's
            # own fixed vocabulary (2 = validation, 3 = write, 4 = doctor,
            # 5 = internal), so relaying a child's `2` or `5` would announce a
            # tan-level failure that never happened. `exitCode` stays
            # RUNTIME_FAILURE, `westExitCode` carries the truth, and a
            # consumer can finally tell west's own unknown-command usage error
            # from `alp-lock`'s exit 7 dependency conflict.
            data["westExitCode"] = out.returncode
            if out.returncode == 0:
                exit_code = ExitCode.SUCCESS
                issues = []
            else:
                exit_code = ExitCode.RUNTIME_FAILURE
                # The child's own diagnosis, whole -- `west: unknown command
                # "alp-lock"`, argparse's usage error, `alp-lock: dependency
                # zephyr-fs conflicts with alp-fs`. The old fixed sentence
                # told a JSON caller to "re-run without --format json", i.e.
                # to use a mode it does not have. Whole stderr, not a chosen
                # line: no heuristic can pick the right one (argparse puts the
                # error last, `alp-lock --check` puts a remediation hint
                # there), and `data.stderr` carries it unbounded regardless.
                detail = (out.stderr or "").strip()
                issues = [
                    Issue(
                        f"{subcommand}.failed",
                        "error",
                        f"`west {west_command}` failed (exit {out.returncode}): {detail}"
                        if detail
                        else f"`west {west_command}` failed (exit {out.returncode}) and "
                        "wrote nothing to stderr; see `data.stdout`.",
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
    # Text mode has no envelope to carry `westExitCode`, so the child's real
    # code goes in the line itself; tan's own process code stays
    # RUNTIME_FAILURE for the reason given on the JSON branch (tan-cli#395).
    typer.echo(
        f"{subcommand}: `west {west_command}` failed "
        f"(exit {result.returncode}; see log above).",
        err=True,
    )
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
