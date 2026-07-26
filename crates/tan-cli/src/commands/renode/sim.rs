// SPDX-License-Identifier: Apache-2.0
//! `tan renode --sim-mode` — the studio hardware-simulator gateway's IO half:
//! bundle/ELF resolution, the two ephemeral listeners, the descriptor + boot
//! script on disk, the Renode child, and the two socket servers. Every wire
//! DECISION (descriptor shape, boot-script text, argv, the control-line
//! protocol) is `tan_core::renode::sim`, unit-tested there.
//!
//! Ported from the retired Python `run_sim`
//! (`scripts/west_commands/alp_renode.py`, deleted in `alp-sdk@df312cec` under
//! ADR-0020 Phase 4). Historical contract `alplabai/alp-sdk#674` — always with
//! the owning repo, since a bare `#674` read here means `tan-cli#674`, which
//! has never existed.
//!
//! SCOPE (`tan-cli#77`, socket half). DEFERRED to a follow-up on the same
//! issue: the `ram_console_buf` RAM-ring streamer that gives the UART socket
//! real console bytes, the wired-UART console path (Renode's own socket
//! terminal), and the per-SKU sim profiles behind the descriptor's
//! `framebuffers`/`peripherals`. Until then the UART socket accepts and holds
//! connections but streams NOTHING — which is exactly what the Python did for a
//! core whose image carries no `ram_console_buf` symbol.
//!
//! Divergences from the Python, all deliberate:
//!   - both listeners are bound on port 0 and their assigned ports read back,
//!     instead of `pick_free_ports(2)` then re-binding. That removes the
//!     Python's bind-then-close TOCTOU window entirely: a port cannot be taken
//!     between picking and binding if it was never released. (The follow-up's
//!     wired-UART path DOES need pick-then-hand-off, because Renode binds that
//!     port itself — reinstate a picker there, not here.)
//!   - the readiness marker goes to stderr in `--format json` mode, where
//!     stdout is reserved for the single Envelope line. The load-bearing
//!     substring is byte-identical on both streams.
//!   - teardown sends `quit`, polls briefly for the exit, THEN kills — the
//!     Python's `terminate()` + `wait(10)` + `kill` on a shorter budget. It does
//!     not SIGTERM first: `quit` is the monitor's own shutdown request and is
//!     strictly more graceful.
//!   - a SKU with no sim profile is served with a warning instead of the
//!     Python's hard refusal, because the socket half works without one
//!     (`sim_profile_deferred_message`). It is never served silently.
//!   - the `renode.cpu-halted` latch (issue #64) applies here too, latched in
//!     the monitor's pump thread rather than a console scan, since the monitor
//!     owns Renode's stdout in this mode.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use tan_core::renode::sim::{
    build_sim_descriptor, build_sim_renode_argv, build_sim_resc_text, dispatch_control_line,
    ready_marker, sim_profile_deferred_message,
};
use tan_core::renode::{
    elf_vector_table_base, platform_files_for_sku, platform_stem_for_sku, zephyr_elf_from_manifest,
};
use tan_core::system_manifest::{SystemManifest, SystemManifestError, parse_system_manifest};

use super::monitor::RenodeMonitor;
use super::{
    RenodeReport, exit_status_desc, fail, fail_with, finish, issue, resolve_renode_binary,
};
use crate::cli::{GlobalArgs, RenodeArgs};
use crate::commands::CommandRun;
use crate::envelope::{Issue, Project};
use crate::exit::ExitCode;
use crate::util::{cli_workspace_root, normalize_path, resolve_sdk_root};

/// One control line → its single reply line. Boxed so the socket server is
/// testable against a fake without a live Renode child.
type ControlDispatch = Arc<dyn Fn(&str) -> String + Send + Sync>;

/// How long teardown waits for Renode to act on `quit` before killing it. The
/// Python allowed `terminate()` + `wait(10)`; one second is enough for the
/// emulation to close its sockets and flush its log, and this is a teardown, not
/// a graceful-shutdown protocol — the process is going away either way.
const QUIT_GRACE: Duration = Duration::from_secs(1);

/// `tan renode --sim-mode` entry — called from [`super::run`] before any
/// build-root work, because sim mode resolves everything from `--image-bundle`
/// instead.
pub(super) fn run(
    g: &GlobalArgs,
    args: &RenodeArgs,
    project: Project,
    mut report: RenodeReport,
) -> CommandRun {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let Some(bundle) = args.image_bundle.as_deref() else {
        return fail(
            g,
            project,
            report,
            "renode.sim-bundle-required",
            "--sim-mode requires --image-bundle <dir>.",
        );
    };
    let bundle_dir = abs(&cwd, bundle);
    if !bundle_dir.is_dir() {
        return fail(
            g,
            project,
            report,
            "renode.sim-bundle-missing",
            &format!(
                "--image-bundle {} is not a directory.",
                bundle_dir.display()
            ),
        );
    }

    let Some(sdk_root) = resolve_sdk_root(g, &cli_workspace_root(g)) else {
        return fail(
            g,
            project,
            report,
            "renode.sdk-root-not-found",
            "Cannot locate alp-sdk root.",
        );
    };

    // The bundle's own manifest, when it has one: it supplies both the SKU and
    // the slice→ELF resolution. Absent is fine (a bare bundle of ELFs).
    let manifest_path = bundle_dir.join("system-manifest.yaml");
    let manifest: Option<SystemManifest> = if manifest_path.is_file() {
        match std::fs::read_to_string(&manifest_path).map(|y| parse_system_manifest(&y)) {
            Ok(Ok(m)) => Some(m),
            // The one new guard tan adds over the Python: an unsupported
            // schema_version is a ValidationFailure, not a runtime failure.
            Ok(Err(e @ SystemManifestError::UnsupportedSchemaVersion { .. })) => {
                return fail_with(
                    g,
                    project,
                    report,
                    "renode.manifest-schema",
                    &format!("{}: {e}", manifest_path.display()),
                    ExitCode::ValidationFailure,
                );
            }
            Ok(Err(e)) => {
                return fail(
                    g,
                    project,
                    report,
                    "renode.manifest-invalid",
                    &format!("{}: {e}", manifest_path.display()),
                );
            }
            Err(e) => {
                return fail(
                    g,
                    project,
                    report,
                    "renode.manifest-unavailable",
                    &format!("{}: {e}", manifest_path.display()),
                );
            }
        }
    } else {
        None
    };

    let sku = match args
        .board
        .as_deref()
        .map(str::trim)
        .filter(|b| !b.is_empty())
    {
        Some(b) => b.to_string(),
        None => match manifest
            .as_ref()
            .map(|m| m.hw_info.sku.trim())
            .filter(|s| !s.is_empty())
        {
            Some(s) => s.to_string(),
            None => {
                return fail(
                    g,
                    project,
                    report,
                    "renode.sku-unresolved",
                    "--sim-mode could not determine the board: pass --board <SKU> \
                     (no hw_info.sku in the bundle manifest).",
                );
            }
        },
    };
    report.sku = sku.clone();

    let elf = match resolve_bundle_elf(&bundle_dir, manifest.as_ref(), args.core.as_deref()) {
        Ok(p) => p,
        Err(msg) => return fail(g, project, report, "renode.elf-missing", &msg),
    };
    if !elf.is_file() {
        return fail(
            g,
            project,
            report,
            "renode.elf-missing",
            &format!("firmware ELF not found at {}.", elf.display()),
        );
    }
    report.elf = elf.to_string_lossy().into_owned();

    // Only the `.repl` matters here: unlike the plain smoke, sim mode GENERATES
    // its own boot script rather than including the SDK's `.resc`.
    let repl = match platform_files_for_sku(&sku, &sdk_root) {
        Ok((repl, _resc)) => repl,
        Err(e) => return fail(g, project, report, "renode.descriptor", &e.to_string()),
    };
    if !repl.is_file() {
        return fail(
            g,
            project,
            report,
            "renode.descriptor-missing",
            &format!("missing Renode descriptor {}.", repl.display()),
        );
    }
    report.platform_stem = platform_stem_for_sku(&sku).unwrap_or_default();
    report.repl = repl.to_string_lossy().into_owned();

    let Some(renode_bin) = resolve_renode_binary() else {
        return fail(
            g,
            project,
            report,
            "renode.binary-missing",
            "`renode` binary not found on PATH. Install Renode (https://renode.io). \
             tan renode does not silently pass when Renode is missing.",
        );
    };

    // BIND BEFORE ADVERTISING. Both listeners are accepting by the time the
    // descriptor names their ports, so a studio client that reads the
    // descriptor and connects immediately can never race into an
    // ECONNREFUSED — the kernel backlogs the connection pre-accept.
    let (ctrl_srv, uart_srv, control_port, uart_port) = match bind_sim_listeners() {
        Ok(bound) => bound,
        Err(e) => {
            return fail(
                g,
                project,
                report,
                "renode.sim-bind-failed",
                &format!("could not bind a sim socket: {e}"),
            );
        }
    };

    let descriptor_path = bundle_dir.join("sim-descriptor.json");
    let descriptor = build_sim_descriptor(control_port, uart_port);
    let descriptor_text = match serde_json::to_string_pretty(&descriptor) {
        Ok(t) => format!("{t}\n"),
        Err(e) => {
            return fail(
                g,
                project,
                report,
                "renode.sim-descriptor-failed",
                &format!("could not serialise the sim descriptor: {e}"),
            );
        }
    };
    if let Err(e) = std::fs::write(&descriptor_path, &descriptor_text) {
        return fail(
            g,
            project,
            report,
            "renode.sim-descriptor-failed",
            &format!("could not write {}: {e}", descriptor_path.display()),
        );
    }
    // Recorded in `data` so a JSON consumer is not left assuming the file name
    // and re-deriving the ports out of the descriptor it has to find first.
    report.descriptor = descriptor_path.to_string_lossy().into_owned();
    report.control_port = control_port;
    report.uart_port = uart_port;

    // Best-effort, exactly like the plain smoke: an unreadable or unexpected
    // ELF seeds no VTOR and leaves Renode's own guess alone.
    let vtor = std::fs::read(&elf)
        .ok()
        .and_then(|bytes| elf_vector_table_base(&bytes));
    let resc_path = bundle_dir.join(".sim-boot.resc");
    if let Err(e) = std::fs::write(&resc_path, build_sim_resc_text(&repl, &elf, vtor)) {
        return fail(
            g,
            project,
            report,
            "renode.sim-boot-script-failed",
            &format!("could not write {}: {e}", resc_path.display()),
        );
    }
    report.resc = resc_path.to_string_lossy().into_owned();

    let log_path = match args.log.as_deref() {
        Some(raw) => abs(&cwd, raw),
        None => bundle_dir.join("renode-sim.log"),
    };
    report.log_path = log_path.to_string_lossy().into_owned();

    let argv = build_sim_renode_argv(&renode_bin, &resc_path);
    report.renode_argv = argv.clone();

    let mut issues: Vec<Issue> = Vec::new();
    // The descriptor's `framebuffers`/`peripherals` are empty and the UART is
    // silent while the per-SKU profile half of tan-cli#77 is deferred. Saying so
    // is not optional: the Python REFUSED a SKU with no profile, so serving one
    // silently would turn its hard error into a successful-looking run with no
    // peripherals to inject into and a console that never speaks. Warning, not
    // error — the control socket really does work — but on BOTH streams.
    let deferred = sim_profile_deferred_message(&sku);
    issues.push(issue("renode.sim-profile-deferred", "warning", &deferred));
    if args.expect.is_some() {
        // Say so rather than ignoring it: sim mode routes the console to a
        // socket, so there is no console text here for --expect to scan.
        issues.push(issue(
            "renode.expect-ignored",
            "info",
            "renode: --expect is ignored in --sim-mode (the console is served on the \
             UART socket, not scanned).",
        ));
    }

    if !g.is_json() {
        println!(
            "tan renode --sim-mode: {sku} booting {}",
            elf.file_name().unwrap_or_default().to_string_lossy()
        );
        println!("  descriptor : {}", descriptor_path.display());
        println!("  control    : tcp://127.0.0.1:{control_port}");
        println!(
            "  uart       : tcp://127.0.0.1:{uart_port} \
             (silent — the ram_console bridge is deferred, tan-cli#77)"
        );
        // Text mode drops `issues`, so the warning has to be printed too or a
        // human sees only the reassuring three lines above.
        println!("{deferred}");
    }

    let mut child = match spawn_renode(&argv, &log_path) {
        Ok(c) => c,
        Err(e) => {
            return fail(
                g,
                project,
                report,
                "renode.run-failed",
                &format!("failed to run renode: {e}"),
            );
        }
    };
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        let _ = child.kill();
        let _ = child.wait();
        return fail(
            g,
            project,
            report,
            "renode.run-failed",
            "failed to run renode: the child's stdio pipes were not created.",
        );
    };

    let monitor = Arc::new(RenodeMonitor::new(stdin, stdout));
    // Swallow the boot output FIRST and exclusively, THEN start accepting
    // clients — otherwise a client command races the boot drain for the monitor
    // and captures boot text as its reply.
    if let Err(e) = monitor.drain_boot() {
        teardown(&monitor, &mut child);
        return fail(
            g,
            project,
            report,
            "renode.sim-monitor-failed",
            &format!(
                "Renode monitor never became ready: {e} (see {}).",
                log_path.display()
            ),
        );
    }

    let bridge = Arc::clone(&monitor);
    let dispatch: ControlDispatch =
        Arc::new(move |line: &str| dispatch_control_line(line, |cmd| bridge.command(cmd)));
    thread::spawn(move || serve_control(ctrl_srv, dispatch));
    thread::spawn(move || serve_uart_silent(uart_srv));

    announce_ready(g, args.timeout);

    // Hold the sockets open until the timeout, failing if Renode dies first.
    let mut early: Option<ExitStatus> = None;
    let deadline = Instant::now() + Duration::from_secs(args.timeout);
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => {
                early = Some(status);
                break;
            }
            Ok(None) => thread::sleep(Duration::from_millis(250)),
            Err(_) => break,
        }
    }

    teardown(&monitor, &mut child);

    if let Some(status) = early {
        return fail(
            g,
            project,
            report,
            "renode.sim-exited-early",
            &format!(
                "Renode exited early ({}); see {}.",
                exit_status_desc(status),
                log_path.display()
            ),
        );
    }
    // Checked LAST and independently of everything above, exactly like the plain
    // smoke's identical latch (`super::run`, issue #64): a Renode that boots,
    // halts the CPU on its first instruction fetch and then sits there until the
    // timeout looks — to every other signal in this function — like a healthy
    // session. Renode's stdout is consumed by the monitor here, so without the
    // latch the halt would at best resurface as an `ERR` on whichever client
    // command came next, and `tan` would still exit 0. The sim path is where a
    // mis-seeded VTOR shows up this way.
    if monitor.cpu_halted() {
        let msg = format!(
            "renode: the CPU halted on its first instruction fetch — no firmware code \
             ever ran, even though the sim session came up (see {}).",
            log_path.display()
        );
        issues.push(issue("renode.cpu-halted", "error", &msg));
        let text = if g.is_json() { Vec::new() } else { vec![msg] };
        return finish(g, project, report, text, issues, ExitCode::RuntimeFailure);
    }
    finish(g, project, report, Vec::new(), issues, ExitCode::Success)
}

/// Ask Renode to `quit`, give it [`QUIT_GRACE`] to actually do it, then make
/// sure it is gone. `quit` on its own is only a REQUEST: killing the child
/// microseconds after the flush (what this used to do) left the emulation no
/// time to close its sockets or flush its log, which is why the Python paired
/// `terminate()` with `wait(10)`.
fn teardown(monitor: &RenodeMonitor<ChildStdin>, child: &mut Child) {
    monitor.quit();
    let deadline = Instant::now() + QUIT_GRACE;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return, // quit worked; try_wait already reaped it
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(_) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Print the readiness marker. The LINE is
/// [`tan_core::renode::sim::ready_marker`] — it carries the consumer's
/// `ready (timeout` poll token and is pinned by a test there, since a reword
/// strands every consumer and this function cannot be tested (it prints
/// inline). The only decision left here is the STREAM: stdout in text mode
/// (where the Python printed it), stderr in JSON mode, where stdout carries the
/// single Envelope line and nothing else. A consumer teeing only stdout to a
/// log file therefore will not see it in JSON mode — poll the merged output, or
/// poll for `sim-descriptor.json` and the port it names.
fn announce_ready(g: &GlobalArgs, timeout: u64) {
    let line = ready_marker(timeout);
    if g.is_json() {
        eprintln!("{line}");
        // stderr is unbuffered, but be explicit: a consumer polling for the
        // token must see it NOW, not when the process exits.
        let _ = std::io::stderr().flush();
    } else {
        println!("{line}");
        let _ = std::io::stdout().flush();
    }
}

/// Absolutise `raw` against `cwd` unless it already is.
fn abs(cwd: &Path, raw: &str) -> PathBuf {
    let p = Path::new(raw);
    if p.is_absolute() {
        normalize_path(p)
    } else {
        normalize_path(&cwd.join(p))
    }
}

/// Bind the control + UART listeners on ephemeral `127.0.0.1` ports and read the
/// assigned port numbers back. Both stay held, so the ports cannot be taken
/// from under us and are distinct by construction. Returns
/// `(control, uart, control_port, uart_port)`.
fn bind_sim_listeners() -> std::io::Result<(TcpListener, TcpListener, u16, u16)> {
    let ctrl = TcpListener::bind("127.0.0.1:0")?;
    let uart = TcpListener::bind("127.0.0.1:0")?;
    let control_port = ctrl.local_addr()?.port();
    let uart_port = uart.local_addr()?.port();
    Ok((ctrl, uart, control_port, uart_port))
}

/// Resolve the firmware ELF inside a pre-built `--image-bundle` dir:
/// the bundle's `system-manifest.yaml` (reusing the slice resolver) →
/// `<bundle>/zephyr/zephyr.elf` → the single `*.elf` in the bundle.
fn resolve_bundle_elf(
    bundle_dir: &Path,
    manifest: Option<&SystemManifest>,
    core: Option<&str>,
) -> Result<PathBuf, String> {
    if let Some(m) = manifest {
        return zephyr_elf_from_manifest(m, bundle_dir, core).map_err(|e| e.to_string());
    }
    let direct = bundle_dir.join("zephyr").join("zephyr.elf");
    if direct.is_file() {
        return Ok(direct);
    }
    let mut elfs: Vec<PathBuf> = std::fs::read_dir(bundle_dir)
        .map_err(|e| {
            format!(
                "could not read --image-bundle {}: {e}",
                bundle_dir.display()
            )
        })?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().is_some_and(|x| x == "elf"))
        .collect();
    elfs.sort();
    match elfs.len() {
        1 => Ok(elfs.remove(0)),
        0 => Err(format!(
            "no firmware ELF in --image-bundle {} (looked for system-manifest.yaml, \
             zephyr/zephyr.elf, *.elf).",
            bundle_dir.display()
        )),
        _ => Err(format!(
            "multiple *.elf in --image-bundle {} ({:?}); can't pick one automatically.",
            bundle_dir.display(),
            elfs.iter()
                .map(|p| p.file_name().unwrap_or_default().to_string_lossy())
                .collect::<Vec<_>>()
        )),
    }
}

/// Spawn headless Renode with stdio wired for the monitor bridge: stdin+stdout
/// are pipes the bridge drives, stderr goes straight to the log file (verbatim
/// from the Python's `stderr=logf`).
fn spawn_renode(argv: &[String], log_path: &Path) -> std::io::Result<std::process::Child> {
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let logf = std::fs::File::create(log_path)?;
    Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(logf))
        .spawn()
}

/// Accept control clients forever, each on its own thread. The listener is
/// already bound + listening by the time the descriptor advertises its port.
fn serve_control(listener: TcpListener, dispatch: ControlDispatch) {
    for stream in listener.incoming() {
        let Ok(stream) = stream else { return };
        let dispatch = Arc::clone(&dispatch);
        thread::spawn(move || handle_control_client(stream, dispatch));
    }
}

/// One control client: line-oriented, ONE request line → ONE reply line, until
/// the peer closes. A bad line never kills the connection — it is answered with
/// `ERR <reason>` (built in `tan_core`) so the framing invariant holds for the
/// rest of the session. A blank line is skipped without a reply, verbatim from
/// the Python.
fn handle_control_client(stream: TcpStream, dispatch: ControlDispatch) {
    let Ok(read_half) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(read_half);
    let mut writer = stream;
    let mut buf: Vec<u8> = Vec::new();
    loop {
        buf.clear();
        match reader.read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => return, // peer closed, or a real IO error
            Ok(_) => {}
        }
        // Lossy, not strict: a stray non-UTF-8 byte must not drop the session.
        let line = String::from_utf8_lossy(&buf);
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let reply = dispatch(line);
        if writeln!(writer, "{reply}").is_err() || writer.flush().is_err() {
            return;
        }
    }
}

/// Accept UART clients and hold each connection open, streaming nothing.
///
/// This is the socket half of a channel whose CONTENT is deferred
/// (`tan-cli#77`): the `ram_console_buf` RAM-ring streamer that fills it is a
/// follow-up. Accepting-and-silent is exactly what the Python did when an
/// image carried no `ram_console_buf` symbol, so a studio client's serial view
/// connects successfully and simply stays empty rather than failing to connect.
fn serve_uart_silent(listener: TcpListener) {
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { return };
        thread::spawn(move || {
            // Output-only to studio; read solely to detect the close and reap.
            let mut sink = [0u8; 256];
            while matches!(stream.read(&mut sink), Ok(n) if n > 0) {}
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufRead;

    #[test]
    fn both_listeners_are_bound_and_connectable_before_anything_is_advertised() {
        // The whole point of binding before writing the descriptor: a client
        // that reads a port out of the descriptor must be able to connect at
        // once. Ephemeral ports keep this safe under a parallel `cargo test`.
        let (_ctrl, _uart, control_port, uart_port) = bind_sim_listeners().unwrap();
        assert_ne!(control_port, uart_port);
        assert_ne!(control_port, 0);
        assert_ne!(uart_port, 0);
        for port in [control_port, uart_port] {
            TcpStream::connect(("127.0.0.1", port))
                .unwrap_or_else(|e| panic!("port {port} not accepting: {e}"));
        }
    }

    #[test]
    fn control_socket_is_line_oriented_one_request_one_reply() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        // A dispatcher standing in for the Renode bridge: echoes the request so
        // replies are identifiable, and ERRs on one specific line to prove a
        // bad line does not kill the connection.
        let dispatch: ControlDispatch = Arc::new(|line: &str| {
            if line == "bad" {
                "ERR malformed".to_string()
            } else {
                format!("reply:{line}")
            }
        });
        thread::spawn(move || serve_control(listener, dispatch));

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let mut writer = stream.try_clone().unwrap();
        let mut reader = BufReader::new(stream);
        let mut reply = String::new();

        writeln!(writer, "first").unwrap();
        writer.flush().unwrap();
        reader.read_line(&mut reply).unwrap();
        assert_eq!(reply, "reply:first\n");

        // A blank line draws NO reply, so the next reply must be `second`'s —
        // this is what proves the request/reply stream stays in step.
        reply.clear();
        write!(writer, "\n   \nbad\n").unwrap();
        writer.flush().unwrap();
        reader.read_line(&mut reply).unwrap();
        assert_eq!(reply, "ERR malformed\n");

        // …and the connection survives the ERR.
        reply.clear();
        writeln!(writer, "second").unwrap();
        writer.flush().unwrap();
        reader.read_line(&mut reply).unwrap();
        assert_eq!(reply, "reply:second\n");
    }

    #[test]
    fn control_socket_serves_several_clients_at_once() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let dispatch: ControlDispatch = Arc::new(|line: &str| format!("ok:{line}"));
        thread::spawn(move || serve_control(listener, dispatch));

        // Both connections stay open simultaneously: a per-client thread, not a
        // one-at-a-time accept loop.
        let a = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let b = TcpStream::connect(("127.0.0.1", port)).unwrap();
        for (mut s, tag) in [(a, "a"), (b, "b")] {
            writeln!(s, "{tag}").unwrap();
            s.flush().unwrap();
            let mut reply = String::new();
            BufReader::new(s).read_line(&mut reply).unwrap();
            assert_eq!(reply, format!("ok:{tag}\n"));
        }
    }

    #[test]
    fn uart_socket_accepts_and_holds_the_connection_open_while_silent() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || serve_uart_silent(listener));

        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        // Silent, not closed: a read with a short timeout must time out rather
        // than return 0 (EOF). Studio's serial view stays connected-and-empty.
        stream
            .set_read_timeout(Some(Duration::from_millis(250)))
            .unwrap();
        let mut buf = [0u8; 16];
        match stream.read(&mut buf) {
            Ok(0) => panic!("the UART socket closed the connection instead of holding it open"),
            Ok(n) => panic!("the UART socket streamed {n} bytes; the streamer is deferred"),
            Err(e) => assert!(
                matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ),
                "unexpected error {e:?}"
            ),
        }
    }

    #[test]
    fn bundle_elf_resolution_order_is_manifest_then_zephyr_dir_then_lone_elf() {
        let root = std::env::temp_dir().join(format!("tan-sim-bundle-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);

        // No manifest, a zephyr/zephyr.elf -> that one.
        let b1 = root.join("b1");
        std::fs::create_dir_all(b1.join("zephyr")).unwrap();
        std::fs::write(b1.join("zephyr").join("zephyr.elf"), b"\x7fELF").unwrap();
        assert_eq!(
            resolve_bundle_elf(&b1, None, None).unwrap(),
            b1.join("zephyr").join("zephyr.elf")
        );

        // A single loose *.elf -> that one.
        let b2 = root.join("b2");
        std::fs::create_dir_all(&b2).unwrap();
        std::fs::write(b2.join("app.elf"), b"\x7fELF").unwrap();
        assert_eq!(
            resolve_bundle_elf(&b2, None, None).unwrap(),
            b2.join("app.elf")
        );

        // Two loose ELFs -> refuse rather than guess.
        std::fs::write(b2.join("other.elf"), b"\x7fELF").unwrap();
        let err = resolve_bundle_elf(&b2, None, None).unwrap_err();
        assert!(err.contains("multiple"), "{err}");

        // Nothing at all -> the "no firmware ELF" message naming what was tried.
        let b3 = root.join("b3");
        std::fs::create_dir_all(&b3).unwrap();
        let err = resolve_bundle_elf(&b3, None, None).unwrap_err();
        assert!(err.contains("no firmware ELF"), "{err}");

        // A manifest wins over both probes.
        let b4 = root.join("b4");
        std::fs::create_dir_all(&b4).unwrap();
        std::fs::write(b4.join("stray.elf"), b"\x7fELF").unwrap();
        let m = parse_system_manifest(
            "schema_version: 1\nhw_info:\n  sku: E1M-V2N101\nslices:\n\
             - core_id: m33_sm\n  os: zephyr\n  status: ok\n  build_dir: m33_sm-zephyr\n",
        )
        .unwrap();
        assert_eq!(
            resolve_bundle_elf(&b4, Some(&m), None).unwrap(),
            b4.join("m33_sm-zephyr").join("zephyr").join("zephyr.elf")
        );

        let _ = std::fs::remove_dir_all(&root);
    }
}
