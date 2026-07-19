// SPDX-License-Identifier: Apache-2.0
//! Pure planning for `tan flash` — the decision + argv-building half of the
//! native port of `west alp-flash` (`scripts/west_commands/alp_flash.py` +
//! `scripts/flash_backends/*`), retiring the west extension under ADR-0020
//! Phase 4. Every string, argv, filter, and per-backend command shape lives here
//! and is unit-tested with no IO; the subprocess/filesystem/temp-file half lives
//! in `tan-cli`'s `commands/flash.rs`.
//!
//! The flow mirrors `alp_flash.dispatch` + `_flash_entry`: walk the manifest's
//! `boot_order` (or the sorted slice `core_id`s when empty), map each step to its
//! slice, append the helper MCUs after, then dispatch each entry's `flash_method`
//! to a backend plan-builder. `flash_args` sub-keys are read tolerantly — a
//! non-mapping value (the AEN701 helper's `flash_args: TBD` string) reads as an
//! empty map.

use std::path::{Path, PathBuf};

use serde_yaml::Value;

use crate::system_manifest::SystemManifest;

mod args;
mod builders;
mod registry;
mod storage;

pub use builders::{
    jlink_commander_script, plan_baremetal_cmake_flash, plan_cc3501e_usb_bootloader,
    plan_swd_probe, plan_zephyr_west_flash,
};
pub use registry::{
    BackendKind, FlashBackendMeta, FlashInputs, FlashPlan, backend_for, registry_keys,
};
pub use storage::{plan_xspi_flashwriter, plan_yocto_wic};

/// Whether an entry is a per-core image slice or an on-module helper MCU.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlashKind {
    /// A per-core image slice (keyed by `core_id`).
    Slice,
    /// An on-module helper MCU (keyed by `name`).
    Helper,
}

impl FlashKind {
    /// The human/JSON label (`slice` | `helper`).
    pub fn as_str(self) -> &'static str {
        match self {
            FlashKind::Slice => "slice",
            FlashKind::Helper => "helper",
        }
    }
}

/// One manifest entry selected for flashing, in dispatch order.
#[derive(Debug, Clone, PartialEq)]
pub struct FlashTarget {
    /// Slice vs. helper.
    pub kind: FlashKind,
    /// `core_id` (slice) or `name` (helper).
    pub id: String,
    /// The `flash_method` string, when present.
    pub flash_method: Option<String>,
    /// The raw `flash_args` value (kept as-is; non-mapping reads as empty).
    pub flash_args: Value,
    /// Slice artefact path (`output_artefact`).
    pub output_artefact: Option<String>,
    /// Helper firmware path (`firmware_path`).
    pub firmware_path: Option<String>,
}

/// Build the ordered flash target list + any `boot_order` warnings, mirroring
/// `alp_flash.dispatch`'s step-building. Pure.
///
/// - Empty `boot_order`: one step per slice `core_id`, sorted ascending.
/// - Non-empty `boot_order`: walked in order; a step referencing a `core_id`
///   not in `slices` is dropped and surfaced as a warning string.
/// - Helpers always come AFTER all slices.
/// - `core = Some` flashes only that slice and skips every helper.
/// - `helper = Some` skips every slice and flashes only that helper.
pub fn plan_flash_targets(
    m: &SystemManifest,
    core: Option<&str>,
    helper: Option<&str>,
) -> (Vec<FlashTarget>, Vec<String>) {
    let mut targets = Vec::new();
    let mut warnings = Vec::new();

    // Slice lookup by non-empty core_id (drop empties, matching the Python dict
    // comprehension guard).
    let find_slice = |cid: &str| {
        m.slices
            .iter()
            .find(|s| !s.core_id.is_empty() && s.core_id == cid)
    };

    // Steps: boot_order when present, else the sorted slice core_ids.
    let steps: Vec<String> = if m.boot_order.is_empty() {
        let mut ids: Vec<String> = m
            .slices
            .iter()
            .filter(|s| !s.core_id.is_empty())
            .map(|s| s.core_id.clone())
            .collect();
        ids.sort();
        ids
    } else {
        m.boot_order
            .iter()
            .filter_map(|step| {
                step.as_mapping()?
                    .iter()
                    .find(|(k, _)| k.as_str() == Some("core"))
                    .and_then(|(_, v)| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
            })
            .collect()
    };

    // ---- slices (skipped entirely when --helper is set) ----
    if helper.is_none() {
        for cid in &steps {
            if let Some(sel) = core {
                if cid != sel {
                    continue;
                }
            }
            match find_slice(cid) {
                Some(s) => targets.push(FlashTarget {
                    kind: FlashKind::Slice,
                    id: s.core_id.clone(),
                    flash_method: s.flash_method.clone(),
                    flash_args: s.flash_args.clone().unwrap_or(Value::Null),
                    output_artefact: s.output_artefact.clone(),
                    firmware_path: None,
                }),
                None => warnings.push(format!(
                    "flash: boot_order references core '{cid}' not in slices; skipping"
                )),
            }
        }
    }

    // ---- helper MCUs (skipped entirely when --core is set) ----
    if core.is_none() {
        for h in &m.helper_mcus {
            if h.name.is_empty() {
                continue;
            }
            if let Some(sel) = helper {
                if h.name != sel {
                    continue;
                }
            }
            targets.push(FlashTarget {
                kind: FlashKind::Helper,
                id: h.name.clone(),
                flash_method: h.flash_method.clone(),
                flash_args: h.flash_args.clone().unwrap_or(Value::Null),
                output_artefact: None,
                firmware_path: h.firmware_path.clone(),
            });
        }
    }

    (targets, warnings)
}

/// Resolve a manifest artefact string to a path. Absolute strings pass through;
/// relative strings try `build_root/artefact` first, then `sdk_root/artefact`
/// when the build candidate is not a real file — mirroring
/// `alp_flash._flash_entry`. `is_file` is injected to keep this pure.
pub fn resolve_artefact_path(
    artefact: &str,
    build_root: &Path,
    sdk_root: Option<&Path>,
    is_file: impl Fn(&Path) -> bool,
) -> PathBuf {
    let p = Path::new(artefact);
    if p.is_absolute() {
        return p.to_path_buf();
    }
    let cand_build = build_root.join(artefact);
    match sdk_root {
        None => cand_build,
        Some(sdk) => {
            let cand_sdk = sdk.join(artefact);
            if is_file(&cand_build) {
                cand_build
            } else if is_file(&cand_sdk) {
                cand_sdk
            } else {
                cand_build
            }
        }
    }
}

/// Outcome of the required-tool gate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolGate {
    /// At least one required tool is present (or the gate is bypassed).
    Proceed,
    /// No required tool present, but `--skip-missing-tools` downgrades to a skip.
    Skip(String),
    /// No required tool present and no skip flag — a hard failure.
    Fail(String),
}

/// The required-tool gate: a backend is usable when AT LEAST ONE of `requires`
/// is on PATH. Bypassed entirely under `--dry-run`, and for backends with an
/// empty `requires` (e.g. `xspi_flashwriter`). `which` is injected. Pure.
pub fn tool_gate(
    requires: &[&str],
    dry_run: bool,
    skip_missing: bool,
    kind: &str,
    id: &str,
    method: &str,
    which: impl Fn(&str) -> bool,
) -> ToolGate {
    if dry_run || requires.is_empty() {
        return ToolGate::Proceed;
    }
    if requires.iter().any(|t| which(t)) {
        return ToolGate::Proceed;
    }
    let msg = format!(
        "flash: {kind} '{id}' backend '{method}' needs one of {requires:?} on PATH; none found."
    );
    if skip_missing {
        ToolGate::Skip(format!("{msg} (skipped via --skip-missing-tools)"))
    } else {
        ToolGate::Fail(msg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    fn manifest(yaml: &str) -> SystemManifest {
        parse_system_manifest(yaml).expect("valid manifest")
    }

    const MULTI: &str = r#"
schema_version: 1
hw_info:
  sku: E1M-V2N101
slices:
- core_id: m33_sm
  os: zephyr
  output_artefact: build/m33_sm-zephyr/zephyr/zephyr.elf
  flash_method: zephyr_west_flash
  flash_args:
    runner: openocd
- core_id: a55_cluster
  os: yocto
  output_artefact: build/a55.wic
  flash_method: yocto_wic_to_sd_or_emmc
  flash_args:
    target: /dev/sdb
helper_mcus:
- name: gd32_bridge
  chip: gd32g553
  firmware_path: firmware/gd32.bin
  flash_method: swd_probe
  flash_args: {}
boot_order: []
"#;

    #[test]
    fn empty_boot_order_sorts_core_ids_helpers_last() {
        let m = manifest(MULTI);
        let (targets, warnings) = plan_flash_targets(&m, None, None);
        assert!(warnings.is_empty());
        let ids: Vec<&str> = targets.iter().map(|t| t.id.as_str()).collect();
        // a55_cluster < m33_sm ascending; helper last.
        assert_eq!(ids, vec!["a55_cluster", "m33_sm", "gd32_bridge"]);
        assert_eq!(targets[2].kind, FlashKind::Helper);
    }

    #[test]
    fn boot_order_is_walked_in_order_unknown_core_warns() {
        let yaml = MULTI.replace(
            "boot_order: []",
            "boot_order:\n- core: m33_sm\n- core: ghost\n- core: a55_cluster",
        );
        let m = manifest(&yaml);
        let (targets, warnings) = plan_flash_targets(&m, None, None);
        let ids: Vec<&str> = targets.iter().map(|t| t.id.as_str()).collect();
        // boot_order order preserved; ghost dropped; helper appended after.
        assert_eq!(ids, vec!["m33_sm", "a55_cluster", "gd32_bridge"]);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("ghost") && warnings[0].contains("not in slices"));
    }

    #[test]
    fn core_filter_selects_only_that_slice_no_helpers() {
        let m = manifest(MULTI);
        let (targets, _) = plan_flash_targets(&m, Some("m33_sm"), None);
        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].id, "m33_sm");
        assert_eq!(targets[0].kind, FlashKind::Slice);
    }

    #[test]
    fn helper_filter_selects_only_that_helper_no_slices() {
        let m = manifest(MULTI);
        let (targets, _) = plan_flash_targets(&m, None, Some("gd32_bridge"));
        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].id, "gd32_bridge");
        assert_eq!(targets[0].kind, FlashKind::Helper);
    }

    #[test]
    fn core_matches_nothing_yields_empty() {
        let m = manifest(MULTI);
        let (targets, warnings) = plan_flash_targets(&m, Some("nope"), None);
        assert!(targets.is_empty());
        assert!(warnings.is_empty());
    }

    #[test]
    fn resolve_artefact_prefers_build_then_sdk_then_falls_back() {
        let build = Path::new("/work/build");
        let sdk = Path::new("/sdk");
        // build candidate is a file -> pick it.
        let got = resolve_artefact_path("out.elf", build, Some(sdk), |p| {
            p == Path::new("/work/build/out.elf")
        });
        assert_eq!(got, PathBuf::from("/work/build/out.elf"));
        // build missing, sdk file exists -> pick sdk.
        let got = resolve_artefact_path("fw.bin", build, Some(sdk), |p| {
            p == Path::new("/sdk/fw.bin")
        });
        assert_eq!(got, PathBuf::from("/sdk/fw.bin"));
        // neither is a file -> fall back to build candidate.
        let got = resolve_artefact_path("x.bin", build, Some(sdk), |_| false);
        assert_eq!(got, PathBuf::from("/work/build/x.bin"));
        // absolute passes through unchanged.
        let got = resolve_artefact_path("/abs/y.elf", build, Some(sdk), |_| false);
        assert_eq!(got, PathBuf::from("/abs/y.elf"));
    }

    #[test]
    fn tool_gate_semantics() {
        let reqs = ["west"];
        // at least one present -> proceed.
        assert_eq!(
            tool_gate(
                &reqs,
                false,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| true
            ),
            ToolGate::Proceed
        );
        // none present + skip_missing -> Skip with the flag phrase.
        match tool_gate(
            &reqs,
            false,
            true,
            "slice",
            "m",
            "zephyr_west_flash",
            |_| false,
        ) {
            ToolGate::Skip(msg) => assert!(msg.contains("skipped via --skip-missing-tools")),
            other => panic!("expected Skip, got {other:?}"),
        }
        // none present + no skip -> Fail.
        assert!(matches!(
            tool_gate(
                &reqs,
                false,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| false
            ),
            ToolGate::Fail(_)
        ));
        // dry-run bypasses regardless.
        assert_eq!(
            tool_gate(
                &reqs,
                true,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| false
            ),
            ToolGate::Proceed
        );
        // empty requires never trips.
        assert_eq!(
            tool_gate(&[], false, false, "helper", "x", "xspi_flashwriter", |_| {
                false
            }),
            ToolGate::Proceed
        );
    }
}
