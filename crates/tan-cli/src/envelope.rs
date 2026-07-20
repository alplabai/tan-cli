// SPDX-License-Identifier: Apache-2.0
//! Machine-readable result envelope — byte-compatible with the TypeScript
//! CLI's `CliEnvelope`. JSON mode writes exactly one of these to stdout.

use serde::Serialize;

/// A single diagnostic carried in an envelope's `issues` list.
#[derive(Debug, Clone, Serialize)]
pub struct Issue {
    /// Stable machine-readable issue identifier.
    pub code: String,
    /// Severity label (e.g. `error`, `warning`).
    pub severity: String,
    /// Human-readable description.
    pub message: String,
}

/// Project context attached to every envelope; fields are absent when unresolved.
#[derive(Debug, Clone, Serialize)]
pub struct Project {
    /// Absolute project root path, if resolved.
    pub root: Option<String>,
    /// Absolute path to the project's `board.yaml`, if found.
    #[serde(rename = "boardYaml")]
    pub board_yaml: Option<String>,
}

/// Top-level result envelope serialized to stdout in JSON mode; `T` is the
/// command-specific payload.
#[derive(Debug, Clone, Serialize)]
pub struct Envelope<T: Serialize> {
    /// Name of the command that produced this envelope.
    pub command: String,
    /// Whether the command succeeded (mirrors `exit_code == 0`).
    pub ok: bool,
    /// Process exit code (0 success; see crate exit-code contract).
    #[serde(rename = "exitCode")]
    pub exit_code: i32,
    /// Resolved project context.
    pub project: Project,
    /// Command-specific result payload.
    pub data: T,
    /// Diagnostics emitted by the command.
    pub issues: Vec<Issue>,
}

impl<T: Serialize> Envelope<T> {
    /// Build an envelope; `ok` is derived from `exit_code == 0`.
    pub fn new(
        command: &str,
        project: Project,
        data: T,
        issues: Vec<Issue>,
        exit_code: i32,
    ) -> Self {
        Self {
            command: command.to_string(),
            ok: exit_code == 0,
            exit_code,
            project,
            data,
            issues,
        }
    }

    /// Serialize to a single-line JSON document (stdout contract).
    pub fn to_json(&self) -> String {
        // NOT actually infallible: `tan image`'s payload embeds
        // `serde_yaml::Value` copied verbatim off system-manifest.yaml
        // (`raw_passthrough`), and serde_json's map-key serializer rejects a
        // null/NaN/sequence/mapping YAML key. The old `.expect()` turned that
        // into a hard panic (release profile is `panic = "abort"`) — zero
        // bytes on stdout, no envelope, just a Rust abort a JSON consumer
        // can't parse. Every one of the 26 call sites expects a `String` back
        // (not a `Result`), so fall back to a minimal, always-serializable
        // envelope carrying the failure as an issue instead of widening the
        // signature.
        serde_json::to_string(self).unwrap_or_else(|err| {
            let fallback = Envelope::<()> {
                command: self.command.clone(),
                ok: false,
                exit_code: crate::exit::ExitCode::InternalFailure.code(),
                project: self.project.clone(),
                data: (),
                issues: vec![Issue {
                    code: "envelope.serialize-failed".to_string(),
                    severity: "error".to_string(),
                    message: format!("failed to serialize command output: {err}"),
                }],
            };
            serde_json::to_string(&fallback)
                .expect("fallback envelope (no user data) is infallible to serialize")
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    /// A `Vec` key (mirrors a YAML sequence/mapping key surviving into
    /// `serde_yaml::Value` passthrough) makes serde_json's map-key serializer
    /// return `Err` rather than a string. Before the fix this `.expect()`
    /// panicked (process abort, zero bytes on stdout); now `to_json` must
    /// still return a single parseable envelope with `ok:false` and an
    /// `envelope.serialize-failed` issue.
    #[test]
    fn to_json_never_panics_on_unserializable_map_keys() {
        let mut data: BTreeMap<Vec<i32>, i32> = BTreeMap::new();
        data.insert(vec![1, 2], 3);
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let env = Envelope::new("test", project, data, Vec::new(), 0);

        let json = env.to_json();
        let parsed: serde_json::Value =
            serde_json::from_str(&json).expect("to_json must always emit valid JSON");
        assert_eq!(parsed["command"], "test");
        assert_eq!(parsed["ok"], false);
        assert_eq!(parsed["exitCode"], 5);
        assert_eq!(parsed["issues"][0]["code"], "envelope.serialize-failed");
    }

    #[test]
    fn to_json_round_trips_a_normal_payload() {
        let project = Project {
            root: Some("/p".to_string()),
            board_yaml: None,
        };
        let env = Envelope::new("test", project, 42, Vec::new(), 0);
        let json = env.to_json();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["ok"], true);
        assert_eq!(parsed["data"], 42);
    }
}
