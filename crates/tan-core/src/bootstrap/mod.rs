// SPDX-License-Identifier: Apache-2.0
//! Pure decision logic for `tan bootstrap` — the native Rust port of the SDK's
//! two canonical bootstrap scripts:
//!
//!   * `alp-sdk/scripts/bootstrap.sh`  (Linux / macOS / git-bash)
//!   * `alp-sdk/scripts/bootstrap.ps1` (native Windows, PowerShell 7+)
//!
//! Those scripts are the parity oracle for CONTROL FLOW and message strings;
//! the FACTS they act on (pins, tool lists, argv, env map, hints) are
//! single-sourced from `alp-sdk/metadata/bootstrap.json` (alp-sdk#917) — see
//! [`manifest`], which is the tan-side reader that makes it so, even though the
//! manifest's own `_comment` (as vendored) still calls tan only an INTENDED
//! future consumer. Everything in this module is IO-free and takes
//! `is_windows: bool` explicitly (the `resolve_project_context` house style)
//! so both platforms' behaviour is unit-testable from either host. The
//! spawning half lives in `tan-cli`'s `commands::bootstrap`.
//!
//! Citations here name an ANCHOR (a function, variable or section heading) and
//! never a line number: the oracles have already moved twice under this port.

mod blocks;
pub mod manifest;
mod prerequisites;
mod runtime;

pub use blocks::{next_steps_block, optional_libs_block, print_env_block, render_env_lines};
pub use manifest::{
    BOOTSTRAP_MANIFEST_REL_PATH, BootstrapFacts, BootstrapManifestError, Tokens, fallback_facts,
    parse_bootstrap_manifest, venv_exe_names,
};
pub use prerequisites::{
    BOOTSTRAP_MANIFEST_REJECTED_PREFIX, MissingPrerequisite, PrereqFailure,
    apply_prerequisite_check, doctor_prerequisite_check, hint_line, install_command,
    posix_python_not_runnable, posix_refusal, python_too_old, reported_missing,
    windows_python_not_runnable, windows_refusal,
};
pub use runtime::{
    HostOs, OS_OFF, YoctoGate, in_play_runtimes, runtime_for_topology_core, yocto_gate,
    yocto_mixed_warning, yocto_only_refusal,
};

/// FALLBACK Zephyr pin, used only when the SDK has no
/// `metadata/bootstrap.json` — which is EVERY released SDK: that file has never
/// existed on alp-sdk `origin/main` (absent from its whole history and from
/// `v0.13.0`), so this constant, not the manifest, is what a customer on a
/// release actually gets. It tracks the value the **`dev`** manifest's
/// `zephyr.version` carries, since dev is the only place the manifest exists;
/// it deliberately does NOT match what the refs taking this path pin, as those
/// predate the manifest and still say `revision: v4.4.0` in `west.yml` — which
/// is exactly why [`resolve_pin_major_minor`] prefers `west.yml` over this
/// constant, and why the live value comes from
/// [`BootstrapFacts::zephyr_version`] whenever a manifest is present.
pub const ZEPHYR_VERSION: &str = "v4.4.1";

/// FALLBACK west pip requirement, used only when the SDK has no
/// `metadata/bootstrap.json`. Mirrors that manifest's `west.pipSpec`.
///
/// It is a FLOOR, not a pin: `west>=0.14.0` is what the pinned Zephyr's
/// `requirements-base.txt` declares (unchanged across the v4.4.0 -> v4.4.1 pin
/// bump — alp-sdk's own manifest still carries this spec at v4.4.1, and
/// `check_bootstrap_manifest.py` is what keeps that honest). Note the two
/// bootstrap scripts still run a
/// bare `pip install --upgrade -q west` and do NOT yet consume `pipSpec`
/// themselves (the manifest calls it "informational today") — tan consuming it
/// is strictly the safer side of that divergence, since an ancient `west` on a
/// long-lived venv is a real failure mode. Changing the floor is a manifest
/// edit, not a tan release.
pub const WEST_REQUIREMENT: &str = "west>=0.14.0";

/// Where a venv keeps its executables, and what they are called. POSIX venvs use
/// `bin/python` + `bin/west`; Windows venvs use `Scripts\python.exe` +
/// `Scripts\west.exe` (`bootstrap.sh`'s `VBIN`/`VPY`/`WEST` assignment;
/// `bootstrap.ps1`'s `$Vpy`/`$West`). The DIRECTORY names are facts from
/// `metadata/bootstrap.json` (`venv.posixBinDir`/`windowsBinDir`); the
/// executable names are not in the manifest and live here. Single owner of the
/// split — tan-cli's `crate::venv` resolves the same three names through this,
/// so the two can never drift.
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
/// PATH but prints nothing (`bootstrap.ps1`'s `$PyVer` check). POSIX leads with
/// `python3`, matching `bootstrap.sh`'s hard-coded interpreter, and falls back
/// to `python` for the (rare) distro where only that exists.
pub fn python_candidates(is_windows: bool) -> &'static [&'static [&'static str]] {
    if is_windows {
        &[&["py", "-3"], &["python"], &["python3"]]
    } else {
        &[&["python3"], &["python"]]
    }
}

/// `"v4.4.0"` -> `"4.4"` — the pin's MAJOR.MINOR, which is all the
/// workspace-reuse test compares (`bootstrap.sh`'s `PIN_MM` / `bootstrap.ps1`'s
/// `$PinMM`). `None` for a branch/SHA revision with no leading `MAJOR.MINOR`.
pub fn pin_major_minor(version: &str) -> Option<String> {
    crate::preflight::parse_major_minor_tag(version)
}

/// The ONE Zephyr pin the workspace-reuse test compares against: the SDK's own
/// `west.yml` `zephyr` revision when it parses, else the manifest's
/// `zephyr.version` (which `check_bootstrap_manifest.py` keeps equal to it),
/// else the [`ZEPHYR_VERSION`] fallback that `facts_version` already carries.
///
/// Why `west.yml` leads: `commands::build::preflight`'s `zephyrVersion` check
/// compares the workspace against exactly that file
/// ([`crate::preflight::parse_west_zephyr_pin`]) and warns when they diverge,
/// while `build`'s auto-bootstrap fires ON that warning. With two pin sources,
/// an SDK pin bump made bootstrap ADOPT a workspace ("Existing workspace
/// reused") that preflight simultaneously called stale — an auto-bootstrap loop
/// that never converges. One authority, one verdict.
pub fn resolve_pin_major_minor(west_yml: Option<&str>, facts_version: &str) -> String {
    west_yml
        .and_then(crate::preflight::parse_west_zephyr_pin)
        .or_else(|| pin_major_minor(facts_version))
        .unwrap_or_default()
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

/// Decide the workspace-selection outcome from already-gathered facts. Mirrors
/// the "workspace selection (reuse a compatible ZEPHYR_BASE)" section of both
/// scripts exactly: reuse needs ALL THREE of a `.west/` topdir, a MAJOR.MINOR
/// match against the pin, and a manifest that resolves to the SDK root.
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

    /// `west.yml` as the SDK ships it, pinning `revision` for the zephyr project.
    fn west_yml(revision: &str) -> String {
        format!(
            "manifest:\n  projects:\n    - name: zephyr\n      revision: {revision}\n  self:\n    \
             path: alp-sdk\n"
        )
    }

    #[test]
    fn the_reuse_pin_comes_from_west_yml_so_it_cannot_disagree_with_preflight() {
        // The deadlock this prevents: after an SDK pin bump to v4.5.0, a 4.4
        // workspace must be judged STALE by the reuse test, because
        // `preflight`'s zephyrVersion check reads the same west.yml and warns —
        // and `build`'s auto-bootstrap fires on that warning. If bootstrap kept
        // comparing against its own hardcoded "4.4" it would ADOPT the very
        // workspace preflight calls stale, and the two would never converge.
        let bumped = west_yml("v4.5.0");
        let pin = resolve_pin_major_minor(Some(&bumped), ZEPHYR_VERSION);
        assert_eq!(pin, "4.5", "west.yml must win over the fallback constant");
        assert_eq!(
            crate::preflight::parse_west_zephyr_pin(&bumped).as_deref(),
            Some(pin.as_str()),
            "bootstrap and preflight must read the same pin from the same file"
        );

        let stale_workspace = ExistingWorkspace {
            version_file: VERSION_44,
            top_is_west_workspace: true,
            manifest_is_sdk: true,
        };
        assert_eq!(
            decide_workspace_reuse(&stale_workspace, &pin),
            WorkspaceChoice::Incompatible,
            "a 4.4 workspace must not be reused once the SDK pins 4.5"
        );
    }

    #[test]
    fn the_reuse_pin_falls_back_when_west_yml_is_absent_or_a_branch() {
        // No west.yml (or a branch/SHA revision with no MAJOR.MINOR) -> the
        // manifest's zephyr.version, which itself falls back to ZEPHYR_VERSION.
        assert_eq!(resolve_pin_major_minor(None, ZEPHYR_VERSION), "4.4");
        assert_eq!(
            resolve_pin_major_minor(Some(&west_yml("main")), ZEPHYR_VERSION),
            "4.4"
        );
        // A manifest that bumped ahead of an unreadable west.yml still wins
        // over the compiled-in constant.
        assert_eq!(resolve_pin_major_minor(None, "v4.6.0"), "4.6");
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
