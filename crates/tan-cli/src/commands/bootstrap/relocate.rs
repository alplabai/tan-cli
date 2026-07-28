// SPDX-License-Identifier: Apache-2.0
//! The workspace-parent guard's IO half (tan-cli#185): list the checkout
//! parent's entries and feed [`tan_core::bootstrap::parent_needs_workspace_guard`],
//! prompt when the guard fires and a human is at the keyboard, and perform the
//! actual relocation. The pure trigger decision lives in `tan_core::bootstrap`;
//! everything here is IO or the `inquire` prompt.
//!
//! `west init -l <alp-sdk>` forces the west topdir to be the checkout's own
//! PARENT (#769, `mod.rs`'s `workspace_dir` derivation) — not this module's to
//! change. What this module decides is WHICH directory gets to be that parent:
//! the checkout's current one (unchanged), a dedicated `alp-workspace/` sibling
//! the customer accepted, or an explicit `--workspace <path>`.

use std::path::{Path, PathBuf};

use tan_core::bootstrap::{DEFAULT_WORKSPACE_DIR_NAME, parent_needs_workspace_guard};

use crate::exit::ExitCode;

use super::steps::native;
use super::west_config::same_directory;

/// Outcome of [`resolve_workspace`].
pub(super) enum Resolution {
    /// No relocation happened — the caller's `RunPaths` stay exactly as built
    /// (the guard didn't fire, the parent is already a west workspace, or an
    /// explicit `--workspace` already names the current parent).
    Unchanged,
    /// The checkout was physically moved; the caller must repoint its
    /// `RunPaths` (`repo_root`, `workspace_dir`, and — since it follows
    /// `workspace_dir` — `venv_dir`) and its `sdk_root` string at the new
    /// location, and surface `notice` (the `bootstrap.workspace-relocated`
    /// warning text).
    Relocated {
        repo_root: PathBuf,
        workspace_dir: PathBuf,
        notice: String,
    },
    /// The guard fired and nothing was attempted: a `--non-interactive` run,
    /// or an interactive decline/cancel. Nothing was written — maps to
    /// `failure()`'s "refusal before any step ran" shape, never `fatal()`'s.
    Refuse {
        exit: ExitCode,
        code: &'static str,
        message: String,
    },
    /// Relocation was ATTEMPTED (accepted interactively, or `--workspace`)
    /// and failed. Maps to `fatal()` — the same `bootstrap.failed` shape
    /// every other failing bootstrap STEP uses — since, unlike [`Refuse`],
    /// something really was tried.
    MoveFailed(String),
}

/// Resolve where the west workspace belongs, relocating the checkout if
/// needed. `explicit` is an already-validated, already-absolutised
/// `--workspace <path>` (see `tan_core::path_guard::resolve_workspace_target`
/// — the caller in `mod.rs` resolves it before any of this IO runs): when
/// given, it answers the question outright — the guard is never even
/// evaluated, matching the issue ("no guard at all"). Otherwise the guard
/// fires only when `parent_needs_workspace_guard` says so, and `interactive`
/// decides whether that becomes a prompt or an immediate refusal.
pub(super) fn resolve_workspace(
    repo_root: &Path,
    workspace_dir: &Path,
    explicit: Option<&Path>,
    interactive: bool,
    venv_dir_name: &str,
) -> Resolution {
    let target_parent = match explicit {
        Some(path) => path.to_path_buf(),
        None => match default_relocation_target(repo_root, workspace_dir, venv_dir_name) {
            Some(target) => {
                if !interactive {
                    return Resolution::Refuse {
                        exit: ExitCode::ValidationFailure,
                        code: "workspace-guard",
                        message: workspace_guard_refusal(workspace_dir, &target),
                    };
                }
                match prompt_relocate(workspace_dir, &target) {
                    PromptOutcome::Accept => target,
                    PromptOutcome::Decline | PromptOutcome::Cancelled => {
                        return Resolution::Refuse {
                            exit: ExitCode::RuntimeFailure,
                            code: "workspace-guard",
                            message: "Relocation declined; nothing was written. Re-run and \
                                      accept, run `tan bootstrap --workspace <path>`, or clone \
                                      alp-sdk into a dedicated directory."
                                .to_string(),
                        };
                    }
                }
            }
            None => return Resolution::Unchanged,
        },
    };

    match relocate_checkout(repo_root, &target_parent) {
        Ok(new_repo_root) if new_repo_root == repo_root => Resolution::Unchanged,
        Ok(new_repo_root) => {
            let notice = format!(
                "moved the alp-sdk checkout from {} to {} so the west workspace \
                 (zephyr/modules/.west/venv) stays out of {} (tan-cli#185)",
                native(repo_root),
                native(&new_repo_root),
                native(workspace_dir),
            );
            Resolution::Relocated {
                workspace_dir: target_parent,
                repo_root: new_repo_root,
                notice,
            }
        }
        Err(message) => Resolution::MoveFailed(message),
    }
}

/// The refusal text for a `--non-interactive` (or `--ci`/`--format json`, or
/// a non-TTY stdin) run that hit the guard: names the two remedies verbatim
/// (tan-cli#185). Names `tan bootstrap --workspace <path>` explicitly, not
/// bare `--workspace <path>` — this same refusal is inherited by `tan build`
/// and `tan doctor --build --fix` (tan-cli#185 review finding 8), neither of
/// which has a `--workspace` flag of its own to pass.
fn workspace_guard_refusal(workspace_dir: &Path, target: &Path) -> String {
    format!(
        "{} holds more than this checkout, and is not itself an existing west workspace; \
         refusing to write the west workspace (zephyr/modules/.west/venv) there without asking. \
         Re-run interactively to move the checkout into {}, run `tan bootstrap --workspace \
         <path>`, or clone alp-sdk into a dedicated directory.",
        native(workspace_dir),
        native(target),
    )
}

/// `Some(<workspace_dir>/alp-workspace)` when the guard fires for this parent,
/// `None` when it does not (nothing to relocate to). `workspace_dir ==
/// repo_root` (the rootless-`repo_root` fallback `mod.rs` uses when
/// `repo_root.parent()` is `None`) has no real parent to guard at all.
fn default_relocation_target(
    repo_root: &Path,
    workspace_dir: &Path,
    venv_dir_name: &str,
) -> Option<PathBuf> {
    if workspace_dir == repo_root {
        return None;
    }
    let checkout_name = repo_root.file_name()?.to_string_lossy().into_owned();
    let entries = list_entries(workspace_dir)?;
    // A TYPED check, not a name match: `.west` is only "an existing west
    // workspace" when `west init -l` actually wrote a readable config there
    // (tan-cli#185 review finding 4) — a plain file or an empty directory
    // happening to be named `.west` is foreign content like anything else.
    let dot_west_is_workspace = workspace_dir.join(".west").join("config").is_file();
    if parent_needs_workspace_guard(
        &entries,
        &checkout_name,
        venv_dir_name,
        dot_west_is_workspace,
    ) {
        Some(workspace_dir.join(DEFAULT_WORKSPACE_DIR_NAME))
    } else {
        None
    }
}

/// Best-effort listing of `parent`'s direct entry names. `None` — not
/// `Some(vec![])` — when the directory cannot even be read: an unreadable
/// parent tells the guard nothing, and `Some(vec![])` would read as
/// "confirmed empty", which is a claim we cannot make. The real problem (if
/// there is one) surfaces naturally downstream, the first time a step
/// actually tries to write there.
fn list_entries(parent: &Path) -> Option<Vec<String>> {
    let read = std::fs::read_dir(parent).ok()?;
    Some(
        read.filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect(),
    )
}

/// Answer to the relocation prompt.
enum PromptOutcome {
    Accept,
    Decline,
    Cancelled,
}

/// Ask whether to move the checkout into `target`. Mirrors
/// `commands::init::resolve`'s `Select`/`Text` prompts: Ctrl-C/Esc and any
/// other prompt error both fold into `Cancelled` (never left as a hang or a
/// silent "no").
fn prompt_relocate(workspace_dir: &Path, target: &Path) -> PromptOutcome {
    let message = format!(
        "{} holds other content. Move the alp-sdk checkout into {} and build the west workspace \
         there?",
        native(workspace_dir),
        native(target),
    );
    match inquire::Confirm::new(&message).with_default(false).prompt() {
        Ok(true) => PromptOutcome::Accept,
        Ok(false) => PromptOutcome::Decline,
        Err(_) => PromptOutcome::Cancelled,
    }
}

/// Moves `repo_root` to be a direct child of `target_parent`, preserving its
/// own basename — the SAME operation whether reached via an interactive
/// accept (`target_parent` = `<original parent>/alp-workspace`) or an
/// explicit `--workspace <path>`. This moves a customer's own git checkout,
/// so it is built to never half-complete:
///
///   - a no-op (`Ok` with the SAME `repo_root` back) when `repo_root` is
///     already a direct child of `target_parent` — covers a retry after
///     success and `--workspace <the-current-parent>`;
///   - refuses outright, moving nothing, when `target_parent` already has an
///     entry named like the checkout — never silently merges into or
///     overwrites whatever is already there;
///   - `create_dir_all(target_parent)` can leave behind harmless empty
///     directories if it fails partway, but touches nothing that holds
///     customer data;
///   - exactly ONE `std::fs::rename` — a single filesystem-level directory
///     move, atomic on the same volume on POSIX (`rename(2)`) and Windows
///     (`MoveFileExW`). It carries the checkout's entire tree in one op —
///     `.git`, uncommitted changes, untracked files, all of it — with no
///     per-file copy step that could stop partway. A cross-device target (a
///     different drive on Windows, a different filesystem on POSIX) or a file
///     locked open somewhere inside the tree fails the WHOLE rename, leaving
///     the checkout exactly where it was; there is no "moved half the files"
///     state this can reach.
fn relocate_checkout(repo_root: &Path, target_parent: &Path) -> Result<PathBuf, String> {
    if let Some(parent) = repo_root.parent() {
        if same_directory(parent, target_parent) {
            return Ok(repo_root.to_path_buf());
        }
    }
    let checkout_name = repo_root.file_name().ok_or_else(|| {
        format!(
            "{} has no final path component to relocate",
            native(repo_root)
        )
    })?;
    let destination = target_parent.join(checkout_name);
    if destination.exists() {
        return Err(format!(
            "{} already exists; refusing to relocate the checkout there (nothing was moved)",
            native(&destination)
        ));
    }
    std::fs::create_dir_all(target_parent).map_err(|e| {
        format!(
            "could not create the workspace directory {}: {e}",
            native(target_parent)
        )
    })?;
    std::fs::rename(repo_root, &destination).map_err(|e| {
        format!(
            "could not move the checkout from {} to {}: {e}{} (the checkout was left in place)",
            native(repo_root),
            native(&destination),
            rename_failure_hint(&e),
        )
    })?;
    Ok(destination)
}

/// A one-line remedy appended to a failed rename's message, when the OS error
/// names a fixable cause (tan-cli#185 review finding 11) — `""` for every
/// other error, since most causes (permissions, a full disk) have no such
/// one-liner.
///
///   * Windows `ERROR_SHARING_VIOLATION` (32): something inside the checkout
///     is open — often the invoking shell's own cwd, verified live by running
///     the move from inside the checkout being moved.
///   * A cross-device move: Windows `ERROR_NOT_SAME_DEVICE` (17), POSIX
///     `EXDEV` (18) — `rename(2)`/`MoveFileExW` cannot cross a volume or
///     filesystem boundary, verified live moving across drives.
fn rename_failure_hint(e: &std::io::Error) -> &'static str {
    match e.raw_os_error() {
        Some(32) if cfg!(windows) => " -- re-run from outside the checkout (e.g. `cd ..` first)",
        Some(17) if cfg!(windows) => " -- pick a --workspace on the same drive as the checkout",
        Some(18) if !cfg!(windows) => {
            " -- pick a --workspace on the same filesystem as the checkout"
        }
        _ => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Fresh temp dir for one test, tagged and pid-scoped like the other
    /// bootstrap test suites in this crate.
    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "tan-bootstrap-relocate-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn a_clean_parent_or_one_already_holding_dot_west_needs_no_relocation() {
        let clean = tmp("default-target-clean");
        let repo_root = clean.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        assert!(default_relocation_target(&repo_root, &clean, ".venv").is_none());

        let workspace = tmp("default-target-workspace");
        let repo_root = workspace.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::create_dir_all(workspace.join(".west")).unwrap();
        std::fs::write(
            workspace.join(".west").join("config"),
            b"[manifest]\npath = alp-sdk\n",
        )
        .unwrap();
        std::fs::create_dir_all(workspace.join("zephyr")).unwrap();
        assert!(default_relocation_target(&repo_root, &workspace, ".venv").is_none());

        let _ = std::fs::remove_dir_all(&clean);
        let _ = std::fs::remove_dir_all(&workspace);
    }

    #[test]
    fn a_dirty_parent_targets_the_dedicated_subdirectory() {
        let dirty = tmp("default-target-dirty");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::write(dirty.join("unrelated.txt"), b"").unwrap();

        let target =
            default_relocation_target(&repo_root, &dirty, ".venv").expect("guard must fire");
        assert_eq!(target, dirty.join(DEFAULT_WORKSPACE_DIR_NAME));

        let _ = std::fs::remove_dir_all(&dirty);
    }

    #[test]
    fn a_dot_west_that_is_a_plain_file_still_guards() {
        // tan-cli#185 review finding 4: a FILE (or an empty directory with no
        // readable config) named `.west` is not a confirmed workspace.
        let dirty = tmp("default-target-dot-west-file");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::write(dirty.join(".west"), b"not a workspace").unwrap();

        let target =
            default_relocation_target(&repo_root, &dirty, ".venv").expect("guard must fire");
        assert_eq!(target, dirty.join(DEFAULT_WORKSPACE_DIR_NAME));

        let _ = std::fs::remove_dir_all(&dirty);
    }

    #[test]
    fn bootstraps_own_venv_needs_no_relocation() {
        // tan-cli#185 review finding 2: a bootstrap that created the venv but
        // died before `west init` ever wrote `.west` must not be refused on
        // retry over its own state.
        let dirty = tmp("default-target-own-venv");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::create_dir_all(dirty.join(".venv")).unwrap();
        assert!(default_relocation_target(&repo_root, &dirty, ".venv").is_none());

        let _ = std::fs::remove_dir_all(&dirty);
    }

    #[test]
    fn an_unreadable_parent_is_treated_as_needing_no_relocation() {
        // `list_entries` returns `None` for a parent that cannot even be
        // read (here: does not exist) — `default_relocation_target` must not
        // treat that as "confirmed dirty".
        let ghost_parent = std::env::temp_dir().join(format!(
            "tan-bootstrap-relocate-ghost-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&ghost_parent);
        let repo_root = ghost_parent.join("alp-sdk");
        assert!(default_relocation_target(&repo_root, &ghost_parent, ".venv").is_none());
    }

    #[test]
    fn a_non_interactive_run_refuses_a_dirty_parent_naming_both_remedies() {
        let dirty = tmp("non-interactive-refuse");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::write(dirty.join("unrelated.txt"), b"").unwrap();

        match resolve_workspace(&repo_root, &dirty, None, false, ".venv") {
            Resolution::Refuse {
                exit,
                code,
                message,
            } => {
                assert_eq!(exit, ExitCode::ValidationFailure);
                assert_eq!(code, "workspace-guard");
                assert!(message.contains("--workspace <path>"), "{message}");
                assert!(message.contains("dedicated directory"), "{message}");
            }
            _ => panic!("expected Refuse"),
        }
        // Nothing written: still exactly the one file plus the checkout dir.
        let names: Vec<_> = std::fs::read_dir(&dirty)
            .unwrap()
            .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
            .collect();
        assert_eq!(names.len(), 2, "{names:?}");
        assert!(repo_root.is_dir(), "the checkout must be left in place");

        let _ = std::fs::remove_dir_all(&dirty);
    }

    #[test]
    fn a_clean_parent_is_unchanged_even_non_interactively() {
        let clean = tmp("non-interactive-clean");
        let repo_root = clean.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();

        assert!(matches!(
            resolve_workspace(&repo_root, &clean, None, false, ".venv"),
            Resolution::Unchanged
        ));

        let _ = std::fs::remove_dir_all(&clean);
    }

    #[test]
    fn an_explicit_workspace_relocates_with_no_guard_and_preserves_the_tree() {
        let dirty = tmp("explicit-workspace-source");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(repo_root.join(".git")).unwrap();
        std::fs::write(repo_root.join("untracked.txt"), b"uncommitted work").unwrap();
        std::fs::write(dirty.join("unrelated.txt"), b"").unwrap(); // would guard, but --workspace bypasses it entirely

        let target_parent = tmp("explicit-workspace-target");
        match resolve_workspace(
            &repo_root,
            &dirty,
            Some(target_parent.as_path()),
            false, // non-interactive -- must still succeed with NO prompt
            ".venv",
        ) {
            Resolution::Relocated {
                repo_root: new_root,
                workspace_dir,
                notice,
            } => {
                assert_eq!(new_root, target_parent.join("alp-sdk"));
                assert_eq!(workspace_dir, target_parent);
                assert!(notice.contains("tan-cli#185"), "{notice}");
            }
            _ => panic!("expected Relocated"),
        }
        assert!(!repo_root.exists(), "the source location must be gone");
        assert!(target_parent.join("alp-sdk").join(".git").is_dir());
        assert_eq!(
            std::fs::read_to_string(target_parent.join("alp-sdk").join("untracked.txt")).unwrap(),
            "uncommitted work",
            "untracked/uncommitted content must move with the directory, unmodified"
        );

        let _ = std::fs::remove_dir_all(&dirty);
        let _ = std::fs::remove_dir_all(&target_parent);
    }

    #[test]
    fn relocating_onto_an_existing_destination_refuses_and_moves_nothing() {
        let dirty = tmp("collision-source");
        let repo_root = dirty.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();
        std::fs::write(repo_root.join("marker.txt"), b"original").unwrap();

        let target_parent = tmp("collision-target");
        std::fs::create_dir_all(target_parent.join("alp-sdk")).unwrap();

        match resolve_workspace(
            &repo_root,
            &dirty,
            Some(target_parent.as_path()),
            false,
            ".venv",
        ) {
            Resolution::MoveFailed(message) => {
                assert!(message.contains("already exists"), "{message}");
                assert!(message.contains("nothing was moved"), "{message}");
            }
            _ => panic!("expected MoveFailed"),
        }
        assert!(repo_root.is_dir(), "the source must be left in place");
        assert_eq!(
            std::fs::read_to_string(repo_root.join("marker.txt")).unwrap(),
            "original",
            "the original checkout's own content must be untouched"
        );

        let _ = std::fs::remove_dir_all(&dirty);
        let _ = std::fs::remove_dir_all(&target_parent);
    }

    #[test]
    fn an_explicit_workspace_already_naming_the_current_parent_is_a_no_op() {
        let parent = tmp("explicit-already-there");
        let repo_root = parent.join("alp-sdk");
        std::fs::create_dir_all(&repo_root).unwrap();

        assert!(matches!(
            resolve_workspace(&repo_root, &parent, Some(parent.as_path()), false, ".venv"),
            Resolution::Unchanged
        ));
        assert!(repo_root.is_dir());

        let _ = std::fs::remove_dir_all(&parent);
    }

    #[test]
    fn rename_failure_hint_names_the_remedy_for_a_sharing_violation_or_cross_device_move() {
        #[cfg(windows)]
        {
            assert!(
                rename_failure_hint(&std::io::Error::from_raw_os_error(32))
                    .contains("outside the checkout")
            );
            assert!(
                rename_failure_hint(&std::io::Error::from_raw_os_error(17)).contains("same drive")
            );
        }
        #[cfg(not(windows))]
        {
            assert!(
                rename_failure_hint(&std::io::Error::from_raw_os_error(18))
                    .contains("same filesystem")
            );
        }
        // An unrelated error (e.g. EACCES/ERROR_ACCESS_DENIED, 13) has no
        // one-line fix -- the hint stays empty rather than guessing.
        assert_eq!(
            rename_failure_hint(&std::io::Error::from_raw_os_error(13)),
            ""
        );
    }
}
