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

use serde::{Deserialize, Deserializer, Serialize};

use crate::model::CoreEntry;

/// `deserialize_with` for a field that must be a *present* JSON key but MAY
/// legally be `null` — e.g. `KconfigSymbol::default`.
///
/// A field merely typed `Option<T>` (no attribute at all) is special-cased by
/// serde's derive: a MISSING key silently deserializes to `None`, exactly
/// like `#[serde(default)]` would — dropping `#[serde(default)]` alone does
/// **not** make the key required (verified: `serde_json::from_str::<S>("{}")`
/// still succeeds for a bare `Option<T>` field). Routing the field through
/// this function opts it out of that implicit-default special case: with no
/// `#[serde(default)]` present, a missing key now hits serde's normal
/// missing-field error, while a present `null` still deserializes to `None`
/// (`Option::<T>::deserialize` on `null` is `Ok(None)`, same as an ordinary
/// `Option<T>` field).
fn deserialize_present_option<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

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
    ///
    /// Deliberately NOT `#[serde(default)]`: the SDK's `_project_symbols`
    /// (alp-sdk#894) sets this key unconditionally (`""` when there is no
    /// `depends on`), so a missing key is never legitimate input — it means
    /// the SDK renamed the field. `#[serde(default)]` would silently turn
    /// that rename into an empty string here instead of a deserialize error.
    pub depends: String,
    /// Resolved default expression, or `None` when the symbol has none.
    ///
    /// `Option<String>`, routed through `deserialize_present_option` so the
    /// KEY is required (a missing key is a rename, not "no default") while a
    /// present JSON `null` still deserializes to `None` — see that
    /// function's doc comment for why a bare `Option<String>` field alone
    /// does not achieve this (a missing key silently defaults to `None`
    /// regardless of `#[serde(default)]`).
    #[serde(deserialize_with = "deserialize_present_option")]
    pub default: Option<String>,
    /// Help text (empty string when there is none).
    ///
    /// See `depends`: NOT `#[serde(default)]` for the same reason — the SDK
    /// always emits this key (`""` when there is no help text).
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
    ///
    /// NOT `#[serde(default)]`: the SDK always emits this key (`[]` when a
    /// board/core has no promptable symbols) — see `depends`/`help` above
    /// for why a missing key must be a loud deserialize error, not a silent
    /// empty menu.
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

    // alp-sdk's canonical `--emit kconfig` contract anchor (alp-sdk#893/#894),
    // vendored byte-for-byte at `tests/fixtures/kconfig-contract/
    // emit-kconfig.golden.json` — the raw emit payload, not `tan`-wrapped.
    // `tests/parity/kconfig_fixture_parity.py` byte-diffs this vendored copy
    // against the pinned alp-sdk checkout's own copy, so an upstream rename
    // of any field here fails CI instead of silently drifting (see that
    // script's docstring). `tan-cli::commands::kconfig`'s envelope
    // round-trip test reads the SAME file independently — one vendored file
    // on disk, not two copies to keep in lockstep.
    const CANONICAL_EMIT_KCONFIG_FIXTURE: &str =
        include_str!("../../../tests/fixtures/kconfig-contract/emit-kconfig.golden.json");

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

    // The SDK always emits `depends`/`help`/`default`/`symbols` (see this
    // module's doc comment + `_project_symbols`); a missing key here means
    // the SDK renamed the field, and MUST fail loudly rather than silently
    // deserializing into an empty/`None` value (the bug these tests guard).

    #[test]
    fn parse_kconfig_rejects_symbol_missing_depends_key() {
        let bad = r#"{
            "schemaVersion": 1, "board": "b", "core": "c",
            "symbols": [
                { "name": "LOG", "type": "bool", "prompt": "Logging",
                  "default": "n", "help": "" }
            ]
        }"#;
        assert!(matches!(parse_kconfig(bad), Err(KconfigError::Json(_))));
    }

    #[test]
    fn parse_kconfig_rejects_symbol_missing_help_key() {
        let bad = r#"{
            "schemaVersion": 1, "board": "b", "core": "c",
            "symbols": [
                { "name": "LOG", "type": "bool", "prompt": "Logging",
                  "depends": "", "default": "n" }
            ]
        }"#;
        assert!(matches!(parse_kconfig(bad), Err(KconfigError::Json(_))));
    }

    #[test]
    fn parse_kconfig_rejects_symbol_missing_default_key() {
        // `default` stays `Option<String>` (a `null` value is legal — see
        // `parse_kconfig_accepts_null_default_but_requires_the_key` below)
        // but the KEY itself must be present.
        let bad = r#"{
            "schemaVersion": 1, "board": "b", "core": "c",
            "symbols": [
                { "name": "LOG", "type": "bool", "prompt": "Logging",
                  "depends": "", "help": "" }
            ]
        }"#;
        assert!(matches!(parse_kconfig(bad), Err(KconfigError::Json(_))));
    }

    #[test]
    fn parse_kconfig_rejects_missing_symbols_key() {
        let bad = r#"{ "schemaVersion": 1, "board": "b", "core": "c" }"#;
        assert!(matches!(parse_kconfig(bad), Err(KconfigError::Json(_))));
    }

    #[test]
    fn parse_kconfig_accepts_null_default_but_requires_the_key() {
        let ok = r#"{
            "schemaVersion": 1, "board": "b", "core": "c",
            "symbols": [
                { "name": "SOME_TRISTATE", "type": "tristate", "prompt": "t",
                  "depends": "", "default": null, "help": "" }
            ]
        }"#;
        let data = parse_kconfig(ok).unwrap();
        assert_eq!(data.symbols[0].default, None);
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

    /// alp-sdk's canonical `--emit kconfig` contract anchor (#893/#894)
    /// deserializes cleanly and every field lands, including the `null`
    /// default (`SOME_TRISTATE`) and the `tristate` type it hasn't otherwise
    /// been exercised for elsewhere in this file's tests.
    #[test]
    fn parse_kconfig_accepts_the_canonical_alp_sdk_fixture() {
        let data = parse_kconfig(CANONICAL_EMIT_KCONFIG_FIXTURE)
            .expect("the canonical alp-sdk fixture must deserialize");
        assert_eq!(data.schema_version, 1);
        assert_eq!(data.board, "alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he");
        assert_eq!(data.core, "m55_he");
        assert_eq!(data.symbols.len(), 5);

        let by_name = |name: &str| {
            data.symbols
                .iter()
                .find(|s| s.name == name)
                .unwrap_or_else(|| panic!("fixture is missing symbol {name}"))
        };

        let log = by_name("LOG");
        assert_eq!(log.r#type, "bool");
        assert_eq!(log.prompt, "Logging");
        assert_eq!(log.depends, "");
        assert_eq!(log.default.as_deref(), Some("n"));
        assert_eq!(log.help, "Enable the logging subsystem.");

        let stack = by_name("MAIN_STACK_SIZE");
        assert_eq!(stack.r#type, "int");
        assert_eq!(stack.depends, "MULTITHREADING");
        assert_eq!(stack.default.as_deref(), Some("1024"));

        let flash = by_name("FLASH_BASE_ADDRESS");
        assert_eq!(flash.r#type, "hex");
        assert_eq!(flash.default.as_deref(), Some("0x0"));

        let bt_name = by_name("BT_DEVICE_NAME");
        assert_eq!(bt_name.r#type, "string");
        assert_eq!(bt_name.default.as_deref(), Some("\"Zephyr\""));

        let tristate = by_name("SOME_TRISTATE");
        assert_eq!(tristate.r#type, "tristate");
        assert_eq!(tristate.depends, "");
        // The one legal-`null` case: the KEY is present, the VALUE is null.
        assert_eq!(tristate.default, None);
        assert_eq!(tristate.help, "");
    }
}
