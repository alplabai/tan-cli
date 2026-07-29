// SPDX-License-Identifier: Apache-2.0
//! `tan init` — initialize a new tan project from a template.

use std::path::PathBuf;

use tan_core::wizard::{
    IOT_STARTER_SUPPORTED_SKU, WizardPlanInput, WizardTemplateId, create_wizard_plan_with_cores,
    vendored_core_ids_for,
};

use super::CommandRun;
use crate::cli::{GlobalArgs, InitArgs};
use crate::exit::ExitCode;

mod from_example;
mod resolve;
mod response;

use from_example::{finish, run_from_example};
use resolve::ResolveErr::{BadArg, Cancelled};
use resolve::{
    app_core_for_template, parse_cores, resolve_destination, resolve_name, resolve_template,
};
use response::{error_run, runtime_failure_run};

// ---------------------------------------------------------------------------
// JSON envelope data
// ---------------------------------------------------------------------------

/// One planned file change in the JSON envelope: its workspace-relative path and
/// change kind (`new`/`update`/`unchanged`, from `WizardFileChangeKind`).
#[derive(serde::Serialize)]
struct FileChangeSer {
    #[serde(rename = "relativePath")]
    relative_path: String,
    kind: String,
}

/// `data` payload for the `init` envelope: the resolved template/destination,
/// whether this was a preview, the planned `file_changes`, post-write
/// `written`/`unchanged` lists, and the SDK path pinned into the new project
/// (if any resolved).
#[derive(serde::Serialize)]
struct InitData {
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    #[serde(rename = "templateId")]
    template_id: String,
    destination: String,
    preview: bool,
    #[serde(rename = "fileChanges")]
    file_changes: Vec<FileChangeSer>,
    written: Vec<String>,
    unchanged: Vec<String>,
    /// SDK path written to the new project's `.alp/sdk-path` pin, or `None`
    /// when no SDK resolved (serialized as `sdkPinned`). Only set on the
    /// actual write path — never during `--preview` or on an error/guard
    /// response, where no files (and no pin) were written.
    #[serde(rename = "sdkPinned")]
    sdk_pinned: Option<String>,
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/// Execute `tan init`: resolve template/name/destination (prompting when
/// interactive), build the scaffold plan (heterogeneous when `--cores` is given),
/// then preview or write files — guarding overwrites behind `--force`.
pub fn run(g: &GlobalArgs, args: &InitArgs) -> CommandRun {
    // `--format json` is the mode the extension always uses and never has a
    // human at the keyboard to answer a prompt; omitting it here let a JSON
    // caller with an unset optional flag (e.g. no `--template`) block forever
    // on an inquire prompt rendered to stderr, or — if stdin was already
    // closed — cancel it and exit 1 with zero bytes on stdout.
    //
    // #198 added the missing terminal term here as a local `interactive_mode`
    // predicate. That predicate now lives on `GlobalArgs` — with its tests, and
    // with the stderr term it was missing — so `scaffold` and `bootstrap`
    // answer the same question from the same place instead of each carrying
    // their own copy. See `GlobalArgs::can_prompt`.
    let is_interactive = g.can_prompt();

    // From-example path: copy an existing SDK example verbatim. Short-circuits
    // before template resolution so it never engages the non-interactive
    // ZephyrApp default; --som/--cores are ignored (the example ships its own
    // board.yaml).
    if let Some(src) = args.from_example.as_deref() {
        return run_from_example(g, args, src, is_interactive);
    }

    // 1. Resolve template.
    let template_id = match resolve_template(args.template.as_deref(), is_interactive) {
        Ok(id) => id,
        Err(Cancelled) => return runtime_failure_run(g),
        Err(BadArg(msg)) => {
            return error_run(
                g,
                ExitCode::ValidationFailure,
                "init.invalid-template",
                &msg,
            );
        }
    };

    // 1b. iot-starter's vendored tree covers exactly one SoM SKU -- the SDK
    // catalog's `iot` entry is AEN-only + preview (its Wi-Fi transport is the
    // CC3501E bridge, silicon-validated only on E1M-AEN801; see
    // wizard/vendored/MANIFEST.md). Reject any other --som right here, before
    // an interactive user is prompted for name/destination (steps 2-3) for
    // nothing -- `sku` depends only on `args.som`, never on those prompts --
    // instead of silently falling back onto a hand-written generator, the
    // exact drift issue #14 retires on every other template.
    let sku = args.som.as_deref().unwrap_or(tan_core::DEFAULT_SOM_SKU);
    if template_id == WizardTemplateId::IotStarter && sku != IOT_STARTER_SUPPORTED_SKU {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-som",
            &format!(
                "Template 'iot-starter' supports only SoM SKU '{IOT_STARTER_SUPPORTED_SKU}'; got '{sku}'."
            ),
        );
    }

    // 2. Resolve name (optional).
    let name = match resolve_name(args.name.as_deref(), is_interactive) {
        Ok(n) => n,
        Err(Cancelled) => return runtime_failure_run(g),
        Err(BadArg(msg)) => {
            return error_run(g, ExitCode::ValidationFailure, "init.invalid-name", &msg);
        }
    };

    // 3. Resolve destination.
    let destination = match resolve_destination(
        args.destination.as_deref(),
        g.project.as_deref(),
        is_interactive,
    ) {
        Ok(d) => d,
        Err(_) => return runtime_failure_run(g),
    };

    // 4. Compute project root.
    let dest_path = PathBuf::from(&destination);
    let project_root = if name.is_empty() {
        dest_path.clone()
    } else {
        dest_path.join(&name)
    };

    // 5. Build plan (heterogeneous when --cores is given; else single-core).
    let cores = match parse_cores(args.cores.as_deref()) {
        Ok(cores) => cores,
        Err(msg) => return error_run(g, ExitCode::ValidationFailure, "init.invalid-cores", &msg),
    };
    // The app core's runtime is fixed (the scaffolded src/ + prj.conf are
    // Zephyr); reject a contradictory --cores request instead of silently
    // overriding it. See `app_core_for_template` for why this must NOT
    // uniformly call `app_core_for_sku`. (`sku` and the iot-starter --som
    // guard are resolved back in step 1b, before the interactive prompts.)
    let app_core = app_core_for_template(template_id, sku);
    if let Some((_, os)) = cores
        .iter()
        .find(|(id, os)| id.as_str() == app_core && os.as_str() != "zephyr")
    {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-cores",
            &format!(
                "Core '{app_core}' is this SoM's app core and runs zephyr; --cores requested '{os}'. Omit the entry or use {app_core}:zephyr."
            ),
        );
    }
    // The vendored scaffold may already pre-declare a companion core (e.g.
    // edge-ai ships `a55_cluster`/`a32_cluster`, os: "off") -- a --cores id
    // colliding with one would append a SECOND, duplicate `cores:` mapping
    // key (serde_yaml/`tan validate` reject it; the SDK Python loader takes
    // last-wins). Reject rather than silently drop the user's os or silently
    // override the scaffold's.
    if let Some((collide_id, os)) = vendored_core_ids_for(template_id, sku)
        .into_iter()
        .find(|(id, _)| id.as_str() != app_core && cores.iter().any(|(cid, _)| cid == id))
    {
        return error_run(
            g,
            ExitCode::ValidationFailure,
            "init.invalid-cores",
            &format!(
                "Core '{collide_id}' is already declared by the {} scaffold (os: {}); edit board.yaml to change its os instead of passing --cores.",
                template_id.as_str(),
                os.trim_matches('"'),
            ),
        );
    }
    let mut plan = create_wizard_plan_with_cores(
        &WizardPlanInput {
            template_id,
            project_name: name.clone(),
            destination: destination.clone(),
            som_sku: args.som.clone(),
        },
        &cores,
    );

    // 5b. Honor --board-yaml: emit the caller's board.yaml verbatim instead of the
    // generated stub. This lets Alp Studio adopt `tan init` as its project render --
    // it passes a fully-resolved board.yaml and expects it copied through untouched
    // (alp-sdk-vscode#64). A template that emits no board.yaml (none does today,
    // now that host-tooling-starter -- the previous example -- is retired) would
    // have nothing to override, so pairing one with --board-yaml is a hard error
    // rather than a silent no-op that would drop the caller's file.
    if let Some(path) = g.board_yaml.as_deref() {
        match std::fs::read_to_string(path) {
            Ok(content) => {
                let mut applied = false;
                for file in plan.files.iter_mut() {
                    if file.relative_path == "board.yaml" {
                        file.content = content.clone();
                        applied = true;
                    }
                }
                if !applied {
                    return error_run(
                        g,
                        ExitCode::ValidationFailure,
                        "init.board-yaml-unsupported",
                        &format!(
                            "--board-yaml was given but template '{}' emits no board.yaml to override.",
                            template_id.as_str()
                        ),
                    );
                }
            }
            Err(err) => {
                return error_run(
                    g,
                    ExitCode::ValidationFailure,
                    "init.board-yaml-unreadable",
                    &format!("--board-yaml '{path}' could not be read: {err}"),
                );
            }
        }
    }

    // Resolve the SDK for the user's actual workspace (cwd honoring
    // --project — NOT the new, often-nested `project_root`) so `finish` can
    // pin it into the new project; `--from-example` reuses the SDK root it
    // already resolved the same way to locate examples/.
    let sdk_root_for_pin = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));

    // 6-9. Diff, guard overwrites, preview, write (shared with the from-example path).
    finish(
        g,
        args,
        template_id.as_str(),
        &destination,
        &project_root,
        &plan.files,
        sdk_root_for_pin.as_deref(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    #[test]
    fn init_pins_the_sdk_discovered_at_the_workspace_root_not_the_nested_project_dir() {
        // Regression: the pin used to re-resolve via `resolve_sdk_tiered(g,
        // project_root)` — `project_root` is the NEW, nested project dir
        // (`<ws>/myproj`), which has no SDK of its own and no `alp-sdk`/
        // `alp-sdk-upstream` sibling either, so a workspace-root-is-the-SDK
        // (discovery) or project-pin resolution silently produced
        // `sdkPinned: null` even though `resolve_sdk_root(g,
        // cli_workspace_root(g))` — what every other command actually uses —
        // resolves the SDK sitting right at the workspace root.
        let tag = format!("init-pin-discovery-{}", std::process::id());
        let ws = std::env::temp_dir().join(tag);
        let _ = std::fs::remove_dir_all(&ws);
        std::fs::create_dir_all(ws.join("scripts")).unwrap();
        std::fs::write(ws.join("scripts").join("alp_project.py"), "").unwrap();

        let g = GlobalArgs {
            project: Some(ws.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: true,
            ci: false,
        };
        let args = InitArgs {
            template: None,
            from_example: None,
            name: Some("myproj".to_string()),
            destination: None,
            som: None,
            cores: None,
            preview: false,
            force: false,
        };

        let result = run(&g, &args);
        assert_eq!(result.exit, ExitCode::Success);
        let json: serde_json::Value =
            serde_json::from_str(result.json.as_deref().expect("json envelope")).unwrap();
        assert_eq!(
            json["data"]["sdkPinned"].as_str(),
            Some(ws.to_string_lossy().as_ref()),
            "sdkPinned must be the workspace-root SDK, not null"
        );
        assert!(ws.join("myproj").join(".alp").join("sdk-path").exists());

        std::fs::remove_dir_all(&ws).unwrap();
    }

    #[test]
    fn edge_ai_cores_colliding_with_a_pre_declared_companion_is_rejected() {
        // Regression: edge-ai's vendored board.yaml already declares a
        // companion core (a55_cluster on V2N, a32_cluster on AEN, os: "off").
        // --cores re-declaring that same id used to append a SECOND `cores:`
        // mapping key -- duplicate YAML rejected by tan validate/serde_yaml.
        let g = GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: true,
            ci: false,
        };
        for (sku, companion) in [("E1M-V2N101", "a55_cluster"), ("E1M-AEN801", "a32_cluster")] {
            let ws = std::env::temp_dir().join(format!(
                "edge-ai-cores-collide-{sku}-{}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&ws);
            let args = InitArgs {
                template: Some("edge-ai-starter".to_string()),
                from_example: None,
                name: Some("proj".to_string()),
                destination: Some(ws.to_string_lossy().into_owned()),
                som: Some(sku.to_string()),
                cores: Some(format!("{companion}:yocto")),
                preview: false,
                force: false,
            };

            let result = run(&g, &args);
            assert_eq!(result.exit, ExitCode::ValidationFailure);
            let json: serde_json::Value =
                serde_json::from_str(result.json.as_deref().expect("json envelope")).unwrap();
            assert_eq!(
                json["issues"][0]["code"].as_str(),
                Some("init.invalid-cores")
            );
            let message = json["issues"][0]["message"].as_str().unwrap_or_default();
            assert!(message.contains(companion), "message: {message}");
            assert!(message.contains("edge-ai-starter"), "message: {message}");
            assert!(!ws.exists(), "a rejected --cores must not write any files");
        }
    }

    #[test]
    fn iot_starter_rejects_a_non_aen801_sku_and_names_the_supported_set() {
        // iot-starter's vendored tree covers exactly one SoM SKU
        // (E1M-AEN801 -- the SDK catalog's `iot` entry is AEN-only + preview,
        // its Wi-Fi transport is the CC3501E bridge). Any other --som must be
        // rejected before a single file is planned, never silently rendered
        // against the AEN tree -- that's the exact drift issue #14 retires
        // on every other template.
        let g = GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: true,
            ci: false,
        };
        let ws = std::env::temp_dir().join(format!("iot-starter-bad-sku-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&ws);
        let args = InitArgs {
            template: Some("iot-starter".to_string()),
            from_example: None,
            name: Some("proj".to_string()),
            destination: Some(ws.to_string_lossy().into_owned()),
            som: Some("E1M-V2N101".to_string()),
            cores: None,
            preview: false,
            force: false,
        };

        let result = run(&g, &args);
        assert_eq!(result.exit, ExitCode::ValidationFailure);
        let json: serde_json::Value =
            serde_json::from_str(result.json.as_deref().expect("json envelope")).unwrap();
        assert_eq!(json["issues"][0]["code"].as_str(), Some("init.invalid-som"));
        let message = json["issues"][0]["message"].as_str().unwrap_or_default();
        assert!(message.contains("E1M-AEN801"), "message: {message}");
        assert!(message.contains("E1M-V2N101"), "message: {message}");
        assert!(!ws.exists(), "a rejected --som must not write any files");
    }
}
