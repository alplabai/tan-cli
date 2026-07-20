// SPDX-License-Identifier: Apache-2.0
//! `tan init` response builders: assemble the `Project`/`InitData` envelope
//! blocks and the shared error/cancel `CommandRun` results.

use crate::cli::GlobalArgs;
use crate::commands::CommandRun;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

use super::{FileChangeSer, InitData};

/// Build the envelope `Project` block, recording `destination` as the root
/// (no `board.yaml` exists yet at init time).
pub(super) fn make_project(destination: &str) -> Project {
    Project {
        root: Some(destination.to_string()),
        board_yaml: None,
    }
}

/// Build an `InitData` payload with empty `written`/`unchanged` lists, used for
/// preview and overwrite-guard responses where no files are actually written.
pub(super) fn empty_data(
    template_id: &str,
    destination: &str,
    preview: bool,
    file_changes: Vec<FileChangeSer>,
) -> InitData {
    InitData {
        schema_version: "1".to_string(),
        template_id: template_id.to_string(),
        destination: destination.to_string(),
        preview,
        file_changes,
        written: vec![],
        unchanged: vec![],
    }
}

/// Build a `RuntimeFailure` `CommandRun` for a cancelled interactive prompt.
/// Always routes through `error_run` so it carries a real envelope — a bare
/// `json: None` here meant an interactive prompt reached under `--format json`
/// (which does not by itself imply non-interactive; see `run()`) could be
/// cancelled and exit 1 with zero bytes on stdout, which the extension's
/// `JSON.parse(stdout)` cannot recover from.
pub(super) fn runtime_failure_run(g: &GlobalArgs) -> CommandRun {
    error_run(g, ExitCode::RuntimeFailure, "init.cancelled", "Cancelled.")
}

/// Build an error `CommandRun` carrying a single error `Issue` (and a matching
/// text/JSON envelope) for the given `exit` code, issue `code`, and `message`.
/// Validation errors pass `ValidationFailure` (exit 2), write errors
/// `WriteFailure` (exit 3) — matching the CLI exit-code contract.
pub(super) fn error_run(g: &GlobalArgs, exit: ExitCode, code: &str, message: &str) -> CommandRun {
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let data = InitData {
        schema_version: "1".to_string(),
        template_id: String::new(),
        destination: String::new(),
        preview: false,
        file_changes: vec![],
        written: vec![],
        unchanged: vec![],
    };
    let text = if g.is_json() {
        vec![]
    } else {
        vec![format!("init: {message}")]
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("init", project, data, issues, exit.code()).to_json());
    CommandRun { exit, text, json }
}

/// Build a `WriteFailure` `CommandRun` for a failed `write_wizard_files` call,
/// carrying the partial `written`/`unchanged` lists accumulated before the
/// failure. `error_run` always reports empty lists — right for validation
/// failures where nothing was touched, wrong here: the files in `partial`
/// really are on disk, and reporting `written: []` for a half-created project
/// leaves an extension with no idea what to clean up or reopen.
pub(super) fn write_error_run(
    g: &GlobalArgs,
    message: &str,
    partial: tan_core::wizard::WizardWriteResult,
) -> CommandRun {
    let project = Project {
        root: None,
        board_yaml: None,
    };
    let issues = vec![Issue {
        code: "init.write-failed".to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let data = InitData {
        schema_version: "1".to_string(),
        template_id: String::new(),
        destination: String::new(),
        preview: false,
        file_changes: vec![],
        written: partial.written,
        unchanged: partial.unchanged,
    };
    let text = if g.is_json() {
        vec![]
    } else {
        vec![format!("init: {message}")]
    };
    let json = g.is_json().then(|| {
        Envelope::new("init", project, data, issues, ExitCode::WriteFailure.code()).to_json()
    });
    CommandRun {
        exit: ExitCode::WriteFailure,
        text,
        json,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn json_globals() -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    #[test]
    fn cancelled_prompt_still_emits_a_json_envelope() {
        // Regression: `runtime_failure_run` used to return `json: None`
        // unconditionally, so `--format json` produced zero bytes on stdout
        // when a prompt was cancelled — unparseable by the extension.
        let run = runtime_failure_run(&json_globals());
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let json = run.json.expect("json envelope must be present on cancel");
        assert!(json.contains("\"ok\":false"));
        assert!(json.contains("init.cancelled"));
    }

    #[test]
    fn write_error_run_reports_the_partial_written_list() {
        // Regression: a write failure used to go through `error_run`, which
        // always reports `written: []` even when files landed before the
        // error.
        let partial = tan_core::wizard::WizardWriteResult {
            written: vec!["board.yaml".to_string()],
            unchanged: vec![],
        };
        let run = write_error_run(&json_globals(), "disk full", partial);
        assert_eq!(run.exit, ExitCode::WriteFailure);
        let json = run.json.expect("json envelope must be present");
        assert!(json.contains("\"written\":[\"board.yaml\"]"));
    }
}
