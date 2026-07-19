// SPDX-License-Identifier: Apache-2.0
//! The `--native` build executor: run each plan slice's `ToolStep` sequentially,
//! folding per-slice outcomes into the envelope (JSON) or a colorful recap
//! (text). Also the consumer-mechanism env gap-filler (`ZEPHYR_BASE` /
//! `EXTRA_ZEPHYR_MODULES`).

use std::path::{Component, Path};
use std::process::Command;

use serde::Serialize;
use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, PolicyAction};
use tan_core::debug::{DoctorCheck, DoctorStatus};
use tan_core::plan_exec::{assemble_slice_env, resolve_action};
use tan_core::preflight::preflight_summary;
use tan_core::system_manifest::{
    overlay_run_results, parse_system_manifest, serialize_system_manifest,
};

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

use super::workspace::{invoke_sdk_emit, west_program, west_workspace_dir, with_venv_on_path};

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

/// Consumer-mechanism env the plan deliberately does NOT carry, filled in as a
/// gap-filler so `tan build` runs a plan slice with no manual setup:
///   * `ZEPHYR_BASE` — the resolved workspace's zephyr. Per ADR-0020 the plan
///     never emits this; it is pure consumer mechanism, always hand-derived.
///   * `EXTRA_ZEPHYR_MODULES` — the alp-sdk checkout, so `west build -b
///     <alp-board>` finds the SDK's boards. This now comes FROM the plan's
///     `env_append_path`; the hand-derived value is only a FALLBACK for an older
///     plan that carries neither the slice-env pin nor the `env_append_path`
///     entry (plan wins / CLI fills gaps).
///
/// Never overrides a key the plan's slice env pins.
fn zephyr_env_overrides(
    zephyr_base: Option<&Path>,
    sdk_root: Option<&Path>,
    slice_env: &std::collections::BTreeMap<String, String>,
    env_append_path: &std::collections::BTreeMap<String, Vec<String>>,
) -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    if !slice_env.contains_key("ZEPHYR_BASE") {
        if let Some(base) = zephyr_base {
            out.push(("ZEPHYR_BASE", base.to_string_lossy().into_owned()));
        }
    }
    // Plan wins: skip the hand-derived value when the plan carries
    // EXTRA_ZEPHYR_MODULES (as a slice-env pin or an env_append_path entry).
    if !slice_env.contains_key("EXTRA_ZEPHYR_MODULES")
        && !env_append_path.contains_key("EXTRA_ZEPHYR_MODULES")
    {
        if let Some(sdk) = sdk_root {
            out.push(("EXTRA_ZEPHYR_MODULES", sdk.to_string_lossy().into_owned()));
        }
    }
    out
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
        let gap_fillers = zephyr_env_overrides(
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
            resolve_zephyr_artefact(&cwd)
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
    write_post_build_manifest(context, plan, base, &results, text_mode);

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

/// Write `<build_root>/system-manifest.yaml` after a `--native` run: fetch the
/// plan-time projection from the SDK (`--emit system-manifest`), overlay this
/// run's per-slice status (identity mapping — both use `ok`/`failed`/`skipped`),
/// serialize, and write under the plan's build root. Best-effort: the manifest
/// is a post-build convenience, so any failure (no SDK, emit error, unsafe
/// build_root, unwritable dir) warns (text mode only) and returns — it never
/// changes the build's exit result.
fn write_post_build_manifest(
    context: &ProjectContext,
    plan: &BuildPlan,
    base: &str,
    results: &[SliceResult],
    warn_enabled: bool,
) {
    let warn = |msg: String| {
        if warn_enabled {
            eprintln!("note: skipped writing system-manifest.yaml — {msg}");
        }
    };

    // Confine the write under the build tree (mirrors materialise_plan): a plan
    // build_root that's absolute or escapes via `..` is refused.
    let rel = Path::new(&plan.build_root);
    if rel.is_absolute() || rel.components().any(|c| matches!(c, Component::ParentDir)) {
        warn(format!("unsafe build_root `{}`", plan.build_root));
        return;
    }

    let yaml = match invoke_sdk_emit(context, "system-manifest", "build.manifest-unavailable") {
        Ok(y) => y,
        Err((_, msg)) => return warn(msg),
    };
    let mut manifest = match parse_system_manifest(&yaml) {
        Ok(m) => m,
        Err(e) => return warn(e.to_string()),
    };

    let overlay: Vec<(String, String, Option<String>, Option<String>)> = results
        .iter()
        // Carry the real artefact/build_dir the run resolved; None preserves the
        // plan-time value (e.g. for a skipped or non-zephyr slice).
        .map(|r| {
            (
                r.core_id.clone(),
                r.status.clone(),
                r.output_artefact.clone(),
                r.build_dir.clone(),
            )
        })
        .collect();
    overlay_run_results(&mut manifest, &overlay);

    let out = match serialize_system_manifest(&manifest) {
        Ok(s) => s,
        Err(e) => return warn(e),
    };

    let dest = Path::new(base).join(rel).join("system-manifest.yaml");
    if let Some(parent) = dest.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            return warn(e.to_string());
        }
    }
    if let Err(e) = std::fs::write(&dest, out) {
        warn(e.to_string());
    }
}

/// After a slice builds `ok`, resolve the real `zephyr.elf` west produced and
/// its build dir. West's default output is a nested `build/` under the slice's
/// run cwd, so the elf lands at `<cwd>/build/zephyr/zephyr.elf`. Returned
/// ABSOLUTE so every consumer (`size`/`renode`/`flash`/`image`) uses the paths
/// verbatim without re-anchoring under its own build_root. `(None, None)` when
/// no elf is there — a non-Zephyr backend, or a build that wrote elsewhere — so
/// the manifest keeps its plan-time values.
///
/// ponytail: assumes west's default nested `build/` dir; a slice that passes
/// `-d <other>` isn't matched here — the `is_file` gate then falls back to the
/// plan-time paths (no regression), upgrade to parse `-d` from the argv if a
/// custom build dir ever ships in a plan command.
fn resolve_zephyr_artefact(slice_cwd: &Path) -> (Option<String>, Option<String>) {
    let west_build = slice_cwd.join("build");
    let elf = west_build.join("zephyr").join("zephyr.elf");
    if elf.is_file() {
        (abs_string(&elf), abs_string(&west_build))
    } else {
        (None, None)
    }
}

/// Absolute, lossy-string form of a path (no filesystem round-trip beyond
/// `std::path::absolute`; falls back to the path as-is if that fails).
fn abs_string(p: &Path) -> Option<String> {
    Some(
        std::path::absolute(p)
            .unwrap_or_else(|_| p.to_path_buf())
            .to_string_lossy()
            .into_owned(),
    )
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

    fn slice_env(pairs: &[(&str, &str)]) -> std::collections::BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn append_map(pairs: &[(&str, &[&str])]) -> std::collections::BTreeMap<String, Vec<String>> {
        pairs
            .iter()
            .map(|(k, vs)| (k.to_string(), vs.iter().map(|s| s.to_string()).collect()))
            .collect()
    }

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("{tag}-{}", std::process::id()))
    }

    #[test]
    fn env_overrides_set_base_and_modules_when_absent() {
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[("ALP_SDK_ROOT", "/sdk")]),
            &append_map(&[]),
        );
        assert_eq!(
            got,
            vec![
                ("ZEPHYR_BASE", "/ws/zephyr".to_string()),
                ("EXTRA_ZEPHYR_MODULES", "/sdk".to_string()),
            ]
        );
    }

    #[test]
    fn env_overrides_respect_plan_pinned_keys() {
        // The plan already pins both -> nothing is overridden.
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[
                ("ZEPHYR_BASE", "/pinned"),
                ("EXTRA_ZEPHYR_MODULES", "/pinned-mod"),
            ]),
            &append_map(&[]),
        );
        assert!(got.is_empty());
    }

    #[test]
    fn env_overrides_skip_extra_modules_when_plan_appends_it() {
        // Plan wins / CLI fills gaps: the plan carries EXTRA_ZEPHYR_MODULES in
        // env_append_path, so the CLI must NOT hand-derive it — but ZEPHYR_BASE
        // (which the plan never carries) is still filled in.
        let got = zephyr_env_overrides(
            Some(Path::new("/ws/zephyr")),
            Some(Path::new("/sdk")),
            &slice_env(&[]),
            &append_map(&[("EXTRA_ZEPHYR_MODULES", &["/plan/sdk"])]),
        );
        assert_eq!(got, vec![("ZEPHYR_BASE", "/ws/zephyr".to_string())]);
    }

    #[test]
    fn env_overrides_empty_when_nothing_resolved() {
        assert!(zephyr_env_overrides(None, None, &slice_env(&[]), &append_map(&[])).is_empty());
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
