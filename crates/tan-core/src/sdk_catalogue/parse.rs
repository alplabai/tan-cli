// SPDX-License-Identifier: Apache-2.0
//! Rust mirror of `packages/alp-core/src/sdkCatalogue/parse.ts`.

use serde_json::Value as JsonValue;
use serde_yaml::{Mapping, Value as YamlValue};
use std::collections::BTreeMap;

use super::types::{
    BoardPreset, ChipDef, ChipKconfig, I2cDevice, MemorySpec, PadRoute, SocCore, SocSpec,
    SomPreset, TopologyCore,
};

fn yget<'a>(map: &'a Mapping, key: &str) -> Option<&'a YamlValue> {
    map.get(YamlValue::String(key.to_string()))
}

fn is_tbd(value: &str) -> bool {
    value.trim() == "TBD"
}

fn str_clean(value: Option<&YamlValue>) -> Option<String> {
    value
        .and_then(YamlValue::as_str)
        .map(str::to_string)
        .filter(|s| !is_tbd(s))
}

fn num_u32(value: Option<&YamlValue>) -> Option<u32> {
    value
        .and_then(YamlValue::as_i64)
        .and_then(|n| u32::try_from(n).ok())
}

fn bool_map(value: Option<&YamlValue>) -> BTreeMap<String, bool> {
    let mut out = BTreeMap::new();
    let Some(map) = value.and_then(YamlValue::as_mapping) else {
        return out;
    };

    for (k, v) in map {
        if let Some(key) = k.as_str() {
            out.insert(key.to_string(), v.as_bool().unwrap_or(false));
        }
    }
    out
}

fn str_list(value: Option<&YamlValue>) -> Vec<String> {
    value
        .and_then(YamlValue::as_sequence)
        .map(|seq| {
            seq.iter()
                .filter_map(YamlValue::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Parse a single board definition (YAML) into a `BoardPreset`, dropping `TBD` strings.
pub fn parse_board_preset(text: &str) -> Result<BoardPreset, serde_yaml::Error> {
    let root: YamlValue = serde_yaml::from_str(text)?;
    let map = root.as_mapping().cloned().unwrap_or_default();
    let name = str_clean(yget(&map, "name")).unwrap_or_default();
    let display_name = str_clean(yget(&map, "display_name")).unwrap_or_else(|| name.clone());

    Ok(BoardPreset {
        name,
        display_name,
        hosts_som_families: str_list(yget(&map, "hosts_som_families")),
        populated: bool_map(yget(&map, "populated")),
    })
}

/// Parse a single chip definition (YAML) into a `ChipDef`; `kconfig` stays `None` when empty.
pub fn parse_chip_def(text: &str) -> Result<ChipDef, serde_yaml::Error> {
    let root: YamlValue = serde_yaml::from_str(text)?;
    let map = root.as_mapping().cloned().unwrap_or_default();
    let chip_id = str_clean(yget(&map, "chip_id")).unwrap_or_default();
    let display_name = str_clean(yget(&map, "display_name")).unwrap_or_else(|| chip_id.clone());

    let kconfig = yget(&map, "kconfig")
        .and_then(YamlValue::as_mapping)
        .map(|kc| ChipKconfig {
            zephyr: str_clean(yget(kc, "zephyr")),
            baremetal: str_clean(yget(kc, "baremetal")),
        })
        .and_then(|k| {
            if k.zephyr.is_some() || k.baremetal.is_some() {
                Some(k)
            } else {
                None
            }
        });

    Ok(ChipDef {
        chip_id,
        display_name,
        vendor: str_clean(yget(&map, "vendor")),
        bus: str_clean(yget(&map, "bus")),
        driver_status: str_clean(yget(&map, "driver_status")),
        families: str_list(yget(&map, "families")),
        kconfig,
    })
}

/// Parse a single SoC specification (JSON) into a `SocSpec`.
pub fn parse_soc_spec(text: &str) -> Result<SocSpec, serde_json::Error> {
    let root: JsonValue = serde_json::from_str(text)?;
    let cores = root
        .get("cores")
        .and_then(JsonValue::as_array)
        .map(|arr| {
            arr.iter()
                .map(|c| SocCore {
                    id: c
                        .get("id")
                        .and_then(JsonValue::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    r#type: c
                        .get("type")
                        .and_then(JsonValue::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    count: c
                        .get("count")
                        .and_then(JsonValue::as_u64)
                        .and_then(|n| u32::try_from(n).ok())
                        .unwrap_or(1),
                    freq_mhz: c
                        .get("freq_mhz")
                        .and_then(JsonValue::as_u64)
                        .and_then(|n| u32::try_from(n).ok()),
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(SocSpec {
        ref_id: root
            .get("ref")
            .and_then(JsonValue::as_str)
            .unwrap_or_default()
            .to_string(),
        vendor: root
            .get("vendor")
            .and_then(JsonValue::as_str)
            .unwrap_or_default()
            .to_string(),
        family: root
            .get("family")
            .and_then(JsonValue::as_str)
            .unwrap_or_default()
            .to_string(),
        part: root
            .get("part")
            .and_then(JsonValue::as_str)
            .unwrap_or_default()
            .to_string(),
        cores,
    })
}

/// Parse a single SoM definition (YAML) into a `SomPreset`, flattening topology,
/// memory, pad routes, and `on_module.i2c_devices`.
pub fn parse_som_preset(text: &str) -> Result<SomPreset, serde_yaml::Error> {
    let root: YamlValue = serde_yaml::from_str(text)?;
    let map = root.as_mapping().cloned().unwrap_or_default();

    let inference_map = yget(&map, "inference").and_then(YamlValue::as_mapping);
    let topology_map = yget(&map, "topology").and_then(YamlValue::as_mapping);
    let on_module_map = yget(&map, "on_module").and_then(YamlValue::as_mapping);
    let memory_map = yget(&map, "memory").and_then(YamlValue::as_mapping);
    let status_map = yget(&map, "status").and_then(YamlValue::as_mapping);

    let dram_mbit = num_u32(memory_map.and_then(|m| yget(m, "dram_mbit")));
    let flash_mbit = num_u32(memory_map.and_then(|m| yget(m, "flash_mbit")));
    let memory = if dram_mbit.is_some() || flash_mbit.is_some() {
        Some(MemorySpec {
            dram_mbit,
            flash_mbit,
        })
    } else {
        None
    };

    let pad_routes = yget(&map, "pad_routes")
        .and_then(YamlValue::as_sequence)
        .map(|routes| {
            routes
                .iter()
                .filter_map(YamlValue::as_mapping)
                .filter_map(|route| {
                    let e1m = str_clean(yget(route, "e1m"))?;
                    Some(PadRoute {
                        e1m,
                        dispatch: str_clean(yget(route, "dispatch")).unwrap_or_default(),
                        dispatch_pin: str_clean(yget(route, "dispatch_pin")),
                        doc: str_clean(yget(route, "doc")),
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let mut i2c_devices = Vec::new();
    if let Some(i2c_map) = on_module_map
        .and_then(|m| yget(m, "i2c_devices"))
        .and_then(YamlValue::as_mapping)
    {
        for (bus_key, bus_def) in i2c_map {
            let Some(bus) = bus_key.as_str() else {
                continue;
            };
            let devices = bus_def
                .as_mapping()
                .and_then(|d| yget(d, "devices"))
                .and_then(YamlValue::as_sequence)
                .cloned()
                .unwrap_or_default();
            for dev in devices {
                let Some(dev_map) = dev.as_mapping() else {
                    continue;
                };
                let Some(chip) = str_clean(yget(dev_map, "chip")) else {
                    continue;
                };
                i2c_devices.push(I2cDevice {
                    bus: bus.to_string(),
                    chip,
                    role: str_clean(yget(dev_map, "role")),
                    address: str_clean(yget(dev_map, "address_7bit")),
                });
            }
        }
    }

    let topology = topology_map
        .map(|topology| {
            topology
                .iter()
                .filter_map(|(id, node)| {
                    let id = id.as_str()?.to_string();
                    let node_map = node.as_mapping();
                    Some(TopologyCore {
                        id,
                        app: node_map.and_then(|m| str_clean(yget(m, "app"))),
                        image: node_map.and_then(|m| str_clean(yget(m, "image"))),
                        machine: node_map.and_then(|m| str_clean(yget(m, "machine"))),
                        board: node_map.and_then(|m| str_clean(yget(m, "board"))),
                        toolchain: node_map.and_then(|m| str_clean(yget(m, "toolchain"))),
                    })
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    let topology_core_ids = topology_map
        .map(|topology| {
            topology
                .keys()
                .filter_map(YamlValue::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();

    let on_module = on_module_map
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| {
                    let key = k.as_str()?;
                    if key == "silicon" {
                        return None;
                    }
                    str_clean(Some(v))
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(SomPreset {
        sku: str_clean(yget(&map, "sku")).unwrap_or_default(),
        display_name: str_clean(yget(&map, "display_name"))
            .or_else(|| str_clean(yget(&map, "sku")))
            .unwrap_or_default(),
        family: str_clean(yget(&map, "family")).unwrap_or_default(),
        silicon: str_clean(yget(&map, "silicon")).unwrap_or_default(),
        silicon_variant: str_clean(yget(&map, "silicon_variant")),
        preferred_backend: inference_map.and_then(|m| str_clean(yget(m, "preferred_backend"))),
        capabilities: bool_map(yget(&map, "capabilities")),
        default_board: str_clean(yget(&map, "default_board")),
        topology_core_ids,
        topology,
        on_module,
        memory,
        preliminary: status_map
            .and_then(|m| yget(m, "preliminary"))
            .and_then(YamlValue::as_bool)
            .unwrap_or(false),
        pad_routes,
        i2c_devices,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_board_chip_som_and_soc() {
        let board = parse_board_preset(
            "name: e1m-evk\ndisplay_name: E1M EVK\nhosts_som_families: [aen]\npopulated: { chip-a: true }\n",
        )
        .unwrap();
        assert_eq!(board.name, "e1m-evk");
        assert_eq!(board.display_name, "E1M EVK");

        let chip = parse_chip_def(
            "chip_id: chip-a\ndisplay_name: Chip A\nvendor: TBD\nfamilies: [aen]\nkconfig:\n  zephyr: CONFIG_CHIP_A\n",
        )
        .unwrap();
        assert_eq!(chip.vendor, None);
        assert_eq!(
            chip.kconfig.as_ref().and_then(|k| k.zephyr.clone()),
            Some("CONFIG_CHIP_A".to_string())
        );

        let som = parse_som_preset(
            "sku: E1M-AEN701\ndisplay_name: E1M AEN701\nfamily: aen\nsilicon: alif-e7\ninference:\n  preferred_backend: ethos_u\ncapabilities: { deepx_dx: true }\ntopology:\n  m55_hp: { app: ./src }\non_module:\n  i2c_devices:\n    i2c0:\n      devices:\n        - chip: ina236\n          role: sensor\n          address_7bit: '0x40'\nstatus:\n  preliminary: true\n",
        )
        .unwrap();
        assert_eq!(som.topology_core_ids, vec!["m55_hp".to_string()]);
        assert_eq!(som.i2c_devices.len(), 1);
        assert!(som.preliminary);

        let soc = parse_soc_spec("{\"ref\":\"soc-ref\",\"vendor\":\"v\",\"family\":\"f\",\"part\":\"p\",\"cores\":[{\"id\":\"m55_hp\",\"type\":\"m55\",\"count\":2,\"freq_mhz\":400}]}").unwrap();
        assert_eq!(soc.ref_id, "soc-ref");
        assert_eq!(soc.cores[0].count, 2);
    }
}
