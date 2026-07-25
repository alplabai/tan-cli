// SPDX-License-Identifier: Apache-2.0
#![doc = include_str!("../README.md")]

mod cli;
mod commands;
mod envelope;
mod exit;
mod progress;
mod style;
mod util;
mod venv;

use clap::Parser;

use cli::{Cli, Command};
use commands::CommandRun;
use envelope::{Envelope, Issue, Project};

fn main() {
    // `Cli::parse()` prints straight to stdout/stderr and calls
    // `process::exit` itself for a parse error, `--help`, or `--version` —
    // none of that goes through `emit`, so `--format json` (already sitting
    // right there in argv) gets no say: a bad flag under `--format json`
    // used to leave stdout completely empty with clap's own usage text on
    // stderr, which is not JSON at all. `try_parse` + a JSON-aware fallback
    // keeps the text-mode UX byte-identical (still delegates to `err.exit()`)
    // while giving JSON callers one parseable envelope instead of nothing.
    let args = match Cli::try_parse() {
        Ok(args) => args,
        Err(err) => std::process::exit(emit_parse_error(
            err,
            wants_json(&std::env::args().collect::<Vec<_>>()),
        )),
    };
    let global = args.global;

    let run: CommandRun = match args.command {
        Command::Validate(args) => commands::validate::run(&global, &args),
        Command::Generate(args) => commands::generate::run(&global, &args),
        Command::Init(args) => commands::init::run(&global, &args),
        Command::Scaffold(args) => commands::scaffold::run(&global, &args),
        Command::Examples => commands::examples::run(&global),
        Command::Doctor(args) => commands::doctor::run(&global, &args),
        Command::Completion(args) => commands::completion::run(&global, &args),
        Command::Diff => commands::diff::run(&global),
        Command::Presets => commands::presets::run(&global),
        Command::Pinmux(args) => commands::pinmux::run(&global, &args),
        Command::Explain(args) => commands::explain::run(&global, &args),
        Command::Inspect(args) => commands::inspect::run(&global, &args),
        Command::Trace(args) => commands::trace::run(&global, &args),
        Command::DebugConfig(args) => commands::debug_config::run(&global, &args),
        Command::SupportBundle(args) => commands::support_bundle::run(&global, &args),
        Command::Sdk(args) => commands::sdk::run(&global, &args),
        Command::Bootstrap(args) => commands::bootstrap::run(&global, &args),
        Command::Build(args) => commands::build::run_build(&global, &args),
        Command::Kconfig(args) => commands::kconfig::run(&global, &args),
        Command::Image(args) => commands::image::run(&global, &args),
        Command::Flash(args) => commands::flash::run(&global, &args),
        Command::Run(args) => commands::run::run(&global, &args),
        Command::Clean(args) => commands::clean::run(&global, &args),
        Command::Renode(args) => commands::renode::run(&global, &args),
        Command::Size(args) => commands::size::run(&global, &args),
        Command::Migrate(args) => commands::build::run(&global, "migrate", &args.args),
        Command::Lock(args) => commands::build::run(&global, "lock", &args.args),
        Command::Quality(args) => commands::build::run(&global, "quality", &args.args),
        Command::Model(args) => commands::sdk_cli::run(&global, "model", &args.args),
        Command::Monitor(args) => commands::sdk_cli::run(&global, "monitor", &args.args),
        Command::NewSom(args) => commands::sdk_cli::run(&global, "new-som", &args.args),
        Command::Faultdecode(args) => commands::sdk_cli::run(&global, "faultdecode", &args.args),
    };

    emit(&global.format, run.json.as_deref(), &run.text);
    std::process::exit(json_exit_code(
        &global.format,
        run.json.as_deref(),
        run.exit.code(),
    ));
}

/// The process exit code must agree with the `exitCode` field of the JSON
/// envelope actually printed to stdout. `Envelope::to_json`'s
/// serialize-failure fallback (an unserializable `data`, e.g. a YAML
/// passthrough with a non-string map key) rewrites the printed envelope's
/// `exitCode` to `InternalFailure` — but that happens deep inside `to_json`,
/// long after the command already set `CommandRun.exit` to its own original
/// (often `Success`) code, and `to_json` has no way back to that field. Trust
/// the JSON actually on stdout over the command's stale `exit` so a consumer
/// never sees `exitCode:5` on stdout while the process exits 0. Text mode
/// prints no envelope, so `run.exit.code()` is unaffected there.
fn json_exit_code(format: &cli::Format, json: Option<&str>, run_exit_code: i32) -> i32 {
    if !matches!(format, cli::Format::Json) {
        return run_exit_code;
    }
    json.and_then(|doc| serde_json::from_str::<serde_json::Value>(doc).ok())
        .and_then(|v| v.get("exitCode")?.as_i64())
        .map_or(run_exit_code, |code| code as i32)
}

fn emit(format: &cli::Format, json: Option<&str>, text: &[String]) {
    match format {
        cli::Format::Json => {
            if let Some(doc) = json {
                println!("{doc}");
            }
        }
        cli::Format::Text => {
            for line in text {
                eprintln!("{line}");
            }
        }
    }
}

/// Whether `--format json`/`--format=json` appears anywhere in raw argv.
/// Parsed textually rather than via clap: this only runs once clap's own
/// parse has already failed (or hit `--help`/`--version`), so `GlobalArgs`
/// was never populated and can't be trusted to answer this.
fn wants_json(raw_args: &[String]) -> bool {
    raw_args.iter().enumerate().any(|(i, a)| {
        a == "--format=json"
            || (a == "--format" && raw_args.get(i + 1).is_some_and(|v| v == "json"))
    })
}

/// Envelope `data` payload for the clap-bypass fallback: just the rendered
/// message clap would otherwise have printed on its own.
#[derive(serde::Serialize)]
struct CliMessage {
    message: String,
}

/// clap's `--help`/`--version`/parse-error paths call `process::exit`
/// themselves via `Cli::parse()`, printing straight to stdout/stderr and
/// never touching `emit` — so under `--format json` the contract (README:
/// "a stable JSON envelope on stdout") was silently broken: stdout stayed
/// empty and the process exited 2, which also collides with this CLI's own
/// `ExitCode::ValidationFailure`. Text mode keeps clap's exact behavior via
/// `err.exit()`; JSON mode gets one parseable envelope instead of nothing.
/// The exit-code collision itself is inherent to clap and not fixable from
/// here without changing the process's exit code out from under text-mode
/// scripts — `cli.parse-error` in `issues[].code` is the intended
/// disambiguator for a JSON consumer.
fn emit_parse_error(err: clap::Error, json_requested: bool) -> i32 {
    if !json_requested {
        err.exit(); // never returns; prints to the stream clap chose itself
    }
    let code = err.exit_code();
    let message = err.render().to_string().trim_end().to_string();
    let issues = if code == 0 {
        Vec::new()
    } else {
        vec![Issue {
            code: "cli.parse-error".to_string(),
            severity: "error".to_string(),
            message: message.clone(),
        }]
    };
    let envelope = Envelope::new(
        "cli",
        Project {
            root: None,
            board_yaml: None,
        },
        CliMessage { message },
        issues,
        code,
    );
    println!("{}", envelope.to_json());
    code
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wants_json_matches_space_and_equals_forms() {
        let space = vec![
            "tan".to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        let equals = vec!["tan".to_string(), "--format=json".to_string()];
        let text = vec!["tan".to_string(), "build".to_string()];
        let dangling = vec!["tan".to_string(), "--format".to_string()];
        assert!(wants_json(&space));
        assert!(wants_json(&equals));
        assert!(!wants_json(&text));
        assert!(!wants_json(&dangling));
    }

    /// Regression for the bypass: an unknown flag under `--format json` used
    /// to leave stdout completely empty (clap's own `err.exit()` prints
    /// usage text to stderr and exits, never touching JSON at all). Now it
    /// must produce one parseable envelope with the CLI's own exit code
    /// mirrored into `exitCode`/`ok`.
    #[test]
    fn json_mode_parse_error_yields_one_parseable_envelope() {
        let err =
            cli::Cli::try_parse_from(["tan", "--format", "json", "--not-a-real-flag"]).unwrap_err();
        assert_eq!(err.exit_code(), 2);

        // Can't call emit_parse_error directly for the error case (it calls
        // process::exit for text mode and would call it here too since we
        // pass json_requested=true it returns instead) — exercise the JSON
        // branch's return value.
        let code = emit_parse_error(err, true);
        assert_eq!(code, 2);
    }

    #[test]
    fn json_mode_help_yields_zero_exit_and_no_issue() {
        let err = cli::Cli::try_parse_from(["tan", "--help"]).unwrap_err();
        assert_eq!(err.exit_code(), 0);
        let code = emit_parse_error(err, true);
        assert_eq!(code, 0);
    }

    /// Regression for the exit-code/envelope divergence: a command can still
    /// believe it succeeded (`CommandRun.exit == Success`) at the moment
    /// `Envelope::to_json` discovers its `data` can't serialize and falls
    /// back to an `ok:false`/`exitCode:5` envelope. The process exit code
    /// must follow the envelope actually printed, not the command's now-stale
    /// `run.exit`.
    #[test]
    fn json_exit_code_follows_serialize_failure_fallback_not_stale_run_exit() {
        use std::collections::BTreeMap;

        let mut data: BTreeMap<Vec<i32>, i32> = BTreeMap::new();
        data.insert(vec![1, 2], 3);
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let env = Envelope::new("test", project, data, Vec::new(), 0);
        let json = env.to_json();

        // Stand-in for `CommandRun.exit`: the command's original, now-stale
        // Success code, set before `to_json` ran into the fallback.
        let stale_run_exit_code = 0;
        let code = json_exit_code(&cli::Format::Json, Some(&json), stale_run_exit_code);
        assert_eq!(code, crate::exit::ExitCode::InternalFailure.code());

        // Core invariant: process exit code == the printed envelope's `exitCode`.
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(i64::from(code), parsed["exitCode"].as_i64().unwrap());

        // Text mode never prints the envelope; `run.exit` passes through untouched.
        assert_eq!(
            json_exit_code(&cli::Format::Text, Some(&json), stale_run_exit_code),
            stale_run_exit_code
        );
    }
}
