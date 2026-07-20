// SPDX-License-Identifier: Apache-2.0
//! `tan run` — build the project, then run it: execute the produced `native_sim`
//! binary for a host target, or flash a hardware target.
//!
//! A thin orchestrator: it reuses `tan build`'s native engine
//! (`build::run_build`) and `tan flash`'s native engine (`flash::run`) verbatim,
//! never re-deriving either. The pure decision — execute vs flash vs
//! short-circuit — lives in `tan_core::run`; this file only spawns the
//! `native_sim` binary and stitches the delegated `CommandRun`'s envelope so
//! every `tan run` result self-identifies as `run`.
//!
//! Target selection is implicit: `board.yaml` (via `--project`) already names
//! the target, so `run` reads host-vs-hardware from the post-build
//! `system-manifest.yaml` — a slice whose Zephyr board is `native_sim` ⇒ host.
//! The runnable binary is located through the SAME artefact resolver `flash`
//! uses (`tan_core::flash::resolve_artefact_path`), so it matches the real build
//! layout (west's nested `build/` and all) instead of a hardcoded path.
//!
//! Flashing real silicon is opt-in: a hardware target flashes only on `--flash`;
//! a bare `tan run` on a hardware project builds + reports, never programming the
//! board. In text mode a host binary streams live (a `native_sim` image is a
//! live device-simulator loop, not a batch job). JSON mode never spawns it —
//! capturing a process that never closes stdout would hang the
//! one-envelope-per-invocation contract — and instead reports the skip.

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
    // Build via the same engine `tan build` uses by default (native,
    // board.yaml-driven plan) — never duplicated here.
    let build_args = BuildArgs {
        plan: false,
        plan_from: None,
        materialise: false,
        native: true,
        manifest: false,
        manifest_from: None,
    };
    let built = build::run_build(g, &build_args);
    let build_ok = built.exit.code() == ExitCode::Success.code();

    let context = resolve_cli_project_context(g);
    let base = PathBuf::from(base_dir(&context));
    let exe = find_native_sim_exe(g, &base);

    match decide_run_action(build_ok, exe.is_some(), args.flash) {
        // Build failed: short-circuit, report the build (re-tagged as `run`).
        RunAction::BuildFailed => retag(built, "run"),
        // Host target: execute the produced native_sim binary.
        RunAction::ExecuteNative => exec_native_sim(g, built, &exe.expect("present per decision")),
        // Hardware target + explicit `--flash`: reuse the native flash path,
        // targeting the SAME project base that was built + native-detected — not
        // cwd. `flash::run` resolves its build root from `current_dir()`, so a
        // bare `app_path: "."` under `--project <dir>` would flash cwd/build (a
        // stale manifest ⇒ the WRONG image on hardware); pass the resolved base.
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

/// Locate the produced `native_sim` executable from the post-build
/// `system-manifest.yaml` under `<base>/build`. Finds the native_sim slice by
/// its Zephyr board target (pure, in tan-core), resolves that slice's real
/// `output_artefact` (`zephyr.elf`) through the SAME resolver `flash` uses, then
/// takes the sibling `zephyr.exe` in the build's `zephyr/` dir. `None` when
/// there's no manifest, no native_sim slice, or the binary isn't on disk — so a
/// hardware project (or an unbuilt/absent binary) falls through to the flash /
/// build-only path, never mis-fires an exec.
fn find_native_sim_exe(g: &GlobalArgs, base: &Path) -> Option<PathBuf> {
    let build_root = base.join("build");
    let yaml = std::fs::read_to_string(build_root.join("system-manifest.yaml")).ok()?;
    let manifest = parse_system_manifest(&yaml).ok()?;
    let slice = native_sim_slice(&manifest)?;
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
