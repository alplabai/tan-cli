// SPDX-License-Identifier: Apache-2.0
//! `tan init` input resolution: parse `--cores`, and resolve the
//! template/name/destination from CLI args, interactive prompts, or defaults.

use std::path::Path;

use inquire::{InquireError, Select, Text};
use tan_core::is_plain_relative;
use tan_core::wizard::{
    WizardTemplateId, app_core_for_sku, infer_runtime_for_core_id, list_wizard_templates,
    vendored_app_core_for_sku, vendored_diagnostics_app_core_for_sku,
    vendored_edge_ai_app_core_for_sku, vendored_iot_app_core_for_sku,
    vendored_sensor_app_core_for_sku,
};

/// Accepted per-core OS values for `--cores` entries (`id:os`).
const CORE_OS_CHOICES: [&str; 4] = ["zephyr", "yocto", "baremetal", "off"];

/// Parse + validate `--cores` (`id[:os],…`) into `(id, os)` pairs. OS is
/// inferred from the core-id silicon class when omitted. Errors (the
/// `init.invalid-cores` issue, exit 2 — validation) on an id outside the
/// schema's `^[a-z][a-z0-9_]+$` pattern, an unknown OS, or a duplicate id —
/// invalid values would otherwise flow verbatim into board.yaml.
/// None/empty → no cores (single-core default).
pub(super) fn parse_cores(raw: Option<&str>) -> Result<Vec<(String, String)>, String> {
    let Some(raw) = raw else {
        return Ok(Vec::new());
    };
    let mut cores: Vec<(String, String)> = Vec::new();
    for entry in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let mut parts = entry.splitn(2, ':');
        let id = parts.next().unwrap_or("").trim().to_string();
        if id.is_empty() {
            continue;
        }
        let valid_id = id.len() >= 2
            && id.as_bytes()[0].is_ascii_lowercase()
            && id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
        if !valid_id {
            return Err(format!(
                "Invalid core id '{id}' in --cores (expected lowercase id matching ^[a-z][a-z0-9_]+$, e.g. m33_sm)."
            ));
        }
        let os = match parts.next().map(str::trim).filter(|s| !s.is_empty()) {
            Some(os) => {
                if !CORE_OS_CHOICES.contains(&os) {
                    return Err(format!(
                        "Invalid OS '{os}' for core '{id}' in --cores (expected one of: zephyr, yocto, baremetal, off)."
                    ));
                }
                os.to_string()
            }
            None => infer_runtime_for_core_id(&id).to_string(),
        };
        if cores.iter().any(|(existing, _)| existing == &id) {
            return Err(format!("Duplicate core id '{id}' in --cores."));
        }
        cores.push((id, os));
    }
    Ok(cores)
}

/// The fixed app-core id `template_id`'s plan will assign `sku`'s app source
/// to — the ground truth `tan init`'s upfront `--cores` validation must agree
/// with. `zephyr-app`/`sensor-starter`/`edge-ai-starter`/`board-diagnostics`/
/// `iot-starter` plan straight from their vendored SDK `minimal`/`sensor`/
/// `edge-ai`/`diagnostics`/`iot` scaffolds (alp-sdk#864/#903), whose
/// `board.yaml` `cores:` key is authoritative for that template
/// (`vendored_app_core_for_sku`/`vendored_sensor_app_core_for_sku`/
/// `vendored_edge_ai_app_core_for_sku`/`vendored_diagnostics_app_core_for_sku`/
/// `vendored_iot_app_core_for_sku`); every other, hand-generated template's
/// own `gen_board_yaml` derives it from `app_core_for_sku`'s SKU-prefix
/// heuristic instead. The two derivations can silently drift on an SDK
/// re-vendor (alp-sdk#877 fixed exactly that: `E1M-V2N101`'s vendored
/// `minimal` scaffold used to carry the non-buildable Alif `m55_hp` core) —
/// picking per-template here is what keeps the CLI-level check from
/// disagreeing with what `create_wizard_plan_with_cores` actually plans.
/// `edge-ai` is heterogeneous (its `board.yaml` lists a companion core before
/// the app core), so `vendored_edge_ai_app_core_for_sku` matters even more
/// here than for the single-core templates.
pub(super) fn app_core_for_template(template_id: WizardTemplateId, sku: &str) -> &'static str {
    match template_id {
        WizardTemplateId::ZephyrApp => vendored_app_core_for_sku(sku),
        WizardTemplateId::SensorStarter => vendored_sensor_app_core_for_sku(sku),
        WizardTemplateId::EdgeAiStarter => vendored_edge_ai_app_core_for_sku(sku),
        WizardTemplateId::BoardDiagnostics => vendored_diagnostics_app_core_for_sku(sku),
        WizardTemplateId::IotStarter => vendored_iot_app_core_for_sku(sku),
        _ => app_core_for_sku(sku),
    }
}

/// Outcome of resolving an interactive/CLI input that didn't succeed.
#[derive(Debug)]
pub(super) enum ResolveErr {
    /// User aborted the prompt (Ctrl-C / Esc) — maps to a runtime failure.
    Cancelled,
    /// A supplied argument was invalid; carries the user-facing message
    /// (rejected with `init.invalid-template`, exit 2 — validation).
    BadArg(String),
}
use ResolveErr::*;

/// Resolve the template id from `--template`, an interactive picker, or the
/// `ZephyrApp` default in non-interactive mode.
pub(super) fn resolve_template(
    arg: Option<&str>,
    interactive: bool,
) -> Result<WizardTemplateId, ResolveErr> {
    if let Some(s) = arg {
        return WizardTemplateId::from_str(s)
            .ok_or_else(|| BadArg(format!("Unknown template '{s}'.")));
    }
    if interactive {
        let templates = list_wizard_templates();
        let options: Vec<String> = templates
            .iter()
            .map(|d| format!("{} — {}", d.id.as_str(), d.description))
            .collect();
        return match Select::new("Select a project template:", options.clone()).prompt() {
            Ok(choice) => {
                let idx = options.iter().position(|o| *o == choice).unwrap_or(0);
                Ok(templates[idx].id)
            }
            Err(InquireError::OperationCanceled) | Err(InquireError::OperationInterrupted) => {
                Err(Cancelled)
            }
            Err(_) => Err(Cancelled),
        };
    }
    // `ZephyrApp`, not `MinimalApp` (tan-cli #97). `minimal-app` is
    // HAND-GENERATED: its CMakeLists.txt never calls `find_package(Zephyr
    // ...)`, so the very first `tan build` after a bare `tan init
    // --non-interactive` pointed west at a plain host CMake project and
    // linked an x86-64 binary for a core declared `os: zephyr`. `zephyr-app`
    // is vendored from the SDK's `minimal` scaffold and does the right thing
    // (find_package(Zephyr) + board.yaml -> alp.conf via EXTRA_CONF_FILE), so
    // the out-of-the-box path is buildable. `minimal-app` stays available via
    // an explicit `--template` and in the interactive picker.
    Ok(WizardTemplateId::ZephyrApp)
}

/// Resolve the optional project name from `--name` or an interactive prompt;
/// empty means scaffold directly into the destination.
pub(super) fn resolve_name(arg: Option<&str>, interactive: bool) -> Result<String, ResolveErr> {
    if let Some(s) = arg {
        return validate_name(s.to_string());
    }
    if interactive {
        return match Text::new("Project name (optional, leave blank to init in destination):")
            .with_default("")
            .prompt()
        {
            Ok(s) => validate_name(s.trim().to_string()),
            Err(InquireError::OperationCanceled) | Err(InquireError::OperationInterrupted) => {
                Err(Cancelled)
            }
            Err(_) => Err(Cancelled),
        };
    }
    Ok(String::new())
}

/// Both callers do `dest_path.join(&name)` unguarded, and `PathBuf::join`
/// keeps `..` lexically and discards `dest_path` entirely for an absolute (or,
/// on Windows, drive-relative/root-relative) right-hand side — so an
/// unchecked `--name` let the project root land anywhere the process can
/// write, `--force` and all. `--from-example` already gates its source the
/// same way; `--name` — the one input that decides *where* files land — did
/// not. Empty is allowed through: it means "scaffold directly into
/// destination", handled separately by the caller.
fn validate_name(name: String) -> Result<String, ResolveErr> {
    if name.is_empty() || is_plain_relative(Path::new(&name)) {
        Ok(name)
    } else {
        Err(BadArg(format!(
            "Invalid --name '{name}': must be a plain relative path (no '..', absolute, or drive-rooted segments)."
        )))
    }
}

/// Resolve the destination directory, preferring `--destination`, then the
/// global `--project`, then an interactive prompt, defaulting to `.`.
///
/// `name_given` is `--name`'s presence, and suppresses the prompt: `--name` is
/// documented as "creates a sub-directory when provided", so it has already
/// decided where the files land (`./<name>`). Asking `Destination directory:
/// (.)` after being handed `--name my-app` asks a question the caller answered
/// — a wrong prompt even at a real terminal, and half of tan-cli #187. The
/// RESOLVED name is deliberately not what gates this: a name typed at the
/// interactive prompt above leaves the destination question genuinely open, so
/// that human still gets asked.
pub(super) fn resolve_destination(
    arg: Option<&str>,
    project: Option<&str>,
    name_given: bool,
    interactive: bool,
) -> Result<String, ResolveErr> {
    if let Some(s) = arg {
        return Ok(s.to_string());
    }
    if let Some(p) = project {
        return Ok(p.to_string());
    }
    if interactive && !name_given {
        return match Text::new("Destination directory:")
            .with_default(".")
            .prompt()
        {
            Ok(s) => Ok(if s.trim().is_empty() {
                ".".to_string()
            } else {
                s
            }),
            Err(InquireError::OperationCanceled) | Err(InquireError::OperationInterrupted) => {
                Err(Cancelled)
            }
            Err(_) => Err(Cancelled),
        };
    }
    Ok(".".to_string())
}

#[cfg(test)]
mod tests {
    use super::{
        WizardTemplateId, app_core_for_template, parse_cores, resolve_destination, resolve_name,
        resolve_template,
    };

    #[test]
    fn a_given_name_answers_the_destination_question() {
        // tan-cli #187, second defect: `--name my-app` is documented as
        // "creates a sub-directory when provided", so it has already said
        // where the files go — yet `Destination directory: (.)` was asked
        // anyway. `interactive` is TRUE here on purpose: reaching the prompt
        // is the regression, and the only correct outcome is the `.` default
        // returned without asking.
        //
        // Deliberate exception to the "no prompt in a unit test" rule that
        // `tests/init_non_tty_stdin.rs` states, and it is not free: if this
        // ever regresses, on a DEVELOPER terminal `isatty(stdin)` is true and
        // crossterm reads the keyboard while the test harness swallows the
        // prompt's render — i.e. it blocks with a blank screen rather than
        // failing. On CI (no TTY and no openable `/dev/tty`) the prompt errors
        // into `Cancelled` and `.unwrap()` panics, which is a clean failure.
        // Kept because this is the only assertion of the flag's meaning at a
        // real terminal — the subprocess suite can only test the non-TTY half.
        assert_eq!(resolve_destination(None, None, true, true).unwrap(), ".");
        // Explicit inputs still outrank it, in the documented order.
        assert_eq!(
            resolve_destination(Some("out"), None, true, true).unwrap(),
            "out"
        );
        assert_eq!(
            resolve_destination(None, Some("proj"), true, true).unwrap(),
            "proj"
        );
        // Without `--name` and without a terminal, the default stands.
        assert_eq!(resolve_destination(None, None, false, false).unwrap(), ".");
    }

    #[test]
    fn the_non_interactive_default_template_is_a_real_zephyr_app() {
        // tan-cli #97: the non-interactive default used to be `MinimalApp`,
        // whose hand-generated CMakeLists.txt never calls
        // `find_package(Zephyr ...)` -- so a bare `tan init
        // --non-interactive` followed by `tan build` linked a host x86-64
        // binary for a core declared `os: zephyr`. The default must name a
        // template that actually loads Zephyr.
        assert_eq!(
            resolve_template(None, false).unwrap(),
            WizardTemplateId::ZephyrApp
        );
        // An explicit --template still wins, minimal-app included.
        assert_eq!(
            resolve_template(Some("minimal-app"), false).unwrap(),
            WizardTemplateId::MinimalApp
        );
    }

    #[test]
    fn app_core_for_template_uses_the_vendored_core_for_zephyr_app() {
        // zephyr-app plans off the vendored SDK scaffold -- its board.yaml's
        // real cores: key (m33_sm for V2N, not app_core_for_sku's guess if
        // the two were ever to disagree again).
        assert_eq!(
            app_core_for_template(WizardTemplateId::ZephyrApp, "E1M-V2N101"),
            "m33_sm"
        );
        assert_eq!(
            app_core_for_template(WizardTemplateId::ZephyrApp, "E1M-AEN801"),
            "m55_hp"
        );
    }

    #[test]
    fn app_core_for_template_uses_the_vendored_core_for_sensor_starter() {
        // sensor-starter also plans off a vendored SDK scaffold (`sensor`) --
        // same treatment as zephyr-app, off its OWN vendored tree.
        assert_eq!(
            app_core_for_template(WizardTemplateId::SensorStarter, "E1M-V2N101"),
            "m33_sm"
        );
        assert_eq!(
            app_core_for_template(WizardTemplateId::SensorStarter, "E1M-AEN801"),
            "m55_hp"
        );
    }

    #[test]
    fn app_core_for_template_uses_the_vendored_core_for_edge_ai_starter() {
        // edge-ai-starter also plans off a vendored SDK scaffold (`edge-ai`)
        // -- same treatment as zephyr-app/sensor-starter, off its OWN
        // vendored tree. This is the FIRST heterogeneous vendored template:
        // its board.yaml lists a companion core (a55_cluster/a32_cluster,
        // os: "off") BEFORE the app core, so this specifically exercises
        // that the CLI-level check picks the app core, not the companion.
        assert_eq!(
            app_core_for_template(WizardTemplateId::EdgeAiStarter, "E1M-V2N101"),
            "m33_sm"
        );
        assert_eq!(
            app_core_for_template(WizardTemplateId::EdgeAiStarter, "E1M-AEN801"),
            "m55_hp"
        );
    }

    #[test]
    fn app_core_for_template_uses_the_vendored_core_for_board_diagnostics() {
        // board-diagnostics also plans off a vendored SDK scaffold
        // (`diagnostics`) -- same treatment as zephyr-app/sensor-starter,
        // off its OWN vendored tree.
        assert_eq!(
            app_core_for_template(WizardTemplateId::BoardDiagnostics, "E1M-V2N101"),
            "m33_sm"
        );
        assert_eq!(
            app_core_for_template(WizardTemplateId::BoardDiagnostics, "E1M-AEN801"),
            "m55_hp"
        );
    }

    #[test]
    fn app_core_for_template_uses_the_vendored_core_for_iot_starter() {
        // iot-starter's vendored tree covers only E1M-AEN801 -- there is no
        // second family to check here, unlike the other vendored templates.
        assert_eq!(
            app_core_for_template(WizardTemplateId::IotStarter, "E1M-AEN801"),
            "m55_hp"
        );
    }

    #[test]
    fn app_core_for_template_uses_the_heuristic_for_hand_generated_templates() {
        // minimal-app is the only remaining hand-generated template (see
        // wizard/vendored/MANIFEST.md) -- its own gen_board_yaml derives the
        // app core from app_core_for_sku; the CLI-level check must match
        // that, not a vendored-tree derivation.
        assert_eq!(
            app_core_for_template(WizardTemplateId::MinimalApp, "E1M-V2N101"),
            "m33_sm"
        );
        assert_eq!(
            app_core_for_template(WizardTemplateId::MinimalApp, "E1M-NX9101"),
            "m33"
        );
    }

    #[test]
    fn parse_cores_accepts_valid_entries_and_infers_os() {
        let cores = parse_cores(Some("m33_sm:zephyr, a55_cluster")).unwrap();
        assert_eq!(
            cores,
            vec![
                ("m33_sm".to_string(), "zephyr".to_string()),
                ("a55_cluster".to_string(), "yocto".to_string()),
            ]
        );
        assert!(parse_cores(None).unwrap().is_empty());
        assert!(parse_cores(Some("  ,, ")).unwrap().is_empty());
    }

    #[test]
    fn parse_cores_rejects_bad_id_bad_os_and_duplicates() {
        // Schema pattern ^[a-z][a-z0-9_]+$ — invalid ids must not reach board.yaml.
        assert!(parse_cores(Some("Weird-ID!:yocto")).is_err());
        assert!(parse_cores(Some("m33_sm:freertos")).is_err());
        assert!(parse_cores(Some("m33_sm,m33_sm:zephyr")).is_err());
    }

    #[test]
    fn resolve_name_accepts_plain_relative_and_empty() {
        assert_eq!(resolve_name(Some("my-app"), false).unwrap(), "my-app");
        assert_eq!(resolve_name(Some("sub/dir"), false).unwrap(), "sub/dir");
        assert_eq!(resolve_name(None, false).unwrap(), "");
    }

    #[test]
    fn resolve_name_rejects_traversal_and_absolute() {
        // Regression: `--name` was joined verbatim onto `--destination`
        // (`dest_path.join(&name)`); an unchecked `..` or absolute name let
        // the project root — and, with `--force`, an overwrite target — land
        // outside the destination the caller chose.
        assert!(resolve_name(Some("../escape"), false).is_err());
        assert!(resolve_name(Some("/etc/passwd"), false).is_err());
        assert!(resolve_name(Some("a/../../b"), false).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn resolve_name_rejects_windows_rooted_and_drive_relative() {
        assert!(resolve_name(Some(r"\windows\x"), false).is_err());
        assert!(resolve_name(Some(r"C:foo"), false).is_err());
        assert!(resolve_name(Some(r"C:\x"), false).is_err());
    }
}
