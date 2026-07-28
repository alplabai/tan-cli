// SPDX-License-Identifier: Apache-2.0
//! `tan generate` — generate board-derived output files.

use std::path::{Path, PathBuf};
use std::process::Command;

use super::CommandRun;
use crate::cli::{GenerateArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

/// Every supported `--emit` mode, used as the default target set when neither
/// `--target` nor `--all` narrows the selection.
///
/// `carrier-netlist` is a board-level export — the deterministic carrier
/// netlist + BOM handoff Alp Studio consumes (alp-sdk#419) — not a per-core
/// build config. `native-sim-overlay` is a Zephyr board overlay (the canonical
/// `alp,pin-array` on `zephyr,gpio-emul`) that makes a GPIO app resolve under
/// `native_sim`. `west-libraries` (tan-cli#114) is the `west.yml` library
/// auto-pin fragment, a single-file board-derived emit exactly like the
/// other four `build/generated/` targets. All three are intentionally
/// `generate` targets only: none is in `tan_core::ALL_EMIT_MODES` (the set
/// `trace` / `support-bundle` enumerate), because those model the *build*
/// generation a slice runs.
///
/// `zephyr-board` (tan-cli#116) is deliberately NOT here: unlike every mode
/// above, it hard-requires `--core <id>` and writes a DIRECTORY of files, not
/// one fixed conventional file, so it cannot be defaulted by a bare
/// `tan generate` / `--all` the way these seven can. It is reachable only via
/// explicit `--target zephyr-board --core <id>` — see `resolve_generate_targets`.
const ALL_EMIT_MODES: [&str; 7] = [
    "zephyr-conf",
    "dts-overlay",
    "native-sim-overlay",
    "cmake-args",
    "yocto-conf",
    "carrier-netlist",
    "west-libraries",
];

/// JSON `data` payload for the `generate` envelope.
#[derive(serde::Serialize)]
struct GenerateData {
    /// Schema version of this payload (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Emit modes that were requested for this run.
    targets: Vec<String>,
    /// Workspace-relative paths of successfully written outputs.
    written: Vec<String>,
    /// Emit modes whose generation failed.
    failed: Vec<String>,
}

/// Run `tan generate`: resolve the board and SDK roots, invoke `alp_project.py`
/// once per emit target, and assemble the text/JSON `CommandRun` result.
pub fn run(g: &GlobalArgs, args: &GenerateArgs) -> CommandRun {
    let workspace_root = crate::util::cli_workspace_root(g);
    let board_path = resolve_board_path(g, &workspace_root);

    // Keep as-given strings for JSON (reproducible in golden fixtures).
    let project_str = g.project.clone().unwrap_or_else(|| ".".to_string());
    let board_yaml_str = g
        .board_yaml
        .clone()
        .unwrap_or_else(|| "board.yaml".to_string());
    let project = Project {
        root: Some(project_str),
        board_yaml: Some(board_yaml_str),
    };

    if !board_path.exists() {
        return failure(
            g,
            project,
            ExitCode::ValidationFailure,
            "board-yaml-missing",
            "board.yaml path could not be resolved or the file does not exist.",
            empty_data(),
            vec!["generate: board.yaml path is unresolved or missing.".to_string()],
        );
    }

    let Some(sdk_root) = crate::util::resolve_sdk_root(g, &workspace_root) else {
        return failure(
            g,
            project,
            ExitCode::ValidationFailure,
            "sdk-root-unresolved",
            "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk switch \
             <version|path>`, or place the project near an alp-sdk checkout.",
            empty_data(),
            vec!["generate: alp-sdk root is unresolved.".to_string()],
        );
    };

    let targets = match resolve_generate_targets(g.target.as_deref(), g.all, args.core.as_deref()) {
        Ok(t) => t,
        Err(message) => {
            let copy = message.clone();
            return failure(
                g,
                project,
                ExitCode::InternalFailure,
                "internal-failure",
                &message,
                empty_data(),
                vec!["generate: internal failure".to_string(), copy],
            );
        }
    };

    // native-sim-overlay is the one target that writes into the hand-editable
    // app source tree (boards/, not build/generated/ -- see output_path_for_emit).
    // Every other writer into a user tree (init, scaffold) diffs planned files
    // against disk and refuses with a would-overwrite issue unless --force; a
    // bare `tan generate` used to truncate a developer's hand-tuned overlay
    // with no check at all. Guard just this target the same way.
    if overlay_would_overwrite(&workspace_root, &targets, args.force) {
        return failure(
            g,
            project,
            ExitCode::WriteFailure,
            "would-overwrite",
            "boards/native_sim_native_64.overlay already exists. Use --force to overwrite.",
            empty_data(),
            vec![
                "generate: boards/native_sim_native_64.overlay already exists; use --force to overwrite."
                    .to_string(),
            ],
        );
    }

    let python = default_python_binary();
    // Guard: the interpreter must be new enough to run `alp_project.py` (the
    // SDK scripts use `@dataclass(slots=True)`, Python 3.10+). Fail with an
    // actionable message instead of the cryptic `dataclass()` TypeError.
    if let Some(message) = crate::util::python_too_old(python) {
        let line = format!("generate: {message}");
        return failure(
            g,
            project,
            ExitCode::RuntimeFailure,
            "python-too-old",
            &message,
            empty_data(),
            vec!["generate: python interpreter too old".to_string(), line],
        );
    }
    let script_path = sdk_root.join("scripts").join("alp_project.py");
    let mut written = Vec::<String>::new();
    let mut failed = Vec::<String>::new();
    let mut issues = Vec::<Issue>::new();

    for emit in &targets {
        let output_path = output_path_for_emit(&workspace_root, emit, args.core.as_deref());
        let output_str = output_path.to_string_lossy().to_string();
        let mut command = Command::new(python);
        command
            .arg(&script_path)
            .arg("--input")
            .arg(&board_path)
            .arg("--emit")
            .arg(emit)
            .arg("--output")
            .arg(&output_str);
        // zephyr-board is the one target `alp_project.py` requires --core for
        // (resolve_generate_targets already refused this target with no
        // --core, so this is always Some here).
        if *emit == "zephyr-board" {
            if let Some(core) = &args.core {
                command.arg("--core").arg(core);
            }
        }
        let status = command.output();

        match status {
            Ok(out) if out.status.success() => {
                written.push(relative_or_full(&workspace_root, &output_path));
            }
            Ok(out) => {
                failed.push((*emit).to_string());
                let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
                issues.push(Issue {
                    code: "generate.emit-failed".to_string(),
                    severity: "error".to_string(),
                    message: if stderr.is_empty() {
                        format!("Generation failed for target '{emit}'.")
                    } else {
                        stderr
                    },
                });
            }
            Err(err) => {
                failed.push((*emit).to_string());
                issues.push(Issue {
                    code: "generate.emit-failed".to_string(),
                    severity: "error".to_string(),
                    message: format!("Generation failed for target '{emit}': {err}"),
                });
            }
        }
    }

    let exit = if failed.is_empty() {
        ExitCode::Success
    } else {
        ExitCode::WriteFailure
    };
    let data = GenerateData {
        schema_version: "1".to_string(),
        targets: targets.iter().map(|s| (*s).to_string()).collect(),
        written,
        failed,
    };

    let text = if g.is_json() {
        Vec::new()
    } else {
        generate_text_lines(g, &data)
    };
    let json = g
        .is_json()
        .then(|| Envelope::new("generate", project, data, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

/// Resolve the `board.yaml` path from `--board-yaml` (absolute or workspace-relative),
/// defaulting to `<workspace_root>/board.yaml`.
fn resolve_board_path(g: &GlobalArgs, workspace_root: &Path) -> PathBuf {
    if let Some(board) = &g.board_yaml {
        let board_path = PathBuf::from(board);
        if board_path.is_absolute() {
            return board_path;
        }
        return workspace_root.join(board_path);
    }

    workspace_root.join("board.yaml")
}

/// Resolve which emit modes to run: all modes when `all` is set or no `--target`
/// is given, otherwise the single matching mode, or an error for an unknown target.
///
/// `--core` (tan-cli#116) is meaningful for exactly one target, `zephyr-board`
/// -- it picks which core's Zephyr board tree `alp_project.py --emit
/// zephyr-board` generates, and the emit REQUIRES it (there is no
/// sum-across-cores fallback the way there is for e.g. `west-libraries`).
/// Passing `--core` with any other target (or with none/`--all`, which never
/// resolves to `zephyr-board` alone) is refused rather than silently ignored --
/// silently ignoring it would let a user believe `--core` scoped a target it
/// does nothing for.
fn resolve_generate_targets(
    target: Option<&str>,
    all: bool,
    core: Option<&str>,
) -> Result<Vec<&'static str>, String> {
    if target != Some("zephyr-board") && core.is_some() {
        return Err("`--core` is only valid with `--target zephyr-board`.".to_string());
    }

    if all || target.is_none() {
        return Ok(ALL_EMIT_MODES.to_vec());
    }

    let target = target.unwrap_or_default();
    if target == "zephyr-board" {
        return match core {
            Some(_) => Ok(vec!["zephyr-board"]),
            None => Err(
                "`--target zephyr-board` requires `--core <id>` (it generates one \
                 core's Zephyr board tree)."
                    .to_string(),
            ),
        };
    }
    if let Some(mode) = ALL_EMIT_MODES.iter().copied().find(|mode| *mode == target) {
        return Ok(vec![mode]);
    }

    Err(format!("Unsupported generate target '{target}'."))
}

/// Map an emit mode to its output file. Most land under
/// `<workspace_root>/build/generated/` (ephemeral build artifacts), but the
/// `native_sim` overlay is a Zephyr board overlay: it must live at
/// `boards/native_sim_native_64.overlay` in the app source tree so
/// `west build -b native_sim/native/64` auto-discovers it. `zephyr-board`
/// writes a DIRECTORY of files (tan-cli#116), not one file: one subdirectory
/// per `--core`, under `build/boards/`, so `west build --board-root
/// build/boards` (docs/porting-new-som.md Step 7) finds every generated core's
/// tree without them colliding. `core` is `None` for every other target.
fn output_path_for_emit(workspace_root: &Path, emit: &str, core: Option<&str>) -> PathBuf {
    if emit == "native-sim-overlay" {
        return workspace_root
            .join("boards")
            .join("native_sim_native_64.overlay");
    }
    if emit == "zephyr-board" {
        // `resolve_generate_targets` refuses this target without `--core`, so
        // by the time this runs `core` is always Some; the fallback name is
        // only reached from a direct unit-test call.
        return workspace_root
            .join("build")
            .join("boards")
            .join(core.unwrap_or("board"));
    }

    let file_name = match emit {
        "zephyr-conf" => "alp.conf",
        "dts-overlay" => "alp.overlay",
        "cmake-args" => "alp-cmake-args.txt",
        "yocto-conf" => "alp-yocto.conf",
        "carrier-netlist" => "carrier-netlist.json",
        "west-libraries" => "alp-west-libs.yml",
        _ => "alp.out",
    };

    workspace_root
        .join("build")
        .join("generated")
        .join(file_name)
}

/// True when a bare `tan generate` (or `--target native-sim-overlay`) would
/// truncate an existing `boards/native_sim_native_64.overlay` without `--force`.
fn overlay_would_overwrite(workspace_root: &Path, targets: &[&str], force: bool) -> bool {
    !force
        && targets.contains(&"native-sim-overlay")
        && output_path_for_emit(workspace_root, "native-sim-overlay", None).exists()
}

/// Render `output_path` relative to `workspace_root`, falling back to the full
/// path when it is not under the root.
fn relative_or_full(workspace_root: &Path, output_path: &Path) -> String {
    output_path
        .strip_prefix(workspace_root)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| output_path.to_string_lossy().to_string())
}

/// The default Python interpreter name: `python` on Windows, `python3` elsewhere.
fn default_python_binary() -> &'static str {
    if cfg!(target_os = "windows") {
        "python"
    } else {
        "python3"
    }
}

/// Build the human-readable (non-JSON) output lines summarizing written/failed
/// targets, listing each target when `--verbose` is set.
fn generate_text_lines(g: &GlobalArgs, data: &GenerateData) -> Vec<String> {
    let mut lines = Vec::<String>::new();
    if data.failed.is_empty() {
        lines.push(format!(
            "generate: wrote {}/{} targets",
            data.written.len(),
            data.targets.len()
        ));
    } else {
        lines.push(format!(
            "generate: wrote {}/{}; failed: {}",
            data.written.len(),
            data.targets.len(),
            data.failed.join(", ")
        ));
    }

    if g.verbose {
        for target in &data.targets {
            lines.push(format!("target: {target}"));
        }
    }
    lines
}

/// A `GenerateData` with no targets/written/failed, used for early-failure envelopes.
fn empty_data() -> GenerateData {
    GenerateData {
        schema_version: "1".to_string(),
        targets: Vec::new(),
        written: Vec::new(),
        failed: Vec::new(),
    }
}

/// Build a failing `CommandRun` carrying a single `generate.{code}` error issue,
/// emitting either the JSON envelope or the provided text lines.
fn failure(
    g: &GlobalArgs,
    project: Project,
    exit: ExitCode,
    code: &str,
    message: &str,
    data: GenerateData,
    text_lines: Vec<String>,
) -> CommandRun {
    let issues = vec![Issue {
        code: format!("generate.{code}"),
        severity: "error".to_string(),
        message: message.to_string(),
    }];
    let text = if g.is_json() { Vec::new() } else { text_lines };
    let json = g
        .is_json()
        .then(|| Envelope::new("generate", project, data, issues, exit.code()).to_json());

    CommandRun { exit, text, json }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_resolution_defaults_to_all() {
        let resolved = resolve_generate_targets(None, false, None).unwrap();
        assert_eq!(resolved, ALL_EMIT_MODES.to_vec());
    }

    #[test]
    fn target_resolution_accepts_single_target() {
        let resolved = resolve_generate_targets(Some("cmake-args"), false, None).unwrap();
        assert_eq!(resolved, vec!["cmake-args"]);
    }

    #[test]
    fn target_resolution_rejects_unknown_target() {
        let err = resolve_generate_targets(Some("unknown"), false, None).unwrap_err();
        assert!(err.contains("Unsupported generate target"));
    }

    #[test]
    fn target_resolution_accepts_carrier_netlist() {
        // The Studio netlist handoff (alp-sdk#419) must reach the SDK spawn,
        // not be rejected at the allowlist. See ALL_EMIT_MODES.
        let resolved = resolve_generate_targets(Some("carrier-netlist"), false, None).unwrap();
        assert_eq!(resolved, vec!["carrier-netlist"]);
    }

    #[test]
    fn carrier_netlist_writes_a_json_artefact() {
        let path = output_path_for_emit(Path::new("/ws"), "carrier-netlist", None);
        assert!(path.ends_with("build/generated/carrier-netlist.json"));
    }

    #[test]
    fn target_resolution_accepts_native_sim_overlay() {
        // The native_sim overlay emit (alp-sdk#438) must reach the SDK spawn.
        let resolved = resolve_generate_targets(Some("native-sim-overlay"), false, None).unwrap();
        assert_eq!(resolved, vec!["native-sim-overlay"]);
    }

    #[test]
    fn native_sim_overlay_writes_a_board_overlay() {
        // Zephyr auto-discovers boards/<board>.overlay in the app source tree,
        // NOT build/generated -- so `west build -b native_sim/native/64` picks
        // it up and native_sim GPIO resolves.
        let path = output_path_for_emit(Path::new("/ws"), "native-sim-overlay", None);
        assert!(path.ends_with("boards/native_sim_native_64.overlay"));
    }

    #[test]
    fn target_resolution_accepts_west_libraries() {
        // tan-cli#114: the west.yml library auto-pin fragment must reach the
        // SDK spawn as a normal default-set target -- no --core required.
        let resolved = resolve_generate_targets(Some("west-libraries"), false, None).unwrap();
        assert_eq!(resolved, vec!["west-libraries"]);
        assert!(ALL_EMIT_MODES.contains(&"west-libraries"));
    }

    #[test]
    fn west_libraries_writes_the_documented_conventional_path() {
        // Matches alp-sdk docs/board-config-emit.md's own example verbatim.
        let path = output_path_for_emit(Path::new("/ws"), "west-libraries", None);
        assert!(
            path.ends_with("build/generated/alp-west-libs.yml"),
            "{path:?}"
        );
    }

    #[test]
    fn target_resolution_zephyr_board_requires_core() {
        // tan-cli#116: the FAILING case -- no --core must be refused, not
        // silently defaulted, since alp_project.py itself hard-requires it.
        let err = resolve_generate_targets(Some("zephyr-board"), false, None).unwrap_err();
        assert!(err.contains("--core"), "{err}");

        // With --core, it resolves to itself alone -- never folded into --all.
        let resolved =
            resolve_generate_targets(Some("zephyr-board"), false, Some("m55_hp")).unwrap();
        assert_eq!(resolved, vec!["zephyr-board"]);
        assert!(!ALL_EMIT_MODES.contains(&"zephyr-board"));
    }

    #[test]
    fn target_resolution_rejects_core_on_other_targets() {
        // The footgun this guards: --core silently doing nothing for a target
        // that doesn't consume it.
        let err = resolve_generate_targets(Some("cmake-args"), false, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
        let err = resolve_generate_targets(None, false, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
    }

    #[test]
    fn zephyr_board_writes_a_per_core_directory_under_build_boards() {
        let path = output_path_for_emit(Path::new("/ws"), "zephyr-board", Some("m55_hp"));
        assert!(path.ends_with("build/boards/m55_hp"), "{path:?}");
        // A different core must land in a different directory -- concurrent
        // `--target zephyr-board` runs for two cores must never collide.
        let other = output_path_for_emit(Path::new("/ws"), "zephyr-board", Some("a55_cluster"));
        assert_ne!(path, other);
    }

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("tan-generate-{tag}-{}", std::process::id()))
    }

    #[test]
    fn overlay_would_overwrite_true_only_when_file_exists_and_not_forced() {
        let ws = unique_temp_dir("overlay-guard");
        let _ = std::fs::remove_dir_all(&ws);
        std::fs::create_dir_all(ws.join("boards")).unwrap();
        std::fs::write(ws.join("boards").join("native_sim_native_64.overlay"), "x").unwrap();

        let targets = vec!["native-sim-overlay"];
        // Existing file, no --force -> would overwrite (this used to be silently
        // truncated with no existence check at all).
        assert!(overlay_would_overwrite(&ws, &targets, false));
        // --force opts back in, same as init/scaffold.
        assert!(!overlay_would_overwrite(&ws, &targets, true));
        // Target not requested -> no guard needed regardless of the file's presence.
        assert!(!overlay_would_overwrite(&ws, &["cmake-args"], false));

        let _ = std::fs::remove_dir_all(&ws);
    }

    #[test]
    fn overlay_would_overwrite_false_when_no_existing_file() {
        let ws = unique_temp_dir("overlay-guard-absent");
        let _ = std::fs::remove_dir_all(&ws);
        std::fs::create_dir_all(&ws).unwrap();

        assert!(!overlay_would_overwrite(
            &ws,
            &["native-sim-overlay"],
            false
        ));

        let _ = std::fs::remove_dir_all(&ws);
    }
}
