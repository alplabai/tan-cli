// SPDX-License-Identifier: Apache-2.0
//! `alp doctor`: the target/server-specific readiness checks and the aggregate
//! report they roll up into.

use serde::Serialize;

use super::context::{DebugRuntimeCapabilities, DebugWorkspaceContext};
use super::target::{DebugServerKind, DebugTargetKind, is_server_supported_for_target};

/// Outcome of a single doctor check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DoctorStatus {
    /// Check passed.
    Pass,
    /// Non-fatal issue (degraded but usable).
    Warn,
    /// Blocking failure.
    Fail,
    /// The running binary could not observe the thing at all, so there is no
    /// verdict to report — reported, but deliberately counted in NONE of the
    /// summary buckets and raising no issue.
    ///
    /// The one producer today is the VS Code extension-presence set: the
    /// standalone `tan` cannot see a marketplace extension, and rendering that
    /// unverifiable assumption as `Pass` printed "is installed." as observed
    /// fact on hosts with no VS Code and inflated the "N passed" count (#102).
    /// Unknown is not `Warn`: a warning says something is wrong, and nothing
    /// here is — the question simply was not askable.
    ///
    /// Consequence, deliberately: `summary.pass + warn + fail` can be smaller
    /// than `checks.len()`. Adding a fourth summary count instead would change
    /// the `tan doctor --build` envelope's key set, which `alp-sdk-vscode`
    /// parses; `--build` emits no unknown check, so it never sees one.
    Unknown,
}

/// A single doctor check with its status and an optional remediation.
#[derive(Debug, Clone, Serialize)]
pub struct DoctorCheck {
    /// Stable check name (e.g. `boardYaml`).
    pub name: String,
    /// The check outcome.
    pub status: DoctorStatus,
    /// Human-readable detail for the outcome.
    pub detail: String,
    /// Suggested fix; omitted from JSON when absent (e.g. on `Pass`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fix: Option<String>,
}

impl DoctorCheck {
    fn new(name: &str, status: DoctorStatus, detail: String, fix: Option<String>) -> Self {
        Self {
            name: name.to_string(),
            status,
            detail,
            fix,
        }
    }
}

/// Per-status check counts for a doctor report.
#[derive(Debug, Clone, Serialize)]
pub struct DoctorSummary {
    /// Number of passing checks.
    pub pass: u32,
    /// Number of warning checks.
    pub warn: u32,
    /// Number of failing checks.
    pub fail: u32,
}

/// Full `alp doctor` report for a target/server combination.
#[derive(Debug, Clone, Serialize)]
pub struct DoctorReport {
    /// ISO-8601 timestamp the report was generated.
    #[serde(rename = "generatedAt")]
    pub generated_at: String,
    /// Target kind the report was run for.
    #[serde(rename = "targetKind")]
    pub target_kind: DebugTargetKind,
    /// Server backend the report was run for.
    pub server: DebugServerKind,
    /// Aggregate pass/warn/fail counts.
    pub summary: DoctorSummary,
    /// Individual checks in evaluation order.
    pub checks: Vec<DoctorCheck>,
    /// Deduplicated remediation steps for non-passing checks.
    #[serde(rename = "nextSteps")]
    pub next_steps: Vec<String>,
    /// Per-tool form of the `hostPrerequisites` check's refusal, so a consumer
    /// never has to parse a rendered line back apart.
    ///
    /// Deliberately the SAME key, the same element type, and the same
    /// `null`-never-`[]` rule as the `bootstrap` envelope's
    /// `data.missingPrerequisites` (see
    /// [`reported_missing`](crate::bootstrap::reported_missing)): one fact
    /// reported by two commands must not have two vocabularies, or a consumer
    /// that learned it from `bootstrap` gets it wrong on `doctor`.
    ///
    /// `null` whenever there is no missing TOOL to name — a clean host, an
    /// error envelope that never reached the probe, and the two Python-floor
    /// refusals, which have no `{tool, command}` pair that could carry the fix.
    /// No `skip_serializing_if`, for the same reason bootstrap has none: the key
    /// is then in every sample, so a consumer can see it exists without
    /// reaching for a schema.
    #[serde(rename = "missingPrerequisites")]
    pub missing_prerequisites: Option<Vec<crate::bootstrap::MissingPrerequisite>>,
}

/// One VS Code extension-presence check.
///
/// `observed` is `None` when the running binary cannot see VS Code at all — the
/// standalone `tan`, which is every caller in this repo. That renders as
/// [`DoctorStatus::Unknown`]: not a pass, not counted, and worded so it never
/// claims an install state nobody looked at (#102). Only a real `Some(false)`
/// carries a fix — there is nothing to repair about a question never asked.
fn extension_check(
    name: &str,
    id: &str,
    observed: Option<bool>,
    missing: DoctorStatus,
    fix: &str,
) -> DoctorCheck {
    let (status, detail) = match observed {
        Some(true) => (DoctorStatus::Pass, format!("{id} is installed.")),
        Some(false) => (missing, format!("{id} is not installed.")),
        None => (
            DoctorStatus::Unknown,
            format!(
                "{id}: unknown — the standalone tan binary cannot see VS Code's installed extensions."
            ),
        ),
    };
    DoctorCheck::new(name, status, detail, fix_when(observed == Some(false), fix))
}

/// Mirror of TS `buildDoctorReport`.
pub fn build_doctor_report(
    context: &DebugWorkspaceContext,
    target: DebugTargetKind,
    server: DebugServerKind,
    runtime: &DebugRuntimeCapabilities,
) -> DoctorReport {
    let has_workspace = is_present(&context.workspace_root);
    let has_sdk = is_present(&context.sdk_root);

    // No `python` check here any more. It probed `context.python_binary` — which
    // in this CLI is ALWAYS the bare `python3`/`python` (`python_path` is never
    // configured; that setting is the extension's), i.e. literally the same name
    // `hostPrerequisites` probes off the manifest's prerequisite list. Two names
    // for one host fact is bad enough; the two also DISAGREED — `Warn` here
    // versus `Fail` there, with two different exit-code consequences — and this
    // one was the weaker probe: no `pythonMinVersion` floor, and no `py`
    // launcher widening, so a Windows host with only the launcher installed got
    // a `python` Warn next to a `hostPrerequisites` Pass about the same
    // interpreter. `hostPrerequisites` (appended by `tan-cli`, see
    // `bootstrap::doctor_prerequisite_check`) is a strict superset, so this one
    // is retired rather than left to contradict it.
    let mut checks: Vec<DoctorCheck> = vec![
        DoctorCheck::new(
            "workspaceRoot",
            status_pass_fail(has_workspace),
            context
                .workspace_root
                .clone()
                .unwrap_or_else(|| "No project directory resolved.".to_string()),
            fix_when(
                !has_workspace,
                "Pass `--project <dir>` or run tan from inside a project directory.",
            ),
        ),
        DoctorCheck::new(
            "sdkRoot",
            status_pass_fail(has_sdk),
            // NOT "The extension could not resolve …": this line is reachable
            // from the standalone binary, where no extension is running and
            // `tan` itself did the resolving (#102). Named generically rather
            // than as a candidate list — the discovery half of that list is
            // owned elsewhere, and a message enumerating it would drift.
            context
                .sdk_root
                .clone()
                .unwrap_or_else(|| "No alp-sdk checkout resolved.".to_string()),
            fix_when(
                !has_sdk,
                "Run `tan sdk switch <path>` or pass `--sdk-root <path>`.",
            ),
        ),
        board_yaml_check(context),
    ];

    if !is_server_supported_for_target(target, server) {
        checks.push(DoctorCheck::new(
            "serverCompatibility",
            DoctorStatus::Fail,
            format!(
                "{} is not supported for {}.",
                server.as_str(),
                target.as_str()
            ),
            Some("Pick a supported backend for the selected target class.".to_string()),
        ));
        return finalize_report(context.generated_at.clone(), target, server, checks);
    }

    let ext = &context.debugger_extensions;
    match target {
        DebugTargetKind::ZephyrMcu | DebugTargetKind::BaremetalMcu => {
            checks.push(extension_check(
                "cortexDebugExtension",
                "marus25.cortex-debug",
                ext.observed(ext.cortex_debug),
                DoctorStatus::Fail,
                "Install marus25.cortex-debug.",
            ));
            checks.push(create_backend_check(server, runtime));
        }
        DebugTargetKind::YoctoUserspace => {
            checks.push(extension_check(
                "cppToolsExtension",
                "ms-vscode.cpptools",
                ext.observed(ext.cpp_tools),
                DoctorStatus::Fail,
                "Install ms-vscode.cpptools.",
            ));
            let gdb = runtime.gdb_executable.clone();
            checks.push(DoctorCheck::new(
                "gdb",
                status_pass_warn(gdb.is_some()),
                gdb.unwrap_or_else(|| "No local gdb executable was found on PATH.".to_string()),
                fix_when(
                    runtime.gdb_executable.is_none(),
                    "Install gdb locally for symbolized remote debugging.",
                ),
            ));
        }
        DebugTargetKind::NativeHost => {
            checks.push(extension_check(
                "codeLLDBExtension",
                "vadimcn.vscode-lldb",
                ext.observed(ext.code_lldb),
                DoctorStatus::Fail,
                "Install vadimcn.vscode-lldb.",
            ));
            // #131: `vadimcn.vscode-lldb` ships its own complete LLDB inside the
            // extension directory and never consults PATH, so a bare PATH probe
            // warning "No local LLDB executable was found" and telling the user
            // to install one is a no-op remedy for a tool the product does not
            // need. Always Pass, mirroring `alp-sdk-vscode`'s own fix for this
            // exact class (`packages/alp-core/src/debug/service.ts`, #369) --
            // informational only, no `fix`, so nothing lands in `nextSteps` and
            // no `doctor.lldb` warning issue is raised. `runtime.lldb_executable`
            // is still read and reported when present: that is real information,
            // only the verdict and the advice were wrong.
            checks.push(DoctorCheck::new(
                "lldb",
                DoctorStatus::Pass,
                runtime.lldb_executable.clone().unwrap_or_else(|| {
                    "vadimcn.vscode-lldb ships its own LLDB, so none is needed on PATH.".to_string()
                }),
                None,
            ));
        }
    }

    finalize_report(context.generated_at.clone(), target, server, checks)
}

/// The `boardYaml` check, whose severity depends on whether the user actually
/// named a project.
///
/// A missing `board.yaml` is a hard failure only when it was ASKED about —
/// `--project <dir>` or `--board-yaml <path>`. With neither flag the path is a
/// guess at the working directory, and the working directory alp-sdk's own
/// `bootstrap` tells a customer to run `tan doctor` from is the SDK checkout
/// root, which has no `board.yaml` and needs none: a project/example directory
/// owns that file. Failing there made the very first command a new customer
/// types report `1 failed` and exit 4 for a non-problem (#100).
///
/// It stays a check rather than disappearing, because "no project selected" is
/// itself worth saying — every project-shaped verdict below it is provisional.
///
/// `tan doctor --build`'s own `boardYaml` (`crate::preflight`) is deliberately
/// untouched and still a hard fail: that mode answers "can this build run", and
/// no build runs without one. Plain `doctor` folds `--build`'s preflight in but
/// drops its duplicate of this check, so only one `boardYaml` is ever emitted.
fn board_yaml_check(context: &DebugWorkspaceContext) -> DoctorCheck {
    let path = context
        .board_yaml_path
        .clone()
        .unwrap_or_else(|| "board.yaml path is unresolved.".to_string());

    if context.board_yaml_exists {
        return DoctorCheck::new("boardYaml", DoctorStatus::Pass, path, None);
    }
    if context.project_selected {
        return DoctorCheck::new(
            "boardYaml",
            DoctorStatus::Fail,
            path,
            Some("Create board.yaml or pass `--board-yaml <path>`.".to_string()),
        );
    }
    DoctorCheck::new(
        "boardYaml",
        DoctorStatus::Warn,
        format!("no project selected — no board.yaml at {path}"),
        Some(
            "Select a project with `--project <dir>` (or `--board-yaml <path>`) to check one."
                .to_string(),
        ),
    )
}

fn create_backend_check(
    server: DebugServerKind,
    runtime: &DebugRuntimeCapabilities,
) -> DoctorCheck {
    let executable = resolve_backend_executable(server, runtime);
    let found = executable.is_some();
    DoctorCheck::new(
        &format!("{}Backend", server.as_str()),
        status_pass_warn(found),
        executable
            .unwrap_or_else(|| format!("No {} executable was found on PATH.", server.as_str())),
        fix_when_owned(
            !found,
            format!("Install {} and make sure it is on PATH.", server.as_str()),
        ),
    )
}

fn resolve_backend_executable(
    server: DebugServerKind,
    runtime: &DebugRuntimeCapabilities,
) -> Option<String> {
    match server {
        DebugServerKind::Jlink => runtime.jlink_executable.clone(),
        DebugServerKind::Openocd => runtime.open_ocd_executable.clone(),
        DebugServerKind::Pyocd => runtime.pyocd_executable.clone(),
        DebugServerKind::Gdbserver => runtime.gdb_executable.clone(),
        DebugServerKind::None => runtime.lldb_executable.clone(),
    }
}

fn finalize_report(
    generated_at: String,
    target: DebugTargetKind,
    server: DebugServerKind,
    checks: Vec<DoctorCheck>,
) -> DoctorReport {
    let summary = DoctorSummary {
        pass: count_status(&checks, DoctorStatus::Pass),
        warn: count_status(&checks, DoctorStatus::Warn),
        fail: count_status(&checks, DoctorStatus::Fail),
    };
    let next_steps = unique_next_steps(&checks);
    DoctorReport {
        generated_at,
        target_kind: target,
        server,
        summary,
        checks,
        next_steps,
        // The prerequisite probe walks PATH and spawns interpreters, so it is
        // appended by `tan-cli` after this pure report is built, never here.
        missing_prerequisites: None,
    }
}

fn count_status(checks: &[DoctorCheck], status: DoctorStatus) -> u32 {
    checks.iter().filter(|c| c.status == status).count() as u32
}

/// Count one check into a summary. [`DoctorStatus::Unknown`] lands in no
/// bucket — see the variant's own note for why it gets no count of its own.
pub(crate) fn count_into(summary: &mut DoctorSummary, status: DoctorStatus) {
    match status {
        DoctorStatus::Pass => summary.pass += 1,
        DoctorStatus::Warn => summary.warn += 1,
        DoctorStatus::Fail => summary.fail += 1,
        DoctorStatus::Unknown => {}
    }
}

/// Fold a check produced AFTER a report builder finalized back into that
/// report's derived fields: count it in `summary`, push it onto `checks`, and
/// re-derive `next_steps` over the FINAL list.
///
/// The counting and the re-derivation are ONE call deliberately. `tan-cli`
/// appends four checks it can only build with IO (`hostPrerequisites`,
/// `sdkProvenance`, the `--build` preflight, the `--fix` bootstrap outcome),
/// each of which used to hand-roll the summary `match` and rely on a separate,
/// trailing `next_steps = unique_next_steps(...)` statement. That statement was
/// deletable with no test noticing — the only test covering it called
/// `unique_next_steps` directly and never went through an append. Making the
/// append itself own the re-derivation removes the statement (and the bug it
/// carried) rather than testing around it: there is no longer an ordering for a
/// caller to get wrong.
///
/// Takes the three fields rather than a report, because the two report types
/// that need it (`DoctorReport` and
/// [`BuildReadinessReport`](crate::BuildReadinessReport)) share these fields but
/// not a type — and a trait over two structs would be more machinery than the
/// three-argument call it replaces.
pub fn append_doctor_check(
    summary: &mut DoctorSummary,
    checks: &mut Vec<DoctorCheck>,
    next_steps: &mut Vec<String>,
    check: DoctorCheck,
) {
    count_into(summary, check.status);
    checks.push(check);
    *next_steps = unique_next_steps(checks);
}

/// As [`append_doctor_check`], but the checks land at the FRONT of the list,
/// keeping their relative order.
///
/// `tan doctor --build` prepends the project/workspace preflight ahead of the
/// host-tool probes: "can a build even start" outranks "is `ninja` installed",
/// and `nextSteps` follows check order, so the preflight's `tan sdk switch
/// <path>` / `tan init` must lead it.
pub fn prepend_doctor_checks(
    summary: &mut DoctorSummary,
    checks: &mut Vec<DoctorCheck>,
    next_steps: &mut Vec<String>,
    prepended: Vec<DoctorCheck>,
) {
    for check in prepended.into_iter().rev() {
        count_into(summary, check.status);
        checks.insert(0, check);
    }
    *next_steps = unique_next_steps(checks);
}

/// Deduplicated `fix` strings of every non-passing check, in check order.
///
/// `pub` and deliberately re-runnable: `finalize_report` computes this once,
/// but [`append_doctor_check`] re-derives it on every later append, because
/// `tan-cli` adds checks that need IO long after the pure builder ran.
/// Idempotent by construction, so recomputing costs nothing.
pub fn unique_next_steps(checks: &[DoctorCheck]) -> Vec<String> {
    let mut steps: Vec<String> = Vec::new();
    for check in checks {
        // `Unknown` alongside `Pass`: a check nobody could run has nothing to
        // remediate, and a next step for one would send a user chasing a
        // verdict that was never reached.
        if matches!(check.status, DoctorStatus::Pass | DoctorStatus::Unknown) {
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

fn is_present(value: &Option<String>) -> bool {
    value.as_deref().is_some_and(|s| !s.is_empty())
}

fn status_pass_fail(ok: bool) -> DoctorStatus {
    if ok {
        DoctorStatus::Pass
    } else {
        DoctorStatus::Fail
    }
}

fn status_pass_warn(ok: bool) -> DoctorStatus {
    if ok {
        DoctorStatus::Pass
    } else {
        DoctorStatus::Warn
    }
}

fn fix_when(unhealthy: bool, fix: &str) -> Option<String> {
    unhealthy.then(|| fix.to_string())
}

fn fix_when_owned(unhealthy: bool, fix: String) -> Option<String> {
    unhealthy.then_some(fix)
}

#[cfg(test)]
mod tests {
    use super::super::context::DebuggerExtensionsState;
    use super::*;

    /// An EXTENSION host's state: it really did enumerate its own extensions,
    /// so the flags are observations. The standalone binary's counterpart is
    /// `extensions_unobservable` below.
    fn extensions_all_installed() -> DebuggerExtensionsState {
        DebuggerExtensionsState {
            cortex_debug: true,
            cpp_tools: true,
            code_lldb: true,
            observable: true,
        }
    }

    /// What the standalone `tan` binary passes: the same inherited `true`s, but
    /// nothing observed them (`tan-cli`'s `standalone_debugger_extensions`).
    fn extensions_unobservable() -> DebuggerExtensionsState {
        DebuggerExtensionsState {
            observable: false,
            ..extensions_all_installed()
        }
    }

    fn healthy_context() -> DebugWorkspaceContext {
        DebugWorkspaceContext {
            generated_at: "1970-01-01T00:00:00.000Z".to_string(),
            workspace_root: Some("/work/proj".to_string()),
            sdk_root: Some("/work/alp-sdk".to_string()),
            board_yaml_path: Some("/work/proj/board.yaml".to_string()),
            west_cwd: Some("/work/proj".to_string()),
            python_binary: "python3".to_string(),
            board_yaml_exists: true,
            project_selected: true,
            debugger_extensions: extensions_all_installed(),
        }
    }

    fn runtime_all_present() -> DebugRuntimeCapabilities {
        DebugRuntimeCapabilities {
            jlink_executable: Some("JLinkGDBServerCL".to_string()),
            open_ocd_executable: Some("openocd".to_string()),
            pyocd_executable: Some("pyocd".to_string()),
            gdb_executable: Some("gdb".to_string()),
            lldb_executable: Some("lldb".to_string()),
        }
    }

    fn runtime_none() -> DebugRuntimeCapabilities {
        DebugRuntimeCapabilities {
            jlink_executable: None,
            open_ocd_executable: None,
            pyocd_executable: None,
            gdb_executable: None,
            lldb_executable: None,
        }
    }

    #[test]
    fn native_host_all_green_passes() {
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        // 3 base checks + codeLLDBExtension + lldb = 5 checks, all pass. The
        // retired `python` check is now `hostPrerequisites`, appended by
        // `tan-cli` because it probes PATH.
        assert_eq!(report.checks.len(), 5);
        assert!(!report.checks.iter().any(|c| c.name == "python"));
        assert_eq!(report.summary.pass, 5);
        assert_eq!(report.summary.warn, 0);
        assert_eq!(report.summary.fail, 0);
        assert!(report.next_steps.is_empty());
        assert_eq!(report.target_kind, DebugTargetKind::NativeHost);
    }

    #[test]
    fn zephyr_with_missing_runtime_warns_backend() {
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            &runtime_none(),
        );
        // 3 base + cortexDebugExtension pass + jlinkBackend warn.
        let backend = report
            .checks
            .iter()
            .find(|c| c.name == "jlinkBackend")
            .expect("backend check present");
        assert_eq!(backend.status, DoctorStatus::Warn);
        assert!(backend.detail.contains("No jlink executable"));
        // Backend warn only: the missing interpreter is `hostPrerequisites`'
        // business now, and it reports one as a `Fail`, not a second `Warn`.
        assert_eq!(report.summary.warn, 1);
        assert_eq!(report.summary.fail, 0);
        assert!(
            report
                .next_steps
                .iter()
                .any(|s| s.contains("Install jlink"))
        );
    }

    #[test]
    fn native_host_lldb_check_passes_even_with_none_on_path() {
        // #131: `vadimcn.vscode-lldb` ships its own LLDB and never reads PATH,
        // so a bare-PATH miss must not Warn or offer an "Install LLDB" fix that
        // fixes nothing. Pre-fix this Warned and pushed a `doctor.lldb` issue.
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_none(),
        );
        let lldb = report
            .checks
            .iter()
            .find(|c| c.name == "lldb")
            .expect("lldb check present");
        assert_eq!(lldb.status, DoctorStatus::Pass);
        assert!(lldb.fix.is_none());
        assert!(
            lldb.detail
                .contains("vadimcn.vscode-lldb ships its own LLDB"),
            "{}",
            lldb.detail
        );
        assert_eq!(report.summary.fail, 0);
        assert_eq!(report.summary.warn, 0);
        assert!(!report.next_steps.iter().any(|s| s.contains("LLDB")));
    }

    #[test]
    fn native_host_lldb_check_still_reports_a_resolved_executable() {
        // Keep `runtime.lldb_executable` visible when present -- real
        // information, only the verdict/advice around a miss were wrong.
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let lldb = report
            .checks
            .iter()
            .find(|c| c.name == "lldb")
            .expect("lldb check present");
        assert_eq!(lldb.status, DoctorStatus::Pass);
        assert_eq!(lldb.detail, "lldb");
    }

    #[test]
    fn missing_workspace_and_sdk_fail() {
        let mut ctx = healthy_context();
        ctx.workspace_root = None;
        ctx.sdk_root = None;
        ctx.board_yaml_exists = false;
        let report = build_doctor_report(
            &ctx,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        assert_eq!(report.summary.fail, 3); // workspaceRoot, sdkRoot, boardYaml
        let workspace = &report.checks[0];
        assert_eq!(workspace.status, DoctorStatus::Fail);
        assert_eq!(workspace.detail, "No project directory resolved.");
        assert!(workspace.fix.is_some());
    }

    #[test]
    fn workspace_root_failure_names_a_terminal_remedy_not_a_vscode_one() {
        // #134: "Open a workspace containing an ALP project." is a VS Code
        // instruction with no terminal equivalent -- and misspelled the brand
        // besides. A reader at a prompt needs a flag, not an editor action.
        let mut ctx = healthy_context();
        ctx.workspace_root = None;
        let report = build_doctor_report(
            &ctx,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = report
            .checks
            .iter()
            .find(|c| c.name == "workspaceRoot")
            .expect("workspaceRoot present");
        assert!(
            !check.detail.contains("workspace")
                && !check
                    .fix
                    .as_deref()
                    .unwrap_or_default()
                    .contains("workspace"),
            "still names a VS Code workspace: {check:?}"
        );
        assert_eq!(
            check.fix.as_deref(),
            Some("Pass `--project <dir>` or run tan from inside a project directory.")
        );
    }

    #[test]
    fn unsupported_server_branch_short_circuits() {
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::Jlink,
            &runtime_all_present(),
        );
        let compat = report.checks.last().expect("a check present");
        assert_eq!(compat.name, "serverCompatibility");
        assert_eq!(compat.status, DoctorStatus::Fail);
        assert_eq!(compat.detail, "jlink is not supported for native-host.");
        assert_eq!(report.summary.fail, 1);
        // base 3 checks + serverCompatibility, no target-specific checks.
        assert_eq!(report.checks.len(), 4);
    }

    #[test]
    fn report_serializes_with_contract_field_names() {
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let json = serde_json::to_string(&report).unwrap();
        assert!(json.contains("\"generatedAt\":\"1970-01-01T00:00:00.000Z\""));
        assert!(json.contains("\"targetKind\":\"native-host\""));
        assert!(json.contains("\"server\":\"none\""));
        assert!(json.contains("\"nextSteps\":[]"));
        // pass checks omit the optional `fix` field.
        assert!(!json.contains("\"fix\""));
    }

    #[test]
    fn extensions_state_serializes_as_camel_case() {
        let json = serde_json::to_string(&extensions_all_installed()).unwrap();
        assert!(json.contains("\"cortexDebug\":true"));
        assert!(json.contains("\"cppTools\":true"));
        assert!(json.contains("\"codeLLDB\":true"));
    }

    /// #132: `peripheralViewerExtension`/`memoryViewExtension` mirrored a TS
    /// `MCU_COMPANION_VIEWERS` that never shipped on any `alp-sdk-vscode`
    /// branch, and `cortex-debug` force-installs both viewers as hard
    /// `extensionDependencies` anyway -- `cortexDebugExtension` already covers
    /// the same ground on the only host that can answer the question. Neither
    /// check exists any more; this pins that a Zephyr/baremetal report emits
    /// exactly `cortexDebugExtension` then the backend check, with nothing
    /// between them.
    #[test]
    fn zephyr_emits_no_companion_viewer_checks_between_cortex_and_backend() {
        let report = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            &runtime_all_present(),
        );
        let names: Vec<&str> = report.checks.iter().map(|c| c.name.as_str()).collect();
        let cortex = names
            .iter()
            .position(|n| *n == "cortexDebugExtension")
            .expect("cortexDebugExtension present");
        let backend = names
            .iter()
            .position(|n| *n == "jlinkBackend")
            .expect("jlinkBackend present");
        assert_eq!(backend, cortex + 1, "{names:?}");
        assert!(!names.contains(&"peripheralViewerExtension"), "{names:?}");
        assert!(!names.contains(&"memoryViewExtension"), "{names:?}");
    }

    #[test]
    fn an_unobservable_host_reports_every_extension_check_as_unknown() {
        // #102: the standalone binary cannot see VS Code, so a hardcoded `true`
        // must not print "is installed." as observed fact, must not join the
        // pass total, and must raise no remediation. All three extension checks
        // -- one per target branch -- go through `extension_check`, so all three
        // are driven here rather than only the `native-host` one the bug report
        // happened to capture.
        let mut ctx = healthy_context();
        ctx.debugger_extensions = extensions_unobservable();

        for (target, server, name) in [
            (
                DebugTargetKind::NativeHost,
                DebugServerKind::None,
                "codeLLDBExtension",
            ),
            (
                DebugTargetKind::ZephyrMcu,
                DebugServerKind::Jlink,
                "cortexDebugExtension",
            ),
            (
                DebugTargetKind::YoctoUserspace,
                DebugServerKind::Gdbserver,
                "cppToolsExtension",
            ),
        ] {
            let report = build_doctor_report(&ctx, target, server, &runtime_all_present());
            let check = report
                .checks
                .iter()
                .find(|c| c.name == name)
                .unwrap_or_else(|| panic!("{name} missing for {}", target.as_str()));
            assert_eq!(check.status, DoctorStatus::Unknown, "{name}");
            assert!(
                !check.detail.contains("is installed."),
                "{name} must not claim an install state nobody probed: {}",
                check.detail
            );
            assert!(check.fix.is_none(), "{name} has nothing to repair");
            // Counted in NO bucket -- the whole point is that "5 passed" stops
            // including a check the CLI never ran.
            let counted = report.summary.pass + report.summary.warn + report.summary.fail;
            assert!(
                (counted as usize) < report.checks.len(),
                "{name}: unknown must not be counted ({counted} vs {})",
                report.checks.len()
            );
            assert!(
                !report.next_steps.iter().any(|s| s.contains("Install ")),
                "{name} raised a next step for an unasked question: {:?}",
                report.next_steps
            );
        }
    }

    #[test]
    fn an_observing_caller_still_gets_a_pass_or_a_fail() {
        // The negative half: `observable: true` is the EXTENSION's state, where
        // `true` is a real observation. Rendering everything `Unknown`
        // unconditionally would silently disarm the check for the one caller
        // that can actually answer it.
        let installed = build_doctor_report(
            &healthy_context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = installed
            .checks
            .iter()
            .find(|c| c.name == "codeLLDBExtension")
            .expect("codeLLDBExtension present");
        assert_eq!(check.status, DoctorStatus::Pass);
        assert_eq!(check.detail, "vadimcn.vscode-lldb is installed.");

        let mut ctx = healthy_context();
        ctx.debugger_extensions = DebuggerExtensionsState {
            code_lldb: false,
            ..extensions_all_installed()
        };
        let missing = build_doctor_report(
            &ctx,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = missing
            .checks
            .iter()
            .find(|c| c.name == "codeLLDBExtension")
            .expect("codeLLDBExtension present");
        assert_eq!(check.status, DoctorStatus::Fail);
        assert_eq!(check.detail, "vadimcn.vscode-lldb is not installed.");
        assert_eq!(check.fix.as_deref(), Some("Install vadimcn.vscode-lldb."));
    }

    #[test]
    fn a_missing_board_yaml_warns_until_a_project_is_selected() {
        // #100(b): at an alp-sdk checkout root -- where `bootstrap`'s printed
        // next steps send every new customer -- there is no `board.yaml` and no
        // reason for one, yet it was the single hard failure and the exit code
        // was 4. It is a failure only once the user NAMED a project.
        let mut unselected = healthy_context();
        unselected.board_yaml_exists = false;
        unselected.project_selected = false;
        let report = build_doctor_report(
            &unselected,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = report
            .checks
            .iter()
            .find(|c| c.name == "boardYaml")
            .expect("boardYaml present");
        assert_eq!(check.status, DoctorStatus::Warn);
        assert!(check.detail.contains("no project selected"), "{check:?}");
        // The exit code the CLI derives is `summary.fail > 0`, so this is the
        // assertion that keeps `tan doctor` from exiting 4 at an SDK root.
        assert_eq!(report.summary.fail, 0, "{:?}", report.checks);

        let mut selected = unselected.clone();
        selected.project_selected = true;
        let report = build_doctor_report(
            &selected,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = report
            .checks
            .iter()
            .find(|c| c.name == "boardYaml")
            .expect("boardYaml present");
        assert_eq!(check.status, DoctorStatus::Fail);
        assert_eq!(report.summary.fail, 1);
    }

    #[test]
    fn sdk_root_failure_names_no_extension() {
        // #102's smaller symptom: "The extension could not resolve an alp-sdk
        // checkout." is reachable from the standalone binary, where nothing but
        // `tan` is running.
        let mut ctx = healthy_context();
        ctx.sdk_root = None;
        let report = build_doctor_report(
            &ctx,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime_all_present(),
        );
        let check = report
            .checks
            .iter()
            .find(|c| c.name == "sdkRoot")
            .expect("sdkRoot present");
        assert!(!check.detail.contains("extension"), "{}", check.detail);
        assert_eq!(
            check.fix.as_deref(),
            Some("Run `tan sdk switch <path>` or pass `--sdk-root <path>`.")
        );
    }

    #[test]
    fn unknown_serializes_as_its_own_status_token() {
        // A consumer that only knows pass/warn/fail must see a value it can
        // reject, not a `pass` it would render as a green tick.
        let json = serde_json::to_string(&DoctorStatus::Unknown).unwrap();
        assert_eq!(json, "\"unknown\"");
    }

    fn check(name: &str, status: DoctorStatus, fix: Option<&str>) -> DoctorCheck {
        DoctorCheck {
            name: name.to_string(),
            status,
            detail: format!("{name} detail"),
            fix: fix.map(str::to_string),
        }
    }

    /// A finalized report: `next_steps` already computed (and empty, because
    /// its one check passes), exactly the state `tan-cli` starts appending to.
    fn finalized() -> (DoctorSummary, Vec<DoctorCheck>, Vec<String>) {
        let checks = vec![check("boardYaml", DoctorStatus::Pass, None)];
        let next_steps = unique_next_steps(&checks);
        assert!(next_steps.is_empty());
        (
            DoctorSummary {
                pass: 1,
                warn: 0,
                fail: 0,
            },
            checks,
            next_steps,
        )
    }

    #[test]
    fn an_appended_checks_fix_reaches_next_steps() {
        // The regression: `next_steps` is computed by the report BUILDER, so
        // every check `tan-cli` appends afterwards (it needs IO to build them)
        // landed with its `fix` missing from the field the envelope documents
        // as "deduplicated remediation steps for non-passing checks" and the
        // extension renders as a Fix button. Deleting the re-derivation inside
        // `append_doctor_check` has to fail HERE.
        let (mut summary, mut checks, mut steps) = finalized();
        append_doctor_check(
            &mut summary,
            &mut checks,
            &mut steps,
            check(
                "hostPrerequisites",
                DoctorStatus::Fail,
                Some("Install the missing prerequisites, then run `tan bootstrap`."),
            ),
        );

        assert_eq!(
            steps,
            vec!["Install the missing prerequisites, then run `tan bootstrap`."]
        );
        // Counted exactly once, in the right bucket -- swapping the summary
        // arms would make a clean host exit 4.
        assert_eq!((summary.pass, summary.warn, summary.fail), (1, 0, 1));
        assert_eq!(checks.len(), 2);
        assert_eq!(checks[1].name, "hostPrerequisites");
    }

    #[test]
    fn appending_a_passing_check_adds_no_step_and_keeps_the_earlier_ones() {
        // The re-derivation runs over the WHOLE list, so it must not be a
        // "clear and rebuild from the new check" either: an earlier appender's
        // step has to survive a later, passing append.
        let (mut summary, mut checks, mut steps) = finalized();
        append_doctor_check(
            &mut summary,
            &mut checks,
            &mut steps,
            check("sdkProvenance", DoctorStatus::Warn, Some("git pull")),
        );
        append_doctor_check(
            &mut summary,
            &mut checks,
            &mut steps,
            check("bootstrapFix", DoctorStatus::Pass, None),
        );

        assert_eq!(steps, vec!["git pull"]);
        assert_eq!((summary.pass, summary.warn, summary.fail), (2, 1, 0));
    }

    #[test]
    fn a_repeated_fix_is_not_duplicated_across_appends() {
        let (mut summary, mut checks, mut steps) = finalized();
        for name in ["west", "cmake"] {
            append_doctor_check(
                &mut summary,
                &mut checks,
                &mut steps,
                check(name, DoctorStatus::Warn, Some("Run `tan bootstrap`.")),
            );
        }
        assert_eq!(steps, vec!["Run `tan bootstrap`."]);
        assert_eq!(summary.warn, 2);
    }

    #[test]
    fn prepended_checks_keep_their_order_and_lead_next_steps() {
        // `--build` puts the project/workspace preflight ahead of the host-tool
        // probes, and `nextSteps` follows check order -- so `tan sdk switch` /
        // `tan init` must LEAD, not trail, an already-appended tool fix.
        let (mut summary, mut checks, mut steps) = finalized();
        append_doctor_check(
            &mut summary,
            &mut checks,
            &mut steps,
            check("west", DoctorStatus::Warn, Some("Install west.")),
        );
        prepend_doctor_checks(
            &mut summary,
            &mut checks,
            &mut steps,
            vec![
                check("sdk", DoctorStatus::Fail, Some("tan sdk switch <path>")),
                check("workspace", DoctorStatus::Fail, Some("tan init")),
            ],
        );

        let names: Vec<&str> = checks.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, ["sdk", "workspace", "boardYaml", "west"]);
        assert_eq!(
            steps,
            vec!["tan sdk switch <path>", "tan init", "Install west."]
        );
        assert_eq!((summary.pass, summary.warn, summary.fail), (1, 1, 2));
    }
}
