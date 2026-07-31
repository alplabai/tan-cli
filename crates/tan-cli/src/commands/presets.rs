// SPDX-License-Identifier: Apache-2.0
//! `tan presets` — list SDK presets (SKUs) plus built-in defaults.
//!
//! Mirrors TS `runPresetsCommand`: built-in library/inference/log/os defaults
//! come from `empty_preset_catalogue`; SKUs are discovered from
//! `<sdk>/metadata/e1m_modules`. An unresolved SDK root is a warning (not a
//! failure) — defaults are still returned.
//!
//! There is no `<sdk>/metadata/carriers` tree in alp-sdk (board.yaml has no
//! `carrier:` block either — see `board.schema.json`'s `additionalProperties:
//! false`), so this command no longer discovers/reports carrier presets.

use std::path::Path;

use tan_core::bootstrap::runtime_for_topology_core;
use tan_core::{empty_preset_catalogue, parse_som_preset};

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// One SoM preset discovered under `<sdk>/metadata/e1m_modules`: SKU id,
/// display name, family, and per-core runtime topology.
#[derive(serde::Serialize)]
struct SomEntry {
    /// SoM SKU id (e.g. `E1M-...`).
    sku: String,
    /// Human-readable SoM name.
    #[serde(rename = "displayName")]
    display_name: String,
    /// SoM family classifier.
    family: String,
    /// Per-core runtime topology (id + resolved OS), for heterogeneous scaffolding.
    cores: Vec<SomCoreEntry>,
}

/// One core of a SoM's topology: its id and the runtime it naturally runs
/// (zephyr for a Cortex-M `board:`, yocto for a Cortex-A `machine:`).
#[derive(serde::Serialize)]
struct SomCoreEntry {
    /// Core id within the SoM topology.
    id: String,
    /// Resolved runtime for the core (`zephyr`, `yocto`, or heuristic fallback).
    os: String,
}

/// The `presets` command payload: discovered SDK presets (SKUs/SoMs) plus the
/// built-in library/inference/log/os defaults. Serialized as the envelope
/// `data` field.
#[derive(serde::Serialize)]
struct PresetsData {
    /// Payload schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved SDK root, or `None` when unresolved.
    #[serde(rename = "sdkRoot")]
    sdk_root: Option<String>,
    /// Bare SKU ids (back-compat); derived from `soms`.
    skus: Vec<String>,
    /// Rich SoM presets discovered from `<sdk>/metadata/e1m_modules/*.yaml`.
    soms: Vec<SomEntry>,
    /// Built-in library defaults from `empty_preset_catalogue` (the per-core
    /// `cores.<id>.libraries` token set).
    libraries: Vec<String>,
    /// ADR-0018 curated libraries discovered from
    /// `<sdk>/metadata/libraries/*.yaml` — the values a board.yaml top-level
    /// `libraries:` entry names (distinct from the per-core `libraries` tokens
    /// above). Empty when the SDK root is unresolved.
    #[serde(rename = "boardLibraries")]
    board_libraries: Vec<String>,
    /// Built-in inference-backend defaults.
    #[serde(rename = "inferenceBackends")]
    inference_backends: Vec<String>,
    /// Built-in log-level defaults.
    #[serde(rename = "logLevels")]
    log_levels: Vec<String>,
    /// Built-in OS choices.
    #[serde(rename = "osChoices")]
    os_choices: Vec<String>,
}

/// Entry point for `tan presets`. Resolves the project context, discovers SoMs
/// from the SDK root (empty + a warning issue when unresolved), merges in
/// built-in defaults, and emits the text or JSON envelope.
pub fn run(g: &GlobalArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let defaults = empty_preset_catalogue();

    let (soms, board_libraries) = match &context.sdk_root {
        Some(root) => (read_soms(root), read_board_libraries(root)),
        None => (Vec::new(), Vec::new()),
    };
    let skus: Vec<String> = soms.iter().map(|s| s.sku.clone()).collect();

    let mut issues = Vec::new();
    if context.sdk_root.is_none() {
        issues.push(Issue {
            code: "presets.sdk-root-unresolved".to_string(),
            severity: "warning".to_string(),
            message:
                "alp-sdk root is unresolved. Returning built-in defaults and empty SDK preset lists."
                    .to_string(),
        });
    }

    let data = PresetsData {
        schema_version: "1".to_string(),
        sdk_root: context.sdk_root.clone(),
        skus,
        soms,
        libraries: defaults.libraries,
        board_libraries,
        inference_backends: defaults.inference_backends,
        log_levels: defaults.log_levels,
        os_choices: defaults.os_choices,
    };

    let text = if g.is_json() {
        Vec::new()
    } else {
        presets_text(&data, g)
    };
    let project = Project::from_context(&context);
    let json = g.is_json().then(|| {
        Envelope::new("presets", project, data, issues, ExitCode::Success.code()).to_json()
    });

    CommandRun {
        exit: ExitCode::Success,
        text,
        json,
    }
}

/// Discover SoM presets from `<sdk>/metadata/e1m_modules`, parsing each
/// (sku + display_name + family) via the shared catalogue parser. Supports both
/// layouts the SDK has used: a flat `E1M-X.yaml` file, or an `E1M-X/som.yaml`
/// directory. Entries that aren't `E1M-*` or lack a yaml are skipped.
fn read_soms(sdk_root: &str) -> Vec<SomEntry> {
    let dir = Path::new(sdk_root).join("metadata").join("e1m_modules");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut soms: Vec<SomEntry> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().to_string();
            if !name.starts_with("E1M-") {
                return None;
            }
            let path = entry.path();
            let yaml_path = if path.is_dir() {
                path.join("som.yaml")
            } else if name.ends_with(".yaml") {
                path
            } else {
                return None;
            };
            let text = std::fs::read_to_string(&yaml_path).ok()?;
            let som = parse_som_preset(&text).ok()?;
            let cores = som
                .topology
                .iter()
                .map(|t| SomCoreEntry {
                    id: t.id.clone(),
                    // OS is resolved from the topology: a `board:` is a Zephyr
                    // (Cortex-M) target, a `machine:` is a Yocto (Cortex-A) one;
                    // fall back to the shared silicon-class heuristic. Shared
                    // with `tan bootstrap`'s Yocto host gate — one owner, so
                    // what `presets` reports and what `bootstrap` gates on can
                    // never disagree.
                    os: runtime_for_topology_core(t).to_string(),
                })
                .collect();
            Some(SomEntry {
                sku: som.sku,
                display_name: som.display_name,
                family: som.family,
                cores,
            })
        })
        .collect();
    soms.sort_by(|a, b| a.sku.cmp(&b.sku));
    soms
}

/// Discover the ADR-0018 curated libraries from `<sdk>/metadata/libraries`: the
/// stem of each `<name>.yaml` manifest (`README*` + non-yaml entries skipped),
/// sorted + de-duplicated. These are the values a board.yaml top-level
/// `libraries:` entry names — distinct from the built-in per-core token
/// defaults in `libraries`.
fn read_board_libraries(sdk_root: &str) -> Vec<String> {
    let dir = Path::new(sdk_root).join("metadata").join("libraries");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().to_string();
            if !name.ends_with(".yaml") || name.to_ascii_lowercase().starts_with("readme") {
                return None;
            }
            Some(name.trim_end_matches(".yaml").to_string())
        })
        .collect();
    names.sort();
    names.dedup();
    names
}

/// Render the human-readable (non-JSON) output lines: a summary count line,
/// plus per-SKU lines when `g.verbose` is set.
fn presets_text(data: &PresetsData, g: &GlobalArgs) -> Vec<String> {
    let mut lines = vec![format!(
        "presets: skus={} libraries={} boardLibraries={}",
        data.skus.len(),
        data.libraries.len(),
        data.board_libraries.len()
    )];
    if g.verbose {
        for sku in &data.skus {
            lines.push(format!("sku: {sku}"));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE_SDK: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../contract/fixtures/presets/sdk-present"
    );

    #[test]
    fn read_board_libraries_lists_yaml_stems_sorted_and_skips_readme() {
        assert_eq!(read_board_libraries(FIXTURE_SDK), vec!["aws-iot", "lvgl"]);
    }

    #[test]
    fn read_board_libraries_empty_when_dir_missing() {
        assert!(read_board_libraries("/no/such/sdk/root").is_empty());
    }
}
