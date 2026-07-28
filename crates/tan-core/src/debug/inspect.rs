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
                "Resolved project directory (cwd, or --project <dir>)."
            } else {
                "No project directory resolved — pass --project <dir>."
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
                "Set with --sdk-root <path> or `tan sdk switch <path>` when \
                 automatic discovery is ambiguous."
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
                "Resolved board.yaml path (default location, or --board-yaml <path>)."
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

#[cfg(test)]
mod tests {
    use super::super::context::DebuggerExtensionsState;
    use super::*;

    fn extensions() -> DebuggerExtensionsState {
        DebuggerExtensionsState {
            cortex_debug: true,
            cpp_tools: true,
            code_lldb: true,
            observable: false,
        }
    }

    fn context_with(workspace_root: Option<&str>, sdk_root: Option<&str>) -> DebugWorkspaceContext {
        DebugWorkspaceContext {
            generated_at: "1970-01-01T00:00:00.000Z".to_string(),
            workspace_root: workspace_root.map(str::to_string),
            sdk_root: sdk_root.map(str::to_string),
            board_yaml_path: None,
            west_cwd: None,
            python_binary: "python3".to_string(),
            board_yaml_exists: false,
            project_selected: false,
            debugger_extensions: extensions(),
        }
    }

    /// #134: the unresolved details used to be "No workspace folder is open."
    /// and "Set alpSdk.path when automatic discovery is ambiguous." -- both VS
    /// Code phrasing that a terminal reader, who has no such setting, cannot
    /// act on. They must name a `tan` flag instead.
    #[test]
    fn unresolved_values_name_a_cli_flag_not_a_vscode_setting() {
        let values = collect_resolved_values(&context_with(None, None));

        let workspace = values.iter().find(|v| v.key == "workspaceRoot").unwrap();
        assert!(
            !workspace.detail.contains("workspace folder"),
            "{}",
            workspace.detail
        );
        assert!(
            workspace.detail.contains("--project"),
            "{}",
            workspace.detail
        );

        let sdk = values.iter().find(|v| v.key == "sdkRoot").unwrap();
        assert!(!sdk.detail.contains("alpSdk"), "{}", sdk.detail);
        assert!(
            sdk.detail.contains("--sdk-root") && sdk.detail.contains("tan sdk switch"),
            "{}",
            sdk.detail
        );
    }

    #[test]
    fn resolved_values_still_read_correctly() {
        let values =
            collect_resolved_values(&context_with(Some("/work/proj"), Some("/work/alp-sdk")));
        let workspace = values.iter().find(|v| v.key == "workspaceRoot").unwrap();
        assert_eq!(
            workspace.value,
            serde_json::Value::String("/work/proj".to_string())
        );
        assert_eq!(workspace.source, DebugValueSource::Workspace);
    }
}
