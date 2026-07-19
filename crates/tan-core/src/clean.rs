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

use std::path::{Component, Path, PathBuf};

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

/// Lexically normalize a path (collapse `.` and `..`) without touching the
/// filesystem. Duplicated from `tan-cli`'s `util::normalize_path` to keep this
/// module pure and dependency-free.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
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
pub fn clean_targets(
    project_root: &Path,
    build_root: &Path,
    manifest: Option<&SystemManifest>,
) -> Vec<PathBuf> {
    let mut candidates = vec![
        build_root.to_path_buf(),
        project_root.join(".alp-build-state.json"),
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
                    candidates.push(resolved);
                }
            }
        }
    }

    dedup_by_normalized(candidates)
}

/// Drop duplicates by lexical-normalized comparison, preserving first-seen order.
fn dedup_by_normalized(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen: Vec<PathBuf> = Vec::new();
    let mut out: Vec<PathBuf> = Vec::new();
    for p in paths {
        let key = normalize(&p);
        if !seen.contains(&key) {
            seen.push(key);
            out.push(p);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    /// A minimal manifest with slices carrying the given `build_dir`s (a `None`
    /// slice for any empty string).
    fn manifest_with(build_dirs: &[&str]) -> SystemManifest {
        let mut yaml = String::from("schema_version: 1\nslices:\n");
        for (i, bd) in build_dirs.iter().enumerate() {
            yaml.push_str(&format!(
                "- core_id: c{i}\n  os: zephyr\n  status: pending\n"
            ));
            if !bd.is_empty() {
                yaml.push_str(&format!("  build_dir: {bd}\n"));
            }
        }
        parse_system_manifest(&yaml).expect("valid test manifest")
    }

    #[test]
    fn no_manifest_yields_build_root_then_state_file() {
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), None);
        assert_eq!(
            got,
            vec![
                PathBuf::from("/p/build"),
                PathBuf::from("/p/.alp-build-state.json"),
            ]
        );
    }

    #[test]
    fn slice_build_dir_under_build_root_adds_no_target() {
        // build_dir relative to project resolves under build_root -> subsumed.
        let m = manifest_with(&["build/m55_hp-zephyr"]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(
            got,
            vec![
                PathBuf::from("/p/build"),
                PathBuf::from("/p/.alp-build-state.json"),
            ]
        );
    }

    #[test]
    fn absolute_slice_build_dir_escaping_build_root_is_appended() {
        let m = manifest_with(&["/var/tmp/yocto"]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(got.last(), Some(&PathBuf::from("/var/tmp/yocto")));
        assert_eq!(got.len(), 3);
    }

    #[test]
    fn dotdot_escape_is_appended_and_duplicates_collapse() {
        // Two slices escaping to the same place -> one extra target.
        let m = manifest_with(&["../out-of-tree", "../out-of-tree"]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(
            got,
            vec![
                PathBuf::from("/p/build"),
                PathBuf::from("/p/.alp-build-state.json"),
                PathBuf::from("/p/../out-of-tree"),
            ]
        );
    }

    #[test]
    fn slice_without_build_dir_contributes_nothing() {
        let m = manifest_with(&[""]);
        let got = clean_targets(Path::new("/p"), Path::new("/p/build"), Some(&m));
        assert_eq!(got.len(), 2);
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
