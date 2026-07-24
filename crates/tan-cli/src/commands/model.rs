// SPDX-License-Identifier: Apache-2.0
//! `tan model` envelope helpers — pure argv/exit/wrap logic for the `alp_cli
//! model` forward. `alp_cli model <build|list|info|doctor> --format json`
//! already emits its own JSON payload; tan doesn't re-derive it, it wraps it
//! in the stable `{command,ok,exitCode,project,data,issues}` envelope so a
//! JSON consumer (the vscode extension) gets one contract for every `tan`
//! subcommand. `model run` streams interactively (like `sdk_cli`'s other
//! forwards) and is not wrappable — see `wrappable_sub`.
//!
//! Pure helpers only; the IO (spawn `python -m alp_cli model …`, capture
//! stdout/stderr) is wired in a follow-up, which is also what calls these —
//! `allow(dead_code)` until that lands.
#![allow(dead_code)]

use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// The `alp_cli model` subcommands whose `--format json` payload tan can wrap
/// in its own envelope. `model run` is interactive/streaming (like the other
/// `sdk_cli` forwards) and is deliberately excluded — Phase-3.
pub const WRAPPABLE_SUBS: [&str; 4] = ["build", "list", "info", "doctor"];

/// Returns the first passthrough token iff it names a wrappable `model` sub
/// (`WRAPPABLE_SUBS`); `None` for empty passthrough, `run`, `--help`, or any
/// other unknown token.
pub fn wrappable_sub(passthrough: &[String]) -> Option<&str> {
    let first = passthrough.first()?.as_str();
    WRAPPABLE_SUBS.iter().copied().find(|&sub| sub == first)
}

/// Build the `alp_cli` argv tail: `-m alp_cli model <passthrough…>`, appending
/// `--format json` when `json` is set unless the passthrough already carries
/// a `--format` token (dedup — mirrors `sdk_cli::build_argv`'s `--json` dedup;
/// the user may have typed `--format` themselves after `model`).
pub fn model_argv(passthrough: &[String], json: bool) -> Vec<String> {
    let mut argv = vec!["-m".to_string(), "alp_cli".to_string(), "model".to_string()];
    argv.extend(passthrough.iter().cloned());
    if json && !passthrough.iter().any(|a| a == "--format") {
        argv.push("--format".to_string());
        argv.push("json".to_string());
    }
    argv
}

/// Map the spawned child's exit code to tan's stable `ExitCode`: `Some(0)` is
/// success, any other code (or a spawn that produced none) is a runtime
/// failure — `model` has no validation/write/doctor exit class of its own.
pub fn map_model_exit(child_code: Option<i32>) -> ExitCode {
    match child_code {
        Some(0) => ExitCode::Success,
        _ => ExitCode::RuntimeFailure,
    }
}

/// Wrap a captured `alp_cli model` run in tan's envelope. `stdout` is parsed
/// as JSON for `data`; a non-`Success` `exit` adds one `model.failed` issue
/// carrying the trimmed `stderr` (or a generic line when stderr is empty) —
/// checked first so it sorts before a `bad-payload` issue when both fire (e.g.
/// a failed run with empty stdout). An unparseable `stdout` adds a
/// `model.bad-payload` issue and `data` becomes `null`.
pub fn wrap_model_json(stdout: &str, stderr: &str, exit: ExitCode, project: Project) -> String {
    let mut issues = Vec::new();

    if exit != ExitCode::Success {
        let message = if stderr.trim().is_empty() {
            "alp_cli model failed with no diagnostic output.".to_string()
        } else {
            stderr.trim().to_string()
        };
        issues.push(Issue {
            code: "model.failed".to_string(),
            severity: "error".to_string(),
            message,
        });
    }

    let data = match serde_json::from_str::<serde_json::Value>(stdout) {
        Ok(v) => v,
        Err(e) => {
            issues.push(Issue {
                code: "model.bad-payload".to_string(),
                severity: "error".to_string(),
                message: format!("could not parse alp_cli model output as JSON: {e}"),
            });
            serde_json::Value::Null
        }
    };

    Envelope::new("model", project, data, issues, exit.code()).to_json()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrappable_sub_detects_the_four_and_rejects_others() {
        assert_eq!(
            wrappable_sub(&["build".into(), "--board".into(), "b.yaml".into()]),
            Some("build")
        );
        assert_eq!(wrappable_sub(&["doctor".into()]), Some("doctor"));
        assert_eq!(wrappable_sub(&["run".into(), "m".into()]), None); // Phase-3, streams
        assert_eq!(wrappable_sub(&["--help".into()]), None);
        assert_eq!(wrappable_sub(&[]), None);
    }

    #[test]
    fn model_argv_appends_format_json_once() {
        let a = model_argv(&["build".into(), "--board".into(), "b.yaml".into()], true);
        assert_eq!(
            a,
            vec![
                "-m", "alp_cli", "model", "build", "--board", "b.yaml", "--format", "json"
            ]
        );
        // dedup: user already passed --format
        let b = model_argv(&["build".into(), "--format".into(), "json".into()], true);
        assert_eq!(b.iter().filter(|s| *s == "--format").count(), 1);
        // text mode: no --format appended
        let c = model_argv(&["build".into()], false);
        assert_eq!(c, vec!["-m", "alp_cli", "model", "build"]);
    }

    #[test]
    fn map_model_exit_maps_success_and_failure() {
        assert_eq!(map_model_exit(Some(0)), ExitCode::Success);
        assert_eq!(map_model_exit(Some(1)), ExitCode::RuntimeFailure);
        assert_eq!(map_model_exit(None), ExitCode::RuntimeFailure);
    }

    #[test]
    fn wrap_model_json_passes_payload_through_as_data() {
        let payload = r#"{"models":[{"name":"demo","targets":[]}]}"#;
        let doc = wrap_model_json(
            payload,
            "",
            ExitCode::Success,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["command"], "model");
        assert_eq!(v["ok"], true);
        assert_eq!(v["exitCode"], 0);
        assert_eq!(v["data"]["models"][0]["name"], "demo");
        assert_eq!(v["issues"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn wrap_model_json_reports_failure_and_bad_payload() {
        // non-success exit → one model.failed issue carrying stderr
        let doc = wrap_model_json(
            "",
            "no blob compiled",
            ExitCode::RuntimeFailure,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], 1);
        assert_eq!(v["issues"][0]["code"], "model.failed");
        assert!(
            v["issues"][0]["message"]
                .as_str()
                .unwrap()
                .contains("no blob compiled")
        );
        // unparseable stdout on a "success" exit → model.bad-payload + null data
        let doc2 = wrap_model_json(
            "not json",
            "",
            ExitCode::Success,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        let v2: serde_json::Value = serde_json::from_str(&doc2).unwrap();
        assert_eq!(v2["issues"][0]["code"], "model.bad-payload");
        assert!(v2["data"].is_null());
    }
}
