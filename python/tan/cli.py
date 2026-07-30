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

from tan.commands.bootstrap_cmd import bootstrap
from tan.commands.build_cmd import build
from tan.commands.clean_cmd import clean
from tan.commands.debug_config_cmd import debug_config
from tan.commands.doctor_cmd import doctor
from tan.commands.examples_cmd import examples
from tan.commands.explain_cmd import explain
from tan.commands.flash_cmd import flash
from tan.commands.generate_cmd import generate
from tan.commands.image_cmd import image
from tan.commands.init_cmd import init
from tan.commands.presets_cmd import presets
from tan.commands.sdk_cmd import sdk
from tan.commands.size_cmd import size
from tan.commands.validate_cmd import validate
from tan.envelope import Envelope, Issue, Project, emit, envelope_emitted
from tan.exit_codes import ExitCode
from tan.version import TAN_VERSION

app = typer.Typer(add_completion=False)

# Registered with a STATIC import, deliberately -- PyInstaller follows the
# static import graph only, so a pkgutil/importlib auto-registry works from
# source and produces a frozen `tan` that cannot find its own commands (see
# `tan.commands.__init__`). Registering here rather than with a decorator in
# the command module keeps `tan.commands.*` free of any `tan.cli` import,
# which would otherwise be a cycle.
app.command("bootstrap")(bootstrap)
app.command("build")(build)
app.command("clean")(clean)
app.command("debug-config")(debug_config)
app.command("doctor")(doctor)
app.command("examples")(examples)
app.command("explain")(explain)
app.command("flash")(flash)
app.command("generate")(generate)
app.command("image")(image)
app.command("init")(init)
app.command("presets")(presets)
app.command("sdk")(sdk)
app.command("size")(size)
app.command("validate")(validate)

#: Commands that read the root `--format` off `ctx.obj`, so the flag may precede
#: the subcommand name for them (clap's `global = true`). Grow this as each
#: command is taught to; see `root` for why an unlisted command must REFUSE the
#: pre-subcommand position rather than silently ignore it.
_HONOURS_ROOT_FORMAT = frozenset({"debug-config", "flash", "image", "size"})


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version"),
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """tan CLI -- board configuration, generation, and project tooling."""
    # Rust's `--format` is `global = true`, so clap accepts it on EITHER side of
    # the subcommand name; four committed goldens invoke `tan --format json
    # debug-config ...`. Click gives the group only what precedes the subcommand,
    # so the value is recorded here and read off `ctx.obj` by any command that
    # honours the pre-subcommand position. A command's OWN `--format` (declared
    # after the command name) still wins -- that is the position every other
    # golden uses.
    ctx.obj = {"format": output_format}
    if version:
        if output_format == "json":
            # Under `--format json`, stdout is the envelope channel even for
            # `--version`: Rust routes clap's version output through
            # `emit_parse_error` (main.rs), giving exit 0, no `issues`, and the
            # rendered line as `data.message`. Reachable only now that `--format`
            # is a root option -- before, this argv was a Click usage error -- so
            # honouring it here is part of adding the flag, not a separate change.
            emit(
                Envelope(
                    "cli",
                    Project(root=None, board_yaml=None),
                    {"message": f"tan {TAN_VERSION}"},
                    [],
                    ExitCode.SUCCESS,
                )
            )
            return
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
    if output_format is not None and ctx.invoked_subcommand not in _HONOURS_ROOT_FORMAT:
        # A command that does not read `ctx.obj` would ACCEPT `--format json`
        # here and then run in text mode: exit 0, human text on stderr, and
        # NOTHING on stdout -- an envelope-less `--format json` run, which is the
        # exact break this port exists to prevent (the extension renders an empty
        # panel with no error). Refusing is the status quo for those commands
        # (Click's own usage error, exit 2, plus `main`'s `cli.parse-error`
        # envelope). Each command joins `_HONOURS_ROOT_FORMAT` when it learns to
        # read `ctx.obj`; until then the flag only works in its documented
        # position, after the subcommand name.
        #
        # LAST in this callback deliberately: `--version` and the bare-invocation
        # refusal both have their own answers, and checking first hijacked them
        # with a worse message (`tan --format json --version` exits 0 with the
        # version line in Rust, and bare `tan --format json` must say "a command
        # is required", not name a `None` subcommand).
        ctx.fail(
            f"--format must be given after the '{ctx.invoked_subcommand}' "
            "subcommand, not before it"
        )


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
        # `not envelope_emitted()`: this fallback exists for the Click-level
        # usage error, which exits before any command runs and so leaves
        # stdout empty. A command that already wrote its own envelope and then
        # exited non-zero (every failed `tan build`) must not get a second one
        # appended -- two JSON documents on stdout is the same break as none.
        if json_mode and code != 0 and not envelope_emitted():
            print(_usage_error_envelope(code))
        raise
