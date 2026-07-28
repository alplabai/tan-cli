// SPDX-License-Identifier: Apache-2.0
//! `tan explain` — describe project/module templates and generation targets.
//!
//! Mirrors TS `runExplainCommand`: `--template` explains an init/scaffold
//! template, `--target` explains a generation output target, and no selector
//! prints an overview. Supplying both is an error (exit 1), as is an unknown id.

use tan_core::wizard::{ModuleTemplateDefinition, WizardFeatureFlags, WizardTemplateDefinition};
use tan_core::wizard::{list_module_templates, list_wizard_templates, vendored_library_names_for};
use tan_core::{GenerationTargetSupport, list_generation_target_support};

use super::CommandRun;
use crate::cli::{ExplainArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// Identifies what the explain result describes: its `kind`
/// (`overview` / `project-template` / `module-template` / `generation-target`)
/// and the resolved `value` (e.g. the template/target id).
#[derive(serde::Serialize)]
struct Selector {
    /// Category of the explained topic.
    kind: String,
    /// Resolved id of the explained topic (empty for an overview).
    value: String,
}

/// The catalog of all explainable ids, always emitted so callers can discover
/// valid selectors even on a failure.
#[derive(serde::Serialize)]
struct Available {
    /// `tan init` project template ids.
    #[serde(rename = "projectTemplates")]
    project_templates: Vec<String>,
    /// `tan scaffold` module template ids.
    #[serde(rename = "moduleTemplates")]
    module_templates: Vec<String>,
    /// Generation output target ids (the `emit` keys).
    #[serde(rename = "generationTargets")]
    generation_targets: Vec<String>,
}

/// JSON `data` payload of the `explain` envelope: the resolved selector, a
/// one-line summary, free-form detail lines, and the `available` catalog.
#[derive(serde::Serialize)]
struct ExplainData {
    /// Payload schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// What this result describes.
    selector: Selector,
    /// Human-readable one-line summary.
    summary: String,
    /// Detail lines elaborating the summary.
    details: Vec<String>,
    /// Catalog of all explainable ids.
    available: Available,
}

/// Runs `tan explain`: resolves `--template`/`--target` (mutually exclusive)
/// against the wizard/module/generation catalogs, or prints an overview when
/// neither is given. Both selectors or an unknown id yield a runtime failure.
pub fn run(g: &GlobalArgs, args: &ExplainArgs) -> CommandRun {
    let project_templates = list_wizard_templates();
    let module_templates = list_module_templates();
    let generation_targets = list_generation_target_support();

    let available = || Available {
        project_templates: project_templates
            .iter()
            .map(|t| t.id.as_str().to_string())
            .collect(),
        module_templates: module_templates
            .iter()
            .map(|t| t.id.as_str().to_string())
            .collect(),
        generation_targets: generation_targets
            .iter()
            .map(|t| t.emit.to_string())
            .collect(),
    };

    let requested_template = args
        .template
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let requested_target = g.target.as_deref().map(str::trim).filter(|s| !s.is_empty());

    if requested_template.is_some() && requested_target.is_some() {
        return failure(
            g,
            available(),
            Selector {
                kind: "overview".to_string(),
                value: String::new(),
            },
            "ambiguous-selector",
            "Use either --template or --target for explain, not both.",
            vec![
                "explain: use either --template or --target, but not both in the same command."
                    .to_string(),
            ],
        );
    }

    if let Some(requested) = requested_template {
        if let Some(pt) = project_templates
            .iter()
            .copied()
            .find(|t| t.id.as_str() == requested)
        {
            return success(
                g,
                available(),
                Selector {
                    kind: "project-template".to_string(),
                    value: pt.id.as_str().to_string(),
                },
                format!("{} ({})", pt.label, pt.id.as_str()),
                project_template_details(pt),
            );
        }
        if let Some(mt) = module_templates
            .iter()
            .copied()
            .find(|t| t.id.as_str() == requested)
        {
            return success(
                g,
                available(),
                Selector {
                    kind: "module-template".to_string(),
                    value: mt.id.as_str().to_string(),
                },
                format!("{} ({})", mt.label, mt.id.as_str()),
                module_template_details(mt),
            );
        }
        return failure(
            g,
            available(),
            Selector {
                kind: "overview".to_string(),
                value: requested.to_string(),
            },
            "template-unknown",
            &format!("Unknown template '{requested}'."),
            vec![format!(
                "explain: unknown template '{requested}'. Run tan explain without selectors to list available topics."
            )],
        );
    }

    if let Some(requested) = requested_target {
        let Some(target) = generation_targets.iter().find(|t| t.emit == requested) else {
            return failure(
                g,
                available(),
                Selector {
                    kind: "overview".to_string(),
                    value: requested.to_string(),
                },
                "target-unknown",
                &format!("Unknown generation target '{requested}'."),
                vec![format!(
                    "explain: unknown generation target '{requested}'. Run tan explain without selectors to list available targets."
                )],
            );
        };
        return success(
            g,
            available(),
            Selector {
                kind: "generation-target".to_string(),
                value: target.emit.to_string(),
            },
            format!("{} ({})", target.display_name, target.emit),
            generation_target_details(target),
        );
    }

    let overview = vec![
        "Use --template to explain a project template (init) or module template (scaffold)."
            .to_string(),
        "Use --target to explain a generation output target.".to_string(),
        format!(
            "Project templates: {}",
            project_templates
                .iter()
                .map(|t| t.id.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ),
        format!(
            "Module templates: {}",
            module_templates
                .iter()
                .map(|t| t.id.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ),
        format!(
            "Generation targets: {}",
            generation_targets
                .iter()
                .map(|t| t.emit)
                .collect::<Vec<_>>()
                .join(", ")
        ),
    ];
    success(
        g,
        available(),
        Selector {
            kind: "overview".to_string(),
            value: "all".to_string(),
        },
        "tan explain topics".to_string(),
        overview,
    )
}

/// Builds the detail lines for a project (`tan init`) template: description,
/// per-template explanation, default libraries, and default feature flags.
fn project_template_details(pt: &WizardTemplateDefinition) -> Vec<String> {
    let mut details = vec![pt.description.to_string()];
    details.extend(pt.explanation.iter().map(|s| (*s).to_string()));
    details.push(format!(
        "Default libraries: {}",
        format_library_names(&template_library_names(pt))
    ));
    details.push(format!(
        "Default features: {}",
        format_feature_flags(pt.features.as_ref())
    ));
    details
}

/// The library names to report for `pt`: the vendored `board.yaml`'s
/// `libraries:` block for a template that has a vendored SDK scaffold
/// (`vendored_library_names_for` returns `Some(..)`, even an empty vec, for
/// every mapped template — that empty answer IS what the scaffold ships,
/// e.g. `zephyr-app`/`sensor-starter`/`board-diagnostics`), or the
/// registry's own `libs` field for the one template still hand-generated
/// (`minimal-app`, `vendored_library_names_for` returns `None` for it).
///
/// Fixes tan-cli#124: this used to read `pt.libs` unconditionally, which is
/// deliberately blanked in the registry for a vendored template (the SDK
/// scaffold's real bytes are the source of truth for what `tan init`
/// writes, not that field) — `edge-ai-starter` reported "Default libraries:
/// (none)" while its vendored board.yaml declares `tflite-micro`.
fn template_library_names(pt: &WizardTemplateDefinition) -> Vec<String> {
    match vendored_library_names_for(pt.id) {
        Some(names) => names,
        None => pt.libs.iter().map(|s| s.to_string()).collect(),
    }
}

/// Formats a resolved library-name list as a comma-joined string, or
/// `(none)` when empty.
fn format_library_names(names: &[String]) -> String {
    if names.is_empty() {
        "(none)".to_string()
    } else {
        names.join(", ")
    }
}

/// Builds the detail lines for a module (`tan scaffold`) template: description,
/// generated function prefix, and a usage hint.
fn module_template_details(mt: &ModuleTemplateDefinition) -> Vec<String> {
    vec![
        mt.description.to_string(),
        format!("Function prefix: {}", mt.function_prefix),
        "Use this template with tan scaffold to generate a module source/header baseline."
            .to_string(),
    ]
}

/// Builds the detail lines for a generation target: display name, output path,
/// and preview label/language.
fn generation_target_details(target: &GenerationTargetSupport) -> Vec<String> {
    vec![
        format!("Display name: {}", target.display_name),
        format!("Output path: {}", target.output_relative_path),
        format!("Preview label: {}", target.preview_label),
        format!("Preview language: {}", target.preview_language_id),
    ]
}

/// Formats the four wizard feature flags as `wifi=.. mqtt=.. ble=.. tls=..`,
/// treating a `None` flag set as all-false.
fn format_feature_flags(features: Option<&WizardFeatureFlags>) -> String {
    let (wifi, mqtt, ble, tls) = match features {
        Some(f) => (f.wifi, f.mqtt, f.ble, f.tls),
        None => (false, false, false, false),
    };
    format!("wifi={wifi} mqtt={mqtt} ble={ble} tls={tls}")
}

/// An empty `Project` (no root, no `board.yaml`) — explain is project-agnostic.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

/// Assembles a success `CommandRun` (exit `Success`): text lines in human mode,
/// or an `explain` envelope with `ExplainData` in JSON mode.
fn success(
    g: &GlobalArgs,
    available: Available,
    selector: Selector,
    summary: String,
    details: Vec<String>,
) -> CommandRun {
    let text = if g.is_json() {
        Vec::new()
    } else {
        explain_text(&summary, &details, g)
    };
    let data = ExplainData {
        schema_version: "1".to_string(),
        selector,
        summary,
        details,
        available,
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "explain",
            null_project(),
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

/// Assembles a failure `CommandRun` (exit `RuntimeFailure`) carrying an
/// `explain.{code}` error issue: the given `text_lines` in human mode, or an
/// envelope with empty summary/details in JSON mode.
fn failure(
    g: &GlobalArgs,
    available: Available,
    selector: Selector,
    code: &str,
    issue_message: &str,
    text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("explain.{code}"),
        severity: "error".to_string(),
        message: issue_message.to_string(),
    }];
    let data = ExplainData {
        schema_version: "1".to_string(),
        selector,
        summary: String::new(),
        details: Vec::new(),
        available,
    };
    let text = if g.is_json() { Vec::new() } else { text_lines };
    let json = g.is_json().then(|| {
        Envelope::new(
            "explain",
            null_project(),
            data,
            issues,
            ExitCode::RuntimeFailure.code(),
        )
        .to_json()
    });
    CommandRun {
        exit: ExitCode::RuntimeFailure,
        text,
        json,
    }
}

/// Renders human-mode output: an `explain: {summary}` header followed by one
/// `- {detail}` line per detail (detail lines are suppressed when `--quiet`).
fn explain_text(summary: &str, details: &[String], g: &GlobalArgs) -> Vec<String> {
    let mut lines = vec![format!("explain: {summary}")];
    if !g.quiet {
        for detail in details {
            lines.push(format!("- {detail}"));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn globals(format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    #[test]
    fn edge_ai_starter_reports_its_vendored_library() {
        // Regression guard for tan-cli#124: the registry's `libs` field is
        // blanked (`&[]`) for this vendored template, but its real vendored
        // board.yaml declares `libraries: [tflite-micro]` -- `explain` must
        // report that, not "(none)".
        let args = ExplainArgs {
            template: Some("edge-ai-starter".to_string()),
        };
        let run = run(&globals(Format::Text), &args);
        assert_eq!(run.exit, ExitCode::Success);
        let text = run.text.join("\n");
        assert!(
            text.contains("- Default libraries: tflite-micro"),
            "got:\n{text}"
        );
        assert!(!text.contains("Default libraries: (none)"), "got:\n{text}");
    }

    #[test]
    fn edge_ai_starter_json_envelope_reports_its_vendored_library() {
        // Same fact, through the JSON envelope's `data.details` array (what
        // the vscode extension actually parses).
        let args = ExplainArgs {
            template: Some("edge-ai-starter".to_string()),
        };
        let run = run(&globals(Format::Json), &args);
        let json = run.json.expect("json envelope must be present");
        assert!(
            json.contains("Default libraries: tflite-micro"),
            "got:\n{json}"
        );
    }

    #[test]
    fn iot_starter_still_reports_mbedtls_and_its_feature_flags() {
        // iot-starter was already hand-synced correctly ahead of this fix
        // (tan-cli#128) -- pins that switching the "Default libraries" line
        // to derive from the vendored board.yaml (tan-cli#124) didn't
        // regress it, and that "Default features" (still registry-sourced)
        // is untouched.
        let args = ExplainArgs {
            template: Some("iot-starter".to_string()),
        };
        let run = run(&globals(Format::Text), &args);
        let text = run.text.join("\n");
        assert!(
            text.contains("- Default libraries: mbedtls"),
            "got:\n{text}"
        );
        assert!(
            text.contains("- Default features: wifi=true mqtt=true ble=false tls=true"),
            "got:\n{text}"
        );
    }

    #[test]
    fn zephyr_app_and_board_diagnostics_genuinely_report_no_libraries() {
        // Both are vendored templates whose real board.yaml has no
        // `libraries:` block at all -- "(none)" is the correct, SOURCED
        // answer for them (`Some(vec![])` from `vendored_library_names_for`),
        // not an unread hand-generator default.
        for id in ["zephyr-app", "board-diagnostics"] {
            let args = ExplainArgs {
                template: Some(id.to_string()),
            };
            let run = run(&globals(Format::Text), &args);
            let text = run.text.join("\n");
            assert!(
                text.contains("- Default libraries: (none)"),
                "{id} got:\n{text}"
            );
        }
    }

    #[test]
    fn minimal_app_falls_back_to_the_registry_libs_field() {
        // minimal-app has no vendored tree -- `vendored_library_names_for`
        // returns `None` for it, so `explain` must keep reading the
        // registry's own (empty) `libs` field for this one template.
        let args = ExplainArgs {
            template: Some("minimal-app".to_string()),
        };
        let run = run(&globals(Format::Text), &args);
        assert_eq!(run.exit, ExitCode::Success);
        let text = run.text.join("\n");
        assert!(text.contains("- Default libraries: (none)"), "got:\n{text}");
    }
}
