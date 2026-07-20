// SPDX-License-Identifier: Apache-2.0
//! Pure removal-planning for `tan clean` — the native port of the retired
//! `west alp-clean` / `scripts/west_commands/alp_clean.py`. No IO: this decides
//! *what* to remove and *how* to classify each path; the filesystem work lives in
//! `tan-cli`'s `commands/clean.rs`.
//!
//! The faithful port removes two targets, in order: the build root (recursively)
//! and the app-root `.alp-build-state.json` (verbatim from `alp_clean.py`
//! `targets[]`). Under ADR-0020 the native command adds one enhancement — a
//! manifest-aware sweep of any slice `build_dir` that escapes the build root
//! (an out-of-tree Yocto tmp dir, say). Slice dirs already under the build root
//! are subsumed by the recursive build-root removal, so they add nothing.

use std::path::{Path, PathBuf};

use crate::path_guard::{is_unsafe_removal_target, normalize};
use crate::system_manifest::SystemManifest;

/// The disposition of one removal target once its filesystem type is known.
/// Pure — decoupled from IO so the message/exit branching is unit-testable
/// without touching disk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CleanAction {
    /// A directory to remove recursively (`shutil.rmtree`).
    RemoveDir,
    /// A file to unlink.
    RemoveFile,
    /// `--dry-run`: a directory that *would* be rmtree'd.
    WouldRemoveDir,
    /// `--dry-run`: a file that *would* be unlinked.
    WouldRemoveFile,
    /// Neither a dir nor a file (absent) — skipped entirely, not counted.
    Absent,
}

/// Classify a target from its probed filesystem type + the dry-run flag. Mirrors
/// `alp_clean.py`'s `is_dir()` / `is_file()` if/elif ladder: a path that is
/// neither is [`CleanAction::Absent`] (falls through, no removal).
pub fn classify(is_dir: bool, is_file: bool, dry_run: bool) -> CleanAction {
    if is_dir {
        if dry_run {
            CleanAction::WouldRemoveDir
        } else {
            CleanAction::RemoveDir
        }
    } else if is_file {
        if dry_run {
            CleanAction::WouldRemoveFile
        } else {
            CleanAction::RemoveFile
        }
    } else {
        CleanAction::Absent
    }
}

/// Pure lexical containment: is `path` `base` itself or below it? Uses
/// component-wise `starts_with` on normalized paths, so `/p/build` contains
/// `/p/build/x` but NOT the sibling `/p/build2` nor a `..`-escape like
/// `/p/build/../out`.
pub fn is_under(base: &Path, path: &Path) -> bool {
    normalize(path).starts_with(normalize(base))
}

/// Ordered, de-duplicated removal targets for `tan clean`:
///
///   1. `build_root` (recursive) — matches `alp_clean.py` `targets[0]`.
///   2. `project_root/.alp-build-state.json` — verbatim `targets[1]`. (The
///      orchestrator actually writes its cache at `build_root/.alp-build-state.json`,
///      already subsumed by the build-root removal; this app-root path is the
///      faithful, usually-absent target the Python kept — preserved, not "fixed".)
///   3. Each manifest slice `build_dir` that lies OUTSIDE `build_root` (absolute,
///      or `..`-escaping). Slice dirs under `build_root` are already covered, so
///      they contribute nothing. This is the single justified reason to consult
///      the manifest; a project whose slices all build under `build_root` yields
///      no extra targets.
///
/// A relative slice `build_dir` is resolved against `project_root`; an absolute
/// one is taken as-is. Duplicates (by lexical normalization) are dropped,
/// preserving first-seen order (build_root stays first).
///
/// **Every** candidate — the build root included — is then screened by
/// [`is_unsafe_removal_target`]. The manifest is unvalidated file content, and
/// `build_dir: ""`, `.`, `/` or `../..` each resolve to the project root or
/// above; before this screen they were appended verbatim and recursively
/// deleted. A rejected candidate is NOT silently dropped — it is returned in
/// [`CleanPlan::rejected`] so the caller can surface it and fail.
pub fn clean_targets(
    project_root: &Path,
    build_root: &Path,
    manifest: Option<&SystemManifest>,
) -> CleanPlan {
    let mut candidates = vec![
        (build_root.to_path_buf(), TargetOrigin::BuildRoot),
        (
            project_root.join(".alp-build-state.json"),
            TargetOrigin::StateFile,
        ),
    ];

    if let Some(m) = manifest {
        for slice in &m.slices {
            if let Some(build_dir) = slice.build_dir.as_deref() {
                let resolved = if Path::new(build_dir).is_absolute() {
                    PathBuf::from(build_dir)
                } else {
                    project_root.join(build_dir)
                };
                if !is_under(build_root, &resolved) {
                    candidates.push((
                        resolved,
                        TargetOrigin::SliceBuildDir {
                            core_id: slice.core_id.clone(),
                            raw: build_dir.to_string(),
                        },
                    ));
                }
            }
        }
    }

    let mut plan = CleanPlan::default();
    for (path, origin) in dedup_by_normalized(candidates) {
        // The state file is a single unlink, never a recursive removal, and it
        // is always `project_root/.alp-build-state.json` — not attacker-shaped.
        if matches!(origin, TargetOrigin::StateFile)
            || !is_unsafe_removal_target(project_root, &path)
        {
            plan.targets.push(path);
        } else {
            plan.rejected.push(RejectedTarget { path, origin });
        }
    }
    plan
}

/// The removal targets for one `tan clean`, plus everything refused as unsafe.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CleanPlan {
    /// Paths cleared for removal, in order (build root first).
    pub targets: Vec<PathBuf>,
    /// Candidates refused by [`is_unsafe_removal_target`]. Non-empty means the
    /// command must report and fail, never quietly clean less than asked.
    pub rejected: Vec<RejectedTarget>,
}

/// A candidate refused as too dangerous to remove, with where it came from so
/// the message can name the culprit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RejectedTarget {
    pub path: PathBuf,
    pub origin: TargetOrigin,
}

/// Provenance of a removal candidate — drives the user-facing message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetOrigin {
    /// The resolved `--build-root` / `<app_path>/build`.
    BuildRoot,
    /// `project_root/.alp-build-state.json`.
    StateFile,
    /// A `build_dir` read from a system-manifest slice.
    SliceBuildDir { core_id: String, raw: String },
}

impl RejectedTarget {
    /// One-line explanation naming the source, for the issue/message text.
    pub fn reason(&self) -> String {
        match &self.origin {
            TargetOrigin::BuildRoot => format!(
                "refusing to remove build root {} — it is the project root, an ancestor of it, or a filesystem root",
                self.path.display()
            ),
            TargetOrigin::StateFile => {
                format!("refusing to remove state file {}", self.path.display())
            }
            TargetOrigin::SliceBuildDir { core_id, raw } => format!(
                "refusing to remove slice '{core_id}' build_dir {raw:?} (resolves to {}) — it is the project root, an ancestor of it, or a filesystem root; fix build/system-manifest.yaml",
                self.path.display()
            ),
        }
    }
}

/// Drop duplicates by lexical-normalized comparison, preserving first-seen order.
fn dedup_by_normalized(paths: Vec<(PathBuf, TargetOrigin)>) -> Vec<(PathBuf, TargetOrigin)> {
    let mut seen: Vec<PathBuf> = Vec::new();
    let mut out: Vec<(PathBuf, TargetOrigin)> = Vec::new();
    for (p, origin) in paths {
        let key = normalize(&p);
        if !seen.contains(&key) {
            seen.push(key);
            out.push((p, origin));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    /// A minimal manifest whose slices carry the given `build_dir`s, each
    /// emitted as an explicit quoted scalar — so `""` tests `build_dir: ""`
    /// (present but empty), NOT the absent-key case. The previous helper
    /// omitted the key for an empty string, which is why the empty-`build_dir`
    /// project-root deletion went uncaught.
    fn manifest_with(build_dirs: &[&str]) -> SystemManifest {
        let mut yaml = String::from("schema_version: 1\nslices:\n");
        for (i, bd) in build_dirs.iter().enumerate() {
            yaml.push_str(&format!(
                "- core_id: c{i}\n  os: zephyr\n  status: pending\n  build_dir: \"{bd}\"\n"
            ));
        }
        parse_system_manifest(&yaml).expect("valid test manifest")
    }

    /// A manifest whose slice omits `build_dir` entirely (the `None` case the
    /// old helper actually exercised).
    fn manifest_without_build_dir() -> SystemManifest {
        parse_system_manifest(
            "schema_version: 1\nslices:\n- core_id: c0\n  os: zephyr\n  status: pending\n",
        )
        .expect("valid test manifest")
    }

    #[test]
    fn no_manifest_yields_build_root_then_state_file() {
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), None);
        assert_eq!(
            got.targets,
            vec![
                PathBuf::from("/p/build"),
                PathBuf::from("/p/.alp-build-state.json"),
            ]
        );
        assert!(got.rejected.is_empty());
    }

    #[test]
    fn slice_build_dir_under_build_root_adds_no_target() {
        // build_dir relative to project resolves under build_root -> subsumed.
        let m = manifest_with(&["build/m55_hp-zephyr"]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(
            got.targets,
            vec![
                PathBuf::from("/p/build"),
                PathBuf::from("/p/.alp-build-state.json"),
            ]
        );
        assert!(got.rejected.is_empty());
    }

    #[test]
    fn absolute_slice_build_dir_escaping_build_root_is_appended() {
        // Out-of-tree build dirs stay supported — the guard blocks catastrophic
        // targets, not merely outside ones.
        let m = manifest_with(&["/var/tmp/yocto"]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(got.targets.last(), Some(&PathBuf::from("/var/tmp/yocto")));
        assert_eq!(got.targets.len(), 3);
        assert!(got.rejected.is_empty());
    }

    #[test]
    fn shallow_dotdot_escape_is_appended_and_duplicates_collapse() {
        // Two slices escaping to the same sibling dir -> one extra target. The
        // parent of the project is NOT an ancestor of it here because the
        // escape lands beside the project, not above it.
        let m = manifest_with(&["../out-of-tree", "../out-of-tree"]);
        let got = clean_targets(
            Path::new("/home/u/p"),
            Path::new("/home/u/p/build"),
            Some(&m),
        );
        assert_eq!(
            got.targets,
            vec![
                PathBuf::from("/home/u/p/build"),
                PathBuf::from("/home/u/p/.alp-build-state.json"),
                PathBuf::from("/home/u/p/../out-of-tree"),
            ]
        );
        assert!(got.rejected.is_empty());
    }

    #[test]
    fn slice_without_build_dir_contributes_nothing() {
        let m = manifest_without_build_dir();
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(got.targets.len(), 2);
        assert!(got.rejected.is_empty());
    }

    /// The critical data-loss case: a present-but-empty `build_dir` resolves to
    /// the project root itself. Before the guard this was appended and
    /// `remove_dir_all`'d, deleting the user's sources at exit 0.
    #[test]
    fn empty_slice_build_dir_is_rejected_not_removed() {
        let m = manifest_with(&[""]);
        let got = clean_targets(
            Path::new("/home/u/myapp"),
            Path::new("/home/u/myapp/build"),
            Some(&m),
        );
        assert_eq!(
            got.targets,
            vec![
                PathBuf::from("/home/u/myapp/build"),
                PathBuf::from("/home/u/myapp/.alp-build-state.json"),
            ]
        );
        assert_eq!(got.rejected.len(), 1);
        assert!(got.rejected[0].reason().contains("refusing to remove"));
    }

    #[test]
    fn catastrophic_slice_build_dirs_are_all_rejected() {
        for raw in [".", "/", "../../..", "../.."] {
            let m = manifest_with(&[raw]);
            let got = clean_targets(
                Path::new("/home/u/myapp"),
                Path::new("/home/u/myapp/build"),
                Some(&m),
            );
            assert_eq!(
                got.targets.len(),
                2,
                "build_dir {raw:?} must not add a removal target"
            );
            assert_eq!(got.rejected.len(), 1, "build_dir {raw:?} must be reported");
        }
    }

    /// The `rm -rf $UNSET_VAR` shape: `--build-root ""` resolves to the project
    /// root, which used to pass every guard and get recursively removed.
    #[test]
    fn build_root_equal_to_project_root_is_rejected() {
        let project = Path::new("/home/u/myapp");
        for build_root in [project.to_path_buf(), project.join("."), project.join("..")] {
            let got = clean_targets(project, &build_root, None);
            assert!(
                !got.targets.iter().any(|t| t == &build_root),
                "{} must not be a removal target",
                build_root.display()
            );
            assert_eq!(
                got.rejected.len(),
                1,
                "{} must be reported",
                build_root.display()
            );
        }
    }

    #[test]
    fn is_under_containment_rules() {
        assert!(is_under(Path::new("/p/build"), Path::new("/p/build/x")));
        assert!(is_under(Path::new("/p/build"), Path::new("/p/build")));
        assert!(!is_under(Path::new("/p/build"), Path::new("/p/build2")));
        assert!(!is_under(Path::new("/p/build"), Path::new("/p")));
        assert!(!is_under(
            Path::new("/p/build"),
            Path::new("/p/build/../out")
        ));
    }

    #[test]
    fn classify_covers_every_arm() {
        assert_eq!(classify(true, false, false), CleanAction::RemoveDir);
        assert_eq!(classify(true, false, true), CleanAction::WouldRemoveDir);
        assert_eq!(classify(false, true, false), CleanAction::RemoveFile);
        assert_eq!(classify(false, true, true), CleanAction::WouldRemoveFile);
        assert_eq!(classify(false, false, false), CleanAction::Absent);
        assert_eq!(classify(false, false, true), CleanAction::Absent);
    }
}
