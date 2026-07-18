// SPDX-License-Identifier: Apache-2.0
//! Command-line surface (clap derive). Global flags mirror CLI.md §3.1.
//!
//! DRAFT SEED (ADR-0020 Phase 2): only the SDK-interacting commands are wired
//! here (validate / generate / doctor / sdk / bootstrap / build + the
//! image/flash/clean/renode west wrappers). The rest of `cli-rs`'s surface is
//! dropped from this snapshot.

use clap::{Args, Parser, Subcommand, ValueEnum};

/// Top-level parsed CLI: global flags plus the selected subcommand.
#[derive(Debug, Parser)]
#[command(
    name = "tan",
    version,
    about = "tan CLI — board configuration, generation, and project tooling.",
    propagate_version = true
)]
pub struct Cli {
    #[command(flatten)]
    pub global: GlobalArgs,

    #[command(subcommand)]
    pub command: Command,
}

/// Flags shared by every subcommand (project/board resolution, output format, mode toggles).
#[derive(Debug, Clone, Args)]
pub struct GlobalArgs {
    /// Project root (defaults to current directory).
    #[arg(long, global = true, value_name = "PATH")]
    pub project: Option<String>,

    /// Explicit board.yaml path (overrides project resolution).
    #[arg(long = "board-yaml", global = true, value_name = "PATH")]
    pub board_yaml: Option<String>,

    /// alp-sdk checkout root.
    #[arg(long = "sdk-root", global = true, value_name = "PATH")]
    pub sdk_root: Option<String>,

    /// Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).
    #[arg(long, global = true, value_name = "EMIT")]
    pub target: Option<String>,

    /// Run command against all relevant targets.
    #[arg(long, global = true)]
    pub all: bool,

    /// Output format.
    #[arg(long, global = true, value_enum, default_value_t = Format::Text)]
    pub format: Format,

    /// Emit additional diagnostic detail.
    #[arg(long, global = true)]
    pub verbose: bool,

    /// Suppress non-essential output.
    #[arg(long, global = true)]
    pub quiet: bool,

    /// Disable ANSI color in text output.
    #[arg(long = "no-color", global = true)]
    pub no_color: bool,

    /// Never prompt; fail instead of asking for input.
    #[arg(long = "non-interactive", global = true)]
    pub non_interactive: bool,

    /// CI mode: implies non-interactive and disables color.
    #[arg(long, global = true)]
    pub ci: bool,
}

impl GlobalArgs {
    /// True when `--format json` was selected.
    pub fn is_json(&self) -> bool {
        matches!(self.format, Format::Json)
    }
}

/// Output format selector for `--format`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Format {
    /// Human-readable text (default).
    Text,
    /// Machine-readable JSON envelope.
    Json,
}

/// The subcommand to dispatch; each variant maps to a command module.
#[derive(Debug, Subcommand)]
pub enum Command {
    /// Validate schema and semantic rules for the active project.
    Validate(ValidateArgs),
    /// Generate build artifacts from board.yaml (alp.conf, overlay, args, yocto).
    Generate,
    /// Diagnose debug readiness for a target/server combination.
    Doctor(DoctorArgs),
    /// Manage local SDK installs (list, install, current, switch).
    Sdk(SdkArgs),
    /// Set up the SDK build environment (west + Zephyr workspace + Python deps).
    Bootstrap(BootstrapArgs),
    /// Build the project. `--plan` consumes the SDK's emitted build plan;
    /// otherwise fans board.yaml into per-core slices via `west alp-build`.
    Build(BuildArgs),
    /// Assemble a flashable image (`west alp-image`).
    Image(WestForwardArgs),
    /// Flash the assembled image to the device (`west alp-flash`).
    Flash(WestForwardArgs),
    /// Remove build dirs + orchestrator cache (`west alp-clean`).
    Clean(WestForwardArgs),
    /// Boot the system manifest in Renode (`west alp-renode`).
    Renode(WestForwardArgs),
}

/// Args for commands that forward verbatim to a `west alp-*` subcommand (`image`/`flash`/`clean`/`renode`).
#[derive(Debug, Args)]
pub struct WestForwardArgs {
    /// Arguments forwarded verbatim to the underlying `west alp-*` command
    /// (e.g. app path, `--core <id>`, `--sequential`, `-b <board>`).
    #[arg(
        trailing_var_arg = true,
        allow_hyphen_values = true,
        value_name = "ARGS"
    )]
    pub args: Vec<String>,
}

/// Args for `build`: plan/manifest inspection, materialisation, native build, and forwarded `west alp-build` args.
#[derive(Debug, Args)]
pub struct BuildArgs {
    /// Show the build plan (consumed from the SDK's `--emit build-plan`) and
    /// exit without building.
    #[arg(long)]
    pub plan: bool,
    /// Read the build plan from a JSON file instead of invoking the SDK. Implies
    /// `--plan`. Use this to consume `alp_orchestrate.py --emit build-plan`
    /// output (Wave C; the live emit is pending on the SDK side).
    #[arg(long = "plan-from", value_name = "FILE")]
    pub plan_from: Option<String>,
    /// Materialise the plan: write its generated files (shared artefacts +
    /// per-slice config) to disk under the build root, instead of just showing
    /// the plan. Requires a plan source (`--plan` / `--plan-from`).
    #[arg(long)]
    pub materialise: bool,
    /// Build natively: consume the plan, materialise its files, then run each
    /// slice's command (`west` / `bitbake` / `cmake`) sequentially — instead of
    /// delegating to `west alp-build`.
    #[arg(long)]
    pub native: bool,
    /// Show the system manifest — the post-build IDE/tool contract
    /// (`build/system-manifest.yaml`): per-core slices + ipc + helper MCUs.
    /// Without `--manifest-from`, asks the SDK for the projection
    /// (`alp_orchestrate.py --emit system-manifest`).
    #[arg(long)]
    pub manifest: bool,
    /// Read the system manifest from a YAML file instead of invoking the SDK
    /// (e.g. the `build/system-manifest.yaml` a build already wrote). Implies
    /// `--manifest`.
    #[arg(long = "manifest-from", value_name = "FILE")]
    pub manifest_from: Option<String>,
    /// Legacy path: delegate to the SDK's `west alp-build` extension instead of
    /// the default plan-driven native build. Requires a workspace where alp-sdk
    /// is the west manifest topdir; the default build no longer needs that.
    #[arg(long)]
    pub west: bool,
    /// Arguments forwarded verbatim to `west alp-build` (app path, `--core <id>`,
    /// `--sequential`, `-b <board>`) when not using `--plan`.
    #[arg(
        trailing_var_arg = true,
        allow_hyphen_values = true,
        value_name = "ARGS"
    )]
    pub args: Vec<String>,
}

/// Args for `bootstrap`: toggles for the pip/west steps and an env-only dry run.
#[derive(Debug, Args)]
pub struct BootstrapArgs {
    /// Skip the pip dependency install step.
    #[arg(long = "no-pip")]
    pub no_pip: bool,
    /// Skip the west init/update step.
    #[arg(long = "no-west")]
    pub no_west: bool,
    /// Only print the environment-variable lines and exit.
    #[arg(long = "print-env")]
    pub print_env: bool,
}

/// Args for `sdk`: a free-form subcommand verb plus its positional argument and cache destination.
#[derive(Debug, Args)]
pub struct SdkArgs {
    /// Subcommand: list, install, current, or switch.
    #[arg(value_name = "SUBCOMMAND")]
    pub subcommand: Option<String>,
    /// Positional argument (version for install, version|path for switch).
    #[arg(value_name = "ARG")]
    pub arg: Option<String>,
    /// Cache root for `install` (default: ~/.alp/sdk-cache).
    #[arg(long)]
    pub destination: Option<String>,
}

/// Args for `doctor`: debug target/server selection and a build-readiness toggle.
#[derive(Debug, Args)]
pub struct DoctorArgs {
    /// Debug target class (zephyr-mcu, baremetal-mcu, yocto-userspace, native-host).
    #[arg(long = "target-kind", value_name = "KIND")]
    pub target_kind: Option<String>,
    /// Debug server backend (jlink, openocd, pyocd, gdbserver, none).
    #[arg(long, value_name = "SERVER")]
    pub server: Option<String>,
    /// Run the build-readiness preflight instead of the debug-readiness checks.
    #[arg(long)]
    pub build: bool,
    /// With `--build`: auto-repair a fixable blocker — run `tan bootstrap` when no
    /// Zephyr workspace is resolved, then re-check.
    #[arg(long)]
    pub fix: bool,
}

/// Args for `validate`: an offline-only toggle that skips the Python SDK spawn.
#[derive(Debug, Args)]
pub struct ValidateArgs {
    /// Run the offline structural validator only (no Python SDK spawn).
    #[arg(long)]
    pub offline: bool,
}
