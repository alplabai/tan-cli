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
lives: one obvious place to add each ``app.command()`` call, which is also the
single table everything else here derives from (``_SUBCOMMAND_NAMES``, and the
pre-/post-subcommand ``--format`` cases in
``tests/commands/test_cli_global_flags.py``).
"""
import io
import sys

import typer
from click.testing import CliRunner
from typer.core import TyperCommand
from typer.main import get_command, get_command_name

from tan.commands.bootstrap_cmd import bootstrap
from tan.commands.build_cmd import build
from tan.commands.clean_cmd import clean
from tan.commands.debug_config_cmd import debug_config
from tan.commands.completion_cmd import completion
from tan.commands.diff_cmd import diff
from tan.commands.inspect_cmd import inspect
from tan.commands.pinmux_cmd import pinmux
from tan.commands.scaffold_cmd import scaffold
from tan.commands.support_bundle_cmd import support_bundle
from tan.commands.trace_cmd import trace
from tan.commands.deferred_cmd import DEFERRED_CONTEXT_SETTINGS
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
from tan.commands.run_cmd import run
from tan.commands.sdk_cmd import sdk
from tan.commands.size_cmd import size
from tan.commands.validate_cmd import validate
from tan.commands.west_forward_cmd import FORWARD_CONTEXT_SETTINGS, lock, migrate, quality
from tan.core.global_flags import RELOCATABLE_FLAG_ARITY
from tan.envelope import (
    Envelope,
    Issue,
    Project,
    emit,
    envelope_emitted,
    envelope_emitted_exit_code,
)
from tan.exit_codes import ExitCode
from tan.output_format import declared_formats, invalid_format_message
from tan.version import TAN_VERSION

app = typer.Typer(add_completion=False)

#: The `--format` a DISPATCHED subcommand actually resolved to, as a bool, or
#: `None` when NO command body ran this process (`main()` resets it per run).
#:
#: This is the missing half of Rust's own structure (tan-cli#546/#491 defect 3).
#: `crates/tan-cli/src/main.rs` decides the channel like this:
#:
#:     match Cli::try_parse() {
#:         Ok(cli) => run(cli),                   // channel = cli.format
#:         Err(e)  => if wants_json(argv) { ... } // channel = the textual scan
#:     }
#:
#: -- so the naive `wants_json` scan is consulted ONLY once clap has already
#: FAILED, and can never affect a run that parsed. The port promoted the same
#: scan to a process-wide switch, which is what let a `--format json` that is
#: not tan's own flag (forwarded past `--` to `west alp-quality`, or sitting in
#: another option's value slot) replace a text-mode command's real, coded answer
#: with an unrequested `command:"cli"` / `cli.parse-error` document on stdout.
#:
#: Two earlier attempts tried to answer the same question by making the argv
#: SCAN smarter (an arity walk, then a clap-style hyphen guard). Both failed,
#: and a third textual shape cannot work either -- MEASURED against the oracle,
#: which answers the two structurally identical argvs differently:
#:
#:     tan quality -- --format json     oracle: TEXT  (0 bytes on stdout)
#:     tan build   -- --format json     oracle: JSON  (`cli.parse-error`)
#:
#: The only thing separating them is whether the parser ACCEPTED the argv
#: (`quality` forwards trailing args, `build` refuses them), which no scan of
#: argv alone can know. So the scan stays exactly as it is -- a faithful port of
#: Rust's `wants_json`, used only where Rust uses it -- and the parse OUTCOME is
#: recorded here instead.
_dispatched_json_mode: bool | None = None


class _DispatchedCommand(TyperCommand):
    """A `TyperCommand` that records the `--format` its own parse resolved.

    `Command.invoke` is called by Click only after `cmd.make_context(...)` has
    already succeeded, so reaching it means the FULL parse (root's flags, the
    subcommand name, and the subcommand's own options) was accepted and this
    command's body is about to run -- exactly Rust's `Ok(cli)` arm, and exactly
    the boundary `main()` cannot otherwise see from the outside.

    The `--format` parameter is found by its OPTION STRING rather than by the
    `output_format` parameter name every command happens to use today: the name
    is a convention 32 modules share, the option string is the contract.
    """

    def invoke(self, ctx):
        global _dispatched_json_mode
        param = next((p for p in ctx.command.params if "--format" in p.opts), None)
        value = ctx.params.get(param.name) if param is not None else None
        # `OutputFormat.JSON` (an enum), the plain string a `ValidateOutputFormat`
        # or a future non-enum choice would give, or `None` for the commands
        # whose `--format` defaults to `None` rather than `TEXT` -- all three
        # collapse to "is this the envelope channel".
        _dispatched_json_mode = getattr(value, "value", value) == "json"
        return super().invoke(ctx)


# Registered with a STATIC import, deliberately -- PyInstaller follows the
# static import graph only, so a pkgutil/importlib auto-registry works from
# source and produces a frozen `tan` that cannot find its own commands (see
# `tan.commands.__init__`). Registering here rather than with a decorator in
# the command module keeps `tan.commands.*` free of any `tan.cli` import,
# which would otherwise be a cycle. Each registration also carries a
# `rich_help_panel` keyword that groups commands into six titled panels on
# `--help`; a new command gains grouping with the same one-line edit that
# registers it.
app.command("bootstrap", rich_help_panel="Setup")(bootstrap)
app.command("build", rich_help_panel="Build & run")(build)
app.command("clean", rich_help_panel="Build & run")(clean)
app.command("completion", rich_help_panel="Setup")(completion)
app.command("debug-config", rich_help_panel="Hardware")(debug_config)
app.command("diff", rich_help_panel="Inspect & author")(diff)
app.command("doctor", rich_help_panel="Setup")(doctor)
app.command("examples", rich_help_panel="Start a project")(examples)
app.command("explain", rich_help_panel="Start a project")(explain)
app.command("faultdecode", rich_help_panel="Hardware")(faultdecode)
app.command("flash", rich_help_panel="Hardware")(flash)
app.command("generate", rich_help_panel="Configure")(generate)
app.command("image", rich_help_panel="Build & run")(image)
app.command("init", rich_help_panel="Start a project")(init)
app.command("inspect", rich_help_panel="Inspect & author")(inspect)
app.command("kconfig", rich_help_panel="Configure")(kconfig)
app.command(
    "lock", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(lock)
app.command(
    "migrate", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(migrate)
app.command("model", rich_help_panel="Configure")(model)
app.command("monitor", rich_help_panel="Hardware")(monitor)
app.command("new-som", rich_help_panel="Inspect & author")(new_som)
app.command("pinmux", rich_help_panel="Inspect & author")(pinmux)
app.command("presets", rich_help_panel="Start a project")(presets)
app.command(
    "quality", context_settings=FORWARD_CONTEXT_SETTINGS, rich_help_panel="Configure"
)(quality)
app.command("run", rich_help_panel="Build & run")(run)
app.command("scaffold", rich_help_panel="Start a project")(scaffold)
app.command("sdk", rich_help_panel="Setup")(sdk)
app.command("size", rich_help_panel="Build & run")(size)
app.command("support-bundle", rich_help_panel="Inspect & author")(support_bundle)
app.command("trace", rich_help_panel="Inspect & author")(trace)
app.command("validate", rich_help_panel="Configure")(validate)

# Every command dispatches through `_DispatchedCommand` -- assigned by walking
# the registration table above rather than by threading `cls=` through all 32
# `app.command(...)` calls, for the same reason `_SUBCOMMAND_NAMES` below is
# DERIVED and not retyped: a command registered next year is covered by the one
# line that registers it, and there is no second place to forget. `cls` is a
# real `CommandInfo` field (`typer.models.CommandInfo`), read by
# `typer.main.get_command_from_info` as `command_info.cls or TyperCommand` when
# `get_command(app)` builds the Click tree -- which happens lazily, well after
# this module finishes importing.
for _info in app.registered_commands:
    _info.cls = _DispatchedCommand

#: Every registered subcommand name, DERIVED from the `app.command(...)` table
#: above rather than retyped beside it (tan-cli#378). Used only to find the
#: argv BOUNDARY `_reorder_global_flags` moves a leading global flag across;
#: it is never itself treated as a flag.
#:
#: Hand-kept, this was a second place to forget a command -- and forgetting one
#: here is silent: a name missing from the set is not recognised as the
#: boundary, so the whole rewrite aborts and every leading global flag on that
#: command goes back to being a Click "No such option". Deriving it means a
#: command registered next year is covered by the single `app.command(...)`
#: line that registers it.
#:
#: `CommandInfo.name` is the explicit name every registration above passes;
#: `get_command_name` is Typer's own `snake_case` -> `kebab-case` fallback, for
#: a future registration that omits it (`app.command()(debug_config)` would
#: otherwise contribute `None`).
_SUBCOMMAND_NAMES = frozenset(
    info.name or get_command_name(info.callback.__name__)
    for info in app.registered_commands
)

#: clap's `GlobalArgs` (`crates/tan-cli/src/cli.rs` lines 24-73) PLUS
#: `--format`: every field there is `#[arg(long, global = true, ...)]`, so clap
#: accepts each on EITHER side of the subcommand name, and `--format` is one of
#: them. `--version` is not -- it lives on `Cli` directly in clap, not
#: `GlobalArgs`, and is root-only on both sides already.
#: Value: the flag's arity (1 = takes a value, 0 = boolean).
#:
#: Imported from `tan.core.global_flags` rather than hand-copied a second
#: time (tan-cli#261): that module is also what
#: `tan.core.global_flags.accept_global_flags` reads to decide which flags a
#: command is missing, so this reorder table and the per-command injection
#: list cannot drift apart the way two independent hand-written copies of
#: clap's `GlobalArgs` field list eventually would. `--format` is in the
#: relocation table but NOT the injection list -- see `RELOCATABLE_FLAG_ARITY`
#: there for why the two must stay separate.
_RELOCATABLE_FLAG_ARITY: dict[str, int] = RELOCATABLE_FLAG_ARITY

#: The oracle's global `--format` only ever had two values (measured:
#: `target/debug/tan.exe --format sarif validate` and `... --format bogus
#: new-som --sku FOO` both answer `error: invalid value '<x>' for '--format
#: <FORMAT>' [possible values: text, json]` at exit 2, in BOTH argv
#: positions) -- but this port's global `--format` is wider than that
#: (tan-cli#403): `_format_callback` accepts `_every_declared_format()`, the
#: union over every registered command's own `--format`, not a hard-coded
#: pair. `_reorder_global_flags` has to relocate that SAME domain, via the
#: SAME function, or a value outside the oracle's two but inside a real
#: command's own domain (`diagnostic-v1`/`sarif`, `validate`'s) collapses the
#: rewrite and drops the subcommand entirely instead of relocating past it
#: (tan-cli#433) -- a second, hand-kept copy of this set is exactly the drift
#: that produced #433 in the first place.


def _reorder_global_flags(argv: list[str]) -> list[str]:
    """Move a leading GLOBAL flag (`_RELOCATABLE_FLAG_ARITY`) from before the
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

    `--format` is relocated by exactly this mechanism too (tan-cli#378), and
    that is the WHOLE of how it is global now. It used to be excepted here --
    left sitting ahead of the subcommand for `root`'s own `--format` to catch
    and stash on `ctx.obj`, with a hand-written set of commands allowed to
    read it back. That set reached 12 of the 32 registered commands and
    stopped; the other 20 (`tan --format json doctor`, `... model list`,
    `... sdk current`, `... validate`, ...) were refused at rc=2 with a
    `cli.parse-error` envelope naming `command: "cli"` -- the wrong command
    for a machine caller to key off, on argv the oracle accepts. Relocation
    needs no such set because it does not depend on a command opting in:
    every registered command declares AND reads its own `--format` (the
    position every golden already uses, and where each command's own
    "'x' (choose from 'text', 'json')" validation lives), so moving the token
    there hands it to a parameter that acts on it. A command added later is
    covered by the `app.command(...)` line that registers it, with nothing
    else to remember -- pinned by
    `tests/commands/test_cli_global_flags.py`, which derives its cases from
    that same registration table.

    `--format`'s relocatable domain is `_every_declared_format()` -- the union
    over every registered command's own `--format`, not just the oracle's
    `text`/`json` pair (tan-cli#403) -- because `root`'s own eager
    `_format_callback` accepts that identical union; the two have to read the
    SAME set or a value `_format_callback` would happily accept gets treated
    here as not-the-flag-at-all and collapses the rewrite instead of
    relocating past the subcommand name (tan-cli#433). A value outside even
    that union -- `bogus` -- still is not this flag, and the rewrite still
    collapses to just that `--format <value>` pair so `root` refuses it at
    parse time, before any command resolves -- see the `--format` branch below
    for why leaving the rest of argv in place does not reach the refusal.

    Deliberately conservative otherwise: any OTHER token before the first
    subcommand name that is not a recognised global flag (or that flag's
    value) — `--help`, `--version`, or a bare positional — aborts the rewrite
    and returns `argv` untouched, so every existing argv shape (a normal `tan
    build ...` with zero leading tokens is a no-op by construction; `--version`,
    a bad command, a bare invocation, all of which have no subcommand token to
    move anything after) sees the exact argv it always has.
    """
    moved: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token in _SUBCOMMAND_NAMES:
            return [token, *moved, *argv[i + 1 :]]
        name = token.split("=", 1)[0]
        arity = _RELOCATABLE_FLAG_ARITY.get(name)
        if arity is None:
            return argv  # not a recognised global flag and not the subcommand
        if name == "--format":
            # The relocatable domain is `_every_declared_format()`, not a
            # hard-coded `text`/`json` pair (tan-cli#433, a regression of
            # tan-cli#403): `_format_callback` already accepts the union over
            # every registered command's `--format`, including `validate`'s
            # `diagnostic-v1`/`sarif`, so this rewrite has to relocate that
            # SAME set or it refuses a value `root` itself would have
            # accepted, collapsing the argv and dropping the subcommand along
            # with it -- exactly #433's `tan --format diagnostic-v1 validate
            # --offline`, which lost `validate` entirely and answered "a
            # command is required" instead of the diagnostic-v1 document `tan
            # validate --offline --format diagnostic-v1` produces.
            #
            # A value outside even that union is not this flag at all, and the
            # oracle refuses such a VALUE at PARSE time, before the command
            # body runs -- measured: `tan --format bogus new-som --sku FOO`
            # rejects the value and does no work, while a rewrite that let the
            # value through made it something only `new-som`'s own body
            # validates, and `new-som` WRITES metadata files. The rewrite
            # therefore collapses to exactly the two tokens that reproduce
            # that refusal through `root`'s own `_format_callback`, dropping
            # the rest of argv -- see the `return` below for why leaving argv
            # alone does not work.
            if "=" in token:
                value = token.split("=", 1)[1]
            else:
                value = argv[i + 1] if i + 1 < n else None
            if value not in _every_declared_format():
                # Just returning `argv` here (the first cut of tan-cli#378)
                # does NOT reach that refusal, for two measured reasons.
                #
                # (1) `root`'s `--format` is a plain, non-`multiple` Click
                # option, so Click's parser overwrites `state.opts` on each
                # occurrence and only ever type-casts the LAST one
                # (`click.parser._match_long_opt`) -- `_format_callback` is
                # never handed the bad value at all when a second `--format`
                # follows. Measured: `tan --format bogus --format json new-som
                # --sku FOO` reached `new-som`'s BODY (its SDK-root preflight
                # message printed), where the oracle answers `invalid value
                # 'bogus'` having run nothing, and where a resolvable
                # `--sdk-root` means real metadata writes.
                #
                # (2) Returning the untouched argv also strands every global
                # flag already scanned ahead of this one back in the
                # pre-subcommand position `root` does not declare: `tan
                # --verbose --format bogus validate` answered `No such option:
                # --verbose` -- naming a flag this port supports, for an argv
                # whose actual fault is the `--format` value.
                #
                # Collapsing to `--format <bad-value>` alone fixes both: it is
                # the only argv that makes `root` refuse THIS value, at parse,
                # before any command resolves. Everything dropped is either a
                # global flag (accepted, never read on a refused invocation) or
                # the subcommand the oracle likewise never runs. The `=`
                # spelling collapses to its single token; a trailing `--format`
                # with no value at all collapses to just the flag, which Click
                # reports as "Option '--format' requires an argument." (exit 2,
                # matching clap's own "a value is required" refusal).
                return argv[i : i + (1 if "=" in token else 2)]
        if "=" in token or arity == 0:
            moved.append(token)
            i += 1
            continue
        if i + 1 >= n:
            return argv  # "--sdk-root" with no value: let Click report it natively
        moved.extend((token, argv[i + 1]))
        i += 2
    return argv  # no subcommand token ever found


def _value_taking_options(command: str | None) -> frozenset[str]:
    """Every option STRING that swallows the next argv token as its value --
    `root`'s own, plus `command`'s when argv names one.

    Read off Click's real parameter objects rather than a table in this
    module: the options that matter here are per-command (`scaffold --name`,
    `explain --template`, `init --destination`), so no table here could know
    them, and a table that tried would be stale the first time a command grew
    an option. `--help`/`--version` and every other boolean are excluded by
    `is_flag`; `click.Argument`s are excluded by the leading-dash test (their
    `opts` carry the parameter NAME, not a flag).
    """
    group = get_command(app)
    params = list(group.params)
    subcommand = group.commands.get(command) if command is not None else None
    if subcommand is not None:
        params += list(subcommand.params)
    return frozenset(
        opt
        for param in params
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("-") and not getattr(param, "is_flag", False) and param.nargs == 1
    )


def _wants_help(argv: list[str]) -> bool:
    """Whether `--help` appears in an OPTION position -- not as some other
    option's VALUE (tan-cli#394).

    A plain `"--help" in argv` was wrong in a way `--help`'s eagerness hides:
    Click's parser takes the next token as a value-carrying option's argument
    WITHOUT checking whether it looks like an option
    (`click.parser._get_value_from_state` pops `rargs[0]` unconditionally), so
    a `--help` consumed that way NEVER reaches the eager help callback. The
    pre-scan believed it had; `main()` then routed the whole argv through
    `_emit_help_envelope`, which runs the command under `CliRunner` and
    reports its captured output as one `command: "cli"` envelope. Measured:
    `tan scaffold --format json --name --help --destination out` wrote three
    real files and answered `{"command":"cli","ok":true,...,"data":{"message":
    "<the escaped scaffold envelope>"}}` -- so a consumer reading
    `data.written` saw `[]` for a run that wrote, and `tan explain --format
    json --template --help` re-labelled a real `explain.template-unknown`
    refusal as `cli.parse-error`. Text mode reported the truth in both cases;
    only the JSON channel lied.

    Walking argv with the real arity table instead means the two channels
    agree: a `--help` in value position is left to the command, which then
    emits its OWN envelope (the same one text mode describes), and a `--help`
    in option position still short-circuits here.
    """
    takes_value = _value_taking_options(
        next((token for token in argv if token in _SUBCOMMAND_NAMES), None)
    )
    i = 0
    while i < len(argv):
        token = argv[i]
        # `--` ends option parsing for Click exactly as it does for clap, so a
        # `--help` after it is a positional argument and the eager help callback
        # never fires. Measured against the oracle: `tan explain -- --help`
        # exits 2 with a refusal, not help.
        if token == "--":
            return False
        if token == "--help":
            return True
        # `--opt=value` carries its value inline, so it consumes one token; a
        # bare value-taking `--opt` eats the token after it, whatever it says.
        i += 2 if token in takes_value else 1
    return False


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


def _format_callback(ctx: typer.Context, value: str | None) -> str | None:
    """Validates `--format`'s value the moment Click parses it. Marked
    `is_eager=True` on the option below, alongside `--version`
    (`_version_callback`), so the two race on ARGV POSITION rather than on
    `root`'s declaration order -- Click sorts eager params by the order they
    actually appeared on the command line, not by where they're declared
    (`click.core.iter_params_for_processing`). Verified against the oracle
    (`target/debug/tan.exe`): `tan --format bogus --version` exits 2 on the
    bad value without ever reaching `--version` (`--format` comes first in
    argv); `tan --version --format bogus` instead prints the version and
    exits 0, never validating the value that comes after it (`--version` wins
    the race and exits before `--format` is ever processed). Without
    `is_eager=True` here, `--version`'s own eager callback would ALWAYS run
    first regardless of position -- eager beats non-eager unconditionally --
    which would have broken the already-tested `--format json --version` /
    `--format=json --version` cases (`test_version_under_format_json_is_an_
    envelope_not_a_bare_line`): `--version`'s callback would fire before
    `--format`'s value had even been parsed.

    clap validates `--format`'s VALUE eagerly too -- measured: `tan --format
    bogus`, `tan --format bogus --version`, and `tan --format "" build` (an
    empty value counts as invalid: clap says "a value is required for
    '--format <FORMAT>' but none was supplied") all exit 2 on the value
    itself. `ctx.fail()` gives the same Click UsageError shape (exit 2) every
    other CLI mistake here already gets.

    Reaches the argv shapes where `--format` is still parsed by `root` since
    tan-cli#378 relocated the pre-subcommand position: no subcommand at all
    (bare `tan --format bogus`, `tan --format bogus --version`), a rewrite
    `_reorder_global_flags` aborted on some earlier unrecognised token (which
    is itself a usage error) -- and, deliberately, EVERY pre-subcommand
    `--format` whose value is outside `_ROOT_FORMAT_VALUES`, which that
    function leaves in place precisely so this refusal is what a caller gets.
    That is the oracle's own ordering: it rejects the value before the
    subcommand runs, so `tan --format bogus new-som --sku FOO` writes nothing.
    With `text`/`json` and a subcommand named, the value lands on that
    command's own `--format` instead, whose identically-shaped "'x' (choose
    from 'text', 'json')" `BadParameter` -- the first statement in every
    command body, before any work -- refuses the post-subcommand position at
    the same exit 2.
    """
    if value is None:
        return value
    # The domain here is the UNION over every registered command, not the
    # two-value list this used to hard-code (tan-cli#403). It has to be: the
    # callback is EAGER and fires before `ctx.invoked_subcommand` is known, so
    # a `text`/`json` domain refused `tan --format diagnostic-v1 validate
    # --offline` at exit 2 while the post-subcommand spelling of the same run
    # emitted the document. `validate` really does declare four formats
    # (`ValidateOutputFormat`), and the two spellings are meant to be one flag.
    #
    # Eagerness itself stays, and is not negotiable: the docstring above pins
    # measured oracle behaviour where `tan --format bogus --version` must exit
    # 2 BEFORE `--version` runs. Moving the whole check into `root`'s body
    # would hand that race to `--version`.
    #
    # This is only the OUTER bound. `root` narrows to the invoked subcommand's
    # own list once it knows which one that is.
    allowed = _every_declared_format()
    if value not in allowed:
        ctx.fail(invalid_format_message(value, allowed))
    return value


def _every_declared_format() -> tuple[str, ...]:
    """Every `--format` value ANY registered command accepts, first-seen order.

    Derived from the live command tree rather than listed. A hand-kept union
    would be a second copy of the same fact, and a command that gained a
    fourth format would then be refused here by a stale copy -- which is the
    exact shape of the bug this replaced.
    """
    seen: dict[str, None] = {}
    group = get_command(app)
    for name in sorted(getattr(group, "commands", {})):
        for value in declared_formats(group.commands[name]):
            seen.setdefault(value, None)
    # `or` guards the degenerate case only: a tree with no `--format` anywhere
    # would otherwise refuse every value, including the two that always worked.
    return tuple(seen) or ("text", "json")


def _version_callback(ctx: typer.Context, value: bool) -> bool:
    """Genuinely eager `--version`, via Typer's own `is_eager=True` +
    `callback=` mechanism (the option below; the same idiom
    `click.version_option()` uses) -- not a hand-rolled `sys.exit` scattered
    through `root`'s body.

    tan-cli#326: a bare `return` from inside `root`'s function BODY does not
    stop Click's own group dispatch. `click.core.MultiCommand.invoke`
    resolves the subcommand and calls the group callback's body BEFORE it
    invokes the subcommand, so a body that just `return`s (the pre-fix shape)
    falls straight through to the subcommand running anyway -- `tan --version
    init --template zephyr-app --destination <dir>` printed the version AND
    created the project. Raising `typer.Exit` from an EAGER option's own
    callback instead stops the run during argument PARSING itself
    (`Command.parse_args`, called from `make_context`), which happens before
    `MultiCommand.invoke` -- and therefore before subcommand resolution -- is
    ever reached; `Command.main` wraps `make_context` and `invoke` in the SAME
    try/except Exit, so this is caught and converted to a real process exit
    exactly the same way a `typer.Exit` raised from the body would be
    (verified empirically: a `CliRunner` probe with a dummy subcommand behind
    two eager options confirms the subcommand never runs when the earlier one
    raises).

    `ctx.resilient_parsing` guards the same case Click's own `version_option`
    guards: shell-completion parsing, which must not have side effects (not
    reachable here today -- `add_completion=False` -- but the guard is the
    documented idiom, kept for when that changes).

    The envelope-vs-plain-text choice below is a raw scan of the real argv
    (`_wants_json`, the SAME textual scan `main()` uses to route `--help`),
    not `ctx.params.get("output_format")` -- deliberately, and NOT what a
    first pass at this fix reached for. `--format`'s own callback is eager
    too, but Click only races two eager options against each other while
    BOTH are options of the SAME command (`root`); `tan --version sdk current
    --format json` puts `--format json` on the OTHER side of the subcommand
    boundary entirely -- Click hands `root` only `["--version"]` and leaves
    `["sdk", "current", "--format", "json"]` as protected args for `sdk`'s own
    parser, so `root`'s `output_format` parameter is `None` there regardless
    of processing order; `ctx.params` genuinely never has the answer. Yet the
    oracle DOES fold that trailing `--format json` into one JSON version
    envelope (tan-cli#326's own repro; verified against `target/debug/tan.exe
    --version sdk current --format json`) -- clap's real version handling
    reads the format value from a scan of the whole process argv at the
    moment it fires, not from however far its own structured parse had
    gotten. A raw scan is what reaches that value from here too. It also
    keeps the three narrower, Click-parseable cases correct as a side effect
    (verified against the oracle for all four): `--format json --version` and
    `--version --format json` both choose JSON (the literal text is present
    either way); `--version --format bogus` chooses plain text (the literal
    "json" is absent, so this never even asks whether "bogus" is a valid
    value -- matching the oracle exactly, which also never validates it once
    `--version` has already won).
    """
    if not value or ctx.resilient_parsing:
        return value
    if _wants_json(sys.argv[1:]):
        # Under `--format json`, stdout is the envelope channel even for
        # `--version`: Rust routes clap's version output through
        # `emit_parse_error` (main.rs), giving exit 0, no `issues`, and the
        # rendered line as `data.message`.
        emit(
            Envelope(
                "cli",
                Project(root=None, board_yaml=None),
                {"message": f"tan {TAN_VERSION}"},
                [],
                ExitCode.SUCCESS,
            )
        )
    else:
        # MUST match /^tan \d+\.\d+\.\d+/ -- the extension rejects the binary
        # otherwise (alp-sdk-vscode/src/alpCli/service.ts:107-121).
        typer.echo(f"tan {TAN_VERSION}")
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True
    ),
    output_format: str = typer.Option(
        None,
        "--format",
        metavar="FORMAT",
        help="Output format: text or json.",
        callback=_format_callback,
        is_eager=True,
    ),
) -> None:
    """tan CLI -- board configuration, generation, and project tooling."""
    # The `ctx.obj["format"]` seam thirteen command modules read as a fallback
    # (`output_format or (ctx.obj or {}).get("format") or "text"`). Since
    # tan-cli#378 relocated the pre-subcommand `--format` past the subcommand
    # name, those reads land on the command's OWN parameter and this is `None`
    # on every argv that names a command -- kept because the seam is what
    # makes the two positions interchangeable if a future argv shape reaches a
    # command without going through `_reorder_global_flags`, and because
    # dropping it would leave `ctx.obj` unset for readers that would then have
    # to be edited in lockstep for no behavioural gain.
    ctx.obj = {"format": output_format}
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
        ctx.fail(
            "a command is required.\n"
            "New here? `tan doctor` checks this host, `tan init` creates a project, "
            "`tan build` builds it.\n"
            "`tan --help` lists all commands by category."
        )
    # No third branch here any more (tan-cli#378). This callback used to refuse
    # a pre-subcommand `--format` for any command outside a hand-written
    # allowlist of commands that honoured it -- the refusal was correct given the
    # premise (a command that merely ACCEPTED the flag and ran in text mode
    # would exit 0 with nothing on stdout), but the premise stopped holding
    # once `_reorder_global_flags` relocates `--format` to where the command's
    # own, always-read parameter picks it up. The allowlist reached 12 of 32
    # commands and stalled, so 20 valid oracle invocations exited 2 with
    # `command: "cli"`; deleting the branch is what makes the flag global for
    # all 32 with nothing left to keep in sync.


def _wants_json(argv: list[str]) -> bool:
    """Textual scan for ``--format json`` / ``--format=json``, mirroring
    Rust's ``wants_json`` (crates/tan-cli/src/main.rs). Needed because a
    usage error (bare invocation, an unknown command, a bad flag) means Click
    exits via its own machinery before any option ever gets parsed into
    something this code could otherwise trust.

    Deliberately still `--`-blind and arity-blind, exactly like Rust's: this is
    the PARSE-FAILURE arm's scan, not the run's channel. Since tan-cli#546 the
    channel a DISPATCHED command answers on comes from `_DispatchedCommand` --
    the command's own resolved `--format` -- and this scan only decides what a
    run that never reached a command body does. Making it cleverer is what the
    two reverted attempts on #546 did; see `_dispatched_json_mode`'s own comment
    for the oracle measurement that rules out any third textual shape too.
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


#: The POSIX 128+SIGINT exit code Ctrl-C lands on. It does NOT reach `main()`
#: as a raw `KeyboardInterrupt`: Typer's own command wrapper catches it wherever
#: in the command's call stack it was raised and re-raises `Exit(130)`, which
#: Click's `Command.main` turns into a plain `sys.exit(130)`. So by the time the
#: handler in `main()` sees it, an interrupted run is indistinguishable from any
#: other non-zero exit that emitted no envelope -- and used to fall into the
#: same `_usage_error_envelope` branch every genuine Click usage error does.
#: Measured on `dev` (SIGINT 4s into a real `tan build --plan-from ... --execute
#: --format json` whose slice spawns `sleep 30`): exit 130 and
#: `{"command":"cli","exitCode":130,...,"code":"cli.parse-error","message":
#: "invalid command line invocation"}` -- an envelope asserting the COMMAND LINE
#: was invalid for a run that was already programming a board, at an exit code
#: outside the contract's fixed 0-5 set (tan-cli#491 defect 4).
_SIGINT_EXIT_CODE = 130

#: What the interrupt envelope says on both channels. Not command-specific:
#: `KeyboardInterrupt` reaches the handler identically from all 32 subcommands,
#: and the command that was running has already lost its own `data` by then.
_INTERRUPTED_MESSAGE = "interrupted (Ctrl-C)"


def _interrupted_envelope() -> str:
    """The one JSON envelope an interrupted `--format json` run reports.

    `cli.interrupted` names what actually happened. `ExitCode.RUNTIME_FAILURE`
    (1), not 130: the envelope contract fixes `exitCode` to `tan.exit_codes.
    ExitCode`'s 0-5, and the wire invariant `process exit code ==
    envelope.exitCode` (tan-cli#327) means the two cannot disagree -- so the
    caller of this function exits 1 as well. A DELIBERATE divergence from the
    oracle, which has no SIGINT handler at all and simply dies from the signal
    with zero bytes on stdout; the port cannot copy that and still keep "stdout
    is the envelope channel" for a `--format json` run. TEXT mode is untouched
    and still exits 130 through Click's own machinery, so an operator's shell
    still sees the signal.
    """
    env = Envelope(
        "cli",
        Project(root=None, board_yaml=None),
        {"message": _INTERRUPTED_MESSAGE},
        [Issue("cli.interrupted", "error", _INTERRUPTED_MESSAGE)],
        int(ExitCode.RUNTIME_FAILURE),
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


def _reconfigure_stdio() -> None:
    """Force UTF-8, LF-only stdout/stderr, once, at the process boundary.

    Every command downstream just `print()`s -- correctness here is what
    makes that safe. A normal Windows `TextIOWrapper` translates a written
    `\\n` to `\\r\\n` and encodes with the process's ANSI code page, neither of
    which the oracle's `serde_json`/`println!` output does. Both are visible
    on stdout, not just in theory: measured, `tan completion --shell bash`
    was 3975 bytes with 108 `\\r` where the oracle's was 3867 bytes with zero
    -- and the emitted script is a hard syntax error when sourced in a strict
    bash (`syntax error near unexpected token $'{\\r''`); `clean --format
    json` and a bare `--format json` both ended `\\r\\n` too, so this is a
    process-wide stdout-newline defect, not a completion-specific one. A
    frozen/piped stream may not implement `.reconfigure()` (e.g. a test
    harness's in-memory buffer) -- `hasattr` skips those rather than raising,
    since the fix only matters for the real console/pipe case it targets.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")


def main() -> None:
    """Process entrypoint.

    Text mode (the default) lets Click run standalone: it already prints its
    own errors/help to stderr and exits with the right code, which is exactly
    the contract there -- Click's exact RENDERING of a usage error carries no
    promises of its own. That rule outlived its statement of record:
    ``tests/parity/oracle.py`` declared it, scoped to the Rust-vs-Python diff,
    and went with the oracle axis in tan-cli#269, so THIS docstring is where it
    lives now. Read it narrowly. What is unpinned is Click/rich's wording and
    box-drawing, because nothing diffs it against the retired binary's clap
    renderer. tan's stderr as a CHANNEL is pinned hard -- ~500 assertions under
    ``python/tests/`` take it as their subject, ``contract/envelopes``' goldens
    require it EMPTY under ``--format json``, and
    ``tests/test_stdout_bytes.py::test_stderr_also_has_no_cr`` forbids ``\r``
    in it.

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
    global _dispatched_json_mode

    _reconfigure_stdio()
    # Reset per run, not merely initialised at import: `main()` is called more
    # than once per process by the in-process tests, and a stale answer from the
    # previous run would decide this one's output channel.
    _dispatched_json_mode = None
    original_argv = sys.argv[1:]
    argv = _reorder_global_flags(original_argv)
    sys.argv = [sys.argv[0], *argv]
    # Scanned off the ORIGINAL argv, not the rewritten one: `_reorder_global_flags`
    # COLLAPSES to just the offending `--format <bad-value>` pair the moment a
    # pre-subcommand `--format` carries a value the oracle's global `--format`
    # does not have (see its own docstring), which drops every later token --
    # including a SECOND, accepted `--format json` the oracle still notices when
    # it decides which channel to answer on. Measured against the oracle: `tan
    # --format sarif --format json validate --offline` answers a 423-byte JSON
    # envelope on stdout (`wants_json` in Rust's own `main.rs` is a textual scan
    # of the whole process argv too, not a re-parse of whatever clap's actual
    # value resolution landed on). Reading the rewritten `argv` here instead
    # left that same invocation with `--format json` already dropped by the
    # collapse, so `json_mode` came back `False` and the usage-error fallback
    # below never ran: `main()` fell into the text-mode branch, which prints
    # Click's error to stderr only -- zero bytes on stdout, a hard violation of
    # "stdout is the envelope channel" for an argv the caller marked `--format
    # json`.
    json_mode = _wants_json(original_argv)

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
        # `prog_name="tan"` -- Click otherwise derives the name it prints in
        # `Usage: ...` from `os.path.basename(sys.argv[0])`, which is the
        # frozen binary's OWN filename (`tan.exe` locally, or whatever
        # `release.yml` renamed the uploaded asset to, e.g.
        # `tan-x86_64-pc-windows-msvc.exe`, if a user runs the download in
        # place). Pinned here rather than left to derive, same as
        # `_emit_help_envelope`'s `prog_name="tan"` above.
        app(prog_name="tan")
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
            # `prog_name="tan"` -- see the text-mode call above; it matters MORE
            # here, since a Click usage error's rendered message (captured via
            # `_TeeStderr`) is what `_usage_error_envelope` folds verbatim into
            # `data.message`, a machine-readable envelope field a consumer
            # should not see vary with how the binary happened to be named.
            app(prog_name="tan")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                code = int(ExitCode.SUCCESS)
            elif not isinstance(code, int):
                code = int(ExitCode.RUNTIME_FAILURE)
            if not envelope_emitted():
                # `_dispatched_json_mode is False` -- a command's body really
                # RAN, and its OWN `--format` resolved to text. That run owes
                # stdout nothing (text mode carries no envelope promise), so
                # neither of the two fallbacks below may fire for it: printing
                # one is how `tan quality -- --format json` used to answer a
                # forwarded west failure with `command:"cli"` /
                # `cli.parse-error` on stdout for a caller that asked for text
                # (tan-cli#546). `None` -- nothing dispatched -- means the parse
                # itself failed, which is precisely Rust's `Err` arm, and the
                # textual `_wants_json` scan that got us into this branch is
                # exactly what Rust consults there.
                ran_in_text_mode = _dispatched_json_mode is False
                if code == _SIGINT_EXIT_CODE and not ran_in_text_mode:
                    # Ahead of the generic usage-error fallback: an interrupted
                    # run is not a usage error, and `sys.exit` here (rather than
                    # re-raising `exc`) is what keeps the process code equal to
                    # the `exitCode` the envelope just printed. See
                    # `_interrupted_envelope`.
                    print(_interrupted_envelope())
                    sys.exit(int(ExitCode.RUNTIME_FAILURE))
                if code != 0 and not ran_in_text_mode:
                    print(_usage_error_envelope(code, captured_stderr.getvalue()))
                raise
            # tan-cli#327: `Envelope.to_json()`'s serialize-failure fallback
            # can report a different `exitCode` (5, `envelope.serialize-
            # failed`) than the command's own `typer.Exit(code)` -- the
            # command chose `code` BEFORE `emit()` ever tried to encode the
            # envelope, so a fallback there leaves `code` stale. The wire
            # invariant is `process exit code == envelope.exitCode`
            # (mirrors the Rust `json_exit_code` boundary and its
            # `json_exit_code_follows_serialize_failure_fallback_not_stale_
            # run_exit` test); `emit()` is the one place that already knows
            # the REAL code, so read it back rather than re-deriving
            # anything from the JSON this process just printed.
            emitted_code = envelope_emitted_exit_code()
            if emitted_code is not None and emitted_code != code:
                sys.exit(emitted_code)
            raise
    finally:
        sys.stderr = real_stderr
