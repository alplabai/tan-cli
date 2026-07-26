// SPDX-License-Identifier: Apache-2.0
//! `tan bootstrap` — set up the SDK's build environment, natively.
//!
//! A cross-platform Rust port of the SDK's two canonical bootstrap scripts
//! (`alp-sdk/scripts/bootstrap.sh` + `alp-sdk/scripts/bootstrap.ps1`): create
//! the workspace venv, install west into it, `west init -l` / `west update` the
//! Zephyr workspace beside the alp-sdk checkout, and install the Python deps.
//! No `bash` anywhere — native Windows is a first-class host (#49), so the two
//! scripts are the parity oracle for CONTROL FLOW and message strings, not a
//! runtime dependency. The FACTS come from `<sdkRoot>/metadata/bootstrap.json`
//! (alp-sdk#917) — its own `_comment` (as vendored) still calls tan only an
//! INTENDED future consumer that no code reads yet; `tan_core::bootstrap`'s
//! `parse_bootstrap_manifest` is what makes that stale. The compiler
//! toolchains (Zephyr SDK, vendor SDKs) stay out of scope; `doctor` detects +
//! points to those.
//!
//! Decision logic lives in `tan_core::bootstrap`; the spawning steps live in
//! [`steps`]; this file is orchestration + the envelope.
//!
//! Text mode streams the (long) install live on inherited stdio; JSON mode
//! captures it and emits exactly one envelope, folding a failing step's output
//! tail into the issue message and every non-fatal warning into an issue.

mod steps;
mod west_config;

use std::path::{Path, PathBuf};

use serde::Serialize;

use tan_core::ManifestReconcile;
use tan_core::bootstrap::{
    BOOTSTRAP_MANIFEST_REL_PATH, BootstrapFacts, ExistingWorkspace, HostOs, MissingPrerequisite,
    Tokens, WorkspaceChoice, YoctoGate, decide_workspace_reuse, fallback_facts, in_play_runtimes,
    next_steps_block, optional_libs_block, parse_bootstrap_manifest, print_env_block,
    reported_missing, resolve_pin_major_minor, yocto_gate, yocto_mixed_warning, yocto_only_refusal,
};
use tan_core::sdk_catalogue::TopologyCore;

use super::CommandRun;
use crate::cli::{BootstrapArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::{MIN_PYTHON, resolve_cli_project_context};

use steps::{Log, Runner, Workspace, ensure_venv, native, pip_phase, west_phase};
use west_config::same_directory;
// The prerequisite gate, shared with `commands::doctor` so plain `tan doctor`
// reports a missing `ninja` without anyone having to run bootstrap first
// (alp-sdk ADR 0021 P0a). `steps` itself stays a private submodule.
pub(crate) use steps::check_prerequisites;
// Re-exported at crate visibility (rather than the module's own `pub(super)`)
// so `commands::sdk`'s `run_switch` can chain the SAME reconciliation (#62) —
// `west_config` itself stays a private submodule; only these names need a
// wider audience than `bootstrap` alone. `run_switch` uses the guarded
// `_for_switch` variant, never the unconditional one directly — that stays
// `bootstrap`-only since only bootstrap's job IS the workspace under its
// topdir. `workspace_synced_to` is the read half of the sync record bootstrap
// writes below; the OUTCOME type itself is `tan_core::ManifestReconcile`.
pub(crate) use west_config::{
    reconcile_west_manifest_path_for_switch, record_workspace_sdk, workspace_synced_to,
};

/// `data` payload for the `bootstrap` envelope: the resolved SDK root, the
/// three paths the run produced, where its facts came from, and the flags.
///
/// `schemaVersion` is `"2"`: v1's `scriptPath` named `<sdkRoot>/scripts/
/// bootstrap.sh`, which this command no longer runs (and no consumer ever read
/// — the VS Code extension runs bootstrap in a terminal, not through the
/// envelope). It is replaced by the paths a caller can actually act on.
#[derive(Serialize)]
struct BootstrapData {
    /// Payload schema version (`"2"`); serialized as `schemaVersion`.
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved alp-sdk root; empty on failure paths.
    #[serde(rename = "sdkRoot")]
    sdk_root: String,
    /// The west topdir the run targeted (`<sdkRoot>/..`, or a reused
    /// `$ZEPHYR_BASE` workspace); empty when unresolved.
    #[serde(rename = "workspaceDir")]
    workspace_dir: String,
    /// `<workspaceDir>/<venv.dirName>` — the hermetic west + Python deps venv.
    #[serde(rename = "venvDir")]
    venv_dir: String,
    /// `<workspaceDir>/zephyr` — the value to export as `ZEPHYR_BASE`.
    #[serde(rename = "zephyrBase")]
    zephyr_base: String,
    /// Whether the workspace facts came from the SDK's
    /// `metadata/bootstrap.json` (alp-sdk#917) rather than tan's documented
    /// fallback constants. A consumer diagnosing "why did it use THAT pin"
    /// needs to know which side it read.
    #[serde(rename = "factsFromManifest")]
    facts_from_manifest: bool,
    /// The Zephyr `MAJOR.MINOR` the workspace-reuse test compared against.
    #[serde(rename = "zephyrPin")]
    zephyr_pin: String,
    /// `--no-pip` (skip the Python dependency installs).
    #[serde(rename = "noPip")]
    no_pip: bool,
    /// `--no-west` (skip west init/update).
    #[serde(rename = "noWest")]
    no_west: bool,
    /// `--print-env` (print the env-var lines and exit, installing nothing).
    #[serde(rename = "printEnv")]
    print_env: bool,
    /// Per-tool form of a `prerequisites-missing` refusal, so a consumer never
    /// has to parse `issues[].message` back apart (the message is
    /// `lines.join(" ")`, and a command contains the same spaces the join used
    /// — the split is not recoverable; alp-sdk-vscode#347 proved that parse
    /// dead and deleted it).
    ///
    /// `null` on every run that has no missing tool to name: a success, a
    /// refusal that fired BEFORE the gate, AND the two Python-floor refusals,
    /// which have no missing tool at all. NEVER `[]` — a run that checked the
    /// tool list and found it clean is exactly a successful run, which reports
    /// `null`, so `[]` would be a second spelling of the same fact and the
    /// distinction would carry no information a consumer could act on.
    ///
    /// No `skip_serializing_if`: an explicit `null` matches
    /// [`crate::envelope::Project`]'s two fields and makes a captured envelope
    /// self-documenting — the key is in every sample, so a consumer can see the
    /// field exists without reaching for a schema. A deliberate divergence from
    /// the sibling `data` payloads (`build`, `flash`, `pinmux`), which omit
    /// their optional keys instead.
    #[serde(rename = "missingPrerequisites")]
    missing_prerequisites: Option<Vec<MissingPrerequisite>>,
}

/// Mutable run state the envelope reports, threaded through the phases.
struct RunPaths {
    /// `REPO_ROOT` — the alp-sdk checkout.
    repo_root: PathBuf,
    /// `WORKSPACE_DIR` — repointed when a `$ZEPHYR_BASE` workspace is adopted.
    workspace_dir: PathBuf,
    /// `<WORKSPACE_DIR>/<venv.dirName>` — follows `workspace_dir`.
    venv_dir: PathBuf,
}

impl RunPaths {
    /// `${SDK_ROOT}` / `${WORKSPACE_DIR}` values for the CURRENT paths.
    /// Re-derived at each use so a repointed workspace is reflected — see
    /// `tan_core::bootstrap::Tokens`.
    fn token_strings(&self) -> (String, String) {
        (
            self.repo_root.to_string_lossy().into_owned(),
            self.workspace_dir.to_string_lossy().into_owned(),
        )
    }
}

/// Runs `tan bootstrap`. Resolves the SDK root and the west topdir beside it,
/// loads the workspace facts, short-circuits on `--print-env`, gates a
/// Yocto-only project on a non-Linux host, then walks the venv → west → pip
/// phases in the scripts' order.
pub fn run(g: &GlobalArgs, args: &BootstrapArgs) -> CommandRun {
    let is_windows = cfg!(windows);
    let host = HostOs::detect(std::env::consts::OS);
    let mut log = Log::new(g.is_json());

    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };
    let Some(sdk_root) = context.sdk_root.clone() else {
        return failure(
            g,
            ExitCode::ValidationFailure,
            "sdk-root-unresolved",
            vec![
                "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk switch \
                 <version|path>`, or run `tan sdk install <version>` first."
                    .to_string(),
            ],
            empty_data(args),
            // The only refusal that predates project resolution.
            Project {
                root: None,
                board_yaml: None,
            },
        );
    };

    // `west init -l <alp-sdk>` always makes the topdir the checkout's PARENT and
    // alp-sdk itself the manifest repo, which is what registers the `alp-*`
    // extension commands (#769). Zephyr + HALs land as its siblings.
    let repo_root = PathBuf::from(&sdk_root);
    let workspace_dir = repo_root
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| repo_root.clone());
    let mut paths = RunPaths {
        venv_dir: workspace_dir.join(".venv"),
        workspace_dir,
        repo_root,
    };

    // Workspace facts: the SDK's manifest when it has one, else the documented
    // fallback constants. An unsupported schemaVersion is FATAL — silently
    // falling back to hand-ported values is the RFC #843 drift.
    let facts = match load_facts(&sdk_root) {
        Ok(facts) => facts,
        Err(message) => {
            let data = data_for(args, &sdk_root, &paths, &fallback_facts(MIN_PYTHON), "");
            return failure(
                g,
                ExitCode::ValidationFailure,
                "manifest",
                vec![message],
                data,
                project,
            );
        }
    };
    // `venv.dirName` is a manifest fact, so the venv path is only final now.
    paths.venv_dir = paths.workspace_dir.join(&facts.venv_dir_name);
    // ONE pin authority, shared with `preflight`'s zephyrVersion check.
    let pin = resolve_pin_major_minor(
        std::fs::read_to_string(paths.repo_root.join("west.yml"))
            .ok()
            .as_deref(),
        &facts.zephyr_version,
    );

    // `--print-env` short-circuits before any prerequisite check or venv work.
    //
    // Both scripts moved this AFTER their prereq check under #917, because
    // reading the manifest needs `python3`/`python` and they must not surface a
    // raw "command not found" first. tan parses the manifest with `serde_json`
    // and has no such bootstrap-of-the-bootstrap, so the short-circuit stays
    // where it is most useful: `--print-env` answers on a machine that is still
    // missing cmake or ninja.
    if args.print_env {
        let (sdk, ws) = paths.token_strings();
        let tokens = Tokens {
            sdk_root: &sdk,
            workspace_dir: &ws,
        };
        let text = print_env_block(&facts, tokens, facts.venv_bin_dir(is_windows), is_windows);
        let data = data_for(args, &sdk_root, &paths, &facts, &pin);
        return finish(g, ExitCode::Success, project, data, Vec::new(), text);
    }

    // Host gate: refuse ONLY a project whose every in-play core is Yocto, on a
    // non-Linux host. A mixed board still bootstraps — nothing here is
    // Yocto-specific (venv + west + Zephyr requirements), and its Zephyr cores
    // need exactly this.
    let mut issues: Vec<Issue> = Vec::new();
    let board = read_board_model(&context).unwrap_or_default();
    let runtimes = in_play_runtimes(&board, &read_som_topology(&board, &sdk_root));
    match yocto_gate(&runtimes, host) {
        YoctoGate::Refuse => {
            let data = data_for(args, &sdk_root, &paths, &facts, &pin);
            return failure(
                g,
                ExitCode::ValidationFailure,
                "yocto-host",
                vec![yocto_only_refusal()],
                data,
                project,
            );
        }
        YoctoGate::Warn => log.warn("yocto-host", &yocto_mixed_warning()),
        YoctoGate::Clear => {}
    }

    let host_python = match check_prerequisites(&facts, is_windows) {
        Ok(python) => python,
        Err(f) => {
            // The ONLY path that fills `missingPrerequisites` — `data_for`'s
            // other six call sites have nothing to say about prerequisites, so
            // the field stays out of its signature and is set here instead.
            // The code comes from the refusal (`prerequisites-missing`, or one
            // of the two Python-floor codes that carry no tool at all).
            //
            // Empty stays `null`, never `[]`: the Python-floor refusals name no
            // tool, and a run that checked the list and found it clean IS a
            // successful run, which reports `null`. Two spellings of one fact
            // would only give a consumer something extra to get wrong.
            let mut data = data_for(args, &sdk_root, &paths, &facts, &pin);
            data.missing_prerequisites = reported_missing(f.missing);
            return failure(g, ExitCode::RuntimeFailure, f.code, f.lines, data, project);
        }
    };

    log.line(&format!("Repo root:       {}", native(&paths.repo_root)));
    if is_windows {
        log.line(&format!(
            "Workspace dir:   {}  (west topdir; alp-sdk is the manifest)",
            native(&paths.workspace_dir)
        ));
        log.line(&format!(
            "Python:          {}.{}",
            host_python.version.0, host_python.version.1
        ));
    } else {
        log.line(&format!(
            "Workspace dir:   {}",
            native(&paths.workspace_dir)
        ));
        log.line(&format!("Detected OS:     {}", os_label(host)));
    }

    let (reuse, clear_zephyr_base) =
        select_workspace(&mut log, is_windows, &pin, &facts, &mut paths);

    let workspace = Workspace {
        is_windows,
        facts: &facts,
        repo_root: &paths.repo_root,
        workspace_dir: &paths.workspace_dir,
        venv_dir: &paths.venv_dir,
    };
    let runner = Runner {
        json: g.is_json(),
        clear_zephyr_base,
    };

    // The venv backs BOTH later phases, so it is created when either will run.
    let venv = if args.no_west && args.no_pip {
        None
    } else {
        match ensure_venv(&workspace, &mut log, &runner, &host_python) {
            Ok(venv) => Some(venv),
            Err(message) => {
                let mut issues = issues;
                issues.extend(log.take_issues());
                let data = data_for(args, &sdk_root, &paths, &facts, &pin);
                return fatal(g, project, data, issues, message);
            }
        }
    };

    if args.no_west {
        log.line("Skipping west setup (--no-west)");
    } else if let Some(venv) = &venv {
        // Reconcile a stale `.west/config` manifest.path BEFORE west init/update
        // (#31): the "already initialised" branch runs `west update` without
        // re-running `west init -l`, so a config left over from a different SDK
        // checkout under the same topdir would silently pull the WRONG SDK's
        // west.yml. Pointless (and a stray write) when an existing workspace is
        // reused, since that path touches neither west nor this topdir.
        //
        // The outcome outlives this block: it is also what decides whether the
        // sync record below may be written at all.
        let reconciled = (!reuse).then(|| west_config::reconcile_west_manifest_path(&sdk_root));
        match &reconciled {
            Some(ManifestReconcile::Rewrote {
                config_path,
                old_rel,
                new_rel,
            }) => log.warn(
                "west-config-reconciled",
                &format!(
                    "reconciled {} manifest.path {old_rel} -> {new_rel} (it named a different SDK \
                     checkout under this topdir, #31)",
                    config_path.display()
                ),
            ),
            // Silent here would be the worst of the three callers: `west
            // update` is about to run against whatever manifest that
            // unrewritten pointer names -- i.e. the WRONG SDK's west.yml,
            // the exact #31 failure this call exists to prevent.
            Some(ManifestReconcile::Failed {
                config_path,
                old_rel,
                reason,
            }) => log.warn(
                "west-config-reconcile-failed",
                &format!(
                    "{}; `west update` will resolve the manifest from whatever that pointer still \
                     names",
                    tan_core::describe_reconcile_failure(config_path, old_rel.as_deref(), reason)
                ),
            ),
            _ => {}
        }
        if let Err(message) = west_phase(&workspace, venv, &mut log, &runner, reuse) {
            let mut issues = issues;
            issues.extend(log.take_issues());
            let data = data_for(args, &sdk_root, &paths, &facts, &pin);
            return fatal(g, project, data, issues, message);
        }
        // `west update` just materialised this topdir's trees from THIS SDK's
        // manifest -- the one fact nothing else on disk records, and the one
        // `tan sdk switch` needs to stop recommending a bootstrap that already
        // happened. Only on the `!reuse` path: a reused workspace was left
        // untouched (and, when adopted via `$ZEPHYR_BASE`, is not even under
        // this topdir).
        //
        // NOT after a Failed reconcile, which is the subtle one: the update
        // above resolved its manifest from the STALE pointer, and a stale
        // pointer naming a sibling alp-sdk checkout still satisfies west's own
        // guard, so `west_phase` returns Ok over trees belonging to the other
        // SDK. Recording a sync here would assert the very thing that did not
        // happen -- and the next `tan sdk switch`, seeing a by-then-rewritable
        // pointer plus `synced = true`, would suppress the bootstrap advice
        // over those trees. That is the gap this branch exists to close,
        // re-entered through the failure path.
        if !matches!(reconciled, Some(ManifestReconcile::Failed { .. })) && !reuse {
            record_workspace_sdk(&paths.workspace_dir, &sdk_root);
        }
    }

    if args.no_pip {
        log.line("Skipping pip installs (--no-pip)");
    } else if let Some(venv) = &venv {
        pip_phase(&workspace, venv, &mut log, &runner);
    }

    // NOTE: this does NOT install the Zephyr SDK (the cross toolchains). Real
    // silicon targets need it -- run `west sdk install` from the workspace once.
    let venv_bin_dir = venv
        .as_ref()
        .map(|v| v.bin_dir.clone())
        .unwrap_or_else(|| facts.venv_bin_dir(is_windows).to_string());
    // `workspace` borrows `paths`; the closing blocks read `paths` directly.
    let _ = workspace;
    let (sdk, ws) = paths.token_strings();
    let tokens = Tokens {
        sdk_root: &sdk,
        workspace_dir: &ws,
    };
    let mut text = optional_libs_block(&facts, host, &native(&paths.workspace_dir));
    text.push(String::new());
    // Deliberately NOT the oracles' "Bootstrap complete." — `doctor`'s
    // `bootstrap_fix_doctor_check` folds this exact line into its `bootstrapFix`
    // check detail, and `tan`'s own house prefix is what reads correctly there
    // ("bootstrap: complete."). The only line in the port that is reworded.
    text.push("bootstrap: complete.".to_string());
    text.extend(next_steps_block(
        &facts,
        tokens,
        &native(&paths.venv_dir),
        &venv_bin_dir,
        is_windows,
    ));
    issues.extend(log.take_issues());
    let data = data_for(args, &sdk_root, &paths, &facts, &pin);
    finish(g, ExitCode::Success, project, data, issues, text)
}

/// Load `<sdkRoot>/metadata/bootstrap.json`, or fall back to the hand-ported
/// constants when the SDK predates alp-sdk#917.
///
/// Version-skew guard, matching `parse_build_plan`: an ABSENT manifest is the
/// legacy path and falls back; a manifest present with an unsupported
/// `schemaVersion` (or one that cannot be parsed) is a hard `Err` naming why.
/// Falling back THERE would silently re-introduce hand-ported behaviour against
/// an SDK that explicitly declared something else — the RFC #843 drift.
///
/// `pub(crate)`: `commands::doctor` reads the same facts for the same gate, and
/// a second reader with its own skew rule is exactly the drift this guard
/// exists to catch.
pub(crate) fn load_facts(sdk_root: &str) -> Result<BootstrapFacts, String> {
    let path = Path::new(sdk_root).join(BOOTSTRAP_MANIFEST_REL_PATH);
    match std::fs::read_to_string(&path) {
        Ok(text) => parse_bootstrap_manifest(&text).map_err(|e| e.to_string()),
        // Any read failure (absent, unreadable) is treated as "legacy SDK":
        // every released alp-sdk today has no manifest at all.
        Err(_) => Ok(fallback_facts(MIN_PYTHON)),
    }
}

/// Workspace selection: reuse the `$ZEPHYR_BASE` tree only when it is a
/// `<pin>.x` west workspace whose manifest IS this alp-sdk checkout. Returns
/// `(reuse, clear_zephyr_base)` and, on reuse, repoints `paths.workspace_dir` /
/// `paths.venv_dir` at the adopted tree. The rejected cases clear
/// `$ZEPHYR_BASE` from every child so a stale tree cannot hijack `west init`.
///
/// Both rejection outcomes are recorded as warnings, not just printed: a JSON
/// consumer otherwise could not tell that its `$ZEPHYR_BASE` was refused and a
/// second workspace built somewhere else entirely.
fn select_workspace(
    log: &mut Log,
    is_windows: bool,
    pin: &str,
    facts: &BootstrapFacts,
    paths: &mut RunPaths,
) -> (bool, bool) {
    // Read the ENVIRONMENT VARIABLE only -- never a shell rc file -- so this
    // behaves identically under bash / zsh / fish / PowerShell / WSL.
    let Some(zephyr_base) = std::env::var("ZEPHYR_BASE")
        .ok()
        .filter(|v| !v.trim().is_empty())
    else {
        return (false, false);
    };
    let zephyr_base_path = PathBuf::from(&zephyr_base);
    let Ok(version_file) = std::fs::read_to_string(zephyr_base_path.join("VERSION")) else {
        return (false, false);
    };
    let top = zephyr_base_path.parent().map(Path::to_path_buf);
    let var = if is_windows {
        "$env:ZEPHYR_BASE"
    } else {
        "$ZEPHYR_BASE"
    };
    let existing = ExistingWorkspace {
        version_file: &version_file,
        top_is_west_workspace: top.as_ref().is_some_and(|t| t.join(".west").is_dir()),
        manifest_is_sdk: top
            .as_deref()
            .is_some_and(|t| manifest_points_at(t, &paths.repo_root)),
    };
    match decide_workspace_reuse(&existing, pin) {
        WorkspaceChoice::Reuse { major_minor } => {
            // Never modify the user's tree: adopt it and skip init/update.
            paths.workspace_dir = top.unwrap_or_else(|| paths.workspace_dir.clone());
            paths.venv_dir = paths.workspace_dir.join(&facts.venv_dir_name);
            log.line(&format!(
                "Reusing compatible alp-sdk workspace from {var}: {} (Zephyr {major_minor}.x)",
                native(&paths.workspace_dir)
            ));
            (true, false)
        }
        WorkspaceChoice::ManifestMismatch => {
            let existing = native(top.as_deref().unwrap_or(&zephyr_base_path));
            log.warn(
                "zephyr-base-manifest-mismatch",
                &format!(
                    "{var} workspace ({existing}) is a {pin}.x tree but its manifest is not \
                     alp-sdk's west.yml -- not reusing it (would leave 'west {}' unknown, #769); \
                     building an alp-sdk workspace at {}",
                    facts.west_extension_guard,
                    native(&paths.workspace_dir)
                ),
            );
            (false, true)
        }
        WorkspaceChoice::Incompatible => {
            // bootstrap.sh's message carries a tail bootstrap.ps1's does not.
            let tail = if is_windows {
                ""
            } else {
                " and building an isolated one"
            };
            log.warn(
                "zephyr-base-incompatible",
                &format!(
                    "{var} ({zephyr_base}) is not a {pin}.x west workspace -- ignoring it{tail}"
                ),
            );
            (false, true)
        }
    }
}

/// Whether `<topdir>/.west/config`'s `[manifest] path` resolves to `repo_root`.
/// west/the venv aren't set up yet at this point, so read the config directly
/// rather than shelling `west config manifest.path`.
///
/// `pub(crate)`, not private: `build::workspace::west_workspace_dir` reuses this
/// SAME check (#61) to refuse a `$ZEPHYR_BASE` workspace whose manifest isn't
/// alp-sdk's, exactly like `select_workspace` above refuses one for `tan
/// bootstrap` -- one manifest-match test, not two copies that could drift.
pub(crate) fn manifest_points_at(topdir: &Path, repo_root: &Path) -> bool {
    let Ok(config) = std::fs::read_to_string(topdir.join(".west").join("config")) else {
        return false;
    };
    let Some(rel) = tan_core::get_manifest_path(&config) else {
        return false;
    };
    same_directory(&topdir.join(rel.trim()), repo_root)
}

/// The project's `board.yaml`, when one resolves and parses.
fn read_board_model(context: &tan_core::ProjectContext) -> Option<tan_core::BoardModel> {
    let path = context.board_yaml_path.as_deref()?;
    tan_core::parse_board_model(&std::fs::read_to_string(path).ok()?).ok()
}

/// The SoM topology for `board`'s `som.sku`, read from the SDK metadata.
/// Supports both layouts the SDK has used — a flat `<sku>.yaml` or an
/// `<sku>/som.yaml` directory. Empty when anything is missing or unparseable,
/// which every caller must treat as "unresolvable, proceed".
fn read_som_topology(board: &tan_core::BoardModel, sdk_root: &str) -> Vec<TopologyCore> {
    let Some(sku) = board
        .som
        .as_ref()
        .and_then(|som| som.sku.as_deref())
        .map(str::trim)
        .filter(|sku| !sku.is_empty())
    else {
        return Vec::new();
    };
    let dir = Path::new(sdk_root).join("metadata").join("e1m_modules");
    for candidate in [
        dir.join(format!("{sku}.yaml")),
        dir.join(sku).join("som.yaml"),
    ] {
        if let Ok(text) = std::fs::read_to_string(&candidate) {
            if let Ok(som) = tan_core::parse_som_preset(&text) {
                return som.topology;
            }
        }
    }
    Vec::new()
}

/// The POSIX script's `OS_LABEL`. `windows-bash` (git-bash/MSYS) has no
/// counterpart here: on Windows `tan bootstrap` runs the native PowerShell
/// flow, which prints the Python version instead of an OS label.
fn os_label(host: HostOs) -> &'static str {
    match host {
        HostOs::Linux => "linux",
        HostOs::MacOs => "macos",
        HostOs::Windows => "windows",
        HostOs::Other => "unknown",
    }
}

/// Assemble the `CommandRun`: one envelope on stdout in JSON mode, the text
/// lines otherwise.
fn finish(
    g: &GlobalArgs,
    exit: ExitCode,
    project: Project,
    data: BootstrapData,
    issues: Vec<Issue>,
    text: Vec<String>,
) -> CommandRun {
    let json = g
        .is_json()
        .then(|| Envelope::new("bootstrap", project, data, issues, exit.code()).to_json());
    CommandRun {
        exit,
        text: if g.is_json() { Vec::new() } else { text },
        json,
    }
}

/// A failing STEP: the fatal message as a `bootstrap.failed` error issue, on
/// top of whatever warnings the run had already recorded, keeping the resolved
/// project + paths (unlike [`failure`], which reports a null project).
fn fatal(
    g: &GlobalArgs,
    project: Project,
    data: BootstrapData,
    mut issues: Vec<Issue>,
    message: String,
) -> CommandRun {
    issues.push(Issue {
        code: "bootstrap.failed".to_string(),
        severity: "error".to_string(),
        message: message.clone(),
    });
    finish(
        g,
        ExitCode::RuntimeFailure,
        project,
        data,
        issues,
        vec![message],
    )
}

/// Assemble a failure `CommandRun` for a refusal before any step ran: one
/// `bootstrap.<code>` issue whose message is `lines` joined, and those same
/// lines as the text output (which is what `doctor --build --fix` and `build`'s
/// auto-bootstrap surface).
///
/// `project` is the resolved one on every refusal that happens AFTER project
/// resolution — the Yocto verdict in particular is DERIVED from that project's
/// `board.yaml`, so reporting a null project told a consumer "every core here
/// targets Yocto" with no way to say which project. Only
/// `sdk-root-unresolved`, which returns before resolution, passes a null one.
fn failure(
    g: &GlobalArgs,
    exit: ExitCode,
    code: &str,
    lines: Vec<String>,
    data: BootstrapData,
    project: Project,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("bootstrap.{code}"),
        severity: "error".to_string(),
        message: lines.join(" "),
    }];
    finish(g, exit, project, data, issues, lines)
}

/// The envelope payload for a run that got as far as resolving its paths.
fn data_for(
    args: &BootstrapArgs,
    sdk_root: &str,
    paths: &RunPaths,
    facts: &BootstrapFacts,
    pin: &str,
) -> BootstrapData {
    // `zephyrBase` is RENDERED FROM THE MANIFEST (`env.ZEPHYR_BASE`), never
    // re-derived as `<workspaceDir>/zephyr`: if alp-sdk repoints that key the
    // printed export line follows it, and a second derivation here would hand a
    // JSON consumer a path nothing else in the run agrees with. Absent key ->
    // empty, same as every other unresolved path field.
    let (sdk, ws) = paths.token_strings();
    let tokens = Tokens {
        sdk_root: &sdk,
        workspace_dir: &ws,
    };
    let zephyr_base = facts
        .env
        .iter()
        .find(|(key, _)| key == "ZEPHYR_BASE")
        .map(|(_, raw)| native(Path::new(&tokens.apply(raw))))
        .unwrap_or_default();
    BootstrapData {
        schema_version: "2".to_string(),
        // `native()` like the other three: a consumer comparing `sdkRoot`
        // against `workspaceDir` (prefix / dirname) needs one separator.
        sdk_root: native(Path::new(sdk_root)),
        workspace_dir: native(&paths.workspace_dir),
        venv_dir: native(&paths.venv_dir),
        zephyr_base,
        facts_from_manifest: facts.from_manifest,
        zephyr_pin: pin.to_string(),
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
        // Only the prerequisite refusal has a verdict to report; every other
        // caller of this leaves it `null` ("not reported"), never `[]`.
        missing_prerequisites: None,
    }
}

/// `BootstrapData` for a failure before any path resolved: empty paths, but
/// carries through the user's flag selections.
fn empty_data(args: &BootstrapArgs) -> BootstrapData {
    BootstrapData {
        schema_version: "2".to_string(),
        sdk_root: String::new(),
        workspace_dir: String::new(),
        venv_dir: String::new(),
        zephyr_base: String::new(),
        facts_from_manifest: false,
        zephyr_pin: String::new(),
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
        // `sdk-root-unresolved` refuses before the gate ever runs.
        missing_prerequisites: None,
    }
}

/// The last few non-empty lines of a failed step's captured output — a pure
/// read of bytes JSON mode already captured via `Command::output()` (mirrors
/// `flash/mod.rs`'s `capture_tail`). Prefers stderr, falling back to stdout
/// when stderr is empty; returns `""` when there is nothing usable.
pub(super) fn capture_tail(stdout: &[u8], stderr: &[u8]) -> String {
    let mut text = String::from_utf8_lossy(stderr).into_owned();
    if text.trim().is_empty() {
        text = String::from_utf8_lossy(stdout).into_owned();
    }
    let tail: Vec<&str> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    tail.join(" | ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_tail_prefers_stderr_and_keeps_last_lines_in_order() {
        // Regression: JSON-mode bootstrap used to discard the captured
        // stdout/stderr entirely (`.output().ok().and_then(|o| o.status.code())`),
        // so the actual failure reason (e.g. a pip traceback, "no such file")
        // never reached the JSON envelope. `capture_tail` is the read of that
        // already-captured output the fix folds into the issue message.
        let stdout = b"apt-get: installing\nzephyr sdk unpack\n";
        let stderr = b"line1\nline2\nline3\nline4\nline5\n";
        let tail = capture_tail(stdout, stderr);
        assert_eq!(tail, "line2 | line3 | line4 | line5");
    }

    #[test]
    fn capture_tail_falls_back_to_stdout_when_stderr_is_empty() {
        let stdout = b"west init failed: no such file or directory\n";
        let tail = capture_tail(stdout, b"");
        assert_eq!(tail, "west init failed: no such file or directory");
    }

    #[test]
    fn capture_tail_is_empty_when_nothing_was_captured() {
        assert_eq!(capture_tail(b"", b""), "");
    }

    /// `--format json`, everything else default.
    fn json_args() -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: crate::cli::Format::Json,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    /// No flags set.
    fn bootstrap_args() -> BootstrapArgs {
        BootstrapArgs {
            no_pip: false,
            no_west: false,
            print_env: false,
        }
    }

    #[test]
    fn missing_prerequisites_is_null_unless_a_tool_is_actually_named() {
        let args = bootstrap_args();
        // Never reported: an explicit `null`, NOT an omitted key.
        let json = serde_json::to_value(empty_data(&args)).unwrap();
        assert_eq!(json["missingPrerequisites"], serde_json::Value::Null);

        // The blocker this guards: the two Python-floor refusals reach the
        // assignment with an EMPTY vec, and `[]` on the wire would spell
        // "checked, nothing missing" -- which is what a successful run reports
        // as `null`. Both must be `null`.
        assert_eq!(
            reported_missing(tan_core::bootstrap::windows_python_not_runnable().missing),
            None
        );
        assert_eq!(
            reported_missing(tan_core::bootstrap::python_too_old((3, 9), (3, 10)).missing),
            None
        );
        assert!(
            reported_missing(tan_core::bootstrap::windows_refusal(&["ninja"]).missing).is_some()
        );

        // Reported: one object per tool, `command` null for a tool tan knows no
        // install command for (a consumer renders that field as something it
        // RUNS, so advice must never appear there).
        let mut data = empty_data(&args);
        data.missing_prerequisites = reported_missing(
            tan_core::bootstrap::windows_refusal(&["ninja", "no-such-tool"]).missing,
        );
        let json = serde_json::to_value(data).unwrap();
        assert_eq!(
            json["missingPrerequisites"],
            serde_json::json!([
                {"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"},
                {"tool": "no-such-tool", "command": null},
            ])
        );
    }

    #[test]
    fn failure_joins_its_lines_with_a_single_space_into_one_issue_message() {
        // The PREMISE of `missingPrerequisites`: this join is why a consumer
        // cannot recover `<tool>`/`<command>` from the message (a command
        // contains the same spaces the join used). Nothing else pins it, and
        // there is no bootstrap golden under `contract/envelopes/` -- bootstrap
        // probes the host PATH, which that suite deliberately excludes -- so a
        // refactor to `join("\n")` would silently change every bootstrap issue
        // message on the wire.
        let refusal = tan_core::bootstrap::windows_refusal(&["ninja"]);
        let run = failure(
            &json_args(),
            ExitCode::RuntimeFailure,
            refusal.code,
            refusal.lines,
            empty_data(&bootstrap_args()),
            Project {
                root: None,
                board_yaml: None,
            },
        );
        let envelope: serde_json::Value =
            serde_json::from_str(&run.json.expect("json mode emits an envelope")).unwrap();
        assert_eq!(
            envelope["issues"][0]["code"],
            "bootstrap.prerequisites-missing"
        );
        assert_eq!(envelope["issues"][0]["severity"], "error");
        assert_eq!(
            envelope["issues"][0]["message"],
            "Missing required tools:   ninja  ->  winget install -e --id Ninja-build.Ninja \
             Install the tools above (then reopen PowerShell) and re-run."
        );
    }

    /// Fresh temp dir for one test, tagged and pid-scoped like the other
    /// command test suites in this crate.
    fn tmp(tag: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("tan-bootstrap-mod-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn an_sdk_without_a_manifest_falls_back_but_a_skewed_one_is_fatal() {
        let sdk = tmp("facts");
        // No metadata/bootstrap.json at all -> legacy SDK -> fallback constants.
        let legacy = load_facts(&sdk.to_string_lossy()).expect("absent manifest must fall back");
        assert!(!legacy.from_manifest);
        assert_eq!(legacy.zephyr_version, "v4.4.0");

        // Present and supported -> its values win.
        let metadata = sdk.join("metadata");
        std::fs::create_dir_all(&metadata).unwrap();
        let real = include_str!("../../../../../contract/fixtures/bootstrap/manifest.json");
        std::fs::write(metadata.join("bootstrap.json"), real).unwrap();
        let loaded = load_facts(&sdk.to_string_lossy()).expect("real manifest must parse");
        assert!(loaded.from_manifest);
        assert_eq!(loaded.west_pip_spec, "west>=0.14.0");

        // Present but skewed -> HARD ERROR, never a silent fallback (RFC #843).
        std::fs::write(
            metadata.join("bootstrap.json"),
            real.replace("\"schemaVersion\": 1", "\"schemaVersion\": 99"),
        )
        .unwrap();
        let err = load_facts(&sdk.to_string_lossy()).expect_err("skew must not fall back");
        assert!(err.contains("schemaVersion 99"), "{err}");
        let _ = std::fs::remove_dir_all(&sdk);
    }

    #[test]
    fn som_topology_reads_both_metadata_layouts_and_tolerates_neither() {
        let sdk = tmp("topology");
        let modules = sdk.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(modules.join("E1M-DIR101")).unwrap();
        let yaml = "schema_version: 1\nsku: E1M-FLAT101\nfamily: aen\nsilicon: alif-e7\n\
                    topology:\n  m55_hp:\n    board: b\n";
        std::fs::write(modules.join("E1M-FLAT101.yaml"), yaml).unwrap();
        std::fs::write(
            modules.join("E1M-DIR101").join("som.yaml"),
            yaml.replace("E1M-FLAT101", "E1M-DIR101"),
        )
        .unwrap();

        let board = |sku: &str| tan_core::BoardModel {
            som: Some(tan_core::model::Som {
                sku: Some(sku.to_string()),
            }),
            ..Default::default()
        };
        let sdk_root = sdk.to_string_lossy().into_owned();
        assert_eq!(
            read_som_topology(&board("E1M-FLAT101"), &sdk_root)
                .first()
                .map(|c| c.id.clone()),
            Some("m55_hp".to_string())
        );
        assert_eq!(
            read_som_topology(&board("E1M-DIR101"), &sdk_root)
                .first()
                .map(|c| c.id.clone()),
            Some("m55_hp".to_string())
        );
        // Unknown SKU / no SKU -> empty, which the Yocto gate treats as "run".
        assert!(read_som_topology(&board("E1M-NOPE"), &sdk_root).is_empty());
        assert!(read_som_topology(&tan_core::BoardModel::default(), &sdk_root).is_empty());
        let _ = std::fs::remove_dir_all(&sdk);
    }
}
