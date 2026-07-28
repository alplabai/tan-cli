// SPDX-License-Identifier: Apache-2.0
//! Host toolchain-root resolution (ADR 0021, #84). The one place that answers
//! "where is a Zephyr SDK on THIS host", for both consumers:
//!
//! - `doctor --build`, which only wants the yes/no ([`zephyr_sdk_detected`]);
//! - build-plan token substitution, which needs the actual path to put behind
//!   `${TOOLCHAIN_ROOT}` ([`resolve_toolchain_root`]).
//!
//! They were one bool-only detector in `commands/doctor.rs` before the token
//! needed a path — kept as a single resolver so the two can never disagree
//! about what counts as installed.
//!
//! ADR 0021's "never mutate PATH" is about *toolchain discovery*: the root
//! resolved here is injected into the build (as a substituted token), it is
//! never prepended to `PATH`. That is a different mechanism from
//! [`crate::venv::with_venv_on_path`], which does rewrite the spawned build
//! subprocess's `PATH` — but only so a nested `west build` / `bitbake` can
//! find `west` itself, never to make a compiler discoverable.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

/// What the host has to put behind `${TOOLCHAIN_ROOT}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ToolchainRoot {
    /// Exactly one root — either named by `ZEPHYR_SDK_INSTALL_DIR` or the
    /// single install found by scanning.
    Resolved(PathBuf),
    /// Nothing installed anywhere this looks.
    NotFound,
    /// Several installs and no `ZEPHYR_SDK_INSTALL_DIR` to disambiguate.
    /// Deliberately NOT "pick the newest": guessing wrong silently builds
    /// against a toolchain the user did not choose, and the version ordering
    /// is not lexicographic anyway (`zephyr-sdk-0.9.0` sorts above
    /// `zephyr-sdk-0.16.5`). The caller reports the candidates and asks.
    Ambiguous(Vec<PathBuf>),
}

impl ToolchainRoot {
    /// The resolved path, if there is exactly one.
    pub(crate) fn path(&self) -> Option<&Path> {
        match self {
            Self::Resolved(path) => Some(path),
            Self::NotFound | Self::Ambiguous(_) => None,
        }
    }

    /// Whether a toolchain is installed at all — ambiguity still counts, it
    /// means several are. This is `doctor --build`'s question.
    pub(crate) fn is_detected(&self) -> bool {
        !matches!(self, Self::NotFound)
    }
}

/// Resolve the host's Zephyr SDK toolchain root without spawning anything:
/// honor `ZEPHYR_SDK_INSTALL_DIR` (only when the directory it names still
/// exists -- the variable is exported from a shell profile and routinely
/// outlives the SDK it once pointed at, e.g. after `rm -rf
/// ~/zephyr-sdk-0.16.5`; trusting presence alone reported a false Pass in
/// `doctor` and the real failure surfaced later as a raw CMake toolchain
/// error, exactly what that preflight exists to catch early), else look for
/// `zephyr-sdk-*` directories in the usual install roots (home + `/opt`).
pub(crate) fn resolve_toolchain_root() -> ToolchainRoot {
    // Nested rather than a let-chain: the workspace's `rust-version = 1.86`
    // predates let-chain stabilisation (1.88).
    if let Some(dir) = std::env::var_os("ZEPHYR_SDK_INSTALL_DIR") {
        if env_dir_still_exists(&dir) {
            return ToolchainRoot::Resolved(PathBuf::from(dir));
        }
    }
    decide(installed_roots())
}

/// `true` when a Zephyr SDK toolchain is installed at all. `doctor --build`'s
/// `BuildToolProbe.zephyr_sdk`.
pub(crate) fn zephyr_sdk_detected() -> bool {
    resolve_toolchain_root().is_detected()
}

/// The one-vs-many decision, split out of the filesystem scan so it is
/// testable without mutating process-global env state (which cargo test's
/// parallel threads would race).
fn decide(mut candidates: Vec<PathBuf>) -> ToolchainRoot {
    match candidates.len() {
        0 => ToolchainRoot::NotFound,
        1 => ToolchainRoot::Resolved(candidates.remove(0)),
        _ => ToolchainRoot::Ambiguous(candidates),
    }
}

/// Every `zephyr-sdk-*` directory under the usual install roots (home +
/// `/opt`), sorted so the reported candidate list is stable run to run.
fn installed_roots() -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = vec![PathBuf::from("/opt")];
    if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
        roots.push(PathBuf::from(home));
    }
    let mut found: Vec<PathBuf> = roots.iter().flat_map(|root| scan_root(root)).collect();
    found.sort();
    found.dedup();
    found
}

/// The `zephyr-sdk-*` directories directly under `root` (missing/unreadable
/// root: none).
fn scan_root(root: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("zephyr-sdk")
        })
        .map(|entry| entry.path())
        .collect()
}

/// `true` when `env_value` (a raw `ZEPHYR_SDK_INSTALL_DIR` value) names a
/// directory that is actually present.
fn env_dir_still_exists(env_value: &OsStr) -> bool {
    Path::new(env_value).is_dir()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("tan-toolchain-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn env_dir_still_exists_requires_the_directory_to_be_present() {
        let present = temp_dir("present-sdk");
        assert!(env_dir_still_exists(present.as_os_str()));

        std::fs::remove_dir_all(&present).unwrap();
        assert!(!env_dir_still_exists(present.as_os_str()));
    }

    #[test]
    fn no_candidates_is_not_found_and_not_detected() {
        let decided = decide(Vec::new());
        assert_eq!(decided, ToolchainRoot::NotFound);
        assert!(!decided.is_detected());
        assert!(decided.path().is_none());
    }

    #[test]
    fn a_single_candidate_resolves() {
        let decided = decide(vec![PathBuf::from("/opt/zephyr-sdk-1.0.1")]);
        assert_eq!(
            decided,
            ToolchainRoot::Resolved(PathBuf::from("/opt/zephyr-sdk-1.0.1"))
        );
        assert!(decided.is_detected());
        assert_eq!(decided.path(), Some(Path::new("/opt/zephyr-sdk-1.0.1")));
    }

    #[test]
    fn several_candidates_are_ambiguous_never_guessed() {
        // Both installed and no ZEPHYR_SDK_INSTALL_DIR: refuse to pick. Note
        // 0.9.0 vs 0.16.5 — the pair a lexicographic "newest" would get
        // backwards, which is why there is no "newest" branch at all.
        let decided = decide(vec![
            PathBuf::from("/opt/zephyr-sdk-0.16.5"),
            PathBuf::from("/opt/zephyr-sdk-0.9.0"),
        ]);
        assert!(matches!(decided, ToolchainRoot::Ambiguous(ref c) if c.len() == 2));
        // Still "installed" for doctor's purposes...
        assert!(decided.is_detected());
        // ...but not a usable ${TOOLCHAIN_ROOT} value.
        assert!(decided.path().is_none());
    }

    #[test]
    fn scan_root_finds_only_zephyr_sdk_dirs_and_tolerates_a_missing_root() {
        let root = temp_dir("scan");
        std::fs::create_dir_all(root.join("zephyr-sdk-1.0.1")).unwrap();
        std::fs::create_dir_all(root.join("not-a-toolchain")).unwrap();
        let found = scan_root(&root);
        assert_eq!(found, vec![root.join("zephyr-sdk-1.0.1")]);

        std::fs::remove_dir_all(&root).unwrap();
        assert!(scan_root(&root).is_empty());
    }
}
