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

use std::path::Path;
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
    use super::capture_tail;

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
}
