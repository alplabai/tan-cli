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

/// A bare `RuntimeFailure` result with no text/JSON, returned when the user
/// cancels an interactive prompt.
pub(super) fn runtime_failure_run() -> CommandRun {
    CommandRun {
        exit: ExitCode::RuntimeFailure,
        text: vec![],
        json: None,
    }
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
