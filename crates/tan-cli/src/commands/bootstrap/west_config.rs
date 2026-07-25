// SPDX-License-Identifier: Apache-2.0
//! Reconciling a stale `.west/config` manifest pointer before `west init` /
//! `west update` runs (#31).

use std::path::{Path, PathBuf};

/// Reconciles a stale `[manifest] path` in `<dirname(sdk_root)>/.west/config`
/// to `sdk_root`, when they diverge (#31). `west init -l <sdk_root>` sets
/// `topdir = dirname(sdk_root)` and writes `[manifest] path = <basename of
/// sdk_root>`; the "already initialised" branch runs `west update` without
/// re-running `west init -l`, so a `.west/config` left behind by a *different*
/// SDK checkout that shares the same topdir (e.g. `tan sdk switch` between two
/// `~/.alp/sdk-cache/<version>` entries) keeps pointing at the stale SDK's
/// `west.yml`.
///
/// Conservative and non-fatal by construction: every failure mode (no parent
/// dir, no `.west/config`, unreadable, no `[manifest] path` line, write
/// failure) falls through to `None` — the `west init -l` step has its own
/// guards, this only fixes the clear-divergence case. Returns `(config_path,
/// old_rel, new_rel)` when it rewrote the file, for the optional text-mode
/// info line.
pub(super) fn reconcile_west_manifest_path(sdk_root: &str) -> Option<(PathBuf, String, String)> {
    let sdk_root_path = Path::new(sdk_root);
    let topdir = sdk_root_path.parent()?;
    let config_path = topdir.join(".west").join("config");
    let contents = std::fs::read_to_string(&config_path).ok()?;
    let current_rel = tan_core::get_manifest_path(&contents)?;

    let configured = topdir.join(&current_rel);
    if same_directory(&configured, sdk_root_path) {
        return None; // already matches -- nothing to do.
    }

    let new_rel = sdk_root_path.file_name()?.to_string_lossy().into_owned();
    let rewritten = tan_core::set_manifest_path(&contents, &new_rel)?;
    // Atomic replace: write a sibling temp in the same `.west/` dir, then
    // rename over `config` (`std::fs::rename` replaces an existing dest on both
    // POSIX and Windows). `.west/config` is the topdir's ONLY manifest pointer,
    // shared by every SDK version under it — a crash mid-write must not leave it
    // truncated/corrupt, which would break `west` for all of them.
    let tmp_path = config_path.with_file_name(format!("config.{}.tan-tmp", std::process::id()));
    if std::fs::write(&tmp_path, &rewritten).is_err() {
        let _ = std::fs::remove_file(&tmp_path);
        return None;
    }
    if std::fs::rename(&tmp_path, &config_path).is_err() {
        let _ = std::fs::remove_file(&tmp_path);
        return None;
    }
    Some((config_path, current_rel, new_rel))
}

/// True when `a` and `b` name the same directory. Canonicalizes when both
/// exist on disk (the reliable answer); falls back to a lexical
/// `tan_core::normalize_path` comparison when either side doesn't (e.g. the
/// stale config's target SDK version was since pruned from the cache).
pub(super) fn same_directory(a: &Path, b: &Path) -> bool {
    match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        (Ok(ca), Ok(cb)) => ca == cb,
        _ => tan_core::normalize_path(a) == tan_core::normalize_path(b),
    }
}

#[cfg(test)]
mod tests {
    use super::reconcile_west_manifest_path;
    use std::path::PathBuf;

    /// Fresh temp dir for one test, tagged and pid-scoped like the other
    /// command test suites in this crate (sdk.rs, clean.rs, flash/mod.rs, …).
    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-bootstrap-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn reconcile_rewrites_a_divergent_manifest_path() {
        // Two cached SDK versions sharing one topdir (~/.alp/sdk-cache/*):
        // `.west/config` still names the FIRST one bootstrap ran `west init
        // -l` against; `tan sdk switch`-ing to the second must reconcile it.
        let topdir = tmp("reconcile-divergent");
        std::fs::create_dir_all(topdir.join("v0.6.0")).unwrap();
        let new_sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.6.0\nfile = west.yml\n[zephyr]\nbase = zephyr\n",
        )
        .unwrap();

        let (config_path, old_rel, new_rel) =
            reconcile_west_manifest_path(&new_sdk.to_string_lossy()).expect("expected a rewrite");
        assert_eq!(config_path, west_dir.join("config"));
        assert_eq!(old_rel, "v0.6.0");
        assert_eq!(new_rel, "v0.7.0");
        assert_eq!(
            std::fs::read_to_string(&config_path).unwrap(),
            "[manifest]\npath = v0.7.0\nfile = west.yml\n[zephyr]\nbase = zephyr\n"
        );
        // Atomic temp+rename leaves no stray `config.*.tan-tmp` behind.
        let leftover = std::fs::read_dir(&west_dir)
            .unwrap()
            .filter_map(Result::ok)
            .any(|e| e.file_name().to_string_lossy().contains("tan-tmp"));
        assert!(!leftover, "temp file was not cleaned up after the rename");
    }

    #[test]
    fn reconcile_is_a_no_op_when_the_manifest_path_already_matches() {
        let topdir = tmp("reconcile-matching");
        let sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = v0.7.0\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();

        assert!(reconcile_west_manifest_path(&sdk.to_string_lossy()).is_none());
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original
        );
    }

    #[test]
    fn reconcile_rewrites_when_the_configured_old_dir_was_pruned_from_the_cache() {
        // The configured `path = <old>` dir no longer exists on disk (the
        // old SDK cache entry was pruned) but the new sdk_root does --
        // exercises `same_directory`'s `normalize_path` fallback branch
        // (canonicalize fails for the missing side).
        let topdir = tmp("reconcile-pruned-old");
        let new_sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        // deliberately NOT creating topdir/v0.6.0.
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.6.0\nfile = west.yml\n",
        )
        .unwrap();

        let (config_path, old_rel, new_rel) =
            reconcile_west_manifest_path(&new_sdk.to_string_lossy()).expect("expected a rewrite");
        assert_eq!(config_path, west_dir.join("config"));
        assert_eq!(old_rel, "v0.6.0");
        assert_eq!(new_rel, "v0.7.0");
        assert_eq!(
            std::fs::read_to_string(&config_path).unwrap(),
            "[manifest]\npath = v0.7.0\nfile = west.yml\n"
        );
    }

    #[test]
    fn reconcile_is_a_no_op_without_a_west_config() {
        let topdir = tmp("reconcile-no-config");
        let sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&sdk).unwrap();
        // No .west/config at all -- the `west init -l` step's own guard handles
        // this case; reconcile must not fail or fabricate anything.
        assert!(reconcile_west_manifest_path(&sdk.to_string_lossy()).is_none());
    }
}
