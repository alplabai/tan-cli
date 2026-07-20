// SPDX-License-Identifier: Apache-2.0
//! Thin forwarders to the SDK's `alp` click CLI (`python -m alp_cli <sub>`):
//! `tan model` / `tan monitor` / `tan new-som` / `tan faultdecode`.
//!
//! Under ADR-0020 the model packaging, serial console, SoM scaffolding and
//! fault-decode arithmetic all live in the SDK (`scripts/alp_cli/`); `tan` is
//! only the command surface. These verbs spawn `python -m alp_cli <sub>` against
//! the resolved SDK checkout and pass the user's args through verbatim,
//! **inheriting stdio** so `monitor`'s interactive miniterm, `faultdecode`'s
//! stdin dump, and `new-som`'s prompts all work. Nothing here re-implements SDK
//! logic — a captured-output envelope (like the `west alp-*` forwarder) is
//! impossible for interactive tools, so these stream live and surface only the
//! child's exit class.

use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::process::Command;

use serde::Serialize;

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// Envelope `data` payload for a failed forward — just the verb, since a
/// captured-output payload is impossible for these interactive passthroughs
/// (see the module doc).
#[derive(Serialize)]
struct ForwardData {
    subcommand: String,
}

/// The default Python interpreter name: `python` on Windows, `python3` elsewhere.
fn default_python_binary() -> &'static str {
    if cfg!(target_os = "windows") {
        "python"
    } else {
        "python3"
    }
}

/// Assemble the `python -m alp_cli <sub> <args…>` argv.
///
/// In JSON mode we append the child's own `--json` flag for the one subcommand
/// that has one (`faultdecode`), so a JSON consumer gets the machine report the
/// SDK already produces — tan does not re-wrap it. The other subcommands have no
/// JSON flag; the mode is a no-op for them.
fn build_argv(subcommand: &str, passthrough: &[String], json: bool) -> Vec<String> {
    let mut argv = vec![
        "-m".to_string(),
        "alp_cli".to_string(),
        subcommand.to_string(),
    ];
    argv.extend(passthrough.iter().cloned());
    if json && subcommand == "faultdecode" && !passthrough.iter().any(|a| a == "--json") {
        argv.push("--json".to_string());
    }
    argv
}

/// Build `PYTHONPATH` for the `alp_cli` child: `sdk_scripts` prepended to
/// whatever `existing` PYTHONPATH the caller/plan set, so alp_cli's absolute
/// imports resolve while an already-set entry (e.g. a vendor NPU-converter
/// package) survives instead of being clobbered. `existing` is injected so
/// this stays pure/testable without mutating process env. Mirrors
/// `build/workspace.rs`'s planner-spawn PYTHONPATH handling.
fn build_pythonpath(
    sdk_scripts: &Path,
    existing: Option<&OsStr>,
) -> Result<OsString, std::env::JoinPathsError> {
    let mut entries = vec![sdk_scripts.to_path_buf()];
    if let Some(existing) = existing {
        entries.extend(std::env::split_paths(existing));
    }
    std::env::join_paths(entries)
}

/// Run an `alp_cli` subcommand by spawning `python -m alp_cli <sub> <args…>`
/// against the resolved SDK checkout, streaming its stdio through. `subcommand`
/// is the bare verb (`model`/`monitor`/`new-som`/`faultdecode`).
pub fn run(g: &GlobalArgs, subcommand: &str, passthrough: &[String]) -> CommandRun {
    let workspace_root = crate::util::cli_workspace_root(g);

    let Some(sdk_root) = crate::util::resolve_sdk_root(g, &workspace_root) else {
        return fail(
            g,
            subcommand,
            ExitCode::ValidationFailure,
            "alp-sdk root is unresolved. Use --sdk-root, pin one with \
             `tan sdk switch <version|path>`, or place the project near an alp-sdk checkout.",
        );
    };

    let python = default_python_binary();
    // Guard: the interpreter must be new enough to run the SDK scripts
    // (`@dataclass(slots=True)`, Python 3.10+) — parity with `generate`/`build`.
    if let Some(message) = crate::util::python_too_old(python) {
        return fail(g, subcommand, ExitCode::RuntimeFailure, &message);
    }

    let argv = build_argv(subcommand, passthrough, g.is_json());
    // Previously `.env("PYTHONPATH", sdk_root.join("scripts"))` unconditionally
    // — an outright overwrite ("ponytail: overwrite is fine — alp_cli needs
    // nothing else on PYTHONPATH", which was wrong) that silently dropped a
    // plan/user-supplied PYTHONPATH (e.g. a vendor NPU-converter package
    // `alp_cli model` imports), even though the sibling planner spawn in
    // build/workspace.rs prepends+preserves for the exact same reason.
    let pythonpath = match build_pythonpath(
        &sdk_root.join("scripts"),
        std::env::var_os("PYTHONPATH").as_deref(),
    ) {
        Ok(p) => p,
        Err(e) => {
            return fail(
                g,
                subcommand,
                ExitCode::RuntimeFailure,
                &format!("failed to build PYTHONPATH for alp_cli: {e}"),
            );
        }
    };
    let mut cmd = Command::new(python);
    cmd.args(&argv)
        .current_dir(&workspace_root)
        .env("ALP_SDK_ROOT", &sdk_root)
        // alp_cli's absolute imports (`from alp_cli import …`) resolve from
        // <sdk>/scripts; mirror `_alp_common.env_with_sdk()`.
        .env("PYTHONPATH", &pythonpath);

    // Inherit stdio (the std default) and stream live — these are interactive /
    // passthrough tools, so capturing their output would break them.
    match cmd.status() {
        Ok(s) if s.success() => CommandRun {
            exit: ExitCode::Success,
            text: Vec::new(),
            json: None,
        },
        Ok(s) => {
            let code = s.code().unwrap_or(1);
            fail(
                g,
                subcommand,
                ExitCode::RuntimeFailure,
                &format!("`alp {subcommand}` exited with code {code} (see log above)."),
            )
        }
        Err(e) => fail(
            g,
            subcommand,
            ExitCode::RuntimeFailure,
            &launch_error(python, &e),
        ),
    }
}

/// Build a failing `CommandRun`: a stderr line in text mode, plus (the fix) a
/// single-issue JSON envelope in JSON mode. This used to return `json: None`
/// unconditionally — "these forwarders have no envelope" was true for the
/// interactive child's OWN captured output, but every failure `fail()` is
/// actually used for (SDK root unresolved, Python too old/missing, launch
/// error, nonzero exit) is something tan itself already knows and can report
/// without capturing anything; `--format json` was producing zero bytes of
/// stdout for all of them.
fn fail(g: &GlobalArgs, subcommand: &str, exit: ExitCode, message: &str) -> CommandRun {
    let text = if g.is_json() {
        Vec::new()
    } else {
        vec![format!("{subcommand}: {message}")]
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            subcommand,
            Project {
                root: None,
                board_yaml: None,
            },
            ForwardData {
                subcommand: subcommand.to_string(),
            },
            vec![Issue {
                code: format!("{subcommand}.failed"),
                severity: "error".to_string(),
                message: message.to_string(),
            }],
            exit.code(),
        )
        .to_json()
    });
    CommandRun { exit, text, json }
}

/// Map a Python launch I/O error to a user-facing message — special-casing
/// `NotFound` with an install hint.
fn launch_error(python: &str, e: &std::io::Error) -> String {
    if e.kind() == std::io::ErrorKind::NotFound {
        format!("`{python}` not found on PATH — install Python 3.10+ and ensure it is on PATH.")
    } else {
        format!("failed to launch {python}: {e}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn argv_prefixes_module_and_subcommand() {
        assert_eq!(
            build_argv("monitor", &["--port".into(), "COM7".into()], false),
            vec!["-m", "alp_cli", "monitor", "--port", "COM7"]
        );
    }

    #[test]
    fn model_group_passthrough_keeps_subcommand() {
        // `tan model build --board b.yaml` → `python -m alp_cli model build …`.
        assert_eq!(
            build_argv(
                "model",
                &["build".into(), "--board".into(), "b.yaml".into()],
                false
            ),
            vec!["-m", "alp_cli", "model", "build", "--board", "b.yaml"]
        );
    }

    #[test]
    fn json_appends_faultdecode_json_flag() {
        let argv = build_argv("faultdecode", &["--cfsr".into(), "0x8200".into()], true);
        assert_eq!(argv.last().unwrap(), "--json");
    }

    #[test]
    fn json_does_not_double_the_faultdecode_json_flag() {
        let argv = build_argv("faultdecode", &["--json".into()], true);
        assert_eq!(argv.iter().filter(|s| *s == "--json").count(), 1);
    }

    #[test]
    fn json_is_a_noop_for_subcommands_without_a_json_flag() {
        // Only faultdecode has a native --json; model/monitor/new-som must not
        // get a flag they would reject.
        assert_eq!(
            build_argv("model", &["build".into()], true),
            vec!["-m", "alp_cli", "model", "build"]
        );
        assert_eq!(
            build_argv("new-som", &["--sku".into(), "E1M-XYZ101".into()], true),
            vec!["-m", "alp_cli", "new-som", "--sku", "E1M-XYZ101"]
        );
    }

    #[test]
    fn pythonpath_prepends_and_preserves_existing_value() {
        // Regression: the old code did `.env("PYTHONPATH", sdk_root.join("scripts"))`
        // unconditionally, REPLACING (not prepending to) whatever PYTHONPATH the
        // caller/plan had already set — dropping e.g. a vendor NPU-converter
        // package `alp_cli model` imports.
        let existing = if cfg!(windows) {
            OsString::from(r"C:\vendor\converter")
        } else {
            OsString::from("/opt/vendor-converter")
        };
        let got = build_pythonpath(Path::new("/sdk/scripts"), Some(existing.as_os_str())).unwrap();
        let parts: Vec<_> = std::env::split_paths(&got).collect();
        assert_eq!(parts[0], Path::new("/sdk/scripts"));
        assert!(
            parts.iter().any(|p| p == Path::new(&existing)),
            "existing PYTHONPATH entry {existing:?} was dropped, got {parts:?}"
        );
    }

    #[test]
    fn pythonpath_is_just_scripts_dir_when_nothing_inherited() {
        let got = build_pythonpath(Path::new("/sdk/scripts"), None).unwrap();
        assert_eq!(got, OsString::from("/sdk/scripts"));
    }

    #[test]
    fn fail_emits_a_json_envelope_instead_of_nothing() {
        // Regression: `fail()` used to always return `json: None`, so
        // `--format json` produced ZERO bytes of stdout for tan's own
        // preflight/launch failures (unresolved SDK root, python too old,
        // missing python, nonzero exit) — not just for the child's own
        // inherited-stdio output, which is the only part that genuinely can't
        // be wrapped.
        let g = GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: None,
            target: None,
            all: false,
            format: crate::cli::Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        };
        let run = fail(
            &g,
            "faultdecode",
            ExitCode::ValidationFailure,
            "alp-sdk root is unresolved.",
        );
        let doc = run.json.expect("json envelope must be emitted on failure");
        let data: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(data["ok"], false);
        assert_eq!(data["exitCode"], 2);
        assert_eq!(data["issues"][0]["code"], "faultdecode.failed");
    }
}
