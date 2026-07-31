# SPDX-License-Identifier: Apache-2.0
"""Uniform stubs for the seven `tan` verbs the Python port does not yet
implement: `scaffold`, `completion`, `diff`, `pinmux`, `inspect`, `trace`, and
`support-bundle`. Every one of them is a REAL, working command in the Rust
oracle (`crates/tan-cli/src/cli.rs`'s `Commands` enum); porting each is
deliberately deferred to v0.6.0 (tan-cli#260), and this module exists only so
a v0.4.1 script that calls one gets a clear, coded refusal instead of Typer's
unknown-command usage error.

**Why registering the verb (rather than leaving it absent) is the fix.** A
name Typer has never heard of is a Click `UsageError`: exit 2, `cli.parse-error`
on the wire, and a message that reads exactly like a typo -- indistinguishable
from `tan bulid`. That is a strictly worse signal than the truth, which is
"this verb exists, tan knows about it, and it is not here YET". Registering it
here changes only the diagnosis; it adds no behaviour the real command would
have. A caller (or the extension) that greps for the issue code below, or the
`tan-cli#260` URL in the message, can special-case "deferred" from "typo"
without a hardcoded verb list of its own.

**Exit code: `RUNTIME_FAILURE` (1), not `VALIDATION_FAILURE` (2), chosen
deliberately.** `VALIDATION_FAILURE` is what Click's `UsageError` already
returns for a truly unknown command/flag -- reusing it here would put the
"known but deferred" case back at the exact same exit code as the "typo" case
this module exists to distinguish it from, silently defeating the point. Every
one of these seven verbs parses cleanly (any positional/flags are accepted,
never rejected) and is refused only once tan has recognised it -- the same
shape as `clean.sdk-root-not-found` (`clean_cmd.py`): a well-formed
invocation of a real command that cannot proceed. `RUNTIME_FAILURE` is what
that shape already uses elsewhere in this port.

**Issue code: one shared `cli.command-deferred`, not seven per-verb codes.**
All seven stubs report literally the same fact -- "this verb is deferred to
v0.6.0" -- so a caller that wants to special-case the situation needs exactly
one code to match, not seven near-duplicates that could drift. This code is
NOT in `contract/issue-codes.json`: nothing consumes it with `===` today (no
different than `cli.parse-error`/`envelope.serialize-failed`, the two other
command-agnostic codes `tan.envelope`/`tan.cli` already emit unregistered).
`contract/` is frozen on this branch, so it cannot be edited here regardless
of status -- but the registry's own `_comment` defines `status: "reserved"`
as exactly this pre-consumer state (the spelling exists at the emission site,
but nobody matches it with `===` yet), which is what `cli.command-deferred`
already is today, not the premature case the earlier wording claimed.
FOLLOW-UP: once `contract/` is open for edits again, register
`cli.command-deferred` there as `"status": "reserved"` with
`"emittedBy": "python/tan/commands/deferred_cmd.py"` and a `"literal"` entry
(a `reserved` row needs both, per the other `reserved` rows already in the
file, and `crates/tan-cli/tests/contract.rs`'s `frozen_issue_codes` gates the
emission site actually matching them) -- not straight to `frozen`, since no
consumer binds to it yet.
"""
from __future__ import annotations

import typer

from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: Shared by every stub below -- see the module docstring's "Issue code"
#: section for why one code, not seven.
DEFERRED_ISSUE_CODE = "cli.command-deferred"

#: The tan-cli issue tracking the real Python port of every verb this module
#: stubs. Named in every stub's message, per the tan-cli#260 deferral itself.
DEFERRED_ISSUE_URL = "https://github.com/alplabai/tan-cli/issues/260"

#: Every verb this module stubs -- the argv surface accepts (and silently
#: discards) anything, so a caller's existing flags/positionals never turn
#: into a SEPARATE parse-error ahead of the deferral message.
DEFERRED_CONTEXT_SETTINGS = {"ignore_unknown_options": True, "allow_extra_args": True}

#: The canonical list of verbs this module stubs -- the single source both
#: `tan.cli._HONOURS_ROOT_FORMAT` and `tests/commands/test_deferred_commands.py`
#: derive from, instead of each retyping the same seven names (a THIRD and
#: FOURTH copy respectively; the individual `_make_stub("...")` calls at the
#: bottom of this module are the first, unavoidable one).
DEFERRED_VERBS = (
    "scaffold",
    "completion",
    "diff",
    "pinmux",
    "inspect",
    "trace",
    "support-bundle",
)


def _deferred_message(name: str) -> str:
    return (
        f"tan {name} is deferred to v0.6.0 and not available in this build "
        f"(see {DEFERRED_ISSUE_URL})."
    )


def _run_deferred(name: str, output_format: str) -> None:
    """Report `name` as deferred and exit `RUNTIME_FAILURE`, in whichever
    format the caller asked for -- see the module docstring for why."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    message = _deferred_message(name)
    if output_format == "json":
        emit(
            Envelope(
                name,
                Project(root=None, board_yaml=None),
                {"message": message},
                [Issue(DEFERRED_ISSUE_CODE, "error", message)],
                ExitCode.RUNTIME_FAILURE,
            )
        )
    else:
        # This function's own contract: text mode writes only to stderr, like
        # every other command's text-mode error line. Whether stdout also ends
        # up carrying a JSON envelope is decided one layer up, by `main`'s
        # textual `_wants_json(argv)` scan -- e.g. `tan --format json scaffold
        # --format text` resolves to text mode HERE (this branch runs) but
        # `main` still sees "json" in argv and synthesizes a mislabelled
        # `cli.parse-error` envelope on stdout for the nonzero exit (measured;
        # pre-existing, shared with flash/size/image -- a `main`-level defect,
        # not this function's to fix).
        typer.echo(f"{name}: {message}", err=True)
    raise typer.Exit(int(ExitCode.RUNTIME_FAILURE))


def _make_stub(name: str):
    """Build one `app.command()`-ready callable for verb `name`. A factory
    rather than seven hand-written near-identical functions: the seven differ
    only in the string `name`, and Typer reads a command's registered NAME
    from the `app.command("...")` call in `cli.py`, not from this function's
    `__name__` -- so nothing here needs a distinct identity beyond its
    docstring (`--help` text) and closure over `name`.
    """

    def command(
        ctx: typer.Context,
        args: list[str] = typer.Argument(None, metavar="ARGS..."),
        output_format: str = typer.Option(
            None, "--format", metavar="FORMAT", help="Output format: text or json."
        ),
    ) -> None:
        del args  # accepted and ignored -- see DEFERRED_CONTEXT_SETTINGS above
        # `--format` is accepted BEFORE the subcommand too (`tan --format json
        # scaffold`, which the oracle's `global = true` clap flag allows and
        # which `_HONOURS_ROOT_FORMAT` in cli.py lists all seven of these verbs
        # under) -- the root callback records it on `ctx.obj` and this option
        # overrides it when repeated after the verb name. Mirrors
        # `debug_config_cmd.py:debug_config`'s `resolved_format` line.
        #
        # `is not None`, not a bare `or`: an explicit `--format ""` must reach
        # `_run_deferred`'s validation and exit 2, matching the oracle
        # (measured: `tan scaffold --format ""` -> rc 2, "a value is required
        # for '--format <FORMAT>'"). A plain `output_format or ...` treats ""
        # as absent and silently falls back to "text" -- rc 1 -- which is the
        # divergence this port used to have.
        resolved_format = (
            output_format if output_format is not None else (ctx.obj or {}).get("format") or "text"
        )
        _run_deferred(name, resolved_format)

    command.__doc__ = (
        f"Deferred to v0.6.0, not yet ported to this build ({DEFERRED_ISSUE_URL})."
    )
    return command


scaffold = _make_stub("scaffold")
completion = _make_stub("completion")
diff = _make_stub("diff")
pinmux = _make_stub("pinmux")
inspect = _make_stub("inspect")
trace = _make_stub("trace")
support_bundle = _make_stub("support-bundle")
