// SPDX-License-Identifier: Apache-2.0
//! `tan renode` — boot the built system-manifest's single Zephyr slice in
//! headless Renode as a no-hardware smoke test. The IO/subprocess side of the
//! port; every pre-flight *decision* (SKU→descriptor, slice selection, argv)
//! lives in `tan_core::renode`. Faithful to the retired `west alp-renode`
//! (`scripts/west_commands/alp_renode.py`); under ADR-0020 Phase 4 `tan` owns
//! this and the Python is retired.
//!
//! SCOPE: the PLAIN (non-sim) headless smoke only. `--sim-mode` (the studio sim
//! gateway, issue #674) is DEFERRED — the flag is wired for surface stability
//! but errors "not yet ported".
//!
//! Divergences from the Python source, all deliberate:
//!   - user-facing prose names `tan renode` / `tan build`, never
//!     `west alp-renode` / `alp build` (repo rule: the binary is `tan`).
//!   - a `schema_version != 1` manifest is a `ValidationFailure` (exit 2) — the
//!     one new guard tan adds; Python had none (every failure was exit 1).
//!   - `--timeout` is enforced even on a SILENT child (reader thread + deadline),
//!     fixing the Python latent hang where a wedged-but-alive Renode blocked the
//!     `for line in proc.stdout` read until EOF.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tan_core::renode::{
    build_renode_argv, platform_files_for_sku, platform_stem_for_sku, select_sku,
    zephyr_elf_from_manifest,
};
use tan_core::system_manifest::{SystemManifestError, parse_system_manifest};

use super::CommandRun;
use crate::cli::{GlobalArgs, RenodeArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{cli_workspace_root, normalize_path, resolve_sdk_root};

/// The `tan renode` JSON `data` payload.
#[derive(Serialize, Default)]
#[serde(rename_all = "camelCase")]
struct RenodeReport {
    /// Resolved SoM SKU driving descriptor selection.
    sku: String,
    /// Renode platform-descriptor stem (e.g. `alif_ensemble_e8`).
    platform_stem: String,
    /// Resolved `.repl` descriptor path.
    repl: String,
    /// Resolved `.resc` descriptor path.
    resc: String,
    /// Resolved Zephyr ELF path.
    elf: String,
    /// Tee log path.
    log_path: String,
    /// Wall-clock cap in seconds.
    timeout: u64,
    /// The `--expect` substring, when given.
    expect: Option<String>,
    /// Whether the `--expect` substring was observed (false when no `--expect`).
    expect_found: bool,
    /// The exact headless Renode argv that ran (empty on a pre-flight failure).
    renode_argv: Vec<String>,
}

/// `tan renode` entry.
pub fn run(g: &GlobalArgs, args: &RenodeArgs) -> CommandRun {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let app_path = normalize_path(&cwd.join(&args.app_path));
    let board_yaml = app_path.join("board.yaml");
    let project = Project {
        root: Some(app_path.to_string_lossy().into_owned()),
        board_yaml: board_yaml
            .is_file()
            .then(|| board_yaml.to_string_lossy().into_owned()),
    };

    let build_root = match args.build_root.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                normalize_path(p)
            } else {
                normalize_path(&cwd.join(p))
            }
        }
        None => app_path.join("build"),
    };
    let log_path = match args.log.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                normalize_path(p)
            } else {
                normalize_path(&cwd.join(p))
            }
        }
        None => build_root.join("renode.log"),
    };

    let mut report = RenodeReport {
        log_path: log_path.to_string_lossy().into_owned(),
        timeout: args.timeout,
        expect: args.expect.clone(),
        ..Default::default()
    };

    // --sim-mode: DEFERRED. Wired for surface stability; errors until studio is
    // repointed to `tan renode --sim-mode` (issue #674).
    if args.sim_mode {
        return fail(
            g,
            project,
            report,
            "renode.sim-mode-unported",
            "renode --sim-mode is not yet ported (studio sim gateway, issue #674); \
             use the plain headless smoke",
        );
    }

    // SDK-root guard — faithful to `find_sdk_root` None → die.
    let Some(sdk_root) = resolve_sdk_root(g, &cli_workspace_root(g)) else {
        return fail(
            g,
            project,
            report,
            "renode.sdk-root-not-found",
            "Cannot locate alp-sdk root.",
        );
    };

    // Load + version-guard the manifest. schema_version != 1 is the one new guard
    // (ValidationFailure 2); every other failure stays RuntimeFailure 1.
    let manifest_path = build_root.join("system-manifest.yaml");
    let manifest = match std::fs::read_to_string(&manifest_path) {
        Ok(yaml) => match parse_system_manifest(&yaml) {
            Ok(m) => m,
            Err(e @ SystemManifestError::UnsupportedSchemaVersion { .. }) => {
                return fail_with(
                    g,
                    project,
                    report,
                    "renode.manifest-schema",
                    &format!("{}: {e}", manifest_path.display()),
                    ExitCode::ValidationFailure,
                );
            }
            Err(e) => {
                return fail(
                    g,
                    project,
                    report,
                    "renode.manifest-invalid",
                    &format!("{}: {e}", manifest_path.display()),
                );
            }
        },
        Err(e) => {
            return fail(
                g,
                project,
                report,
                "renode.manifest-unavailable",
                &format!(
                    "no system-manifest.yaml at {}; run `tan build {}` first ({e}).",
                    manifest_path.display(),
                    app_path.display()
                ),
            );
        }
    };

    let sku = match select_sku(&manifest, args.board.as_deref()) {
        Ok(s) => s,
        Err(e) => return fail(g, project, report, "renode.sku-unresolved", &e.to_string()),
    };
    report.sku = sku.clone();

    let elf = match zephyr_elf_from_manifest(&manifest, &build_root) {
        Ok(p) => p,
        Err(e) => return fail(g, project, report, "renode.slice", &e.to_string()),
    };
    if !elf.is_file() {
        return fail(
            g,
            project,
            report,
            "renode.elf-missing",
            &format!(
                "Zephyr ELF not found at {}; run `tan build {}` first.",
                elf.display(),
                app_path.display()
            ),
        );
    }
    report.elf = elf.to_string_lossy().into_owned();

    let (repl, resc) = match platform_files_for_sku(&sku, &sdk_root) {
        Ok(pair) => pair,
        Err(e) => return fail(g, project, report, "renode.descriptor", &e.to_string()),
    };
    for descriptor in [&repl, &resc] {
        if !descriptor.is_file() {
            return fail(
                g,
                project,
                report,
                "renode.descriptor-missing",
                &format!("missing Renode descriptor {}.", descriptor.display()),
            );
        }
    }
    report.platform_stem = platform_stem_for_sku(&sku).unwrap_or_default();
    report.repl = repl.to_string_lossy().into_owned();
    report.resc = resc.to_string_lossy().into_owned();

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

    let mut issues: Vec<Issue> = Vec::new();
    let mut text: Vec<String> = Vec::new();
    if let Some(bundle) = args.image_bundle.as_deref() {
        let msg = format!(
            "renode: --image-bundle {bundle} accepted but unused by the single-slice smoke."
        );
        issues.push(issue("renode.image-bundle-unused", "info", &msg));
        if !g.is_json() {
            text.push(msg);
        }
    }

    let argv = build_renode_argv(&renode_bin, &repl, &resc, &elf);
    report.renode_argv = argv.clone();

    if !g.is_json() {
        text.push(format!(
            "renode: booting {} on {} (log -> {})",
            elf.display(),
            repl.file_name()
                .map(|n| n.to_string_lossy())
                .unwrap_or_default(),
            log_path.display()
        ));
    }

    // Run — enforce the wall-clock timeout even on a silent child.
    let found = match run_renode(
        &argv,
        &log_path,
        args.timeout,
        args.expect.as_deref(),
        !g.is_json(),
    ) {
        Ok(found) => found,
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
    report.expect_found = found;

    let mut exit = ExitCode::Success;
    if let Some(expect) = args.expect.as_deref() {
        if !found {
            exit = ExitCode::RuntimeFailure;
            let msg = format!(
                "renode: console did not contain {expect:?} within {}s (see {}).",
                args.timeout,
                log_path.display()
            );
            issues.push(issue("renode.expect-not-found", "error", &msg));
            if !g.is_json() {
                text.push(msg);
            }
        }
    }

    finish(g, project, report, text, issues, exit)
}

/// Resolve the `renode` executable path via `where`/`which`, returning the first
/// path line (argv[0]); `None` when it's not on PATH — the explicit non-zero
/// exit path, never a silent pass.
fn resolve_renode_binary() -> Option<String> {
    let resolver = if cfg!(windows) { "where" } else { "which" };
    let out = Command::new(resolver).arg("renode").output().ok()?;
    if !out.status.success() {
        return None;
    }
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(str::to_string)
}

/// Run Renode, tee-ing its (stdout+stderr) console to `log_path`. Terminates on:
/// the `expect` marker appearing, the child exiting (EOF), or `timeout_s`
/// elapsing — the deadline fires even on a silent child via a reader thread +
/// `recv_timeout` (the Python `for line in proc.stdout` blocked until EOF).
/// Returns whether the `expect` marker was seen (always false when `expect` is
/// `None`). `echo_stdout` tees each line to the CLI's own stdout (text mode
/// only — in JSON mode it would corrupt the single-line Envelope).
fn run_renode(
    argv: &[String],
    log_path: &Path,
    timeout_s: u64,
    expect: Option<&str>,
    echo_stdout: bool,
) -> std::io::Result<bool> {
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut logf = std::fs::File::create(log_path)?;

    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    // One channel fed by a reader thread per pipe (stderr merged into stdout,
    // matching the Python STDOUT redirect).
    let (tx, rx) = mpsc::channel::<String>();
    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    let tx_err = tx.clone();
    let h_out = thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let h_err = thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            if tx_err.send(line).is_err() {
                break;
            }
        }
    });

    // Read loop in a closure so teardown (kill + reap + join) ALWAYS runs — even
    // if a log write fails mid-run, we must not leak the Renode child + threads.
    let read_result = (|| -> std::io::Result<bool> {
        let deadline = Instant::now() + Duration::from_secs(timeout_s);
        let mut found = false;
        loop {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            match rx.recv_timeout(deadline - now) {
                Ok(line) => {
                    writeln!(logf, "{line}")?;
                    logf.flush()?;
                    if echo_stdout {
                        println!("{line}");
                    }
                    if let Some(exp) = expect {
                        if line.contains(exp) {
                            found = true;
                            break;
                        }
                    }
                }
                // Timeout (silent child) or both readers done (child EOF) — stop.
                Err(_) => break,
            }
        }
        Ok(found)
    })();

    // Teardown: kill + reap the child; the reader threads then observe EOF.
    let _ = child.kill();
    let _ = child.wait();
    let _ = h_out.join();
    let _ = h_err.join();

    read_result
}

/// An issue of the given severity.
fn issue(code: &str, severity: &str, message: &str) -> Issue {
    Issue {
        code: code.to_string(),
        severity: severity.to_string(),
        message: message.to_string(),
    }
}

/// A `RuntimeFailure` (exit 1) error `CommandRun`.
fn fail(
    g: &GlobalArgs,
    project: Project,
    report: RenodeReport,
    code: &str,
    message: &str,
) -> CommandRun {
    fail_with(g, project, report, code, message, ExitCode::RuntimeFailure)
}

/// An error `CommandRun` with an explicit exit class (text or JSON per format).
fn fail_with(
    g: &GlobalArgs,
    project: Project,
    report: RenodeReport,
    code: &str,
    message: &str,
    exit: ExitCode,
) -> CommandRun {
    let issues = vec![issue(code, "error", message)];
    let text = vec![format!("renode: {message}")];
    finish(g, project, report, text, issues, exit)
}

/// Assemble the `CommandRun`: one JSON envelope in JSON mode, else the text lines.
fn finish(
    g: &GlobalArgs,
    project: Project,
    report: RenodeReport,
    text: Vec<String>,
    issues: Vec<Issue>,
    exit: ExitCode,
) -> CommandRun {
    if g.is_json() {
        let json = Envelope::new("renode", project, report, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit,
            text,
            json: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fake echo child that prints the given lines then exits — stands in for
    /// Renode so `run_renode`'s tee/expect/timeout logic is testable without an
    /// install. Uses the host shell (`sh -c` / `cmd /C`) to echo.
    fn echo_argv(lines: &[&str]) -> Vec<String> {
        if cfg!(windows) {
            let mut a = vec!["cmd".to_string(), "/C".to_string()];
            a.push(
                lines
                    .iter()
                    .map(|l| format!("echo {l}"))
                    .collect::<Vec<_>>()
                    .join(" & "),
            );
            a
        } else {
            let script = lines
                .iter()
                .map(|l| format!("echo '{l}'"))
                .collect::<Vec<_>>()
                .join("; ");
            vec!["sh".to_string(), "-c".to_string(), script]
        }
    }

    fn tmp_log(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-renode-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join("renode.log")
    }

    #[test]
    fn expect_marker_present_returns_found_and_writes_log() {
        let log = tmp_log("hit");
        let argv = echo_argv(&["boot", "[hello] done", "tail"]);
        let found = run_renode(&argv, &log, 30, Some("[hello] done"), true).unwrap();
        assert!(found);
        let contents = std::fs::read_to_string(&log).unwrap();
        assert!(contents.contains("[hello] done"), "{contents}");
        let _ = std::fs::remove_dir_all(log.parent().unwrap());
    }

    #[test]
    fn expect_marker_absent_returns_not_found() {
        let log = tmp_log("miss");
        let argv = echo_argv(&["boot", "running", "tail"]);
        let found = run_renode(&argv, &log, 30, Some("[hello] done"), false).unwrap();
        assert!(!found);
        let _ = std::fs::remove_dir_all(log.parent().unwrap());
    }

    #[test]
    fn no_expect_always_returns_false_and_still_tees() {
        let log = tmp_log("noexpect");
        let argv = echo_argv(&["one", "two"]);
        let found = run_renode(&argv, &log, 30, None, false).unwrap();
        assert!(!found);
        let contents = std::fs::read_to_string(&log).unwrap();
        assert!(contents.contains("one"), "{contents}");
        let _ = std::fs::remove_dir_all(log.parent().unwrap());
    }
}
