// SPDX-License-Identifier: Apache-2.0
//! Pure decision logic for `tan bootstrap` — the native Rust port of the SDK's
//! two canonical bootstrap scripts:
//!
//!   * `alp-sdk/scripts/bootstrap.sh`  (Linux / macOS / git-bash)
//!   * `alp-sdk/scripts/bootstrap.ps1` (native Windows, PowerShell 7+)
//!
//! Those scripts are the parity oracle: message strings, flag spellings and
//! step order come from them verbatim. Everything in this module is IO-free and
//! takes `is_windows: bool` explicitly (the `resolve_project_context` house
//! style) so both platforms' behaviour is unit-testable from either host. The
//! spawning half lives in `tan-cli`'s `commands::bootstrap`.

mod blocks;
mod runtime;

pub use blocks::{next_steps_block, optional_libs_block, print_env_block};
pub use runtime::{
    HostOs, OS_OFF, YoctoGate, in_play_runtimes, runtime_for_topology_core, yocto_gate,
    yocto_mixed_warning, yocto_only_refusal,
};

/// The Zephyr revision the alp-sdk `west.yml` pins, mirrored from
/// `bootstrap.sh`'s `ZEPHYR_VERSION` / `bootstrap.ps1`'s `$ZephyrVersion`.
/// Used ONLY to decide whether an existing `$ZEPHYR_BASE` tree is reusable —
/// `west init -l` takes the actual revision from alp-sdk's own `west.yml`.
pub const ZEPHYR_VERSION: &str = "v4.4.0";

/// The pip requirement installed into the workspace venv for `west`.
///
/// Deliberately UNPINNED: there is no west version pin anywhere in alp-sdk
/// (`pyproject.toml` declares PyYAML/jsonschema/cryptography, and both bootstrap
/// scripts run a bare `pip install --upgrade -q west`). Pinning is a one-line
/// change here — e.g. `"west==1.2.0"` — and this constant is the single place
/// that would need to move.
pub const WEST_REQUIREMENT: &str = "west";

/// Where a venv keeps its executables, and what they are called. POSIX venvs use
/// `bin/python` + `bin/west`; Windows venvs use `Scripts\python.exe` +
/// `Scripts\west.exe` (bootstrap.sh:185 vs bootstrap.ps1:172-173). Single owner
/// of the split — `commands::build::workspace` resolves the same three names
/// through this, so the two can never drift.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VenvLayout {
    /// Executable sub-directory: `bin` (POSIX) or `Scripts` (Windows).
    pub bin_dir: &'static str,
    /// Interpreter file name: `python` or `python.exe`.
    pub python: &'static str,
    /// west launcher file name: `west` or `west.exe`.
    pub west: &'static str,
}

/// The [`VenvLayout`] for the given host.
pub fn venv_layout(is_windows: bool) -> VenvLayout {
    if is_windows {
        VenvLayout {
            bin_dir: "Scripts",
            python: "python.exe",
            west: "west.exe",
        }
    } else {
        VenvLayout {
            bin_dir: "bin",
            python: "python",
            west: "west",
        }
    }
}

/// Host-interpreter candidates to probe, best first — the caller runs each in
/// order and keeps the first that actually executes.
///
/// Windows leads with the `py` launcher (`py -3`) because a machine can have a
/// perfectly good 3.12 with NO bare `python` on PATH, and because the bare
/// `python.exe` there is very often the Microsoft Store alias, which exists on
/// PATH but prints nothing (bootstrap.ps1:110-114). POSIX leads with `python3`,
/// matching `bootstrap.sh`'s hard-coded interpreter, and falls back to `python`
/// for the (rare) distro where only that exists.
pub fn python_candidates(is_windows: bool) -> &'static [&'static [&'static str]] {
    if is_windows {
        &[&["py", "-3"], &["python"], &["python3"]]
    } else {
        &[&["python3"], &["python"]]
    }
}

/// `"v4.4.0"` -> `"4.4"` — the pin's MAJOR.MINOR, which is all the
/// workspace-reuse test compares (bootstrap.sh:136 / bootstrap.ps1:132).
/// `None` for a branch/SHA revision with no leading `MAJOR.MINOR`.
pub fn pin_major_minor(version: &str) -> Option<String> {
    crate::preflight::parse_major_minor_tag(version)
}

/// Already-probed facts about the tree `$ZEPHYR_BASE` points at. The caller has
/// established that `$ZEPHYR_BASE` is set and `<ZEPHYR_BASE>/VERSION` exists
/// (both scripts skip the whole block otherwise, silently).
#[derive(Debug, Clone, Copy)]
pub struct ExistingWorkspace<'a> {
    /// Body of `<ZEPHYR_BASE>/VERSION`.
    pub version_file: &'a str,
    /// `<ZEPHYR_BASE>/../.west/` is a directory.
    pub top_is_west_workspace: bool,
    /// `<top>/.west/config`'s `[manifest] path` resolves to the SDK root.
    pub manifest_is_sdk: bool,
}

/// What to do with the `$ZEPHYR_BASE` workspace — the three outcomes both
/// scripts distinguish, each with its own message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkspaceChoice {
    /// Compatible AND alp-sdk-manifested: reuse it untouched. Skips `west init`
    /// / `west update` AND the #769 legibility guard.
    Reuse {
        /// The reused tree's Zephyr `MAJOR.MINOR`, for the ok message.
        major_minor: String,
    },
    /// A matching-version west workspace whose manifest is NOT alp-sdk's
    /// `west.yml` — reusing it would leave `west alp-migrate` unknown (#769).
    ManifestMismatch,
    /// Not a `<pin>.x` west workspace at all — ignore it so it cannot hijack
    /// `west init`, and build an isolated workspace.
    Incompatible,
}

/// Decide the workspace-selection outcome from already-gathered facts.
/// Mirrors bootstrap.sh:150-162 / bootstrap.ps1:150-163 exactly: reuse needs
/// ALL THREE of a `.west/` topdir, a MAJOR.MINOR match against the pin, and a
/// manifest that resolves to the SDK root.
pub fn decide_workspace_reuse(
    existing: &ExistingWorkspace,
    pin_major_minor: &str,
) -> WorkspaceChoice {
    let existing_mm = crate::preflight::parse_zephyr_version_file(existing.version_file);
    let version_matches = existing_mm.as_deref() == Some(pin_major_minor);
    if existing.top_is_west_workspace && version_matches {
        if existing.manifest_is_sdk {
            return WorkspaceChoice::Reuse {
                major_minor: existing_mm.unwrap_or_default(),
            };
        }
        return WorkspaceChoice::ManifestMismatch;
    }
    WorkspaceChoice::Incompatible
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn venv_layout_splits_bin_and_scripts() {
        let posix = venv_layout(false);
        assert_eq!(
            (posix.bin_dir, posix.python, posix.west),
            ("bin", "python", "west")
        );
        let win = venv_layout(true);
        assert_eq!(
            (win.bin_dir, win.python, win.west),
            ("Scripts", "python.exe", "west.exe")
        );
    }

    #[test]
    fn python_candidate_order_leads_with_py_launcher_on_windows_only() {
        assert_eq!(
            python_candidates(true),
            &[&["py", "-3"][..], &["python"], &["python3"]]
        );
        assert_eq!(python_candidates(false), &[&["python3"][..], &["python"]]);
    }

    #[test]
    fn pin_parses_to_major_minor() {
        assert_eq!(pin_major_minor(ZEPHYR_VERSION).as_deref(), Some("4.4"));
        assert_eq!(pin_major_minor("4.4.0").as_deref(), Some("4.4"));
        assert_eq!(pin_major_minor("main"), None);
    }

    /// A real Zephyr `VERSION` file body (extra fields present, order as shipped).
    const VERSION_44: &str =
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\nEXTRAVERSION =\n";

    #[test]
    fn reuse_needs_workspace_version_and_manifest() {
        let facts = ExistingWorkspace {
            version_file: VERSION_44,
            top_is_west_workspace: true,
            manifest_is_sdk: true,
        };
        assert_eq!(
            decide_workspace_reuse(&facts, "4.4"),
            WorkspaceChoice::Reuse {
                major_minor: "4.4".to_string()
            }
        );
    }

    #[test]
    fn matching_version_with_a_foreign_manifest_is_a_mismatch_not_a_reuse() {
        // #769: reusing this would leave `west alp-migrate` unknown.
        let facts = ExistingWorkspace {
            version_file: VERSION_44,
            top_is_west_workspace: true,
            manifest_is_sdk: false,
        };
        assert_eq!(
            decide_workspace_reuse(&facts, "4.4"),
            WorkspaceChoice::ManifestMismatch
        );
    }

    #[test]
    fn a_stale_version_or_missing_dot_west_is_incompatible() {
        let stale = ExistingWorkspace {
            version_file: "VERSION_MAJOR = 3\nVERSION_MINOR = 7\n",
            top_is_west_workspace: true,
            manifest_is_sdk: true,
        };
        assert_eq!(
            decide_workspace_reuse(&stale, "4.4"),
            WorkspaceChoice::Incompatible
        );

        let no_west = ExistingWorkspace {
            version_file: VERSION_44,
            top_is_west_workspace: false,
            manifest_is_sdk: true,
        };
        assert_eq!(
            decide_workspace_reuse(&no_west, "4.4"),
            WorkspaceChoice::Incompatible
        );

        let unparseable = ExistingWorkspace {
            version_file: "not a version file",
            top_is_west_workspace: true,
            manifest_is_sdk: true,
        };
        assert_eq!(
            decide_workspace_reuse(&unparseable, "4.4"),
            WorkspaceChoice::Incompatible
        );
    }
}
