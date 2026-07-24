// SPDX-License-Identifier: Apache-2.0
//! The spawning half of `tan bootstrap`: prerequisite probes, venv creation,
//! `west init`/`update`/`zephyr-export` + the #769 legibility guard, and the
//! pip installs.
//!
//! Every message string here is lifted verbatim from the parity oracles
//! (`alp-sdk/scripts/bootstrap.sh` and `alp-sdk/scripts/bootstrap.ps1`); only
//! the `bootstrap: ` line prefix is tan's (it replaces the scripts' coloured
//! `[bootstrap]` tag, matching this CLI's house style). All decision logic is
//! in `tan_core::bootstrap` — this file only does IO.

use std::path::{Path, PathBuf};
use std::process::Command;

use tan_core::bootstrap::{VenvLayout, WEST_REQUIREMENT, venv_layout};

use super::capture_tail;
use crate::util::{HostPython, MIN_PYTHON, command_on_path, probe_host_python};

/// Progress reporter. Text mode streams live to stderr (pip/west take minutes,
/// so a summary at the end would look like a hang); JSON mode stays silent so
/// the single stdout envelope is the only output.
pub(super) struct Log {
    /// Whether the caller asked for `--format json`.
    pub json: bool,
}

impl Log {
    /// Emit one progress line (the scripts' `info`/`ok`/`warn` — all three land
    /// on the same stream here, which is where `CommandRun::text` goes too).
    pub(super) fn line(&self, message: &str) {
        if !self.json {
            eprintln!("bootstrap: {message}");
        }
    }
}

/// Child-process launcher shared by every step.
pub(super) struct Runner {
    /// JSON mode: capture stdout/stderr instead of inheriting them.
    pub json: bool,
    /// Drop `$ZEPHYR_BASE` from every child. Set when workspace selection
    /// rejected the ambient value — both scripts `unset`/`$null` it so a stale
    /// tree cannot hijack `west init` (bootstrap.sh:158/161).
    pub clear_zephyr_base: bool,
}

impl Runner {
    /// Run `cmd` to completion. `Err` carries whatever detail is recoverable:
    /// the captured tail in JSON mode, a launch error in either mode, and `""`
    /// in text mode where the child's own log already streamed to the terminal.
    pub(super) fn run(&self, cmd: &mut Command) -> Result<(), String> {
        if self.clear_zephyr_base {
            cmd.env_remove("ZEPHYR_BASE");
        }
        let program = cmd.get_program().to_string_lossy().into_owned();
        if self.json {
            match cmd.output() {
                Ok(out) if out.status.success() => Ok(()),
                Ok(out) => Err(capture_tail(&out.stdout, &out.stderr)),
                Err(e) => Err(format!("failed to launch {program}: {e}")),
            }
        } else {
            match cmd.status() {
                Ok(status) if status.success() => Ok(()),
                Ok(_) => Err(String::new()),
                Err(e) => Err(format!("failed to launch {program}: {e}")),
            }
        }
    }

    /// Run `cmd` capturing its output in BOTH modes — for the `west help`
    /// legibility probe, which reads the text rather than showing it.
    pub(super) fn capture(&self, cmd: &mut Command) -> String {
        if self.clear_zephyr_base {
            cmd.env_remove("ZEPHYR_BASE");
        }
        match cmd.output() {
            Ok(out) => format!(
                "{}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            ),
            Err(_) => String::new(),
        }
    }
}

/// Compose a fatal message: the script's own `die` text plus whatever detail
/// [`Runner::run`] recovered. Text mode usually has none (the log already
/// streamed), so the bare script message is what the user sees there.
pub(super) fn die(base: &str, detail: String) -> String {
    if detail.trim().is_empty() {
        base.to_string()
    } else {
        format!("{base}: {detail}")
    }
}

/// Render `path` with this platform's separator. The `ProjectContext` carries
/// forward-slash paths on every OS, which would print as `C:/dev/ws\zephyr` in
/// the Windows copy-paste blocks.
pub(super) fn native(path: &Path) -> String {
    let rendered = path.to_string_lossy().into_owned();
    if cfg!(windows) {
        rendered.replace('/', "\\")
    } else {
        rendered
    }
}

/// One Windows prerequisite and the `winget` one-liner printed when it is
/// missing (bootstrap.ps1:89-94). Installing system packages stays the user's
/// call — the script prints the command rather than running it, and so do we.
struct Prereq {
    /// Executable name probed on PATH.
    name: &'static str,
    /// The exact `winget install` line to print when it is absent.
    hint: &'static str,
}

/// Windows prerequisites, in the script's order (bootstrap.ps1:89-94).
const WINDOWS_PREREQS: &[Prereq] = &[
    Prereq {
        name: "git",
        hint: "winget install -e --id Git.Git",
    },
    Prereq {
        name: "cmake",
        hint: "winget install -e --id Kitware.CMake",
    },
    Prereq {
        name: "python",
        hint: "winget install -e --id Python.Python.3.12",
    },
    Prereq {
        name: "ninja",
        hint: "winget install -e --id Ninja-build.Ninja",
    },
];

/// POSIX prerequisites (bootstrap.sh:106). Deliberately SHORTER than the
/// Windows list — no `ninja`, and no Python version check.
///
/// That asymmetry is parity, not an oversight: bootstrap.ps1:109-118 enforces
/// Python >= 3.10 with its own Store-alias and too-old messages, while
/// bootstrap.sh checks only that `git`/`cmake`/`python3` resolve and stops
/// there. Keep it. (Every path that actually runs an SDK script still applies
/// `crate::util::python_too_old` on both platforms.)
const POSIX_PREREQS: &[&str] = &["git", "cmake", "python3"];

/// `Get-Command <name>` equivalent, with one deliberate widening: `python`
/// counts as present when the `py` launcher is installed, because
/// `tan_core::bootstrap::python_candidates` leads with `py -3` and a
/// launcher-only machine is a perfectly good Windows Python host.
/// bootstrap.ps1:92 checks the bare name only, so this can only make bootstrap
/// succeed where the script would have refused — never the reverse.
fn windows_prereq_present(name: &str) -> bool {
    command_on_path(name) || (name == "python" && command_on_path("py"))
}

/// Prerequisite gate. Returns the host interpreter the workspace venv will be
/// created with; `Err` carries the fatal message lines verbatim.
pub(super) fn check_prerequisites(is_windows: bool) -> Result<HostPython, Vec<String>> {
    if is_windows {
        let missing: Vec<&Prereq> = WINDOWS_PREREQS
            .iter()
            .filter(|p| !windows_prereq_present(p.name))
            .collect();
        if !missing.is_empty() {
            let mut lines = vec!["Missing required tools:".to_string()];
            lines.extend(
                missing
                    .iter()
                    .map(|p| format!("  {}  ->  {}", p.name, p.hint)),
            );
            lines.push("Install the tools above (then reopen PowerShell) and re-run.".to_string());
            return Err(lines);
        }
        // Python >= 3.10 (dataclass slots, `X | None` unions in the tooling).
        let Some(python) = probe_host_python() else {
            return Err(vec![
                "python did not run (Windows Store alias?).  Install real Python: winget install \
                 -e --id Python.Python.3.12, reopen PowerShell, re-run."
                    .to_string(),
            ]);
        };
        if python.version < MIN_PYTHON {
            return Err(vec![format!(
                "Python {}.{} found; the SDK tooling needs >= {}.{} (winget install -e --id \
                 Python.Python.3.12).",
                python.version.0, python.version.1, MIN_PYTHON.0, MIN_PYTHON.1
            )]);
        }
        return Ok(python);
    }

    let missing: Vec<&str> = POSIX_PREREQS
        .iter()
        .copied()
        .filter(|bin| !command_on_path(bin))
        .collect();
    if !missing.is_empty() {
        return Err(vec![format!(
            "Missing required tools: {}.  Install them and re-run.",
            missing.join(" ")
        )]);
    }
    // No version check here — see POSIX_PREREQS. The only extra failure this
    // adds over bootstrap.sh is an interpreter that is on PATH but cannot run,
    // which the script would have hit one step later at `python3 -m venv`.
    probe_host_python().ok_or_else(|| {
        vec![
            "python3 is on PATH but did not run.  Install a working Python 3 and re-run."
                .to_string(),
        ]
    })
}

/// The resolved workspace paths every step works against.
pub(super) struct Workspace<'a> {
    /// Native Windows host (selects the venv layout and the message variants).
    pub is_windows: bool,
    /// The alp-sdk checkout — `REPO_ROOT`, and `west init -l`'s argument.
    pub repo_root: &'a Path,
    /// The west topdir — `WORKSPACE_DIR`, the SDK checkout's parent (#769).
    pub workspace_dir: &'a Path,
    /// `<WORKSPACE_DIR>/.venv`.
    pub venv_dir: &'a Path,
}

impl Workspace<'_> {
    fn layout(&self) -> VenvLayout {
        venv_layout(self.is_windows)
    }

    /// The workspace venv's interpreter.
    pub(super) fn venv_python(&self) -> PathBuf {
        let layout = self.layout();
        self.venv_dir.join(layout.bin_dir).join(layout.python)
    }

    /// The workspace venv's `west` launcher.
    pub(super) fn venv_west(&self) -> PathBuf {
        let layout = self.layout();
        self.venv_dir.join(layout.bin_dir).join(layout.west)
    }

    /// A `west` invocation rooted at the workspace topdir, with the venv's
    /// `Scripts`/`bin` prepended to PATH so the nested `west build`/`bitbake`
    /// spawns resolve the SAME west (#106).
    fn west(&self) -> Command {
        let west = self.venv_west();
        let mut cmd = Command::new(&west);
        cmd.current_dir(self.workspace_dir);
        crate::commands::build::workspace::with_venv_on_path(&mut cmd, &west.to_string_lossy());
        cmd
    }
}

/// Create (or reuse) the workspace venv and refresh `pip`/`wheel` in it.
///
/// Everything — west, the Zephyr requirements, the SDK extras — installs into
/// this workspace-local venv, never the system interpreter / `--user` /
/// `--break-system-packages` (alp-sdk#93: a half-removed system `packaging`
/// once broke `west init`, and a global west couples the build to the host
/// interpreter's state). Idempotent: an existing venv is reused.
pub(super) fn ensure_venv(
    ws: &Workspace,
    log: &Log,
    runner: &Runner,
    host: &HostPython,
) -> Result<(), String> {
    let _ = std::fs::create_dir_all(ws.workspace_dir);
    // bootstrap.sh:178 accepts EITHER layout, so a venv created from git-bash
    // (`Scripts/`) is reused rather than clobbered by a native-Windows run.
    let present = ws.venv_dir.join("bin").join("python").is_file()
        || ws.venv_dir.join("Scripts").join("python.exe").is_file();
    if present {
        log.line(&format!(
            "Workspace venv already present at {}",
            native(ws.venv_dir)
        ));
    } else {
        log.line(&format!(
            "Creating workspace venv at {}",
            native(ws.venv_dir)
        ));
        let mut create = host.command();
        create.arg("-m").arg("venv").arg(ws.venv_dir);
        runner.run(&mut create).map_err(|detail| {
            die(
                &format!("{} -m venv {} failed", host.display(), native(ws.venv_dir)),
                detail,
            )
        })?;
    }
    let mut upgrade = Command::new(ws.venv_python());
    upgrade.args(["-m", "pip", "install", "--upgrade", "-q", "pip", "wheel"]);
    if runner.run(&mut upgrade).is_err() {
        log.line("pip/wheel upgrade reported a problem");
    }
    Ok(())
}

/// Install west into the venv, then `west init -l` / `west update` /
/// `west zephyr-export`, then the #769 legibility guard. `reuse` short-circuits
/// all of it (including the guard), exactly as both scripts do for a workspace
/// adopted from `$ZEPHYR_BASE`.
pub(super) fn west_phase(
    ws: &Workspace,
    log: &Log,
    runner: &Runner,
    reuse: bool,
) -> Result<(), String> {
    // west into the venv (NOT global / --user) so the system interpreter can't
    // break it.
    if !ws.venv_west().is_file() {
        log.line("Installing west into the workspace venv");
        let mut install = Command::new(ws.venv_python());
        install.args(["-m", "pip", "install", "--upgrade", "-q", WEST_REQUIREMENT]);
        runner
            .run(&mut install)
            .map_err(|detail| die("pip install west (venv) failed", detail))?;
    }

    if reuse {
        log.line(
            "Existing workspace reused -- skipping 'west init' / 'west update' (left untouched)",
        );
        return Ok(());
    }

    let workspace = native(ws.workspace_dir);
    if !ws.workspace_dir.join(".west").is_dir() {
        log.line(&format!(
            "Creating alp-sdk workspace at {workspace} (alp-sdk's west.yml is the manifest; takes \
             a few minutes)"
        ));
        // -l makes alp-sdk (REPO_ROOT) the manifest repo; topdir = its parent =
        // WORKSPACE_DIR. Zephyr (pinned in alp-sdk's west.yml) + HALs + extras
        // are fetched by `west update`. alp-sdk's self.west-commands then
        // exposes the alp-* extension commands in this workspace (#769).
        let mut init = ws.west();
        init.arg("init").arg("-l").arg(ws.repo_root);
        runner
            .run(&mut init)
            .map_err(|detail| die("west init -l failed", detail))?;
        // Only bootstrap.sh mentions the cold-cache size on the fresh path.
        log.line(if ws.is_windows {
            "Running 'west update' (shallow + narrow)"
        } else {
            "Running 'west update' (shallow + narrow; ~30 MB on a cold cache)"
        });
    } else {
        log.line(&format!(
            "alp-sdk workspace already initialised at {workspace}"
        ));
        log.line("Running 'west update' (shallow + narrow)");
    }

    let mut update = ws.west();
    update.args(["update", "--narrow", "-o=--depth=1"]);
    runner
        .run(&mut update)
        .map_err(|detail| die("west update failed", detail))?;
    // Failure deliberately IGNORED (`|| true` in bootstrap.sh:211, no rc check
    // in bootstrap.ps1:213).
    let mut export = ws.west();
    export.arg("zephyr-export");
    let _ = runner.run(&mut export);

    // Legibility guard (#769): fail at bootstrap time -- not at first
    // `tan build` -- if the workspace manifest doesn't register the alp-*
    // extension commands.
    let mut help = ws.west();
    help.arg("help");
    if !runner.capture(&mut help).contains("alp-migrate") {
        return Err(format!(
            "workspace at {workspace} does not register 'west alp-migrate' -- its manifest is not \
             alp-sdk's west.yml (#769). Check 'west -C {workspace} config manifest.path'."
        ));
    }
    log.line(&format!(
        "alp-* extension commands registered ('west alp-migrate' resolves in {workspace})"
    ));
    Ok(())
}

/// The Python dependency installs, all into the SAME workspace venv and all
/// NON-FATAL (a warn each), matching both scripts.
pub(super) fn pip_phase(ws: &Workspace, log: &Log, runner: &Runner) {
    let vpy = ws.venv_python();
    let requirements = ws
        .workspace_dir
        .join("zephyr")
        .join("scripts")
        .join("requirements.txt");
    if requirements.is_file() {
        log.line("Installing Zephyr Python requirements into the venv");
        let mut cmd = Command::new(&vpy);
        cmd.args(["-m", "pip", "install", "-q", "-r"])
            .arg(&requirements);
        if runner.run(&mut cmd).is_err() {
            log.line("Zephyr requirements install reported a problem -- check manually");
        }
    }
    // SDK-side extras: alp_project.py needs jsonschema; the MCUboot dev-key
    // script needs imgtool.
    log.line("Installing alp-sdk Python extras into the venv (jsonschema, imgtool)");
    let mut extras = Command::new(&vpy);
    extras.args(["-m", "pip", "install", "-q", "jsonschema", "imgtool"]);
    if runner.run(&mut extras).is_err() {
        log.line("alp-sdk extras install reported a problem -- check manually");
    }
    // tan's Python backend (alp_cli) -- editable, so a `git pull` in the
    // checkout updates the backend in place.
    log.line(&format!(
        "Installing the tan CLI's Python backend into the venv (pip install -e {})",
        native(ws.repo_root)
    ));
    let mut editable = Command::new(&vpy);
    editable
        .args(["-m", "pip", "install", "-q", "-e"])
        .arg(ws.repo_root);
    if runner.run(&mut editable).is_err() {
        log.line("alp_cli editable install reported a problem -- check manually");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn die_appends_a_detail_only_when_there_is_one() {
        // Text mode: the child's log already streamed, so the bare script
        // message is what the user should see -- no dangling colon.
        assert_eq!(
            die("west update failed", String::new()),
            "west update failed"
        );
        assert_eq!(
            die("west update failed", "  \n ".to_string()),
            "west update failed"
        );
        assert_eq!(
            die(
                "west update failed",
                "fatal: not a git repository".to_string()
            ),
            "west update failed: fatal: not a git repository"
        );
    }

    #[test]
    fn venv_paths_follow_the_shared_layout() {
        let repo = Path::new("/ws/alp-sdk");
        let top = Path::new("/ws");
        let venv = Path::new("/ws/.venv");
        let posix = Workspace {
            is_windows: false,
            repo_root: repo,
            workspace_dir: top,
            venv_dir: venv,
        };
        assert!(posix.venv_python().ends_with("bin/python"));
        assert!(posix.venv_west().ends_with("bin/west"));

        let win = Workspace {
            is_windows: true,
            repo_root: repo,
            workspace_dir: top,
            venv_dir: venv,
        };
        assert!(win.venv_python().ends_with("Scripts/python.exe"));
        assert!(win.venv_west().ends_with("Scripts/west.exe"));
    }
}
