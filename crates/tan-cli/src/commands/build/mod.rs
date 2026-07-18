// SPDX-License-Identifier: Apache-2.0
//! `tan build` / `image` / `flash` / `clean` / `renode` — the build workflow.
//!
//! Each is the single user-facing entry that **hides `west`**: `tan build` runs
//! `west alp-build`, `tan flash` runs `west alp-flash`, etc. The per-core /
//! per-platform routing (Zephyr→`west build`, Yocto→`bitbake`, baremetal→CMake +
//! vendor toolchain) stays in the SDK's orchestrator (`alp_orchestrate.py`); the
//! CLI never re-decides the backend. Args after the subcommand are forwarded
//! verbatim to the `west alp-*` command.
//!
//! Text mode inherits stdio so the build streams live in the caller's terminal;
//! JSON mode captures + emits a single envelope.

mod execute;
mod materialise;
mod native;
mod plan_modes;
mod preflight;
mod workspace;

use serde::Serialize;

use super::CommandRun;
use crate::cli::{BuildArgs, GlobalArgs};

use native::native_build;
use plan_modes::{manifest_command, plan_command};

// `run` is the legacy `west`-delegating entry (`image`/`flash`/`clean`/`renode`)
// and `probe_build_preflight` is shared with `tan doctor --build`; both stay
// importable at `crate::commands::build::*` for `main.rs` / `doctor.rs`.
pub(crate) use preflight::probe_build_preflight;
pub use workspace::run;

/// Envelope `data` for the `west`-delegating path: the `alp-*` command run, its
/// cwd, and the forwarded args.
#[derive(Serialize)]
struct BuildData {
    /// Envelope `data` schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// The `west alp-*` command invoked (e.g. `alp-build`).
    #[serde(rename = "westCommand")]
    west_command: String,
    /// Working directory the `west` command ran in.
    #[serde(rename = "westCwd")]
    west_cwd: String,
    /// Passthrough args forwarded verbatim after the subcommand.
    args: Vec<String>,
}

/// `tan build` entry. `--native` runs the CLI-native build (consume plan →
/// materialise → execute); `--plan` / `--materialise` consume + show/write the
/// plan; otherwise delegate to `west alp-build` (the Wave A2 behavior).
pub fn run_build(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
    if args.manifest || args.manifest_from.is_some() {
        manifest_command(g, args)
    } else if args.west {
        // Legacy escape hatch: delegate to `west alp-build` (needs alp-sdk as the
        // west manifest topdir). The default build no longer requires that.
        run(g, "build", &args.args)
    } else if args.plan || args.plan_from.is_some() || args.materialise {
        plan_command(g, args)
    } else {
        // Default (and `--native`): consume the SDK build-plan and run each slice's
        // command directly, so no `west alp-build` extension command is needed.
        native_build(g, args)
    }
}
