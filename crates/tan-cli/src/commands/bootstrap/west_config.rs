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
pub(crate) fn reconcile_west_manifest_path(sdk_root: &str) -> Option<(PathBuf, String, String)> {
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

/// Outcome of [`reconcile_west_manifest_path_for_switch`].
pub(crate) enum SwitchReconcile {
    /// Rewrote a stale pointer that named either #62's reported state (a
    /// pruned directory) or another real alp-sdk checkout under this topdir.
    Rewrote {
        config_path: PathBuf,
        old_rel: String,
        new_rel: String,
    },
    /// `.west/config`'s `manifest.path` names a directory that exists on disk
    /// and is NOT an alp-sdk checkout -- left untouched.
    Blocked {
        config_path: PathBuf,
        old_rel: String,
    },
}

/// Same divergence check as [`reconcile_west_manifest_path`], gated by one
/// extra guard for `tan sdk switch` specifically: only rewrite when the stale
/// target is missing (#62's reported state) or is itself a real alp-sdk
/// checkout. `tan bootstrap`'s job IS the workspace under its topdir, so it
/// reconciles unconditionally; `sdk switch` only repoints the active-SDK
/// selection and must not also silently repoint an unrelated directory that
/// happens to share the topdir -- concretely, a plain Zephyr workspace
/// (`.west/config` naming `path = zephyr`) with an alp-sdk checkout cloned
/// beside it as a sibling: switching to that sibling must not overwrite the
/// Zephyr workspace's OWN manifest pointer just because it lives under the
/// same parent directory.
pub(crate) fn reconcile_west_manifest_path_for_switch(sdk_root: &str) -> Option<SwitchReconcile> {
    let sdk_root_path = Path::new(sdk_root);
    let topdir = sdk_root_path.parent()?;
    let config_path = topdir.join(".west").join("config");
    let contents = std::fs::read_to_string(&config_path).ok()?;
    let current_rel = tan_core::get_manifest_path(&contents)?;

    let configured = topdir.join(&current_rel);
    if same_directory(&configured, sdk_root_path) {
        return None; // already matches -- nothing to do.
    }
    if configured.exists() && !crate::util::has_loader_script(&configured) {
        return Some(SwitchReconcile::Blocked {
            config_path,
            old_rel: current_rel,
        });
    }

    reconcile_west_manifest_path(sdk_root).map(|(config_path, old_rel, new_rel)| {
        SwitchReconcile::Rewrote {
            config_path,
            old_rel,
            new_rel,
        }
    })
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
    use super::{
        SwitchReconcile, reconcile_west_manifest_path, reconcile_west_manifest_path_for_switch,
    };
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

    #[test]
    fn switch_guard_blocks_a_manifest_path_naming_a_real_non_sdk_directory() {
        // A plain Zephyr workspace (`.west/config` naming `path = zephyr`, no
        // `scripts/alp_project.py` under it) with an alp-sdk checkout cloned
        // beside it as a sibling: `tan sdk switch <the sibling>` must not
        // repoint the Zephyr workspace's OWN manifest just because it shares
        // a topdir with the SDK being switched to -- that config names a real,
        // unrelated directory, not #62's "pruned and unambiguously broken"
        // state or a stale alp-sdk sibling.
        let topdir = tmp("switch-guard-real-non-sdk");
        std::fs::create_dir_all(topdir.join("zephyr")).unwrap();
        let sdk = topdir.join("alp-sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = zephyr\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();

        match reconcile_west_manifest_path_for_switch(&sdk.to_string_lossy()) {
            Some(SwitchReconcile::Blocked { old_rel, .. }) => assert_eq!(old_rel, "zephyr"),
            other => panic!(
                "expected Blocked, got a rewrite or no-op: {}",
                other.is_some()
            ),
        }
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original,
            "the unrelated Zephyr workspace's manifest.path must be left untouched"
        );
    }

    #[test]
    fn switch_guard_allows_a_manifest_path_naming_a_stale_sdk_sibling() {
        // The ordinary #62 case: `.west/config` names a DIFFERENT, still-present
        // alp-sdk checkout under the same topdir (both have the loader script).
        // The switch guard must not block this -- it is exactly what `sdk
        // switch` is meant to reconcile.
        let topdir = tmp("switch-guard-sdk-sibling");
        let old_sdk = topdir.join("v0.11.0");
        std::fs::create_dir_all(old_sdk.join("scripts")).unwrap();
        std::fs::write(old_sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let new_sdk = topdir.join("v0.13.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.11.0\nfile = west.yml\n",
        )
        .unwrap();

        match reconcile_west_manifest_path_for_switch(&new_sdk.to_string_lossy()) {
            Some(SwitchReconcile::Rewrote { new_rel, .. }) => assert_eq!(new_rel, "v0.13.0"),
            other => panic!("expected a rewrite, got: {}", other.is_some()),
        }
    }

    #[test]
    fn switch_guard_allows_a_manifest_path_naming_a_pruned_directory() {
        // #62's reported state: the configured `path` no longer exists on disk
        // at all. The guard's `configured.exists()` check must fall through to
        // "allow" here, not "block" -- a missing directory can never be an
        // unrelated real workspace.
        let topdir = tmp("switch-guard-pruned");
        let new_sdk = topdir.join("v0.13.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        // deliberately NOT creating topdir/v0.11.0.
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.11.0\nfile = west.yml\n",
        )
        .unwrap();

        match reconcile_west_manifest_path_for_switch(&new_sdk.to_string_lossy()) {
            Some(SwitchReconcile::Rewrote { new_rel, .. }) => assert_eq!(new_rel, "v0.13.0"),
            other => panic!("expected a rewrite, got: {}", other.is_some()),
        }
    }
}
