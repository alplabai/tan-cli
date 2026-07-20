// SPDX-License-Identifier: Apache-2.0
//! `tan init` input resolution: parse `--cores`, and resolve the
//! template/name/destination from CLI args, interactive prompts, or defaults.

use std::path::Path;

use inquire::{InquireError, Select, Text};
use tan_core::is_plain_relative;
use tan_core::wizard::{WizardTemplateId, infer_runtime_for_core_id, list_wizard_templates};

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
/// `MinimalApp` default in non-interactive mode.
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
    Ok(WizardTemplateId::MinimalApp)
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
pub(super) fn resolve_destination(
    arg: Option<&str>,
    project: Option<&str>,
    interactive: bool,
) -> Result<String, ResolveErr> {
    if let Some(s) = arg {
        return Ok(s.to_string());
    }
    if let Some(p) = project {
        return Ok(p.to_string());
    }
    if interactive {
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
    use super::{parse_cores, resolve_name};

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
