// SPDX-License-Identifier: Apache-2.0
//! Pinmux capability table (`metadata/pinmux/<family>.yaml`, schema
//! `pinmux-capability-v1`): each E1M edge pad and the silicon function backing
//! it. Rust twin of the extension's `@alp-sdk/core/pinmux/{models,parse}` — the
//! serialized field names match `PinmuxPad`/`PinmuxTable` there so the CLI
//! envelope is consumed without a second parser.

/// One row of a pinmux capability table: an E1M edge pad and the silicon
/// function backing it. `silicon_peripheral` is empty for plain GPIO.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct PinmuxPad {
    #[serde(rename = "e1mPad")]
    pub e1m_pad: String,
    #[serde(rename = "e1mFunction")]
    pub e1m_function: String,
    pub owner: String,
    #[serde(rename = "siliconPeripheral")]
    pub silicon_peripheral: String,
    #[serde(rename = "siliconPad")]
    pub silicon_pad: String,
}

/// A parsed `metadata/pinmux/<family>.yaml` capability table.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct PinmuxTable {
    pub family: String,
    #[serde(rename = "displayName", skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    pub pads: Vec<PinmuxPad>,
}

/// The only `schemaVersion` this parser accepts. `metadata/pinmux/<family>.yaml`
/// is gated to this by `pinmux-capability-v1.schema.json`.
const SCHEMA_VERSION: &str = "pinmux-capability-v1";

#[derive(serde::Deserialize)]
struct RawTable {
    #[serde(rename = "schemaVersion")]
    schema_version: Option<String>,
    family: Option<String>,
    display_name: Option<String>,
    pads: Option<Vec<RawPad>>,
}

#[derive(serde::Deserialize)]
struct RawPad {
    e1m_pad: Option<String>,
    e1m_function: Option<String>,
    owner: Option<String>,
    silicon_peripheral: Option<String>,
    silicon_pad: Option<String>,
}

/// Parse a `pinmux-capability-v1` YAML document into a `PinmuxTable`. Fail-soft
/// (never errors): a malformed document yields an empty table; a pad missing
/// `e1m_pad` or `e1m_function` is dropped; other string fields default to "".
/// Mirrors the extension's `parsePinmuxTable`.
///
/// This swallows a genuine YAML syntax error the same way it swallows an
/// ordinary "some pads dropped" table, which is right for a caller with no
/// error channel -- but it also means a caller that DOES have one (the CLI
/// envelope's `issues[]`) can't tell "no pads" from "table failed to parse".
/// Use [`parse_pinmux_table_checked`] to keep that distinction.
pub fn parse_pinmux_table(text: &str) -> PinmuxTable {
    parse_pinmux_table_checked(text).unwrap_or_else(|_| PinmuxTable {
        family: String::new(),
        display_name: None,
        pads: Vec::new(),
    })
}

/// Parse a `pinmux-capability-v1` YAML document, surfacing a genuine YAML
/// syntax error instead of swallowing it into an empty table. `pads` rows
/// still fail soft per-row (a row missing `e1m_pad`/`e1m_function`, or
/// carrying the `"TBD"` sentinel `e1m_pad`, is dropped, not an `Err`) -- only
/// "the document itself did not parse" or "`schemaVersion` is not
/// `pinmux-capability-v1`" is `Err` here. The latter matters because
/// `pinmux-capability-v1.schema.json` requires `minItems: 1` on `pads`: a real
/// v1 document is never legitimately empty, so a differently-shaped
/// `schemaVersion` (a future v2) must not be allowed to silently parse into a
/// zero-pad table indistinguishable from success.
pub fn parse_pinmux_table_checked(text: &str) -> Result<PinmuxTable, serde_yaml::Error> {
    let raw: RawTable = serde_yaml::from_str(text)?;
    if raw.schema_version.as_deref() != Some(SCHEMA_VERSION) {
        use serde::de::Error as _;
        return Err(serde_yaml::Error::custom(format!(
            "unsupported pinmux capability schemaVersion {:?} (expected {SCHEMA_VERSION:?})",
            raw.schema_version
        )));
    }
    let pads = raw
        .pads
        .unwrap_or_default()
        .into_iter()
        .filter_map(|p| {
            let e1m_pad = p.e1m_pad?;
            if e1m_pad == "TBD" {
                // Sentinel row: the source TSV doesn't carry an E1M edge ball
                // for this silicon pad. Mirrors the extension's loader, which
                // filters these out rather than surfacing "TBD" as a pad name.
                return None;
            }
            Some(PinmuxPad {
                e1m_pad,
                e1m_function: p.e1m_function?,
                owner: p.owner.unwrap_or_default(),
                silicon_peripheral: p.silicon_peripheral.unwrap_or_default(),
                silicon_pad: p.silicon_pad.unwrap_or_default(),
            })
        })
        .collect();
    Ok(PinmuxTable {
        family: raw.family.unwrap_or_default(),
        display_name: raw.display_name,
        pads,
    })
}

/// Resolve a SoM SKU to its pinmux family stem (the `metadata/pinmux/<stem>.yaml`
/// basename). Mirrors the extension's `pinmuxFamilyForSku` prefix table; returns
/// `None` for an unrecognized SKU. E1M-V2M reuses the base V2N pinout in full
/// (see `metadata/e1m_modules/v2n-m1/README.md`) -- there is no separate
/// `v2n-m1.yaml` table, so it maps to `"v2n"` too.
pub fn pinmux_family_for_sku(sku: &str) -> Option<&'static str> {
    const TABLE: &[(&str, &str)] = &[
        ("E1M-AEN", "aen"),
        ("E1M-NX9", "imx93"),
        ("E1M-V2N", "v2n"),
        ("E1M-V2M", "v2n"),
    ];
    TABLE
        .iter()
        .find(|(prefix, _)| sku.starts_with(prefix))
        .map(|(_, stem)| *stem)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
schemaVersion: pinmux-capability-v1
family: aen
display_name: \"E1M-AEN (Alif Ensemble)\"
pads:
  - { e1m_pad: \"A3\", e1m_function: \"PWM6\", owner: \"alif\", silicon_peripheral: \"UT3_T1_C\", silicon_pad: \"P10_7\" }
  - { e1m_pad: \"A15\", e1m_function: \"ANA_S0\", owner: \"alif\", silicon_peripheral: \"\", silicon_pad: \"P0_0\" }
";

    #[test]
    fn parses_family_display_and_pads() {
        let t = parse_pinmux_table(SAMPLE);
        assert_eq!(t.family, "aen");
        assert_eq!(t.display_name.as_deref(), Some("E1M-AEN (Alif Ensemble)"));
        assert_eq!(t.pads.len(), 2);
        assert_eq!(t.pads[0].e1m_pad, "A3");
        assert_eq!(t.pads[0].e1m_function, "PWM6");
        assert_eq!(t.pads[0].silicon_pad, "P10_7");
        assert_eq!(t.pads[1].silicon_peripheral, ""); // plain GPIO
    }

    #[test]
    fn drops_pads_missing_required_keys_and_defaults_strings() {
        let t = parse_pinmux_table(
            "schemaVersion: pinmux-capability-v1\nfamily: aen\npads:\n  - { e1m_pad: \"A3\" }\n  - { e1m_pad: \"A4\", e1m_function: \"PWM4\" }\n",
        );
        // first pad has no e1m_function -> dropped; second keeps, owner defaults to "".
        assert_eq!(t.pads.len(), 1);
        assert_eq!(t.pads[0].e1m_function, "PWM4");
        assert_eq!(t.pads[0].owner, "");
    }

    #[test]
    fn fail_soft_on_malformed_yaml() {
        let t = parse_pinmux_table(": : not yaml : :");
        assert_eq!(t.family, "");
        assert!(t.pads.is_empty());
    }

    #[test]
    fn checked_variant_surfaces_the_parse_error_fail_soft_hides() {
        // `parse_pinmux_table` collapses "genuinely malformed YAML" and "valid
        // YAML with a few dropped rows" into the identical empty-table result,
        // so a caller with an issues[] channel (tan pinmux --format json)
        // can't warn on one and not the other. The checked variant must Err.
        assert!(parse_pinmux_table_checked(": : not yaml : :").is_err());
        assert!(parse_pinmux_table_checked(SAMPLE).is_ok());
    }

    #[test]
    fn rejects_non_v1_schema_version_as_typed_skew_not_empty_table() {
        // A `pinmux-capability-v2` (or missing-version) doc must not parse as
        // a silently-successful zero-pad table: `pads` is `minItems: 1` in the
        // v1 schema, so an empty table is never a legitimate v1 document.
        let v2 = "schemaVersion: pinmux-capability-v2\nfamily: aen\npads: []\n";
        assert!(parse_pinmux_table_checked(v2).is_err());
        let no_version = "family: aen\npads:\n  - { e1m_pad: \"A3\", e1m_function: \"PWM6\", owner: \"alif\", silicon_peripheral: \"\", silicon_pad: \"P0_0\" }\n";
        assert!(parse_pinmux_table_checked(no_version).is_err());
    }

    #[test]
    fn drops_tbd_sentinel_pads() {
        let t = parse_pinmux_table(
            "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n  - { e1m_pad: \"TBD\", e1m_function: \"TBD\", owner: \"renesas\", silicon_peripheral: \"BL_PWM\", silicon_pad: \"PA5\" }\n  - { e1m_pad: \"A3\", e1m_function: \"PWM6\", owner: \"alif\", silicon_peripheral: \"\", silicon_pad: \"P0_0\" }\n",
        );
        assert_eq!(t.pads.len(), 1);
        assert_eq!(t.pads[0].e1m_pad, "A3");
    }

    #[test]
    fn sku_to_family_prefix_map() {
        assert_eq!(pinmux_family_for_sku("E1M-AEN701"), Some("aen"));
        assert_eq!(pinmux_family_for_sku("E1M-V2N44"), Some("v2n"));
        // E1M-V2M reuses the base V2N pinout in full; no separate table.
        assert_eq!(pinmux_family_for_sku("E1M-V2M01"), Some("v2n"));
        assert_eq!(pinmux_family_for_sku("E1M-NX93"), Some("imx93"));
        assert_eq!(pinmux_family_for_sku("E1M-UNKNOWN"), None);
    }
}
