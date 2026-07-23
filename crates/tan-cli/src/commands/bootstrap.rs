// SPDX-License-Identifier: Apache-2.0
//! `tan bootstrap` — set up the SDK's build environment.
//!
//! Orchestrates the SDK's own canonical `scripts/bootstrap.sh` (install west,
//! create the Zephyr workspace via `west init`/`west update`, install Zephyr's
//! Python requirements). The CLI does not reimplement the per-OS steps — the SDK
//! owns them. The compiler toolchains (Zephyr SDK, vendor SDKs) stay out of
//! scope; `doctor` detects + points to those.
//!
//! Text mode inherits stdio so the (long) install streams live in the caller's
//! terminal; JSON mode captures the run and emits a single envelope.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;

use super::CommandRun;
use crate::cli::{BootstrapArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// `data` payload for the `bootstrap` envelope: the resolved SDK root, the
/// `bootstrap.sh` path, and the pass-through flags forwarded to the script.
#[derive(Serialize)]
struct BootstrapData {
    /// Payload schema version (`"1"`); serialized as `schemaVersion`.
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved alp-sdk root; empty on failure paths.
    #[serde(rename = "sdkRoot")]
    sdk_root: String,
    /// Absolute path to `<sdkRoot>/scripts/bootstrap.sh`; empty on failure paths.
    #[serde(rename = "scriptPath")]
    script_path: String,
    /// `--no-pip` flag forwarded to `bootstrap.sh` (skip Python requirements).
    #[serde(rename = "noPip")]
    no_pip: bool,
    /// `--no-west` flag forwarded to `bootstrap.sh` (skip west init/update).
    #[serde(rename = "noWest")]
    no_west: bool,
    /// `--print-env` flag forwarded to `bootstrap.sh` (print env, no install).
    #[serde(rename = "printEnv")]
    print_env: bool,
}

/// Runs `tan bootstrap`: resolves the SDK root, then invokes the SDK's own
/// `scripts/bootstrap.sh` via `bash`. JSON mode captures the run into one
/// envelope; text mode streams the install live with inherited stdio. Returns
/// early on Windows, an unresolved SDK root, or a missing script.
pub fn run(g: &GlobalArgs, args: &BootstrapArgs) -> CommandRun {
    // bootstrap.sh is POSIX-only; on native Windows point at WSL2 / the docs.
    if cfg!(windows) {
        return failure(
            g,
            ExitCode::RuntimeFailure,
            "windows-unsupported",
            "bootstrap.sh is POSIX-only. On Windows use WSL2 (Ubuntu) or follow the native steps in docs/cross-platform-setup.md §4.",
            empty_data(args),
            vec![
                "bootstrap: not supported on native Windows.".to_string(),
                "Use WSL2 (Ubuntu) or docs/cross-platform-setup.md §4.".to_string(),
            ],
        );
    }

    let context = resolve_cli_project_context(g);
    let Some(sdk_root) = context.sdk_root.clone() else {
        return failure(
            g,
            ExitCode::ValidationFailure,
            "sdk-root-unresolved",
            "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk switch \
             <version|path>`, or run `tan sdk install <version>` first.",
            empty_data(args),
            vec!["bootstrap: alp-sdk root is unresolved.".to_string()],
        );
    };

    let script = Path::new(&sdk_root).join("scripts").join("bootstrap.sh");
    let script_str = script.to_string_lossy().to_string();
    if !script.exists() {
        return failure(
            g,
            ExitCode::RuntimeFailure,
            "script-missing",
            &format!("bootstrap.sh not found at {script_str}; is this a valid alp-sdk checkout?"),
            empty_data(args),
            vec![format!("bootstrap: {script_str} not found.")],
        );
    }

    // Reconcile a stale `.west/config` manifest.path BEFORE handing off to
    // bootstrap.sh (#31): its "already initialised" branch runs `west update`
    // without re-running `west init -l`, so a config left over from a
    // different SDK checkout under the same topdir would otherwise silently
    // pull the WRONG SDK's west.yml. Skipped for --no-west: that flag does no
    // west init/update at all, so there is nothing to reconcile.
    if !args.no_west {
        if let Some((config_path, _old_rel, new_rel)) = reconcile_west_manifest_path(&sdk_root) {
            if !g.is_json() {
                println!(
                    "bootstrap: reconciled {} manifest.path -> {new_rel}",
                    config_path.display()
                );
            }
        }
    }

    let mut sh_args: Vec<String> = vec![script_str.clone()];
    if args.no_pip {
        sh_args.push("--no-pip".to_string());
    }
    if args.no_west {
        sh_args.push("--no-west".to_string());
    }
    if args.print_env {
        sh_args.push("--print-env".to_string());
    }

    let data = BootstrapData {
        schema_version: "1".to_string(),
        sdk_root: sdk_root.clone(),
        script_path: script_str.clone(),
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
    };
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    if g.is_json() {
        // Capture the run; emit exactly one envelope on stdout.
        //
        // Used to be `.output().ok().and_then(|o| o.status.code())`, which
        // threw away both the `io::Error` from a failed spawn (bash missing
        // from PATH, sandboxed CI PATH) AND the captured stdout/stderr on any
        // non-zero exit — every failure collapsed into the same generic
        // "bootstrap.sh reported a failure; re-run without --format json to
        // see the log" message, even for a launch error bootstrap.sh never
        // got the chance to produce. The output was already captured right
        // here; fold its tail into the issue instead of discarding it.
        let outcome = Command::new("bash").args(&sh_args).output();
        let (exit, issues) = match outcome {
            Ok(o) if o.status.success() => (ExitCode::Success, Vec::new()),
            Ok(o) => {
                let tail = capture_tail(&o.stdout, &o.stderr);
                let message = if tail.is_empty() {
                    "bootstrap.sh reported a failure; re-run without --format json to see the log."
                        .to_string()
                } else {
                    format!("bootstrap.sh reported a failure: {tail}")
                };
                (
                    ExitCode::RuntimeFailure,
                    vec![Issue {
                        code: "bootstrap.failed".to_string(),
                        severity: "error".to_string(),
                        message,
                    }],
                )
            }
            Err(e) => (
                ExitCode::RuntimeFailure,
                vec![Issue {
                    code: "bootstrap.launch-failed".to_string(),
                    severity: "error".to_string(),
                    message: format!("failed to launch bash: {e}"),
                }],
            ),
        };
        let json = Envelope::new("bootstrap", project, data, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        // Text mode: stream the install live (inherited stdio).
        let status = Command::new("bash").args(&sh_args).status();
        let (exit, line) = match status {
            Ok(s) if s.success() => (ExitCode::Success, "bootstrap: complete.".to_string()),
            Ok(_) => (
                ExitCode::RuntimeFailure,
                "bootstrap: failed (see log above).".to_string(),
            ),
            Err(e) => (
                ExitCode::RuntimeFailure,
                format!("bootstrap: failed to launch bash: {e}"),
            ),
        };
        CommandRun {
            exit,
            text: vec![line],
            json: None,
        }
    }
}

/// Reconciles a stale `[manifest] path` in `<dirname(sdk_root)>/.west/config`
/// to `sdk_root`, when they diverge (#31). `west init -l <sdk_root>` sets
/// `topdir = dirname(sdk_root)` and writes `[manifest] path = <basename of
/// sdk_root>`; `bootstrap.sh`'s "already initialised" branch runs `west
/// update` without re-running `west init -l`, so a `.west/config` left behind
/// by a *different* SDK checkout that shares the same topdir (e.g. `tan sdk
/// switch` between two `~/.alp/sdk-cache/<version>` entries) keeps pointing at
/// the stale SDK's `west.yml`.
///
/// Conservative and non-fatal by construction: every failure mode (no parent
/// dir, no `.west/config`, unreadable, no `[manifest] path` line, write
/// failure) falls through to `None` — bootstrap.sh has its own guards, this
/// only fixes the clear-divergence case. Returns `(config_path, old_rel,
/// new_rel)` when it rewrote the file, for the optional text-mode info line.
fn reconcile_west_manifest_path(sdk_root: &str) -> Option<(PathBuf, String, String)> {
    let sdk_root_path = Path::new(sdk_root);
    let topdir = sdk_root_path.parent()?;
    let config_path = topdir.join(".west").join("config");
    let contents = std::fs::read_to_string(&config_path).ok()?;
    let current_rel = tan_core::get_manifest_path(&contents)?;

    let configured = topdir.join(&current_rel);
    if same_directory(&configured, sdk_root_path) {
        return None; // already matches -- nothing to do.
    }

    let new_rel = sdk_root_path.file_name()?.to_string_lossy().into_owned();
    let rewritten = tan_core::set_manifest_path(&contents, &new_rel)?;
    std::fs::write(&config_path, &rewritten).ok()?;
    Some((config_path, current_rel, new_rel))
}

/// True when `a` and `b` name the same directory. Canonicalizes when both
/// exist on disk (the reliable answer); falls back to a lexical
/// `tan_core::normalize_path` comparison when either side doesn't (e.g. the
/// stale config's target SDK version was since pruned from the cache).
fn same_directory(a: &Path, b: &Path) -> bool {
    match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        (Ok(ca), Ok(cb)) => ca == cb,
        _ => tan_core::normalize_path(a) == tan_core::normalize_path(b),
    }
}

/// The last few non-empty lines of `bootstrap.sh`'s captured output — a pure
/// read of bytes JSON mode already captured via `Command::output()` (mirrors
/// `flash/mod.rs`'s `capture_tail`). Prefers stderr, falling back to stdout
/// when stderr is empty; returns `""` when there is nothing usable.
fn capture_tail(stdout: &[u8], stderr: &[u8]) -> String {
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

/// Builds a `BootstrapData` for failure paths: empty `sdk_root`/`script_path`,
/// but carries through the user's flag selections.
fn empty_data(args: &BootstrapArgs) -> BootstrapData {
    BootstrapData {
        schema_version: "1".to_string(),
        sdk_root: String::new(),
        script_path: String::new(),
        no_pip: args.no_pip,
        no_west: args.no_west,
        print_env: args.print_env,
    }
}

/// Assembles a `CommandRun` for an early-return failure: one `bootstrap.<code>`
/// issue, a null project, and either the JSON envelope or the given text lines
/// depending on `g.is_json()`.
fn failure(
    g: &GlobalArgs,
    exit: ExitCode,
    code: &str,
    message: &str,
    data: BootstrapData,
    text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("bootstrap.{code}"),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    // Failure paths report a null project (matches the other commands).
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let text = if g.is_json() { Vec::new() } else { text_lines };
    let json = g
        .is_json()
        .then(|| Envelope::new("bootstrap", project, data, issues, exit.code()).to_json());
    CommandRun { exit, text, json }
}

#[cfg(test)]
mod tests {
    use super::{capture_tail, reconcile_west_manifest_path};
    use std::path::PathBuf;

    /// Fresh temp dir for one test, tagged and pid-scoped like the other
    /// command test suites in this crate (sdk.rs, clean.rs, flash/mod.rs, …).
    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-bootstrap-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn capture_tail_prefers_stderr_and_keeps_last_lines_in_order() {
        // Regression: JSON-mode bootstrap used to discard the captured
        // stdout/stderr entirely (`.output().ok().and_then(|o| o.status.code())`),
        // so the actual failure reason (e.g. a pip traceback, "no such file")
        // never reached the JSON envelope. `capture_tail` is the read of that
        // already-captured output the fix now folds into the issue message.
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

    #[test]
    fn reconcile_rewrites_a_divergent_manifest_path() {
        // Two cached SDK versions sharing one topdir (~/.alp/sdk-cache/*):
        // `.west/config` still names the FIRST one bootstrap ran `west init
        // -l` against; `tan sdk switch`-ing to the second must reconcile it.
        let topdir = tmp("reconcile-divergent");
        std::fs::create_dir_all(topdir.join("v0.6.0")).unwrap();
        let new_sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.6.0\nfile = west.yml\n[zephyr]\nbase = zephyr\n",
        )
        .unwrap();

        let (config_path, old_rel, new_rel) =
            reconcile_west_manifest_path(&new_sdk.to_string_lossy()).expect("expected a rewrite");
        assert_eq!(config_path, west_dir.join("config"));
        assert_eq!(old_rel, "v0.6.0");
        assert_eq!(new_rel, "v0.7.0");
        assert_eq!(
            std::fs::read_to_string(&config_path).unwrap(),
            "[manifest]\npath = v0.7.0\nfile = west.yml\n[zephyr]\nbase = zephyr\n"
        );
    }

    #[test]
    fn reconcile_is_a_no_op_when_the_manifest_path_already_matches() {
        let topdir = tmp("reconcile-matching");
        let sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&sdk).unwrap();
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        let original = "[manifest]\npath = v0.7.0\nfile = west.yml\n";
        std::fs::write(west_dir.join("config"), original).unwrap();

        assert!(reconcile_west_manifest_path(&sdk.to_string_lossy()).is_none());
        assert_eq!(
            std::fs::read_to_string(west_dir.join("config")).unwrap(),
            original
        );
    }

    #[test]
    fn reconcile_rewrites_when_the_configured_old_dir_was_pruned_from_the_cache() {
        // The configured `path = <old>` dir no longer exists on disk (the
        // old SDK cache entry was pruned) but the new sdk_root does --
        // exercises `same_directory`'s `normalize_path` fallback branch
        // (canonicalize fails for the missing side).
        let topdir = tmp("reconcile-pruned-old");
        let new_sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&new_sdk).unwrap();
        // deliberately NOT creating topdir/v0.6.0.
        let west_dir = topdir.join(".west");
        std::fs::create_dir_all(&west_dir).unwrap();
        std::fs::write(
            west_dir.join("config"),
            "[manifest]\npath = v0.6.0\nfile = west.yml\n",
        )
        .unwrap();

        let (config_path, old_rel, new_rel) =
            reconcile_west_manifest_path(&new_sdk.to_string_lossy()).expect("expected a rewrite");
        assert_eq!(config_path, west_dir.join("config"));
        assert_eq!(old_rel, "v0.6.0");
        assert_eq!(new_rel, "v0.7.0");
        assert_eq!(
            std::fs::read_to_string(&config_path).unwrap(),
            "[manifest]\npath = v0.7.0\nfile = west.yml\n"
        );
    }

    #[test]
    fn reconcile_is_a_no_op_without_a_west_config() {
        let topdir = tmp("reconcile-no-config");
        let sdk = topdir.join("v0.7.0");
        std::fs::create_dir_all(&sdk).unwrap();
        // No .west/config at all -- bootstrap.sh's own `west init -l` guard
        // handles this case; reconcile must not fail or fabricate anything.
        assert!(reconcile_west_manifest_path(&sdk.to_string_lossy()).is_none());
    }
}
