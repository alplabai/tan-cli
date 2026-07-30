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

use tan_core::bootstrap::{
    BootstrapFacts, HostOs, PrereqFailure, Tokens, posix_python_not_runnable, posix_refusal,
    posix_venv_unusable, python_too_old, venv_exe_names, windows_python_not_runnable,
    windows_refusal,
};

use super::capture_tail;
use crate::envelope::Issue;
use crate::util::{HostPython, command_on_path, probe_host_python, python_venv_capable};

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

    /// The recorded warnings that mean the WORKSPACE IS NOT USABLE, in the
    /// order they were raised. Empty when the run only hit cosmetic problems.
    ///
    /// See [`WORKSPACE_BLOCKING`] for which ones those are and why.
    pub(super) fn blocking(&self) -> Vec<&str> {
        self.warnings
            .iter()
            .map(|(code, _)| code.as_str())
            .filter(|code| WORKSPACE_BLOCKING.contains(code))
            .collect()
    }

    /// Drain the recorded warnings as envelope issues.
    ///
    /// `escalate_blocking` promotes the [`WORKSPACE_BLOCKING`] ones to
    /// `severity: "error"` — set when they actually blocked the verdict, i.e.
    /// the run refused to report success over them. Under `--allow-partial`
    /// they stay `warning`, because the customer was told and chose to proceed,
    /// and an `error` on a run that exits 0 is its own kind of lie.
    ///
    /// Varying severity for one code is not new (`build.unknown-backend` does
    /// the same under `executionPolicy`), and it is free on the wire here:
    /// every affected code is `reserved` with no consumer.
    pub(super) fn take_issues(&mut self, escalate_blocking: bool) -> Vec<Issue> {
        self.warnings
            .drain(..)
            .map(|(code, message)| Issue {
                severity: if escalate_blocking && WORKSPACE_BLOCKING.contains(&code.as_str()) {
                    "error".to_string()
                } else {
                    "warning".to_string()
                },
                code: format!("bootstrap.{code}"),
                message,
            })
            .collect()
    }
}

/// The `pip_phase` warnings after which the workspace cannot do the thing the
/// customer bootstrapped it for (tan-cli#220). Each phase stays non-fatal in
/// itself — the run continues and the workspace is left on disk — but a run
/// that hit one of these has NOT produced a working environment and must not
/// say `bootstrap: complete.`
///
/// * `zephyr-requirements` — Zephyr's own `requirements.txt`. The first-blink
///   CI job measured the whole chain in one run: the install failed on `hidapi`
///   needing `libudev.h`, bootstrap printed `complete.`, and the build that
///   followed died with `ModuleNotFoundError: No module named 'elftools'`.
/// * `sdk-extras` — `jsonschema` (which `alp_project.py`, the loader every
///   plan emit goes through, imports) and `imgtool`.
/// * `editable-install` — tan's own Python backend (`alp_cli`).
///
/// `pip-upgrade` is deliberately ABSENT: it upgrades `pip`/`wheel` themselves,
/// and the pip that is already there still installs packages. It is the one
/// genuinely cosmetic member of the phase.
///
/// This list is about the VERDICT, not about refusing to proceed. A venv
/// missing `hidapi` still builds `native_sim` perfectly well, so nothing here
/// blocks a later command — see the `--allow-partial` note in
/// `commands::bootstrap::run`.
pub(super) const WORKSPACE_BLOCKING: [&str; 3] =
    ["zephyr-requirements", "sdk-extras", "editable-install"];

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
/// the fatal message lines verbatim PLUS the structured per-tool form of them
/// (see [`PrereqFailure`]).
///
/// `pub(crate)` rather than `pub(super)`: `commands::doctor` runs this SAME
/// gate so plain `tan doctor` reports a missing `ninja` without anyone having
/// to run bootstrap first (alp-sdk ADR 0021 P0a). One probe, not two that could
/// disagree about what the host is missing.
///
/// This is the PROBING half only — `prereq_present` walks PATH and
/// `probe_host_python` spawns interpreters, so it has to live here. Every
/// message and every struct it returns is built by
/// `tan_core::bootstrap::prerequisites`, which is pure and therefore testable
/// on both platforms from either host; nothing below renders a string itself.
///
/// The POSIX and Windows lists genuinely differ (Windows adds `ninja`) and only
/// Windows enforces `pythonMinVersion` — `bootstrap.sh` has no version check at
/// all. The manifest records that asymmetry faithfully ("recorded faithfully,
/// not silently unified") and so does this: the floor below is applied on the
/// Windows branch only. Every path that actually RUNS an SDK script still
/// applies `crate::util::python_too_old` on both platforms.
pub(crate) fn check_prerequisites(
    facts: &BootstrapFacts,
    host: HostOs,
) -> Result<HostPython, PrereqFailure> {
    // The HOST, not just `is_windows`: the manifest keys its install commands
    // `linux`/`macos`/`windows` while keying the tool LISTS `posix`/`windows`, so
    // the refusals below need the finer fact to hand a macOS user `brew install`
    // instead of Linux's `apt-get` line. Resolved ONCE here and passed down, so
    // no branch can look a tool up in the wrong OS's table.
    let is_windows = host == HostOs::Windows;
    let install = facts.install.for_host(host);
    let missing: Vec<&str> = facts
        .prerequisites(host)
        .iter()
        .filter(|tool| !prereq_present(tool, is_windows))
        .map(String::as_str)
        .collect();

    if is_windows {
        if !missing.is_empty() {
            return Err(windows_refusal(&missing, install));
        }
        // Python >= pythonMinVersion (dataclass slots, `X | None` unions).
        // Probe against the MANIFEST's floor, not tan's compiled-in one: the
        // check just below enforces the manifest's, and probing to a lower bar
        // would stop at the first candidate that clears 3.10 (`py -3`, often
        // the launcher's older default) and then fail the host for it, while a
        // newer `python` sat one candidate down the list.
        let Some(python) = probe_host_python(facts.python_min_version) else {
            return Err(windows_python_not_runnable(install));
        };
        if python.version < facts.python_min_version {
            return Err(python_too_old(
                python.version,
                facts.python_min_version,
                install,
            ));
        }
        return Ok(python);
    }

    if !missing.is_empty() {
        return Err(posix_refusal(&missing, install));
    }
    // No version check here — see the doc comment. The manifest floor is still
    // what the probe PREFERS (it only picks between candidates that ran; it
    // never refuses), so this branch cannot fail on version. The only extra
    // failure this adds over bootstrap.sh is an interpreter on PATH that cannot
    // run, which the script would have hit one step later at `python3 -m venv`.
    let python =
        probe_host_python(facts.python_min_version).ok_or_else(posix_python_not_runnable)?;
    // tan-cli#161: python3 ran and cleared every check above, and bootstrap
    // still died at `python3 -m venv` on a fresh Debian/Ubuntu host --
    // `python3-venv` is a package Debian splits OUT of base `python3`, so
    // presence + a working interpreter both say nothing about it. Linux-only:
    // every install command this gate can name is apt's (see `manifest.rs`'s
    // `linux` map, which is apt-get-only project-wide), and this is genuinely
    // a Debian/Ubuntu packaging split, not a general POSIX one -- macOS/BSD
    // pythons ship `ensurepip` in the base install.
    if host == HostOs::Linux && !python_venv_capable(&python) {
        return Err(posix_venv_unusable());
    }
    Ok(python)
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
#[derive(Debug)]
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
/// interpreter's state). Idempotent: an existing venv is reused -- but only a
/// USABLE one (tan-cli#161). A venv from a bootstrap that died between
/// `python -m venv` and the pip installs (the Debian/Ubuntu `ensurepip`-missing
/// case a moment before this in the flow, and any other partial failure) has a
/// real `bin/python` -- so [`Workspace::venv_present`] alone accepts it -- but
/// no `pip` module at all. Left alone, every later step here dies the exact
/// same way it did the first time, and the reporter's only way out was
/// `rm -rf` the directory by hand: a retry must either reuse a KNOWN-GOOD venv
/// or start clean, never silently inherit the wreckage.
pub(super) fn ensure_venv(
    ws: &Workspace,
    log: &mut Log,
    runner: &Runner,
    host: &HostPython,
) -> Result<VenvBin, String> {
    let _ = std::fs::create_dir_all(ws.workspace_dir);
    if ws.venv_present() {
        let existing = ws.venv_bin();
        if venv_has_usable_pip(&existing) {
            log.line(&format!(
                "Workspace venv already present at {}",
                native(ws.venv_dir)
            ));
        } else {
            log.line(&format!(
                "Workspace venv at {} has no usable pip (a previous bootstrap likely failed \
                 partway) -- removing and recreating it",
                native(ws.venv_dir)
            ));
            std::fs::remove_dir_all(ws.venv_dir).map_err(|e| {
                format!(
                    "failed to remove the broken venv at {}: {e}",
                    native(ws.venv_dir)
                )
            })?;
            create_venv(ws, log, runner, host)?;
        }
    } else {
        create_venv(ws, log, runner, host)?;
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

/// `python -m venv ws.venv_dir` with `host`'s interpreter, reporting progress
/// first. Split out of [`ensure_venv`] so both the fresh-venv path and the
/// broken-venv recreate path (tan-cli#161) run the exact same creation step —
/// a second, slightly different copy of it is how the two would end up
/// disagreeing about what "created" means.
fn create_venv(
    ws: &Workspace,
    log: &mut Log,
    runner: &Runner,
    host: &HostPython,
) -> Result<(), String> {
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
    })
}

/// Whether an existing venv's own interpreter can actually run `pip` — the
/// probe [`ensure_venv`] uses to tell a healthy venv from the corpse of a
/// bootstrap that died partway (tan-cli#161: `bin/python` exists and IS the
/// real interpreter, since Debian's ensurepip failure still leaves that file
/// behind; what is missing is `pip` itself). `false` on a spawn failure too —
/// unlike [`crate::util::python_venv_capable`]'s fail-OPEN (an inconclusive
/// answer about a host interpreter must not block a clean host), an existing
/// venv that cannot even be spawned is itself the evidence of brokenness this
/// check exists to catch, so it fails CLOSED (triggers a recreate).
fn venv_has_usable_pip(venv: &VenvBin) -> bool {
    Command::new(&venv.python)
        .args(["-m", "pip", "--version"])
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
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

/// The Python dependency installs, all into the SAME workspace venv, each
/// PHASE non-fatal (a recorded warn, and the run continues to the next).
///
/// The phases stay non-fatal; the RUN's verdict does not. Three of these four
/// warnings mean the workspace cannot do what it was bootstrapped for, and
/// `commands::bootstrap::run` refuses to print `bootstrap: complete.` over them
/// (tan-cli#220) — see [`WORKSPACE_BLOCKING`].
///
/// This USED TO SAY the phases were non-fatal "matching both scripts", and that
/// parity claim no longer holds. alp-sdk's `scripts/bootstrap.sh` and
/// `bootstrap.ps1` still warn and report success; tan does not. The divergence
/// is deliberate — a command that predicts its own next failure and then calls
/// itself complete has reported a false success, and tan is the surface ADR-0020
/// makes customer-facing — but it IS a divergence, and pretending otherwise in
/// this comment is how the two on-ramps drift apart unnoticed. Moving the
/// scripts to match is tracked as alp-sdk#1038.
pub(super) fn pip_phase(ws: &Workspace, venv: &VenvBin, log: &mut Log, runner: &Runner) {
    let requirements = ws.workspace_dir.join(&ws.facts.zephyr_requirements_path);
    if requirements.is_file() {
        log.line("Installing Zephyr Python requirements into the venv");
        let mut cmd = Command::new(&venv.python);
        cmd.args(["-m", "pip", "install", "-q", "-r"])
            .arg(&requirements);
        if runner.run(&mut cmd).is_err() {
            // Stays NON-FATAL (see this function's doc comment), but "check
            // manually" told the reader nothing actionable. Measured in CI on a
            // stock ubuntu-24.04 runner: the failure is `hidapi` building from
            // source, which needs pkg-config and the libusb-1.0 headers --
            // `Exception: pkg-config package 'libusb-1.0 >= 1.0.9' not found`.
            // The workspace still looks complete afterwards (.west/, .venv,
            // zephyr/ and modules/ all exist), so the next command a customer
            // runs is where it surfaces, far from the cause. Name the likely
            // remedy here rather than leaving them to read pip's traceback.
            log.warn(
                "zephyr-requirements",
                "Zephyr requirements install reported a problem -- the venv is \
                 incomplete and a later `tan init`/`tan build` may fail. On \
                 Linux this is usually `hidapi` needing native headers: \
                 `sudo apt-get install -y pkg-config libusb-1.0-0-dev libudev-dev`, then \
                 re-run `tan bootstrap`.",
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

    /// An executable `#!/bin/sh` stand-in that ignores its arguments and exits
    /// with `code`. Deliberately NOT `/bin/true` / `/bin/false`: macOS ships
    /// neither under `/bin` (they live in `/usr/bin`), so hardcoding the Linux
    /// path passed ubuntu and windows and failed only the macos-latest leg
    /// with `failed to launch /bin/true: No such file or directory (os error
    /// 2)`. A script written into the test's own temp dir depends on no host
    /// layout at all.
    #[cfg(unix)]
    fn exit_code_shim(dir: &std::path::Path, name: &str, code: i32) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(dir).unwrap();
        let path = dir.join(name);
        std::fs::write(&path, format!("#!/bin/sh\nexit {code}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    /// Stand-ins for a venv whose `pip` works vs. one whose `-m pip --version`
    /// fails, without needing a real Python venv on the test host (both ignore
    /// whatever args are appended and exit 0/1 respectively).
    #[cfg(unix)]
    #[test]
    fn venv_has_usable_pip_reads_the_probe_exit_status() {
        let root = tmp("venv-usable-probe");
        let ok = exit_code_shim(&root, "python-ok", 0);
        let healthy = VenvBin {
            python: ok.clone(),
            west: ok,
            bin_dir: "bin".to_string(),
        };
        assert!(venv_has_usable_pip(&healthy));

        let bad = exit_code_shim(&root, "python-bad", 1);
        let broken = VenvBin {
            python: bad.clone(),
            west: bad,
            bin_dir: "bin".to_string(),
        };
        assert!(!venv_has_usable_pip(&broken));

        let _ = std::fs::remove_dir_all(&root);
    }

    /// A venv interpreter that cannot even be spawned (missing entirely) fails
    /// CLOSED -- the opposite default from `python_venv_capable`'s fail-open,
    /// and deliberately so: an unusable EXISTING venv is exactly the state
    /// this probe exists to catch, not an inconclusive read on a host
    /// interpreter that has not been touched yet.
    #[test]
    fn venv_has_usable_pip_fails_closed_when_it_cannot_even_launch() {
        let root = tmp("venv-usable-missing");
        let ghost = VenvBin {
            python: root.join("bin").join("does-not-exist"),
            west: root.join("bin").join("does-not-exist-west"),
            bin_dir: "bin".to_string(),
        };
        assert!(!venv_has_usable_pip(&ghost));
        let _ = std::fs::remove_dir_all(&root);
    }

    /// tan-cli#161's second bug, driven end to end: a `.venv` left behind by a
    /// bootstrap that died between `python -m venv` and the pip installs (real
    /// `bin/python`, no working pip) must be REMOVED and recreated, not
    /// silently reused -- reuse is exactly what made the reporter's second
    /// `tan bootstrap` fail identically to the first, with nothing new to act
    /// on. `host` is an always-succeeding `#!/bin/sh` stand-in here so
    /// `create_venv`'s spawn "succeeds" without needing a real Python on the
    /// test host; it does not write real venv files, which is fine -- what
    /// this test proves is that the WRECKAGE is gone, i.e. a recreate was
    /// actually attempted rather than the existing corpse being kept.
    #[cfg(unix)]
    #[test]
    fn ensure_venv_recreates_a_venv_with_no_usable_pip() {
        let root = tmp("ensure-venv-broken");
        let venv_dir = root.join(".venv");
        let python_path = venv_dir.join("bin").join("python");
        std::fs::create_dir_all(python_path.parent().unwrap()).unwrap();
        // A real, executable script that runs (unlike Debian's actual failure
        // mode, whose `bin/python` runs fine and only `-m pip` fails) but
        // fails EVERY invocation -- sufficient to make `venv_has_usable_pip`
        // read it as broken, which is all `ensure_venv` consults.
        std::fs::write(&python_path, "#!/bin/sh\nexit 1\n").unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&python_path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        assert!(
            python_path.is_file(),
            "the broken interpreter must exist first"
        );

        let facts = fallback_facts((3, 10));
        let ws = Workspace {
            is_windows: false,
            facts: &facts,
            repo_root: &root,
            workspace_dir: &root,
            venv_dir: &venv_dir,
        };
        let mut log = Log::new(true);
        let runner = Runner {
            json: true,
            clear_zephyr_base: false,
        };
        let host = HostPython {
            argv: vec![
                exit_code_shim(&root, "host-python", 0)
                    .to_string_lossy()
                    .into_owned(),
            ],
            version: (3, 10),
        };

        let result = ensure_venv(&ws, &mut log, &runner, &host);
        assert!(result.is_ok(), "{result:?}");
        // The broken interpreter must be GONE -- the always-succeeding host
        // stand-in writes nothing, so its continued presence would mean the
        // corpse was reused rather than removed.
        assert!(
            !python_path.exists(),
            "the broken venv should have been removed, not reused"
        );

        let _ = std::fs::remove_dir_all(&root);
    }

    /// The mirror case: a venv whose pip DOES work is reused untouched, never
    /// removed -- `ensure_venv` must not recreate a perfectly good venv on
    /// every single bootstrap run.
    #[cfg(unix)]
    #[test]
    fn ensure_venv_reuses_a_venv_with_usable_pip() {
        let root = tmp("ensure-venv-healthy");
        let venv_dir = root.join(".venv");
        let python_path = venv_dir.join("bin").join("python");
        std::fs::create_dir_all(python_path.parent().unwrap()).unwrap();
        let marker = "#!/bin/sh\nexit 0\n";
        std::fs::write(&python_path, marker).unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&python_path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }

        let facts = fallback_facts((3, 10));
        let ws = Workspace {
            is_windows: false,
            facts: &facts,
            repo_root: &root,
            workspace_dir: &root,
            venv_dir: &venv_dir,
        };
        let mut log = Log::new(true);
        let runner = Runner {
            json: true,
            clear_zephyr_base: false,
        };
        let host = HostPython {
            argv: vec!["/bin/false".to_string()],
            version: (3, 10),
        };

        let result = ensure_venv(&ws, &mut log, &runner, &host);
        assert!(result.is_ok(), "{result:?}");
        // Untouched: same bytes as written above. If `ensure_venv` had wrongly
        // recreated it, `create_venv` would have run `/bin/false -m venv` --
        // which fails, so this call would have returned `Err` instead of
        // `Ok`, AND the file (if a `remove_dir_all` ran first) would be gone.
        assert_eq!(std::fs::read_to_string(&python_path).unwrap(), marker);

        let _ = std::fs::remove_dir_all(&root);
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
        let issues = log.take_issues(false);
        assert_eq!(issues.len(), 2);
        assert_eq!(issues[0].code, "bootstrap.sdk-extras");
        assert_eq!(issues[0].severity, "warning");
        assert!(issues[1].message.contains("alp_cli editable install"));
        // Draining is idempotent -- no double-reporting on a second call.
        assert!(log.take_issues(false).is_empty());
    }

    /// #220. Both of the above are workspace-blocking, so a run that refused to
    /// report success escalates them to `error` — an envelope that exits
    /// non-zero while every issue in it says `warning` invites a consumer to
    /// treat the whole thing as advisory.
    #[test]
    fn blocking_warnings_are_named_and_escalate_only_when_they_blocked_the_verdict() {
        let mut log = Log::new(true);
        log.warn("pip-upgrade", "pip/wheel upgrade reported a problem");
        log.warn("sdk-extras", "alp-sdk extras install reported a problem");

        // `pip-upgrade` is deliberately NOT blocking: the pip already present
        // still installs packages.
        assert_eq!(log.blocking(), vec!["sdk-extras"]);

        let issues = log.take_issues(true);
        let severity = |code: &str| {
            issues
                .iter()
                .find(|i| i.code == format!("bootstrap.{code}"))
                .map(|i| i.severity.as_str())
                .unwrap_or("MISSING")
        };
        assert_eq!(severity("sdk-extras"), "error");
        assert_eq!(
            severity("pip-upgrade"),
            "warning",
            "a non-blocking warning must not be escalated just because a sibling was"
        );
    }

    /// Under `--allow-partial` the run reports success, so the same codes stay
    /// `warning`: an `error` issue on a run that exits 0 is its own kind of lie.
    #[test]
    fn blocking_warnings_stay_warnings_when_the_run_reported_success() {
        let mut log = Log::new(true);
        log.warn("zephyr-requirements", "requirements install failed");
        let issues = log.take_issues(false);
        assert_eq!(issues[0].severity, "warning");
    }

    /// A clean run has nothing to block on — pinned so the blocking set cannot
    /// quietly start matching everything.
    #[test]
    fn a_run_with_no_warnings_blocks_nothing() {
        let log = Log::new(true);
        assert!(log.blocking().is_empty());
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
        // What only THIS side can prove: the PATH probe reads the manifest's
        // list (a manifest that adds a tool is enforced without a tan release)
        // and hands the right per-platform refusal back. The refusals' own
        // wording, and the structured entries they carry, are pinned where they
        // are built -- `tan_core::bootstrap::prerequisites`, PATH-free and so
        // testable for `ninja` too, which this test could never assume absent.
        let mut facts = fallback_facts((3, 10));
        facts.prerequisites_posix = vec!["tan-no-such-tool-xyz".to_string()];
        facts.prerequisites_macos = vec!["tan-no-such-tool-xyz".to_string()];
        facts.prerequisites_windows = vec!["tan-no-such-tool-xyz".to_string()];

        let win = check_prerequisites(&facts, HostOs::Windows).unwrap_err();
        assert_eq!(
            win,
            windows_refusal(
                &["tan-no-such-tool-xyz"],
                facts.install.for_host(HostOs::Windows)
            )
        );
        // Both POSIX hosts, because `check_prerequisites` is now what resolves
        // WHICH install map a refusal carries: hard-coding either one (or
        // collapsing the two back into an `is_windows` bool) would hand a macOS
        // user Linux's `apt-get` lines, and only comparing against the other
        // host's own map catches it.
        for host in [HostOs::Linux, HostOs::MacOs, HostOs::Other] {
            let posix = check_prerequisites(&facts, host).unwrap_err();
            assert_eq!(
                posix,
                posix_refusal(&["tan-no-such-tool-xyz"], facts.install.for_host(host)),
                "{host:?}"
            );
        }

        // macOS reads `prerequisites.macos` when the manifest declares one, and
        // `prerequisites.posix` when it does not (alp-sdk v0.14.0). Both
        // directions asserted, because getting this wrong is not cosmetic: the
        // v0.14.0 `posix` list carries `xz` and `wget`, which stock macOS does
        // not ship, so reading `posix` there refuses the bootstrap outright on a
        // supported host.
        let mut split = fallback_facts((3, 10));
        split.prerequisites_posix = vec!["tan-posix-only-xyz".to_string()];
        split.prerequisites_macos = vec!["tan-macos-only-xyz".to_string()];
        let mac = check_prerequisites(&split, HostOs::MacOs).unwrap_err();
        assert_eq!(
            mac.missing
                .iter()
                .map(|m| m.tool.as_str())
                .collect::<Vec<_>>(),
            ["tan-macos-only-xyz"],
            "macOS must read prerequisites.macos, not prerequisites.posix"
        );
        let lin = check_prerequisites(&split, HostOs::Linux).unwrap_err();
        assert_eq!(
            lin.missing
                .iter()
                .map(|m| m.tool.as_str())
                .collect::<Vec<_>>(),
            ["tan-posix-only-xyz"],
            "Linux must keep reading prerequisites.posix"
        );

        // An SDK that predates `prerequisites.macos` leaves it empty, and macOS
        // must then behave exactly as it always did -- read `posix`. The
        // fallback is the OLD behaviour, not a new guess.
        split.prerequisites_macos = Vec::new();
        let mac_legacy = check_prerequisites(&split, HostOs::MacOs).unwrap_err();
        assert_eq!(
            mac_legacy
                .missing
                .iter()
                .map(|m| m.tool.as_str())
                .collect::<Vec<_>>(),
            ["tan-posix-only-xyz"],
            "an undeclared prerequisites.macos must fall back to posix"
        );

        // A tool that IS present must not be reported -- `cargo` runs this
        // test, so it is on PATH by construction.
        facts.prerequisites_posix = vec!["cargo".to_string()];
        facts.prerequisites_macos = vec!["cargo".to_string()];
        facts.prerequisites_windows = vec!["cargo".to_string()];
        let refused = check_prerequisites(&facts, HostOs::detect(std::env::consts::OS))
            .err()
            .map(|f| f.code);
        assert_ne!(
            refused,
            Some("prerequisites-missing"),
            "a tool on PATH must not be reported missing"
        );
    }
}
