// SPDX-License-Identifier: Apache-2.0
//! `tan run` — build the project, then run it: execute the produced `native_sim`
//! binary for a host target, or flash a hardware target.
//!
//! A thin orchestrator: it reuses `tan build`'s native engine
//! (`build::run_build_native_outcome`) and `tan flash`'s native engine
//! (`flash::run`) verbatim, never re-deriving either. The pure decision —
//! execute vs flash vs short-circuit — lives in `tan_core::run`; this file only
//! spawns the `native_sim` binary and stitches the delegated `CommandRun`'s
//! envelope so every `tan run` result self-identifies as `run`.
//!
//! **Root cause fix (third attempt at this defect):** host-vs-hardware and
//! flash-safety are decided from the `NativeBuildOutcome`
//! (`crate::commands::build::NativeBuildOutcome`) the
//! build we JUST RAN returns in memory — `outcome.native_sim_target` /
//! `outcome.manifest_written` — NEVER by re-reading `build/system-manifest.yaml`
//! off disk afterward. The two earlier (reverted) attempts kept that re-read
//! and belted the staleness with an mtime "freshness" check; mtime is leaky (a
//! sub-2s inter-invocation window slips through) and a no-`--flash` arm still
//! read a stale host binary regardless. There is no `manifest_path` variable,
//! no mtime comparison, and no `is_native_sim_target` disk read anywhere in
//! this file's decision path — see `tan_core::run`'s module doc for the full
//! writeup.
//!
//! The runnable `native_sim` binary is still located on disk
//! ([`find_native_sim_exe`]) — you need the actual file to execute it — but
//! that probe only runs AFTER the decision already says `ExecuteNative`; it is
//! never the target decision itself. It resolves through the SAME artefact
//! resolver `flash` uses (`tan_core::flash::resolve_artefact_path`), so it
//! matches the real build layout (west's nested `build/` and all) instead of a
//! hardcoded path.
//!
//! Flashing real silicon is opt-in: a hardware target flashes only on `--flash`,
//! and only when THIS run's manifest file write is confirmed to have succeeded
//! (`outcome.manifest_written`); a bare `tan run` on a hardware project builds +
//! reports, never programming the board. In text mode a host binary streams
//! live (a `native_sim` image is a live device-simulator loop, not a batch
//! job). JSON mode never spawns it — capturing a process that never closes
//! stdout would hang the one-envelope-per-invocation contract — and instead
//! reports the skip.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{Value, json};
use tan_core::ProjectContext;
use tan_core::flash::resolve_artefact_path;
use tan_core::run::{NATIVE_SIM_EXE, RunAction, decide_run_action, native_sim_slice};
use tan_core::system_manifest::parse_system_manifest;

use super::CommandRun;
use super::{build, flash};
use crate::cli::{BuildArgs, FlashArgs, GlobalArgs, RunArgs};
use crate::exit::ExitCode;
use crate::util::{cli_workspace_root, resolve_cli_project_context, resolve_sdk_root};

/// `tan run` entry point.
pub fn run(g: &GlobalArgs, args: &RunArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let base = PathBuf::from(base_dir(&context));

    // Build via the same engine `tan build` uses by default (native,
    // board.yaml-driven plan) — never duplicated here. The OUTCOME variant
    // (not `run_build`) is what carries this run's own in-memory
    // manifest-written / native_sim-target signals — see the module doc for
    // why deciding from those, instead of re-reading `system-manifest.yaml`
    // off disk afterward, is the actual fix.
    let build_args = BuildArgs {
        plan: false,
        plan_from: None,
        materialise: false,
        native: true,
        manifest: false,
        manifest_from: None,
        // `tan run` is "build then run", so it inherits `build`'s default
        // auto-bootstrap; `--no-auto-bootstrap` is a `build`-only flag.
        no_auto_bootstrap: false,
    };
    let outcome = build::run_build_native_outcome(g, &build_args);
    let built = outcome.run;
    let build_ok = built.exit.code() == ExitCode::Success.code();

    match decide_run_action(
        build_ok,
        outcome.native_sim_target,
        args.flash,
        outcome.manifest_written,
    ) {
        // Build failed: short-circuit, report the build (re-tagged as `run`).
        RunAction::BuildFailed => retag(built, "run"),
        // `--flash` requested, but this run's OWN build outcome doesn't
        // confirm it's safe (unknown target, or a confirmed hardware target
        // whose manifest FILE write failed) — refuse rather than risk
        // flashing a stale/wrong image. Board untouched.
        RunAction::ManifestStale => manifest_stale_refusal(g, built),
        // Host target (this run's build says native_sim): execute the
        // produced native_sim binary — probed on disk NOW, since the target
        // decision above never depended on the exe's presence. `None` here
        // means THIS run didn't actually produce a runnable binary (slice
        // skipped/failed, or the artefact went missing since); report + stop
        // rather than running a stale binary left from a previous build.
        //
        RunAction::ExecuteNative => execute_native_arm(g, built, &base, outcome.manifest_written),
        // Hardware target + explicit `--flash`, manifest write confirmed:
        // reuse the native flash path, targeting the SAME project base that
        // was built — not cwd. `flash::run` resolves its build root from
        // `current_dir()`, so a bare `app_path: "."` under `--project <dir>`
        // would flash cwd/build (the WRONG image); pass the resolved base.
        RunAction::Flash => retag(
            flash::run(g, &flash_args_for(&base, args.core.clone())),
            "run",
        ),
        // Hardware target, no `--flash`: the build succeeded, but programming the
        // board is the dangerous path and needs explicit consent — report + stop.
        RunAction::BuildOnly => {
            let mut run = retag(built, "run");
            if !g.is_json() {
                run.text
                    .push("run: built; pass --flash to program the board.".to_string());
            }
            run
        }
    }
}

/// The `RunAction::ExecuteNative` arm: run this build's `native_sim` binary, or
/// report that there isn't a trustworthy one.
///
/// `find_native_sim_exe` decides an on-disk `zephyr.exe` is fresh from the
/// manifest's `status: ok` — but that manifest is only THIS run's when the
/// post-build write actually succeeded. `manifest_written == false` (a Windows
/// sharing violation, a failed emit) means a PREVIOUS run's ok-status manifest
/// and its `zephyr.exe` may still be on disk; executing that would report
/// success for an edit this run never compiled. So an unconfirmed write makes
/// the probe untrusted — same in-memory signal the flash arm gates on — and we
/// report `native_sim_unavailable` rather than run stale bytes.
fn execute_native_arm(
    g: &GlobalArgs,
    built: CommandRun,
    base: &Path,
    manifest_written: bool,
) -> CommandRun {
    match find_native_sim_exe(g, base).filter(|_| manifest_written) {
        Some(exe) => exec_native_sim(g, built, &exe),
        None => native_sim_unavailable(g, built),
    }
}

/// The project build-tree base (`build/<core>-<os>/` lives here) — mirrors
/// `commands::build`'s private helper of the same name; kept as its own tiny
/// copy here since that one isn't exported (path resolution only, not
/// build/flash logic, so this isn't the duplication the unit is meant to avoid).
fn base_dir(context: &ProjectContext) -> String {
    context
        .west_cwd
        .clone()
        .or_else(|| context.workspace_root.clone())
        .unwrap_or_else(|| ".".to_string())
}

/// Build the `FlashArgs` for `run --flash`, anchored on the resolved project
/// `base` (`flash`'s `app_path` = its project dir, whose `build/` holds the
/// manifest). Threading `base` — not `"."` — makes `run --flash` program the
/// same project it built, even under `--project <dir>` where cwd differs.
fn flash_args_for(base: &Path, core: Option<String>) -> FlashArgs {
    FlashArgs {
        app_path: base.to_string_lossy().into_owned(),
        build_root: None,
        dry_run: false,
        core,
        helper: None,
        skip_missing_tools: false,
    }
}

/// `--flash` was requested, but this run's OWN build outcome does not confirm
/// it is safe to flash: either `outcome.native_sim_target` couldn't be
/// established at all (the SDK's system-manifest emit failed this run), or a
/// confirmed hardware target's `outcome.manifest_written` came back `false`
/// (the post-build `system-manifest.yaml` FILE write failed — a broken venv,
/// a Windows sharing violation — even though the emit itself succeeded).
/// Refuse rather than let `flash::run` program whatever a stale/absent file
/// on disk might say. Mirrors [`native_sim_unavailable`]'s shape (`ok:false`,
/// an error `Issue`).
fn manifest_stale_refusal(g: &GlobalArgs, built: CommandRun) -> CommandRun {
    const MESSAGE: &str = "run: --flash refused — this build's own outcome does not confirm it \
                            is safe to flash (either the target could not be determined, or \
                            this run's system-manifest.yaml write failed). Check the build \
                            output above, then retry `tan run --flash`.";
    let mut run = retag(built, "run");
    run.exit = ExitCode::RuntimeFailure;
    if !g.is_json() {
        run.text.push(MESSAGE.to_string());
    }
    run.json = run.json.map(|j| match serde_json::from_str::<Value>(&j) {
        Ok(mut v) => {
            if let Some(obj) = v.as_object_mut() {
                obj.insert("ok".to_string(), json!(false));
                obj.insert(
                    "exitCode".to_string(),
                    json!(ExitCode::RuntimeFailure.code()),
                );
                if let Some(issues) = obj.get_mut("issues").and_then(Value::as_array_mut) {
                    issues.push(json!({
                        "code": "run.manifest-stale",
                        "severity": "error",
                        "message": MESSAGE,
                    }));
                }
            }
            serde_json::to_string(&v).unwrap_or(j)
        }
        Err(_) => j,
    });
    run
}

/// Locate the produced `native_sim` executable from the post-build
/// `system-manifest.yaml` under `<base>/build`. Finds the native_sim slice by
/// its Zephyr board target (pure, in tan-core), resolves that slice's real
/// `output_artefact` (`zephyr.elf`) through the SAME resolver `flash` uses, then
/// takes the sibling `zephyr.exe` in the build's `zephyr/` dir. `None` when
/// there's no manifest, no native_sim slice, the slice didn't build `ok` THIS
/// run, or the binary isn't on disk — so a hardware project, an unbuilt/absent
/// binary, and a skipped slice (which would otherwise resolve to a STALE
/// `zephyr.exe` left from a previous run) all fall through to
/// `native_sim_unavailable` instead of silently executing old firmware.
fn find_native_sim_exe(g: &GlobalArgs, base: &Path) -> Option<PathBuf> {
    let build_root = base.join("build");
    let yaml = std::fs::read_to_string(build_root.join("system-manifest.yaml")).ok()?;
    let manifest = parse_system_manifest(&yaml).ok()?;
    let slice = native_sim_slice(&manifest)?;
    // A skipped/failed slice (missing tool, broken venv) leaves last run's
    // zephyr.exe sitting at the same path; without this check `tan run`
    // executes it and reports success while the source edit that triggered
    // this run was never actually compiled.
    if slice.status != "ok" {
        return None;
    }
    let elf = slice.output_artefact.as_deref().filter(|s| !s.is_empty())?;

    let sdk_root = resolve_sdk_root(g, &cli_workspace_root(g));
    let elf_path = resolve_artefact_path(elf, &build_root, sdk_root.as_deref(), |p| p.is_file());
    // The native_sim runnable sits beside its `zephyr.elf` in the `zephyr/` dir.
    let exe = elf_path.parent()?.join(NATIVE_SIM_EXE);
    exe.is_file().then_some(exe)
}

/// After a successful build, execute the `native_sim` binary. Text mode streams
/// it live (inherited stdio); JSON mode never executes it (see the module doc)
/// and instead reports the skip via `data.exec.executed: false`.
fn exec_native_sim(g: &GlobalArgs, built: CommandRun, exe: &Path) -> CommandRun {
    if g.is_json() {
        return skip_exec_result(built, exe);
    }

    eprintln!("run: executing {}", exe.display());
    let (ok, rc) = match Command::new(exe).status() {
        Ok(s) => (s.success(), s.code()),
        Err(e) => {
            eprintln!("run: failed to launch {}: {e}", exe.display());
            (false, None)
        }
    };

    let exit = if ok {
        ExitCode::Success
    } else {
        ExitCode::RuntimeFailure
    };
    with_exec_result(built, exit, exe, ok, rc)
}

/// The manifest declares a `native_sim` (host) target, but THIS run produced
/// no runnable `zephyr.exe` — the slice was skipped/failed (broken venv,
/// missing tool) or its artefact is missing. Report + stop: never execute a
/// binary we didn't just build, and never fall through to flashing hardware
/// (the manifest already said this is a host target). Mirrors
/// `with_exec_result`'s failure shape (`ok:false`, an error `Issue`) so a JSON
/// consumer sees this the same way it sees any other run failure.
fn native_sim_unavailable(g: &GlobalArgs, built: CommandRun) -> CommandRun {
    const MESSAGE: &str = "run: native_sim target, but this build produced no runnable \
                            zephyr.exe (slice skipped/failed, or artefact missing) — \
                            see build output above.";
    let mut run = retag(built, "run");
    run.exit = ExitCode::RuntimeFailure;
    if !g.is_json() {
        run.text.push(MESSAGE.to_string());
    }
    run.json = run.json.map(|j| match serde_json::from_str::<Value>(&j) {
        Ok(mut v) => {
            if let Some(obj) = v.as_object_mut() {
                obj.insert("ok".to_string(), json!(false));
                obj.insert(
                    "exitCode".to_string(),
                    json!(ExitCode::RuntimeFailure.code()),
                );
                if let Some(issues) = obj.get_mut("issues").and_then(Value::as_array_mut) {
                    issues.push(json!({
                        "code": "run.native-sim-unavailable",
                        "severity": "error",
                        "message": MESSAGE,
                    }));
                }
            }
            serde_json::to_string(&v).unwrap_or(j)
        }
        Err(_) => j,
    });
    run
}

/// The `data.exec` skip reason JSON mode reports when a `native_sim` binary was
/// found but not executed (see [`exec_native_sim`]).
const NATIVE_SIM_JSON_SKIP_REASON: &str =
    "native_sim exec skipped in --format json (run in text mode to execute)";

/// JSON-mode outcome when a `native_sim` binary WAS found: re-tag the build's
/// own (already-successful) envelope as `run` and nest
/// `{executed:false, reason, binary}` under `data.exec` — the exit code stays
/// the build's own `Success`, since this is a deliberate skip, not a failure.
fn skip_exec_result(built: CommandRun, exe: &Path) -> CommandRun {
    let mut run = retag(built, "run");
    run.json = run.json.map(|j| match serde_json::from_str::<Value>(&j) {
        Ok(mut v) => {
            if let Some(data) = v
                .as_object_mut()
                .and_then(|obj| obj.get_mut("data"))
                .and_then(Value::as_object_mut)
            {
                data.insert(
                    "exec".to_string(),
                    json!({
                        "executed": false,
                        "reason": NATIVE_SIM_JSON_SKIP_REASON,
                        "binary": exe.to_string_lossy(),
                    }),
                );
            }
            serde_json::to_string(&v).unwrap_or(j)
        }
        Err(_) => j,
    });
    run
}

/// Re-tag a delegated `CommandRun`'s envelope `command` field (e.g. `build` /
/// `flash`) as `run`'s own, so every `tan run` envelope self-identifies like its
/// sibling commands. Falls back to the untouched JSON on a parse failure (should
/// not happen — the JSON always comes from `Envelope::to_json`).
fn retag(run: CommandRun, command: &str) -> CommandRun {
    let json = run.json.map(|j| retag_json(&j, command));
    CommandRun {
        exit: run.exit,
        text: run.text,
        json,
    }
}

fn retag_json(json: &str, command: &str) -> String {
    match serde_json::from_str::<Value>(json) {
        Ok(mut v) => {
            if let Some(obj) = v.as_object_mut() {
                obj.insert("command".to_string(), json!(command));
            }
            serde_json::to_string(&v).unwrap_or_else(|_| json.to_string())
        }
        Err(_) => json.to_string(),
    }
}

/// The human-readable exec-failure fact, shared by the text line and the
/// `run.exec-failed` JSON issue (every other failing envelope in this CLI
/// carries at least one `Issue`).
fn exec_failure_message(exe: &Path, rc: Option<i32>) -> String {
    match rc {
        Some(code) => format!("{} exited with code {code}", exe.display()),
        None => format!("{} did not run to completion", exe.display()),
    }
}

/// Fold the `native_sim` execution outcome into the build's own envelope:
/// re-tag as `run`, override `ok`/`exitCode` with the execution's outcome, and
/// nest `{binary, ok, rc}` under `data.exec`. On failure also appends a
/// `run.exec-failed` error `Issue`. Text mode gets the same fact as an appended
/// line; JSON is `None` in text mode (unchanged).
fn with_exec_result(
    built: CommandRun,
    exit: ExitCode,
    exe: &Path,
    ok: bool,
    rc: Option<i32>,
) -> CommandRun {
    let mut text = built.text;
    if !ok {
        text.push(format!("run: {}", exec_failure_message(exe, rc)));
    }

    let json = built.json.map(|j| match serde_json::from_str::<Value>(&j) {
        Ok(mut v) => {
            if let Some(obj) = v.as_object_mut() {
                obj.insert("command".to_string(), json!("run"));
                obj.insert("ok".to_string(), json!(ok));
                obj.insert("exitCode".to_string(), json!(exit.code()));
                if let Some(data) = obj.get_mut("data").and_then(Value::as_object_mut) {
                    data.insert(
                        "exec".to_string(),
                        json!({
                            "binary": exe.to_string_lossy(),
                            "ok": ok,
                            "rc": rc,
                        }),
                    );
                }
                if !ok {
                    if let Some(issues) = obj.get_mut("issues").and_then(Value::as_array_mut) {
                        issues.push(json!({
                            "code": "run.exec-failed",
                            "severity": "error",
                            "message": exec_failure_message(exe, rc),
                        }));
                    }
                }
            }
            serde_json::to_string(&v).unwrap_or(j)
        }
        Err(_) => j,
    });

    CommandRun { exit, text, json }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retag_json_overrides_only_the_command_key() {
        let src = r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1"},"issues":[]}"#;
        let got = retag_json(src, "run");
        let v: Value = serde_json::from_str(&got).unwrap();
        assert_eq!(v["command"], "run");
        assert_eq!(v["ok"], true);
        assert_eq!(v["data"]["schemaVersion"], "1");
    }

    fn global() -> GlobalArgs {
        use clap::Parser;
        crate::cli::Cli::parse_from(["tan", "run"]).global
    }

    /// Resolves the native_sim exe as the `zephyr.exe` sibling of the manifest
    /// slice's real built `zephyr.elf` — via the shared artefact resolver, so
    /// the actual (nested) build layout is honored, not a hardcoded path.
    #[test]
    fn find_native_sim_exe_from_manifest_sibling_of_elf() {
        let base = std::env::temp_dir().join(format!("tan-run-nsim-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        // The real (nested) west layout: <base>/build/native_sim-zephyr/build/zephyr/.
        let zephyr_dir = base
            .join("build")
            .join("native_sim-zephyr")
            .join("build")
            .join("zephyr");
        std::fs::create_dir_all(&zephyr_dir).unwrap();
        let elf = zephyr_dir.join("zephyr.elf");
        std::fs::write(&elf, "").unwrap();
        let exe = zephyr_dir.join("zephyr.exe");
        std::fs::write(&exe, "").unwrap();

        // The manifest carries the ABSOLUTE elf path the build resolved.
        let manifest = format!(
            "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n  os: \
             zephyr\n  board: native_sim\n  status: ok\n  output_artefact: {}\nipc: \
             []\nhelper_mcus: []\nboot_order: []\n",
            elf.to_string_lossy().replace('\\', "/")
        );
        std::fs::write(base.join("build").join("system-manifest.yaml"), manifest).unwrap();

        assert_eq!(find_native_sim_exe(&global(), &base), Some(exe));

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn find_native_sim_exe_none_for_hardware_manifest() {
        let base = std::env::temp_dir().join(format!("tan-run-nsim-hw-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("build")).unwrap();
        std::fs::write(
            base.join("build").join("system-manifest.yaml"),
            "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: m55_hp\n  os: zephyr\n  \
             board: alp_e1m_aen701_m55_hp\n  status: ok\nipc: []\nhelper_mcus: []\nboot_order: \
             []\n",
        )
        .unwrap();

        assert_eq!(find_native_sim_exe(&global(), &base), None);

        std::fs::remove_dir_all(&base).ok();
    }

    /// Regression for the stale-binary defect (worklist run/mod.rs:126): a
    /// slice that this run left `status: skipped` must NOT resolve to a
    /// `zephyr.exe` that is still sitting on disk from a previous successful
    /// build, or `tan run` would silently execute old firmware.
    #[test]
    fn find_native_sim_exe_none_when_slice_skipped_even_with_stale_exe_on_disk() {
        let base = std::env::temp_dir().join(format!("tan-run-nsim-stale-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let zephyr_dir = base
            .join("build")
            .join("native_sim-zephyr")
            .join("build")
            .join("zephyr");
        std::fs::create_dir_all(&zephyr_dir).unwrap();
        let elf = zephyr_dir.join("zephyr.elf");
        std::fs::write(&elf, "").unwrap();
        std::fs::write(zephyr_dir.join("zephyr.exe"), "").unwrap();

        // Last run's exe is still on disk, but THIS run skipped the slice.
        let manifest = format!(
            "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n  os: \
             zephyr\n  board: native_sim\n  status: skipped\n  output_artefact: {}\nipc: \
             []\nhelper_mcus: []\nboot_order: []\n",
            elf.to_string_lossy().replace('\\', "/")
        );
        std::fs::write(base.join("build").join("system-manifest.yaml"), manifest).unwrap();

        assert_eq!(find_native_sim_exe(&global(), &base), None);

        std::fs::remove_dir_all(&base).ok();
    }

    /// Root-cause regression (A1 — the third attempt's exact reachable
    /// failure, at THIS file's own layer): `board.yaml` edited to
    /// `native_sim`, an EARLIER hardware build's manifest still on disk,
    /// THIS run's native_sim build succeeds but its post-build manifest FILE
    /// write fails (`manifest_written = false`). `run()` no longer re-reads
    /// any file to decide the target — `decide_run_action` is called
    /// directly with `outcome.native_sim_target` from the build, and that
    /// alone must settle `ExecuteNative`, never `Flash`, however `--flash`
    /// is set. The two earlier (reverted) attempts routed this exact case to
    /// `Flash` by re-reading the stale on-disk hardware manifest.
    #[test]
    fn a1_native_sim_target_with_failed_manifest_write_never_flashes() {
        assert_ne!(
            decide_run_action(true, Some(true), true, false),
            RunAction::Flash
        );
        assert_eq!(
            decide_run_action(true, Some(true), true, false),
            RunAction::ExecuteNative
        );
    }

    /// Root-cause regression (A2): `board.yaml` edited to a hardware target,
    /// a stale native_sim manifest + `zephyr.exe` left on disk from an
    /// earlier run, bare `tan run` (no `--flash`). THIS run's build outcome
    /// says hardware (`Some(false)`) — `run()` must stop at `BuildOnly`,
    /// never reach `ExecuteNative` and execute the stale binary.
    #[test]
    fn a2_hardware_target_bare_run_never_executes_native() {
        assert_eq!(
            decide_run_action(true, Some(false), false, true),
            RunAction::BuildOnly
        );
        assert_ne!(
            decide_run_action(true, Some(false), false, true),
            RunAction::ExecuteNative
        );
    }

    /// R1 regression (Fable-found residual in the ExecuteNative arm): a stale
    /// native_sim manifest (`status: ok`) and its `zephyr.exe` are on disk from
    /// an earlier run, and THIS run's manifest write FAILED
    /// (`manifest_written = false`). `find_native_sim_exe` would happily resolve
    /// the stale exe — so the arm must refuse it on the unconfirmed-write signal
    /// and report `native_sim_unavailable`, not execute last run's binary.
    /// JSON mode so a *confirmed* write's happy path skips the spawn rather than
    /// launching a process in the test.
    #[test]
    fn execute_native_arm_refuses_stale_exe_when_manifest_write_unconfirmed() {
        let base = std::env::temp_dir().join(format!("tan-run-r1-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let zephyr_dir = base
            .join("build")
            .join("native_sim-zephyr")
            .join("build")
            .join("zephyr");
        std::fs::create_dir_all(&zephyr_dir).unwrap();
        let elf = zephyr_dir.join("zephyr.elf");
        std::fs::write(&elf, "").unwrap();
        std::fs::write(zephyr_dir.join("zephyr.exe"), "").unwrap();
        let manifest = format!(
            "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n  os: \
             zephyr\n  board: native_sim\n  status: ok\n  output_artefact: {}\nipc: \
             []\nhelper_mcus: []\nboot_order: []\n",
            elf.to_string_lossy().replace('\\', "/")
        );
        std::fs::write(base.join("build").join("system-manifest.yaml"), manifest).unwrap();

        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["tan", "run", "--format", "json"]).global;
        let built = || {
            CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1"},"issues":[]}"#
                    .to_string(),
            ),
        }
        };

        // The exe IS resolvable on disk — the only thing that changes the
        // outcome is the write-confirmation flag.
        assert!(find_native_sim_exe(&g, &base).is_some());

        // Unconfirmed write: refuse, even though a runnable exe is right there.
        let refused = execute_native_arm(&g, built(), &base, false);
        let v: Value = serde_json::from_str(refused.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["ok"], false);
        assert!(
            v["issues"]
                .as_array()
                .unwrap()
                .iter()
                .any(|i| i["code"] == "run.native-sim-unavailable"),
            "{v}"
        );

        // Confirmed write: the same exe is accepted (JSON mode skips the spawn).
        let ok = execute_native_arm(&g, built(), &base, true);
        let v: Value = serde_json::from_str(ok.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["data"]["exec"]["executed"], false);

        std::fs::remove_dir_all(&base).ok();
    }

    /// `--flash` on an unknown target (this run's SDK system-manifest emit
    /// failed, so `outcome.native_sim_target == None`) must refuse — flashing
    /// blind (assuming hardware, or assuming native_sim) is never acceptable.
    #[test]
    fn flash_refused_when_target_unknown() {
        assert_eq!(
            decide_run_action(true, None, true, true),
            RunAction::ManifestStale
        );
        assert_eq!(
            decide_run_action(true, None, true, false),
            RunAction::ManifestStale
        );
    }

    /// A confirmed hardware target whose manifest FILE write failed must
    /// still refuse `--flash`, even though the SDK emit itself (and so the
    /// target determination) succeeded — `manifest_written` gates the write,
    /// not the target read.
    #[test]
    fn flash_refused_when_hardware_manifest_write_failed() {
        assert_eq!(
            decide_run_action(true, Some(false), true, false),
            RunAction::ManifestStale
        );
    }

    /// A bare `tan run` (no `--flash`) is unaffected by manifest-write
    /// success or an unknown target — the dangerous action is flashing, not
    /// building/reporting.
    #[test]
    fn build_only_unaffected_by_manifest_written_or_unknown_target() {
        assert_eq!(
            decide_run_action(true, Some(false), false, false),
            RunAction::BuildOnly
        );
        assert_eq!(
            decide_run_action(true, None, false, false),
            RunAction::BuildOnly
        );
    }

    #[test]
    fn manifest_stale_refusal_reports_runtime_failure_with_issue() {
        let built = CommandRun {
            exit: ExitCode::Success,
            text: vec!["build: complete.".to_string()],
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1","baseDir":"build","slices":[]},"issues":[]}"#
                    .to_string(),
            ),
        };
        let run = manifest_stale_refusal(&global(), built);
        assert_eq!(run.exit.code(), ExitCode::RuntimeFailure.code());
        let v: Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["command"], "run");
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], ExitCode::RuntimeFailure.code());
        let issues = v["issues"].as_array().unwrap();
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0]["code"], "run.manifest-stale");
        assert_eq!(issues[0]["severity"], "error");
    }

    #[test]
    fn native_sim_unavailable_reports_runtime_failure_with_issue() {
        let built = CommandRun {
            exit: ExitCode::Success,
            text: vec!["build: complete.".to_string()],
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1","baseDir":"build","slices":[]},"issues":[]}"#
                    .to_string(),
            ),
        };
        let run = native_sim_unavailable(&global(), built);
        assert_eq!(run.exit.code(), ExitCode::RuntimeFailure.code());
        let v: Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["command"], "run");
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], ExitCode::RuntimeFailure.code());
        let issues = v["issues"].as_array().unwrap();
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0]["code"], "run.native-sim-unavailable");
        assert_eq!(issues[0]["severity"], "error");
    }

    /// Hardware-safety: `run --flash --project <dir>` must flash <dir>, not cwd.
    /// `flash::run` resolves `build_root = current_dir()/app_path/build`, so
    /// anchoring `app_path` on the resolved base (not ".") is what keeps a stale
    /// cwd/build/system-manifest.yaml from being programmed onto the board.
    #[test]
    fn flash_args_target_the_project_base_not_cwd() {
        let base = Path::new("/some/project/dir");
        let fa = flash_args_for(base, Some("m33".to_string()));
        assert_eq!(fa.app_path, base.to_string_lossy());
        assert_ne!(fa.app_path, ".");
        assert_eq!(fa.core.as_deref(), Some("m33"));
    }

    #[test]
    fn find_native_sim_exe_none_when_manifest_absent() {
        let base = std::env::temp_dir().join(format!("tan-run-nsim-none-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        assert_eq!(find_native_sim_exe(&global(), &base), None);

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn with_exec_result_nests_exec_under_data_and_overrides_ok() {
        let built = CommandRun {
            exit: ExitCode::Success,
            text: vec!["build: complete.".to_string()],
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1","baseDir":"build","slices":[]},"issues":[]}"#
                    .to_string(),
            ),
        };
        let run = with_exec_result(
            built,
            ExitCode::RuntimeFailure,
            Path::new("/tmp/zephyr.exe"),
            false,
            Some(1),
        );
        assert_eq!(run.exit.code(), 1);
        let v: Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["command"], "run");
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], 1);
        assert_eq!(v["data"]["exec"]["ok"], false);
        assert_eq!(v["data"]["exec"]["rc"], 1);
        assert!(run.text.iter().any(|l| l.contains("exited with code 1")));
        let issues = v["issues"].as_array().unwrap();
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0]["code"], "run.exec-failed");
        assert_eq!(issues[0]["severity"], "error");
    }

    #[test]
    fn with_exec_result_leaves_issues_empty_on_success() {
        let built = CommandRun {
            exit: ExitCode::Success,
            text: vec!["build: complete.".to_string()],
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1","baseDir":"build","slices":[]},"issues":[]}"#
                    .to_string(),
            ),
        };
        let run = with_exec_result(
            built,
            ExitCode::Success,
            Path::new("/tmp/zephyr.exe"),
            true,
            Some(0),
        );
        let v: Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert!(v["issues"].as_array().unwrap().is_empty());
    }

    #[test]
    fn skip_exec_result_reports_not_executed_and_keeps_success() {
        let built = CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: Some(
                r#"{"command":"build","ok":true,"exitCode":0,"project":{"root":null,"boardYaml":null},"data":{"schemaVersion":"1","baseDir":"build","slices":[]},"issues":[]}"#
                    .to_string(),
            ),
        };
        let run = skip_exec_result(built, Path::new("/tmp/zephyr.exe"));
        assert_eq!(run.exit.code(), 0);
        let v: Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(v["command"], "run");
        assert_eq!(v["ok"], true);
        assert_eq!(v["data"]["exec"]["executed"], false);
        assert_eq!(v["data"]["exec"]["binary"], "/tmp/zephyr.exe");
        assert_eq!(v["data"]["exec"]["reason"], NATIVE_SIM_JSON_SKIP_REASON);
    }
}
