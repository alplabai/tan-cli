// SPDX-License-Identifier: Apache-2.0
//! Machine-readable result envelope — byte-compatible with the TypeScript
//! CLI's `CliEnvelope`. JSON mode writes exactly one of these to stdout.

use serde::Serialize;
use std::path::Path;
use tan_core::{ProjectContext, SdkSourceTier, debug::DebugWorkspaceContext};

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

impl Project {
    /// The `project` block for a resolved [`ProjectContext`] — the one place
    /// every command builds it (tan-cli#236).
    ///
    /// `board_yaml` is `Some` only when a file is really at the resolved path.
    /// The doc above has always said "if found"; before this, twenty call sites
    /// each cloned `context.board_yaml_path` straight through, so the field
    /// named `<root>/board.yaml` whether or not anything was there and a
    /// consumer that opened it got ENOENT. `null` is not a new value here —
    /// it is what the field already carries wherever resolution produces
    /// nothing at all.
    ///
    /// Deliberately NOT done by making
    /// [`tan_core::project::resolve_project_context`] return `None`:
    /// `ProjectContext::board_yaml_path` is the path a command ACTS on, and
    /// several need it precisely when the file is absent — `doctor`'s
    /// `read_board_model`, `validate`/`diff`'s "board.yaml is not valid YAML"
    /// path, and `tan-core::debug::create_debug_workspace_context`, which
    /// mirrors the TypeScript side by carrying the path and a SEPARATE
    /// `board_yaml_exists` flag. Emptying it there would strip the path out of
    /// every "no board.yaml at `<path>`" message. Reporting is the seam; the
    /// resolver is not.
    ///
    /// `root` is passed through untouched — tan-cli#236 rules a
    /// directory-is-not-a-project question explicitly out of scope.
    pub fn from_context(context: &ProjectContext) -> Self {
        Self::from_context_with(context, |p| Path::new(p).exists())
    }

    /// The `project` block for a [`DebugWorkspaceContext`] — `doctor`,
    /// `inspect` and `support-bundle` hold one of these rather than a bare
    /// [`ProjectContext`].
    ///
    /// Reuses the context's own `board_yaml_exists`, which
    /// [`tan_core::debug::create_debug_workspace_context`] already computed
    /// through its injected probe, instead of stat-ing the path a second time.
    /// One answer per run: a second probe could disagree with the flag the rest
    /// of the same envelope was built from.
    pub fn from_debug_context(context: &DebugWorkspaceContext) -> Self {
        Project {
            root: context.workspace_root.clone(),
            board_yaml: context
                .board_yaml_path
                .clone()
                .filter(|_| context.board_yaml_exists),
        }
    }

    /// [`Project::from_context`] with the existence probe injected, mirroring
    /// [`tan_core::project::resolve_project_context`]'s own shape so the rule
    /// is unit-testable without touching a filesystem.
    pub fn from_context_with(context: &ProjectContext, exists: impl Fn(&str) -> bool) -> Self {
        Project {
            root: context.workspace_root.clone(),
            board_yaml: context.board_yaml_path.clone().filter(|path| exists(path)),
        }
    }
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

    fn context_with_board_yaml(board_yaml_path: Option<&str>) -> ProjectContext {
        ProjectContext {
            workspace_root: Some("/work/proj".to_string()),
            sdk_root: Some("/work/alp-sdk".to_string()),
            board_yaml_path: board_yaml_path.map(str::to_string),
            west_cwd: Some("/work/proj".to_string()),
            python_binary: "python3".to_string(),
        }
    }

    /// tan-cli#236: the resolver joins root + configured name unconditionally,
    /// so this path is `Some` whether or not a file is there. The envelope must
    /// not repeat the claim — a consumer that opens it gets ENOENT.
    #[test]
    fn board_yaml_is_null_when_nothing_is_at_the_resolved_path() {
        let context = context_with_board_yaml(Some("/work/proj/board.yaml"));
        let project = Project::from_context_with(&context, |_| false);
        assert_eq!(project.board_yaml, None);
    }

    /// The other direction (tan-cli#170): a real `board.yaml` must be reported,
    /// not dropped. Together with the test above, this is what stops the fix
    /// being "always null", which would pass the #236 case alone.
    #[test]
    fn board_yaml_is_reported_when_the_file_really_exists() {
        let context = context_with_board_yaml(Some("/work/proj/board.yaml"));
        let project = Project::from_context_with(&context, |p| p == "/work/proj/board.yaml");
        assert_eq!(project.board_yaml.as_deref(), Some("/work/proj/board.yaml"));
    }

    /// The probe must be asked about the path the envelope is about to report,
    /// not about the root or some other field.
    #[test]
    fn the_existence_probe_is_asked_about_the_board_yaml_path_itself() {
        let context = context_with_board_yaml(Some("/work/proj/board.yaml"));
        let asked = std::cell::RefCell::new(Vec::new());
        let project = Project::from_context_with(&context, |p| {
            asked.borrow_mut().push(p.to_string());
            true
        });
        assert_eq!(
            asked.into_inner(),
            vec!["/work/proj/board.yaml".to_string()]
        );
        assert!(project.board_yaml.is_some());
    }

    /// tan-cli#236 rules `project.root` explicitly out of scope: a directory
    /// that is not a project still legitimately reports where the run stood.
    /// A fix that emptied `root` alongside `board_yaml` would be a wider,
    /// unasked-for wire change.
    #[test]
    fn root_is_passed_through_whatever_the_board_yaml_verdict() {
        let context = context_with_board_yaml(Some("/work/proj/board.yaml"));
        for exists in [true, false] {
            let project = Project::from_context_with(&context, |_| exists);
            assert_eq!(project.root.as_deref(), Some("/work/proj"), "{exists}");
        }
    }

    /// A context that resolved no path at all stays null without the probe
    /// inventing one.
    #[test]
    fn an_unresolved_board_yaml_path_stays_null() {
        let context = context_with_board_yaml(None);
        let project = Project::from_context_with(&context, |_| true);
        assert_eq!(project.board_yaml, None);
    }

    /// `doctor`/`inspect`/`support-bundle` hold a `DebugWorkspaceContext`,
    /// which already carries the answer. It must be trusted in BOTH directions
    /// rather than re-probed — two probes in one run can disagree.
    #[test]
    fn the_debug_context_variant_trusts_its_own_exists_flag() {
        let base = tan_core::debug::create_debug_workspace_context(
            &context_with_board_yaml(Some("/work/proj/board.yaml")),
            "1970-01-01T00:00:00Z".to_string(),
            |_| true,
            true,
            crate::commands::doctor::standalone_debugger_extensions(),
        );
        assert_eq!(
            Project::from_debug_context(&base).board_yaml.as_deref(),
            Some("/work/proj/board.yaml")
        );

        let missing = tan_core::debug::create_debug_workspace_context(
            &context_with_board_yaml(Some("/work/proj/board.yaml")),
            "1970-01-01T00:00:00Z".to_string(),
            |_| false,
            true,
            crate::commands::doctor::standalone_debugger_extensions(),
        );
        assert_eq!(Project::from_debug_context(&missing).board_yaml, None);
        assert_eq!(
            Project::from_debug_context(&missing).root.as_deref(),
            Some("/work/proj")
        );
    }

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
