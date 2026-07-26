// SPDX-License-Identifier: Apache-2.0
//! Pure `tan renode --sim-mode` logic — the studio hardware-simulator contract.
//! No IO: the `sim-descriptor.json` document, the generated sim boot script, the
//! headless sim argv, the control-socket line protocol (translate + normalise +
//! dispatch), and the Renode-monitor line classifier all live here as IO-free
//! functions. Sockets, the child process and the monitor plumbing are in
//! `tan-cli`'s `commands/renode/`.
//!
//! The contract is NOT re-derived from prose: it is ported from the retired
//! Python `west alp-renode --sim-mode` (`scripts/west_commands/alp_renode.py`,
//! deleted in `alp-sdk@df312cec` under ADR-0020 Phase 4) whose own opt-in e2e
//! test pinned the wire behaviour. The four wire elements the issue prose omits
//! and the Python carries — the `ERR <reason>` reply, the `ready (timeout …`
//! readiness marker, the LOWERCASE `0xnn` hex reply formatting, and the Secure
//! `SCB->VTOR` write — are all reproduced here (the marker is emitted by the
//! CLI, being IO).
//!
//! SCOPE (`tan-cli#77`, socket half): ports + descriptor + readiness marker +
//! the three-verb control protocol. DEFERRED to a follow-up on the same issue:
//! the `ram_console_buf` RAM-ring → UART-socket streamer (`RamConsoleStreamer`),
//! the wired-UART console path (`emulation CreateServerSocketTerminal` +
//! `connector Connect`), and the per-SKU `_SIM_BOARD_PROFILES` that fill the
//! descriptor's `framebuffers`/`peripherals` — which are `[]` here. The Python
//! REFUSED a SKU with no profile (`sim_profile_for_sku` raised); tan serves the
//! socket half instead, and says so out loud through
//! [`sim_profile_deferred_message`] — never silently.
//!
//! Historical contract: `alplabai/alp-sdk#674` (CLOSED 2026-07-13). Never cite a
//! bare `#674` from this repo — read here it means `tan-cli#674`, which has
//! never existed and 404s. Always carry the owning repo.

use std::path::Path;

use serde_json::{Value, json};

/// Why a control line could not be served. Every variant is rendered into the
/// single-line `ERR <reason>` reply — the connection survives, so one
/// request → one reply still holds (mirrors the Python bridge's
/// `except … : reply = f"ERR {e}"`).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SimError {
    /// A `sysbus ReadBytes` line whose base or count is not an integer.
    #[error("malformed ReadBytes {line:?}: {reason}")]
    MalformedReadBytes {
        /// The offending line.
        line: String,
        /// Which token failed to parse.
        reason: String,
    },
    /// A `sysbus WriteBytes` line whose base or a data byte is not an integer.
    #[error("malformed WriteBytes {line:?}: {reason}")]
    MalformedWriteBytes {
        /// The offending line.
        line: String,
        /// Which token failed to parse.
        reason: String,
    },
    /// `sysbus WriteBytes <base>` with no data bytes after the base.
    #[error("WriteBytes with no data bytes: {line:?}")]
    WriteBytesNoData {
        /// The offending line.
        line: String,
    },
    /// Renode returned fewer bytes than the client asked for. A short read is a
    /// real error — never silently padded.
    #[error("ReadBytes returned {got} bytes, expected {want}: {out:?}")]
    ShortRead {
        /// How many byte tokens were actually parsed.
        got: usize,
        /// How many the client asked for.
        want: usize,
        /// The raw Renode output, for the diagnostic.
        out: String,
    },
}

/// What kind of reply a translated control line needs.
#[derive(Debug, PartialEq, Eq)]
pub enum ControlKind {
    /// The reply is Renode's `ReadBytes` list, to be normalised to `count`
    /// space-separated lowercase `0xnn` tokens.
    ReadBytes {
        /// How many bytes the client asked for.
        count: usize,
    },
    /// The reply is the first non-empty output line, or `ok` when silent.
    Plain,
}

/// Assemble the `sim-descriptor.json` document — studio's `@alp/sim-protocol`
/// `SimDescriptorSchema`. EXACTLY four keys, in this order (`serde_json` is
/// built with `preserve_order`, so the emitted object keeps it), and both socket
/// values are `tcp://127.0.0.1:<port>` URIs.
///
/// `framebuffers`/`peripherals` are empty: they come from the per-SKU sim
/// profiles, which are the deferred half of `tan-cli#77`. They are present-and-
/// empty rather than absent because the schema requires all four keys — a
/// studio client that reads the descriptor must find the arrays it iterates.
/// Empty is NOT reported as success: every sim run carries
/// [`sim_profile_deferred_message`] as a warning issue.
pub fn build_sim_descriptor(control_port: u16, uart_port: u16) -> Value {
    json!({
        "control_socket": format!("tcp://127.0.0.1:{control_port}"),
        "uart_socket": format!("tcp://127.0.0.1:{uart_port}"),
        "framebuffers": Value::Array(Vec::new()),
        "peripherals": Value::Array(Vec::new()),
    })
}

/// SKUs whose Python `_SIM_BOARD_PROFILES` console was a WIRED hardware UART
/// (`{"kind": "uart", …}`, served by Renode's own socket terminal) rather than
/// the `ram_console_buf` RAM ring. For these the UART socket is silent here for
/// a SECOND reason — the wired-console path is deferred too — so the warning
/// says so instead of letting an operator assume the firmware simply printed
/// nothing.
const WIRED_CONSOLE_SKUS: [&str; 1] = ["E1M-AEN801"];

/// The warning every `--sim-mode` run carries while the per-SKU profile half of
/// `tan-cli#77` is deferred: it states plainly that the descriptor's
/// `framebuffers`/`peripherals` are empty and that the UART socket is silent.
///
/// This exists because the alternative is a descriptor that LOOKS successful.
/// The Python refused outright (`sim_profile_for_sku` raised `AlpRenodeError`
/// for any SKU with no profile, `E1M-AEN801` and `E1M-V2N101` being the only two
/// wired); tan keeps exit 0 because the socket half genuinely works — a client
/// that already knows a monitor node path can drive `sysbus ReadBytes` /
/// `WriteBytes` and any verbatim monitor line — but a caller must not have to
/// infer the gap from an empty array. A studio client doing
/// `peripherals.find(p => p.id === "camera")` gets nothing, and this is what
/// tells it why.
pub fn sim_profile_deferred_message(sku: &str) -> String {
    let mut msg = format!(
        "renode: no --sim-mode board profile for {sku} yet, so sim-descriptor.json's \
         `framebuffers` and `peripherals` are BOTH empty — studio discovers no camera, \
         display or sensor to inject into — and the UART socket streams NOTHING (it \
         accepts and holds the connection, but the console stays silent). The control \
         socket works: `sysbus ReadBytes` / `sysbus WriteBytes` and any verbatim monitor \
         line are served, so a client that already knows a node path can still drive the \
         machine. The per-SKU profiles are the deferred half of tan-cli#77."
    );
    if WIRED_CONSOLE_SKUS.contains(&sku) {
        msg.push_str(&format!(
            " {sku}'s console was a WIRED hardware UART served by Renode's own socket \
             terminal, and that path is deferred as well — a second, independent reason \
             this run's UART socket is silent rather than the firmware being quiet."
        ));
    }
    msg
}

/// The readiness marker line. The substring `ready (timeout` is the CONSUMER's
/// poll token — the retired Python's own e2e polled the process output for it
/// before reading `sim-descriptor.json`, so a reword strands every consumer.
/// It lives here, with the rest of the wire decisions, so it is pinned by a
/// test; `tan-cli` only chooses which stream to print it on.
pub fn ready_marker(timeout: u64) -> String {
    format!("tan renode --sim-mode: ready (timeout {timeout}s).")
}

/// Generate the sim boot script: create the machine, load the platform, load the
/// ELF, seed the Secure VTOR, start.
///
/// `vtor` (the image's vector-table address) is written to the Secure
/// `SCB->VTOR` (0xE000ED08) AFTER `LoadELF` and BEFORE `start`. On ARMv8-M with
/// TrustZone (Renode >= 1.16) `LoadELF` does NOT seed the Secure VTOR, so every
/// exception fetches its handler from address 0 and the core HardFault-storms;
/// on real silicon the boot ROM / secure world sets it. Harmless on
/// pre-TrustZone Renode, where 0xE000ED08 is just VTOR. `None` writes nothing —
/// an image whose vector table could not be located leaves Renode's own guess
/// alone rather than being handed a wrong address.
///
/// WHERE `vtor` COMES FROM, and how that differs from the Python: the caller
/// passes [`super::elf_vector_table_base`], which returns the **`p_paddr` of the
/// `PT_LOAD` segment containing `e_entry`**, validated by checking that the
/// segment's second word (the reset vector) equals `e_entry` — the same reader
/// the plain smoke uses. The Python instead looked up the `_vector_table` SYMBOL
/// (`elf_symbol(elf, "_vector_table")`). The two coincide on the V2N101 M33
/// image (`0x08003000`, `vaddr == paddr`, table first in segment), so the target
/// SKU is unaffected, but the FAILURE SHAPES differ: the Python raised
/// `AlpRenodeError` on a non-ELF or truncated image (and, being outside its own
/// try/except, it surfaced as a traceback), whereas every `None` case here is
/// SILENT — no VTOR line is written and the boot script is otherwise identical.
/// `None` is returned for: a non-ELF / not-32-bit / not-little-endian / <52-byte
/// file, a truncated program-header table, `e_phentsize < 32`, no `PT_LOAD`
/// covering `e_entry`, and a covering segment whose second word is not the entry
/// point (a table that is not first in its segment lands here). An image linked
/// so `p_paddr != p_vaddr` yields the LOAD (physical) address, which is what
/// Renode's `sysbus` sees — deliberate, and the reason the plain path reads the
/// same field.
///
/// This is a DIFFERENT mechanism from the plain smoke's `cpu VectorTableOffset
/// $vtor` monitor variable ([`super::build_renode_argv`]): the sim path owns its
/// generated script, so it writes the register directly and needs no cooperation
/// from an SDK-side `.resc`. The value is formatted like Python's `hex()` —
/// lowercase, unpadded.
///
/// The machine name is `v2n_sim` for every SKU: the Python's `machine`
/// parameter existed but `run_sim` never passed it, so this is the only name the
/// contract has ever used.
pub fn build_sim_resc_text(repl: &Path, elf: &Path, vtor: Option<u32>) -> String {
    let vtor_line = match vtor {
        Some(v) => format!("sysbus WriteDoubleWord 0xE000ED08 {v:#x}\n"),
        None => String::new(),
    };
    format!(
        "mach create \"v2n_sim\"\n\
         machine LoadPlatformDescription @{}\n\
         sysbus LoadELF @{}\n\
         {vtor_line}\
         start\n",
        repl.display(),
        elf.display(),
    )
}

/// Headless Renode argv for `--sim-mode`. `--console` keeps the monitor on the
/// child's stdin/stdout so the control bridge can drive it; the boot script
/// routes nothing else to stdout, so it carries only monitor traffic. Flag order
/// is a machine contract — VERBATIM from the Python.
pub fn build_sim_renode_argv(renode_bin: &str, resc: &Path) -> Vec<String> {
    vec![
        renode_bin.to_string(),
        "--disable-xwt".to_string(),
        "--plain".to_string(),
        "--console".to_string(),
        "-e".to_string(),
        format!("i @{}", resc.display()),
    ]
}

/// Parse an integer token the way Python's `int(tok, 0)` does: `0x`/`0X` hex,
/// `0b`/`0B` binary, `0o`/`0O` octal, else decimal. `None` on anything else.
///
/// DIVERGENCES from the Python, all in harmless directions and all deliberate:
///   - a SIGNED token (`-1`) is REJECTED rather than accepted-and-masked.
///     Python's `int("-1", 0) & 0xFF` is 255 (infinite-precision two's
///     complement); a negative address or data byte is nonsense on this wire, so
///     it becomes an `ERR malformed …` reply instead of a silently-reinterpreted
///     write.
///   - a LEADING-ZERO decimal (`010`) is ACCEPTED as 10, where `int("010", 0)`
///     raises (Python forbids it, to stop C programmers reading it as octal —
///     octal needs the `0o` prefix). Accepting it can only widen what a client
///     may send; nothing that worked before changes meaning, since `010` had no
///     meaning before.
///   - an UNDERSCORE-grouped token (`1_0`) is REJECTED, where `int("1_0", 0)` is
///     10 (PEP 515 digit separators). Losing it can only narrow what a client may
///     send, and no studio client has ever emitted one — the wire carries `0x…`
///     tokens machine-generated from integers.
fn parse_int_auto(tok: &str) -> Option<u64> {
    let (radix, digits) = if let Some(d) = tok.strip_prefix("0x").or_else(|| tok.strip_prefix("0X"))
    {
        (16, d)
    } else if let Some(d) = tok.strip_prefix("0b").or_else(|| tok.strip_prefix("0B")) {
        (2, d)
    } else if let Some(d) = tok.strip_prefix("0o").or_else(|| tok.strip_prefix("0O")) {
        (8, d)
    } else {
        (10, tok)
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return None;
    }
    u64::from_str_radix(digits, radix).ok()
}

/// Map one studio control line to `(kind, renode_commands)` — the whole verb
/// vocabulary, three arms:
///
/// 1. `sysbus ReadBytes <base> <count>` → forwarded verbatim; the reply needs
///    byte-token normalisation ([`normalize_readbytes_output`]).
/// 2. `sysbus WriteBytes <base> <hex…>` → EXPANDED to one `sysbus WriteByte
///    <base+i> <byte>` per byte, because Renode's own `WriteBytes` takes
///    `(bytes, addr)` — the reverse of studio's `<base> <hex…>` order. Byte and
///    address are formatted lowercase-unpadded, like Python's `hex()`.
/// 3. anything else (a peripheral `inject` template, a property get/set) →
///    forwarded VERBATIM; the reply is its first non-empty output line, or `ok`.
///
/// The control socket is deliberately NOT Renode's raw telnet monitor: studio's
/// wire vocabulary does not match Renode's monitor API 1:1, so the bridge
/// translates and normalises to exactly one reply line.
pub fn translate_control_command(line: &str) -> Result<(ControlKind, Vec<String>), SimError> {
    let parts: Vec<&str> = line.split_whitespace().collect();
    if parts.len() >= 4 && parts[0] == "sysbus" && parts[1] == "ReadBytes" {
        let (base, count) = (parts[2], parts[3]);
        let bad = |tok: &str| SimError::MalformedReadBytes {
            line: line.to_string(),
            reason: format!("invalid integer token {tok:?}"),
        };
        parse_int_auto(base).ok_or_else(|| bad(base))?;
        let count_v = parse_int_auto(count).ok_or_else(|| bad(count))?;
        // Forward the ORIGINAL token text, not a reformatted value: the Python
        // did, and Renode is the one that parses it.
        return Ok((
            ControlKind::ReadBytes {
                count: count_v as usize,
            },
            vec![format!("sysbus ReadBytes {base} {count}")],
        ));
    }
    if parts.len() >= 3 && parts[0] == "sysbus" && parts[1] == "WriteBytes" {
        let bad = |tok: &str| SimError::MalformedWriteBytes {
            line: line.to_string(),
            reason: format!("invalid integer token {tok:?}"),
        };
        let base = parse_int_auto(parts[2]).ok_or_else(|| bad(parts[2]))?;
        let mut data: Vec<u8> = Vec::new();
        for tok in &parts[3..] {
            // `& 0xFF` verbatim from the Python: an oversized token is masked,
            // not rejected.
            data.push((parse_int_auto(tok).ok_or_else(|| bad(tok))? & 0xFF) as u8);
        }
        if data.is_empty() {
            return Err(SimError::WriteBytesNoData {
                line: line.to_string(),
            });
        }
        let mut cmds = Vec::with_capacity(data.len());
        for (i, b) in data.iter().enumerate() {
            // NOT `bad()`: that renders as `invalid integer token "…"`, and
            // every token here parsed fine — it is the arithmetic that failed.
            let addr = base
                .checked_add(i as u64)
                .ok_or_else(|| SimError::MalformedWriteBytes {
                    line: line.to_string(),
                    reason: format!("base {base:#x} + byte offset {i} overflows a 64-bit address"),
                })?;
            cmds.push(format!("sysbus WriteByte {addr:#x} {b:#x}"));
        }
        return Ok((ControlKind::Plain, cmds));
    }
    Ok((ControlKind::Plain, vec![line.to_string()]))
}

/// Every `0[xX]<hexdigits>` token in `s`, masked to its low byte.
///
/// Masking is done by taking the token's LAST TWO hex digits rather than
/// parsing the whole token: `value & 0xFF` is exactly that, and it cannot
/// overflow on an arbitrarily long token the way a fixed-width parse would
/// (Python's `int()` is arbitrary-precision, so a width limit here would be a
/// silent divergence).
fn hex_tokens_low_bytes(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + 2 < b.len() {
        if b[i] == b'0' && (b[i + 1] == b'x' || b[i + 1] == b'X') && b[i + 2].is_ascii_hexdigit() {
            let start = i + 2;
            let mut j = start;
            while j < b.len() && b[j].is_ascii_hexdigit() {
                j += 1;
            }
            let tail = &s[j.saturating_sub(2).max(start)..j];
            out.push(u8::from_str_radix(tail, 16).unwrap_or(0));
            i = j;
        } else {
            i += 1;
        }
    }
    out
}

/// Turn Renode `ReadBytes` output into `count` space-separated LOWERCASE `0xnn`
/// tokens on ONE line — the studio control-socket reply contract. Renode prints
/// a bracketed, comma-separated, UPPER-case list spread over lines
/// (`[\n0xDE, 0xAD, \n]`); studio wants `0xde 0xad`.
///
/// Only the bracketed body is scanned. Scoping to the brackets is what keeps an
/// echoed command line from leaking in as a phantom data byte — the echo of
/// `sysbus ReadBytes 0x20000000 4` carries `0x20000000`, which masks to `0x00`.
///
/// Errors [`SimError::ShortRead`] when fewer than `count` byte tokens were
/// seen: a short read is a real error, never silently padded.
pub fn normalize_readbytes_output(renode_out: &str, count: usize) -> Result<String, SimError> {
    let body = match (renode_out.find('['), renode_out.rfind(']')) {
        (Some(lo), Some(hi)) if lo < hi => &renode_out[lo + 1..hi],
        _ => renode_out,
    };
    let bytes = hex_tokens_low_bytes(body);
    if bytes.len() < count {
        return Err(SimError::ShortRead {
            got: bytes.len(),
            want: count,
            out: renode_out.to_string(),
        });
    }
    Ok(bytes[..count]
        .iter()
        .map(|b| format!("{b:#04x}"))
        .collect::<Vec<_>>()
        .join(" "))
}

/// Run ONE studio control line through the bridge and return its single reply
/// line (no trailing newline). `run` executes one Renode monitor command and
/// returns its captured output, or an error string.
///
/// Never fails: a malformed line or a monitor error becomes `ERR <reason>` so
/// the connection survives and one request → one reply always holds. The reply
/// is flattened to a single line for the same reason — a multi-line reply would
/// desynchronise a line-oriented client for the rest of the session (hardening
/// beyond the Python, whose error strings happened to be newline-free).
pub fn dispatch_control_line(
    line: &str,
    mut run: impl FnMut(&str) -> Result<String, String>,
) -> String {
    let reply = match dispatch_inner(line.trim(), &mut run) {
        Ok(reply) => reply,
        Err(reason) => format!("ERR {reason}"),
    };
    single_line(&reply)
}

/// Collapse CR/LF to spaces so a reply can never span lines.
fn single_line(s: &str) -> String {
    s.replace(['\r', '\n'], " ").trim().to_string()
}

fn dispatch_inner(
    line: &str,
    run: &mut impl FnMut(&str) -> Result<String, String>,
) -> Result<String, String> {
    let (kind, cmds) = translate_control_command(line).map_err(|e| e.to_string())?;
    match kind {
        ControlKind::ReadBytes { count } => {
            let out = run(&cmds[0])?;
            normalize_readbytes_output(&out, count).map_err(|e| e.to_string())
        }
        ControlKind::Plain => {
            let mut out = String::new();
            for cmd in &cmds {
                out = run(cmd)?;
            }
            // A property SET (an inject) prints nothing → `ok`. A property GET
            // prints its value → echo the first non-empty line back so callers
            // can read state.
            Ok(out
                .lines()
                .map(str::trim)
                .find(|l| !l.is_empty())
                .unwrap_or("ok")
                .to_string())
        }
    }
}

/// What one Renode monitor stdout line means while awaiting `sentinel`.
#[derive(Debug, PartialEq, Eq)]
pub enum MonitorLine {
    /// The bare sentinel — this command's output is complete.
    Done,
    /// A monitor-side `[ERROR]`; must surface rather than be masked as `ok`.
    Error,
    /// Noise to drop: the echoed sentinel-input, `[INFO]`/`[WARNING]` logs, and
    /// the monitor's echo of the command we wrote.
    Ignore,
    /// Real command output.
    Output,
}

/// Classify one monitor line. Ordering is the contract and is load-bearing:
///
/// The monitor echoes each line we WRITE and then prints its output, so the
/// `echo "<sentinel>"` we append appears TWICE — once as the echoed input
/// (`echo "__ALP_SIM_DONE_1__"`) and once as echo's own output (the bare
/// sentinel). Only the bare form, an EXACT match, terminates the command;
/// the echoed-input form is dropped so its token cannot pollute the captured
/// output. `[ERROR]` is checked BEFORE `[INFO]`/`[WARNING]`, and is never
/// dropped — a monitor-side fault (a `WriteByte` to a faulting address) must
/// surface instead of being reported as `ok`.
pub fn classify_monitor_line(line: &str, sentinel: &str, cmd: &str) -> MonitorLine {
    let s = line.trim();
    if s == sentinel {
        return MonitorLine::Done;
    }
    if s.contains(sentinel) {
        return MonitorLine::Ignore;
    }
    if line.contains("[ERROR]") {
        return MonitorLine::Error;
    }
    if line.contains("[INFO]") || line.contains("[WARNING]") {
        return MonitorLine::Ignore;
    }
    if s == cmd || s.ends_with(cmd) {
        return MonitorLine::Ignore;
    }
    MonitorLine::Output
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    // -----------------------------------------------------------------
    // sim-descriptor.json (studio SimDescriptorSchema)
    // -----------------------------------------------------------------

    #[test]
    fn descriptor_has_exactly_the_four_schema_keys_in_order() {
        let d = build_sim_descriptor(40001, 40002);
        let obj = d.as_object().expect("an object");
        // Exactly four keys, and `preserve_order` keeps the schema's order.
        assert_eq!(
            obj.keys().collect::<Vec<_>>(),
            vec![
                "control_socket",
                "uart_socket",
                "framebuffers",
                "peripherals"
            ]
        );
        assert_eq!(d["control_socket"], "tcp://127.0.0.1:40001");
        assert_eq!(d["uart_socket"], "tcp://127.0.0.1:40002");
        // Present-and-empty, never absent: a studio client iterates both.
        assert_eq!(d["framebuffers"], json!([]));
        assert_eq!(d["peripherals"], json!([]));
    }

    #[test]
    fn descriptor_serialises_with_the_socket_uri_scheme() {
        let text = serde_json::to_string(&build_sim_descriptor(1, 65535)).unwrap();
        assert!(text.contains("\"tcp://127.0.0.1:1\""), "{text}");
        assert!(text.contains("\"tcp://127.0.0.1:65535\""), "{text}");
    }

    // -----------------------------------------------------------------
    // Generated boot script + argv
    // -----------------------------------------------------------------

    #[test]
    fn resc_boots_headless_platform_elf_start() {
        let text = build_sim_resc_text(Path::new("/m/p.repl"), Path::new("/b/fw.elf"), None);
        assert!(text.contains("mach create \"v2n_sim\""), "{text}");
        assert!(
            text.contains("machine LoadPlatformDescription @/m/p.repl"),
            "{text}"
        );
        assert!(text.contains("sysbus LoadELF @/b/fw.elf"), "{text}");
        assert!(text.trim_end().ends_with("start"), "{text}");
        // Deferred half: no wired-UART socket terminal yet.
        assert!(!text.contains("CreateServerSocketTerminal"), "{text}");
        assert!(!text.contains("connector Connect"), "{text}");
        // No vtor -> no write at all (Renode keeps its own guess).
        assert!(!text.contains("0xE000ED08"), "{text}");
        // The generated script NEVER includes the SDK's own `.resc` (the plain
        // path's `i @…`). That independence is what keeps this path's direct
        // `sysbus WriteDoubleWord 0xE000ED08` clear of the plain path's `$vtor` /
        // `cpu VectorTableOffset` mechanism (alp-sdk#947/#953): an include added
        // here would double-seed VTOR and, with `$vtor` unset, fail the whole
        // boot with `Cannot convert type 'string' to 'uint'`.
        assert!(!text.contains("i @"), "{text}");
    }

    #[test]
    fn resc_seeds_the_secure_vtor_after_loadelf_and_before_start() {
        // The exact Python-format value: lowercase, unpadded (`hex(0x08003000)`
        // is '0x8003000'). This line IS the contract.
        let text = build_sim_resc_text(Path::new("p.repl"), Path::new("fw.elf"), Some(0x0800_3000));
        assert!(
            text.contains("sysbus WriteDoubleWord 0xE000ED08 0x8003000"),
            "{text}"
        );
        let load = text.find("LoadELF").unwrap();
        let vtor = text.find("0xE000ED08").unwrap();
        let start = text.rfind("start").unwrap();
        assert!(load < vtor && vtor < start, "{text}");
    }

    #[test]
    fn sim_argv_is_the_exact_headless_contract() {
        let argv = build_sim_renode_argv("/opt/renode/renode", Path::new("/b/.sim-boot.resc"));
        assert_eq!(
            argv,
            vec![
                "/opt/renode/renode",
                "--disable-xwt",
                "--plain",
                "--console",
                "-e",
                "i @/b/.sim-boot.resc",
            ]
        );
        // `--console` is required: the bridge drives the monitor over stdio.
        // `--hide-monitor` is refused alongside it by Renode 1.16.1.
        assert!(!argv.iter().any(|a| a == "--hide-monitor"));
    }

    // -----------------------------------------------------------------
    // ReadBytes normalisation
    // -----------------------------------------------------------------

    #[test]
    fn normalize_readbytes_lowercases_and_flattens_the_bracketed_list() {
        let out = "[\n0xDE, 0xAD, 0xBE, 0xEF, \n]\n";
        assert_eq!(
            normalize_readbytes_output(out, 4).unwrap(),
            "0xde 0xad 0xbe 0xef"
        );
    }

    #[test]
    fn normalize_readbytes_ignores_the_echoed_command_address() {
        // Regression: the echoed `sysbus ReadBytes 0x20000000 4` line carries
        // 0x20000000, which masks to 0x00 — it must NOT leak in as a byte.
        let out = "sysbus ReadBytes 0x20000000 4\n[\n0xDE, 0xAD, 0xBE, 0xEF, \n]\n";
        assert_eq!(
            normalize_readbytes_output(out, 4).unwrap(),
            "0xde 0xad 0xbe 0xef"
        );
    }

    #[test]
    fn normalize_readbytes_short_read_is_an_error_never_padded() {
        let err = normalize_readbytes_output("[ 0x01, 0x02, ]", 4).unwrap_err();
        assert_eq!(
            err,
            SimError::ShortRead {
                got: 2,
                want: 4,
                out: "[ 0x01, 0x02, ]".to_string()
            }
        );
        assert!(err.to_string().contains("expected 4"), "{err}");
    }

    #[test]
    fn normalize_readbytes_masks_wide_tokens_to_their_low_byte() {
        // `int(tok,16) & 0xFF`, done by taking the last two hex digits so an
        // arbitrarily long token cannot overflow.
        assert_eq!(
            normalize_readbytes_output("[ 0xDEAD, 0x5, 0x1234567890ABCDEF12 ]", 3).unwrap(),
            "0xad 0x05 0x12"
        );
    }

    #[test]
    fn normalize_readbytes_falls_back_to_the_whole_output_without_brackets() {
        assert_eq!(
            normalize_readbytes_output("0x41 0x42", 2).unwrap(),
            "0x41 0x42"
        );
    }

    // -----------------------------------------------------------------
    // Control-line translation (the three verbs)
    // -----------------------------------------------------------------

    #[test]
    fn translate_readbytes_forwards_verbatim_and_carries_the_count() {
        let (kind, cmds) = translate_control_command("sysbus ReadBytes 0x1000 8").unwrap();
        assert_eq!(kind, ControlKind::ReadBytes { count: 8 });
        assert_eq!(cmds, vec!["sysbus ReadBytes 0x1000 8"]);
    }

    #[test]
    fn translate_writebytes_expands_to_ordered_lowercase_writebyte() {
        // Studio order is <base> <hex…>; Renode's WriteBytes is (bytes, addr),
        // so the bridge expands to per-byte WriteByte at base+i. Lowercase and
        // unpadded, matching Python's hex().
        let (kind, cmds) =
            translate_control_command("sysbus WriteBytes 0x20000000 0xde 0xad 0xbe 0xef").unwrap();
        assert_eq!(kind, ControlKind::Plain);
        assert_eq!(
            cmds,
            vec![
                "sysbus WriteByte 0x20000000 0xde",
                "sysbus WriteByte 0x20000001 0xad",
                "sysbus WriteByte 0x20000002 0xbe",
                "sysbus WriteByte 0x20000003 0xef",
            ]
        );
    }

    #[test]
    fn translate_writebytes_masks_oversized_bytes() {
        let (_, cmds) = translate_control_command("sysbus WriteBytes 0x100 0x1de 256").unwrap();
        assert_eq!(
            cmds,
            vec!["sysbus WriteByte 0x100 0xde", "sysbus WriteByte 0x101 0x0"]
        );
    }

    #[test]
    fn translate_rejects_a_writebytes_with_no_data() {
        assert_eq!(
            translate_control_command("sysbus WriteBytes 0x20000000").unwrap_err(),
            SimError::WriteBytesNoData {
                line: "sysbus WriteBytes 0x20000000".to_string()
            }
        );
    }

    #[test]
    fn translate_rejects_malformed_bases_and_counts() {
        // Must surface as a SimError so the bridge replies ERR instead of the
        // client thread dying.
        let e = translate_control_command("sysbus WriteBytes zzz 0xde").unwrap_err();
        assert!(e.to_string().starts_with("malformed WriteBytes"), "{e}");
        let e = translate_control_command("sysbus ReadBytes 0x1000 xx").unwrap_err();
        assert!(e.to_string().starts_with("malformed ReadBytes"), "{e}");
        // Signed tokens are rejected rather than masked (documented divergence).
        let e = translate_control_command("sysbus WriteBytes -1 0xde").unwrap_err();
        assert!(e.to_string().starts_with("malformed WriteBytes"), "{e}");
    }

    #[test]
    fn translate_accepts_decimal_and_the_other_python_radices() {
        let (kind, _) = translate_control_command("sysbus ReadBytes 4096 4").unwrap();
        assert_eq!(kind, ControlKind::ReadBytes { count: 4 });
        let (_, cmds) = translate_control_command("sysbus WriteBytes 0o20 0b1010").unwrap();
        assert_eq!(cmds, vec!["sysbus WriteByte 0x10 0xa"]);
    }

    #[test]
    fn documented_int_token_divergences_from_python_hold() {
        // A leading-zero decimal: accepted as 10 here, `int("010", 0)` raises.
        assert_eq!(parse_int_auto("010"), Some(10));
        // PEP 515 digit separators: rejected here, `int("1_0", 0)` is 10.
        assert_eq!(parse_int_auto("1_0"), None);
        // …and both directions are harmless, so they only need pinning, not
        // fixing: the reject surfaces as an `ERR malformed …` reply.
        let e = translate_control_command("sysbus ReadBytes 0x1000 1_0").unwrap_err();
        assert!(e.to_string().starts_with("malformed ReadBytes"), "{e}");
    }

    #[test]
    fn a_writebytes_address_overflow_names_the_arithmetic_not_a_token() {
        // Every token parsed fine — reusing the token-level reason here would
        // render as `invalid integer token "base + offset overflows"`.
        let e =
            translate_control_command("sysbus WriteBytes 0xFFFFFFFFFFFFFFFF 0x1 0x2").unwrap_err();
        let text = e.to_string();
        assert!(text.starts_with("malformed WriteBytes"), "{text}");
        assert!(text.contains("overflows a 64-bit address"), "{text}");
        assert!(!text.contains("invalid integer token"), "{text}");
    }

    // -----------------------------------------------------------------
    // The deferred-profile warning + the readiness marker
    // -----------------------------------------------------------------

    #[test]
    fn the_deferred_profile_warning_states_the_empty_arrays_and_silent_uart() {
        // Silence is the one unacceptable option: an empty descriptor must never
        // read as a successful one.
        let m = sim_profile_deferred_message("E1M-V2N101");
        assert!(m.contains("E1M-V2N101"), "{m}");
        assert!(m.contains("framebuffers"), "{m}");
        assert!(m.contains("peripherals"), "{m}");
        assert!(m.contains("BOTH empty"), "{m}");
        assert!(m.contains("streams NOTHING"), "{m}");
        assert!(m.contains("tan-cli#77"), "{m}");
        // A ram_console SKU gets no wired-UART clause.
        assert!(!m.contains("WIRED hardware UART"), "{m}");
    }

    #[test]
    fn the_deferred_profile_warning_fires_for_aen801_and_names_its_wired_console() {
        // AEN801 is this project's first-target SKU and the Python served its
        // console over a WIRED UART, so it needs the second reason spelled out.
        let m = sim_profile_deferred_message("E1M-AEN801");
        assert!(m.contains("E1M-AEN801"), "{m}");
        assert!(m.contains("BOTH empty"), "{m}");
        assert!(m.contains("WIRED hardware UART"), "{m}");
        assert!(m.contains("deferred as well"), "{m}");
    }

    #[test]
    fn the_ready_marker_carries_the_consumers_poll_token() {
        // `ready (timeout` IS the contract: the retired Python's e2e polled the
        // process output for this substring before reading the descriptor.
        let line = ready_marker(60);
        assert!(line.contains("ready (timeout"), "{line}");
        assert_eq!(line, "tan renode --sim-mode: ready (timeout 60s).");
    }

    #[test]
    fn translate_forwards_an_inject_template_verbatim() {
        let line = "sysbus.iic8.i2c_tmp112 Temperature 85";
        let (kind, cmds) = translate_control_command(line).unwrap();
        assert_eq!(kind, ControlKind::Plain);
        assert_eq!(cmds, vec![line]);
    }

    // -----------------------------------------------------------------
    // The full control-socket dispatch — the four e2e assertions the
    // retired Python's `test_sim_mode_end_to_end` pinned, against a fake
    // monitor standing in for Renode.
    // -----------------------------------------------------------------

    /// A fake Renode monitor: a byte-addressed memory plus a property store,
    /// answering the same monitor vocabulary the bridge emits.
    #[derive(Default)]
    struct FakeMonitor {
        mem: HashMap<u64, u8>,
        props: HashMap<String, String>,
    }

    impl FakeMonitor {
        fn command(&mut self, cmd: &str) -> Result<String, String> {
            let p: Vec<&str> = cmd.split_whitespace().collect();
            match p.as_slice() {
                ["sysbus", "WriteByte", addr, val] => {
                    let a = parse_int_auto(addr).ok_or("bad addr")?;
                    let v = parse_int_auto(val).ok_or("bad val")? as u8;
                    self.mem.insert(a, v);
                    Ok(String::new()) // a write prints nothing
                }
                ["sysbus", "ReadBytes", addr, count] => {
                    let a = parse_int_auto(addr).ok_or("bad addr")?;
                    let n = parse_int_auto(count).ok_or("bad count")?;
                    // Renode's real shape: bracketed, comma-separated, UPPER-case.
                    let body: Vec<String> = (0..n)
                        .map(|i| format!("0x{:02X}", self.mem.get(&(a + i)).copied().unwrap_or(0)))
                        .collect();
                    Ok(format!("{cmd}\n[\n{}, \n]\n", body.join(", ")))
                }
                [node, prop, value] => {
                    self.props
                        .insert(format!("{node} {prop}"), value.to_string());
                    Ok(String::new()) // a property SET prints nothing
                }
                [node, prop] => Ok(self
                    .props
                    .get(&format!("{node} {prop}"))
                    .cloned()
                    .unwrap_or_default()),
                _ => Err(format!("No such command {cmd:?}")),
            }
        }
    }

    #[test]
    fn control_socket_round_trips_the_four_end_to_end_assertions() {
        let mut fake = FakeMonitor::default();
        let mut cmd = |line: &str| dispatch_control_line(line, |c| fake.command(c));

        // 1. a WriteBytes replies `ok` (per-byte WriteByte prints nothing).
        assert_eq!(
            cmd("sysbus WriteBytes 0x08010000 0xde 0xad 0xbe 0xef"),
            "ok"
        );
        // 2. the ReadBytes reply is lowercase, space-separated, one line.
        assert_eq!(cmd("sysbus ReadBytes 0x08010000 4"), "0xde 0xad 0xbe 0xef");
        // 3. an inject (property SET) replies `ok`.
        assert_eq!(cmd("sysbus.iic8.i2c_tmp112 Temperature 85"), "ok");
        // 4. a property GET echoes the value back.
        assert!(cmd("sysbus.iic8.i2c_tmp112 Temperature").contains("85"));
    }

    #[test]
    fn a_malformed_line_replies_err_and_never_panics() {
        let mut fake = FakeMonitor::default();
        let reply = dispatch_control_line("sysbus WriteBytes 0x100", |c| fake.command(c));
        assert!(reply.starts_with("ERR "), "{reply}");
        assert!(reply.contains("no data bytes"), "{reply}");
    }

    #[test]
    fn a_monitor_error_replies_err_carrying_the_reason() {
        let reply = dispatch_control_line("bogus", |_| Err("Renode monitor is unusable".into()));
        assert_eq!(reply, "ERR Renode monitor is unusable");
    }

    #[test]
    fn a_short_read_replies_err_rather_than_a_padded_answer() {
        let reply = dispatch_control_line("sysbus ReadBytes 0x100 8", |_| {
            Ok("[ 0x01, 0x02, ]".to_string())
        });
        assert!(reply.starts_with("ERR "), "{reply}");
        assert!(reply.contains("expected 8"), "{reply}");
    }

    #[test]
    fn every_reply_is_exactly_one_line() {
        // One request -> one reply is the whole framing contract: a multi-line
        // reply would desynchronise the client for the rest of the session.
        for reply in [
            dispatch_control_line("get thing", |_| Ok("a\nb\nc".to_string())),
            dispatch_control_line("get thing", |_| Err("line one\nline two".to_string())),
            dispatch_control_line("sysbus ReadBytes 0x1 2", |_| {
                Ok("[\n0x1,\n0x2,\n]".to_string())
            }),
        ] {
            assert!(!reply.contains('\n'), "{reply:?}");
            assert!(!reply.contains('\r'), "{reply:?}");
        }
    }

    // -----------------------------------------------------------------
    // Monitor line classification
    // -----------------------------------------------------------------

    #[test]
    fn only_the_bare_sentinel_terminates_a_command() {
        let sent = "__ALP_SIM_DONE_7__";
        let cmd = "sysbus ReadBytes 0x1000 4";
        assert_eq!(
            classify_monitor_line(sent, sent, cmd),
            MonitorLine::Done,
            "the bare sentinel is the completion marker"
        );
        assert_eq!(
            classify_monitor_line("  __ALP_SIM_DONE_7__  ", sent, cmd),
            MonitorLine::Done,
            "trimmed exact match still counts"
        );
        // The echoed INPUT carries the sentinel but is not it — dropping it is
        // what keeps `echo "…"` out of the captured output.
        assert_eq!(
            classify_monitor_line("echo \"__ALP_SIM_DONE_7__\"", sent, cmd),
            MonitorLine::Ignore
        );
    }

    #[test]
    fn errors_surface_and_info_warning_and_the_command_echo_do_not() {
        let sent = "__ALP_SIM_DONE_1__";
        let cmd = "sysbus WriteByte 0x0 0x1";
        assert_eq!(
            classify_monitor_line("12:00:00.1 [ERROR] sysbus: no peripheral", sent, cmd),
            MonitorLine::Error,
            "a monitor-side fault must not be masked as ok"
        );
        // [ERROR] is checked BEFORE [INFO]/[WARNING]: a line carrying both must
        // still surface as an error.
        assert_eq!(
            classify_monitor_line("[INFO] and [ERROR] together", sent, cmd),
            MonitorLine::Error
        );
        assert_eq!(
            classify_monitor_line("12:00:00.1 [INFO] machine: started", sent, cmd),
            MonitorLine::Ignore
        );
        assert_eq!(
            classify_monitor_line("12:00:00.1 [WARNING] cpu: slow", sent, cmd),
            MonitorLine::Ignore
        );
        assert_eq!(classify_monitor_line(cmd, sent, cmd), MonitorLine::Ignore);
        assert_eq!(
            classify_monitor_line(&format!("(monitor) {cmd}"), sent, cmd),
            MonitorLine::Ignore,
            "the monitor prefixes its echo — endswith is the match"
        );
        assert_eq!(
            classify_monitor_line("[\n0xDE, ]", sent, cmd),
            MonitorLine::Output
        );
    }
}
