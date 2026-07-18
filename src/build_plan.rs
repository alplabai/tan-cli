// SPDX-License-Identifier: Apache-2.0
//! The alp-sdk build-plan contract, consumer side.
//!
//! Mirrors `metadata/schemas/build-plan-v1.schema.json` in alp-sdk. The
//! plan is the SOLE input seam into the executor (ADR-0020): `tan` never
//! reads SDK internals, only this JSON. DRAFT — models the load-bearing
//! subset. Unknown *required-for-execution* keys must be rejected (not
//! ignored), so an older `tan` fails loudly rather than silently
//! hand-porting the value it doesn't understand — that is the #843
//! version-skew guard.

use serde::Deserialize;
use std::collections::BTreeMap;

/// The `schemaVersion` this `tan` build understands. A plan declaring a
/// different version must be rejected, never fallen back on.
pub const SUPPORTED_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BuildPlan {
    pub schema_version: u32,
    pub sku: String,
    pub build_root: String,
    pub slices: Vec<Slice>,
    #[serde(default)]
    pub warnings: Vec<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Slice {
    pub core_id: String,
    pub backend: String,
    pub build_dir: String,
    /// `None` for a slice the planner cannot build yet (carried with a
    /// warning, never dropped).
    pub command: Option<Command>,
    /// Env vars to SET verbatim on the slice subprocess.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Env vars to APPEND (os.pathsep) *if not already present* — SDK-owned
    /// module / PYTHONPATH paths the consumer adds without overriding its
    /// own resolved values ("plan wins / CLI fills gaps"). ADR-0020 item 3.
    #[serde(default)]
    pub env_append_path: BTreeMap<String, Vec<String>>,
}

#[derive(Debug, Deserialize)]
pub struct Command {
    pub tool: String,
    pub args: Vec<String>,
    pub cwd: String,
}

impl BuildPlan {
    /// Parse + version-skew guard: reject a plan we cannot safely execute
    /// instead of falling back to hand-ported behaviour (the #843 fix).
    pub fn parse(json: &str) -> anyhow::Result<Self> {
        let plan: BuildPlan = serde_json::from_str(json)?;
        if plan.schema_version != SUPPORTED_SCHEMA_VERSION {
            anyhow::bail!(
                "build-plan schemaVersion {} unsupported (this tan understands {})",
                plan.schema_version,
                SUPPORTED_SCHEMA_VERSION
            );
        }
        Ok(plan)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_minimal_plan_with_env_append_path() {
        let json = r#"{
            "schemaVersion": 1,
            "sku": "E1M-V2N101",
            "buildRoot": "build",
            "slices": [{
                "coreId": "m33_sm",
                "backend": "zephyr",
                "buildDir": "build/m33_sm-zephyr",
                "command": {"tool": "west", "args": ["build"], "cwd": "build/m33_sm-zephyr"},
                "env": {"ALP_SDK_ROOT": "/sdk"},
                "envAppendPath": {"PYTHONPATH": ["/sdk/scripts"]}
            }],
            "warnings": []
        }"#;
        let plan = BuildPlan::parse(json).expect("valid plan");
        assert_eq!(plan.slices.len(), 1);
        let s = &plan.slices[0];
        assert_eq!(s.env["ALP_SDK_ROOT"], "/sdk");
        assert_eq!(s.env_append_path["PYTHONPATH"], vec!["/sdk/scripts"]);
    }

    #[test]
    fn rejects_an_unsupported_schema_version() {
        let json = r#"{"schemaVersion": 2, "sku": "x", "buildRoot": "b", "slices": []}"#;
        assert!(BuildPlan::parse(json).is_err());
    }
}
