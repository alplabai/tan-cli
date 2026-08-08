# SPDX-License-Identifier: Apache-2.0
"""`tan faultdecode` -- decode an ARM Cortex-M (ARMv8-M) fault dump.

Port of `alp_cli/faultdecode.py` (alp-sdk `scripts/alp_cli/faultdecode.py`).
Until now `tan faultdecode` was a thin forwarder to `python -m alp_cli
faultdecode` (`crates/tan-cli/src/commands/sdk_cli.rs`); this is the native
replacement, so `--json`'s OUTPUT CONTRACT is that forward's own: the SDK's
`--json` report shape (`fault_detected`/`inputs`/`flags`/`addresses`/
`root_cause`/`symbols`), unwrapped, on stdout. `sdk_cli.rs`'s success path
streamed the child's stdio through untouched; `--json` reproduces exactly what
a caller already received through that pipe, so nothing downstream (a saved
script, the forwarder itself) observes a change.

`--format json` is a DIFFERENT surface, and tan-cli#399 is the decision to
make it one: it emits tan's `{command,ok,exitCode,project,data,issues}`
envelope with the report as `data`. `--format json` is what the vscode
extension drives (`contract/README.md:4-8`), and its `isEnvelope` guard
(`alp-sdk-vscode src/alpCli/service.ts:705-716`) requires `command`, `ok`,
`exitCode` and `issues[]` -- the unwrapped report has none of them, so
`parseEnvelope` returned `null`, `classifyOutcome` saw rc 0, and a successful
decode rendered as the literal string `Command completed.` with `root_cause`
on the wire and unreachable. The v0.4.1 oracle maps the global `--format json`
onto the child's `--json` and therefore prints the raw report for both
spellings; that divergence is deliberate and is the whole content of the fix.
`--json` wins when both are given, so an existing caller cannot have its shape
changed by a `--format json` something else in the chain added.

A firmware engineer pastes the fault registers a HardFault prints (CFSR, HFSR,
optionally DFSR, BFAR, MMFAR) and gets back the human-readable cause plus, when
an ELF is supplied, the faulting symbol and `file:line` -- instead of staring at
CFSR hex.

The decode/parse/render core lives in `tan.core.faultdecode` (pure, no I/O);
this module owns the CLI surface plus the two things that touch the outside
world: reading a pasted dump (`--file`/stdin) and best-effort `addr2line`
symbolication.

It is strictly HW-free: pure register arithmetic. Symbolication is best-effort
and optional -- if no ELF or no `addr2line`-class tool is found it is skipped,
never fatal. Exit code is 0 for any successful analysis (even "no fault flags
set"); only genuinely bad input (no registers, an unparseable value, or a bad
`--elf`/`--file` path) is nonzero (`ExitCode.VALIDATION_FAILURE`, matching the
original's `click.BadParameter`/`click.UsageError`, both exit 2).
"""

from __future__ import annotations

import json as _json
import select
import subprocess
import sys
import threading
from pathlib import Path

import typer

from tan.commands.doctor_cmd import on_path
from tan.core.faultdecode import (
    Symbol,
    decode,
    parse_dump,
    report_to_json,
    render_human,
)
from tan.env import no_color_requested
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

_ADDR2LINE_TOOLS = ("arm-zephyr-eabi-addr2line", "llvm-addr2line", "addr2line")


def resolve_symbol(addr: int, elf: Path) -> Symbol | None:
    """Resolve ``addr`` to ``func`` + ``file:line`` via an addr2line-class tool.

    Tries ``arm-zephyr-eabi-addr2line`` then ``llvm-addr2line`` then plain
    ``addr2line``. Returns ``None`` (caller skips gracefully) if no tool is on
    PATH or the lookup fails -- symbolication is a convenience, never required.

    Uses `doctor_cmd.on_path`, NOT `shutil.which` (tan-cli#503): on Windows,
    `shutil.which` inserts `os.curdir` ahead of `$PATH`, so an
    `addr2line.exe`/`llvm-addr2line.exe`/`arm-zephyr-eabi-addr2line.exe`
    sitting at the root of a checked-out project is reported as "available".
    Spawning it by bare NAME afterwards (rather than the resolved absolute
    path) would reopen the same hole a second way -- `CreateProcess`'s own
    current-directory-first search -- even past a hardened probe, so the
    resolved absolute path from `on_path` is what gets spawned below, exactly
    like `size_cmd`/`doctor_cmd`/`flash_cmd`/`build/execute.py` already do for
    every other tool probe in this package.
    """
    tool = next((p for t in _ADDR2LINE_TOOLS if (p := on_path(t))), None)
    if tool is None or not elf.is_file():
        return None
    try:
        proc = subprocess.run(
            [tool, "-f", "-C", "-e", str(elf), f"0x{addr:x}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env-dependent
        return None
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    func = lines[0]
    location = lines[1] if len(lines) > 1 else "??:?"
    if func in ("??", "") and location in ("??:?", "??:0", ":?"):
        return None  # tool ran but knows nothing about this address
    return Symbol(addr=addr, func=func or "??", location=location or "??:?")


def _use_color(no_color: bool) -> bool:
    """`alp_cli.diagnostic._use_color(False if no_color else None)`: `--no-color`
    forces off, else `NO_COLOR` forces off, else follow stdout's own tty-ness --
    stdout, not stderr, because this command's human text goes to stdout (the
    original's `click.echo` default), matching the forwarded SDK behaviour this
    replaces."""
    if no_color:
        return False
    if no_color_requested():
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _parse_hexint(option: str, value: str | None) -> int | None:
    """A fault-register value, hex by default: these are CPU status registers,
    always read in hex, so a bare ``8200`` means ``0x8200`` (not decimal 8200)
    and ``0x8200`` works too. Mirrors the original's `_HexInt.convert`."""
    if value is None:
        return None
    text = value.strip()
    try:
        # base 16 accepts an optional 0x prefix and a bare hex run alike.
        return int(text, 16)
    except ValueError as err:
        raise typer.BadParameter(
            f"{value!r} is not a valid integer (try 0x...)", param_hint=f"--{option}"
        ) from err


#: How long the IMPLICIT stdin auto-consume waits for a non-TTY stdin to offer
#: something -- bytes, or EOF, both of which make it readable -- before giving
#: up and decoding from the flags alone (tan-cli#388).
#:
#: Not 0: `producer | tan faultdecode` races the producer's first write against
#: this process's own startup, and losing that race would drop a dump the user
#: really did pipe. Not unbounded either -- unbounded is the defect. A quarter
#: second is far longer than a scheduler hiccup and far shorter than the
#: indefinite block a held-open pipe used to cause.
_STDIN_READY_TIMEOUT_S = 0.25


def _stdin_offers_input() -> bool:
    """Whether reading `sys.stdin` will terminate promptly (tan-cli#388).

    `sys.stdin.read()` returns at EOF, and a pipe reaches EOF only when its
    last writer CLOSES it -- not when the writer merely stops writing. Every
    caller that spawns `tan` as a child with `stdin=PIPE` and does not close
    the write end (the default shape of `subprocess.Popen(..., stdin=PIPE)`)
    therefore used to block the child forever, with zero bytes on stdout and
    stderr and no exit code: worse than a malformed report, because there is
    nothing for the caller to classify.

    `select` reports a pipe readable both when bytes are buffered and when the
    writer has closed, so a real `echo ... | tan faultdecode` still reads its
    dump and only the open-and-idle pipe falls through.

    `select.select` answers this directly wherever it can, which is every
    POSIX host. It CANNOT on Windows -- there it accepts sockets only -- and an
    in-memory `CliRunner` stdin has no `fileno` at all
    (`io.UnsupportedOperation` derives from both `OSError` and `ValueError`).

    Falling back to "just read it" was the first shape of this fix, and it left
    the bug fully intact on Windows: measured on windows-latest,
    `test_an_idle_open_stdin_pipe_falls_through_to_the_no_dump_refusal` still
    hit its 20 s bound, because an idle open pipe with no registers supplied
    took the fallback and blocked exactly as before. A platform-specific
    readiness check meant a platform-specific hang, on a platform this repo
    gates as a required check.

    So the fallback is a BOUNDED read rather than a guess: the read still
    happens, still terminates at EOF, and still yields a dump that arrives
    inside the window -- but a writer that never writes and never closes costs
    `_STDIN_READY_TIMEOUT_S`, not the process.
    """
    try:
        ready, _, _ = select.select([sys.stdin], [], [], _STDIN_READY_TIMEOUT_S)
    except (OSError, ValueError):
        return _stdin_offers_input_by_reading()
    return bool(ready)


#: Filled by `_stdin_offers_input_by_reading` so `_read_dump` does not read a
#: second time -- those bytes are already consumed, and a pipe cannot be
#: rewound.
_PREREAD_STDIN: list[str] = []


def _stdin_offers_input_by_reading() -> bool:
    """The Windows / no-`fileno` path: read stdin on a daemon thread, bounded.

    Reading is the only way to learn whether a Windows pipe will ever deliver,
    so this reads -- but off the main thread, so `_STDIN_READY_TIMEOUT_S` is a
    real bound and not an aspiration. Whatever arrived is stashed in
    :data:`_PREREAD_STDIN` for `_read_dump` to return, because a pipe read
    cannot be undone and reading twice would drop the dump this exists to
    preserve.

    The thread is a daemon precisely because it may still be parked in
    `read()` at interpreter exit; it holds nothing but stdin, and leaving it
    is the correct outcome for a producer that never speaks.
    """
    got: list[str] = []

    def _drain() -> None:
        try:
            got.append(sys.stdin.read())
        except (OSError, ValueError):  # pragma: no cover - env-dependent
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    reader.join(_STDIN_READY_TIMEOUT_S)
    if not got:
        return False
    _PREREAD_STDIN.append(got[0])
    return bool(got[0])


def _read_dump(file_: str | None) -> str:
    """Read a pasted dump from --file, '-' (stdin), or piped stdin.

    `--file -` is the EXPLICIT opt-in and always reads stdin to EOF, whatever
    else is on the command line: the caller asked for that read by name, and a
    dump has no terminator other than EOF.

    The IMPLICIT read (no `--file` at all) is attempted UNCONDITIONALLY, flags
    or no flags (tan-cli#503): the command's own help promises "Explicit flags
    win over a parsed dump", true only if the dump is still read when flags
    are present too -- `faultdecode()`'s `pick()` merge relies on it. Safe
    against the tan-cli#388 hang either way: `_stdin_offers_input()` bounds
    the readiness probe to `_STDIN_READY_TIMEOUT_S` regardless of flags.

    `sys.stdin is None` (defect 3) happens with fd 0 closed -- a daemon/
    service parent, some CI runners, a frozen no-console launch -- and is
    guarded explicitly below, in both branches: a bare `.read()`/`.isatty()`
    on `None` used to raise `AttributeError` instead of the coded
    `faultdecode.no-registers` refusal. `.isatty()` is also wrapped in the
    same `try`/`except (AttributeError, ValueError)` `_use_color` already
    uses, for a stream that EXISTS yet still lacks `.isatty()`.
    """
    if file_ == "-":
        if sys.stdin is None:  # fd 0 closed (tan-cli#503): nothing to read.
            return ""
        return sys.stdin.read()
    if file_ is not None:
        return Path(file_).read_text(encoding="utf-8", errors="ignore")
    # Auto-consume piped stdin (non-tty) so `... | tan faultdecode` just works,
    # flags or no flags.
    if sys.stdin is None:
        return ""
    try:
        is_tty = sys.stdin.isatty()
    except (AttributeError, ValueError):
        return ""
    if is_tty or not _stdin_offers_input():
        return ""
    if _PREREAD_STDIN:
        # The readiness check had to consume stdin to answer (Windows, or a
        # stdin with no `fileno`). Return what it read rather than reading
        # again: a pipe cannot be rewound, so a second read returns "" and
        # would silently drop the dump.
        return _PREREAD_STDIN.pop()
    try:
        return sys.stdin.read()
    except (OSError, ValueError):  # pragma: no cover - env-dependent
        return ""


def _check_elf_path(value: str | None) -> Path | None:
    """`click.Path(exists=True, dir_okay=False)` equivalent for `--elf`."""
    if value is None:
        return None
    path = Path(value)
    if not path.exists():
        raise typer.BadParameter(f"'{value}' does not exist.", param_hint="--elf")
    if path.is_dir():
        raise typer.BadParameter(f"'{value}' is a directory.", param_hint="--elf")
    return path


def _check_file_path(value: str | None) -> str | None:
    """`click.Path(exists=True, dir_okay=False, allow_dash=True)` equivalent for
    `--file`: `-` is passed through untouched (means stdin, see `_read_dump`)."""
    if value is None or value == "-":
        return value
    path = Path(value)
    if not path.exists():
        raise typer.BadParameter(f"'{value}' does not exist.", param_hint="--file")
    if path.is_dir():
        raise typer.BadParameter(f"'{value}' is a directory.", param_hint="--file")
    return value


def faultdecode(
    ctx: typer.Context,
    cfsr: str = typer.Option(None, "--cfsr", help="Configurable Fault Status Register."),
    hfsr: str = typer.Option(None, "--hfsr", help="HardFault Status Register."),
    dfsr: str = typer.Option(None, "--dfsr", help="Debug Fault Status Register (optional)."),
    bfar: str = typer.Option(None, "--bfar", help="BusFault Address Register."),
    mmfar: str = typer.Option(None, "--mmfar", help="MemManage Fault Address Register."),
    mmfsr: str = typer.Option(
        None, "--mmfsr", help="MemManage sub-register (composed into CFSR)."
    ),
    bfsr: str = typer.Option(None, "--bfsr", help="BusFault sub-register (composed into CFSR)."),
    ufsr: str = typer.Option(
        None, "--ufsr", help="UsageFault sub-register (composed into CFSR)."
    ),
    pc: str = typer.Option(None, "--pc", help="Program Counter (symbolicated with --elf)."),
    lr: str = typer.Option(None, "--lr", help="Link Register (symbolicated with --elf)."),
    elf: str = typer.Option(
        None,
        "--elf",
        help="ELF for --pc/--lr symbolication (best-effort; skipped if no tool/elf).",
    ),
    file_: str = typer.Option(
        None,
        "--file",
        help="Read a pasted dump from this file ('-' for stdin) and grep registers.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable JSON report."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI colours."),
    project: str = typer.Option(  # accepted, not read; see below
        None, "--project", metavar="PATH", help="Project root (unused: faultdecode is HW-free)."
    ),
    sdk_root: str = typer.Option(  # accepted, not read; see below
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root (unused; see below)."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    board_yaml: str = typer.Option(None, "--board-yaml", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
) -> None:
    """Decode an ARM Cortex-M (ARMv8-M) fault dump.

    Supply registers as flags, and/or paste a dump via ``--file``/stdin and it
    greps the register names out. Explicit flags win over a parsed dump.

    `--project`/`--sdk-root`/`--board-yaml`/`--target`/`--all`/`--verbose`/
    `--quiet`/`--non-interactive`/`--ci` are declared, not consumed: this
    command reads no board.yaml and drives no alp-sdk checkout -- it is pure
    ARMv8-M register arithmetic, same as the SDK original it replaces -- but
    the oracle's clap `GlobalArgs` are `global = true`, so every verb
    (`faultdecode` included) accepts all of them; a caller (or a saved
    script) that passes any through unconditionally must not get a parse
    error -- `tan faultdecode --ci ...` exits the same with or without `--ci`
    on the oracle. `--no-color` is the one exception in this group with real
    meaning (see `_use_color`), and `--format` is documented separately
    below.

    `--format` is accepted BEFORE the subcommand too (`tan --format json
    faultdecode ...`), same as `debug-config`; the root callback records it on
    `ctx.obj` and this option overrides it when repeated after the command
    name. Both positions land on the same shape: tan's own envelope, with the
    decode report as `data`, exactly like `debug-config`'s (tan-cli#399). The
    oracle instead maps the global `--format json` onto the forwarded child's
    `--json` (`crates/tan-cli/src/commands/sdk_cli.rs`) and prints the
    unwrapped report for both spellings -- a deliberate divergence, taken
    because `--format json` is the surface the vscode extension parses and it
    rejects any document without `command`/`ok`/`exitCode`/`issues[]`. The
    unwrapped report is unchanged and still one flag away: `--json`, which
    also wins when both are given.
    """
    del board_yaml, target, all_targets, verbose, quiet, non_interactive, ci
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    # `--json` wins over `--format json`: it is the older, unwrapped surface
    # every saved script and the Rust forwarder use (tan-cli#399).
    envelope_mode = resolved_format == "json" and not as_json

    elf_path = _check_elf_path(elf)
    file_value = _check_file_path(file_)

    # 1. Gather registers. The FLAGS are parsed first, before any dump is read
    #    (tan-cli#388): reading stdin first meant `--cfsr 0x8200` -- the
    #    primary documented invocation, where no dump is needed or wanted --
    #    still blocked on a stdin nobody was ever going to write to.
    cfsr_i = _parse_hexint("cfsr", cfsr)
    hfsr_i = _parse_hexint("hfsr", hfsr)
    dfsr_i = _parse_hexint("dfsr", dfsr)
    bfar_i = _parse_hexint("bfar", bfar)
    mmfar_i = _parse_hexint("mmfar", mmfar)
    mmfsr_i = _parse_hexint("mmfsr", mmfsr)
    bfsr_i = _parse_hexint("bfsr", bfsr)
    ufsr_i = _parse_hexint("ufsr", ufsr)
    pc_i = _parse_hexint("pc", pc)
    lr_i = _parse_hexint("lr", lr)

    # A piped/pasted dump is always read (tan-cli#503), whether or not fault
    # registers were also supplied as flags -- see `_read_dump`'s docstring.
    # Explicit flags still win: `pick()` below prefers the flag value and only
    # falls back to a value the dump provided.
    dump_text = _read_dump(file_value)
    parsed: dict[str, int] = parse_dump(dump_text) if dump_text else {}

    def pick(name: str, flag_val: int | None) -> int | None:
        return flag_val if flag_val is not None else parsed.get(name)

    cfsr_v = pick("cfsr", cfsr_i)
    # Compose CFSR from explicit sub-registers when no combined CFSR was given.
    if cfsr_i is None and (mmfsr_i is not None or bfsr_i is not None or ufsr_i is not None):
        cfsr_v = (
            ((mmfsr_i or 0) & 0xFF)
            | (((bfsr_i or 0) & 0xFF) << 8)
            | (((ufsr_i or 0) & 0xFFFF) << 16)
        )

    hfsr_v = pick("hfsr", hfsr_i)
    dfsr_v = pick("dfsr", dfsr_i)
    bfar_v = pick("bfar", bfar_i)
    mmfar_v = pick("mmfar", mmfar_i)
    pc_v = pick("pc", pc_i)
    lr_v = pick("lr", lr_i)

    # No status registers at all => bad input (this is an analysis tool, but it
    # needs *something* to analyse). Exit nonzero with a usage hint.
    if cfsr_v is None and hfsr_v is None and dfsr_v is None:
        no_registers = (
            "no fault registers supplied -- pass --cfsr/--hfsr/--dfsr "
            "or pipe a dump via --file/-/stdin."
        )
        if envelope_mode:
            # The refusal has to agree with the success path about whether
            # stdout is an envelope (tan-cli#399); leaving it to `cli.main`'s
            # generic fallback gave the consumer `command: "cli"` here and
            # `command: "faultdecode"` one invocation later.
            emit(
                Envelope(
                    "faultdecode",
                    Project(root=None, board_yaml=None),
                    None,
                    [Issue("faultdecode.no-registers", "error", no_registers)],
                    ExitCode.VALIDATION_FAILURE,
                )
            )
        else:
            typer.echo(f"Error: {no_registers}", err=True)
        raise typer.Exit(int(ExitCode.VALIDATION_FAILURE))

    report = decode(
        cfsr=cfsr_v or 0,
        hfsr=hfsr_v or 0,
        dfsr=dfsr_v or 0,
        bfar=bfar_v,
        mmfar=mmfar_v,
    )

    # 2. Best-effort symbolication (optional, never fatal).
    symbols: dict[str, Symbol] = {}
    if elf_path is not None:
        for which, addr in (("pc", pc_v), ("lr", lr_v)):
            if addr is not None:
                sym = resolve_symbol(addr, elf_path)
                if sym is not None:
                    symbols[which] = sym

    if as_json:
        typer.echo(_json.dumps(report_to_json(report, symbols or None), indent=2))
    elif envelope_mode:
        # `project` is hardcoded null/null, not resolved: this command reads no
        # board.yaml and drives no checkout (see the docstring's
        # declared-not-consumed list), so reporting a root here would claim a
        # resolution that never happened.
        emit(
            Envelope(
                "faultdecode",
                Project(root=None, board_yaml=None),
                report_to_json(report, symbols or None),
                [],
                ExitCode.SUCCESS,
            )
        )
    else:
        color = _use_color(no_color)
        typer.echo(render_human(report, symbols or None, color))
        if pc_v is not None and elf_path is None:
            typer.echo(
                "  (note: --pc given without --elf -- pass --elf <app.elf> to resolve the symbol)",
                err=True,
            )

    # Analysis tool: a successful decode is always exit 0 (even "no flags set").
