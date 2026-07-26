// SPDX-License-Identifier: Apache-2.0
//! `tan support-bundle` — write a diagnostic bundle (inspect + trace + doctor).
//!
//! Mirrors TS `runSupportBundleCommand`: assemble an inspect report, generation
//! trace, and doctor report into one JSON file, then return an envelope with the
//! written path + decision count. Unsupported backend → exit 4; the exit code
//! otherwise follows the doctor summary (fail > 0 → exit 4).

use std::path::{Path, PathBuf};

use serde::Serialize;
use tan_core::{
    ALL_EMIT_MODES, DebugGenerationTraceDecision, DebugServerKind, DebugTargetKind,
    DebugTraceOutcome, DebugWorkspaceContext, DebuggerExtensionsState, DoctorCheck, DoctorReport,
    DoctorStatus, ProjectContext, build_doctor_report, collect_resolved_values,
    collect_runtime_capabilities_from_commands, create_debug_workspace_context, create_loader_plan,
    generation_target_support, is_server_supported_for_target, parse_server_kind,
    parse_target_kind,
};

use super::CommandRun;
use crate::cli::{GlobalArgs, SupportBundleArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{command_on_path, generated_at_iso, normalize_path, resolve_cli_project_context};

/// Stdout envelope `data` payload for `support-bundle`: the written bundle path
/// plus the resolved target/server and trace decision count.
#[derive(Serialize)]
struct SupportBundleData {
    /// Payload schema version (always `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// ISO-8601 timestamp the command ran.
    #[serde(rename = "generatedAt")]
    generated_at: String,
    /// Absolute path of the written bundle file (empty on failure).
    #[serde(rename = "outputPath")]
    output_path: String,
    /// Resolved debug target kind.
    #[serde(rename = "targetKind")]
    target_kind: DebugTargetKind,
    /// Resolved debug server kind.
    server: DebugServerKind,
    /// Number of generation-trace decisions captured.
    #[serde(rename = "decisionCount")]
    decision_count: usize,
}

// ── bundle file payload (not part of the stdout envelope) ───────────────────

/// Inspect section of the bundle file: the resolved debug workspace context and
/// its flattened resolved values.
#[derive(Serialize)]
struct InspectReport<'a> {
    /// Section schema version (always `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: &'a str,
    /// ISO-8601 generation timestamp.
    #[serde(rename = "generatedAt")]
    generated_at: &'a str,
    /// Resolved debug workspace context.
    context: &'a DebugWorkspaceContext,
    /// Flattened resolved values derived from `context`.
    #[serde(rename = "resolvedValues")]
    resolved_values: Vec<tan_core::DebugResolvedValue>,
}

/// Trace section of the bundle file: the workflow id and the captured
/// generation-trace decisions.
#[derive(Serialize)]
struct TraceReport<'a> {
    /// Section schema version (always `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: &'a str,
    /// ISO-8601 generation timestamp.
    #[serde(rename = "generatedAt")]
    generated_at: &'a str,
    /// Workflow identifier (`"cli.support-bundle"`).
    workflow: &'a str,
    /// Per-target generation-trace decisions.
    decisions: &'a [DebugGenerationTraceDecision],
}

/// Top-level JSON written to the bundle file: combines inspect, trace, and
/// doctor sections plus freeform notes.
#[derive(Serialize)]
struct BundlePayload<'a> {
    /// Bundle schema version (always `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: &'a str,
    /// ISO-8601 generation timestamp.
    #[serde(rename = "generatedAt")]
    generated_at: &'a str,
    /// Inspect report section.
    inspect: InspectReport<'a>,
    /// Generation-trace section.
    trace: TraceReport<'a>,
    /// Doctor report section.
    doctor: &'a DoctorReport,
    /// Freeform metadata notes (target, server, workspace root).
    notes: Vec<String>,
}

/// Entry point for `tan support-bundle`: resolves project/target/server, builds
/// the inspect+trace+doctor payload, writes it to a bundle file, and returns the
/// envelope. Exit is `DoctorFailure` on unsupported backend or doctor fail count
/// > 0, else `Success`.
pub fn run(g: &GlobalArgs, args: &SupportBundleArgs) -> CommandRun {
    let generated_at = generated_at_iso();
    let project = resolve_cli_project_context(g);
    let context = create_debug_workspace_context(
        &project,
        generated_at.clone(),
        |path| Path::new(path).exists(),
        DebuggerExtensionsState {
            cortex_debug: true,
            peripheral_viewer: true,
            memory_view: true,
            cpp_tools: true,
            code_lldb: true,
        },
    );

    let target = match parse_target_kind(args.target_kind.as_deref()) {
        Ok(t) => t,
        Err(message) => return internal_failure(g, &generated_at, message),
    };
    let server = match parse_server_kind(args.server.as_deref()) {
        Ok(s) => s,
        Err(message) => return internal_failure(g, &generated_at, message),
    };

    if !is_server_supported_for_target(target, server) {
        return server_incompatible(g, &generated_at, target, server);
    }

    let runtime = collect_runtime_capabilities_from_commands(command_on_path);

    let decisions =
        match create_bundle_trace_decisions(&project, g.target.as_deref(), args.path.as_deref()) {
            Ok(d) => d,
            Err(message) => return internal_failure(g, &generated_at, message),
        };
    let doctor = bundle_doctor_report(&context, target, server, &runtime);

    // Assemble + serialize the bundle file (side effect; not in the envelope).
    let notes = vec![
        format!("targetKind={}", target.as_str()),
        format!("server={}", server.as_str()),
        format!(
            "workspaceRoot={}",
            context.workspace_root.clone().unwrap_or_default()
        ),
    ];
    let payload = BundlePayload {
        schema_version: "1",
        generated_at: &generated_at,
        inspect: InspectReport {
            schema_version: "1",
            generated_at: &generated_at,
            context: &context,
            resolved_values: collect_resolved_values(&context),
        },
        trace: TraceReport {
            schema_version: "1",
            generated_at: &generated_at,
            workflow: "cli.support-bundle",
            decisions: &decisions,
        },
        doctor: &doctor,
        notes,
    };
    let content = serde_json::to_string_pretty(&payload).expect("bundle is serializable");

    let output_path = match write_bundle(
        args.destination.as_deref(),
        context.workspace_root.as_deref().unwrap_or("."),
        &generated_at,
        &content,
    ) {
        Ok(p) => p,
        Err(message) => return internal_failure(g, &generated_at, message),
    };

    let issues = doctor_checks_to_issues(&doctor.checks);
    let exit = if doctor.summary.fail > 0 {
        ExitCode::DoctorFailure
    } else {
        ExitCode::Success
    };

    let data = SupportBundleData {
        schema_version: "1".to_string(),
        generated_at: generated_at.clone(),
        output_path: output_path.clone(),
        target_kind: target,
        server,
        decision_count: decisions.len(),
    };
    let project_env = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };
    let text = if g.is_json() {
        Vec::new()
    } else {
        support_bundle_text(&output_path, decisions.len(), g)
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("support-bundle", project_env, data, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

/// The bundle's doctor section: the pure report PLUS the host-prerequisite
/// gate.
///
/// The gate runs here too, not just in `tan doctor`. A support bundle is what a
/// user attaches *precisely when bootstrap failed*, and
/// `DoctorReport::missing_prerequisites` defines `null` as "checked, nothing
/// missing" — so a bundle built without it would hide the missing `ninja` that
/// caused it AND positively assert the host is fine. A missing prerequisite
/// therefore also makes `tan support-bundle` exit 4 with a
/// `support-bundle.hostPrerequisites` error issue (the bundle file is still
/// written).
///
/// Its own function rather than four inline lines in `run` so the gate call is
/// reachable from a test: `run` writes files and resolves a real project, so a
/// no-op'd call inline there was invisible to the suite.
fn bundle_doctor_report(
    context: &DebugWorkspaceContext,
    target: DebugTargetKind,
    server: DebugServerKind,
    runtime: &tan_core::DebugRuntimeCapabilities,
) -> DoctorReport {
    let mut doctor = build_doctor_report(context, target, server, runtime);
    crate::commands::doctor::append_host_prerequisites(&mut doctor, context.sdk_root.as_deref());
    // Same reasoning as the line above: a bundle is attached precisely when
    // something failed, and "this host has no Zephyr SDK build at all" /
    // "long paths are off" are facts the maintainer would otherwise reconstruct
    // from a build log.
    crate::commands::doctor::append_host_environment(&mut doctor);
    doctor
}

/// Builds the generation-trace decisions: one `Planned` entry per emit target
/// (with the loader command/output it would run) when the project context is
/// fully resolved, otherwise a single `Failed` entry. Appends a path-focus entry
/// when `focus` is set.
fn create_bundle_trace_decisions(
    context: &ProjectContext,
    target: Option<&str>,
    focus: Option<&str>,
) -> Result<Vec<DebugGenerationTraceDecision>, String> {
    let mut decisions = Vec::new();

    match (
        context.workspace_root.as_deref(),
        context.sdk_root.as_deref(),
        context.board_yaml_path.as_deref(),
    ) {
        (Some(workspace_root), Some(sdk_root), Some(board_path)) => {
            for emit in resolve_targets(target)? {
                let support = generation_target_support(emit).expect("target validated");
                let plan = create_loader_plan(
                    workspace_root,
                    sdk_root,
                    board_path,
                    &context.python_binary,
                    support,
                );
                decisions.push(DebugGenerationTraceDecision {
                    key: format!("generation.target.{emit}"),
                    outcome: DebugTraceOutcome::Planned,
                    output_path: Some(plan.output_path),
                    detail: format!("Would run: {}", plan.command_line),
                });
            }
        }
        _ => decisions.push(DebugGenerationTraceDecision {
            key: "generation.targets".to_string(),
            outcome: DebugTraceOutcome::Failed,
            output_path: None,
            detail: "Generation targets were not traced because project context is unresolved."
                .to_string(),
        }),
    }

    if let Some(focus_path) = focus {
        decisions.push(DebugGenerationTraceDecision {
            key: format!("config.path.{focus_path}"),
            outcome: DebugTraceOutcome::Planned,
            output_path: None,
            detail: "Path-level trace was requested and captured as part of bundle metadata."
                .to_string(),
        });
    }

    Ok(decisions)
}

/// Resolves the emit targets to trace: all of `ALL_EMIT_MODES` when `raw` is
/// `None`, a single validated mode when given, or an error for an unknown mode.
fn resolve_targets(raw: Option<&str>) -> Result<Vec<&'static str>, String> {
    match raw {
        None => Ok(ALL_EMIT_MODES.to_vec()),
        Some(target) => match ALL_EMIT_MODES.iter().copied().find(|m| *m == target) {
            Some(m) => Ok(vec![m]),
            None => Err(format!(
                "Unsupported trace target '{target}'. Allowed values: {}.",
                ALL_EMIT_MODES.join(", ")
            )),
        },
    }
}

/// Writes `content` to a timestamped `debug-support-bundle-*.json` file under
/// `destination` (resolved against cwd) or `<workspace_root>/.alp-support`,
/// creating the directory. Returns the written path.
fn write_bundle(
    destination: Option<&str>,
    workspace_root: &str,
    generated_at: &str,
    content: &str,
) -> Result<String, String> {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let base_dir = match destination {
        Some(dest) => normalize_path(&cwd.join(dest)),
        None => Path::new(workspace_root).join(".alp-support"),
    };
    let file_name = format!(
        "debug-support-bundle-{}.json",
        timestamp_for_file(generated_at)
    );
    let output_path = base_dir.join(file_name);

    std::fs::create_dir_all(&base_dir).map_err(|e| e.to_string())?;
    std::fs::write(&output_path, content).map_err(|e| e.to_string())?;
    Ok(output_path.to_string_lossy().to_string())
}

/// Makes an ISO timestamp filename-safe by replacing `:` and `.` with `-`.
fn timestamp_for_file(iso_timestamp: &str) -> String {
    iso_timestamp
        .chars()
        .map(|c| if c == ':' || c == '.' { '-' } else { c })
        .collect()
}

/// Maps non-`Pass` doctor checks to envelope `Issue`s, coding each as
/// `support-bundle.<name>` and severity `error` for `Fail`, `warning` otherwise.
fn doctor_checks_to_issues(checks: &[DoctorCheck]) -> Vec<Issue> {
    checks
        .iter()
        .filter(|c| c.status != DoctorStatus::Pass)
        .map(|c| Issue {
            code: format!("support-bundle.{}", c.name),
            severity: if c.status == DoctorStatus::Fail {
                "error".to_string()
            } else {
                "warning".to_string()
            },
            message: c.detail.clone(),
        })
        .collect()
}

/// Builds the human-readable (non-JSON) output lines: the exported path and
/// decision count, plus a `--format json` hint when `--verbose`.
fn support_bundle_text(output_path: &str, decision_count: usize, g: &GlobalArgs) -> Vec<String> {
    let mut lines = vec![
        format!("support-bundle: exported {output_path}"),
        format!("support-bundle: trace decisions={decision_count}"),
    ];
    if g.verbose {
        lines.push(
            "support-bundle: include --format json for machine-readable envelopes.".to_string(),
        );
    }
    lines
}

/// Builds the `DoctorFailure` result for an unsupported server/target pairing:
/// a `support-bundle.server-compatibility` issue and an empty-path data payload.
fn server_incompatible(
    g: &GlobalArgs,
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
) -> CommandRun {
    let issues = vec![Issue {
        code: "support-bundle.server-compatibility".to_string(),
        severity: "error".to_string(),
        message: format!(
            "Server '{}' is not supported for target '{}'.",
            server.as_str(),
            target.as_str()
        ),
    }];
    let data = SupportBundleData {
        schema_version: "1".to_string(),
        generated_at: generated_at.to_string(),
        output_path: String::new(),
        target_kind: target,
        server,
        decision_count: 0,
    };
    let text = if g.is_json() {
        Vec::new()
    } else {
        vec![format!(
            "support-bundle: server '{}' is not supported for target '{}'.",
            server.as_str(),
            target.as_str()
        )]
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "support-bundle",
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

/// Builds the `InternalFailure` result for an unexpected error: a
/// `support-bundle.internal-failure` issue and a placeholder data payload.
fn internal_failure(g: &GlobalArgs, generated_at: &str, message: String) -> CommandRun {
    let issues = vec![Issue {
        code: "support-bundle.internal-failure".to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    let data = SupportBundleData {
        schema_version: "1".to_string(),
        generated_at: generated_at.to_string(),
        output_path: String::new(),
        target_kind: DebugTargetKind::NativeHost,
        server: DebugServerKind::None,
        decision_count: 0,
    };
    let text = if g.is_json() {
        Vec::new()
    } else {
        vec!["support-bundle: internal failure".to_string(), message]
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "support-bundle",
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

/// Returns an empty envelope `Project` (no root or `board.yaml`) for failure paths.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::doctor::tests::TempTree;

    fn context() -> DebugWorkspaceContext {
        DebugWorkspaceContext {
            generated_at: "1970-01-01T00:00:00.000Z".to_string(),
            workspace_root: Some("/work/proj".to_string()),
            // No SDK root -> the prerequisite gate falls back to tan's built-in
            // tool list, which is still a real probe of this host.
            sdk_root: None,
            board_yaml_path: Some("/work/proj/board.yaml".to_string()),
            west_cwd: Some("/work/proj".to_string()),
            python_binary: "python3".to_string(),
            board_yaml_exists: true,
            debugger_extensions: DebuggerExtensionsState {
                cortex_debug: true,
                peripheral_viewer: true,
                memory_view: true,
                cpp_tools: true,
                code_lldb: true,
            },
        }
    }

    fn runtime() -> tan_core::DebugRuntimeCapabilities {
        tan_core::DebugRuntimeCapabilities {
            jlink_executable: None,
            open_ocd_executable: None,
            pyocd_executable: None,
            gdb_executable: None,
            lldb_executable: Some("lldb".to_string()),
        }
    }

    #[test]
    fn the_bundles_doctor_section_runs_the_host_prerequisite_gate() {
        // `build_doctor_report` alone never probes prerequisites, so it
        // serializes `"missingPrerequisites": null` -- which that field defines
        // as "checked, nothing missing" -- for a host nobody checked. Dropping
        // the `append_host_prerequisites` call has to fail HERE; the assertion
        // is on the CHECK's presence, not its status, because the status is
        // whatever this host's PATH produces.
        let doctor = bundle_doctor_report(
            &context(),
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            &runtime(),
        );
        let check = doctor
            .checks
            .iter()
            .find(|c| c.name == "hostPrerequisites")
            .expect("the bundle's doctor section must carry the prerequisite verdict");
        assert!(
            check
                .detail
                .contains("facts from tan's built-in fallback list"),
            "{}",
            check.detail
        );
        // Counted, not just appended -- the exit code is `summary.fail > 0`.
        let counted = doctor.summary.pass + doctor.summary.warn + doctor.summary.fail;
        assert_eq!(counted as usize, doctor.checks.len());
    }

    #[test]
    fn the_written_bundle_file_carries_the_prerequisite_verdict() {
        // The wiring seam: `run` -> `bundle_doctor_report`. Swapping that one
        // call for the bare `build_doctor_report` writes a bundle whose doctor
        // section has no `hostPrerequisites` check and `"missingPrerequisites":
        // null` -- which that field DEFINES as "checked, nothing missing" --
        // for a host nobody probed, and the test above still passes because it
        // calls the helper directly. A bundle is attached PRECISELY when
        // bootstrap failed, so it is the last artifact that may assert a host
        // is clean without looking; assert on the file the command actually
        // wrote.
        let tree = TempTree::new("bundle-run");
        let g = GlobalArgs {
            project: Some(tree.path().to_string_lossy().into_owned()),
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
        };
        let run = run(
            &g,
            &SupportBundleArgs {
                target_kind: None,
                server: None,
                path: None,
                destination: Some(tree.path().to_string_lossy().into_owned()),
            },
        );
        let json = run.json.expect("json envelope");
        let envelope: serde_json::Value = serde_json::from_str(&json).expect("envelope is JSON");
        let output_path = envelope["data"]["outputPath"]
            .as_str()
            .expect("the bundle path is reported");
        let bundle: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(output_path).expect("bundle written"))
                .expect("bundle is JSON");
        let names: Vec<&str> = bundle["doctor"]["checks"]
            .as_array()
            .expect("doctor.checks is an array")
            .iter()
            .map(|c| c["name"].as_str().unwrap_or_default())
            .collect();
        assert!(names.contains(&"hostPrerequisites"), "{names:?}");
    }

    #[test]
    fn a_failing_prerequisite_check_becomes_a_support_bundle_error_issue() {
        // The `support-bundle.` prefix, not `doctor.` and not
        // `bootstrap.prerequisites-missing`: in this CLI an issue code's prefix
        // is the command that emitted the envelope, without exception.
        let issues = doctor_checks_to_issues(&[
            DoctorCheck {
                name: "boardYaml".to_string(),
                status: DoctorStatus::Pass,
                detail: "fine".to_string(),
                fix: None,
            },
            DoctorCheck {
                name: "hostPrerequisites".to_string(),
                status: DoctorStatus::Fail,
                detail: "Missing required tools: ninja.".to_string(),
                fix: Some(
                    "Install the missing prerequisites, then run `tan bootstrap`.".to_string(),
                ),
            },
        ]);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].code, "support-bundle.hostPrerequisites");
        assert_eq!(issues[0].severity, "error");
        assert_eq!(issues[0].message, "Missing required tools: ninja.");
    }
}
