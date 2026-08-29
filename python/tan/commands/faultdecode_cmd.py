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
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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
from tan.core.inert import NOT_APPLICABLE, inert_help
from tan.env import no_color_requested, stdin_is_tty
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
    resolved absolute path from `on_path` is what gets spawned below.

    Since tan-cli#532 that is no longer a second pattern: `on_path` delegates
    to `tan.core.tool_lookup.resolve_tool`, the same lookup `flash_cmd`,
    `size_cmd` and `build/execute.py` call, so every tool probe in tan now
    resolves identically. This docstring previously said those three
    hand-rolled their own walks returning a bare `bool` -- true when it was
    written, false since #567 consolidated them, and stated in the past tense
    here so the next reader does not act on the older shape.
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


class NegativeRegisterValue(Exception):
    """A fault register was given a negative value (tan-cli#616).

    Not a `typer.BadParameter`: this refusal carries a REGISTERED issue code
    (`faultdecode.invalid-register-value`) so a `--format json` consumer can
    branch on it, the same way `faultdecode.no-registers` already lets it tell
    "nothing to analyse" from a tan crash. `BadParameter` would land on
    `cli.main`'s generic `command: "cli"` / `cli.parse-error` fallback instead
    -- the two-paths-of-one-verb disagreement tan-cli#399 closed everywhere
    else in this command.
    """

    def __init__(self, option: str, value: str) -> None:
        self.option = option
        self.value = value
        self.message = (
            f"--{option}: {value!r} is negative -- a fault register value is "
            "unsigned, so there is nothing to decode. Pass the value exactly as "
            "the dump printed it (hex, with or without a 0x prefix)."
        )
        super().__init__(self.message)


def _parse_hexint(option: str, value: str | None) -> int | None:
    """A fault-register value, hex by default: these are CPU status registers,
    always read in hex, so a bare ``8200`` means ``0x8200`` (not decimal 8200)
    and ``0x8200`` works too. Mirrors the original's `_HexInt.convert`.

    A NEGATIVE value is refused (tan-cli#616). `int(text, 16)` happily accepts
    a leading `-`, and everything downstream then treats the result as a
    register word: `--cfsr=-8200` rendered `"0x-0008200"` (not a hex integer in
    any sense), decoded twelve flags that are not set out of Python's infinite
    two's-complement sign extension, concluded "Stack overflow", and exited 0.
    A register is unsigned by construction, so a sign is a user error and the
    only correct answer is a refusal -- confident nonsense at exit 0 is the
    worst possible one for a tool whose entire output is a diagnosis.
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
    if parsed < 0:
        raise NegativeRegisterValue(option, value)
    return parsed


def _refuse(code: str, message: str, *, envelope_mode: bool) -> typer.Exit:
    """Build `faultdecode`'s refusal on whichever surface the caller asked for,
    and return the `typer.Exit` for the caller to `raise`.

    One place, because the refusals have to agree with the success path AND
    with each other about whether stdout is an envelope (tan-cli#399): a second
    refusal that wrote plain text under `--format json` would hand the consumer
    `command: "cli"` here and `command: "faultdecode"` one invocation later,
    which is the exact defect that issue closed for the first one.
    """
    if envelope_mode:
        emit(
            Envelope(
                "faultdecode",
                Project(root=None, board_yaml=None),
                None,
                [Issue(code, "error", message)],
                ExitCode.VALIDATION_FAILURE,
            )
        )
    else:
        typer.echo(f"Error: {message}", err=True)
    return typer.Exit(int(ExitCode.VALIDATION_FAILURE))


#: Chunk size for each background stdin read. `.read1(n)` (see
#: `_read_stdin_bounded` for why not `.read(n)`) never blocks trying to fill
#: this, so its exact value only bounds worst-case per-chunk latency, not
#: correctness -- 64 KiB is the same figure `shutil.copyfileobj` defaults to.
_STDIN_CHUNK_BYTES = 65536

#: Idle bound (tan-cli#537): how long the background reader waits, after
#: either the LAST chunk that arrived or start (if none yet), before
#: deciding no more is coming. Reset on every chunk -- this is what lets a
#: slow-but-steady producer (a serial capture, a script draining a device)
#: read in full, which a TOTAL budget (attempts 2 and 6/7) cannot do.
#:
#: Measured, not argued: an ad-hoc bench script (tan-cli#537, not checked in) fed a
#: real OS pipe 26 lines with a 0.24s `time.sleep` between each and recorded
#: the gaps the reader thread actually observed -- max 0.250s under this
#: environment's own scheduling jitter, essentially the nominal 0.24s with no
#: slack to spare. 2.0s is that measured worst case with roughly 8x headroom,
#: not a number chosen by reasoning about the idle window in the abstract --
#: that reasoning is exactly what produced the reverted "20x the idle window"
#: comment in attempt 6/7, which was arithmetically false for this same
#: shape.
_STDIN_IDLE_TIMEOUT_S = 2.0

#: Byte cap (tan-cli#537): a hard ceiling on accumulated stdin bytes. No
#: earlier attempt had one -- unguarded, `yes "CFSR: 0x00008200" | tan
#: faultdecode` reached 409.7 MB RSS over 9.59s (issue #537, measured). A
#: real pasted fault dump is at most a few KB; 1 MiB is roughly three orders
#: of magnitude below that measured unguarded growth while staying two-plus
#: orders of magnitude above any legitimate paste. Bounds the ACCUMULATED
#: BUFFER itself (checked after every chunk), not the read loop -- a large
#: total-cap alone would still let the buffer grow unbounded until it fired.
_STDIN_BYTE_CAP = 1_048_576

#: Total bound (tan-cli#537): a backstop so a producer that never goes idle
#: for `_STDIN_IDLE_TIMEOUT_S` and never reaches `_STDIN_BYTE_CAP` (a
#: continuous writer trickling below both) still terminates. NOT the primary
#: defence -- the idle bound is, for exactly the reason attempts 6/7 got a
#: fixed total wrong: any fixed total remains capable of truncating a
#: sufficiently slow legitimate producer. 30s is ~5x the 6.2s measured
#: slow-but-steady shape (the same ad-hoc bench), which is comfortably
#: inside it because the IDLE bound is what actually governs that case (each
#: gap is ~0.24s, far under `_STDIN_IDLE_TIMEOUT_S`); this total exists only
#: to catch the case the idle bound cannot. Whichever bound fires, the
#: read still ANNOUNCES it (`_stdin_bound_message`) rather than silently
#: discarding -- that announcement, not the value chosen here, is what makes
#: a truncation recoverable instead of a confident wrong root cause.
_STDIN_TOTAL_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class _StdinReadOutcome:
    """Result of one bounded background stdin BYTE read (tan-cli#537)."""

    #: Raw bytes accumulated before whichever bound fired (or clean EOF).
    data: bytes
    #: Which bound fired, or `None` on a clean EOF (the writer closed the
    #: pipe on its own, no bound needed to intervene at all).
    bound: str | None  # None | "idle" | "byte-cap" | "total-cap"


def _read_stdin_bounded(
    buffer: object,
    *,
    idle_s: float = _STDIN_IDLE_TIMEOUT_S,
    byte_cap: int = _STDIN_BYTE_CAP,
    total_s: float = _STDIN_TOTAL_TIMEOUT_S,
    chunk_bytes: int = _STDIN_CHUNK_BYTES,
) -> _StdinReadOutcome:
    """Read `buffer` (a binary stream, e.g. `sys.stdin.buffer`) to bytes,
    bounded three ways at once (tan-cli#537): an IDLE timeout that resets on
    every chunk, a hard BYTE CAP on the accumulated buffer, and a TOTAL
    timeout as a backstop. All state (the queue, the buffer, the deadlines)
    is local to this call -- tan-cli#537's own module-global `_PREREAD_STDIN`
    stash is what let one invocation's empty read leak into the next
    in-process call; nothing here survives past the `return`.

    ONE mechanism on every platform: a single daemon thread looping a
    single-syscall chunk read and pushing each chunk to a `queue.Queue`,
    with EOF pushed in-band as `b""` -- never inferred from a clock. This
    deletes the select()-on-POSIX/thread-on-Windows split the previous
    reader carried: `select()` is WinSock-only on Windows (anonymous pipes
    are not selectable there at all), and neither `PeekNamedPipe` nor a
    console wait handle answers "will this read terminate?" -- only "is a
    byte available?", a different question. A chunked thread reader needs
    none of that and behaves identically everywhere.

    Never `buffer.read(n)`: `io.BufferedReader.read(n)` blocks issuing
    repeated raw reads until it collects `n` bytes OR hits EOF -- it does NOT
    return early just because some bytes are already available. Measured
    (the same ad-hoc bench): with `.read(65536)` a producer
    writing 26 lines with 0.24s gaps between them delivered exactly ONE
    chunk containing the whole dump, at EOF, 6.29s after the first byte --
    the per-chunk idle-reset this function exists to provide never had
    anything to reset against, silently degrading to the "wait for the
    whole thing" case attempt 6/7 already got a total budget wrong for.

    The chunk primitive is `os.read(fd, chunk_bytes)` on `buffer`'s real file
    descriptor when it has one (a genuine `sys.stdin.buffer` over a pipe,
    file, or console), falling back to `buffer.read1(chunk_bytes)` only when
    it does not (`typer.testing.CliRunner`'s in-memory `BytesIO`-backed
    stdin double, whose `.fileno()` raises `io.UnsupportedOperation` --
    `CliRunner` hands the command an already-complete buffer, so `.read1()`
    there returns immediately and reaches its own in-band EOF quickly; it
    never blocks on a real held-open pipe, so it carries none of the risk
    the next paragraph describes). Both return AT MOST ONE underlying
    system read's worth of data rather than trying to fill `chunk_bytes` --
    the same bench run measured 27 `.read1()` chunks, one per line plus the
    terminating empty read, with gaps at 0.239-0.250s, matching the
    writer's own 0.24s sleep, and `os.read` shares that same one-syscall
    contract by construction (it does not go through any buffering layer
    at all).

    `os.read(fd, ...)` over `buffer.read1(...)` on a REAL stdin is not a
    style choice, it is the fix for a second, more severe defect this
    function's own first cut hit: with the reader parked in
    `buffer.read1()` (a `BufferedReader` method) when the idle bound fires
    and the caller proceeds without joining the thread, a normal process
    exit then aborts -- measured, reproducibly, via
    `test_registers_on_the_command_line_never_wait_for_an_open_stdin_pipe`
    (a REAL subprocess, not `CliRunner`) -- with `Fatal Python error:
    _enter_buffered_busy: could not acquire lock for <_io.BufferedReader
    name='<stdin>'> at interpreter shutdown, possibly due to daemon
    threads` and exit code -6 (SIGABRT): CPython's interpreter-shutdown
    finalizer tries to flush/close `sys.stdin`, which needs the
    `BufferedReader`'s internal per-object lock, and the abandoned thread
    is holding that same lock parked in a real blocking read syscall that
    will never return (the parent still owns the open write end) --
    exactly the "daemon thread parked in a read at interpreter exit" risk
    the design flagged for Windows specifically, reproduced here on Linux
    too. `os.read(fd, n)` is a bare syscall on the raw descriptor with no
    Python-level stream OBJECT and therefore no such lock for the
    finalizer to contend on; the same reproduction, switched to it, exits
    0 cleanly (verified with a second ad-hoc reproduction script,
    tan-cli#537, not checked in).

    Termination is never inferred: EOF is the in-band `b""` chunk, the idle
    bound fires only via `queue.Queue.get(timeout=...)` actually elapsing
    with nothing delivered, the byte cap fires only once the accumulated
    total has actually reached it, and the total bound fires only once the
    wall clock has actually run out. Whichever one fires, what already
    arrived is kept (never the discard-on-timeout shape of attempt 1, where
    `got.append(sys.stdin.read())` only ran on `read()` RETURNING, so a join
    timeout threw away every buffered byte).

    The reader thread is abandoned, not joined, on every exit from the loop
    below except a clean EOF: it may still be parked in a read when this
    function returns (a producer that never closes its end), and it holds
    nothing but stdin, so leaving it for the daemon-thread interpreter-exit
    path to reap is the correct outcome -- the same trade the previous
    reader already made for exactly the same reason, and now safe to make
    (see the `os.read` paragraph above) because nothing it holds blocks
    that exit any more.
    """
    try:
        fd: int | None = buffer.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        fd = None

    chunk_queue: queue.Queue[bytes] = queue.Queue()

    def _read_one_chunk() -> bytes:
        if fd is not None:
            return os.read(fd, chunk_bytes)
        return buffer.read1(chunk_bytes)  # type: ignore[attr-defined]

    def _drain() -> None:
        try:
            while True:
                chunk = _read_one_chunk()
                chunk_queue.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):  # pragma: no cover - env-dependent
            chunk_queue.put(b"")

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    chunks: list[bytes] = []
    total = 0
    bound: str | None = None
    start = time.monotonic()
    while True:
        remaining_total = total_s - (time.monotonic() - start)
        if remaining_total <= 0:
            bound = "total-cap"
            break
        wait = min(idle_s, remaining_total)
        try:
            chunk = chunk_queue.get(timeout=wait)
        except queue.Empty:
            # Which bound actually expired depends on which of the two was
            # the shorter (and therefore limiting) wait, NOT on which one is
            # checked first above: if `remaining_total` was the smaller of
            # the two, the wait timed out because the TOTAL deadline was
            # imminent, not because the reader went idle for a full
            # `idle_s` -- mislabelling that as "idle" would misreport a
            # total-cap termination on a producer whose chunks are only
            # slightly slower than `idle_s - remaining_total`.
            bound = "total-cap" if wait < idle_s else "idle"
            break
        if not chunk:
            break  # clean EOF -- no bound fired, whatever arrived is complete
        chunks.append(chunk)
        total += len(chunk)
        if total >= byte_cap:
            bound = "byte-cap"
            break

    data = b"".join(chunks)
    if bound == "byte-cap":
        data = data[:byte_cap]
    return _StdinReadOutcome(data=data, bound=bound)


def _stdin_bound_message(bound: str, bytes_read: int) -> str:
    """The stderr/`issues[]` announcement for whichever bound
    `_read_stdin_bounded` fired (tan-cli#537 constraint 4): names the bound
    BY NAME and the remedy -- `--file -` is already the explicit
    read-to-EOF path, the unbounded opt-in a truncated caller should be told
    to reach for. A truncated decode the engineer is told about is a
    recoverable inconvenience; a silent one is a confident wrong root
    cause, which is the whole cost this design refuses to pay silently."""
    remedy = "use `--file -` to read the complete dump instead (reads to EOF, no bound)"
    if bound == "idle":
        return (
            f"stdin idle for {_STDIN_IDLE_TIMEOUT_S:g}s after {bytes_read} byte(s) "
            f"without reaching EOF -- decoded what arrived; {remedy}."
        )
    if bound == "byte-cap":
        return (
            f"stdin exceeded the {_STDIN_BYTE_CAP}-byte cap without reaching EOF -- "
            f"decoded the first {_STDIN_BYTE_CAP} bytes only; {remedy}."
        )
    if bound == "total-cap":
        return (
            f"stdin did not reach EOF within {_STDIN_TOTAL_TIMEOUT_S:g}s -- decoded "
            f"the {bytes_read} byte(s) read so far; {remedy}."
        )
    raise AssertionError(f"unknown stdin bound: {bound!r}")  # pragma: no cover


@dataclass(frozen=True)
class ImplicitStdinResult:
    """Result of the IMPLICIT (unnamed, auto-consume) stdin read (tan-cli#537)
    -- `... | tan faultdecode`, no `--file` on the command line at all.

    Never raises: a detached/replaced/absent stdin degrades to
    `attempted=False, text=""`, the same "nothing to offer" answer a real
    closed pipe gives, because the implicit path's whole job is "just work
    when there is something to read, fall through cleanly when there is
    not" -- `--file -` is the surface that raises on a stdin it cannot
    serve, because THAT read was named explicitly.
    """

    #: The decoded dump text -- "" when nothing was read at all.
    text: str
    #: Which bound fired during the read, or `None` when either no read was
    #: attempted or the read reached a clean EOF with no bound needed.
    bound: str | None
    #: Whether a background read was actually started (False for a TTY, a
    #: detached/`None` stdin, or a text-only replacement with no `.buffer`).
    attempted: bool
    #: How many bytes were actually read (0 when not attempted, or when an
    #: attempted read got nothing before its idle bound fired).
    bytes_read: int


def _read_implicit_stdin() -> ImplicitStdinResult:
    """The IMPLICIT stdin reader tan-cli#537 exists to redesign.

    Reads `sys.stdin.buffer` as BYTES via `_read_stdin_bounded`, accumulates,
    and decodes EXACTLY ONCE at the end with `bytes.decode("utf-8",
    errors="ignore")` -- byte-identical to what `--file <path>` already does
    via `Path.read_text(encoding="utf-8", errors="ignore")` (same codec, same
    error handler), so decode parity with `--file` stops being a property
    tested for and becomes one that cannot be violated: there is one decode
    call, on the complete buffer, with the same two operands on both paths.
    This is also what deletes the entire class that produced attempts 3 and
    5: no chunk boundary exists for a multi-byte sequence to split across (no
    decode happens per chunk at all), so no `UnicodeDecodeError` can be
    raised mid-read for an `except` to swallow, and nothing here ever calls
    `.reconfigure()`, so its `io.UnsupportedOperation` on an already-read
    stream never arises either.

    Routes the TTY/detached/replaced-stream checks through this repo's own
    guarded probes rather than hand-rolling a second copy of either
    (tan-cli#488's `_TeeStderr` class of bug): `stdin_is_tty()` -- not a bare
    `sys.stdin.isatty()` -- absorbs both `sys.stdin is None` and a replaced
    stream with no `.isatty()` at all. Reading `.buffer` adds ONE further
    shape neither existing probe covers: a text-only replacement stream
    (e.g. a test harness's `io.StringIO`) has no `.buffer` attribute at all
    -- `getattr(..., "buffer", None)` degrades that to `attempted=False`
    cleanly, matching every other "nothing to offer" case, rather than
    raising `AttributeError` from inside a background thread.
    """
    if sys.stdin is None or stdin_is_tty():
        return ImplicitStdinResult(text="", bound=None, attempted=False, bytes_read=0)
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return ImplicitStdinResult(text="", bound=None, attempted=False, bytes_read=0)
    outcome = _read_stdin_bounded(buf)
    text = outcome.data.decode("utf-8", errors="ignore")
    return ImplicitStdinResult(
        text=text, bound=outcome.bound, attempted=True, bytes_read=len(outcome.data)
    )


def _read_dump(file_: str | None) -> str:
    """Read a pasted dump from `--file <path>` or `--file -` (stdin to EOF).

    Returns `""` when `file_` is `None`: the IMPLICIT auto-consume path
    (no `--file` on the command line at all) is `_read_implicit_stdin`
    above, a SEPARATE function since tan-cli#537 -- folding it into this one
    (the pre-#537 shape) is what let a module-global stash of pre-read bytes
    (`_PREREAD_STDIN`) survive between invocations; splitting it out means
    this function needs no such stash at all.

    `--file -` is the EXPLICIT unbounded opt-in `_stdin_bound_message` tells
    a truncated implicit-read caller to reach for: it reads to EOF with NO
    idle/byte/total bound, whatever else was on the command line, because
    the caller named this read specifically and a dump has no terminator
    other than EOF. Reads `.buffer` and decodes once with the same
    `errors="ignore"` UTF-8 decode `--file <path>` and the implicit path
    both use, rather than the previous text-layer `sys.stdin.read()`, which
    could itself raise `UnicodeDecodeError` on a non-UTF-8 host and broke
    decode parity a second way.

    tan-cli#488 round 5 class sweep: `sys.stdin` itself, not just the result
    of calling `.isatty()` on it, can be `None` -- a process launched with
    its standard handles detached (a GUI launcher, a `pythonw`-style spawn,
    or a shell that closed fd 0 before exec). `--file -` names stdin
    explicitly, so a detached stdin (or one replaced by a text-only stream
    with no `.buffer`) is refused with a clear message rather than a raw
    `AttributeError`.
    """
    if file_ is None:
        return ""
    if file_ == "-":
        if sys.stdin is None:
            raise typer.BadParameter(
                "stdin is detached (no standard input handle for this process), "
                "so `--file -` cannot read a dump from it",
                param_hint="--file",
            )
        buf = getattr(sys.stdin, "buffer", None)
        if buf is None:
            raise typer.BadParameter(
                "stdin has no binary buffer to read from (it has been replaced "
                "by a text-only stream), so `--file -` cannot read a dump from it",
                param_hint="--file",
            )
        return buf.read().decode("utf-8", errors="ignore")
    return Path(file_).read_text(encoding="utf-8", errors="ignore")



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
    project: str = typer.Option(
        None,
        "--project",
        metavar="PATH",
        help=inert_help(
            "Project root. Not read: faultdecode is pure ARMv8-M register "
            "arithmetic and opens no project.",
            NOT_APPLICABLE,
        ),
    ),
    sdk_root: str = typer.Option(
        None,
        "--sdk-root",
        metavar="PATH",
        help=inert_help(
            "alp-sdk checkout root. Not read: faultdecode is pure ARMv8-M "
            "register arithmetic and drives no alp-sdk checkout.",
            NOT_APPLICABLE,
        ),
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
    try:
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
    except NegativeRegisterValue as err:
        raise _refuse(
            "faultdecode.invalid-register-value", err.message, envelope_mode=envelope_mode
        ) from None

    # Read the dump UNCONDITIONALLY (tan-cli#503, defect 1) -- not gated on
    # whether any register flag was also given. A prior version skipped this
    # read whenever ANY of cfsr/hfsr/dfsr/bfar/mmfar/mmfsr/bfsr/ufsr was
    # supplied, which broke the command's own documented contract ("Explicit
    # flags win over a parsed dump" only holds if the dump is still read when
    # flags are present) two ways: a piped CFSR/BFAR was silently dropped in
    # favour of the flag's registers alone (measured: `... | tan faultdecode
    # --hfsr 0x40000000` reported a self-contradictory "Forced HardFault ...
    # its own status bits are clear" while the piped CFSR=0x00008200/
    # BFAR=0xdeadbeef it never read said otherwise), and --bfar/--mmfar
    # counted as "a register was given" without satisfying the cfsr/hfsr/dfsr
    # gate below, so the dump was skipped AND the command still refused with
    # `faultdecode.no-registers`.
    #
    # `--file` (path or `-`) is explicit and unbounded; with no `--file` at
    # all the IMPLICIT path (tan-cli#537) reads `sys.stdin.buffer` on a
    # background thread bounded three ways (idle/byte-cap/total, see
    # `_read_stdin_bounded`), so making this unconditional does not reopen
    # tan-cli#388's unbounded hang -- measured,
    # `test_registers_on_the_command_line_never_wait_for_an_open_stdin_pipe`
    # (a real held-open OS pipe, not `CliRunner`) still returns in well under
    # a second, not the 20 s bound it fails at.
    implicit_bound: str | None = None
    implicit_bytes = 0
    implicit_silent = False
    if file_value is not None:
        dump_text = _read_dump(file_value)
    else:
        implicit = _read_implicit_stdin()
        dump_text = implicit.text
        implicit_bound = implicit.bound
        implicit_bytes = implicit.bytes_read
        implicit_silent = implicit.attempted and implicit.bound == "idle" and implicit.bytes_read == 0
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
        if implicit_silent:
            # tan-cli#537: distinguishes "no stdin at all" (TTY, detached, a
            # closed pipe already at EOF) from "a pipe WAS open and offered
            # nothing" -- today's refusal could not tell the two apart, and
            # the second one is the shape a caller can actually fix by
            # waiting longer (`--file -`) rather than by piping something in
            # the first place.
            no_registers = (
                "no fault registers supplied, and stdin was open but silent for "
                f"{_STDIN_IDLE_TIMEOUT_S:g}s -- pass --cfsr/--hfsr/--dfsr, or use "
                "`--file -` to wait for a dump indefinitely."
            )
        else:
            no_registers = (
                "no fault registers supplied -- pass --cfsr/--hfsr/--dfsr "
                "or pipe a dump via --file/-/stdin."
            )
        # The refusal has to agree with the success path about whether stdout is
        # an envelope (tan-cli#399); leaving it to `cli.main`'s generic fallback
        # gave the consumer `command: "cli"` here and `command: "faultdecode"`
        # one invocation later. `_refuse` is that agreement, shared with the
        # negative-register refusal above.
        raise _refuse(
            "faultdecode.no-registers", no_registers, envelope_mode=envelope_mode
        )

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

    # tan-cli#537 constraint 4: whichever bound fired on the IMPLICIT stdin
    # read, announce it -- on stderr in EVERY mode (not only `--format json`'s
    # `issues[]`), naming the bound and the `--file -` remedy. Only when
    # something was actually read: a bound firing on zero bytes is the
    # separate "silent holder" refusal handled above, not a truncation.
    stdin_issues: list[Issue] = []
    if implicit_bound is not None and implicit_bytes > 0:
        bound_message = _stdin_bound_message(implicit_bound, implicit_bytes)
        typer.echo(f"Warning: {bound_message}", err=True)
        stdin_issues.append(Issue("faultdecode.stdin-truncated", "warning", bound_message))

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
                stdin_issues,
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
