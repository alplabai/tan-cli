// SPDX-License-Identifier: Apache-2.0
//! `--emit kconfig` contract (tan-cli #35, alp-sdk#894) — the **consumed**
//! shape of the SDK's `alp_orchestrate --emit kconfig --core <id>`: a
//! board-scoped, user-settable Kconfig symbol menu for one core (the vscode
//! `prj.conf` LSP's live feed).
//!
//! Unlike every other emit `tan-core` models (`build_plan`/`system_manifest`),
//! this one is **workspace-dependent**: the SDK needs a bootstrapped
//! `ZEPHYR_BASE` to run the real Kconfig solver, so `tan-cli::commands::kconfig`
//! resolves + injects it before spawning (`tan-cli`'s own
//! `commands::build::resolve_zephyr_base`) — this module only parses the
//! resulting JSON and, when `--core` is omitted, picks it from the project's
//! declared cores. Pure: no IO, no Zephyr/SDK spawn.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::model::CoreEntry;

/// The kconfig emit schema version this CLI knows how to consume.
pub const KCONFIG_SCHEMA_VERSION: u32 = 1;

/// One promptable Kconfig symbol, projected by the SDK's real Kconfig solver
/// (`kconfiglib`-backed) — `CONFIG_` stripped (the LSP prepends it back),
/// scoped to symbols with a real Kconfig prompt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KconfigSymbol {
    /// Bare symbol name (no `CONFIG_` prefix).
    pub name: String,
    /// Kconfig type: `bool` | `tristate` | `int` | `hex` | `string`.
    pub r#type: String,
    /// The symbol's user-facing prompt text.
    pub prompt: String,
    /// Aggregated `depends on` expression (empty string when there is none).
    #[serde(default)]
    pub depends: String,
    /// Resolved default expression, or `None` when the symbol has none.
    #[serde(default)]
    pub default: Option<String>,
    /// Help text (empty string when there is none).
    #[serde(default)]
    pub help: String,
}

/// The whole `--emit kconfig` document — the deserialization target for
/// `alp_orchestrate --emit kconfig --core <id>` AND the `data` payload of
/// `tan kconfig`'s envelope (mirrored field-for-field, so `tan kconfig`'s
/// `data` is exactly the emit body).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KconfigData {
    /// Emit schema version; must equal `KCONFIG_SCHEMA_VERSION` to be consumed.
    #[serde(rename = "schemaVersion")]
    pub schema_version: u32,
    /// The resolved Zephyr board target the symbols were solved for.
    pub board: String,
    /// The `--core <id>` this menu was scoped to.
    pub core: String,
    /// Promptable symbols, sorted by `name`.
    #[serde(default)]
    pub symbols: Vec<KconfigSymbol>,
}

/// Why a `--emit kconfig` JSON document could not be consumed.
#[derive(Debug, thiserror::Error)]
pub enum KconfigError {
    /// The document failed JSON deserialization; holds the parse error text.
    #[error("kconfig emit is not valid JSON: {0}")]
    Json(String),
    /// The document's `schemaVersion` differs from the version this CLI consumes.
    #[error(
        "unsupported kconfig schemaVersion {found} (this CLI consumes v{supported}); \
         upgrade the CLI or the SDK so the versions match"
    )]
    UnsupportedSchemaVersion { found: u32, supported: u32 },
}

/// Just enough of the document to read `schemaVersion` without requiring
/// every other field to be present first — mirrors
/// `build_plan::parse_build_plan`'s probe-first version-skew guard.
#[derive(Deserialize)]
struct SchemaVersionProbe {
    #[serde(rename = "schemaVersion")]
    schema_version: u32,
}

/// Parse + version-guard a `--emit kconfig` JSON document. Pure: no IO.
pub fn parse_kconfig(json: &str) -> Result<KconfigData, KconfigError> {
    if let Ok(probe) = serde_json::from_str::<SchemaVersionProbe>(json) {
        if probe.schema_version != KCONFIG_SCHEMA_VERSION {
            return Err(KconfigError::UnsupportedSchemaVersion {
                found: probe.schema_version,
                supported: KCONFIG_SCHEMA_VERSION,
            });
        }
    }
    serde_json::from_str(json).map_err(|e| KconfigError::Json(e.to_string()))
}

/// Resolve the `--core <id>` to scope a `tan kconfig` emit to when none was
/// given explicitly: the board's one declared Zephyr core, when unambiguous.
///
/// Deliberately reads the project's ACTUAL declared cores (`board.yaml`'s
/// `cores:` block) rather than guessing from the SoM SKU alone (the
/// `app_core_for_sku` heuristic `tan init`/`tan scaffold` use before a
/// board.yaml exists) — a multi-core board's kconfig menu differs per core,
/// so silently guessing wrong here would hand the LSP the wrong core's menu.
/// Zero or more than one Zephyr core is genuinely ambiguous: the caller must
/// pass `--core` explicitly, so this returns every declared core id (sorted)
/// for the error message to name, mirroring the SDK's own `--core <id> not
/// present ... (have: ...)` message shape.
pub fn resolve_default_kconfig_core(
    cores: Option<&BTreeMap<String, CoreEntry>>,
) -> Result<String, Vec<String>> {
    let empty = BTreeMap::new();
    let cores = cores.unwrap_or(&empty);
    let zephyr_cores: Vec<&String> = cores
        .iter()
        .filter(|(_, entry)| entry.os.as_deref() == Some("zephyr"))
        .map(|(id, _)| id)
        .collect();
    if let [only] = zephyr_cores.as_slice() {
        return Ok((*only).clone());
    }
    Err(cores.keys().cloned().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
        "schemaVersion": 1,
        "board": "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he",
        "core": "m55_he",
        "symbols": [
            { "name": "LOG", "type": "bool", "prompt": "Logging",
              "depends": "y", "default": "n", "help": "Enable logging." },
            { "name": "MAIN_STACK_SIZE", "type": "int", "prompt": "Main stack size",
              "depends": "", "default": null, "help": "" }
        ]
    }"#;

    #[test]
    fn parse_kconfig_round_trips_the_sample_envelope_shape() {
        let data = parse_kconfig(SAMPLE).unwrap();
        assert_eq!(data.schema_version, 1);
        assert_eq!(data.board, "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he");
        assert_eq!(data.core, "m55_he");
        assert_eq!(data.symbols.len(), 2);
        assert_eq!(data.symbols[0].name, "LOG");
        assert_eq!(data.symbols[0].r#type, "bool");
        assert_eq!(data.symbols[0].default.as_deref(), Some("n"));
        assert_eq!(data.symbols[1].default, None);
    }

    #[test]
    fn parse_kconfig_serializes_the_type_field_as_type() {
        let data = parse_kconfig(SAMPLE).unwrap();
        let json = serde_json::to_value(&data).unwrap();
        assert_eq!(json["symbols"][0]["type"], "bool");
        assert!(json["symbols"][0].get("r#type").is_none());
        // schemaVersion round-trips as a JSON number, not a string.
        assert!(json["schemaVersion"].is_number());
        assert_eq!(json["schemaVersion"], 1);
    }

    #[test]
    fn parse_kconfig_rejects_unsupported_schema_version() {
        let bad = SAMPLE.replacen("\"schemaVersion\": 1", "\"schemaVersion\": 2", 1);
        match parse_kconfig(&bad) {
            Err(KconfigError::UnsupportedSchemaVersion { found, supported }) => {
                assert_eq!(found, 2);
                assert_eq!(supported, KCONFIG_SCHEMA_VERSION);
            }
            other => panic!("expected UnsupportedSchemaVersion, got {other:?}"),
        }
    }

    #[test]
    fn parse_kconfig_rejects_malformed_json() {
        assert!(matches!(
            parse_kconfig("not json"),
            Err(KconfigError::Json(_))
        ));
    }

    fn core(os: &str) -> CoreEntry {
        CoreEntry {
            os: Some(os.to_string()),
            ..Default::default()
        }
    }

    #[test]
    fn resolve_default_core_picks_the_one_zephyr_core() {
        let mut cores = BTreeMap::new();
        cores.insert("m55_he".to_string(), core("zephyr"));
        cores.insert("a55_cluster".to_string(), core("yocto"));
        assert_eq!(
            resolve_default_kconfig_core(Some(&cores)),
            Ok("m55_he".to_string())
        );
    }

    #[test]
    fn resolve_default_core_is_ambiguous_with_two_zephyr_cores() {
        let mut cores = BTreeMap::new();
        cores.insert("m55_he".to_string(), core("zephyr"));
        cores.insert("m55_hp".to_string(), core("zephyr"));
        let err = resolve_default_kconfig_core(Some(&cores)).unwrap_err();
        assert_eq!(err, vec!["m55_he".to_string(), "m55_hp".to_string()]);
    }

    #[test]
    fn resolve_default_core_errs_with_no_zephyr_core() {
        let mut cores = BTreeMap::new();
        cores.insert("a55_cluster".to_string(), core("yocto"));
        let err = resolve_default_kconfig_core(Some(&cores)).unwrap_err();
        assert_eq!(err, vec!["a55_cluster".to_string()]);
    }

    #[test]
    fn resolve_default_core_errs_with_no_cores_declared() {
        assert_eq!(resolve_default_kconfig_core(None), Err(Vec::new()));
    }
}
