// SPDX-License-Identifier: Apache-2.0
//! Small shared helpers for command implementations.

use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use tan_core::{
    ProjectContext, ProjectResolutionInput, ProjectSettings, SdkSourceTier, format_iso8601_utc,
    resolve_active_sdk, resolve_global_default_sdk, resolve_project_context,
    resolve_sdk_source_tier,
};

use crate::cli::GlobalArgs;

/// Current UTC instant as an ISO-8601 string. Honors `SOURCE_DATE_EPOCH` for
/// reproducible output (tests, CI snapshots). Shared by doctor/inspect/trace.
pub fn generated_at_iso() -> String {
    if let Ok(raw) = std::env::var("SOURCE_DATE_EPOCH") {
        if let Ok(secs) = raw.trim().parse::<i64>() {
            return format_iso8601_utc(secs, 0);
        }
    }
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => format_iso8601_utc(d.as_secs() as i64, d.subsec_millis()),
        Err(_) => format_iso8601_utc(0, 0),
    }
}

/// Whether `command` resolves on PATH (mirrors the TS CLI's
/// `commandExistsOnPath`). Shared by doctor, support-bundle, size, `flash`'s
/// tool gate and `tool_available` above — every one of those decisions is
/// only meant to answer "is this on the user's PATH", so the lookup itself
/// must never consult the current directory.
pub fn command_on_path(command: &str) -> bool {
    if cfg!(windows) {
        // `where.exe` searches the CURRENT DIRECTORY before %PATH% (Windows'
        // documented search order), so a project checked out with its own
        // `openocd.exe`/`renode.exe` at its root would make `where` report it
        // as "available" — and the flash tool-gate / renode resolver then
        // spawn exactly that project-controlled binary. Walk PATH by hand
        // instead so a project directory can never supply the executable.
        windows_path_lookup(command).is_some()
    } else {
        Command::new("which")
            .arg(command)
            .output()
            .map(|out| out.status.success())
            .unwrap_or(false)
    }
}

/// PATH-only lookup for `command` on Windows: try each `%PATH%` entry, and for
/// an extension-less bare name (the normal case — `cmake`, `west`, `renode`)
/// each `%PATHEXT%` suffix, exactly as `CreateProcess` resolves a program name
/// once the CWD is excluded. Returns the first match.
fn windows_path_lookup(command: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let dirs: Vec<PathBuf> = std::env::split_paths(&path).collect();
    let pathext = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".to_string());
    find_on_path(command, &dirs, &pathext)
}

/// Pure search core split out of [`windows_path_lookup`] purely for unit
/// testing: given an explicit directory list (never the real environment or
/// CWD), does `command` exist as a file directly under one of them?
fn find_on_path(command: &str, dirs: &[PathBuf], pathext: &str) -> Option<PathBuf> {
    let exts: Vec<&str> = pathext.split(';').filter(|e| !e.is_empty()).collect();
    let has_ext = Path::new(command).extension().is_some();
    for dir in dirs {
        // `split_paths` yields an empty PathBuf for `;;` / a trailing `;` in
        // %PATH% (both routine on Windows). `PathBuf::new().join(x)` == `x`,
        // a bare relative path whose `is_file()` silently resolves against the
        // process CWD — reintroducing exactly the CWD hit this function exists
        // to avoid. Skip it instead of joining onto an empty base.
        if dir.as_os_str().is_empty() {
            continue;
        }
        if has_ext {
            let candidate = dir.join(command);
            if candidate.is_file() {
                return Some(candidate);
            }
        } else {
            for ext in &exts {
                let candidate = dir.join(format!("{command}{ext}"));
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

/// Whether a build tool resolves on this host: an absolute path (e.g. a venv
/// `west`) must exist on disk; a bare name must be on PATH. The single gate
/// predicate shared by the build pre-flight probe and the slice executor —
/// keep them answering the same question so preflight verdicts match what
/// actually runs.
pub fn tool_available(tool: &str) -> bool {
    let path = Path::new(tool);
    if path.is_absolute() {
        path.exists()
    } else {
        command_on_path(tool)
    }
}

/// Minimum Python the alp-sdk loader scripts require. `@dataclass(slots=True)`
/// (used throughout `scripts/alp_cli/`) landed in CPython 3.10, so an older
/// interpreter dies the moment any SDK script imports, with a cryptic
/// `TypeError: dataclass() got an unexpected keyword argument 'slots'`. Shared
/// by the `validate`/`generate` pre-flight guards.
pub const MIN_PYTHON: (u32, u32) = (3, 10);

/// Parse `sys.version_info[:2]` output ("3.10", "3.9\n", "  3.14  ") into
/// `(major, minor)`. `None` on anything unparseable. Split out from
/// [`python_version`] so the parsing is unit-testable without spawning.
fn parse_python_version(stdout: &str) -> Option<(u32, u32)> {
    let line = stdout.trim().lines().last()?.trim();
    let (major, minor) = line.split_once('.')?;
    Some((major.trim().parse().ok()?, minor.trim().parse().ok()?))
}

/// Probe `binary`'s Python version as `(major, minor)`. `None` when the
/// interpreter cannot be run or its output cannot be parsed — callers must NOT
/// treat `None` as "too old" (a missing/broken interpreter is a different
/// failure the real invocation surfaces on its own).
pub fn python_version(binary: &str) -> Option<(u32, u32)> {
    let out = Command::new(binary)
        .arg("-c")
        .arg("import sys;print('%d.%d' % sys.version_info[:2])")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_python_version(&String::from_utf8_lossy(&out.stdout))
}

/// Probe `program`'s version by spawning `program --version` and extracting
/// the first dotted-number token from its output (stdout, falling back to
/// stderr for a tool that banners there). `None` when the program cannot be
/// spawned or nothing in its output looks like a version.
///
/// The version tan-cli#123's `doctor --build` reports: WHATEVER TAN ITSELF
/// RESOLVED for `program`, never a second, independent re-probe a consumer
/// would have to reconcile against the presence check beside it. Presence
/// still comes from [`command_on_path`]/[`tool_available`]; this is purely the
/// extra, optional fact.
///
/// `ponytail:` one shared `--version` flag plus a generic first-dotted-token
/// scan, not a per-tool parser — every tool this feeds today (`git`, `cmake`,
/// `ninja`, `west`, `bitbake`, `dtc`, `gperf`) accepts long `--version`, and a
/// banner with an unrelated dotted number ahead of the real one would
/// misparse; add a per-tool parser if that's ever observed in practice.
pub fn tool_version(program: &str) -> Option<String> {
    let output = Command::new(program).arg("--version").output().ok()?;
    let text = if !output.stdout.is_empty() {
        String::from_utf8_lossy(&output.stdout).into_owned()
    } else {
        String::from_utf8_lossy(&output.stderr).into_owned()
    };
    first_version_token(&text)
}

/// The leading `v`-optional dotted-number run of the first whitespace token
/// that has one, e.g. `"West version: v1.5.0"` -> `"1.5.0"`, `"cmake version
/// 3.28.1\n\n..."` -> `"3.28.1"`, `"Version: DTC 1.7.0-g0c1e5cb"` -> `"1.7.0"`.
/// `None` when no token qualifies — requiring a literal `.` means a bare
/// year/count (e.g. a copyright line) is never mistaken for a version.
fn first_version_token(text: &str) -> Option<String> {
    for raw in text.split_whitespace() {
        let word = raw.trim_start_matches(['v', 'V']);
        let digits: String = word
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == '.')
            .collect();
        if digits.contains('.') && digits.chars().next().is_some_and(|c| c.is_ascii_digit()) {
            return Some(digits.trim_end_matches('.').to_string());
        }
    }
    None
}

/// A user-facing error string when `binary` is a Python older than
/// [`MIN_PYTHON`]; `None` when it is new enough OR its version can't be
/// determined (don't block on an unknown — let the real call run and surface
/// its own error). Turns the SDK's cryptic `dataclass(slots=True)` traceback
/// into an actionable message.
pub fn python_too_old(binary: &str) -> Option<String> {
    match python_version(binary) {
        Some(found) if found < MIN_PYTHON => Some(format!(
            "Python {}.{} found at `{}`, but alp-sdk requires Python {}.{}+. \
             Put a newer `python` first on PATH (VS Code users can instead set \
             alpSdk.pythonPath).",
            found.0, found.1, binary, MIN_PYTHON.0, MIN_PYTHON.1
        )),
        _ => None,
    }
}

/// A host Python interpreter that was probed and actually ran.
#[derive(Debug)]
pub struct HostPython {
    /// argv prefix that launches it — `["py", "-3"]`, `["python3"]`, …
    pub argv: Vec<String>,
    /// The `(major, minor)` it reported.
    pub version: (u32, u32),
}

impl HostPython {
    /// A fresh [`Command`] for this interpreter, with its launcher flags applied.
    pub fn command(&self) -> Command {
        let mut cmd = Command::new(&self.argv[0]);
        cmd.args(&self.argv[1..]);
        cmd
    }

    /// How to spell this interpreter in a message (`py -3`, `python3`, …).
    pub fn display(&self) -> String {
        self.argv.join(" ")
    }
}

/// Whether `python`'s `venv` module can actually create a usable virtual
/// environment (tan-cli#161). `python -m venv --help` cannot tell — argparse
/// answers it before `ensurepip` is ever touched — so this probes the real
/// dependency: `import ensurepip`, which fails fast (no venv directory
/// created) on the Debian/Ubuntu split where `python3-venv` is a separate,
/// unmet package. Verified against a real Ubuntu 24.04 host missing it:
/// `python3 -m venv --help` exits 0 while `python3 -c "import ensurepip"`
/// exits 1 with `ModuleNotFoundError: No module named 'ensurepip'` — the exact
/// shape `python3 -m venv` itself fails with a moment later.
///
/// `true` when the probe cannot be run at all (spawn failure) — this is not
/// the check that should block a host on an inconclusive answer; the real
/// `python -m venv` a moment later surfaces its own error if something is
/// genuinely wrong.
pub fn python_venv_capable(python: &HostPython) -> bool {
    python
        .command()
        .args(["-c", "import ensurepip"])
        .output()
        .map(|out| out.status.success())
        .unwrap_or(true)
}

/// Find the host interpreter `tan bootstrap` creates the workspace venv with:
/// walk [`tan_core::bootstrap::python_candidates`] in order and take the first
/// that actually RUNS and is at least `minimum`, falling back to the first that
/// merely ran (so the caller can report a real version in its too-old message
/// rather than "did not run"). `None` when no candidate runs at all.
///
/// `minimum` is a PARAMETER because the floor bootstrap enforces is the
/// manifest's `prerequisites.pythonMinVersion`, not tan's compiled-in
/// [`MIN_PYTHON`]. Probing against the compiled-in floor while enforcing the
/// manifest's would hard-fail a host that has a good interpreter one candidate
/// further down the list the moment alp-sdk bumps its floor — the exact skew
/// manifest consumption exists to survive. Non-bootstrap callers pass
/// [`MIN_PYTHON`].
///
/// "Actually runs" is the whole point on Windows: the Microsoft Store
/// `python.exe` alias sits on PATH and satisfies any presence check, but
/// executing it prints nothing and opens the Store instead (bootstrap.ps1, the
/// "Prerequisite check" section's `python did not run (Windows Store alias?)`
/// Fail — anchored by name because the oracle's line numbers have already
/// drifted twice).
/// Requiring parseable output rejects it, and the `py -3` candidate ahead of it
/// means a machine with only the launcher installed still bootstraps.
///
/// The version preference is what keeps that ordering safe: `py -3` resolves to
/// the LAUNCHER's default, which is routinely an older install than the bare
/// `python` on PATH (a 3.9 default alongside a 3.14 on PATH is exactly the
/// shape that would otherwise make bootstrap refuse a perfectly good host).
pub fn probe_host_python(minimum: (u32, u32)) -> Option<HostPython> {
    let mut first_that_ran: Option<HostPython> = None;
    for candidate in tan_core::bootstrap::python_candidates(cfg!(windows)) {
        let Some((program, flags)) = candidate.split_first() else {
            continue;
        };
        let output = Command::new(program)
            .args(flags)
            .arg("-c")
            .arg("import sys;print('%d.%d' % sys.version_info[:2])")
            .output();
        let Ok(output) = output else { continue };
        if !output.status.success() {
            continue;
        }
        let Some(version) = parse_python_version(&String::from_utf8_lossy(&output.stdout)) else {
            continue;
        };
        let found = HostPython {
            argv: candidate.iter().map(|s| (*s).to_string()).collect(),
            version,
        };
        if version >= minimum {
            return Some(found);
        }
        first_that_ran.get_or_insert(found);
    }
    first_that_ran
}

/// Lexically normalize a path (collapse `.` and `..`) without touching the
/// filesystem, mirroring Node's `path.resolve` behavior on the joined result.
pub fn normalize_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Resolve the workspace root from `--project` (joined to CWD) or CWD itself.
/// Unnormalized join, matching the resolution `generate`/`init`/`examples` use
/// before probing for the SDK checkout.
pub fn cli_workspace_root(g: &GlobalArgs) -> PathBuf {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    match &g.project {
        Some(project) => cwd.join(project),
        None => cwd,
    }
}

/// True if `root` contains `scripts/alp_project.py`, marking it a valid SDK root.
pub fn has_loader_script(root: &Path) -> bool {
    root.join("scripts").join("alp_project.py").exists()
}

/// The user's home `.alp` directory (`~/.alp`): `USERPROFILE` on Windows else
/// `HOME`, falling back to `.` when neither is set. Shared by the SDK install
/// cache root and the machine-global default-SDK pointer (`tan sdk switch
/// --global` / the `globalDefault` resolution tier).
pub fn home_alp_dir() -> PathBuf {
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".alp")
}

/// The explicit SDK path to honor before auto-discovery, in precedence order:
/// `--sdk-root` (terminal — returned as-is so the caller's loader check fails
/// loudly on a bad path), then the workspace active-SDK pointer
/// (`.alp/sdk-path`, written by `tan sdk switch`), then the machine-global
/// default pointer (`~/.alp/sdk-default`, written by `tan sdk switch
/// --global`). Both pointers are best-effort: each is only used when it still
/// points at a real checkout (`has_loader`), so a stale pointer silently falls
/// through to the next tier instead of locking the user out. Returns `("",
/// None)` when nothing applies here (core auto-discovery in `tan_core::project`
/// takes it from there — the caller reports that as `Discovery` once core
/// actually resolves something). The second element names WHICH of these three
/// branches produced a non-empty path, for `resolve_cli_project_context` to
/// hand to [`crate::sdk_report`] without a second resolution. Pure —
/// filesystem access is injected for unit testing.
fn effective_sdk_path_with(
    sdk_root_arg: Option<&str>,
    workspace_root: &str,
    global_default_dir: &str,
    has_loader: &impl Fn(&str) -> bool,
    path_exists: &impl Fn(&str) -> bool,
    read_file: &impl Fn(&str) -> Option<String>,
) -> (String, Option<SdkSourceTier>) {
    if let Some(root) = sdk_root_arg {
        if !root.trim().is_empty() {
            return (root.to_string(), Some(SdkSourceTier::SdkRootFlag));
        }
    }
    if let Some(pointer) = resolve_active_sdk(workspace_root, path_exists, read_file) {
        if has_loader(&pointer) {
            return (pointer, Some(SdkSourceTier::ProjectPin));
        }
    }
    if let Some(pointer) = resolve_global_default_sdk(global_default_dir, path_exists, read_file) {
        if has_loader(&pointer) {
            return (pointer, Some(SdkSourceTier::GlobalDefault));
        }
    }
    (String::new(), None)
}

/// Filesystem-backed [`effective_sdk_path_with`]: `--sdk-root` > `.alp/sdk-path`
/// pointer (best-effort) > `~/.alp/sdk-default` (best-effort) > `("",
/// None)` (auto-discovery).
fn effective_sdk_path(g: &GlobalArgs, workspace_root: &Path) -> (String, Option<SdkSourceTier>) {
    effective_sdk_path_with(
        g.sdk_root.as_deref(),
        &workspace_root.to_string_lossy(),
        &home_alp_dir().to_string_lossy(),
        &|p| has_loader_script(Path::new(p)),
        &|p| Path::new(p).exists(),
        &|p| std::fs::read_to_string(p).ok(),
    )
}

/// Resolve the alp-sdk root: honor `--sdk-root` when it has the loader script,
/// then the workspace active-SDK pointer (`.alp/sdk-path`), then the
/// machine-global default pointer (`~/.alp/sdk-default`), otherwise probe the
/// workspace and sibling `alp-sdk` / `alp-sdk-upstream` dirs. Shared by
/// `generate` (codegen), `init --from-example`, and `examples`.
pub fn resolve_sdk_root(g: &GlobalArgs, workspace_root: &Path) -> Option<PathBuf> {
    if let Some(root) = &g.sdk_root {
        let candidate = PathBuf::from(root);
        if has_loader_script(&candidate) {
            record_resolved_sdk_root(&candidate, SdkSourceTier::SdkRootFlag);
            return Some(candidate);
        }
        return None;
    }

    // Workspace active-SDK pointer (`tan sdk switch`); best-effort — only when it
    // still points at a real checkout, else fall through to the global default.
    if let Some(pointer) = resolve_active_sdk(
        &workspace_root.to_string_lossy(),
        |p| Path::new(p).exists(),
        |p| std::fs::read_to_string(p).ok(),
    ) {
        let candidate = PathBuf::from(&pointer);
        if has_loader_script(&candidate) {
            record_resolved_sdk_root(&candidate, SdkSourceTier::ProjectPin);
            return Some(candidate);
        }
    }

    // Machine-global default pointer (`tan sdk switch --global`); same
    // best-effort rule — a stale pointer falls through to auto-discovery.
    if let Some(pointer) = resolve_global_default_sdk(
        &home_alp_dir().to_string_lossy(),
        |p| Path::new(p).exists(),
        |p| std::fs::read_to_string(p).ok(),
    ) {
        let candidate = PathBuf::from(&pointer);
        if has_loader_script(&candidate) {
            record_resolved_sdk_root(&candidate, SdkSourceTier::GlobalDefault);
            return Some(candidate);
        }
    }

    let discovered = discover_sdk_root(workspace_root);
    if let Some(candidate) = &discovered {
        record_resolved_sdk_root(candidate, SdkSourceTier::Discovery);
    }
    discovered
}

/// Report the SDK root + tier a [`resolve_sdk_root`] branch actually returned,
/// to [`crate::sdk_report`] — never a fresh resolution (see that module: this
/// is the branch that already ran, not a second lookup).
fn record_resolved_sdk_root(path: &Path, tier: SdkSourceTier) {
    crate::sdk_report::record(&path.to_string_lossy(), tier);
}

/// Sibling/workspace auto-discovery: the workspace root itself, then sibling
/// `alp-sdk` / `alp-sdk-upstream` directories, first one with the loader
/// script wins — and failing all three, the nearest ENCLOSING checkout
/// ([`tan_core::nearest_ancestor_sdk`]), which is what resolves the documented
/// Quickstart `tan --project examples/<cat>/<name> build` whose workspace root
/// sits levels below the checkout it was invoked from (issue #101). The
/// ancestor tier is shared with [`tan_core::discover_workspace_sdk`] so the two
/// keep agreeing (see [`resolve_sdk_tiered`]); the lateral candidate set stays
/// deliberately wider here (`alp-sdk-upstream`, first-match-wins).
fn discover_sdk_root(workspace_root: &Path) -> Option<PathBuf> {
    let parent = workspace_root.parent().map(Path::to_path_buf);
    let candidates = [
        workspace_root.to_path_buf(),
        parent
            .as_ref()
            .map(|p| p.join("alp-sdk"))
            .unwrap_or_else(|| PathBuf::from("alp-sdk")),
        parent
            .as_ref()
            .map(|p| p.join("alp-sdk-upstream"))
            .unwrap_or_else(|| PathBuf::from("alp-sdk-upstream")),
    ];

    candidates
        .into_iter()
        .find(|c| has_loader_script(c))
        .or_else(|| {
            tan_core::nearest_ancestor_sdk(&workspace_root.to_string_lossy(), |p| {
                Path::new(p).exists()
            })
            .map(PathBuf::from)
        })
}

/// Resolve the active SDK path across the full four-tier precedence chain
/// (`--sdk-root` > project pin > global default > discovery), reporting which
/// tier produced it. Unlike [`resolve_sdk_root`], `--sdk-root` is returned
/// as-is even when it lacks the loader script (terminal, matching
/// [`effective_sdk_path`]) — callers that need a validated result apply their
/// own `has_loader_script` check on the returned path. The discovery tier
/// uses [`tan_core::discover_workspace_sdk`] — the SAME candidate set +
/// exactly-one-or-none rule `effective_sdk_path`/`resolve_cli_project_context`
/// resolve for this workspace (no `alp-sdk-upstream`, ambiguous is `None`) —
/// so `sourceTier` never claims `discovery` for a path build/validate/doctor
/// won't actually resolve. Used by `tan sdk current --json`'s `sourceTier`.
///
/// Deliberately does NOT call [`crate::sdk_report::record`] itself — unlike
/// [`resolve_sdk_root`] and [`resolve_cli_project_context`], this resolver has
/// a second call site (`sdk.rs`'s `switch_cache_roots`) that only wants the
/// active SDK as a CANDIDATE CACHE ROOT for a `tan sdk switch` that is about
/// to repoint the pin to something else entirely; recording there reported
/// the SDK `switch` was replacing, not the one it switched to. The one call
/// site that treats this result as "the active SDK" (`sdk.rs`'s
/// `run_current`) records it explicitly at its own call.
pub fn resolve_sdk_tiered(
    g: &GlobalArgs,
    workspace_root: &Path,
) -> (Option<String>, SdkSourceTier) {
    let sdk_root_flag = g
        .sdk_root
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());

    let project_pin = resolve_active_sdk(
        &workspace_root.to_string_lossy(),
        |p| Path::new(p).exists(),
        |p| std::fs::read_to_string(p).ok(),
    )
    .filter(|p| has_loader_script(Path::new(p)));

    let global_default = resolve_global_default_sdk(
        &home_alp_dir().to_string_lossy(),
        |p| Path::new(p).exists(),
        |p| std::fs::read_to_string(p).ok(),
    )
    .filter(|p| has_loader_script(Path::new(p)));

    let discovery = tan_core::discover_workspace_sdk(&workspace_root.to_string_lossy(), |p| {
        Path::new(p).exists()
    });

    resolve_sdk_source_tier(
        sdk_root_flag,
        project_pin.as_deref(),
        global_default.as_deref(),
        discovery.as_deref(),
    )
}

/// The resolution shared by [`resolve_cli_project_context`] and
/// [`resolve_cli_project_context_no_sdk_report`] — everything except whether
/// to also tell [`crate::sdk_report`] what was resolved. Split out so a
/// command that wants `board_yaml_path`/`workspace_root` for REPORTING only
/// can reuse the identical resolution without duplicating it, rather than
/// re-deriving a narrower version by hand (tan-cli#111 follow-up).
fn resolve_cli_project_context_inner(g: &GlobalArgs) -> (ProjectContext, Option<SdkSourceTier>) {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_arg = g.project.clone().unwrap_or_else(|| ".".to_string());
    let workspace_root = normalize_path(&cwd.join(&project_arg))
        .to_string_lossy()
        .to_string();

    // `--sdk-root` > `.alp/sdk-path` pointer > `~/.alp/sdk-default` > `("",
    // None)` (core auto-discovery below). `sdk_tier_hint` names which of
    // those three branches produced `sdk_path`, when one did.
    let (sdk_path, sdk_tier_hint) = effective_sdk_path(g, Path::new(&workspace_root));
    let settings = ProjectSettings {
        sdk_path,
        python_path: String::new(),
        board_yaml_path: g
            .board_yaml
            .clone()
            .unwrap_or_else(|| "board.yaml".to_string()),
        west_cwd: String::new(),
    };
    let context = resolve_project_context(
        &ProjectResolutionInput {
            workspace_folders: vec![workspace_root],
            settings,
            is_windows: cfg!(windows),
        },
        |p| Path::new(p).exists(),
    );
    (context, sdk_tier_hint)
}

/// Resolve the project context from the global args, mirroring the TS commands'
/// `path.resolve(cwd, project) + resolveProjectContext` boilerplate. Shared by
/// `validate`, `diff`, `presets`, `doctor`, and every command that actually
/// drives the resolved SDK.
pub fn resolve_cli_project_context(g: &GlobalArgs) -> ProjectContext {
    let (context, sdk_tier_hint) = resolve_cli_project_context_inner(g);

    // Report to `sdk_report` what THIS call actually resolved — never a
    // fresh resolution. `sdk_tier_hint` is only a hint from `effective_sdk_path`'s
    // own branch; core's `resolve_project_context` re-validates the loader
    // marker, so a hinted tier whose validation failed must not be reported
    // (`context.sdk_root` stayed `None`, nothing to record). When the hint was
    // empty (no `--sdk-root`/pointer applied) but `context.sdk_root` still
    // resolved, that came from core's own self/sibling/ancestor-walk
    // auto-discovery, i.e. `Discovery`.
    if let Some(root) = &context.sdk_root {
        crate::sdk_report::record(root, sdk_tier_hint.unwrap_or(SdkSourceTier::Discovery));
    }

    context
}

/// Same resolution as [`resolve_cli_project_context`], but never calls
/// [`crate::sdk_report::record`] — for a command that only wants
/// `board_yaml_path`/`workspace_root` off the shared resolver and does not
/// itself resolve-and-use an SDK in the sense `envelope.rs`'s `sdk` field
/// documents. `debug-config` calls this: it reads only `board/system-manifest.yaml`
/// under the workspace, never the SDK checkout, so recording one would add an
/// undeclared `sdk` envelope key as a side effect of a field it merely
/// reports (tan-cli#111 follow-up).
pub fn resolve_cli_project_context_no_sdk_report(g: &GlobalArgs) -> ProjectContext {
    resolve_cli_project_context_inner(g).0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_version_parse_handles_common_shapes() {
        assert_eq!(parse_python_version("3.10\n"), Some((3, 10)));
        assert_eq!(parse_python_version("3.9"), Some((3, 9)));
        assert_eq!(parse_python_version("  3.14  "), Some((3, 14)));
        // Last line wins — some interpreters print a banner before the value.
        assert_eq!(parse_python_version("noise\n3.12\n"), Some((3, 12)));
        assert_eq!(parse_python_version(""), None);
        assert_eq!(parse_python_version("python 3"), None);
    }

    /// Real `/bin/true` and `/bin/false` stand in for a working vs. broken
    /// `ensurepip` import without needing an actual Python on the test host —
    /// both ignore whatever args `python_venv_capable` appends and exit
    /// 0/1 respectively, which is all the function reads.
    #[cfg(unix)]
    #[test]
    fn venv_capable_reads_the_probe_exit_status() {
        let ok = HostPython {
            argv: vec!["true".to_string()],
            version: (3, 12),
        };
        assert!(python_venv_capable(&ok));

        let broken = HostPython {
            argv: vec!["false".to_string()],
            version: (3, 12),
        };
        assert!(!python_venv_capable(&broken));
    }

    /// A python that cannot even be spawned (bogus argv) must NOT block —
    /// that verdict belongs to the real `python -m venv` call a moment later,
    /// not to this probe guessing at a launch failure.
    #[test]
    fn venv_capable_fails_open_when_the_probe_cannot_launch() {
        let unlaunchable = HostPython {
            argv: vec!["tan-cli-no-such-interpreter-xyz".to_string()],
            version: (3, 12),
        };
        assert!(python_venv_capable(&unlaunchable));
    }

    #[test]
    fn first_version_token_handles_real_tool_banners() {
        assert_eq!(
            first_version_token("West version: v1.5.0\n"),
            Some("1.5.0".to_string())
        );
        assert_eq!(
            first_version_token(
                "cmake version 3.28.1\n\nCMake suite maintained and supported by Kitware."
            ),
            Some("3.28.1".to_string())
        );
        assert_eq!(first_version_token("1.11.1\n"), Some("1.11.1".to_string()));
        assert_eq!(
            first_version_token("git version 2.43.0"),
            Some("2.43.0".to_string())
        );
        assert_eq!(
            first_version_token("GNU gperf 3.1\nCopyright (C) 2024 Free Software Foundation"),
            Some("3.1".to_string())
        );
        // A trailing git-describe suffix must not swallow the version with it.
        assert_eq!(
            first_version_token("Version: DTC 1.7.0-g0c1e5cb\n"),
            Some("1.7.0".to_string())
        );
        // A bare year/count is never mistaken for a version (no `.`).
        assert_eq!(
            first_version_token("Copyright (C) 2024 Free Software Foundation"),
            None
        );
        assert_eq!(first_version_token("no digits here"), None);
        assert_eq!(first_version_token(""), None);
    }

    #[test]
    fn min_python_boundary() {
        // 3.9 is rejected; 3.10 and 3.14 clear the `< MIN_PYTHON` guard.
        assert!((3u32, 9u32) < MIN_PYTHON);
        assert!((3u32, 10u32) >= MIN_PYTHON);
        assert!((3u32, 14u32) >= MIN_PYTHON);
    }

    /// Unique scratch dir per test/tag, matching the pattern used by
    /// `size.rs`'s tests.
    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-util-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// The regression this guards: `where.exe` searches the CWD before PATH,
    /// so a naive port of that behavior would report a project-local dropped
    /// binary as "on PATH". `find_on_path` takes an explicit directory list
    /// with no CWD concept at all, so a directory holding a same-named file
    /// that is NOT in that list must never be found — structurally proving
    /// the lookup is PATH-only.
    #[test]
    fn find_on_path_never_finds_a_file_outside_the_given_dirs() {
        let cwd_like = tmp("cwd");
        std::fs::write(cwd_like.join("openocd.EXE"), b"").unwrap();
        let path_dir = tmp("path");

        // `cwd_like` deliberately excluded from `dirs` -> must not resolve.
        assert!(
            find_on_path(
                "openocd",
                std::slice::from_ref(&path_dir),
                ".COM;.EXE;.BAT;.CMD"
            )
            .is_none()
        );

        // Once it's an actual PATH entry, the same file resolves.
        assert!(find_on_path("openocd", &[cwd_like, path_dir], ".COM;.EXE;.BAT;.CMD").is_some());
    }

    /// `split_paths` on a real `%PATH%` like `C:\Windows;;C:\bar;` yields an
    /// empty `PathBuf` for each `;;`/trailing `;`. `PathBuf::new().join(name)`
    /// == bare `name`, whose `is_file()` resolves against the process CWD —
    /// so without the empty-entry skip, a same-named file sitting in the CWD
    /// would resolve even though the CWD was never a PATH entry. Drops a
    /// marker into the real test-process CWD (no `chdir`, so this stays safe
    /// under the default multi-threaded test runner) to prove it's ignored.
    /// Pins the fix at util.rs:58.
    #[test]
    fn find_on_path_skips_empty_path_entries_and_ignores_cwd() {
        let name = format!("tan-util-emptyentry-{}", std::process::id());
        let marker = std::env::current_dir().unwrap().join(format!("{name}.EXE"));
        std::fs::write(&marker, b"").unwrap();

        // Empty entry (as produced by `;;` / a trailing `;`) plus a real,
        // unrelated PATH dir that does NOT contain the binary.
        let path_dir = tmp("emptyentry-path");
        let result = find_on_path(&name, &[PathBuf::new(), path_dir], ".COM;.EXE;.BAT;.CMD");

        let _ = std::fs::remove_file(&marker);
        assert!(result.is_none());
    }

    #[test]
    fn find_on_path_tries_each_pathext_suffix_for_bare_names() {
        let dir = tmp("pathext");
        std::fs::write(dir.join("west.BAT"), b"").unwrap();
        assert!(find_on_path("west", &[dir], ".COM;.EXE;.BAT;.CMD").is_some());
    }

    #[test]
    fn find_on_path_uses_exact_name_when_extension_already_given() {
        let dir = tmp("exact");
        std::fs::write(dir.join("tool.sh"), b"").unwrap();
        // Extension is already explicit -> looked up verbatim, not with
        // another PATHEXT suffix appended.
        assert!(find_on_path("tool.sh", std::slice::from_ref(&dir), ".COM;.EXE").is_some());
        assert!(find_on_path("tool", &[dir], ".COM;.EXE").is_none());
    }

    #[test]
    fn normalize_collapses_current_and_parent_dirs() {
        assert_eq!(
            normalize_path(Path::new("/a/b/./c")),
            PathBuf::from("/a/b/c")
        );
        assert_eq!(
            normalize_path(Path::new("/a/b/../c")),
            PathBuf::from("/a/c")
        );
    }

    /// A `.alp/sdk-path` pointer that resolves to `sdk_path`, present + readable.
    fn pointer_fs(
        sdk_path: &'static str,
    ) -> (impl Fn(&str) -> bool, impl Fn(&str) -> Option<String>) {
        let json = format!("{{\"sdkPath\":\"{sdk_path}\"}}");
        (
            |p: &str| p.ends_with("sdk-path"),
            move |p: &str| p.ends_with("sdk-path").then(|| json.clone()),
        )
    }

    #[test]
    fn sdk_root_arg_wins_over_pointer() {
        let (exists, read) = pointer_fs("/from/pointer");
        // Explicit --sdk-root is terminal and returned verbatim, even ahead of a
        // valid pointer; the caller's own loader check validates it.
        let (got, tier) = effective_sdk_path_with(
            Some("/explicit"),
            "/work",
            "/home/.alp",
            &|_| true,
            &exists,
            &read,
        );
        assert_eq!(got, "/explicit");
        assert_eq!(tier, Some(SdkSourceTier::SdkRootFlag));
    }

    #[test]
    fn blank_sdk_root_arg_falls_through_to_pointer() {
        let (exists, read) = pointer_fs("/from/pointer");
        let (got, tier) = effective_sdk_path_with(
            Some("   "),
            "/work",
            "/home/.alp",
            &|p| p == "/from/pointer",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/pointer");
        assert_eq!(tier, Some(SdkSourceTier::ProjectPin));
    }

    #[test]
    fn valid_pointer_is_used_when_no_sdk_root_arg() {
        let (exists, read) = pointer_fs("/from/pointer");
        let (got, tier) = effective_sdk_path_with(
            None,
            "/work",
            "/home/.alp",
            &|p| p == "/from/pointer",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/pointer");
        assert_eq!(tier, Some(SdkSourceTier::ProjectPin));
    }

    #[test]
    fn stale_pointer_falls_through_to_global_default_then_discovery() {
        let (exists, read) = pointer_fs("/gone");
        // Pointer resolves but its target has no loader script -> best-effort skip,
        // and the global-default lookup here has no matching pointer file either.
        let (got, tier) =
            effective_sdk_path_with(None, "/work", "/home/.alp", &|_| false, &exists, &read);
        assert_eq!(got, "");
        assert_eq!(tier, None);
    }

    #[test]
    fn no_arg_and_no_pointer_yields_empty() {
        let (got, tier) =
            effective_sdk_path_with(None, "/work", "/home/.alp", &|_| true, &|_| false, &|_| {
                None
            });
        assert_eq!(got, "");
        assert_eq!(tier, None);
    }

    /// A `~/.alp/sdk-default` pointer that resolves to `sdk_path`, present + readable.
    fn global_default_fs(
        sdk_path: &'static str,
    ) -> (impl Fn(&str) -> bool, impl Fn(&str) -> Option<String>) {
        let json = format!("{{\"sdkPath\":\"{sdk_path}\"}}");
        (
            |p: &str| p.ends_with("sdk-default"),
            move |p: &str| p.ends_with("sdk-default").then(|| json.clone()),
        )
    }

    #[test]
    fn global_default_used_when_no_project_pointer() {
        let (exists, read) = global_default_fs("/from/global");
        let (got, tier) = effective_sdk_path_with(
            None,
            "/work",
            "/home/.alp",
            &|p| p == "/from/global",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/global");
        assert_eq!(tier, Some(SdkSourceTier::GlobalDefault));
    }

    #[test]
    fn stale_global_default_falls_through_to_empty() {
        let (exists, read) = global_default_fs("/gone");
        // Pointer resolves but its target has no loader script -> best-effort skip.
        let (got, tier) =
            effective_sdk_path_with(None, "/work", "/home/.alp", &|_| false, &exists, &read);
        assert_eq!(got, "");
        assert_eq!(tier, None);
    }

    #[test]
    fn resolve_sdk_tiered_prefers_sdk_root_flag() {
        let ws = tmp("tiered-flag");
        let g = GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: Some("/explicit/sdk".to_string()),
            target: None,
            all: false,
            format: crate::cli::Format::Text,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        };
        let (path, tier) = resolve_sdk_tiered(&g, &ws);
        assert_eq!(path.as_deref(), Some("/explicit/sdk"));
        assert_eq!(tier, tan_core::SdkSourceTier::SdkRootFlag);
        // `--sdk-root` is deterministic regardless of the real machine's
        // `~/.alp` state — the lower tiers (project pin / global default /
        // discovery) touch the actual home directory and process cwd, so they
        // are covered purely in `tan_core::sdk`'s `resolve_sdk_source_tier`
        // tests instead of here.
    }
}
