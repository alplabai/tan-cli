# SPDX-License-Identifier: Apache-2.0
"""`tan migrate` / `tan lock` / `tan quality` -- the three `west`-delegating
extension commands that survive ADR-0020 Phase 4 (`west alp-build` itself was
retired; `tan build` is native now -- see `build_cmd.py`'s module doc). Port of
`crates/tan-cli/src/commands/build/workspace.rs::run` (the oracle's own,
now-narrowed, "legacy west forward" entry) and its `west_argv`/
`west_workspace_dir`/`west_program` helpers.

Every flag NOT declared on the command below (`--workspace`, `--no-verify`,
`--json`, `--junit`, `--sarif`, ...) belongs to the underlying `west
alp-<verb>` extension command, not to `tan` -- it is forwarded verbatim,
mirroring the oracle's own `[ARGS]...` catch-all
(`crates/tan-cli/src/cli.rs`'s `WestForwardArgs`). `ignore_unknown_options` in
each command's `context_settings` is what lets an unrecognised flag fall
through to that catch-all instead of Click rejecting it outright. `--check`/
`--preview`/`--apply` (`migrate`) and `--profile` (`quality`) used to be in
that undeclared set too, until tan-cli#454 (see below) made them real,
declared options for the one reason a flag in this file ever needs to be:
each is REQUIRED by its child with no default `tan` could supply.

Four defects this module shipped with, and what each fix costs:

**tan-cli#391 -- no child takes a positional.** `_run_forward` used to insert
the resolved app path as `argv[1]` whenever every forwarded token began with a
dash. `west alp-build` was the only `alp-*` command that ever declared an
app-path positional, and ADR-0020 Phase 4 retired it; the injection outlived
its target. The three surviving children declare flags only -- `alp_lock.py`:
`--check`, `--workspace`, `--board`; `alp_migrate.py`: `--check`, `--preview`,
`--apply`, `--all`, `--board`, `--no-verify`; `alp_quality.py`: `--profile`,
`--json`, `--junit`, `--sarif` -- so every realistic `tan migrate` / `tan lock`
run died on `west alp-migrate: error: unexpected arguments: ['<project>']`
before doing any work. `tan quality --profile quick` survived only by
accident (`quick` is a non-dash token, so the guard suppressed the injection);
`--profile=quick` is one token and did not. Deleting the injection alone is
NOT the whole fix: `run_cwd` is the west TOPDIR, so a child left to its own
`Path("board.yaml")` fallback resolves the WORKSPACE ROOT's board.yaml rather
than the project's. `_target_args` below names the project through the
`--board` flag `alp-migrate`/`alp-lock` really declare. `alp-quality` declares
no target flag at all and is therefore left cwd-targeted, exactly as before.

**tan-cli#395 -- the child's output was captured and discarded.** JSON mode
already spawned the child with `capture_output=True` and then read neither
stream; the previous version of this docstring defended that as "never the
child's own stdout, which for an interactive tool cannot be captured without
breaking it", but the capture had already happened one line above -- the only
decision actually being made was to throw the result away. `alp-quality`
exists to produce a report, `alp-migrate --preview` a diff, `alp-lock` a
resolved manifest, and a JSON-only caller could obtain none of them. `data`
now carries `stdout`, `stderr` and `westExitCode`, and the `<verb>.failed`
message is derived from the child's own stderr instead of a fixed sentence
telling the caller to re-run in a mode it does not have. The envelope's own
`exitCode` deliberately does NOT propagate the child's: `tan/exit_codes.py`
fixes tan's vocabulary (0/1/2/3/4/5), so a child's `2` would arrive
indistinguishable from tan's own `VALIDATION_FAILURE`. `data.westExitCode` is
what makes a lock conflict (`7`) distinguishable from west's own usage error.
This is a DELIBERATE divergence from the oracle, which calls `cmd.output()`
and never reads `out.stdout`/`out.stderr` either
(`crates/tan-cli/src/commands/build/workspace.rs`).

**tan-cli#405 -- tan's own globals leaked into the child.** These were the
only three commands registered with `ignore_unknown_options` AND the only
three that never called `accept_global_flags`, so seven of the ten flags in
`tan.core.global_flags._GLOBAL_FLAG_SPECS` were unknown here -- and an unknown
flag under `ignore_unknown_options` is not rejected, it is dropped into the
`ARGS...` catch-all and handed to a child that declares none of them.
`tan --ci quality --profile quick` became `west alp-quality --ci --profile
quick`; `tan --target cm33 quality` leaked the VALUE `cm33`, which then read
as a caller-supplied positional and silently changed the argv shape. All three
now call `accept_global_flags`. `--all` is the one collision: it is both a tan
global and a real `alp-migrate` flag, so `migrate` declares it for real and
forwards it back to the child after consuming it -- a bare
`accept_global_flags(migrate)` would swallow it, which is what the v0.4.1
oracle does.

**tan-cli#454 -- `quality` and `migrate` could never succeed.**
`alp_quality.py` declares `--profile {quick,pr,full,release}` REQUIRED;
`alp_migrate.py` declares a REQUIRED mutually-exclusive `(--check | --preview
| --apply)` group. Neither was ever declared on tan's own Typer surface --
both fell into the `ARGS...` catch-all like every other undeclared flag, which
happened to still work for a caller who typed the exact right token, but meant
`tan quality --help` never named `--profile` and `tan migrate --help` never
named any of the three modes, so a caller relying on `--help` (the entire
point of a documented CLI surface) had no way to discover what to pass. On a
correctly bootstrapped workspace, every invocation that omitted the required
flag reached `west`, which rejected it with its own argparse usage error --
forwarded back as the opaque `quality.failed` / `migrate.failed`, indistinguishable
from `west` itself failing for an unrelated reason. Fixed by declaring
`--profile` (`quality`) and `--check`/`--preview`/`--apply` (`migrate`) for
real -- their choice lists and mutual-exclusion read from
`scripts/west_commands/alp_quality.py` / `alp_migrate.py` verbatim, forwarded
back through `extra_args` exactly like `migrate`'s pre-existing `--all`
(tan-cli#405) -- and refusing EARLY, before `west` is ever spawned, with a
coded `quality.profile-required` / `migrate.mode-required` issue naming the
missing flag when none of the required ones was supplied. See
`_refuse_required`.

Text mode inherits stdio -- `alp-migrate`/`alp-lock`/`alp-quality` may prompt
or stream their own progress, exactly like the oracle's `west`-delegating path
(its own module doc: "Text mode inherits stdio so the build streams live"), so
there is nothing to capture there and nothing to report but the child's exit
code. JSON mode captures and reports ONE envelope.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer

from tan.commands.build_output import ProjectContext, resolve_project_context, to_posix
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.core.global_flags import accept_global_flags
from tan.core.venv import west_program, west_workspace_dir, with_venv_on_path
from tan.envelope import Envelope, Issue, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: Passed to `app.command(...)` for each forwarder below: lets an unrecognised
#: flag (the underlying `west alp-*` command's own surface) fall through to the
#: `args` catch-all instead of Click's usual "no such option" usage error.
FORWARD_CONTEXT_SETTINGS = {"ignore_unknown_options": True}

#: Kept as the underscore-named alias this module (and its own tests) always
#: called it -- the implementation moved to `tan.core.venv.west_workspace_dir`
#: (tan-cli#289 review) so `flash_cmd.py` shares it instead of importing a
#: private name across modules.
_west_workspace_dir = west_workspace_dir

#: The verbs whose child declares `--board` (tan-cli#391). `quality` is absent
#: on purpose: `alp_quality.py`'s surface is `--profile`/`--json`/`--junit`/
#: `--sarif` and nothing else, so there is no flag through which tan could name
#: a project and inventing one would be a usage error, not targeting.
_BOARD_TARGETED = frozenset({"migrate", "lock"})

#: How much of the child's stderr the `<verb>.failed` message may quote
#: (tan-cli#395). `data.stderr` keeps the stream VERBATIM and uncapped -- it is
#: the payload; this caps only the one-line summary, so a child that dumps a
#: megabyte on one line cannot turn a single issue into an unreadable envelope.
_MAX_MESSAGE_DETAIL = 500

#: Verbatim copy of `scripts/west_commands/alp_quality.py`'s own
#: `choices=("quick", "pr", "full", "release")` (tan-cli#454) -- read from the
#: SDK checkout the resolver points at, not guessed. Kept as one tuple so the
#: `--help` metavar, the `--profile <value>` validation and the "which one is
#: missing" refusal message can never drift apart from each other.
_QUALITY_PROFILES = ("quick", "pr", "full", "release")


def _west_argv(subcommand: str, passthrough: list[str]) -> list[str]:
    """Port of `workspace.rs::west_argv`: `alp-<subcommand>` followed by the
    forwarded args, verbatim."""
    return [f"alp-{subcommand}", *passthrough]


def _launch_error(err: OSError) -> str:
    """Port of `workspace.rs::west_launch_error`."""
    if isinstance(err, FileNotFoundError):
        return "west not found on PATH — run `tan bootstrap` and ensure west is on PATH."
    return f"failed to launch west: {err}"


def _has_flag(args: Sequence[str], flag: str) -> bool:
    """Whether the caller already spelled `flag` themselves, in either form
    argparse accepts for a value-taking option (`--board X`, `--board=X`).

    argparse's prefix ABBREVIATION (`--boa X`) is deliberately not detected:
    tan's own `--board` is prepended AHEAD of the forwarded tokens, so a
    caller's later spelling -- abbreviated or not -- is the one argparse keeps.
    The one case where that "last wins" safety net does not apply is
    `alp-migrate`'s `--all`, which is MUTUALLY EXCLUSIVE with `--board` rather
    than merely overridden by it; `--all` takes no value, and `migrate`
    declares it for real below, so the exact-match check is what actually runs
    for it.
    """
    prefix = f"{flag}="
    return any(arg == flag or arg.startswith(prefix) for arg in args)


def _target_args(subcommand: str, child_args: Sequence[str], board_yaml: str) -> list[str]:
    """The flags that tell the child WHICH project to act on (tan-cli#391).

    Empty for `quality` (no such flag exists), empty when the caller already
    named a board themselves, and empty for `migrate --all` -- the child's
    usage line is `[--all | --board BOARD]`, so adding `--board` behind a
    caller's `--all` would trade one usage error for another.
    """
    if subcommand not in _BOARD_TARGETED:
        return []
    if _has_flag(child_args, "--board"):
        return []
    if subcommand == "migrate" and _has_flag(child_args, "--all"):
        return []
    return ["--board", board_yaml]


def _last_meaningful_line(stderr: str) -> str:
    """The child's own last word. Both shapes tan-cli#395 measured put the
    actionable line LAST -- argparse prints its usage block and then `west
    alp-migrate: error: ...`, and west prints its own usage and then `west:
    unknown command "alp-lock"; ...` -- so the last non-blank line is the one
    a caller needs. The full stream is in `data.stderr` regardless, which is
    what makes this a summary rather than a lossy substitute."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    detail = lines[-1]
    return detail if len(detail) <= _MAX_MESSAGE_DETAIL else f"{detail[:_MAX_MESSAGE_DETAIL]}..."


def _child_failure_message(west_command: str, returncode: int, stderr: str) -> str:
    """tan-cli#395: the fixed "re-run without --format json to see the log"
    told a JSON-only caller to use a channel it does not have, and named
    neither the child's exit code nor its diagnostic. The fallback survives for
    a child that fails silently -- there is genuinely nothing else to say
    then."""
    detail = _last_meaningful_line(stderr)
    if not detail:
        return (
            f"`west {west_command}` failed with exit code {returncode}; the child "
            "wrote nothing to stderr -- re-run without --format json to see the log."
        )
    return f"`west {west_command}` failed with exit code {returncode}: {detail}"


@dataclass(frozen=True)
class _Forward:
    """One resolved `west alp-*` invocation, computed once and shared by both
    output modes so text and JSON can never spawn different argv."""

    subcommand: str
    west_bin: str
    #: The `alp-<verb>` argv -- `argv[0]` is the verb, the `west` binary is NOT
    #: part of it (see `full_argv`).
    argv: tuple[str, ...]
    run_cwd: str
    env: dict[str, str]

    @property
    def west_command(self) -> str:
        return self.argv[0]

    @property
    def full_argv(self) -> list[str]:
        return [self.west_bin, *self.argv]

    def data(self) -> dict[str, object]:
        """`data.args` is every argument handed to the `alp-<verb>` command --
        tan's own `--board` included, tan's globals excluded (tan-cli#391/#405).
        It used to echo the caller's tokens only, so the envelope could not
        name the injected argument the child actually died on. `westCommand`
        carries `argv[0]`, so `west <westCommand> <args...>` reconstructs the
        exact child command line."""
        return {
            "schemaVersion": "1",
            "westCommand": self.west_command,
            "westCwd": self.run_cwd,
            "args": list(self.argv[1:]),
        }


def _plan(
    subcommand: str,
    passthrough: Sequence[str],
    extra_args: Sequence[str],
    context: ProjectContext,
) -> _Forward:
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

    child_args = [*passthrough, *extra_args]
    argv = _west_argv(
        subcommand, [*_target_args(subcommand, child_args, context.board_yaml), *child_args]
    )
    return _Forward(
        subcommand=subcommand,
        west_bin=west_bin,
        argv=tuple(argv),
        run_cwd=run_cwd,
        env=with_venv_on_path(dict(os.environ), west_bin),
    )


def _spawn_captured(plan: _Forward) -> subprocess.CompletedProcess[str]:
    """JSON mode's child. `capture_output=True` was always here -- tan-cli#395
    is that neither stream was ever READ afterwards, not that the capture was
    missing. Raises `OSError` when `west` itself cannot be launched."""
    return subprocess.run(
        plan.full_argv,
        cwd=plan.run_cwd,
        env=plan.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _echo_sdk_resolution(sdk: SdkInfo | None) -> None:
    """tan-cli#478: the SDK-resolution pair on stderr, for the text paths.

    Every `--format json` path in this module gets the pair for free from
    `Envelope.__init__`'s central seam. NO text path does -- neither
    `_run_text` (which hands stdio to the child) nor `_refuse_required`'s
    refusal (which never builds an envelope at all) reaches `Envelope`. Both
    call this instead of open-coding the loop, so a third text path cannot
    be added that discloses in JSON and stays silent on the screen: that is
    precisely how `_refuse_required` shipped silent while `_run_text` did
    not.
    """
    if sdk is None:
        return
    for issue in sdk_resolution_issues(
        sdk.broken_project_pin, sdk.source_tier, sdk.foreign_global_default_for
    ):
        typer.echo(issue.message, err=True)


def _run_text(plan: _Forward, sdk: SdkInfo | None) -> None:
    """Inherit stdio: the child streams its own progress and may prompt.

    tan-cli#478 review finding 8: `_plan` resolves `west_bin`/`workspace` off
    `context.sdk.root` (see `_plan`'s own `sdk_path` line), so this command
    SPAWNS `west` out of the resolved SDK root -- same shape as `validate`'s
    spawn, worse consequence, since the child is a real build rather than a
    schema check. The JSON envelope below already discloses a foreign
    `globalDefault` via `Envelope.__init__`'s central seam. Text mode never
    reaches `Envelope` at all (the child inherits stdio directly), so this
    is printed BEFORE the spawn -- once stdio is handed to the child, tan
    has no stream left of its own to write a warning to.
    """
    _echo_sdk_resolution(sdk)
    try:
        result = subprocess.run(plan.full_argv, cwd=plan.run_cwd, env=plan.env, check=False)
    except OSError as err:
        typer.echo(f"{plan.subcommand}: {_launch_error(err)}", err=True)
        raise typer.Exit(int(ExitCode.RUNTIME_FAILURE)) from err
    if result.returncode == 0:
        typer.echo(f"{plan.subcommand}: complete.", err=True)
        raise typer.Exit(0)
    # The log IS above, but the code the child chose is not in it -- tan's own
    # exit stays 1 on this path too (tan-cli#395), so naming the child's code is
    # the only way a text-mode caller can tell a `7` from a `2`.
    typer.echo(
        f"{plan.subcommand}: `west {plan.west_command}` failed with exit code "
        f"{result.returncode} (see log above).",
        err=True,
    )
    raise typer.Exit(int(ExitCode.RUNTIME_FAILURE))


def _refuse_required(
    subcommand: str, code: str, message: str, context: ProjectContext, output_format: OutputFormat
) -> NoReturn:
    """tan-cli#454: `alp-quality --profile` and `alp-migrate`'s one-of
    `--check`/`--preview`/`--apply` are REQUIRED by the child's own argparse,
    with no default tan could pick on a caller's behalf (guessing a quality
    profile, or guessing whether to write board.yaml, is guessing wrong for
    someone). Neither was ever declared on tan's own Typer surface, so
    `tan quality --help` never named `--profile` and every realistic run
    without it reached `west`, which rejected it with its OWN argparse usage
    error -- forwarded back to the caller as an opaque `<verb>.failed`,
    indistinguishable from `west` itself failing, and undiscoverable from
    `--help`. Refusing HERE, before `west` is ever spawned, fixes both: no
    child process runs, and the coded issue below names the exact missing
    flag. `VALIDATION_FAILURE` (2), matching what a Click-level required
    option would report, but under the REAL command name and a real code --
    the same "no generic `cli.parse-error` fallback" precedent
    `new_som_cmd.py`'s own `new-som.failed` sets, not `command: "cli"`.

    This refusal's own `data` -- `{"schemaVersion": "1"}` only -- is
    deliberately narrower than `_run_forward`'s JSON branches below, which
    fill in `westCommand`/`westCwd`/`args`/`westExitCode`/`stdout`/`stderr`
    for every path that actually reaches `_plan()`. No `west` invocation was
    ever planned here -- there is no `westCommand` to name, no `westCwd` to
    resolve, no child argv to report -- so there is nothing to backfill;
    `faultdecode_cmd.py`'s own `data: None` refusal is the established
    precedent for a bare payload on this exact kind of pre-spawn refusal.
    Annotated `NoReturn`, not `None`: every caller ends its own function at
    this call (`raise typer.Exit` always fires), and an accurate signature is
    what lets a type checker prove the return type of a caller like
    `_resolve_quality_profile` sound instead of just trusting the docstring."""
    if output_format == "json":
        emit(
            Envelope(
                subcommand,
                context.project(),
                {"schemaVersion": "1"},
                [Issue(code, "error", message)],
                ExitCode.VALIDATION_FAILURE,
                sdk=context.sdk,
            )
        )
    else:
        # tan-cli#478 (PR #504 review): this branch printed the refusal ALONE
        # while the JSON branch above disclosed the SDK-resolution pair -- and
        # a bare `tan quality` / `tan migrate` lands here, having resolved the
        # checkout `_plan` would have run `west` out of. Order as `_run_text`.
        _echo_sdk_resolution(context.sdk)
        typer.echo(f"{subcommand}: {message}", err=True)
    raise typer.Exit(int(ExitCode.VALIDATION_FAILURE))


def _run_forward(
    subcommand: str,
    passthrough: list[str],
    project: str | None,
    board_yaml: str | None,
    sdk_root: str | None,
    output_format: OutputFormat,
    extra_args: Sequence[str] = (),
) -> None:
    """`extra_args` is what tan CONSUMED and hands back to the child --
    `migrate`'s `--all` and nothing else today (tan-cli#405). The JSON branch
    stays inline rather than moving to its own helper: both `<verb>.failed`
    sites have to be reachable from one function for
    `tests/gates/test_every_issue_code_is_registered.py`'s prefix-template scan
    to resolve them."""
    context = resolve_project_context(project, board_yaml, sdk_root)
    plan = _plan(subcommand, passthrough, extra_args, context)
    if output_format != "json":
        _run_text(plan, context.sdk)
        return
    try:
        out = _spawn_captured(plan)
    except OSError as err:
        # No child ran, so there is no code to report -- `null`, not a
        # fabricated 1. The keys stay present so a consumer sees ONE shape on
        # every branch THAT REACHES A PLAN (tan-cli#395) -- `_refuse_required`'s
        # own, earlier refusal never computes one and carries none of these
        # keys; see that function's own docstring for why that is a separate,
        # narrower shape rather than an omission here.
        data = {**plan.data(), "westExitCode": None, "stdout": "", "stderr": ""}
        exit_code = ExitCode.RUNTIME_FAILURE
        issues = [Issue(f"{subcommand}.failed", "error", _launch_error(err))]
    else:
        # `or ""`: this is the boundary where another process's output enters
        # the envelope, and a `None` would serialise as `null` for a field a
        # consumer reads as text.
        stdout, stderr = out.stdout or "", out.stderr or ""
        data = {**plan.data(), "westExitCode": out.returncode, "stdout": stdout, "stderr": stderr}
        if out.returncode == 0:
            exit_code, issues = ExitCode.SUCCESS, []
        else:
            exit_code = ExitCode.RUNTIME_FAILURE
            message = _child_failure_message(plan.west_command, out.returncode, stderr)
            issues = [Issue(f"{subcommand}.failed", "error", message)]
    emit(Envelope(subcommand, context.project(), data, issues, exit_code, sdk=context.sdk))
    raise typer.Exit(int(exit_code))


#: Why the catch-all parameter is `west_args` and not the obvious `args`
#: (tan-cli#405): `accept_global_flags` returns a `def wrapper(*args, **kwargs)`
#: carrying a synthesised `__signature__`, but Typer resolves each parameter's
#: type through `typing.get_type_hints(callback)`, which reads the WRAPPER's own
#: `__annotations__` -- where the name `args` already means the varargs tuple.
#: A parameter named `args` therefore came back annotated `object` instead of
#: `list[str]`, and Typer aborted with `RuntimeError: Type not yet supported`
#: for every one of these three commands. The metavar is pinned to `ARGS...`
#: so nothing about the CLI surface or the help text moves with the rename.


def _resolve_migrate_extra_args(
    check: bool,
    preview: bool,
    apply_changes: bool,
    all_boards: bool,
    west_args: Sequence[str],
    project: str | None,
    board_yaml: str | None,
    sdk_root: str | None,
    output_format: OutputFormat,
) -> list[str]:
    """The `alp-migrate` argv `migrate()` forwards past its own required-flag
    check -- split out so `migrate()` stays a short dispatcher (the module
    size gate's own per-function budget counts a docstring's lines too).
    `--all` is declared on `migrate()` for real -- BOTH a tan global and an
    `alp-migrate` flag (tan-cli#405); folded in here, after the mode(s), to
    keep the child's own meaning.

    `--check`/`--preview`/`--apply` (tan-cli#454) are a REQUIRED mutually-
    exclusive group on the child's own argparse. NONE selected refuses EARLY
    via `_refuse_required` -- unless `_has_flag` (see `_target_args`) finds
    one already in `west_args`, the documented escape hatch
    (`tan migrate -- --check`), in which case `modes` is left empty rather
    than duplicating the caller's own token. MORE than one selected on tan's
    own surface (`tan migrate --check --preview`) used to build the
    contradictory argv itself and spawn `west`, reporting the opaque
    `migrate.failed` the absence-refusal exists to remove -- refused the same
    way, with the same code, naming the other failure.
    """
    mode_flags = (("--check", check), ("--preview", preview), ("--apply", apply_changes))
    modes = [flag for flag, chosen in mode_flags if chosen]
    if len(modes) > 1:
        _refuse_required(
            "migrate",
            "migrate.mode-required",
            "only one of `--check`, `--preview`, `--apply` may be given "
            "(`west alp-migrate`).",
            resolve_project_context(project, board_yaml, sdk_root),
            output_format,
        )
    if not modes and not any(_has_flag(west_args, flag) for flag, _chosen in mode_flags):
        _refuse_required(
            "migrate",
            "migrate.mode-required",
            "one of `--check`, `--preview`, `--apply` is required (`west alp-migrate`).",
            resolve_project_context(project, board_yaml, sdk_root),
            output_format,
        )
    return [*modes, *(["--all"] if all_boards else [])]


def migrate(
    west_args: list[str] = typer.Argument(None, metavar="ARGS..."),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    all_boards: bool = typer.Option(
        False,
        "--all",
        help="Migrate every board in the workspace (`west alp-migrate --all`).",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Report version drift; nonzero on drift (`west alp-migrate --check`).",
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="Unified diff + diagnostic-v1 JSON, no writes (`west alp-migrate --preview`).",
    ),
    apply_changes: bool = typer.Option(
        False, "--apply", help="Rewrite board.yaml in place (`west alp-migrate --apply`)."
    ),
) -> None:
    """Migrate board.yaml to the current schema (`west alp-migrate`)."""
    resolved_args = list(west_args or [])
    extra_args = _resolve_migrate_extra_args(
        check, preview, apply_changes, all_boards, resolved_args,
        project, board_yaml, sdk_root, output_format,
    )
    _run_forward(
        "migrate",
        resolved_args,
        project,
        board_yaml,
        sdk_root,
        output_format,
        extra_args=extra_args,
    )


def lock(
    west_args: list[str] = typer.Argument(None, metavar="ARGS..."),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Pin/lock library dependencies (`west alp-lock`)."""
    _run_forward("lock", list(west_args or []), project, board_yaml, sdk_root, output_format)


def _resolve_quality_profile(
    profile: str | None,
    west_args: Sequence[str],
    project: str | None,
    board_yaml: str | None,
    sdk_root: str | None,
    output_format: OutputFormat,
) -> str | None:
    """The `--profile` value `quality()` forwards past its own required-flag
    check -- split out so `quality()` itself stays a short dispatcher (see
    `_resolve_migrate_extra_args`'s own docstring for why this split exists).

    tan-cli#454: `alp_quality.py` declares `--profile` REQUIRED with a fixed
    choice list (`_QUALITY_PROFILES`, mirrored verbatim), and no default tan
    could pick on a caller's behalf. Absence refuses EARLY via
    `_refuse_required` -- unless `_has_flag` (see `_target_args`) finds
    `--profile` already sitting in `west_args`, the documented escape hatch
    (`tan quality -- --profile quick`), in which case this returns `None`
    rather than refusing: `quality()` never parsed that copy, so it is not
    THIS function's own value, only proof the flag already reaches `west`
    unaided -- appending a second, tan-supplied `--profile` behind it would
    duplicate the flag. An out-of-list VALUE (not absence) still raises the
    standard Click usage error via `typer.BadParameter`, the established
    pattern `new_som_cmd.py` uses for its own choice-shaped flags
    (`click_type=click.Choice(...)` crashes under this repo's pinned
    typer/click pair -- see that module's own comment).
    """
    if profile is not None and profile not in _QUALITY_PROFILES:
        raise typer.BadParameter(
            f"{profile!r} is not one of {', '.join(repr(c) for c in _QUALITY_PROFILES)}.",
            param_hint="'--profile'",
        )
    if profile is None:
        if _has_flag(west_args, "--profile"):
            return None
        _refuse_required(
            "quality",
            "quality.profile-required",
            "`--profile` is required (`west alp-quality --profile "
            f"{{{','.join(_QUALITY_PROFILES)}}}`).",
            resolve_project_context(project, board_yaml, sdk_root),
            output_format,
        )
    return profile


def quality(
    west_args: list[str] = typer.Argument(None, metavar="ARGS..."),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    profile: str = typer.Option(
        None,
        "--profile",
        metavar="{quick,pr,full,release}",
        help="Quality profile to run (`west alp-quality --profile`; REQUIRED).",
    ),
) -> None:
    """Run the board.yaml quality checks (`west alp-quality`)."""
    resolved_args = list(west_args or [])
    profile = _resolve_quality_profile(
        profile, resolved_args, project, board_yaml, sdk_root, output_format
    )
    _run_forward(
        "quality",
        resolved_args,
        project,
        board_yaml,
        sdk_root,
        output_format,
        # `None`: the caller already supplied `--profile` through the
        # `ARGS...` escape hatch, and it is already in `resolved_args` --
        # appending it again would duplicate the flag argparse then rejects.
        extra_args=["--profile", profile] if profile is not None else [],
    )


# tan-cli#405: these were the last three commands not to accept the oracle's
# global set, and the only three where NOT accepting a flag meant handing it to
# a different program rather than refusing it -- `ignore_unknown_options` routes
# an unknown flag into the `ARGS...` catch-all, and that catch-all is the
# child's argv. Applied here, at the definition, for the reason
# `accept_global_flags` documents: by the time `cli.py` registers the command,
# the Click `Command` Typer built is already final.
migrate = accept_global_flags(migrate)
lock = accept_global_flags(lock)
quality = accept_global_flags(quality)
