// SPDX-License-Identifier: Apache-2.0
//! `tan doctor` — diagnose host build readiness plus debug readiness for a
//! target/server combination.
//!
//! Mirrors the TypeScript `runDoctorCommand`: resolve the project context,
//! probe runtime capabilities (binaries on PATH), and build a doctor report.
//! It additionally folds in [`probe_build_preflight`] — the scope the docs
//! always claimed for it (#100); see [`assemble_doctor_report`].
//! Exit code is `doctorFailure` (4) when any check fails, `internalFailure`
//! (5) on an invalid `--target-kind`/`--server`, and `success` (0) otherwise.

use std::path::Path;

use tan_core::{
    BuildOs, BuildToolProbe, DebugServerKind, DebugTargetKind, DebuggerExtensionsState,
    DoctorCheck, DoctorReport, DoctorStatus, DoctorSummary, HostEnvProbe, ProjectContext,
    append_doctor_check, board_os_set, build_doctor_report, build_readiness_report,
    collect_runtime_capabilities_from_commands, create_debug_workspace_context,
    host_environment_checks, is_server_supported_for_target, parse_board_model, parse_server_kind,
    parse_target_kind, prepend_doctor_checks, server_choices_for_target,
};

use tan_core::bootstrap::{
    BOOTSTRAP_MANIFEST_REJECTED_FIX, BOOTSTRAP_MANIFEST_REJECTED_PREFIX, BootstrapFacts, HostOs,
    apply_prerequisite_check, doctor_prerequisite_check, fallback_facts,
};

use super::CommandRun;
use crate::cli::{BootstrapArgs, DoctorArgs, GlobalArgs};
use crate::commands::bootstrap::{check_prerequisites, load_facts};
use crate::commands::build::probe_build_preflight;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::style::{self, Theme};
use crate::util::{MIN_PYTHON, command_on_path, generated_at_iso, resolve_cli_project_context};

/// Entry point for `tan doctor`: dispatches to `--build` readiness, else resolves
/// the debug context, validates `--target-kind`/`--server`, probes runtime
/// capabilities, and emits the doctor report (text or JSON envelope).
pub fn run(g: &GlobalArgs, args: &DoctorArgs) -> CommandRun {
    let generated_at = generated_at_iso();
    if args.build {
        return run_build_readiness(g, &generated_at, args.fix);
    }
    let project = resolve_cli_project_context(g);
    let context = resolve_context(g, &project, &generated_at);

    // Resolved project paths are reported on the success path only (mirrors TS).
    let resolved_project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    // An ABSENT `--target-kind` used to parse to `native-host`, and an absent
    // `--server` to `none`, so plain `tan doctor` reported debug readiness for
    // the host binary no matter what board the project declared: it checked for
    // CodeLLDB on a project whose only debuggable artefact is an ELF on an M33
    // behind a SWD probe (#208). Every debug verdict it printed was answering a
    // question about a different target. An explicitly PASSED flag still wins
    // outright -- this only supplies the default the flag omits.
    let target = match args.target_kind.as_deref() {
        Some(raw) => match parse_target_kind(Some(raw)) {
            Ok(value) => value,
            Err(message) => return internal_failure(g, &generated_at, message),
        },
        None => default_target_kind(&project),
    };
    // Derived from the target, not defaulted to `none` beside it. `none` is
    // valid for `native-host` ALONE (`server_choices_for_target`), so pairing a
    // derived `zephyr-mcu` with a hardcoded `none` would trip
    // `is_server_supported_for_target` below and turn every plain `tan doctor`
    // in a real project into a `serverCompatibility` failure. Taking the
    // target's own first supported choice reuses that existing table instead of
    // opening a second one to disagree with it, and for `native-host` it still
    // resolves to `none` -- byte-identical to the old default.
    let server = match args.server.as_deref() {
        Some(raw) => match parse_server_kind(Some(raw)) {
            Ok(value) => value,
            Err(message) => return internal_failure(g, &generated_at, message),
        },
        None => server_choices_for_target(target)
            .first()
            .copied()
            .unwrap_or(DebugServerKind::None),
    };

    if !is_server_supported_for_target(target, server) {
        return unsupported_server(g, &generated_at, target, server);
    }

    let runtime = collect_runtime_capabilities_from_commands(command_on_path);
    let report = assemble_doctor_report(
        &context,
        target,
        server,
        &runtime,
        probe_build_preflight(g, &project),
    );

    let exit = if report.summary.fail > 0 {
        ExitCode::DoctorFailure
    } else {
        ExitCode::Success
    };
    let issues = checks_to_issues(&report.checks);
    let text = if g.is_json() {
        Vec::new()
    } else {
        format_doctor_text(g, &report)
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("doctor", resolved_project, report, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

/// `tan doctor --build` — build-readiness preflight. Resolves the OS set from
/// the active `board.yaml` (explicit core `os:` fields; all three when none are
/// declared), probes host build tools, and reports per-OS toolchain readiness.
/// Advisory only — the authoritative per-core resolution stays the SDK
/// build-plan emit's job.
fn run_build_readiness(g: &GlobalArgs, generated_at: &str, fix: bool) -> CommandRun {
    let mut context = resolve_cli_project_context(g);

    // `--fix`: when no Zephyr workspace is resolved, bootstrap one on demand
    // (reuses a compatible Zephyr, else bootstraps), then re-resolve the context.
    let mut bootstrap_fix_check: Option<DoctorCheck> = None;
    if fix
        && probe_build_preflight(g, &context)
            .iter()
            .any(|c| c.name == "workspace" && c.status == DoctorStatus::Fail)
    {
        // `let _ = ...` used to throw away the whole bootstrap CommandRun --
        // its exit code, its text, and (in --format json, where `bootstrap`
        // *captures* rather than streams) its entire envelope including the
        // `bootstrap.failed` / `bootstrap.prerequisites-missing` /
        // `bootstrap.yocto-host` issue. A failed or refused `--fix` was then
        // completely invisible: JSON mode reported nothing at all, so `--fix`
        // looked like a silent no-op. Fold the outcome into a doctor check
        // instead so it survives into the report/envelope. `--fix` is an
        // EXPLICIT opt-in, so it is deliberately not covered by `tan build`'s
        // `--no-auto-bootstrap`.
        let bootstrap_run = crate::commands::bootstrap::run(
            g,
            &BootstrapArgs {
                no_pip: false,
                no_west: false,
                print_env: false,
                workspace: None,
            },
        );
        bootstrap_fix_check = Some(bootstrap_fix_doctor_check(&bootstrap_run));
        context = resolve_cli_project_context(g);
    }

    let resolved_project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let os_set = read_board_model(&context)
        .map(|board| board_os_set(&board))
        .unwrap_or_else(|| vec![BuildOs::Zephyr, BuildOs::Yocto, BuildOs::Baremetal]);

    // `missingPrerequisites[].command` — the one field the extension's
    // `runToolchainFix` puts behind a Fix button — comes from the SDK's
    // `prerequisites.install`, so `--build` reads the same facts the prerequisite
    // gate does. This mode adds no `hostPrerequisites` CHECK (see
    // `append_host_prerequisites`); it needs the facts only for the commands.
    //
    // The REJECTION is a different matter and is not dropped. Falling back to
    // tan's compiled-in constants for a manifest that resolved and was refused —
    // version skew, unparseable — while reporting nothing is exactly the silent
    // substitution `bootstrap::manifest` calls the RFC #843 drift this
    // architecture exists to kill, and `--build` is the mode the extension shells
    // for `runToolchainFix`: on a future `schemaVersion: 2` SDK its Fix button
    // would otherwise run a stale command with no warning anywhere. Nothing else
    // in this report would say so — `append_sdk_provenance` reads git and
    // `metadata/sdk_version.yaml`, never the manifest. Plain `doctor` folds the
    // same message into `hostPrerequisites`' detail tail; this mode has no such
    // check, so it gets its own, sharing the wording constants so the two modes
    // cannot word one verdict two ways.
    //
    // Resolved BEFORE the probe below (not after, as originally): the `python`
    // check (#120) needs `facts.python_min_version` — the SAME floor `tan
    // bootstrap` enforces — to decide Pass vs Fail, not a second, tan-compiled-in
    // one that could drift from it.
    let (facts, manifest_error) = resolve_bootstrap_facts(context.sdk_root.as_deref());

    let probe = BuildToolProbe {
        west: command_on_path("west"),
        west_version: crate::util::tool_version("west"),
        cmake: command_on_path("cmake"),
        cmake_version: crate::util::tool_version("cmake"),
        ninja: command_on_path("ninja"),
        ninja_version: crate::util::tool_version("ninja"),
        bitbake: command_on_path("bitbake"),
        bitbake_version: crate::util::tool_version("bitbake"),
        zephyr_sdk: crate::toolchain::zephyr_sdk_detected(),
        bmaptool: command_on_path("bmaptool"),
        dd: command_on_path("dd"),
        is_linux: cfg!(target_os = "linux"),
        is_windows: cfg!(target_os = "windows"),
        // ANY of the accepted extractors counts -- patoolib takes the first it
        // finds, so probing only `7z` would call a host with `7zz` unequipped.
        seven_zip: tan_core::SEVEN_ZIP_PROGRAMS
            .iter()
            .any(|name| command_on_path(name)),
        git: command_on_path("git"),
        git_version: crate::util::tool_version("git"),
        // Resolved via bootstrap's own multi-candidate probe (`py -3`, `python3`,
        // `python`, ...) against the manifest floor, so this check's Pass/Fail is
        // driven by the EXACT SAME resolution `tan bootstrap` itself uses (#120)
        // — not a second, independent guess at which interpreter tan would run.
        python_version: crate::util::probe_host_python(facts.python_min_version).map(|h| h.version),
        dtc: command_on_path("dtc"),
        dtc_version: crate::util::tool_version("dtc"),
        gperf: command_on_path("gperf"),
        gperf_version: crate::util::tool_version("gperf"),
    };

    let mut report = build_readiness_report(
        generated_at.to_string(),
        os_set,
        &probe,
        facts.install.for_host(HostOs::detect(std::env::consts::OS)),
        facts.python_min_version,
    );
    // `westResolved`'s check (from `probe_build_preflight`, appended below by
    // `prepend_doctor_checks`) reports only PRESENCE. Its version (#123) is
    // resolved independently here, through the IDENTICAL `west_program` lookup
    // that check's own probe used — never `probe.west_version` (bare PATH) —
    // so the two rows can never be attributed to the wrong resolver, which is
    // precisely the bug tan-cli#123 was filed over.
    if let Some(version) = resolve_west_resolved_version(g, &context) {
        report
            .check_versions
            .insert("westResolved".to_string(), version);
    }
    if let Some(message) = manifest_error {
        // `Warn`, matching what plain `doctor` downgrades the same rejection to:
        // a read-only report should not be the thing that exits `DoctorFailure`
        // over a manifest, and the commands it fell back to are still real.
        append_doctor_check(
            &mut report.summary,
            &mut report.checks,
            &mut report.next_steps,
            DoctorCheck {
                name: "bootstrapManifest".to_string(),
                status: DoctorStatus::Warn,
                detail: format!("{BOOTSTRAP_MANIFEST_REJECTED_PREFIX}{message}"),
                fix: Some(BOOTSTRAP_MANIFEST_REJECTED_FIX.to_string()),
            },
        );
    }
    append_sdk_provenance(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        context.sdk_root.as_deref(),
    );

    // Real gate: prepend the project/workspace readiness (can a build even
    // start?) ahead of the host-tool probes, sharing `tan build`'s pre-flight
    // checks so `doctor` and `build` agree on what "ready" means.
    prepend_doctor_checks(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        probe_build_preflight(g, &context),
    );

    // Surface the `--fix` bootstrap outcome (see the discarded-CommandRun
    // comment above) right after the readiness checks it was meant to repair.
    if let Some(check) = bootstrap_fix_check {
        append_doctor_check(
            &mut report.summary,
            &mut report.checks,
            &mut report.next_steps,
            check,
        );
    }

    let exit = if report.summary.fail > 0 {
        ExitCode::DoctorFailure
    } else {
        ExitCode::Success
    };
    let issues = checks_to_issues(&report.checks);
    let text = if g.is_json() {
        Vec::new()
    } else {
        format_build_text(g, &report)
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("doctor", resolved_project, report, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

/// The version of the SAME `west` `westResolved`'s check verified is present:
/// the workspace-venv binary [`crate::venv::west_program`] resolves, not
/// bare PATH's. `probe_build_preflight` (which builds the `westResolved`
/// check itself) reports only presence, not a version, so this resolves the
/// identical lookup independently rather than threading a version back out of
/// that call — the two must never end up naming different binaries (tan-cli#123:
/// the reported bug was exactly a version read off a DIFFERENT resolver than
/// the one the verdict came from).
fn resolve_west_resolved_version(g: &GlobalArgs, context: &ProjectContext) -> Option<String> {
    let base = crate::commands::build::base_dir(context);
    let sdk_root = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));
    let west = crate::venv::west_program(&base, sdk_root.as_deref());
    crate::util::tool_version(&west)
}

/// The plain `tan doctor` report: the pure check set, plus the checks that can
/// only be built with IO — the host prerequisite gate, the host-environment
/// probes, the SDK provenance, and the shared build-readiness preflight.
///
/// The preflight is folded in because alp-sdk's onboarding path points every
/// new customer at PLAIN `tan doctor` — `bootstrap`'s printed next steps,
/// `README.md`'s Quickstart ("catches a missing toolchain/HAL before it bites
/// later"), `docs/cli.md`, `docs/getting-started.md` — and none of them names
/// `--build`. Without it this command probed nothing about whether a build
/// could run and printed byte-identical output across four materially
/// different host states (#100). It is [`probe_build_preflight`], the same
/// call `tan build` and `tan doctor --build` make, not a second copy of the
/// rules.
///
/// `--build` is NOT thereby a superset and the two stay distinct: it resolves
/// its OS set from the active `board.yaml` (`zephyr · yocto · baremetal`) and
/// layers a `BuildToolProbe` on top, where plain doctor stays a
/// `native-host · none` debug report that has gained build-readiness facts.
///
/// Prepended, matching `--build`: "can a build even start" outranks the debug
/// tooling, and `nextSteps` follows check order.
///
/// The preflight's own `boardYaml` is DROPPED — it and the debug report's ask
/// the same question, and emitting both would report one fact twice under one
/// name. The debug one survives because it is the one shared with `inspect` /
/// `support-bundle` and the one whose severity is project-selection aware (see
/// `tan_core::debug::doctor`'s `board_yaml_check`); `--build`'s copy is
/// untouched.
///
/// Its own function rather than four inline lines in `run` so the appends are
/// reachable from a test. `run` resolves a real project and shells `git`, so a
/// dropped `append_host_prerequisites` call inline there was invisible to the
/// suite — the same hole `support_bundle::bundle_doctor_report` closes.
///
/// Every append lands AFTER `build_doctor_report` computed `nextSteps`, so each
/// goes through `append_doctor_check`, which re-derives it. That is why there is
/// no trailing `next_steps = unique_next_steps(...)` statement here: a
/// statement a caller can forget is a bug waiting to be re-introduced, and this
/// one already was one.
fn assemble_doctor_report(
    context: &tan_core::DebugWorkspaceContext,
    target: DebugTargetKind,
    server: DebugServerKind,
    runtime: &tan_core::DebugRuntimeCapabilities,
    preflight: Vec<DoctorCheck>,
) -> DoctorReport {
    let mut report = build_doctor_report(context, target, server, runtime);
    prepend_doctor_checks(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        preflight
            .into_iter()
            .filter(|c| c.name != "boardYaml")
            .collect(),
    );
    append_host_prerequisites(&mut report, context.sdk_root.as_deref());
    append_host_environment(&mut report);
    append_zephyr_sdk_toolchain(&mut report);
    append_sdk_provenance(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        context.sdk_root.as_deref(),
    );
    report
}

/// Append the `zephyrSdk` check to PLAIN `tan doctor` (tan-cli#160). It
/// already exists under `--build`, gated on the active `board.yaml` declaring
/// a Zephyr core; here it is unconditional, for the same reason
/// `append_host_environment`'s checks are — this is a HOST fact (env var /
/// scanned install dir, `crate::toolchain::zephyr_sdk_detected`), needing no
/// `board.yaml`, no workspace and no SDK, and ADR 0021 Lane 1 P0a runs `tan
/// doctor` before anything project-shaped exists. Most boards this SDK
/// targets pair a Zephyr-on-M core with Yocto-on-A (`project_v05...` /
/// H2-2026 scope), so a Yocto-only project seeing this check too is the
/// uncommon case, and a `Fail` on it is still true regardless: this host
/// genuinely has no Zephyr SDK.
///
/// This is the check the alp-sdk#855 fresh-host run needed and never got: on
/// that run `tan doctor` (plain) never mentioned the word "toolchain" at all,
/// and `tan doctor --build`'s equivalent check was reached only after
/// `tan init` — a full project creation — stood between the user and the
/// first sign of trouble. Surfacing it at the FIRST command a customer runs
/// closes that gap directly rather than relying on `zephyrSdkAvailableForHost`
/// (which answers "can one be provisioned here", not "is one installed") to
/// be read as a substitute for it.
fn append_zephyr_sdk_toolchain(report: &mut DoctorReport) {
    append_doctor_check(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        tan_core::zephyr_sdk_toolchain_check(crate::toolchain::zephyr_sdk_detected()),
    );
}

/// Append the `hostPrerequisites` check: run `bootstrap`'s own prerequisite
/// gate and report its verdict, so a missing `ninja` is visible from plain
/// `tan doctor` (alp-sdk ADR 0021, Lane 1 P0a).
///
/// The `ninja` case is WINDOWS-ONLY, because the tool list is: the manifest's
/// `prerequisites.posix` is `[git, cmake, python3]` and names no `ninja` at all
/// (`prerequisites.windows` adds it). The manifest records that asymmetry
/// faithfully rather than unifying the two lists, and so does this — on a
/// Linux/macOS host a missing `ninja` still surfaces only through
/// `tan doctor --build`'s `BuildToolProbe`, which probes it by name on every
/// platform.
///
/// PLAIN `tan doctor`, not `--build`, deliberately. Prerequisites are a HOST
/// fact — they need no `board.yaml`, no workspace, no SDK — and P0a runs this
/// BEFORE the bootstrap terminal exists, when there is nothing project-shaped to
/// resolve yet. `--build` is the project-shaped report (its OS set comes from
/// the active `board.yaml`) and already probes `ninja`/`cmake` separately
/// through `BuildToolProbe`; adding this there would report the same tool twice
/// under two names.
///
/// Facts: the SDK's `metadata/bootstrap.json` when one resolves and parses,
/// else tan's built-in fallback list. Either way the host IS checked and the
/// check's detail names which list it checked against, the same fact
/// bootstrap's envelope reports as `factsFromManifest`; a doctor run without an
/// SDK must not quietly claim the host is fine.
///
/// A manifest that resolved but was REFUSED (version skew, unparseable) is a
/// third case, not the second. `load_facts`'s `Err` names why — that is the
/// whole reason it returns a `Result`, and `tan bootstrap` treats the same
/// message as a fatal `ValidationFailure`. Doctor keeps the message and hands it
/// to `doctor_prerequisite_check` rather than `.ok()`-ing it away: this command
/// exists to tell a user why bootstrap refuses, so it is the last command that
/// may hide the reason. It does NOT repeat bootstrap's fatal verdict (a
/// read-only report should not be the thing that exits 4 over a manifest), it
/// downgrades it to a `Warn` naming the rejection.
///
/// `pub(crate)` — `commands::support_bundle` builds the same `DoctorReport` and
/// must append the same gate. A bundle is what a user attaches PRECISELY when
/// bootstrap failed, so one serialized with `"missingPrerequisites": null` for a
/// host nobody probed would positively claim a host is clean while hiding the
/// missing `ninja` that is the reason for the bundle.
pub(crate) fn append_host_prerequisites(report: &mut DoctorReport, sdk_root: Option<&str>) {
    let (facts, manifest_error) = resolve_bootstrap_facts(sdk_root);
    let host = HostOs::detect(std::env::consts::OS);
    let refusal = check_prerequisites(&facts, host).err();

    let check = doctor_prerequisite_check(
        refusal.as_ref(),
        facts.prerequisites(host == HostOs::Windows),
        facts.from_manifest,
        manifest_error.as_deref(),
    );
    // The verdict is folded in by a PURE function, so the summary arithmetic and
    // the structured `{tool, command}` half (which rides on the report rather
    // than the check, because the issue message is prose the split is not
    // recoverable from -- alp-sdk-vscode#347) are tested over all three
    // outcomes, not only whichever one this test host's PATH happens to produce.
    apply_prerequisite_check(
        report,
        check,
        refusal.map(|f| f.missing).unwrap_or_default(),
    );
}

/// The bootstrap facts behind BOTH doctor modes, with the message naming why a
/// resolved manifest was refused (`None` when none was, or when there was no SDK
/// to read one from).
///
/// One resolver, because both modes now read these facts and a second copy of the
/// skew rule is exactly the drift `load_facts`' `Result` exists to catch: plain
/// `doctor` needs the tool LIST and the refusal wording, `--build` needs
/// `prerequisites.install` for its `missingPrerequisites[].command`, and an SDK
/// whose manifest one mode refuses cannot be one the other silently accepts.
fn resolve_bootstrap_facts(sdk_root: Option<&str>) -> (BootstrapFacts, Option<String>) {
    match sdk_root.map(load_facts) {
        Some(Ok(facts)) => (facts, None),
        Some(Err(message)) => (fallback_facts(MIN_PYTHON), Some(message)),
        None => (fallback_facts(MIN_PYTHON), None),
    }
}

/// Append the host-environment checks — `zephyrSdkAvailableForHost`,
/// `longPaths` (Windows only) and `homePath` (alp-sdk ADR 0021, "Cross-cutting
/// requirements").
///
/// This function is the IO half and nothing else: three probes, then one call
/// into the pure `tan_core::host_env`. The split is what makes the checks
/// testable at all — the verdicts depend entirely on which machine is running,
/// so a test that called this would only ever exercise the branch this host
/// sits in (a served `windows-x86_64`/`linux-x86_64`, always). All four host
/// tags and both registry states are driven directly against the pure
/// functions instead; see `tan_core::host_env`'s tests.
///
/// PLAIN `tan doctor`, not `--build`, for the same reason
/// [`append_host_prerequisites`] is: these need no `board.yaml`, no workspace
/// and no SDK, and ADR 0021 Lane 1 P0a runs `tan doctor` before anything
/// project-shaped exists. `--build` deliberately does NOT get them.
/// `zephyrSdkAvailableForHost` looks adjacent to `--build`'s `zephyrSdk` probe
/// but answers the opposite question — "can an SDK be installed on this host
/// at all" versus "is one installed here" — and reporting the SDK story twice
/// under two names that could be confused for each other is exactly the trap
/// #81 documents (renamed from `zephyrSdkHost` in tan-cli#160 to make the two
/// impossible to conflate at a glance, after #159/#166 turned the SECOND check
/// into a hard `Fail` — a `[+]` here beside a hard-failing `zephyrSdk` was
/// worse than either alone).
///
/// `pub(crate)`: `commands::support_bundle` builds the same report and must
/// carry the same facts. A bundle from a `windows-arm64` or Intel-Mac host that
/// omitted the reason nothing can be provisioned would send a maintainer
/// hunting through a build log for a fact the host already knew.
pub(crate) fn append_host_environment(report: &mut DoctorReport) {
    let home = host_home();
    let probe = HostEnvProbe {
        // `OS` is safe as a constant — a Windows binary does not run on macOS.
        // `ARCH` is NOT (see `host_arch`).
        os: std::env::consts::OS,
        arch: host_arch(),
        long_paths_enabled: long_paths_enabled(),
        home: home.as_deref(),
    };
    for check in host_environment_checks(&probe) {
        append_doctor_check(
            &mut report.summary,
            &mut report.checks,
            &mut report.next_steps,
            check,
        );
    }
}

/// The user's home directory: `USERPROFILE` on Windows else `HOME`, matching
/// [`crate::util::home_alp_dir`]'s resolution so the path this check reports a
/// space in is the same one tan would actually put `~/.alp` under.
///
/// No `.` fallback here, unlike `home_alp_dir`: "unset" is a real, reportable
/// state for a doctor check, and reporting `.` would claim a clean home nobody
/// resolved.
fn host_home() -> Option<String> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(|v| v.to_string_lossy().into_owned())
}

/// The arch of the MACHINE, which is not `std::env::consts::ARCH`.
///
/// `ARCH` is fixed at compile time — it names the target the binary was built
/// for. `.github/workflows/release.yml` ships `x86_64-pc-windows-msvc` and
/// `x86_64-apple-darwin` as separate assets from their aarch64 siblings, and
/// both of those x86_64 binaries run on aarch64 hardware:
///
/// * **Windows on ARM emulates x64 transparently**, so the x86_64 asset is the
///   likeliest way tan runs there at all. On the constant it reports
///   `windows-x86_64`, a served host — which would make the `windows-aarch64`
///   arm this whole check exists for almost unreachable in practice.
/// * **Rosetta** runs the x86_64 asset on Apple silicon. On the constant it
///   reports `macos-x86_64` and FAILS a fully served machine, exiting 4 and
///   telling its owner to go find a Linux box.
///
/// Both OSes will say so if asked, and the mapping from what they answer to an
/// arch token is pure ([`tan_core::host_env::arch_for_image_file_machine`],
/// [`tan_core::host_env::arch_for_proc_translated`]) and unit-tested there. The
/// constant survives only as the fallback for a query that fails or names a
/// machine type tan has no token for — inventing a tag there would fail a host
/// merely because tan could not name it.
///
/// Linux is left on the constant deliberately: tan ships no Linux asset whose
/// arch can differ from its host's, and box64-style emulation is not a
/// configuration Alp Lab supports.
#[cfg(windows)]
fn host_arch() -> &'static str {
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, IsWow64Process2};

    let mut process_machine: u16 = 0;
    let mut native_machine: u16 = 0;
    // SAFETY: `GetCurrentProcess` returns a pseudo-handle that needs no close
    // and is always valid; both out-parameters are live `u16`s. The call
    // returns 0 on failure, in which case `native_machine` is untouched — so
    // the result is only read when it returned non-zero.
    let ok = unsafe {
        IsWow64Process2(
            GetCurrentProcess(),
            &mut process_machine,
            &mut native_machine,
        )
    };
    if ok == 0 {
        return std::env::consts::ARCH;
    }
    tan_core::host_env::arch_for_image_file_machine(native_machine)
        .unwrap_or(std::env::consts::ARCH)
}

/// macOS: `sysctl.proc_translated` is 1 exactly when this process is an x86_64
/// binary running under Rosetta, which only exists on Apple silicon — so it is
/// a direct statement that the machine is `aarch64`. The sysctl is absent on
/// older systems, where the call fails and a native binary's own arch is the
/// right answer anyway.
///
/// Declared rather than pulled in through `libc`: it is one `extern` line
/// against `libSystem`, which every macOS binary already links, and the crate
/// has no `libc` edge today.
#[cfg(target_os = "macos")]
fn host_arch() -> &'static str {
    unsafe extern "C" {
        fn sysctlbyname(
            name: *const std::ffi::c_char,
            oldp: *mut std::ffi::c_void,
            oldlenp: *mut usize,
            newp: *mut std::ffi::c_void,
            newlen: usize,
        ) -> std::ffi::c_int;
    }

    let mut translated: std::ffi::c_int = 0;
    let mut size = std::mem::size_of::<std::ffi::c_int>();
    // SAFETY: the name is a NUL-terminated literal; `translated`/`size` are a
    // live `c_int` and its byte length; `newp`/`newlen` are null/0, which is
    // the documented form for a READ. A non-zero return means the sysctl is
    // absent (pre-Big Sur), and `translated` keeps its initialised 0.
    let rc = unsafe {
        sysctlbyname(
            c"sysctl.proc_translated".as_ptr(),
            std::ptr::from_mut(&mut translated).cast(),
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    tan_core::host_env::arch_for_proc_translated(rc == 0 && translated == 1, std::env::consts::ARCH)
}

/// Linux and everything else: the binary's arch is the machine's. See the
/// Windows arm above for why the other two are not.
#[cfg(not(any(windows, target_os = "macos")))]
fn host_arch() -> &'static str {
    std::env::consts::ARCH
}

/// Read `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled`.
///
/// The registry API, not `reg query`: shelling out costs a process, inherits
/// whatever `PATH` the caller has (the one thing a broken host is likely to
/// have wrong), and turns a typed DWORD into text that then has to be parsed
/// back. `windows-sys` is already resolved in the lock tree, so this is a
/// direct edge onto a crate that was being built anyway, target-gated to
/// Windows.
///
/// The three-way mapping from the returned status is NOT here — it is
/// [`tan_core::host_env::classify_long_paths`], which is unit-tested on every
/// host. What is left below is the `RegGetValueW` call and nothing else, so
/// the untested region is the FFI alone rather than the branching that decides
/// what a user is told.
///
/// `HKLM\SYSTEM\...` is not subject to WOW64 registry redirection, so no
/// `KEY_WOW64_*` flag is needed for a 32-bit `tan` on a 64-bit host.
#[cfg(windows)]
fn long_paths_enabled() -> Option<bool> {
    use windows_sys::Win32::System::Registry::{
        HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RegGetValueW,
    };

    /// NUL-terminated UTF-16, as the `W` entry points require.
    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }

    let subkey = wide(tan_core::host_env::LONG_PATHS_KEY.trim_start_matches(r"HKLM\"));
    let value = wide("LongPathsEnabled");
    let mut data: u32 = 0;
    let mut size: u32 = std::mem::size_of::<u32>() as u32;
    // SAFETY: `subkey`/`value` are NUL-terminated UTF-16 buffers that outlive
    // the call; `data`/`size` are a live `u32` and its byte length, and
    // `RRF_RT_REG_DWORD` makes the API reject any value whose type is not a
    // 4-byte DWORD rather than overrun the buffer. A null `pdwtype` is
    // documented as "do not return the type".
    let status = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            subkey.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_DWORD,
            std::ptr::null_mut(),
            std::ptr::from_mut(&mut data).cast(),
            &mut size,
        )
    };
    tan_core::host_env::classify_long_paths(status, data)
}

/// `classify_long_paths` compares against `winerror.h` values re-declared in
/// `tan-core` (which must stay platform-neutral and cannot import
/// `windows-sys`). These two asserts are what stop the copies drifting: they
/// are checked at compile time against the real headers, on the one target
/// where the real headers exist.
#[cfg(windows)]
const _: () = {
    assert!(tan_core::host_env::WIN_ERROR_SUCCESS == windows_sys::Win32::Foundation::ERROR_SUCCESS);
    assert!(
        tan_core::host_env::WIN_ERROR_FILE_NOT_FOUND
            == windows_sys::Win32::Foundation::ERROR_FILE_NOT_FOUND
    );
};

/// No such registry value off Windows. `host_environment_checks` never reaches
/// the `longPaths` check on a non-Windows `os`, so this is only ever the unused
/// field of the probe struct.
#[cfg(not(windows))]
fn long_paths_enabled() -> Option<bool> {
    None
}

/// Append an SDK-provenance check (conformance Issue 4 + 6): records the SDK
/// checkout's git short-commit and `metadata/sdk_version.yaml`, so a build plan
/// can be traced to the planner that produced it, and warns when the checkout
/// is behind its upstream tracking ref.
fn append_sdk_provenance(
    summary: &mut DoctorSummary,
    checks: &mut Vec<DoctorCheck>,
    next_steps: &mut Vec<String>,
    sdk_root: Option<&str>,
) {
    let Some(root) = sdk_root else {
        return;
    };

    let commit = git_short_commit(root);
    let version = read_sdk_version(root);

    let mut detail = match (&version, &commit) {
        (Some(v), Some(c)) => format!("alp-sdk {v} @ {c}"),
        (None, Some(c)) => format!("alp-sdk @ {c}"),
        (Some(v), None) => format!("alp-sdk {v}"),
        (None, None) => {
            format!("alp-sdk at {root} (no git checkout / metadata/sdk_version.yaml)")
        }
    };

    // Advisory: reads the local remote-tracking ref, performs no network fetch,
    // so it only reflects the checkout's state as of the last `git fetch`.
    let (status, fix) = match git_behind_upstream(root) {
        Some(n) if n > 0 => {
            detail = format!("{detail} — {n} commit(s) behind upstream");
            (
                DoctorStatus::Warn,
                Some(format!("Update the SDK checkout: git -C {root} pull")),
            )
        }
        _ => (DoctorStatus::Pass, None),
    };

    append_doctor_check(
        summary,
        checks,
        next_steps,
        DoctorCheck {
            name: "sdkProvenance".to_string(),
            status,
            detail,
            fix,
        },
    );
}

/// `git -C <root> rev-parse --short HEAD`, or `None` when `root` is not a git
/// checkout (e.g. an extracted SDK release archive).
fn git_short_commit(root: &str) -> Option<String> {
    let output = std::process::Command::new("git")
        .args(["-C", root, "rev-parse", "--short", "HEAD"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let commit = String::from_utf8(output.stdout).ok()?.trim().to_string();
    (!commit.is_empty()).then_some(commit)
}

/// Count of commits `HEAD` is behind its upstream tracking ref, without
/// fetching. `None` when there is no upstream or `root` is not a git checkout.
fn git_behind_upstream(root: &str) -> Option<u32> {
    let output = std::process::Command::new("git")
        .args(["-C", root, "rev-list", "--count", "HEAD..@{upstream}"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8(output.stdout).ok()?.trim().parse().ok()
}

/// Read a version from `<root>/metadata/sdk_version.yaml`: a `version: X` line
/// if present, else the first bare scalar. `None` when the file is absent.
///
/// The parse itself is `tan_core::parse_sdk_version_yaml` — shared with
/// `check_sdk_readiness`'s fallback (tan-cli#162), so `tan sdk
/// install`/`current`/`switch` and this `sdkProvenance` check read the SAME
/// version out of the SAME file rather than two copies of the scan able to
/// disagree.
fn read_sdk_version(root: &str) -> Option<String> {
    let path = Path::new(root).join("metadata").join("sdk_version.yaml");
    let text = std::fs::read_to_string(path).ok()?;
    tan_core::parse_sdk_version_yaml(&text)
}

/// Read + parse the active `board.yaml`, returning `None` when it is absent or
/// unparseable (the preflight then falls back to checking all three backends).
/// The debug target plain `tan doctor` reports on when `--target-kind` is
/// omitted, resolved from the project's own `board.yaml` (#208).
///
/// `native-host` is kept for the case it is actually right — no `board.yaml`
/// resolves, so there is no board to debug and the host binary is the only
/// target there is. That is also the directory alp-sdk's bootstrap tells a
/// customer to run `tan doctor` from (see `board_yaml_check`), so it stays the
/// quiet, unsurprising answer there rather than a guess about hardware.
///
/// With a board, the OS set decides, reusing [`board_os_set`] rather than
/// re-reading `board.yaml` a second way. A multicore board declares several, and
/// only one target can be reported, so the priority is fixed and stated:
///
/// 1. `zephyr` — `zephyr-mcu`
/// 2. `baremetal` — `baremetal-mcu`
/// 3. `yocto` — `yocto-userspace`
///
/// MCU-class first because that is what the checks below actually answer — a
/// probe, a GDB server and a debugger extension, all host-side tooling. The
/// `yocto-userspace` path needs a booted target running `gdbserver`, which no
/// amount of host inspection can establish, so it is the weakest default of the
/// three and ranks last. A board that declares nothing at all resolves to all
/// three (`board_os_set`'s documented fallback) and therefore lands on
/// `zephyr-mcu`, which is the SDK's primary backend.
///
/// `--target-kind` overrides all of it; this only fills the gap.
fn default_target_kind(context: &ProjectContext) -> DebugTargetKind {
    let Some(board) = read_board_model(context) else {
        return DebugTargetKind::NativeHost;
    };
    let os_set = board_os_set(&board);
    if os_set.contains(&BuildOs::Zephyr) {
        DebugTargetKind::ZephyrMcu
    } else if os_set.contains(&BuildOs::Baremetal) {
        DebugTargetKind::BaremetalMcu
    } else if os_set.contains(&BuildOs::Yocto) {
        DebugTargetKind::YoctoUserspace
    } else {
        DebugTargetKind::NativeHost
    }
}

fn read_board_model(context: &ProjectContext) -> Option<tan_core::BoardModel> {
    let path = context.board_yaml_path.as_deref()?;
    let source = std::fs::read_to_string(path).ok()?;
    parse_board_model(&source).ok()
}

/// Fold a `bootstrap --fix` `CommandRun` into a `DoctorCheck`. Text mode
/// carries a one-line summary in `text`; JSON mode captures the whole run and
/// only puts a message in `json` (see bootstrap.rs), so pull the first issue
/// message back out of that envelope rather than reporting nothing.
fn bootstrap_fix_doctor_check(run: &CommandRun) -> DoctorCheck {
    let status = if run.exit == ExitCode::Success {
        DoctorStatus::Pass
    } else {
        DoctorStatus::Fail
    };
    let detail = if !run.text.is_empty() {
        run.text.join(" ")
    } else {
        run.json
            .as_deref()
            .and_then(bootstrap_envelope_issue_message)
            .unwrap_or_else(|| format!("tan bootstrap --fix exited {}", run.exit.code()))
    };
    let fix = (status != DoctorStatus::Pass)
        .then(|| "Run `tan bootstrap` directly to see the full install log.".to_string());
    DoctorCheck {
        name: "bootstrapFix".to_string(),
        status,
        detail,
        fix,
    }
}

/// Pull the first `issues[].message` out of a serialized bootstrap envelope.
fn bootstrap_envelope_issue_message(json: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(json).ok()?;
    value
        .get("issues")?
        .as_array()?
        .first()?
        .get("message")?
        .as_str()
        .map(str::to_string)
}

/// Render the `--build` readiness report as human-readable lines, with the
/// resolved OS set (e.g. `zephyr · yocto`) as the subtitle.
fn format_build_text(g: &GlobalArgs, report: &tan_core::BuildReadinessReport) -> Vec<String> {
    let subtitle = report
        .os_set
        .iter()
        .map(|os| {
            serde_json::to_value(os)
                .ok()
                .and_then(|v| v.as_str().map(str::to_string))
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" · ");
    style::render_report(
        g,
        "tan doctor --build",
        &subtitle,
        &report.checks,
        &report.summary,
        &report.next_steps,
    )
}

/// Resolve the debug workspace context, mirroring TS `resolveCliDebugContext`.
fn resolve_context(
    g: &GlobalArgs,
    project: &ProjectContext,
    generated_at: &str,
) -> tan_core::DebugWorkspaceContext {
    create_debug_workspace_context(
        project,
        generated_at.to_string(),
        |path| Path::new(path).exists(),
        project_selected(g),
        standalone_debugger_extensions(),
    )
}

/// Whether the user NAMED a project, rather than letting resolution default to
/// the working directory: `--project` or `--board-yaml`, either one non-empty.
///
/// Those two flags are the whole selection surface —
/// `resolve_cli_project_context` defaults them to `.` and `board.yaml` — so
/// with neither given the resolved `board.yaml` path is a guess, not a request.
pub(crate) fn project_selected(g: &GlobalArgs) -> bool {
    [g.project.as_deref(), g.board_yaml.as_deref()]
        .into_iter()
        .flatten()
        .any(|value| !value.trim().is_empty())
}

/// The `DebuggerExtensionsState` for the standalone `tan` binary.
///
/// The three flags are NOT MEANINGFUL here and nothing may read them as facts:
/// only a VS Code extension host can enumerate its own marketplace extensions,
/// so these are an inherited assumption from the TS `resolveCliDebugContext`
/// (where `true` is correct, because that code CAN introspect its host). They
/// are kept so the port stays faithful to `createExtensionCheck`'s three real
/// TS counterparts, and `observable: false` is what stops them being reported
/// — every derived check renders `unknown` instead of printing "vadimcn.
/// vscode-lldb is installed." on a headless container and counting itself
/// among the passes (#102).
///
/// `pub(crate)`: `inspect` and `support-bundle` build the same context and must
/// carry the same disclaimer, not a third copy of the literal.
pub(crate) fn standalone_debugger_extensions() -> DebuggerExtensionsState {
    DebuggerExtensionsState {
        cortex_debug: true,
        cpp_tools: true,
        code_lldb: true,
        observable: false,
    }
}

/// Map `Fail`/`Warn` `DoctorCheck`s to envelope `Issue`s, prefixing the check
/// name with `doctor.` and mapping the status to `error`/`warning` severity.
///
/// `Unknown` raises nothing, for the same reason it counts nothing: an issue is
/// a claim, and the CLI never observed the thing.
fn checks_to_issues(checks: &[DoctorCheck]) -> Vec<Issue> {
    checks
        .iter()
        .filter(|c| matches!(c.status, DoctorStatus::Warn | DoctorStatus::Fail))
        .map(|c| Issue {
            code: format!("doctor.{}", c.name),
            severity: if c.status == DoctorStatus::Fail {
                "error".to_string()
            } else {
                "warning".to_string()
            },
            message: c.detail.clone(),
        })
        .collect()
}

/// Render the doctor report as human-readable lines, with `<target> · <server>`
/// as the subtitle.
fn format_doctor_text(g: &GlobalArgs, report: &DoctorReport) -> Vec<String> {
    let subtitle = format!(
        "{} · {}",
        report.target_kind.as_str(),
        report.server.as_str()
    );
    style::render_report(
        g,
        "tan doctor",
        &subtitle,
        &report.checks,
        &report.summary,
        &report.next_steps,
    )
}

/// Build a checkless `DoctorReport` (summary `fail: 1`) for error paths, carrying
/// the given `target`/`server` and `next_steps` hints.
fn empty_report(
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
    next_steps: Vec<String>,
) -> DoctorReport {
    DoctorReport {
        generated_at: generated_at.to_string(),
        target_kind: target,
        server,
        summary: DoctorSummary {
            pass: 0,
            warn: 0,
            fail: 1,
        },
        checks: Vec::new(),
        next_steps,
        // These paths refuse before the prerequisite gate ever runs, so there is
        // no verdict to report -- `null`, exactly as bootstrap's own
        // pre-gate refusals report it.
        missing_prerequisites: None,
    }
}

/// Build the `DoctorFailure` (exit 4) result when `server` is not supported for
/// `target`: a `doctor.server-compatibility` issue plus an empty report.
fn unsupported_server(
    g: &GlobalArgs,
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
) -> CommandRun {
    let issues = vec![Issue {
        code: "doctor.server-compatibility".to_string(),
        severity: "error".to_string(),
        message: format!(
            "Server '{}' is not supported for '{}'.",
            server.as_str(),
            target.as_str()
        ),
    }];
    let data = empty_report(
        generated_at,
        target,
        server,
        vec!["Choose a supported server for the selected target-kind.".to_string()],
    );
    let text = if g.is_json() {
        Vec::new()
    } else {
        Theme::from_args(g).error_lines(&format!(
            "Server '{}' is not supported for target '{}'.",
            server.as_str(),
            target.as_str()
        ))
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "doctor",
            null_project(),
            data,
            issues,
            ExitCode::DoctorFailure.code(),
        )
        .to_json()
    });

    CommandRun {
        exit: ExitCode::DoctorFailure,
        text,
        json,
    }
}

/// Build the `InternalFailure` (exit 5) result for an invalid `--target-kind`
/// or `--server`: a `doctor.internal-failure` issue plus an empty report.
fn internal_failure(g: &GlobalArgs, generated_at: &str, message: String) -> CommandRun {
    let issues = vec![Issue {
        code: "doctor.internal-failure".to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    let data = empty_report(
        generated_at,
        DebugTargetKind::NativeHost,
        DebugServerKind::None,
        Vec::new(),
    );
    let text = if g.is_json() {
        Vec::new()
    } else {
        Theme::from_args(g).error_lines(&message)
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "doctor",
            null_project(),
            data,
            issues,
            ExitCode::InternalFailure.code(),
        )
        .to_json()
    });

    CommandRun {
        exit: ExitCode::InternalFailure,
        text,
        json,
    }
}

/// A `Project` with no resolved paths, used on error envelopes where the
/// workspace was never resolved.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use tan_core::bootstrap::{MissingPrerequisite, posix_refusal, windows_refusal};

    /// A tool name no host can have, so the refusal path below is reached on
    /// every platform instead of only on whichever host forgot to install
    /// something.
    const ABSENT_TOOL: &str = "tan-no-such-tool-xyz";

    /// A temp directory that removes itself on drop.
    ///
    /// Two reasons this is a guard type and not a `create_dir_all` + a trailing
    /// `remove_dir_all`: the trailing call is skipped by a failing assertion —
    /// exactly the case a regression produces, so the leak lands on every
    /// failing run and never gets cleaned up — and `Drop` runs on the unwind.
    ///
    /// Keyed on the test's own `label` as well as the pid, because the pid is
    /// process-unique, not test-unique: cargo runs a crate's tests as threads
    /// of ONE process, so two tests sharing the pid-only name would collide
    /// non-deterministically (one's `remove_dir_all` deleting the other's tree
    /// mid-assertion).
    ///
    /// `pub(crate)` so `commands::support_bundle`'s own wiring test uses this
    /// guard rather than a second copy of it — the label keying above already
    /// makes it safe across modules.
    pub(crate) struct TempTree(std::path::PathBuf);

    impl TempTree {
        pub(crate) fn new(label: &str) -> Self {
            let path =
                std::env::temp_dir().join(format!("tan-test-{label}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        pub(crate) fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// `--format json` global args, optionally rooted at a scratch project.
    /// `sdk_root: None` on purpose — the tests below assert on wiring that must
    /// hold with no SDK resolved, which is also the state the extension calls
    /// `tan doctor` in before anything is bootstrapped.
    fn json_global(project: Option<&Path>) -> GlobalArgs {
        GlobalArgs {
            project: project.map(|p| p.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: crate::cli::Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        }
    }

    /// The `data.checks[].name` list out of a serialized doctor envelope, in
    /// order — the two `run` tests below assert on the checks the COMMAND
    /// emitted, not on what a helper returns when called directly.
    fn envelope_check_names(json: &str) -> Vec<String> {
        let value: serde_json::Value = serde_json::from_str(json).expect("envelope is JSON");
        value["data"]["checks"]
            .as_array()
            .expect("data.checks is an array")
            .iter()
            .map(|c| c["name"].as_str().unwrap_or_default().to_string())
            .collect()
    }

    #[test]
    fn issues_skip_passing_checks() {
        let checks = vec![
            DoctorCheck {
                name: "ok".to_string(),
                status: DoctorStatus::Pass,
                detail: "fine".to_string(),
                fix: None,
            },
            DoctorCheck {
                name: "warned".to_string(),
                status: DoctorStatus::Warn,
                detail: "careful".to_string(),
                fix: Some("do x".to_string()),
            },
            DoctorCheck {
                name: "broken".to_string(),
                status: DoctorStatus::Fail,
                detail: "nope".to_string(),
                fix: Some("do y".to_string()),
            },
        ];
        let issues = checks_to_issues(&checks);
        assert_eq!(issues.len(), 2);
        assert_eq!(issues[0].code, "doctor.warned");
        assert_eq!(issues[0].severity, "warning");
        assert_eq!(issues[1].code, "doctor.broken");
        assert_eq!(issues[1].severity, "error");
    }

    #[test]
    fn unsupported_server_emits_doctor_failure_envelope() {
        let g = json_global(None);
        let run = unsupported_server(
            &g,
            "1970-01-01T00:00:00.000Z",
            DebugTargetKind::NativeHost,
            DebugServerKind::Jlink,
        );
        assert_eq!(run.exit, ExitCode::DoctorFailure);
        let json = run.json.expect("json envelope");
        assert!(json.contains("\"command\":\"doctor\""));
        assert!(json.contains("\"exitCode\":4"));
        assert!(json.contains("\"ok\":false"));
        assert!(json.contains("\"root\":null"));
        assert!(json.contains("doctor.server-compatibility"));
        assert!(json.contains("\"checks\":[]"));
        // Explicit `null`, not an omitted key -- a consumer can see the field
        // exists from any captured envelope, matching bootstrap's.
        assert!(json.contains("\"missingPrerequisites\":null"));
        assert!(json.contains("Server 'jlink' is not supported for 'native-host'."));
        assert!(json.contains("Choose a supported server for the selected target-kind."));
    }

    /// #208. Plain `tan doctor` reported `native-host` for every project, so a
    /// Zephyr board got a CodeLLDB verdict and no word about a probe. `TempTree`
    /// gives each case a real `board.yaml` on disk, because `default_target_kind`
    /// resolves through `read_board_model`, which reads the file.
    #[test]
    fn the_default_debug_target_comes_from_the_board_not_from_native_host() {
        let tree = TempTree::new("target-kind");
        let context = |yaml: Option<&str>| {
            let path = tree.path().join("board.yaml");
            match yaml {
                Some(body) => std::fs::write(&path, body).unwrap(),
                None => {
                    let _ = std::fs::remove_file(&path);
                }
            }
            ProjectContext {
                workspace_root: Some(tree.path().to_string_lossy().to_string()),
                sdk_root: None,
                board_yaml_path: Some(path.to_string_lossy().to_string()),
                west_cwd: None,
                python_binary: "python3".to_string(),
            }
        };

        let core =
            |os: &str| format!("schemaVersion: 2\ncores:\n  c0:\n    os: {os}\n    app: ./s\n");
        assert_eq!(
            default_target_kind(&context(Some(&core("zephyr")))),
            DebugTargetKind::ZephyrMcu
        );
        assert_eq!(
            default_target_kind(&context(Some(&core("baremetal")))),
            DebugTargetKind::BaremetalMcu
        );
        assert_eq!(
            default_target_kind(&context(Some(&core("yocto")))),
            DebugTargetKind::YoctoUserspace
        );

        // Multicore: one target must be reported, and the stated priority is
        // zephyr > baremetal > yocto.
        assert_eq!(
            default_target_kind(&context(Some(
                "schemaVersion: 2\ncores:\n  a55:\n    os: yocto\n    app: ./a\n  m33:\n    os: zephyr\n    app: ./m\n"
            ))),
            DebugTargetKind::ZephyrMcu,
            "a board with both must not be reported as a Linux userspace target"
        );

        // No board.yaml on disk -- the one case `native-host` is genuinely right,
        // and the directory alp-sdk's bootstrap tells a customer to run `tan
        // doctor` from.
        assert_eq!(
            default_target_kind(&context(None)),
            DebugTargetKind::NativeHost
        );
    }

    /// The unit test above proves `default_target_kind` computes the right
    /// answer; this one proves `run` USES it. Without it, replacing the call
    /// with a bare `DebugTargetKind::NativeHost` left the suite green -- the
    /// defect #208 reports would have been fully reintroduced with no test
    /// noticing. Drives the whole command and reads the envelope, so it covers
    /// the flag-precedence branch too.
    #[test]
    fn run_reports_the_derived_target_in_the_envelope_and_an_explicit_flag_still_wins() {
        let tree = TempTree::new("run-target-kind");
        std::fs::write(
            tree.path().join("board.yaml"),
            "schemaVersion: 2\ncores:\n  m33:\n    os: zephyr\n    app: ./src\n",
        )
        .unwrap();
        let g = json_global(Some(tree.path()));
        let target_of = |args: &DoctorArgs| {
            let json = run(&g, args).json.expect("json envelope");
            let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
            value["data"]["targetKind"]
                .as_str()
                .unwrap_or_else(|| panic!("no data.targetKind: {json}"))
                .to_string()
        };

        assert_eq!(
            target_of(&DoctorArgs {
                target_kind: None,
                server: None,
                build: false,
                fix: false,
            }),
            "zephyr-mcu",
            "a zephyr board must not be reported as native-host (#208)"
        );

        // An explicit flag is still absolute -- the derivation only fills a gap.
        assert_eq!(
            target_of(&DoctorArgs {
                target_kind: Some("native-host".to_string()),
                server: None,
                build: false,
                fix: false,
            }),
            "native-host"
        );
    }

    /// The derived target must bring a server its own compatibility table
    /// accepts, or every plain `tan doctor` in a real project turns into a
    /// `serverCompatibility` failure — the regression the derivation would
    /// otherwise introduce.
    #[test]
    fn every_derivable_target_has_a_supported_default_server() {
        for target in [
            DebugTargetKind::ZephyrMcu,
            DebugTargetKind::BaremetalMcu,
            DebugTargetKind::YoctoUserspace,
            DebugTargetKind::NativeHost,
        ] {
            let server = server_choices_for_target(target)
                .first()
                .copied()
                .unwrap_or(DebugServerKind::None);
            assert!(
                is_server_supported_for_target(target, server),
                "{target:?} defaults to an unsupported {server:?}"
            );
        }
        // native-host still resolves to `none`, byte-identical to the old default.
        assert_eq!(
            server_choices_for_target(DebugTargetKind::NativeHost)
                .first()
                .copied(),
            Some(DebugServerKind::None)
        );
    }

    #[test]
    fn the_plain_doctor_report_carries_both_io_built_checks() {
        // `run` resolves a real project and shells `git`, so the two appends
        // were unreachable from a test and dropping either left the suite
        // green -- proven by mutation. Asserting on the CHECK NAMES, not their
        // statuses, because both statuses depend on the host.
        let tree = TempTree::new("assemble");
        let context = tan_core::DebugWorkspaceContext {
            generated_at: "1970-01-01T00:00:00.000Z".to_string(),
            workspace_root: Some("/work/proj".to_string()),
            // A real directory, so `append_sdk_provenance` reaches its check
            // instead of its `sdk_root.is_none()` early return. It is not a git
            // checkout and has no metadata/sdk_version.yaml, which is a case
            // that function already handles.
            sdk_root: Some(tree.path().to_string_lossy().to_string()),
            board_yaml_path: Some("/work/proj/board.yaml".to_string()),
            west_cwd: None,
            python_binary: "python3".to_string(),
            board_yaml_exists: true,
            project_selected: true,
            debugger_extensions: standalone_debugger_extensions(),
        };
        let runtime = tan_core::DebugRuntimeCapabilities {
            jlink_executable: None,
            open_ocd_executable: None,
            pyocd_executable: None,
            gdb_executable: None,
            lldb_executable: Some("lldb".to_string()),
        };

        let report = assemble_doctor_report(
            &context,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime,
            Vec::new(),
        );
        let names: Vec<&str> = report.checks.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains(&"hostPrerequisites"), "{names:?}");
        assert!(names.contains(&"sdkProvenance"), "{names:?}");
        // Every check is counted EXCEPT the unobservable extension ones, which
        // are counted nowhere by design (#102) -- otherwise the exit code
        // (`fail > 0`) and the rendered summary disagree with the list they
        // summarize.
        let counted = report.summary.pass + report.summary.warn + report.summary.fail;
        let unknown = report
            .checks
            .iter()
            .filter(|c| c.status == DoctorStatus::Unknown)
            .count();
        assert_eq!(counted as usize + unknown, report.checks.len());
        // The retired `python` check must not come back alongside it.
        assert!(!names.contains(&"python"), "{names:?}");
    }

    #[test]
    fn host_prerequisites_probes_the_host_and_lands_as_one_counted_check() {
        // Wiring only. The three OUTCOMES (pass, tool-naming refusal, Python
        // floor) are pinned in `tan_core::bootstrap::prerequisites` against a
        // supplied verdict, because the outcome HERE depends on the test host's
        // PATH -- on a clean host only the `Pass` branch ever executes, which is
        // what made the previous version of this test mutation-dead.
        let mut report = empty_report(
            "1970-01-01T00:00:00.000Z",
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            Vec::new(),
        );
        report.summary.fail = 0;
        // No SDK root resolves -> the fallback tool list, still a real probe.
        append_host_prerequisites(&mut report, None);

        assert_eq!(report.checks.len(), 1);
        let check = &report.checks[0];
        assert_eq!(check.name, "hostPrerequisites");
        assert_eq!(
            report.summary.pass + report.summary.warn + report.summary.fail,
            1,
            "the appended check must be counted exactly once"
        );
        assert!(
            check
                .detail
                .contains("facts from tan's built-in fallback list"),
            "a doctor run with no SDK must say which list it checked: {}",
            check.detail
        );
        // No SDK resolved is NOT the same fact as a manifest that resolved and
        // was refused -- the two used to render byte-identically.
        assert!(
            !check
                .detail
                .contains(tan_core::bootstrap::BOOTSTRAP_MANIFEST_REJECTED_PREFIX),
            "{}",
            check.detail
        );
    }

    #[test]
    fn a_skewed_bootstrap_manifest_reaches_the_check_instead_of_being_swallowed() {
        // `load_facts` refuses a present-but-skewed manifest with a message
        // naming the version; doctor used to `.ok()` it away and report a `Pass`
        // indistinguishable from "no SDK resolved". Written against a real SDK
        // root on disk so it exercises `load_facts` itself, not a hand-made
        // `Err`.
        let root = TempTree::new("skew");
        let metadata = root.path().join("metadata");
        std::fs::create_dir_all(&metadata).unwrap();
        std::fs::write(metadata.join("bootstrap.json"), r#"{"schemaVersion": 99}"#).unwrap();

        let mut report = empty_report(
            "1970-01-01T00:00:00.000Z",
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            Vec::new(),
        );
        report.summary.fail = 0;
        append_host_prerequisites(&mut report, Some(&root.path().to_string_lossy()));

        let check = &report.checks[0];
        assert!(
            check.detail.contains("schemaVersion 99"),
            "the refusal reason must survive into the report: {}",
            check.detail
        );
        // Never a bare Pass: the host list still got checked (against the
        // fallback), but the manifest was read and refused, and `tan bootstrap`
        // will exit 4 on the same message.
        assert_ne!(check.status, DoctorStatus::Pass);
        let fix = check.fix.clone().expect("a refused manifest is actionable");
        // Host-independent proof that this appender goes through
        // `append_doctor_check`: the check is non-Pass on every host here, so
        // its fix has to be in the field the extension renders as a Fix button.
        // `next_steps` was computed by the report builder BEFORE this append.
        assert_eq!(report.next_steps, vec![fix]);
    }

    #[test]
    fn a_refused_host_carries_the_structured_pairs_and_this_platforms_wording() {
        // The two tests above run the gate on a CLEAN host, where `refusal` is
        // always `None` -- so `refusal.map(|f| f.missing).unwrap_or_default()`
        // could be replaced by `Vec::new()` and nothing failed, leaving
        // `missingPrerequisites` permanently `null` even on a `Fail`. That is
        // the exact "consumer must re-parse the prose" defect this branch
        // removes, so the refusal has to be FORCED rather than waited for.
        //
        // Forced through a manifest, not a hand-made `PrereqFailure`: this is
        // the one test that drives the whole `load_facts` -> probe ->
        // `apply_prerequisite_check` chain the binary runs.
        let root = TempTree::new("refused");
        let metadata = root.path().join("metadata");
        std::fs::create_dir_all(&metadata).unwrap();
        // The REAL vendored manifest with only the two prerequisite lists
        // repointed, so the fixture stays schema-valid as the manifest grows
        // (a manifest tan REFUSED would take a different path entirely -- that
        // one is `a_skewed_bootstrap_manifest_reaches_the_check_...` above).
        let mut doc: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../contract/fixtures/bootstrap/manifest.json"
        ))
        .unwrap();
        doc["prerequisites"]["posix"] = serde_json::json!([ABSENT_TOOL]);
        doc["prerequisites"]["windows"] = serde_json::json!([ABSENT_TOOL]);
        std::fs::write(metadata.join("bootstrap.json"), doc.to_string()).unwrap();

        let mut report = empty_report(
            "1970-01-01T00:00:00.000Z",
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            Vec::new(),
        );
        report.summary.fail = 0;
        append_host_prerequisites(&mut report, Some(&root.path().to_string_lossy()));

        assert_eq!(report.checks[0].name, "hostPrerequisites");
        assert_eq!(report.checks[0].status, DoctorStatus::Fail);
        // The headline field of the whole change: the machine half of the
        // verdict, reaching the report from the SAME refusal the prose came
        // from. `command: null` because tan knows no install one-liner for an
        // unlisted tool -- and prose in that field is a button that fails.
        assert_eq!(
            report.missing_prerequisites,
            Some(vec![MissingPrerequisite {
                tool: ABSENT_TOOL.to_string(),
                command: None,
            }])
        );
        // Host wiring: the two refusals word themselves differently (Windows
        // lists a per-tool hint line and a reopen-PowerShell tail), so
        // hard-coding either side -- or letting the host detection decay to a
        // constant -- shows up here rather than as a silently POSIX-shaped
        // report on a Windows host. The install map is this host's own, from the
        // manifest that was just written to disk: passing the wrong OS's would
        // change the Windows hint lines and fail the same assertion.
        let host = HostOs::detect(std::env::consts::OS);
        let facts = resolve_bootstrap_facts(Some(&root.path().to_string_lossy())).0;
        let install = facts.install.for_host(host);
        let expected = if host == HostOs::Windows {
            windows_refusal(&[ABSENT_TOOL], install)
        } else {
            posix_refusal(&[ABSENT_TOOL], install)
        };
        assert!(
            report.checks[0]
                .detail
                .starts_with(&expected.lines.join(" ")),
            "{}",
            report.checks[0].detail
        );
        assert!(
            report.checks[0]
                .detail
                .ends_with("(facts from alp-sdk metadata/bootstrap.json)"),
            "{}",
            report.checks[0].detail
        );
    }

    #[test]
    fn the_doctor_command_emits_the_host_prerequisite_check() {
        // The wiring seam itself: `run` -> `assemble_doctor_report`. Swapping
        // that one call for the bare `build_doctor_report` ships a binary with
        // no `hostPrerequisites` check and `"missingPrerequisites": null` --
        // the whole feature absent -- and every test above still passes,
        // because they call the helper directly. Asserting on the emitted
        // ENVELOPE is what closes it.
        let tree = TempTree::new("run-plain");
        let g = json_global(Some(tree.path()));
        let run = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: false,
                fix: false,
            },
        );
        let json = run.json.expect("json envelope");
        let names = envelope_check_names(&json);
        assert!(names.iter().any(|n| n == "hostPrerequisites"), "{names:?}");
        // Same seam, the host-environment appends: dropping
        // `append_host_environment` from `assemble_doctor_report` ships a
        // binary with none of the three checks and every other test still
        // green, because they drive the pure functions directly. The ENVELOPE
        // is what closes it. Names only -- the statuses are this host's.
        assert!(
            names.iter().any(|n| n == "zephyrSdkAvailableForHost"),
            "{names:?}"
        );
        assert!(names.iter().any(|n| n == "homePath"), "{names:?}");
        assert_eq!(
            names.iter().any(|n| n == "longPaths"),
            cfg!(windows),
            "longPaths is Windows-only: {names:?}"
        );
        // tan-cli#160: this used to appear ONLY under `--build`, reached only
        // after a full project existed -- nothing before `tan build` itself
        // ever said "toolchain" on the alp-sdk#855 fresh-host run. Dropping
        // `append_zephyr_sdk_toolchain` from `assemble_doctor_report` would
        // leave every OTHER test green (they drive the pure function
        // directly); only the emitted envelope catches it.
        assert!(names.iter().any(|n| n == "zephyrSdk"), "{names:?}");
    }

    #[test]
    fn plain_doctor_and_build_zephyr_sdk_checks_never_disagree_in_status() {
        // The exact contradiction tan-cli#160 named: `zephyrSdkAvailableForHost`
        // (`[+]`) sitting beside a hard-failing `zephyrSdk` on the SAME run
        // would be worse than either alone. Both checks are now driven by the
        // identical `crate::toolchain::zephyr_sdk_detected()` probe under
        // `--build`'s `zephyrSdk`, and plain doctor's `zephyrSdk` uses the SAME
        // probe -- so the two run modes cannot report a different verdict for
        // "is the Zephyr SDK installed here" no matter what this host's real
        // state is.
        let tree = TempTree::new("doctor-plain-vs-build-zephyrsdk-agree");
        let g = json_global(Some(tree.path()));
        let plain = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: false,
                fix: false,
            },
        );
        let build = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        );
        let plain_status = envelope_check_status(&plain.json.expect("json envelope"), "zephyrSdk");
        let build_status = envelope_check_status(&build.json.expect("json envelope"), "zephyrSdk");
        assert_eq!(
            plain_status, build_status,
            "plain vs --build zephyrSdk status disagree"
        );
    }

    /// One check's `detail` out of a serialized doctor envelope.
    fn envelope_check_detail(json: &str, name: &str) -> String {
        let value: serde_json::Value = serde_json::from_str(json).expect("envelope is JSON");
        value["data"]["checks"]
            .as_array()
            .expect("data.checks is an array")
            .iter()
            .find(|c| c["name"] == name)
            .unwrap_or_else(|| panic!("no `{name}` check in {json}"))["detail"]
            .as_str()
            .unwrap_or_default()
            .to_string()
    }

    /// One check's `status` out of a serialized doctor envelope.
    fn envelope_check_status(json: &str, name: &str) -> String {
        let value: serde_json::Value = serde_json::from_str(json).expect("envelope is JSON");
        value["data"]["checks"]
            .as_array()
            .expect("data.checks is an array")
            .iter()
            .find(|c| c["name"] == name)
            .unwrap_or_else(|| panic!("no `{name}` check in {json}"))["status"]
            .as_str()
            .unwrap_or_default()
            .to_string()
    }

    /// Every `(name, detail)` pair of a serialized doctor envelope, in order —
    /// the machine form of the rendered report a user reads.
    fn envelope_check_rows(json: &str) -> Vec<(String, String)> {
        let value: serde_json::Value = serde_json::from_str(json).expect("envelope is JSON");
        value["data"]["checks"]
            .as_array()
            .expect("data.checks is an array")
            .iter()
            .map(|c| {
                (
                    c["name"].as_str().unwrap_or_default().to_string(),
                    c["detail"].as_str().unwrap_or_default().to_string(),
                )
            })
            .collect()
    }

    #[test]
    fn plain_doctor_probes_the_build_environment_and_tells_two_hosts_apart() {
        // #100's headline: plain `tan doctor` printed BYTE-IDENTICAL output
        // across four materially different host states, including the pair that
        // differed by whether the documented example build worked at all. It
        // ran no build-environment probe whatsoever.
        //
        // Differentiated on the `workspace` check specifically, because that is
        // a fact ONLY the folded-in preflight can see. Picking `sdk` or
        // `boardYaml` instead would prove nothing: the debug report already
        // carries `sdkRoot`/`boardYaml`, so those two states differ with or
        // without the fold, and the test would stay green through the mutation
        // it exists to catch. Deleting the `prepend_doctor_checks(...,
        // preflight)` call in `assemble_doctor_report` removes the `workspace`
        // check entirely and `envelope_check_detail` panics.
        let without = TempTree::new("plain-no-workspace");
        let with = TempTree::new("plain-with-workspace");
        // The one difference between the two hosts: a west workspace marker.
        std::fs::create_dir_all(with.path().join(".west")).unwrap();

        let args = || DoctorArgs {
            target_kind: None,
            server: None,
            build: false,
            fix: false,
        };
        let a = run(&json_global(Some(without.path())), &args())
            .json
            .expect("json envelope");
        let b = run(&json_global(Some(with.path())), &args())
            .json
            .expect("json envelope");

        // Absolute: the state that HAS a workspace says so, naming its own dir.
        // Matched on the leaf name — the reported path is separator-normalized
        // (forward slashes even on Windows), so the raw `PathBuf` string does
        // not appear verbatim.
        let with_detail = envelope_check_detail(&b, "workspace");
        let leaf = with
            .path()
            .file_name()
            .expect("temp dir has a name")
            .to_string_lossy()
            .into_owned();
        assert!(
            with_detail.starts_with("Zephyr workspace at") && with_detail.ends_with(&leaf),
            "{with_detail}"
        );
        // Relative, so the assertion holds on a host with an ambient
        // `$ZEPHYR_BASE` (which would give state A a workspace of its own — a
        // different one, never state B's).
        assert_ne!(
            envelope_check_detail(&a, "workspace"),
            with_detail,
            "plain `tan doctor` must not report the same workspace verdict for \
             a host with a west workspace and one without"
        );
        // And the reports as a whole are not byte-identical, which is the
        // symptom the issue actually filed.
        assert_ne!(envelope_check_rows(&a), envelope_check_rows(&b));

        // The preflight-only names are all present — the fold is the whole
        // preflight, not one cherry-picked row.
        let names = envelope_check_names(&b);
        for name in ["sdk", "workspace", "westResolved"] {
            assert!(names.contains(&name.to_string()), "{name} in {names:?}");
        }
        // ...minus the preflight's own `boardYaml`, which collides by NAME with
        // the debug report's. Exactly one is emitted, and it is the
        // project-selection-aware one (`--project` is set here but the file
        // does not exist, so it is the hard-fail wording, not the preflight's
        // "run `tan init`").
        assert_eq!(
            names.iter().filter(|n| *n == "boardYaml").count(),
            1,
            "{names:?}"
        );
        assert!(
            !envelope_check_detail(&b, "boardYaml").contains("tan init"),
            "the surviving boardYaml must be the debug report's"
        );
    }

    #[test]
    fn plain_doctor_does_not_fail_at_a_checkout_root_with_no_project_selected() {
        // #100(b) at the command level: `bootstrap` prints `tan doctor` as the
        // customer's very next command, run from the SDK checkout root it just
        // set up — which has no `board.yaml` and needs none. With no
        // `--project`/`--board-yaml`, `boardYaml` must warn, not fail.
        //
        // No `--project` is the whole point, so this run resolves against the
        // test process's own cwd — the `tan-cli` package root, which carries no
        // `board.yaml` (nor does the repo root above it), the same shape as an
        // alp-sdk checkout root.
        assert!(!project_selected(&json_global(None)));
        assert!(project_selected(&json_global(Some(Path::new("/proj")))));
        let json = run(
            &json_global(None),
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: false,
                fix: false,
            },
        )
        .json
        .expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let board = value["data"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "boardYaml")
            .unwrap_or_else(|| panic!("{json}"));
        assert_eq!(board["status"], "warn", "{json}");
        // ...and it raises a `warning`, not an `error`, in the envelope a
        // consumer reads.
        let issue = value["issues"]
            .as_array()
            .unwrap()
            .iter()
            .find(|i| i["code"] == "doctor.boardYaml")
            .unwrap_or_else(|| panic!("{json}"));
        assert_eq!(issue["severity"], "warning");
    }

    #[test]
    fn plain_doctor_reports_the_vscode_extension_check_as_unknown() {
        // #102 at the command level: on a headless host the standalone binary
        // printed `[+] codeLLDBExtension  vadimcn.vscode-lldb is installed.`
        // and counted it among "5 passed". Reverting
        // `standalone_debugger_extensions` to `observable: true` fails here.
        let tree = TempTree::new("plain-unknown-ext");
        let json = run(
            &json_global(Some(tree.path())),
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: false,
                fix: false,
            },
        )
        .json
        .expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let check = value["data"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "codeLLDBExtension")
            .unwrap_or_else(|| panic!("{json}"));
        assert_eq!(check["status"], "unknown", "{json}");
        assert!(
            !check["detail"]
                .as_str()
                .unwrap_or_default()
                .contains("is installed."),
            "{json}"
        );
        // Raises no issue and appears in no next step: the CLI observed nothing.
        assert!(
            !value["issues"]
                .as_array()
                .unwrap()
                .iter()
                .any(|i| i["code"] == "doctor.codeLLDBExtension"),
            "{json}"
        );
    }

    #[test]
    fn doctor_build_envelope_keys_are_unchanged() {
        // The live cross-repo contract. `alp-sdk-vscode` shells ONLY
        // `tan doctor --build` (`src/toolchain.ts:280`, `:353`) and parses this
        // envelope; plain `tan doctor` has no consumer there, which is what
        // makes the fold above safe. So plain doctor's shape may move and this
        // one may NOT. Renaming any key below — `osSet`, `nextSteps`,
        // `missingPrerequisites`, `exitCode`, … — fails here.
        let tree = TempTree::new("build-envelope-keys");
        let json = run(
            &json_global(Some(tree.path())),
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        )
        .json
        .expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");

        let keys = |v: &serde_json::Value| -> Vec<String> {
            let mut k: Vec<String> = v
                .as_object()
                .expect("a JSON object")
                .keys()
                .cloned()
                .collect();
            k.sort();
            k
        };
        assert_eq!(
            keys(&value),
            ["command", "data", "exitCode", "issues", "ok", "project"]
        );
        assert_eq!(keys(&value["project"]), ["boardYaml", "root"]);
        assert_eq!(
            keys(&value["data"]),
            [
                "checks",
                "generatedAt",
                "missingPrerequisites",
                "nextSteps",
                "osSet",
                "schemaVersion",
                "summary",
            ]
        );
        assert_eq!(keys(&value["data"]["summary"]), ["fail", "pass", "warn"]);
        assert_eq!(value["command"], "doctor");

        // Every check keeps `{name, status, detail}` (+ optional `fix`, +
        // optional `version` since tan-cli#123), and no `--build` check is ever
        // `unknown` — the new status is produced only by the VS Code
        // extension-presence set, which this mode does not emit. A consumer
        // that only knows pass/warn/fail must stay correct here.
        for check in value["data"]["checks"].as_array().expect("checks array") {
            for key in ["name", "status", "detail"] {
                assert!(check.get(key).is_some(), "{key} missing from {check}");
            }
            assert!(
                keys(check)
                    .iter()
                    .all(|k| ["detail", "fix", "name", "status", "version"].contains(&k.as_str())),
                "unexpected key in {check}"
            );
            assert_ne!(check["status"], "unknown", "{check}");
        }
    }

    #[test]
    fn doctor_build_wires_git_python_dtc_and_gperf_into_the_real_envelope() {
        // Integration proof for tan-cli#120/#123's actual wiring (the pure
        // logic is unit-tested exhaustively in `tan_core::build_readiness`;
        // this is the ONE test that proves `BuildToolProbe`'s new fields are
        // actually populated by `run_build_readiness`, not just accepted by
        // its signature). No `board.yaml` -> `os_set` defaults to all three
        // backends, so `dtc`/`gperf` (Zephyr-gated) are reachable too.
        let tree = TempTree::new("build-new-checks-wired");
        let json = run(
            &json_global(Some(tree.path())),
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        )
        .json
        .expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let names: Vec<&str> = value["data"]["checks"]
            .as_array()
            .expect("checks array")
            .iter()
            .map(|c| c["name"].as_str().unwrap_or_default())
            .collect();
        for expected in ["git", "python", "dtc", "gperf"] {
            assert!(names.contains(&expected), "{expected} missing: {names:?}");
        }
    }

    #[test]
    fn doctor_build_reports_westresolved_version_from_the_resolved_venv_not_bare_path() {
        // tan-cli#123's exact bug, guarded end-to-end: `westResolved`'s
        // reported version must come from `resolve_west_resolved_version`'s
        // OWN `west_program` lookup, never a re-probe of a bare-PATH `west`
        // (`BuildToolProbe::west_version`) that can be a different binary
        // entirely. Before this test, deleting `resolve_west_resolved_version`'s
        // `&west` argument (regressing it to a bare `"west"`) or deleting the
        // `report.check_versions.insert("westResolved", ...)` call in
        // `run_build_readiness` both left the full suite green.
        //
        // Precondition, not an assumption: a bare-PATH `west` that itself
        // reports a version would make the assertion below pass under BOTH
        // the correct code and the `tool_version("west")` regression, proving
        // nothing. Skip on the rare host where one is globally installed --
        // `west` normally lives only inside a bootstrapped venv (`venv.rs`'s
        // module doc).
        if crate::util::tool_version("west").is_some() {
            return;
        }

        let tree = TempTree::new("west-resolved-version");
        let (bin_sub, west_name) = if cfg!(windows) {
            ("Scripts", "west.exe")
        } else {
            ("bin", "west")
        };
        let bin_dir = tree.path().join(".venv").join(bin_sub);
        std::fs::create_dir_all(&bin_dir).unwrap();
        let west_path = bin_dir.join(west_name);
        // The stand-in only has to print a dotted number for `--version`.
        // On unix that is a `sh` script, NOT a copy of a real binary: copying
        // rustup's `cargo` out of its toolchain dir breaks the `$ORIGIN/../lib`
        // RPATH it resolves `libstd-*.so` through, so the copy fails to start
        // and `tool_version` reports `None` (green on Windows and macOS, red on
        // Linux). On Windows the venv west must be a real `west.exe` -- a
        // script cannot carry that name -- and a Rust binary there links std
        // statically, so the copy does run.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::write(&west_path, "#!/bin/sh\necho 'West version: v99.98.97'\n")
                .expect("write the west stand-in script");
            let mut perms = std::fs::metadata(&west_path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&west_path, perms).unwrap();
        }
        #[cfg(windows)]
        std::fs::copy(env!("CARGO"), &west_path).expect("copy cargo as a west stand-in");
        let expected = crate::util::tool_version(&west_path.to_string_lossy())
            .expect("the copied cargo binary reports its own --version");

        let json = run(
            &json_global(Some(tree.path())),
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        )
        .json
        .expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let west_resolved = value["data"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "westResolved")
            .unwrap_or_else(|| panic!("westResolved missing: {json}"));
        assert_eq!(
            west_resolved["version"].as_str(),
            Some(expected.as_str()),
            "westResolved must carry the RESOLVED venv west's version, not a \
             bare-PATH re-probe: {json}"
        );
    }

    #[test]
    fn doctor_build_does_not_repeat_the_host_environment_checks() {
        // The deliberate NEGATIVE half of the placement decision (#81's "one
        // fact, one check" trap). `--build` already carries a `zephyrSdk` probe
        // -- "is an SDK installed here" -- and `zephyrSdkAvailableForHost`
        // answers the opposite question. Adding these there later, by reflex,
        // would report the SDK story twice under two names; this fails if
        // someone does.
        let tree = TempTree::new("run-build-no-hostenv");
        let g = json_global(Some(tree.path()));
        let run = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        );
        let names = envelope_check_names(&run.json.expect("json envelope"));
        for name in ["zephyrSdkAvailableForHost", "longPaths", "homePath"] {
            assert!(!names.contains(&name.to_string()), "{name} in {names:?}");
        }
    }

    #[test]
    fn doctor_build_reports_the_project_preflight_before_the_host_tool_probes() {
        // Same seam, `--build` side: deleting the `prepend_doctor_checks(...,
        // probe_build_preflight(...))` call leaves a report that probes host
        // tools but never asks whether a build could start at all, and no test
        // noticed. The ORDER is the assertion because prepending is the point:
        // "no SDK selected" has to reach the user ahead of "west not found",
        // which is merely its consequence.
        let tree = TempTree::new("run-build");
        let g = json_global(Some(tree.path()));
        let run = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        );
        let json = run.json.expect("json envelope");
        let names = envelope_check_names(&json);
        let west = names
            .iter()
            .position(|n| n == "west")
            .unwrap_or_else(|| panic!("{names:?}"));
        for name in ["sdk", "boardYaml", "workspace"] {
            let at = names
                .iter()
                .position(|n| n == name)
                .unwrap_or_else(|| panic!("{name} missing from {names:?}"));
            assert!(at < west, "{name} must precede west: {names:?}");
        }
    }

    #[test]
    fn doctor_build_warns_that_it_fell_back_from_a_refused_manifest() {
        // `--build` resolved the bootstrap facts and threw the rejection message
        // away, so a version-skewed or unparseable `metadata/bootstrap.json`
        // made it substitute tan's compiled-in install commands with NOTHING on
        // the wire -- no check, no issue. `sdkProvenance` cannot cover it: it
        // reports the git short-commit and `metadata/sdk_version.yaml`, never the
        // manifest. And this is the mode alp-sdk-vscode shells for
        // `runToolchainFix`, so on a future `schemaVersion: 2` SDK its Fix button
        // would run a stale command silently.
        //
        // Driven through `run` against a real skewed manifest on disk, not a
        // hand-made `Err`: the whole defect was a discarded return value one
        // level up from `load_facts`, which a direct call could not see.
        let tree = TempTree::new("run-build-skew");
        let sdk = TempTree::new("run-build-skew-sdk");
        let metadata = sdk.path().join("metadata");
        std::fs::create_dir_all(&metadata).unwrap();
        std::fs::write(metadata.join("bootstrap.json"), r#"{"schemaVersion": 99}"#).unwrap();
        // `scripts/alp_project.py` is what makes `--sdk-root` RESOLVE
        // (`project::contains_loader_script`); without it `context.sdk_root` is
        // `None`, no manifest is read, and this test would pass for the wrong
        // reason on a change that reintroduced the discard.
        let scripts = sdk.path().join("scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        std::fs::write(scripts.join("alp_project.py"), "").unwrap();

        let g = GlobalArgs {
            sdk_root: Some(sdk.path().to_string_lossy().into_owned()),
            ..json_global(Some(tree.path()))
        };
        let skewed = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        );
        let json = skewed.json.expect("json envelope");
        let value: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let check = value["data"]["checks"]
            .as_array()
            .expect("data.checks is an array")
            .iter()
            .find(|c| c["name"] == "bootstrapManifest")
            .unwrap_or_else(|| panic!("--build must report the refused manifest: {json}"));

        // `Warn`, the same downgrade plain `doctor` applies: the fallback
        // commands are still real, and a read-only report is not the thing that
        // should exit `DoctorFailure` over a manifest.
        assert_eq!(check["status"], "warn");
        // Named verbatim -- a warning that does not say WHICH version was
        // refused is not actionable, and it is the message `tan bootstrap` exits
        // 4 on.
        let detail = check["detail"].as_str().unwrap_or_default();
        assert!(
            detail.starts_with(BOOTSTRAP_MANIFEST_REJECTED_PREFIX)
                && detail.contains("schemaVersion 99"),
            "{detail}"
        );
        // Shared with plain `doctor`'s tail through one constant, so the two
        // modes cannot word one verdict two ways.
        assert_eq!(check["fix"], BOOTSTRAP_MANIFEST_REJECTED_FIX);

        // A manifest that parses raises nothing -- otherwise the warning would
        // fire on every healthy SDK and mean nothing.
        std::fs::write(
            metadata.join("bootstrap.json"),
            include_str!("../../../../contract/fixtures/bootstrap/manifest.json"),
        )
        .unwrap();
        let clean = run(
            &g,
            &DoctorArgs {
                target_kind: None,
                server: None,
                build: true,
                fix: false,
            },
        );
        let clean_json = clean.json.expect("json envelope");
        let names = envelope_check_names(&clean_json);
        assert!(
            !names.contains(&"bootstrapManifest".to_string()),
            "{names:?}"
        );
    }

    #[cfg(windows)]
    #[test]
    fn host_arch_agrees_with_what_windows_itself_reports() {
        // An INDEPENDENT oracle for the `IsWow64Process2` result: Windows sets
        // `PROCESSOR_ARCHITEW6432` to the NATIVE arch in an emulated/WOW64
        // process and leaves it unset in a native one, where
        // `PROCESSOR_ARCHITECTURE` is already native. Nothing in `host_arch`
        // reads either variable, so this is a real cross-check and not a
        // restatement — swapping the ARM64/AMD64 arms of
        // `arch_for_image_file_machine` fails here on any Windows host.
        //
        // What it CANNOT catch on an x64 host is `host_arch()` being replaced
        // wholesale by `std::env::consts::ARCH`, because the two agree here by
        // construction; that mutation is caught by
        // `an_emulated_process_resolves_the_machines_arch_not_its_own` in
        // tan-core, which drives the mapping over both machine types.
        let native = std::env::var("PROCESSOR_ARCHITEW6432")
            .or_else(|_| std::env::var("PROCESSOR_ARCHITECTURE"))
            .unwrap_or_default()
            .to_ascii_uppercase();
        let expected = match native.as_str() {
            "ARM64" => "aarch64",
            "AMD64" => "x86_64",
            // x86 / IA64 / an unset environment: tan has no token, and the
            // fallback is whatever it was built for. Nothing to assert.
            _ => return,
        };
        assert_eq!(
            host_arch(),
            expected,
            "host_arch must report the machine Windows reports ({native})"
        );
    }

    #[test]
    fn bootstrap_fix_check_surfaces_json_mode_failure_instead_of_silence() {
        // Simulates `bootstrap`'s JSON-mode CommandRun: empty `text`, the
        // failure captured only inside the serialized envelope. Before the fix
        // `let _ = bootstrap::run(...)` discarded this whole value, so a
        // failing `--fix` was invisible. The message is a real one bootstrap
        // can emit now that it runs west natively -- the old fixture pinned
        // "bootstrap.sh reported a failure", which no code path produces since
        // the bash dependency was dropped (#49).
        let envelope = r#"{"command":"bootstrap","ok":false,"exitCode":1,"project":{"root":null,"boardYaml":null},"data":{},"issues":[{"code":"bootstrap.failed","severity":"error","message":"west update failed: fatal: unable to access remote repository"}]}"#;
        let run = CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: Vec::new(),
            json: Some(envelope.to_string()),
        };
        let check = bootstrap_fix_doctor_check(&run);
        assert_eq!(check.status, DoctorStatus::Fail);
        assert!(check.detail.contains("west update failed"));
        assert!(check.fix.is_some());
    }

    #[test]
    fn bootstrap_fix_check_passes_on_success() {
        let run = CommandRun {
            exit: ExitCode::Success,
            text: vec!["bootstrap: complete.".to_string()],
            json: None,
        };
        let check = bootstrap_fix_doctor_check(&run);
        assert_eq!(check.status, DoctorStatus::Pass);
        assert!(check.fix.is_none());
    }
}
