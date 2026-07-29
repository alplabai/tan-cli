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

/// The `zephyrSdk` check: is the Zephyr SDK toolchain actually installed HERE
/// (`ZEPHYR_SDK_INSTALL_DIR` / a scanned install dir — [`crate::toolchain`] on
/// the `tan-cli` side), as opposed to `zephyrSdkAvailableForHost`'s "COULD one
/// be provisioned on this machine at all". Pulled out to a function (tan-cli#160)
/// so plain `tan doctor` can carry the SAME check `--build` does: nothing
/// before `tan build` itself ever said the word "toolchain" on the alp-sdk#855
/// fresh-host run, and the two-name confusion between this check and
/// `zephyrSdkAvailableForHost` is exactly what let a `[+]` sit beside a problem
/// that cost 73 of that run's ~80 minutes.
///
/// **`Fail`, not `Warn`** (#159), and the ONE Zephyr check with no host
/// exemption. The argument that keeps `dtc`/`gperf` at `Warn` is that the
/// Zephyr SDK's native-Windows hosttools bundle ships neither (alp-sdk#967) —
/// i.e. the SDK is what supplies them. So an ABSENT SDK is not the same class
/// of finding at all: there is no host, Windows included, on which a Zephyr
/// build succeeds without a toolchain.
///
/// Measured on the fresh-host run in alp-sdk#855: this reported `[!]`, the
/// report said `10 passed · 7 warnings · 0 failed`, `tan doctor --build`
/// exited 0, and the very next command died with `Could not find a package
/// configuration file provided by "Zephyr-sdk"`. A readiness command that
/// cannot fail hands a green light into a guaranteed failure.
///
/// The `fix` names the EXACT command the alp-sdk#855 reporter used to get
/// past this (`west sdk install --version 1.0.1 -t arm-zephyr-eabi`, the same
/// one this repo's own "Live state" notes record as e2e-verified), not merely
/// a docs URL — tan does not run it itself (`west sdk install` is a real
/// download+extract, `west`'s job once a workspace exists, not a doctor-time
/// side effect), only names it precisely enough to act on.
/// The exact `west sdk install` invocation the `zephyrSdk` check names — the
/// ONE place it is assembled.
///
/// It used to be formatted three times over
/// [`crate::ZEPHYR_SDK_INSTALL_VERSION`] (in `detail`, in `fix`, twice inside
/// `fix` alone), and #210 adds a fourth reader: `missingPrerequisites[].command`,
/// which the extension renders as a Fix BUTTON. Four literals of one command,
/// one of them runnable with a click, is exactly the drift that ends with a
/// button running a different version than the prose beside it recommends.
pub fn zephyr_sdk_install_command() -> String {
    format!(
        "west sdk install --version {} -t arm-zephyr-eabi",
        crate::ZEPHYR_SDK_INSTALL_VERSION,
    )
}

/// The 7-Zip install one-liner for native Windows (#204).
///
/// COMPILED IN rather than read from the manifest's `prerequisites.install`,
/// because 7-Zip is not in any `prerequisites.<os>` list — it is a prerequisite
/// of `west sdk install`, which bootstrap deliberately does not run, so it never
/// gated bootstrap and was never added. That makes this the one install command
/// in this file the SDK does not own, and it is written down here rather than
/// invented at the call site so the follow-up that moves it into
/// `metadata/bootstrap.json` (alp-sdk#1036) has a single thing to delete.
///
/// Verified resolvable before being written down (`winget show 7zip.7zip` ->
/// `Found 7-Zip [7zip.7zip]`, publisher Igor Pavlov), and shaped like the
/// manifest's own Windows entries (`winget install -e --id Kitware.CMake`). The
/// rule this file states everywhere else still holds — an INVENTED command
/// behind a Fix button is worse than `null` — so this one is checked, not
/// guessed.
pub const SEVEN_ZIP_INSTALL_COMMAND: &str = "winget install -e --id 7zip.7zip";

/// The PATH names west's `.7z` extraction will accept. west delegates to
/// `patoolib`, which shells out to any ONE of these and has no pure-Python
/// fallback — so finding any single one of them is enough, and probing only
/// `7z` would report a false negative on a host that has `7zz` or `unar`.
pub const SEVEN_ZIP_PROGRAMS: [&str; 6] = ["7z", "7za", "7zr", "7zz", "7zzs", "unar"];

pub fn zephyr_sdk_toolchain_check(detected: bool) -> DoctorCheck {
    DoctorCheck {
        name: "zephyrSdk".to_string(),
        status: if detected {
            DoctorStatus::Pass
        } else {
            DoctorStatus::Fail
        },
        detail: if detected {
            "Zephyr SDK toolchain detected.".to_string()
        } else {
            // The pinned command belongs here too, not only in `fix` — `fix`
            // renders only under `--verbose` (`style.rs`'s "Next steps"
            // block), so plain `tan doctor` used to show this Fail with no
            // remedy at all, exactly the gap alp-sdk#855's fresh-host
            // reporter hit. Every sibling doctor check (`sdk`/`workspace` in
            // `preflight.rs`, `hostPrerequisites` in `bootstrap/
            // prerequisites.rs`) already inlines its one-liner in `detail`;
            // this one is the check the issue was filed about, so it cannot
            // be the exception.
            format!(
                "Zephyr SDK toolchain not detected (ZEPHYR_SDK_INSTALL_DIR unset) — from an \
                 initialised west workspace, run `{}`.",
                zephyr_sdk_install_command(),
            )
        },
        fix: if detected {
            None
        } else {
            Some(format!(
                "Install the Zephyr SDK toolchain (arm-zephyr-eabi, version {}): from an \
                 initialised west workspace, run `{}`. Details: \
                 https://docs.zephyrproject.org/latest/develop/toolchains/zephyr_sdk.html",
                crate::ZEPHYR_SDK_INSTALL_VERSION,
                zephyr_sdk_install_command(),
            ))
        },
    }
}

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

/// Host build-tool presence (probed by the caller; kept IO-free here). Each
/// `_version` field is whatever `tan-cli` itself resolved for that SAME probe
/// (tan-cli#123) — never a second, independently-PATH-probed answer a consumer
/// would have to reconcile with the presence bool beside it.
#[derive(Debug, Clone)]
pub struct BuildToolProbe {
    /// `west` is on PATH (Zephyr build driver).
    pub west: bool,
    /// `west --version`, resolved off bare PATH — the SAME lookup [`west`]
    /// reflects. Deliberately NOT the workspace-venv west's version (that is
    /// `westResolved`'s, attached separately by `tan-cli::commands::doctor`
    /// from `crate::venv::west_program`'s OWN resolution) — conflating the two
    /// is the exact PATH-vs-venv disagreement tan-cli#123 was filed over.
    ///
    /// [`west`]: BuildToolProbe::west
    pub west_version: Option<String>,
    /// `cmake` is on PATH (Zephyr/baremetal build generator).
    pub cmake: bool,
    /// `cmake --version`, matching [`cmake`](BuildToolProbe::cmake)'s probe.
    pub cmake_version: Option<String>,
    /// `ninja` is on PATH (Zephyr build backend).
    pub ninja: bool,
    /// `ninja --version`, matching [`ninja`](BuildToolProbe::ninja)'s probe.
    pub ninja_version: Option<String>,
    /// `bitbake` is on PATH (Yocto build driver).
    pub bitbake: bool,
    /// `bitbake --version`, matching [`bitbake`](BuildToolProbe::bitbake)'s probe.
    pub bitbake_version: Option<String>,
    /// Zephyr SDK toolchain detected (via env / install dir, not PATH).
    pub zephyr_sdk: bool,
    /// `bmaptool` — the preferred Yocto `.wic` flasher (sparse-aware).
    pub bmaptool: bool,
    /// `dd` — the Yocto `.wic` flash fallback when `bmaptool` is absent.
    pub dd: bool,
    /// Host is Linux (gates Yocto builds, which are Linux-only).
    pub is_linux: bool,
    /// Host is native Windows (gates the `sevenZip` check, which is a
    /// native-Windows-only prerequisite). NOT `!is_linux` — macOS is neither,
    /// and `west sdk install` needs no external extractor there.
    pub is_windows: bool,
    /// Any one of [`SEVEN_ZIP_PROGRAMS`] is on PATH — west's `.7z` extractor
    /// for `west sdk install` on native Windows (#204).
    pub seven_zip: bool,
    /// `git` is on PATH (tan-cli#120) — every backend's build-plan emission
    /// runs `alp_project.py` against a git checkout, so this is checked
    /// unconditionally rather than gated on the declared `os_set`.
    pub git: bool,
    /// `git --version`, matching [`git`](BuildToolProbe::git)'s probe.
    pub git_version: Option<String>,
    /// The Python interpreter tan would run `alp_project.py` with, as
    /// `(major, minor)` — `None` for BOTH "not on PATH" and "on PATH but did
    /// not run" (tan-cli#120; e.g. the Windows Store `python.exe` alias). A
    /// bare presence bool would lose the version-floor distinction the
    /// `bootstrap` prerequisite gate already makes (`python-not-runnable` vs
    /// `python-too-old`) — see [`push_python`].
    pub python_version: Option<(u32, u32)>,
    /// `dtc` is on PATH — Zephyr's devicetree compiler (tan-cli#120).
    pub dtc: bool,
    /// `dtc --version`, matching [`dtc`](BuildToolProbe::dtc)'s probe.
    pub dtc_version: Option<String>,
    /// `gperf` is on PATH — Zephyr's syscall perfect-hash generator (tan-cli#120).
    pub gperf: bool,
    /// `gperf --version`, matching [`gperf`](BuildToolProbe::gperf)'s probe.
    pub gperf_version: Option<String>,
}

/// The build-readiness preflight result: declared OS set, per-tool checks, a
/// pass/warn/fail summary, and deduped remediation steps. Serializes camelCase
/// via [`BuildReadinessReportWire`] (`#[serde(into = ...)]`) — see that type
/// and [`check_versions`](BuildReadinessReport::check_versions) for why.
#[derive(Debug, Clone, Serialize)]
#[serde(into = "BuildReadinessReportWire")]
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
    /// Per-check RESOLVED version (tan-cli#123), keyed by [`DoctorCheck::name`].
    /// Only a check whose probe actually resolved one is present here — `west`
    /// and `westResolved` can (and, per #123's motivating bug, MUST) carry
    /// DIFFERENT entries under this map even though they share no field on
    /// [`DoctorCheck`] itself, and `zephyrSdk`/`bmaptool`/`vendorToolchain`/
    /// `yoctoHost` are never present at all — none names a single tool with a
    /// single version.
    ///
    /// Not itself a wire field: [`BuildReadinessReportWire`]'s `From` impl
    /// folds each entry into its check's `version` at serialization time.
    /// `DoctorCheck` cannot carry the field directly — it is built by struct
    /// literal in roughly twenty places across the crate, including
    /// `tan_core::preflight` (the source of the `westResolved` check itself),
    /// which is out of scope for this change — so widening its field list
    /// would break every one of those call sites for a fact only THIS report
    /// carries.
    pub check_versions: BTreeMap<String, String>,
}

/// [`DoctorCheck`] plus its resolved version (tan-cli#123): the wire shape of
/// one `data.checks[]` entry, `{name, status, detail, fix, version}`. Exists
/// only so [`BuildReadinessReportWire`] can merge
/// [`BuildReadinessReport::check_versions`] into `checks` without `DoctorCheck`
/// itself gaining the field — see that field's doc comment for why not.
#[derive(Debug, Clone, Serialize)]
struct CheckWithVersion {
    #[serde(flatten)]
    check: DoctorCheck,
    #[serde(skip_serializing_if = "Option::is_none")]
    version: Option<String>,
}

/// Wire form of [`BuildReadinessReport`]: identical except `checks` is
/// [`CheckWithVersion`], carrying each entry's resolved version inline.
/// Produced only by the `#[serde(into = ...)]` conversion below; nothing else
/// constructs one.
#[derive(Debug, Clone, Serialize)]
struct BuildReadinessReportWire {
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    #[serde(rename = "generatedAt")]
    generated_at: String,
    #[serde(rename = "osSet")]
    os_set: Vec<BuildOs>,
    summary: DoctorSummary,
    checks: Vec<CheckWithVersion>,
    #[serde(rename = "nextSteps")]
    next_steps: Vec<String>,
    #[serde(rename = "missingPrerequisites")]
    missing_prerequisites: Option<Vec<MissingPrerequisite>>,
}

impl From<BuildReadinessReport> for BuildReadinessReportWire {
    fn from(report: BuildReadinessReport) -> Self {
        let mut check_versions = report.check_versions;
        let checks = report
            .checks
            .into_iter()
            .map(|check| {
                let version = check_versions.remove(&check.name);
                CheckWithVersion { check, version }
            })
            .collect();
        Self {
            schema_version: report.schema_version,
            generated_at: report.generated_at,
            os_set: report.os_set,
            summary: report.summary,
            checks,
            next_steps: report.next_steps,
            missing_prerequisites: report.missing_prerequisites,
        }
    }
}

/// Assemble the build-readiness report for an OS set + probed host tools.
///
/// `install` is the manifest's `prerequisites.install` map already resolved for
/// this host
/// ([`InstallCommands::for_host`](crate::bootstrap::InstallCommands::for_host)) —
/// the single source of every `missingPrerequisites[].command` this report
/// carries. Resolved by the caller, which is the side that knows the host, so
/// this function cannot look a tool up in the wrong OS's table.
///
/// `python_min_version` is the manifest's `prerequisites.pythonMinVersion`
/// (`BootstrapFacts::python_min_version`), the SAME floor `tan bootstrap`
/// enforces — not a second, tan-compiled-in one that could drift from it.
pub fn build_readiness_report(
    generated_at: String,
    os_set: Vec<BuildOs>,
    probe: &BuildToolProbe,
    install: &BTreeMap<String, String>,
    python_min_version: (u32, u32),
) -> BuildReadinessReport {
    let mut checks: Vec<DoctorCheck> = Vec::new();
    let mut seen: BTreeSet<&'static str> = BTreeSet::new();
    let mut missing: Vec<MissingPrerequisite> = Vec::new();
    let mut check_versions: BTreeMap<String, String> = BTreeMap::new();

    // Host-universal, unconditional on `os_set` (tan-cli#120): EVERY backend's
    // build-plan emission runs `alp_project.py` out of a git checkout with
    // this Python, not just Zephyr's `west update`/`west build` — so, unlike
    // everything below, these two are not gated on a declared OS. Pushed
    // first so they read as fundamentals ahead of any one target's toolchain.
    push_tool(
        &mut checks,
        &mut seen,
        &mut missing,
        &mut check_versions,
        install,
        "git",
        probe.git,
        probe.git_version.as_deref(),
        "all",
        "Install git.",
    );
    push_python(
        &mut checks,
        &mut seen,
        &mut missing,
        &mut check_versions,
        install,
        probe.python_version,
        python_min_version,
    );

    if os_set.contains(&BuildOs::Zephyr) {
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            &mut check_versions,
            install,
            "west",
            probe.west,
            probe.west_version.as_deref(),
            "Zephyr",
            "Install west via `tan bootstrap`.",
        );
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            &mut check_versions,
            install,
            "cmake",
            probe.cmake,
            probe.cmake_version.as_deref(),
            "Zephyr/baremetal",
            "Install CMake (>=3.20).",
        );
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            &mut check_versions,
            install,
            "ninja",
            probe.ninja,
            probe.ninja_version.as_deref(),
            "Zephyr",
            "Install Ninja.",
        );
        // dtc/gperf (tan-cli#120): Zephyr-build prerequisites ONLY -- gated
        // right here so neither ever appears on a Yocto- or baremetal-only
        // report -- and WARN, not Fail, on absence: the retired `alp doctor`'s
        // own `_check_dtc`/`_check_gperf` were warn-only
        // (`contract/fixtures/bootstrap/manifest.json`'s
        // `manualInstallHints.windows.note` element 3 records the same verdict
        // this port preserves), and a native-Windows Zephyr SDK install ships
        // neither tool at all (alp-sdk#967) -- a hard `Fail` here would
        // contradict an environment tan's own bootstrap docs already call
        // supported. See [`BUILD_BLOCKING`] for the full severity argument.
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            &mut check_versions,
            install,
            "dtc",
            probe.dtc,
            probe.dtc_version.as_deref(),
            "Zephyr",
            "Install the devicetree compiler (dtc).",
        );
        push_tool(
            &mut checks,
            &mut seen,
            &mut missing,
            &mut check_versions,
            install,
            "gperf",
            probe.gperf,
            probe.gperf_version.as_deref(),
            "Zephyr",
            "Install gperf.",
        );
        // Zephyr SDK is detected (env / install dir), not a PATH binary. See
        // [`zephyr_sdk_toolchain_check`] for the FAIL-not-warn argument and the
        // alp-sdk#855 measurement behind it.
        //
        // It cannot go through `push_tool` -- that function's whole shape is a
        // PATH probe keyed into the manifest's `prerequisites.install` map, and
        // `zephyrSdk` is neither. But `push_tool` was ALSO the only writer of
        // `missing`, so bypassing it silently cost this check its
        // `missingPrerequisites` row (#203, #210): the extension reads that list
        // to decide a check is actionable, so the one build-blocking `Fail` a
        // first-install customer hits was the only row in the table with no Fix
        // button. Recording it here keeps "absent from the list" meaning "tan did
        // not consider this" rather than "structurally invisible".
        //
        // The command is real and runnable, not `null`: `west sdk install` is
        // what alp-sdk#855's reporter used to get past this. tan still does not
        // RUN it (a download+extract is west's job once a workspace exists, not a
        // read-only doctor's side effect) -- it names it precisely enough that
        // the consumer can.
        if !probe.zephyr_sdk {
            missing.push(MissingPrerequisite {
                tool: "zephyrSdk".to_string(),
                command: Some(zephyr_sdk_install_command()),
            });
        }
        checks.push(zephyr_sdk_toolchain_check(probe.zephyr_sdk));

        // 7-Zip (#204): a hard prerequisite of the `west sdk install` the check
        // directly above tells the customer to run, on native Windows only --
        // west delegates `.7z` extraction to `patoolib`, which shells out to an
        // external extractor and has no pure-Python fallback. Without it that
        // command dies inside patoolib with an error naming no Alp surface and
        // no mention of 7-Zip, and until now the fact existed in exactly one
        // place: a prose line in `tan bootstrap`'s TEXT output
        // (`manualInstallHints.windows.note[1]`), reaching no JSON consumer.
        //
        // Gated on `!probe.zephyr_sdk` deliberately. 7-Zip matters only for
        // INSTALLING the toolchain; once the SDK is present the extractor is
        // irrelevant, and a permanent warn row for a tool nothing will use again
        // is noise on every subsequent run. So it appears exactly when it is
        // actionable -- beside the `zephyrSdk` Fail it unblocks -- and vanishes
        // with it.
        //
        // `Warn`, not `Fail`: a host that already has the SDK is not reached at
        // all, and among hosts that do not, this blocks the REMEDY rather than
        // the build. `zephyrSdk` is the `Fail` that stops things.
        if probe.is_windows && !probe.zephyr_sdk {
            if !probe.seven_zip {
                missing.push(MissingPrerequisite {
                    tool: "sevenZip".to_string(),
                    command: Some(SEVEN_ZIP_INSTALL_COMMAND.to_string()),
                });
            }
            checks.push(DoctorCheck {
                name: "sevenZip".to_string(),
                status: if probe.seven_zip {
                    DoctorStatus::Pass
                } else {
                    DoctorStatus::Warn
                },
                detail: if probe.seven_zip {
                    "7-Zip is available — `west sdk install` can extract the toolchain.".to_string()
                } else {
                    format!(
                        "No 7-Zip on PATH (looked for {}) — `west sdk install` extracts the \
                         toolchain with patoolib, which shells out to one of these and has no \
                         pure-Python fallback, so it will fail on native Windows. Install it \
                         with `{SEVEN_ZIP_INSTALL_COMMAND}`.",
                        SEVEN_ZIP_PROGRAMS.join(", "),
                    )
                },
                fix: if probe.seven_zip {
                    None
                } else {
                    Some(format!(
                        "Install 7-Zip before running `west sdk install`: \
                         `{SEVEN_ZIP_INSTALL_COMMAND}`."
                    ))
                },
            });
        }
    }

    if os_set.contains(&BuildOs::Yocto) {
        if probe.is_linux {
            push_tool(
                &mut checks,
                &mut seen,
                &mut missing,
                &mut check_versions,
                install,
                "bitbake",
                probe.bitbake,
                probe.bitbake_version.as_deref(),
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
            &mut check_versions,
            install,
            "cmake",
            probe.cmake,
            probe.cmake_version.as_deref(),
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
        check_versions,
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
///
/// `version` (tan-cli#123) is recorded into `check_versions` under `name` when
/// `Some`, independent of `present` — a tool can resolve on PATH (the
/// caller's own presence probe) while its own `--version` spawn fails to
/// parse, and that should cost it the version, not the check.
#[allow(clippy::too_many_arguments)]
fn push_tool(
    checks: &mut Vec<DoctorCheck>,
    seen: &mut BTreeSet<&'static str>,
    missing: &mut Vec<MissingPrerequisite>,
    check_versions: &mut BTreeMap<String, String>,
    install: &BTreeMap<String, String>,
    name: &'static str,
    present: bool,
    version: Option<&str>,
    need: &str,
    fix: &str,
) {
    if !seen.insert(name) {
        return;
    }
    if let Some(version) = version {
        check_versions.insert(name.to_string(), version.to_string());
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
///
/// Widened again for `git` (tan-cli#120): `alp_project.py` — the loader every
/// backend's build-plan emission runs through — resolves out of a git
/// checkout and (on the Zephyr path) `west update` shells `git` directly, so a
/// missing one is exactly as fatal as a missing `cmake`, and it is exactly the
/// manifest's own `prerequisites` set again (`[git, cmake, python3]` /
/// `[git, cmake, python, ninja]`).
///
/// `python` is ALSO build-blocking — `alp_project.py` cannot run at all below
/// the manifest's floor — but it does NOT appear in this array. Its real state
/// is three-valued (absent, on-PATH-but-did-not-run, present-but-too-old), which
/// does not fit `push_tool`'s two-valued present/absent switch without losing
/// the "too old" detail tan-cli#120 asks this check to carry; [`push_python`]
/// decides its severity directly instead (`Fail` on anything but a
/// floor-clearing interpreter), independently of this array.
///
/// `dtc` and `gperf` (tan-cli#120) are deliberately NOT here, on two
/// independent grounds. They are Zephyr-build prerequisites only — their
/// `push_tool` call sites sit inside the `BuildOs::Zephyr` branch, so neither
/// even appears in a Yocto-only or baremetal-only report, unlike this array,
/// which would apply uniformly to every `push_tool` call for that name
/// regardless of which branch it came from. And separately from the gating:
/// the retired `alp doctor`'s own `_check_dtc`/`_check_gperf` were WARN-only
/// (`contract/fixtures/bootstrap/manifest.json`'s
/// `manualInstallHints.windows.note` element 3 records the same verdict this
/// port preserves), and alp-sdk#967 verified the Zephyr SDK's native-Windows
/// hosttools bundle ships neither tool at all — a hard `Fail` here would flag
/// as broken an environment tan's own bootstrap docs already call supported.
const BUILD_BLOCKING: [&str; 3] = ["cmake", "ninja", "git"];

/// `python`'s manifest install-command key differs by host —
/// `prerequisites.install.linux`/`.macos` list it as `python3`,
/// `prerequisites.install.windows` as bare `python`
/// (`contract/fixtures/bootstrap/manifest.json`) — while the DoctorCheck name
/// stays the stable `python` on every host (`push_python`'s own `NAME`). The
/// two used to diverge on `missingPrerequisites[].tool` too: this always
/// named it `python`, so a POSIX entry could not be re-keyed back into
/// `prerequisites.install`, and it disagreed with `tan bootstrap`'s own
/// `posix_refusal`, which reports the very same missing tool as `python3` —
/// two names for one tool across two modes of the same binary.
///
/// `install` is already resolved for ONE host by the caller, so which key is
/// actually present in it doubles as the host signal (no `HostOs` parameter
/// needed): a served POSIX map only ever carries `python3`, a served Windows
/// map only ever carries `python`. An UNSERVED host (no `install` entry for
/// either key, e.g. `HostOs::Other`) falls back to `python` — the check's own
/// stable name — which is harmless precisely because `command` is `None`
/// there regardless of which spelling `tool` carries.
fn python_prerequisite(install: &BTreeMap<String, String>) -> (&'static str, Option<String>) {
    match install.get("python3") {
        Some(command) => ("python3", Some(command.clone())),
        None => ("python", install.get("python").cloned()),
    }
}

/// The `python` doctor check (tan-cli#120): a VERSION FLOOR, not bare
/// presence. `probe_version` is `None` for both "not on PATH" and "on PATH but
/// did not run" (the Windows Store `python.exe` alias) — `tan-cli`'s host
/// probe cannot cheaply tell those two apart, and one `not found` detail
/// covers both honestly; what this check adds over a bare presence bool is the
/// THIRD case bootstrap's own gate already distinguishes — present, but below
/// `floor` — which gets its own, differently-worded detail rather than being
/// misreported as "not found" the way running this through [`push_tool`]
/// would.
///
/// `python` is build-blocking (see [`BUILD_BLOCKING`]'s doc comment for why it
/// is decided here instead of through that array): every backend's
/// build-plan emission runs `alp_project.py`, which cannot import at all on an
/// interpreter below the SDK's floor.
fn push_python(
    checks: &mut Vec<DoctorCheck>,
    seen: &mut BTreeSet<&'static str>,
    missing: &mut Vec<MissingPrerequisite>,
    check_versions: &mut BTreeMap<String, String>,
    install: &BTreeMap<String, String>,
    probe_version: Option<(u32, u32)>,
    floor: (u32, u32),
) {
    const NAME: &str = "python";
    if !seen.insert(NAME) {
        return;
    }
    if let Some((major, minor)) = probe_version {
        check_versions.insert(NAME.to_string(), format!("{major}.{minor}"));
    }
    let (tool, command) = python_prerequisite(install);
    let (status, detail) = match probe_version {
        Some(found) if found >= floor => (
            DoctorStatus::Pass,
            format!("python {}.{} available.", found.0, found.1),
        ),
        Some(found) => (
            DoctorStatus::Fail,
            format!(
                "python {}.{} found, but alp-sdk requires >= {}.{}.",
                found.0, found.1, floor.0, floor.1
            ),
        ),
        None => (
            DoctorStatus::Fail,
            "python not found on PATH (or did not run) — needed for all builds.".to_string(),
        ),
    };
    if status != DoctorStatus::Pass {
        missing.push(MissingPrerequisite {
            tool: tool.to_string(),
            command: command.clone(),
        });
    }
    checks.push(DoctorCheck {
        name: NAME.to_string(),
        status,
        detail,
        fix: if status == DoctorStatus::Pass {
            None
        } else {
            let base = format!("Install Python (>={}.{}).", floor.0, floor.1);
            Some(match &command {
                Some(command) => format!("{base} `{command}`"),
                None => base,
            })
        },
    });
}

fn count(checks: &[DoctorCheck], status: DoctorStatus) -> u32 {
    checks.iter().filter(|c| c.status == status).count() as u32
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bootstrap::{HostOs, fallback_facts};
    use crate::validate::parse_board_model;

    /// The manifest's `prerequisites.pythonMinVersion`, matching `install`'s
    /// own `fallback_facts((3, 10))` below -- both must read the same fixture
    /// floor.
    const FLOOR: (u32, u32) = (3, 10);

    /// One host's REAL install map — the fallback constants, which
    /// `bootstrap::manifest`'s
    /// `the_fallback_matches_the_real_manifest_field_for_field` pins byte-equal to
    /// the vendored `metadata/bootstrap.json`. Every command asserted below is
    /// therefore the producer's own string.
    fn install(host: HostOs) -> BTreeMap<String, String> {
        fallback_facts(FLOOR).install.for_host(host).clone()
    }

    fn probe_all_present() -> BuildToolProbe {
        BuildToolProbe {
            west: true,
            west_version: Some("1.5.0".to_string()),
            cmake: true,
            cmake_version: Some("3.28.1".to_string()),
            ninja: true,
            ninja_version: Some("1.11.1".to_string()),
            bitbake: true,
            bitbake_version: Some("2.4.0".to_string()),
            zephyr_sdk: true,
            bmaptool: true,
            dd: true,
            is_linux: true,
            is_windows: false,
            seven_zip: true,
            git: true,
            git_version: Some("2.43.0".to_string()),
            python_version: Some((3, 12)),
            dtc: true,
            dtc_version: Some("1.7.0".to_string()),
            gperf: true,
            gperf_version: Some("3.1".to_string()),
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
            FLOOR,
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
            west_version: None,
            cmake: false,
            cmake_version: None,
            ninja: false,
            ninja_version: None,
            bitbake: false,
            bitbake_version: None,
            zephyr_sdk: false,
            bmaptool: false,
            dd: false,
            is_linux: true,
            // Linux, so the `sevenZip` check does not fire for the many tests
            // built on this helper -- a Windows-only prerequisite must not
            // appear on every "nothing installed" report. The tests that DO
            // exercise it set `is_windows` explicitly.
            is_windows: false,
            seven_zip: false,
            git: false,
            git_version: None,
            python_version: None,
            dtc: false,
            dtc_version: None,
            gperf: false,
            gperf_version: None,
        }
    }

    #[test]
    fn missing_tools_report_with_next_steps() {
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        // west + dtc + gperf stay advisory; cmake + ninja + git + python
        // block the build (#103, widened by #120), and zephyrSdk joins them
        // (#159) -- no host builds Zephyr without a toolchain.
        assert_eq!(report.summary.warn, 3);
        assert_eq!(report.summary.fail, 5);
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
        // that builds fine routinely fails this probe. `vendorToolchain` is
        // advisory by design.
        //
        // `zephyrSdk` is NOT advisory any more (#159). The reason `dtc`/`gperf`
        // stay `Warn` is that the Zephyr SDK's native-Windows bundle supplies
        // them (alp-sdk#967) -- which is exactly why an ABSENT SDK is a
        // different finding: there is no host on which a Zephyr build survives
        // it. Measured on a fresh host in alp-sdk#855, this reported `Warn`,
        // the report said `0 failed`, rc was 0, and the next command died on
        // `Could not find a package configuration file provided by
        // "Zephyr-sdk"`.
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr, BuildOs::Baremetal],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
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
        assert_eq!(status("zephyrSdk"), DoctorStatus::Fail);
        assert_eq!(status("vendorToolchain"), DoctorStatus::Warn);
        // #120: `git`/`python` widen the same `Fail` line; `dtc`/`gperf` stay
        // `Warn`, matching the retired `alp doctor`'s own verdict.
        assert_eq!(status("git"), DoctorStatus::Fail);
        assert_eq!(status("python"), DoctorStatus::Fail);
        assert_eq!(status("dtc"), DoctorStatus::Warn);
        assert_eq!(status("gperf"), DoctorStatus::Warn);

        // A Yocto host: `bitbake` is build-blocking for Yocto but #103 is
        // explicit its `Warn` is correct -- widening must not have swept it up.
        let yocto = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        assert!(
            yocto
                .checks
                .iter()
                .any(|c| c.name == "bitbake" && c.status == DoctorStatus::Warn)
        );
        // NOT 0 any more: `git`/`python` are unconditional on `os_set` (#120),
        // so a Yocto-only report still fails on them even though neither
        // `cmake` nor `ninja` is even checked here.
        assert_eq!(yocto.summary.fail, 2);

        // Present tools are still clean -- the severity change touches the
        // absent branch only.
        let clean = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_all_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        assert_eq!(clean.summary.fail, 0);
    }

    #[test]
    fn zephyr_sdk_toolchain_check_names_the_exact_pinned_install_command() {
        // tan-cli#160(b): the reporter got past this with `west sdk install
        // --version 1.0.1 -t arm-zephyr-eabi` -- "knowledge tan never
        // provided". The fix string must now provide it verbatim, not merely
        // a docs URL a fresh-host customer has to translate into a command
        // themselves.
        let absent = zephyr_sdk_toolchain_check(false);
        assert_eq!(absent.status, DoctorStatus::Fail);
        let fix = absent.fix.expect("absent zephyrSdk must carry a fix");
        assert!(
            fix.contains("west sdk install --version 1.0.1 -t arm-zephyr-eabi"),
            "{fix}"
        );
        // The docs URL is additional context, not a REPLACEMENT for the
        // command -- both must be present.
        assert!(fix.contains("zephyr_sdk.html"), "{fix}");
        // #160(b) review: `fix` alone is invisible on the default (non
        // `--verbose`) text path, so plain `tan doctor` left this Fail with
        // no remedy. The command must ALSO be in `detail`, which every text
        // mode renders unconditionally.
        assert!(
            absent
                .detail
                .contains("west sdk install --version 1.0.1 -t arm-zephyr-eabi"),
            "{}",
            absent.detail
        );
    }

    #[test]
    fn zephyr_sdk_toolchain_check_passes_clean_with_no_fix_when_detected() {
        let present = zephyr_sdk_toolchain_check(true);
        assert_eq!(present.status, DoctorStatus::Pass);
        assert!(present.fix.is_none());
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
            FLOOR,
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
        // #120: `git`'s fix follows the identical append rule; `python`'s fix
        // comes from `push_python`, not `push_tool`, but must carry the SAME
        // manifest command under the SAME rule (looked up as `python3` on
        // POSIX -- see `python_install_command`).
        assert_eq!(fix_for("git"), "Install git. `sudo apt-get install -y git`");
        assert_eq!(
            fix_for("python"),
            "Install Python (>=3.10). `sudo apt-get install -y python3`"
        );
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
            FLOOR,
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
            FLOOR,
        );
        assert_eq!(
            report.missing_prerequisites,
            Some(vec![
                // git/python (#120) are pushed first -- unconditional, ahead of
                // any `os_set` branch.
                MissingPrerequisite {
                    tool: "git".to_string(),
                    command: Some("winget install -e --id Git.Git".to_string()),
                },
                MissingPrerequisite {
                    tool: "python".to_string(),
                    command: Some("winget install -e --id Python.Python.3.12".to_string()),
                },
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
                // dtc/gperf (#120): no Windows install one-liner either -- the
                // Zephyr SDK's native-Windows hosttools ship neither
                // (alp-sdk#967) -- so `null`, same rule as `west`/`bitbake`.
                MissingPrerequisite {
                    tool: "dtc".to_string(),
                    command: None,
                },
                MissingPrerequisite {
                    tool: "gperf".to_string(),
                    command: None,
                },
                // #203/#210: the one build-blocking Fail, and until now the only
                // row the extension could not put a Fix button on, because it
                // bypassed `push_tool` and so never reached this list at all.
                MissingPrerequisite {
                    tool: "zephyrSdk".to_string(),
                    command: Some(
                        "west sdk install --version 1.0.1 -t arm-zephyr-eabi".to_string(),
                    ),
                },
            ])
        );
    }

    /// #204. `sevenZip` is native-Windows-only AND only while the SDK is still
    /// missing, so it must be absent from the list above (`is_windows: false`)
    /// and present here.
    #[test]
    fn seven_zip_is_reported_only_on_windows_and_only_while_the_sdk_is_absent() {
        let tools = |probe: &BuildToolProbe| {
            build_readiness_report(
                "t".to_string(),
                vec![BuildOs::Zephyr],
                probe,
                &install(HostOs::Windows),
                FLOOR,
            )
        };

        let windows_no_sdk = tools(&BuildToolProbe {
            is_linux: false,
            is_windows: true,
            ..probe_none_present()
        });
        let seven_zip = windows_no_sdk
            .checks
            .iter()
            .find(|c| c.name == "sevenZip")
            .expect("native Windows without an SDK must carry the sevenZip check");
        assert_eq!(seven_zip.status, DoctorStatus::Warn);
        assert!(
            seven_zip.detail.contains("7z, 7za, 7zr, 7zz, 7zzs, unar"),
            "the detail must name every extractor patoolib accepts: {}",
            seven_zip.detail
        );
        assert!(
            windows_no_sdk
                .missing_prerequisites
                .as_ref()
                .unwrap()
                .contains(&MissingPrerequisite {
                    tool: "sevenZip".to_string(),
                    command: Some(SEVEN_ZIP_INSTALL_COMMAND.to_string()),
                }),
            "it must be actionable, not prose-only -- that was the whole defect"
        );

        // Same host, SDK already installed: the extractor is never used again,
        // so the row does not linger.
        let windows_with_sdk = tools(&BuildToolProbe {
            is_linux: false,
            is_windows: true,
            zephyr_sdk: true,
            ..probe_none_present()
        });
        assert!(
            !windows_with_sdk.checks.iter().any(|c| c.name == "sevenZip"),
            "a host that already has the SDK needs no extractor row"
        );

        // POSIX never sees it at all -- west needs no external extractor there.
        let posix = tools(&BuildToolProbe {
            seven_zip: false,
            ..probe_none_present()
        });
        assert!(!posix.checks.iter().any(|c| c.name == "sevenZip"));
        assert!(
            !posix
                .missing_prerequisites
                .as_ref()
                .unwrap()
                .iter()
                .any(|m| m.tool == "sevenZip")
        );
    }

    /// The `zephyrSdk` command is assembled once and read four ways. Pin that
    /// they agree, so a version bump cannot leave the Fix button running a
    /// different toolchain than the prose beside it names.
    #[test]
    fn the_zephyr_sdk_command_is_one_string_in_every_place_it_appears() {
        let command = zephyr_sdk_install_command();
        assert!(command.contains(crate::ZEPHYR_SDK_INSTALL_VERSION));

        let check = zephyr_sdk_toolchain_check(false);
        assert!(check.detail.contains(&command), "{}", check.detail);
        let fix = check.fix.expect("an absent SDK must carry a fix");
        assert!(fix.contains(&command), "{fix}");

        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        let entry = report
            .missing_prerequisites
            .unwrap()
            .into_iter()
            .find(|m| m.tool == "zephyrSdk")
            .expect("zephyrSdk must reach missingPrerequisites (#203, #210)");
        assert_eq!(entry.command, Some(command));
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
        // The `ninja` expectation below is CROSS-REPO DATA, not a tan rule.
        // This assertion previously expected `None` and carried a note saying
        // it would go red once alp-sdk#971's widened `prerequisites.posix` was
        // re-vendored, with the instruction to update the expectation rather
        // than weaken it. That is what happened, and this is that update.
        //
        // Worth keeping precise, because it was mis-stated more than once: the
        // map read here is `fallback_facts` (via `fn install(host)`), tan's
        // HAND-PORTED copy of the manifest -- not `contract/fixtures/bootstrap/
        // manifest.json`. Re-vendoring the fixture alone never moves this;
        // fixing the hand-ported constants is what does. The two being separate
        // is exactly how they drifted apart.
        for (host, cmake_command, git_command, python_tool, python_command, ninja_command) in [
            (
                HostOs::Linux,
                Some("sudo apt-get install -y cmake"),
                Some("sudo apt-get install -y git"),
                "python3",
                Some("sudo apt-get install -y python3"),
                Some("sudo apt-get install -y ninja-build"),
            ),
            (
                HostOs::MacOs,
                Some("brew install cmake"),
                Some("brew install git"),
                "python3",
                Some("brew install python3"),
                Some("brew install ninja"),
            ),
            // A POSIX host the manifest does not serve keeps the old all-`null`
            // behaviour rather than being handed the nearest OS's commands --
            // and, with no `python3` key to read the host signal off of
            // either, `tool` falls back to the check's own stable `python`.
            (HostOs::Other, None, None, "python", None, None),
        ] {
            let report = build_readiness_report(
                "t".to_string(),
                vec![BuildOs::Zephyr],
                &probe_none_present(),
                &install(host),
                FLOOR,
            );
            let missing = report.missing_prerequisites.expect("tools are missing");
            let tools: Vec<&str> = missing.iter().map(|m| m.tool.as_str()).collect();
            assert_eq!(
                tools,
                [
                    "git",
                    python_tool,
                    "west",
                    "cmake",
                    "ninja",
                    "dtc",
                    "gperf",
                    // #203/#210. Its command is tan's own (`west sdk install`),
                    // not the manifest's, so it is the one entry here that is
                    // identical on all three hosts.
                    "zephyrSdk",
                ],
                "{host:?}"
            );
            let command_for = |tool: &str| {
                missing
                    .iter()
                    .find(|m| m.tool == tool)
                    .and_then(|m| m.command.as_deref())
            };
            assert_eq!(command_for("cmake"), cmake_command, "{host:?}");
            assert_eq!(command_for("git"), git_command, "{host:?}");
            // The one entry here whose command is tan's own (`west sdk install`)
            // rather than the manifest's, so it is identical on all three hosts
            // -- including `HostOs::Other`, where every manifest-sourced command
            // is `null`.
            assert_eq!(
                command_for("zephyrSdk"),
                Some(zephyr_sdk_install_command().as_str()),
                "{host:?}"
            );
            // `python`'s `tool` self-resolves back into the SAME per-host
            // `install` map -- `python3` on a served POSIX host, matching
            // `tan bootstrap`'s own `posix_refusal` naming for the identical
            // missing tool (the divergence a doctor-vs-bootstrap review found).
            assert_eq!(command_for(python_tool), python_command, "{host:?}");
            assert_eq!(command_for("west"), None, "{host:?}");
            assert_eq!(command_for("ninja"), ninja_command, "{host:?}");
            // No POSIX install map lists either -- same "no invented command"
            // rule, on every host including the ones that do serve cmake/git.
            assert_eq!(command_for("dtc"), None, "{host:?}");
            assert_eq!(command_for("gperf"), None, "{host:?}");
        }
    }

    #[test]
    fn doctor_and_bootstrap_name_the_same_missing_python_the_same_way_on_posix() {
        // The two modes of the SAME binary must "cannot word one verdict two
        // ways" (this file's own stated goal, `append_host_prerequisites`'s
        // doc comment) for a tool as basic as python. `tan bootstrap`'s
        // `posix_refusal` names it straight from the manifest's
        // `prerequisites.posix` (`python3`); before this fix `push_python`
        // named the identical missing tool `python` regardless of host,
        // disagreeing with bootstrap and leaving a POSIX consumer unable to
        // re-key `missingPrerequisites[].tool` back into
        // `prerequisites.install`.
        let facts = fallback_facts(FLOOR);
        let install = facts.install.for_host(HostOs::Linux);

        let bootstrap_missing = crate::bootstrap::posix_refusal(
            &facts
                .prerequisites(HostOs::Linux)
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            install,
        )
        .missing;
        let bootstrap_python = bootstrap_missing
            .iter()
            .find(|m| m.tool.starts_with("python"))
            .expect("bootstrap names a missing python tool");

        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            install,
            FLOOR,
        );
        let doctor_python = report
            .missing_prerequisites
            .expect("tools are missing")
            .into_iter()
            .find(|m| m.tool.starts_with("python"))
            .expect("doctor names a missing python tool");

        assert_eq!(doctor_python.tool, bootstrap_python.tool);
        assert_eq!(doctor_python.command, bootstrap_python.command);
    }

    #[test]
    fn missing_prerequisites_covers_only_path_tools_and_respects_the_os_gate() {
        // Two rules in one: a Zephyr-only project must never be told to install
        // `bitbake` (the check is not even emitted), and the non-PATH checks
        // that genuinely cannot be expressed as a `{tool, command}` pair --
        // `bmaptool` (two tools, one advisory, `dd` fallback) and
        // `vendorToolchain` (no tool name at all) -- must stay out.
        //
        // `zephyrSdk` used to be listed here as a third such check. It is not
        // one: it has exactly one name and exactly one runnable command, and
        // excluding it cost the only build-blocking `Fail` its Fix button
        // (#203, #210). It is now reported like every other absent prerequisite,
        // and the Yocto half below still proves the OS gate holds -- a
        // Yocto-only project never reaches the Zephyr branch, so it sees no
        // `zephyrSdk` row at all.
        let zephyr_only = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        let tools: Vec<String> = zephyr_only
            .missing_prerequisites
            .expect("tools are missing")
            .into_iter()
            .map(|m| m.tool)
            .collect();
        assert_eq!(
            // `python3`, not `python`: `install(HostOs::Linux)` is a served
            // POSIX map, so `tool` self-resolves into it (see
            // `a_posix_host_gets_its_own_package_manager_and_never_winget`).
            tools,
            [
                "git",
                "python3",
                "west",
                "cmake",
                "ninja",
                "dtc",
                "gperf",
                "zephyrSdk"
            ]
        );

        // Yocto declared AND a Linux host -> `bitbake` is checked, so it is
        // reportable; no OS's install map lists it (it is a whole host-package
        // set), so `command` is `null` even against the richest map there is.
        // git/python (#120) are STILL reported here too -- they are
        // unconditional, not gated on Yocto.
        let yocto = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_none_present(),
            &install(HostOs::Windows),
            FLOOR,
        );
        assert_eq!(
            yocto.missing_prerequisites,
            Some(vec![
                MissingPrerequisite {
                    tool: "git".to_string(),
                    command: Some("winget install -e --id Git.Git".to_string()),
                },
                MissingPrerequisite {
                    tool: "python".to_string(),
                    command: Some("winget install -e --id Python.Python.3.12".to_string()),
                },
                MissingPrerequisite {
                    tool: "bitbake".to_string(),
                    command: None,
                },
            ])
        );

        // Yocto declared on a NON-Linux host -> the `bitbake` check is replaced
        // by `yoctoHost`, and dtc/gperf never appear (Zephyr not declared) --
        // but git/python (#120) are STILL reported: they check a host fact
        // every backend needs, independent of whether THIS host can run the
        // declared OS at all.
        let non_linux = BuildToolProbe {
            is_linux: false,
            ..probe_none_present()
        };
        let off_host = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &non_linux,
            &install(HostOs::Windows),
            FLOOR,
        );
        assert_eq!(
            off_host.missing_prerequisites,
            Some(vec![
                MissingPrerequisite {
                    tool: "git".to_string(),
                    command: Some("winget install -e --id Git.Git".to_string()),
                },
                MissingPrerequisite {
                    tool: "python".to_string(),
                    command: Some("winget install -e --id Python.Python.3.12".to_string()),
                },
            ])
        );
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
            FLOOR,
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
            FLOOR,
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
            FLOOR,
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
            FLOOR,
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
            FLOOR,
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
            FLOOR,
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
            FLOOR,
        );
        assert!(!zephyr_only.checks.iter().any(|c| c.name == "bmaptool"));
    }

    #[test]
    fn git_and_python_are_host_universal_and_build_blocking() {
        // tan-cli#120: unlike every other check in this file, git/python are
        // NOT gated on `os_set` -- `alp_project.py`'s build-plan emission runs
        // through both on EVERY backend, not just Zephyr's `west update`. A
        // Baremetal-only project declares no Zephyr and no Yocto; if git/python
        // were gated the way every other check is, neither would appear here.
        let probe = BuildToolProbe {
            git: false,
            python_version: None,
            ..probe_none_present()
        };
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Baremetal],
            &probe,
            &install(HostOs::Linux),
            FLOOR,
        );
        let status = |name: &str| {
            report
                .checks
                .iter()
                .find(|c| c.name == name)
                .unwrap_or_else(|| panic!("no {name} check"))
                .status
        };
        assert_eq!(status("git"), DoctorStatus::Fail);
        assert_eq!(status("python"), DoctorStatus::Fail);
    }

    #[test]
    fn dtc_and_gperf_warn_not_fail_and_stay_zephyr_only() {
        // Faithful port of the retired `alp doctor`'s `_check_dtc`/`_check_gperf`,
        // which were WARN-only
        // (`contract/fixtures/bootstrap/manifest.json`'s
        // `manualInstallHints.windows.note` element 3).
        let zephyr = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        let status_of = |report: &BuildReadinessReport, name: &str| {
            report
                .checks
                .iter()
                .find(|c| c.name == name)
                .unwrap_or_else(|| panic!("no {name} check"))
                .status
        };
        assert_eq!(status_of(&zephyr, "dtc"), DoctorStatus::Warn);
        assert_eq!(status_of(&zephyr, "gperf"), DoctorStatus::Warn);

        // Neither appears at all on a Yocto-only or baremetal-only report --
        // #120's whole reason for gating them on `os_set` instead of listing
        // them unconditionally like git/python.
        let yocto_only = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Yocto],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        assert!(!yocto_only.checks.iter().any(|c| c.name == "dtc"));
        assert!(!yocto_only.checks.iter().any(|c| c.name == "gperf"));
        let baremetal_only = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Baremetal],
            &probe_none_present(),
            &install(HostOs::Linux),
            FLOOR,
        );
        assert!(!baremetal_only.checks.iter().any(|c| c.name == "dtc"));
        assert!(!baremetal_only.checks.iter().any(|c| c.name == "gperf"));
    }

    #[test]
    fn python_check_distinguishes_absent_too_old_and_floor_clearing() {
        // tan-cli#120: a version FLOOR, not bare presence -- three real
        // states, three different details, only one of them a `Pass`.
        let python_of = |report: &BuildReadinessReport| {
            report
                .checks
                .iter()
                .find(|c| c.name == "python")
                .unwrap()
                .clone()
        };

        let absent = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &BuildToolProbe {
                python_version: None,
                ..probe_none_present()
            },
            &install(HostOs::Linux),
            FLOOR,
        );
        let check = python_of(&absent);
        assert_eq!(check.status, DoctorStatus::Fail);
        assert!(check.detail.contains("not found"), "{}", check.detail);

        let too_old = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &BuildToolProbe {
                python_version: Some((3, 9)),
                ..probe_none_present()
            },
            &install(HostOs::Linux),
            FLOOR,
        );
        let check = python_of(&too_old);
        assert_eq!(check.status, DoctorStatus::Fail);
        assert!(
            check.detail.contains("3.9") && check.detail.contains(">= 3.10"),
            "{}",
            check.detail
        );
        // Never confused with "not found" -- the whole point of the distinction
        // #120 asks this check to make.
        assert!(!check.detail.contains("not found"), "{}", check.detail);

        let at_floor = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &BuildToolProbe {
                python_version: Some((3, 10)),
                ..probe_none_present()
            },
            &install(HostOs::Linux),
            FLOOR,
        );
        assert_eq!(python_of(&at_floor).status, DoctorStatus::Pass);

        let newer = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &BuildToolProbe {
                python_version: Some((3, 14)),
                ..probe_none_present()
            },
            &install(HostOs::Linux),
            FLOOR,
        );
        assert_eq!(python_of(&newer).status, DoctorStatus::Pass);
    }

    #[test]
    fn check_versions_ride_along_in_the_serialized_checks_and_omit_when_unknown() {
        // tan-cli#123: `version` lives on the entry it describes, not a
        // sibling map -- and is ABSENT (never `null`) for a check with none
        // resolved (`ninja` here) or none meaningful at all (`zephyrSdk`).
        let probe = BuildToolProbe {
            cmake_version: Some("3.28.1".to_string()),
            ninja_version: None,
            ..probe_all_present()
        };
        let report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &probe,
            &install(HostOs::Linux),
            FLOOR,
        );
        assert_eq!(
            report.check_versions.get("cmake").map(String::as_str),
            Some("3.28.1")
        );
        assert_eq!(report.check_versions.get("ninja"), None);

        let json = serde_json::to_value(&report).unwrap();
        let checks = json["checks"].as_array().unwrap();
        let entry = |name: &str| {
            checks
                .iter()
                .find(|c| c["name"] == name)
                .unwrap_or_else(|| panic!("no {name} entry"))
        };
        assert_eq!(entry("cmake")["version"], "3.28.1");
        // `ninja` resolved (`Pass`) but no version -- omitted, not `null`.
        assert!(
            entry("ninja").get("version").is_none(),
            "{:?}",
            entry("ninja")
        );
        // `zephyrSdk` never carries a version at all -- no tool name, no probe.
        assert!(
            entry("zephyrSdk").get("version").is_none(),
            "{:?}",
            entry("zephyrSdk")
        );
    }

    #[test]
    fn west_and_west_resolved_carry_independently_different_versions() {
        // The exact bug tan-cli#123 was filed over: a `west (workspace)` row
        // reading a PATH-probed version while its own status came from the
        // venv resolver. `westResolved`'s CHECK comes from `tan_core::preflight`
        // (merged in by `tan-cli`'s `commands::doctor` via
        // `prepend_doctor_checks`, not built here), but its VERSION is attached
        // the same way this report's own checks' are -- through
        // `check_versions`, keyed by name -- so the two can carry genuinely
        // different answers without the map itself caring which file built
        // which check.
        let mut report = build_readiness_report(
            "t".to_string(),
            vec![BuildOs::Zephyr],
            &BuildToolProbe {
                west_version: Some("0.14.0".to_string()),
                ..probe_all_present()
            },
            &install(HostOs::Linux),
            FLOOR,
        );
        // Stands in for `commands::doctor::run_build_readiness`'s post-hoc
        // insert, once `westResolved` (from `probe_build_preflight`) has been
        // merged into `report.checks`.
        report
            .check_versions
            .insert("westResolved".to_string(), "1.5.0".to_string());

        assert_eq!(
            report.check_versions.get("west").map(String::as_str),
            Some("0.14.0")
        );
        assert_eq!(
            report
                .check_versions
                .get("westResolved")
                .map(String::as_str),
            Some("1.5.0")
        );
    }
}
