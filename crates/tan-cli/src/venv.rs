// SPDX-License-Identifier: Apache-2.0
//! Workspace-venv resolution, shared by every command that spawns a tool the
//! `tan bootstrap` venv owns.
//!
//! `west` normally lives ONLY inside the bootstrapped venv — nothing activates
//! it for a GUI-launched editor, so the ambient PATH has no `west` at all. This
//! module is what lets `tan` find it anyway. It used to be private to
//! `commands::build`, which is why `tan build` worked on such a host while `tan
//! flash` failed every Zephyr slice with "needs one of [\"west\"] on PATH; none
//! found" (#59).

use std::path::{Path, PathBuf};
use std::process::Command;

use tan_core::bootstrap::venv_layout;

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
/// every caller needs. `None` when none resolve (CI, an activated venv, the
/// contract harness).
fn find_workspace_venv(start: &str, sdk_root: Option<&Path>) -> Option<PathBuf> {
    let layout = venv_layout(cfg!(windows));
    let has_west = |venv: &Path| venv.join(layout.bin_dir).join(layout.west).is_file();

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

/// The west-capable workspace venv's executable directory (`bin`, `Scripts` on
/// Windows), if one resolves. This is the handle a spawning command needs: the
/// directory to look tool names up in, and the directory to put on the child's
/// PATH. `None` when no west-capable venv resolves.
pub(crate) fn venv_bin_dir(start: &str, sdk_root: Option<&Path>) -> Option<PathBuf> {
    let sub = venv_layout(cfg!(windows)).bin_dir;
    find_workspace_venv(start, sdk_root).map(|venv| venv.join(sub))
}

/// Resolve `tool` INSIDE an already-located venv bin dir, returning its absolute
/// path when it is really a file there. `.exe` is appended on Windows unless the
/// caller already spelled it. `None` means "this venv does not provide that
/// tool" — the caller then keeps its PATH behaviour instead of spawning a path
/// that doesn't exist.
pub(crate) fn tool_in_venv(bin: &Path, tool: &str) -> Option<String> {
    let name = if cfg!(windows) && !tool.to_ascii_lowercase().ends_with(".exe") {
        format!("{tool}.exe")
    } else {
        tool.to_string()
    };
    let candidate = bin.join(name);
    candidate
        .is_file()
        .then(|| candidate.to_string_lossy().into_owned())
}

/// Resolve the `west` program to launch: the west-capable workspace venv's
/// `west` binary (see `find_workspace_venv`), falling back to `"west"` on PATH
/// when none resolve (CI, an activated venv, the contract harness) — behaving
/// exactly as before in those environments.
pub(crate) fn west_program(start: &str, sdk_root: Option<&Path>) -> String {
    venv_bin_dir(start, sdk_root)
        .and_then(|bin| tool_in_venv(&bin, "west"))
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
pub(crate) fn venv_python(start: &str, sdk_root: Option<&Path>) -> Option<String> {
    venv_bin_dir(start, sdk_root).and_then(|bin| tool_in_venv(&bin, "python"))
}

/// When `tool` is a resolved venv program (an absolute path), prepend its
/// directory to `command`'s `PATH`. `west alp-*` spawns `alp_orchestrate`, which
/// spawns nested `west build`/`bitbake` that resolve `west` via PATH — without
/// this, they fail with "west not found in PATH" and silently skip the slice
/// unless the user activated the venv. A bare `"west"` (PATH fallback) is left as-is.
pub(crate) fn with_venv_on_path(command: &mut Command, tool: &str) {
    let bin = Path::new(tool);
    if !bin.is_absolute() {
        return;
    }
    let Some(dir) = bin.parent() else {
        return;
    };
    prepend_path(command, dir);
}

/// Put `dir` at the FRONT of `command`'s `PATH`.
///
/// Reads whatever PATH is ALREADY staged on `command` — the plan's own
/// `env`/`env_append_path` may have pinned one via `.envs(env)` before this
/// runs — and falls back to the parent process's PATH only when the assembled
/// env carried none. Reading `std::env::var_os("PATH")` unconditionally (the
/// old code) discarded a plan-pinned PATH outright: `Command::envs` had already
/// set it on `command`, and this then overwrote it with the CLI's own ambient
/// PATH, silently dropping e.g. a pinned cross-toolchain directory.
pub(crate) fn prepend_path(command: &mut Command, dir: &Path) {
    let existing = command
        .get_envs()
        .find(|(k, _)| *k == std::ffi::OsStr::new("PATH"))
        .and_then(|(_, v)| v.map(|v| v.to_os_string()))
        .or_else(|| std::env::var_os("PATH"))
        .unwrap_or_default();
    let mut paths = vec![dir.to_path_buf()];
    paths.extend(std::env::split_paths(&existing));
    if let Ok(joined) = std::env::join_paths(paths) {
        command.env("PATH", joined);
    }
}

#[cfg(test)]
mod west_program_tests {
    use super::{tool_in_venv, venv_bin_dir, venv_python, west_program};

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
    fn tool_in_venv_resolves_only_files_that_exist() {
        // The gate `flash` relies on: a tool the venv really provides resolves to
        // an absolute path; one it doesn't is `None`, so the caller keeps its PATH
        // behaviour instead of spawning a binary that isn't there.
        let root = tmp("toolvenv");
        let (sub, exe) = venv_parts();
        let venv_bin = root.join(".venv").join(sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        std::fs::write(venv_bin.join(exe), "").unwrap();
        let bin = venv_bin_dir(&root.to_string_lossy(), None).expect("west-capable venv");

        assert_eq!(
            tool_in_venv(&bin, "west"),
            Some(venv_bin.join(exe).to_string_lossy().into_owned())
        );
        assert_eq!(tool_in_venv(&bin, "openocd"), None);
        std::fs::remove_dir_all(&root).unwrap();
    }
}

#[cfg(test)]
mod with_venv_on_path_tests {
    use super::with_venv_on_path;
    use std::process::Command;

    fn path_env(cmd: &Command) -> String {
        cmd.get_envs()
            .find(|(k, _)| *k == std::ffi::OsStr::new("PATH"))
            .and_then(|(_, v)| v)
            .expect("PATH must be set")
            .to_string_lossy()
            .into_owned()
    }

    #[test]
    fn prepends_onto_a_path_already_staged_on_the_command_not_the_process_path() {
        // Regression: `with_venv_on_path` used to unconditionally read
        // `std::env::var_os("PATH")` (the CLI's OWN process env), discarding
        // whatever PATH the plan's `env`/`env_append_path` had already staged
        // on `command` via `.envs(env)` moments earlier — e.g. a pinned cross
        // toolchain directory the plan needs ahead of anything else.
        let mut cmd = Command::new("does-not-matter");
        cmd.env("PATH", "/plan/toolchain/bin");
        let venv_west = if cfg!(windows) {
            r"C:\ws\.venv\Scripts\west.exe"
        } else {
            "/ws/.venv/bin/west"
        };
        with_venv_on_path(&mut cmd, venv_west);

        let path = path_env(&cmd);
        let venv_dir = std::path::Path::new(venv_west)
            .parent()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        assert!(
            path.starts_with(&venv_dir),
            "venv dir must lead PATH, got: {path}"
        );
        assert!(
            path.contains("/plan/toolchain/bin"),
            "the plan-staged PATH segment must survive, got: {path}"
        );
    }

    #[test]
    fn falls_back_to_process_path_when_command_has_none_staged() {
        let mut cmd = Command::new("does-not-matter");
        let venv_west = if cfg!(windows) {
            r"C:\ws\.venv\Scripts\west.exe"
        } else {
            "/ws/.venv/bin/west"
        };
        with_venv_on_path(&mut cmd, venv_west);
        // No panic / no-op when nothing was staged — PATH is still set to at
        // least the venv dir (plus whatever the test process's own PATH is).
        let path = path_env(&cmd);
        let venv_dir = std::path::Path::new(venv_west)
            .parent()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        assert!(path.starts_with(&venv_dir), "got: {path}");
    }

    #[test]
    fn a_bare_path_tool_is_left_untouched() {
        let mut cmd = Command::new("does-not-matter");
        cmd.env("PATH", "/plan/toolchain/bin");
        with_venv_on_path(&mut cmd, "west"); // not absolute -> no-op
        assert_eq!(path_env(&cmd), "/plan/toolchain/bin");
    }
}
