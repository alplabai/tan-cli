// SPDX-License-Identifier: Apache-2.0
//! The plan-inspection / manifest modes: `tan build --manifest`, `--plan`,
//! `--materialise`, and the shared success/error `CommandRun` builders they and
//! the native build reuse.

use std::path::Path;

use serde::Serialize;
use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, PolicyAction, parse_build_plan, summarize_plan};
use tan_core::plan_exec::resolve_action;
use tan_core::system_manifest::{parse_system_manifest, summarize_manifest};

use super::CommandRun;
use crate::cli::{BuildArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use super::materialise::materialise_plan;
use super::native::base_dir;
use super::token_substitution::apply_plan_token_substitution;
use super::workspace::invoke_sdk_emit;

/// `tan build --manifest [--manifest-from FILE]` — the post-build IDE/tool
/// contract (`build/system-manifest.yaml`). With `--manifest-from` we read a
/// local manifest (the one `tan build --native` already wrote); otherwise we
/// ask the SDK for the projection (`alp_orchestrate --emit system-manifest`,
/// registered in `emit-registry-v1.json`). Either way we parse + version-guard
/// it and emit the manifest in the envelope (the IDE reads this instead of
/// shelling python).
pub(super) fn manifest_command(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let yaml = match &args.manifest_from {
        Some(path) => match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(e) => {
                return plan_error_run(
                    g,
                    project,
                    "build.manifest-unavailable",
                    format!("failed to read manifest file `{path}`: {e}"),
                    ExitCode::RuntimeFailure,
                );
            }
        },
        None => match invoke_sdk_emit(
            &context,
            "system-manifest",
            "build.manifest-unavailable",
            &[],
            &[],
        ) {
            Ok(s) => s,
            Err((code, message)) => {
                return plan_error_run(g, project, code, message, ExitCode::RuntimeFailure);
            }
        },
    };

    match parse_system_manifest(&yaml) {
        Ok(manifest) => {
            if g.is_json() {
                let json = Envelope::new(
                    "build",
                    project,
                    &manifest,
                    Vec::new(),
                    ExitCode::Success.code(),
                )
                .to_json();
                CommandRun {
                    exit: ExitCode::Success,
                    text: Vec::new(),
                    json: Some(json),
                }
            } else {
                CommandRun {
                    exit: ExitCode::Success,
                    text: summarize_manifest(&manifest),
                    json: None,
                }
            }
        }
        Err(e) => plan_error_run(
            g,
            project,
            "build.manifest-invalid",
            e.to_string(),
            ExitCode::RuntimeFailure,
        ),
    }
}

/// `tan build --plan [--plan-from FILE] [--materialise]` — consume the build
/// plan (the SDK's single source of truth; the CLI only deserializes it), then
/// either show it or materialise its files. No execution. Thin wrapper
/// resolving the host's toolchain root exactly once (see
/// [`plan_command_with_toolchain`]) — the production entry every real caller
/// (`build/mod.rs`'s dispatch) uses.
pub(super) fn plan_command(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
    plan_command_with_toolchain(g, args, &crate::toolchain::resolve_toolchain_root())
}

/// [`plan_command`]'s actual body, with the resolved `ToolchainRoot` threaded
/// in as a parameter instead of calling `crate::toolchain::resolve_toolchain_
/// root()` itself: a previous version resolved it internally, which made this
/// function's own `--materialise` demotion tests silently host-dependent —
/// they assumed no toolchain was installed, but actually got whatever this
/// machine's `ZEPHYR_SDK_INSTALL_DIR`/`/opt`/`$HOME` happen to resolve to,
/// passing on a clean CI box and failing on any developer machine with a real
/// Zephyr SDK. Splitting the resolution out (mirrors `execute_slices` vs
/// `execute_slices_outcome`'s test/production split) lets a test inject a
/// fixed value and stay deterministic regardless of ambient host state.
fn plan_command_with_toolchain(
    g: &GlobalArgs,
    args: &BuildArgs,
    toolchain: &crate::toolchain::ToolchainRoot,
) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let json = match acquire_plan_json(&context, args) {
        Ok(json) => json,
        Err((code, message)) => {
            return plan_error_run(g, project, code, message, ExitCode::RuntimeFailure);
        }
    };
    let plan = match parse_build_plan(&json) {
        Ok(plan) => plan,
        Err(e) => {
            return plan_error_run(
                g,
                project,
                "build.plan-invalid",
                e.to_string(),
                ExitCode::RuntimeFailure,
            );
        }
    };

    if !args.materialise {
        return show_plan_run(g, project, &plan, &json);
    }

    // Build-plan token substitution (alp-sdk #865): the SAME pass/guards
    // `native::native_build_outcome` applies, run here too — a tokened plan
    // reaching `materialise_plan` unsubstituted would write literal
    // `${SDK_ROOT}`/`${PROJECT_ROOT}` into config-artefact contents (and,
    // since `${`/`}` are ordinary path characters, even create a literal
    // `${PROJECT_ROOT}/` directory) instead of erroring or resolving them.
    let base = base_dir(&context);
    let (plan, demoted) = match apply_plan_token_substitution(g, &context, &base, &plan, toolchain)
    {
        Ok(result) => result,
        Err((code, message)) => {
            return plan_error_run(g, project, code, message, ExitCode::RuntimeFailure);
        }
    };

    // tan-cli #89: a demoted slice's `configArtefacts` were already stripped
    // INSIDE the substituted plan (`tan_core::plan_tokens`), so the write
    // below never sees them — `materialise_plan` itself needs no changes.
    // What `--native`'s executor decides per-slice at dispatch, `--materialise`
    // has to decide HERE instead: there is no per-slice dispatch seam to
    // attach a fail/skip outcome to, just one write. Checked BEFORE
    // `materialise_plan` runs: writing the OTHER slices' artefacts under a
    // policy that declares this condition fatal would leave a half-
    // materialised tree behind an error exit — and this also matches
    // pre-#89 behaviour for the fail case (nothing written), unlike
    // `--native`, which now builds the other slices and fails only the
    // demoted one (see the executor's own doc for why the two routes differ).
    let fail = resolve_action(
        plan.execution_policy.as_ref(),
        |p| p.missing_tool,
        PolicyAction::Skip,
    ) == PolicyAction::Fail;
    if fail && !demoted.is_empty() {
        let message = demoted
            .iter()
            .map(|d| format!("slice `{}`: {}", d.core_id, d.reason))
            .collect::<Vec<_>>()
            .join("\n");
        return plan_error_run(
            g,
            project,
            "build.toolchain-root-unresolved",
            message,
            ExitCode::RuntimeFailure,
        );
    }

    match materialise_plan(&plan, Path::new(&base)) {
        Ok(written) => {
            // Skip (the common case): the demoted artefacts are simply
            // absent from `written` — still fold a warning Issue per demoted
            // slice so the skip is loud in the envelope too, not just an
            // omission a caller has to notice on its own.
            let issues = demoted
                .iter()
                .map(|d| Issue {
                    code: "build.toolchain-root-unresolved".to_string(),
                    severity: "warning".to_string(),
                    message: format!(
                        "slice `{}`: configArtefacts not materialised — {}",
                        d.core_id, d.reason
                    ),
                })
                .collect();
            materialise_ok_run(g, project, &base, written, issues)
        }
        Err(e) => plan_error_run(
            g,
            project,
            "build.materialise-failed",
            e.message(),
            ExitCode::WriteFailure,
        ),
    }
}

/// Acquire the plan's raw JSON — from `--plan-from <FILE>` if given, else the
/// SDK's `--emit build-plan` — mirroring `native::acquire_plan`'s sourcing but
/// keeping the unparsed `serde_json::Value`, not just the typed `BuildPlan`.
/// `--plan --format json` needs the raw JSON: the SDK's per-slice schema
/// requires fields (`appDir`, `toolchain`, `artifacts`, `debug`, `sdkVersion`,
/// `sdkCommit`, …) that `BuildPlan` doesn't model yet, so re-serialising the
/// typed struct would drop them and emit a schema-invalid plan.
fn acquire_plan_json(
    context: &ProjectContext,
    args: &BuildArgs,
) -> Result<String, (&'static str, String)> {
    match &args.plan_from {
        Some(path) => std::fs::read_to_string(path).map_err(|e| {
            (
                "build.plan-unavailable",
                format!("failed to read plan file `{path}`: {e}"),
            )
        }),
        None => invoke_sdk_emit(context, "build-plan", "build.plan-unavailable", &[], &[]),
    }
}

/// Render the acquired build plan without executing: JSON passes the raw
/// SDK-emitted `raw_json` straight through in the envelope (so schema-required
/// fields the typed `BuildPlan` doesn't model survive verbatim, per ADR 0014);
/// text emits `summarize_plan` off the parsed `plan`.
fn show_plan_run(g: &GlobalArgs, project: Project, plan: &BuildPlan, raw_json: &str) -> CommandRun {
    if g.is_json() {
        // `raw_json` was already parsed once by `parse_build_plan` (the caller),
        // so re-parsing to a generic `Value` here cannot fail.
        let value: serde_json::Value =
            serde_json::from_str(raw_json).expect("raw_json already validated by parse_build_plan");
        let json = Envelope::new(
            "build",
            project,
            value,
            Vec::new(),
            ExitCode::Success.code(),
        )
        .to_json();
        CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit: ExitCode::Success,
            text: summarize_plan(plan),
            json: None,
        }
    }
}

/// Envelope `data` for a `--materialise` run: where files were written and which.
#[derive(Serialize)]
struct MaterialiseData {
    /// Envelope `data` schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Base dir the artefacts were written under.
    #[serde(rename = "baseDir")]
    base_dir: String,
    /// Relative paths of every artefact written.
    written: Vec<String>,
}

/// Build the success `CommandRun` after materialising: JSON envelope or a text
/// listing of the written files. `issues` (tan-cli #89) is a warning per
/// slice this run demoted and skipped — a `--materialise` run can be `ok`
/// with fewer files written than the plan names, and this is how that stays
/// visible instead of a caller having to notice the omission on its own.
/// Single caller (the `--materialise` success path above), so a plain `Vec`
/// param is fine — no builder needed for one call site.
fn materialise_ok_run(
    g: &GlobalArgs,
    project: Project,
    base: &str,
    written: Vec<String>,
    issues: Vec<Issue>,
) -> CommandRun {
    if g.is_json() {
        let data = MaterialiseData {
            schema_version: "1".to_string(),
            base_dir: base.to_string(),
            written: written.clone(),
        };
        let json =
            Envelope::new("build", project, data, issues, ExitCode::Success.code()).to_json();
        CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        let mut text = vec![format!(
            "materialised {} file(s) under {}:",
            written.len(),
            base
        )];
        text.extend(written.into_iter().map(|p| format!("  {p}")));
        // Text mode has no separate issues[] to read — fold the same warning
        // into the recap so a skip is loud on a terminal too, not just in JSON.
        for issue in &issues {
            text.push(format!("note: {}", issue.message));
        }
        CommandRun {
            exit: ExitCode::Success,
            text,
            json: None,
        }
    }
}

/// Build a failure `CommandRun` carrying a single `Issue` (`code`/`message`) at
/// the given `exit`: JSON emits a null-data envelope; text emits `build: <msg>`.
pub(super) fn plan_error_run(
    g: &GlobalArgs,
    project: Project,
    code: &str,
    message: String,
    exit: ExitCode,
) -> CommandRun {
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    if g.is_json() {
        let json = Envelope::new(
            "build",
            project,
            serde_json::Value::Null,
            issues,
            exit.code(),
        )
        .to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit,
            text: vec![format!("build: {message}")],
            json: None,
        }
    }
}

#[cfg(test)]
mod show_plan_run_tests {
    use super::*;

    fn json_args() -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: crate::cli::Format::Json,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    // The SDK's per-slice schema requires fields (e.g. `appDir`) that
    // `BuildSlice` doesn't model. `--plan --format json` must pass the raw
    // emitted JSON through verbatim, not re-serialise the typed `BuildPlan` —
    // else a required field silently disappears from the plan tan hands back.
    #[test]
    fn json_envelope_preserves_fields_the_typed_plan_does_not_model() {
        let raw_json = r#"{
          "schemaVersion": 1,
          "boardYaml": "/proj/board.yaml",
          "sku": "E1M-AEN701",
          "buildRoot": "build",
          "slices": [
            {
              "coreId": "m55_hp",
              "backend": "zephyr",
              "buildDir": "build/m55_hp-zephyr",
              "appDir": "app",
              "toolchain": "zephyr-0.16",
              "artifacts": ["zephyr.elf"],
              "debug": { "gdbPort": 3333 },
              "sdkVersion": "0.11.0",
              "sdkCommit": "deadbeef",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/m55_hp-zephyr" },
              "env": {}
            }
          ],
          "sharedArtefacts": [],
          "warnings": []
        }"#;
        let plan = parse_build_plan(raw_json).expect("sample plan must parse");
        let project = Project {
            root: None,
            board_yaml: None,
        };

        let run = show_plan_run(&json_args(), project, &plan, raw_json);
        let json = run.json.expect("json mode must emit a json body");
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(value["data"]["slices"][0]["appDir"], "app");
        assert_eq!(value["data"]["slices"][0]["sdkVersion"], "0.11.0");
        assert_eq!(value["data"]["slices"][0]["sdkCommit"], "deadbeef");
    }
}

/// tan-cli #89's `--materialise` route: a slice-confined unresolved
/// `${TOOLCHAIN_ROOT}` must not fail the write outright, but has no
/// per-slice dispatch seam either — so `plan_command` decides once, up
/// front (see the `--materialise` branch's own comment). Exercises
/// `plan_command_with_toolchain` directly (not through `Cli::parse_from` —
/// mirrors `show_plan_run_tests` above, which does the same for this file's
/// other private helpers) with a REAL SDK-root fixture on disk, so
/// `apply_plan_token_substitution`'s `resolve_sdk_root` seam runs for real —
/// but with an EXPLICIT `ToolchainRoot::NotFound` rather than the production
/// entry's `resolve_toolchain_root()`, so these tests stay deterministic
/// regardless of whether the machine running `cargo test` actually has a
/// Zephyr SDK installed (see `plan_command_with_toolchain`'s own doc).
#[cfg(test)]
mod materialise_toolchain_demotion_tests {
    use super::*;

    fn global(project: &std::path::Path, sdk_root: &std::path::Path) -> GlobalArgs {
        GlobalArgs {
            project: Some(project.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: Some(sdk_root.to_string_lossy().into_owned()),
            target: None,
            all: false,
            format: crate::cli::Format::Json,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    fn materialise_args(plan_from: &str) -> BuildArgs {
        BuildArgs {
            plan: false,
            plan_from: Some(plan_from.to_string()),
            materialise: true,
            native: false,
            manifest: false,
            manifest_from: None,
            no_auto_bootstrap: false,
            pristine: false,
        }
    }

    /// A real temp dir marked as an SDK root (`scripts/alp_project.py`), so
    /// `--sdk-root <dir>` makes `resolve_sdk_root` deterministically resolve
    /// to it — same fixture shape `token_substitution.rs`'s own tests use.
    fn sdk_root_dir(tag: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("tan-plan-modes-sdk-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("alp_project.py"), b"").unwrap();
        dir
    }

    fn project_dir(tag: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("tan-plan-modes-proj-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A two-slice tokened plan: slice `c1` names an unresolved
    /// `${TOOLCHAIN_ROOT}` in its own `env` (demotable, tan-cli #89); slice
    /// `c2` names none, and must materialise regardless of `c1`'s demotion.
    fn two_slice_toolchain_confined_plan() -> &'static str {
        r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "configArtefacts": [
                { "path": "build/c1/alp.conf", "contents": "CONFIG_GPIO=y\n" }
              ],
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
              "env": { "ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}" } },
            { "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2",
              "configArtefacts": [
                { "path": "build/c2/alp.conf", "contents": "CONFIG_UART=y\n" }
              ],
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c2" } }
          ],
          "sharedArtefacts": []
        }"#
    }

    #[test]
    fn materialise_skips_a_slice_confined_toolchain_root_with_a_warning_issue() {
        // Default (skip) policy: the demoted slice's configArtefacts are
        // simply absent from disk and from `written` — but the run stays
        // exit 0 AND the omission is loud in the envelope (a warning Issue
        // naming the slice), not a silent gap a caller has to notice on its
        // own. The OTHER slice is unaffected.
        let sdk_root = sdk_root_dir("skip");
        let project = project_dir("skip");
        let plan_path = project.join("plan.json");
        std::fs::write(&plan_path, two_slice_toolchain_confined_plan()).unwrap();

        let g = global(&project, &sdk_root);
        let args = materialise_args(plan_path.to_str().unwrap());
        let run =
            plan_command_with_toolchain(&g, &args, &crate::toolchain::ToolchainRoot::NotFound);
        assert_eq!(run.exit.code(), 0, "run: {:?}", run.json);

        assert!(
            !project.join("build/c1/alp.conf").exists(),
            "a demoted slice's configArtefacts must not be written"
        );
        assert!(
            project.join("build/c2/alp.conf").exists(),
            "the other slice must materialise normally"
        );

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let written = env["data"]["written"].as_array().unwrap();
        assert!(
            !written.iter().any(|w| w.as_str().unwrap().contains("c1")),
            "got: {written:?}"
        );
        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "build.toolchain-root-unresolved"
                    && i["severity"] == "warning"
                    && i["message"].as_str().unwrap().contains("c1")),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&project).ok();
        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn materialise_writes_nothing_when_policy_says_fail() {
        // Same plan, `executionPolicy.missingTool: "fail"`: `--materialise`
        // has no per-slice dispatch to attach a failure to, so it refuses
        // BEFORE writing anything — nothing partial is left behind an error
        // exit, matching pre-#89 behaviour for the fail case (unlike
        // `--native`, which now builds the other slice and fails only the
        // demoted one — the two routes deliberately differ, see the
        // `--materialise` branch's own comment above).
        let sdk_root = sdk_root_dir("fail");
        let project = project_dir("fail");
        let plan_path = project.join("plan.json");
        let plan_json = two_slice_toolchain_confined_plan().replace(
            r#""sharedArtefacts": []"#,
            r#""executionPolicy": { "missingTool": "fail" }, "sharedArtefacts": []"#,
        );
        std::fs::write(&plan_path, plan_json).unwrap();

        let g = global(&project, &sdk_root);
        let args = materialise_args(plan_path.to_str().unwrap());
        let run =
            plan_command_with_toolchain(&g, &args, &crate::toolchain::ToolchainRoot::NotFound);
        assert_ne!(run.exit.code(), 0, "run: {:?}", run.json);
        assert!(!project.join("build/c1/alp.conf").exists());
        assert!(
            !project.join("build/c2/alp.conf").exists(),
            "the fail route writes NOTHING, not just the demoted slice's files"
        );

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "build.toolchain-root-unresolved"
                    && i["message"].as_str().unwrap().contains("c1")),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&project).ok();
        std::fs::remove_dir_all(&sdk_root).ok();
    }
}
