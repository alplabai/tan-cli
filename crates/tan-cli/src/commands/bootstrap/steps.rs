// SPDX-License-Identifier: Apache-2.0
//! The spawning half of `tan bootstrap`: prerequisite probes, venv creation,
//! `west init`/`update`/`zephyr-export` + the #769 legibility guard, and the
//! pip installs.
//!
//! Message strings and step ORDER come from the parity oracles
//! (`alp-sdk/scripts/bootstrap.sh` + `bootstrap.ps1`); only the `bootstrap: `
//! line prefix is tan's (it replaces the scripts' coloured `[bootstrap]` tag,
//! matching this CLI's house style). The FACTS those steps act on — tool lists,
//! argv, pip specs, paths — come from `metadata/bootstrap.json` via
//! [`BootstrapFacts`], never from literals here (alp-sdk#917).
//!
//! Citations name an ANCHOR (function/variable/section), never a line number:
//! the oracles have moved twice under this port.

use std::path::{Path, PathBuf};
use std::process::Command;

use tan_core::bootstrap::{BootstrapFacts, Tokens, venv_exe_names};

use super::capture_tail;
use crate::envelope::Issue;
use crate::util::{HostPython, command_on_path, probe_host_python};

/// Progress reporter. Text mode streams live to stderr (pip/west take minutes,
/// so a summary at the end would look like a hang); JSON mode stays silent so
/// the single stdout envelope is the only output.
///
/// Warnings are RECORDED as well as printed. They used to be print-only, which
/// in JSON mode meant a run where the Zephyr requirements, the SDK extras AND
/// the editable install all failed still emitted `ok:true, exitCode:0,
/// issues:[]` — every non-fatal failure silently swallowed.
pub(super) struct Log {
    /// Whether the caller asked for `--format json`.
    json: bool,
    /// `(issue code suffix, message)` for every warning raised so far.
    warnings: Vec<(String, String)>,
}

impl Log {
    /// A reporter for the given output mode.
    pub(super) fn new(json: bool) -> Self {
        Log {
            json,
            warnings: Vec::new(),
        }
    }

    /// Emit one progress line (the scripts' `info`/`ok`).
    pub(super) fn line(&self, message: &str) {
        if !self.json {
            eprintln!("bootstrap: {message}");
        }
    }

    /// Emit AND record a non-fatal warning (the scripts' `warn`). `code`
    /// becomes the envelope issue code `bootstrap.<code>`.
    pub(super) fn warn(&mut self, code: &str, message: &str) {
        self.line(message);
        self.warnings.push((code.to_string(), message.to_string()));
    }

    /// Drain the recorded warnings as `warning`-severity envelope issues.
    pub(super) fn take_issues(&mut self) -> Vec<Issue> {
        self.warnings
            .drain(..)
            .map(|(code, message)| Issue {
                code: format!("bootstrap.{code}"),
                severity: "warning".to_string(),
                message,
            })
            .collect()
    }
}

/// Child-process launcher shared by every step.
pub(super) struct Runner {
    /// JSON mode: capture stdout/stderr instead of inheriting them.
    pub json: bool,
    /// Drop `$ZEPHYR_BASE` from every child. Set when workspace selection
    /// rejected the ambient value — both scripts `unset`/`$null` it so a stale
    /// tree cannot hijack `west init`.
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

/// The `winget install` one-liner printed for a missing Windows prerequisite
/// (`bootstrap.ps1`'s `$Prereqs`). The TOOL LIST is a manifest fact
/// (`prerequisites.windows`); these hints are not in the manifest, so they stay
/// keyed by tool name here. An unlisted tool gets a generic line rather than
/// being dropped from the report.
fn winget_hint(tool: &str) -> String {
    match tool {
        "git" => "winget install -e --id Git.Git".to_string(),
        "cmake" => "winget install -e --id Kitware.CMake".to_string(),
        "python" => "winget install -e --id Python.Python.3.12".to_string(),
        "ninja" => "winget install -e --id Ninja-build.Ninja".to_string(),
        other => format!("install `{other}` and put it on PATH"),
    }
}

/// `Get-Command <name>` / `command -v <name>` equivalent, with one deliberate
/// widening on Windows: `python` counts as present when the `py` launcher is
/// installed, because `tan_core::bootstrap::python_candidates` leads with
/// `py -3` and a launcher-only machine is a perfectly good Windows Python host.
/// `bootstrap.ps1`'s `$Prereqs` loop checks the bare name only, so this can
/// only make bootstrap succeed where the script would have refused.
fn prereq_present(tool: &str, is_windows: bool) -> bool {
    command_on_path(tool) || (is_windows && tool == "python" && command_on_path("py"))
}

/// Prerequisite gate, over the manifest's per-OS `prerequisites` list. Returns
/// the host interpreter the workspace venv will be created with; `Err` carries
/// the fatal message lines verbatim.
///
/// The POSIX and Windows lists genuinely differ (Windows adds `ninja`) and only
/// Windows enforces `pythonMinVersion` — `bootstrap.sh` has no version check at
/// all. The manifest records that asymmetry faithfully ("recorded faithfully,
/// not silently unified") and so does this: the floor below is applied on the
/// Windows branch only. Every path that actually RUNS an SDK script still
/// applies `crate::util::python_too_old` on both platforms.
pub(super) fn check_prerequisites(
    facts: &BootstrapFacts,
    is_windows: bool,
) -> Result<HostPython, Vec<String>> {
    let missing: Vec<&String> = facts
        .prerequisites(is_windows)
        .iter()
        .filter(|tool| !prereq_present(tool, is_windows))
        .collect();

    if is_windows {
        if !missing.is_empty() {
            let mut lines = vec!["Missing required tools:".to_string()];
            lines.extend(
                missing
                    .iter()
                    .map(|tool| format!("  {tool}  ->  {}", winget_hint(tool))),
            );
            lines.push("Install the tools above (then reopen PowerShell) and re-run.".to_string());
            return Err(lines);
        }
        // Python >= pythonMinVersion (dataclass slots, `X | None` unions).
        let (min_major, min_minor) = facts.python_min_version;
        // Probe against the MANIFEST's floor, not tan's compiled-in one: the
        // check just below enforces the manifest's, and probing to a lower bar
        // would stop at the first candidate that clears 3.10 (`py -3`, often
        // the launcher's older default) and then fail the host for it, while a
        // newer `python` sat one candidate down the list.
        let Some(python) = probe_host_python(facts.python_min_version) else {
            return Err(vec![
                "python did not run (Windows Store alias?).  Install real Python: winget install \
                 -e --id Python.Python.3.12, reopen PowerShell, re-run."
                    .to_string(),
            ]);
        };
        if python.version < (min_major, min_minor) {
            return Err(vec![format!(
                "Python {}.{} found; the SDK tooling needs >= {min_major}.{min_minor} (winget \
                 install -e --id Python.Python.3.12).",
                python.version.0, python.version.1
            )]);
        }
        return Ok(python);
    }

    if !missing.is_empty() {
        let names: Vec<&str> = missing.iter().map(|t| t.as_str()).collect();
        return Err(vec![format!(
            "Missing required tools: {}.  Install them and re-run.",
            names.join(" ")
        )]);
    }
    // No version check here — see the doc comment. The manifest floor is still
    // what the probe PREFERS (it only picks between candidates that ran; it
    // never refuses), so this branch cannot fail on version. The only extra
    // failure this adds over bootstrap.sh is an interpreter on PATH that cannot
    // run, which the script would have hit one step later at `python3 -m venv`.
    probe_host_python(facts.python_min_version).ok_or_else(|| {
        vec![
            "python3 is on PATH but did not run.  Install a working Python 3 and re-run."
                .to_string(),
        ]
    })
}

/// The resolved workspace paths every step works against.
pub(super) struct Workspace<'a> {
    /// Native Windows host (selects message variants and the default venv layout).
    pub is_windows: bool,
    /// The workspace-assembly facts (manifest, or the documented fallback).
    pub facts: &'a BootstrapFacts,
    /// The alp-sdk checkout — `REPO_ROOT`, and `west init -l`'s argument.
    pub repo_root: &'a Path,
    /// The west topdir — `WORKSPACE_DIR`, the SDK checkout's parent (#769).
    pub workspace_dir: &'a Path,
    /// `<WORKSPACE_DIR>/<venv.dirName>`.
    pub venv_dir: &'a Path,
}

/// The venv executables, resolved against the bin directory that actually
/// exists on disk.
pub(super) struct VenvBin {
    /// The venv's interpreter.
    pub python: PathBuf,
    /// The venv's `west` launcher.
    pub west: PathBuf,
    /// The bin sub-directory that won (`bin` or `Scripts`) — the closing
    /// activation hint must name the real one.
    pub bin_dir: String,
}

impl Workspace<'_> {
    /// Resolve the venv's executables by which bin directory EXISTS, POSIX name
    /// first — `bootstrap.sh`'s `VBIN` assignment does exactly this.
    ///
    /// The bug this fixes: the presence check accepts EITHER layout (so a
    /// git-bash-created `Scripts/` venv is reused, not clobbered), but the
    /// executables used to be derived from the HOST instead. On a POSIX host
    /// that meant creation was skipped and then `bin/python` — which does not
    /// exist — was spawned, turning a reusable venv into a FATAL
    /// `pip install west (venv) failed`.
    fn venv_bin(&self) -> VenvBin {
        let posix = self.facts.venv_posix_bin_dir.as_str();
        let windows = self.facts.venv_windows_bin_dir.as_str();
        let bin_dir = if self.venv_dir.join(posix).is_dir() {
            posix
        } else if self.venv_dir.join(windows).is_dir() {
            windows
        } else {
            // Nothing on disk yet — assume this host's own layout.
            self.facts.venv_bin_dir(self.is_windows)
        };
        let names = venv_exe_names(bin_dir, self.facts);
        VenvBin {
            python: self.venv_dir.join(bin_dir).join(names.python),
            west: self.venv_dir.join(bin_dir).join(names.west),
            bin_dir: bin_dir.to_string(),
        }
    }

    /// Whether a venv interpreter already exists under EITHER layout.
    fn venv_present(&self) -> bool {
        let posix = venv_exe_names(&self.facts.venv_posix_bin_dir, self.facts);
        let windows = venv_exe_names(&self.facts.venv_windows_bin_dir, self.facts);
        self.venv_dir
            .join(&self.facts.venv_posix_bin_dir)
            .join(posix.python)
            .is_file()
            || self
                .venv_dir
                .join(&self.facts.venv_windows_bin_dir)
                .join(windows.python)
                .is_file()
    }

    /// A `west` invocation rooted at the workspace topdir, with the venv's bin
    /// dir prepended to PATH so the nested `west build`/`bitbake` spawns
    /// resolve the SAME west (#106).
    fn west(&self, venv: &VenvBin) -> Command {
        let mut cmd = Command::new(&venv.west);
        cmd.current_dir(self.workspace_dir);
        crate::venv::with_venv_on_path(&mut cmd, &venv.west.to_string_lossy());
        cmd
    }
}

/// Create (or reuse) the workspace venv and refresh the manifest's
/// `pip.bootstrapUpgrade` packages in it.
///
/// Everything — west, the Zephyr requirements, the SDK extras — installs into
/// this workspace-local venv, never the system interpreter / `--user` /
/// `--break-system-packages` (alp-sdk#93: a half-removed system `packaging`
/// once broke `west init`, and a global west couples the build to the host
/// interpreter's state). Idempotent: an existing venv is reused.
pub(super) fn ensure_venv(
    ws: &Workspace,
    log: &mut Log,
    runner: &Runner,
    host: &HostPython,
) -> Result<VenvBin, String> {
    let _ = std::fs::create_dir_all(ws.workspace_dir);
    if ws.venv_present() {
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
    let venv = ws.venv_bin();
    let mut upgrade = Command::new(&venv.python);
    upgrade
        .args(["-m", "pip", "install", "--upgrade", "-q"])
        .args(&ws.facts.pip_bootstrap_upgrade);
    if runner.run(&mut upgrade).is_err() {
        log.warn("pip-upgrade", "pip/wheel upgrade reported a problem");
    }
    Ok(venv)
}

/// Install west into the venv, then `west init -l` / `west update` /
/// `west zephyr-export`, then the #769 legibility guard. `reuse` short-circuits
/// all of it (including the guard), exactly as both scripts do for a workspace
/// adopted from `$ZEPHYR_BASE`.
pub(super) fn west_phase(
    ws: &Workspace,
    venv: &VenvBin,
    log: &mut Log,
    runner: &Runner,
    reuse: bool,
) -> Result<(), String> {
    // west into the venv (NOT global / --user) so the system interpreter can't
    // break it. `west.pipSpec` is a manifest FLOOR, not a hard pin.
    //
    // DOCUMENTED DIVERGENCE from the schema's own stance: bootstrap-v1's
    // description of `west.pipSpec` reads "Informational today -- both
    // bootstrap scripts still run an unpinned `pip install --upgrade -q west`;
    // this is the recorded floor, not (yet) fed into that command." tan
    // deliberately DOES feed it: a declared floor that nothing honours is not a
    // floor, and `--upgrade west` vs `--upgrade "west>=0.14.0"` resolve to the
    // same wheel today, so honouring it costs nothing and starts mattering the
    // day the floor moves ahead of what a stale venv has. Raise on alp-sdk#917
    // so the schema wording catches up with its consumers.
    if !venv.west.is_file() {
        log.line("Installing west into the workspace venv");
        let mut install = Command::new(&venv.python);
        install
            .args(["-m", "pip", "install", "--upgrade", "-q"])
            .arg(&ws.facts.west_pip_spec);
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
        let mut init = ws.west(venv);
        init.args(&ws.facts.west_init_args).arg(ws.repo_root);
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

    let mut update = ws.west(venv);
    update.args(&ws.facts.west_update_args);
    runner
        .run(&mut update)
        .map_err(|detail| die("west update failed", detail))?;
    // Failure deliberately IGNORED (`|| true` in bootstrap.sh, no rc check in
    // bootstrap.ps1).
    let mut export = ws.west(venv);
    export.args(&ws.facts.west_export_args);
    let _ = runner.run(&mut export);

    // Legibility guard (#769): fail at bootstrap time -- not at first
    // `tan build` -- if the workspace manifest doesn't register the alp-*
    // extension commands. The searched-for command is a manifest fact
    // (`west.extensionGuardCommand`); the scripts hardcode it a second time in
    // their own die message, which we interpolate instead so it cannot go stale.
    let guard = &ws.facts.west_extension_guard;
    let mut help = ws.west(venv);
    help.arg("help");
    if !runner.capture(&mut help).contains(guard.as_str()) {
        return Err(format!(
            "workspace at {workspace} does not register 'west {guard}' -- its manifest is not \
             alp-sdk's west.yml (#769). Check 'west -C {workspace} config manifest.path'."
        ));
    }
    log.line(&format!(
        "alp-* extension commands registered ('west {guard}' resolves in {workspace})"
    ));
    Ok(())
}

/// The Python dependency installs, all into the SAME workspace venv and all
/// NON-FATAL (a recorded warn each), matching both scripts.
pub(super) fn pip_phase(ws: &Workspace, venv: &VenvBin, log: &mut Log, runner: &Runner) {
    let requirements = ws.workspace_dir.join(&ws.facts.zephyr_requirements_path);
    if requirements.is_file() {
        log.line("Installing Zephyr Python requirements into the venv");
        let mut cmd = Command::new(&venv.python);
        cmd.args(["-m", "pip", "install", "-q", "-r"])
            .arg(&requirements);
        if runner.run(&mut cmd).is_err() {
            log.warn(
                "zephyr-requirements",
                "Zephyr requirements install reported a problem -- check manually",
            );
        }
    }
    // SDK-side extras: alp_project.py needs jsonschema; the MCUboot dev-key
    // script needs imgtool. bootstrap.sh space-joins the list in its info line
    // (`${PIP_SDK_EXTRAS[*]}`); bootstrap.ps1 comma-joins it (`-join ', '`).
    let extras = &ws.facts.pip_sdk_extras;
    let rendered = if ws.is_windows {
        extras.join(", ")
    } else {
        extras.join(" ")
    };
    log.line(&format!(
        "Installing alp-sdk Python extras into the venv ({rendered})"
    ));
    let mut extras_cmd = Command::new(&venv.python);
    extras_cmd.args(["-m", "pip", "install", "-q"]).args(extras);
    if runner.run(&mut extras_cmd).is_err() {
        log.warn(
            "sdk-extras",
            "alp-sdk extras install reported a problem -- check manually",
        );
    }
    // tan's Python backend (alp_cli) -- editable, so a `git pull` in the
    // checkout updates the backend in place.
    let repo_root = ws.repo_root.to_string_lossy().into_owned();
    let workspace_dir = ws.workspace_dir.to_string_lossy().into_owned();
    let editable = Tokens {
        sdk_root: &repo_root,
        workspace_dir: &workspace_dir,
    }
    .apply(&ws.facts.pip_editable_install);
    log.line(&format!(
        "Installing the tan CLI's Python backend into the venv (pip install -e {})",
        native(Path::new(&editable))
    ));
    let mut editable_cmd = Command::new(&venv.python);
    editable_cmd
        .args(["-m", "pip", "install", "-q", "-e"])
        .arg(&editable);
    if runner.run(&mut editable_cmd).is_err() {
        log.warn(
            "editable-install",
            "alp_cli editable install reported a problem -- check manually",
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tan_core::bootstrap::fallback_facts;

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
    fn warnings_survive_into_json_issues() {
        // Regression: warnings used to be print-only, so a JSON run where every
        // non-fatal pip step failed still reported `ok:true, issues:[]`.
        let mut log = Log::new(true);
        log.line("this is only progress");
        log.warn("sdk-extras", "alp-sdk extras install reported a problem");
        log.warn(
            "editable-install",
            "alp_cli editable install reported a problem",
        );
        let issues = log.take_issues();
        assert_eq!(issues.len(), 2);
        assert_eq!(issues[0].code, "bootstrap.sdk-extras");
        assert_eq!(issues[0].severity, "warning");
        assert!(issues[1].message.contains("alp_cli editable install"));
        // Draining is idempotent -- no double-reporting on a second call.
        assert!(log.take_issues().is_empty());
    }

    /// Unique scratch dir per tag, matching the other command test suites.
    fn tmp(tag: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("tan-bootstrap-steps-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn venv_bin_follows_the_directory_that_exists_not_the_host() {
        // Regression: a git-bash-created `Scripts/` venv was accepted by the
        // presence check but then addressed as `bin/python`, so a POSIX host
        // skipped creation and fatally failed at `pip install west (venv)`.
        let facts = fallback_facts((3, 10));
        let root = tmp("venvbin");
        let venv = root.join(".venv");
        std::fs::create_dir_all(venv.join("Scripts")).unwrap();
        std::fs::write(venv.join("Scripts").join("python.exe"), b"").unwrap();

        for is_windows in [false, true] {
            let ws = Workspace {
                is_windows,
                facts: &facts,
                repo_root: &root,
                workspace_dir: &root,
                venv_dir: &venv,
            };
            assert!(ws.venv_present(), "Scripts-layout venv must be detected");
            let bin = ws.venv_bin();
            assert_eq!(bin.bin_dir, "Scripts");
            assert!(bin.python.ends_with("Scripts/python.exe"));
            assert!(bin.west.ends_with("Scripts/west.exe"));
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn venv_bin_prefers_the_posix_layout_and_falls_back_to_the_host() {
        let facts = fallback_facts((3, 10));
        let root = tmp("venvbin-posix");
        let venv = root.join(".venv");
        std::fs::create_dir_all(venv.join("bin")).unwrap();
        let ws = Workspace {
            is_windows: true,
            facts: &facts,
            repo_root: &root,
            workspace_dir: &root,
            venv_dir: &venv,
        };
        // `bin/` exists -> wins even on a Windows host (bootstrap.sh's VBIN order).
        assert_eq!(ws.venv_bin().bin_dir, "bin");
        // Nothing on disk -> this host's own layout.
        let empty = root.join("nothing");
        let ws = Workspace {
            is_windows: true,
            facts: &facts,
            repo_root: &root,
            workspace_dir: &root,
            venv_dir: &empty,
        };
        assert!(!ws.venv_present());
        assert_eq!(ws.venv_bin().bin_dir, "Scripts");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn prerequisites_come_from_the_manifest_list() {
        // A manifest that adds a tool must be enforced without a tan release.
        let mut facts = fallback_facts((3, 10));
        facts.prerequisites_posix = vec!["tan-no-such-tool-xyz".to_string()];
        facts.prerequisites_windows = vec!["tan-no-such-tool-xyz".to_string()];
        let err = check_prerequisites(&facts, cfg!(windows)).unwrap_err();
        assert!(
            err.iter().any(|l| l.contains("tan-no-such-tool-xyz")),
            "{err:?}"
        );
        // The Windows branch renders a per-tool hint line.
        let win = check_prerequisites(&facts, true).unwrap_err();
        assert_eq!(win[0], "Missing required tools:");
        assert!(
            win[1].contains("  ->  install `tan-no-such-tool-xyz`"),
            "{win:?}"
        );
    }
}
