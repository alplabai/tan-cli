# SPDX-License-Identifier: Apache-2.0
"""The `tan` command-line surface: the Typer app, the root callback, and the
`--format json` error path that wraps Click's own dispatch.

Owns the process entrypoint (``main``) so ``__main__.py`` stays a one-line
shim. Commands register here with a STATIC import in a later task -- see
``tan.commands.__init__`` for why an importlib/pkgutil registry is a trap --
which is why this module, not a package `__init__`, is where `app` lives:
one obvious place to add each `app.command()` / `app.add_typer()` call.
"""
import sys

import typer

from tan.envelope import Envelope, Issue, Project
from tan.exit_codes import ExitCode
from tan.version import TAN_VERSION

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context, version: bool = typer.Option(False, "--version")) -> None:
    """tan CLI -- board configuration, generation, and project tooling."""
    if version:
        # MUST match /^tan \d+\.\d+\.\d+/ -- the extension rejects the binary
        # otherwise (alp-sdk-vscode/src/alpCli/service.ts:107-121).
        typer.echo(f"tan {TAN_VERSION}")
        return
    if ctx.invoked_subcommand is None:
        # Bare invocation. Rust's clap requires a subcommand and exits 2 with
        # help on stderr (crates/tan-cli/src/cli.rs); invoke_without_command
        # exists only so --version can run without one, so an actually-bare
        # call has to be rejected by hand here, or `tan` with no args
        # silently "succeeds" -- the defect this module exists to fix. Click's
        # own idiom for "this callback found the invocation invalid":
        # ctx.fail() raises its usual UsageError (usage line + message to
        # stderr, exit code 2), the same shape every other CLI mistake here
        # already gets, so bare invocation does not need its own bespoke
        # rendering.
        ctx.fail("a command is required")


def _wants_json(argv: list[str]) -> bool:
    """Textual scan for ``--format json`` / ``--format=json``, mirroring
    Rust's ``wants_json`` (crates/tan-cli/src/main.rs). Needed because a
    usage error (bare invocation, an unknown command, a bad flag) means Click
    exits via its own machinery before any option ever gets parsed into
    something this code could otherwise trust.
    """
    for i, arg in enumerate(argv):
        if arg == "--format=json":
            return True
        if arg == "--format" and i + 1 < len(argv) and argv[i + 1] == "json":
            return True
    return False


def _usage_error_envelope(exit_code: int) -> str:
    """The JSON envelope for a Click-level usage error under `--format json`.

    Deliberately generic: this fires from a bare ``except SystemExit``, after
    Click has already rendered and printed its own (specific) message to
    stderr -- by that point the original exception object, and the message
    text it carried, are gone. Rust's ``emit_parse_error`` (main.rs) can
    afford the specific clap message because it intercepts the error object
    itself, before clap prints anything; recovering that here would mean
    depending on Typer's private, vendored click-alike exception hierarchy
    (`typer._click.exceptions`, NOT the public `click` package's classes --
    confirmed empirically against typer==0.27.0/click==8.4.1: TyperGroup and
    everything it raises descends from `typer._click.core`/`exceptions`, not
    `click`'s own) purely to recover a string this contract's tests never
    inspect. Catching the process exit instead is the version-stable seam.
    """
    message = "invalid command line invocation"
    env = Envelope(
        "cli",
        Project(root=None, board_yaml=None),
        {"message": message},
        [Issue("cli.parse-error", "error", message)],
        exit_code,
    )
    return env.to_json()


def main() -> None:
    """Process entrypoint.

    Text mode (the default) lets Click run standalone: it already prints its
    own errors/help to stderr and exits with the right code, which is exactly
    the contract there -- stderr carries no promises of its own (see
    ``tests/parity/oracle.py``'s module docstring).

    ``--format json`` cannot be handled that way: Click's default dispatch
    prints straight to stdout/stderr and calls `sys.exit` itself for a usage
    error, none of which goes through the envelope, so a bare invocation or a
    bad flag under `--format json` would otherwise leave stdout either empty
    or carrying human text instead of the one JSON envelope the contract
    promises (the hard constraint: "stdout is the envelope channel"). Rust's
    ``main.rs`` hits the identical problem and solves it by intercepting the
    parse error before clap prints it; the equivalent interception point here
    is process exit itself -- `app()` still runs standalone (so its stderr
    text and exit code are unchanged), and this wraps it only to add the
    missing stdout envelope when the exit signals failure under `--format
    json`.
    """
    argv = sys.argv[1:]
    json_mode = _wants_json(argv)
    try:
        app()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            code = int(ExitCode.SUCCESS)
        elif not isinstance(code, int):
            code = int(ExitCode.RUNTIME_FAILURE)
        if json_mode and code != 0:
            print(_usage_error_envelope(code))
        raise
