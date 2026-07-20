// SPDX-License-Identifier: Apache-2.0
//! `tan scaffold` — scaffold a module into an existing tan project.

use inquire::{InquireError, Select, Text};
use std::path::PathBuf;
use tan_core::wizard::{
    ModuleScaffoldInput, ModuleTemplateId, WizardFileChangeKind, collect_wizard_file_changes,
    create_module_scaffold_plan, create_scaffold_tree_preview, list_module_templates,
    write_wizard_files,
};

use super::CommandRun;
use crate::cli::{GlobalArgs, ScaffoldArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

// ---------------------------------------------------------------------------
// JSON envelope data
// ---------------------------------------------------------------------------

/// One planned file change in the JSON envelope: a project-relative path and
/// its `kind` (`create` / `update` / `unchanged`).
#[derive(serde::Serialize)]
struct FileChangeSer {
    #[serde(rename = "relativePath")]
    relative_path: String,
    kind: String,
}

/// `data` payload for the `scaffold` envelope: resolved inputs plus the planned
/// file changes and (post-write) the `written` / `unchanged` path lists.
#[derive(serde::Serialize)]
struct ScaffoldData {
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    #[serde(rename = "templateId")]
    template_id: String,
    #[serde(rename = "moduleName")]
    module_name: String,
    #[serde(rename = "normalizedModuleName")]
    normalized_module_name: String,
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

/// Entry point for `tan scaffold`: resolve module name + template + destination,
/// build the scaffold plan, then guard overwrites, preview, or write files —
/// returning the `CommandRun` (exit code, human text, optional JSON envelope).
pub fn run(g: &GlobalArgs, args: &ScaffoldArgs) -> CommandRun {
    // `--format json` is the mode the extension always uses and never has a
    // human at the keyboard to answer a prompt; omitting it here let a JSON
    // caller with no `--name`/`--template` block forever on an inquire prompt
    // rendered to stderr, or — if stdin was already closed — cancel it and
    // exit 1 with zero bytes on stdout (mirrors the identical fix in `init`).
    let is_interactive = !g.non_interactive && !g.ci && !g.is_json();

    // 1. Resolve module name (required).
    let module_name = match resolve_module_name(args.name.as_deref(), is_interactive) {
        Ok(n) => n,
        Err(NeedName) => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "scaffold.name-required",
                "Module name is required. Use --name <name> or run interactively.",
            );
        }
        Err(Cancelled) | Err(BadArg(_)) => return runtime_failure_run(g),
    };

    // 2. Resolve template.
    let template_id = match resolve_template(args.template.as_deref(), is_interactive) {
        Ok(id) => id,
        Err(Cancelled) => return runtime_failure_run(g),
        Err(BadArg(msg)) => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "scaffold.invalid-template",
                &msg,
            );
        }
        Err(NeedName) => unreachable!(),
    };

    // 3. Resolve destination.
    let destination = args
        .destination
        .as_deref()
        .or(g.project.as_deref())
        .unwrap_or(".")
        .to_string();

    let project_root = PathBuf::from(&destination);

    // 4. Build plan.
    let plan = match create_module_scaffold_plan(&ModuleScaffoldInput {
        template_id,
        module_name: module_name.clone(),
        destination: destination.clone(),
    }) {
        Ok(p) => p,
        Err(e) => return error_run(g, ExitCode::ValidationFailure, "scaffold.invalid-name", &e),
    };

    // 5. Collect file changes.
    let changes = collect_wizard_file_changes(&project_root, &plan.files);
    let file_changes_ser: Vec<FileChangeSer> = changes
        .iter()
        .map(|c| FileChangeSer {
            relative_path: c.relative_path.clone(),
            kind: c.kind.as_str().to_string(),
        })
        .collect();

    let has_updates = changes
        .iter()
        .any(|c| c.kind == WizardFileChangeKind::Update);

    // 6. Preview mode. Checked BEFORE the overwrite guard: `--preview` never
    // touches disk, so it has nothing to be guarded against, but the guard
    // used to run first and reject a preview of a project with local edits
    // with `scaffold.would-overwrite` (exit 3) instead of showing the plan
    // (same ordering bug as `tan init`, see init/from_example.rs).
    if args.preview {
        let tree = create_scaffold_tree_preview(&plan.files);
        let project = make_project(&destination);
        let data = empty_data(
            template_id,
            &module_name,
            &plan.normalized_name,
            &destination,
            true,
            file_changes_ser,
        );
        let text = if g.is_json() {
            vec![]
        } else {
            vec![
                format!(
                    "scaffold: preview for module '{}' (template '{}')",
                    plan.normalized_name,
                    template_id.as_str()
                ),
                tree,
            ]
        };
        let json = g.is_json().then(|| {
            Envelope::new("scaffold", project, data, vec![], ExitCode::Success.code()).to_json()
        });
        return CommandRun {
            exit: ExitCode::Success,
            text,
            json,
        };
    }

    // 7. Guard against unforced overwrites.
    if has_updates && !args.force {
        let project = make_project(&destination);
        let data = empty_data(
            template_id,
            &module_name,
            &plan.normalized_name,
            &destination,
            args.preview,
            file_changes_ser,
        );
        let issues = vec![Issue {
            code: "scaffold.would-overwrite".to_string(),
            severity: "error".to_string(),
            message: "One or more files would be overwritten. Use --force to allow updates."
                .to_string(),
        }];
        let text = if g.is_json() {
            vec![]
        } else {
            vec!["scaffold: would overwrite existing files; use --force to proceed.".to_string()]
        };
        let json = g.is_json().then(|| {
            Envelope::new(
                "scaffold",
                project,
                data,
                issues,
                ExitCode::WriteFailure.code(),
            )
            .to_json()
        });
        return CommandRun {
            exit: ExitCode::WriteFailure,
            text,
            json,
        };
    }

    // 8. Write files.
    match write_wizard_files(&project_root, &plan.files) {
        Ok(result) => {
            let project = make_project(&destination);
            let data = ScaffoldData {
                schema_version: "1".to_string(),
                template_id: template_id.as_str().to_string(),
                module_name: module_name.clone(),
                normalized_module_name: plan.normalized_name.clone(),
                destination: destination.clone(),
                preview: false,
                file_changes: file_changes_ser,
                written: result.written.clone(),
                unchanged: result.unchanged.clone(),
            };
            let text = if g.is_json() {
                vec![]
            } else {
                vec![
                    format!(
                        "scaffold: created module '{}' (template '{}')",
                        plan.normalized_name,
                        template_id.as_str()
                    ),
                    format!(
                        "  written: {}, unchanged: {}",
                        result.written.len(),
                        result.unchanged.len()
                    ),
                ]
            };
            let json = g.is_json().then(|| {
                Envelope::new("scaffold", project, data, vec![], ExitCode::Success.code()).to_json()
            });
            CommandRun {
                exit: ExitCode::Success,
                text,
                json,
            }
        }
        // `e.partial` lists the files that landed before the failure — an
        // empty `written: []` here (what a plain error_run would report)
        // would contradict a project that is actually half on disk.
        Err(e) => write_error_run(g, &format!("Failed to write files: {}", e.error), e.partial),
    }
}

// ---------------------------------------------------------------------------
// Resolution helpers
// ---------------------------------------------------------------------------

/// Failure outcomes from the interactive/argument resolution helpers.
enum ResolveErr {
    /// User aborted an interactive prompt (Esc / Ctrl-C).
    Cancelled,
    /// No module name supplied and none can be prompted for.
    NeedName,
    /// An argument value was invalid (e.g. unknown template id).
    BadArg(String),
}
use ResolveErr::*;

/// Resolve the module name from `--name`, else an interactive prompt; errors
/// with `NeedName` when absent and non-interactive, `Cancelled` on abort.
fn resolve_module_name(arg: Option<&str>, interactive: bool) -> Result<String, ResolveErr> {
    if let Some(s) = arg {
        return Ok(s.to_string());
    }
    if interactive {
        return match Text::new("Module name:").prompt() {
            Ok(s) if s.trim().is_empty() => Err(NeedName),
            Ok(s) => Ok(s.trim().to_string()),
            Err(InquireError::OperationCanceled) | Err(InquireError::OperationInterrupted) => {
                Err(Cancelled)
            }
            Err(_) => Err(Cancelled),
        };
    }
    Err(NeedName)
}

/// Resolve the `ModuleTemplateId` from `--template`, else an interactive picker;
/// defaults to `SensorDriver` when non-interactive and no flag is given.
fn resolve_template(arg: Option<&str>, interactive: bool) -> Result<ModuleTemplateId, ResolveErr> {
    if let Some(s) = arg {
        return ModuleTemplateId::from_str(s)
            .ok_or_else(|| BadArg(format!("Unknown module template '{s}'.")));
    }
    if interactive {
        let templates = list_module_templates();
        let options: Vec<String> = templates
            .iter()
            .map(|d| format!("{} — {}", d.id.as_str(), d.label))
            .collect();
        return match Select::new("Select a module template:", options.clone()).prompt() {
            Ok(choice) => {
                let idx = options.iter().position(|o| *o == choice).unwrap_or(0);
                Ok(templates[idx].id)
            }
            Err(InquireError::OperationCanceled) | Err(InquireError::OperationInterrupted) => {
                Err(Cancelled)
            }
            Err(_) => Err(Cancelled),
        };
    }
    Ok(ModuleTemplateId::SensorDriver)
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

/// Build the envelope `Project` block with `destination` as the project root.
fn make_project(destination: &str) -> Project {
    Project {
        root: Some(destination.to_string()),
        board_yaml: None,
    }
}

/// Build `ScaffoldData` for the non-write paths (overwrite guard, preview):
/// inputs and `file_changes` are populated but `written`/`unchanged` stay empty.
fn empty_data(
    template_id: ModuleTemplateId,
    module_name: &str,
    normalized: &str,
    destination: &str,
    preview: bool,
    file_changes: Vec<FileChangeSer>,
) -> ScaffoldData {
    ScaffoldData {
        schema_version: "1".to_string(),
        template_id: template_id.as_str().to_string(),
        module_name: module_name.to_string(),
        normalized_module_name: normalized.to_string(),
        destination: destination.to_string(),
        preview,
        file_changes,
        written: vec![],
        unchanged: vec![],
    }
}

/// Build a `RuntimeFailure` `CommandRun` for a cancelled interactive prompt.
/// Always routes through `error_run` so it carries a real envelope — a bare
/// `json: None` here meant an interactive prompt reached under `--format json`
/// (which does not by itself imply non-interactive; see `run()`) could be
/// cancelled and exit 1 with zero bytes on stdout, unparseable by the extension.
fn runtime_failure_run(g: &GlobalArgs) -> CommandRun {
    error_run(
        g,
        ExitCode::RuntimeFailure,
        "scaffold.cancelled",
        "Cancelled.",
    )
}

/// Build an error `CommandRun` carrying a single error `Issue` (and a matching
/// text/JSON envelope) for the given `exit` code, issue `code`, and `message`.
/// Every call site used to hardcode `RuntimeFailure` (exit 1) regardless of
/// failure class — `tan init`'s equivalent returns `ValidationFailure` (2) for
/// bad input and `WriteFailure` (3) for a failed write, per exit.rs's
/// documented contract; a caller branching on exit code (CI, the extension)
/// could not tell "bad --name" from "disk full" from "prompt cancelled".
fn error_run(g: &GlobalArgs, exit: ExitCode, code: &str, message: &str) -> CommandRun {
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let data = ScaffoldData {
        schema_version: "1".to_string(),
        template_id: String::new(),
        module_name: String::new(),
        normalized_module_name: String::new(),
        destination: String::new(),
        preview: false,
        file_changes: vec![],
        written: vec![],
        unchanged: vec![],
    };
    let text = if g.is_json() {
        vec![]
    } else {
        vec![format!("scaffold: {message}")]
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("scaffold", project, data, issues, exit.code()).to_json());
    CommandRun { exit, text, json }
}

/// Build a `WriteFailure` `CommandRun` for a failed `write_wizard_files` call,
/// carrying the partial `written`/`unchanged` lists accumulated before the
/// failure — `error_run` always reports empty lists, which made a half-created
/// module report `written: []` even though those files are on disk.
fn write_error_run(
    g: &GlobalArgs,
    message: &str,
    partial: tan_core::wizard::WizardWriteResult,
) -> CommandRun {
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let issues = vec![Issue {
        code: "scaffold.write-failed".to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let data = ScaffoldData {
        schema_version: "1".to_string(),
        template_id: String::new(),
        module_name: String::new(),
        normalized_module_name: String::new(),
        destination: String::new(),
        preview: false,
        file_changes: vec![],
        written: partial.written,
        unchanged: partial.unchanged,
    };
    let text = if g.is_json() {
        vec![]
    } else {
        vec![format!("scaffold: {message}")]
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "scaffold",
            project,
            data,
            issues,
            ExitCode::WriteFailure.code(),
        )
        .to_json()
    });
    CommandRun {
        exit: ExitCode::WriteFailure,
        text,
        json,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn json_globals() -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    #[test]
    fn error_run_exit_code_matches_the_requested_class() {
        // Pins `error_run` as a passthrough of its `exit` parameter. Not a
        // regression test by itself — every real call site used to hardcode
        // `ExitCode::RuntimeFailure` here, and reverting any one of them still
        // leaves this test green because `exit` never changes on the way
        // through. See `run_reports_validation_failure_when_name_is_missing`
        // below, which drives the actual `scaffold.name-required` call site.
        let validation = error_run(&json_globals(), ExitCode::ValidationFailure, "x", "y");
        assert_eq!(validation.exit, ExitCode::ValidationFailure);
        assert_eq!(validation.exit.code(), 2);

        let write = error_run(&json_globals(), ExitCode::WriteFailure, "x", "y");
        assert_eq!(write.exit, ExitCode::WriteFailure);
        assert_eq!(write.exit.code(), 3);
    }

    #[test]
    fn run_reports_validation_failure_when_name_is_missing() {
        // Regression: the `scaffold.name-required` call site in `run()` used
        // to hardcode `ExitCode::RuntimeFailure` (exit 1) instead of
        // `ValidationFailure` (exit 2) — a CI/extension caller branching on
        // exit code could not distinguish "bad input" from "prompt cancelled"
        // from "disk full". `--format json` forces non-interactive (see
        // `is_interactive` in `run()`), so this hits the missing-name branch
        // without touching disk or blocking on a prompt.
        let args = ScaffoldArgs {
            template: None,
            name: None,
            destination: None,
            preview: false,
            force: false,
        };
        let run = run(&json_globals(), &args);
        assert_eq!(run.exit, ExitCode::ValidationFailure);
        assert_eq!(run.exit.code(), 2);
        let json = run.json.expect("json envelope must be present");
        assert!(json.contains("scaffold.name-required"));
    }

    #[test]
    fn cancelled_prompt_still_emits_a_json_envelope() {
        // Regression: `runtime_failure_run` used to return `json: None`
        // unconditionally, so `--format json` produced zero bytes on stdout
        // on a cancelled prompt.
        let run = runtime_failure_run(&json_globals());
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let json = run.json.expect("json envelope must be present on cancel");
        assert!(json.contains("\"ok\":false"));
        assert!(json.contains("scaffold.cancelled"));
    }

    #[test]
    fn write_error_run_reports_the_partial_written_list() {
        // Regression: a write failure used to go through `error_run`, which
        // always reports `written: []` even when files landed before the error.
        let partial = tan_core::wizard::WizardWriteResult {
            written: vec!["module.c".to_string()],
            unchanged: vec![],
        };
        let run = write_error_run(&json_globals(), "disk full", partial);
        assert_eq!(run.exit, ExitCode::WriteFailure);
        let json = run.json.expect("json envelope must be present");
        assert!(json.contains("\"written\":[\"module.c\"]"));
    }
}
