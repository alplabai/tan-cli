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

/// A user-facing error string when `binary` is a Python older than
/// [`MIN_PYTHON`]; `None` when it is new enough OR its version can't be
/// determined (don't block on an unknown — let the real call run and surface
/// its own error). Turns the SDK's cryptic `dataclass(slots=True)` traceback
/// into an actionable message.
pub fn python_too_old(binary: &str) -> Option<String> {
    match python_version(binary) {
        Some(found) if found < MIN_PYTHON => Some(format!(
            "Python {}.{} found at `{}`, but alp-sdk requires Python {}.{}+. \
             Put a newer `python` on PATH (or set alpSdk.pythonPath in the \
             VS Code extension).",
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
/// through to the next tier instead of locking the user out. Returns `""` when
/// nothing applies (core auto-discovery in `tan_core::project` takes it from
/// there). Pure — filesystem access is injected for unit testing.
fn effective_sdk_path_with(
    sdk_root_arg: Option<&str>,
    workspace_root: &str,
    global_default_dir: &str,
    has_loader: &impl Fn(&str) -> bool,
    path_exists: &impl Fn(&str) -> bool,
    read_file: &impl Fn(&str) -> Option<String>,
) -> String {
    if let Some(root) = sdk_root_arg {
        if !root.trim().is_empty() {
            return root.to_string();
        }
    }
    if let Some(pointer) = resolve_active_sdk(workspace_root, path_exists, read_file) {
        if has_loader(&pointer) {
            return pointer;
        }
    }
    if let Some(pointer) = resolve_global_default_sdk(global_default_dir, path_exists, read_file) {
        if has_loader(&pointer) {
            return pointer;
        }
    }
    String::new()
}

/// Filesystem-backed [`effective_sdk_path_with`]: `--sdk-root` > `.alp/sdk-path`
/// pointer (best-effort) > `~/.alp/sdk-default` (best-effort) > `""` (auto-discovery).
fn effective_sdk_path(g: &GlobalArgs, workspace_root: &Path) -> String {
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
            return Some(candidate);
        }
    }

    discover_sdk_root(workspace_root)
}

/// Sibling/workspace auto-discovery: the workspace root itself, then sibling
/// `alp-sdk` / `alp-sdk-upstream` directories, first one with the loader
/// script wins. The tail of [`resolve_sdk_root`]'s precedence chain (the
/// generate/init/examples family) — deliberately NOT what
/// [`resolve_sdk_tiered`]'s discovery tier reports; see
/// [`tan_core::discover_workspace_sdk`] for that.
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

    candidates.into_iter().find(|c| has_loader_script(c))
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

/// Resolve the project context from the global args, mirroring the TS commands'
/// `path.resolve(cwd, project) + resolveProjectContext` boilerplate. Shared by
/// `validate`, `diff`, `presets`, and `doctor`.
pub fn resolve_cli_project_context(g: &GlobalArgs) -> ProjectContext {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_arg = g.project.clone().unwrap_or_else(|| ".".to_string());
    let workspace_root = normalize_path(&cwd.join(&project_arg))
        .to_string_lossy()
        .to_string();

    let settings = ProjectSettings {
        // `--sdk-root` > `.alp/sdk-path` pointer > `""` (core auto-discovery).
        sdk_path: effective_sdk_path(g, Path::new(&workspace_root)),
        python_path: String::new(),
        board_yaml_path: g
            .board_yaml
            .clone()
            .unwrap_or_else(|| "board.yaml".to_string()),
        west_cwd: String::new(),
    };
    resolve_project_context(
        &ProjectResolutionInput {
            workspace_folders: vec![workspace_root],
            settings,
            is_windows: cfg!(windows),
        },
        |p| Path::new(p).exists(),
    )
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
        let got = effective_sdk_path_with(
            Some("/explicit"),
            "/work",
            "/home/.alp",
            &|_| true,
            &exists,
            &read,
        );
        assert_eq!(got, "/explicit");
    }

    #[test]
    fn blank_sdk_root_arg_falls_through_to_pointer() {
        let (exists, read) = pointer_fs("/from/pointer");
        let got = effective_sdk_path_with(
            Some("   "),
            "/work",
            "/home/.alp",
            &|p| p == "/from/pointer",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/pointer");
    }

    #[test]
    fn valid_pointer_is_used_when_no_sdk_root_arg() {
        let (exists, read) = pointer_fs("/from/pointer");
        let got = effective_sdk_path_with(
            None,
            "/work",
            "/home/.alp",
            &|p| p == "/from/pointer",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/pointer");
    }

    #[test]
    fn stale_pointer_falls_through_to_global_default_then_discovery() {
        let (exists, read) = pointer_fs("/gone");
        // Pointer resolves but its target has no loader script -> best-effort skip,
        // and the global-default lookup here has no matching pointer file either.
        let got = effective_sdk_path_with(None, "/work", "/home/.alp", &|_| false, &exists, &read);
        assert_eq!(got, "");
    }

    #[test]
    fn no_arg_and_no_pointer_yields_empty() {
        let got =
            effective_sdk_path_with(None, "/work", "/home/.alp", &|_| true, &|_| false, &|_| {
                None
            });
        assert_eq!(got, "");
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
        let got = effective_sdk_path_with(
            None,
            "/work",
            "/home/.alp",
            &|p| p == "/from/global",
            &exists,
            &read,
        );
        assert_eq!(got, "/from/global");
    }

    #[test]
    fn stale_global_default_falls_through_to_empty() {
        let (exists, read) = global_default_fs("/gone");
        // Pointer resolves but its target has no loader script -> best-effort skip.
        let got = effective_sdk_path_with(None, "/work", "/home/.alp", &|_| false, &exists, &read);
        assert_eq!(got, "");
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
