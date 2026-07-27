// SPDX-License-Identifier: Apache-2.0
//! Pure build pre-flight: given the CLI's resolved project/workspace state,
//! report ordered readiness checks so `tan build` (and `tan doctor`) can tell the
//! user what is missing and how to fix it *before* a build is attempted —
//! instead of surfacing a raw `west` / CMake error. Filesystem probing happens in
//! the adapter (the CLI); this module stays pure and unit-testable.

use crate::debug::{DoctorCheck, DoctorStatus, DoctorSummary};

/// Resolved, already-probed inputs for the build pre-flight. The CLI fills these
/// from `resolve_project_context` + filesystem checks; this module never touches
/// the filesystem.
#[derive(Debug, Clone, Default)]
pub struct PreflightInput {
    /// Resolved alp-sdk checkout, or `None` when unresolved.
    pub sdk_root: Option<String>,
    /// Whether the resolved `board.yaml` exists on disk.
    pub board_yaml_present: bool,
    /// Resolved west workspace topdir (holds `.west/`), or `None` when none was
    /// found (reused or bootstrapped).
    pub workspace_dir: Option<String>,
    /// Whether a usable `west` was resolved (a workspace venv west or one on PATH).
    pub west_available: bool,
    /// Zephyr `MAJOR.MINOR.PATCH` present in the reused workspace (e.g.
    /// `"4.4.1"`), read from `<workspace>/zephyr/VERSION`. `None` when there is
    /// no workspace or the file is unreadable/unparseable.
    pub workspace_zephyr_version: Option<String>,
    /// Zephyr `MAJOR.MINOR.PATCH` the active SDK's `west.yml` pins (e.g.
    /// `"4.4.1"`). `None` when unresolved or the pin is a branch/SHA (no version
    /// to compare).
    pub sdk_zephyr_pin: Option<String>,
    /// The `--project` value this invocation ran with, when it had one. Only
    /// the `sdk` remediation reads it, and only to stay honest: the `.alp/
    /// sdk-path` pointer `tan sdk switch` writes is scoped to
    /// `cli_workspace_root` (cwd joined with `--project`), so a bare `tan sdk
    /// switch <path>` does NOT fix a `tan --project <p> …` invocation — it
    /// reports success and changes nothing about the failure (issue #101).
    pub project_scope: Option<String>,
}

/// Ordered build-readiness checks. A `Fail` blocks the build; a `Warn` is
/// advisory. Each non-`Pass` `detail` embeds the one-line fix so the guidance is
/// visible even without `--verbose`.
pub fn build_preflight_checks(input: &PreflightInput) -> Vec<DoctorCheck> {
    let mut checks = Vec::new();

    // Remediation text below names `tan`, not `alp` — RFC #837 retired the `alp`
    // binary, and these strings land verbatim in issues[].message / nextSteps[]
    // that the vscode extension renders. Telling a user to run a binary that no
    // longer exists (`command not found: alp`) is a dead end, not a fix.
    // The `--project` variant is not cosmetic. The pointer `tan sdk switch`
    // writes is scoped per `--project` (pinned by
    // `switch_and_current_use_project_scoped_workspace_root_not_process_cwd`),
    // so suggesting a bare `tan sdk switch <path>` to someone who ran
    // `tan --project <p> …` sends them to a command that prints "Switched
    // project SDK to …", visibly updates `tan sdk current`, and then fails
    // byte-for-byte identically. Name the scope, and name `--sdk-root` — the
    // one flag that always works and which this message never mentioned.
    checks.push(match (&input.sdk_root, &input.project_scope) {
        (Some(root), _) => pass("sdk", format!("alp-sdk at {root}")),
        (None, Some(project)) => fail(
            "sdk",
            &format!(
                "no SDK selected — the `.alp/sdk-path` pointer is scoped to `--project`, so run `tan --project {project} sdk switch <path>` (or pass `--sdk-root <path>`)"
            ),
            &format!("tan --project {project} sdk switch <path>"),
        ),
        (None, None) => fail(
            "sdk",
            "no SDK selected — run `tan sdk switch <path>` or `tan sdk install <ver>`",
            "tan sdk switch <path>",
        ),
    });

    checks.push(if input.board_yaml_present {
        pass("boardYaml", "board.yaml found".to_string())
    } else {
        fail(
            "boardYaml",
            "board.yaml not found — run `tan init` or pass `--board-yaml <path>`",
            "tan init",
        )
    });

    checks.push(match &input.workspace_dir {
        Some(dir) => pass("workspace", format!("Zephyr workspace at {dir}")),
        None => fail(
            "workspace",
            "no Zephyr workspace — run `tan bootstrap` (reuses a compatible Zephyr, else bootstraps one)",
            "tan bootstrap",
        ),
    });

    checks.push(if input.west_available {
        pass("westResolved", "west resolved".to_string())
    } else {
        warn(
            "westResolved",
            "west not found — run `tan bootstrap` to create the workspace venv",
            "tan bootstrap",
        )
    });

    // Reused-workspace Zephyr compatibility. A workspace bootstrapped once and
    // reused across sessions can sit on a stale Zephyr after the SDK bumps its
    // west.yml pin — which surfaces as build failures that look like regressions
    // (the v3.7.0-vs-v4.4.0 incident). The reuse check used to run only at
    // bootstrap time; do it here too so `tan build`/`tan doctor` stop reporting
    // a clean Pass on a stale workspace. Only emitted when there is a workspace
    // to check AND both versions are known — Warn (advisory), so a mismatch
    // guides re-bootstrap without hard-blocking a workspace that may still
    // build.
    //
    // Both sides are FULL `MAJOR.MINOR.PATCH` (#98). Comparing the truncated
    // `MAJOR.MINOR` let a patch-level SDK pin bump (`v4.4.0` -> `v4.4.1`) read
    // as a match, so this check Passed, `build`'s auto-bootstrap never fired on
    // it, and the build was green against the wrong Zephyr.
    if input.workspace_dir.is_some() {
        if let (Some(ws), Some(pin)) = (&input.workspace_zephyr_version, &input.sdk_zephyr_pin) {
            checks.push(if ws == pin {
                pass("zephyrVersion", format!("Zephyr v{ws} matches the SDK pin"))
            } else {
                warn(
                    "zephyrVersion",
                    &format!(
                        "reused Zephyr v{ws} != SDK pin v{pin} — run `tan bootstrap` to refresh the workspace"
                    ),
                    "tan bootstrap",
                )
            });
        }
    }

    checks
}

/// Extract `MAJOR.MINOR.PATCH` from a Zephyr `VERSION` file body (the
/// `VERSION_MAJOR` / `VERSION_MINOR` / `PATCHLEVEL` assignment lines). `None`
/// when either of the first two is absent or non-numeric; an absent or
/// non-numeric `PATCHLEVEL` reads as `0`, which is what Zephyr's own
/// `.0` releases write. Pure — the caller reads the file.
///
/// PATCHLEVEL is part of the answer since #98: truncating it here made a
/// `v4.4.0` workspace compare equal to a `v4.4.1` pin, so both the
/// workspace-reuse decision and the `zephyrVersion` readiness check silently
/// accepted a stale tree.
pub fn parse_zephyr_version_file(body: &str) -> Option<String> {
    let mut major: Option<u32> = None;
    let mut minor: Option<u32> = None;
    let mut patch: u32 = 0;
    for line in body.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        match key.trim() {
            "VERSION_MAJOR" => major = value.trim().parse().ok(),
            "VERSION_MINOR" => minor = value.trim().parse().ok(),
            "PATCHLEVEL" => patch = value.trim().parse().unwrap_or(0),
            _ => {}
        }
    }
    Some(format!("{}.{}", major?, minor?))
}

/// Extract the Zephyr pin as `MAJOR.MINOR.PATCH` from a `west.yml` body: the
/// `manifest.projects[]` entry named `zephyr`, whose `revision` is a `vX.Y.Z`
/// tag. `None` when absent or the revision is a branch/SHA (nothing to compare).
/// Pure — the caller reads the file.
pub fn parse_west_zephyr_pin(body: &str) -> Option<String> {
    #[derive(serde::Deserialize)]
    struct WestFile {
        manifest: WestManifest,
    }
    #[derive(serde::Deserialize)]
    struct WestManifest {
        #[serde(default)]
        projects: Vec<WestProject>,
    }
    #[derive(serde::Deserialize)]
    struct WestProject {
        name: Option<String>,
        revision: Option<String>,
    }

    let parsed: WestFile = serde_yaml::from_str(body).ok()?;
    let revision = parsed
        .manifest
        .projects
        .into_iter()
        .find(|p| p.name.as_deref() == Some("zephyr"))?
        .revision?;
    parse_version_tag(&revision)
}

/// `"v4.4.1"` / `"4.4.1"` -> `"4.4.1"`; `None` for a branch/SHA revision that
/// has no leading `MAJOR.MINOR`. Also the parse behind
/// [`crate::bootstrap::pin_version`] — the workspace-reuse test compares the
/// SAME shape [`parse_zephyr_version_file`] produces, so there is one
/// normalisation, not two.
///
/// Normalises the two shapes that would otherwise defeat the comparison: a
/// missing PATCH reads as `0` (so a `v4.4` pin matches a `4.4.0` tree rather
/// than provoking an endless refresh), and a pre-release suffix is dropped from
/// the patch component (`v4.4.0-rc1` -> `4.4.0`) rather than failing the whole
/// parse and disabling the check outright.
pub fn parse_version_tag(revision: &str) -> Option<String> {
    let trimmed = revision.trim();
    let stripped = trimmed.strip_prefix('v').unwrap_or(trimmed);
    let mut parts = stripped.split('.');
    let major: u32 = parts.next()?.parse().ok()?;
    let minor: u32 = parts.next()?.parse().ok()?;
    let patch: u32 = parts
        .next()
        .and_then(|p| p.split(|c: char| !c.is_ascii_digit()).next())
        .and_then(|p| p.parse().ok())
        .unwrap_or(0);
    Some(format!("{major}.{minor}"))
}

/// A build is blocked when any check failed.
pub fn preflight_blocked(checks: &[DoctorCheck]) -> bool {
    checks.iter().any(|c| c.status == DoctorStatus::Fail)
}

/// Tally checks into a pass/warn/fail summary.
pub fn preflight_summary(checks: &[DoctorCheck]) -> DoctorSummary {
    let mut summary = DoctorSummary {
        pass: 0,
        warn: 0,
        fail: 0,
    };
    for check in checks {
        // The same tally `append_doctor_check` uses, not a second copy of the
        // status→bucket mapping — two of them drifted the moment a fourth
        // status existed.
        crate::debug::count_into(&mut summary, check.status);
    }
    summary
}

/// Deduplicated fix hints for the non-passing checks, in evaluation order.
pub fn preflight_next_steps(checks: &[DoctorCheck]) -> Vec<String> {
    let mut steps: Vec<String> = Vec::new();
    for check in checks {
        if check.status == DoctorStatus::Pass {
            continue;
        }
        if let Some(fix) = &check.fix {
            if !steps.contains(fix) {
                steps.push(fix.clone());
            }
        }
    }
    steps
}

fn pass(name: &str, detail: String) -> DoctorCheck {
    DoctorCheck {
        name: name.to_string(),
        status: DoctorStatus::Pass,
        detail,
        fix: None,
    }
}

fn fail(name: &str, detail: &str, fix: &str) -> DoctorCheck {
    DoctorCheck {
        name: name.to_string(),
        status: DoctorStatus::Fail,
        detail: detail.to_string(),
        fix: Some(fix.to_string()),
    }
}

fn warn(name: &str, detail: &str, fix: &str) -> DoctorCheck {
    DoctorCheck {
        name: name.to_string(),
        status: DoctorStatus::Warn,
        detail: detail.to_string(),
        fix: Some(fix.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready() -> PreflightInput {
        PreflightInput {
            sdk_root: Some("/sdk".to_string()),
            board_yaml_present: true,
            workspace_dir: Some("/ws".to_string()),
            west_available: true,
            workspace_zephyr_version: Some("4.4.1".to_string()),
            sdk_zephyr_pin: Some("4.4.1".to_string()),
            project_scope: None,
        }
    }

    #[test]
    fn fully_ready_is_not_blocked_and_all_pass() {
        let checks = build_preflight_checks(&ready());
        assert!(!preflight_blocked(&checks));
        let summary = preflight_summary(&checks);
        // sdk + boardYaml + workspace + westResolved + zephyrVersion (matched).
        assert_eq!((summary.pass, summary.warn, summary.fail), (5, 0, 0));
        assert!(preflight_next_steps(&checks).is_empty());
    }

    #[test]
    fn stale_reused_zephyr_warns_but_does_not_block() {
        let input = PreflightInput {
            workspace_zephyr_version: Some("3.7.0".to_string()),
            sdk_zephyr_pin: Some("4.4.1".to_string()),
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        // A mismatch is advisory, not blocking — the workspace may still build.
        assert!(!preflight_blocked(&checks));
        let zv = checks.iter().find(|c| c.name == "zephyrVersion").unwrap();
        assert_eq!(zv.status, DoctorStatus::Warn);
        assert!(zv.detail.contains("3.7.0"));
        assert!(zv.detail.contains("4.4.1"));
        assert!(
            preflight_next_steps(&checks)
                .iter()
                .any(|s| s == "tan bootstrap")
        );
    }

    #[test]
    fn a_patch_level_pin_bump_is_stale_too() {
        // #98: `v4.4.0` and `v4.4.1` both truncate to "4.4", so this check used
        // to Pass — and `build`'s auto-bootstrap, which fires on this exact
        // Warn, never ran. The build was then green against the Zephyr (and
        // hal_alif) the PREVIOUS SDK pinned.
        //
        // Both sides go through the REAL parsers, not literal strings: the
        // truncation lived in `parse_zephyr_version_file` /
        // `parse_west_zephyr_pin`, so a test that hand-feeds
        // `build_preflight_checks` two already-different strings would pass
        // just as happily with the bug still in.
        let input = PreflightInput {
            workspace_zephyr_version: parse_zephyr_version_file(
                "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\nEXTRAVERSION =\n",
            ),
            sdk_zephyr_pin: parse_west_zephyr_pin(
                "manifest:\n  projects:\n    - name: zephyr\n      revision: v4.4.1\n",
            ),
            ..ready()
        };
        assert_eq!(input.workspace_zephyr_version.as_deref(), Some("4.4.0"));
        assert_eq!(input.sdk_zephyr_pin.as_deref(), Some("4.4.1"));
        let checks = build_preflight_checks(&input);
        let zv = checks.iter().find(|c| c.name == "zephyrVersion").unwrap();
        assert_eq!(zv.status, DoctorStatus::Warn, "{}", zv.detail);
    }

    #[test]
    fn zephyr_version_check_is_skipped_when_unknown() {
        // No workspace VERSION / no comparable pin -> no zephyrVersion check
        // (don't nag when we can't actually verify).
        let input = PreflightInput {
            workspace_zephyr_version: None,
            sdk_zephyr_pin: Some("4.4.1".to_string()),
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        assert!(checks.iter().all(|c| c.name != "zephyrVersion"));
    }

    #[test]
    fn parses_zephyr_version_file() {
        let body = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\nEXTRAVERSION =\n";
        assert_eq!(parse_zephyr_version_file(body).as_deref(), Some("4.4.1"));
        // PATCHLEVEL is carried, not truncated (#98) — this is the whole bug.
        let dot_zero = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\nEXTRAVERSION =\n";
        assert_eq!(
            parse_zephyr_version_file(dot_zero).as_deref(),
            Some("4.4.0")
        );
        // An absent PATCHLEVEL is a `.0` release, not an unparseable file.
        assert_eq!(
            parse_zephyr_version_file("VERSION_MAJOR = 4\nVERSION_MINOR = 4\n").as_deref(),
            Some("4.4.0")
        );
        assert_eq!(parse_zephyr_version_file("VERSION_MAJOR = 4\n"), None);
        assert_eq!(parse_zephyr_version_file(""), None);
    }

    #[test]
    fn parses_west_zephyr_pin() {
        let body = "manifest:\n  projects:\n    - name: hal_alif\n      revision: v2.2.0\n    - name: zephyr\n      revision: v4.4.1\n";
        assert_eq!(parse_west_zephyr_pin(body).as_deref(), Some("4.4.1"));
        // A branch/SHA revision has no MAJOR.MINOR to compare -> None.
        let sha = "manifest:\n  projects:\n    - name: zephyr\n      revision: abcdef1234\n";
        assert_eq!(parse_west_zephyr_pin(sha), None);
        // No zephyr project -> None.
        let none = "manifest:\n  projects:\n    - name: hal_alif\n      revision: v2.2.0\n";
        assert_eq!(parse_west_zephyr_pin(none), None);
    }

    #[test]
    fn version_tags_normalise_a_missing_or_pre_release_patch() {
        assert_eq!(parse_version_tag("v4.4.1").as_deref(), Some("4.4.1"));
        assert_eq!(parse_version_tag("4.4.1").as_deref(), Some("4.4.1"));
        // A pin with no PATCH must match a `.0` tree, not chase it forever.
        assert_eq!(parse_version_tag("v4.4").as_deref(), Some("4.4.0"));
        // A pre-release suffix must not disable the comparison outright.
        assert_eq!(parse_version_tag("v4.5.0-rc1").as_deref(), Some("4.5.0"));
        assert_eq!(parse_version_tag("main"), None);
    }

    #[test]
    fn missing_sdk_blocks_with_a_switch_hint() {
        let input = PreflightInput {
            sdk_root: None,
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        assert!(preflight_blocked(&checks));
        let sdk = checks.iter().find(|c| c.name == "sdk").unwrap();
        assert_eq!(sdk.status, DoctorStatus::Fail);
        // Regression guard: the `alp` binary was retired (RFC #837) — a
        // remediation hint pointing at it is a dead end for the user.
        assert!(sdk.detail.contains("tan sdk switch"));
        assert!(!sdk.detail.contains("alp sdk switch"));
        assert!(
            preflight_next_steps(&checks)
                .iter()
                .any(|s| s.contains("tan sdk switch"))
        );
    }

    #[test]
    fn missing_sdk_under_project_scope_names_the_scoped_switch() {
        // Issue #101: a bare `tan sdk switch <path>` reports success and leaves
        // a `tan --project <p> …` build failing byte-for-byte identically,
        // because the pointer it writes is scoped to `--project`. The hint has
        // to carry the scope, and has to name `--sdk-root`.
        let input = PreflightInput {
            sdk_root: None,
            project_scope: Some("examples/peripheral-io/gpio-button-led".to_string()),
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        let sdk = checks.iter().find(|c| c.name == "sdk").unwrap();
        assert_eq!(sdk.status, DoctorStatus::Fail);
        assert!(
            sdk.detail
                .contains("tan --project examples/peripheral-io/gpio-button-led sdk switch <path>")
        );
        assert!(sdk.detail.contains("--sdk-root"));
        assert_eq!(
            sdk.fix.as_deref(),
            Some("tan --project examples/peripheral-io/gpio-button-led sdk switch <path>")
        );
    }

    #[test]
    fn missing_board_and_workspace_both_block() {
        let input = PreflightInput {
            board_yaml_present: false,
            workspace_dir: None,
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        assert!(preflight_blocked(&checks));
        let summary = preflight_summary(&checks);
        assert_eq!(summary.fail, 2);
    }

    #[test]
    fn missing_west_warns_but_does_not_block() {
        let input = PreflightInput {
            west_available: false,
            ..ready()
        };
        let checks = build_preflight_checks(&input);
        assert!(!preflight_blocked(&checks));
        let west = checks.iter().find(|c| c.name == "westResolved").unwrap();
        assert_eq!(west.status, DoctorStatus::Warn);
    }
}
