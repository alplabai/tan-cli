// SPDX-License-Identifier: Apache-2.0
//! Machine-readable result envelope — byte-compatible with the TypeScript
//! CLI's `CliEnvelope`. JSON mode writes exactly one of these to stdout.

use serde::Serialize;
use tan_core::SdkSourceTier;

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

/// The alp-sdk root a command actually resolved and used, plus which
/// precedence tier produced it. Populated from [`crate::sdk_report`] — a
/// value RECORDED at the moment the resolver that built `project` (or a
/// sibling resolver the same command also called) actually resolved one,
/// never from a fresh resolution (see that module for why).
#[derive(Debug, Clone, Serialize)]
pub struct SdkInfo {
    /// Absolute path to the alp-sdk checkout the command resolved and used.
    pub root: String,
    /// Which precedence tier produced it.
    #[serde(rename = "sourceTier")]
    pub source_tier: SdkSourceTier,
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
    /// The SDK root + tier the command actually resolved and used, when one
    /// did. Absent entirely (not `null`) when nothing resolved — see
    /// [`crate::sdk_report`].
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sdk: Option<SdkInfo>,
    /// Command-specific result payload.
    pub data: T,
    /// Diagnostics emitted by the command.
    pub issues: Vec<Issue>,
}

impl<T: Serialize> Envelope<T> {
    /// Build an envelope; `ok` is derived from `exit_code == 0`. `sdk` is
    /// populated from whatever [`crate::sdk_report`] recorded during this
    /// command's own resolution — not a fresh lookup.
    pub fn new(
        command: &str,
        project: Project,
        data: T,
        issues: Vec<Issue>,
        exit_code: i32,
    ) -> Self {
        let sdk =
            crate::sdk_report::take().map(|(root, source_tier)| SdkInfo { root, source_tier });
        Self {
            command: command.to_string(),
            ok: exit_code == 0,
            exit_code,
            project,
            sdk,
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
                sdk: self.sdk.clone(),
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
        crate::sdk_report::reset();
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
        crate::sdk_report::reset();
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

    /// Nothing recorded by `crate::sdk_report` before `Envelope::new` -> the
    /// `sdk` key must be ABSENT from the emitted JSON entirely, not `null`
    /// (that's what keeps the existing contract goldens byte-identical).
    #[test]
    fn sdk_key_is_absent_when_nothing_was_recorded() {
        crate::sdk_report::reset();
        let project = Project {
            root: Some("/p".to_string()),
            board_yaml: None,
        };
        let env = Envelope::new("test", project, 1, Vec::new(), 0);
        let json = env.to_json();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(
            !parsed.as_object().unwrap().contains_key("sdk"),
            "sdk key must be absent, not present as null: {json}"
        );
    }

    /// Whatever `crate::sdk_report` has recorded at the moment `Envelope::new`
    /// runs is exactly what ends up in the `sdk` key — this is the whole
    /// contract the recorder exists to serve. Also pins the member set: no
    /// contract golden exercises this (none of the 11 fixtures resolves an
    /// SDK, so none ever emits the `sdk` key at all), so a member silently
    /// added or renamed inside `sdk` would otherwise ship unnoticed.
    #[test]
    fn sdk_key_reflects_whatever_was_recorded() {
        crate::sdk_report::reset();
        crate::sdk_report::record("/resolved/sdk", SdkSourceTier::Discovery);
        let project = Project {
            root: Some("/p".to_string()),
            board_yaml: None,
        };
        let env = Envelope::new("test", project, 1, Vec::new(), 0);
        let json = env.to_json();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["sdk"]["root"], "/resolved/sdk");
        assert_eq!(parsed["sdk"]["sourceTier"], "discovery");
        let keys: std::collections::BTreeSet<&str> = parsed["sdk"]
            .as_object()
            .expect("sdk must be a JSON object")
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            ["root", "sourceTier"].into_iter().collect(),
            "sdk member set must be exactly {{root, sourceTier}}: {json}"
        );
    }
}
