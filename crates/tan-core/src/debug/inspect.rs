// SPDX-License-Identifier: Apache-2.0
//! `alp inspect`: the resolved project-context values surfaced with a source
//! tag and human detail.

use serde::Serialize;

use super::context::DebugWorkspaceContext;

/// Where a resolved `alp inspect` value originated.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DebugValueSource {
    /// Derived from the active workspace folder.
    Workspace,
    /// From an explicit extension/user setting.
    Setting,
    /// A built-in default.
    Default,
    /// Probed at runtime (e.g. file existence).
    Runtime,
    /// Computed from other values.
    Derived,
    /// Could not be resolved.
    Unresolved,
}

/// A single resolved project value surfaced by `alp inspect`.
#[derive(Debug, Clone, Serialize)]
pub struct DebugResolvedValue {
    /// Stable key (e.g. `workspaceRoot`).
    pub key: String,
    /// The resolved value (string, bool, or null).
    pub value: serde_json::Value,
    /// How the value was obtained.
    pub source: DebugValueSource,
    /// Human-readable explanation.
    pub detail: String,
}

/// Mirror of TS `collectResolvedValues`: the project-context fields surfaced by
/// `alp inspect`, each tagged with a source + human detail.
pub fn collect_resolved_values(context: &DebugWorkspaceContext) -> Vec<DebugResolvedValue> {
    use serde_json::Value;

    let opt_value = |v: &Option<String>| match v {
        Some(s) => Value::String(s.clone()),
        None => Value::Null,
    };

    vec![
        DebugResolvedValue {
            key: "workspaceRoot".to_string(),
            value: opt_value(&context.workspace_root),
            source: if context.workspace_root.is_some() {
                DebugValueSource::Workspace
            } else {
                DebugValueSource::Unresolved
            },
            detail: if context.workspace_root.is_some() {
                "Resolved from the active workspace folder."
            } else {
                "No workspace folder is open."
            }
            .to_string(),
        },
        DebugResolvedValue {
            key: "sdkRoot".to_string(),
            value: opt_value(&context.sdk_root),
            source: if context.sdk_root.is_some() {
                DebugValueSource::Workspace
            } else {
                DebugValueSource::Unresolved
            },
            detail: if context.sdk_root.is_some() {
                "Resolved alp-sdk root used for scripts and schemas."
            } else {
                "Set alpSdk.path when automatic discovery is ambiguous."
            }
            .to_string(),
        },
        DebugResolvedValue {
            key: "boardYamlPath".to_string(),
            value: opt_value(&context.board_yaml_path),
            source: if context.board_yaml_path.is_some() {
                DebugValueSource::Setting
            } else {
                DebugValueSource::Unresolved
            },
            detail: if context.board_yaml_path.is_some() {
                "Resolved board.yaml path from project settings."
            } else {
                "board.yaml path is unresolved."
            }
            .to_string(),
        },
        DebugResolvedValue {
            key: "boardYamlExists".to_string(),
            value: serde_json::Value::Bool(context.board_yaml_exists),
            source: DebugValueSource::Runtime,
            detail: if context.board_yaml_exists {
                "board.yaml exists at the resolved path."
            } else {
                "board.yaml is missing at the resolved path."
            }
            .to_string(),
        },
        DebugResolvedValue {
            key: "westCwd".to_string(),
            value: opt_value(&context.west_cwd),
            source: if context.west_cwd.is_some() {
                DebugValueSource::Setting
            } else {
                DebugValueSource::Default
            },
            detail: if context.west_cwd.is_some() {
                "Working directory used for west commands."
            } else {
                "Defaults to the workspace root."
            }
            .to_string(),
        },
        DebugResolvedValue {
            key: "pythonBinary".to_string(),
            value: serde_json::Value::String(context.python_binary.clone()),
            source: if context.python_binary == "python3" || context.python_binary == "python" {
                DebugValueSource::Default
            } else {
                DebugValueSource::Setting
            },
            detail: "Interpreter used for loader and validation scripts.".to_string(),
        },
    ]
}
