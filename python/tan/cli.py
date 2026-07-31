# SPDX-License-Identifier: Apache-2.0
"""The `tan` command-line surface: the Typer app, the root callback, and the
`--format json` error path that wraps Click's own dispatch.

Defines ``main``, but no longer owns the PROCESS boundary: ``pyproject.toml``'s
``[project.scripts]`` names ``tan.__main__:main``, and ``__main__.py`` wraps
this ``main`` to swallow a closed stdout (``tan generate --help | head``) as a
quiet success instead of a traceback. Anything that must happen for EVERY
invocation regardless of subcommand belongs there, not here.

Commands register here with a STATIC import -- see ``tan.commands.__init__``
for why an importlib/pkgutil registry is a trap: it works from source and
fails inside a PyInstaller ``--onefile`` binary, which is how tan actually
ships. That is why this module, not a package ``__init__``, is where ``app``
lives: one obvious place to add each ``app.command()`` call, and one list
(``_SUBCOMMAND_NAMES``) that must track it.
"""
import io
import sys

import typer
from click.testing import CliRunner
from typer.main import get_command

from tan.commands.bootstrap_cmd import bootstrap
from tan.commands.build_cmd import build
from tan.commands.clean_cmd import clean
from tan.commands.debug_config_cmd import debug_config
from tan.commands.deferred_cmd import (
    DEFERRED_CONTEXT_SETTINGS,
    DEFERRED_VERBS,
    completion,
    diff,
    inspect,
    pinmux,
    scaffold,
    support_bundle,
    trace,
)
from tan.commands.doctor_cmd import doctor
from tan.commands.examples_cmd import examples
from tan.commands.explain_cmd import explain
from tan.commands.faultdecode_cmd import faultdecode
from tan.commands.flash_cmd import flash
from tan.commands.generate_cmd import generate
from tan.commands.image_cmd import image
from tan.commands.init_cmd import init
from tan.commands.kconfig_cmd import kconfig
from tan.commands.model_cmd import model
from tan.commands.monitor_cmd import monitor
from tan.commands.new_som_cmd import new_som
from tan.commands.presets_cmd import presets
from tan.commands.renode_cmd import renode
from tan.commands.run_cmd import run
from tan.commands.sdk_cmd import sdk
from tan.commands.size_cmd import size
from tan.commands.validate_cmd import validate
from tan.commands.west_forward_cmd import FORWARD_CONTEXT_SETTINGS, lock, migrate, quality
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
app.command("completion", context_settings=DEFERRED_CONTEXT_SETTINGS)(completion)
app.command("debug-config")(debug_config)
app.command("diff", context_settings=DEFERRED_CONTEXT_SETTINGS)(diff)
app.command("doctor")(doctor)
app.command("examples")(examples)
app.command("explain")(explain)
app.command("faultdecode")(faultdecode)
app.command("flash")(flash)
app.command("generate")(generate)
app.command("image")(image)
app.command("init")(init)
app.command("inspect", context_settings=DEFERRED_CONTEXT_SETTINGS)(inspect)
app.command("kconfig")(kconfig)
app.command("lock", context_settings=FORWARD_CONTEXT_SETTINGS)(lock)
app.command("migrate", context_settings=FORWARD_CONTEXT_SETTINGS)(migrate)
app.command("model")(model)
app.command("monitor")(monitor)
app.command("new-som")(new_som)
app.command("pinmux", context_settings=DEFERRED_CONTEXT_SETTINGS)(pinmux)
app.command("presets")(presets)
app.command("quality", context_settings=FORWARD_CONTEXT_SETTINGS)(quality)
app.command("renode")(renode)
app.command("run")(run)
app.command("scaffold", context_settings=DEFERRED_CONTEXT_SETTINGS)(scaffold)
app.command("sdk")(sdk)
app.command("size")(size)
app.command("support-bundle", context_settings=DEFERRED_CONTEXT_SETTINGS)(support_bundle)
app.command("trace", context_settings=DEFERRED_CONTEXT_SETTINGS)(trace)
app.command("validate")(validate)

#: Every registered subcommand name -- must track the `app.command(...)` calls
#: above. Used only to find the argv BOUNDARY `_reorder_global_flags` moves a
#: leading global flag across; it is never itself treated as a flag.
_SUBCOMMAND_NAMES = frozenset(
    {
        "bootstrap", "build", "clean", "completion", "debug-config", "diff",
        "doctor", "examples", "explain", "faultdecode", "flash", "generate",
        "image", "init", "inspect", "kconfig", "lock", "migrate", "model",
        "monitor", "new-som", "pinmux", "presets", "quality", "renode", "run",
        "scaffold", "sdk", "size", "support-bundle", "trace", "validate",
    }
)

#: clap's `GlobalArgs` (`crates/tan-cli/src/cli.rs` lines 24-73) minus
#: `--format`: every field there is `#[arg(long, global = true, ...)]`, so
#: clap accepts it on EITHER side of the subcommand name. `--format` is
#: deliberately excluded -- it already has its own root-level, per-command
#: allowlisted mechanism below (`_HONOURS_ROOT_FORMAT`), rolled out command by
#: command on purpose (see `root`'s refusal branch and
#: `test_debug_config_command.py`'s `--format json validate` case, which
#: pins `validate` to STILL be refused pre-subcommand until it is taught to
#: read `ctx.obj` itself); folding `--format` into the blanket reorder below
#: would silently skip that refusal for every not-yet-migrated command.
#: `--version` is not here either -- it lives on `Cli` directly in clap, not
#: `GlobalArgs`, and is root-only on both sides already.
#: Value: the flag's arity (1 = takes a value, 0 = boolean).
_GLOBAL_FLAG_ARITY: dict[str, int] = {
    "--project": 1,
    "--board-yaml": 1,
    "--sdk-root": 1,
    "--target": 1,
    "--all": 0,
    "--verbose": 0,
    "--quiet": 0,
    "--no-color": 0,
    "--non-interactive": 0,
    "--ci": 0,
}


def _reorder_global_flags(argv: list[str]) -> list[str]:
    """Move a leading GLOBAL flag (`_GLOBAL_FLAG_ARITY`) from before the
    subcommand name to immediately after it, where a command that implements
    it already has its own local option declared (see e.g. `clean_cmd.clean`'s
    trailing `--quiet`/`--ci`/`--target`/... parameters) -- Click only reads
    options declared on the GROUP callback (`root`, below) for anything
    appearing before the subcommand name, and `root` does not declare these,
    so today they are a hard parse error there.
    Concretely: `alp-sdk-vscode/src/west.ts`'s `alpBuild` invokes `tan
    --project <app> build`, and `alpCli/vscodeAdapter.ts`'s `withSdkRoot`
    prepends `--sdk-root <path>` ahead of the subcommand for nearly every
    command the extension runs (`runAlpCommand`/`runAlpInTerminal`).

    A pure `list[str] -> list[str]` argv rewrite, run before Typer/Click ever
    sees it, so no per-command code has to change to gain the pre-subcommand
    position -- only the POSITION moves. A command that does not implement a
    given flag at all keeps failing exactly as it does in the (already
    correct, already tested) post-subcommand position -- e.g. `tan build
    --sdk-root x --bogus` and `tan --sdk-root x build --bogus` both still
    fail on `--bogus`; this never invents support a command never had, and
    never swallows an unrecognised flag silently.

    `--format` is left in place rather than moved: it is skipped over (kept
    ahead of the subcommand, exactly where it was typed) so scanning can
    continue past it, because `root` (below) already declares its own
    `--format` and reads pre-subcommand values off `ctx.obj` -- a LEADING
    `--format json --sdk-root X doctor` must not abort the whole rewrite and
    strand `--sdk-root` in the unrecognised pre-subcommand position. This is
    NOT full parity with the oracle: the oracle's clap `--format` is `global =
    true` and actually runs doctor at rc=4 (`tan --format json --sdk-root X
    doctor`); this port only lets the argv survive the reorder and reach
    `root`, which then refuses any command outside `_HONOURS_ROOT_FORMAT`
    (below) with rc=2 and a `cli.parse-error` envelope -- `doctor` is not yet
    in that set, so `python -m tan --format json --sdk-root X doctor` is
    still rc=2 today. The worked, pinned example is `debug-config`, which
    IS in `_HONOURS_ROOT_FORMAT`: `tan --format json debug-config ...`
    reaches the command and emits the JSON envelope, per
    `test_debug_config_command.py`'s `--format json validate` case. Each
    command joins `_HONOURS_ROOT_FORMAT` -- and only then gains this rc=4-style
    parity -- when it learns to read `ctx.obj["format"]`.

    Deliberately conservative otherwise: any OTHER token before the first
    subcommand name that is not a recognised global flag (or that flag's
    value) — `--help`, `--version`, or a bare positional — aborts the rewrite
    and returns `argv` untouched, so every existing argv shape (a normal `tan
    build ...` with zero leading tokens is a no-op by construction; `--version`,
    a bad command, a bare invocation, all of which have no subcommand token to
    move anything after) sees the exact argv it always has.
    """
    moved: list[str] = []
    kept: list[str] = []  # `--format` tokens, left before the subcommand
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token in _SUBCOMMAND_NAMES:
            return [*kept, token, *moved, *argv[i + 1 :]]
        name = token.split("=", 1)[0]
        if name == "--format":
            if "=" in token:
                kept.append(token)
                i += 1
                continue
            if i + 1 >= n:
                return argv  # "--format" with no value: let Click report it natively
            kept.extend((token, argv[i + 1]))
            i += 2
            continue
        arity = _GLOBAL_FLAG_ARITY.get(name)
        if arity is None:
            return argv  # not a recognised global flag and not the subcommand
        if "=" in token or arity == 0:
            moved.append(token)
            i += 1
            continue
        if i + 1 >= n:
            return argv  # "--sdk-root" with no value: let Click report it natively
        moved.extend((token, argv[i + 1]))
        i += 2
    return argv  # no subcommand token ever found


def _wants_help(argv: list[str]) -> bool:
    """Whether `--help` appears anywhere in argv. Textual, like `_wants_json`
    below: Click's own `--help` is an eager option that can short-circuit
    parsing before anything else runs, so this only needs to know the token
    is present, not where."""
    return "--help" in argv


def _emit_help_envelope(argv: list[str]) -> int:
    """Under `--format json`, `--help` must still land as ONE JSON envelope on
    stdout -- mirroring Rust's `emit_parse_error` path for clap's DisplayHelp
    error kind (`main.rs`, `json_mode_help_yields_zero_exit_and_no_issue`:
    exit 0, `issues: []`, the rendered help as `data.message`). Click's own
    `--help` handling prints straight to stdout and calls `ctx.exit(0)` before
    a command (or even `root`) ever runs, bypassing `emit()` entirely, so it
    has to be intercepted here instead -- `CliRunner` drives the exact same
    Click command and captures what it would have printed as a string,
    without ever touching the real stdout.

    Not help-specific in what it reads back: `result.exit_code`/`result.output`
    cover the rare case where argv makes Click reject the invocation before
    ever reaching the eager `--help` callback, the same way Rust's generic
    `err.exit_code()`/`err.render()` do for ANY clap parse outcome, help
    included.

    Returns the exit code the ENVELOPE just printed reports, so the caller can
    `sys.exit` it: `tan --format json badcmd --help` renders help for an
    unknown command, which Click (and the oracle) both exit 2 for, and a
    process exit of 0 there would contradict the very envelope on stdout.
    """
    result = CliRunner().invoke(get_command(app), argv, prog_name="tan")
    message = result.output.strip()
    code = result.exit_code
    issues = [] if code == 0 else [Issue("cli.parse-error", "error", message)]
    emit(
        Envelope(
            "cli",
            Project(root=None, board_yaml=None),
            {"message": message},
            issues,
            code,
        )
    )
    return code


#: Commands that read the root `--format` off `ctx.obj`, so the flag may precede
#: the subcommand name for them (clap's `global = true`). Grow this as each
#: command is taught to; see `root` for why an unlisted command must REFUSE the
#: pre-subcommand position rather than silently ignore it.
#:
#: The non-deferred four (`debug-config`/`flash`/`image`/`size`) and
#: `faultdecode` are hand-listed here -- each command's own module is where its
#: `ctx.obj["format"]` read lives, so there is no shared list to derive them
#: from the way the deferred seven have `deferred_cmd.DEFERRED_VERBS`.
#: `faultdecode` was verified against the oracle the same way (measured:
#: `target/debug/tan.exe --format json faultdecode --cfsr 0x8200` reaches the
#: command rather than erroring on `--format`'s position) and its own module
#: (`faultdecode_cmd.py:resolved_format`) already reads `ctx.obj`; this entry
#: was the missing wire-up.
#:
#: The seven `deferred_cmd.py` stubs are DERIVED from `DEFERRED_VERBS` rather
#: than retyped here -- a third hardcoded copy of the same seven names is
#: exactly the drift this set exists to prevent (an eighth stub added to
#: `deferred_cmd.py` without a matching edit here would otherwise pass every
#: test while silently regressing to exit 2 for the new verb). Verified
#: against the oracle (`target/debug/tan.exe`): `tan --format json scaffold`
#: (and the other six) all reach the real command rather than erroring on
#: `--format`'s position -- clap's `--format` is genuinely global, so a stub
#: that refuses it pre-command would hand the JSON caller most likely to check
#: for the deferral's issue code the exact typo-shaped exit-2 `cli.parse-error`
#: that module exists to eliminate. Each stub reads `ctx.obj["format"]` (see
#: `deferred_cmd.py`).
_HONOURS_ROOT_FORMAT = frozenset(
    {"debug-config", "flash", "image", "size", "faultdecode", *DEFERRED_VERBS}
)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version"),
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """tan CLI -- board configuration, generation, and project tooling."""
    if output_format is not None and output_format not in ("text", "json"):
        # clap validates `--format`'s VALUE eagerly, ahead of everything else
        # in this callback -- measured: `tan --format bogus`, `tan --format
        # bogus --version`, and `tan --format "" build` (an empty value counts
        # as invalid: clap says "a value is required for '--format <FORMAT>'
        # but none was supplied") all exit 2 on the value itself, never
        # reaching `--version` or the bare-invocation check below. Without this,
        # a root-position `--format ""` silently defaulted to text mode for
        # every command in `_HONOURS_ROOT_FORMAT` (rc 1, diverging from the
        # oracle's rc 2) instead of being refused here. `ctx.fail()` gives the
        # same Click UsageError shape (exit 2) every other CLI mistake here
        # already gets.
        ctx.fail(f"'{output_format}' is not one of 'text', 'json'")
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


def _usage_error_envelope(exit_code: int, captured_stderr: str = "") -> str:
    """The JSON envelope for a Click-level usage error under `--format json`.

    `captured_stderr` is Click's own rendered message (usage line + the
    specific complaint, e.g. "Error: No such option: --bogus") -- tee'd off
    the real stderr stream by the caller as it printed, not recovered from the
    exception object. Without it this function had exactly one message for
    EVERY usage error ("invalid command line invocation"), so the actual
    reason a caller's argv was rejected existed nowhere: not on stdout (this
    generic string), and not on stderr either (the pre-fix caller discarded
    the capture whenever no command-level envelope had been emitted, which is
    precisely the case here). Rust's ``emit_parse_error`` (main.rs) affords
    the specific clap message because it intercepts the error object itself,
    before clap prints anything; recovering the equivalent object here would
    mean depending on Typer's private, vendored click-alike exception
    hierarchy (`typer._click.exceptions`, NOT the public `click` package's
    classes -- confirmed empirically against typer==0.27.0/click==8.4.1:
    TyperGroup and everything it raises descends from
    `typer._click.core`/`exceptions`, not `click`'s own); tee-ing the text
    Click already rendered is the version-stable seam instead.
    """
    message = captured_stderr.strip() or "invalid command line invocation"
    env = Envelope(
        "cli",
        Project(root=None, board_yaml=None),
        {"message": message},
        [Issue("cli.parse-error", "error", message)],
        exit_code,
    )
    return env.to_json()


class _TeeStderr:
    """Writes through to the REAL stderr immediately, while also keeping a
    copy -- needed only to fold a Click-level usage error's message into the
    JSON envelope (`_usage_error_envelope`, above).

    Pre-fix, `--format json` wrapped the whole run in
    `contextlib.redirect_stderr(io.StringIO())`: nothing reached the real
    stderr until the process was about to exit, so a long-running `tan build
    --format json` against a real Zephyr tree printed NOTHING for the whole
    build, then dumped it all at once -- a customer watching the console sees
    a hang, not a build. Every write goes to `_real` first, synchronously, so
    a slice's output (`build_cmd._stream`) streams exactly as it does in text
    mode; the buffered copy exists purely so the `SystemExit` handler below can
    read back what Click already printed.
    """

    def __init__(self, real: object) -> None:
        self._real = real
        self._buffer = io.StringIO()

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._real.write(s)

    def flush(self) -> None:
        self._real.flush()

    def getvalue(self) -> str:
        return self._buffer.getvalue()


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
    argv = _reorder_global_flags(sys.argv[1:])
    sys.argv = [sys.argv[0], *argv]
    json_mode = _wants_json(argv)

    if json_mode and _wants_help(argv):
        # `--help` short-circuits Click before `root`/any command ever runs
        # (see `_emit_help_envelope`), so it needs its own path entirely --
        # by the time a `SystemExit` from it would reach the block below,
        # Click has already printed the human help text straight to stdout.
        # `sys.exit`, not a bare `return`: the process exit code must agree
        # with the `exitCode` of the envelope just printed (Rust's own
        # `json_exit_code` doc comment states the same invariant) --
        # `tan --format json badcmd --help` renders help for an unknown
        # command at exit 2, and a bare `return` here left the process exiting
        # 0 regardless.
        sys.exit(_emit_help_envelope(argv))

    if not json_mode:
        app()
        return

    # `--format json`, past `--help`: TEE stderr for the duration of the run
    # (`_TeeStderr`) rather than capturing it -- every write still reaches the
    # real stderr AS IT HAPPENS, so a slice's live output (build's `_stream`)
    # streams exactly as it does in text mode; a long `tan build --format
    # json` against a real Zephyr tree no longer goes silent for the whole
    # build and dumps at the end. The kept copy exists only to fold Click's
    # own pre-dispatch usage-error text (bare invocation, an unknown command,
    # a bad flag -- printed straight to stderr before any command runs,
    # mirroring clap's `err.exit()`) into the envelope below via
    # `_usage_error_envelope`, so the specific reason a caller's argv was
    # rejected is not silently different between the two channels.
    # `not envelope_emitted()` -- the same flag `emit()` sets -- still gates
    # the envelope fallback itself: a command that already wrote its own and
    # then exited non-zero (every failed `tan build`) must not get a second
    # one appended, two JSON documents on stdout is the same break as none.
    real_stderr = sys.stderr
    captured_stderr = _TeeStderr(real_stderr)
    sys.stderr = captured_stderr
    try:
        try:
            app()
        except SystemExit as exc:
            code = exc.code
            if code is None:
                code = int(ExitCode.SUCCESS)
            elif not isinstance(code, int):
                code = int(ExitCode.RUNTIME_FAILURE)
            if code != 0 and not envelope_emitted():
                print(_usage_error_envelope(code, captured_stderr.getvalue()))
            raise
    finally:
        sys.stderr = real_stderr
