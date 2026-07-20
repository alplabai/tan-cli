// SPDX-License-Identifier: Apache-2.0
//! Rust mirror of `packages/alp-core/src/sdkCatalogue/derive.ts`.

use std::collections::BTreeMap;

use super::types::{AcceleratorAvail, BoardPreset, ChipChoice, ChipDef, SdkCatalogue, SomPreset};

fn som_by_sku<'a>(catalogue: &'a SdkCatalogue, sku: &str) -> Option<&'a SomPreset> {
    catalogue.soms.iter().find(|s| s.sku == sku)
}

/// Boards in `catalogue` whose `hosts_som_families` include the SoM's family for `sku`.
pub fn boards_for_som(catalogue: &SdkCatalogue, sku: &str) -> Vec<BoardPreset> {
    let Some(som) = som_by_sku(catalogue, sku) else {
        return Vec::new();
    };

    catalogue
        .boards
        .iter()
        .filter(|b| b.hosts_som_families.iter().any(|f| f == &som.family))
        .cloned()
        .collect()
}

/// Topology core ids for the SoM identified by `sku`, or empty if unknown.
pub fn core_ids_for_som(catalogue: &SdkCatalogue, sku: &str) -> Vec<String> {
    som_by_sku(catalogue, sku)
        .map(|s| s.topology_core_ids.clone())
        .unwrap_or_default()
}

/// The board's default chip-population map (a clone of `populated`).
pub fn chip_defaults(board: &BoardPreset) -> BTreeMap<String, bool> {
    board.populated.clone()
}

/// Merge a board's default population with a per-board override map, override winning.
pub fn effective_populated(
    selected_preset: Option<&BoardPreset>,
    board_populated: Option<&BTreeMap<String, bool>>,
) -> BTreeMap<String, bool> {
    let mut out = selected_preset.map(chip_defaults).unwrap_or_default();
    if let Some(populated) = board_populated {
        for (key, value) in populated {
            out.insert(key.clone(), *value);
        }
    }
    out
}

/// Chips available to `sku`, each tagged with its enabled state after applying overlays.
pub fn effective_chip_choices(
    catalogue: &SdkCatalogue,
    sku: &str,
    selected_preset: Option<&BoardPreset>,
    board_populated: Option<&BTreeMap<String, bool>>,
) -> Vec<ChipChoice> {
    let effective = effective_populated(selected_preset, board_populated);
    chips_for_som(catalogue, sku)
        .into_iter()
        .map(|chip| ChipChoice {
            enabled: effective.get(&chip.chip_id).copied().unwrap_or(false),
            chip_id: chip.chip_id,
            display_name: chip.display_name,
            vendor: chip.vendor,
            bus: chip.bus,
            driver_status: chip.driver_status,
        })
        .collect()
}

/// Availability of the known accelerators for `som`, derived from its preferred
/// backend and `deepx_dxm1` capability; `cpu` is always available.
pub fn accelerator_availability(som: &SomPreset) -> Vec<AcceleratorAvail> {
    let preferred_backend = som.preferred_backend.as_deref();
    let has_deepx = som.capabilities.get("deepx_dxm1").copied().unwrap_or(false);
    vec![
        AcceleratorAvail {
            id: "ethos_u".to_string(),
            label: "Ethos-U".to_string(),
            available: preferred_backend == Some("ethos_u"),
        },
        AcceleratorAvail {
            id: "drpai".to_string(),
            label: "DRP-AI".to_string(),
            available: preferred_backend == Some("drpai"),
        },
        AcceleratorAvail {
            id: "deepx_dxm1".to_string(),
            label: "DeepX DX-M1".to_string(),
            available: has_deepx || preferred_backend == Some("deepx_dxm1"),
        },
        AcceleratorAvail {
            id: "cpu".to_string(),
            label: "CPU fallback".to_string(),
            available: true,
        },
    ]
}

/// Map a SoM SKU prefix to its chip family key, or `None` if unrecognized.
pub fn chip_family_for_sku(sku: &str) -> Option<&'static str> {
    if sku.starts_with("E1M-AEN") {
        return Some("aen");
    }
    if sku.starts_with("E1M-NX9") {
        return Some("imx93");
    }
    if sku.starts_with("E1M-V2M") {
        return Some("v2n-m1");
    }
    if sku.starts_with("E1M-V2N") {
        return Some("v2n");
    }
    None
}

/// Chips in `catalogue` whose `families` include the chip family resolved from `sku`.
pub fn chips_for_som(catalogue: &SdkCatalogue, sku: &str) -> Vec<ChipDef> {
    let Some(family) = chip_family_for_sku(sku) else {
        return Vec::new();
    };

    catalogue
        .chips
        .iter()
        .filter(|chip| chip.families.iter().any(|f| f == family))
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_catalogue() -> SdkCatalogue {
        SdkCatalogue {
            soms: vec![
                SomPreset {
                    sku: "E1M-AEN701".to_string(),
                    display_name: "E1M-AEN701".to_string(),
                    family: "aen".to_string(),
                    silicon: "alif-e7".to_string(),
                    silicon_variant: None,
                    preferred_backend: Some("ethos_u".to_string()),
                    capabilities: BTreeMap::from([("deepx_dxm1".to_string(), true)]),
                    default_board: None,
                    topology_core_ids: vec!["m55_hp".to_string()],
                    topology: vec![],
                    on_module: vec![],
                    memory: None,
                    preliminary: false,
                    pad_routes: vec![],
                    i2c_devices: vec![],
                },
                SomPreset {
                    sku: "E1M-OTHER".to_string(),
                    display_name: "E1M-OTHER".to_string(),
                    family: "other".to_string(),
                    silicon: "x".to_string(),
                    silicon_variant: None,
                    preferred_backend: None,
                    capabilities: BTreeMap::new(),
                    default_board: None,
                    topology_core_ids: vec![],
                    topology: vec![],
                    on_module: vec![],
                    memory: None,
                    preliminary: false,
                    pad_routes: vec![],
                    i2c_devices: vec![],
                },
            ],
            boards: vec![BoardPreset {
                name: "e1m-evk".to_string(),
                display_name: "E1M EVK".to_string(),
                hosts_som_families: vec!["aen".to_string()],
                populated: BTreeMap::from([("chip-a".to_string(), true)]),
            }],
            chips: vec![
                ChipDef {
                    chip_id: "chip-a".to_string(),
                    display_name: "Chip A".to_string(),
                    families: vec!["aen".to_string()],
                    vendor: None,
                    bus: None,
                    driver_status: None,
                    kconfig: None,
                },
                ChipDef {
                    chip_id: "chip-b".to_string(),
                    display_name: "Chip B".to_string(),
                    families: vec!["v2n".to_string()],
                    vendor: None,
                    bus: None,
                    driver_status: None,
                    kconfig: None,
                },
            ],
            socs: vec![],
            sdk_version: None,
        }
    }

    #[test]
    fn derives_board_and_core_helpers() {
        let c = fixture_catalogue();
        assert_eq!(boards_for_som(&c, "E1M-AEN701").len(), 1);
        assert_eq!(
            core_ids_for_som(&c, "E1M-AEN701"),
            vec!["m55_hp".to_string()]
        );
        assert!(boards_for_som(&c, "UNKNOWN").is_empty());
    }

    #[test]
    fn derives_accelerator_and_chip_helpers() {
        let c = fixture_catalogue();
        let som = &c.soms[0];
        let avail = accelerator_availability(som);
        assert!(avail.iter().any(|a| a.id == "ethos_u" && a.available));
        assert!(avail.iter().any(|a| a.id == "deepx_dxm1" && a.available));
        assert_eq!(chip_family_for_sku("E1M-AEN701"), Some("aen"));
        assert_eq!(chips_for_som(&c, "E1M-AEN701").len(), 1);
    }

    #[test]
    fn computes_effective_chip_overlays() {
        let c = fixture_catalogue();
        let board = &c.boards[0];
        let overlay = BTreeMap::from([("chip-a".to_string(), false)]);
        let choices = effective_chip_choices(&c, "E1M-AEN701", Some(board), Some(&overlay));
        assert_eq!(choices.len(), 1);
        assert!(!choices[0].enabled);

        let merged = effective_populated(Some(board), Some(&overlay));
        assert_eq!(merged.get("chip-a"), Some(&false));
    }
}
