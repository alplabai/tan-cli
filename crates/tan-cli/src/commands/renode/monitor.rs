// SPDX-License-Identifier: Apache-2.0
//! Drives Renode's monitor over the child's stdin/stdout for `tan renode
//! --sim-mode`. Plumbing ONLY — every decision about what a monitor line means
//! is [`tan_core::renode::sim::classify_monitor_line`], which is unit-tested
//! there; this file owns the pipes, the reader thread and the deadline.
//!
//! Ported from the retired Python `RenodeMonitor`
//! (`scripts/west_commands/alp_renode.py`, deleted in `alp-sdk@df312cec`).
//!
//! The pipes are GENERIC (`W: Write`, `R: Read`) rather than `ChildStdin`/
//! `ChildStdout` for one reason: every studio command funnels through
//! [`RenodeMonitor::command`], so a wrong sentinel, a wrong `broken` latch or a
//! wrong deadline silently mis-answers all of them — and with the concrete child
//! types hardcoded, none of that was reachable from a test without a real Renode
//! install. The tests below drive it over a loopback socket pair instead.

use std::io::{BufReader, Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use tan_core::renode::renode_cpu_halted;
use tan_core::renode::sim::{MonitorLine, classify_monitor_line};

use super::pump_lossy_lines;

/// Per-command deadline. A wedged-but-alive Renode must not strand a client.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
/// Total budget for the boot drain, split across [`DRAIN_ATTEMPTS`].
const DRAIN_TIMEOUT: Duration = Duration::from_secs(90);
/// On a loaded runner the pinned dotnet Renode's monitor can be slow to first
/// respond, and a single lost sync would strand the whole session.
const DRAIN_ATTEMPTS: u32 = 3;
/// Every failure path reports this. Reused for a poisoned lock: a client thread
/// that panicked mid-command may have left unread lines in the channel, so the
/// next command could otherwise capture *its* output — indistinguishable from a
/// correct reply, which is the one outcome worth refusing outright.
const UNUSABLE: &str = "Renode monitor is unusable after an earlier failure.";

struct MonitorInner<W: Write> {
    stdin: W,
    rx: mpsc::Receiver<String>,
    seq: u64,
    /// Latched by a timeout / EOF / write failure. Once set, every `command`
    /// fails fast rather than returning another command's stale output.
    broken: bool,
}

/// A serialised bridge to Renode's monitor. `command` writes the line plus an
/// `echo "<sentinel>"` marker and drains stdout until the bare sentinel comes
/// back. The reader thread + channel is what lets the per-command deadline
/// actually fire — a bare blocking read would hang forever on a wedged Renode.
pub(super) struct RenodeMonitor<W: Write> {
    inner: Mutex<MonitorInner<W>>,
    /// Latched by the pump thread, NOT by a command's collection window: Renode's
    /// stdout is consumed here in full, so a `CPU was halted` line emitted
    /// between two client commands (or after the last one) belongs to no window
    /// at all. Checked on the way past, it is caught wherever it lands — the
    /// plain smoke's `renode.cpu-halted` latch (`super::run`, issue #64) exists
    /// for the same reason: Renode halts the CPU on its first instruction fetch
    /// and still exits 0, so nothing else distinguishes it from a healthy run.
    /// The sim path is precisely where a mis-seeded VTOR shows up this way.
    cpu_halted: Arc<AtomicBool>,
}

impl<W: Write + Send> RenodeMonitor<W> {
    /// Take ownership of the child's stdio and start pumping stdout. In
    /// production `W`/`R` are the child's `ChildStdin`/`ChildStdout`.
    pub(super) fn new<R: Read + Send + 'static>(stdin: W, stdout: R) -> Self {
        // Two hops: the shared lossy-line pump, then a forwarder that inspects
        // every line for the halt marker before handing it on. The forwarder is
        // what makes the latch window-independent; keeping the pump untouched is
        // what keeps this file's line-framing identical to the plain smoke's.
        let (raw_tx, raw_rx) = mpsc::channel::<String>();
        thread::spawn(move || pump_lossy_lines(BufReader::new(stdout), raw_tx));
        let (tx, rx) = mpsc::channel::<String>();
        let cpu_halted = Arc::new(AtomicBool::new(false));
        let latch = Arc::clone(&cpu_halted);
        // Both senders drop when their thread ends, and the forwarder ends when
        // the pump does, so `Disconnected` on `rx` still IS stdout EOF.
        thread::spawn(move || {
            for line in raw_rx {
                if renode_cpu_halted(&line) {
                    latch.store(true, Ordering::Relaxed);
                }
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        Self {
            inner: Mutex::new(MonitorInner {
                stdin,
                rx,
                seq: 0,
                broken: false,
            }),
            cpu_halted,
        }
    }

    fn lock(&self) -> Result<MutexGuard<'_, MonitorInner<W>>, String> {
        self.inner.lock().map_err(|_| UNUSABLE.to_string())
    }

    /// Whether Renode ever reported the CPU halted, wherever that line landed.
    /// Read at teardown: the halt does not fail the command it happens to
    /// interleave with, it fails the RUN.
    pub(super) fn cpu_halted(&self) -> bool {
        self.cpu_halted.load(Ordering::Relaxed)
    }

    /// Run one monitor command and return its captured output.
    pub(super) fn command(&self, cmd: &str) -> Result<String, String> {
        self.command_within(cmd, COMMAND_TIMEOUT)
    }

    fn command_within(&self, cmd: &str, timeout: Duration) -> Result<String, String> {
        let mut inner = self.lock()?;
        if inner.broken {
            return Err(UNUSABLE.to_string());
        }
        inner.seq += 1;
        let sentinel = format!("__ALP_SIM_DONE_{}__", inner.seq);
        // The marker is QUOTED on purpose: Renode >= 1.16 reads a bare
        // `echo TOKEN` as an element lookup ("No such emulation element").
        let write = writeln!(inner.stdin, "{cmd}")
            .and_then(|()| writeln!(inner.stdin, "echo \"{sentinel}\""))
            .and_then(|()| inner.stdin.flush());
        if let Err(e) = write {
            inner.broken = true;
            return Err(format!("Renode monitor write failed for {cmd:?}: {e}"));
        }

        let deadline = Instant::now() + timeout;
        let mut out: Vec<String> = Vec::new();
        let mut errors: Vec<String> = Vec::new();
        loop {
            let now = Instant::now();
            let remaining = deadline.checked_duration_since(now).unwrap_or_default();
            let line = match inner.rx.recv_timeout(remaining) {
                Ok(line) => line,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    inner.broken = true;
                    return Err(format!(
                        "timed out after {}s awaiting Renode response to {cmd:?}.",
                        timeout.as_secs()
                    ));
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    inner.broken = true;
                    return Err(format!(
                        "Renode monitor closed while awaiting response to {cmd:?}."
                    ));
                }
            };
            match classify_monitor_line(&line, &sentinel, cmd) {
                // Reaching the sentinel with errors collected does NOT latch
                // `broken`: the monitor itself is fine, this one command failed.
                MonitorLine::Done if !errors.is_empty() => {
                    return Err(format!(
                        "Renode reported an error for {cmd:?}: {}",
                        errors.join(" | ")
                    ));
                }
                MonitorLine::Done => return Ok(out.join("\n")),
                MonitorLine::Error => errors.push(line.trim().to_string()),
                MonitorLine::Ignore => {}
                MonitorLine::Output => out.push(line),
            }
        }
    }

    /// Swallow the boot-time monitor output so the first real client command
    /// gets a clean reply. Retried: each attempt sends a fresh `version` with
    /// its own sentinel, which re-syncs, and clears the `broken` latch the
    /// previous attempt's timeout set.
    pub(super) fn drain_boot(&self) -> Result<(), String> {
        let per = std::cmp::max(
            Duration::from_secs(10),
            DRAIN_TIMEOUT / DRAIN_ATTEMPTS.max(1),
        );
        let mut last = format!("drain_boot failed after {DRAIN_ATTEMPTS} attempts");
        for _ in 0..DRAIN_ATTEMPTS {
            self.lock()?.broken = false;
            match self.command_within("version", per) {
                Ok(_) => return Ok(()),
                Err(e) => last = e,
            }
        }
        Err(last)
    }

    /// Best-effort `quit` on teardown. This only ASKS; whether Renode gets time
    /// to shut its emulation down depends on the caller polling for the exit
    /// afterwards (`sim::teardown` does, briefly) — the Python's
    /// `terminate()` + `wait(10)` + `kill` is what this pairs with.
    pub(super) fn quit(&self) {
        if let Ok(mut inner) = self.lock() {
            let _ = writeln!(inner.stdin, "quit");
            let _ = inner.stdin.flush();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufRead;
    use std::net::{TcpListener, TcpStream};

    /// A monitor wired to a loopback socket pair, with the FAKE-Renode end
    /// returned. A socket (not a spawned shell child, as `super::run_renode`'s
    /// tests use) because the monitor WRITES before it reads: a `cmd /C echo …`
    /// child can exit before that write lands, and the resulting broken pipe
    /// would latch `broken` at random. A socket the test holds open cannot
    /// race, and gives the missing-sentinel TIMEOUT — as opposed to EOF — a
    /// deterministic shape.
    fn monitor_on_a_socket() -> (RenodeMonitor<TcpStream>, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let fake = TcpStream::connect(addr).unwrap();
        let (tan_side, _) = listener.accept().unwrap();
        let write_half = tan_side.try_clone().unwrap();
        (RenodeMonitor::new(write_half, tan_side), fake)
    }

    /// The happy path, plus the exact bytes the monitor puts on the wire. The
    /// first command's sentinel is deterministically `__ALP_SIM_DONE_1__`, so a
    /// fake can answer without seeing the request.
    #[test]
    fn a_command_returns_the_output_collected_before_the_sentinel() {
        let (monitor, fake) = monitor_on_a_socket();
        let mut w = fake.try_clone().unwrap();
        w.write_all(b"foo\n__ALP_SIM_DONE_1__\n").unwrap();
        w.flush().unwrap();

        assert_eq!(monitor.command("get thing").unwrap(), "foo");

        // The command line, then the QUOTED marker: Renode >= 1.16 reads a bare
        // `echo TOKEN` as an element lookup, so the quotes are contract.
        let mut r = BufReader::new(fake);
        let (mut first, mut second) = (String::new(), String::new());
        r.read_line(&mut first).unwrap();
        r.read_line(&mut second).unwrap();
        assert_eq!(first.trim_end(), "get thing");
        assert_eq!(second.trim_end(), "echo \"__ALP_SIM_DONE_1__\"");
    }

    /// A sentinel that never comes must hit the DEADLINE (not block forever),
    /// and must latch `broken` — otherwise the next command would collect this
    /// command's late output and answer it as its own.
    #[test]
    fn a_missing_sentinel_times_out_and_latches_the_monitor_broken() {
        let (monitor, mut fake) = monitor_on_a_socket();
        // Chatter but no sentinel, and the socket stays OPEN: this is the
        // wedged-but-alive Renode, distinct from the EOF path.
        fake.write_all(b"12:00:00.1 [INFO] machine: started\n")
            .unwrap();
        fake.flush().unwrap();

        let e = monitor
            .command_within("version", Duration::from_millis(300))
            .unwrap_err();
        assert!(e.starts_with("timed out after"), "{e}");
        assert!(e.contains("\"version\""), "{e}");

        assert_eq!(monitor.command("version").unwrap_err(), UNUSABLE);
        drop(fake); // held open for the whole timeout, on purpose
    }

    /// An `[ERROR]` arriving BEFORE the sentinel must surface as the command's
    /// failure instead of being reported as empty-output success — and must NOT
    /// latch `broken`, because the monitor itself is fine.
    #[test]
    fn an_error_before_the_sentinel_fails_that_command_only() {
        let (monitor, mut fake) = monitor_on_a_socket();
        fake.write_all(b"12:00:00.1 [ERROR] sysbus: no peripheral at 0x0\n__ALP_SIM_DONE_1__\n")
            .unwrap();
        fake.flush().unwrap();

        let e = monitor.command("sysbus WriteByte 0x0 0x1").unwrap_err();
        assert!(e.starts_with("Renode reported an error"), "{e}");
        assert!(e.contains("no peripheral at 0x0"), "{e}");

        // The session survives: the next command gets sentinel 2 and works.
        fake.write_all(b"1.16.1\n__ALP_SIM_DONE_2__\n").unwrap();
        fake.flush().unwrap();
        assert_eq!(monitor.command("version").unwrap(), "1.16.1");
    }

    /// Issue #64's latch, on the sim path: the halt line belongs to NO command's
    /// collection window here, and must still be caught.
    #[test]
    fn a_cpu_halt_outside_any_command_window_is_still_latched() {
        let (monitor, mut fake) = monitor_on_a_socket();
        assert!(!monitor.cpu_halted());
        fake.write_all(
            b"cpu: PC does not lay in memory or PC and SP are equal to zero. \
              CPU was halted.\n",
        )
        .unwrap();
        fake.flush().unwrap();

        // No command is ever issued — the pump thread is the only reader.
        let mut latched = false;
        for _ in 0..200 {
            if monitor.cpu_halted() {
                latched = true;
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            latched,
            "a halt landing between commands must still fail the run"
        );
        drop(fake);
    }
}
