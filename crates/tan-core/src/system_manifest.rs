// SPDX-License-Identifier: Apache-2.0
//! The ALP **system manifest** (`build/system-manifest.yaml`) — the single
//! derived projection of a `board.yaml` that the SDK's `alp_orchestrate.py
//! --emit system-manifest` emits, and the IDE/tool CONTRACT for a (possibly
//! multi-image) project. Tools read THIS for folder layout + build/flash
//! wiring instead of re-deriving from board.yaml + the SoM presets.
//! (ADR-0020 Phase 4 retired the SDK-side `west alp-build` executor —
//! alp-sdk is plans-only; `tan build` is the executor now.)
//!
//! No vendored schema copy ships in this repo; the shape here was last
//! diffed against `metadata/schemas/system-manifest-v1.schema.json` at
//! alp-sdk v0.8.0. Since then the SDK has added at least `slices[].recipe`
//! and `hw_info.eeprom` — both additive under the v1 tolerant-reader policy
//! below, so a stale diff doesn't break reads; it only means a typed field
//! this module doesn't yet model (harmless on read, see the write-path note
//! on `serialize_system_manifest_raw` below for why that never matters on
//! write either).
//!
//! TOLERANT READER (the stability policy negotiated in alp-sdk#106):
//! `schema_version` 1 is additive-only; a breaking change ships as v2 through a
//! deprecation cycle. So we never `deny_unknown_fields` (a newer SDK's additive
//! fields are ignored, not rejected), and fields the schema marks "required"
//! but the emitter omits for `off`/`pending` slices (`flash_method`/
//! `flash_args`) are modeled `Option`.

use serde::{Deserialize, Serialize};

/// The schema major this CLI consumes. A different value is rejected rather
/// than silently mis-applied.
pub const SYSTEM_MANIFEST_SCHEMA_VERSION: u32 = 1;

/// Resolved hardware identity for the project.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct HwInfo {
    /// Resolved board SKU (e.g. `E1M-AEN701`).
    #[serde(default)]
    pub sku: String,
    /// System-on-Module hardware revision, if declared.
    #[serde(default)]
    pub som_hw_rev: Option<String>,
    /// Carrier/EVK board name, if declared.
    #[serde(default)]
    pub board_name: Option<String>,
    /// Carrier/EVK board hardware revision, if declared.
    #[serde(default)]
    pub board_hw_rev: Option<String>,
    /// Resolved silicon id (e.g. `alif:ensemble:e7`), if declared.
    #[serde(default)]
    pub silicon: Option<String>,
}

/// One per-core image: its runtime + the build/flash wiring the IDE needs to
/// build, run, debug, and flash it without re-deriving anything.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Slice {
    /// Core identifier this image targets (e.g. `m55_hp`, `a32_cluster`).
    pub core_id: String,
    /// Resolved runtime (`zephyr` | `yocto` | `baremetal` | `off`); the
    /// value-set is owned by `board.schema.json` `cores.<core>.os`.
    pub os: String,
    /// Application source path or app/recipe name for this image.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub app: Option<String>,
    /// Yocto image name, when `os` is `yocto`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image: Option<String>,
    /// Yocto machine, when `os` is `yocto`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub machine: Option<String>,
    /// Zephyr board target, when `os` is `zephyr`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub board: Option<String>,
    /// Resolved toolchain id for building this image.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub toolchain: Option<String>,
    /// Build output directory for this image.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub build_dir: Option<String>,
    /// Path to the built firmware/image artefact.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_artefact: Option<String>,
    /// Build/flash status: `pending` | `ok` | `failed` | `skipped` (per
    /// `system-manifest-v1.schema.json`'s `slices[].status` enum — `blocked`
    /// is an `IpcLink` status, not a `Slice` one).
    #[serde(default)]
    pub status: String,
    /// Path to this slice's build log, if produced.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub log_path: Option<String>,
    /// Human-readable explanation when `status` indicates a problem.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    /// Schema-required, but the emitter omits it for `off`/`pending` slices.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flash_method: Option<String>,
    /// Method-specific (shape varies by `flash_method`); kept opaque.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flash_args: Option<serde_yaml::Value>,
}

impl Slice {
    /// A slice that participates in build/flash (its core isn't `off`).
    pub fn is_active(&self) -> bool {
        self.os != "off"
    }
}

/// An inter-core link (rpmsg / mailbox) with its resolved carve-out. A blocked
/// link carries `status: blocked` + `reason`. Extra carve-out fields beyond the
/// schema's `required` set are tolerated (the schema is `additionalProperties:
/// true` here) and ignored.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IpcLink {
    /// Link name.
    pub name: String,
    /// Transport kind (e.g. `rpmsg`, `mailbox`).
    pub kind: String,
    /// Core ids participating in the link.
    #[serde(default)]
    pub endpoints: Vec<String>,
    /// Link status (e.g. `blocked`), when not implicitly OK.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    /// Explanation when `status` is `blocked`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// An on-module helper MCU (e.g. the GD32 bridge) that ships its own firmware.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HelperMcu {
    /// Helper MCU name.
    pub name: String,
    /// Chip part id (e.g. `cc3501e`).
    pub chip: String,
    /// Path to the helper's firmware image, when known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firmware_path: Option<String>,
    /// Flash method id for this helper, when known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flash_method: Option<String>,
    /// An object, or the string `"TBD"` when the recipe isn't finalized.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flash_args: Option<serde_yaml::Value>,
}

/// The whole manifest — the deserialization target for `build/system-manifest.yaml`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SystemManifest {
    /// Schema major version; guarded against `SYSTEM_MANIFEST_SCHEMA_VERSION`.
    pub schema_version: u32,
    /// Identifier of the tool/script that emitted this manifest.
    #[serde(default)]
    pub generated_by: String,
    /// Resolved hardware identity.
    #[serde(default)]
    pub hw_info: HwInfo,
    /// Per-core images.
    #[serde(default)]
    pub slices: Vec<Slice>,
    /// Inter-core links.
    #[serde(default)]
    pub ipc: Vec<IpcLink>,
    /// On-module helper MCUs.
    #[serde(default)]
    pub helper_mcus: Vec<HelperMcu>,
    /// Inter-image boot sequencing — opaque until a SoM declares `boot_order`.
    #[serde(default)]
    pub boot_order: Vec<serde_yaml::Value>,
    /// Resolved storage partitions — present only when the project declares storage.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub storage: Vec<serde_yaml::Value>,
}

impl SystemManifest {
    /// Slices whose core participates in build/flash (`os != off`).
    pub fn active_slices(&self) -> impl Iterator<Item = &Slice> {
        self.slices.iter().filter(|s| s.is_active())
    }

    /// The slice for a given core id, if present.
    pub fn slice_for_core(&self, core_id: &str) -> Option<&Slice> {
        self.slices.iter().find(|s| s.core_id == core_id)
    }
}

/// Why a system-manifest could not be consumed.
#[derive(Debug, thiserror::Error)]
pub enum SystemManifestError {
    /// The document is not valid YAML or doesn't match the manifest shape.
    #[error("system-manifest is not valid YAML: {0}")]
    Parse(String),
    /// `schema_version` differs from the version this CLI consumes.
    #[error(
        "unsupported system-manifest schema_version {found} (this CLI consumes v{supported}); \
         upgrade the CLI or the SDK so the versions match"
    )]
    UnsupportedSchemaVersion { found: u32, supported: u32 },
}

/// Parse + version-guard a `system-manifest.yaml` document. Pure: no IO.
/// Tolerant — unknown additive-v1 fields are ignored.
pub fn parse_system_manifest(yaml: &str) -> Result<SystemManifest, SystemManifestError> {
    let manifest: SystemManifest =
        serde_yaml::from_str(yaml).map_err(|e| SystemManifestError::Parse(e.to_string()))?;
    if manifest.schema_version != SYSTEM_MANIFEST_SCHEMA_VERSION {
        return Err(SystemManifestError::UnsupportedSchemaVersion {
            found: manifest.schema_version,
            supported: SYSTEM_MANIFEST_SCHEMA_VERSION,
        });
    }
    Ok(manifest)
}

/// One slice's post-build outcome as the executor reports it:
/// `(core_id, status, output_artefact, build_dir, reason)`. A `None` in any
/// optional position means "leave the plan-time value untouched".
pub type SliceRunResult = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// Overlay this run's per-slice outcomes onto a plan-time manifest so the
/// written `system-manifest.yaml` reflects what actually built. Each result is
/// a [`SliceRunResult`]; slices are matched by `core_id` and slices with no
/// matching result (e.g. `off` cores) are left untouched.
///
/// No status remap is needed: the executor's vocabulary (`ok`/`failed`/
/// `skipped`) is already a subset of the manifest schema's status enum
/// (`pending`/`ok`/`failed`/`skipped` — `system-manifest-v1.schema.json`), so
/// the status is copied verbatim. `output_artefact`, `build_dir`, and `reason`
/// are each written only when the result carries one — a `None` never wipes a
/// plan-time value, so a slice the executor could not resolve keeps whatever
/// the planner emitted. Pure: no IO.
pub fn overlay_run_results(manifest: &mut SystemManifest, results: &[SliceRunResult]) {
    for (core_id, status, output_artefact, build_dir, reason) in results {
        if let Some(slice) = manifest.slices.iter_mut().find(|s| &s.core_id == core_id) {
            slice.status = status.clone();
            if output_artefact.is_some() {
                slice.output_artefact = output_artefact.clone();
            }
            if build_dir.is_some() {
                slice.build_dir = build_dir.clone();
            }
            if reason.is_some() {
                slice.reason = reason.clone();
            }
        }
    }
}

/// Serialize a manifest back to YAML (the post-build `system-manifest.yaml`
/// bytes). Kept in tan-core so the CLI needs no direct `serde_yaml` dependency;
/// the error is stringified for the CLI's best-effort write path.
///
/// DO NOT use this on the post-build rewrite path: it goes through the typed
/// `SystemManifest`, which is a deliberately TOLERANT (lossy-on-write) reader
/// — it has no field for e.g. an rpmsg `IpcLink`'s carve-out (`shm_addr`,
/// `shm_size`, ...) beyond `name`/`kind`/`endpoints`/`status`/`reason`, or for
/// `hw_info.eeprom`. Re-serializing the typed struct silently deletes every
/// such field from the file on disk. Use `overlay_run_results_raw` +
/// `serialize_system_manifest_raw` (below) for any write, which round-trip
/// through `serde_yaml::Value` and so preserve fields this struct doesn't
/// model. This typed serializer stays only for the read-only round-trip tests
/// in this module.
pub fn serialize_system_manifest(manifest: &SystemManifest) -> Result<String, String> {
    serde_yaml::to_string(manifest).map_err(|e| e.to_string())
}

/// Parse a `system-manifest.yaml` document straight into the RAW
/// `serde_yaml::Value` form (str-in / `Value`-out), the entry point the write
/// path must use instead of `parse_system_manifest`. The typed reader is
/// TOLERANT on read but lossy on write (see `serialize_system_manifest`), so
/// starting the write path from `parse_system_manifest` already threw away
/// every additive field (ipc carve-out, `hw_info.eeprom`, ...) before
/// `overlay_run_results_raw` ever ran — reserializing the typed struct once
/// was not the only place those fields died. Kept in tan-core (not just the
/// raw serializer) so a caller never needs its own `serde_yaml` dependency:
/// the whole raw seam is parse -> `overlay_run_results_raw` ->
/// `serialize_system_manifest_raw`, all through this module. Still
/// version-guarded identically to `parse_system_manifest`, so switching a
/// caller to this pair changes nothing about which documents are accepted.
pub fn parse_system_manifest_raw(yaml: &str) -> Result<serde_yaml::Value, SystemManifestError> {
    let raw: serde_yaml::Value =
        serde_yaml::from_str(yaml).map_err(|e| SystemManifestError::Parse(e.to_string()))?;
    match raw
        .get("schema_version")
        .and_then(serde_yaml::Value::as_u64)
    {
        Some(found) if found == u64::from(SYSTEM_MANIFEST_SCHEMA_VERSION) => Ok(raw),
        Some(found) => Err(SystemManifestError::UnsupportedSchemaVersion {
            found: found as u32,
            supported: SYSTEM_MANIFEST_SCHEMA_VERSION,
        }),
        None => Err(SystemManifestError::Parse(
            "missing or non-numeric schema_version".to_string(),
        )),
    }
}

/// Overlay this run's per-slice outcomes onto the RAW manifest value parsed
/// from YAML (same semantics as `overlay_run_results`, but operating on
/// `serde_yaml::Value` instead of the typed `SystemManifest` — see
/// `serialize_system_manifest_raw` for why the write path must use this pair
/// instead of the typed one). Slices are matched by `core_id`; a slice not
/// shaped as a mapping, or with no matching result, is left untouched.
pub fn overlay_run_results_raw(raw: &mut serde_yaml::Value, results: &[SliceRunResult]) {
    let Some(slices) = raw
        .get_mut("slices")
        .and_then(serde_yaml::Value::as_sequence_mut)
    else {
        return;
    };
    for slice in slices.iter_mut() {
        let Some(map) = slice.as_mapping_mut() else {
            continue;
        };
        let Some(core_id) = map.get("core_id").and_then(serde_yaml::Value::as_str) else {
            continue;
        };
        let Some((_, status, output_artefact, build_dir, reason)) =
            results.iter().find(|(cid, ..)| cid == core_id)
        else {
            continue;
        };
        map.insert("status".into(), status.clone().into());
        if let Some(a) = output_artefact {
            map.insert("output_artefact".into(), a.clone().into());
        }
        if let Some(b) = build_dir {
            map.insert("build_dir".into(), b.clone().into());
        }
        if let Some(r) = reason {
            map.insert("reason".into(), r.clone().into());
        }
    }
}

/// Serialize the RAW manifest value (as returned by `serde_yaml::from_str`,
/// NOT the typed `SystemManifest`) back to YAML.
///
/// THE WRITE PATH MUST USE THIS, not `serialize_system_manifest`: the typed
/// struct models only the schema fields this CLI currently reads, and the
/// tolerant-reader policy at the top of this module deliberately treats
/// everything else as ignorable — safe for a read-only consumer, but the
/// post-build rewrite in `build/execute/manifest.rs` parses the SDK's
/// `--emit system-manifest` YAML, overlays this run's results, and OVERWRITES
/// `system-manifest.yaml` with the result. Going through `SystemManifest`
/// there deletes every additive field it doesn't model — an rpmsg `IpcLink`'s
/// shared-memory carve-out, `hw_info.eeprom`, anything a newer SDK adds —
/// which defeats `tan image`'s `raw_passthrough` (written specifically to
/// preserve those fields into the bundle manifest, but by the time it runs
/// `tan build` has already deleted them from the source file). Round-tripping
/// through `serde_yaml::Value` instead never drops a field it didn't
/// explicitly overlay.
pub fn serialize_system_manifest_raw(raw: &serde_yaml::Value) -> Result<String, String> {
    serde_yaml::to_string(raw).map_err(|e| e.to_string())
}

/// Deterministic, human-readable summary lines (text-mode `tan build`). Pure.
pub fn summarize_manifest(m: &SystemManifest) -> Vec<String> {
    let mut lines = vec![format!(
        "system-manifest: {} — {} slice(s), {} active, {} ipc, {} helper mcu(s)",
        m.hw_info.sku,
        m.slices.len(),
        m.active_slices().count(),
        m.ipc.len(),
        m.helper_mcus.len(),
    )];
    for s in &m.slices {
        let target = s
            .board
            .as_deref()
            .or(s.machine.as_deref())
            .or(s.image.as_deref())
            .unwrap_or("-");
        lines.push(format!(
            "  {} [{}] {} ({})",
            s.core_id, s.os, s.status, target
        ));
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    // The real `--emit system-manifest` output for the heterogeneous AEN701
    // example: an `off` A-core slice WITHOUT flash_method/flash_args, two Zephyr
    // slices WITH them, and a helper MCU whose flash_args is the string "TBD"
    // plus an undeclared `note` field — all the tolerant-reader corner cases.
    const AEN701: &str = r#"
schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-AEN701
  som_hw_rev: r1
  board_name: E1M-EVK
  board_hw_rev: r1
  silicon: alif:ensemble:e7
slices:
- core_id: a32_cluster
  os: 'off'
  app: alp-image-edge
  machine: e1m-aen701-a32
  toolchain: poky-glibc
  status: pending
- core_id: m55_hp
  os: zephyr
  app: ./src
  board: alp_e1m_aen701_m55_hp
  toolchain: arm-zephyr-eabi
  status: pending
  flash_method: zephyr_west_flash
  flash_args:
    runner: openocd
- core_id: m55_he
  os: zephyr
  app: alp-stock-shim
  board: alp_e1m_aen701_m55_he
  toolchain: arm-zephyr-eabi
  status: pending
  flash_method: zephyr_west_flash
  flash_args:
    runner: openocd
ipc: []
helper_mcus:
- name: cc3501e_otp
  chip: cc3501e
  firmware_path: TBD
  flash_method: TBD
  flash_args: TBD
  note: firmware_path TBD; populated when the upstream firmware release lands
boot_order: []
"#;

    #[test]
    fn parses_the_real_aen701_manifest() {
        let m = parse_system_manifest(AEN701).expect("valid manifest");
        assert_eq!(m.schema_version, 1);
        assert_eq!(m.generated_by, "scripts/alp_orchestrate.py");
        assert_eq!(m.hw_info.sku, "E1M-AEN701");
        assert_eq!(m.hw_info.silicon.as_deref(), Some("alif:ensemble:e7"));
        assert_eq!(m.slices.len(), 3);

        // The `off` slice omits flash_method/flash_args — tolerated as None.
        let a32 = m.slice_for_core("a32_cluster").expect("a32 slice");
        assert_eq!(a32.os, "off");
        assert!(a32.flash_method.is_none());
        assert!(!a32.is_active());

        // An active Zephyr slice carries the flash wiring.
        let hp = m.slice_for_core("m55_hp").expect("m55_hp slice");
        assert_eq!(hp.flash_method.as_deref(), Some("zephyr_west_flash"));
        assert!(hp.is_active());
        assert_eq!(m.active_slices().count(), 2);

        // Helper MCU keeps its string flash_args; the undeclared `note` is ignored.
        assert_eq!(m.helper_mcus.len(), 1);
        assert_eq!(m.helper_mcus[0].chip, "cc3501e");
    }

    #[test]
    fn rejects_unsupported_schema_version() {
        let yaml = AEN701.replace("schema_version: 1", "schema_version: 2");
        let err = parse_system_manifest(&yaml).unwrap_err();
        assert!(matches!(
            err,
            SystemManifestError::UnsupportedSchemaVersion { found: 2, .. }
        ));
    }

    #[test]
    fn tolerates_unknown_additive_fields() {
        // A future additive-v1 block at the root must be ignored, not rejected.
        let yaml = format!("{AEN701}\nfuture_block:\n  anything: 1\n");
        let m = parse_system_manifest(&yaml).expect("unknown fields tolerated");
        assert_eq!(m.slices.len(), 3);
    }

    #[test]
    fn overlay_sets_status_and_leaves_unmatched_slices_untouched() {
        let mut m = parse_system_manifest(AEN701).unwrap();
        // a32_cluster (an `off` core) has no result — must stay `pending`.
        overlay_run_results(
            &mut m,
            &[
                (
                    "m55_hp".to_string(),
                    "ok".to_string(),
                    Some("build/m55_hp-zephyr/build/zephyr/zephyr.elf".to_string()),
                    Some("build/m55_hp-zephyr/build".to_string()),
                    None,
                ),
                ("m55_he".to_string(), "failed".to_string(), None, None, None),
                // A result for a core not in the manifest is a no-op.
                ("ghost".to_string(), "ok".to_string(), None, None, None),
            ],
        );

        let hp = m.slice_for_core("m55_hp").unwrap();
        assert_eq!(hp.status, "ok");
        assert_eq!(
            hp.output_artefact.as_deref(),
            Some("build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );
        assert_eq!(hp.build_dir.as_deref(), Some("build/m55_hp-zephyr/build"));

        // failed with no artefact: status flips, artefact stays as the plan had it (None).
        let he = m.slice_for_core("m55_he").unwrap();
        assert_eq!(he.status, "failed");
        assert!(he.output_artefact.is_none());

        // The unmatched `off` slice is untouched.
        let a32 = m.slice_for_core("a32_cluster").unwrap();
        assert_eq!(a32.status, "pending");
    }

    #[test]
    fn overlay_none_artefact_preserves_existing_value() {
        let mut m = parse_system_manifest(AEN701).unwrap();
        // Seed a plan-time artefact, then overlay a status-only result (None).
        m.slices[1].output_artefact = Some("build/plan-time.elf".to_string());
        let core = m.slices[1].core_id.clone();
        overlay_run_results(&mut m, &[(core, "ok".to_string(), None, None, None)]);
        assert_eq!(
            m.slices[1].output_artefact.as_deref(),
            Some("build/plan-time.elf")
        );
        assert_eq!(m.slices[1].status, "ok");
    }

    #[test]
    fn overlaid_manifest_serializes_and_reparses() {
        let mut m = parse_system_manifest(AEN701).unwrap();
        overlay_run_results(
            &mut m,
            &[("m55_hp".to_string(), "ok".to_string(), None, None, None)],
        );
        let yaml = serialize_system_manifest(&m).expect("serialize");
        let reparsed = parse_system_manifest(&yaml).expect("reparse");
        assert_eq!(reparsed.slice_for_core("m55_hp").unwrap().status, "ok");
    }

    // AEN701 with an rpmsg carve-out (shm_addr/shm_size beyond what `IpcLink`
    // models) and an `hw_info.eeprom` block (beyond what `HwInfo` models) — the
    // two additive-field shapes the typed struct silently drops on rewrite.
    fn manifest_with_additive_fields() -> String {
        AEN701
            .replace(
                "ipc: []\n",
                "ipc:\n- name: rpmsg0\n  kind: rpmsg\n  endpoints: [a55, m33]\n  shm_addr: 0x48000000\n  shm_size: 0x100000\n",
            )
            .replace("hw_info:\n", "hw_info:\n  eeprom:\n    magic: 0xA5\n")
    }

    #[test]
    fn typed_reserialize_drops_additive_fields_ipc_carveout_and_eeprom() {
        // Pins the DEFECT that makes overlay_run_results_raw/
        // serialize_system_manifest_raw necessary: going through the typed
        // SystemManifest (tolerant on READ) silently deletes any field it
        // doesn't model when used to WRITE the file back out.
        let yaml = manifest_with_additive_fields();
        let m = parse_system_manifest(&yaml).unwrap();
        let out = serialize_system_manifest(&m).unwrap();
        assert!(!out.contains("shm_addr"), "typed reserialize kept shm_addr");
        assert!(!out.contains("eeprom"), "typed reserialize kept eeprom");
    }

    #[test]
    fn overlay_run_results_raw_preserves_additive_fields_ipc_carveout_and_eeprom() {
        // The fix: overlay+serialize on the raw serde_yaml::Value never goes
        // through SystemManifest, so fields it doesn't model (an rpmsg carve
        // -out's shm_addr/shm_size, hw_info.eeprom) survive the write, and
        // this run's overlay (status/output_artefact/build_dir) still lands.
        let yaml = manifest_with_additive_fields();
        let mut raw: serde_yaml::Value = serde_yaml::from_str(&yaml).unwrap();
        overlay_run_results_raw(
            &mut raw,
            &[(
                "m55_hp".to_string(),
                "ok".to_string(),
                Some("build/m55_hp-zephyr/build/zephyr/zephyr.elf".to_string()),
                None,
                None,
            )],
        );
        let out = serialize_system_manifest_raw(&raw).expect("serialize raw");

        assert!(out.contains("shm_addr"), "raw reserialize dropped shm_addr");
        assert!(out.contains("eeprom"), "raw reserialize dropped eeprom");

        let reparsed = parse_system_manifest(&out).expect("reparse");
        let hp = reparsed.slice_for_core("m55_hp").unwrap();
        assert_eq!(hp.status, "ok");
        assert_eq!(
            hp.output_artefact.as_deref(),
            Some("build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );
        // Untouched slices/fields stay as-is.
        let a32 = reparsed.slice_for_core("a32_cluster").unwrap();
        assert_eq!(a32.status, "pending");
    }

    #[test]
    fn overlay_run_results_and_raw_thread_reason_through() {
        // L22 regression: a skipped/failed slice's `reason` (the executor's
        // "no command" / "{tool} not found in PATH; ..." text) must land in
        // both the typed and raw overlay so a skipped slice explains itself
        // in the written system-manifest.yaml, matching the schema's
        // `slices[].reason` field.
        let mut m = parse_system_manifest(AEN701).unwrap();
        overlay_run_results(
            &mut m,
            &[(
                "m55_he".to_string(),
                "skipped".to_string(),
                None,
                None,
                Some("no command".to_string()),
            )],
        );
        assert_eq!(
            m.slice_for_core("m55_he").unwrap().reason.as_deref(),
            Some("no command")
        );

        let yaml = manifest_with_additive_fields();
        let mut raw: serde_yaml::Value = serde_yaml::from_str(&yaml).unwrap();
        overlay_run_results_raw(
            &mut raw,
            &[(
                "m55_hp".to_string(),
                "skipped".to_string(),
                None,
                None,
                Some("west not found in PATH; this is normal on non-Zephyr hosts".to_string()),
            )],
        );
        let out = serialize_system_manifest_raw(&raw).expect("serialize raw");
        let reparsed = parse_system_manifest(&out).expect("reparse");
        assert_eq!(
            reparsed.slice_for_core("m55_hp").unwrap().reason.as_deref(),
            Some("west not found in PATH; this is normal on non-Zephyr hosts")
        );
    }

    #[test]
    fn parse_system_manifest_raw_preserves_additive_fields_ipc_carveout_and_eeprom() {
        // Regression: the write path (crates/tan-cli/.../manifest.rs) must be
        // able to reach the raw overlay/serialize pair without ever routing
        // through the lossy typed SystemManifest. Before this fn existed the
        // only str-in entry point was parse_system_manifest, which already
        // drops shm_addr/eeprom at parse time (they aren't modeled), so
        // starting the raw seam from it would still lose them on write.
        let yaml = manifest_with_additive_fields();
        let raw = parse_system_manifest_raw(&yaml).expect("valid manifest");
        let out = serialize_system_manifest_raw(&raw).expect("serialize raw");
        assert!(out.contains("shm_addr"), "raw parse dropped shm_addr");
        assert!(out.contains("eeprom"), "raw parse dropped eeprom");
    }

    #[test]
    fn parse_system_manifest_raw_rejects_unsupported_schema_version() {
        // The raw entry point must enforce the same version guard as the
        // typed one -- switching a caller to it must not silently start
        // accepting a schema_version this CLI doesn't consume.
        let yaml = AEN701.replace("schema_version: 1", "schema_version: 2");
        let err = parse_system_manifest_raw(&yaml).unwrap_err();
        assert!(matches!(
            err,
            SystemManifestError::UnsupportedSchemaVersion { found: 2, .. }
        ));
    }

    #[test]
    fn parse_system_manifest_raw_rejects_schema_version_beyond_u32() {
        // 2^32 + 1 is congruent to 1 mod 2^32, so a naive `as u32` truncation
        // of the guard would wrongly accept it as the supported version. The
        // raw guard must compare the full u64 and reject it, same as the
        // typed `parse_system_manifest` (whose serde field is `u32` and
        // errors on this value at deserialization).
        let yaml = AEN701.replace("schema_version: 1", "schema_version: 4294967297");
        let err = parse_system_manifest_raw(&yaml).unwrap_err();
        assert!(matches!(
            err,
            SystemManifestError::UnsupportedSchemaVersion { .. }
        ));
    }

    #[test]
    fn round_trips_through_serde() {
        let m = parse_system_manifest(AEN701).unwrap();
        let yaml = serde_yaml::to_string(&m).unwrap();
        let reparsed = parse_system_manifest(&yaml).unwrap();
        assert_eq!(m, reparsed);
    }
}
