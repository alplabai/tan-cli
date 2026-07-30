// SPDX-License-Identifier: Apache-2.0
//! `tan debug-config` — generate (or preview) a VS Code launch.json entry.
//!
//! Mirrors TS `runDebugConfigCommand`: build a launch draft for the target/
//! server, then either preview it (`--preview`) or merge it into
//! `<workspace>/.vscode/launch.json`. Invalid kind / unsupported backend →
//! exit 5; a failed write → exit 3.

use std::path::{Path, PathBuf};

use serde_json::Value;
use tan_core::run::{native_sim_exe_beside, native_sim_slice};
use tan_core::runners::{parse_runners_config, runner_arg_value, runner_arg_values};
use tan_core::size::{SocVariant, resolve_variant};
use tan_core::system_manifest::{Slice, SystemManifest, parse_system_manifest};
use tan_core::{
    DebugServerKind, DebugTargetKind, LaunchResolution, ProjectContext, apply_launch_resolution,
    create_launch_draft, create_launch_json_write_plan, fill_debug_probe_identity_gaps,
    is_unresolved_placeholder, launch_preview_document, launch_preview_notes, parse_board_model,
    parse_server_kind, parse_target_kind,
};

use super::CommandRun;
use crate::cli::{DebugConfigArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{generated_at_iso, normalize_path, resolve_cli_project_context_no_sdk_report};

/// `data` payload of the `debug-config` envelope (serialized as camelCase JSON).
#[derive(serde::Serialize)]
struct DebugConfigData {
    /// Envelope data-schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// ISO-8601 generation timestamp.
    #[serde(rename = "generatedAt")]
    generated_at: String,
    /// Resolved debug target kind.
    #[serde(rename = "targetKind")]
    target_kind: DebugTargetKind,
    /// Resolved debug server backend.
    server: DebugServerKind,
    /// `true` when previewing only (no write performed).
    preview: bool,
    /// Path to the `.vscode/launch.json` that was (or would be) written.
    #[serde(rename = "launchJsonPath")]
    launch_json_path: String,
    /// `true` when an existing launch config was replaced rather than appended.
    replaced: bool,
    /// Human-readable preview/usage notes.
    notes: Vec<String>,
    /// The launch configuration itself — the very thing the command produces.
    /// Additive: the envelope used to describe the write (path, replaced,
    /// notes) without carrying the object, so an automated consumer had to
    /// re-read `launch.json` or scrape the text preview to see what was
    /// generated (alp-sdk-vscode#339).
    configuration: Value,
}

/// Entry point for `tan debug-config`: parse target/server, build the launch
/// draft, then preview it (`--preview`) or merge it into `.vscode/launch.json`.
pub fn run(g: &GlobalArgs, args: &DebugConfigArgs) -> CommandRun {
    let generated_at = generated_at_iso();
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));

    // Errors before workspace resolution report a cwd-based launch.json path
    // and a zephyr-mcu/none placeholder (matches the TS catch block).
    let cwd_launch_path = || {
        cwd.join(".vscode")
            .join("launch.json")
            .to_string_lossy()
            .to_string()
    };

    let target = match parse_target_kind(args.target_kind.as_deref()) {
        Ok(t) => t,
        Err(message) => return internal_failure(g, &generated_at, message, cwd_launch_path()),
    };
    let server = match parse_server_kind(args.server.as_deref()) {
        Ok(s) => s,
        Err(message) => return internal_failure(g, &generated_at, message, cwd_launch_path()),
    };
    let mut draft = match create_launch_draft(target, server, args.pre_launch_task.as_deref()) {
        Ok(d) => d,
        Err(message) => return internal_failure(g, &generated_at, message, cwd_launch_path()),
    };

    let project_arg = g.project.clone().unwrap_or_else(|| ".".to_string());
    let workspace_root = normalize_path(&cwd.join(&project_arg));
    let launch_json_path = workspace_root
        .join(".vscode")
        .join("launch.json")
        .to_string_lossy()
        .to_string();

    // tan-cli#170: every other command's `Project.root`/`Project.board_yaml`
    // come from this SAME shared resolver (`bootstrap`, `doctor`, `presets`,
    // `validate`, …); `debug-config` was the one holdout still hardcoding
    // `board_yaml: None` on every path, even a success with a valid
    // `board.yaml` sitting in the resolved root. Bound once (not just for
    // `board_yaml_path`) so the reported `project.root` is this SAME
    // `context.workspace_root` — already posix-normalized, like every other
    // command's golden — instead of the locally-computed `workspace_root:
    // PathBuf` below's native `to_string_lossy()`, which put a
    // native-backslash `root` next to a forward-slash `boardYaml` in the same
    // envelope object on Windows (#170 follow-up). Reporting-only — no
    // consumer binds either field yet. The `_no_sdk_report` variant: unlike
    // every other caller of this resolver, `debug-config` does not DRIVE the
    // SDK the way `build`/`size`/`validate` do, so it must not add an
    // undeclared `sdk` envelope key as a side effect of a field it merely
    // reports (tan-cli#111 follow-up). alp-sdk#1026's metadata fallback below
    // (`fill_debug_probe_identity_from_sdk`) does now best-effort READ under
    // `context.sdk_root` when one resolves — that stays a silent, optional
    // enrichment exactly like the `board/system-manifest.yaml` read already
    // was, not a new reported dependency, so the choice not to record here
    // is unchanged.
    let context = resolve_cli_project_context_no_sdk_report(g);
    //
    // tan-cli#236 completes it: built through the shared constructor, so
    // `boardYaml` is null when nothing is actually at the resolved path.
    // Routing `board_yaml` through the resolver (above) without this would have
    // traded a hardcoded null for a path to a file that need not exist — the
    // same field disagreeing with the filesystem, in the other direction.
    let project = Project::from_context(&context);

    // Fill the `<resolved-…>` placeholders from what this project's own build
    // recorded (#66). Nothing here fails the command: pre-build, or against a
    // Zephyr that reshaped `runners.yaml`, the draft keeps its placeholders.
    let (mut resolution, registered_runners, build_core_id) =
        resolve_from_build(&workspace_root, target, server, args.core.as_deref());

    // alp-sdk#1026: whatever the build did NOT already resolve, try the SDK's
    // published per-variant debug-probe identity next — `--core` if given,
    // else the core id the build itself just resolved. `targetId` (pyOCD)
    // needs neither: `pyocd_target` is a scalar per variant, so it resolves
    // pre-build with no `--core` and no prior build at all. `device` (J-Link)
    // is the opposite: `jlink_device` is keyed BY core id, so on a
    // never-built project with no `--core`, `identity_core` is `None` and
    // `device` stays the placeholder — that combination is deliberately
    // covered by a test (`fill_debug_probe_identity_gaps_never_guesses_a_device_without_a_matching_core_id`
    // in `tan_core::debug_launch`, and `debug_config_jlink_device_stays_the_placeholder_with_no_core_and_no_build`
    // here) rather than left silently unresolved with no coverage.
    let identity_core = args.core.clone().or(build_core_id);
    let before_identity_fill = resolution.clone();
    let identity_debug_block_found =
        fill_debug_probe_identity_from_sdk(&mut resolution, &context, identity_core.as_deref());
    // Which launch-configuration JSON keys the SDK fallback (not a real
    // build) just populated — the ONLY fields `sdk_identity_overwrites` below
    // is allowed to flag (alp-sdk#1026 review finding #1). A field a real
    // build already resolved is excluded here even though it may ALSO
    // overwrite a customer's value: that overwrite is pre-existing, intended
    // behaviour (`merge_configuration`'s own doc comment), not something this
    // PR introduces or is scoped to disclose.
    let mut sdk_filled_json_fields: Vec<&'static str> = Vec::new();
    if before_identity_fill.device.is_none() && resolution.device.is_some() {
        sdk_filled_json_fields.push("device");
    }
    if before_identity_fill.target_id.is_none() && resolution.target_id.is_some() {
        sdk_filled_json_fields.push("targetId");
    }
    if before_identity_fill.config_files.is_empty() && !resolution.config_files.is_empty() {
        sdk_filled_json_fields.push("configFiles");
    }

    // `--svd` is the ONLY producer of `resolution.svd` (tan-cli#197): the SDK
    // ships no SVD, so without the flag the field is structurally always
    // `None` and `apply_launch_resolution` drops both svd keys.
    if let Some(svd_arg) = args.svd.as_deref() {
        match resolve_user_svd(&cwd, &workspace_root, svd_arg) {
            Ok(svd) => resolution.svd = Some(svd),
            Err(message) => {
                return internal_failure(g, &generated_at, message, launch_json_path);
            }
        }
    }

    apply_launch_resolution(&mut draft, &resolution);
    let mut notes = preview_notes_for(&draft, &registered_runners, server);
    // A non-MCU draft carries no `svdFile` key at all, and
    // `apply_launch_resolution` only replaces keys that already exist — so a
    // `--svd` here is a no-op. Say so rather than accepting the flag in
    // silence and leaving the user to wonder why no peripheral view appeared.
    if args.svd.is_some() && draft.get("svdFile").is_none() {
        notes.push(format!(
            "--svd was given, but target kind '{}' emits no svdFile field, so it had no effect: \
             the Cortex Peripherals view is a cortex-debug (MCU) feature.",
            args.target_kind.as_deref().unwrap_or("zephyr-mcu"),
        ));
    }

    // alp-sdk#1026 review finding #4: the generic "Placeholder fields..."
    // note is real but unspecific — running `--server openocd` today gives
    // `issues: []` / `ok: true` with the only signal being a note that names
    // `device`, a key an OpenOCD draft does not even carry. When the SDK DID
    // resolve an identity for this variant but not the specific field THIS
    // server needs (every Alif variant today, for `openocd_config`), say so
    // explicitly — on preview too, not just a write, since this is advisory
    // about resolution state, not about what a write changed on disk.
    let mut identity_issues: Vec<Issue> = Vec::new();
    if identity_debug_block_found {
        if let Some(field) = server_identity_field(server) {
            if draft.get(field).map(has_placeholder).unwrap_or(false) {
                identity_issues.push(sdk_identity_key_absent_issue(field));
            }
        }
    }

    if args.preview {
        return success(
            g,
            &generated_at,
            target,
            server,
            &launch_json_path,
            true,
            false,
            &notes,
            &draft,
            project,
            identity_issues,
        );
    }

    // Write mode: merge into .vscode/launch.json.
    let vscode_dir = Path::new(&launch_json_path)
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| workspace_root.join(".vscode"));
    if let Err(e) = std::fs::create_dir_all(&vscode_dir) {
        return write_failure(
            g,
            &generated_at,
            target,
            server,
            &launch_json_path,
            e.to_string(),
        );
    }

    // `.ok()` here used to collapse a READ error on an EXISTING launch.json
    // (wrong encoding e.g. UTF-16LE from PowerShell `>` redirection, a denied
    // ACL, a sharing violation) into the same `None` as "no file yet". That
    // fed create_launch_json_write_plan(None, ...), which builds a *fresh*
    // document and the write below then overwrote the user's file wholesale
    // — silently destroying every hand-written debug configuration at exit 0.
    // The malformed-JSON case just below is deliberately guarded (no write);
    // a read error must refuse to write for the same reason.
    let existing = if Path::new(&launch_json_path).exists() {
        match std::fs::read_to_string(&launch_json_path) {
            Ok(content) => Some(content),
            Err(e) => {
                return internal_failure(
                    g,
                    &generated_at,
                    format!("Alp: failed to read existing .vscode/launch.json: {e}"),
                    cwd_launch_path(),
                );
            }
        }
    } else {
        None
    };

    // alp-sdk#1026 review finding #1: compute this BEFORE the write, against
    // the file as it stood — `create_launch_json_write_plan` below already
    // performs the same overwrite (that part of its behaviour is intentional,
    // see its own doc comment), this only detects it so it can be disclosed.
    let sdk_identity_overwrites =
        tan_core::sdk_identity_overwrites(existing.as_deref(), &draft, &sdk_filled_json_fields);

    let plan = match create_launch_json_write_plan(existing.as_deref(), &draft) {
        Ok(p) => p,
        // A malformed existing launch.json surfaces as an internal failure in TS.
        Err(message) => return internal_failure(g, &generated_at, message, cwd_launch_path()),
    };

    if let Err(e) = std::fs::write(&launch_json_path, &plan.content) {
        return write_failure(
            g,
            &generated_at,
            target,
            server,
            &launch_json_path,
            e.to_string(),
        );
    }

    // #133 reopened: report a legacy-entry migration so the customer knows
    // WHY the file changed under them (their old `"ALP: ..."` entry is gone,
    // folded into the correctly-named one) rather than discovering it only by
    // diffing the file themselves.
    let mut issues = identity_issues;
    if let Some(from) = &plan.migrated_from {
        issues.push(legacy_entry_migrated_issue(
            from,
            draft["name"].as_str().unwrap_or_default(),
        ));
    }
    // tan-cli#179: the ordinary same-name merge left a DIFFERENT leftover
    // legacy entry silently untouched — say so, even though (unlike a
    // migration) nothing about the file's shape changed because of it.
    if let Some(legacy) = &plan.legacy_entry_present {
        issues.push(legacy_entry_untouched_issue(legacy));
    }
    // #182 review finding #2: a splice or fallback write that dropped a
    // comment (or trailing comma) the customer's file held must say so —
    // #182 named unqualified success on a write that destroys user-authored
    // content as the one thing that is never acceptable, not just "diffable".
    if plan.comments_dropped {
        issues.push(comments_dropped_issue());
    }
    // alp-sdk#1026 review finding #1: this write just replaced a concrete
    // existing value with one resolved from the SDK's published debug-probe
    // identity rather than a real build — say so, the same way a dropped
    // comment is disclosed rather than left for the customer to notice by
    // diffing the file themselves.
    for (field, existing_value, incoming_value) in &sdk_identity_overwrites {
        issues.push(sdk_identity_overwrite_issue(
            field,
            existing_value,
            incoming_value,
        ));
    }

    success(
        g,
        &generated_at,
        target,
        server,
        &launch_json_path,
        false,
        plan.replaced,
        &notes,
        // tan-cli#180: report what this write actually put in the file — the
        // merged/migrated result — never the fresh `draft`, which still
        // carries its own `<resolved-…>` placeholders even after a merge
        // resolved them from the customer's real, hand-filled values.
        &plan.written_configuration,
        project,
        issues,
    )
}

/// The #133 migration report: emitted when a pre-#155 `"ALP: ..."` entry was
/// found in place of the current `"Alp: ..."` name and adopted onto it (see
/// `tan_core::debug_launch::create_launch_json_write_plan`). Severity `info`,
/// not `warning` or `error` — nothing failed and no action is required; this
/// exists so an automated consumer (or a customer reading `--format json`)
/// can tell WHY the file changed under them instead of only diffing it.
fn legacy_entry_migrated_issue(from: &str, to: &str) -> Issue {
    Issue {
        code: "debug-config.legacy-entry-migrated".to_string(),
        severity: "info".to_string(),
        message: format!(
            "Migrated the legacy launch-configuration entry \"{from}\" into \"{to}\". \
             Any value you had hand-filled in on the old entry for an unresolved-\
             placeholder field (device, miDebuggerServerAddress, configFiles, …) \
             carried across; every other field tan owns was refreshed to this run's \
             values, same as an ordinary re-run. The old entry is gone."
        ),
    }
}

/// tan-cli#179: emitted when the ORDINARY same-name merge ran (an exact hit
/// against the current `"Alp: ..."` name) and a legacy `"ALP: ..."`
/// counterpart of the SAME draft ALSO still sits in the file. Distinct from
/// [`legacy_entry_migrated_issue`], which fires on the MISS path where the
/// legacy entry is the one adopted — here NEITHER entry was touched beyond
/// the ordinary merge, so the customer's real hand-filled values may still be
/// stranded on the leftover entry with nothing pointing at it. Severity
/// `info`, same reasoning as the migration notice: nothing failed and there
/// is no forced action, but silence here is exactly the #133 symptom the
/// customer hits next.
fn legacy_entry_untouched_issue(legacy_name: &str) -> Issue {
    Issue {
        code: "debug-config.legacy-entry-untouched".to_string(),
        severity: "info".to_string(),
        message: format!(
            "A leftover legacy launch-configuration entry \"{legacy_name}\" still sits in \
             .vscode/launch.json alongside the entry this run updated. It was left \
             untouched — nothing decides which of the two you may have hand-edited is \
             authoritative — so if you filled in real values on the legacy entry, copy \
             them onto the maintained one and remove the legacy entry yourself."
        ),
    }
}

/// tan-cli#182 review finding #2: emitted whenever this write dropped a
/// comment (or trailing comma) sitting inside a byte span it rewrote — the
/// one maintained entry a splice replaced, or, on the whole-document
/// fallback, the customer's entire original file. Severity `info`, same as
/// [`legacy_entry_migrated_issue`]: nothing failed and there is no action to
/// take, but a tool that discarded user-authored content must never report
/// unqualified success (#182's own non-negotiable floor).
fn comments_dropped_issue() -> Issue {
    Issue {
        code: "debug-config.comments-dropped".to_string(),
        severity: "info".to_string(),
        message: "This write dropped a comment (or trailing comma) that sat inside the \
                   part of .vscode/launch.json it rewrote — either inside the one entry \
                   being updated, or, if the file's shape couldn't be confidently \
                   spliced, anywhere in the file. Everything outside that span is \
                   untouched."
            .to_string(),
    }
}

/// alp-sdk#1026 review finding #1: emitted whenever the SDK's published
/// debug-probe identity (not a real build) just replaced a concrete existing
/// value on the entry this run wrote. Severity `info`, same reasoning as its
/// three siblings above: the overwrite itself is not new or wrong (a value
/// resolved from a real build already overwrote unconditionally, by design —
/// see `tan_core::debug_launch::merge_configuration`'s doc comment) but a
/// tool that replaces a customer's own value at `exit 0` with `issues: []`
/// has told them nothing happened.
fn sdk_identity_overwrite_issue(field: &str, existing_value: &str, incoming_value: &str) -> Issue {
    Issue {
        code: "debug-config.sdk-identity-overwrite".to_string(),
        severity: "info".to_string(),
        message: format!(
            "This write replaced the existing `{field}` value \"{existing_value}\" with \
             \"{incoming_value}\", resolved from the SDK's published debug-probe identity \
             (alp-sdk#987) rather than from a real build. If \"{existing_value}\" was a value \
             you filled in on purpose — e.g. a J-Link flash-unlock device profile more specific \
             than the generic attach device the SDK publishes — restore it in \
             .vscode/launch.json; a value tan itself resolves from a real build will overwrite \
             it again the same way."
        ),
    }
}

/// alp-sdk#1026 review finding #4: emitted when the SDK DID publish a
/// debug-probe identity for this project's SoC variant, but that identity
/// does not (yet) include a value for `field` — distinct from, and more
/// specific than, the generic "Placeholder fields..." note every unresolved
/// field already gets regardless of why. Severity `info`: this is the
/// schema's own documented stance (`soc-spec-v1.schema.json:379`) that an
/// unpopulated key is a published "unknown", not an error and not a bug.
fn sdk_identity_key_absent_issue(field: &str) -> Issue {
    Issue {
        code: "debug-config.sdk-identity-key-absent".to_string(),
        severity: "info".to_string(),
        message: format!(
            "This SoM's SDK-published debug-probe identity (alp-sdk#987) does not include a \
             value for `{field}` yet, so it stays the placeholder shown in `configuration` — an \
             unpopulated key is the correct published \"unknown\" (alp-sdk#1026), never a guess."
        ),
    }
}

/// Build a success `CommandRun`: emit the JSON envelope (or text lines) for a
/// completed preview or write at `ExitCode::Success`.
///
/// `configuration` is the launch configuration to REPORT — the caller decides
/// which one that is. `--preview` never merges anything (it returns before
/// the customer's file is even read), so it passes the fresh draft, which is
/// also all there is. A write passes the write plan's own
/// `written_configuration` instead (tan-cli#180): the merged/migrated result
/// that actually landed on disk, not the draft's stale `<resolved-…>`
/// placeholders a merge may have already overwritten with the customer's
/// real values.
#[allow(clippy::too_many_arguments)]
fn success(
    g: &GlobalArgs,
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
    launch_json_path: &str,
    preview: bool,
    replaced: bool,
    notes: &[String],
    configuration: &Value,
    project: Project,
    issues: Vec<Issue>,
) -> CommandRun {
    let data = DebugConfigData {
        schema_version: "1".to_string(),
        generated_at: generated_at.to_string(),
        target_kind: target,
        server,
        preview,
        launch_json_path: launch_json_path.to_string(),
        replaced,
        notes: notes.to_vec(),
        configuration: configuration.clone(),
    };
    let text = if g.is_json() {
        Vec::new()
    } else {
        debug_config_text(
            target,
            server,
            launch_json_path,
            replaced,
            preview,
            notes,
            configuration,
            g,
            &issues,
        )
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "debug-config",
            project,
            data,
            issues,
            ExitCode::Success.code(),
        )
        .to_json()
    });
    CommandRun {
        exit: ExitCode::Success,
        text,
        json,
    }
}

/// Failure `CommandRun` for invalid kind / unsupported backend / malformed
/// existing launch.json: exits `InternalFailure` (5) with a `zephyr-mcu`/`none`
/// placeholder target.
fn internal_failure(
    g: &GlobalArgs,
    generated_at: &str,
    message: String,
    launch_json_path: String,
) -> CommandRun {
    failure_envelope(
        g,
        generated_at,
        DebugTargetKind::ZephyrMcu,
        DebugServerKind::None,
        launch_json_path,
        ExitCode::InternalFailure,
        "internal-failure",
        message,
        vec!["debug-config: internal failure".to_string()],
    )
}

/// Failure `CommandRun` for a filesystem error while creating the directory or
/// writing launch.json: exits `WriteFailure` (3), preserving the resolved
/// target/server.
fn write_failure(
    g: &GlobalArgs,
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
    launch_json_path: &str,
    message: String,
) -> CommandRun {
    failure_envelope(
        g,
        generated_at,
        target,
        server,
        launch_json_path.to_string(),
        ExitCode::WriteFailure,
        "write-failure",
        message,
        vec!["debug-config: failed to write launch.json.".to_string()],
    )
}

/// Shared failure path: assemble the issue + `data` payload, emit text or a
/// null-project JSON envelope, and return a `CommandRun` at the given `exit`.
#[allow(clippy::too_many_arguments)]
fn failure_envelope(
    g: &GlobalArgs,
    generated_at: &str,
    target: DebugTargetKind,
    server: DebugServerKind,
    launch_json_path: String,
    exit: ExitCode,
    code: &str,
    message: String,
    mut text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("debug-config.{code}"),
        severity: "error".to_string(),
        message: message.clone(),
    }];
    let data = DebugConfigData {
        schema_version: "1".to_string(),
        generated_at: generated_at.to_string(),
        target_kind: target,
        server,
        preview: false,
        launch_json_path,
        replaced: false,
        notes: Vec::new(),
        // No draft exists on this path — the failure happened before (or
        // instead of) generating one. `null`, not an empty object, so a
        // consumer cannot mistake it for a configuration with no fields.
        configuration: Value::Null,
    };
    let text = if g.is_json() {
        Vec::new()
    } else {
        text_lines.push(message);
        text_lines
    };
    // TS createFailureResult reports a null project.
    let json = g.is_json().then(|| {
        Envelope::new(
            "debug-config",
            Project {
                root: None,
                board_yaml: None,
            },
            data,
            issues,
            exit.code(),
        )
        .to_json()
    });
    CommandRun { exit, text, json }
}

/// Render the human-readable (non-JSON) output lines for a successful preview
/// or write, including the pretty-printed launch document and notes unless
/// `--quiet`.
#[allow(clippy::too_many_arguments)]
fn debug_config_text(
    target: DebugTargetKind,
    server: DebugServerKind,
    launch_json_path: &str,
    replaced: bool,
    preview: bool,
    notes: &[String],
    draft: &Value,
    g: &GlobalArgs,
    issues: &[Issue],
) -> Vec<String> {
    let mut lines = Vec::new();
    if preview {
        lines.push(format!(
            "debug-config: preview target={} server={}",
            target.as_str(),
            server.as_str()
        ));
        lines.push(format!("launch.json path: {launch_json_path}"));
        if !g.quiet {
            lines.push(String::new());
            let document = launch_preview_document(draft.clone());
            lines.push(serde_json::to_string_pretty(&document).unwrap_or_default());
            lines.push(String::new());
            lines.extend(notes.iter().map(|n| format!("note: {n}")));
        }
    } else {
        let action = if replaced { "updated" } else { "written" };
        lines.push(format!(
            "debug-config: {action} target={} server={}",
            target.as_str(),
            server.as_str()
        ));
        lines.push(format!("launch.json: {launch_json_path}"));
        // Always shown, even under --quiet: this is a one-time notice that the
        // file just lost a differently-named entry (folded into this one), not
        // routine noise like the resolution notes below it.
        for issue in issues {
            if issue.code == "debug-config.legacy-entry-migrated" {
                lines.push(format!("debug-config: {}", issue.message));
            }
        }
        // tan-cli#179: same treatment — a leftover legacy entry sitting
        // untouched next to the one this run just updated is exactly the
        // kind of fact that must survive --quiet, not routine resolution
        // noise.
        for issue in issues {
            if issue.code == "debug-config.legacy-entry-untouched" {
                lines.push(format!("debug-config: {}", issue.message));
            }
        }
        // Same treatment for a dropped comment/trailing comma (#182 review
        // finding #2): a notice about content this run destroyed is never
        // routine noise, so it survives --quiet too.
        for issue in issues {
            if issue.code == "debug-config.comments-dropped" {
                lines.push(format!("note: {}", issue.message));
            }
        }
        if !g.quiet {
            lines.extend(notes.iter().map(|n| format!("note: {n}")));
        }
    }
    lines
}

/// The manifest `os` a debug target class runs on, or `None` for a target with
/// no per-core build slice keyed by `os`. `NativeHost` is exactly that case —
/// its slice is selected by board target instead, in [`select_slice`].
fn manifest_os_for_target(target: DebugTargetKind) -> Option<&'static str> {
    match target {
        DebugTargetKind::ZephyrMcu => Some("zephyr"),
        DebugTargetKind::BaremetalMcu => Some("baremetal"),
        DebugTargetKind::YoctoUserspace => Some("yocto"),
        DebugTargetKind::NativeHost => None,
    }
}

/// Select the manifest slice a debug draft resolves against, for a given
/// target/`--core`.
///
/// `NativeHost` is a special case: its runnable artefact is the project's
/// `native_sim` slice, found by board target via
/// [`tan_core::run::native_sim_slice`] — the SAME discriminator `tan run`
/// uses to pick the host binary — not by `os`. A board that also builds a
/// real Zephyr MCU slice still has one or more slices with `os: zephyr`; the
/// old `os`-keyed match took the first of those, which on such a board is
/// often the MCU slice, pointing `Alp: Native Sim Debug` at a Cortex-M ELF
/// that CodeLLDB then can't launch on the host. `--core` is intentionally
/// unused on this arm: a `native_sim` slice's `core_id` is not a hardware
/// core selector.
///
/// Every other target kind keeps the existing `os` + `--core` match.
fn select_slice<'a>(
    manifest: &'a SystemManifest,
    target: DebugTargetKind,
    core: Option<&str>,
) -> Option<&'a Slice> {
    if target == DebugTargetKind::NativeHost {
        return native_sim_slice(manifest);
    }
    let os = manifest_os_for_target(target)?;
    // `--core` names the slice outright; otherwise the first slice of this
    // target's OS wins, which is the whole manifest on a single-core project.
    manifest
        .slices
        .iter()
        .find(|s| s.os == os && core.map(|c| s.core_id == c).unwrap_or(true))
}

/// The `runners.yaml` runner id a debug server reads its arguments from.
fn runner_id_for_server(server: DebugServerKind) -> Option<&'static str> {
    match server {
        DebugServerKind::Jlink => Some("jlink"),
        DebugServerKind::Openocd => Some("openocd"),
        DebugServerKind::Pyocd => Some("pyocd"),
        DebugServerKind::Gdbserver | DebugServerKind::None => None,
    }
}

/// The launch-configuration JSON key the SDK's debug-probe identity
/// (`variants[].debug`) resolves for a given server — `None` for a server the
/// identity has no concept of at all (`gdbserver`/`none`, neither of which
/// `create_launch_draft` ever pairs with a `variants[].debug` field).
fn server_identity_field(server: DebugServerKind) -> Option<&'static str> {
    match server {
        DebugServerKind::Jlink => Some("device"),
        DebugServerKind::Openocd => Some("configFiles"),
        DebugServerKind::Pyocd => Some("targetId"),
        DebugServerKind::Gdbserver | DebugServerKind::None => None,
    }
}

/// Rewrite a path under `workspace_root` as `${workspaceFolder}/<rel>`, so a
/// committed `launch.json` stays portable; an artefact outside the project
/// (an out-of-tree build root) is left absolute rather than mangled.
fn workspace_relative(workspace_root: &Path, path: &str) -> String {
    Path::new(path)
        .strip_prefix(workspace_root)
        .ok()
        .map(|rel| format!("${{workspaceFolder}}/{}", rel.to_string_lossy()))
        .unwrap_or_else(|| path.to_string())
}

/// Resolve `--svd` into the value the launch configuration should carry.
///
/// **Anchor: the current directory, not the project root.** `--svd` is a
/// per-invocation flag typed at a shell prompt, so a relative path means what
/// the shell means by it. (A board-level `debug.svd` key, should one ever be
/// added, travels with the project and must anchor on the project root
/// instead — the two have different lifetimes, so they get different anchors
/// deliberately rather than by omission.) The emitted string then goes through
/// the same [`workspace_relative`] rewrite as `executable`: inside the project
/// it becomes `${workspaceFolder}/…` so a committed launch.json stays
/// portable, outside it stays absolute — which is the normal case here, since
/// a vendor SVD lives in the vendor SDK the user installed.
///
/// **A bad path is a HARD ERROR, never a silent drop back to "no SVD".**
/// tan-cli#67 established that cortex-debug fails the whole session on an
/// `svdFile` it cannot read, which is why the *unresolved* case drops the key.
/// But the user explicitly named this file: falling back would make a typo
/// indistinguishable from not passing the flag, and the failure would surface
/// as an unexplained empty peripheral view. Fail here, where the message can
/// name the path.
fn resolve_user_svd(cwd: &Path, workspace_root: &Path, arg: &str) -> Result<String, String> {
    if arg.trim().is_empty() {
        return Err("Alp: --svd was given an empty path.".to_string());
    }
    // `join` on an absolute `arg` replaces the base, so this handles both.
    let candidate = normalize_path(&cwd.join(arg));
    let meta = std::fs::metadata(&candidate).map_err(|e| {
        format!(
            "Alp: --svd path cannot be read: {} ({e}). \
             Pass the path to the vendor's own .svd file; the SDK ships none (alp-sdk#948).",
            candidate.display(),
        )
    })?;
    if !meta.is_file() {
        return Err(format!(
            "Alp: --svd path is not a file: {}",
            candidate.display(),
        ));
    }
    Ok(workspace_relative(
        workspace_root,
        &candidate.to_string_lossy(),
    ))
}

/// Everything this project's own build knows about how to debug it: the
/// per-core ELF from `system-manifest.yaml`, and the probe/tool paths from that
/// slice's `runners.yaml` — the same file `west flash` reads.
///
/// Best-effort throughout. A missing manifest (pre-build), a missing slice, an
/// unreadable or reshaped `runners.yaml` each leave the corresponding field
/// unresolved instead of failing the command: `debug-config` must still emit
/// its draft before the first build.
///
/// The third return value is the `core_id` of the slice this run actually
/// selected (`None` before a matching slice is found) — the SAME id `--core`
/// would have named explicitly. alp-sdk#1026's SDK-metadata fallback (see
/// `fill_debug_probe_identity_from_sdk`) needs it to index `jlink_device`
/// (keyed per core) even when the caller passed no `--core` of its own, so a
/// single-core project's ALREADY-built slice still resolves without forcing
/// the user to repeat a core id `tan` already knows.
fn resolve_from_build(
    workspace_root: &Path,
    target: DebugTargetKind,
    server: DebugServerKind,
    core: Option<&str>,
) -> (LaunchResolution, Vec<String>, Option<String>) {
    let mut resolution = LaunchResolution::default();
    let manifest_path = workspace_root.join("build").join("system-manifest.yaml");
    let Ok(yaml) = std::fs::read_to_string(&manifest_path) else {
        return (resolution, Vec::new(), None);
    };
    let Ok(manifest) = parse_system_manifest(&yaml) else {
        return (resolution, Vec::new(), None);
    };
    let Some(slice) = select_slice(&manifest, target, core) else {
        return (resolution, Vec::new(), None);
    };
    let core_id = Some(slice.core_id.clone());

    if let Some(artefact) = slice.output_artefact.as_deref().filter(|a| !a.is_empty()) {
        // A manifest records the ELF for EVERY zephyr slice, native_sim
        // included: `resolve_zephyr_artefact` (build/execute/manifest.rs) is
        // tan's only writer of `output_artefact` and stores
        // `<slice-cwd>/build/zephyr/zephyr.elf` unconditionally — there is no
        // `.exe` branch, and alp-sdk (planner-only) never writes the field at
        // all.
        // So a host target needs the sibling swap, via the same tan-core
        // helper `tan run` uses; every other target kind genuinely wants the
        // artefact verbatim. #83 took it verbatim here too, which pointed
        // `Alp: Native Sim Debug` at a `zephyr.elf` CodeLLDB cannot launch.
        let artefact = match target {
            DebugTargetKind::NativeHost => native_sim_exe_beside(artefact),
            _ => artefact.to_string(),
        };
        resolution.executable = Some(workspace_relative(workspace_root, &artefact));
    }

    let Some(build_dir) = slice.build_dir.as_deref().filter(|b| !b.is_empty()) else {
        return (resolution, Vec::new(), core_id);
    };
    let runners_path = Path::new(build_dir).join("zephyr").join("runners.yaml");
    let Ok(text) = std::fs::read_to_string(&runners_path) else {
        return (resolution, Vec::new(), core_id);
    };
    let Ok(runners) = parse_runners_config(&text) else {
        return (resolution, Vec::new(), core_id);
    };

    resolution.gdb_path = runners.gdb.clone();
    if let Some(runner) = runner_id_for_server(server) {
        match server {
            DebugServerKind::Jlink => {
                resolution.device = runner_arg_value(&runners, runner, "--device");
            }
            DebugServerKind::Openocd => {
                resolution.server_path = runners.openocd.clone();
                resolution.search_dirs = runners.openocd_search.clone();
                resolution.config_files = runner_arg_values(&runners, runner, "--config");
            }
            DebugServerKind::Pyocd => {
                resolution.target_id = runner_arg_value(&runners, runner, "--target");
            }
            DebugServerKind::Gdbserver | DebugServerKind::None => {}
        }
    }
    (resolution, runners.runners.clone(), core_id)
}

/// alp-sdk#1026: fill `resolution`'s remaining `device`/`target_id`/
/// `config_files` gaps from the SDK's published per-variant debug-probe
/// identity (`variants[].debug`, alp-sdk#987), so `tan debug-config` resolves
/// a real J-Link device / pyOCD target before the project has ever been
/// built — the case `resolve_from_build`'s `runners.yaml` read structurally
/// cannot cover.
///
/// Reuses the SAME metadata-layout walk `tan size` drives
/// (`crate::util::read_sdk_som_and_soc`) instead of a second walk of
/// `metadata/socs/**` — the exact drift #1026 itself is about (a schema with
/// no reader, then two readers that could disagree). Pure fill-the-gap logic
/// is `fill_debug_probe_identity_gaps` (`tan_core::debug_launch`); everything
/// here is the IO side: locating `board.yaml`, reading `som.sku` out of it,
/// then the shared SoM-preset/SoC-JSON read and (unlike `tan size`) a
/// forward-only `resolve_variant` match.
///
/// Best-effort throughout, exactly like `resolve_from_build`: a missing
/// `board.yaml`/`som.sku`, no resolved SDK root, a missing/unreadable SoM
/// preset or SoC-JSON file, or a SoC variant that resolves but declares no
/// `debug` block each leave `resolution` exactly as it was — the caller's
/// existing placeholder note still applies, and nothing here can fail the
/// command.
///
/// Returns whether a `variants[].debug` block was actually found for the
/// resolved SoC variant — distinct from whether every field this run wanted
/// got filled from it. `run` uses this (alp-sdk#1026 review finding #4) to
/// tell "the SDK publishes an identity for this part, but not a value for
/// the specific field this server needs yet" (e.g. every Alif variant today,
/// for `openocd_config`) apart from "no identity was resolvable at all" —
/// only the former is worth a dedicated notice; the latter is already the
/// generic "still needs resolution" note every unresolved field gets.
fn fill_debug_probe_identity_from_sdk(
    resolution: &mut LaunchResolution,
    context: &ProjectContext,
    core_id: Option<&str>,
) -> bool {
    let Some(board_yaml_path) = context.board_yaml_path.as_deref() else {
        return false;
    };
    let Ok(board_text) = std::fs::read_to_string(board_yaml_path) else {
        return false;
    };
    let Ok(model) = parse_board_model(&board_text) else {
        return false;
    };
    let Some(sku) = model.som.and_then(|som| som.sku) else {
        return false;
    };
    let Some(sdk_root) = context.sdk_root.as_deref() else {
        return false;
    };
    let metadata_root = Path::new(sdk_root).join("metadata");

    // Shared metadata-layout walk with `tan size` — see `read_sdk_som_and_soc`'s
    // doc comment. `sku: None` here (unlike `tan size`) deliberately disables
    // `resolve_variant`'s sku reverse-fallback: a drifted/`TBD` preset must
    // resolve NO identity rather than possibly a WRONG one that still
    // connects a live debug session to the wrong part (alp-sdk#1026 review
    // finding #7) — a missing budget is a lesser harm than a wrong device.
    let Some((preset, soc)) = crate::util::read_sdk_som_and_soc(&metadata_root, &sku) else {
        return false;
    };
    let variants: Vec<SocVariant> = soc
        .get("variants")
        .and_then(|v| serde_json::from_value(v.clone()).ok())
        .unwrap_or_default();
    let Some(variant) = resolve_variant(preset.silicon_variant.as_deref(), None, &variants) else {
        return false;
    };
    let Some(debug) = variant.debug.as_ref() else {
        return false;
    };
    fill_debug_probe_identity_gaps(
        resolution,
        core_id,
        &debug.jlink_device,
        debug.pyocd_target.as_deref(),
        debug.openocd_config.as_deref(),
    );
    true
}

/// Whether any `<…>` placeholder survived resolution, anywhere in the draft —
/// including inside `configFiles`, which is an array.
///
/// The string test is [`is_unresolved_placeholder`], the SAME predicate the
/// launch.json merge uses, so "keep the still-needs-resolution note" and "do
/// not overwrite this by hand-filled value" can never disagree. It used to be
/// `s.contains("<resolved-")`, which called the two-token `<host>:<port>` a
/// real address: a yocto config whose `<resolved-gdb>` resolved then dropped
/// the note while `miDebuggerServerAddress` was still unusable.
fn has_placeholder(value: &Value) -> bool {
    match value {
        Value::String(s) => is_unresolved_placeholder(s),
        Value::Array(items) => items.iter().any(has_placeholder),
        Value::Object(map) => map.values().any(has_placeholder),
        _ => false,
    }
}

/// The preview notes, minus the "still needs resolution" warning once nothing
/// is left to resolve. Keyed off the FINAL draft rather than off "did anything
/// resolve": a partly-resolved config (a board that registers no OpenOCD runner
/// still has `<resolved-openocd-board-cfg>`) must keep the warning, and a fully
/// resolved one must lose it — otherwise the note is noise on configs that are
/// fine and silence on configs that are not.
fn preview_notes_for(
    draft: &Value,
    registered_runners: &[String],
    server: DebugServerKind,
) -> Vec<String> {
    let mut notes: Vec<String> = launch_preview_notes()
        .into_iter()
        .filter(|n| !n.starts_with("Placeholder fields") || has_placeholder(draft))
        .collect();
    // The most common reason a placeholder survives: the board never registered
    // this server. Say so, instead of leaving the user to wonder which project-
    // specific value they are supposed to invent.
    if let Some(runner) = runner_id_for_server(server) {
        if !registered_runners.is_empty() && !registered_runners.iter().any(|r| r == runner) {
            notes.push(format!(
                "This build registers no '{runner}' runner (runners.yaml: {registered_runners:?}), \
                 so its fields could not be resolved.",
            ));
        }
    }
    notes
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-debug-config-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn global(project: &Path) -> GlobalArgs {
        GlobalArgs {
            project: Some(project.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Text,
            verbose: false,
            quiet: true,
            no_color: true,
            non_interactive: true,
            ci: false,
        }
    }

    // Regression for the data-loss bug: a read error on an EXISTING
    // launch.json (here, non-UTF-8 bytes as PowerShell `>` redirection would
    // produce) must refuse to write, exactly like the malformed-JSON case.
    // Before the fix, `.ok()` turned the read Err into `None`, which was
    // treated as "no file yet" and the write below overwrote it wholesale.
    #[test]
    fn unreadable_existing_launch_json_refuses_to_write() {
        let dir = tmp("unreadable");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        let launch_json = vscode_dir.join("launch.json");
        let not_utf8: &[u8] = &[0xFF, 0xFE, b'{', 0, b'}', 0];
        std::fs::write(&launch_json, not_utf8).unwrap();

        let g = global(&dir);
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);

        assert_eq!(run_result.exit, ExitCode::InternalFailure);
        let after = std::fs::read(&launch_json).unwrap();
        assert_eq!(
            after, not_utf8,
            "an unreadable existing launch.json must be left untouched, not overwritten"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// alp-sdk#1026 end-to-end: with NO build at all (no `system-manifest.yaml`,
    /// no `runners.yaml`), a `board.yaml` naming a SoM and an SDK checkout
    /// publishing that SoM's variant `debug` block, `device`/`targetId` must
    /// resolve from the SDK metadata rather than staying the placeholder --
    /// the exact gap #1026 reports as inert.
    #[test]
    fn debug_config_resolves_device_and_target_id_from_sdk_metadata_pre_build() {
        let dir = tmp("sdk-metadata-fallback");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let sdk = dir.join("sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let som_dir = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&som_dir).unwrap();
        std::fs::write(
            som_dir.join("E1M-AEN801.yaml"),
            "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n\
             silicon_variant: AE822FA0E5597LS0\n",
        )
        .unwrap();
        let soc_dir = sdk
            .join("metadata")
            .join("socs")
            .join("alif")
            .join("ensemble");
        std::fs::create_dir_all(&soc_dir).unwrap();
        std::fs::write(
            soc_dir.join("e8.json"),
            r#"{
                "soc_spec_version": 1,
                "ref": "alif:ensemble:e8",
                "vendor": "Alif Semiconductor",
                "family": "Ensemble",
                "part": "E8",
                "variants": [
                    {
                        "order_code": "AE822FA0E5597LS0",
                        "debug": {
                            "pyocd_target": "AE822FA0E5597LS0",
                            "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
                        }
                    }
                ]
            }"#,
        )
        .unwrap();

        let mut g = global(&dir);
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: Some("m55_hp".to_string()),
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(json["data"]["configuration"]["device"], "Cortex-M55");

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The `--server pyocd` sibling of the test above: `targetId` resolves
    /// from `pyocd_target`, and needs no `--core` at all (`jlink_device` is
    /// the only per-core field; `pyocd_target` is a scalar per variant).
    #[test]
    fn debug_config_resolves_pyocd_target_id_from_sdk_metadata_pre_build() {
        let dir = tmp("sdk-metadata-fallback-pyocd");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let sdk = dir.join("sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let som_dir = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&som_dir).unwrap();
        std::fs::write(
            som_dir.join("E1M-AEN801.yaml"),
            "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n\
             silicon_variant: AE822FA0E5597LS0\n",
        )
        .unwrap();
        let soc_dir = sdk
            .join("metadata")
            .join("socs")
            .join("alif")
            .join("ensemble");
        std::fs::create_dir_all(&soc_dir).unwrap();
        std::fs::write(
            soc_dir.join("e8.json"),
            r#"{
                "soc_spec_version": 1,
                "ref": "alif:ensemble:e8",
                "vendor": "Alif Semiconductor",
                "family": "Ensemble",
                "part": "E8",
                "variants": [
                    {
                        "order_code": "AE822FA0E5597LS0",
                        "debug": {
                            "pyocd_target": "AE822FA0E5597LS0",
                            "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
                        }
                    }
                ]
            }"#,
        )
        .unwrap();

        let mut g = global(&dir);
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("pyocd".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(
            json["data"]["configuration"]["targetId"],
            "AE822FA0E5597LS0"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// alp-sdk#987's own stance: `openocd_config` is absent from every SoC
    /// family today, and that absence must stay the published "unknown" --
    /// the OpenOCD draft's `configFiles` keeps its placeholder rather than
    /// inventing a config path, and the preview note says so.
    #[test]
    fn debug_config_openocd_config_files_stays_the_placeholder_when_the_sdk_publishes_none() {
        let dir = tmp("sdk-metadata-fallback-openocd");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let sdk = dir.join("sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let som_dir = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&som_dir).unwrap();
        std::fs::write(
            som_dir.join("E1M-AEN801.yaml"),
            "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n\
             silicon_variant: AE822FA0E5597LS0\n",
        )
        .unwrap();
        let soc_dir = sdk
            .join("metadata")
            .join("socs")
            .join("alif")
            .join("ensemble");
        std::fs::create_dir_all(&soc_dir).unwrap();
        // No openocd_config key -- exactly every real Alif variant today.
        std::fs::write(
            soc_dir.join("e8.json"),
            r#"{
                "soc_spec_version": 1,
                "ref": "alif:ensemble:e8",
                "vendor": "Alif Semiconductor",
                "family": "Ensemble",
                "part": "E8",
                "variants": [
                    {
                        "order_code": "AE822FA0E5597LS0",
                        "debug": {
                            "pyocd_target": "AE822FA0E5597LS0",
                            "jlink_device": {"m55_hp": "Cortex-M55"}
                        }
                    }
                ]
            }"#,
        )
        .unwrap();

        let mut g = global(&dir);
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: Some("m55_hp".to_string()),
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("openocd".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(
            json["data"]["configuration"]["configFiles"],
            serde_json::json!(["<resolved-openocd-board-cfg>"]),
            "an absent openocd_config must stay the placeholder, never a guess"
        );
        assert!(
            json["data"]["notes"].as_array().unwrap().iter().any(|n| n
                .as_str()
                .unwrap_or_default()
                .starts_with("Placeholder fields")),
            "the placeholder note must still be present: {}",
            json["data"]["notes"]
        );
        // alp-sdk#1026 review finding #4: the generic note above names
        // `device`, which this OpenOCD draft does not even carry -- the
        // specific, correctly-worded signal is this issue, present even on
        // `--preview` since it is advisory about resolution state, not about
        // a write.
        let issues = json["issues"].as_array().expect("issues array");
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "debug-config.sdk-identity-key-absent"
                    && i["message"]
                        .as_str()
                        .unwrap_or_default()
                        .contains("configFiles")),
            "expected a sdk-identity-key-absent issue naming configFiles: {issues:?}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// alp-sdk#1026 review finding #1 (data loss): a WRITE, not a preview —
    /// every one of the three tests above only ever exercised `--preview`,
    /// so the write path with this fallback had zero coverage. A customer's
    /// `.vscode/launch.json` already holds a concrete, hand-filled `device`
    /// (here, the more-specific `jlink_flash_device`-style profile a
    /// customer might reasonably have copied in); the SDK's generic
    /// `jlink_device` identity resolves and REPLACES it, same as a real
    /// build's resolution always has — but this run must disclose that in
    /// `issues[]`, not report `ok: true` / `issues: []` as if nothing
    /// happened.
    #[test]
    fn debug_config_write_discloses_when_sdk_identity_overwrites_a_hand_filled_device() {
        let dir = tmp("sdk-metadata-overwrite-disclosure");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        std::fs::write(
            vscode_dir.join("launch.json"),
            r#"{
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "jlink",
                        "device": "AE822FA0E5597LS0_M55_HE"
                    }
                ]
            }"#,
        )
        .unwrap();

        let sdk = dir.join("sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let som_dir = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&som_dir).unwrap();
        std::fs::write(
            som_dir.join("E1M-AEN801.yaml"),
            "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n\
             silicon_variant: AE822FA0E5597LS0\n",
        )
        .unwrap();
        let soc_dir = sdk
            .join("metadata")
            .join("socs")
            .join("alif")
            .join("ensemble");
        std::fs::create_dir_all(&soc_dir).unwrap();
        std::fs::write(
            soc_dir.join("e8.json"),
            r#"{
                "soc_spec_version": 1,
                "ref": "alif:ensemble:e8",
                "vendor": "Alif Semiconductor",
                "family": "Ensemble",
                "part": "E8",
                "variants": [
                    {
                        "order_code": "AE822FA0E5597LS0",
                        "debug": {
                            "pyocd_target": "AE822FA0E5597LS0",
                            "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
                        }
                    }
                ]
            }"#,
        )
        .unwrap();

        let mut g = global(&dir);
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: Some("m55_hp".to_string()),
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();

        // The overwrite happened (matches a real build's own resolution
        // behaviour — unchanged by this PR).
        assert_eq!(json["data"]["configuration"]["device"], "Cortex-M55");
        let on_disk: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(vscode_dir.join("launch.json")).unwrap())
                .unwrap();
        assert_eq!(on_disk["configurations"][0]["device"], "Cortex-M55");

        // …and it was DISCLOSED, not silent.
        let issues = json["issues"].as_array().expect("issues array");
        let overwrite_issue = issues
            .iter()
            .find(|i| i["code"] == "debug-config.sdk-identity-overwrite")
            .unwrap_or_else(|| panic!("no overwrite issue in {issues:?}"));
        assert_eq!(overwrite_issue["severity"], "info");
        let message = overwrite_issue["message"].as_str().unwrap();
        assert!(message.contains("AE822FA0E5597LS0_M55_HE"), "{message}");
        assert!(message.contains("Cortex-M55"), "{message}");

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// alp-sdk#1026 review finding #3: `jlink_device` is keyed BY core id, so
    /// on a project that has never been built AND passes no `--core`,
    /// `identity_core` is `None` and `device` must stay the placeholder —
    /// there is no core to index the map with, and no "only entry" guess.
    /// `targetId` (pyOCD) is the opposite case, already covered by
    /// `debug_config_resolves_pyocd_target_id_from_sdk_metadata_pre_build`
    /// above (a scalar, needs no core at all).
    #[test]
    fn debug_config_jlink_device_stays_the_placeholder_with_no_core_and_no_build() {
        let dir = tmp("sdk-metadata-fallback-no-core-jlink");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let sdk = dir.join("sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let som_dir = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&som_dir).unwrap();
        std::fs::write(
            som_dir.join("E1M-AEN801.yaml"),
            "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n\
             silicon_variant: AE822FA0E5597LS0\n",
        )
        .unwrap();
        let soc_dir = sdk
            .join("metadata")
            .join("socs")
            .join("alif")
            .join("ensemble");
        std::fs::create_dir_all(&soc_dir).unwrap();
        std::fs::write(
            soc_dir.join("e8.json"),
            r#"{
                "soc_spec_version": 1,
                "ref": "alif:ensemble:e8",
                "vendor": "Alif Semiconductor",
                "family": "Ensemble",
                "part": "E8",
                "variants": [
                    {
                        "order_code": "AE822FA0E5597LS0",
                        "debug": {
                            "pyocd_target": "AE822FA0E5597LS0",
                            "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
                        }
                    }
                ]
            }"#,
        )
        .unwrap();

        let mut g = global(&dir);
        g.sdk_root = Some(sdk.to_string_lossy().into_owned());
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None, // no --core, and no build ever ran either.
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(json["data"]["configuration"]["device"], "<resolved-device>");

        let _ = std::fs::remove_dir_all(&dir);
    }

    // The `<host>:<port>` hole in the note logic: a yocto draft whose
    // `<resolved-gdb>` DID resolve has no `<resolved-` string left, so the
    // prefix-only predicate dropped the "still needs resolution" note while
    // `miDebuggerServerAddress` was still the unusable `<host>:<port>` — the
    // note goes silent on exactly the config that cannot launch.
    #[test]
    fn the_placeholder_note_survives_an_unresolved_host_port() {
        let mut draft = create_launch_draft(
            DebugTargetKind::YoctoUserspace,
            DebugServerKind::Gdbserver,
            None,
        )
        .unwrap();
        apply_launch_resolution(
            &mut draft,
            &LaunchResolution {
                gdb_path: Some("/opt/gdb/bin/aarch64-poky-linux-gdb".into()),
                ..Default::default()
            },
        );
        assert_eq!(draft["miDebuggerServerAddress"], "<host>:<port>");
        assert!(!has_placeholder(&draft["miDebuggerPath"]));

        let notes = preview_notes_for(&draft, &[], DebugServerKind::Gdbserver);
        assert!(
            notes.iter().any(|n| n.starts_with("Placeholder fields")),
            "an unresolved <host>:<port> must keep the note: {notes:?}"
        );
    }

    /// Write a `system-manifest.yaml` at `<workspace>/build/system-manifest.yaml`.
    fn write_manifest(workspace: &Path, yaml: &str) {
        let build_dir = workspace.join("build");
        std::fs::create_dir_all(&build_dir).unwrap();
        std::fs::write(build_dir.join("system-manifest.yaml"), yaml).unwrap();
    }

    /// A manifest with a Cortex-M Zephyr MCU slice FIRST and a `native_sim`
    /// slice SECOND — the exact ordering that broke `native-host` resolution
    /// before this fix (the old `os`-keyed match took the first `os: zephyr`
    /// slice, which on this manifest is the MCU one, not the host binary).
    ///
    /// BOTH slices record `zephyr.elf`, because that is the only thing tan
    /// ever writes: `resolve_zephyr_artefact` (build/execute/manifest.rs)
    /// stores `<slice-cwd>/build/zephyr/zephyr.elf` unconditionally, with no
    /// `.exe` branch for native_sim, and alp-sdk NEVER writes `output_artefact`
    /// at all. This fixture originally wrote `zephyr.exe` on the native_sim
    /// slice — a manifest tan cannot produce — which is precisely why it
    /// could not see that `resolve_from_build` was taking the ELF verbatim.
    ///
    /// That `.elf` claim is not prose here: it is pinned on the PRODUCER side
    /// by `build::execute::manifest`'s
    /// `resolve_zephyr_artefact_names_the_elf_even_for_a_native_sim_slice`.
    /// If a `.exe` branch is ever added there, that test fails and this
    /// fixture gets revisited — rather than both silently drifting back to
    /// encoding a manifest tan cannot produce, which is the blind spot itself
    /// and not merely #83's instance of it.
    fn manifest_mcu_then_native_sim(workspace: &Path) -> String {
        let root = workspace.to_string_lossy().replace('\\', "/");
        format!(
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n\
             - core_id: m55_hp\n  os: zephyr\n  board: alp_e1m_aen701_m55_hp\n  status: ok\n  \
             output_artefact: {root}/build/m55_hp-zephyr/build/zephyr/zephyr.elf\n\
             - core_id: native_sim\n  os: zephyr\n  board: native_sim\n  status: ok\n  \
             output_artefact: {root}/build/native_sim-zephyr/build/zephyr/zephyr.elf\n\
             ipc: []\nhelper_mcus: []\nboot_order: []\n"
        )
    }

    // Regression for the actual bug: with a Cortex-M Zephyr slice FIRST and a
    // `native_sim` slice second, `native-host` resolution must take the
    // native_sim artefact, never fall through to the MCU one — the old
    // `os`-keyed match took the first `os: zephyr` slice regardless of which
    // one it was, pointing `Alp: Native Sim Debug` at a Cortex-M ELF.
    //
    // And it must resolve the RUNNABLE: the slice records `zephyr.elf` (all
    // tan ever writes), so `program` has to be the sibling `zephyr.exe`.
    // Taking `output_artefact` verbatim hands CodeLLDB an ELF it cannot
    // launch — the same class of failure, one directory entry over.
    #[test]
    fn native_host_resolves_native_sim_slice_not_the_first_zephyr_slice() {
        let dir = tmp("native-host-mixed");
        write_manifest(&dir, &manifest_mcu_then_native_sim(&dir));

        let (resolution, _, _) = resolve_from_build(
            &dir,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            None,
        );

        assert_eq!(
            resolution.executable.as_deref(),
            Some("${workspaceFolder}/build/native_sim-zephyr/build/zephyr/zephyr.exe")
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    // With ONLY a Cortex-M Zephyr slice (no `native_sim` slice at all),
    // `native-host` resolution must resolve NO executable rather than adopt
    // the MCU ELF — the draft keeps its own placeholder `program`.
    #[test]
    fn native_host_resolves_nothing_when_manifest_has_no_native_sim_slice() {
        let dir = tmp("native-host-mcu-only");
        let root = dir.to_string_lossy().replace('\\', "/");
        let manifest = format!(
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n\
             - core_id: m55_hp\n  os: zephyr\n  board: alp_e1m_aen701_m55_hp\n  status: ok\n  \
             output_artefact: {root}/build/m55_hp-zephyr/build/zephyr/zephyr.elf\n\
             ipc: []\nhelper_mcus: []\nboot_order: []\n"
        );
        write_manifest(&dir, &manifest);

        let (resolution, _, _) = resolve_from_build(
            &dir,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            None,
        );

        assert_eq!(resolution.executable, None);

        let _ = std::fs::remove_dir_all(&dir);
    }

    // `zephyr-mcu` behaviour is unchanged by the native-host fix: on the same
    // two-slice manifest, bare resolution still takes the first `os: zephyr`
    // slice (the MCU one, listed first), and `--core` still pins a specific
    // slice explicitly.
    #[test]
    fn zephyr_mcu_resolution_unchanged_by_the_native_host_fix() {
        let dir = tmp("zephyr-mcu-unchanged");
        write_manifest(&dir, &manifest_mcu_then_native_sim(&dir));

        let (bare, _, _) = resolve_from_build(
            &dir,
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            None,
        );
        assert_eq!(
            bare.executable.as_deref(),
            Some("${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );

        let (pinned, _, _) = resolve_from_build(
            &dir,
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            Some("m55_hp"),
        );
        assert_eq!(
            pinned.executable.as_deref(),
            Some("${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    // Zephyr's qualified board form (`native_sim/native/64`) must still be
    // recognised for `native-host` resolution, not just the bare `native_sim`
    // name — otherwise the fix would quietly depend on a board string real
    // manifests don't always use.
    #[test]
    fn native_host_resolves_qualified_native_sim_board_form() {
        let dir = tmp("native-host-qualified-board");
        let root = dir.to_string_lossy().replace('\\', "/");
        let manifest = format!(
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n\
             - core_id: native_sim\n  os: zephyr\n  board: native_sim/native/64\n  status: \
             ok\n  output_artefact: {root}/build/native_sim-zephyr/build/zephyr/zephyr.elf\n\
             ipc: []\nhelper_mcus: []\nboot_order: []\n"
        );
        write_manifest(&dir, &manifest);

        let (resolution, _, _) = resolve_from_build(
            &dir,
            DebugTargetKind::NativeHost,
            DebugServerKind::None,
            None,
        );

        assert_eq!(
            resolution.executable.as_deref(),
            Some("${workspaceFolder}/build/native_sim-zephyr/build/zephyr/zephyr.exe")
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    // Bug 1 at the command boundary: the envelope's `data.configuration` — the
    // very object alp-sdk-vscode#342 writes into launch.json — must carry no
    // `preLaunchTask` unless one was asked for. Nothing in this repo, in
    // alp-sdk-vscode, or in a generated project defines a task, and VS Code
    // aborts pre-launch on a name it cannot resolve, so a default here means
    // the emitted configuration cannot start a session at all.
    #[test]
    fn envelope_configuration_carries_a_pre_launch_task_only_when_opted_in() {
        let dir = tmp("prelaunch-optin");
        let mut g = global(&dir);
        g.format = Format::Json;
        let mut args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };

        let default_json = run(&g, &args).json.expect("json envelope");
        assert!(
            !default_json.contains("preLaunchTask"),
            "default debug-config output must not name a task nothing defines:
{default_json}"
        );

        args.pre_launch_task = Some("alpRun: build".to_string());
        let opted_in: Value = serde_json::from_str(&run(&g, &args).json.expect("json envelope"))
            .expect("envelope is JSON");
        assert_eq!(
            opted_in["data"]["configuration"]["preLaunchTask"],
            "alpRun: build"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #133 reopened, driven end-to-end through `run()`: the exact reported
    /// transcript — a hand-filled `"device": "AE822F4M55_HP"` sitting on the
    /// orphaned legacy `"ALP: Zephyr Debug (J-Link)"` entry. Asserts the value
    /// survives onto the correctly-named entry (both in the returned envelope
    /// AND in the file actually written to disk), and that the run reports
    /// the migration as an `issues[]` entry rather than silently rewriting the
    /// customer's file.
    #[test]
    fn run_migrates_a_legacy_alp_entry_and_reports_it_as_an_issue() {
        let dir = tmp("migrate-legacy");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        let launch_json = vscode_dir.join("launch.json");
        std::fs::write(
            &launch_json,
            serde_json::to_string_pretty(&serde_json::json!({
                "version": "0.2.0",
                "configurations": [{
                    "name": "ALP: Zephyr Debug (J-Link)",
                    "type": "cortex-debug",
                    "request": "launch",
                    "cwd": "${workspaceFolder}",
                    "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                    "servertype": "jlink",
                    "device": "AE822F4M55_HP",
                    "interface": "swd",
                }],
            }))
            .unwrap(),
        )
        .unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        // tan-cli#180: `data.configuration` now reports the MERGED result —
        // the customer's real, hand-filled `device` — not the fresh draft's
        // own `<resolved-device>` placeholder. Before the fix this read
        // `"<resolved-device>"` here even though the file on disk (checked
        // below) already carried the real value, so the envelope told a
        // consumer the write had NOT resolved something it plainly had.
        assert_eq!(
            envelope["data"]["configuration"]["name"],
            "Alp: Zephyr Debug (J-Link)"
        );
        assert_eq!(
            envelope["data"]["configuration"]["device"], "AE822F4M55_HP",
            "the envelope must report what was actually written, not the \
             draft's stale placeholder: {envelope}"
        );
        assert_eq!(envelope["data"]["replaced"], true);
        let issues = envelope["issues"].as_array().unwrap();
        assert_eq!(issues.len(), 1, "{envelope}");
        assert_eq!(issues[0]["code"], "debug-config.legacy-entry-migrated");
        assert_eq!(issues[0]["severity"], "info");
        assert!(
            issues[0]["message"]
                .as_str()
                .unwrap()
                .contains("ALP: Zephyr Debug (J-Link)"),
            "{envelope}"
        );

        // The actual file on disk, not just the in-memory draft, carries the
        // migrated after-state.
        let after: Value =
            serde_json::from_str(&std::fs::read_to_string(&launch_json).unwrap()).unwrap();
        let configs = after["configurations"].as_array().unwrap();
        assert_eq!(
            configs.len(),
            1,
            "the legacy entry must be adopted in place, not left behind: {after}"
        );
        assert_eq!(configs[0]["name"], "Alp: Zephyr Debug (J-Link)");
        assert_eq!(configs[0]["device"], "AE822F4M55_HP");

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The failing-case pairing #133 asks for: on a workspace with NO legacy
    /// entry at all (the common case — a fresh `.vscode/launch.json`), the
    /// migration issue must never appear. A test that only proves migration
    /// happens when it should, with nothing proving it does not happen when it
    /// should not, would pass a version that unconditionally attaches the
    /// issue.
    #[test]
    fn run_emits_no_migration_issue_when_no_legacy_entry_exists() {
        let dir = tmp("no-migration");
        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("native-host".to_string()),
            server: None,
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(
            envelope["issues"].as_array().unwrap().len(),
            0,
            "a fresh launch.json must not report a migration that never happened: {envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The migration notice is printed in TEXT mode even under `--quiet`
    /// (`global()` sets `quiet: true`) — this is a one-time, meaningful notice
    /// about a file change under the customer's feet, not routine resolution
    /// noise that `--quiet` is meant to suppress.
    #[test]
    fn text_mode_reports_the_migration_even_when_quiet() {
        let dir = tmp("migrate-legacy-text");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        std::fs::write(
            vscode_dir.join("launch.json"),
            serde_json::to_string_pretty(&serde_json::json!({
                "version": "0.2.0",
                "configurations": [{
                    "name": "ALP: Native Sim Debug",
                    "type": "lldb",
                    "request": "launch",
                    "program": "${workspaceFolder}/build/native_sim/zephyr/zephyr.exe",
                    "cwd": "${workspaceFolder}",
                }],
            }))
            .unwrap(),
        )
        .unwrap();

        let g = global(&dir);
        assert!(g.quiet, "this test only proves something if quiet is set");
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("native-host".to_string()),
            server: None,
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);
        assert!(
            run_result
                .text
                .iter()
                .any(|l| l.contains("Migrated the legacy launch-configuration entry")),
            "{:?}",
            run_result.text
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// tan-cli#182 review finding #2, at the command boundary: a write that
    /// drops a comment inside the entry being updated must surface
    /// `debug-config.comments-dropped` as an `issues[]` entry, severity
    /// `info`, not just succeed silently — #182's own non-negotiable floor.
    #[test]
    fn run_reports_a_comments_dropped_issue_when_a_write_drops_one() {
        let dir = tmp("comments-dropped-issue");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        std::fs::write(
            vscode_dir.join("launch.json"),
            "{\n  \"version\": \"0.2.0\",\n  \"configurations\": [\n    {\n      \"name\": \"Alp: Zephyr Debug (J-Link)\",\n      \"type\": \"cortex-debug\",\n      \"request\": \"launch\",\n      // hand-picked after bring-up\n      \"cwd\": \"${workspaceFolder}\",\n      \"executable\": \"${workspaceFolder}/build/app/zephyr/zephyr.elf\",\n      \"servertype\": \"jlink\",\n      \"device\": \"OLD_DEVICE\",\n      \"interface\": \"swd\"\n    }\n  ]\n}\n",
        )
        .unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        let issues = envelope["issues"].as_array().unwrap();
        let found = issues
            .iter()
            .find(|i| i["code"] == "debug-config.comments-dropped")
            .unwrap_or_else(|| panic!("no comments-dropped issue: {envelope}"));
        assert_eq!(found["severity"], "info");

        let after = std::fs::read_to_string(vscode_dir.join("launch.json")).unwrap();
        assert!(
            !after.contains("hand-picked after bring-up"),
            "the fixture must actually have dropped the comment for this test \
             to prove anything: {after}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The failing-case pairing: an ordinary re-run against a comment-free
    /// file (the common case) must never report `comments-dropped`.
    #[test]
    fn run_emits_no_comments_dropped_issue_on_an_ordinary_write() {
        let dir = tmp("no-comments-dropped");
        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("native-host".to_string()),
            server: None,
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert!(
            envelope["issues"]
                .as_array()
                .unwrap()
                .iter()
                .all(|i| i["code"] != "debug-config.comments-dropped"),
            "a fresh write with nothing to drop must not report dropping anything: {envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// tan-cli#180, the preview-side guard: `--preview` never reads or writes
    /// the customer's file (it returns before the read), so it must keep
    /// reporting the fresh draft even when a legacy entry that WOULD migrate
    /// on a real write sits right there in `.vscode/launch.json`. This is
    /// exactly the invariant the four `debug-config-preview-*` goldens pin —
    /// a regression here would move all four for the wrong reason.
    #[test]
    fn preview_mode_reports_the_draft_even_when_a_legacy_entry_would_migrate() {
        let dir = tmp("preview-ignores-legacy");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        std::fs::write(
            vscode_dir.join("launch.json"),
            serde_json::to_string_pretty(&serde_json::json!({
                "version": "0.2.0",
                "configurations": [{
                    "name": "ALP: Zephyr Debug (J-Link)",
                    "type": "cortex-debug",
                    "servertype": "jlink",
                    "device": "AE822F4M55_HP",
                }],
            }))
            .unwrap(),
        )
        .unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert_eq!(
            envelope["data"]["configuration"]["device"], "<resolved-device>",
            "preview must report the draft's own placeholder, never a value \
             implying a merge that never ran: {envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Every `--svd` test passes an ABSOLUTE path on purpose. `resolve_user_svd`
    /// anchors a relative path on the process cwd, and cargo runs these tests
    /// in threads that share one cwd — a `set_current_dir` here would race
    /// every other test in the binary. The cwd anchoring is documented on the
    /// flag and exercised by hand, not by a test that can flake.
    fn args_with_svd(target_kind: &str, svd: Option<&str>, preview: bool) -> DebugConfigArgs {
        DebugConfigArgs {
            core: None,
            target_kind: Some(target_kind.to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: svd.map(str::to_string),
            preview,
        }
    }

    #[test]
    fn a_user_supplied_svd_inside_the_project_is_emitted_workspace_relative() {
        let dir = tmp("svd-in-project");
        let svd = dir.join("E8.svd");
        std::fs::write(&svd, "<device/>").unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = args_with_svd("zephyr-mcu", Some(&svd.to_string_lossy()), true);
        let run_result = run(&g, &args);

        assert_eq!(run_result.exit, ExitCode::Success);
        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        let config = &envelope["data"]["configuration"];
        // Both keys, because cortex-debug has spelled it both ways across
        // versions and the draft carries both.
        assert_eq!(config["svdFile"], "${workspaceFolder}/E8.svd");
        assert_eq!(config["svdPath"], "${workspaceFolder}/E8.svd");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_user_supplied_svd_outside_the_project_stays_absolute() {
        let dir = tmp("svd-outside-project");
        let vendor = tmp("svd-vendor-sdk");
        let svd = vendor.join("AE722F80F55D5AS.svd");
        std::fs::write(&svd, "<device/>").unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = args_with_svd("zephyr-mcu", Some(&svd.to_string_lossy()), true);
        let run_result = run(&g, &args);

        assert_eq!(run_result.exit, ExitCode::Success);
        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        // The normal case: a vendor SVD lives in the vendor SDK, not the
        // project, so it must NOT be mangled into a ${workspaceFolder} path.
        assert_eq!(
            envelope["data"]["configuration"]["svdFile"],
            Value::String(normalize_path(&svd).to_string_lossy().into_owned())
        );

        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&vendor);
    }

    #[test]
    fn a_missing_svd_path_fails_instead_of_silently_dropping_the_key() {
        let dir = tmp("svd-missing");
        let missing = dir.join("nope.svd");

        let g = global(&dir);
        let args = args_with_svd("zephyr-mcu", Some(&missing.to_string_lossy()), false);
        let run_result = run(&g, &args);

        // Falling back to "no SVD" would make a typo indistinguishable from
        // not passing the flag — the user explicitly named this file.
        assert_eq!(run_result.exit, ExitCode::InternalFailure);
        assert!(
            !dir.join(".vscode").join("launch.json").exists(),
            "a refused --svd must not have written launch.json"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// tan-cli#179, driven end-to-end through `run()`: the "dangerous branch"
    /// repro (a maintained `"Alp: ..."` entry AND a leftover
    /// `"ALP: ..."` one, both present) must surface a
    /// `debug-config.legacy-entry-untouched` issue naming the leftover entry.
    #[test]
    fn run_reports_a_leftover_legacy_entry_left_untouched_by_the_ordinary_merge() {
        let dir = tmp("legacy-untouched");
        let vscode_dir = dir.join(".vscode");
        std::fs::create_dir_all(&vscode_dir).unwrap();
        std::fs::write(
            vscode_dir.join("launch.json"),
            serde_json::to_string_pretty(&serde_json::json!({
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "servertype": "jlink",
                        "device": "<resolved-device>",
                    },
                    {
                        "name": "ALP: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "servertype": "jlink",
                        "device": "AE822F4M55_HP",
                    },
                ],
            }))
            .unwrap(),
        )
        .unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: false,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        let issues = envelope["issues"].as_array().unwrap();
        let found = issues
            .iter()
            .find(|i| i["code"] == "debug-config.legacy-entry-untouched")
            .unwrap_or_else(|| panic!("no legacy-entry-untouched issue: {envelope}"));
        assert_eq!(found["severity"], "info");
        assert!(
            found["message"]
                .as_str()
                .unwrap()
                .contains("ALP: Zephyr Debug (J-Link)"),
            "{envelope}"
        );
        // No migration happened -- the maintained entry merged ordinarily.
        assert!(
            issues
                .iter()
                .all(|i| i["code"] != "debug-config.legacy-entry-migrated"),
            "{envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// tan-cli#170: `project.boardYaml` must report a resolvable `board.yaml`
    /// instead of hardcoding `null` on a success — same resolver every other
    /// command (`bootstrap`, `doctor`, `presets`, …) already uses.
    #[test]
    fn envelope_reports_the_projects_board_yaml_when_one_exists() {
        let dir = tmp("board-yaml-reported");
        std::fs::write(dir.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        let board_yaml = envelope["project"]["boardYaml"]
            .as_str()
            .unwrap_or_else(|| panic!("project.boardYaml must be populated: {envelope}"));
        assert!(
            board_yaml.ends_with("board.yaml"),
            "expected a path ending in board.yaml, got {board_yaml}"
        );
        // #170's own rationale, applied: `project.root` and `project.boardYaml`
        // must not ship with different separators in the same object.
        let root = envelope["project"]["root"].as_str().unwrap_or_default();
        assert_eq!(
            board_yaml.contains('\\'),
            root.contains('\\'),
            "root and boardYaml disagree on separator: {envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// tan-cli#236, the pair of the test above: #170's fix routed this field
    /// through the shared resolver, which builds `<root>/board.yaml`
    /// unconditionally — so without #236 it traded a hardcoded null for a path
    /// to a file that need not exist. `debug-config` succeeds in a directory
    /// with no `board.yaml` (the four golden previews all do), which makes it
    /// the command where the wrong value is most reachable.
    #[test]
    fn envelope_reports_a_null_board_yaml_when_the_directory_has_none() {
        let dir = tmp("board-yaml-absent");
        assert!(!dir.join("board.yaml").exists());

        let mut g = global(&dir);
        g.format = Format::Json;
        let args = DebugConfigArgs {
            core: None,
            target_kind: Some("zephyr-mcu".to_string()),
            server: Some("jlink".to_string()),
            pre_launch_task: None,
            svd: None,
            preview: true,
        };
        let run_result = run(&g, &args);
        assert_eq!(run_result.exit, ExitCode::Success);

        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert!(
            envelope["project"]["boardYaml"].is_null(),
            "no board.yaml is there -- the field must not name one: {envelope}"
        );
        // `root` is deliberately untouched: #236 rules it out of scope, and a
        // run still legitimately reports where it stood.
        assert!(
            envelope["project"]["root"].is_string(),
            "root must still report the resolved directory: {envelope}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_svd_path_that_is_a_directory_is_refused() {
        let dir = tmp("svd-is-a-dir");
        let not_a_file = dir.join("svd-dir");
        std::fs::create_dir_all(&not_a_file).unwrap();

        let g = global(&dir);
        let args = args_with_svd("zephyr-mcu", Some(&not_a_file.to_string_lossy()), true);

        assert_eq!(run(&g, &args).exit, ExitCode::InternalFailure);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_empty_svd_path_is_refused_rather_than_treated_as_absent() {
        let dir = tmp("svd-empty");
        let g = global(&dir);
        let args = args_with_svd("zephyr-mcu", Some("   "), true);

        assert_eq!(run(&g, &args).exit, ExitCode::InternalFailure);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn svd_on_a_target_kind_without_the_field_is_reported_not_silently_ignored() {
        let dir = tmp("svd-non-mcu");
        let svd = dir.join("E8.svd");
        std::fs::write(&svd, "<device/>").unwrap();

        let mut g = global(&dir);
        g.format = Format::Json;
        let mut args = args_with_svd("native-host", Some(&svd.to_string_lossy()), true);
        args.server = None;
        let run_result = run(&g, &args);

        assert_eq!(run_result.exit, ExitCode::Success);
        let envelope: Value =
            serde_json::from_str(&run_result.json.expect("json envelope")).unwrap();
        assert!(
            envelope["data"]["configuration"].get("svdFile").is_none(),
            "a native-host draft has no svdFile field to fill"
        );
        let notes = envelope["data"]["notes"].as_array().unwrap();
        assert!(
            notes
                .iter()
                .any(|n| n.as_str().unwrap_or_default().contains("--svd was given")),
            "accepting --svd here and saying nothing is the silent no-op this note exists to \
             prevent: {notes:?}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }
}
