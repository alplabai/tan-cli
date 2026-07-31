// SPDX-License-Identifier: Apache-2.0
//! Command-line surface (clap derive). Global flags mirror CLI.md §3.1.

use std::io::IsTerminal;

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

    /// Never prompt. A command with a documented default takes it (`tan init`
    /// scaffolds `zephyr-app` into `.`); one without fails instead of asking
    /// (`tan scaffold` needs `--name`). The same rule applies unasked when
    /// stdin or stderr is not a terminal — piped, redirected, or a CI runner.
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

    /// True when `tan` may block on an interactive prompt: none of
    /// `--non-interactive` / `--ci` / `--format json` was given, AND **both**
    /// stdin and stderr are real terminals.
    ///
    /// The thin wrapper over [`interactive_mode`] that reads the real process
    /// handles. ONE home for the rule, because it was three: #198 added the
    /// terminal term to `init` alone, `bootstrap` had grown its own copy in
    /// #185, and `scaffold` had neither and still hung.
    ///
    /// A missing terminal is treated as non-interactive (flag-derived
    /// defaults), not as an error: that is what `--template`'s own help already
    /// promised ("defaults to zephyr-app when not given and there is no TTY to
    /// prompt on"), so the documented commands run unchanged under redirected
    /// stdio instead of demanding an undocumented `--non-interactive`.
    /// `progress::spinner` and `style::Theme::from_args` already gate on
    /// `stderr().is_terminal()` for the same reason.
    pub fn can_prompt(&self) -> bool {
        interactive_mode(
            self.non_interactive,
            self.ci,
            self.is_json(),
            std::io::stdin().is_terminal(),
            std::io::stderr().is_terminal(),
        )
    }
}

/// Whether `tan` may prompt.
///
/// A pure predicate so every term has a failing test — inlined in `can_prompt`,
/// the terminal terms are unreachable from a unit test and the only way to
/// notice one had been dropped would be a customer hanging. Introduced in #198
/// as an `init`-local `interactive_mode`; lifted here so `scaffold` and
/// `bootstrap` answer the same question from the same place.
///
/// `stdin_is_tty` was the term missing from `init` (#187): without it `tan init`
/// rendered an inquire prompt to a terminal that was not there and then blocked
/// — no timeout, no diagnostic, no exit. From the caller's side that is
/// indistinguishable from a slow operation, which is the worst of the available
/// failure modes: CI, a `sh -c` from a script, an IDE task runner and a
/// `Command::output()` from another tool all hang.
///
/// `stderr_is_tty` is the OTHER half, and it is not redundant, because inquire
/// splits the two handles:
/// - It renders to **stderr** (`inquire::terminal::crossterm`, `IO::Std(
///   stderr())`), so a redirected stderr makes the prompt invisible.
/// - It reads through crossterm's `tty_fd()`, which uses stdin only when stdin
///   is a terminal and otherwise **opens `/dev/tty`**.
///
/// So `stdin=tty, stderr=piped` — every wrapper that captures output while
/// inheriting the terminal — still hangs on a stdin-only gate, with the prompt
/// written into the capture where nobody sees it. Confirmed live under a pty.
/// It also means the Unix block was never on the redirected stdin at all: `tan
/// init </dev/null` from a real terminal session blocked on `/dev/tty`, and the
/// `init: Cancelled.` exit 1 seen instead on CI and in agent shells is only
/// what happens where `/dev/tty` cannot be opened.
fn interactive_mode(
    non_interactive: bool,
    ci: bool,
    is_json: bool,
    stdin_is_tty: bool,
    stderr_is_tty: bool,
) -> bool {
    !non_interactive && !ci && !is_json && stdin_is_tty && stderr_is_tty
}

#[cfg(test)]
mod interactive_mode_tests {
    use super::interactive_mode;

    /// #187/#198: the terminal terms are the ones that were missing, so they
    /// get the failing cases. Every flag term was already right; this table
    /// exists so that dropping one again turns a test red instead of turning a
    /// customer's terminal into a hang with no output and no timeout.
    #[test]
    fn a_prompt_needs_a_terminal_to_prompt_on() {
        // The only combination that may prompt: a human, at a TTY, with no
        // machine-mode flag set.
        assert!(interactive_mode(false, false, false, true, true));

        // No stdin TTY -> never prompt, whatever the flags say. This is #187.
        assert!(!interactive_mode(false, false, false, false, true));

        // No stderr TTY -> never prompt either. inquire renders there, and
        // reads via /dev/tty regardless, so a stdin-only gate leaves this case
        // hanging with the prompt written into whatever captured stderr.
        assert!(!interactive_mode(false, false, false, true, false));

        // Each machine-mode flag independently suppresses prompting even with a
        // real terminal attached -- pinned so a refactor cannot quietly make
        // one of them advisory.
        assert!(
            !interactive_mode(true, false, false, true, true),
            "--non-interactive"
        );
        assert!(!interactive_mode(false, true, false, true, true), "--ci");
        assert!(
            !interactive_mode(false, false, true, true, true),
            "--format json"
        );
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
    Examples(ExamplesArgs),
    /// Diagnose host build readiness plus debug readiness for a target/server pair.
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

/// Args for `examples`: a case-insensitive substring filter over the catalog's
/// `id`/`title` (tan-cli#164 — 97 entries is too many to scan unfiltered).
#[derive(Debug, Args)]
pub struct ExamplesArgs {
    /// Case-insensitive substring to match against `id` or `title`; unset lists
    /// every example.
    #[arg(long, value_name = "SUBSTRING")]
    pub filter: Option<String>,
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
/// `--timeout`, `--expect`, `--sim-mode`). `--sim-mode` serves the studio sim
/// gateway's SOCKET contract (`tan-cli#77`, ported from the retired Python);
/// the `ram_console_buf` UART streamer and the per-SKU sim profiles behind the
/// descriptor's `framebuffers`/`peripherals` remain deferred on that issue.
/// Historical contract `alp-sdk#674` (closed) — always with its owning repo.
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
    /// Studio hardware-simulator mode: boot `--image-bundle`'s firmware
    /// headless and serve the control + UART sockets named by the bundle's
    /// `sim-descriptor.json`. Requires `--image-bundle`; `--expect` is ignored.
    // NOT a doc comment: clap prints the lines above verbatim in `tan renode
    // --help`, so repo-internal citation policy does not belong there. The
    // socket half is implemented (`tan-cli#77`, ported from the retired Python
    // `west alp-renode --sim-mode`); the ram_console → UART streamer, the
    // wired-UART console path and the per-SKU sim profiles stay deferred on the
    // same issue, which is why the UART socket is silent for now. The
    // historical contract is `alp-sdk#674`. Both issue numbers carry their repo
    // because a bare `#674` is what let three alp-sdk workflows drift into
    // linking `tan-cli#674` — an issue that has never existed here and 404s.
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
    /// Force-wipe every slice's build dir before dispatch, regardless of the
    /// recorded SDK-switch stamp (tan-cli#163) — the manual counterpart to the
    /// automatic sdk-switch-pristine wipe, for a stale build dir the stamp
    /// heuristic doesn't (or can't yet) catch. Same wipe, same two safety
    /// guards (an explicit `-d`/`--build-dir` in the slice's own command, or a
    /// plan cwd outside `build/`): this never touches a dir tan can't vouch
    /// for, same as the automatic path. A slice the wipe declines — for either
    /// guard, or because the dir was never configured — says so on the
    /// envelope and in text (`build.pristine-skipped`, tan-cli#183), so
    /// "pristine" never silently means "incremental".
    #[arg(long)]
    pub pristine: bool,
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
    /// Only print the environment-variable lines and exit, installing nothing.
    /// Written to STDOUT (tan-cli#227) — unlike every other text output, which
    /// goes to stderr — so `eval "$(tan bootstrap --print-env)"` and
    /// `tan bootstrap --print-env > env.sh` both work.
    #[arg(long = "print-env")]
    pub print_env: bool,
    /// Report success even when a dependency install failed and the workspace
    /// cannot build (tan-cli#220). The failures are still printed and still
    /// reported as issues — this only changes the verdict, for the case where
    /// the missing packages are ones you know you do not need.
    #[arg(long = "allow-partial")]
    pub allow_partial: bool,
    /// Build the west workspace under this directory instead of the
    /// checkout's parent, moving the checkout there first if it isn't
    /// already (tan-cli#185). Answers the workspace-parent guard outright —
    /// no prompt, no refusal, whatever the parent otherwise holds.
    #[arg(long, value_name = "PATH")]
    pub workspace: Option<String>,
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
    ///
    /// KEPT DELIBERATELY (tan-cli#112), not merely tolerated: since #100
    /// folded build-readiness into PLAIN `tan doctor` too, this flag reads as
    /// redundant, and the natural cleanup would be to retire it. It must not
    /// be retired, or turned into a usage error, because both
    /// `alp-sdk-vscode` call sites pinned to `SUPPORTED_CLI_VERSION "0.4.0"`
    /// hardcode it as a literal argv entry with no fallback --
    /// `["doctor", "--build"]` (the Toolchain Doctor panel) and
    /// `["doctor", "--build", "--fix"]` (its "Bootstrap now" remedy, the
    /// primary recovery path from a failed check). Either change breaks both
    /// at the same moment.
    ///
    /// This is NOT a deprecation-window shim scheduled to collapse into plain
    /// `doctor` on a timer: `--build` and plain `doctor` stay two distinct,
    /// permanent modes (own OS-set resolution, own check vocabulary -- see
    /// `run_build_readiness`'s doc comment in `commands/doctor.rs`), and there
    /// is no planned removal date. What WOULD have to be true before dropping
    /// it: `alp-sdk-vscode` stops calling it at both sites, in the same PR
    /// that bumps its `SUPPORTED_CLI_VERSION` pin past `"0.4.0"` -- and even
    /// then, retiring the flag here is its own deliberate decision, not
    /// something that falls out of that bump automatically.
    #[arg(long)]
    pub build: bool,
    /// With `--build`: auto-repair a fixable blocker — run `tan bootstrap` when no
    /// Zephyr workspace is resolved, then re-check.
    ///
    /// `requires`: `run()` reads this flag only inside its `--build` branch, so
    /// `tan doctor --fix` used to parse, be accepted, and produce output
    /// line-for-line identical to a plain `tan doctor` — no "fixed N", no
    /// "nothing to fix", no error (#100). A usage error is the honest answer;
    /// `--build --fix` is unaffected.
    #[arg(long, requires = "build")]
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
    /// Point cortex-debug's Cortex Peripherals (register) view at an SVD file
    /// you supply. This is the ONLY source of one: the SDK ships no SVD
    /// (alp-sdk#948, whose blocker is an open vendor-redistribution licence
    /// question), so without this flag `svdFile`/`svdPath` are always dropped
    /// and the peripheral view is simply absent. Point it at the vendor's own
    /// SVD, which you are entitled to download.
    ///
    /// A relative path resolves against the CURRENT DIRECTORY. A path that
    /// does not name a readable file fails the command — it never falls back
    /// to "no SVD", because a typo would otherwise be indistinguishable from
    /// not passing the flag at all.
    #[arg(long, value_name = "PATH")]
    pub svd: Option<String>,
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
/// that writes into the hand-editable app source tree instead of `build/generated/`,
/// plus the core selector `--target zephyr-board` requires.
#[derive(Debug, Args)]
pub struct GenerateArgs {
    /// Allow overwriting existing files.
    #[arg(long)]
    pub force: bool,
    /// Core id to generate a Zephyr board tree for. Required by (and only
    /// meaningful for) `--target zephyr-board` (alp-sdk#523, tan-cli#116): it
    /// picks which core's board tree gets generated, matching `--core` on
    /// `tan kconfig`.
    #[arg(long, value_name = "CORE_ID")]
    pub core: Option<String>,
}

/// Args for `init`: template, naming, destination, SoM/cores selection, and preview/force toggles.
#[derive(Debug, Args)]
pub struct InitArgs {
    /// Project template id (e.g. zephyr-app, sensor-starter); defaults to
    /// zephyr-app when not given and there is no TTY to prompt on.
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
    /// Target SoM SKU written into the generated board.yaml (e.g. E1M-AEN801,
    /// the default).
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

#[cfg(test)]
mod tests {
    use super::*;

    /// `clap`'s own consistency pass over the whole command tree — it panics on
    /// a `requires`/`conflicts_with` naming an argument id that does not exist,
    /// which is the one way the `--fix` relationship below could silently
    /// become a no-op again.
    #[test]
    fn the_command_tree_is_internally_consistent() {
        use clap::CommandFactory;
        Cli::command().debug_assert();
    }

    #[test]
    fn doctor_fix_is_a_usage_error_without_build() {
        // #100(a): `tan doctor --fix` used to parse, be accepted, and produce
        // output line-for-line identical to a plain `tan doctor` — `run()`
        // reads the flag only inside its `--build` branch. Dropping the
        // `requires = "build"` attribute fails here.
        let err = Cli::try_parse_from(["tan", "doctor", "--fix"])
            .expect_err("`--fix` without `--build` must be rejected");
        assert_eq!(
            err.kind(),
            clap::error::ErrorKind::MissingRequiredArgument,
            "{err}"
        );
        assert!(err.to_string().contains("--build"), "{err}");

        // The supported spelling still parses, and plain `doctor` is untouched.
        for argv in [
            vec!["tan", "doctor", "--build", "--fix"],
            vec!["tan", "doctor", "--build"],
            vec!["tan", "doctor"],
        ] {
            Cli::try_parse_from(&argv).unwrap_or_else(|e| panic!("{argv:?}: {e}"));
        }
    }
}
