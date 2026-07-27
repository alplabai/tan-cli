// SPDX-License-Identifier: Apache-2.0
//! Build-readiness pre-flight: the shared readiness probe (`tan build`'s gate
//! and `tan doctor --build` speak the same language) plus the text-mode
//! auto-bootstrap that collapses `init → switch → bootstrap → build`.

use std::path::Path;

use tan_core::ProjectContext;
use tan_core::debug::{DoctorCheck, DoctorStatus};
use tan_core::preflight::{
    PreflightInput, build_preflight_checks, preflight_blocked, preflight_next_steps,
    preflight_summary,
};

use super::CommandRun;
use crate::cli::{BootstrapArgs, GlobalArgs};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use crate::venv::west_program;

use super::native::base_dir;
use super::workspace::west_workspace_dir;

/// Probe the build prerequisites (SDK / board.yaml / workspace / west) into the
/// pure `tan_core::preflight` checks. Shared by `tan build`'s pre-flight gate and
/// `tan doctor --build` so both speak the same readiness language.
pub(crate) fn probe_build_preflight(g: &GlobalArgs, context: &ProjectContext) -> Vec<DoctorCheck> {
    let base = base_dir(context);
    let sdk_root = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));
    let workspace = west_workspace_dir(&base, sdk_root.as_deref());
    let west = west_program(&base, sdk_root.as_deref());
    let west_available = crate::util::tool_available(&west);

    let input = PreflightInput {
        sdk_root: sdk_root
            .as_deref()
            .map(|p| p.to_string_lossy().into_owned()),
        board_yaml_present: context
            .board_yaml_path
            .as_deref()
            .is_some_and(|p| Path::new(p).exists()),
        workspace_dir: workspace
            .as_deref()
            .map(|p| p.to_string_lossy().into_owned()),
        west_available,
        workspace_zephyr_version: read_workspace_zephyr_version(workspace.as_deref()),
        sdk_zephyr_pin: read_sdk_zephyr_pin(sdk_root.as_deref()),
        project_scope: g.project.clone(),
    };
    build_preflight_checks(&input)
}

/// Read the reused workspace's Zephyr `MAJOR.MINOR.PATCH` from
/// `<workspace>/zephyr/VERSION`. `None` when there is no workspace or the file
/// is absent/unparseable — the preflight then simply skips the
/// version-compatibility check.
fn read_workspace_zephyr_version(workspace_dir: Option<&Path>) -> Option<String> {
    let version_file = workspace_dir?.join("zephyr").join("VERSION");
    let body = std::fs::read_to_string(version_file).ok()?;
    tan_core::preflight::parse_zephyr_version_file(&body)
}

/// Read the active SDK's pinned Zephyr `MAJOR.MINOR.PATCH` from
/// `<sdk>/west.yml`. `None` when unresolved, unreadable, or the pin is a
/// branch/SHA.
fn read_sdk_zephyr_pin(sdk_root: Option<&Path>) -> Option<String> {
    let west_yml = sdk_root?.join("west.yml");
    let body = std::fs::read_to_string(west_yml).ok()?;
    tan_core::preflight::parse_west_zephyr_pin(&body)
}

/// Text-mode build pre-flight gate over an already-probed set of checks — split
/// out from probing so the warm `tan build` path can reuse a single
/// [`probe_build_preflight`] call instead of probing twice (once here, once in
/// [`maybe_auto_bootstrap`]). If any check blocks the build, returns a colorful
/// readiness report to short-circuit with; `None` means all clear — proceed.
pub(super) fn gate_from_checks(g: &GlobalArgs, checks: Vec<DoctorCheck>) -> Option<CommandRun> {
    if !preflight_blocked(&checks) {
        return None;
    }
    let summary = preflight_summary(&checks);
    let steps = preflight_next_steps(&checks);
    let text = crate::style::render_report(g, "Build readiness", "", &checks, &summary, &steps);
    Some(CommandRun {
        exit: ExitCode::ValidationFailure,
        text,
        json: None,
    })
}

/// Text-mode convenience that collapses `init → switch → bootstrap → build`:
/// when the SDK and board.yaml are present, bootstrap on demand (reuses a
/// compatible Zephyr, else bootstraps) and return the re-resolved context. Fires
/// either when no Zephyr workspace is resolved OR when a reused workspace's
/// Zephyr is stale versus the SDK pin (so a stale reuse self-heals instead of
/// producing build failures that look like regressions). Returns `None` —
/// leaving the readiness gate to guide the user — when the workspace is already
/// resolved and current, or when the SDK or board.yaml is missing (bootstrap
/// needs the SDK, so those are surfaced first rather than triggering a doomed
/// bootstrap). Bootstrap streams live via inherited stdio, so the returned
/// summary is intentionally discarded.
pub(super) fn maybe_auto_bootstrap(
    g: &GlobalArgs,
    checks: &[DoctorCheck],
) -> Option<ProjectContext> {
    let is_fail = |name: &str| {
        checks
            .iter()
            .any(|c| c.name == name && c.status == DoctorStatus::Fail)
    };
    let is_warn = |name: &str| {
        checks
            .iter()
            .any(|c| c.name == name && c.status == DoctorStatus::Warn)
    };
    // Bootstrap when there is no workspace at all, OR when the reused workspace's
    // Zephyr diverges from the SDK pin (stale reuse — `tan bootstrap` refreshes
    // it to the pinned version). Still gated on the SDK + board.yaml being
    // present, since bootstrap needs the SDK.
    //
    // `zephyrVersion` compares the full MAJOR.MINOR.PATCH since #98, so a
    // patch-level pin bump now reaches this branch too. That closes the second
    // half of the same blind spot: `--no-auto-bootstrap`'s own `--help` promises
    // the default bootstraps a "stale" workspace, and against a `v4.4.0` tree on
    // a `v4.4.1` pin it previously did not.
    let missing_workspace = is_fail("workspace");
    let stale_zephyr = is_warn("zephyrVersion");
    if (!missing_workspace && !stale_zephyr) || is_fail("sdk") || is_fail("boardYaml") {
        return None;
    }
    let reason = if missing_workspace {
        "No Zephyr workspace yet"
    } else {
        "Reused Zephyr workspace is stale versus the SDK pin"
    };
    eprintln!("{reason} — bootstrapping (reuse a compatible Zephyr, else bootstrap one)…");
    let bootstrap_run = crate::commands::bootstrap::run(
        g,
        &BootstrapArgs {
            no_pip: false,
            no_west: false,
            print_env: false,
        },
    );
    // `bootstrap::run` can return early with the ONLY actionable message the
    // user gets — a missing `git`/`cmake`/`python` with its `winget`/apt hint,
    // or a Yocto-only project on a non-Linux host. The old `let _ =` discarded
    // that `CommandRun` entirely, so the readiness gate below fell through to
    // its generic `run \`tan bootstrap\`` hint, which on those hosts can never
    // succeed. This is text-mode-only (the JSON path never calls
    // `maybe_auto_bootstrap` — see the `!g.is_json()` gate at the call site),
    // so printing here is the full fix; there is no envelope to fold an Issue
    // into.
    print_bootstrap_failure(&bootstrap_run, std::io::stderr());
    Some(resolve_cli_project_context(g))
}

/// Print a delegated bootstrap's own diagnostic lines when it failed (e.g. the
/// Windows "not supported, use WSL2" pointer) instead of silently falling
/// through to the readiness gate's generic — and here unactionable — hint.
/// A no-op on success. Takes an explicit writer so this is unit-testable
/// without capturing real stderr.
fn print_bootstrap_failure(run: &CommandRun, mut out: impl std::io::Write) {
    if run.exit.code() != 0 {
        for line in &run.text {
            let _ = writeln!(out, "{line}");
        }
    }
}

#[cfg(test)]
mod print_bootstrap_failure_tests {
    use super::*;

    #[test]
    fn prints_the_failed_run_s_lines() {
        // Regression: this used to be `let _ = crate::commands::bootstrap::run(...)`
        // — a failed delegate's own diagnostic (e.g. bootstrap's missing-tool
        // list with its per-tool install hints) was thrown away entirely, and
        // the readiness gate's generic "run `tan bootstrap`" hint took its
        // place. `bootstrap` is native on every host now (#49), so the shape
        // that matters is a real prerequisite failure, not a platform refusal.
        let run = CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: vec![
                "Missing required tools:".to_string(),
                "  cmake  ->  winget install -e --id Kitware.CMake".to_string(),
                "Install the tools above (then reopen PowerShell) and re-run.".to_string(),
            ],
            json: None,
        };
        let mut buf = Vec::new();
        print_bootstrap_failure(&run, &mut buf);
        let printed = String::from_utf8(buf).unwrap();
        assert!(printed.contains("Missing required tools"), "{printed}");
        assert!(
            printed.contains("winget install -e --id Kitware.CMake"),
            "{printed}"
        );
    }

    #[test]
    fn is_silent_on_a_successful_run() {
        let run = CommandRun {
            exit: ExitCode::Success,
            text: vec!["should never be printed".to_string()],
            json: None,
        };
        let mut buf = Vec::new();
        print_bootstrap_failure(&run, &mut buf);
        assert!(buf.is_empty());
    }
}
