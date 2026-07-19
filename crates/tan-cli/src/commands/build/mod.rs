// SPDX-License-Identifier: Apache-2.0
//! `tan build` — the build workflow entry.
//!
//! `tan build` is native now: it consumes the SDK's `--emit build-plan`,
//! materialises the per-slice files, and runs each slice's command directly
//! (`native_build`) — no `west alp-build` extension command involved. `tan flash`
//! and `tan renode` are likewise their own native commands (`commands::flash` /
//! `commands::renode`), as is `tan image` (`commands::image`). The per-core /
//! per-platform routing (Zephyr→`west build`, Yocto→`bitbake`, baremetal→CMake +
//! vendor toolchain) stays in the SDK's orchestrator (`alp_orchestrate.py`); the
//! CLI never re-decides the backend.
//!
//! `run` (aka `commands::build::run`) is the remaining `west`-delegating entry —
//! now used ONLY by `Command::{Migrate,Lock,Quality}` (→ `west alp-migrate` /
//! `alp-lock` / `alp-quality`, which survive ADR-0020 Phase 4). `west alp-build`
//! itself is deleted under Phase 4, so `tan build` no longer routes through `run`.
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

// `run` is the `west`-delegating entry (now only `migrate`/`lock`/`quality`)
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

/// `tan build` entry. `--manifest` shows the system manifest; `--plan` /
/// `--materialise` consume + show/write the plan; otherwise (and with `--native`)
/// run the CLI-native build: consume the plan, materialise its files, then run
/// each slice's command directly, so no `west alp-build` extension command is
/// needed.
pub fn run_build(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
    if args.manifest || args.manifest_from.is_some() {
        manifest_command(g, args)
    } else if args.plan || args.plan_from.is_some() || args.materialise {
        plan_command(g, args)
    } else {
        native_build(g, args)
    }
}
