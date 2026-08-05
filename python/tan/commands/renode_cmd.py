# SPDX-License-Identifier: Apache-2.0
"""`tan renode` -- boot the built system-manifest's single Zephyr slice in
headless Renode as a no-hardware smoke test.

Port of `crates/tan-cli/src/commands/renode/mod.rs`: the IO/subprocess half.
Every pre-flight *decision* (SKU -> descriptor, slice selection, argv, the two
console-line classifiers) is pure in `tan.core.renode_plan`; this module
resolves paths, probes PATH for the `renode` binary, and spawns + tees it.

**Renode is an external tool tan does not vendor.** How its ABSENCE is
handled is most of this command's value: a missing `renode` on PATH is a
CODED, ACTIONABLE `renode.binary-missing` issue naming what to install
(https://renode.io) -- never a traceback, never a silent `ok: true`. Mirrors
`flash_cmd.py`'s own refusal shape for a missing hardware tool.

**This file now covers BOTH the plain headless smoke and `--sim-mode`, the
studio hardware-simulator gateway** that serves a control + UART socket pair
for `alp-sdk-vscode`'s `RealRenodeAdapter` (`tan-cli#77`). The plain path's
pre-flight *decisions* stay pure in `tan.core.renode_plan`; `--sim-mode`'s own
pure half (the `sim-descriptor.json` document, the generated boot script, the
control-line protocol, the monitor-line classifier) is
`tan.core.renode_sim`. This module resolves paths, probes PATH for the
`renode` binary, and owns every bit of IO: spawning + teeing the plain smoke,
and -- for `--sim-mode` -- binding the two ephemeral listeners, writing the
descriptor + boot script, spawning Renode with its monitor on a pipe, and
serving both sockets.

`--sim-mode` is a faithful port of `crates/tan-cli/src/commands/renode/
{sim,monitor}.rs` + `crates/tan-core/src/renode/sim.rs` (landed as
`5152fd4 feat(renode): implement the --sim-mode socket contract (#77) (#96)`),
itself ported from the retired Python `west alp-renode --sim-mode`
(`scripts/west_commands/alp_renode.py`, deleted in `alp-sdk@df312cec` under
ADR-0020 Phase 4). Every wire decision below was diff-verified against the
shipped `tan.exe` oracle driven live through the full pipeline -- every
pre-flight refusal code, the generated `sim-descriptor.json` and
`.sim-boot.resc` byte-for-byte, a real control-socket round trip, the
`renode.cpu-halted` latch, and `renode.sim-exited-early` -- not inferred from
source alone.

SCOPE (`tan-cli#77`, socket half): ports + descriptor + readiness marker + the
three-verb control protocol. DEFERRED to a follow-up on the same issue: the
`ram_console_buf` RAM-ring -> UART-socket streamer, the wired-UART console
path, and the per-SKU sim profiles behind the descriptor's
`framebuffers`/`peripherals` -- which stay `[]` here, with every run carrying
`renode.sim-profile-deferred` as a warning issue so an empty descriptor is
never mistaken for success. See `tan.core.renode_sim`'s module docstring for
the fuller account, including why this issue's own "no reference
implementation exists" framing does not hold: a Rust port of exactly this
contract already exists (frozen, but readable and CI-verified) and this file
is a faithful Python port of it, not a fresh re-derivation from issue prose.

Divergences from the Rust oracle worth flagging, both verified against the
shipped `tan.exe` rather than inferred from source. First, the ones shared
with (already documented for) the plain smoke:
  * `data.repl`/`data.resc`/`data.elf`/`data.logPath` and `project.root` are
    all reported in the HOST's NATIVE path style (backslashes on Windows,
    unconverted), NOT forward-slash-normalised -- unlike most other `tan`
    commands (`flash`, `doctor`, `build`). This is the oracle's own behaviour
    (`Path::to_string_lossy`, no `to_posix`), reproduced faithfully rather
    than "fixed" to match the other commands' convention.
  * FIXED (tan-cli#470): `project.root` -- and, with it, the default build
    root `<project.root>/build` and the `tan build --project ...` remedy in
    every manifest/elf-missing message -- used to anchor on the `APP_PATH`
    positional alone, silently dropping a supplied `--project` and resolving
    against the CWD instead. It now anchors on `--project` exactly like
    `flash`/`doctor`/`build`/`size`/`image`, via the SAME
    `build_output.resolve_app_base` ladder those two already call (an
    explicit `APP_PATH` positional wins; otherwise `--project`, then cwd) --
    fed a native-separator workspace dir and renormalised after, since
    `resolve_app_base` itself is separator-agnostic and this command still
    does not POSIX-normalise (previous bullet). The alp-sdk checkout
    resolution (`--sdk-root`'s fallback chain) already honoured `--project`
    before this fix, exactly as the oracle's own `cli_workspace_root(g)`
    does.
  * `sdk.root` in the envelope is the RAW `--sdk-root` value as typed, never
    absolutised -- verified: `tan renode --sdk-root ./sdk --format json`
    reports `"sdk":{"root":"./sdk", ...}`.
  * `data.renodeArgv[0]` carries `on_path()`'s own PATHEXT-match casing (e.g.
    `...\\fakebin\\renode.CMD`), where the oracle's `where`-based
    `resolve_renode_binary` reports the on-disk casing (`...\\fakebin\\
    renode.cmd`). `on_path` is shared with `flash_cmd.py`/`doctor_cmd.py`, so
    this is pre-existing there too, not renode-specific; not fixed here to
    avoid touching a helper three commands depend on for one byte-level
    diff on one field.
  * With no `--sdk-root` and `--project .`, the discovery-tier `sdk.root` is
    lexically normalised (`.../renodefx/alp-sdk`) because it is resolved
    through `pathlib.Path`, which collapses a `.` path segment on
    construction; the oracle's raw `PathBuf::join` does not normalise, and
    reports the unnormalised `.../renodefx/./alp-sdk`. Only the `--project .`
    discovery path is affected -- `--sdk-root` itself is reported raw (see
    above).

`--sim-mode`-specific behaviour, pinned by driving the oracle live rather
than only reading `sim.rs`/`monitor.rs`:
  * `data.logPath` starts as the PLAIN smoke's own default
    (`<build_root>/renode.log`, computed before the sim/plain branch even
    though sim mode never uses a build root) and only becomes the
    sim-specific default (`<image-bundle>/renode-sim.log`) once the run gets
    far enough to resolve it -- AFTER `repl`/`elf`/`descriptor`/the socket
    ports are all already known. A pre-flight failure up to and including
    `renode.binary-missing` therefore reports the PLAIN default in
    `data.logPath`, not the sim one -- verified: `tan renode --sim-mode
    --image-bundle <dir> --sdk-root <sdk> --board <sku>` with `renode`
    missing from PATH reports `logPath` as `<app_path>/build/renode.log`.
  * The readiness marker (`tan.core.renode_sim.ready_marker`) and, in text
    mode only, five human-readable header lines (`sku`/elf, `descriptor`,
    `control`, `uart`, and the `renode.sim-profile-deferred` warning's own
    text) are printed DIRECTLY to a real stream the moment they are known --
    stdout in text mode, and (for the readiness marker only; the header
    lines are text-mode-only) stderr in JSON mode -- rather than being
    buffered into the text/issues the envelope machinery below prints once
    at the very end. This is because `--sim-mode` blocks for `--timeout`
    seconds serving sockets: an operator or a studio launcher needs the
    descriptor and ports the instant they exist, not once the whole run
    finishes. Verified with the streams captured separately.
  * `--expect` is accepted (for global-flag parity, like `--image-bundle` on
    the plain smoke) but reported back via an INFO issue
    (`renode.expect-ignored`) rather than acted on: sim mode routes the
    console to the UART socket, not to a scannable text stream.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import resolve_sdk_root_wide, sdk_ladder_divergence_issue
from tan.commands.sdk_cmd import global_default_foreign_project_issue, project_pin_issue
from tan.commands.build_output import (
    ManifestInvalid,
    ManifestUnavailable,
    load_manifest,
    resolve_app_base,
)
from tan.commands.doctor_cmd import on_path
from tan.core.global_flags import accept_global_flags
from tan.core.renode_plan import (
    RenodeError,
    build_renode_argv,
    elf_vector_table_base,
    platform_files_for_sku,
    platform_stem_for_sku,
    renode_cpu_halted,
    renode_rejected_argv,
    select_sku,
    zephyr_elf_from_manifest,
)
from tan.core.renode_sim import (
    MonitorLine,
    build_sim_descriptor,
    build_sim_renode_argv,
    build_sim_resc_text,
    classify_monitor_line,
    dispatch_control_line,
    ready_marker,
    sim_profile_deferred_message,
)
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat
from tan.core.shapes import is_sdk_root

#: The `SystemManifestError` message prefix `parse_system_manifest` raises
#: for a `schema_version` mismatch -- distinguishing it from every other
#: parse failure is what picks `renode.manifest-schema` (ValidationFailure)
#: over `renode.manifest-invalid` (RuntimeFailure), mirroring the oracle's
#: `SystemManifestError::UnsupportedSchemaVersion` match arm.
_SCHEMA_VERSION_PREFIX = "unsupported system-manifest schema_version"


def _normalize_join(base: str, path: str) -> str:
    """`normalize_path(base.join(path))`: cwd-anchored (unless `path` is
    already absolute, matching `PathBuf::join`'s replace-on-absolute rule),
    lexically normalised, in the HOST's own separators -- deliberately NOT
    forward-slash-forced (see the module docstring)."""
    return os.path.normpath(os.path.join(base, path))


def _resolve_root_arg(raw: str | None, cwd: str, default: str) -> str:
    """A `--build-root`/`--log`-shaped override: absolute as typed
    (normalised), relative anchored on the real CWD; `default` when absent."""
    if raw is None:
        return default
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return _normalize_join(cwd, raw)


#: tan-cli#408: one implementation, in `tan.core.shapes`, imported under the
#: private name this module's call site already uses.
_is_sdk_root = is_sdk_root


@dataclass(frozen=True)
class _SdkRootAndTier:
    """`_resolve_sdk_root_and_tier`'s own return shape -- a named result, not
    a growing tuple (the same reasoning `build_cmd.SdkRootResolution`
    documents): a fact one caller needs (`foreign_global_default_for`,
    tan-cli#464) must not force every field before it to be re-counted by
    position."""

    path: str | None
    tier: str | None
    broken_project_pin: str | None = None
    foreign_global_default_for: str | None = None


def _resolve_sdk_root_and_tier(
    sdk_root_arg: str | None, workspace_root: str
) -> _SdkRootAndTier:
    """`--sdk-root` (TERMINAL -- returned AS TYPED when it has the loader
    script, `None` otherwise, never falling through to a lower tier) > the
    project's own `.alp/sdk-path` pin > the machine-global default
    (`~/.alp/sdk-default`) > the WIDE positional walk
    (`build_cmd.resolve_sdk_root_wide`) -- wide, not the narrow ladder thirteen
    other commands take, because the oracle's `renode` reads its platform
    descriptors out of the child `<ws>/alp-sdk` over a competing `../alp-sdk`
    (tan-cli#263). No `ALP_SDK_ROOT` tier (tried and reverted -- see
    `resolve_sdk_root_ladder`'s own docstring).

    `.broken_project_pin` (tan-cli#263 review) is the broken project pin
    carried through from `resolve_sdk_root_wide`, `None` on the `--sdk-root`
    branch. Same for `.foreign_global_default_for` (tan-cli#464).
    """
    if sdk_root_arg is not None:
        if _is_sdk_root(sdk_root_arg):
            return _SdkRootAndTier(sdk_root_arg, "sdkRootFlag")
        return _SdkRootAndTier(None, None)
    resolution = resolve_sdk_root_wide(None, Path(workspace_root))
    found = resolution.path
    if found is None:
        return _SdkRootAndTier(None, None, resolution.broken_project_pin)
    return _SdkRootAndTier(
        str(found), resolution.tier, resolution.broken_project_pin,
        resolution.foreign_global_default_for,
    )


def _data(**overrides: Any) -> dict[str, Any]:
    """The `tan renode` JSON `data` payload, camelCase, every key always
    present (mirrors the Rust `RenodeReport`'s `#[derive(Default)]`)."""
    out: dict[str, Any] = {
        "sku": "",
        "platformStem": "",
        "repl": "",
        "resc": "",
        "elf": "",
        "logPath": "",
        "timeout": 0,
        "expect": None,
        "expectFound": False,
        "renodeArgv": [],
        # `--sim-mode` only: always present and empty/zero on the plain
        # smoke (and on a sim failure before each is resolved), matching
        # the oracle's own `RenodeReport::default()` fields.
        "descriptor": "",
        "controlPort": 0,
        "uartPort": 0,
    }
    out.update(overrides)
    return out


def _issue(code: str, severity: str, message: str) -> Issue:
    return Issue(code, severity, message)


# ── spawning + teeing ────────────────────────────────────────────────────────

#: Posted by each reader thread on EOF. `run_renode` treats "both readers
#: posted their sentinel" as the child's own pipes having closed -- the same
#: signal Rust's `mpsc::RecvTimeoutError::Disconnected` carries once every
#: `Sender` clone (one per pipe) has dropped.
_EOF = object()


def _pump_lossy_lines(stream, q: "queue.Queue[object]") -> None:
    """Read `\\n`-delimited lines from `stream` (binary) into `q`, lossily
    replacing invalid UTF-8 instead of dying on the first bad line -- a
    stray byte (reset framing noise, a half-initialised UART) must not
    truncate the tee log or a later `--expect` match."""
    try:
        while True:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            while line.endswith(("\n", "\r")):
                line = line[:-1]
            q.put(line)
    except (OSError, ValueError):
        pass
    finally:
        q.put(_EOF)


def run_renode(
    argv: list[str], log_path: str, timeout_s: float, expect: str | None, echo_stdout: bool
) -> tuple[bool, int | None, bool, bool]:
    """Run Renode, tee-ing its (stdout+stderr) console to `log_path`.
    Terminates on: the `expect` marker appearing, the child exiting (both
    pipes EOF), or `timeout_s` elapsing -- the deadline fires even on a
    silent child via a reader-thread + queue-with-timeout, never a blocking
    line-iterator that would hang forever on a wedged-but-alive Renode.

    Returns `(expect_found, natural_exit, argv_rejected, cpu_halted)`:
    `natural_exit` is the child's own return code, but ONLY when it exited on
    its OWN before the deadline/kill -- a forced-kill-for-timeout child
    always reports `None` here, so a plain timeout is never mistaken for a
    self-inflicted crash. `argv_rejected`/`cpu_halted` are latched from the
    console text (Renode exits 0 in both failure shapes, so the exit status
    alone carries no signal for either).

    Raises `OSError` when the process cannot even be spawned (the binary
    vanished between the PATH gate and here, or is not executable) --
    the caller maps that to `renode.run-failed`.
    """
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    logf = open(log_path, "w", encoding="utf-8", newline="\n")
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        logf.close()
        raise

    q: "queue.Queue[object]" = queue.Queue()
    t_out = threading.Thread(target=_pump_lossy_lines, args=(proc.stdout, q), daemon=True)
    t_err = threading.Thread(target=_pump_lossy_lines, args=(proc.stderr, q), daemon=True)
    t_out.start()
    t_err.start()

    found = False
    natural_exit: int | None = None
    argv_rejected = False
    cpu_halted = False
    eof_count = 0
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline reached; child may still be alive (silent hang)
            try:
                item = q.get(timeout=remaining)
            except queue.Empty:
                break
            if item is _EOF:
                eof_count += 1
                if eof_count >= 2:
                    # Both readers hit EOF -- the child's own pipes closed,
                    # which happens at process exit. Capture the REAL status
                    # now, before the unconditional kill() below turns every
                    # outcome (including this one) into an indistinguishable
                    # "we killed it".
                    try:
                        natural_exit = proc.wait()
                    except OSError:
                        natural_exit = None
                    break
                continue
            line = item  # type: ignore[assignment]
            logf.write(line + "\n")
            logf.flush()
            if echo_stdout:
                print(line)
            if renode_rejected_argv(line):
                argv_rejected = True
            if renode_cpu_halted(line):
                cpu_halted = True
            if expect is not None and expect in line:
                found = True
                break
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait()
        except OSError:
            pass
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        logf.close()

    return found, natural_exit, argv_rejected, cpu_halted


def _rust_debug_str(text: str) -> str:
    """Rust's `{:?}` for a `&str`: double-quoted, with `\\` and `"` escaped.
    Used for `--expect`'s value in the `renode.expect-not-found` message,
    where Python's own `repr()` (single-quoted) would read differently."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exit_status_desc(code: int) -> str:
    """A short description for a log message. Matches the oracle's
    `exit_status_desc`, which falls through to Rust `ExitStatus`'s own
    `Display` for anything that isn't a plain exit code -- on Unix that's
    `"signal: N (NAME)"` for a signal death, verified against the shipped
    `tan.exe`. A negative `returncode` is Python's own (POSIX-only) spelling
    of "killed by signal N"."""
    if code >= 0:
        return f"exit code {code}"
    try:
        import signal  # noqa: PLC0415 -- POSIX-only, kept out of module scope

        name = signal.Signals(-code).name
    except ValueError:
        return f"signal: {-code}"
    return f"signal: {-code} ({name})"


def _best_effort_vtor(elf_path: str) -> int | None:
    """Read `elf_path` purely to learn where its vector table really is, so
    the descriptor can point the CPU at it instead of letting Renode guess
    from the lowest vaddr -- which halts an MRAM-linked image before it
    executes a single instruction. Best-effort: an unreadable or unexpected
    ELF returns `None` and the caller injects nothing, leaving Renode exactly
    as it was."""
    try:
        with open(elf_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return elf_vector_table_base(data)


# ── --sim-mode: the monitor bridge ──────────────────────────────────────────

#: Per-command deadline. A wedged-but-alive Renode must not strand a client.
_COMMAND_TIMEOUT_S = 15.0
#: Total budget for the boot drain, split across `_DRAIN_ATTEMPTS`.
_DRAIN_TIMEOUT_S = 90.0
#: On a loaded runner the pinned Renode's monitor can be slow to first
#: respond, and a single lost sync would strand the whole session.
_DRAIN_ATTEMPTS = 3
#: Every `RenodeMonitor` failure path reports this once broken. Reused for a
#: broken-latch race: a client thread that failed mid-command may have left
#: unread lines in the queue, so the next command could otherwise capture
#: *its* output -- indistinguishable from a correct reply, which is the one
#: outcome worth refusing outright.
_MONITOR_UNUSABLE = "Renode monitor is unusable after an earlier failure."
#: How long teardown waits for Renode to act on `quit` before killing it.
#: This is a teardown, not a graceful-shutdown protocol -- the process is
#: going away either way; one second is enough for the emulation to close
#: its sockets and flush its log.
_QUIT_GRACE_S = 1.0


class RenodeMonitor:
    """Drives Renode's monitor over the child's stdin/stdout for `tan renode
    --sim-mode`. Plumbing ONLY -- every decision about what a monitor line
    means is `tan.core.renode_sim.classify_monitor_line`, unit-tested there;
    this class owns the pipes, the reader thread and the deadline.

    Port of `crates/tan-cli/src/commands/renode/monitor.rs`'s
    `RenodeMonitor<W>`. `command` writes the line plus an `echo "<sentinel>"`
    marker and drains stdout until the bare sentinel comes back; the pump
    thread + queue-with-timeout is what lets the per-command deadline
    actually fire, rather than a blocking read hanging forever on a wedged
    Renode. `command`/`quit`/`drain_boot` all serialise on `self._lock`,
    matching the Rust `Mutex<MonitorInner<W>>` -- a client sending two
    commands concurrently must not interleave their sentinels.
    """

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin = stdin
        self._lock = threading.Lock()
        self._seq = 0
        self._broken = False
        self._cpu_halted_flag = False
        self._lines: "queue.Queue[object]" = queue.Queue()
        raw: "queue.Queue[object]" = queue.Queue()
        threading.Thread(target=_pump_lossy_lines, args=(stdout, raw), daemon=True).start()
        threading.Thread(target=self._forward, args=(raw,), daemon=True).start()

    def _forward(self, raw: "queue.Queue[object]") -> None:
        """Two hops: the shared lossy-line pump, then this forwarder, which
        inspects every line for the halt marker (issue #64) before handing
        it on -- this is what makes the latch window-independent, catching a
        `CPU was halted` line that lands between two client commands (or
        after the last one), which belongs to no command's collection
        window at all."""
        while True:
            item = raw.get()
            if item is _EOF:
                self._lines.put(_EOF)
                return
            line = item  # type: ignore[assignment]
            if renode_cpu_halted(line):
                self._cpu_halted_flag = True
            self._lines.put(line)

    def cpu_halted(self) -> bool:
        """Whether Renode ever reported the CPU halted, wherever that line
        landed. Read at teardown: the halt does not fail the command it
        happens to interleave with, it fails the RUN."""
        return self._cpu_halted_flag

    def command(self, cmd: str) -> str:
        """Run one monitor command and return its captured output. Raises
        `RuntimeError` (message = the failure reason) on a write failure,
        timeout, EOF, or a monitor-reported `[ERROR]` for this command."""
        return self._command_within(cmd, _COMMAND_TIMEOUT_S)

    def _command_within(self, cmd: str, timeout_s: float) -> str:
        with self._lock:
            if self._broken:
                raise RuntimeError(_MONITOR_UNUSABLE)
            self._seq += 1
            sentinel = f"__ALP_SIM_DONE_{self._seq}__"
            try:
                # The marker is QUOTED on purpose: Renode >= 1.16 reads a
                # bare `echo TOKEN` as an element lookup ("No such emulation
                # element").
                self._stdin.write(f"{cmd}\n".encode())
                self._stdin.write(f'echo "{sentinel}"\n'.encode())
                self._stdin.flush()
            except (OSError, ValueError) as err:
                self._broken = True
                raise RuntimeError(
                    f"Renode monitor write failed for {_rust_debug_str(cmd)}: {err}"
                ) from err

            deadline = time.monotonic() + timeout_s
            out: list[str] = []
            errors: list[str] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._broken = True
                    raise RuntimeError(
                        f"timed out after {int(timeout_s)}s awaiting Renode "
                        f"response to {_rust_debug_str(cmd)}."
                    )
                try:
                    item = self._lines.get(timeout=remaining)
                except queue.Empty:
                    self._broken = True
                    raise RuntimeError(
                        f"timed out after {int(timeout_s)}s awaiting Renode "
                        f"response to {_rust_debug_str(cmd)}."
                    )
                if item is _EOF:
                    self._broken = True
                    raise RuntimeError(
                        f"Renode monitor closed while awaiting response to "
                        f"{_rust_debug_str(cmd)}."
                    )
                line = item  # type: ignore[assignment]
                kind = classify_monitor_line(line, sentinel, cmd)
                if kind is MonitorLine.DONE:
                    # Reaching the sentinel with errors collected does NOT
                    # latch `broken`: the monitor itself is fine, this one
                    # command failed.
                    if errors:
                        raise RuntimeError(
                            f"Renode reported an error for {_rust_debug_str(cmd)}: "
                            + " | ".join(errors)
                        )
                    return "\n".join(out)
                if kind is MonitorLine.ERROR:
                    errors.append(line.strip())
                elif kind is MonitorLine.IGNORE:
                    pass
                else:
                    out.append(line)

    def drain_boot(self) -> None:
        """Swallow the boot-time monitor output so the first real client
        command gets a clean reply. Retried: each attempt sends a fresh
        `version` with its own sentinel, which re-syncs, and clears the
        `broken` latch the previous attempt's timeout set. Raises
        `RuntimeError` after `_DRAIN_ATTEMPTS` failures."""
        per = max(10.0, _DRAIN_TIMEOUT_S / max(_DRAIN_ATTEMPTS, 1))
        last = f"drain_boot failed after {_DRAIN_ATTEMPTS} attempts"
        for _ in range(_DRAIN_ATTEMPTS):
            with self._lock:
                self._broken = False
            try:
                self._command_within("version", per)
                return
            except RuntimeError as err:
                last = str(err)
        raise RuntimeError(last)

    def quit(self) -> None:
        """Best-effort `quit` on teardown. This only ASKS; whether Renode
        gets time to shut its emulation down depends on the caller polling
        for the exit afterwards (`_teardown_sim` does, briefly)."""
        with self._lock:
            try:
                self._stdin.write(b"quit\n")
                self._stdin.flush()
            except (OSError, ValueError):
                pass


def _bind_sim_listeners() -> tuple[socket.socket, socket.socket, int, int]:
    """Bind the control + UART listeners on ephemeral `127.0.0.1` ports and
    read the assigned port numbers back. Both stay held (LISTENING), so the
    ports cannot be taken from under us and are distinct by construction.
    Returns `(control, uart, control_port, uart_port)`.

    BIND BEFORE ADVERTISING is the whole point: by the time a caller writes
    `sim-descriptor.json` naming these ports, both listeners are already
    accepting, so a studio client that reads the descriptor and connects at
    once can never race into an ECONNREFUSED -- the kernel backlogs the
    connection pre-accept.
    """
    ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ctrl.bind(("127.0.0.1", 0))
        ctrl.listen()
        uart = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            uart.bind(("127.0.0.1", 0))
            uart.listen()
        except OSError:
            uart.close()
            raise
    except OSError:
        ctrl.close()
        raise
    return ctrl, uart, ctrl.getsockname()[1], uart.getsockname()[1]


def _close_quietly(*socks: socket.socket) -> None:
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


def _serve_control(listener: socket.socket, monitor: RenodeMonitor) -> None:
    """Accept control clients forever, each on its own thread. The listener
    is already bound + listening by the time the descriptor advertises its
    port."""
    while True:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        threading.Thread(
            target=_handle_control_client, args=(conn, monitor), daemon=True
        ).start()


def _handle_control_client(conn: socket.socket, monitor: RenodeMonitor) -> None:
    """One control client: line-oriented, ONE request line -> ONE reply
    line, until the peer closes. A bad line never kills the connection --
    `dispatch_control_line` answers it with `ERR <reason>` so the framing
    invariant holds for the rest of the session. A blank line is skipped
    without a reply, verbatim from the retired Python."""
    try:
        reader = conn.makefile("rb")
        while True:
            raw = reader.readline()
            if not raw:
                return  # peer closed, or a real IO error
            # Lossy, not strict: a stray non-UTF-8 byte must not drop the
            # session.
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            reply = dispatch_control_line(line, monitor.command)
            try:
                conn.sendall(f"{reply}\n".encode())
            except OSError:
                return
    except OSError:
        return
    finally:
        _close_quietly(conn)


def _serve_uart_silent(listener: socket.socket) -> None:
    """Accept UART clients and hold each connection open, streaming
    nothing.

    This is the socket half of a channel whose CONTENT is deferred
    (`tan-cli#77`): the `ram_console_buf` RAM-ring streamer that fills it is
    a follow-up. Accepting-and-silent is exactly what the retired Python did
    when an image carried no `ram_console_buf` symbol, so a studio client's
    serial view connects successfully and simply stays empty rather than
    failing to connect."""
    while True:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        threading.Thread(target=_hold_uart_client, args=(conn,), daemon=True).start()


def _hold_uart_client(conn: socket.socket) -> None:
    """Output-only to studio; read solely to detect the close and reap."""
    try:
        while True:
            data = conn.recv(256)
            if not data:
                return
    except OSError:
        return
    finally:
        _close_quietly(conn)


def _spawn_renode_sim(argv: list[str], log_path: str) -> subprocess.Popen:
    """Spawn headless Renode with stdio wired for the monitor bridge: stdin
    + stdout are pipes the bridge drives, stderr goes straight to the log
    file (verbatim from the retired Python's `stderr=logf`)."""
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(log_path, "wb") as logf:
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf
        )


def _teardown_sim(monitor: RenodeMonitor, proc: subprocess.Popen) -> None:
    """Ask Renode to `quit`, give it `_QUIT_GRACE_S` to actually do it, then
    make sure it is gone. `quit` on its own is only a REQUEST: killing the
    child microseconds after the flush would leave the emulation no time to
    close its sockets or flush its log."""
    monitor.quit()
    deadline = time.monotonic() + _QUIT_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return  # quit worked; poll() already reaped it
        time.sleep(0.025)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait()
    except OSError:
        pass


def _announce_ready(json_mode: bool, timeout: int) -> None:
    """Print the readiness marker. The LINE is
    `tan.core.renode_sim.ready_marker` -- it carries the consumer's `ready
    (timeout` poll token and is pinned by a test there, since a reword
    strands every consumer. The only decision left here is the STREAM:
    stdout in text mode (where the retired Python printed it), stderr in
    JSON mode, where stdout carries the single Envelope line and nothing
    else. A consumer teeing only stdout to a log file therefore will not see
    it in JSON mode -- poll the merged output, or poll for
    `sim-descriptor.json` and the port it names."""
    line = ready_marker(timeout)
    if json_mode:
        print(line, file=sys.stderr)
        sys.stderr.flush()
    else:
        print(line)
        sys.stdout.flush()


def _resolve_bundle_elf(bundle_dir: str, manifest: Any, core: str | None) -> str:
    """Resolve the firmware ELF inside a pre-built `--image-bundle` dir: the
    bundle's `system-manifest.yaml` (reusing the slice resolver) ->
    `<bundle>/zephyr/zephyr.elf` -> the single `*.elf` in the bundle. Raises
    `RenodeError` on every failure -- the caller maps all of them to
    `renode.elf-missing`, matching the oracle's `resolve_bundle_elf`."""
    if manifest is not None:
        return zephyr_elf_from_manifest(manifest, bundle_dir, core)
    direct = os.path.join(bundle_dir, "zephyr", "zephyr.elf")
    if os.path.isfile(direct):
        return direct
    try:
        names = os.listdir(bundle_dir)
    except OSError as err:
        raise RenodeError(f"could not read --image-bundle {bundle_dir}: {err}") from err
    elfs = sorted(
        name
        for name in names
        if name.endswith(".elf") and os.path.isfile(os.path.join(bundle_dir, name))
    )
    if len(elfs) == 1:
        return os.path.join(bundle_dir, elfs[0])
    if not elfs:
        raise RenodeError(
            f"no firmware ELF in --image-bundle {bundle_dir} (looked for "
            "system-manifest.yaml, zephyr/zephyr.elf, *.elf)."
        )
    names_repr = "[" + ", ".join(_rust_debug_str(e) for e in elfs) + "]"
    raise RenodeError(
        f"multiple *.elf in --image-bundle {bundle_dir} ({names_repr}); "
        "can't pick one automatically."
    )


# ── the command ─────────────────────────────────────────────────────────────


def _run(
    app_path_arg: str,
    build_root_arg: str | None,
    board_arg: str | None,
    core_arg: str | None,
    sdk_root_arg: str | None,
    project_arg: str | None,
    image_bundle_arg: str | None,
    log_arg: str | None,
    timeout: int,
    expect: str | None,
    json_mode: bool,
    cwd: str,
    sim_mode_arg: bool = False,
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str], SdkInfo | None, Project]:
    """Everything between argument parsing and the envelope. Returns
    `(exit_code, data, issues, text_lines, sdk, project)`."""
    # tan-cli#470: the SAME ladder `size`/`image` resolve through
    # `resolve_app_base` -- an explicit APP_PATH positional wins (anchored on
    # cwd); otherwise the default build root falls back to `--project`, then
    # cwd. Routed through the shared function itself rather than re-derived a
    # third time (a parallel derivation is how tan-cli#289/#291 happened).
    # `resolve_app_base` is separator-agnostic (it only ever returns one of
    # the two strings handed to it), so feeding it a native-separator
    # workspace dir and renormalising the result keeps this command's own
    # native-path-style convention (see the module docstring) even though
    # `size`/`image` feed it a POSIX one.
    workspace_dir = _normalize_join(cwd, project_arg or ".")
    app_path = os.path.normpath(resolve_app_base(app_path_arg, workspace_dir))
    board_yaml_path = os.path.join(app_path, "board.yaml")
    # tan-cli#236: routed through the shared seam, though this site already
    # existence-checked by hand.
    project = Project.resolved(app_path, board_yaml_path)

    build_root = _resolve_root_arg(build_root_arg, cwd, os.path.join(app_path, "build"))
    log_path = _resolve_root_arg(log_arg, cwd, os.path.join(build_root, "renode.log"))

    # Filled in incrementally as each pre-flight step resolves, mirroring the
    # oracle's `RenodeReport`, which is one mutable struct written to as
    # `run()` progresses -- so a LATER failure (e.g. `renode.binary-missing`)
    # still reports the sku/platformStem/repl/resc/elf a caller resolved
    # before it (verified against the shipped binary).
    known: dict[str, Any] = {}

    def data(**overrides: Any) -> dict[str, Any]:
        return _data(logPath=log_path, timeout=timeout, expect=expect, **known, **overrides)

    def fail(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        return exit_code, data(), [_issue(code, "error", message)], [f"renode: {message}"], None, project

    # `--sim-mode` branches FIRST, resolving everything from `--image-bundle`
    # instead of the build root below -- mirrors the oracle's `mod.rs::run`,
    # which constructs its `RenodeReport` with this same PLAIN-mode
    # `log_path` default before handing off to `sim::run`, so a pre-flight
    # sim failure up to and including `renode.binary-missing` still reports
    # THIS `log_path`, not the sim-specific one (see the module docstring).
    if sim_mode_arg:
        return _run_sim(
            app_path=app_path,
            image_bundle_arg=image_bundle_arg,
            board_arg=board_arg,
            core_arg=core_arg,
            sdk_root_arg=sdk_root_arg,
            project_arg=project_arg,
            log_arg=log_arg,
            timeout=timeout,
            expect=expect,
            json_mode=json_mode,
            cwd=cwd,
            project=project,
            base_log_path=log_path,
        )

    # SDK-root guard: `cli_workspace_root(g)` is `cwd` joined with `--project`
    # (the GLOBAL flag) -- NOT `app_path`. See the module docstring for why
    # these two deliberately diverge in the oracle.
    workspace_root = os.path.join(cwd, project_arg) if project_arg else cwd
    sdk_resolution = _resolve_sdk_root_and_tier(sdk_root_arg, workspace_root)
    sdk_root = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_broken_pin = sdk_resolution.broken_project_pin
    sdk_foreign_default = sdk_resolution.foreign_global_default_for
    if sdk_root is None:
        return fail("renode.sdk-root-not-found", "Cannot locate alp-sdk root.")
    sdk = SdkInfo(sdk_root, sdk_tier or "none")

    # WHICH checkout answered -- carried by every envelope this function can
    # still produce, refusals included. Both of these used to be appended ~60
    # lines below, past six `fail_sdk` returns that dropped them: measured on a
    # workspace holding a child and a lateral checkout, `tan renode` refused
    # with `renode.manifest-unavailable` and no `sdk.discovery-divergent` at
    # all. That refusal is the one that matters most, because its own message
    # says to run `tan build` first -- and `tan build` is on the OTHER ladder,
    # so a silent refusal sends the reader to the command whose checkout
    # disagrees with the one this envelope's `sdk.root` just named.
    sdk_context_issues: list[Issue] = []
    pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier or "none")
    if pin_issue is not None:
        sdk_context_issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(sdk_foreign_default)
    if foreign_issue is not None:
        sdk_context_issues.append(foreign_issue)
    # tan-cli#407. `sdk_root_arg` is the RAW flag -- `sdk_root` above is the
    # resolved text, and an explicit root puts both ladders on the terminal
    # `sdkRootFlag` tier, where the helper correctly returns `None`.
    divergence = sdk_ladder_divergence_issue(sdk_root_arg, Path(workspace_root), wide=True)
    if divergence is not None:
        sdk_context_issues.append(divergence)

    def fail_sdk(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        # The refusal FIRST, context after: `issues[0]` stays the thing that
        # failed for any consumer that reads it positionally.
        return (
            exit_code,
            data(),
            [_issue(code, "error", message), *sdk_context_issues],
            [f"renode: {message}"],
            sdk,
            project,
        )

    manifest_path = os.path.join(build_root, "system-manifest.yaml")
    try:
        _text, manifest = load_manifest(build_root)
    except ManifestUnavailable as err:
        return fail_sdk(
            "renode.manifest-unavailable",
            f"no system-manifest.yaml at {manifest_path}; run `tan build --project "
            f"{app_path}` first ({err.detail}).",
        )
    except ManifestInvalid as err:
        message = f"{err.path}: {err.detail}"
        if err.detail.startswith(_SCHEMA_VERSION_PREFIX):
            return fail_sdk("renode.manifest-schema", message, ExitCode.VALIDATION_FAILURE)
        return fail_sdk("renode.manifest-invalid", message)

    try:
        sku = select_sku(manifest, board_arg)
    except RenodeError as err:
        return fail_sdk("renode.sku-unresolved", err.message)
    known["sku"] = sku

    try:
        elf = zephyr_elf_from_manifest(manifest, build_root, core_arg)
    except RenodeError as err:
        return fail_sdk("renode.slice", err.message)
    if not os.path.isfile(elf):
        # Deliberately BEFORE `known["elf"]` is set: the oracle's own
        # `report.elf` assignment sits after this check too, so an
        # `elf-missing` failure reports `data.elf: ""`, not the unbuilt path
        # -- verified against the shipped binary.
        return fail_sdk(
            "renode.elf-missing",
            f"Zephyr ELF not found at {elf}; run `tan build --project {app_path}` first.",
        )
    known["elf"] = elf

    try:
        repl, resc = platform_files_for_sku(sku, sdk_root)
    except RenodeError as err:
        return fail_sdk("renode.descriptor", err.message)
    for descriptor in (repl, resc):
        if not os.path.isfile(descriptor):
            # Same ordering rule as `elf` above: `platformStem`/`repl`/`resc`
            # stay empty in `data` on this failure.
            return fail_sdk(
                "renode.descriptor-missing", f"missing Renode descriptor {descriptor}."
            )
    known["platformStem"] = platform_stem_for_sku(sku)
    known["repl"] = repl
    known["resc"] = resc

    renode_bin = on_path("renode")
    if renode_bin is None:
        return fail_sdk(
            "renode.binary-missing",
            "`renode` binary not found on PATH. Install Renode (https://renode.io). "
            "tan renode does not silently pass when Renode is missing.",
        )

    # Same two SDK-context issues the refusals above carry, in the same order.
    # tan-cli#407's half says the `.repl`/`.resc` platform descriptors this
    # smoke boots came from the WIDE ladder's checkout while the ELF it boots
    # was built by `tan build` off the NARROW one -- a descriptor/ELF pair from
    # two SDK versions is a simulation failure with no visible cause.
    issues: list[Issue] = list(sdk_context_issues)
    text: list[str] = []
    if image_bundle_arg is not None:
        msg = (
            f"renode: --image-bundle {image_bundle_arg} accepted but unused by the "
            "single-slice smoke."
        )
        issues.append(_issue("renode.image-bundle-unused", "info", msg))
        if not json_mode:
            text.append(msg)

    vtor = _best_effort_vtor(elf)
    argv = build_renode_argv(renode_bin, repl, resc, elf, vtor)
    # Set BEFORE `run_renode` is called (mirrors the oracle's
    # `report.renode_argv = argv.clone()` at crates/tan-cli/src/commands/
    # renode/mod.rs:271, also before its own `run_renode` call): a spawn
    # failure below must still report the exact command line that could not
    # be started, not an empty `renodeArgv`.
    known["renodeArgv"] = argv

    if not json_mode:
        text.append(
            f"renode: booting {elf} on {os.path.basename(repl)} (log -> {log_path})"
        )

    try:
        found, natural_exit, argv_rejected, cpu_halted = run_renode(
            argv, log_path, float(timeout), expect, not json_mode
        )
    except OSError as err:
        return fail_sdk("renode.run-failed", f"failed to run renode: {err}")

    exit_code = ExitCode.SUCCESS
    if argv_rejected:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            "renode: rejected the command line and printed its usage instead of booting "
            f"— nothing was simulated (see {log_path})."
        )
        issues.append(_issue("renode.argv-rejected", "error", msg))
        if not json_mode:
            text.append(msg)
    elif cpu_halted:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            "renode: the CPU halted on its first instruction fetch — no firmware code "
            f"ever ran, even though the process exited cleanly (see {log_path})."
        )
        issues.append(_issue("renode.cpu-halted", "error", msg))
        if not json_mode:
            text.append(msg)
    elif expect is not None:
        if not found:
            exit_code = ExitCode.RUNTIME_FAILURE
            # `{expect:?}` in Rust -- `Debug` for `&str`, which is double-quoted,
            # not Python `repr()`'s single quotes.
            msg = (
                f"renode: console did not contain {_rust_debug_str(expect)} within "
                f"{timeout}s (see {log_path})."
            )
            issues.append(_issue("renode.expect-not-found", "error", msg))
            if not json_mode:
                text.append(msg)
    elif natural_exit is not None and natural_exit != 0:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            f"renode: exited early ({_exit_status_desc(natural_exit)}) before the "
            f"{timeout}s timeout (see {log_path})."
        )
        issues.append(_issue("renode.exited-nonzero", "error", msg))
        if not json_mode:
            text.append(msg)

    return (
        exit_code,
        data(expectFound=found),
        issues,
        text,
        sdk,
        project,
    )


def _run_sim(
    *,
    app_path: str,
    image_bundle_arg: str | None,
    board_arg: str | None,
    core_arg: str | None,
    sdk_root_arg: str | None,
    project_arg: str | None,
    log_arg: str | None,
    timeout: int,
    expect: str | None,
    json_mode: bool,
    cwd: str,
    project: Project,
    base_log_path: str,
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str], SdkInfo | None, Project]:
    """`tan renode --sim-mode`: the studio hardware-simulator gateway. Port
    of `crates/tan-cli/src/commands/renode/sim.rs::run`. Returns the same
    6-tuple shape `_run` does; `base_log_path` is the PLAIN smoke's own
    `log_path` default, reported in `data.logPath` until this function
    resolves its own sim-specific default further down (see the module
    docstring)."""
    # `logPath` starts as the PLAIN smoke's default and lives in `known` (not
    # a hardcoded `data()` kwarg) precisely so the later `known["logPath"] =
    # log_path` overwrite below can replace it without a duplicate-keyword
    # collision against `**known`.
    known: dict[str, Any] = {"logPath": base_log_path}

    def data(**overrides: Any) -> dict[str, Any]:
        return _data(timeout=timeout, expect=expect, **known, **overrides)

    def fail(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        return exit_code, data(), [_issue(code, "error", message)], [f"renode: {message}"], None, project

    if image_bundle_arg is None:
        return fail("renode.sim-bundle-required", "--sim-mode requires --image-bundle <dir>.")
    bundle_dir = _normalize_join(cwd, image_bundle_arg)
    if not os.path.isdir(bundle_dir):
        return fail(
            "renode.sim-bundle-missing", f"--image-bundle {bundle_dir} is not a directory."
        )

    # SDK-root guard: the SAME wide ladder + workspace root the plain smoke
    # uses -- the oracle's `sim::run` calls the identical `resolve_sdk_root`
    # the plain `mod.rs::run` does, with no sim-specific branching.
    workspace_root = os.path.join(cwd, project_arg) if project_arg else cwd
    sdk_resolution = _resolve_sdk_root_and_tier(sdk_root_arg, workspace_root)
    sdk_root = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_broken_pin = sdk_resolution.broken_project_pin
    sdk_foreign_default = sdk_resolution.foreign_global_default_for
    if sdk_root is None:
        return fail("renode.sdk-root-not-found", "Cannot locate alp-sdk root.")
    sdk = SdkInfo(sdk_root, sdk_tier or "none")

    # The `--sim-mode` half of the plain smoke's `sdk_context_issues` -- same
    # two issues, same order, same reason for computing them HERE rather than
    # past the refusals. See `_run`'s own comment.
    sdk_context_issues: list[Issue] = []
    pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier or "none")
    if pin_issue is not None:
        sdk_context_issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(sdk_foreign_default)
    if foreign_issue is not None:
        sdk_context_issues.append(foreign_issue)
    divergence = sdk_ladder_divergence_issue(sdk_root_arg, Path(workspace_root), wide=True)
    if divergence is not None:
        sdk_context_issues.append(divergence)

    def fail_sdk(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        return (
            exit_code,
            data(),
            [_issue(code, "error", message), *sdk_context_issues],
            [f"renode: {message}"],
            sdk,
            project,
        )

    # The bundle's own manifest, when it has one: it supplies both the SKU
    # and the slice -> ELF resolution. Absent is fine (a bare bundle of
    # ELFs) -- unlike the plain smoke, a missing manifest is NOT an error
    # here.
    manifest_path = os.path.join(bundle_dir, "system-manifest.yaml")
    manifest = None
    if os.path.isfile(manifest_path):
        try:
            _text, manifest = load_manifest(bundle_dir)
        except ManifestUnavailable as err:
            return fail_sdk("renode.manifest-unavailable", f"{manifest_path}: {err.detail}")
        except ManifestInvalid as err:
            message = f"{err.path}: {err.detail}"
            if err.detail.startswith(_SCHEMA_VERSION_PREFIX):
                return fail_sdk("renode.manifest-schema", message, ExitCode.VALIDATION_FAILURE)
            return fail_sdk("renode.manifest-invalid", message)

    board = (board_arg or "").strip()
    if board:
        sku = board
    else:
        manifest_sku = (manifest.sku or "").strip() if manifest is not None else ""
        if not manifest_sku:
            return fail_sdk(
                "renode.sku-unresolved",
                "--sim-mode could not determine the board: pass --board <SKU> "
                "(no hw_info.sku in the bundle manifest).",
            )
        sku = manifest_sku
    known["sku"] = sku

    try:
        elf = _resolve_bundle_elf(bundle_dir, manifest, core_arg)
    except RenodeError as err:
        return fail_sdk("renode.elf-missing", err.message)
    if not os.path.isfile(elf):
        return fail_sdk("renode.elf-missing", f"firmware ELF not found at {elf}.")
    known["elf"] = elf

    # Only the `.repl` matters here: unlike the plain smoke, sim mode
    # GENERATES its own boot script rather than including the SDK's `.resc`.
    try:
        repl, _resc_unused = platform_files_for_sku(sku, sdk_root)
    except RenodeError as err:
        return fail_sdk("renode.descriptor", err.message)
    if not os.path.isfile(repl):
        return fail_sdk("renode.descriptor-missing", f"missing Renode descriptor {repl}.")
    known["platformStem"] = platform_stem_for_sku(sku)
    known["repl"] = repl

    renode_bin = on_path("renode")
    if renode_bin is None:
        return fail_sdk(
            "renode.binary-missing",
            "`renode` binary not found on PATH. Install Renode (https://renode.io). "
            "tan renode does not silently pass when Renode is missing.",
        )

    # BIND BEFORE ADVERTISING -- see `_bind_sim_listeners`'s own docstring.
    try:
        ctrl_sock, uart_sock, control_port, uart_port = _bind_sim_listeners()
    except OSError as err:
        return fail_sdk("renode.sim-bind-failed", f"could not bind a sim socket: {err}")

    descriptor_path = os.path.join(bundle_dir, "sim-descriptor.json")
    descriptor_text = json.dumps(build_sim_descriptor(control_port, uart_port), indent=2) + "\n"
    try:
        with open(descriptor_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(descriptor_text)
    except OSError as err:
        _close_quietly(ctrl_sock, uart_sock)
        return fail_sdk(
            "renode.sim-descriptor-failed", f"could not write {descriptor_path}: {err}"
        )
    # Recorded in `data` so a JSON consumer is not left assuming the file
    # name and re-deriving the ports out of the descriptor it has to find
    # first.
    known["descriptor"] = descriptor_path
    known["controlPort"] = control_port
    known["uartPort"] = uart_port

    # Best-effort, exactly like the plain smoke: an unreadable or unexpected
    # ELF seeds no VTOR and leaves Renode's own guess alone.
    vtor = _best_effort_vtor(elf)
    resc_path = os.path.join(bundle_dir, ".sim-boot.resc")
    try:
        with open(resc_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(build_sim_resc_text(repl, elf, vtor))
    except OSError as err:
        _close_quietly(ctrl_sock, uart_sock)
        return fail_sdk("renode.sim-boot-script-failed", f"could not write {resc_path}: {err}")
    known["resc"] = resc_path

    log_path = _resolve_root_arg(log_arg, cwd, os.path.join(bundle_dir, "renode-sim.log"))
    # From here on, `data()`'s `logPath` is THIS sim-specific value, not
    # `base_log_path` -- mirrors the oracle's `report.log_path = ...`
    # overwrite, which happens at this exact point (after repl/elf/
    # descriptor/ports are already known, before the argv is built).
    known["logPath"] = log_path

    argv = build_sim_renode_argv(renode_bin, resc_path)
    known["renodeArgv"] = argv

    issues: list[Issue] = list(sdk_context_issues)
    text: list[str] = []
    # The descriptor's `framebuffers`/`peripherals` are empty and the UART
    # is silent while the per-SKU profile half of `tan-cli#77` is deferred.
    # Saying so is not optional -- see `sim_profile_deferred_message`'s own
    # docstring.
    deferred = sim_profile_deferred_message(sku)
    issues.append(_issue("renode.sim-profile-deferred", "warning", deferred))
    if expect is not None:
        # Say so rather than ignoring it: sim mode routes the console to a
        # socket, so there is no console text here for `--expect` to scan.
        issues.append(
            _issue(
                "renode.expect-ignored",
                "info",
                "renode: --expect is ignored in --sim-mode (the console is served "
                "on the UART socket, not scanned).",
            )
        )

    if not json_mode:
        # Printed DIRECTLY (not appended to `text`, which text mode prints
        # once at the very end): sim mode blocks for `--timeout` seconds
        # serving sockets, and an operator needs the descriptor + ports the
        # instant they exist. Verified stream-separated against the oracle.
        print(f"tan renode --sim-mode: {sku} booting {os.path.basename(elf)}")
        print(f"  descriptor : {descriptor_path}")
        print(f"  control    : tcp://127.0.0.1:{control_port}")
        print(
            f"  uart       : tcp://127.0.0.1:{uart_port} "
            "(silent — the ram_console bridge is deferred, tan-cli#77)"
        )
        # Text mode drops `issues`, so the warning has to be printed too or
        # a human sees only the reassuring four lines above.
        print(deferred)
        sys.stdout.flush()

    try:
        proc = _spawn_renode_sim(argv, log_path)
    except OSError as err:
        _close_quietly(ctrl_sock, uart_sock)
        return fail_sdk("renode.run-failed", f"failed to run renode: {err}")
    if proc.stdin is None or proc.stdout is None:
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass
        _close_quietly(ctrl_sock, uart_sock)
        return fail_sdk(
            "renode.run-failed",
            "failed to run renode: the child's stdio pipes were not created.",
        )

    monitor = RenodeMonitor(proc.stdin, proc.stdout)
    # Swallow the boot output FIRST and exclusively, THEN start accepting
    # clients -- otherwise a client command races the boot drain for the
    # monitor and captures boot text as its reply.
    try:
        monitor.drain_boot()
    except RuntimeError as err:
        _teardown_sim(monitor, proc)
        _close_quietly(ctrl_sock, uart_sock)
        return fail_sdk(
            "renode.sim-monitor-failed",
            f"Renode monitor never became ready: {err} (see {log_path}).",
        )

    threading.Thread(target=_serve_control, args=(ctrl_sock, monitor), daemon=True).start()
    threading.Thread(target=_serve_uart_silent, args=(uart_sock,), daemon=True).start()

    _announce_ready(json_mode, timeout)

    # Hold the sockets open until the timeout, failing if Renode dies first.
    early_exit: int | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            early_exit = code
            break
        time.sleep(0.25)

    _teardown_sim(monitor, proc)
    _close_quietly(ctrl_sock, uart_sock)

    if early_exit is not None:
        return fail_sdk(
            "renode.sim-exited-early",
            f"Renode exited early ({_exit_status_desc(early_exit)}); see {log_path}.",
        )
    # Checked LAST and independently of everything above, exactly like the
    # plain smoke's identical latch (issue #64): a Renode that boots, halts
    # the CPU on its first instruction fetch and then sits there until the
    # timeout looks -- to every other signal here -- like a healthy
    # session. Renode's stdout is consumed by the monitor in this mode, so
    # without the latch the halt would at best resurface as an `ERR` on
    # whichever client command came next, and `tan` would still exit 0. The
    # sim path is where a mis-seeded VTOR shows up this way.
    if monitor.cpu_halted():
        msg = (
            "renode: the CPU halted on its first instruction fetch — no firmware "
            f"code ever ran, even though the sim session came up (see {log_path})."
        )
        issues.append(_issue("renode.cpu-halted", "error", msg))
        if not json_mode:
            text.append(msg)
        return ExitCode.RUNTIME_FAILURE, data(), issues, text, sdk, project

    return ExitCode.SUCCESS, data(), issues, text, sdk, project


def renode(
    app_path: str = typer.Argument(
        ".",
        metavar="APP_PATH",
        help="Application source directory (default: `.`). Used to derive the default "
        "build root (`<app_path>/build`) and the Envelope project context.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="DIR",
        help="Override the build root holding `system-manifest.yaml` "
        "(default: `<app_path>/build`).",
    ),
    board: str = typer.Option(
        None,
        "--board",
        metavar="SKU",
        help="Override the SoM SKU used to pick the Renode platform descriptor "
        "(default: `hw_info.sku` from the manifest).",
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="Boot the Zephyr slice with this core_id. Needed when the manifest carries "
        "more than one Zephyr slice; optional when the project has exactly one.",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(  # noqa: ARG001 -- accepted for GlobalArgs parity, unused below
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path. Accepted for parity with every other command's "
        "global flag; `tan renode` itself derives `project.boardYaml` from "
        "`<APP_PATH>/board.yaml`'s own existence, matching the oracle.",
    ),
    image_bundle: str = typer.Option(
        None,
        "--image-bundle",
        metavar="DIR",
        help="Directory of pre-built per-slice artefacts. Accepted for parity with the "
        "dual-OS flow; unused by the single-Zephyr-slice smoke.",
    ),
    log: str = typer.Option(
        None,
        "--log",
        metavar="FILE",
        help="Tee the Renode UART/console output to this file "
        "(default: `<build_root>/renode.log`).",
    ),
    timeout: int = typer.Option(
        120,
        "--timeout",
        min=0,
        metavar="SECS",
        help="Wall-clock cap for the Renode run, in seconds.",
    ),
    expect: str = typer.Option(
        None,
        "--expect",
        metavar="STR",
        help="If set, stop early (exit 0) when this substring appears in any console "
        "line; exit 1 if the run ends without it.",
    ),
    sim_mode: bool = typer.Option(
        False,
        "--sim-mode",
        help="Studio hardware-simulator mode: boot --image-bundle's firmware headless "
        "and serve the control + UART sockets named by the bundle's "
        "sim-descriptor.json. Requires --image-bundle; --expect is ignored.",
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Boot the built system manifest's single Zephyr slice in headless Renode as a
    no-hardware smoke test (native)."""
    json_mode = output_format == "json"

    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "."

    sdk: SdkInfo | None = None
    project_obj = Project(root=None, board_yaml=None)
    try:
        exit_code, data, issues, text_lines, sdk, project_obj = _run(
            app_path_arg=app_path,
            build_root_arg=build_root,
            board_arg=board,
            core_arg=core,
            sdk_root_arg=sdk_root,
            project_arg=project,
            image_bundle_arg=image_bundle,
            log_arg=log,
            timeout=timeout,
            expect=expect,
            json_mode=json_mode,
            cwd=cwd,
            sim_mode_arg=sim_mode,
        )
    except Exception as err:  # noqa: BLE001 -- the whole point of this guard
        # Anything reaching here is a tan bug, reported as one with an
        # envelope: a raw traceback means an empty stdout and an extension
        # that renders nothing, with no error visible on either side.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = _data()
        issues = [Issue("renode.internal-failure", "error", f"{type(err).__name__}: {err}")]
        text_lines = ["renode: internal failure"]

    if json_mode:
        emit(Envelope("renode", project_obj, data, issues, exit_code, sdk=sdk))
    else:
        for line in text_lines:
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`/`--verbose`) on top of `--board-yaml`, already declared and read
# above; see `tan.core.global_flags`.
renode = accept_global_flags(renode)
