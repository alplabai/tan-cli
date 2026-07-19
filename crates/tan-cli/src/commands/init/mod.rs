// SPDX-License-Identifier: Apache-2.0
//! `tan init` — initialize a new tan project from a template.

use std::path::PathBuf;

use tan_core::wizard::{WizardPlanInput, app_core_for_sku, create_wizard_plan_with_cores};

use super::CommandRun;
use crate::cli::{GlobalArgs, InitArgs};
use crate::exit::ExitCode;

mod from_example;
mod resolve;
mod response;

use from_example::{finish, run_from_example};
use resolve::ResolveErr::{BadArg, Cancelled};
use resolve::{parse_cores, resolve_destination, resolve_name, resolve_template};
use response::{error_run, runtime_failure_run};

// ---------------------------------------------------------------------------
// JSON envelope data
// ---------------------------------------------------------------------------

/// One planned file change in the JSON envelope: its workspace-relative path and
/// change kind (`create`/`update`/`unchanged`, from `WizardFileChangeKind`).
#[derive(serde::Serialize)]
struct FileChangeSer {
    #[serde(rename = "relativePath")]
    relative_path: String,
    kind: String,
}

/// `data` payload for the `init` envelope: the resolved template/destination,
/// whether this was a preview, the planned `file_changes`, and post-write
/// `written`/`unchanged` lists.
#[derive(serde::Serialize)]
struct InitData {
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    #[serde(rename = "templateId")]
    template_id: String,
    destination: String,
    preview: bool,
    #[serde(rename = "fileChanges")]
    file_changes: Vec<FileChangeSer>,
    written: Vec<String>,
    unchanged: Vec<String>,
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Execute `tan init`: resolve template/name/destination (prompting when
/// interactive), build the scaffold plan (heterogeneous when `--cores` is given),
/// then preview or write files — guarding overwrites behind `--force`.
pub fn run(g: &GlobalArgs, args: &InitArgs) -> CommandRun {
    let is_interactive = !g.non_interactive && !g.ci;

    // From-example path: copy an existing SDK example verbatim. Short-circuits
    // before template resolution so it never engages the non-interactive
    // MinimalApp default; --som/--cores are ignored (the example ships its own
    // board.yaml).
    if let Some(src) = args.from_example.as_deref() {
        return run_from_example(g, args, src, is_interactive);
    }

    // 1. Resolve template.
    let template_id = match resolve_template(args.template.as_deref(), is_interactive) {
        Ok(id) => id,
        Err(Cancelled) => {
            eprintln!("Cancelled.");
            return runtime_failure_run();
        }
        Err(BadArg(msg)) => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "init.invalid-template",
                &msg,
            );
        }
    };

    // 2. Resolve name (optional).
    let name = match resolve_name(args.name.as_deref(), is_interactive) {
        Ok(n) => n,
        Err(_) => {
            eprintln!("Cancelled.");
            return runtime_failure_run();
        }
    };

    // 3. Resolve destination.
    let destination = match resolve_destination(
        args.destination.as_deref(),
        g.project.as_deref(),
        is_interactive,
    ) {
        Ok(d) => d,
        Err(_) => {
            eprintln!("Cancelled.");
            return runtime_failure_run();
        }
    };

    // 4. Compute project root.
    let dest_path = PathBuf::from(&destination);
    let project_root = if name.is_empty() {
        dest_path.clone()
    } else {
        dest_path.join(&name)
    };

    // 5. Build plan (heterogeneous when --cores is given; else single-core).
    let cores = match parse_cores(args.cores.as_deref()) {
        Ok(cores) => cores,
        Err(msg) => return error_run(g, ExitCode::ValidationFailure, "init.invalid-cores", &msg),
    };
    // The app core's runtime is fixed (the scaffolded src/ + prj.conf are
    // Zephyr); reject a contradictory --cores request instead of silently
    // overriding it.
    let app_core = app_core_for_sku(args.som.as_deref().unwrap_or(tan_core::DEFAULT_SOM_SKU));
    if let Some((_, os)) = cores
        .iter()
        .find(|(id, os)| id.as_str() == app_core && os.as_str() != "zephyr")
    {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-cores",
            &format!(
                "Core '{app_core}' is this SoM's app core and runs zephyr; --cores requested '{os}'. Omit the entry or use {app_core}:zephyr."
            ),
        );
    }
    let mut plan = create_wizard_plan_with_cores(
        &WizardPlanInput {
            template_id,
            project_name: name.clone(),
            destination: destination.clone(),
            som_sku: args.som.clone(),
        },
        &cores,
    );

    // 5b. Honor --board-yaml: emit the caller's board.yaml verbatim instead of the
    // generated stub. This lets Alp Studio adopt `tan init` as its project render --
    // it passes a fully-resolved board.yaml and expects it copied through untouched
    // (alp-sdk-vscode#64). Templates that emit no board.yaml (host-tooling-starter)
    // have nothing to override, so pairing them with --board-yaml is a hard error
    // rather than a silent no-op that would drop the caller's file.
    if let Some(path) = g.board_yaml.as_deref() {
        match std::fs::read_to_string(path) {
            Ok(content) => {
                let mut applied = false;
                for file in plan.files.iter_mut() {
                    if file.relative_path == "board.yaml" {
                        file.content = content.clone();
                        applied = true;
                    }
                }
                if !applied {
                    return error_run(
                        g,
                        ExitCode::ValidationFailure,
                        "init.board-yaml-unsupported",
                        &format!(
                            "--board-yaml was given but template '{}' emits no board.yaml to override.",
                            template_id.as_str()
                        ),
                    );
                }
            }
            Err(err) => {
                return error_run(
                    g,
                    ExitCode::ValidationFailure,
                    "init.board-yaml-unreadable",
                    &format!("--board-yaml '{path}' could not be read: {err}"),
                );
            }
        }
    }

    // 6-9. Diff, guard overwrites, preview, write (shared with the from-example path).
    finish(
        g,
        args,
        template_id.as_str(),
        &destination,
        &project_root,
        &plan.files,
    )
}
