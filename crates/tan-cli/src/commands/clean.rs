// SPDX-License-Identifier: Apache-2.0
//! `tan clean` — natively remove the per-project build dir + the orchestrator's
//! state cache. The IO/filesystem side of the port; every removal *decision*
//! lives in `tan_core::clean`. Faithful to the retired `west alp-clean`
//! (`scripts/west_commands/alp_clean.py`), which `alp clean` delegated to —
//! under ADR-0020 Phase 4 the Python cleaner is retired and `tan` owns this.
//!
//! Divergences from the Python source, all deliberate:
//!   - root comes from tan's global `--project`, not a positional `app_path`.
//!   - the SDK-root probe reuses `util::resolve_sdk_root` (marker
//!     `scripts/alp_project.py` + `.alp/sdk-path`) instead of `find_sdk_root`'s
//!     `scripts/alp_orchestrate/__init__.py`.
//!   - the text prefix is `clean:`, not `alp-clean:` (the binary is `tan`).
//!   - a manifest-aware sweep of out-of-tree slice build dirs is added.
//!   - a data-loss guard refuses a build-root with no path depth (a filesystem
//!     root); the Python had none.

use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use tan_core::system_manifest::parse_system_manifest;
use tan_core::{CleanAction, clean_classify, clean_targets};

use super::CommandRun;
use crate::cli::{CleanArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{
    cli_workspace_root, normalize_path, resolve_cli_project_context, resolve_sdk_root,
};

/// One removal target's record in the JSON `data` payload.
#[derive(Serialize)]
struct TargetRecord {
    /// The target path (verbatim, unresolved).
    path: String,
    /// Filesystem kind at probe time: `dir` | `file` | `absent`.
    kind: String,
    /// What happened (or would happen): `removed` | `would-remove` |
    /// `remove-failed` | `absent`.
    action: String,
}

/// The `tan clean` JSON `data` payload.
#[derive(Serialize)]
struct CleanReport {
    /// The resolved build root that was (or would be) removed.
    #[serde(rename = "buildRoot")]
    build_root: String,
    /// Whether this was a `--dry-run` (nothing deleted).
    #[serde(rename = "dryRun")]
    dry_run: bool,
    /// Every considered target with its classification.
    targets: Vec<TargetRecord>,
    /// Count of targets actually removed (0 under `--dry-run`).
    removed: u32,
}

/// A path with no `Normal` component is a filesystem/drive root (or empty) —
/// rmtree'ing it would be catastrophic. The Python had no such guard; this is a
/// deliberate, non-faithful data-loss net.
fn is_dangerous_root(p: &Path) -> bool {
    !p.components().any(|c| matches!(c, Component::Normal(_)))
}

/// `tan clean` entry. See the module docs for the faithful-plus behaviour.
pub fn run(g: &GlobalArgs, args: &CleanArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    // App base: a non-`.` positional roots the removal at that app dir; `.`
    // falls back to tan's resolved workspace (which already folded in
    // `--project`), else the current directory.
    let project_root = if args.app_path == "." {
        context
            .workspace_root
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| cwd.clone())
    } else {
        cwd.join(&args.app_path)
    };
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    // SDK-root guard — faithful to alp_clean.py `log.die('Cannot locate
    // alp-sdk root.')`. ponytail: arguably YAGNI (removing a build dir needs no
    // SDK), but the faithful default keeps it; drop this block to relax it.
    if resolve_sdk_root(g, &cli_workspace_root(g)).is_none() {
        return finish(
            g,
            project,
            CleanReport {
                build_root: String::new(),
                dry_run: args.dry_run,
                targets: Vec::new(),
                removed: 0,
            },
            vec!["clean: Cannot locate alp-sdk root.".to_string()],
            vec![error(
                "clean.sdk-root-not-found",
                "Cannot locate alp-sdk root.",
            )],
            ExitCode::RuntimeFailure,
        );
    }

    // build_root: absolute as-is, relative against the project root; default
    // `<project_root>/build`.
    let build_root = match args.build_root.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                normalize_path(p)
            } else {
                normalize_path(&project_root.join(p))
            }
        }
        None => project_root.join("build"),
    };

    if is_dangerous_root(&build_root) {
        let bad = build_root.to_string_lossy().into_owned();
        return finish(
            g,
            project,
            CleanReport {
                build_root: bad.clone(),
                dry_run: args.dry_run,
                targets: Vec::new(),
                removed: 0,
            },
            vec![format!(
                "clean: refusing to remove `{bad}` — build root has no path depth"
            )],
            vec![error(
                "clean.unsafe-build-root",
                &format!("refusing to remove `{bad}`: build root has no path depth"),
            )],
            ExitCode::RuntimeFailure,
        );
    }

    let mut text: Vec<String> = Vec::new();
    let mut issues: Vec<Issue> = Vec::new();

    // Best-effort, manifest-aware sweep. Absence is silent; a parse/version error
    // is a warning, never fatal (NEVER fail clean over an unreadable manifest).
    let manifest = match std::fs::read_to_string(build_root.join("system-manifest.yaml")) {
        Ok(yaml) => match parse_system_manifest(&yaml) {
            Ok(m) => Some(m),
            Err(e) => {
                if !g.quiet {
                    text.push(format!(
                        "clean: ignoring unreadable system-manifest.yaml: {e}"
                    ));
                }
                issues.push(warning(
                    "clean.manifest-unreadable",
                    &format!("ignoring unreadable system-manifest.yaml: {e}"),
                ));
                None
            }
        },
        Err(_) => None,
    };

    let targets = clean_targets(&project_root, &build_root, manifest.as_ref());

    let mut records: Vec<TargetRecord> = Vec::new();
    let mut removed = 0u32;
    let mut exit = ExitCode::Success;

    for target in &targets {
        let path = target.to_string_lossy().into_owned();
        match clean_classify(target.is_dir(), target.is_file(), args.dry_run) {
            CleanAction::WouldRemoveDir => {
                text.push(format!("[DRY] would rmtree {path}"));
                records.push(record(&path, "dir", "would-remove"));
            }
            CleanAction::WouldRemoveFile => {
                text.push(format!("[DRY] would unlink {path}"));
                records.push(record(&path, "file", "would-remove"));
            }
            CleanAction::RemoveDir => {
                text.push(format!("clean: removing {path}"));
                // Best-effort (Python `rmtree(ignore_errors=True)`): a failure is
                // warned and still counted removed; it does NOT fail the command.
                if let Err(e) = std::fs::remove_dir_all(target) {
                    if !g.quiet {
                        text.push(format!(
                            "clean: warning: could not fully remove {path}: {e}"
                        ));
                    }
                }
                removed += 1;
                records.push(record(&path, "dir", "removed"));
            }
            CleanAction::RemoveFile => {
                text.push(format!("clean: removing {path}"));
                // The state-file unlink is NOT ignore_errors in the Python — a
                // failure propagates to exit 1.
                if let Err(e) = std::fs::remove_file(target) {
                    issues.push(error(
                        "clean.remove-failed",
                        &format!("failed to remove {path}: {e}"),
                    ));
                    text.push(format!("clean: error: failed to remove {path}: {e}"));
                    exit = ExitCode::RuntimeFailure;
                    records.push(record(&path, "file", "remove-failed"));
                } else {
                    removed += 1;
                    records.push(record(&path, "file", "removed"));
                }
            }
            CleanAction::Absent => {
                records.push(record(&path, "absent", "absent"));
            }
        }
    }

    // Faithful trailing line — suppressed under --dry-run (Python guards it with
    // `and not args.dry_run`) and when a hard removal error already fired.
    if removed == 0 && !args.dry_run && exit == ExitCode::Success {
        text.push("clean: nothing to remove".to_string());
    }

    let report = CleanReport {
        build_root: build_root.to_string_lossy().into_owned(),
        dry_run: args.dry_run,
        targets: records,
        removed,
    };
    finish(g, project, report, text, issues, exit)
}

/// A `TargetRecord` builder.
fn record(path: &str, kind: &str, action: &str) -> TargetRecord {
    TargetRecord {
        path: path.to_string(),
        kind: kind.to_string(),
        action: action.to_string(),
    }
}

/// An `error`-severity issue.
fn error(code: &str, message: &str) -> Issue {
    Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }
}

/// A `warning`-severity issue.
fn warning(code: &str, message: &str) -> Issue {
    Issue {
        code: code.to_string(),
        severity: "warning".to_string(),
        message: message.to_string(),
    }
}

/// Assemble the `CommandRun`: one JSON envelope in JSON mode, else the text lines.
fn finish(
    g: &GlobalArgs,
    project: Project,
    report: CleanReport,
    text: Vec<String>,
    issues: Vec<Issue>,
    exit: ExitCode,
) -> CommandRun {
    if g.is_json() {
        let json = Envelope::new("clean", project, report, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
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
    use crate::cli::Format;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-clean-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        // A fake SDK marker so `resolve_sdk_root` (via --sdk-root) is satisfied.
        std::fs::create_dir_all(d.join("scripts")).unwrap();
        std::fs::write(d.join("scripts").join("alp_project.py"), "").unwrap();
        d
    }

    fn global(project: &Path, format: Format) -> GlobalArgs {
        let p = project.to_string_lossy().into_owned();
        GlobalArgs {
            project: Some(p.clone()),
            board_yaml: None,
            sdk_root: Some(p),
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        }
    }

    /// Populate `<root>/build/…` + the app-root state file.
    fn populate(root: &Path) {
        std::fs::create_dir_all(root.join("build").join("m55_hp-zephyr")).unwrap();
        std::fs::write(root.join("build").join("marker"), "x").unwrap();
        std::fs::write(root.join(".alp-build-state.json"), "{}").unwrap();
    }

    #[test]
    fn dry_run_lists_but_deletes_nothing() {
        let root = tmp("dry");
        populate(&root);
        let g = global(&root, Format::Text);
        let run = run(
            &g,
            &CleanArgs {
                app_path: ".".to_string(),
                build_root: None,
                dry_run: true,
            },
        );
        assert_eq!(run.exit, ExitCode::Success);
        let joined = run.text.join("\n");
        assert!(joined.contains("[DRY] would rmtree"), "{joined}");
        assert!(joined.contains("[DRY] would unlink"), "{joined}");
        assert!(!joined.contains("nothing to remove"), "{joined}");
        assert!(root.join("build").is_dir());
        assert!(root.join(".alp-build-state.json").is_file());
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn real_run_removes_then_reports_nothing_on_second_pass() {
        let root = tmp("real");
        populate(&root);
        let g = global(&root, Format::Text);
        let args = CleanArgs {
            app_path: ".".to_string(),
            build_root: None,
            dry_run: false,
        };
        let run1 = run(&g, &args);
        assert_eq!(run1.exit, ExitCode::Success);
        assert!(!root.join("build").exists());
        assert!(!root.join(".alp-build-state.json").exists());

        let run2 = run(&g, &args);
        assert_eq!(run2.exit, ExitCode::Success);
        assert!(
            run2.text.iter().any(|l| l == "clean: nothing to remove"),
            "{:?}",
            run2.text
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn json_mode_emits_one_clean_envelope() {
        let root = tmp("json");
        populate(&root);
        let g = global(&root, Format::Json);
        let run = run(
            &g,
            &CleanArgs {
                app_path: ".".to_string(),
                build_root: None,
                dry_run: false,
            },
        );
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["command"], "clean");
        assert_eq!(doc["ok"], true);
        assert_eq!(doc["exitCode"], 0);
        assert!(
            doc["data"]["targets"]
                .as_array()
                .unwrap()
                .iter()
                .any(|t| { t["action"] == "removed" })
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn positional_app_path_roots_removal() {
        // A non-`.` positional roots the removal at that app dir, overriding
        // `--project`. Using an absolute positional makes it cwd-independent.
        let sdk = tmp("posarg-sdk"); // carries the SDK marker
        let app = tmp("posarg-app");
        populate(&app);
        let mut g = global(&app, Format::Text);
        g.project = Some(sdk.to_string_lossy().into_owned()); // --project points elsewhere
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        let run = run(
            &g,
            &CleanArgs {
                app_path: app.to_string_lossy().into_owned(),
                build_root: None,
                dry_run: false,
            },
        );
        assert_eq!(run.exit, ExitCode::Success);
        assert!(!app.join("build").exists(), "positional app build/ removed");
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&app).unwrap();
    }

    #[test]
    fn missing_sdk_root_exits_one() {
        // A project dir WITHOUT the SDK marker and no --sdk-root override.
        let root = std::env::temp_dir().join(format!("tan-clean-nosdk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let mut g = global(&root, Format::Text);
        g.sdk_root = None; // force auto-discovery, which fails (no marker anywhere)
        // Guard against a real sibling alp-sdk on the dev box resolving: only
        // assert when nothing resolves.
        if resolve_sdk_root(&g, &cli_workspace_root(&g)).is_none() {
            let run = run(
                &g,
                &CleanArgs {
                    app_path: ".".to_string(),
                    build_root: None,
                    dry_run: false,
                },
            );
            assert_eq!(run.exit, ExitCode::RuntimeFailure);
            assert!(
                run.text
                    .iter()
                    .any(|l| l.contains("Cannot locate alp-sdk root"))
            );
        }
        std::fs::remove_dir_all(&root).unwrap();
    }
}
