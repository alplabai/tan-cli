// SPDX-License-Identifier: Apache-2.0
//! The `tan` command surface — DRAFT STUBS.
//!
//! Real implementations land in Phase 2 (alp-sdk ADR-0020), reconciled with
//! alp-sdk-vscode's `cli-rs`. This file exists to pin the surface: the whole
//! user-facing command set moves here (build / flash / image / size / renode
//! / clean / sdk / doctor / validate), because under B alp-sdk keeps zero
//! user commands.

use anyhow::bail;
use clap::Args;

#[derive(Args)]
pub struct BuildArgs {
    /// Path to the board.yaml (defaults to ./board.yaml).
    #[arg(long)]
    pub board_yaml: Option<String>,
    /// Build only this core.
    #[arg(long)]
    pub core: Option<String>,
}

/// `tan build` — the sole executor entry point (draft).
///
/// Intended flow (ADR-0020):
/// 1. fetch the plan: `python -m alp_orchestrate --input <board.yaml>
///    [--core X] --emit build-plan` (resolve the SDK's venv interpreter by
///    explicit path — never PATH);
/// 2. `BuildPlan::parse` — reject an unsupported schemaVersion / unknown
///    required key (version-skew guard) rather than hand-porting;
/// 3. materialise each slice's `configArtefacts` + the plan's shared
///    artefacts (confined under `buildRoot`; a plan is trusted input in the
///    same domain as board.yaml);
/// 4. run each slice's `command` with `env` (set verbatim) + `envAppendPath`
///    (append-if-absent), applying the plan's `executionPolicy`
///    (missing-tool = skip, unknown-backend = fail, null-command = skip);
/// 5. write `system-manifest.yaml` + `.alp-build-state.json` (consumed by
///    `tan flash/size/image`).
pub fn build(_args: BuildArgs) -> anyhow::Result<()> {
    bail!("tan build: not yet implemented (draft skeleton — ADR-0020 Phase 2)")
}

/// Placeholder for every not-yet-ported command.
pub fn todo(name: &str) -> anyhow::Result<()> {
    bail!("tan {name}: not yet implemented (draft skeleton — ADR-0020 Phase 2)")
}
