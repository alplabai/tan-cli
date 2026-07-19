// SPDX-License-Identifier: Apache-2.0
//! Command-line surface (clap derive). Global flags mirror CLI.md §3.1.

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
    /// Initialize a new tan project from a template.
    Init(InitArgs),
    /// Scaffold a module into an existing project.
    Scaffold(ScaffoldArgs),
    /// List the SDK's ready-made example projects (source for `tan init --from-example`).
    Examples,
    /// Diagnose debug readiness for a target/server combination.
    Doctor(DoctorArgs),
    /// Emit a shell completion script (bash, zsh, or fish).
    Completion(CompletionArgs),
    /// Show how board.yaml normalization changes the effective config.
    Diff,
    /// List SDK presets (SKUs, carriers) and built-in catalogue defaults.
    Presets,
    /// Show the E1M pinmux capability table (E1M pad → silicon function) for a SoM family.
    Pinmux(PinmuxArgs),
    /// Explain a project/module template or a generation target.
    Explain(ExplainArgs),
    /// Inspect resolved project/debug context values.
    Inspect(InspectArgs),
    /// Trace the generation decisions a build would make.
    Trace(TraceArgs),
    /// Generate or preview a VS Code launch.json debug configuration.
    DebugConfig(DebugConfigArgs),
    /// Export a diagnostic support bundle (inspect + trace + doctor).
    SupportBundle(SupportBundleArgs),
    /// Manage local SDK installs (list, install, current, switch).
    Sdk(SdkArgs),
    /// Set up the SDK build environment (west + Zephyr workspace + Python deps).
    Bootstrap(BootstrapArgs),
    /// Build the project. `--plan` consumes the SDK's emitted build plan;
    /// otherwise fans board.yaml into per-core slices via `west alp-build`.
    Build(BuildArgs),
    /// Assemble a flashable-image bundle from `build/system-manifest.yaml` (native).
    Image(ImageArgs),
    /// Flash every slice + helper MCU from `build/system-manifest.yaml` onto the
    /// device in `boot_order` (native).
    Flash(FlashArgs),
    /// Remove the per-project build dir + orchestrator state cache (native).
    Clean(CleanArgs),
    /// Boot the system manifest in Renode (`west alp-renode`).
    Renode(WestForwardArgs),
    /// Report per-slice firmware footprint vs the SoM memory budget (native).
    Size(SizeArgs),
    /// Migrate board.yaml to the current schema (`west alp-migrate`).
    Migrate(WestForwardArgs),
    /// Pin/lock library dependencies (`west alp-lock`).
    Lock(WestForwardArgs),
    /// Run the board.yaml quality checks (`west alp-quality`).
    Quality(WestForwardArgs),
    /// Compile + package board.yaml `models:` into `.alpmodel` (`alp model`).
    Model(WestForwardArgs),
    /// Open a serial console to the board (`alp monitor`).
    Monitor(WestForwardArgs),
    /// Scaffold a new SoM's metadata skeleton (`alp new-som`).
    NewSom(WestForwardArgs),
    /// Decode an ARM Cortex-M (ARMv8-M) fault dump (`alp faultdecode`).
    Faultdecode(WestForwardArgs),
}

/// Args for `pinmux`: the family target, resolved from an explicit `--family`
/// stem or mapped from a `--sku`.
#[derive(Debug, Args)]
pub struct PinmuxArgs {
    /// SoM SKU to resolve the pinmux family from (e.g. `E1M-AEN701`).
    #[arg(long)]
    pub sku: Option<String>,
    /// Pinmux family stem directly (e.g. `aen`, `v2n`); overrides `--sku`.
    #[arg(long)]
    pub family: Option<String>,
}

/// Args for `clean`: build-root override + dry-run. Native — mirrors the retired
/// `west alp-clean` flags (`--build-root`, `--dry-run`); root comes from the
/// global `--project` (tan convention), not a positional `app_path`.
#[derive(Debug, Args)]
pub struct CleanArgs {
    /// Override the build root to remove (default: `<project_root>/build`).
    #[arg(long = "build-root", value_name = "PATH")]
    pub build_root: Option<String>,
    /// List the paths that would be removed; delete nothing.
    #[arg(long = "dry-run")]
    pub dry_run: bool,
}

/// Args for `size`: report per-slice FLASH/RAM footprint vs the SoM memory
/// budget. Native — mirrors the retired `west alp-size` flags (positional
/// `app_path`, `--build-root`, `--board`, `--fail-over-budget`). The Python
/// `--json` flag is dropped in favour of the global `--format json`.
#[derive(Debug, Args)]
pub struct SizeArgs {
    /// Application source directory (default: `.`). `build_root` defaults to
    /// `<app_path>/build`. Overrides the global `--project` when not `.`.
    #[arg(value_name = "APP_PATH", default_value = ".")]
    pub app_path: String,
    /// Override the build root holding `system-manifest.yaml`
    /// (default: `<app_path>/build`).
    #[arg(long = "build-root", value_name = "PATH")]
    pub build_root: Option<String>,
    /// Override the SoM SKU used to resolve the memory budget
    /// (default: `hw_info.sku` from the manifest). Distinct from `--board-yaml`.
    #[arg(long, value_name = "SKU")]
    pub board: Option<String>,
    /// Exit non-zero if any slice exceeds its resolved budget (slices with an
    /// unknown budget are skipped + reported, never guessed).
    #[arg(long = "fail-over-budget")]
    pub fail_over_budget: bool,
}

/// Args for `flash`: walk `build/system-manifest.yaml` and program every slice
/// and helper MCU in `boot_order`. Native — mirrors the retired `west alp-flash`
/// flags verbatim (required positional `app_path` plus build-root/dry-run/core/
/// helper/skip-missing-tools) so existing callers don't break. The Python JSON
/// flag has no analogue — the global `--format json` emits the standard tan
/// Envelope.
#[derive(Debug, Args)]
pub struct FlashArgs {
    /// Application source directory. `build_root` defaults to `<app_path>/build`.
    /// Required positional (faithful to `west alp-flash`; not folded into
    /// `--project`).
    #[arg(value_name = "APP_PATH")]
    pub app_path: String,
    /// Override the build root holding `system-manifest.yaml`
    /// (default: `<app_path>/build`).
    #[arg(long = "build-root", value_name = "PATH")]
    pub build_root: Option<String>,
    /// Print the flash command each backend WOULD run and return ok without
    /// spawning; also bypasses the required-tool PATH gate.
    #[arg(long = "dry-run")]
    pub dry_run: bool,
    /// Flash only the slice with this `core_id` (skips every other slice AND all
    /// helpers).
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
    /// Flash only the helper MCU with this name (skips ALL slices and every
    /// other helper).
    #[arg(long, value_name = "NAME")]
    pub helper: Option<String>,
    /// When a backend's required tools are all absent from PATH, warn + skip the
    /// entry instead of failing it. No effect under `--dry-run`.
    #[arg(long = "skip-missing-tools")]
    pub skip_missing_tools: bool,
}

/// Args for `image`: bundle the built slices + helper firmware from
/// `build/system-manifest.yaml` into `image-bundle/`. Native — mirrors the
/// retired `west alp-image` flags (positional `app_path`, `--build-root`); the
/// positional is optional and folds into the global `--project` workspace.
#[derive(Debug, Args)]
pub struct ImageArgs {
    /// Application source directory (default: the resolved `--project`
    /// workspace). `build_root` defaults to `<app_path>/build`. An explicit
    /// positional overrides `--project`.
    #[arg(value_name = "APP_PATH")]
    pub app_path: Option<String>,
    /// Override the build root holding `system-manifest.yaml`
    /// (default: `<app_path>/build`).
    #[arg(long = "build-root", value_name = "PATH")]
    pub build_root: Option<String>,
}

/// Args for commands that forward their tail verbatim to an underlying
/// subprocess — either a `west alp-*` extension
/// (`flash`/`renode`/`migrate`/`lock`/`quality`) or the
/// SDK `alp` CLI (`model`/`monitor`/`new-som`/`faultdecode`).
#[derive(Debug, Args)]
pub struct WestForwardArgs {
    /// Arguments forwarded verbatim to the underlying command (e.g. app path,
    /// `--core <id>`, `--sequential`, `-b <board>`, `--port COM7`, `--cfsr 0x…`).
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

/// Args for `support-bundle`: debug target/server selection, optional trace path, and output directory.
#[derive(Debug, Args)]
pub struct SupportBundleArgs {
    /// Debug target class (zephyr-mcu, baremetal-mcu, yocto-userspace, native-host).
    #[arg(long = "target-kind", value_name = "KIND")]
    pub target_kind: Option<String>,
    /// Debug server backend (jlink, openocd, pyocd, gdbserver, none).
    #[arg(long, value_name = "SERVER")]
    pub server: Option<String>,
    /// Limit generation tracing to this config key path.
    #[arg(long)]
    pub path: Option<String>,
    /// Output directory for the bundle (default: `<workspace>/.alp-support`).
    #[arg(long)]
    pub destination: Option<String>,
}

/// Args for `debug-config`: debug target/server selection and a preview (no-write) toggle.
#[derive(Debug, Args)]
pub struct DebugConfigArgs {
    /// Debug target class (zephyr-mcu, baremetal-mcu, yocto-userspace, native-host).
    #[arg(long = "target-kind", value_name = "KIND")]
    pub target_kind: Option<String>,
    /// Debug server backend (jlink, openocd, pyocd, gdbserver, none).
    #[arg(long, value_name = "SERVER")]
    pub server: Option<String>,
    /// Print the launch configuration without writing launch.json.
    #[arg(long)]
    pub preview: bool,
}

/// Args for `explain`: the template id to describe.
#[derive(Debug, Args)]
pub struct ExplainArgs {
    /// Template id to explain (project or module template).
    #[arg(long)]
    pub template: Option<String>,
}

/// Args for `inspect`: an optional key-path filter and an origin-metadata toggle.
#[derive(Debug, Args)]
pub struct InspectArgs {
    /// Limit output to resolved values under this key path.
    #[arg(long)]
    pub path: Option<String>,
    /// Include source + detail metadata for each value.
    #[arg(long = "show-origin")]
    pub show_origin: bool,
}

/// Args for `trace`: an optional key-path filter for the generation trace.
#[derive(Debug, Args)]
pub struct TraceArgs {
    /// Limit tracing to this config key path.
    #[arg(long)]
    pub path: Option<String>,
}

/// Args for `completion`: the target shell for the emitted script.
#[derive(Debug, Args)]
pub struct CompletionArgs {
    /// Target shell (bash, zsh, or fish). Defaults to bash.
    #[arg(long, value_name = "SHELL")]
    pub shell: Option<String>,
}

/// Args for `init`: template, naming, destination, SoM/cores selection, and preview/force toggles.
#[derive(Debug, Args)]
pub struct InitArgs {
    /// Project template id (e.g. minimal-app, sensor-starter).
    #[arg(long)]
    pub template: Option<String>,
    /// Copy an existing SDK example project verbatim instead of expanding a
    /// template. Value is the example's `category/name` source dir under the SDK
    /// `examples/` directory (e.g. `audio/i2s-tone`); see `tan examples`.
    #[arg(
        long = "from-example",
        value_name = "SOURCE_DIR",
        conflicts_with = "template"
    )]
    pub from_example: Option<String>,
    /// Project name; creates a sub-directory when provided.
    #[arg(long)]
    pub name: Option<String>,
    /// Destination directory (default: current directory or --project).
    #[arg(long)]
    pub destination: Option<String>,
    /// Target SoM SKU written into the generated board.yaml (e.g. E1M-AEN701).
    #[arg(long)]
    pub som: Option<String>,
    /// Comma-separated cores for a heterogeneous project, `id[:os]`
    /// (e.g. `m33_sm:zephyr,a55_cluster:yocto`); OS is inferred from the id when omitted.
    #[arg(long)]
    pub cores: Option<String>,
    /// Show planned files without writing anything.
    #[arg(long)]
    pub preview: bool,
    /// Allow overwriting existing files.
    #[arg(long)]
    pub force: bool,
}

/// Args for `scaffold`: module template, name, destination, and preview/force toggles.
#[derive(Debug, Args)]
pub struct ScaffoldArgs {
    /// Module template id (e.g. sensor-driver, connectivity-service).
    #[arg(long)]
    pub template: Option<String>,
    /// Module name (required).
    #[arg(long)]
    pub name: Option<String>,
    /// Destination project root (default: current directory or --project).
    #[arg(long)]
    pub destination: Option<String>,
    /// Show planned files without writing anything.
    #[arg(long)]
    pub preview: bool,
    /// Allow overwriting existing files.
    #[arg(long)]
    pub force: bool,
}
