// SPDX-License-Identifier: Apache-2.0
//! SDK-catalogue domain types (SoM/board/chip/SoC presets and derived views).

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A System-on-Module (SoM) preset derived from an SDK `som` definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SomPreset {
    /// Module SKU, e.g. `E1M-AEN701`.
    pub sku: String,
    /// Human-readable module name (falls back to `sku` when absent).
    pub display_name: String,
    /// SoM family key used to match boards and chips, e.g. `aen`.
    pub family: String,
    /// Silicon identifier, e.g. `alif-e7`.
    pub silicon: String,
    /// Optional silicon variant suffix.
    #[serde(default)]
    pub silicon_variant: Option<String>,
    /// Preferred inference backend id (from `inference.preferred_backend`).
    #[serde(default)]
    pub preferred_backend: Option<String>,
    /// Capability flags keyed by name (e.g. `deepx_dxm1`).
    #[serde(default)]
    pub capabilities: BTreeMap<String, bool>,
    /// Default board `name` to preselect, if any.
    #[serde(default)]
    pub default_board: Option<String>,
    /// Core ids declared under `topology`, in map order.
    #[serde(default)]
    pub topology_core_ids: Vec<String>,
    /// Per-core topology entries.
    #[serde(default)]
    pub topology: Vec<TopologyCore>,
    /// On-module string entries (excludes the `silicon` key).
    #[serde(default)]
    pub on_module: Vec<String>,
    /// DRAM/flash sizing, when declared under `memory`.
    #[serde(default)]
    pub memory: Option<MemorySpec>,
    /// Whether the SoM is marked preliminary (from `status.preliminary`).
    #[serde(default)]
    pub preliminary: bool,
    /// Pin/pad routing entries from `pad_routes`.
    #[serde(default)]
    pub pad_routes: Vec<PadRoute>,
    /// I2C devices flattened from `on_module.i2c_devices`.
    #[serde(default)]
    pub i2c_devices: Vec<I2cDevice>,
}

/// A single pad-routing entry mapping an E1M pad to a dispatch target.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PadRoute {
    /// E1M pad name (required; entries without it are dropped on parse).
    pub e1m: String,
    /// Dispatch destination identifier.
    pub dispatch: String,
    /// Optional specific dispatch pin.
    #[serde(default)]
    pub dispatch_pin: Option<String>,
    /// Optional free-form documentation note.
    #[serde(default)]
    pub doc: Option<String>,
}

/// An on-module I2C device, keyed by the bus it sits on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct I2cDevice {
    /// I2C bus key, e.g. `i2c0`.
    pub bus: String,
    /// Chip identifier (required; entries without it are skipped on parse).
    pub chip: String,
    /// Optional role label (e.g. `sensor`).
    #[serde(default)]
    pub role: Option<String>,
    /// Optional 7-bit address (from `address_7bit`).
    #[serde(default)]
    pub address: Option<String>,
}

/// One core entry within a SoM's `topology` map.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TopologyCore {
    /// Core id (the topology map key), e.g. `m55_hp`.
    pub id: String,
    /// Optional application source path for this core.
    #[serde(default)]
    pub app: Option<String>,
    /// Optional image name/target.
    #[serde(default)]
    pub image: Option<String>,
    /// Optional machine identifier.
    #[serde(default)]
    pub machine: Option<String>,
    /// Optional board override for this core.
    #[serde(default)]
    pub board: Option<String>,
    /// Optional toolchain override for this core.
    #[serde(default)]
    pub toolchain: Option<String>,
}

/// DRAM/flash sizing for a SoM, in megabits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemorySpec {
    /// DRAM size in megabits.
    #[serde(default)]
    pub dram_mbit: Option<u32>,
    /// Flash size in megabits.
    #[serde(default)]
    pub flash_mbit: Option<u32>,
}

/// A board preset derived from an SDK `board` definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoardPreset {
    /// Board `name` slug, e.g. `e1m-evk`.
    pub name: String,
    /// Human-readable board name (falls back to `name`).
    pub display_name: String,
    /// SoM family keys this board can host.
    #[serde(default)]
    pub hosts_som_families: Vec<String>,
    /// Default chip-population flags keyed by chip id.
    #[serde(default)]
    pub populated: BTreeMap<String, bool>,
}

/// A chip definition derived from an SDK `chip` definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChipDef {
    /// Chip identifier slug.
    pub chip_id: String,
    /// Human-readable chip name (falls back to `chip_id`).
    pub display_name: String,
    /// SoM families this chip applies to.
    #[serde(default)]
    pub families: Vec<String>,
    /// Optional vendor name.
    #[serde(default)]
    pub vendor: Option<String>,
    /// Optional bus the chip sits on.
    #[serde(default)]
    pub bus: Option<String>,
    /// Optional driver maturity status.
    #[serde(default)]
    pub driver_status: Option<String>,
    /// Optional Kconfig symbol mapping, present only when non-empty.
    #[serde(default)]
    pub kconfig: Option<ChipKconfig>,
}

/// Per-target Kconfig symbol names that enable a chip's driver.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChipKconfig {
    /// Zephyr Kconfig symbol, e.g. `CONFIG_CHIP_A`.
    #[serde(default)]
    pub zephyr: Option<String>,
    /// Baremetal Kconfig symbol.
    #[serde(default)]
    pub baremetal: Option<String>,
}

/// One core group within a SoC specification.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SocCore {
    /// Core id, e.g. `m55_hp`.
    pub id: String,
    /// Core type, e.g. `m55` (serialized as `type`).
    pub r#type: String,
    /// Number of cores in this group (defaults to 1 on parse).
    pub count: u32,
    /// Optional clock frequency in MHz.
    #[serde(default)]
    pub freq_mhz: Option<u32>,
}

/// A SoC specification parsed from its JSON definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SocSpec {
    /// SoC reference id (from the JSON `ref` field).
    pub ref_id: String,
    /// Vendor name.
    pub vendor: String,
    /// SoC family.
    pub family: String,
    /// Part number.
    pub part: String,
    /// Core groups making up the SoC.
    #[serde(default)]
    pub cores: Vec<SocCore>,
}

/// The aggregated SDK catalogue of SoMs, boards, chips, and SoCs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SdkCatalogue {
    /// All SoM presets.
    #[serde(default)]
    pub soms: Vec<SomPreset>,
    /// All board presets.
    #[serde(default)]
    pub boards: Vec<BoardPreset>,
    /// All chip definitions.
    #[serde(default)]
    pub chips: Vec<ChipDef>,
    /// All SoC specifications.
    #[serde(default)]
    pub socs: Vec<SocSpec>,
    /// Optional SDK version stamp.
    #[serde(default)]
    pub sdk_version: Option<String>,
}

/// Availability of a named inference accelerator for a given SoM.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcceleratorAvail {
    /// Accelerator id, e.g. `ethos_u`, `drpai`, `deepx_dxm1`, `cpu`.
    pub id: String,
    /// Human-readable accelerator label.
    pub label: String,
    /// Whether this accelerator is available on the SoM.
    pub available: bool,
}

/// A chip available to a SoM together with its effective enabled state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChipChoice {
    /// Chip identifier slug.
    pub chip_id: String,
    /// Human-readable chip name.
    pub display_name: String,
    /// Optional vendor name.
    #[serde(default)]
    pub vendor: Option<String>,
    /// Optional bus the chip sits on.
    #[serde(default)]
    pub bus: Option<String>,
    /// Optional driver maturity status.
    #[serde(default)]
    pub driver_status: Option<String>,
    /// Whether the chip is populated/enabled after applying overlays.
    pub enabled: bool,
}
