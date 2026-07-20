// SPDX-License-Identifier: Apache-2.0
//! `tan completion` — emit a shell completion script.
//!
//! Parity with TS `runCompletionCommand`: the scripts are embedded verbatim
//! via `include_str!` so the contract envelope's `script` field is exact. They
//! started as byte-for-byte captures of the reference TS CLI's output (12
//! commands); native-only commands (`build`, `flash`, `run`, …) have since
//! been added by hand, so `embedded_scripts_list_every_cli_command` below
//! checks them against `cli::Command` on every test run instead of trusting
//! them to stay in sync.

use super::CommandRun;
use crate::cli::{CompletionArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// Verbatim bash completion script, captured from the reference TS CLI.
const BASH_SCRIPT: &str = include_str!("completion_scripts/bash.bash");
/// Verbatim zsh completion script, captured from the reference TS CLI.
const ZSH_SCRIPT: &str = include_str!("completion_scripts/zsh.zsh");
/// Verbatim fish completion script, captured from the reference TS CLI.
const FISH_SCRIPT: &str = include_str!("completion_scripts/fish.fish");

/// Envelope `data` payload for `completion`: the resolved shell and its script.
#[derive(serde::Serialize)]
struct CompletionData {
    /// Data schema version, serialized as `schemaVersion`; always `"1"`.
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Resolved shell name (`bash`/`zsh`/`fish`).
    shell: String,
    /// The emitted completion script body.
    script: String,
}

/// Build an empty `Project` (no root / `board.yaml`); `completion` is project-agnostic.
fn null_project() -> Project {
    Project {
        root: None,
        board_yaml: None,
    }
}

/// Mirror TS `resolveShell`: default `bash`; trim + lowercase; else unsupported.
fn resolve_shell(raw: Option<&str>) -> Option<&'static str> {
    match raw.unwrap_or("bash").trim().to_ascii_lowercase().as_str() {
        "bash" => Some("bash"),
        "zsh" => Some("zsh"),
        "fish" => Some("fish"),
        _ => None,
    }
}

/// Select the embedded completion script for `shell`; unknown shells fall back to bash.
fn script_for(shell: &str) -> &'static str {
    match shell {
        "zsh" => ZSH_SCRIPT,
        "fish" => FISH_SCRIPT,
        _ => BASH_SCRIPT,
    }
}

/// Run `tan completion`: resolve `--shell` and emit its script, or fail with
/// `completion.shell-unsupported` (exit `RuntimeFailure`) for an unsupported shell.
pub fn run(g: &GlobalArgs, args: &CompletionArgs) -> CommandRun {
    let Some(shell) = resolve_shell(args.shell.as_deref()) else {
        let issues = vec![Issue {
            code: "completion.shell-unsupported".to_string(),
            severity: "error".to_string(),
            message: "Unsupported shell. Allowed values: bash, zsh, fish.".to_string(),
        }];
        let data = CompletionData {
            schema_version: "1".to_string(),
            shell: "bash".to_string(),
            script: String::new(),
        };
        let text = if g.is_json() {
            Vec::new()
        } else {
            vec!["completion: unsupported shell. Use --shell bash|zsh|fish.".to_string()]
        };
        let json = g.is_json().then(|| {
            Envelope::new(
                "completion",
                null_project(),
                data,
                issues,
                ExitCode::RuntimeFailure.code(),
            )
            .to_json()
        });
        return CommandRun {
            exit: ExitCode::RuntimeFailure,
            text,
            json,
        };
    };

    let script = script_for(shell).to_string();
    if !g.is_json() {
        // The script IS the payload (README: "`tan completion --shell zsh`
        // emits a completion script"), and the only sane way to consume it is
        // `eval "$(tan completion --shell zsh)"` / `> file` stdout capture.
        // `main::emit` sends every `CommandRun::text` line to STDERR (correct
        // for status/diagnostic prose elsewhere), so routing the script
        // through `text` made both the eval and the redirect capture nothing
        // while the script scrolled past looking like an error dump. Print
        // the payload straight to stdout instead, and leave `text` empty so
        // nothing doubles up on stderr.
        println!("{script}");
    }
    let text = Vec::new();
    let data = CompletionData {
        schema_version: "1".to_string(),
        shell: shell.to_string(),
        script,
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "completion",
            null_project(),
            data,
            Vec::new(),
            ExitCode::Success.code(),
        )
        .to_json()
    });

    CommandRun {
        exit: ExitCode::Success,
        text,
        json,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    #[test]
    fn resolve_shell_defaults_and_normalizes() {
        assert_eq!(resolve_shell(None), Some("bash"));
        assert_eq!(resolve_shell(Some("  ZSH ")), Some("zsh"));
        assert_eq!(resolve_shell(Some("fish")), Some("fish"));
        assert_eq!(resolve_shell(Some("tcsh")), None);
    }

    #[test]
    fn scripts_are_nonempty_and_shell_specific() {
        assert!(BASH_SCRIPT.contains("_tan_complete"));
        assert!(ZSH_SCRIPT.contains("#compdef tan"));
        assert!(FISH_SCRIPT.contains("__fish_use_subcommand"));
        // The captured zsh script collapses the `_arguments -C` continuations.
        assert!(ZSH_SCRIPT.contains("_arguments -C     '1:command:->command'"));
    }

    /// Word-boundary substring check: a bare `.contains` would also match a
    /// command name that is a fragment of an unrelated token, silently hiding
    /// a real drift.
    fn script_lists(script: &str, name: &str) -> bool {
        script
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '-')
            .any(|tok| tok == name)
    }

    /// The scripts used to be a frozen 12-command capture of the retired TS
    /// CLI while `cli::Command` grew to 31 variants, so `tan build<TAB>` (and
    /// every command added since) offered nothing on any shell. Read the
    /// authoritative command list straight off clap's own command graph
    /// (never hand-duplicate it here) so this fails the moment a future
    /// `Command` variant ships without a matching completion entry.
    #[test]
    fn embedded_scripts_list_every_cli_command() {
        use clap::CommandFactory;
        let root = crate::cli::Cli::command();
        let missing: Vec<String> = root
            .get_subcommands()
            .map(|c| c.get_name().to_string())
            .filter(|name| {
                !script_lists(BASH_SCRIPT, name)
                    || !script_lists(ZSH_SCRIPT, name)
                    || !script_lists(FISH_SCRIPT, name)
            })
            .collect();
        assert!(
            missing.is_empty(),
            "completion scripts missing commands: {missing:?}"
        );
    }

    fn global(format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    /// Regression for the completion script going to stdout: before the fix,
    /// a successful text-mode run returned the script split into `text`
    /// lines, which `main::emit` writes with `eprintln!` — so
    /// `eval "$(tan completion --shell zsh)"` and `... > file` both captured
    /// nothing. The payload now goes straight to stdout via `println!`
    /// inside `run`, so `text` must come back empty.
    #[test]
    fn text_mode_success_leaves_text_empty_script_goes_to_stdout() {
        let run = run(&global(Format::Text), &CompletionArgs { shell: None });
        assert_eq!(run.exit.code(), 0);
        assert!(run.text.is_empty());
    }
}
