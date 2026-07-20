// SPDX-License-Identifier: Apache-2.0
//! Build-plan contract (Wave C) — the **consumed** shape of the SDK's
//! `alp_orchestrate.py --emit build-plan` JSON, locked as ADR 0014
//! (alp-sdk `docs/adr/0014-build-plan-emit-cli-contract.md`, 2026-06-04).
//!
//! The CLI *consumes* this plan; it does **not** compute it. The planner — the
//! fast-moving, vendor-heavy part (partition allocation, sysbuild, TF-M) — stays
//! the SDK's single source of truth (see `docs/BUILD_ORCHESTRATION.md` and
//! `docs/PROPOSAL-alp-build-core.md`). This module is pure: it only models +
//! parses the plan JSON. Materialise / execute / schedule live in the CLI
//! (`alp-cli`).
//!
//! Contract notes (ADR 0014, mirrored here so the types stay honest):
//!   * camelCase keys; `schemaVersion` independent of board.yaml's version.
//!   * Every artefact carries its `contents` (`GeneratedFile`) so materialise is
//!     pure byte-write IO — no content-derivation leaks to the consumer.
//!   * **No `inputHash`** (the consumer computes its own cache key over the plan)
//!     and **no `sequential`** (parallelism policy is the consumer's scheduler).
//!   * One slice per non-`off` core, sorted by `coreId`. A slice the script
//!     cannot build yet carries `command: null` + a `no-command` warning — never
//!     dropped.
//!   * The per-slice `command` shape is **not frozen** (it will grow, e.g.
//!     `--sysbuild`); we never assume a fixed arg layout.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// The build-plan schema version this CLI knows how to consume. A plan with a
/// different `schemaVersion` is rejected rather than silently mis-applied.
pub const BUILD_PLAN_SCHEMA_VERSION: u32 = 1;

/// Per-core build backend. Serialized lowercase to match the emit + `BuildOs`.
///
/// `Unknown` is a deliberate catch-all: this used to be a closed 3-variant
/// enum, so ONE slice naming a backend the CLI didn't know (e.g. a future
/// `linux` A-cluster backend) failed `serde_json::from_str` for the WHOLE
/// plan document, before `executionPolicy.unknownBackend` (below) could ever
/// be consulted per-slice. Keeping the raw string lets the executor apply
/// that policy (skip/fail) and name the offending backend in its message,
/// instead of the plan being rejected outright as "not valid JSON".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Backend {
    /// Zephyr RTOS build (`west`).
    Zephyr,
    /// Yocto/OpenEmbedded build (`bitbake`).
    Yocto,
    /// Bare-metal build (`cmake`).
    Baremetal,
    /// A backend string this CLI does not (yet) recognise, preserved verbatim
    /// so the executor can apply `executionPolicy.unknownBackend` and report
    /// which backend it was.
    Unknown(String),
}

impl Backend {
    /// The lowercase wire string for this backend (matches the serde encoding).
    pub fn as_str(&self) -> &str {
        match self {
            Backend::Zephyr => "zephyr",
            Backend::Yocto => "yocto",
            Backend::Baremetal => "baremetal",
            Backend::Unknown(raw) => raw.as_str(),
        }
    }

    /// True when this backend is not one of the three the executor knows how
    /// to run. Callers apply `ExecutionPolicy::unknown_backend` off this.
    pub fn is_unknown(&self) -> bool {
        matches!(self, Backend::Unknown(_))
    }
}

// Hand-rolled (not derived): Backend must stay a bare lowercase JSON string
// on the wire (ADR 0014), which `#[serde(rename_all = "lowercase")]` only
// gives you for a fieldless enum. `Unknown(String)` carries data, so the
// derive would switch to `{"Unknown":"linux"}` externally-tagged framing;
// these impls keep the flat-string contract for every variant.
impl Serialize for Backend {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for Backend {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = String::deserialize(deserializer)?;
        Ok(match raw.as_str() {
            "zephyr" => Backend::Zephyr,
            "yocto" => Backend::Yocto,
            "baremetal" => Backend::Baremetal,
            _ => Backend::Unknown(raw),
        })
    }
}

/// A file the SDK's planner wants written verbatim (config or shared artefact).
/// `contents` is REQUIRED — the CLI byte-writes it; it never derives content.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GeneratedFile {
    /// Destination path (relative to the build root) the consumer writes to.
    pub path: String,
    /// Verbatim file body to write; never derived by the consumer.
    pub contents: String,
}

/// One concrete tool invocation (`west` / `bitbake` / `cmake`). Its shape is
/// **not frozen** — it comes from the emit and will grow (e.g. `--sysbuild`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolStep {
    /// Executable to run (e.g. `west`, `bitbake`, `cmake`).
    pub tool: String,
    /// Arguments passed to `tool`, in order.
    pub args: Vec<String>,
    /// Working directory the invocation runs in.
    pub cwd: String,
}

impl ToolStep {
    /// `tool arg arg ...` for display.
    pub fn display(&self) -> String {
        if self.args.is_empty() {
            self.tool.clone()
        } else {
            format!("{} {}", self.tool, self.args.join(" "))
        }
    }
}

/// One build slice — a single non-`off` core. Lean by contract (ADR 0014): the
/// command already encodes the board/app, so the consumer needs only what it
/// runs + writes, not the planner's intermediate fields.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BuildSlice {
    /// Identifier of the core this slice builds (e.g. `m55_hp`).
    pub core_id: String,
    /// Build backend driving this slice.
    pub backend: Backend,
    /// Output directory for this slice's build.
    pub build_dir: String,
    /// Per-slice config files to materialise before running `command`.
    #[serde(default)]
    pub config_artefacts: Vec<GeneratedFile>,
    /// `None` when the planner cannot build this core yet (paired with a
    /// `no-command` warning); the slice is reported, never dropped.
    #[serde(default)]
    pub command: Option<ToolStep>,
    /// Environment overrides applied when running `command`.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Env vars to APPEND (os.pathsep-joined) *if not already present* on the
    /// slice subprocess — SDK-owned module / PYTHONPATH paths, so a consumer
    /// that resolves those itself is not silently overridden ("plan wins / CLI
    /// fills gaps", ADR-0020 item 3). A newer field the SDK now emits; absent on
    /// older plans (defaults to empty).
    #[serde(default)]
    pub env_append_path: BTreeMap<String, Vec<String>>,
}

/// What the consumer's executor should do with a degenerate slice: `skip` it
/// (continue the build) or `fail` the whole build. The wire form is lowercase.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PolicyAction {
    /// Carry on — report the slice as skipped, keep the build exit successful.
    Skip,
    /// Abort the build with a failure.
    Fail,
}

/// Per-condition execution policy (ADR-0020): how the executor treats an
/// unknown backend, a build tool missing on the host, or a slice with a null
/// command. Each entry is optional; an absent entry means the executor applies
/// its own built-in default. A newer top-level block the SDK now emits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct ExecutionPolicy {
    /// Action when a slice names a backend the executor does not recognise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unknown_backend: Option<PolicyAction>,
    /// Action when a slice's build tool is not present on the host.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub missing_tool: Option<PolicyAction>,
    /// Action when a slice carries no command (`command: null`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub null_command: Option<PolicyAction>,
}

/// A non-fatal note from the planner (e.g. "core X has no build command").
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PlanWarning {
    /// Machine-readable warning code (e.g. `no-command`).
    pub code: String,
    /// Set when the warning is about a specific core (e.g. `no-command`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub core_id: Option<String>,
    /// Human-readable warning text.
    pub message: String,
}

/// The whole plan — the deserialization target for `--emit build-plan`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BuildPlan {
    /// Plan schema version; must equal `BUILD_PLAN_SCHEMA_VERSION` to be consumed.
    pub schema_version: u32,
    /// Tool/script that emitted the plan (e.g. `scripts/alp_orchestrate.py`).
    #[serde(default)]
    pub generated_by: String,
    /// Path to the source `board.yaml` the plan was derived from.
    pub board_yaml: String,
    /// Board SKU the plan targets.
    pub sku: String,
    /// Root directory for all build output.
    pub build_root: String,
    /// One slice per non-`off` core, sorted by `coreId`.
    pub slices: Vec<BuildSlice>,
    /// Cross-slice generated files (e.g. IPC headers, DTS overlays).
    #[serde(default)]
    pub shared_artefacts: Vec<GeneratedFile>,
    /// Non-fatal planner notes.
    #[serde(default)]
    pub warnings: Vec<PlanWarning>,
    /// How the executor should treat degenerate slices (unknown backend /
    /// missing tool / null command). Absent on older plans — the executor then
    /// applies its own defaults. A newer top-level block the SDK now emits.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_policy: Option<ExecutionPolicy>,
}

impl BuildPlan {
    /// Every generated file the consumer must materialise, in a deterministic
    /// order: shared artefacts first, then each slice's config artefacts in
    /// slice order. Pure — the CLI does the byte-writes. The SDK guarantees
    /// these `contents` match what `west alp-build` would write itself, so
    /// materialising them cannot drift from the on-disk build.
    pub fn all_artefacts(&self) -> Vec<&GeneratedFile> {
        let mut out: Vec<&GeneratedFile> = self.shared_artefacts.iter().collect();
        for slice in &self.slices {
            out.extend(slice.config_artefacts.iter());
        }
        out
    }

    /// True when `build_root` is the one location every post-build consumer
    /// reads: `<project>/build`.
    ///
    /// The native build writes `system-manifest.yaml` under `build_root`, but
    /// `tan flash` / `size` / `image` / `renode` each run as a SEPARATE
    /// invocation with no plan in hand, so they hardcode `<project>/build` as
    /// where the manifest lives. If a plan ever emitted a different
    /// `build_root`, the build would write the manifest somewhere those
    /// consumers never look — leaving them to read a stale manifest still
    /// sitting under `build/` (and, for `flash`, program it onto silicon). So
    /// the executor rejects such a plan loudly rather than build into a location
    /// the rest of the suite cannot find. Every plan the SDK emits today uses
    /// `"build"`; this pins that shared assumption at the one seam that knows
    /// the plan. A nested or dotted form that still normalizes to `build`
    /// (`./build`, `build/.`) is accepted.
    pub fn build_root_is_consumer_default(&self) -> bool {
        use std::path::{Component, Path};
        let p = Path::new(&self.build_root);
        // A leading `..` normalizes to nothing on an empty accumulator, so
        // `../build` would lexically collapse to `build` — reject any escape /
        // rooted form explicitly, then require the remainder to normalize to
        // exactly `build`.
        !p.is_absolute()
            && !p.components().any(|c| matches!(c, Component::ParentDir))
            && crate::path_guard::normalize(p) == Path::new(CONSUMER_BUILD_ROOT)
    }
}

/// The single build-tree directory name every post-build consumer
/// (`flash`/`size`/`image`/`renode`) reads relative to the project root. See
/// [`BuildPlan::build_root_is_consumer_default`].
pub const CONSUMER_BUILD_ROOT: &str = "build";

/// Why a build-plan JSON could not be consumed.
#[derive(Debug, thiserror::Error)]
pub enum BuildPlanError {
    /// The document failed JSON deserialization; holds the parse error text.
    #[error("build plan is not valid JSON: {0}")]
    Json(String),
    /// The plan's `schemaVersion` differs from the version this CLI consumes.
    #[error(
        "unsupported build-plan schemaVersion {found} (this CLI consumes v{supported}); \
         upgrade the CLI or the SDK so the versions match"
    )]
    UnsupportedSchemaVersion { found: u32, supported: u32 },
}

/// Just enough of the plan to read `schemaVersion` without requiring every
/// other v1 field to be present.
#[derive(Deserialize)]
struct SchemaVersionProbe {
    #[serde(rename = "schemaVersion")]
    schema_version: u32,
}

/// Parse + version-guard a build-plan JSON document. Pure: no IO.
pub fn parse_build_plan(json: &str) -> Result<BuildPlan, BuildPlanError> {
    // Check schemaVersion FIRST, on a minimal probe struct, before the full
    // typed deserialize. The old order deserialized straight into `BuildPlan`
    // (every v1 field required, no `#[serde(default)]`), so a genuine v2 plan
    // that renamed/dropped a required field (e.g. `buildRoot`) failed there
    // with "missing field `buildRoot`" and never reached the version check --
    // the user got a generic JSON-parse error instead of the actionable
    // "upgrade the CLI or the SDK" message this guard exists to give. If the
    // probe itself can't find/parse `schemaVersion` (malformed JSON, or a
    // shape where the field is missing/mistyped), fall through to the full
    // parse so its error message stands on its own.
    if let Ok(probe) = serde_json::from_str::<SchemaVersionProbe>(json) {
        if probe.schema_version != BUILD_PLAN_SCHEMA_VERSION {
            return Err(BuildPlanError::UnsupportedSchemaVersion {
                found: probe.schema_version,
                supported: BUILD_PLAN_SCHEMA_VERSION,
            });
        }
    }
    let plan: BuildPlan =
        serde_json::from_str(json).map_err(|e| BuildPlanError::Json(e.to_string()))?;
    if plan.schema_version != BUILD_PLAN_SCHEMA_VERSION {
        return Err(BuildPlanError::UnsupportedSchemaVersion {
            found: plan.schema_version,
            supported: BUILD_PLAN_SCHEMA_VERSION,
        });
    }
    Ok(plan)
}

/// Human-readable, deterministic summary lines for `alp build --plan` (text
/// mode). Pure so it is unit-testable without the CLI.
pub fn summarize_plan(plan: &BuildPlan) -> Vec<String> {
    let mut lines = Vec::new();
    lines.push(format!(
        "build plan (schema v{}) — {}",
        plan.schema_version, plan.sku
    ));
    lines.push(format!("  board.yaml: {}", plan.board_yaml));
    lines.push(format!("  build root: {}", plan.build_root));
    lines.push(format!("  slices ({}):", plan.slices.len()));
    for s in &plan.slices {
        let cmd = s
            .command
            .as_ref()
            .map(ToolStep::display)
            .unwrap_or_else(|| "(no command)".to_string());
        lines.push(format!(
            "    - {} [{}] {}  -> {}",
            s.core_id,
            s.backend.as_str(),
            cmd,
            s.build_dir
        ));
    }
    let shared: Vec<&str> = plan
        .shared_artefacts
        .iter()
        .map(|f| f.path.as_str())
        .collect();
    lines.push(format!(
        "  shared artefacts ({}): {}",
        shared.len(),
        if shared.is_empty() {
            "-".to_string()
        } else {
            shared.join(", ")
        }
    ));
    if plan.warnings.is_empty() {
        lines.push("  warnings: 0".to_string());
    } else {
        lines.push(format!("  warnings ({}):", plan.warnings.len()));
        for w in &plan.warnings {
            match &w.core_id {
                Some(c) => lines.push(format!("    - [{}] {}: {}", w.code, c, w.message)),
                None => lines.push(format!("    - [{}] {}", w.code, w.message)),
            }
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors the ADR 0014 emit shape: lean slices (sorted by coreId), nullable
    // command, `generatedBy`, no `sequential`/`inputHash`, warnings carry coreId.
    const SAMPLE: &str = r#"{
      "schemaVersion": 1,
      "generatedBy": "scripts/alp_orchestrate.py",
      "boardYaml": "/proj/board.yaml",
      "sku": "E1M-AEN701",
      "buildRoot": "build",
      "slices": [
        {
          "coreId": "m55_he",
          "backend": "baremetal",
          "buildDir": "build/m55_he-baremetal",
          "configArtefacts": [{ "path": "build/m55_he-baremetal/cmake-args.txt", "contents": "-DALP_CORE_ID=m55_he\n" }],
          "command": { "tool": "cmake", "args": ["-S", "he_app", "-B", "build/m55_he-baremetal"], "cwd": "build/m55_he-baremetal" },
          "env": { "ALP_SDK_ROOT": "/sdk" }
        },
        {
          "coreId": "m55_hp",
          "backend": "zephyr",
          "buildDir": "build/m55_hp-zephyr",
          "configArtefacts": [{ "path": "build/m55_hp-zephyr/alp.conf", "contents": "CONFIG_GPIO=y\n" }],
          "command": { "tool": "west", "args": ["build", "-b", "alif_e7_dk_rtss_hp", "app"], "cwd": "build/m55_hp-zephyr" },
          "env": { "ALP_SDK_ROOT": "/sdk" }
        }
      ],
      "sharedArtefacts": [
        { "path": "build/generated/alp/system_ipc.h", "contents": "/* ipc */\n" },
        { "path": "build/generated/dts-reservations.dtsi", "contents": "/* res */\n" },
        { "path": "build/generated/dts-partitions.dtsi", "contents": "/* parts */\n" }
      ],
      "warnings": []
    }"#;

    #[test]
    fn parses_a_well_formed_plan() {
        let plan = parse_build_plan(SAMPLE).expect("sample should parse");
        assert_eq!(plan.schema_version, 1);
        assert_eq!(plan.generated_by, "scripts/alp_orchestrate.py");
        assert_eq!(plan.sku, "E1M-AEN701");
        // One slice per core, sorted by coreId (m55_he before m55_hp).
        assert_eq!(
            plan.slices
                .iter()
                .map(|s| s.core_id.as_str())
                .collect::<Vec<_>>(),
            vec!["m55_he", "m55_hp"]
        );
        assert_eq!(plan.slices[0].backend, Backend::Baremetal);
        assert_eq!(plan.slices[1].backend, Backend::Zephyr);
        assert_eq!(
            plan.slices[1].env.get("ALP_SDK_ROOT").map(String::as_str),
            Some("/sdk")
        );
        assert_eq!(plan.shared_artefacts.len(), 3);
        assert!(plan.warnings.is_empty());
    }

    #[test]
    fn round_trips_through_json() {
        let plan = parse_build_plan(SAMPLE).unwrap();
        let json = serde_json::to_string(&plan).unwrap();
        let again = parse_build_plan(&json).unwrap();
        assert_eq!(plan, again);
    }

    #[test]
    fn build_root_consumer_default_accepts_only_build() {
        let with_root = |r: &str| {
            let mut p = parse_build_plan(SAMPLE).unwrap();
            p.build_root = r.to_string();
            p.build_root_is_consumer_default()
        };
        // The one value the whole flash/size/image/renode suite reads, plus
        // dotted forms that normalize to it.
        assert!(with_root("build"));
        assert!(with_root("./build"));
        assert!(with_root("build/."));
        // Anything else would put the manifest where the consumers never look.
        assert!(!with_root("out"));
        assert!(!with_root("build/nested"));
        assert!(!with_root("../build"));
        assert!(!with_root("/abs/build"));
        assert!(!with_root(""));
    }

    #[test]
    fn a_non_build_build_root_still_parses() {
        // Tolerant read / validated act: parsing a plan whose buildRoot isn't
        // `build` succeeds (inspection, `--plan` must still show it); it is the
        // ACTING path (native_build) that refuses it via
        // build_root_is_consumer_default, not the parser.
        let plan =
            parse_build_plan(&SAMPLE.replace(r#""buildRoot": "build""#, r#""buildRoot": "out""#))
                .expect("a non-build buildRoot must still parse");
        assert_eq!(plan.build_root, "out");
        assert!(!plan.build_root_is_consumer_default());
    }

    #[test]
    fn command_display_joins_args() {
        let plan = parse_build_plan(SAMPLE).unwrap();
        let cmd = plan.slices[1]
            .command
            .as_ref()
            .expect("zephyr slice has a command");
        assert_eq!(cmd.display(), "west build -b alif_e7_dk_rtss_hp app");
    }

    #[test]
    fn carries_commandless_slice_with_warning() {
        // A core the planner can't build yet: command is null, paired with a
        // `no-command` warning that names the core. The slice is still present.
        let json = r#"{
          "schemaVersion": 1,
          "boardYaml": "/p/board.yaml",
          "sku": "E1M-X",
          "buildRoot": "build",
          "slices": [
            { "coreId": "m33_sm", "backend": "zephyr", "buildDir": "build/m33_sm-zephyr", "command": null }
          ],
          "warnings": [
            { "code": "no-command", "coreId": "m33_sm", "message": "no build command for core 'm33_sm'" }
          ]
        }"#;
        let plan = parse_build_plan(json).unwrap();
        assert_eq!(plan.slices.len(), 1);
        assert!(plan.slices[0].command.is_none());
        assert_eq!(plan.warnings[0].code, "no-command");
        assert_eq!(plan.warnings[0].core_id.as_deref(), Some("m33_sm"));
        // Defaulted optionals on the lean slice.
        assert!(plan.slices[0].config_artefacts.is_empty());
        assert!(plan.slices[0].env.is_empty());

        let summary = summarize_plan(&plan).join("\n");
        assert!(summary.contains("m33_sm [zephyr] (no command)"));
        assert!(summary.contains("[no-command] m33_sm: no build command"));
    }

    #[test]
    fn all_artefacts_collects_shared_then_per_slice() {
        let plan = parse_build_plan(SAMPLE).unwrap();
        let arts = plan.all_artefacts();
        // 3 shared + 1 config per slice (2 slices) = 5, shared first.
        assert_eq!(arts.len(), 5);
        assert_eq!(arts[0].path, "build/generated/alp/system_ipc.h");
        let paths: Vec<&str> = arts.iter().map(|a| a.path.as_str()).collect();
        assert!(paths.contains(&"build/m55_he-baremetal/cmake-args.txt"));
        assert!(paths.contains(&"build/m55_hp-zephyr/alp.conf"));
    }

    #[test]
    fn parses_execution_policy_and_env_append_path() {
        // The newer ADR-0020 fields the SDK now emits: per-slice envAppendPath
        // and a top-level executionPolicy. Both round-trip and stay intact.
        let json = r#"{
          "schemaVersion": 1,
          "boardYaml": "/p/board.yaml",
          "sku": "E1M-V2N101",
          "buildRoot": "build",
          "slices": [
            { "coreId": "m33_sm", "backend": "zephyr", "buildDir": "build/m33_sm-zephyr",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/m33_sm-zephyr" },
              "env": { "ALP_SDK_ROOT": "/sdk" },
              "envAppendPath": { "PYTHONPATH": ["/sdk/scripts"] } }
          ],
          "executionPolicy": { "unknownBackend": "fail", "missingTool": "skip", "nullCommand": "skip" }
        }"#;
        let plan = parse_build_plan(json).unwrap();
        assert_eq!(
            plan.slices[0].env_append_path["PYTHONPATH"],
            vec!["/sdk/scripts".to_string()]
        );
        let policy = plan.execution_policy.clone().expect("policy present");
        assert_eq!(policy.unknown_backend, Some(PolicyAction::Fail));
        assert_eq!(policy.missing_tool, Some(PolicyAction::Skip));
        assert_eq!(policy.null_command, Some(PolicyAction::Skip));
        // Round-trips with the new fields intact.
        let again = parse_build_plan(&serde_json::to_string(&plan).unwrap()).unwrap();
        assert_eq!(plan, again);
    }

    #[test]
    fn plan_without_new_fields_defaults_them() {
        // Older plans lack both — env_append_path defaults empty, policy is None.
        let plan = parse_build_plan(SAMPLE).unwrap();
        assert!(plan.slices[0].env_append_path.is_empty());
        assert!(plan.execution_policy.is_none());
    }

    #[test]
    fn rejects_unsupported_schema_version() {
        let bumped = SAMPLE.replace("\"schemaVersion\": 1", "\"schemaVersion\": 99");
        match parse_build_plan(&bumped) {
            Err(BuildPlanError::UnsupportedSchemaVersion { found, supported }) => {
                assert_eq!(found, 99);
                assert_eq!(supported, BUILD_PLAN_SCHEMA_VERSION);
            }
            other => panic!("expected schema-version error, got {other:?}"),
        }
    }

    #[test]
    fn schema_version_mismatch_detected_before_structural_fields() {
        // Regression: the version guard used to run AFTER the full struct
        // deserialize. A v2 plan that renamed/dropped a v1-required field
        // (buildRoot, missing here) used to fail with a JSON "missing field"
        // error instead of the actionable UnsupportedSchemaVersion message.
        let json = r#"{
          "schemaVersion": 2,
          "boardYaml": "/p/board.yaml",
          "sku": "E1M-X",
          "slices": []
        }"#;
        match parse_build_plan(json) {
            Err(BuildPlanError::UnsupportedSchemaVersion { found, supported }) => {
                assert_eq!(found, 2);
                assert_eq!(supported, BUILD_PLAN_SCHEMA_VERSION);
            }
            other => panic!("expected schema-version error, got {other:?}"),
        }
    }

    #[test]
    fn unknown_backend_does_not_reject_whole_plan() {
        // Regression: Backend used to be a closed 3-variant enum with no
        // catch-all, so ONE slice naming a not-yet-known backend failed
        // serde_json::from_str for the WHOLE plan -- executionPolicy's
        // unknownBackend could never be applied because the plan never
        // parsed. Unknown(raw) keeps the document parseable per-slice.
        let json = SAMPLE.replacen("\"backend\": \"baremetal\"", "\"backend\": \"linux\"", 1);
        let plan = parse_build_plan(&json).expect("unknown backend must not fail the whole plan");
        assert_eq!(
            plan.slices[0].backend,
            Backend::Unknown("linux".to_string())
        );
        assert!(plan.slices[0].backend.is_unknown());
        assert!(!plan.slices[1].backend.is_unknown());
        // Serializes back to the raw string (flat wire contract), not the
        // externally-tagged `{"Unknown":"linux"}` a plain derive would give.
        let round_tripped = serde_json::to_string(&plan).unwrap();
        assert!(round_tripped.contains("\"backend\":\"linux\""));
    }

    #[test]
    fn rejects_malformed_json() {
        assert!(matches!(
            parse_build_plan("{not json"),
            Err(BuildPlanError::Json(_))
        ));
    }

    #[test]
    fn summary_lists_each_slice() {
        let plan = parse_build_plan(SAMPLE).unwrap();
        let joined = summarize_plan(&plan).join("\n");
        assert!(joined.contains("E1M-AEN701"));
        assert!(joined.contains("m55_he [baremetal] cmake -S he_app -B build/m55_he-baremetal"));
        assert!(joined.contains("m55_hp [zephyr] west build -b alif_e7_dk_rtss_hp app"));
        assert!(joined.contains("shared artefacts (3):"));
        assert!(joined.contains("warnings: 0"));
    }
}
