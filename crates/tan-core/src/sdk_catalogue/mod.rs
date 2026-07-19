// SPDX-License-Identifier: Apache-2.0
//! Rust mirror of `packages/alp-core/src/sdkCatalogue/{parse,derive}.ts`.

mod derive;
mod parse;
mod types;

pub use derive::{
    accelerator_availability, boards_for_som, chip_defaults, chip_family_for_sku, chips_for_som,
    core_ids_for_som, effective_chip_choices, effective_populated,
};
pub use parse::{parse_board_preset, parse_chip_def, parse_soc_spec, parse_som_preset};
pub use types::{
    AcceleratorAvail, BoardPreset, ChipChoice, ChipDef, ChipKconfig, I2cDevice, MemorySpec,
    PadRoute, SdkCatalogue, SocCore, SocSpec, SomPreset, TopologyCore,
};
