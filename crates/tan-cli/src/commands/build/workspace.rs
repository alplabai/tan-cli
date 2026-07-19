// SPDX-License-Identifier: Apache-2.0
//! The `west`-workspace plumbing: invoking the SDK planner (`--emit`), resolving
//! the west-capable venv / workspace topdir / `west` program, the legacy
//! `west alp-*` delegation entry (`run`), and its argv / launch-error helpers.

use std::path::{Path, PathBuf};
use std::process::Command;

use tan_core::ProjectContext;

use super::BuildData;
use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use super::native::base_dir;

/// Invoke the SDK's `alp_orchestrate.py --emit build-plan` and return its stdout
/// (deterministic, write-free JSON). The plan is the SDK's single source of
/// truth — we only run + parse it. Works against whatever SDK checkout is
/// resolved (`--sdk-root` / settings / bootstrap); the schema-version guard in
/// `parse_build_plan` rejects an incompatible emit.
/// Invoke the SDK's `alp_orchestrate --emit <emit>` (module form, scripts/ on
/// PYTHONPATH) for the project's board.yaml and return its stdout. `emit` is the
/// emit kind (`build-plan` /
/// `system-manifest`); `err_code` is the envelope issue code used for every
/// failure on this path so each caller keeps its own stable code.
pub(super) fn invoke_sdk_emit(
    context: &ProjectContext,
    emit: &str,
    err_code: &'static str,
) -> Result<String, (&'static str, String)> {
    let sdk_root = context.sdk_root.as_deref().ok_or((
        err_code,
        format!(
            "no alp-sdk checkout found — pass `--sdk-root <PATH>`, pin one with \
             `tan sdk switch <version|path>`, set it in settings, or run `tan bootstrap`. The \
             {emit} comes from the SDK's `alp_orchestrate --emit {emit}`."
        ),
    ))?;
    let board_yaml = context.board_yaml_path.as_deref().ok_or((
        err_code,
        "no board.yaml found — pass `--board-yaml <PATH>` or run from a project.".to_string(),
    ))?;
    // The SDK bakes the planner's `sys.executable` into every Zephyr slice
    // command as `-DPython3_EXECUTABLE=<...>` (alp-sdk#787), so the planner must
    // run under the west-capable workspace venv python — not a bare PATH
    // `python3`, which may lack the `west` module entirely (sibling of the #106
    // venv-on-PATH fix for the west child). Fall back to the configured/resolved
    // `context.python_binary` only when no workspace venv resolves.
    let planner_python = venv_python(
        &base_dir(context),
        context.sdk_root.as_deref().map(Path::new),
    )
    .unwrap_or_else(|| context.python_binary.clone());
    // Same guard `validate`/`generate` apply: a Python < 3.10 dies the moment an
    // SDK script imports (`@dataclass(slots=True)`) with a cryptic
    // `TypeError: dataclass() got an unexpected keyword argument 'slots'`.
    // Surface an actionable message instead of that traceback — the build/emit
    // path previously lacked this guard (found via the tan↔alp-sdk e2e run).
    if let Some(message) = crate::util::python_too_old(&planner_python) {
        return Err((err_code, message));
    }
    let scripts_dir = Path::new(sdk_root).join("scripts");
    // Invoke the planner as a module (`python -m alp_orchestrate`) with scripts/
    // on PYTHONPATH — the same way the SDK's own west_commands do. That resolves
    // both the modern package layout (scripts/alp_orchestrate/) and the legacy
    // flat module (scripts/alp_orchestrate.py), so the CLI works against any SDK
    // release. Gate on either being present so the error is clear.
    let has_planner = scripts_dir.join("alp_orchestrate.py").is_file()
        || scripts_dir
            .join("alp_orchestrate")
            .join("__init__.py")
            .is_file();
    if !has_planner {
        return Err((
            err_code,
            format!(
                "the SDK at `{sdk_root}` has no `alp_orchestrate` planner under scripts/ — pin \
                 to an SDK release that ships `--emit {emit}`."
            ),
        ));
    }
    // Prepend scripts/ to PYTHONPATH, preserving any value the caller set.
    let mut path_entries = vec![scripts_dir.clone()];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        path_entries.extend(std::env::split_paths(&existing));
    }
    let pythonpath = std::env::join_paths(path_entries).map_err(|e| {
        (
            err_code,
            format!("failed to build PYTHONPATH for the SDK planner: {e}"),
        )
    })?;
    let output = Command::new(&planner_python)
        .args([
            "-m",
            "alp_orchestrate",
            "--input",
            board_yaml,
            "--emit",
            emit,
        ])
        .env("PYTHONPATH", &pythonpath)
        .output()
        .map_err(|e| {
            (
                err_code,
                format!("failed to run `{planner_python} -m alp_orchestrate --emit {emit}`: {e}"),
            )
        })?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stderr = stderr.trim();
        return Err((
            err_code,
            format!(
                "the SDK {emit} emit failed (rc {}){}",
                output.status.code().unwrap_or(-1),
                if stderr.is_empty() {
                    String::new()
                } else {
                    format!(": {stderr}")
                }
            ),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Build the `west` argv: `alp-<subcommand>` followed by the forwarded args.
fn west_argv(subcommand: &str, passthrough: &[String]) -> Vec<String> {
    let mut argv = vec![format!("alp-{subcommand}")];
    argv.extend(passthrough.iter().cloned());
    argv
}

/// Locate the workspace `.venv` directory whose `west` is actually present —
/// the one search shared by `west_program` (the venv's `west` binary) and
/// `venv_python` (the venv's `python`, used by `invoke_sdk_emit`). Prefer a
/// Python venv created by `tan bootstrap` so builds use the hermetic west
/// rather than a (possibly broken or absent) global one, in this order:
///   1. a `.venv` in the project tree, searched from `start` upward;
///   2. the workspace venv derived from `$ZEPHYR_BASE` (`<ZEPHYR_BASE>/../.venv`),
///      so an activated-but-not-on-PATH workspace still resolves;
///   3. the SDK's canonical `<sdk-parent>/zephyrproject/.venv` — bootstrap.sh's
///      default `WORKSPACE_DIR` — so `tan --sdk-root <X> build` finds the
///      bootstrapped venv WITHOUT the user activating it first.
///
/// Gated on `west` actually being present under the candidate `.venv` (not
/// just the directory existing) — that's what makes this the west-CAPABLE venv
/// both callers need. `None` when none resolve (CI, an activated venv, the
/// contract harness).
fn find_workspace_venv(start: &str, sdk_root: Option<&Path>) -> Option<PathBuf> {
    let (sub, west_exe) = if cfg!(windows) {
        ("Scripts", "west.exe")
    } else {
        ("bin", "west")
    };
    let has_west = |venv: &Path| venv.join(sub).join(west_exe).is_file();

    // 1. A `.venv` in the project tree.
    let mut dir = Some(Path::new(start));
    while let Some(d) = dir {
        let candidate = d.join(".venv");
        if has_west(&candidate) {
            return Some(candidate);
        }
        dir = d.parent();
    }

    // 2. The workspace venv from $ZEPHYR_BASE (workspace = ZEPHYR_BASE/..).
    if let Ok(zephyr_base) = std::env::var("ZEPHYR_BASE") {
        if let Some(workspace) = Path::new(&zephyr_base).parent() {
            let candidate = workspace.join(".venv");
            if has_west(&candidate) {
                return Some(candidate);
            }
        }
    }

    // 3. The SDK workspace venv derived from `--sdk-root`. Post-alp-sdk#782 the
    //    workspace topdir is the SDK's parent (`west init -l <alp-sdk>`), so the
    //    venv is `<sdk-parent>/.venv`; older bootstraps used
    //    `<sdk-parent>/zephyrproject/.venv`. Check both.
    if let Some(parent) = sdk_root.and_then(|s| s.parent()) {
        for workspace in [parent.to_path_buf(), parent.join("zephyrproject")] {
            let candidate = workspace.join(".venv");
            if has_west(&candidate) {
                return Some(candidate);
            }
        }
    }

    None
}

/// Resolve the `west` program to launch: the west-capable workspace venv's
/// `west` binary (see `find_workspace_venv`), falling back to `"west"` on PATH
/// when none resolve (CI, an activated venv, the contract harness) — behaving
/// exactly as before in those environments.
pub(super) fn west_program(start: &str, sdk_root: Option<&Path>) -> String {
    let (sub, exe) = if cfg!(windows) {
        ("Scripts", "west.exe")
    } else {
        ("bin", "west")
    };
    find_workspace_venv(start, sdk_root)
        .map(|venv| venv.join(sub).join(exe).to_string_lossy().into_owned())
        .unwrap_or_else(|| "west".to_string())
}

/// Resolve the west-capable workspace venv's `python` (see
/// `find_workspace_venv`), if one resolves. The SDK planner
/// (`alp_orchestrate`) bakes its own `sys.executable` into every Zephyr slice
/// command as `-DPython3_EXECUTABLE=<...>`, so `invoke_sdk_emit` must run the
/// planner under this python — not a bare PATH `python3`, which may lack the
/// `west` module entirely (alp-sdk#787; sibling of the #106 venv-on-PATH fix
/// for the west child). `None` when no workspace venv resolves — the caller
/// then falls back to `context.python_binary`.
fn venv_python(start: &str, sdk_root: Option<&Path>) -> Option<String> {
    let (sub, exe) = if cfg!(windows) {
        ("Scripts", "python.exe")
    } else {
        ("bin", "python")
    };
    find_workspace_venv(start, sdk_root).and_then(|venv| {
        let candidate = venv.join(sub).join(exe);
        candidate
            .is_file()
            .then(|| candidate.to_string_lossy().into_owned())
    })
}

/// Resolve the west workspace topdir — the directory holding `.west/`. `west alp-*`
/// are extension commands discovered *only* from a workspace manifest, so they must
/// be launched from inside the workspace, not the app dir. Checks: the project tree
/// upward (app inside a workspace), then `$ZEPHYR_BASE/..`, then the SDK-derived
/// layouts — `<sdk-parent>` (alp-sdk-manifest topdir, post-alp-sdk#782) and the
/// legacy `<sdk-parent>/zephyrproject`. `None` when no workspace is found (the
/// caller then keeps the pre-existing behavior of running in the app dir).
pub(super) fn west_workspace_dir(start: &str, sdk_root: Option<&Path>) -> Option<PathBuf> {
    let is_workspace = |dir: &Path| dir.join(".west").is_dir();

    // 1. The project tree (if the app lives inside a workspace).
    let mut dir = Some(Path::new(start));
    while let Some(d) = dir {
        if is_workspace(d) {
            return Some(d.to_path_buf());
        }
        dir = d.parent();
    }
    // 2. `$ZEPHYR_BASE/..` (the workspace topdir).
    if let Ok(zephyr_base) = std::env::var("ZEPHYR_BASE") {
        if let Some(workspace) = Path::new(&zephyr_base).parent() {
            if is_workspace(workspace) {
                return Some(workspace.to_path_buf());
            }
        }
    }
    // 3. SDK-derived layouts.
    if let Some(parent) = sdk_root.and_then(|s| s.parent()) {
        for workspace in [parent.to_path_buf(), parent.join("zephyrproject")] {
            if is_workspace(&workspace) {
                return Some(workspace);
            }
        }
    }
    None
}

/// When `tool` is a resolved venv `west` (an absolute path), prepend its
/// directory to `command`'s `PATH`. `west alp-*` spawns `alp_orchestrate`, which
/// spawns nested `west build`/`bitbake` that resolve `west` via PATH — without
/// this, they fail with "west not found in PATH" and silently skip the slice
/// unless the user activated the venv. A bare `"west"` (PATH fallback) is left as-is.
pub(super) fn with_venv_on_path(command: &mut Command, tool: &str) {
    let bin = Path::new(tool);
    if !bin.is_absolute() {
        return;
    }
    let Some(dir) = bin.parent() else {
        return;
    };
    let existing = std::env::var_os("PATH").unwrap_or_default();
    let mut paths = vec![dir.to_path_buf()];
    paths.extend(std::env::split_paths(&existing));
    if let Ok(joined) = std::env::join_paths(paths) {
        command.env("PATH", joined);
    }
}

/// `subcommand` is the bare tan verb (`build`/`flash`/`renode`); `image` is
/// native now (`commands::image`) and no longer routes here.
pub fn run(g: &GlobalArgs, subcommand: &str, passthrough: &[String]) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let west_cwd = context
        .west_cwd
        .clone()
        .or_else(|| context.workspace_root.clone())
        .unwrap_or_else(|| ".".to_string());

    let sdk_root = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));
    let west_bin = west_program(&west_cwd, sdk_root.as_deref());
    // `west alp-*` are extension commands discovered only from a workspace
    // manifest, so run them from the workspace topdir (holding `.west/`), not the
    // app dir. When a workspace resolves, pass the project dir as the `app_path`
    // positional the alp-* commands require — unless the caller already gave a
    // positional (e.g. `tan build <app>`). No workspace → keep the old app-dir cwd.
    let workspace = west_workspace_dir(&west_cwd, sdk_root.as_deref());
    let run_cwd = workspace
        .as_ref()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|| west_cwd.clone());
    let mut argv = west_argv(subcommand, passthrough);
    if workspace.is_some() && !passthrough.iter().any(|a| !a.starts_with('-')) {
        let app = std::fs::canonicalize(&west_cwd)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| west_cwd.clone());
        argv.insert(1, app);
    }
    let west_command = argv[0].clone();
    let data = BuildData {
        schema_version: "1".to_string(),
        west_command: west_command.clone(),
        west_cwd: run_cwd.clone(),
        args: passthrough.to_vec(),
    };
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    if g.is_json() {
        let mut cmd = Command::new(&west_bin);
        cmd.args(&argv).current_dir(&run_cwd);
        with_venv_on_path(&mut cmd, &west_bin);
        let result = cmd.output();
        let (exit, issues) = match result {
            Ok(out) if out.status.success() => (ExitCode::Success, Vec::new()),
            Ok(_) => (
                ExitCode::RuntimeFailure,
                vec![issue(
                    subcommand,
                    format!(
                        "`west {west_command}` failed; re-run without --format json to see the log."
                    ),
                )],
            ),
            Err(e) => (
                ExitCode::RuntimeFailure,
                vec![issue(subcommand, west_launch_error(&e))],
            ),
        };
        let json = Envelope::new(subcommand, project, data, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        // Text mode: stream the build live (inherited stdio).
        let mut cmd = Command::new(&west_bin);
        cmd.args(&argv).current_dir(&run_cwd);
        with_venv_on_path(&mut cmd, &west_bin);
        let status = cmd.status();
        let (exit, line) = match status {
            Ok(s) if s.success() => (ExitCode::Success, format!("{subcommand}: complete.")),
            Ok(_) => (
                ExitCode::RuntimeFailure,
                format!("{subcommand}: `west {west_command}` failed (see log above)."),
            ),
            Err(e) => (
                ExitCode::RuntimeFailure,
                format!("{subcommand}: {}", west_launch_error(&e)),
            ),
        };
        CommandRun {
            exit,
            text: vec![line],
            json: None,
        }
    }
}

/// Build an `error`-severity envelope `Issue` with code `<subcommand>.failed`.
fn issue(subcommand: &str, message: String) -> Issue {
    Issue {
        code: format!("{subcommand}.failed"),
        severity: "error".to_string(),
        message,
    }
}

/// Map a `west` launch I/O error to a user-facing message — special-casing
/// `NotFound` with a bootstrap/PATH hint.
fn west_launch_error(e: &std::io::Error) -> String {
    if e.kind() == std::io::ErrorKind::NotFound {
        "west not found on PATH — run `tan bootstrap` and ensure west is on PATH.".to_string()
    } else {
        format!("failed to launch west: {e}")
    }
}

#[cfg(test)]
mod west_argv_tests {
    use super::west_argv;

    #[test]
    fn forwards_args_after_the_west_command() {
        assert_eq!(
            west_argv(
                "build",
                &[
                    "examples/uart-echo".to_string(),
                    "--core".to_string(),
                    "m55_hp".to_string()
                ]
            ),
            vec!["alp-build", "examples/uart-echo", "--core", "m55_hp"]
        );
        assert_eq!(west_argv("renode", &[]), vec!["alp-renode"]);
        assert_eq!(
            west_argv("flash", &["--sequential".to_string()]),
            vec!["alp-flash", "--sequential"]
        );
    }
}

#[cfg(test)]
mod west_program_tests {
    use super::{venv_python, west_program, west_workspace_dir};

    fn tmp(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("alp-westp-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    fn venv_parts() -> (&'static str, &'static str) {
        if cfg!(windows) {
            ("Scripts", "west.exe")
        } else {
            ("bin", "west")
        }
    }

    fn python_parts() -> (&'static str, &'static str) {
        if cfg!(windows) {
            ("Scripts", "python.exe")
        } else {
            ("bin", "python")
        }
    }

    #[test]
    fn finds_project_tree_venv_searching_upward() {
        let root = tmp("proj");
        let (sub, exe) = venv_parts();
        let venv_bin = root.join(".venv").join(sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        let west = venv_bin.join(exe);
        std::fs::write(&west, "").unwrap();
        let cwd = root.join("a").join("b");
        std::fs::create_dir_all(&cwd).unwrap();

        // No sdk_root needed — the project-tree venv is found by the upward walk.
        assert_eq!(
            west_program(&cwd.to_string_lossy(), None),
            west.to_string_lossy()
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn resolves_bootstrap_workspace_from_sdk_root() {
        // Step 2 ($ZEPHYR_BASE) is checked before the sdk-root default; skip when
        // an activated env would take precedence so the assertion stays deterministic.
        if std::env::var_os("ZEPHYR_BASE").is_some() {
            return;
        }
        let root = tmp("sdk");
        let (sub, exe) = venv_parts();
        let sdk = root.join("sdk-root");
        std::fs::create_dir_all(&sdk).unwrap();
        // Canonical bootstrap layout: <sdk-parent>/zephyrproject/.venv/<sub>/west.
        let venv_bin = root.join("zephyrproject").join(".venv").join(sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        let west = venv_bin.join(exe);
        std::fs::write(&west, "").unwrap();
        // A cwd with no project-tree venv above it.
        let cwd = root.join("elsewhere");
        std::fs::create_dir_all(&cwd).unwrap();

        assert_eq!(
            west_program(&cwd.to_string_lossy(), Some(sdk.as_path())),
            west.to_string_lossy()
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn falls_back_to_path_west_when_nothing_resolves() {
        let root = tmp("none");
        std::fs::create_dir_all(&root).unwrap();
        // Only assert the no-signal default when no ambient $ZEPHYR_BASE could win.
        if std::env::var_os("ZEPHYR_BASE").is_none() {
            assert_eq!(west_program(&root.to_string_lossy(), None), "west");
        }
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn venv_python_finds_python_next_to_a_west_capable_venv() {
        let root = tmp("pyvenv");
        let (west_sub, west_exe) = venv_parts();
        let (py_sub, py_exe) = python_parts();
        // west_sub == py_sub on every platform ("bin" or "Scripts") — a real venv
        // has both binaries in the same dir; write via each name to prove
        // `venv_python` isn't just reusing `west_program`'s west path.
        let venv_bin = root.join(".venv").join(west_sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        std::fs::write(venv_bin.join(west_exe), "").unwrap();
        assert_eq!(west_sub, py_sub);
        let python = root.join(".venv").join(py_sub).join(py_exe);
        std::fs::write(&python, "").unwrap();
        let cwd = root.join("a").join("b");
        std::fs::create_dir_all(&cwd).unwrap();

        assert_eq!(
            venv_python(&cwd.to_string_lossy(), None),
            Some(python.to_string_lossy().into_owned())
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn venv_python_is_none_when_no_west_capable_venv_resolves() {
        let root = tmp("pyvenv-none");
        std::fs::create_dir_all(&root).unwrap();
        // Only assert when no ambient $ZEPHYR_BASE could win — mirrors the
        // `falls_back_to_path_west_when_nothing_resolves` west_program test.
        if std::env::var_os("ZEPHYR_BASE").is_none() {
            assert_eq!(venv_python(&root.to_string_lossy(), None), None);
        }
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn venv_python_is_none_when_west_capable_venv_lacks_python() {
        // A west-capable venv resolves (find_workspace_venv's west-gate passes),
        // but its python binary is absent — venv_python must return None, not a
        // path to a file that doesn't exist (else invoke_sdk_emit would spawn a
        // nonexistent python).
        let root = tmp("pyvenv-nopy");
        let (sub, west_exe) = venv_parts();
        let venv_bin = root.join(".venv").join(sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        std::fs::write(venv_bin.join(west_exe), "").unwrap(); // west present, python absent
        if std::env::var_os("ZEPHYR_BASE").is_none() {
            assert_eq!(venv_python(&root.to_string_lossy(), None), None);
        }
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn workspace_dir_resolves_topdir_from_sdk_root() {
        // Step 2 ($ZEPHYR_BASE) precedes the sdk-root layouts; skip when set.
        if std::env::var_os("ZEPHYR_BASE").is_some() {
            return;
        }
        let root = tmp("ws");
        // alp-sdk#782 layout: workspace topdir = the SDK checkout's parent.
        let workspace = root.join("workspace");
        let sdk = workspace.join("alp-sdk");
        std::fs::create_dir_all(&sdk).unwrap();
        std::fs::create_dir_all(workspace.join(".west")).unwrap();
        let cwd = root.join("external-app"); // not inside any workspace
        std::fs::create_dir_all(&cwd).unwrap();

        assert_eq!(
            west_workspace_dir(&cwd.to_string_lossy(), Some(sdk.as_path())),
            Some(workspace)
        );
        std::fs::remove_dir_all(&root).unwrap();
    }
}
