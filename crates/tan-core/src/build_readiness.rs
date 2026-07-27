// SPDX-License-Identifier: Apache-2.0
//! Build-readiness preflight — the toolchains a build (and the Yocto `.wic`
//! flash) needs, keyed off the OSes the active `board.yaml` declares. Used by
//! `tan doctor --build` (and, later, as `tan build`'s preflight).
//!
//! Scope boundary: `board.yaml` alone does not carry each core's *type*
//! (Cortex-M vs Cortex-A) — that resolves from the SoM topology in the SDK
//! metadata (owned by `alp_orchestrate.py`, deliberately not reimplemented here,
//! see EXTENSION_CLI_INTEGRATION.md §9). So we key off cores' explicit `os:`
//! fields; when none are declared we check all three backends. The authoritative
//! per-core resolution stays the SDK build-plan emit's job — this is advisory.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use crate::bootstrap::{MissingPrerequisite, reported_missing};
// `unique_next_steps` is the doctor module's, not a second copy: this file used
// to carry a byte-identical clone, and two implementations of "what goes in
// `nextSteps`" is one drift away from the two reports disagreeing.
use crate::debug::{DoctorCheck, DoctorStatus, DoctorSummary, unique_next_steps};
use crate::model::BoardModel;

/// Why a non-Linux host cannot run a Yocto build. Shared verbatim by `doctor
/// --build`'s `yoctoHost` check and `tan bootstrap`'s Yocto host gate
/// (`crate::bootstrap::yocto_only_refusal`) so the two never say different
/// things about the same host limitation.
pub const YOCTO_HOST_DETAIL: &str =
    "Yocto builds are Linux-only; use WSL2 or a Linux host/container.";

/// A build backend a `board.yaml` can target. Serializes lowercase.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum BuildOs {
    /// Zephyr RTOS build (west + CMake + Ninja + Zephyr SDK).
    Zephyr,
    /// Yocto Linux image build (Linux-only; bitbake).
    Yocto,
    /// Baremetal build (CMake + a vendor toolchain).
    Baremetal,
}

impl BuildOs {
    fn parse(raw: &str) -> Option<BuildOs> {
        match raw {
            "zephyr" => Some(BuildOs::Zephyr),
            "yocto" => Some(BuildOs::Yocto),
            "baremetal" => Some(BuildOs::Baremetal),
            _ => None,
        }
    }
}

/// The set of build OSes a `board.yaml` declares (top-level `os:` plus each
/// core's explicit `os:`). When nothing is declared, returns all three — the
/// SoM-default OS lives in SDK metadata we don't resolve here.
pub fn board_os_set(board: &BoardModel) -> Vec<BuildOs> {
    let mut set: BTreeSet<BuildOs> = BTreeSet::new();
    if let Some(os) = board.os.as_deref().and_then(BuildOs::parse) {
        set.insert(os);
    }
    if let Some(cores) = &board.cores {
        for core in cores.values() {
            if let Some(os) = core.os.as_deref().and_then(BuildOs::parse) {
                set.insert(os);
            }
        }
    }
    if set.is_empty() {
        return vec![BuildOs::Zephyr, BuildOs::Yocto, BuildOs::Baremetal];
    }
    set.into_iter().collect()
}

/// Host build-tool presence (probed by the caller; kept IO-free here).
#[derive(Debug, Clone, Copy)]
pub struct BuildToolProbe {
    /// `west` is on PATH (Zephyr build driver).
    pub west: bool,
    /// `cmake` is on PATH (Zephyr/baremetal build generator).
    pub cmake: bool,
    /// `ninja` is on PATH (Zephyr build backend).
    pub ninja: bool,
    /// `bitbake` is on PATH (Yocto build driver).
    pub bitbake: bool,
    /// Zephyr SDK toolchain detected (via env / install dir, not PATH).
    pub zephyr_sdk: bool,
    /// `bmaptool` — the preferred Yocto `.wic` flasher (sparse-aware).
    pub bmaptool: bool,
    /// `dd` — the Yocto `.wic` flash fallback when `bmaptool` is absent.
    pub dd: bool,
    /// Host is Linux (gates Yocto builds, which are Linux-only).
    pub is_linux: bool,
}

/// The build-readiness preflight result: declared OS set, per-tool checks, a
/// pass/warn/fail summary, and deduped remediation steps. Serializes camelCase.
#[derive(Debug, Clone, Serialize)]
pub struct BuildReadinessReport {
    /// Report envelope schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    pub schema_version: String,
    /// Timestamp the report was generated (supplied by the caller).
    #[serde(rename = "generatedAt")]
    pub generated_at: String,
    /// The build OSes this report's checks cover.
    #[serde(rename = "osSet")]
    pub os_set: Vec<BuildOs>,
    /// Aggregate pass/warn/fail counts over `checks`.
    pub summary: DoctorSummary,
    /// Per-tool readiness checks.
    pub checks: Vec<DoctorCheck>,
    /// Deduped fix hints for every non-passing check.
    #[serde(rename = "nextSteps")]
    pub next_steps: Vec<String>,
    /// The PATH-binary checks above that came back absent, in the form a
    /// consumer can put behind a button.
    ///
    /// Data only — there is deliberately no second `hostPrerequisites` CHECK in
    /// this mode. `--build` already probes `west`/`cmake`/`ninja`/`bitbake`
    /// through [`BuildToolProbe`] and reports each as its own check; mirroring
    /// plain `doctor`'s aggregate check here would report the same tool twice
    /// under two names. What `--build` could NOT give a consumer was the
    /// COMMAND — `alp-sdk-vscode` calls only `tan doctor --build`
    /// (`src/toolchain.ts:219`, `:248`), so with no runnable string in the
    /// payload its `runToolchainFix` had nothing to put behind the Fix button
    /// that ADR 0021 Lane 1 P0a asks for, and a missing `ninja` stayed
    /// `failed to launch (exit code: 1)`.
    ///
    /// Populated from the PATH-binary probes ONLY, i.e. exactly the checks
    /// [`push_tool`] emits. `zephyrSdk` is detected from an env var and
    /// remediated by a docs URL, `bmaptool` is a two-tool advisory with a
    /// working `dd` fallback, and `yoctoHost`/`vendorToolchain` name no tool at
    /// all — none has a single `{tool, command}` pair that could carry it.
    /// Deriving the list inside `push_tool` also means it inherits that
    /// function's OS gating and dedup for free: a Zephyr-only project never
    /// pushes the `bitbake` check, so it can never be told to install bitbake.
    ///
    /// Same tri-state as the `doctor` and `bootstrap` envelopes'
    /// `missingPrerequisites` (see
    /// [`reported_missing`](crate::bootstrap::reported_missing)): `null` when
    /// nothing is missing, a populated array otherwise, **never `[]`**.
    #[serde(rename = "missingPrerequisites")]
    pub missing_prerequisites: Option<Vec<MissingPrerequisite>>,
}

/// Assemble the build-readiness report for an OS set + probed host tools.
///
/// `install` is the manifest's `prerequisites.install` map already resolved for
/// this host
/// ([`InstallCommands::for_host`](crate::bootstrap::InstallCommands::for_host)) —
/// the single source of every `missingPrerequisites[].command` this report
/// carries. Resolved by the caller, which is the side that knows the host, so
/// this function cannot look a tool up in the wrong OS's table.
pub fn build_readiness_report(
    generated_at: String,
    os_set: Vec<BuildOs>,
    probe: &BuildToolProbe,
    install: &BTreeMap<String, String>,
) -> BuildReadinessReport {
    let mut checks: Vec<DoctorCheck> = Vec::new();
    let mut seen: BTreeSet<&'static str> = BTreeSet::new();
    let mut missing: Vec<MissingPrerequisite> = Vec::new();

    if os_set.contains(&BuildOs::Zephyr) {
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            install,
            "west",
            probe.west,
            "Zephyr",
            "Install west via `tan bootstrap`.",
        );
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            install,
            "cmake",
            probe.cmake,
            "Zephyr/baremetal",
            "Install CMake (>=3.20).",
        );
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            install,
            "ninja",
            probe.ninja,
            "Zephyr",
            "Install Ninja.",
        );
        // Zephyr SDK is detected (env / install dir), not a PATH binary.
        checks.push(DoctorCheck {
            name: "zephyrSdk".to_string(),
            status: if probe.zephyr_sdk {
                DoctorStatus::Pass
            } else {
                DoctorStatus::Warn
            },
            detail: if probe.zephyr_sdk {
                "Zephyr SDK toolchain detected.".to_string()
            } else {
                "Zephyr SDK toolchain not detected (ZEPHYR_SDK_INSTALL_DIR unset).".to_string()
            },
            fix: if probe.zephyr_sdk {
                None
            } else {
                Some(
                    "Install the Zephyr SDK: https://docs.zephyrproject.org/latest/develop/toolchains/zephyr_sdk.html"
                        .to_string(),
                )
            },
        });
    }

    if os_set.contains(&BuildOs::Yocto) {
        if probe.is_linux {
            push_tool(
                &mut checks,
                &mut seen,
                &mut missing,
                install,
                "bitbake",
                probe.bitbake,
                "Yocto",
                "Install the Yocto host packages (see docs/getting-started.md).",
            );
            // Flash prerequisite: `tan flash` writes the Yocto `.wic` to SD/eMMC
            // via `bmaptool` (sparse-aware, preferred) and falls back to `dd`.
            // Warn early so the gap shows at doctor time, not mid-flash.
            let (status, detail, fix) = if probe.bmaptool {
                (
                    DoctorStatus::Pass,
                    "bmaptool is available — fast sparse Yocto .wic flashing.".to_string(),
                    None,
                )
            } else if probe.dd {
                (
                    DoctorStatus::Warn,
                    "bmaptool not found; Yocto .wic flash falls back to dd (slower)."
                        .to_string(),
                    Some(
                        "Install bmaptool for sparse .wic flashing (e.g. `apt install bmap-tools`)."
                            .to_string(),
                    ),
                )
            } else {
                (
                    DoctorStatus::Warn,
                    "neither bmaptool nor dd on PATH — Yocto .wic flash (`tan flash`) will fail."
                        .to_string(),
                    Some(
                        "Install bmaptool (`apt install bmap-tools`) or dd (coreutils)."
                            .to_string(),
                    ),
                )
            };
            checks.push(DoctorCheck {
                name: "bmaptool".to_string(),
                status,
                detail,
                fix,
            });
        } else {
            checks.push(DoctorCheck {
                name: "yoctoHost".to_string(),
                status: DoctorStatus::Warn,
                detail: YOCTO_HOST_DETAIL.to_string(),
                fix: Some("Run Yocto builds on Linux (WSL2 / Docker).".to_string()),
            });
        }
    }

    if os_set.contains(&BuildOs::Baremetal) {
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            install,
            "cmake",
            probe.cmake,
            "baremetal",
            "Install CMake (>=3.20).",
        );
        checks.push(DoctorCheck {
            name: "vendorToolchain".to_string(),
            status: DoctorStatus::Warn,
            detail: "Baremetal needs a vendor toolchain (Alif/Renesas/NXP), per SoC family."
                .to_string(),
            fix: Some(
                "Install the vendor toolchain for your SoC (see docs/getting-started.md §8)."
                    .to_string(),
            ),
        });
    }

    let summary = DoctorSummary {
        pass: count(&checks, DoctorStatus::Pass),
        warn: count(&checks, DoctorStatus::Warn),
        fail: count(&checks, DoctorStatus::Fail),
    };
    let next_steps = unique_next_steps(&checks);

    BuildReadinessReport {
        schema_version: "1".to_string(),
        generated_at,
        os_set,
        summary,
        checks,
        next_steps,
        missing_prerequisites: reported_missing(missing),
    }
}

/// Append a PATH-binary readiness check, deduped by `name` (a tool needed by
/// more than one declared OS is reported once), and record an absent one in
/// `missing` for the report's `missingPrerequisites`.
///
/// The two land in the SAME function so the machine-readable list cannot drift
/// from the checks: it inherits this function's OS gating (its callers are
/// already inside the `os_set` branches, so a Zephyr-only project never reaches
/// the `bitbake` call) and its `seen` dedup, instead of re-deriving either.
///
/// `command` is `None` for any tool this host's `prerequisites.install` map lists
/// no one-liner for — `west` (installed into the venv by `tan bootstrap`, not by
/// a package manager) and `bitbake` (a whole host-package set) on every OS, and
/// everything at all on a host the manifest does not serve. A named tool with a
/// `null` command is still worth reporting: the consumer renders the name and
/// falls back to the check's `fix` prose. An INVENTED command would be worse
/// than `null`, because that field is rendered as a button.
///
/// There is no `is_windows` gate here any more. It existed because the commands
/// were a hardcoded `winget` table that no other host could run; they are the
/// manifest's per-OS fact now, so the map handed in IS the gate — a Linux host
/// resolves `install.linux` and gets `apt-get` lines, and no branch can hand it
/// `winget`.
///
/// KNOWN CEILING, on POSIX — now only for the compiled-in fallback. The map's
/// key set is schema-constrained to equal `prerequisites.<os>`, and alp-sdk#971
/// widened `prerequisites.posix` to `[git, cmake, python3, ninja]` with
/// `install.linux.ninja` / `install.macos.ninja`, so a live SDK checkout
/// resolves an actionable POSIX `ninja` here with no change in tan. What still
/// lags is tan's own vendored copy — `contract/fixtures/bootstrap/manifest.json`
/// and the [`fallback_facts`](crate::bootstrap::fallback_facts) constants pinned
/// byte-equal to it — which is a separate cross-repo re-vendor (fixture +
/// constants + `PINNED_SDK_TAG`), not part of this severity fix. Until it lands,
/// `ninja` resolves to `null` on POSIX only when the manifest could not be read
/// at all. `null` stays the safe degrade — an invented `apt-get install -y
/// ninja-build` behind a Fix button is worse — and it does NOT soften the
/// severity: see [`BUILD_BLOCKING`].
#[allow(clippy::too_many_arguments)]
fn push_tool(
    checks: &mut Vec<DoctorCheck>,
    seen: &mut BTreeSet<&'static str>,
    missing: &mut Vec<MissingPrerequisite>,
    install: &BTreeMap<String, String>,
    name: &'static str,
    present: bool,
    need: &str,
    fix: &str,
) {
    if !seen.insert(name) {
        return;
    }
    let command = install.get(name).cloned();
    if !present {
        missing.push(MissingPrerequisite {
            tool: name.to_string(),
            command: command.clone(),
        });
    }
    checks.push(DoctorCheck {
        name: name.to_string(),
        status: if present {
            DoctorStatus::Pass
        } else if BUILD_BLOCKING.contains(&name) {
            DoctorStatus::Fail
        } else {
            DoctorStatus::Warn
        },
        detail: if present {
            format!("{name} is available.")
        } else {
            format!("{name} not found on PATH — needed for {need} builds.")
        },
        fix: if present {
            None
        } else {
            // The manifest's one-liner rides in the PROSE too, not just in
            // `missingPrerequisites[].command`. Text mode renders `checks` and
            // `nextSteps` and nothing else (`doctor::format_build_text` ->
            // `style::render_report`), so a terminal user was reading "Install
            // Ninja." while the extension's Fix button got the runnable string
            // from the same report — the command half of #103, unfixed for
            // everyone not driving tan from VS Code. Appended rather than
            // substituted, because the prose carries constraints the command
            // does not (`cmake (>=3.20)`), and omitted entirely when the map has
            // no entry, which is the same "never invent one" rule as `command`.
            Some(match &command {
                Some(command) => format!("{fix} `{command}`"),
                None => fix.to_string(),
            })
        },
    });
}

/// Tools whose absence does not degrade a build but STOPS it, so [`push_tool`]
/// reports them absent as `Fail` rather than `Warn` (#103). `ninja` is the
/// generator CMake picks by default on every Zephyr host, so without it
/// `west build` dies outright — rating that `Warn` told the user their host was
/// ready and then failed the build anyway.
///
/// Widened past the `ninja` #103 names because `cmake` is capped by the very
/// same line and is at least as blocking (Zephyr AND baremetal). Deliberately
/// NOT the rest of the probe set, on two different grounds:
///
/// * `west` — build-blocking, but this check probes BARE PATH while the executor
///   resolves west from the workspace venv (`venv::west_program`), which is where
///   `tan bootstrap` puts it. A correctly bootstrapped host that builds fine
///   routinely has no `west` on PATH, so failing here would be a false negative
///   that exits `tan doctor --build` non-zero on a working machine. The
///   venv-aware verdict is the preflight's `westResolved` check, which the same
///   report already carries.
/// * `bitbake`, and the non-`push_tool` checks `zephyrSdk` / `bmaptool` /
///   `vendorToolchain` — optional or advisory by design; #103 is explicit that
///   their `Warn` is correct.
///
/// The set that survives is exactly the manifest's own `prerequisites`, so
/// `--build` now agrees with plain `doctor`, whose `hostPrerequisites` check has
/// always called a missing prerequisite `Fail`
/// ([`doctor_prerequisite_check`](crate::bootstrap::doctor_prerequisite_check)).
/// Two modes, one verdict on the same host fact.
///
/// Severity is deliberately INDEPENDENT of whether `install` carries a one-liner:
/// a `null` command means tan cannot offer a button, not that the build will
/// somehow succeed, and the check's `fix` prose still says what to do. Gating the
/// `Fail` on a resolvable command would instead make the severity vary by host
/// and silently downgrade itself the day a manifest key moved.
const BUILD_BLOCKING: [&str; 2] = ["cmake", "ninja"];

fn count(checks: &[DoctorCheck], status: DoctorStatus) -> u32 {
    checks.iter().filter(|c| c.status == status).count() as u32
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bootstrap::{HostOs, fallback_facts};
    use crate::validate::parse_board_model;

    /// One host's REAL install map — the fallback constants, which
    /// `bootstrap::manifest`'s
    /// `the_fallback_matches_the_real_manifest_field_for_field` pins byte-equal to
    /// the vendored `metadata/bootstrap.json`. Every command asserted below is
    /// therefore the producer's own string.
    fn install(host: HostOs) -> BTreeMap<String, String> {
        fallback_facts((3, 10)).install.for_host(host).clone()
    }

    fn probe_all_present() -> BuildToolProbe {
        BuildToolProbe {
            west: true,
            cmake: true,
            ninja: true,
            bitbake: true,
            zephyr_sdk: true,
            bmaptool: true,
            dd: true,
            is_linux: true,
        }
    }

    #[test]
    fn os_set_from_explicit_core_os() {
        let board = parse_board_model(
            "schemaVersion: 2\ncores:\n  m55_hp:\n    os: zephyr\n    app: ./src\n  a32:\n    os: yocto\n    app: ./a\n",
        )
        .unwrap();
        assert_eq!(board_os_set(&board), vec![BuildOs::Zephyr, BuildOs::Yocto]);
    }

    #[test]
    fn os_set_falls_back_to_all_when_undeclared() {
        let board =
            parse_board_model("schemaVersion: 2\ncores:\n  m55_hp:\n    app: ./src\n").unwrap();
        assert_eq!(
            board_os_set(&board),
            vec![BuildOs::Zephyr, BuildOs::Yocto, BuildOs::Baremetal]
        );
    }

    #[test]
    fn zephyr_checks_present_pass_clean() {
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert_eq!(report.summary.fail, 0);
        assert!(report.summary.warn == 0);
        assert!(
            report
                .checks
                .iter()
                .any(|c| c.name == "west" && c.status == DoctorStatus::Pass)
        );
        assert!(report.checks.iter().any(|c| c.name == "zephyrSdk"));
    }

    fn probe_none_present() -> BuildToolProbe {
        BuildToolProbe {
            west: false,
            cmake: false,
            ninja: false,
            bitbake: false,
            zephyr_sdk: false,
            bmaptool: false,
            dd: false,
            is_linux: true,
        }
    }

    #[test]
    fn missing_tools_report_with_next_steps() {
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        // west + zephyrSdk stay advisory; cmake + ninja block the build (#103).
        assert_eq!(report.summary.warn, 2);
        assert_eq!(report.summary.fail, 2);
        assert!(!report.next_steps.is_empty());
    }

    #[test]
    fn a_build_blocking_tool_fails_and_an_optional_one_warns() {
        // #103: `ninja` is CMake's default generator on every Zephyr host, so a
        // missing one does not degrade the build, it stops it -- reporting that
        // `Warn` let `tan doctor --build` exit 0 on a host that cannot build.
        // `cmake` is capped by the same line and is at least as blocking.
        //
        // `west` stays `Warn` on purpose: this check probes bare PATH, but the
        // executor resolves west from the workspace venv, so a bootstrapped host
        // that builds fine routinely fails this probe. `zephyrSdk` and
        // `vendorToolchain` are advisory by design.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr, BuildOs::Baremetal],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        let status = |name: &str| {
            report
                .checks
                .iter()
                .find(|c| c.name == name)
                .unwrap_or_else(|| panic!("no {name} check"))
                .status
        };
        assert_eq!(status("ninja"), DoctorStatus::Fail);
        assert_eq!(status("cmake"), DoctorStatus::Fail);
        assert_eq!(status("west"), DoctorStatus::Warn);
        assert_eq!(status("zephyrSdk"), DoctorStatus::Warn);
        assert_eq!(status("vendorToolchain"), DoctorStatus::Warn);

        // A Yocto host: `bitbake` is build-blocking for Yocto but #103 is
        // explicit its `Warn` is correct -- widening must not have swept it up.
        let yocto = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        assert!(
            yocto
                .checks
                .iter()
                .any(|c| c.name == "bitbake" && c.status == DoctorStatus::Warn)
        );
        assert_eq!(yocto.summary.fail, 0);

        // Present tools are still clean -- the severity change touches the
        // absent branch only.
        let clean = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert_eq!(clean.summary.fail, 0);
    }

    #[test]
    fn the_fix_prose_carries_the_manifest_command_when_there_is_one() {
        // The other half of #103. `missingPrerequisites[].command` has been
        // manifest-sourced since #95, but text mode renders only `checks` and
        // `nextSteps`, so a terminal user saw "Install CMake (>=3.20)." and never
        // the runnable string the extension's Fix button gets from the same
        // report. Appended, not substituted: `(>=3.20)` is a constraint the
        // command does not carry.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        let fix_for = |name: &str| {
            report
                .checks
                .iter()
                .find(|c| c.name == name)
                .and_then(|c| c.fix.clone())
                .unwrap_or_else(|| panic!("no {name} fix"))
        };
        assert_eq!(
            fix_for("cmake"),
            "Install CMake (>=3.20). `sudo apt-get install -y cmake`"
        );
        // No entry in the map -> the prose stays bare rather than inventing one,
        // the same rule `command` follows. `west` is pip-installed into the venv
        // by `tan bootstrap`, so no OS's map lists it.
        assert_eq!(fix_for("west"), "Install west via `tan bootstrap`.");
        // Every fix hint reaches the user's terminal through `nextSteps`.
        assert!(
            report
                .next_steps
                .iter()
                .any(|s| s.contains("sudo apt-get install -y cmake")),
            "{:?}",
            report.next_steps
        );
    }

    #[test]
    fn a_clean_host_reports_missing_prerequisites_as_null_never_empty() {
        // `[]` would mean "checked, nothing missing" in a vocabulary where
        // `null` already means exactly that -- one fact, one spelling, shared
        // with the `doctor` and `bootstrap` envelopes.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr, BuildOs::Yocto, BuildOs::Baremetal],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert_eq!(report.missing_prerequisites, None);
    }

    #[test]
    fn a_missing_tool_carries_its_winget_command_on_windows() {
        // The whole point of the field: `alp-sdk-vscode` calls only
        // `tan doctor --build`, and its Fix button needs something RUNNABLE.
        let probe = BuildToolProbe {
            is_linux: false,
            ..probe_none_present()
        };
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe,
            &install(HostOs::Windows),
        );
        assert_eq!(
            report.missing_prerequisites,
            Some(vec![
                // `west` is pip-installed into the venv by `tan bootstrap`, so
                // the manifest lists no one-liner for it -- named with a `null`
                // command rather than dropped or invented.
                MissingPrerequisite {
                    tool: "west".to_string(),
                    command: None,
                },
                MissingPrerequisite {
                    tool: "cmake".to_string(),
                    command: Some("winget install -e --id Kitware.CMake".to_string()),
                },
                MissingPrerequisite {
                    tool: "ninja".to_string(),
                    command: Some("winget install -e --id Ninja-build.Ninja".to_string()),
                },
            ])
        );
    }

    #[test]
    fn a_posix_host_gets_its_own_package_manager_and_never_winget() {
        // Issue #90's "also still true": every POSIX entry reported
        // `command: null` because the commands were a hardcoded `winget` table no
        // POSIX host could run, and handing a Linux or macOS user `winget install
        // …` behind a button would be worse than that `null`.
        // `prerequisites.install.linux`/`.macos` supply real ones now -- per tool,
        // so `ninja` (absent from `prerequisites.posix`, hence from both POSIX
        // install maps) stays `null` while `cmake` becomes runnable, and the two
        // hosts get DIFFERENT commands for it.
        //
        // The `ninja` expectation below is CROSS-REPO DATA, not a tan rule:
        // alp-sdk#971 has since widened `prerequisites.posix` to include `ninja`
        // and added `install.linux.ninja` / `install.macos.ninja`. This asserts
        // against `fallback_facts`, i.e. tan's VENDORED copy of that manifest,
        // which is still on the older pin -- so it is green here and will go red
        // the moment the fixture + constants + `PINNED_SDK_TAG` are re-vendored.
        // That red is the gate doing its job: update the expectation to the two
        // new commands then, do not weaken or delete the assertion.
        for (host, cmake_command) in [
            (HostOs::Linux, Some("sudo apt-get install -y cmake")),
            (HostOs::MacOs, Some("brew install cmake")),
            // A POSIX host the manifest does not serve keeps the old all-`null`
            // behaviour rather than being handed the nearest OS's commands.
            (HostOs::Other, None),
        ] {
            let report = build_readiness_report(
                "t".to_string(),
                vec![BuildOs::Zephyr],
                &probe_none_present(),
                &install(host),
            );
            let missing = report.missing_prerequisites.expect("tools are missing");
            let tools: Vec<&str> = missing.iter().map(|m| m.tool.as_str()).collect();
            assert_eq!(tools, ["west", "cmake", "ninja"], "{host:?}");
            let command_for = |tool: &str| {
                missing
                    .iter()
                    .find(|m| m.tool == tool)
                    .and_then(|m| m.command.as_deref())
            };
            assert_eq!(command_for("cmake"), cmake_command, "{host:?}");
            assert_eq!(command_for("west"), None, "{host:?}");
            assert_eq!(command_for("ninja"), None, "{host:?}");
        }
    }

    #[test]
    fn missing_prerequisites_covers_only_path_tools_and_respects_the_os_gate() {
        // Two rules in one: a Zephyr-only project must never be told to install
        // `bitbake` (the check is not even emitted), and the non-PATH checks --
        // `zephyrSdk` (env-var detection, docs-URL fix), `bmaptool` (two tools,
        // one advisory, `dd` fallback), `vendorToolchain` (no tool name at all)
        // -- must stay out, since no `{tool, command}` pair can carry them.
        let zephyr_only = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        let tools: Vec<String> = zephyr_only
            .missing_prerequisites
            .expect("tools are missing")
            .into_iter()
            .map(|m| m.tool)
            .collect();
        assert_eq!(tools, ["west", "cmake", "ninja"]);

        // Yocto declared AND a Linux host -> `bitbake` is checked, so it is
        // reportable; no OS's install map lists it (it is a whole host-package
        // set), so `command` is `null` even against the richest map there is.
        let yocto = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_none_present(),
            &install(HostOs::Windows),
        );
        assert_eq!(
            yocto.missing_prerequisites,
            Some(vec![MissingPrerequisite {
                tool: "bitbake".to_string(),
                command: None,
            }])
        );

        // Yocto declared on a NON-Linux host -> the `bitbake` check is replaced
        // by `yoctoHost`, and nothing is reported as installable.
        let non_linux = BuildToolProbe {
            is_linux: false,
            ..probe_none_present()
        };
        let off_host = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &non_linux,
            &install(HostOs::Windows),
        );
        assert_eq!(off_host.missing_prerequisites, None);
    }

    #[test]
    fn a_tool_needed_by_two_declared_oses_is_reported_once() {
        // Same dedup the checks get -- `missing` is filled inside `push_tool`,
        // after the `seen` guard, precisely so the two cannot drift.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr, BuildOs::Baremetal],
            &probe_none_present(),
            &install(HostOs::Linux),
        );
        let tools: Vec<String> = report
            .missing_prerequisites
            .expect("tools are missing")
            .into_iter()
            .map(|m| m.tool)
            .collect();
        assert_eq!(tools.iter().filter(|t| *t == "cmake").count(), 1);
    }

    #[test]
    fn the_build_report_serializes_the_field_as_an_explicit_null() {
        // No `skip_serializing_if`, matching `doctor`'s and `bootstrap`'s: the
        // key is in every captured envelope, so a consumer can see it exists
        // without reaching for a schema.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        let json = serde_json::to_string(&report).unwrap();
        assert!(json.contains("\"missingPrerequisites\":null"), "{json}");
    }

    #[test]
    fn cmake_not_duplicated_across_zephyr_and_baremetal() {
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr, BuildOs::Baremetal],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert_eq!(
            report.checks.iter().filter(|c| c.name == "cmake").count(),
            1
        );
    }

    #[test]
    fn yocto_on_non_linux_warns() {
        let probe = BuildToolProbe {
            is_linux: false,
            ..probe_all_present()
        };
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe,
            &install(HostOs::Linux),
        );
        assert!(report.checks.iter().any(|c| c.name == "yoctoHost"));
    }

    #[test]
    fn yocto_flash_checks_bmaptool() {
        // bmaptool present → a passing flash-prereq check.
        let pass = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert!(
            pass.checks
                .iter()
                .any(|c| c.name == "bmaptool" && c.status == DoctorStatus::Pass)
        );

        // Neither bmaptool nor dd → warn + a next step (and no bmaptool check for
        // a Zephyr-only project).
        let probe = BuildToolProbe {
            bmaptool: false,
            dd: false,
            ..probe_all_present()
        };
        let warn = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe,
            &install(HostOs::Linux),
        );
        assert!(
            warn.checks
                .iter()
                .any(|c| c.name == "bmaptool" && c.status == DoctorStatus::Warn)
        );
        let zephyr_only = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_all_present(),
            &install(HostOs::Linux),
        );
        assert!(!zephyr_only.checks.iter().any(|c| c.name == "bmaptool"));
    }
}
