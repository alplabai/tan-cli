// SPDX-License-Identifier: Apache-2.0
//! Backend registry + the plan-builder contract types. `backend_for` resolves a
//! `flash_method` string to its tool-gate `requires` list + `BackendKind`;
//! `FlashInputs`/`FlashPlan` are the shared input/output shape every per-backend
//! builder consumes and produces.

use std::path::Path;

use serde_yaml::Value;

/// Which backend implementation a `flash_method` resolves to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendKind {
    /// `swd_probe` — J-Link / OpenOCD / pyOCD external probe.
    Swd,
    /// `zephyr_west_flash` — `west flash`.
    Zephyr,
    /// `baremetal_cmake_flash` — `cmake --build --target`.
    Cmake,
    /// `cc3501e_usb_bootloader` — CC3501E USB-CDC bootloader (shape-complete).
    Cc3501e,
    /// `yocto_wic_to_sd_or_emmc` / `yocto_wic` — bmaptool / dd to a block device.
    YoctoWic,
    /// `xspi_flashwriter` — Renesas Flash Writer over SCIF (HW-gated).
    Xspi,
}

/// A registered backend: the tool-gate `requires` list + which builder to call.
#[derive(Debug, Clone, Copy)]
pub struct FlashBackendMeta {
    /// Executables the gate checks (at least one must be on PATH).
    pub requires: &'static [&'static str],
    /// The builder to dispatch to.
    pub kind: BackendKind,
}

/// Resolve a `flash_method` string to its backend metadata, or `None`.
pub fn backend_for(method: &str) -> Option<FlashBackendMeta> {
    let meta = match method {
        "swd_probe" => FlashBackendMeta {
            requires: &["JLinkExe", "JLink", "openocd", "pyocd"],
            kind: BackendKind::Swd,
        },
        "zephyr_west_flash" => FlashBackendMeta {
            requires: &["west"],
            kind: BackendKind::Zephyr,
        },
        "baremetal_cmake_flash" => FlashBackendMeta {
            requires: &["cmake"],
            kind: BackendKind::Cmake,
        },
        "cc3501e_usb_bootloader" => FlashBackendMeta {
            requires: &["cc3501e-flasher", "cc3501e-tool"],
            kind: BackendKind::Cc3501e,
        },
        "yocto_wic_to_sd_or_emmc" | "yocto_wic" => FlashBackendMeta {
            requires: &["bmaptool", "dd"],
            kind: BackendKind::YoctoWic,
        },
        "xspi_flashwriter" => FlashBackendMeta {
            requires: &[],
            kind: BackendKind::Xspi,
        },
        _ => return None,
    };
    Some(meta)
}

/// The registered method names, sorted — for the "Available: …" error when a
/// `flash_method` has no backend.
pub fn registry_keys() -> Vec<&'static str> {
    let mut keys = vec![
        "baremetal_cmake_flash",
        "cc3501e_usb_bootloader",
        "swd_probe",
        "xspi_flashwriter",
        "yocto_wic",
        "yocto_wic_to_sd_or_emmc",
        "zephyr_west_flash",
    ];
    keys.sort_unstable();
    keys
}

/// Everything a backend plan-builder consumes. Injected by the CLI layer.
pub struct FlashInputs<'a> {
    /// Resolved artefact path.
    pub artefact: &'a Path,
    /// The entry's `flash_args`.
    pub flash_args: &'a Value,
    /// The slice/helper id (log lines).
    pub core_id: &'a str,
    /// SoM SKU.
    pub sku: &'a str,
    /// `--dry-run`.
    pub dry_run: bool,
    /// The env half of the confirm gate (`ALP_FLASH_FORCE=1`). The per-entry
    /// `flash_args.confirm` is OR-ed in by the yocto/xspi plan builders, so the
    /// effective gate is `flash_args.confirm OR ALP_FLASH_FORCE=1`.
    pub force_confirm: bool,
}

/// A built flash plan: the argv, the success message, whether it is planning-
/// only (never spawns real device IO), and — for the J-Link path — the
/// Commander script the CLI must materialise to a temp file.
#[derive(Debug, Clone, PartialEq)]
pub struct FlashPlan {
    /// The command to run (a `"|"` token marks a decompress→dd pipeline).
    pub argv: Vec<String>,
    /// Message on a clean spawn (rc 0).
    pub ok_message: String,
    /// Plan-only: print "would run …" and do not spawn (dry-run gate is separate).
    pub planning_only: bool,
    /// J-Link Commander script content; the CLI writes it to a temp file and
    /// appends its path as the final `-CommanderScript` arg.
    pub jlink_script: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_keys_are_sorted_and_complete() {
        let keys = registry_keys();
        assert_eq!(keys.len(), 7);
        let mut sorted = keys.clone();
        sorted.sort_unstable();
        assert_eq!(keys, sorted);
        assert!(backend_for("swd_probe").is_some());
        assert!(backend_for("yocto_wic").is_some());
        assert!(backend_for("nope").is_none());
    }
}
