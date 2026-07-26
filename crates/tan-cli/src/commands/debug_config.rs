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
use tan_core::system_manifest::{Slice, SystemManifest, parse_system_manifest};
use tan_core::{
    DebugServerKind, DebugTargetKind, LaunchResolution, apply_launch_resolution,
    create_launch_draft, create_launch_json_write_plan, launch_preview_document,
    launch_preview_notes, parse_server_kind, parse_target_kind,
};

use super::CommandRun;
use crate::cli::{DebugConfigArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{generated_at_iso, normalize_path};

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

    // Fill the `<resolved-…>` placeholders from what this project's own build
    // recorded (#66). Nothing here fails the command: pre-build, or against a
    // Zephyr that reshaped `runners.yaml`, the draft keeps its placeholders.
    let (resolution, registered_runners) =
        resolve_from_build(&workspace_root, target, server, args.core.as_deref());
    apply_launch_resolution(&mut draft, &resolution);
    let notes = preview_notes_for(&draft, &registered_runners, server);

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
            &workspace_root,
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

    success(
        g,
        &generated_at,
        target,
        server,
        &launch_json_path,
        false,
        plan.replaced,
        &notes,
        &draft,
        &workspace_root,
    )
}

/// Build a success `CommandRun`: emit the JSON envelope (or text lines) for a
/// completed preview or write at `ExitCode::Success`.
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
    draft: &Value,
    workspace_root: &Path,
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
        configuration: draft.clone(),
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
            draft,
            g,
        )
    };
    let project = Project {
        root: Some(workspace_root.to_string_lossy().to_string()),
        board_yaml: None,
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "debug-config",
            project,
            data,
            Vec::new(),
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
/// often the MCU slice, pointing `ALP: Native Sim Debug` at a Cortex-M ELF
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

/// Everything this project's own build knows about how to debug it: the
/// per-core ELF from `system-manifest.yaml`, and the probe/tool paths from that
/// slice's `runners.yaml` — the same file `west flash` reads.
///
/// Best-effort throughout. A missing manifest (pre-build), a missing slice, an
/// unreadable or reshaped `runners.yaml` each leave the corresponding field
/// unresolved instead of failing the command: `debug-config` must still emit
/// its draft before the first build.
fn resolve_from_build(
    workspace_root: &Path,
    target: DebugTargetKind,
    server: DebugServerKind,
    core: Option<&str>,
) -> (LaunchResolution, Vec<String>) {
    let mut resolution = LaunchResolution::default();
    let manifest_path = workspace_root.join("build").join("system-manifest.yaml");
    let Ok(yaml) = std::fs::read_to_string(&manifest_path) else {
        return (resolution, Vec::new());
    };
    let Ok(manifest) = parse_system_manifest(&yaml) else {
        return (resolution, Vec::new());
    };
    let Some(slice) = select_slice(&manifest, target, core) else {
        return (resolution, Vec::new());
    };

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
        // `ALP: Native Sim Debug` at a `zephyr.elf` CodeLLDB cannot launch.
        let artefact = match target {
            DebugTargetKind::NativeHost => native_sim_exe_beside(artefact),
            _ => artefact.to_string(),
        };
        resolution.executable = Some(workspace_relative(workspace_root, &artefact));
    }

    let Some(build_dir) = slice.build_dir.as_deref().filter(|b| !b.is_empty()) else {
        return (resolution, Vec::new());
    };
    let runners_path = Path::new(build_dir).join("zephyr").join("runners.yaml");
    let Ok(text) = std::fs::read_to_string(&runners_path) else {
        return (resolution, Vec::new());
    };
    let Ok(runners) = parse_runners_config(&text) else {
        return (resolution, Vec::new());
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
    (resolution, runners.runners.clone())
}

/// Whether any `<resolved-…>` placeholder survived resolution, anywhere in the
/// draft — including inside `configFiles`, which is an array.
fn has_placeholder(value: &Value) -> bool {
    match value {
        Value::String(s) => s.contains("<resolved-"),
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
    // one it was, pointing `ALP: Native Sim Debug` at a Cortex-M ELF.
    //
    // And it must resolve the RUNNABLE: the slice records `zephyr.elf` (all
    // tan ever writes), so `program` has to be the sibling `zephyr.exe`.
    // Taking `output_artefact` verbatim hands CodeLLDB an ELF it cannot
    // launch — the same class of failure, one directory entry over.
    #[test]
    fn native_host_resolves_native_sim_slice_not_the_first_zephyr_slice() {
        let dir = tmp("native-host-mixed");
        write_manifest(&dir, &manifest_mcu_then_native_sim(&dir));

        let (resolution, _) = resolve_from_build(
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

        let (resolution, _) = resolve_from_build(
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

        let (bare, _) = resolve_from_build(
            &dir,
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink,
            None,
        );
        assert_eq!(
            bare.executable.as_deref(),
            Some("${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );

        let (pinned, _) = resolve_from_build(
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

        let (resolution, _) = resolve_from_build(
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
}
