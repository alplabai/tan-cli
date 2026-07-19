// SPDX-License-Identifier: Apache-2.0
//! The `--native` build executor: run each plan slice's `ToolStep` sequentially,
//! folding per-slice outcomes into the envelope (JSON) or a colorful recap
//! (text). Also the consumer-mechanism env gap-filler (`ZEPHYR_BASE` /
//! `EXTRA_ZEPHYR_MODULES`).

mod env;
mod manifest;

use std::path::Path;
use std::process::Command;

use serde::Serialize;
use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, PolicyAction};
use tan_core::debug::{DoctorCheck, DoctorStatus};
use tan_core::plan_exec::{assemble_slice_env, resolve_action};
use tan_core::preflight::preflight_summary;

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

use super::workspace::{west_program, west_workspace_dir, with_venv_on_path};

/// `SliceResult.reason` for a plan slice that carries no command. The recap
/// keys its `(no command)` rendering off this exact string — keep producer and
/// renderer on the one constant.
const SKIP_REASON_NO_COMMAND: &str = "no command";

/// Per-slice outcome of a `--native` run, folded into the envelope.
#[derive(Serialize)]
struct SliceResult {
    /// The core this slice builds (e.g. `m55_hp`).
    #[serde(rename = "coreId")]
    core_id: String,
    /// Build backend for the slice (`zephyr` / `yocto` / `baremetal`).
    backend: String,
    /// Outcome: `"ok"`, `"failed"`, or `"skipped"` (no command, or the slice's
    /// tool is not on this host).
    status: String, // "ok" | "failed" | "skipped"
    /// Process exit code, when the tool actually launched.
    #[serde(skip_serializing_if = "Option::is_none")]
    rc: Option<i32>,
    /// Why the slice was skipped (verbatim, e.g. the missing-tool reason).
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
    /// Real on-disk `zephyr.elf` path after a successful build (absolute), fed
    /// into the post-build manifest so `size`/`renode`/`flash` find the artefact
    /// west actually produced. Internal plumbing — kept out of the JSON envelope.
    #[serde(skip)]
    output_artefact: Option<String>,
    /// Real on-disk build directory (west's nested `<cwd>/build`, absolute) —
    /// what `image` tars. Internal plumbing — kept out of the JSON envelope.
    #[serde(skip)]
    build_dir: Option<String>,
}

/// Envelope `data` for a `--native` build: the base dir plus each slice result.
#[derive(Serialize)]
struct BuildRunData {
    /// Envelope `data` schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Project build-tree base the slices ran under.
    #[serde(rename = "baseDir")]
    base_dir: String,
    /// Outcome of each slice, in plan order.
    slices: Vec<SliceResult>,
}

/// Run each slice's `ToolStep` sequentially under `base`. Text mode streams each
/// build live (inherited stdio) with per-slice headers; JSON mode captures and
/// folds per-slice results into the envelope. Commandless slices are skipped.
/// Runs all slices (does not abort early) and exits non-zero if any failed.
pub(super) fn execute_slices(
    g: &GlobalArgs,
    context: &ProjectContext,
    project: Project,
    plan: &BuildPlan,
    base: &str,
) -> CommandRun {
    let base_path = Path::new(base);
    let text_mode = !g.is_json();
    let theme = crate::style::Theme::from_args(g);
    let mut results: Vec<SliceResult> = Vec::new();
    let mut any_failed = false;
    let sdk_root = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));
    // Auto-manage the build env so `tan build` needs no manual
    // `source .venv/activate` / `export ZEPHYR_BASE`: derive ZEPHYR_BASE from the
    // resolved workspace and pass the alp-sdk checkout as an extra Zephyr module,
    // so `west build -b <alp-board>` finds the SDK's boards without the user
    // wiring `-DEXTRA_ZEPHYR_MODULES`. The plan's per-slice env still wins.
    let zephyr_base = west_workspace_dir(base, sdk_root.as_deref())
        .map(|ws| ws.join("zephyr"))
        .filter(|z| z.is_dir());

    for slice in &plan.slices {
        let backend = slice.backend.as_str().to_string();
        let Some(cmd) = &slice.command else {
            // `null_command` policy: Skip (default / older plan) vs Fail.
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.null_command,
                PolicyAction::Skip,
            ) == PolicyAction::Fail;
            if text_mode {
                let (ds, tail) = if fail {
                    (DoctorStatus::Fail, "no command")
                } else {
                    (DoctorStatus::Warn, "no command, skipped")
                };
                eprintln!(
                    "{}",
                    theme.slice_result(ds, &format!("{} [{}] — {}", slice.core_id, backend, tail))
                );
            }
            if fail {
                any_failed = true;
            }
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(SKIP_REASON_NO_COMMAND.to_string()),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        };

        if text_mode {
            eprintln!(
                "{}",
                theme.slice_start(&slice.core_id, &backend, &cmd.display())
            );
        }
        let cwd = base_path.join(&cmd.cwd);
        // The build dir must exist before the tool runs (west/cmake build there).
        if let Err(e) = std::fs::create_dir_all(&cwd) {
            if text_mode {
                eprintln!(
                    "{}",
                    theme.slice_result(
                        DoctorStatus::Fail,
                        &format!("cannot create build dir {}: {e}", cwd.display())
                    )
                );
            }
            any_failed = true;
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: "failed".to_string(),
                rc: None,
                reason: None,
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }
        let tool = if cmd.tool == "west" {
            west_program(base, sdk_root.as_deref())
        } else {
            cmd.tool.clone()
        };

        // SDK parity (alp-sdk#114 / orchestrator.py:299): a build tool that's
        // simply not installed on this host is normal — non-Zephyr dev boxes
        // won't have `bitbake`, non-Linux boxes may lack the vendor toolchain.
        // Treat it as skipped (exit 0), not failed, and say why.
        if !crate::util::tool_available(&tool) {
            // The SDK's verbatim reason wording. "not found in PATH" assumes a
            // PATH-resolved tool; an absolute `tool` here is effectively
            // unreachable (west_program only returns venv paths it verified
            // with `is_file()`).
            let reason =
                format!("{tool} not found in PATH; this is normal on non-{backend} dev hosts");
            // `missing_tool` policy: Skip (default / older plan) vs Fail.
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.missing_tool,
                PolicyAction::Skip,
            ) == PolicyAction::Fail;
            if text_mode {
                let ds = if fail {
                    DoctorStatus::Fail
                } else {
                    DoctorStatus::Warn
                };
                eprintln!(
                    "{}",
                    theme
                        .slice_result(ds, &format!("{} [{}] — {}", slice.core_id, backend, reason))
                );
            }
            if fail {
                any_failed = true;
            }
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(reason),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }

        // Assemble the slice subprocess env (pure, in tan-core): slice env +
        // plan `env_append_path` (append/de-dup, seeded from the inherited
        // process env) + the CLI's pre-gated consumer-mechanism gap-fillers
        // (ZEPHYR_BASE always; EXTRA_ZEPHYR_MODULES only when the plan didn't
        // carry it — "plan wins / CLI fills gaps").
        let gap_fillers = env::zephyr_env_overrides(
            zephyr_base.as_deref(),
            sdk_root.as_deref(),
            &slice.env,
            &slice.env_append_path,
        );
        let env = assemble_slice_env(
            &slice.env,
            &slice.env_append_path,
            |k| std::env::var_os(k).map(|v| v.to_string_lossy().into_owned()),
            &gap_fillers,
        );

        let mut command = Command::new(&tool);
        command
            .args(&cmd.args)
            .current_dir(&cwd)
            .envs(env.iter().map(|(k, v)| (k, v)));
        with_venv_on_path(&mut command, &tool);

        let (status, rc) = if text_mode {
            match command.status() {
                Ok(s) if s.success() => ("ok", s.code()),
                Ok(s) => ("failed", s.code()),
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::NotFound {
                        eprintln!(
                            "   {tool} not found on PATH — install it to build this {backend} slice"
                        );
                    } else {
                        eprintln!("   launch error: {e}");
                    }
                    ("failed", None)
                }
            }
        } else {
            match command.output() {
                Ok(o) if o.status.success() => ("ok", o.status.code()),
                Ok(o) => ("failed", o.status.code()),
                Err(_) => ("failed", None),
            }
        };
        if status == "failed" {
            any_failed = true;
        }
        if text_mode {
            let ds = if status == "ok" {
                DoctorStatus::Pass
            } else {
                DoctorStatus::Fail
            };
            let note = match rc {
                Some(c) => format!("{status} (rc={c})"),
                None => status.to_string(),
            };
            eprintln!("{}", theme.slice_result(ds, &note));
        }
        // On success, resolve the real on-disk artefact west produced so the
        // post-build manifest points the consumers at the elf that exists.
        let (output_artefact, build_dir) = if status == "ok" {
            manifest::resolve_zephyr_artefact(&cwd)
        } else {
            (None, None)
        };
        results.push(SliceResult {
            core_id: slice.core_id.clone(),
            backend,
            status: status.to_string(),
            rc,
            reason: None,
            output_artefact,
            build_dir,
        });
    }

    // Output seam: write the post-build system-manifest.yaml (the contract
    // `tan flash`/`size`/`image` read) reflecting this run's per-slice status.
    // Best-effort — a fetch/parse/write failure warns but never fails the build.
    manifest::write_post_build_manifest(context, plan, base, &results, text_mode);

    let exit = if any_failed {
        ExitCode::RuntimeFailure
    } else {
        ExitCode::Success
    };

    if g.is_json() {
        let issues = if any_failed {
            vec![Issue {
                code: "build.slice-failed".to_string(),
                severity: "error".to_string(),
                message: "one or more slices failed to build".to_string(),
            }]
        } else {
            Vec::new()
        };
        let data = BuildRunData {
            schema_version: "1".to_string(),
            base_dir: base.to_string(),
            slices: results,
        };
        let json = Envelope::new("build", project, data, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        // Colorful per-slice recap: each slice as a check (ok / skipped / failed),
        // a colored summary line, and next-step hints when something failed.
        let checks: Vec<DoctorCheck> = results
            .iter()
            .map(|r| {
                let (status, detail) = match r.status.as_str() {
                    "ok" => (DoctorStatus::Pass, r.backend.clone()),
                    "skipped" => match r.reason.as_deref() {
                        Some(reason) if reason != SKIP_REASON_NO_COMMAND => {
                            (DoctorStatus::Warn, format!("{} — {reason}", r.backend))
                        }
                        _ => (DoctorStatus::Warn, format!("{} (no command)", r.backend)),
                    },
                    _ => (
                        DoctorStatus::Fail,
                        match r.rc {
                            Some(code) => format!("{} (rc={code})", r.backend),
                            None => format!("{} (did not run)", r.backend),
                        },
                    ),
                };
                DoctorCheck {
                    name: r.core_id.clone(),
                    status,
                    detail,
                    fix: None,
                }
            })
            .collect();
        let summary = preflight_summary(&checks);
        let next_steps = if summary.fail > 0 {
            vec![
                "see the streamed logs above for each failed slice".to_string(),
                "install any missing tool (west / bitbake) or fix the app's Zephyr CMakeLists"
                    .to_string(),
            ]
        } else {
            Vec::new()
        };
        let text = crate::style::render_report(g, "Build", "", &checks, &summary, &next_steps);
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
    use tan_core::build_plan::parse_build_plan;

    /// A context with no SDK — `invoke_sdk_emit` errs, exercising the graceful
    /// degrade in `write_post_build_manifest` without a real planner.
    fn no_sdk_context() -> ProjectContext {
        ProjectContext {
            workspace_root: None,
            sdk_root: None,
            board_yaml_path: None,
            west_cwd: None,
            python_binary: "python3".to_string(),
        }
    }

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("{tag}-{}", std::process::id()))
    }

    #[test]
    fn native_execute_runs_commands_skips_commandless_and_reports() {
        use clap::Parser;
        // A real GlobalArgs in JSON mode (captures output, no stderr noise).
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        // A portable success command on every CI platform.
        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }},
                {{ "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2", "command": null }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices.len(), 2);
        assert_eq!(slices[0]["status"], "ok");
        assert_eq!(slices[1]["status"], "skipped");
        assert_eq!(slices[1]["reason"], "no command");
        // The build dir for the runnable slice was created.
        assert!(base.join("build/c1").is_dir());

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_skips_missing_tool_and_exits_zero() {
        // SDK parity (alp-sdk#114): a slice whose build tool isn't on this host
        // (e.g. `bitbake` on a non-Yocto dev box) is skipped, not failed — the
        // whole run still exits 0.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "a32_cluster", "backend": "yocto", "buildDir": "build/a32",
              "command": { "tool": "alp-missing-tool-e114", "args": [], "cwd": "build/a32" } },
            { "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2", "command": null }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-missing-tool");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "skipped");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(
            reason.contains("not found in PATH; this is normal on non-"),
            "got: {reason}"
        );
        assert!(env["issues"].as_array().unwrap().is_empty());

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_fails_missing_tool_when_policy_says_fail() {
        // execution_policy.missingTool = "fail" flips the default skip: a slice
        // whose tool isn't on this host now FAILS the run (exit 1), instead of
        // the None-policy skip covered by the test above.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "a32_cluster", "backend": "yocto", "buildDir": "build/a32",
              "command": { "tool": "alp-missing-tool-e114", "args": [], "cwd": "build/a32" } }
          ],
          "executionPolicy": { "missingTool": "fail" },
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-missing-tool-fail");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        // The reason string the CLI shows is unchanged (only skip->fail routing).
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(
            reason.contains("not found in PATH; this is normal on non-"),
            "got: {reason}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_reports_failed_for_a_real_nonzero_exit() {
        // A genuinely present tool that exits nonzero is still "failed" (exit 1)
        // — the missing-tool skip path above must not swallow real failures.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "1"]"#)
        } else {
            ("sh", r#"["-c", "exit 1"]"#)
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-failing-tool");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        assert_eq!(slices[0]["rc"], 1);

        std::fs::remove_dir_all(&base).ok();
    }
}
