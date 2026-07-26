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
    Generate(GenerateArgs),
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
    /// List SDK presets (SKUs) and built-in catalogue defaults.
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
    /// Build the project natively: consume the SDK's emitted build plan,
    /// materialise its files, then run each per-core slice's command directly.
    Build(BuildArgs),
    /// Show the board-scoped Kconfig symbol menu for one core (the vscode
    /// `prj.conf` LSP's live feed). Needs a bootstrapped Zephyr workspace.
    Kconfig(KconfigArgs),
    /// Assemble a flashable-image bundle from `build/system-manifest.yaml` (native).
    Image(ImageArgs),
    /// Flash every slice + helper MCU from `build/system-manifest.yaml` onto the
    /// device in `boot_order` (native).
    Flash(FlashArgs),
    /// Build the project, then run it: execute the produced native_sim binary
    /// for a host target, or flash a hardware target (native).
    Run(RunArgs),
    /// Remove the per-project build dir + orchestrator state cache (native).
    Clean(CleanArgs),
    /// Boot the built system manifest's single Zephyr slice in headless Renode
    /// as a no-hardware smoke test (native).
    Renode(RenodeArgs),
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

/// Args for `clean`: app-path + build-root override + dry-run. Native — mirrors
/// the retired `west alp-clean` flags (`--build-root`, `--dry-run`). Takes the
/// unified OPTIONAL positional `app_path` (default `.`): a non-`.` value roots
/// the removal at that app dir; `.` falls back to the global `--project`.
#[derive(Debug, Args)]
pub struct CleanArgs {
    /// Application source directory (default: `.`). `build_root` defaults to
    /// `<app_path>/build`; a non-`.` value overrides the global `--project`.
    #[arg(value_name = "APP_PATH", default_value = ".")]
    pub app_path: String,
    /// Override the build root to remove (default: `<app_path>/build`).
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

/// Args for `renode`: boot the built system-manifest's single Zephyr slice in
/// headless Renode. Native — mirrors the retired `west alp-renode` flags
/// (positional `app_path`, `--build-root`, `--board`, `--image-bundle`, `--log`,
/// `--timeout`, `--expect`, `--sim-mode`). `--sim-mode` is wired for surface
/// stability but errors "not yet ported" (studio sim gateway, issue #674).
#[derive(Debug, Args)]
pub struct RenodeArgs {
    /// Application source directory (default: `.`). Used to derive the default
    /// build root (`<app_path>/build`) and the Envelope project context.
    #[arg(value_name = "APP_PATH", default_value = ".")]
    pub app_path: String,
    /// Override the build root holding `system-manifest.yaml`
    /// (default: `<app_path>/build`).
    #[arg(long = "build-root", value_name = "DIR")]
    pub build_root: Option<String>,
    /// Override the SoM SKU used to pick the Renode platform descriptor
    /// (default: `hw_info.sku` from the manifest).
    #[arg(long, value_name = "SKU")]
    pub board: Option<String>,
    /// Boot the Zephyr slice with this `core_id`. Needed on a manifest carrying
    /// more than one Zephyr slice (an AEN801's `m55_hp` + `m55_he`), which the
    /// single-slice smoke otherwise refuses outright; optional when the project
    /// has exactly one.
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
    /// Directory of pre-built per-slice artefacts. Accepted for parity with the
    /// dual-OS flow; unused by the single-Zephyr-slice smoke.
    #[arg(long = "image-bundle", value_name = "DIR")]
    pub image_bundle: Option<String>,
    /// Tee the Renode UART/console output to this file
    /// (default: `<build_root>/renode.log`).
    #[arg(long, value_name = "FILE")]
    pub log: Option<String>,
    /// Wall-clock cap for the Renode run, in seconds.
    #[arg(long, value_name = "SECS", default_value_t = 120)]
    pub timeout: u64,
    /// If set, stop early (exit 0) when this substring appears in any console
    /// line; exit 1 if the run ends without it.
    #[arg(long, value_name = "STR")]
    pub expect: Option<String>,
    /// DEFERRED studio hardware-simulator mode (issue #674): wired for surface
    /// stability but errors "not yet ported" until studio is repointed to `tan`.
    #[arg(long = "sim-mode")]
    pub sim_mode: bool,
}

/// Args for `flash`: walk `build/system-manifest.yaml` and program every slice
/// and helper MCU in `boot_order`. Native — mirrors the retired `west alp-flash`
/// flags verbatim (required positional `app_path` plus build-root/dry-run/core/
/// helper/skip-missing-tools) so existing callers don't break. The Python JSON
/// flag has no analogue — the global `--format json` emits the standard tan
/// Envelope.
#[derive(Debug, Args)]
pub struct FlashArgs {
    /// Application source directory (default: `.`, the current directory).
    /// `build_root` defaults to `<app_path>/build`. Optional positional (the
    /// unified app-path convention: `tan flash` from the app dir needs no
    /// argument, matching the retired Python `app_path` default).
    #[arg(value_name = "APP_PATH", default_value = ".")]
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
/// (`migrate`/`lock`/`quality`) or the
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

/// Args for `run`: build the project, then run it. Thin orchestrator over the
/// native `build` + `flash` commands — it reuses their engines, never
/// re-derives them. The host-vs-hardware target is read from what the build
/// produces (a `native_sim` binary ⇒ host), so `run` needs no `--board`
/// selector: `board.yaml` (via `--project`) already names the target.
#[derive(Debug, Args)]
pub struct RunArgs {
    /// Program the board after building (hardware targets only). Required opt-in:
    /// without it, `run` on a hardware project builds and reports but never
    /// flashes. Ignored for a native_sim/host target, which always runs the
    /// produced binary and never flashes.
    #[arg(long)]
    pub flash: bool,
    /// With `--flash`, flash only the slice with this `core_id` (forwarded
    /// verbatim to the native flash path's `--core`).
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
}

/// Args for `build`: plan/manifest inspection, materialisation, and native build.
#[derive(Debug, Args)]
pub struct BuildArgs {
    /// Show the build plan (consumed from the SDK's `--emit build-plan`) and
    /// exit without building.
    #[arg(long)]
    pub plan: bool,
    /// Read the build plan from a JSON file instead of invoking the SDK. Implies
    /// `--plan`. Use this to consume `alp_orchestrate.py --emit build-plan`
    /// output instead of the live emit (which is the default plan source).
    #[arg(long = "plan-from", value_name = "FILE")]
    pub plan_from: Option<String>,
    /// Materialise the plan: write its generated files (shared artefacts +
    /// per-slice config) to disk under the build root, instead of just showing
    /// the plan. With no `--plan-from`, the plan is fetched live from the SDK.
    #[arg(long)]
    pub materialise: bool,
    /// Build natively: consume the plan, materialise its files, then run each
    /// slice's command (`west` / `bitbake` / `cmake`) sequentially. This is the
    /// default; the flag is kept as an explicit opt-in.
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
    /// Never bootstrap implicitly. By default a text-mode build with no Zephyr
    /// workspace (or a stale one) runs `tan bootstrap` first, which clones
    /// Zephyr + the HALs beside the SDK checkout and takes minutes. Use this to
    /// keep `tan build` to building and get the readiness report instead.
    #[arg(long = "no-auto-bootstrap")]
    pub no_auto_bootstrap: bool,
}

/// Args for `kconfig`: scope the board-scoped Kconfig symbol menu to one core.
#[derive(Debug, Args)]
pub struct KconfigArgs {
    /// Core id to scope the Kconfig symbol menu to (default: the board's one
    /// declared Zephyr core, when unambiguous).
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
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
    /// Cache root to install into, and the first root `switch <version>` looks
    /// in (default: ~/.alp/sdk-cache).
    #[arg(long)]
    pub destination: Option<String>,
    /// With `switch`: pin the machine-global default (`~/.alp/sdk-default`)
    /// instead of the current project (`.alp/sdk-path`).
    #[arg(long)]
    pub global: bool,
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
    /// Resolve against the build slice with this `core_id`. Defaults to the
    /// first slice matching the target class's OS, which is the whole project
    /// on a single-core board; needed to pick a side on a multicore one.
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
    /// Emit `preLaunchTask: <TASK>` on the generated configuration. Off by
    /// default: nothing in this repo, in alp-sdk-vscode, or in a generated
    /// project contributes a task, and VS Code aborts pre-launch (so the
    /// session never starts) on a `preLaunchTask` it cannot resolve. Pass the
    /// name your own `tasks.json` or `TaskProvider` registers.
    #[arg(long = "pre-launch-task", value_name = "TASK")]
    pub pre_launch_task: Option<String>,
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

/// Args for `generate`: overwrite toggle for the one target (`native-sim-overlay`)
/// that writes into the hand-editable app source tree instead of `build/generated/`.
#[derive(Debug, Args)]
pub struct GenerateArgs {
    /// Allow overwriting existing files.
    #[arg(long)]
    pub force: bool,
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
