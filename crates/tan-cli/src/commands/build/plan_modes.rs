// SPDX-License-Identifier: Apache-2.0
//! The plan-inspection / manifest modes: `tan build --manifest`, `--plan`,
//! `--materialise`, and the shared success/error `CommandRun` builders they and
//! the native build reuse.

use std::path::Path;

use serde::Serialize;
use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, parse_build_plan, summarize_plan};
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
/// either show it or materialise its files. No execution.
pub(super) fn plan_command(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
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
    let plan = match apply_plan_token_substitution(g, &context, &base, &plan) {
        Ok(plan) => plan,
        Err((code, message)) => {
            return plan_error_run(g, project, code, message, ExitCode::RuntimeFailure);
        }
    };

    match materialise_plan(&plan, Path::new(&base)) {
        Ok(written) => materialise_ok_run(g, project, &base, written),
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
/// listing of the written files.
fn materialise_ok_run(
    g: &GlobalArgs,
    project: Project,
    base: &str,
    written: Vec<String>,
) -> CommandRun {
    if g.is_json() {
        let data = MaterialiseData {
            schema_version: "1".to_string(),
            base_dir: base.to_string(),
            written: written.clone(),
        };
        let json =
            Envelope::new("build", project, data, Vec::new(), ExitCode::Success.code()).to_json();
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
