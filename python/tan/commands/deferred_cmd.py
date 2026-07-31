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
command-agnostic codes `tan.envelope`/`tan.cli` already emit unregistered),
and `contract/` is the frozen wire registry for codes a real consumer already
binds to -- registering a code for a stub that performs no work would be
premature. Promote it there the moment a real consumer starts matching it.
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
        # stdout is the envelope channel in json mode only; text mode has no
        # such contract, so the refusal goes to stderr like every other
        # command's text-mode error line.
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
        args: list[str] = typer.Argument(None, metavar="ARGS..."),
        output_format: str = typer.Option(
            "text", "--format", metavar="FORMAT", help="Output format: text or json."
        ),
    ) -> None:
        del args  # accepted and ignored -- see DEFERRED_CONTEXT_SETTINGS above
        _run_deferred(name, output_format)

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
