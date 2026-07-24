// SPDX-License-Identifier: Apache-2.0
//! `tan bootstrap` — set up the SDK's build environment, natively.
//!
//! A cross-platform Rust port of the SDK's two canonical bootstrap scripts
//! (`alp-sdk/scripts/bootstrap.sh` + `alp-sdk/scripts/bootstrap.ps1`): create
//! the workspace venv, install west into it, `west init -l` / `west update` the
//! Zephyr workspace beside the alp-sdk checkout, and install the Python deps.
//! No `bash` anywhere — native Windows is a first-class host (#49), so the two
//! scripts are the parity oracle for step order and message strings, not a
//! runtime dependency. The compiler toolchains (Zephyr SDK, vendor SDKs) stay
//! out of scope; `doctor` detects + points to those.
//!
//! Decision logic lives in `tan_core::bootstrap`; the spawning steps live in
//! `steps`; this file is orchestration + the envelope.
//!
//! Text mode streams the (long) install live on inherited stdio; JSON mode
//! captures it and emits exactly one envelope, folding a failing step's output
//! tail into the issue message.

mod steps;
mod west_config;

use std::path::{Path, PathBuf};

use serde::Serialize;

use tan_core::bootstrap::{
    ExistingWorkspace, HostOs, WorkspaceChoice, YoctoGate, ZEPHYR_VERSION, decide_workspace_reuse,
    in_play_runtimes, next_steps_block, optional_libs_block, pin_major_minor, print_env_block,
    yocto_gate, yocto_mixed_warning, yocto_only_refusal,
};
use tan_core::sdk_catalogue::TopologyCore;

use super::CommandRun;
use crate::cli::{BootstrapArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use steps::{
    Log, Runner, Workspace, check_prerequisites, ensure_venv, native, pip_phase, west_phase,
};
use west_config::{reconcile_west_manifest_path, same_directory};

/// `data` payload for the `bootstrap` envelope: the resolved SDK root, the
/// three paths the run produced, and the pass-through flags.
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
    /// `<workspaceDir>/.venv` — the hermetic west + Python deps venv.
    #[serde(rename = "venvDir")]
    venv_dir: String,
    /// `<workspaceDir>/zephyr` — the value to export as `ZEPHYR_BASE`.
    #[serde(rename = "zephyrBase")]
    zephyr_base: String,
    /// `--no-pip` (skip the Python dependency installs).
    #[serde(rename = "noPip")]
    no_pip: bool,
    /// `--no-west` (skip west init/update).
    #[serde(rename = "noWest")]
    no_west: bool,
    /// `--print-env` (print the env-var lines and exit, installing nothing).
    #[serde(rename = "printEnv")]
    print_env: bool,
}

/// Runs `tan bootstrap`. Resolves the SDK root and the west topdir beside it,
/// short-circuits on `--print-env`, gates a Yocto-only project on a non-Linux
/// host, then walks the venv → west → pip phases in the scripts' order.
pub fn run(g: &GlobalArgs, args: &BootstrapArgs) -> CommandRun {
    let is_windows = cfg!(windows);
    let host = HostOs::detect(std::env::consts::OS);
    let log = Log { json: g.is_json() };

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
        );
    };

    // `west init -l <alp-sdk>` always makes the topdir the checkout's PARENT and
    // alp-sdk itself the manifest repo, which is what registers the `alp-*`
    // extension commands (#769). Zephyr + HALs land as its siblings.
    let repo_root = PathBuf::from(&sdk_root);
    let mut workspace_dir = repo_root
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| repo_root.clone());
    let mut venv_dir = workspace_dir.join(".venv");

    // --print-env short-circuits BEFORE any prerequisite check or venv work.
    if args.print_env {
        let text = print_env_block(&native(&workspace_dir), is_windows);
        let data = data_for(args, &sdk_root, &workspace_dir, &venv_dir);
        return finish(g, ExitCode::Success, project, data, Vec::new(), text);
    }

    // Host gate: refuse ONLY a project whose every in-play core is Yocto, on a
    // non-Linux host. A mixed board still bootstraps — nothing here is
    // Yocto-specific (venv + west + Zephyr requirements), and its Zephyr cores
    // need exactly this.
    let mut issues: Vec<Issue> = Vec::new();
    let runtimes = in_play_runtimes(
        &read_board_model(&context).unwrap_or_default(),
        &read_som_topology(&context, &sdk_root),
    );
    match yocto_gate(&runtimes, host) {
        YoctoGate::Refuse => {
            let data = data_for(args, &sdk_root, &workspace_dir, &venv_dir);
            return failure(
                g,
                ExitCode::ValidationFailure,
                "yocto-host",
                vec![yocto_only_refusal()],
                data,
            );
        }
        YoctoGate::Warn => {
            let message = yocto_mixed_warning();
            log.line(&message);
            issues.push(Issue {
                code: "bootstrap.yocto-host".to_string(),
                severity: "warning".to_string(),
                message,
            });
        }
        YoctoGate::Clear => {}
    }

    let host_python = match check_prerequisites(is_windows) {
        Ok(python) => python,
        Err(lines) => {
            let data = data_for(args, &sdk_root, &workspace_dir, &venv_dir);
            return failure(
                g,
                ExitCode::RuntimeFailure,
                "prerequisites-missing",
                lines,
                data,
            );
        }
    };

    log.line(&format!("Repo root:       {}", native(&repo_root)));
    if is_windows {
        log.line(&format!(
            "Workspace dir:   {}  (west topdir; alp-sdk is the manifest)",
            native(&workspace_dir)
        ));
        log.line(&format!(
            "Python:          {}.{}",
            host_python.version.0, host_python.version.1
        ));
    } else {
        log.line(&format!("Workspace dir:   {}", native(&workspace_dir)));
        log.line(&format!("Detected OS:     {}", os_label(host)));
    }

    let (reuse, clear_zephyr_base) = select_workspace(
        &log,
        is_windows,
        &repo_root,
        &mut workspace_dir,
        &mut venv_dir,
    );

    let workspace = Workspace {
        is_windows,
        repo_root: &repo_root,
        workspace_dir: &workspace_dir,
        venv_dir: &venv_dir,
    };
    let runner = Runner {
        json: g.is_json(),
        clear_zephyr_base,
    };
    let data = || data_for(args, &sdk_root, &workspace_dir, &venv_dir);

    if !args.no_west || !args.no_pip {
        if let Err(message) = ensure_venv(&workspace, &log, &runner, &host_python) {
            return failure(g, ExitCode::RuntimeFailure, "failed", vec![message], data());
        }
    }

    if args.no_west {
        log.line("Skipping west setup (--no-west)");
    } else {
        // Reconcile a stale `.west/config` manifest.path BEFORE west init/update
        // (#31): the "already initialised" branch runs `west update` without
        // re-running `west init -l`, so a config left over from a different SDK
        // checkout under the same topdir would silently pull the WRONG SDK's
        // west.yml. Pointless (and a stray write) when an existing workspace is
        // reused, since that path touches neither west nor this topdir.
        if !reuse {
            if let Some((config_path, _old_rel, new_rel)) = reconcile_west_manifest_path(&sdk_root)
            {
                log.line(&format!(
                    "reconciled {} manifest.path -> {new_rel}",
                    config_path.display()
                ));
            }
        }
        if let Err(message) = west_phase(&workspace, &log, &runner, reuse) {
            return failure(g, ExitCode::RuntimeFailure, "failed", vec![message], data());
        }
    }

    if args.no_pip {
        log.line("Skipping pip installs (--no-pip)");
    } else {
        pip_phase(&workspace, &log, &runner);
    }

    // NOTE: this does NOT install the Zephyr SDK (the cross toolchains). Real
    // silicon targets need it -- run `west sdk install` from the workspace once.
    let mut text = optional_libs_block(host, &native(&workspace_dir));
    text.push(String::new());
    text.push("bootstrap: complete.".to_string());
    text.extend(next_steps_block(
        &native(&workspace_dir),
        &native(&venv_dir),
        &native(&repo_root),
        is_windows,
    ));
    finish(g, ExitCode::Success, project, data(), issues, text)
}

/// Workspace selection: reuse the `$ZEPHYR_BASE` tree only when it is a
/// `<pin>.x` west workspace whose manifest IS this alp-sdk checkout. Returns
/// `(reuse, clear_zephyr_base)` and, on reuse, repoints `workspace_dir` /
/// `venv_dir` at the adopted tree. The rejected cases clear `$ZEPHYR_BASE` from
/// every child so a stale tree cannot hijack `west init`.
fn select_workspace(
    log: &Log,
    is_windows: bool,
    repo_root: &Path,
    workspace_dir: &mut PathBuf,
    venv_dir: &mut PathBuf,
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
    let pin = pin_major_minor(ZEPHYR_VERSION).unwrap_or_default();
    let var = if is_windows {
        "$env:ZEPHYR_BASE"
    } else {
        "$ZEPHYR_BASE"
    };
    let facts = ExistingWorkspace {
        version_file: &version_file,
        top_is_west_workspace: top.as_ref().is_some_and(|t| t.join(".west").is_dir()),
        manifest_is_sdk: top
            .as_deref()
            .is_some_and(|t| manifest_points_at(t, repo_root)),
    };
    match decide_workspace_reuse(&facts, &pin) {
        WorkspaceChoice::Reuse { major_minor } => {
            // Never modify the user's tree: adopt it and skip init/update.
            *workspace_dir = top.unwrap_or_else(|| workspace_dir.clone());
            *venv_dir = workspace_dir.join(".venv");
            log.line(&format!(
                "Reusing compatible alp-sdk workspace from {var}: {} (Zephyr {major_minor}.x)",
                native(workspace_dir)
            ));
            (true, false)
        }
        WorkspaceChoice::ManifestMismatch => {
            let existing = native(top.as_deref().unwrap_or(&zephyr_base_path));
            log.line(&format!(
                "{var} workspace ({existing}) is a {pin}.x tree but its manifest is not alp-sdk's \
                 west.yml"
            ));
            log.line(&format!(
                "-- not reusing it (would leave 'west alp-migrate' unknown, #769); building an \
                 alp-sdk workspace at {}",
                native(workspace_dir)
            ));
            (false, true)
        }
        WorkspaceChoice::Incompatible => {
            // bootstrap.sh's message carries a tail bootstrap.ps1's does not.
            let tail = if is_windows {
                ""
            } else {
                " and building an isolated one"
            };
            log.line(&format!(
                "{var} ({zephyr_base}) is not a {pin}.x west workspace -- ignoring it{tail}"
            ));
            (false, true)
        }
    }
}

/// Whether `<topdir>/.west/config`'s `[manifest] path` resolves to `repo_root`.
/// west/the venv aren't set up yet at this point, so read the config directly
/// rather than shelling `west config manifest.path`.
fn manifest_points_at(topdir: &Path, repo_root: &Path) -> bool {
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

/// The SoM topology for the project's `som.sku`, read from the SDK metadata.
/// Supports both layouts the SDK has used — a flat `<sku>.yaml` or an
/// `<sku>/som.yaml` directory. Empty when anything is missing or unparseable,
/// which every caller must treat as "unresolvable, proceed".
fn read_som_topology(context: &tan_core::ProjectContext, sdk_root: &str) -> Vec<TopologyCore> {
    let Some(sku) = read_board_model(context)
        .and_then(|board| board.som)
        .and_then(|som| som.sku)
        .filter(|sku| !sku.trim().is_empty())
    else {
        return Vec::new();
    };
    let dir = Path::new(sdk_root).join("metadata").join("e1m_modules");
    for candidate in [
        dir.join(format!("{sku}.yaml")),
        dir.join(&sku).join("som.yaml"),
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
        json: None.or(json),
    }
}

/// Assemble a failure `CommandRun`: one `bootstrap.<code>` issue whose message
/// is `lines` joined, a null project, and those same lines as the text output
/// (which is what `doctor --build --fix` and `build`'s auto-bootstrap surface).
fn failure(
    g: &GlobalArgs,
    exit: ExitCode,
    code: &str,
    lines: Vec<String>,
    data: BootstrapData,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("bootstrap.{code}"),
        severity: "error".to_string(),
        message: lines.join(" "),
    }];
    // Failure paths report a null project (matches the other commands).
    let project = Project {
        root: None,
        board_yaml: None,
    };
    finish(g, exit, project, data, issues, lines)
}

/// The envelope payload for a run that got as far as resolving its paths.
fn data_for(
    args: &BootstrapArgs,
    sdk_root: &str,
    workspace_dir: &Path,
    venv_dir: &Path,
) -> BootstrapData {
    BootstrapData {
        schema_version: "2".to_string(),
        sdk_root: sdk_root.to_string(),
        workspace_dir: native(workspace_dir),
        venv_dir: native(venv_dir),
        zephyr_base: native(&workspace_dir.join("zephyr")),
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
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
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
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
    use super::capture_tail;

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
}
