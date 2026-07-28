// SPDX-License-Identifier: Apache-2.0
//! `tan generate` — generate board-derived output files.

use std::path::{Path, PathBuf};
use std::process::Command;

use super::CommandRun;
use crate::cli::{GenerateArgs, GlobalArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
// Every supported `--emit` mode, used as the default target set when neither
// `--target` nor `--all` narrows the selection.
//
// tan-cli#165: this used to be a SECOND, hand-maintained copy of the target
// list, independent of `tan_core::loader::GENERATION_TARGET_CATALOG` (the
// list `explain`/`trace`/`support-bundle` read) -- exactly how the two
// drifted apart, silently, with nothing to catch it (the catalog carried
// only 4 of these 8). Fixed by deleting this copy: `generate` now reads
// `tan_core::ALL_EMIT_MODES` directly, the same single source `explain`
// (`generation_target_support`/`list_generation_target_support`) and
// `trace`/`support-bundle` (`ALL_EMIT_MODES` itself) already read.
//
// `os-topology` (tan-cli#115) is the ninth entry: `--emit os-topology
// --output <path>` was measured live against the pinned SDK tag and writes
// a normal single file exactly like the other eight (its own docs describe
// the bare/no-`--output` invocation as "JSON to stdout, for IDEs", but that
// is a documented convenience default, not a technical restriction --
// `alp_project.py`'s shared `_write_or_print` helper honors `--output` for
// it identically to `carrier-netlist`/`west-libraries`/`hw-info-h`), so it
// drops into this same `generate --target` shape rather than needing a new
// `tan inspect` verb.
//
// `zephyr-board` (tan-cli#116) is deliberately NOT in `ALL_EMIT_MODES`:
// unlike every mode above, it hard-requires `--core <id>` and writes a
// DIRECTORY of files, not one fixed conventional file, so it cannot be
// defaulted by a bare `tan generate` / `--all` the way these nine can. It
// is reachable only via explicit `--target zephyr-board --core <id>` -- see
// `resolve_generate_targets`.
use tan_core::ALL_EMIT_MODES;

/// Targets `--core` optionally scopes, beyond `zephyr-board` (which hard-
/// requires it). Verbatim from `alp_project.py`'s own `--core` help at the
/// pinned SDK tag: for the per-core modes (`zephyr-conf`/`yocto-conf`/
/// `cmake-args`) it picks the single slice to emit; for the project-wide
/// modes (`dts-overlay`/`west-libraries`/`hw-info-h`) it scopes the union
/// calculation to one slice (for `hw-info-h` specifically: which slice's OS
/// lands in the generated `ALP_HW_BUILD_OS`/`ALP_HW_BUILD_PRIMARY_CORE`
/// macros). The SDK itself hard-errors when `--core` names a core outside
/// a per-core mode's OS class (e.g. `--emit yocto-conf --core <a-zephyr-core>`),
/// so `tan` does not need to re-validate that compatibility here.
///
/// `carrier-netlist` and `native-sim-overlay` are deliberately NOT in this
/// set: both are board-/SoM-mounting facts `alp_project.py` never reads
/// `--core` for at all, so passing it would silently do nothing -- the
/// footgun `resolve_generate_targets` refuses instead of tolerating.
/// `os-topology` (tan-cli#115) is likewise excluded: measured live, the SDK
/// accepts `--core` for it but ignores it outright (`alp_project.py: --core
/// is ignored for --emit os-topology (project-level emit)`, printed to
/// stderr, exit 0, unchanged output covering every core regardless) --
/// exactly the same "does nothing" shape `carrier-netlist`/`native-sim-overlay`
/// are refused for, not a genuine per-core scope.
const CORE_SCOPABLE_TARGETS: [&str; 6] = [
    "zephyr-conf",
    "yocto-conf",
    "cmake-args",
    "dts-overlay",
    "west-libraries",
    "hw-info-h",
];

/// True when `emit` accepts `--core` as an optional scoping flag (in addition
/// to `zephyr-board`, which hard-requires it and is handled separately by
/// both `resolve_generate_targets` and the command-building loop).
fn target_accepts_core(emit: &str) -> bool {
    CORE_SCOPABLE_TARGETS.contains(&emit)
}

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

    // Every `resolve_generate_targets` error is a user-supplied argument-shape
    // mistake (an unknown --target, or --core paired with a target that
    // doesn't accept it) -- never an internal fault -- so this reports
    // ExitCode::ValidationFailure (2) / "generate.invalid-target", not
    // ExitCode::InternalFailure (5) / "generate.internal-failure" (tan-cli#117
    // review finding 3: an ordinary usage mistake used to surface identically
    // to a genuine bug).
    let targets = match resolve_generate_targets(g.target.as_deref(), g.all, args.core.as_deref()) {
        Ok(t) => t,
        Err(message) => {
            let copy = message.clone();
            return failure(
                g,
                project,
                ExitCode::ValidationFailure,
                "invalid-target",
                &message,
                empty_data(),
                vec![
                    "generate: invalid target/argument combination".to_string(),
                    copy,
                ],
            );
        }
    };

    // zephyr-board is always resolved alone (see resolve_generate_targets), so
    // deriving its output directory name once here, up front, covers the one
    // iteration of the emit loop below that needs it. `--core` is Some here:
    // resolve_generate_targets already refused `--target zephyr-board` with
    // no `--core`.
    //
    // tan-cli#116 review finding 1: this used to be a bare `core` id
    // (`build/boards/<core>/`), not the SDK's own `alp_e1m_<sku-slug>_<core>`
    // board-directory convention -- so two SoMs sharing a core id (e.g. a
    // project retargeted from E1M-AEN901 to E1M-AEN801, both with `m55_hp`)
    // collided in the same `build/boards/m55_hp/`, each run's files landing
    // beside the other SoM's stale ones instead of a fresh directory.
    let mut zephyr_board_dir_name: Option<String> = None;
    if targets.contains(&"zephyr-board") {
        let core = args
            .core
            .as_deref()
            .expect("resolve_generate_targets requires --core for zephyr-board");
        let sku = std::fs::read_to_string(&board_path)
            .ok()
            .and_then(|text| tan_core::parse_board_model(&text).ok())
            .and_then(|model| model.som)
            .and_then(|som| som.sku);
        match sku
            .as_deref()
            .and_then(|sku| tan_core::zephyr_board_dir_name(sku, core))
        {
            Some(name) => zephyr_board_dir_name = Some(name),
            None => {
                return failure(
                    g,
                    project,
                    ExitCode::ValidationFailure,
                    "board-sku-unresolved",
                    "board.yaml's som.sku is missing or is not an E1M-* SKU; `--target \
                     zephyr-board` needs it to name the generated board directory \
                     (alp-sdk's `alp_e1m_<sku-slug>_<core>` convention).",
                    empty_data(),
                    vec![
                        "generate: som.sku is missing or unrecognised; cannot name the \
                         zephyr-board directory."
                            .to_string(),
                    ],
                );
            }
        }
    }

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
        let output_path =
            output_path_for_emit(&workspace_root, emit, zephyr_board_dir_name.as_deref());
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
        // zephyr-board hard-requires --core (resolve_generate_targets already
        // refused this target with no --core, so this is always Some here);
        // the CORE_SCOPABLE_TARGETS accept it optionally -- forwarding it is
        // what makes e.g. `tan generate --target zephyr-conf --core m55_hp`
        // byte-identical to what the planner's build-plan `configArtefacts`
        // materialises for the same core (tan-cli#117 review finding 2).
        if *emit == "zephyr-board" || target_accepts_core(emit) {
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
/// `--core` (tan-cli#116) is REQUIRED for `zephyr-board` (it picks which
/// core's Zephyr board tree `alp_project.py --emit zephyr-board` generates;
/// there is no sum-across-cores fallback the way there is for e.g.
/// `west-libraries`), and is optionally accepted -- passed through, not
/// required -- for [`CORE_SCOPABLE_TARGETS`] (tan-cli#116 review finding 2:
/// `alp_project.py`'s own `--core` help documents it as meaningful for five
/// more targets than the single one this used to allow). Passing `--core`
/// with any other target (or with none/`--all`, which never resolves to a
/// single core-aware target alone) is refused rather than silently ignored --
/// silently ignoring it would let a user believe `--core` scoped a target it
/// does nothing for.
fn resolve_generate_targets(
    target: Option<&str>,
    all: bool,
    core: Option<&str>,
) -> Result<Vec<&'static str>, String> {
    if core.is_some() {
        let core_is_valid_here = match target {
            Some("zephyr-board") => true,
            Some(t) if !all => target_accepts_core(t),
            _ => false,
        };
        if !core_is_valid_here {
            return Err(format!(
                "`--core` is only valid with `--target zephyr-board` (required), or one \
                 of {} (optional scoping); it does nothing for the default/--all target set.",
                CORE_SCOPABLE_TARGETS.join(", ")
            ));
        }
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

/// Map an emit mode to its output file.
///
/// tan-cli#165: this used to hand-duplicate a `match emit { .. }` file-name
/// table that had already drifted out of sync with
/// `tan_core::loader::GENERATION_TARGET_CATALOG` (the same paths, a second
/// hand-maintained copy). Fixed by reading
/// [`tan_core::generation_target_support`] instead -- its
/// `output_relative_path` is exactly this path, workspace-root-relative,
/// for every target except `zephyr-board`. Most land under
/// `<workspace_root>/build/generated/` (ephemeral build artifacts); the
/// `native_sim` overlay's catalog entry instead points at
/// `boards/native_sim_native_64.overlay` in the app source tree so
/// `west build -b native_sim/native/64` auto-discovers it -- no special
/// case needed here, the catalog value already differs per-target.
///
/// `zephyr-board` writes a DIRECTORY of files (tan-cli#116), not one file:
/// one subdirectory per SDK board-dir name, under `build/boards/`, so
/// `west build --board-root build/boards` (docs/porting-new-som.md Step 7,
/// which pairs `--output build/boards/alp_e1m_aen901_m55_hp/` with
/// `--board-root build/boards`) finds every generated board's tree without
/// them colliding. Its catalog entry's `output_relative_path` is
/// documentary only (`is_directory: true`), so this is the one target still
/// special-cased here rather than read from the catalog: `board_dir_name`
/// (the caller's already-resolved [`tan_core::zephyr_board_dir_name`]
/// result, e.g. `alp_e1m_aen801_m55_hp` -- NOT a bare core id: two SoMs
/// sharing a core id must never collide, tan-cli#116 review finding 1) is
/// `None` for every other target.
fn output_path_for_emit(
    workspace_root: &Path,
    emit: &str,
    board_dir_name: Option<&str>,
) -> PathBuf {
    if emit == "zephyr-board" {
        // `run` always resolves `board_dir_name` before reaching this target
        // (returning a validation failure otherwise), so it is always Some in
        // the real call path; the fallback is only reached from a direct
        // unit-test call.
        return workspace_root
            .join("build")
            .join("boards")
            .join(board_dir_name.unwrap_or("board"));
    }

    let relative = tan_core::generation_target_support(emit)
        .map(|target| target.output_relative_path)
        .unwrap_or("build/generated/alp.out");
    workspace_root.join(relative)
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
    use crate::cli::Format;

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
    fn target_resolution_accepts_hw_info_h() {
        // tan-cli#113: the ALP_HW_BUILD_* identifier-header emit must reach
        // the SDK spawn as a normal default-set target -- no --core required.
        let resolved = resolve_generate_targets(Some("hw-info-h"), false, None).unwrap();
        assert_eq!(resolved, vec!["hw-info-h"]);
        assert!(ALL_EMIT_MODES.contains(&"hw-info-h"));
    }

    #[test]
    fn hw_info_h_writes_the_documented_conventional_path() {
        // Matches alp-sdk docs/board-config-emit.md's own example verbatim,
        // read at the pinned SDK tag (not copied from a floating `dev`).
        let path = output_path_for_emit(Path::new("/ws"), "hw-info-h", None);
        assert!(
            path.ends_with("build/generated/alp_hw_info_build.h"),
            "{path:?}"
        );
    }

    #[test]
    fn target_resolution_accepts_os_topology() {
        // tan-cli#115: the per-core natural-vs-effective OS facts emit must
        // reach the SDK spawn as a normal default-set target -- no --core
        // (the SDK ignores it for this target; see CORE_SCOPABLE_TARGETS'
        // doc comment) required.
        let resolved = resolve_generate_targets(Some("os-topology"), false, None).unwrap();
        assert_eq!(resolved, vec!["os-topology"]);
        assert!(ALL_EMIT_MODES.contains(&"os-topology"));
    }

    #[test]
    fn os_topology_writes_a_json_artefact() {
        // tan-cli#115: measured live against the pinned SDK tag -- `--emit
        // os-topology --output <path>` writes a normal single JSON file
        // exactly like carrier-netlist, not stdout-only.
        let path = output_path_for_emit(Path::new("/ws"), "os-topology", None);
        assert!(
            path.ends_with("build/generated/os-topology.json"),
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
    fn target_resolution_rejects_core_on_targets_that_dont_consume_it() {
        // The footgun this guards: --core silently doing nothing for a target
        // that doesn't consume it (carrier-netlist/native-sim-overlay are
        // board-/SoM-mounting facts alp_project.py never reads --core for;
        // os-topology accepts but ignores it, tan-cli#115), or for the
        // default/--all set (which mixes core-scoped and core-blind targets
        // in one run).
        let err =
            resolve_generate_targets(Some("carrier-netlist"), false, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
        let err = resolve_generate_targets(Some("native-sim-overlay"), false, Some("m55_hp"))
            .unwrap_err();
        assert!(err.contains("--core"), "{err}");
        let err = resolve_generate_targets(Some("os-topology"), false, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
        let err = resolve_generate_targets(None, false, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
        let err = resolve_generate_targets(Some("cmake-args"), true, Some("m55_hp")).unwrap_err();
        assert!(err.contains("--core"), "{err}");
    }

    #[test]
    fn target_resolution_accepts_core_for_the_scopable_targets() {
        // tan-cli#116 review finding 2: --core used to be hard-refused for
        // every target except zephyr-board, contradicting alp_project.py's
        // own --core help at the pinned SDK tag, which documents it as
        // meaningful for these five as well. The FAILING case this guards:
        // `--target zephyr-conf --core m55_hp` used to be refused outright.
        for target in CORE_SCOPABLE_TARGETS {
            let resolved = resolve_generate_targets(Some(target), false, Some("m55_hp")).unwrap();
            assert_eq!(resolved, vec![target], "target={target}");
        }
    }

    #[test]
    fn target_accepts_core_matches_the_scopable_set() {
        assert!(target_accepts_core("zephyr-conf"));
        assert!(target_accepts_core("yocto-conf"));
        assert!(target_accepts_core("cmake-args"));
        assert!(target_accepts_core("dts-overlay"));
        assert!(target_accepts_core("west-libraries"));
        assert!(target_accepts_core("hw-info-h"));
        // The FAILING case: a target --core does nothing for must stay false,
        // or the command-building loop would silently forward a flag the SDK
        // never reads for it.
        assert!(!target_accepts_core("carrier-netlist"));
        assert!(!target_accepts_core("native-sim-overlay"));
        assert!(!target_accepts_core("zephyr-board"));
        assert!(!target_accepts_core("os-topology"));
    }

    #[test]
    fn zephyr_board_writes_under_the_sdk_board_dir_name_not_a_bare_core_id() {
        // tan-cli#116 review finding 1: the directory must be the SDK's own
        // `alp_e1m_<sku-slug>_<core>` convention, not a bare core id.
        let dir_name = tan_core::zephyr_board_dir_name("E1M-AEN801", "m55_hp").unwrap();
        let path = output_path_for_emit(Path::new("/ws"), "zephyr-board", Some(&dir_name));
        assert!(
            path.ends_with("build/boards/alp_e1m_aen801_m55_hp"),
            "{path:?}"
        );

        // The FAILING case the bare-core-id naming produced: two projects on
        // the SAME core but DIFFERENT SoMs (a first-class SoM-swap flow) used
        // to collide in one `build/boards/m55_hp/` directory. With the SKU
        // folded into the name they never collide.
        let other_dir_name = tan_core::zephyr_board_dir_name("E1M-AEN901", "m55_hp").unwrap();
        let other = output_path_for_emit(Path::new("/ws"), "zephyr-board", Some(&other_dir_name));
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

    /// Turns `dir` into a directory `has_loader_script` accepts, without
    /// needing a real `alp_project.py` -- these `run()`-level tests all fail
    /// out before ever spawning it.
    fn make_sdk_root(dir: &std::path::Path) {
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("alp_project.py"), "").unwrap();
    }

    fn global(
        project: &std::path::Path,
        sdk_root: &std::path::Path,
        target: Option<&str>,
    ) -> GlobalArgs {
        GlobalArgs {
            project: Some(project.to_string_lossy().into_owned()),
            board_yaml: None,
            sdk_root: Some(sdk_root.to_string_lossy().into_owned()),
            target: target.map(str::to_string),
            all: false,
            format: Format::Json,
            verbose: false,
            quiet: false,
            no_color: false,
            non_interactive: false,
            ci: false,
        }
    }

    #[test]
    fn zephyr_board_without_core_is_a_validation_failure_not_internal() {
        // tan-cli#116 review finding 3: an ordinary usage mistake (here,
        // `--target zephyr-board` with no `--core`, which alp_project.py
        // itself hard-requires) must not surface as
        // ExitCode::InternalFailure ("bug/unreachable state"). FAILING case:
        // this used to be exit code 5 with issue code
        // "generate.internal-failure".
        let ws = unique_temp_dir("no-core-validation-ws");
        let _ = std::fs::remove_dir_all(&ws);
        std::fs::create_dir_all(&ws).unwrap();
        std::fs::write(ws.join("board.yaml"), "som:\n  sku: E1M-AEN801\n").unwrap();
        let sdk = unique_temp_dir("no-core-validation-sdk");
        let _ = std::fs::remove_dir_all(&sdk);
        make_sdk_root(&sdk);

        let g = global(&ws, &sdk, Some("zephyr-board"));
        let result = run(
            &g,
            &GenerateArgs {
                force: false,
                core: None,
            },
        );

        assert_eq!(result.exit, ExitCode::ValidationFailure);
        let json: serde_json::Value =
            serde_json::from_str(result.json.as_deref().expect("json envelope")).unwrap();
        assert_eq!(json["exitCode"], 2);
        assert_ne!(json["issues"][0]["code"], "generate.internal-failure");

        let _ = std::fs::remove_dir_all(&ws);
        let _ = std::fs::remove_dir_all(&sdk);
    }

    #[test]
    fn zephyr_board_with_a_non_e1m_sku_is_refused_before_naming_a_directory() {
        // tan-cli#116 review finding 1's guard: without a resolvable
        // `alp_e1m_<sku-slug>_<core>` name, `run()` must refuse rather than
        // fall back to the collision-prone bare-core-id directory.
        let ws = unique_temp_dir("bad-sku-ws");
        let _ = std::fs::remove_dir_all(&ws);
        std::fs::create_dir_all(&ws).unwrap();
        std::fs::write(ws.join("board.yaml"), "som:\n  sku: BOGUS-SKU\n").unwrap();
        let sdk = unique_temp_dir("bad-sku-sdk");
        let _ = std::fs::remove_dir_all(&sdk);
        make_sdk_root(&sdk);

        let g = global(&ws, &sdk, Some("zephyr-board"));
        let result = run(
            &g,
            &GenerateArgs {
                force: false,
                core: Some("m55_hp".to_string()),
            },
        );

        assert_eq!(result.exit, ExitCode::ValidationFailure);
        let json: serde_json::Value =
            serde_json::from_str(result.json.as_deref().expect("json envelope")).unwrap();
        assert_eq!(json["issues"][0]["code"], "generate.board-sku-unresolved");

        let _ = std::fs::remove_dir_all(&ws);
        let _ = std::fs::remove_dir_all(&sdk);
    }
}
