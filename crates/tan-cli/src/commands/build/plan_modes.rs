// SPDX-License-Identifier: Apache-2.0
//! The plan-inspection / manifest modes: `tan build --manifest`, `--plan`,
//! `--materialise`, and the shared success/error `CommandRun` builders they and
//! the native build reuse.

use std::path::Path;

use serde::Serialize;
use tan_core::build_plan::{BuildPlan, summarize_plan};
use tan_core::system_manifest::{parse_system_manifest, summarize_manifest};

use super::CommandRun;
use crate::cli::{BuildArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use super::materialise::materialise_plan;
use super::native::{acquire_plan, base_dir};
use super::workspace::invoke_sdk_emit;

/// `tan build --manifest [--manifest-from FILE]` — the post-build IDE/tool
/// contract (`build/system-manifest.yaml`). With `--manifest-from` we read a
/// local manifest (e.g. one `west alp-build` already wrote); otherwise we ask
/// the SDK for the projection (`alp_orchestrate.py --emit system-manifest`,
/// status: pending). Either way we parse + version-guard it and emit the
/// manifest in the envelope (the IDE reads this instead of shelling python).
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
        None => match invoke_sdk_emit(&context, "system-manifest", "build.manifest-unavailable") {
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

    let plan = match acquire_plan(&context, args) {
        Ok(plan) => plan,
        Err((code, message)) => {
            return plan_error_run(g, project, code, message, ExitCode::RuntimeFailure);
        }
    };

    if !args.materialise {
        return show_plan_run(g, project, &plan);
    }

    let base = base_dir(&context);
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

/// Render the acquired build plan without executing: JSON emits the plan in an
/// envelope; text emits `summarize_plan`.
fn show_plan_run(g: &GlobalArgs, project: Project, plan: &BuildPlan) -> CommandRun {
    if g.is_json() {
        let json =
            Envelope::new("build", project, plan, Vec::new(), ExitCode::Success.code()).to_json();
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
