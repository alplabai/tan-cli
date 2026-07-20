// SPDX-License-Identifier: Apache-2.0
//! `tan diff` — show how `normalize_board_model` changes the parsed board.yaml.
//!
//! Mirrors TS `runDiffCommand`: parse board.yaml, normalize it, and diff the
//! two as JSON trees (added/removed/changed by path). Null object entries are
//! pruned first so the diff matches the TS model's sparse (undefined-omitting)
//! shape.

use std::path::Path;

use serde_json::Value;
use tan_core::{
    BoardModel, DiffEntry, DiffKind, collect_diff_entries, normalize_board_model,
    parse_board_model, prune_nulls,
};

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// Envelope `data` payload for `tan diff`: the normalize-diff result for one board.yaml.
#[derive(serde::Serialize)]
struct DiffData {
    /// Schema version of this payload shape (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved path to the board.yaml that was diffed.
    #[serde(rename = "boardYamlPath")]
    board_yaml_path: String,
    /// `true` when normalization produced no effective-config changes.
    unchanged: bool,
    /// Number of entries in `changes`.
    #[serde(rename = "changeCount")]
    change_count: usize,
    /// Per-path added/removed/changed entries between the parsed and normalized models.
    changes: Vec<DiffEntry>,
}

/// Run `tan diff`: resolve and read board.yaml, parse + normalize the model, and emit
/// the JSON-tree diff as text lines and/or an envelope. Returns a success `CommandRun`
/// (even when there are no changes); read/parse problems are routed through `failure`.
pub fn run(g: &GlobalArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let board_path = match &context.board_yaml_path {
        Some(path) if Path::new(path).exists() => path.clone(),
        _ => {
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                "board-yaml-missing",
                "board.yaml path could not be resolved or the file does not exist.",
                context.board_yaml_path.clone().unwrap_or_default(),
                vec!["diff: board.yaml path is unresolved or missing.".to_string()],
            );
        }
    };

    let text = match std::fs::read_to_string(&board_path) {
        Ok(t) => t,
        Err(e) => {
            return failure(
                g,
                project,
                ExitCode::InternalFailure,
                "internal-failure",
                &format!("could not read board.yaml: {e}"),
                board_path.clone(),
                vec!["diff: internal failure".to_string(), e.to_string()],
            );
        }
    };

    let model = match parse_board_model(&text) {
        Ok(m) => m,
        // A malformed board.yaml is a validation failure the user needs to
        // fix, not an internal tan bug -- matches the offline `validate`
        // path's identical parse-error remap (exit 2 `schema-violation`,
        // not exit 5 `internal-failure`).
        Err(e) => {
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                "schema-violation",
                &e.to_string(),
                board_path.clone(),
                vec!["diff: validation failure".to_string(), e.to_string()],
            );
        }
    };

    // `.expect("board model is serializable")` used to sit here: BoardModel's
    // e1m_routes is opaque `serde_yaml::Value`, and serde_json refuses to
    // serialize a map with a non-string key (a null/sequence/mapping YAML
    // key). This binary is built `panic = "abort"`, so that `.expect()`
    // aborted the whole process with no envelope and no in-contract exit
    // code the moment a board.yaml carried an unusual `e1m_routes` shape.
    // Report it as an ordinary diff failure instead.
    let before = match serialize_pruned(&model) {
        Ok(v) => v,
        Err(message) => {
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                "board-model-not-representable",
                &message,
                board_path.clone(),
                vec!["diff: validation failure".to_string(), message.clone()],
            );
        }
    };
    let normalized = normalize_board_model(model);
    let after = match serialize_pruned(&normalized) {
        Ok(v) => v,
        Err(message) => {
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                "board-model-not-representable",
                &message,
                board_path.clone(),
                vec!["diff: validation failure".to_string(), message.clone()],
            );
        }
    };
    let changes = collect_diff_entries(&before, &after);

    let data = DiffData {
        schema_version: "1".to_string(),
        board_yaml_path: board_path.clone(),
        unchanged: changes.is_empty(),
        change_count: changes.len(),
        changes,
    };

    let text_lines = if g.is_json() {
        Vec::new()
    } else {
        format_diff_text(&data, g, &board_path)
    };
    let json = g.is_json().then(|| {
        Envelope::new("diff", project, data, Vec::new(), ExitCode::Success.code()).to_json()
    });

    CommandRun {
        exit: ExitCode::Success,
        text: text_lines,
        json,
    }
}

/// Serialize a board model to JSON and prune null object entries. Fallible:
/// `e1m_routes` carries arbitrary opaque `serde_yaml::Value` data, and
/// serde_json rejects a map with a non-string key (null/sequence/mapping),
/// which a hand-authored `e1m_routes` can legally contain in YAML.
fn serialize_pruned(model: &BoardModel) -> Result<Value, String> {
    serde_json::to_value(model)
        .map(prune_nulls)
        .map_err(|e| format!("board.yaml could not be represented as JSON: {e}"))
}

/// Build a failed `CommandRun` with the given `exit` code, a `diff.{code}` envelope issue,
/// and an empty-change `DiffData`. Text output is suppressed under `--json`.
#[allow(clippy::too_many_arguments)]
fn failure(
    g: &GlobalArgs,
    project: Project,
    exit: ExitCode,
    code: &str,
    message: &str,
    board_yaml_path: String,
    text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("diff.{code}"),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let data = DiffData {
        schema_version: "1".to_string(),
        board_yaml_path,
        unchanged: false,
        change_count: 0,
        changes: Vec::new(),
    };
    let text = if g.is_json() { Vec::new() } else { text_lines };
    let json = g
        .is_json()
        .then(|| Envelope::new("diff", project, data, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

/// Render the human-readable diff lines: a summary header plus one `KIND path: before -> after`
/// line per change (change lines omitted under `--quiet`); a single "no differences" line when empty.
fn format_diff_text(data: &DiffData, g: &GlobalArgs, board_path: &str) -> Vec<String> {
    if data.changes.is_empty() {
        return vec!["diff: no effective-config differences detected.".to_string()];
    }

    let mut lines = vec![format!(
        "diff: {} differences in {}",
        data.changes.len(),
        board_path
    )];
    if !g.quiet {
        for change in &data.changes {
            lines.push(format!(
                "{} {}: {} -> {}",
                kind_label(change.kind),
                change.path,
                format_value(&change.before),
                format_value(&change.after),
            ));
        }
    }
    lines
}

/// Map a `DiffKind` to its uppercase text label (`ADDED` / `REMOVED` / `CHANGED`).
fn kind_label(kind: DiffKind) -> &'static str {
    match kind {
        DiffKind::Added => "ADDED",
        DiffKind::Removed => "REMOVED",
        DiffKind::Changed => "CHANGED",
    }
}

/// Format a diff side for display: `<undefined>` for `None`, JSON-quoted for strings, and
/// compact JSON otherwise, truncated to 117 chars + `...` when longer than 120.
fn format_value(value: &Option<Value>) -> String {
    match value {
        None => "<undefined>".to_string(),
        Some(Value::String(s)) => serde_json::to_string(s).unwrap_or_else(|_| s.clone()),
        Some(val) => {
            let raw = serde_json::to_string(val).unwrap_or_else(|_| val.to_string());
            if raw.chars().count() > 120 {
                let truncated: String = raw.chars().take(117).collect();
                format!("{truncated}...")
            } else {
                raw
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serialize_pruned_succeeds_for_an_ordinary_model() {
        let model = BoardModel::default();
        assert!(serialize_pruned(&model).is_ok());
    }

    #[test]
    fn serialize_pruned_reports_non_string_map_key_instead_of_panicking() {
        // A complex (sequence) YAML key nested under e1m_routes -- legal YAML,
        // but serde_json's map-key serializer only accepts strings/ints/bools.
        // The old `.expect("board model is serializable")` aborted the
        // (panic=abort) process on input like this; must return Err instead.
        let yaml = "e1m_routes:\n  usb0:\n    ? [d_p, d_n]\n    : pads\n";
        let model = parse_board_model(yaml).expect("valid yaml");
        let err = serialize_pruned(&model).expect_err("non-string map key must be rejected");
        assert!(err.contains("could not be represented as JSON"));
    }
}
