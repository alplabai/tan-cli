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
import queue
import subprocess
import sys
import threading
import time
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


def _parse_hexint(option: str, value: str | None, *, enforce_range: bool = True) -> int | None:
    """A fault-register value, hex by default: these are CPU status registers,
    always read in hex, so a bare ``8200`` means ``0x8200`` (not decimal 8200)
    and ``0x8200`` works too. Mirrors the original's `_HexInt.convert`.

    Rejects anything outside ``0x0..0xFFFFFFFF`` when `enforce_range` is set
    (tan-cli#503, defect 5): `int(text, 16)` happily accepts a leading ``-``
    (``"-8200"`` -> -33280), and that arbitrary-precision negative went on to
    corrupt everything downstream that assumed a 32-bit register word --
    `_scan`'s bitwise-and against it reported a dozen bogus flags and the
    wrong root cause, and `report_to_json`'s ``f"0x{v:08x}"`` printed the
    malformed, non-hex ``"0x-0008200"`` on the JSON contract. Refusing here
    gives the caller a coded refusal instead of a confident wrong diagnosis --
    but this is NOT the only place a register value enters the command:
    `tan.core.faultdecode.parse_dump` greps a pasted dump through its own
    regex, a second, independent entry point, bounded a different way (a
    RANGE check on the parsed integer, not a width cap on the regex --
    `core/faultdecode.py:309`) since its alternative cannot match a leading
    `-` in the first place. Both paths agree on the outcome that matters:
    `decode()` only ever sees a well-formed 32-bit word.

    `enforce_range=False` for `--pc`/`--lr` (round 4): defect 5's own
    reproduction was `--cfsr`-only, and the oracle applies NO range check to
    `--pc`/`--lr` (measured: `--cfsr 0x8200 --pc 0x100000000` on the shipped
    v0.4.1 binary exits 0). They are addresses fed only to best-effort
    `addr2line` symbolication, never `_scan`'s bit arithmetic, so bounding
    them was scope creep past what defect 5 named. `int(text, 16)` still
    runs either way; only the width/sign bound is skipped.
    """
    if value is None:
        return None
    text = value.strip()
    try:
        # base 16 accepts an optional 0x prefix and a bare hex run alike.
        parsed = int(text, 16)
    except ValueError as err:
        raise typer.BadParameter(
            f"{value!r} is not a valid integer (try 0x...)", param_hint=f"--{option}"
        ) from err
    if enforce_range and not 0 <= parsed <= 0xFFFFFFFF:
        raise typer.BadParameter(
            f"{value!r} is out of range for a 32-bit register (must be 0x0..0xFFFFFFFF)",
            param_hint=f"--{option}",
        )
    return parsed


#: How long the IMPLICIT stdin auto-consume waits, IDLE -- i.e. since the
#: last line arrived, not since the read began -- for a non-TTY stdin to
#: deliver another line or EOF before giving up and decoding from whatever
#: arrived so far, plus the flags (tan-cli#388, tan-cli#503).
#:
#: Not 0: `producer | tan faultdecode` races the producer's first write
#: against this process's own startup, and losing that race would drop a
#: dump the user really did pipe. Not unbounded either -- unbounded is the
#: defect this bounds. And not a TOTAL budget for the whole read either --
#: that was tan-cli#503's own regression: a fixed budget cuts off a
#: slow-but-steady producer (a serial capture, a script draining a device)
#: mid-dump just because it kept writing past the window, silently
#: truncating a report that would have arrived in full a moment later. Each
#: line arriving resets the window (see `_read_implicit_stdin`), so a
#: steadily-producing writer is read to completion regardless of how long
#: that takes in total; only a producer that goes idle for a whole window,
#: or never writes at all, is cut off. A quarter second is far longer than a
#: scheduler hiccup and far shorter than the indefinite block a held-open,
#: silent pipe used to cause.
_STDIN_READY_TIMEOUT_S = 0.25

#: The OTHER half of the bound (tan-cli#503 round 4): a hard ceiling on the
#: WHOLE implicit-stdin read, wall-clock, regardless of how many lines keep
#: resetting the idle window above. Idle-only bounding closes tan-cli#388
#: (a producer that stalls or never writes) but reopens it for the opposite
#: shape: a producer that never goes idle -- writes continuously, faster
#: than `_STDIN_READY_TIMEOUT_S` apart, forever (`yes | tan faultdecode`, or
#: a device whose diagnostic UART never stops talking) -- never trips the
#: idle check and so was, again, unbounded: zero bytes on stdout AND stderr,
#: the exact unclassifiable hang tan-cli#388 was filed against. 20x the idle
#: window: generous enough that a real fault dump -- at most a few dozen
#: lines, even paced right at the edge of the idle window -- always finishes
#: well inside it, while still turning a truly runaway producer into a
#: single-digit-second wait instead of forever. When this fires,
#: `_read_implicit_stdin` returns what arrived plus a `truncated` flag
#: instead of silently discarding it -- a customer told about a truncated
#: decode can trust it; a silent one cannot, and a hang is worse than either.
_STDIN_TOTAL_TIMEOUT_S = 5.0


def _stdin_errors_ignore() -> None:
    """Make `sys.stdin` decode exactly like `--file` does (tan-cli#503):
    `_read_dump` below reads a pasted file with `Path.read_text(encoding=
    "utf-8", errors="ignore")` -- BOTH operands pinned, not just the error
    policy. Pinning only `errors="ignore"` here (an earlier round of this
    fix) closed the crash but left `encoding` at whatever `sys.stdin` was
    already open with -- the platform/locale default (`PYTHONIOENCODING`,
    or a legacy code page on Windows), which is not necessarily UTF-8. Two
    non-UTF-8 encodings can each decode the SAME byte without error (so
    `errors="ignore"` never fires on either side) yet produce DIFFERENT
    text: measured, piping a dump containing a lone `0xE9` byte through
    `PYTHONIOENCODING=cp1252` decoded it as `chr(0xE9)` ("e"-acute) and
    shifted the `BFAR: 0x...` regex match just enough to lose the address
    entirely, while the identical bytes read via `--file` (pinned UTF-8)
    dropped the byte and matched `BFAR` correctly -- two different
    diagnoses from one dump, with no exception on either path to signal
    the divergence. Pinning `encoding="utf-8"` alongside `errors="ignore"`
    makes the two routes decode identically BY CONSTRUCTION, the same way
    `--file`'s own call already does, rather than merely by coincidence of
    the host's default encoding. `hasattr` skips a stream that cannot
    reconfigure (e.g. a test harness's in-memory stdin) -- the same guard
    `cli._reconfigure_stdio` uses for stdout/stderr.

    The `reconfigure()` call itself is guarded too (round 4): a
    `TextIOWrapper` raises `io.UnsupportedOperation` -- a subclass of BOTH
    `OSError` and `ValueError`, so no extra import is needed to catch it --
    the moment ANY read has already happened on it (measured: reconfiguring
    the SAME encoding it already has still raises post-read, this is not an
    encoding-change-only restriction). `hasattr` alone only proves the method
    EXISTS, not that calling it will succeed on this particular stream's
    current state; failing silently here just means the stream keeps
    whatever encoding it already opened with -- exactly the pre-fix
    behaviour this function exists to improve on, not a new failure mode.
    """
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):  # pragma: no cover - state-dependent
            pass


def _drain_bounded_queue(
    q: "queue.Queue[str | None]", deadline: float
) -> tuple[list[str], bool]:
    """The main-thread half of `_read_implicit_stdin`'s read: pull queued
    lines until EOF, a whole IDLE window (`_STDIN_READY_TIMEOUT_S`) elapses
    since the last one, or `deadline` (the TOTAL cap, `_STDIN_TOTAL_TIMEOUT_S`
    since the read began) passes -- whichever comes first (tan-cli#503 round
    4). Returns `(lines, truncated)`; `truncated` is `True` only when the
    TOTAL cap is what ended the read, never for an idle timeout or a clean
    EOF -- those are the ordinary "producer is done" endings this function
    already had, not a loss the caller needs to disclose.

    Near the deadline, `min(_STDIN_READY_TIMEOUT_S, remaining)` shrinks the
    `get()` timeout below a full idle window, so a `queue.Empty` there is
    AMBIGUOUS on its own -- it fires identically whether the total cap is
    what expired or the producer just happened to go idle in that last
    sliver of time. Re-checking the clock against `deadline` after the
    `Empty`, not just before the `get()`, is what tells the two apart;
    checking only before it (an earlier draft of this fix) always attributed
    that ambiguous case to a plain idle timeout and silently dropped the
    disclosure the whole fix exists to make -- caught by feeding a
    continuous producer directly to this function and observing `truncated`
    come back `False` after the cap had, in fact, fired.
    """
    lines: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return lines, True  # total cap fired -- keep what arrived, flag it
        try:
            item = q.get(timeout=min(_STDIN_READY_TIMEOUT_S, remaining))
        except queue.Empty:
            if time.monotonic() >= deadline:
                return lines, True  # total cap fired while waiting on get()
            return lines, False  # genuine idle timeout: give up cleanly
        if item is None:
            return lines, False  # EOF: the producer closed its end
        lines.append(item)


def _read_implicit_stdin() -> tuple[str, bool]:
    """Read whatever an IMPLICIT (no `--file`) piped stdin offers, bounded by
    BOTH an IDLE window (`_STDIN_READY_TIMEOUT_S` since the LAST line, not
    since the read began) AND a TOTAL wall-clock cap
    (`_STDIN_TOTAL_TIMEOUT_S`, `_drain_bounded_queue`) -- tan-cli#388,
    tan-cli#503. Returns `(text, truncated)`: `truncated` is `True` only when
    the TOTAL cap is what ended the read, so the caller can tell the customer
    the decode may be incomplete instead of presenting it as the whole dump.

    A daemon thread reads `sys.stdin` line by line (never one unbounded
    `read()`, which blocks past readiness until EOF) and hands each line to
    the main thread over a `Queue` the instant it arrives; `_drain_bounded_
    queue` does the bounding. Idle-only bounding alone reopens tan-cli#388
    for a producer that never goes idle -- writes faster than the idle window
    elapses, forever -- so a fixed idle reset with no outer ceiling is, again,
    unbounded; total-only bounding (an earlier round's `reader.join(...)`)
    truncates a slow-but-steady producer mid-dump the moment it outlives one
    fixed budget (tan-cli#503's own regression). Both together: a producer
    writing faster than the idle window is read to completion, up to the
    total cap; a producer that goes idle for a whole window, or never writes
    at all, is cut off at the idle bound, same as before.
    `_stdin_errors_ignore()` closes a SECOND regression: `_drain`'s `except
    (OSError, ValueError)` also swallowed a `UnicodeDecodeError` (a
    `ValueError` subclass) `readline()` raises decoding a WHOLE buffered
    chunk (up to ~8 KB) at once -- one stray non-UTF-8 byte anywhere in it
    used to discard every complete line already sitting there, undelivered,
    plus everything after, not merely a trailing partial line. Abandoning the
    daemon thread mid-`readline()` at a genuine idle or total timeout still
    costs only whatever it had not yet turned into a returned, queued line.
    """
    _stdin_errors_ignore()
    q: queue.Queue[str | None] = queue.Queue()

    def _drain() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                q.put(line if line else None)  # None is the EOF sentinel
                if not line:
                    break
        except (OSError, ValueError):  # pragma: no cover - env-dependent
            q.put(None)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    deadline = time.monotonic() + _STDIN_TOTAL_TIMEOUT_S
    lines, truncated = _drain_bounded_queue(q, deadline)
    return "".join(lines), truncated


def _read_dump(file_: str | None) -> tuple[str, bool]:
    """Read a pasted dump from --file, '-' (stdin), or piped stdin. Returns
    `(text, truncated)`; `truncated` is always `False` outside the IMPLICIT
    path below -- `--file`/`--file -` are explicit opt-ins bounded only by
    EOF, never by `_STDIN_TOTAL_TIMEOUT_S`.

    `--file -` is the EXPLICIT opt-in and always reads stdin to EOF, whatever
    else is on the command line: the caller asked for that read by name, and a
    dump has no terminator other than EOF.

    The IMPLICIT read (no `--file` at all) is attempted UNCONDITIONALLY,
    whether or not a fault register was also supplied as a flag: the command's
    own help promises "Explicit flags win over a parsed dump", which is only
    true if a dump is still read when flags are present too, and the merge in
    `faultdecode()`'s `pick()` exists precisely to let a flag override a
    parsed value register-by-register (tan-cli#503) -- a piped CFSR/BFAR must
    not be silently dropped just because `--hfsr` was also given. This is safe
    against the tan-cli#388 hang because `_read_implicit_stdin` bounds every
    IDLE gap in the read to `_STDIN_READY_TIMEOUT_S` AND the whole read to
    `_STDIN_TOTAL_TIMEOUT_S` (tan-cli#503 round 4): an idle-or-stalled open
    pipe with nothing (more) to contribute costs at most a quarter second, and
    a producer that never goes idle costs at most the total cap -- neither
    shape reaches the process, so gating the read on "were there already
    flags" was never load-bearing for #388 -- it only cost the merge.
    """
    if file_ == "-":
        if sys.stdin is None:  # fd 0 closed (tan-cli#503): nothing to read.
            return "", False
        _stdin_errors_ignore()  # a stray byte must not raise (tan-cli#503)
        return sys.stdin.read(), False
    if file_ is not None:
        return Path(file_).read_text(encoding="utf-8", errors="ignore"), False
    # Auto-consume piped stdin (non-tty) so `... | tan faultdecode` just works,
    # flags or no flags.
    if sys.stdin is None:
        return "", False
    try:
        # A replaced/wrapped stdin (not currently done anywhere in this repo,
        # but `cli.main` already does exactly this to `sys.stderr` via
        # `_TeeStderr` under `--format json`) may exist yet lack `.isatty()`
        # entirely, raising `AttributeError` rather than answering False --
        # the same shape `doctor_cmd.fix_suppressed_issue` had to route
        # around for `_TeeStderr` on stderr. Mirrors `_use_color`'s identical
        # guard on `sys.stdout.isatty()` above.
        is_tty = sys.stdin.isatty()
    except (AttributeError, ValueError):
        return "", False
    if is_tty:
        return "", False
    return _read_implicit_stdin()


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
    # No 32-bit range bound on pc/lr (tan-cli#503 round 4): see
    # `_parse_hexint`'s docstring -- defect 5 was CFSR-scoped and the oracle
    # itself applies no such bound to these two.
    pc_i = _parse_hexint("pc", pc, enforce_range=False)
    lr_i = _parse_hexint("lr", lr, enforce_range=False)

    # A piped/pasted dump is always read (tan-cli#503), whether or not fault
    # registers were also supplied as flags -- see `_read_dump`'s docstring.
    # Explicit flags still win: `pick()` below prefers the flag value and only
    # falls back to a value the dump provided.
    dump_text, dump_truncated = _read_dump(file_value)
    parsed: dict[str, int] = parse_dump(dump_text) if dump_text else {}
    # `dump_truncated` is `True` only when `_STDIN_TOTAL_TIMEOUT_S` (not the
    # idle window) is what ended the read: a continuously writing producer
    # that never went idle. Told to the customer wherever the decode itself
    # is told to them (tan-cli#503 round 4) -- a truncated decode disclosed
    # is fine, a silent one is not.
    truncation_notice = (
        f"stdin was still delivering data after {_STDIN_TOTAL_TIMEOUT_S:g}s "
        "(a continuously writing producer never went idle), so the read was "
        "cut off there; this decode reflects only what arrived by then and "
        "may be incomplete."
        if dump_truncated
        else None
    )

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
        if truncation_notice:
            # The read WAS cut off, and still found nothing usable -- say
            # so, rather than leaving the customer to guess whether the
            # producer was ever going to send a register at all.
            no_registers += f" ({truncation_notice})"
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
        # Stdout stays the unwrapped SDK report shape unchanged (the output
        # CONTRACT, see the module docstring) even when truncated -- the
        # notice goes to stderr, same channel the forwarded child always
        # used for anything that was not the report itself.
        if truncation_notice:
            typer.echo(f"Warning: {truncation_notice}", err=True)
        typer.echo(_json.dumps(report_to_json(report, symbols or None), indent=2))
    elif envelope_mode:
        # `project` is hardcoded null/null, not resolved: this command reads no
        # board.yaml and drives no checkout (see the docstring's
        # declared-not-consumed list), so reporting a root here would claim a
        # resolution that never happened.
        issues = (
            [Issue("faultdecode.stdin-truncated", "warning", truncation_notice)]
            if truncation_notice
            else []
        )
        emit(
            Envelope(
                "faultdecode",
                Project(root=None, board_yaml=None),
                report_to_json(report, symbols or None),
                issues,
                ExitCode.SUCCESS,
            )
        )
    else:
        color = _use_color(no_color)
        typer.echo(render_human(report, symbols or None, color))
        if truncation_notice:
            typer.echo(f"  (note: {truncation_notice})", err=True)
        if pc_v is not None and elf_path is None:
            typer.echo(
                "  (note: --pc given without --elf -- pass --elf <app.elf> to resolve the symbol)",
                err=True,
            )

    # Analysis tool: a successful decode is always exit 0 (even "no flags set").
