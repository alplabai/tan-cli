// SPDX-License-Identifier: Apache-2.0
//! `tan init --from-example` path plus the shared `finish` step (diff, guard
//! overwrites, preview, write) used by both the template and example paths.

use std::path::{Component, Path, PathBuf};

use tan_core::wizard::{
    ExampleReadError, WizardFileChangeKind, WizardPlannedFile, collect_wizard_file_changes,
    create_scaffold_tree_preview, read_example_tree, retarget_board_yaml_som, write_wizard_files,
};

use crate::cli::{GlobalArgs, InitArgs};
use crate::commands::CommandRun;
use crate::envelope::{Envelope, Issue};
use crate::exit::ExitCode;

use super::resolve::{resolve_destination, resolve_name};
use super::response::{empty_data, error_run, make_project, runtime_failure_run};
use super::{FileChangeSer, InitData};

/// From-example path: resolve name/destination, locate the example under the SDK
/// `examples/` tree, read it verbatim, and hand off to `finish`. `--som`/`--cores`
/// are ignored — the example carries its own `board.yaml`.
pub(super) fn run_from_example(
    g: &GlobalArgs,
    args: &InitArgs,
    src: &str,
    is_interactive: bool,
) -> CommandRun {
    let src = src.trim();
    if src.is_empty() {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-example",
            "--from-example requires a non-empty example source directory.",
        );
    }
    // Reject absolute paths and `..` traversal — the source must stay under the
    // SDK examples/ directory.
    let src_path = Path::new(src);
    if src_path.is_absolute()
        || src_path.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::Prefix(_) | Component::RootDir
            )
        })
    {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-example",
            &format!(
                "Invalid example '{src}': must be a relative path under the SDK examples/ directory."
            ),
        );
    }

    // Resolve name + destination + project root (same as the template path).
    let name = match resolve_name(args.name.as_deref(), is_interactive) {
        Ok(n) => n,
        Err(_) => {
            eprintln!("Cancelled.");
            return runtime_failure_run();
        }
    };
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
    let dest_path = PathBuf::from(&destination);
    let project_root = if name.is_empty() {
        dest_path.clone()
    } else {
        dest_path.join(&name)
    };

    // Locate the SDK examples/ root.
    let workspace_root = crate::util::cli_workspace_root(g);
    let Some(sdk_root) = crate::util::resolve_sdk_root(g, &workspace_root) else {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.sdk-root-unresolved",
            "alp-sdk root is unresolved. Use --sdk-root or run near an alp-sdk checkout to copy an example.",
        );
    };
    let examples_root = sdk_root.join("examples");
    let example_dir = examples_root.join(src);
    // Containment guard: canonicalize and require the resolved example to stay
    // inside examples/. Defeats directory-symlink escapes and rooted-but-driveless
    // paths (e.g. `/foo` on Windows) that the lexical check above can miss — both
    // would otherwise resolve outside the SDK tree and get copied verbatim.
    let contained = match (examples_root.canonicalize(), example_dir.canonicalize()) {
        (Ok(root), Ok(dir)) => dir.starts_with(&root),
        _ => false,
    };
    if !contained {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.example-not-found",
            &format!("Example '{src}' was not found under the SDK examples/ directory."),
        );
    }
    let files = match read_example_tree(&example_dir) {
        Ok(files) if files.is_empty() => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "init.example-not-found",
                &format!("Example '{src}' contains no files to copy."),
            );
        }
        Ok(files) => files,
        Err(ExampleReadError::NotFound) => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "init.example-not-found",
                &format!("Example '{src}' was not found under the SDK examples/ directory."),
            );
        }
        Err(ExampleReadError::Unreadable(detail)) => {
            return error_run(
                g,
                ExitCode::RuntimeFailure,
                "init.example-unreadable",
                &format!("Example '{src}' could not be read: {detail}"),
            );
        }
    };

    // Retarget the copied board.yaml onto the chosen SoM (--som), so a user can
    // scaffold an example onto their own SoM instead of the example's default.
    let files: Vec<WizardPlannedFile> = match args.som.as_deref() {
        Some(sku) => files
            .into_iter()
            .map(|f| {
                if f.relative_path == "board.yaml" {
                    WizardPlannedFile {
                        content: retarget_board_yaml_som(&f.content, sku),
                        ..f
                    }
                } else {
                    f
                }
            })
            .collect(),
        None => files,
    };

    let template_id = format!("example:{src}");
    finish(g, args, &template_id, &destination, &project_root, &files)
}

/// Diff the planned `files` against `project_root`, then guard overwrites,
/// preview, or write — shared by the template and from-example paths. `template_id`
/// is recorded verbatim in the envelope (e.g. `minimal-app` or `example:audio/i2s-tone`).
pub(super) fn finish(
    g: &GlobalArgs,
    args: &InitArgs,
    template_id: &str,
    destination: &str,
    project_root: &Path,
    files: &[WizardPlannedFile],
) -> CommandRun {
    // Collect file changes.
    let changes = collect_wizard_file_changes(project_root, files);
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

    // Guard against unforced overwrites.
    if has_updates && !args.force {
        let project = make_project(destination);
        let data = empty_data(template_id, destination, args.preview, file_changes_ser);
        let issues = vec![Issue {
            code: "init.would-overwrite".to_string(),
            severity: "error".to_string(),
            message: "One or more files would be overwritten. Use --force to allow updates."
                .to_string(),
        }];
        let text = if g.is_json() {
            vec![]
        } else {
            vec!["init: would overwrite existing files; use --force to proceed.".to_string()]
        };
        let json = g.is_json().then(|| {
            Envelope::new("init", project, data, issues, ExitCode::WriteFailure.code()).to_json()
        });
        return CommandRun {
            exit: ExitCode::WriteFailure,
            text,
            json,
        };
    }

    // Preview mode — show plan, no writes.
    if args.preview {
        let tree = create_scaffold_tree_preview(files);
        let project = make_project(destination);
        let data = empty_data(template_id, destination, true, file_changes_ser);
        let text = if g.is_json() {
            vec![]
        } else {
            vec![format!("init: preview for template '{template_id}'"), tree]
        };
        let json = g.is_json().then(|| {
            Envelope::new("init", project, data, vec![], ExitCode::Success.code()).to_json()
        });
        return CommandRun {
            exit: ExitCode::Success,
            text,
            json,
        };
    }

    // Write files.
    match write_wizard_files(project_root, files) {
        Ok(result) => {
            let project = make_project(destination);
            let data = InitData {
                schema_version: "1".to_string(),
                template_id: template_id.to_string(),
                destination: destination.to_string(),
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
                        "init: created '{}' from template '{}'",
                        project_root.display(),
                        template_id
                    ),
                    format!(
                        "  written: {}, unchanged: {}",
                        result.written.len(),
                        result.unchanged.len()
                    ),
                ]
            };
            let json = g.is_json().then(|| {
                Envelope::new("init", project, data, vec![], ExitCode::Success.code()).to_json()
            });
            CommandRun {
                exit: ExitCode::Success,
                text,
                json,
            }
        }
        Err(e) => error_run(
            g,
            ExitCode::WriteFailure,
            "init.write-failed",
            &format!("Failed to write files: {e}"),
        ),
    }
}
