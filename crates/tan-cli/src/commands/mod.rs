// SPDX-License-Identifier: Apache-2.0
//! Command dispatch root for the `tan` CLI: declares the per-command sub-modules
//! and the shared `CommandRun` result every command's `run` returns.
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
/// `tan build` (and `image`/`flash`/`renode`) — `west` build workflows.
pub mod build;
/// `tan clean` — natively remove the per-project build dir + orchestrator cache.
pub mod clean;
/// `tan completion` — shell completion script generation.
pub mod completion;
/// `tan debug-config` — debug/launch configuration drafting.
pub mod debug_config;
/// `tan diff` — show how `board.yaml` normalization changes the effective config.
pub mod diff;
/// `tan doctor` — diagnose debug readiness (`--build` runs the build-readiness preflight).
pub mod doctor;
/// `tan examples` — list the SDK's ready-made example projects (for `init --from-example`).
pub mod examples;
/// `tan explain` — explain a project/module template or a generation target.
pub mod explain;
/// `tan flash` — natively program every slice + helper MCU from the system manifest.
pub mod flash;
/// `tan generate` — Zephyr conf / DTS overlay / CMake args / Yocto conf codegen.
pub mod generate;
/// `tan image` — assemble a flashable-image bundle from the system manifest (native).
pub mod image;
/// `tan init` — new-project scaffolding (including heterogeneous cores).
pub mod init;
/// `tan inspect` — inspect resolved project/debug context values.
pub mod inspect;
/// `tan kconfig` — board-scoped Kconfig symbol menu for one core (the vscode
/// `prj.conf` LSP's live feed); wraps the SDK's `--emit kconfig`.
pub mod kconfig;
/// `tan model` envelope-wrapping helpers for the `alp_cli model` forward
/// (argv/exit/wrap; the spawn/IO wiring lands in a follow-up).
pub mod model;
/// `tan pinmux` — the E1M pinmux capability table (E1M pad → silicon function) for a SoM family.
pub mod pinmux;
/// `tan presets` — list SDK presets (SKUs/SoMs) + built-in catalogue defaults.
pub mod presets;
/// `tan renode` — boot the manifest's single Zephyr slice in headless Renode (native).
pub mod renode;
/// `tan run` — build, then execute the native_sim binary (host) or flash (hardware).
pub mod run;
/// `tan scaffold` — scaffold a module into an existing project.
pub mod scaffold;
/// `tan sdk` — SDK release listing/management.
pub mod sdk;
/// `tan model`/`monitor`/`new-som`/`faultdecode` — forwarders to the SDK `alp` CLI.
pub mod sdk_cli;
/// `tan size` — per-slice FLASH/RAM footprint vs the SoM memory budget (native).
pub mod size;
/// `tan support-bundle` — collect a diagnostics support bundle.
pub mod support_bundle;
/// `tan trace` — trace the generation decisions a build would make.
pub mod trace;
/// `tan validate` — schema-aware `board.yaml` validation.
pub mod validate;
