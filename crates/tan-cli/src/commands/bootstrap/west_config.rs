// SPDX-License-Identifier: Apache-2.0
//! Reconciling a stale `.west/config` manifest pointer before `west init` /
//! `west update` runs (#31).

use std::path::{Path, PathBuf};

use tan_core::ManifestReconcile;

/// Reconciles a stale `[manifest] path` in `<dirname(sdk_root)>/.west/config`
/// to `sdk_root`, when they diverge (#31). `west init -l <sdk_root>` sets
/// `topdir = dirname(sdk_root)` and writes `[manifest] path = <basename of
/// sdk_root>`; the "already initialised" branch runs `west update` without
/// re-running `west init -l`, so a `.west/config` left behind by a *different*
/// SDK checkout that shares the same topdir (e.g. `tan sdk switch` between two
/// `~/.alp/sdk-cache/<version>` entries) keeps pointing at the stale SDK's
/// `west.yml`.
///
/// Still non-fatal, but no longer SILENT: the [`ManifestReconcile`] outcome
/// separates "nothing to do" from "a rewrite was needed and did not happen", so
/// a caller can tell the user their workspace is still broken instead of
/// reporting clean success over a `.west/config` it could not write.
pub(crate) fn reconcile_west_manifest_path(sdk_root: &str) -> ManifestReconcile {
    reconcile(sdk_root, false)
}

/// The shared body of the two entry points. `switch_guard` is the only
/// difference between them, and it sits INSIDE the single read of
/// `.west/config` rather than in a second pass over the file — the guard has to
/// judge the same bytes the rewrite acts on.
fn reconcile(sdk_root: &str, switch_guard: bool) -> ManifestReconcile {
    let sdk_root_path = Path::new(sdk_root);
    let Some(topdir) = sdk_root_path.parent() else {
        return ManifestReconcile::NotApplicable;
    };
    let config_path = topdir.join(".west").join("config");
    let contents = match std::fs::read_to_string(&config_path) {
        Ok(contents) => contents,
        // ENOENT is the ordinary "no west workspace under this topdir" case.
        // ANY other read error (a read-only ACL, a `config` held open by another
        // process, an I/O error) means a `.west/config` IS there and we cannot
        // see whether it is stale — reporting that as "nothing to do" is exactly
        // the silent-success bug.
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return ManifestReconcile::NotApplicable;
        }
        Err(err) => {
            return ManifestReconcile::Failed {
                config_path,
                old_rel: None,
                reason: err.to_string(),
            };
        }
    };
    // A config with no `[manifest] path` line carries no pointer to reconcile;
    // `west` itself is what complains about that.
    let Some(current_rel) = tan_core::get_manifest_path(&contents) else {
        return ManifestReconcile::NotApplicable;
    };

    let configured = topdir.join(&current_rel);
    if same_directory(&configured, sdk_root_path) {
        return ManifestReconcile::AlreadyMatches;
    }
    if switch_guard && configured.exists() && !crate::util::has_loader_script(&configured) {
        return ManifestReconcile::Blocked {
            config_path,
            old_rel: current_rel,
        };
    }
    // A path with no final component (a bare root, or one ending in `..`) has no
    // name to write into the config — nothing to reconcile TO.
    let Some(new_rel) = sdk_root_path.file_name() else {
        return ManifestReconcile::NotApplicable;
    };
    write_manifest_path(
        config_path,
        &contents,
        current_rel,
        new_rel.to_string_lossy().into_owned(),
    )
}

/// Rewrites `config_path`'s `[manifest] path` to `new_rel`, mapping every way
/// that can fail onto [`ManifestReconcile::Failed`] with the OS's own reason
/// attached rather than onto a silent no-op.
///
/// Atomic replace: write a sibling temp in the same `.west/` dir, then rename
/// over `config` (`std::fs::rename` replaces an existing dest on both POSIX and
/// Windows). `.west/config` is the topdir's ONLY manifest pointer, shared by
/// every SDK version under it — a crash mid-write must not leave it
/// truncated/corrupt, which would break `west` for all of them.
fn write_manifest_path(
    config_path: PathBuf,
    contents: &str,
    old_rel: String,
    new_rel: String,
) -> ManifestReconcile {
    let failed = |config_path, reason: String| ManifestReconcile::Failed {
        config_path,
        old_rel: Some(old_rel.clone()),
        reason,
    };
    // Unreachable in practice (`get_manifest_path` just found that line), but a
    // rewrite that did not happen is a failure, never a no-op.
    let Some(rewritten) = tan_core::set_manifest_path(contents, &new_rel) else {
        return failed(
            config_path,
            "no [manifest] path line to rewrite".to_string(),
        );
    };
    let tmp_path = config_path.with_file_name(format!("config.{}.tan-tmp", std::process::id()));
    if let Err(err) = std::fs::write(&tmp_path, &rewritten) {
        let _ = std::fs::remove_file(&tmp_path);
        return failed(config_path, err.to_string());
    }
    if let Err(err) = std::fs::rename(&tmp_path, &config_path) {
        // The Windows case worth naming: a `config` open in another process, or
        // marked read-only, fails the replace even though writing the temp file
        // beside it succeeded.
        let _ = std::fs::remove_file(&tmp_path);
        return failed(config_path, err.to_string());
    }
    ManifestReconcile::Rewrote {
        config_path,
        old_rel,
        new_rel,
    }
}

/// Where a topdir records which SDK checkout its west workspace was last
/// `west update`d against. Beside `.west/config` on purpose: it describes THAT
/// workspace, and disappears with it when the user deletes `.west/`.
fn workspace_sdk_record(topdir: &Path) -> PathBuf {
    topdir.join(".west").join("tan-workspace-sdk")
}

/// Records that `topdir`'s workspace is now synced to `sdk_root` — called by
/// `tan bootstrap` after a `west update` that actually ran.
///
/// Nothing else on disk answers "which SDK's manifest were these trees checked
/// out from": `.west/config` names the manifest repo but is rewritten by the
/// reconcile above (and by `west init`) WITHOUT the trees changing, so it cannot
/// stand in for the update having happened. Best-effort — a workspace that
/// cannot record it simply keeps getting the `tan bootstrap` advice.
pub(crate) fn record_workspace_sdk(topdir: &Path, sdk_root: &str) {
    let _ = super::super::sdk::write_sdk_pointer(&workspace_sdk_record(topdir), sdk_root);
}

/// Whether `sdk_root`'s topdir workspace was last synced to `sdk_root` itself.
///
/// `false` for a workspace with no record at all — including every workspace
/// bootstrapped before this file existed. That is the honest answer ("cannot be
/// confirmed"), and it costs one advisory line that a single `tan bootstrap`
/// run clears for good.
pub(crate) fn workspace_synced_to(sdk_root: &str) -> bool {
    let sdk_root_path = Path::new(sdk_root);
    let Some(topdir) = sdk_root_path.parent() else {
        return false;
    };
    let Ok(raw) = std::fs::read_to_string(workspace_sdk_record(topdir)) else {
        return false;
    };
    let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return false;
    };
    parsed
        .get("sdkPath")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|recorded| same_directory(Path::new(recorded), sdk_root_path))
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
pub(crate) fn reconcile_west_manifest_path_for_switch(sdk_root: &str) -> ManifestReconcile {
    reconcile(sdk_root, true)
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
        ManifestReconcile, reconcile_west_manifest_path, reconcile_west_manifest_path_for_switch,
        record_workspace_sdk, workspace_synced_to,
    };
    use std::path::PathBuf;

    /// `(config_path, old_rel, new_rel)` from a [`ManifestReconcile::Rewrote`],
    /// panicking with the actual outcome otherwise — the shape most tests here
    /// assert on.
    fn expect_rewrote(outcome: ManifestReconcile) -> (PathBuf, String, String) {
        match outcome {
            ManifestReconcile::Rewrote {
                config_path,
                old_rel,
                new_rel,
            } => (config_path, old_rel, new_rel),
            other => panic!("expected a rewrite, got {other:?}"),
        }
    }

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
            expect_rewrote(reconcile_west_manifest_path(&new_sdk.to_string_lossy()));
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

        assert_eq!(
            reconcile_west_manifest_path(&sdk.to_string_lossy()),
            ManifestReconcile::AlreadyMatches
        );
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
            expect_rewrote(reconcile_west_manifest_path(&new_sdk.to_string_lossy()));
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
        // this case; reconcile must not fail or fabricate anything. NotApplicable
        // rather than Failed: an ABSENT config is the one read error that means
        // "no workspace here", which is why the arm is matched on ENOENT alone.
        assert_eq!(
            reconcile_west_manifest_path(&sdk.to_string_lossy()),
            ManifestReconcile::NotApplicable
        );
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
            ManifestReconcile::Blocked { old_rel, .. } => assert_eq!(old_rel, "zephyr"),
            other => panic!("expected Blocked, got {other:?}"),
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

        let (_, _, new_rel) = expect_rewrote(reconcile_west_manifest_path_for_switch(
            &new_sdk.to_string_lossy(),
        ));
        assert_eq!(new_rel, "v0.13.0");
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

        let (_, _, new_rel) = expect_rewrote(reconcile_west_manifest_path_for_switch(
            &new_sdk.to_string_lossy(),
        ));
        assert_eq!(new_rel, "v0.13.0");
    }

    #[test]
    fn reconcile_reports_a_config_it_cannot_read_as_failed_not_as_a_no_op() {
        // The silent-success bug: every read failure used to return the same
        // `None` an already-correct config does. Only ENOENT means "no
        // workspace"; anything else (a read-only ACL, a `config` another process
        // holds open -- the routine Windows shape) leaves a REAL, possibly stale
        // pointer we could not even look at. A directory named `config` is the
        // portable way to force a non-ENOENT read error (EISDIR / access denied).
        let topdir = tmp("reconcile-unreadable");
        let new_sdk = topdir.join("v0.13.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(west_dir.join("config")).unwrap();

        match reconcile_west_manifest_path(&new_sdk.to_string_lossy()) {
            ManifestReconcile::Failed {
                config_path,
                old_rel,
                reason,
            } => {
                assert_eq!(config_path, west_dir.join("config"));
                // Nothing was read, so there is no old value to report -- the
                // caller must not invent one.
                assert_eq!(old_rel, None);
                assert!(!reason.is_empty(), "the OS reason must be carried through");
            }
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn reconcile_reports_a_rewrite_it_cannot_write_as_failed() {
        // The other half of the same bug: the write/rename arms also returned
        // `None`. Occupying the atomic temp path with a DIRECTORY makes the
        // sibling write fail on every platform, leaving `.west/config` at its
        // stale value -- which must be reported, not swallowed.
        let topdir = tmp("reconcile-unwritable");
        let new_sdk = topdir.join("v0.13.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = v0.11.0\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();
        std::fs::create_dir_all(west_dir.join(format!("config.{}.tan-tmp", std::process::id())))
            .unwrap();

        match reconcile_west_manifest_path(&new_sdk.to_string_lossy()) {
            ManifestReconcile::Failed { old_rel, .. } => {
                assert_eq!(old_rel.as_deref(), Some("v0.11.0"))
            }
            other => panic!("expected Failed, got {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original,
            "a failed rewrite must leave the config exactly as it was"
        );
    }

    #[test]
    fn a_workspace_is_synced_only_to_the_sdk_its_record_names() {
        // The fact `sdk switch` derives its `tan bootstrap` advice from. No
        // record at all -> not synced (an older tan bootstrapped it, or nothing
        // did); a record naming another SDK under the same topdir -> not synced.
        let topdir = tmp("workspace-sync-record");
        let old_sdk = topdir.join("v0.11.0");
        let new_sdk = topdir.join("v0.13.0");
        std::fs::create_dir_all(&old_sdk).unwrap();
        std::fs::create_dir_all(&new_sdk).unwrap();
        std::fs::create_dir_all(topdir.join(".west")).unwrap();

        assert!(!workspace_synced_to(&new_sdk.to_string_lossy()));

        record_workspace_sdk(&topdir, &old_sdk.to_string_lossy());
        assert!(!workspace_synced_to(&new_sdk.to_string_lossy()));
        assert!(workspace_synced_to(&old_sdk.to_string_lossy()));

        record_workspace_sdk(&topdir, &new_sdk.to_string_lossy());
        assert!(workspace_synced_to(&new_sdk.to_string_lossy()));
    }
}
