// SPDX-License-Identifier: Apache-2.0
//! `tan kconfig` — board-scoped Kconfig symbol menu for one core (the vscode
//! `prj.conf` LSP's live feed). Wraps the SDK's `alp_orchestrate --emit
//! kconfig --core <id>` (alp-sdk#894) in the standard `Envelope<KconfigData>`
//! (tan-cli#35).
//!
//! Workspace-dependent — the SDK's one deliberate exception to "every emit is
//! hermetic" (`docs/cli.md`): the real Kconfig solver needs a bootstrapped
//! `ZEPHYR_BASE` (v4.4.0), so this command resolves it via the SAME
//! workspace/venv resolver `tan build` already uses
//! (`build::resolve_zephyr_base`) and injects it into the emit's child env
//! BEFORE spawning. A missing workspace fails loud with an actionable `tan
//! bootstrap` hint instead of letting the child hit its own cryptic Python
//! `exit(2)`.

use std::path::Path;

use tan_core::{KconfigData, ProjectContext, parse_kconfig, resolve_default_kconfig_core};

use super::CommandRun;
use super::build::{base_dir, invoke_sdk_emit, resolve_zephyr_base};
use crate::cli::{GlobalArgs, KconfigArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

/// Run `tan kconfig`: resolve the `--core` to target, resolve `ZEPHYR_BASE`,
/// invoke the SDK's `--emit kconfig`, and wrap its JSON in the envelope.
pub fn run(g: &GlobalArgs, args: &KconfigArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    // Setup-class check #1: no SDK checkout resolved. `invoke_sdk_emit` would
    // also catch this (further down, mid-spawn), but only with exit 1 — for
    // the LSP consumer a missing SDK and a missing Zephyr workspace (below)
    // are both "not bootstrapped"; pre-check here so every setup-class
    // failure from `tan kconfig` is uniformly exit 2, never a spawn.
    let Some(sdk_root) = context.sdk_root.as_deref().map(Path::new) else {
        return failure(
            g,
            project,
            ExitCode::ValidationFailure,
            "kconfig.no-sdk-root",
            "no alp-sdk checkout found — pass `--sdk-root <PATH>`, pin one with `tan sdk \
             switch <version|path>`, or run `tan bootstrap` first.",
            None,
        );
    };

    let core = match resolve_core(args, &context) {
        Ok(core) => core,
        Err((code, message)) => {
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                code,
                &message,
                None,
            );
        }
    };

    // Setup-class check #2: no bootstrapped Zephyr workspace. `base` is the
    // SAME exec base `invoke_sdk_emit` effectively runs under (it inherits
    // the process cwd — see `native::base_dir`'s doc), so `ZEPHYR_BASE` is
    // derived from the one place, not a base that could diverge from it.
    let base = base_dir(&context);
    let Some(zephyr_base) = resolve_zephyr_base(&base, Some(sdk_root)) else {
        return failure(
            g,
            project,
            ExitCode::ValidationFailure,
            "kconfig.no-workspace",
            "no bootstrapped Zephyr workspace found for `--emit kconfig` (needs \
             ZEPHYR_BASE) — run `tan bootstrap` first.",
            Some(core),
        );
    };

    let extra_env = [("ZEPHYR_BASE", zephyr_base.to_string_lossy().into_owned())];
    let stdout = match invoke_sdk_emit(
        &context,
        "kconfig",
        "kconfig.emit-failed",
        &["--core", &core],
        &extra_env,
    ) {
        Ok(s) => s,
        Err((code, message)) => {
            return failure(
                g,
                project,
                ExitCode::RuntimeFailure,
                code,
                &message,
                Some(core),
            );
        }
    };

    let data = match parse_kconfig(&stdout) {
        Ok(d) => d,
        Err(e) => {
            return failure(
                g,
                project,
                ExitCode::RuntimeFailure,
                "kconfig.parse-failed",
                &e.to_string(),
                Some(core),
            );
        }
    };

    let text = if g.is_json() {
        Vec::new()
    } else {
        kconfig_text_lines(g, &data)
    };
    let json = g.is_json().then(|| {
        Envelope::new(
            "kconfig",
            project,
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

/// Resolve the `--core <id>` to scope this emit to: the explicit flag, or —
/// when omitted — the board's one declared Zephyr core (see
/// `resolve_default_kconfig_core`; ambiguous or missing declarations are a
/// clear error naming the board's cores, never a guess).
fn resolve_core(
    args: &KconfigArgs,
    context: &ProjectContext,
) -> Result<String, (&'static str, String)> {
    if let Some(core) = &args.core {
        return Ok(core.clone());
    }
    let Some(board_yaml) = context.board_yaml_path.as_deref() else {
        return Err((
            "kconfig.board-yaml-missing",
            "no board.yaml found — pass --board-yaml <PATH> or run from a project.".to_string(),
        ));
    };
    let source = std::fs::read_to_string(board_yaml).map_err(|e| {
        (
            "kconfig.board-yaml-missing",
            format!("failed to read board.yaml at `{board_yaml}`: {e}"),
        )
    })?;
    let model = tan_core::parse_board_model(&source).map_err(|e| {
        (
            "kconfig.board-yaml-invalid",
            format!("failed to parse board.yaml at `{board_yaml}`: {e}"),
        )
    })?;
    resolve_default_kconfig_core(model.cores.as_ref()).map_err(|candidates| {
        let hint = if candidates.is_empty() {
            "board.yaml declares no cores".to_string()
        } else {
            format!("declared cores: {}", candidates.join(", "))
        };
        (
            "kconfig.core-ambiguous",
            format!(
                "--core <id> is required (board.yaml doesn't declare exactly one Zephyr \
                 core); {hint}"
            ),
        )
    })
}

/// Human-readable summary line(s) for text mode.
fn kconfig_text_lines(g: &GlobalArgs, data: &KconfigData) -> Vec<String> {
    let mut lines = vec![format!(
        "kconfig: {} symbol(s) for core '{}' ({})",
        data.symbols.len(),
        data.core,
        data.board
    )];
    if g.verbose {
        for symbol in &data.symbols {
            lines.push(format!(
                "  {} [{}] — {}",
                symbol.name, symbol.r#type, symbol.prompt
            ));
        }
    }
    lines
}

/// A `KconfigData` with no symbols, used for early-failure envelopes; `core`
/// is whatever was already resolved (empty string if resolution itself failed).
fn empty_data(core: Option<String>) -> KconfigData {
    KconfigData {
        schema_version: tan_core::KCONFIG_SCHEMA_VERSION,
        board: String::new(),
        core: core.unwrap_or_default(),
        symbols: Vec::new(),
    }
}

/// Build a failing `CommandRun` carrying a single `kconfig.{code}` error issue.
fn failure(
    g: &GlobalArgs,
    project: Project,
    exit: ExitCode,
    code: &str,
    message: &str,
    core: Option<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: code.to_string(),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let text = if g.is_json() {
        Vec::new()
    } else {
        vec![format!("kconfig: {message}")]
    };
    let json = g.is_json().then(|| {
        Envelope::new("kconfig", project, empty_data(core), issues, exit.code()).to_json()
    });

    CommandRun { exit, text, json }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_EMIT: &str = r#"{
        "schemaVersion": 1,
        "board": "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he",
        "core": "m55_he",
        "symbols": [
            { "name": "LOG", "type": "bool", "prompt": "Logging",
              "depends": "y", "default": "n", "help": "Enable logging." }
        ]
    }"#;

    fn no_op_project() -> Project {
        Project {
            root: None,
            board_yaml: None,
        }
    }

    /// Hermetic: no Zephyr/SDK/network spawn — exercises the JSON -> KconfigData
    /// -> Envelope path directly, the same shape `invoke_sdk_emit`'s stdout
    /// would feed `run` in the success case.
    #[test]
    fn emit_json_round_trips_into_the_kconfig_envelope() {
        let data = parse_kconfig(SAMPLE_EMIT).expect("sample emit must parse");
        let json = Envelope::new(
            "kconfig",
            no_op_project(),
            data,
            Vec::new(),
            ExitCode::Success.code(),
        )
        .to_json();

        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["command"], "kconfig");
        assert_eq!(parsed["ok"], true);
        assert_eq!(parsed["exitCode"], 0);
        assert_eq!(parsed["data"]["schemaVersion"], 1);
        assert!(parsed["data"]["schemaVersion"].is_number());
        assert_eq!(
            parsed["data"]["board"],
            "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he"
        );
        assert_eq!(parsed["data"]["core"], "m55_he");
        assert_eq!(parsed["data"]["symbols"][0]["name"], "LOG");
        assert_eq!(parsed["data"]["symbols"][0]["type"], "bool");
        assert_eq!(parsed["data"]["symbols"][0]["default"], "n");
    }

    #[test]
    fn resolve_core_uses_the_explicit_flag_without_touching_board_yaml() {
        let args = KconfigArgs {
            core: Some("m55_he".to_string()),
        };
        let context = ProjectContext {
            workspace_root: None,
            sdk_root: None,
            board_yaml_path: None, // would fail resolution if this path were taken
            west_cwd: None,
            python_binary: "python3".to_string(),
        };
        assert_eq!(resolve_core(&args, &context), Ok("m55_he".to_string()));
    }

    #[test]
    fn resolve_core_reports_missing_board_yaml_without_a_flag() {
        let args = KconfigArgs { core: None };
        let context = ProjectContext {
            workspace_root: None,
            sdk_root: None,
            board_yaml_path: None,
            west_cwd: None,
            python_binary: "python3".to_string(),
        };
        let (code, _) = resolve_core(&args, &context).unwrap_err();
        assert_eq!(code, "kconfig.board-yaml-missing");
    }

    #[test]
    fn resolve_core_picks_the_one_declared_zephyr_core() {
        let dir =
            std::env::temp_dir().join(format!("tan-kconfig-resolve-core-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let board_yaml = dir.join("board.yaml");
        std::fs::write(
            &board_yaml,
            "schemaVersion: 2\ncores:\n  m55_he:\n    os: zephyr\n  a55_cluster:\n    os: yocto\n",
        )
        .unwrap();

        let args = KconfigArgs { core: None };
        let context = ProjectContext {
            workspace_root: Some(dir.to_string_lossy().into_owned()),
            sdk_root: None,
            board_yaml_path: Some(board_yaml.to_string_lossy().into_owned()),
            west_cwd: None,
            python_binary: "python3".to_string(),
        };
        assert_eq!(resolve_core(&args, &context), Ok("m55_he".to_string()));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn resolve_core_is_ambiguous_with_two_zephyr_cores_and_names_them() {
        let dir = std::env::temp_dir().join(format!(
            "tan-kconfig-resolve-core-ambiguous-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let board_yaml = dir.join("board.yaml");
        std::fs::write(
            &board_yaml,
            "schemaVersion: 2\ncores:\n  m55_he:\n    os: zephyr\n  m55_hp:\n    os: zephyr\n",
        )
        .unwrap();

        let args = KconfigArgs { core: None };
        let context = ProjectContext {
            workspace_root: Some(dir.to_string_lossy().into_owned()),
            sdk_root: None,
            board_yaml_path: Some(board_yaml.to_string_lossy().into_owned()),
            west_cwd: None,
            python_binary: "python3".to_string(),
        };
        let (code, message) = resolve_core(&args, &context).unwrap_err();
        assert_eq!(code, "kconfig.core-ambiguous");
        assert!(message.contains("m55_he"));
        assert!(message.contains("m55_hp"));

        std::fs::remove_dir_all(&dir).ok();
    }
}
