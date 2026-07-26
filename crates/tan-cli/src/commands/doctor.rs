// SPDX-License-Identifier: Apache-2.0
//! `tan doctor` — diagnose debug readiness for a target/server combination.
//!
//! Mirrors the TypeScript `runDoctorCommand`: resolve the project context,
//! probe runtime capabilities (binaries on PATH), and build a doctor report.
//! Exit code is `doctorFailure` (4) when any check fails, `internalFailure`
//! (5) on an invalid `--target-kind`/`--server`, and `success` (0) otherwise.

use std::path::Path;

use tan_core::{
    BuildOs, BuildToolProbe, DebugServerKind, DebugTargetKind, DebuggerExtensionsState,
    DoctorCheck, DoctorReport, DoctorStatus, DoctorSummary, HostEnvProbe, ProjectContext,
    append_doctor_check, board_os_set, build_doctor_report, build_readiness_report,
    collect_runtime_capabilities_from_commands, create_debug_workspace_context,
    host_environment_checks, is_server_supported_for_target, parse_board_model, parse_server_kind,
    parse_target_kind, prepend_doctor_checks,
};

use tan_core::bootstrap::{apply_prerequisite_check, doctor_prerequisite_check, fallback_facts};

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
    let context = resolve_context(g, &generated_at);

    // Resolved project paths are reported on the success path only (mirrors TS).
    let resolved_project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let target = match parse_target_kind(args.target_kind.as_deref()) {
        Ok(value) => value,
        Err(message) => return internal_failure(g, &generated_at, message),
    };
    let server = match parse_server_kind(args.server.as_deref()) {
        Ok(value) => value,
        Err(message) => return internal_failure(g, &generated_at, message),
    };

    if !is_server_supported_for_target(target, server) {
        return unsupported_server(g, &generated_at, target, server);
    }

    let runtime = collect_runtime_capabilities_from_commands(command_on_path);
    let report = assemble_doctor_report(&context, target, server, &runtime);

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

    let probe = BuildToolProbe {
        west: command_on_path("west"),
        cmake: command_on_path("cmake"),
        ninja: command_on_path("ninja"),
        bitbake: command_on_path("bitbake"),
        zephyr_sdk: crate::toolchain::zephyr_sdk_detected(),
        bmaptool: command_on_path("bmaptool"),
        dd: command_on_path("dd"),
        is_linux: cfg!(target_os = "linux"),
        is_windows: cfg!(windows),
    };

    let mut report = build_readiness_report(generated_at.to_string(), os_set, &probe);
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

/// The plain `tan doctor` report: the pure check set, plus the checks that can
/// only be built with IO — the host prerequisite gate, the host-environment
/// probes, and the SDK provenance.
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
) -> DoctorReport {
    let mut report = build_doctor_report(context, target, server, runtime);
    append_host_prerequisites(&mut report, context.sdk_root.as_deref());
    append_host_environment(&mut report);
    append_sdk_provenance(
        &mut report.summary,
        &mut report.checks,
        &mut report.next_steps,
        context.sdk_root.as_deref(),
    );
    report
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
    let (facts, manifest_error) = match sdk_root.map(load_facts) {
        Some(Ok(facts)) => (facts, None),
        Some(Err(message)) => (fallback_facts(MIN_PYTHON), Some(message)),
        None => (fallback_facts(MIN_PYTHON), None),
    };
    let is_windows = cfg!(windows);
    let refusal = check_prerequisites(&facts, is_windows).err();

    let check = doctor_prerequisite_check(
        refusal.as_ref(),
        facts.prerequisites(is_windows),
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

/// Append the host-environment checks — `zephyrSdkHost`, `longPaths`
/// (Windows only) and `homePath` (alp-sdk ADR 0021, "Cross-cutting
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
/// `zephyrSdkHost` looks adjacent to `--build`'s `zephyrSdk` probe but answers
/// the opposite question — "can an SDK be installed on this host at all" versus
/// "is one installed here" — and reporting the SDK story twice under two names
/// is exactly the trap #81 documents.
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
fn read_sdk_version(root: &str) -> Option<String> {
    let path = Path::new(root).join("metadata").join("sdk_version.yaml");
    let text = std::fs::read_to_string(path).ok()?;
    let mut bare: Option<String> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("version:") {
            let value = rest.trim().trim_matches('"').trim_matches('\'');
            if !value.is_empty() {
                return Some(value.to_string());
            }
        } else if bare.is_none() && !line.contains(':') {
            bare = Some(line.trim_matches('"').trim_matches('\'').to_string());
        }
    }
    bare
}

/// Read + parse the active `board.yaml`, returning `None` when it is absent or
/// unparseable (the preflight then falls back to checking all three backends).
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
fn resolve_context(g: &GlobalArgs, generated_at: &str) -> tan_core::DebugWorkspaceContext {
    let project = resolve_cli_project_context(g);

    // The CLI assumes the marquee debugger extensions are present (it cannot
    // probe VS Code), matching the TS CLI's resolveCliDebugContext.
    let extensions = DebuggerExtensionsState {
        cortex_debug: true,
        peripheral_viewer: true,
        memory_view: true,
        cpp_tools: true,
        code_lldb: true,
    };
    create_debug_workspace_context(
        &project,
        generated_at.to_string(),
        |path| Path::new(path).exists(),
        extensions,
    )
}

/// Map non-passing `DoctorCheck`s to envelope `Issue`s, prefixing the check name
/// with `doctor.` and mapping `Fail`/`Warn` status to `error`/`warning` severity.
fn checks_to_issues(checks: &[DoctorCheck]) -> Vec<Issue> {
    checks
        .iter()
        .filter(|c| c.status != DoctorStatus::Pass)
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
            debugger_extensions: DebuggerExtensionsState {
                cortex_debug: true,
                peripheral_viewer: true,
                memory_view: true,
                cpp_tools: true,
                code_lldb: true,
            },
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
        );
        let names: Vec<&str> = report.checks.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains(&"hostPrerequisites"), "{names:?}");
        assert!(names.contains(&"sdkProvenance"), "{names:?}");
        // Every appended check is counted, or the exit code (`fail > 0`) and
        // the rendered summary disagree with the list they summarize.
        let counted = report.summary.pass + report.summary.warn + report.summary.fail;
        assert_eq!(counted as usize, report.checks.len());
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
        // `is_windows` wiring: the two refusals word themselves differently
        // (Windows lists a per-tool hint line and a reopen-PowerShell tail),
        // so hard-coding either side -- or letting `cfg!(windows)` decay to
        // `false` -- shows up here rather than as a silently POSIX-shaped
        // report on a Windows host.
        let expected = if cfg!(windows) {
            windows_refusal(&[ABSENT_TOOL])
        } else {
            posix_refusal(&[ABSENT_TOOL])
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
        assert!(names.iter().any(|n| n == "zephyrSdkHost"), "{names:?}");
        assert!(names.iter().any(|n| n == "homePath"), "{names:?}");
        assert_eq!(
            names.iter().any(|n| n == "longPaths"),
            cfg!(windows),
            "longPaths is Windows-only: {names:?}"
        );
    }

    #[test]
    fn doctor_build_does_not_repeat_the_host_environment_checks() {
        // The deliberate NEGATIVE half of the placement decision (#81's "one
        // fact, one check" trap). `--build` already carries a `zephyrSdk` probe
        // -- "is an SDK installed here" -- and `zephyrSdkHost` answers the
        // opposite question. Adding these there later, by reflex, would report
        // the SDK story twice under two names; this fails if someone does.
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
        for name in ["zephyrSdkHost", "longPaths", "homePath"] {
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
