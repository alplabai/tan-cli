# SPDX-License-Identifier: Apache-2.0
"""Pure `tan renode --sim-mode` logic -- the studio hardware-simulator
contract. No IO: the `sim-descriptor.json` document, the generated sim boot
script, the headless sim argv, the control-socket line protocol (translate +
normalise + dispatch), and the Renode-monitor line classifier all live here as
IO-free functions. Sockets, the child process and the monitor plumbing are in
`tan.commands.renode_cmd`.

Port of `crates/tan-core/src/renode/sim.rs`, unit-tested there -- that module's
own `#[cfg(test)]` block is the oracle for every case below (confirmed both by
reading it and by driving the shipped `tan.exe` oracle live through the full
`--sim-mode` pipeline: pre-flight gates, the generated `sim-descriptor.json`
and `.sim-boot.resc`, a real control-socket round trip, and both the
`renode.cpu-halted` and `renode.sim-exited-early` post-spawn outcomes).

Despite `tan-cli#77`'s own framing ("no reference implementation, the retired
Python is gone"), a Rust port of exactly this contract already exists and is
built into the oracle -- `crates/tan-core/src/renode/sim.rs` +
`crates/tan-cli/src/commands/renode/{sim,monitor}.rs`, landed by
`5152fd4 feat(renode): implement the --sim-mode socket contract (#77) (#96)`.
This module is a faithful Python port of THAT (frozen, but readable and
already CI-verified) Rust code, not a fresh re-derivation from issue prose.

The contract itself is NOT re-derived from prose: the Rust module says it was
ported from the retired Python `west alp-renode --sim-mode`
(`scripts/west_commands/alp_renode.py`, deleted in `alp-sdk@df312cec` under
ADR-0020 Phase 4) whose own opt-in e2e test pinned the wire behaviour. The
four wire elements that issue prose alone would omit and the Python carried --
the `ERR <reason>` reply, the `ready (timeout ...` readiness marker, the
LOWERCASE `0xnn` hex reply formatting, and the Secure `SCB->VTOR` write -- are
all reproduced here (the marker is emitted by the CLI, being IO).

SCOPE (`tan-cli#77`, socket half): ports + descriptor + readiness marker + the
three-verb control protocol. DEFERRED to a follow-up on the same issue: the
`ram_console_buf` RAM-ring -> UART-socket streamer, the wired-UART console
path (Renode's own socket terminal), and the per-SKU `_SIM_BOARD_PROFILES`
that fill the descriptor's `framebuffers`/`peripherals` -- which are `[]`
here. The retired Python REFUSED a SKU with no profile; tan serves the socket
half instead, and says so out loud through [`sim_profile_deferred_message`]
-- never silently.

Historical contract: `alplabai/alp-sdk#674` (CLOSED 2026-07-13). Never cite a
bare `#674` from this repo -- read here it means `tan-cli#674`, which has
never existed and 404s. Always carry the owning repo. (The Rust source this
was ported from makes the same point but never names its OWN repo either --
`crates/tan-core/src/renode/mod.rs:14` says only "issue #674", which is
exactly the missing-prefix shape that let three alp-sdk workflows drift into
linking a nonexistent `tan-cli#674`. `crates/` is frozen and out of scope to
edit here; flagged instead of fixed.)
"""
from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

#: SKUs whose retired-Python `_SIM_BOARD_PROFILES` console was a WIRED
#: hardware UART (`{"kind": "uart", ...}`, served by Renode's own socket
#: terminal) rather than the `ram_console_buf` RAM ring. For these the UART
#: socket is silent here for a SECOND reason -- the wired-console path is
#: deferred too -- so the warning says so instead of letting an operator
#: assume the firmware simply printed nothing.
WIRED_CONSOLE_SKUS: tuple[str, ...] = ("E1M-AEN801",)

#: The largest value `parse_int_auto` accepts -- mirrors Rust `u64::MAX`. A
#: token whose value doesn't fit is `None`, matching `u64::from_str_radix`'s
#: own failure rather than silently widening to Python's arbitrary-precision
#: ints.
_U64_MAX = (1 << 64) - 1


class SimError(Exception):
    """Why a control line's translation or dispatch failed. `str(err)` is
    EXACTLY the reason text `dispatch_control_line` folds into its single
    `ERR <reason>` reply -- collapses the four Rust `SimError` variants
    (`MalformedReadBytes`/`MalformedWriteBytes`/`WriteBytesNoData`/
    `ShortRead`) into one exception class, since nothing downstream branches
    on which variant fired, only the rendered text."""


def _rust_debug_str(text: str) -> str:
    """Rust's `{:?}` for a `&str`: double-quoted, with `\\` and `"` escaped.
    Duplicated (not imported) from `tan.commands.renode_cmd`'s identical
    helper -- this module is pure/IO-free and must not depend on the command
    file that depends on it."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rust_debug_list(items: list[str]) -> str:
    """Rust's `Vec<String>` `{:?}` spelling -- double-quoted, comma-separated."""
    return "[" + ", ".join(_rust_debug_str(i) for i in items) + "]"


# ── sim-descriptor.json (studio SimDescriptorSchema) ────────────────────────


def build_sim_descriptor(control_port: int, uart_port: int) -> dict[str, Any]:
    """Assemble the `sim-descriptor.json` document -- studio's
    `@alp/sim-protocol` `SimDescriptorSchema`. EXACTLY four keys, in this
    order (a plain `dict` preserves insertion order, and `json.dumps` never
    reorders it, mirroring `serde_json`'s `preserve_order`), and both socket
    values are `tcp://127.0.0.1:<port>` URIs.

    `framebuffers`/`peripherals` are empty: they come from the per-SKU sim
    profiles, which are the deferred half of `tan-cli#77`. They are
    present-and-empty rather than absent because the schema requires all
    four keys -- a studio client that reads the descriptor must find the
    arrays it iterates. Empty is NOT reported as success: every sim run
    carries [`sim_profile_deferred_message`] as a warning issue.
    """
    return {
        "control_socket": f"tcp://127.0.0.1:{control_port}",
        "uart_socket": f"tcp://127.0.0.1:{uart_port}",
        "framebuffers": [],
        "peripherals": [],
    }


def sim_profile_deferred_message(sku: str) -> str:
    """The warning every `--sim-mode` run carries while the per-SKU profile
    half of `tan-cli#77` is deferred: it states plainly that the
    descriptor's `framebuffers`/`peripherals` are empty and that the UART
    socket is silent.

    This exists because the alternative is a descriptor that LOOKS
    successful. The retired Python refused outright for any SKU with no
    profile (`E1M-AEN801` and `E1M-V2N101` being the only two wired); tan
    keeps exit 0 because the socket half genuinely works -- a client that
    already knows a monitor node path can drive `sysbus ReadBytes` /
    `WriteBytes` and any verbatim monitor line -- but a caller must not have
    to infer the gap from an empty array.
    """
    msg = (
        f"renode: no --sim-mode board profile for {sku} yet, so "
        "sim-descriptor.json's `framebuffers` and `peripherals` are BOTH "
        "empty — studio discovers no camera, display or sensor to "
        "inject into — and the UART socket streams NOTHING (it accepts "
        "and holds the connection, but the console stays silent). The "
        "control socket works: `sysbus ReadBytes` / `sysbus WriteBytes` and "
        "any verbatim monitor line are served, so a client that already "
        "knows a node path can still drive the machine. The per-SKU "
        "profiles are the deferred half of tan-cli#77."
    )
    if sku in WIRED_CONSOLE_SKUS:
        msg += (
            f" {sku}'s console was a WIRED hardware UART served by Renode's "
            "own socket terminal, and that path is deferred as well — "
            "a second, independent reason this run's UART socket is silent "
            "rather than the firmware being quiet."
        )
    return msg


def ready_marker(timeout: int) -> str:
    """The readiness marker line. The substring `ready (timeout` is the
    CONSUMER's poll token -- the retired Python's own e2e polled the process
    output for it before reading `sim-descriptor.json`, so a reword strands
    every consumer. It lives here, with the rest of the wire decisions, so
    it is pinned by a test; `tan.commands.renode_cmd` only chooses which
    stream to print it on."""
    return f"tan renode --sim-mode: ready (timeout {timeout}s)."


# ── generated boot script + argv ─────────────────────────────────────────────


def build_sim_resc_text(repl: str, elf: str, vtor: int | None) -> str:
    """Generate the sim boot script: create the machine, load the platform,
    load the ELF, seed the Secure VTOR, start.

    `vtor` (the image's vector-table address) is written to the Secure
    `SCB->VTOR` (0xE000ED08) AFTER `LoadELF` and BEFORE `start`. On ARMv8-M
    with TrustZone (Renode >= 1.16) `LoadELF` does NOT seed the Secure VTOR,
    so every exception fetches its handler from address 0 and the core
    HardFault-storms; on real silicon the boot ROM / secure world sets it.
    Harmless on pre-TrustZone Renode, where 0xE000ED08 is just VTOR. `None`
    writes nothing -- an image whose vector table could not be located
    leaves Renode's own guess alone rather than being handed a wrong
    address.

    `repl`/`elf` are plain path strings, not `pathlib.Path` -- matches the
    rest of this command's manifest-consuming surface (`tan.core.renode_plan`),
    which keeps a caller's own path style (native separators, unconverted)
    rather than re-rendering it.

    This is a DIFFERENT mechanism from the plain smoke's `cpu
    VectorTableOffset $vtor` monitor variable
    (`tan.core.renode_plan.build_renode_argv`): the sim path owns its
    generated script, so it writes the register directly and needs no
    cooperation from an SDK-side `.resc`. The value is formatted like
    Python's own `hex()` -- lowercase, unpadded -- which is what `f"{v:#x}"`
    already does.

    The machine name is `v2n_sim` for every SKU: the retired Python's
    `machine` parameter existed but `run_sim` never passed it, so this is
    the only name the contract has ever used.
    """
    vtor_line = f"sysbus WriteDoubleWord 0xE000ED08 {vtor:#x}\n" if vtor is not None else ""
    return (
        'mach create "v2n_sim"\n'
        f"machine LoadPlatformDescription @{repl}\n"
        f"sysbus LoadELF @{elf}\n"
        f"{vtor_line}"
        "start\n"
    )


def build_sim_renode_argv(renode_bin: str, resc: str) -> list[str]:
    """Headless Renode argv for `--sim-mode`. `--console` keeps the monitor
    on the child's stdin/stdout so the control bridge can drive it; the boot
    script routes nothing else to stdout, so it carries only monitor
    traffic. Flag order is a machine contract -- VERBATIM from the retired
    Python."""
    return [renode_bin, "--disable-xwt", "--plain", "--console", "-e", f"i @{resc}"]


# ── control-line translation + ReadBytes normalisation ──────────────────────


def parse_int_auto(tok: str) -> int | None:
    """Parse an integer token the way Python's own `int(tok, 0)` does:
    `0x`/`0X` hex, `0b`/`0B` binary, `0o`/`0O` octal, else decimal. `None` on
    anything else, including a value too large for a `u64` (mirrors
    `u64::from_str_radix`'s own failure rather than silently widening to
    Python's arbitrary-precision ints).

    DIVERGENCES from the retired Python, all in harmless directions and all
    deliberate (mirrors the Rust port this function itself is ported from):
      - a SIGNED token (`-1`) is REJECTED rather than accepted-and-masked.
        Python's `int("-1", 0) & 0xFF` is 255 (infinite-precision two's
        complement); a negative address or data byte is nonsense on this
        wire, so it becomes an `ERR malformed ...` reply instead of a
        silently-reinterpreted write.
      - a LEADING-ZERO decimal (`010`) is ACCEPTED as 10, where
        `int("010", 0)` raises (Python forbids it, to stop C programmers
        reading it as octal -- octal needs the `0o` prefix). Accepting it
        can only widen what a client may send.
      - an UNDERSCORE-grouped token (`1_0`) is REJECTED, where
        `int("1_0", 0)` is 10 (PEP 515 digit separators). Losing it can only
        narrow what a client may send, and no studio client has ever
        emitted one -- the wire carries `0x...` tokens machine-generated
        from integers.
    """
    if tok[:2] in ("0x", "0X"):
        radix, digits = 16, tok[2:]
    elif tok[:2] in ("0b", "0B"):
        radix, digits = 2, tok[2:]
    elif tok[:2] in ("0o", "0O"):
        radix, digits = 8, tok[2:]
    else:
        radix, digits = 10, tok
    if not digits or not digits.isascii() or not digits.isalnum():
        return None
    try:
        value = int(digits, radix)
    except ValueError:
        return None
    return value if value <= _U64_MAX else None


def translate_control_command(line: str) -> tuple[int | None, list[str]]:
    """Map one studio control line to `(read_count, renode_commands)` -- the
    whole verb vocabulary, three arms:

    1. `sysbus ReadBytes <base> <count>` -> forwarded verbatim; `read_count`
       is the requested byte count, and the reply needs byte-token
       normalisation ([`normalize_readbytes_output`]).
    2. `sysbus WriteBytes <base> <hex...>` -> EXPANDED to one `sysbus
       WriteByte <base+i> <byte>` per byte, because Renode's own
       `WriteBytes` takes `(bytes, addr)` -- the reverse of studio's
       `<base> <hex...>` order. Byte and address are formatted
       lowercase-unpadded, like Python's `hex()`. `read_count` is `None`.
    3. anything else (a peripheral `inject` template, a property get/set)
       -> forwarded VERBATIM; `read_count` is `None` and the reply is its
       first non-empty output line, or `ok`.

    The control socket is deliberately NOT Renode's raw telnet monitor:
    studio's wire vocabulary does not match Renode's monitor API 1:1, so
    the bridge translates and normalises to exactly one reply line.

    Raises [`SimError`] on a malformed base/count/data token, or a
    `WriteBytes` with no data bytes.
    """
    parts = line.split()
    if len(parts) >= 4 and parts[0] == "sysbus" and parts[1] == "ReadBytes":
        base_tok, count_tok = parts[2], parts[3]
        if parse_int_auto(base_tok) is None:
            raise SimError(
                f"malformed ReadBytes {_rust_debug_str(line)}: invalid integer "
                f"token {_rust_debug_str(base_tok)}"
            )
        count = parse_int_auto(count_tok)
        if count is None:
            raise SimError(
                f"malformed ReadBytes {_rust_debug_str(line)}: invalid integer "
                f"token {_rust_debug_str(count_tok)}"
            )
        # Forward the ORIGINAL token text, not a reformatted value: the
        # retired Python did, and Renode is the one that parses it.
        return count, [f"sysbus ReadBytes {base_tok} {count_tok}"]

    if len(parts) >= 3 and parts[0] == "sysbus" and parts[1] == "WriteBytes":
        base = parse_int_auto(parts[2])
        if base is None:
            raise SimError(
                f"malformed WriteBytes {_rust_debug_str(line)}: invalid integer "
                f"token {_rust_debug_str(parts[2])}"
            )
        data: list[int] = []
        for tok in parts[3:]:
            value = parse_int_auto(tok)
            if value is None:
                raise SimError(
                    f"malformed WriteBytes {_rust_debug_str(line)}: invalid "
                    f"integer token {_rust_debug_str(tok)}"
                )
            # `& 0xFF` verbatim from the retired Python: an oversized token
            # is masked, not rejected.
            data.append(value & 0xFF)
        if not data:
            raise SimError(f"WriteBytes with no data bytes: {_rust_debug_str(line)}")
        cmds: list[str] = []
        for i, byte in enumerate(data):
            addr = base + i
            if addr > _U64_MAX:
                # NOT the "invalid integer token" phrasing: every token
                # parsed fine here -- it is the arithmetic that failed.
                raise SimError(
                    f"malformed WriteBytes {_rust_debug_str(line)}: base "
                    f"{base:#x} + byte offset {i} overflows a 64-bit address"
                )
            cmds.append(f"sysbus WriteByte {addr:#x} {byte:#x}")
        return None, cmds

    return None, [line]


def _hex_tokens_low_bytes(s: str) -> list[int]:
    """Every `0[xX]<hexdigits>` token in `s`, masked to its low byte.

    Masking is done by taking the token's LAST TWO hex digits rather than
    parsing the whole token: that is exactly `value & 0xFF`, and it cannot
    overflow on an arbitrarily long token the way a fixed-width parse
    would.
    """
    out: list[int] = []
    i, n = 0, len(s)
    hexdigits = "0123456789abcdefABCDEF"
    while i + 2 < n:
        if s[i] == "0" and s[i + 1] in ("x", "X") and s[i + 2] in hexdigits:
            start = i + 2
            j = start
            while j < n and s[j] in hexdigits:
                j += 1
            tail = s[max(j - 2, start) : j]
            out.append(int(tail, 16))
            i = j
        else:
            i += 1
    return out


def normalize_readbytes_output(renode_out: str, count: int) -> str:
    """Turn Renode `ReadBytes` output into `count` space-separated LOWERCASE
    `0xnn` tokens on ONE line -- the studio control-socket reply contract.
    Renode prints a bracketed, comma-separated, UPPER-case list spread over
    lines (`[\\n0xDE, 0xAD, \\n]`); studio wants `0xde 0xad`.

    Only the bracketed body is scanned. Scoping to the brackets is what
    keeps an echoed command line from leaking in as a phantom data byte --
    the echo of `sysbus ReadBytes 0x20000000 4` carries `0x20000000`, which
    masks to `0x00`.

    Raises [`SimError`] when fewer than `count` byte tokens were seen: a
    short read is a real error, never silently padded.
    """
    lo, hi = renode_out.find("["), renode_out.rfind("]")
    body = renode_out[lo + 1 : hi] if lo != -1 and hi != -1 and lo < hi else renode_out
    byte_values = _hex_tokens_low_bytes(body)
    if len(byte_values) < count:
        raise SimError(
            f"ReadBytes returned {len(byte_values)} bytes, expected {count}: "
            f"{_rust_debug_str(renode_out)}"
        )
    return " ".join(f"{b:#04x}" for b in byte_values[:count])


# ── the full control-socket dispatch ─────────────────────────────────────────


def _single_line(s: str) -> str:
    """Collapse CR/LF to spaces so a reply can never span lines."""
    return s.replace("\r", " ").replace("\n", " ").strip()


def dispatch_control_line(line: str, run: Callable[[str], str]) -> str:
    """Run ONE studio control line through the bridge and return its single
    reply line (no trailing newline). `run` executes one Renode monitor
    command and returns its captured output, or raises with the failure
    reason as its message.

    Never fails: a malformed line or a monitor error becomes `ERR <reason>`
    so the connection survives and one request -> one reply always holds.
    The reply is flattened to a single line for the same reason -- a
    multi-line reply would desynchronise a line-oriented client for the
    rest of the session.
    """
    try:
        reply = _dispatch_inner(line.strip(), run)
    except Exception as err:  # noqa: BLE001 -- documented: this must never raise
        reply = f"ERR {err}"
    return _single_line(reply)


def _dispatch_inner(line: str, run: Callable[[str], str]) -> str:
    count, cmds = translate_control_command(line)
    if count is not None:
        out = run(cmds[0])
        return normalize_readbytes_output(out, count)
    out = ""
    for cmd in cmds:
        out = run(cmd)
    # A property SET (an inject) prints nothing -> `ok`. A property GET
    # prints its value -> echo the first non-empty line back so callers can
    # read state.
    for candidate in out.splitlines():
        candidate = candidate.strip()
        if candidate:
            return candidate
    return "ok"


# ── Renode monitor line classification ───────────────────────────────────────


class MonitorLine(Enum):
    """What one Renode monitor stdout line means while awaiting a
    sentinel."""

    #: The bare sentinel -- this command's output is complete.
    DONE = "done"
    #: A monitor-side `[ERROR]`; must surface rather than be masked as `ok`.
    ERROR = "error"
    #: Noise to drop: the echoed sentinel-input, `[INFO]`/`[WARNING]` logs,
    #: and the monitor's echo of the command we wrote.
    IGNORE = "ignore"
    #: Real command output.
    OUTPUT = "output"


def classify_monitor_line(line: str, sentinel: str, cmd: str) -> MonitorLine:
    """Classify one monitor line. Ordering is the contract and is
    load-bearing:

    The monitor echoes each line we WRITE and then prints its output, so
    the `echo "<sentinel>"` we append appears TWICE -- once as the echoed
    input (`echo "__ALP_SIM_DONE_1__"`) and once as echo's own output (the
    bare sentinel). Only the bare form, an EXACT match, terminates the
    command; the echoed-input form is dropped so its token cannot pollute
    the captured output. `[ERROR]` is checked BEFORE `[INFO]`/`[WARNING]`,
    and is never dropped -- a monitor-side fault (a `WriteByte` to a
    faulting address) must surface instead of being reported as `ok`.
    """
    s = line.strip()
    if s == sentinel:
        return MonitorLine.DONE
    if sentinel in s:
        return MonitorLine.IGNORE
    if "[ERROR]" in line:
        return MonitorLine.ERROR
    if "[INFO]" in line or "[WARNING]" in line:
        return MonitorLine.IGNORE
    if s == cmd or s.endswith(cmd):
        return MonitorLine.IGNORE
    return MonitorLine.OUTPUT
