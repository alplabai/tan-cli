// SPDX-License-Identifier: Apache-2.0
//! Command dispatch root for the `tan` CLI: declares the per-command sub-modules
//! and the shared `CommandRun` result every command's `run` returns.
//!
//! DRAFT SEED (ADR-0020 Phase 2): only the SDK-interacting commands are ported
//! here. The rest of `cli-rs`'s surface (init/scaffold/examples/completion/diff/
//! presets/pinmux/explain/inspect/trace/debug-config/support-bundle) is dropped
//! from this snapshot and lands in the history-preserving extraction later.
use crate::exit::ExitCode;

/// Uniform outcome of running a command: exit code plus the human/JSON output split.
pub struct CommandRun {
    /// Process exit code conveying success or the failure class.
    pub exit: ExitCode,
    /// Human text (stderr-bound); empty in JSON mode.
    pub text: Vec<String>,
    /// JSON document (stdout-bound) in JSON mode.
    pub json: Option<String>,
}

/// `tan bootstrap` — per-OS toolchain/SDK bootstrap orchestration.
pub mod bootstrap;
/// `tan build` (and `image`/`flash`/`clean`/`renode`) — `west` build workflows.
pub mod build;
/// `tan doctor` — diagnose debug readiness (`--build` runs the build-readiness preflight).
pub mod doctor;
/// `tan generate` — Zephyr conf / DTS overlay / CMake args / Yocto conf codegen.
pub mod generate;
/// `tan sdk` — SDK release listing/management.
pub mod sdk;
/// `tan validate` — schema-aware `board.yaml` validation.
pub mod validate;
