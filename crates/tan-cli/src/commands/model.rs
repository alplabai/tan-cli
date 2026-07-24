// SPDX-License-Identifier: Apache-2.0
//! `tan model` — forwards to `alp_cli model <build|list|info|doctor|run>`.
//! `alp_cli model <build|list|info|doctor> --format json` already emits its
//! own JSON payload; tan doesn't re-derive it, it wraps it in the stable
//! `{command,ok,exitCode,project,data,issues}` envelope so a JSON consumer
//! (the vscode extension) gets one contract for every `tan` subcommand.
//!
//! `model` is a real clap subcommand group (`ModelSub`), not a
//! trailing-var-arg passthrough — a passthrough parses every token after
//! `model` as opaque, so a global flag placed there (e.g. `tan model list
//! --format json`, the shape the vscode extension calls) got swallowed into
//! the forwarded argv instead of being parsed as `GlobalArgs::format`.
//!
//! `model run` streams interactively (like `sdk_cli`'s other forwards) and is
//! not wrappable — see `is_wrappable`; in text mode (any sub) tan just
//! streams too, since a captured/re-printed transcript would only delay
//! multi-minute `vela`/`dxcom` compiles.

use std::process::Command;

use super::{CommandRun, sdk_cli};
use crate::cli::{GlobalArgs, ModelSub};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// Append `--flag value` to `argv` iff `value` is `Some`.
fn push_opt(argv: &mut Vec<String>, flag: &str, value: &Option<String>) {
    if let Some(v) = value {
        argv.push(flag.to_string());
        argv.push(v.clone());
    }
}

/// Map a structured [`ModelSub`] to the `alp_cli model` argv tail (everything
/// after `model` itself) — e.g. `Build { board: Some("b.yaml"), .. }` →
/// `["build", "--board", "b.yaml"]` — including only the options that are
/// `Some`. `Run` forwards its passthrough verbatim (Phase-3, streams).
pub fn sub_argv(sub: &ModelSub) -> Vec<String> {
    match sub {
        ModelSub::Build(a) => {
            let mut argv = vec!["build".to_string()];
            push_opt(&mut argv, "--board", &a.board);
            push_opt(&mut argv, "--out", &a.out);
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            push_opt(&mut argv, "--model", &a.model);
            argv
        }
        ModelSub::List(a) => {
            let mut argv = vec!["list".to_string()];
            push_opt(&mut argv, "--board", &a.board);
            push_opt(&mut argv, "--out", &a.out);
            argv
        }
        ModelSub::Info(a) => {
            let mut argv = vec!["info".to_string(), a.name.clone()];
            push_opt(&mut argv, "--out", &a.out);
            push_opt(&mut argv, "--board", &a.board);
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            argv
        }
        ModelSub::Check(a) => {
            let mut argv = vec!["check".to_string()];
            if let Some(m) = &a.model {
                argv.push(m.clone());
            }
            push_opt(&mut argv, "--sku", &a.sku);
            push_opt(&mut argv, "--board", &a.board);
            push_opt(&mut argv, "--model", &a.select);
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            argv
        }
        ModelSub::Zoo(a) => {
            let mut argv = vec!["zoo".to_string()];
            push_opt(&mut argv, "--sku", &a.sku);
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            argv
        }
        ModelSub::Add(a) => {
            let mut argv = vec!["add".to_string(), a.zoo_id.clone()];
            push_opt(&mut argv, "--board", &a.board);
            push_opt(&mut argv, "--name", &a.name);
            push_opt(&mut argv, "--models-dir", &a.models_dir);
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            argv
        }
        ModelSub::Prep(a) => {
            let mut argv = vec!["prep".to_string(), a.raw.clone()];
            argv.push("--calibration".to_string());
            argv.push(a.calibration.clone());
            push_opt(&mut argv, "--out", &a.out);
            if a.per_channel {
                argv.push("--per-channel".to_string());
            }
            if let Some(n) = a.min_samples {
                argv.push("--min-samples".to_string());
                argv.push(n.to_string());
            }
            argv
        }
        ModelSub::Doctor => vec!["doctor".to_string()],
        ModelSub::Run(fwd) => {
            let mut argv = vec!["run".to_string()];
            argv.extend(fwd.args.iter().cloned());
            argv
        }
    }
}

/// Whether `sub` is one of the four subs whose `--format json` payload tan
/// can wrap in its own envelope. `Run` is interactive/streaming (like the
/// other `sdk_cli` forwards) and is deliberately excluded — Phase-3.
pub fn is_wrappable(sub: &ModelSub) -> bool {
    !matches!(sub, ModelSub::Run(_))
}

/// Build the full `alp_cli` argv: `-m alp_cli model <tail…>`, appending
/// `--format json` when `json` is set. The structured sub's own args never
/// contain `--format` (it's the global, not a per-sub option), so unlike the
/// old passthrough there's no dedup to do.
pub fn model_argv(sub: &ModelSub, json: bool) -> Vec<String> {
    let mut argv = vec!["-m".to_string(), "alp_cli".to_string(), "model".to_string()];
    argv.extend(sub_argv(sub));
    if json {
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

/// Wrap a captured `alp_cli model` run in tan's envelope, returning the JSON
/// document alongside the EFFECTIVE exit code (which the caller must also use
/// for `CommandRun.exit` — they must agree, see `envelope.rs`'s `ok` == `exit
/// == 0` invariant). `stdout` is parsed as JSON for `data`; a non-`Success`
/// `exit` adds one `model.failed` issue carrying the trimmed `stderr` (or a
/// generic line when stderr is empty) — checked first so it sorts before a
/// `bad-payload` issue when both fire (e.g. a failed run with empty stdout).
///
/// An unparseable `stdout` adds a `model.bad-payload` issue and `data` becomes
/// `null`; if the child otherwise reported success, the effective exit is
/// downgraded to `InternalFailure` so `ok` stays reliable for a JSON consumer
/// — a child that exits 0 but prints garbage is tan's problem to flag, not a
/// silent `ok:true`.
pub fn wrap_model_json(
    stdout: &str,
    stderr: &str,
    exit: ExitCode,
    project: Project,
) -> (String, ExitCode) {
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

    let mut bad_payload = false;
    let data = match serde_json::from_str::<serde_json::Value>(stdout) {
        Ok(v) => v,
        Err(e) => {
            bad_payload = true;
            issues.push(Issue {
                code: "model.bad-payload".to_string(),
                severity: "error".to_string(),
                message: format!("could not parse alp_cli model output as JSON: {e}"),
            });
            serde_json::Value::Null
        }
    };

    let effective_exit = if bad_payload && exit == ExitCode::Success {
        ExitCode::InternalFailure
    } else {
        exit
    };

    let doc = Envelope::new("model", project, data, issues, effective_exit.code()).to_json();
    (doc, effective_exit)
}

/// Best-effort `Project` for the envelope: reuses `resolve_cli_project_context`
/// (the same resolution `validate`/`diff`/`doctor` use). `build`/`list`/`info`
/// are board-scoped so this is the useful case; `doctor` isn't, but a
/// best-effort (possibly non-existent) `board_yaml` path is harmless there —
/// `Project` only carries a path, it never asserts the file exists.
fn project_from(g: &GlobalArgs) -> Project {
    let context = resolve_cli_project_context(g);
    Project {
        root: context.workspace_root,
        board_yaml: context.board_yaml_path,
    }
}

/// `tan model <sub>` entry point. Text mode and the non-wrappable `Run` sub
/// stream live through `sdk_cli::run` — the existing forwarder, unchanged. A
/// wrappable sub (`is_wrappable`) under `--format json` instead resolves the
/// SDK/python once via `sdk_cli::prepare_alp_cli` (shared, not re-derived) and
/// spawns the child CAPTURED so its `--format json` payload can be wrapped in
/// tan's envelope.
pub fn run(g: &GlobalArgs, sub: &ModelSub) -> CommandRun {
    if !g.is_json() || !is_wrappable(sub) {
        return sdk_cli::run(g, "model", &sub_argv(sub));
    }

    let spawn = match sdk_cli::prepare_alp_cli(g, "model") {
        Ok(spawn) => spawn,
        Err(guard_failure) => return guard_failure,
    };

    let project = project_from(g);
    let argv = model_argv(sub, true);
    let output = Command::new(&spawn.python)
        .args(&argv)
        .current_dir(&spawn.workspace_root)
        .env("ALP_SDK_ROOT", &spawn.sdk_root)
        .env("PYTHONPATH", &spawn.pythonpath)
        .output();

    match output {
        Ok(out) => {
            let exit = map_model_exit(out.status.code());
            let stdout = String::from_utf8_lossy(&out.stdout);
            let stderr = String::from_utf8_lossy(&out.stderr);
            let (json, effective_exit) = wrap_model_json(&stdout, &stderr, exit, project);
            CommandRun {
                exit: effective_exit,
                text: Vec::new(),
                json: Some(json),
            }
        }
        Err(e) => {
            let message = sdk_cli::launch_error(&spawn.python, &e);
            let issues = vec![Issue {
                code: "model.failed".to_string(),
                severity: "error".to_string(),
                message,
            }];
            let json = Envelope::new(
                "model",
                project,
                serde_json::Value::Null,
                issues,
                ExitCode::RuntimeFailure.code(),
            )
            .to_json();
            CommandRun {
                exit: ExitCode::RuntimeFailure,
                text: Vec::new(),
                json: Some(json),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::{
        ModelAddArgs, ModelBuildArgs, ModelCheckArgs, ModelInfoArgs, ModelListArgs, ModelPrepArgs,
        ModelZooArgs, WestForwardArgs,
    };

    #[test]
    fn sub_argv_maps_build_including_only_set_options() {
        let sub = ModelSub::Build(ModelBuildArgs {
            board: Some("b.yaml".to_string()),
            out: Some("o".to_string()),
            metadata_root: None,
            model: None,
        });
        assert_eq!(
            sub_argv(&sub),
            vec!["build", "--board", "b.yaml", "--out", "o"]
        );
    }

    #[test]
    fn sub_argv_forwards_the_model_selector() {
        // B1 regression: the vscode panel builds a single model via `tan model
        // build --model <name>` — the alp_cli argv must carry `--model NAME`.
        let sub = ModelSub::Build(ModelBuildArgs {
            board: None,
            out: None,
            metadata_root: None,
            model: Some("demo".to_string()),
        });
        let argv = sub_argv(&sub);
        assert_eq!(argv, vec!["build", "--model", "demo"]);
    }

    #[test]
    fn sub_argv_maps_list_info_doctor_run() {
        assert_eq!(
            sub_argv(&ModelSub::List(ModelListArgs {
                board: None,
                out: Some("build/models".to_string()),
            })),
            vec!["list", "--out", "build/models"]
        );
        assert_eq!(
            sub_argv(&ModelSub::Info(ModelInfoArgs {
                name: "demo".to_string(),
                out: None,
                board: Some("b.yaml".to_string()),
                metadata_root: None,
            })),
            vec!["info", "demo", "--board", "b.yaml"]
        );
        assert_eq!(sub_argv(&ModelSub::Doctor), vec!["doctor"]);
        assert_eq!(
            sub_argv(&ModelSub::Run(WestForwardArgs {
                args: vec!["m".to_string()],
            })),
            vec!["run", "m"]
        );
    }

    #[test]
    fn sub_argv_maps_check_with_required_sku() {
        let sub = ModelSub::Check(ModelCheckArgs {
            model: Some("m.tflite".to_string()),
            sku: Some("E1M-AEN801".to_string()),
            board: None,
            select: None,
            metadata_root: None,
        });
        assert_eq!(
            sub_argv(&sub),
            vec!["check", "m.tflite", "--sku", "E1M-AEN801"]
        );
    }

    #[test]
    fn model_argv_appends_format_json_for_check() {
        let sub = ModelSub::Check(ModelCheckArgs {
            model: Some("m.tflite".to_string()),
            sku: Some("E1M-AEN801".to_string()),
            board: None,
            select: None,
            metadata_root: Some("meta".to_string()),
        });
        let argv = model_argv(&sub, true);
        // -m alp_cli model check m.tflite --sku E1M-AEN801 --metadata-root meta --format json
        assert!(argv.ends_with(&["--format".to_string(), "json".to_string()]));
        assert!(argv.contains(&"check".to_string()));
        assert!(argv.contains(&"--metadata-root".to_string()));
        assert!(is_wrappable(&sub), "check must be wrappable");
    }

    #[test]
    fn sub_argv_maps_board_mode_with_selector() {
        let sub = ModelSub::Check(ModelCheckArgs {
            model: None,
            sku: None,
            board: Some("board.yaml".to_string()),
            select: Some("tiny".to_string()),
            metadata_root: None,
        });
        assert_eq!(
            sub_argv(&sub),
            vec!["check", "--board", "board.yaml", "--model", "tiny"]
        );
    }

    #[test]
    fn sub_argv_maps_zoo_with_sku() {
        let sub = ModelSub::Zoo(ModelZooArgs {
            sku: Some("E1M-AEN801".to_string()),
            metadata_root: None,
        });
        assert_eq!(sub_argv(&sub), vec!["zoo", "--sku", "E1M-AEN801"]);
    }

    #[test]
    fn sub_argv_maps_add_with_id_and_opts() {
        let sub = ModelSub::Add(ModelAddArgs {
            zoo_id: "example-tiny".to_string(),
            board: Some("board.yaml".to_string()),
            name: None,
            models_dir: None,
            metadata_root: None,
        });
        assert_eq!(
            sub_argv(&sub),
            vec!["add", "example-tiny", "--board", "board.yaml"]
        );
        assert!(is_wrappable(&sub), "add must be wrappable");
    }

    #[test]
    fn sub_argv_maps_prep_required_calibration() {
        let sub = ModelSub::Prep(ModelPrepArgs {
            raw: "m.onnx".to_string(),
            calibration: "cal".to_string(),
            out: None,
            per_channel: false,
            min_samples: None,
        });
        assert_eq!(
            sub_argv(&sub),
            vec!["prep", "m.onnx", "--calibration", "cal"]
        );
    }

    #[test]
    fn sub_argv_maps_prep_flags_and_min_samples() {
        let sub = ModelSub::Prep(ModelPrepArgs {
            raw: "m.onnx".to_string(),
            calibration: "cal".to_string(),
            out: Some("o.onnx".to_string()),
            per_channel: true,
            min_samples: Some(16),
        });
        let argv = sub_argv(&sub);
        assert!(argv.contains(&"--per-channel".to_string()));
        assert_eq!(
            argv.windows(2)
                .find(|w| w[0] == "--min-samples")
                .map(|w| &w[1]),
            Some(&"16".to_string())
        );
        assert!(is_wrappable(&sub), "prep must be wrappable");
    }

    #[test]
    fn is_wrappable_excludes_only_run() {
        assert!(is_wrappable(&ModelSub::Doctor));
        assert!(is_wrappable(&ModelSub::List(ModelListArgs {
            board: None,
            out: None,
        })));
        assert!(!is_wrappable(&ModelSub::Run(WestForwardArgs {
            args: vec![],
        })));
    }

    #[test]
    fn model_argv_appends_format_json_for_wrappable_subs() {
        let sub = ModelSub::Build(ModelBuildArgs {
            board: Some("b.yaml".to_string()),
            out: None,
            metadata_root: None,
            model: None,
        });
        assert_eq!(
            model_argv(&sub, true),
            vec![
                "-m", "alp_cli", "model", "build", "--board", "b.yaml", "--format", "json"
            ]
        );
        // text mode: no --format appended
        assert_eq!(
            model_argv(&sub, false),
            vec!["-m", "alp_cli", "model", "build", "--board", "b.yaml"]
        );
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
        let (doc, effective_exit) = wrap_model_json(
            payload,
            "",
            ExitCode::Success,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        assert_eq!(effective_exit, ExitCode::Success);
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["command"], "model");
        assert_eq!(v["ok"], true);
        assert_eq!(v["exitCode"], 0);
        assert_eq!(v["data"]["models"][0]["name"], "demo");
        assert_eq!(v["issues"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn wrap_model_json_reports_failure_and_bad_payload() {
        // non-success exit + unparseable (empty) stdout → BOTH issues fire,
        // model.failed first so a JSON consumer sees the primary cause before
        // the secondary "couldn't even parse the output" complaint.
        let (doc, effective_exit) = wrap_model_json(
            "",
            "no blob compiled",
            ExitCode::RuntimeFailure,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        assert_eq!(effective_exit, ExitCode::RuntimeFailure);
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], 1);
        assert_eq!(v["issues"].as_array().unwrap().len(), 2);
        assert_eq!(v["issues"][0]["code"], "model.failed");
        assert!(
            v["issues"][0]["message"]
                .as_str()
                .unwrap()
                .contains("no blob compiled")
        );
        assert_eq!(v["issues"][1]["code"], "model.bad-payload");

        // unparseable stdout on a child-reported "success" exit → downgraded to
        // InternalFailure (exit 5) so `ok` stays reliable for a JSON consumer;
        // a 0-exit child that printed garbage is not a success tan can vouch for.
        let (doc2, effective_exit2) = wrap_model_json(
            "not json",
            "",
            ExitCode::Success,
            Project {
                root: None,
                board_yaml: None,
            },
        );
        assert_eq!(effective_exit2, ExitCode::InternalFailure);
        let v2: serde_json::Value = serde_json::from_str(&doc2).unwrap();
        assert_eq!(v2["ok"], false);
        assert_eq!(v2["exitCode"], 5);
        assert_eq!(v2["issues"][0]["code"], "model.bad-payload");
        assert!(v2["data"].is_null());
    }

    fn json_global() -> GlobalArgs {
        GlobalArgs {
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
        }
    }

    fn text_global() -> GlobalArgs {
        GlobalArgs {
            format: crate::cli::Format::Text,
            ..json_global()
        }
    }

    /// A `--sdk-root` guaranteed not to resolve, regardless of this machine's
    /// ambient checkouts — routes `sdk_cli::prepare_alp_cli`'s guard to fail
    /// deterministically (no live python/alp_cli spawn) whichever code path
    /// reaches it, so these tests don't depend on (or accidentally spawn
    /// against) a real alp-sdk sibling checkout.
    const NO_SDK_ROOT: &str = "/definitely/does/not/exist/alp-sdk";

    #[test]
    fn text_mode_delegates_to_streaming_forwarder() {
        // text mode must NOT capture/wrap: json is None (streamed), regardless
        // of sub — mirrors sdk_cli::run's own streaming CommandRun shape. Even
        // `doctor` (wrappable) must not produce json here; the guard failure
        // routed through unconditionally by the unresolved sdk_root proves it
        // (`fail()` only emits json when `g.is_json()`).
        let g = GlobalArgs {
            sdk_root: Some(NO_SDK_ROOT.to_string()),
            ..text_global()
        };
        let run = run(&g, &ModelSub::Doctor);
        assert!(run.json.is_none());
    }

    #[test]
    fn json_non_wrappable_sub_delegates_and_does_not_wrap_as_model_payload() {
        // `model run` is Phase-3 / interactive → still streams (delegates to
        // sdk_cli::run) even under --format json; `is_wrappable` returning
        // false routes straight there instead of attempting a captured spawn.
        let run_sub = ModelSub::Run(WestForwardArgs {
            args: vec!["m".to_string()],
        });
        assert!(!is_wrappable(&run_sub));
        let g = GlobalArgs {
            sdk_root: Some(NO_SDK_ROOT.to_string()),
            ..json_global()
        };
        let run = run(&g, &run_sub);
        let doc = run
            .json
            .expect("guard failure emits a json envelope in json mode");
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        // sdk_cli::fail()'s ForwardData shape (`data.subcommand`), never a
        // model-payload wrap (`data.models`) — proves this sub's failure came
        // from the forwarder, not from model::run's own captured-spawn/wrap path.
        assert_eq!(v["data"]["subcommand"], "model");
    }

    #[test]
    fn json_wrappable_sub_with_unresolved_sdk_root_yields_a_guard_failure_envelope() {
        // Exercises prepare_alp_cli's failure branch (SDK root guard) without a
        // real python spawn: --sdk-root pointing at a nonexistent path under
        // --format json for a wrappable sub must yield an ok:false envelope,
        // not attempt to spawn anything.
        let g = GlobalArgs {
            sdk_root: Some(NO_SDK_ROOT.to_string()),
            ..json_global()
        };
        let run = run(&g, &ModelSub::Doctor);
        assert_eq!(run.exit, ExitCode::ValidationFailure);
        let doc = run.json.expect("guard failure must emit a json envelope");
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["exitCode"], 2);
        assert_eq!(v["issues"][0]["code"], "model.failed");
    }

    /// Regression for the bug this commit fixes: a global placed AFTER `model`
    /// (the vscode extension's exact call shape, `model doctor --format
    /// json`) must parse into `GlobalArgs::format`, not get swallowed into a
    /// passthrough — proven by asserting clap itself resolves it, independent
    /// of any hand-rolled forwarding logic.
    #[test]
    fn global_after_model_subcommand_parses_as_a_global_not_passthrough() {
        use clap::Parser;
        let cli = crate::cli::Cli::try_parse_from(["tan", "model", "doctor", "--format", "json"])
            .expect("global after `model doctor` must parse");
        assert!(cli.global.is_json());
        assert!(matches!(
            cli.command,
            crate::cli::Command::Model(crate::cli::ModelArgs {
                sub: ModelSub::Doctor
            })
        ));
    }
}
