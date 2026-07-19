// SPDX-License-Identifier: Apache-2.0
//! `alp trace`: the per-artifact generation decisions recorded during a trace.

use serde::Serialize;

/// Result of a single generation decision in an `alp trace`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DebugTraceOutcome {
    /// Output was planned but not written.
    Planned,
    /// Output was written to disk.
    Written,
    /// Generation failed.
    Failed,
}

/// One generation decision recorded by `alp trace`.
#[derive(Debug, Clone, Serialize)]
pub struct DebugGenerationTraceDecision {
    /// Identifier of the artifact (e.g. `zephyr-conf`).
    pub key: String,
    /// What happened to the artifact.
    pub outcome: DebugTraceOutcome,
    /// Target path, when an output path applies.
    #[serde(rename = "outputPath", skip_serializing_if = "Option::is_none")]
    pub output_path: Option<String>,
    /// Human-readable explanation.
    pub detail: String,
}
