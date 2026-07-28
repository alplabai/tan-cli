// SPDX-License-Identifier: Apache-2.0
//! `tan init --from-example` path plus the shared `finish` step (diff, guard
//! overwrites, preview, write) used by both the template and example paths.

use std::path::{Path, PathBuf};

use tan_core::wizard::{
    ExampleReadError, WizardFileChangeKind, WizardPlannedFile, collect_wizard_file_changes,
    create_scaffold_tree_preview, read_example_tree, retarget_board_yaml_som, write_wizard_files,
};

use crate::cli::{GlobalArgs, InitArgs};
use crate::commands::CommandRun;
use crate::envelope::{Envelope, Issue};
use crate::exit::ExitCode;

use super::resolve::ResolveErr::{BadArg, Cancelled};
use super::resolve::{resolve_destination, resolve_name};
use super::response::{empty_data, error_run, make_project, runtime_failure_run, write_error_run};
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
    if is_unsafe_example_source(src_path) {
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
        Err(Cancelled) => return runtime_failure_run(g),
        Err(BadArg(msg)) => {
            return error_run(g, ExitCode::ValidationFailure, "init.invalid-name", &msg);
        }
    };
    let destination = match resolve_destination(
        args.destination.as_deref(),
        g.project.as_deref(),
        args.name.is_some(),
        is_interactive,
    ) {
        Ok(d) => d,
        Err(_) => return runtime_failure_run(g),
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
    if !is_contained(&examples_root, &example_dir) {
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

    // Honor --board-yaml: emit the caller's board.yaml verbatim instead of the
    // example's own. The template path (mod.rs) does this before calling
    // `finish`; this path used to skip straight to `finish` and drop the
    // caller's file entirely — the exact silent no-op the template path's
    // `--board-yaml` handling exists to refuse instead of committing. Every
    // discovered example ships a board.yaml (discover_examples requires it),
    // so `applied` failing here would mean a hand-typed --from-example path
    // pointed at a non-example directory, not a template gap.
    let files: Vec<WizardPlannedFile> = match g.board_yaml.as_deref() {
        Some(path) => match std::fs::read_to_string(path) {
            Ok(content) => {
                let mut applied = false;
                let files: Vec<WizardPlannedFile> = files
                    .into_iter()
                    .map(|f| {
                        if f.relative_path == "board.yaml" {
                            applied = true;
                            WizardPlannedFile {
                                content: content.clone(),
                                ..f
                            }
                        } else {
                            f
                        }
                    })
                    .collect();
                if !applied {
                    return error_run(
                        g,
                        ExitCode::ValidationFailure,
                        "init.board-yaml-unsupported",
                        &format!(
                            "--board-yaml was given but example '{src}' has no board.yaml to override."
                        ),
                    );
                }
                files
            }
            Err(err) => {
                return error_run(
                    g,
                    ExitCode::ValidationFailure,
                    "init.board-yaml-unreadable",
                    &format!("--board-yaml '{path}' could not be read: {err}"),
                );
            }
        },
        None => files,
    };

    let template_id = format!("example:{src}");
    // Pin the SAME `sdk_root` already resolved above (against the user's
    // actual workspace, to locate examples/) — not a fresh resolution against
    // `project_root`, which is the newly created (nested) project dir and has
    // no bearing on where the SDK actually lives.
    finish(
        g,
        args,
        &template_id,
        &destination,
        &project_root,
        &files,
        Some(sdk_root.as_path()),
    )
}

/// True when `src_path` is a syntactically safe example source: relative, no
/// `..`, no absolute/drive-rooted/root-relative components, no bare `.`.
/// Pulled out of `run_from_example` so the lexical traversal guard — the
/// source must stay under the SDK `examples/` tree — has direct unit coverage
/// instead of only living inline, untested, in the CLI entry point.
///
/// Delegates to `tan_core::is_plain_relative` — the hand-rolled
/// `is_absolute() || any(ParentDir|Prefix|RootDir)` check this replaced let a
/// `CurDir` (`.`) component through, so `tan init --from-example .` cleared
/// this guard, `examples_root.join(".")` canonicalized right back to
/// `examples_root`, and `is_contained` passed too — copying the SDK's entire
/// `examples/` tree into the user's project as one "example".
fn is_unsafe_example_source(src_path: &Path) -> bool {
    !tan_core::is_plain_relative(src_path)
}

/// Pin `sdk_root` — already resolved for the user's actual workspace by each
/// call site (`resolve_sdk_root(g, &cli_workspace_root(g))` for the template
/// path; the SDK root already resolved to locate the example's `examples/`
/// tree for `--from-example`) — into `<project_root>/.alp/sdk-path`, so the
/// new project is reproducible without a separate `tan sdk switch`.
/// Deliberately does NOT re-resolve against `project_root`: that's the newly
/// created (often nested) project directory, not the workspace the SDK was
/// actually found under. `has_loader_script` is re-checked defensively (every
/// caller already guarantees it via `resolve_sdk_root`'s own contract) and
/// `None`/an invalid root/a failed write are all a silent skip — never a
/// reason to fail `tan init` itself.
fn pin_resolved_sdk(project_root: &Path, sdk_root: Option<&Path>) -> Option<String> {
    let sdk_root = sdk_root.filter(|p| crate::util::has_loader_script(p))?;
    let sdk_path = sdk_root.to_string_lossy().to_string();
    crate::commands::sdk::write_sdk_pointer(&project_root.join(".alp").join("sdk-path"), &sdk_path)
        .ok()?;
    Some(sdk_path)
}

/// True when `dir`, once canonicalized, stays inside canonicalized `root`.
/// Defeats directory-symlink escapes and rooted-but-driveless paths (e.g.
/// `/foo` on Windows) that the lexical `is_unsafe_example_source` check above
/// can miss; a missing/unreadable path on either side is never "contained".
fn is_contained(root: &Path, dir: &Path) -> bool {
    match (root.canonicalize(), dir.canonicalize()) {
        (Ok(root), Ok(dir)) => dir.starts_with(&root),
        _ => false,
    }
}

/// Diff the planned `files` against `project_root`, then guard overwrites,
/// preview, or write — shared by the template and from-example paths. `template_id`
/// is recorded verbatim in the envelope (e.g. `minimal-app` or `example:audio/i2s-tone`).
/// `sdk_root_for_pin` is the SDK already resolved for the caller's actual
/// workspace (see [`pin_resolved_sdk`]); only used on the real write path.
pub(super) fn finish(
    g: &GlobalArgs,
    args: &InitArgs,
    template_id: &str,
    destination: &str,
    project_root: &Path,
    files: &[WizardPlannedFile],
    sdk_root_for_pin: Option<&Path>,
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

    // Preview mode — show plan, no writes. Checked BEFORE the overwrite guard:
    // `--preview` never touches disk, so it has nothing to be guarded against,
    // but the guard used to run first and reject it with `init.would-overwrite`
    // (exit 3) for a project that has local edits — the read-only operation
    // failed instead of showing the plan.
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

    // Write files.
    match write_wizard_files(project_root, files) {
        Ok(result) => {
            // Pin the already-resolved SDK (see `sdk_root_for_pin`'s doc)
            // into the new project — silently skipped when the caller passed
            // `None` or it isn't a real checkout. Only reached here (after
            // the files landed), never on the preview/guard/error paths above.
            let sdk_pinned = pin_resolved_sdk(project_root, sdk_root_for_pin);

            let project = make_project(destination);
            let data = InitData {
                schema_version: "1".to_string(),
                template_id: template_id.to_string(),
                destination: destination.to_string(),
                preview: false,
                file_changes: file_changes_ser,
                written: result.written.clone(),
                unchanged: result.unchanged.clone(),
                sdk_pinned,
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
        Err(e) => {
            // `e.partial` lists the files that landed before the failure —
            // reporting `written: []` here (what a bare `error_run` does)
            // would contradict a project that is actually half on disk.
            let message = format!("Failed to write files: {}", e.error);
            write_error_run(g, &message, e.partial)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The traversal/containment guards below had zero test coverage: a future
    // refactor that swapped the canonicalize-containment check for a cheaper
    // `starts_with`, or that dropped the `Prefix`/`RootDir` arm while keeping
    // `ParentDir`, would still pass every other test in the suite while
    // `tan init --from-example` copied files from outside the SDK tree.

    #[test]
    fn unsafe_example_source_rejects_traversal_and_absolute() {
        assert!(is_unsafe_example_source(Path::new("../escape")));
        assert!(is_unsafe_example_source(Path::new("a/../../b")));
        assert!(is_unsafe_example_source(Path::new("/etc/passwd")));
        // Bare `.` — the hand-rolled check this delegated to `is_plain_relative`
        // missed this: `.` is neither absolute nor a ParentDir/Prefix/RootDir
        // component, yet `examples_root.join(".")` canonicalizes right back to
        // `examples_root`, so `--from-example .` copied the whole examples/ tree.
        assert!(is_unsafe_example_source(Path::new(".")));
        // A leading `./` keeps an explicit CurDir component too — same class
        // of gap, one directory deeper.
        assert!(is_unsafe_example_source(Path::new("./audio/i2s-tone")));
        assert!(!is_unsafe_example_source(Path::new("audio/i2s-tone")));
    }

    #[cfg(windows)]
    #[test]
    fn unsafe_example_source_rejects_windows_rooted_and_drive_relative() {
        // Driveless-rooted (`\x`) and drive-relative (`C:x`) paths are NOT
        // `is_absolute()` on Windows, yet still escape a `join`.
        assert!(is_unsafe_example_source(Path::new(r"\windows\x")));
        assert!(is_unsafe_example_source(Path::new(r"C:x")));
        assert!(is_unsafe_example_source(Path::new(r"C:\x")));
    }

    #[test]
    fn contained_accepts_nested_and_rejects_sibling_and_missing() {
        let tag = format!("init-from-example-contain-{}", std::process::id());
        let base = std::env::temp_dir().join(tag);
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("examples");
        let outside = base.join("outside");
        std::fs::create_dir_all(root.join("audio/i2s-tone")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();

        assert!(is_contained(&root, &root.join("audio/i2s-tone")));
        // A directory that exists but sits outside root — the shape a
        // symlink escape or a rooted-but-driveless join would produce.
        assert!(!is_contained(&root, &outside));
        // A path that doesn't exist can't be canonicalized — must not be
        // treated as contained by default.
        assert!(!is_contained(&root, &root.join("does-not-exist")));

        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn pin_resolved_sdk_writes_pointer_for_an_already_resolved_sdk_root() {
        let tag = format!("init-pin-resolved-sdk-{}", std::process::id());
        let base = std::env::temp_dir().join(tag);
        let _ = std::fs::remove_dir_all(&base);
        let project_root = base.join("project");
        std::fs::create_dir_all(&project_root).unwrap();
        let sdk_root = base.join("sdk");
        std::fs::create_dir_all(sdk_root.join("scripts")).unwrap();
        std::fs::write(sdk_root.join("scripts").join("alp_project.py"), "").unwrap();

        let pinned = pin_resolved_sdk(&project_root, Some(&sdk_root));

        assert_eq!(pinned.as_deref(), Some(sdk_root.to_string_lossy().as_ref()));
        assert!(project_root.join(".alp").join("sdk-path").exists());

        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn pin_resolved_sdk_silently_skips_when_sdk_root_is_not_a_checkout() {
        // Every real caller already guarantees `has_loader_script` via
        // `resolve_sdk_root`'s own contract; this pins the defensive re-check
        // itself so a future caller that skips that guarantee fails closed
        // (skip) instead of pinning a non-SDK directory.
        let tag = format!("init-pin-skip-{}", std::process::id());
        let base = std::env::temp_dir().join(tag);
        let _ = std::fs::remove_dir_all(&base);
        let project_root = base.join("project");
        std::fs::create_dir_all(&project_root).unwrap();
        let not_sdk = base.join("not-an-sdk");
        std::fs::create_dir_all(&not_sdk).unwrap();

        let pinned = pin_resolved_sdk(&project_root, Some(&not_sdk));

        assert_eq!(pinned, None);
        assert!(!project_root.join(".alp").join("sdk-path").exists());

        std::fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn pin_resolved_sdk_silently_skips_when_nothing_resolved() {
        let tag = format!("init-pin-skip-none-{}", std::process::id());
        let base = std::env::temp_dir().join(tag);
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        assert_eq!(pin_resolved_sdk(&base, None), None);
        assert!(!base.join(".alp").join("sdk-path").exists());

        std::fs::remove_dir_all(&base).unwrap();
    }
}
