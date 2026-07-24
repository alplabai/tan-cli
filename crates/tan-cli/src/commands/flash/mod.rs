// SPDX-License-Identifier: Apache-2.0
//! `tan flash` — walk `build/system-manifest.yaml` and program every slice +
//! helper MCU onto attached hardware in `boot_order`. The IO/subprocess half of
//! the native port of `west alp-flash` (`scripts/west_commands/alp_flash.py` +
//! `scripts/flash_backends/*`), retiring the west extension under ADR-0020
//! Phase 4. Every argv/decision/message is pure in `tan_core::flash`; this file
//! only resolves paths, probes PATH, spawns subprocesses, and materialises the
//! J-Link Commander temp file.
//!
//! Per-entry rc convention mirrors `alp_flash._flash_entry` exactly: `0` success
//! / clean-dry-run / clean-skip-via-flag, `-1` silently skipped (no flash_method
//! / tools missing under `--skip-missing-tools` / an unresolved `TBD` value in
//! `flash_args`), `>0` failed. `failed` counts
//! only `rc > 0`; skipped entries never count. Within rc 0, `status` further
//! distinguishes a real/dry-run "ok" from a "planned" entry (the yocto_wic/xspi
//! confirm gate declining a REAL write — nothing was programmed), so a JSON
//! consumer can't mistake a no-op for a completed flash.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

use tan_core::flash::{
    BackendKind, FlashInputs, FlashPlan, FlashTarget, ToolGate, backend_for, flash_args_has_tbd,
    plan_baremetal_cmake_flash, plan_flash_targets, plan_swd_probe, plan_xspi_flashwriter,
    plan_yocto_wic, plan_zephyr_west_flash, registry_keys, resolve_artefact_path, tool_gate,
};
use tan_core::system_manifest::parse_system_manifest;

use super::CommandRun;
use crate::cli::{FlashArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{
    cli_workspace_root, command_on_path, resolve_cli_project_context, resolve_sdk_root,
};

/// One entry's result in the envelope `data`.
#[derive(Debug, Clone, Serialize)]
struct FlashEntry {
    /// `slice` | `helper`.
    kind: String,
    /// `core_id` (slice) or `name` (helper).
    id: String,
    /// The `flash_method`, when present.
    #[serde(skip_serializing_if = "Option::is_none")]
    method: Option<String>,
    /// `ok` | `skipped` | `planned` | `failed`.
    status: String,
    /// Internal per-entry rc (`-1` skip / `0` ok / `>0` fail).
    rc: i32,
    /// Human-readable outcome/reason.
    message: String,
}

/// The `flash` envelope payload.
#[derive(Debug, Clone, Serialize)]
struct FlashData {
    #[serde(rename = "schemaVersion")]
    schema_version: &'static str,
    #[serde(rename = "buildRoot")]
    build_root: String,
    entries: Vec<FlashEntry>,
}

/// `tan flash` entry.
pub fn run(g: &GlobalArgs, args: &FlashArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    // SDK root is required (faithful: the Python `find_sdk_root() is None` die).
    let sdk_root = match resolve_sdk_root(g, &cli_workspace_root(g)) {
        Some(r) => r,
        None => {
            return error_run(
                g,
                project,
                String::new(),
                "flash.sdk-root-not-found",
                "Cannot locate alp-sdk root.".to_string(),
            );
        }
    };

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let app_path = cwd.join(&args.app_path);
    let build_root = match args.build_root.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                cwd.join(p)
            }
        }
        None => app_path.join("build"),
    };
    let build_root_str = build_root.to_string_lossy().into_owned();

    let manifest_path = build_root.join("system-manifest.yaml");
    if !manifest_path.is_file() {
        return error_run(
            g,
            project,
            build_root_str,
            "flash.manifest-not-found",
            format!(
                "system-manifest.yaml not found at {}; run `tan build --project {}` first.",
                manifest_path.display(),
                args.app_path
            ),
        );
    }
    let yaml = match std::fs::read_to_string(&manifest_path) {
        Ok(y) => y,
        Err(e) => {
            return error_run(
                g,
                project,
                build_root_str,
                "flash.manifest-not-found",
                format!("{}: {e}", manifest_path.display()),
            );
        }
    };
    let manifest = match parse_system_manifest(&yaml) {
        Ok(m) => m,
        Err(e) => {
            return error_run(
                g,
                project,
                build_root_str,
                "flash.manifest-invalid",
                format!("{}: {e}", manifest_path.display()),
            );
        }
    };

    let sku = manifest.hw_info.sku.clone();
    let force_confirm = std::env::var("ALP_FLASH_FORCE").as_deref() == Ok("1");
    let (targets, warnings, refused) =
        plan_flash_targets(&manifest, args.core.as_deref(), args.helper.as_deref());

    let mut text: Vec<String> = Vec::new();
    let mut issues: Vec<Issue> = Vec::new();
    let mut entries: Vec<FlashEntry> = Vec::new();
    // Seeded with the status-refused slices: they never become a
    // `FlashTarget`/loop iteration below, so they cannot increment `failed`
    // there -- but a slice `tan build` reports non-`ok` must still fail the
    // overall run (`ok:false`/exit 1), not disappear into a clean exit.
    let mut failed = refused.len();
    let mut flashed_anything = false;

    for w in &warnings {
        text.push(w.clone());
        issues.push(Issue {
            code: "flash.boot-order-unknown-core".to_string(),
            severity: "warning".to_string(),
            message: w.clone(),
        });
    }
    for r in &refused {
        text.push(r.clone());
        // error, not warning: `plan_flash_targets` refused to select this
        // slice's (possibly stale) artefact for flashing at all -- see its
        // doc comment. Never silent, and `ok` must disagree with a green
        // exit code here exactly like a spawned flash failure does below.
        issues.push(Issue {
            code: "flash.slice-not-built".to_string(),
            severity: "error".to_string(),
            message: r.clone(),
        });
    }

    for t in &targets {
        let (rc, entry, lines) =
            flash_entry(g, t, &sku, &build_root, &sdk_root, force_confirm, args);
        text.extend(lines);
        // A failed entry used to land only in `data.entries[].message`; `issues`
        // is the channel `--format json` consumers (the vscode extension) key
        // error rendering off, and `build` already treats a failed step this
        // way (`build.slice-failed`) — mirror it so `ok:false` never ships with
        // an empty issues list.
        if rc > 0 {
            issues.push(Issue {
                code: "flash.entry-failed".to_string(),
                severity: "error".to_string(),
                message: entry.message.clone(),
            });
        }
        // A confirm-gated no-op (yocto_wic/xspi, unconfirmed) is reported with
        // its own "planned" status instead of "ok" (see flash_entry); surface
        // the reason as a warning issue too, since `status` alone is prose no
        // automated consumer parses.
        if entry.status == "planned" {
            issues.push(Issue {
                code: "flash.confirm-required".to_string(),
                severity: "warning".to_string(),
                message: entry.message.clone(),
            });
        }
        entries.push(entry);
        if rc < 0 {
            continue; // silently skipped — not counted, doesn't set flashed_anything
        }
        flashed_anything = true;
        if rc > 0 {
            failed += 1;
        }
    }

    if !flashed_anything && refused.is_empty() {
        // A refused (non-`ok`-status) slice DID match the requested filters —
        // it was refused, not absent — so "nothing matched" would be a
        // misleading second message on top of the flash.slice-not-built
        // issue(s) already pushed above.
        let msg = "flash: nothing matched the requested filters.".to_string();
        text.push(msg.clone());
        // Same class of bug as above: a `--core`/`--helper` filter matching
        // nothing used to warn only in text mode, so `--format json` reported
        // `ok:true` with an empty `entries`/`issues` for a flash that never
        // touched a device.
        issues.push(Issue {
            code: "flash.nothing-matched".to_string(),
            severity: "warning".to_string(),
            message: msg,
        });
    }
    text.push(format!("flash: {failed} failure(s)."));

    let exit = if failed > 0 {
        ExitCode::RuntimeFailure
    } else {
        ExitCode::Success
    };

    if g.is_json() {
        let data = FlashData {
            schema_version: "1",
            build_root: build_root_str,
            entries,
        };
        let json = Envelope::new("flash", project, data, issues, exit.code()).to_json();
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

/// Dispatch + run one target. Returns `(rc, entry, text-lines)`.
fn flash_entry(
    g: &GlobalArgs,
    t: &FlashTarget,
    sku: &str,
    build_root: &Path,
    sdk_root: &Path,
    force_confirm: bool,
    args: &FlashArgs,
) -> (i32, FlashEntry, Vec<String>) {
    let kind = t.kind.as_str();
    let id = t.id.as_str();
    let mut lines: Vec<String> = Vec::new();

    // No flash_method -> silent skip (rc -1). A helper carrying
    // `update_channel` instead (alp-sdk#868: AEN's cc3501e_otp, applied
    // over the bridge SPI programming OTP) gets a clearer reason than the
    // generic "no flash_method" -- it was never meant to be a customer
    // flash target at all, not just one whose wiring isn't finalised yet.
    let method = match t.flash_method.as_deref().filter(|m| !m.is_empty()) {
        Some(m) => m,
        None => {
            let msg = match t.update_channel.as_deref().filter(|c| !c.is_empty()) {
                Some(channel) => format!(
                    "flash: {kind} '{id}' is Alp-OTA-updated (update_channel: {channel}), not a \
                     customer flash target; skipping"
                ),
                None => format!("flash: {kind} '{id}' has no flash_method; skipping"),
            };
            lines.push(msg.clone());
            return (-1, entry(t, None, "skipped", -1, msg), lines);
        }
    };

    // Unknown flash_method -> hard failure (rc 1).
    let meta = match backend_for(method) {
        Some(m) => m,
        None => {
            let msg = format!(
                "flash: {kind} '{id}' uses flash_method '{method}' which has no registered \
                 backend. Available: {:?}",
                registry_keys()
            );
            lines.push(msg.clone());
            return (1, entry(t, Some(method), "failed", 1, msg), lines);
        }
    };

    // A resolved backend with unresolved `flash_args` (the AEN801 cc3501e
    // helper's `mode: TBD, device: TBD`) is the SDK's documented pending
    // sentinel (issue #2's `TBD` convention -- see `image.rs`'s `TBD`
    // firmware_path skip and `sdk_catalogue::parse::is_tbd`), not a flash
    // failure: one helper whose args aren't finalised must never fail the
    // whole run and block the resolved slices. Checked before artefact
    // resolution / dispatch so it skips cleanly under both --dry-run and a
    // real run.
    if flash_args_has_tbd(&t.flash_args) {
        let msg = format!(
            "flash: {kind} '{id}' has an unresolved 'TBD' flash_arg (e.g. mode/device not \
             finalised); skipping"
        );
        lines.push(msg.clone());
        return (-1, entry(t, Some(method), "skipped", -1, msg), lines);
    }

    // Artefact resolution: output_artefact (slice) or firmware_path (helper).
    let artefact_str = t
        .output_artefact
        .as_deref()
        .filter(|s| !s.is_empty())
        .or_else(|| t.firmware_path.as_deref().filter(|s| !s.is_empty()));
    let artefact_owned;
    let artefact_str = match artefact_str {
        Some(s) => s,
        None => {
            if args.dry_run {
                artefact_owned = format!("<missing-artefact-for-{id}>");
                &artefact_owned
            } else {
                let msg = format!(
                    "flash: {kind} '{id}' has no output_artefact / firmware_path; can't flash."
                );
                lines.push(msg.clone());
                return (1, entry(t, Some(method), "failed", 1, msg), lines);
            }
        }
    };
    let artefact_path =
        resolve_artefact_path(artefact_str, build_root, Some(sdk_root), |p| p.is_file());

    // Required-tool gate (bypassed under --dry-run / empty requires).
    match tool_gate(
        meta.requires,
        args.dry_run,
        args.skip_missing_tools,
        kind,
        id,
        method,
        command_on_path,
    ) {
        ToolGate::Proceed => {}
        ToolGate::Skip(msg) => {
            lines.push(msg.clone());
            return (-1, entry(t, Some(method), "skipped", -1, msg), lines);
        }
        ToolGate::Fail(msg) => {
            lines.push(msg.clone());
            return (1, entry(t, Some(method), "failed", 1, msg), lines);
        }
    }

    let inp = FlashInputs {
        artefact: &artefact_path,
        flash_args: &t.flash_args,
        core_id: id,
        sku,
        dry_run: args.dry_run,
        force_confirm,
    };
    let plan = match dispatch_plan(meta.kind, &inp) {
        Ok(p) => p,
        Err(msg) => {
            lines.push(format!("flash: {kind} '{id}' -> {method}"));
            lines.push(format!("  FAIL: {msg}"));
            return (1, entry(t, Some(method), "failed", 1, msg), lines);
        }
    };

    lines.push(format!("flash: {kind} '{id}' -> {method}"));

    // Planning-only (yocto/xspi confirm gate) or --dry-run: show, don't spawn.
    if plan.planning_only || args.dry_run {
        let display = display_argv(&plan);
        if args.dry_run {
            // The user explicitly asked for a preview — nothing was ever going
            // to run. rc 0 / status "ok" (alp_flash's "clean-dry-run").
            let msg = format!("would run {display}");
            lines.push(format!("  {msg}"));
            return (0, entry(t, Some(method), "ok", 0, msg), lines);
        }
        // `plan.planning_only` without `--dry-run`: the BACKEND itself declined
        // a real write because the confirm gate isn't armed (yocto_wic/xspi:
        // `flash_args.confirm` is false and `ALP_FLASH_FORCE` isn't set) —
        // alp_flash's "clean-skip-via-flag". This used to report byte-identical
        // to a real write (`status:"ok"`), with the reason thrown away, so a
        // `--format json` consumer could not tell "nothing was written" from
        // "programmed the device". Keep rc 0 (the documented rc contract — this
        // IS a clean, non-error outcome) but give it a distinct status; `run()`
        // also turns it into a warning Issue.
        let msg = format!(
            "would run {display} -- NOT written: flash_args.confirm is false (set \
             ALP_FLASH_FORCE=1 or flash_args.confirm: true to actually flash)"
        );
        lines.push(format!("  {msg}"));
        return (0, entry(t, Some(method), "planned", 0, msg), lines);
    }

    // Real spawn.
    match execute(g, &plan) {
        Ok(outcome) if outcome.success => {
            lines.push(format!("  ok: {}", plan.ok_message));
            (0, entry(t, Some(method), "ok", 0, plan.ok_message), lines)
        }
        Ok(outcome) => {
            let msg = execute_message(g, &outcome, method, id);
            lines.push(format!("  FAIL: {msg}"));
            (1, entry(t, Some(method), "failed", 1, msg), lines)
        }
        Err(_) => {
            let msg = format!("{method}[{id}]: flash command failed");
            lines.push(format!("  FAIL: {msg}"));
            (1, entry(t, Some(method), "failed", 1, msg), lines)
        }
    }
}

/// Route a backend kind to its pure plan-builder.
fn dispatch_plan(kind: BackendKind, inp: &FlashInputs) -> Result<FlashPlan, String> {
    match kind {
        BackendKind::Swd => plan_swd_probe(inp, command_on_path),
        BackendKind::Zephyr => plan_zephyr_west_flash(inp),
        BackendKind::Cmake => plan_baremetal_cmake_flash(inp),
        BackendKind::YoctoWic => plan_yocto_wic(inp, command_on_path),
        BackendKind::Xspi => plan_xspi_flashwriter(inp),
    }
}

/// The would-run display string; J-Link plans show a `<generated.jlink>`
/// placeholder for the temp Commander script.
fn display_argv(plan: &FlashPlan) -> String {
    let mut parts: Vec<&str> = plan.argv.iter().map(String::as_str).collect();
    if plan.jlink_script.is_some() {
        parts.push("<generated.jlink>");
    }
    parts.join(" ")
}

/// Outcome of running a flash plan: success, plus — in JSON/capture mode only —
/// the process output captured by the SINGLE spawn, so the failure message
/// reuses it instead of re-running the hardware-programming tool (which would
/// re-flash the device on a first-attempt failure).
struct ExecOutcome {
    success: bool,
    captured: Option<std::process::Output>,
}

/// Spawn the plan and report success. Handles the three real shapes: a J-Link
/// plan (write the Commander script to a temp file, append its path), a
/// decompress→dd pipeline (a `"|"` token), and a plain single process.
fn execute(g: &GlobalArgs, plan: &FlashPlan) -> std::io::Result<ExecOutcome> {
    if let Some(pos) = plan.argv.iter().position(|a| a == "|") {
        let (left, right) = plan.argv.split_at(pos);
        return spawn_pipeline(g.is_json(), left, &right[1..]);
    }
    if let Some(script) = &plan.jlink_script {
        return spawn_jlink(g.is_json(), &plan.argv, script);
    }
    spawn_single(g.is_json(), &plan.argv)
}

/// A single process: capture in JSON mode (output kept for the failure message —
/// never re-spawned), inherit stdio (stream live) in text.
fn spawn_single(capture: bool, argv: &[String]) -> std::io::Result<ExecOutcome> {
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..]);
    if capture {
        let out = cmd.output()?;
        Ok(ExecOutcome {
            success: out.status.success(),
            captured: Some(out),
        })
    } else {
        Ok(ExecOutcome {
            success: cmd.status()?.success(),
            captured: None,
        })
    }
}

/// The J-Link path: materialise the Commander script to a temp file, append it
/// as the final `-CommanderScript` arg, spawn, and remove the temp file.
fn spawn_jlink(capture: bool, argv: &[String], script: &str) -> std::io::Result<ExecOutcome> {
    let path = jlink_temp_path();
    {
        let mut f = std::fs::File::create(&path)?;
        f.write_all(script.as_bytes())?;
    }
    let mut full = argv.to_vec();
    full.push(path.to_string_lossy().into_owned());
    let result = spawn_single(capture, &full);
    let _ = std::fs::remove_file(&path);
    result
}

/// A unique scratch path for the J-Link Commander script.
fn jlink_temp_path() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    std::env::temp_dir().join(format!("tan-flash-{}-{nanos}.jlink", std::process::id()))
}

/// A decompress→dd pipeline: wire the decompressor's stdout into dd's stdin.
/// Fails when either process fails (matches the Python pipeline rc folding).
fn spawn_pipeline(
    capture: bool,
    left: &[String],
    right: &[String],
) -> std::io::Result<ExecOutcome> {
    let mut lc = Command::new(&left[0]);
    lc.args(&left[1..]).stdout(Stdio::piped());
    if capture {
        lc.stderr(Stdio::piped());
    }
    let mut lchild = lc.spawn()?;
    let lout = lchild
        .stdout
        .take()
        .ok_or_else(|| std::io::Error::other("decompressor stdout unavailable"))?;

    // Drain the decompressor's stderr on a background thread for the pipeline's
    // lifetime. `lc.stderr(Stdio::piped())` above creates the pipe but nobody
    // was reading it: once the decompressor writes more than the OS pipe
    // buffer (64 KiB on Linux), its write() blocks forever, it never reaches
    // EOF on stdout either, `right`'s read() blocks too, and `lchild.wait()`
    // below never returns — a silent hang mid-write to a real block device.
    let stderr_drain = lchild.stderr.take().map(|mut e| {
        std::thread::spawn(move || {
            use std::io::Read;
            let mut buf = Vec::new();
            let _ = e.read_to_end(&mut buf);
            buf
        })
    });

    let mut rc = Command::new(&right[0]);
    rc.args(&right[1..]).stdin(Stdio::from(lout));
    // Capture dd's (the right/sink process) output for the failure tail — it is
    // the meaningful diagnostic; the decompressor rarely errors alone.
    let (right_ok, captured) = if capture {
        let out = rc.output()?;
        (out.status.success(), Some(out))
    } else {
        (rc.status()?.success(), None)
    };
    let left_ok = lchild.wait()?.success();
    if let Some(handle) = stderr_drain {
        let _ = handle.join();
    }
    Ok(ExecOutcome {
        success: right_ok && left_ok,
        captured,
    })
}

/// The failure message: in JSON mode reuse the output ALREADY captured by the
/// single spawn (never re-run the flash); in text mode the child already
/// streamed, so report the rc-style summary.
fn execute_message(g: &GlobalArgs, outcome: &ExecOutcome, method: &str, id: &str) -> String {
    if g.is_json() {
        if let Some(out) = &outcome.captured {
            if let Some(tail) = capture_tail(out) {
                return format!("{method}[{id}]: {tail}");
            }
        }
    }
    format!("{method}[{id}]: flash command failed")
}

/// The failure tail from the ALREADY-captured process output (JSON mode) — a
/// pure read, no second spawn. Returns the last 4 non-empty output lines joined
/// by " | ", or `None` when the process actually succeeded.
fn capture_tail(out: &std::process::Output) -> Option<String> {
    if out.status.success() {
        return None;
    }
    let mut text = String::from_utf8_lossy(&out.stderr).into_owned();
    if text.trim().is_empty() {
        text = String::from_utf8_lossy(&out.stdout).into_owned();
    }
    let tail: Vec<&str> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    if tail.is_empty() {
        Some(format!("exited rc={}", out.status.code().unwrap_or(-1)))
    } else {
        Some(tail.join(" | "))
    }
}

/// Build one `FlashEntry`.
fn entry(
    t: &FlashTarget,
    method: Option<&str>,
    status: &str,
    rc: i32,
    message: String,
) -> FlashEntry {
    FlashEntry {
        kind: t.kind.as_str().to_string(),
        id: t.id.clone(),
        method: method.map(str::to_string),
        status: status.to_string(),
        rc,
        message,
    }
}

/// A hard error `CommandRun` (exit 1), text or JSON per format.
fn error_run(
    g: &GlobalArgs,
    project: Project,
    build_root: String,
    code: &str,
    message: String,
) -> CommandRun {
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    if g.is_json() {
        let data = FlashData {
            schema_version: "1",
            build_root,
            entries: Vec::new(),
        };
        let json = Envelope::new(
            "flash",
            project,
            data,
            issues,
            ExitCode::RuntimeFailure.code(),
        )
        .to_json();
        CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: vec![format!("flash: {message}")],
            json: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-flash-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// A fake SDK carrying just the loader marker (enough for resolve_sdk_root).
    fn fake_sdk(root: &Path) {
        std::fs::create_dir_all(root.join("scripts")).unwrap();
        std::fs::write(root.join("scripts").join("alp_project.py"), "").unwrap();
    }

    fn global(sdk: &Path, format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: Some(sdk.to_string_lossy().into_owned()),
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    fn flash_args(app: &Path, build_root: &Path, dry_run: bool) -> FlashArgs {
        FlashArgs {
            app_path: app.to_string_lossy().into_owned(),
            build_root: Some(build_root.to_string_lossy().into_owned()),
            dry_run,
            core: None,
            helper: None,
            skip_missing_tools: false,
        }
    }

    #[test]
    fn missing_manifest_exits_one_with_issue() {
        let sdk = tmp("nomani-sdk");
        fake_sdk(&sdk);
        let app = tmp("nomani-app");
        let build_root = tmp("nomani-build"); // exists, no manifest
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, false));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["command"], "flash");
        assert_eq!(doc["ok"], false);
        assert_eq!(doc["issues"][0]["code"], "flash.manifest-not-found");
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn schema_version_two_exits_one() {
        let sdk = tmp("schema2-sdk");
        fake_sdk(&sdk);
        let app = tmp("schema2-app");
        let build_root = tmp("schema2-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 2\nhw_info:\n  sku: X\nslices: []\nhelper_mcus: []\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Text);
        let run = run(&g, &flash_args(&app, &build_root, false));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn core_matches_nothing_exits_zero_nothing_matched() {
        let sdk = tmp("nomatch-sdk");
        fake_sdk(&sdk);
        let app = tmp("nomatch-app");
        let build_root = tmp("nomatch-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: X\nslices:\n- core_id: m33\n  os: zephyr\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus: \
             []\nboot_order: []\n",
        )
        .unwrap();
        let mut a = flash_args(&app, &build_root, true);
        a.core = Some("does-not-exist".to_string());
        let g = global(&sdk, Format::Text);
        let run = run(&g, &a);
        assert_eq!(run.exit, ExitCode::Success);
        assert!(run.text.iter().any(|l| l.contains("nothing matched")));
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn dry_run_zephyr_ok_and_tbd_helper_fails() {
        let sdk = tmp("dry-sdk");
        fake_sdk(&sdk);
        let app = tmp("dry-app");
        let build_root = tmp("dry-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n- core_id: m55_hp\n  os: \
             zephyr\n  output_artefact: m55_hp-zephyr/zephyr/zephyr.elf\n  status: ok\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus:\n- \
             name: aen701_helper\n  chip: cc3501e\n  firmware_path: TBD\n  flash_method: TBD\n  \
             flash_args: TBD\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, true));
        // helper flash_method 'TBD' has no backend -> one failure -> exit 1.
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["command"], "flash");
        let entries = doc["data"]["entries"].as_array().unwrap();
        let zephyr = entries.iter().find(|e| e["id"] == "m55_hp").unwrap();
        assert_eq!(zephyr["status"], "ok");
        assert!(
            zephyr["message"]
                .as_str()
                .unwrap()
                .contains("would run west flash")
        );
        let helper = entries.iter().find(|e| e["id"] == "aen701_helper").unwrap();
        assert_eq!(helper["status"], "failed");
        // A failed entry used to be invisible in `issues` — only `ok:false`
        // and the free-text `data.entries[].message` said anything went wrong.
        let issue = doc["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["code"] == "flash.entry-failed");
        assert!(
            issue.is_some(),
            "a failed entry must surface a flash.entry-failed issue"
        );
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn tbd_flash_args_on_a_registered_backend_skips_not_fails() {
        // Regression: a helper MCU with a VALID, registered `flash_method`
        // (`swd_probe`) but unresolved `flash_args: {mode: TBD, device: TBD}`.
        // `TBD` is the SDK's documented pending/not-yet-resolved sentinel
        // (issue #2's convention -- `sdk_catalogue::parse::is_tbd`; `image.rs`
        // already treats a `TBD` firmware_path as a non-fatal Pending skip),
        // so this must be a skip, not a hard failure that takes down the
        // whole run (including the real, resolved m55 zephyr slice) and
        // forces users to pass `--core` to work around it.
        let sdk = tmp("tbd-args-sdk");
        fake_sdk(&sdk);
        let app = tmp("tbd-args-app");
        let build_root = tmp("tbd-args-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n- core_id: m55_he\n  os: \
             zephyr\n  output_artefact: m55_he-zephyr/zephyr/zephyr.elf\n  status: ok\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus:\n- \
             name: unresolved_helper\n  chip: gd32g553\n  firmware_path: firmware/helper.bin\n  \
             flash_method: swd_probe\n  flash_args:\n    mode: TBD\n    device: \
             TBD\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, true));
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        // The resolved m55 slice still plans/would-run; one unresolved helper
        // must not fail the run.
        assert_eq!(run.exit, ExitCode::Success, "got envelope: {doc}");
        assert_eq!(doc["ok"], true);
        let entries = doc["data"]["entries"].as_array().unwrap();
        let zephyr = entries.iter().find(|e| e["id"] == "m55_he").unwrap();
        assert_eq!(zephyr["status"], "ok");
        let helper = entries
            .iter()
            .find(|e| e["id"] == "unresolved_helper")
            .unwrap();
        assert_eq!(helper["status"], "skipped");
        assert_eq!(helper["rc"], -1);
        assert!(helper["message"].as_str().unwrap().contains("TBD"));
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn bare_scalar_tbd_flash_args_on_a_registered_backend_skips_not_fails() {
        // Companion to `tbd_flash_args_on_a_registered_backend_skips_not_fails`
        // (mapping TBD) and `dry_run_zephyr_ok_and_tbd_helper_fails` (bare
        // scalar TBD, but with an UNREGISTERED `flash_method: TBD` that fails
        // at `backend_for` before the scan ever runs). This one pairs a bare
        // `flash_args: TBD` scalar with a VALID, registered
        // `flash_method: swd_probe` so it actually exercises
        // `flash_args_has_tbd` past the backend lookup.
        let sdk = tmp("tbd-bare-sdk");
        fake_sdk(&sdk);
        let app = tmp("tbd-bare-app");
        let build_root = tmp("tbd-bare-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n- core_id: m55_he\n  os: \
             zephyr\n  output_artefact: m55_he-zephyr/zephyr/zephyr.elf\n  status: ok\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus:\n- \
             name: unresolved_helper\n  chip: gd32g553\n  firmware_path: firmware/helper.bin\n  \
             flash_method: swd_probe\n  flash_args: TBD\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, true));
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(run.exit, ExitCode::Success, "got envelope: {doc}");
        assert_eq!(doc["ok"], true);
        let entries = doc["data"]["entries"].as_array().unwrap();
        let helper = entries
            .iter()
            .find(|e| e["id"] == "unresolved_helper")
            .unwrap();
        assert_eq!(helper["status"], "skipped");
        assert_eq!(helper["rc"], -1);
        assert!(helper["message"].as_str().unwrap().contains("TBD"));
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn update_channel_helper_is_skipped_not_failed() {
        // alp-sdk#868: the AEN CC3501E helper dropped flash_method/flash_args
        // entirely and gained `update_channel: alp_ota_spi_otp` instead (it is
        // Alp-OTA-updated over the bridge SPI/OTP, never customer-flashed).
        // Must be a clean skip (rc -1, status "skipped") with a message that
        // names the reason, not the generic "no flash_method" -- and it must
        // not fail the whole run.
        let sdk = tmp("update-channel-sdk");
        fake_sdk(&sdk);
        let app = tmp("update-channel-app");
        let build_root = tmp("update-channel-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n- core_id: m55_hp\n  os: \
             zephyr\n  output_artefact: m55_hp-zephyr/zephyr/zephyr.elf\n  status: ok\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus:\n- \
             name: cc3501e_otp\n  chip: cc3501e\n  firmware_path: firmware/cc3501e.bin\n  \
             update_channel: alp_ota_spi_otp\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, true));
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(run.exit, ExitCode::Success, "got envelope: {doc}");
        assert_eq!(doc["ok"], true);
        let entries = doc["data"]["entries"].as_array().unwrap();
        let zephyr = entries.iter().find(|e| e["id"] == "m55_hp").unwrap();
        assert_eq!(zephyr["status"], "ok");
        let helper = entries.iter().find(|e| e["id"] == "cc3501e_otp").unwrap();
        assert_eq!(helper["status"], "skipped");
        assert_eq!(helper["rc"], -1);
        let msg = helper["message"].as_str().unwrap();
        assert!(msg.contains("Alp-OTA-updated"), "got: {msg}");
        assert!(msg.contains("alp_ota_spi_otp"), "got: {msg}");
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn core_filter_matches_nothing_in_json_mode_reports_issue() {
        let sdk = tmp("nomatch-json-sdk");
        fake_sdk(&sdk);
        let app = tmp("nomatch-json-app");
        let build_root = tmp("nomatch-json-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: X\nslices:\n- core_id: m33\n  os: zephyr\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus: \
             []\nboot_order: []\n",
        )
        .unwrap();
        let mut a = flash_args(&app, &build_root, true);
        a.core = Some("does-not-exist".to_string());
        let g = global(&sdk, Format::Json);
        let run = run(&g, &a);
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["ok"], true);
        assert!(doc["data"]["entries"].as_array().unwrap().is_empty());
        // Before the fix "nothing matched" was a text-only line: `--format
        // json` discards `text` entirely, so a filter typo that flashed
        // nothing was byte-identical to a real success in the envelope.
        let issue = doc["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["code"] == "flash.nothing-matched");
        assert!(issue.is_some(), "expected a flash.nothing-matched issue");
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn planning_only_without_confirm_is_reported_planned_not_ok() {
        let sdk = tmp("plan-sdk");
        fake_sdk(&sdk);
        let app = tmp("plan-app");
        let build_root = tmp("plan-build");
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-V2N101\nslices: []\nhelper_mcus:\n- name: \
             fip_helper\n  chip: xspi\n  firmware_path: firmware/fip.bin\n  flash_method: \
             xspi_flashwriter\n  flash_args:\n    flash_partition: mtd1\n    port: COM24\n\
             boot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        // dry_run=false: a REAL flash was requested, but xspi_flashwriter's
        // confirm gate (flash_args.confirm / ALP_FLASH_FORCE) is not armed, so
        // the backend itself refuses to write. Before this fix the entry
        // reported status "ok", rc 0 — byte-identical to a real completed write.
        let run = run(&g, &flash_args(&app, &build_root, false));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["ok"], true);
        let entries = doc["data"]["entries"].as_array().unwrap();
        let helper = entries.iter().find(|e| e["id"] == "fip_helper").unwrap();
        assert_eq!(helper["status"], "planned");
        assert!(helper["message"].as_str().unwrap().contains("confirm"));
        let issue = doc["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["code"] == "flash.confirm-required");
        assert!(
            issue.is_some(),
            "a confirm-gated no-op must surface a flash.confirm-required issue"
        );
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn slice_not_built_refuses_to_flash_stale_artefact() {
        // Fable's exact scenario: run 1 built ok (status: ok, elf on disk);
        // run 2's `west` resolution breaks and the slice comes back
        // `status: failed` in the rewritten manifest, but `output_artefact`
        // still points at run 1's elf (overlay_run_results never wipes it).
        // Before this fix that elf got silently flashed with `ok:true` and
        // zero issues.
        let sdk = tmp("stale-sdk");
        fake_sdk(&sdk);
        let app = tmp("stale-app");
        let build_root = tmp("stale-build");
        let stale_elf = build_root.join("m55_hp-zephyr").join("zephyr");
        std::fs::create_dir_all(&stale_elf).unwrap();
        std::fs::write(stale_elf.join("zephyr.elf"), b"stale-run-1-elf").unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n- core_id: m55_hp\n  os: \
             zephyr\n  output_artefact: m55_hp-zephyr/zephyr/zephyr.elf\n  status: failed\n  \
             flash_method: zephyr_west_flash\n  flash_args:\n    runner: openocd\nhelper_mcus: \
             []\nboot_order: []\n",
        )
        .unwrap();
        let g = global(&sdk, Format::Json);
        let run = run(&g, &flash_args(&app, &build_root, false));
        assert_eq!(run.exit, ExitCode::RuntimeFailure, "must not exit clean");
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["ok"], false, "ok must not disagree with exitCode");
        // never turned into a FlashTarget, so no spawn/entry for it either.
        assert!(doc["data"]["entries"].as_array().unwrap().is_empty());
        let issue = doc["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["code"] == "flash.slice-not-built")
            .unwrap_or_else(|| panic!("expected a flash.slice-not-built issue, got {doc}"));
        assert_eq!(issue["severity"], "error");
        assert!(issue["message"].as_str().unwrap().contains("m55_hp"));
        assert!(issue["message"].as_str().unwrap().contains("failed"));
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn pipeline_drains_decompressor_stderr_so_capture_mode_does_not_deadlock() {
        // Regression for the pipe-buffer deadlock: a decompressor that writes
        // more than the OS pipe buffer to stderr blocks on write() once nobody
        // reads it, so it never reaches EOF on stdout either, `right` blocks
        // reading its stdin, and `lchild.wait()` never returns. Run the
        // pipeline on a background thread with a bounded wait — a regression
        // hangs this test instead of hanging the whole suite forever.
        let scratch = tmp("pipeline-stderr-src");
        let file = scratch.join("noisy.txt");
        std::fs::write(&file, vec![b'a'; 300_000]).unwrap();

        let (left, right): (Vec<String>, Vec<String>) = if cfg!(windows) {
            (
                vec![
                    "cmd".to_string(),
                    "/c".to_string(),
                    format!("type {} 1>&2", file.display()),
                ],
                vec!["findstr".to_string(), "x".to_string()],
            )
        } else {
            (
                vec![
                    "sh".to_string(),
                    "-c".to_string(),
                    format!("cat {} >&2", file.display()),
                ],
                vec!["cat".to_string()],
            )
        };

        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let result = spawn_pipeline(true, &left, &right);
            let _ = tx.send(result.is_ok());
        });
        match rx.recv_timeout(std::time::Duration::from_secs(20)) {
            Ok(ok) => assert!(ok, "pipeline should complete cleanly"),
            Err(_) => panic!("spawn_pipeline deadlocked: decompressor stderr not drained"),
        }
        std::fs::remove_dir_all(&scratch).unwrap();
    }
}
