// SPDX-License-Identifier: Apache-2.0
//! Board configuration model — Rust mirror of the TypeScript
//! `@alp-sdk/core` board/configurator model.
//!
//! The contract (shared JSON schema + fixtures) is the single source of
//! truth across the TS and Rust implementations.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Parsed `board.yaml` document. Unknown fields are ignored so that the
/// first Rust phases can expand safely while YAML keeps richer data.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct BoardModel {
    /// Schema revision. Absent in YAML is treated as v1 (matches TS, where
    /// `model.schema_version >= 2` is false for `undefined`).
    #[serde(default, rename = "schemaVersion")]
    pub schema_version: Option<u32>,

    /// System-on-module selection.
    #[serde(default)]
    pub som: Option<Som>,

    /// v1 only. In v2 this moves into a per-core `cores:` block.
    #[serde(default)]
    pub os: Option<String>,

    /// Named board preset applied to this configuration.
    #[serde(default)]
    pub preset: Option<String>,

    /// v2 per-core configuration, keyed by core name.
    #[serde(default)]
    pub cores: Option<BTreeMap<String, CoreEntry>>,

    /// Inter-processor communication shared-memory carve-outs.
    #[serde(default)]
    pub ipc: Option<Vec<IpcCarveOut>>,

    /// Inference backend/arena configuration (v1 top-level).
    #[serde(default)]
    pub inference: Option<Inference>,

    /// Enabled library identifiers (v1 top-level). Each entry is either a
    /// bare canonical manifest name (project-wide) or a `{name, cores?}`
    /// object scoping the library to a subset of cores.
    #[serde(default)]
    pub libraries: Option<Vec<LibraryEntry>>,

    /// IoT connectivity toggles (v1 top-level).
    #[serde(default)]
    pub iot: Option<Iot>,

    /// Diagnostics/logging configuration.
    #[serde(default)]
    pub diagnostics: Option<Diagnostics>,

    /// Populated-component flags keyed by component id.
    #[serde(default)]
    pub populated: Option<BTreeMap<String, bool>>,

    /// Selected chip identifiers.
    #[serde(default)]
    pub chips: Option<Vec<String>>,

    /// E1M routing entries, preserved as opaque YAML values.
    #[serde(default)]
    pub e1m_routes: Option<BTreeMap<String, serde_yaml::Value>>,
}

/// System-on-module identification.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Som {
    /// SoM part number / SKU.
    #[serde(default)]
    pub sku: Option<String>,
}

/// Per-core configuration block (schema v2).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CoreEntry {
    /// Operating system / runtime for this core.
    #[serde(default)]
    pub os: Option<String>,

    /// Application identifier built for this core.
    #[serde(default)]
    pub app: Option<String>,

    /// Image type / variant for this core.
    #[serde(default)]
    pub image: Option<String>,

    /// Peripheral identifiers assigned to this core.
    #[serde(default)]
    pub peripherals: Option<Vec<String>>,

    /// Enabled library identifiers for this core.
    #[serde(default)]
    pub libraries: Option<Vec<String>>,

    /// Inference configuration for this core.
    #[serde(default)]
    pub inference: Option<Inference>,

    /// IoT connectivity toggles for this core.
    #[serde(default)]
    pub iot: Option<Iot>,
}

/// One inter-processor shared-memory carve-out.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct IpcCarveOut {
    /// Carve-out name.
    pub name: String,
    /// IPC channel kind (`rpmsg`, `raw_shmem`, `mailbox_only`).
    pub kind: String,
    /// Endpoint identifiers attached to this carve-out.
    pub endpoints: Vec<String>,
    /// Size of the carve-out in KiB.
    pub carve_out_kb: u32,
    /// Override the default cacheable hint (default carve-out is non-cacheable).
    #[serde(default)]
    pub cacheable: Option<bool>,
    /// Explicit base address override; normally the orchestrator allocates one.
    #[serde(default)]
    pub address: Option<u64>,
}

/// A single top-level `libraries:` entry: either a bare canonical manifest
/// name (project-wide) or a `{name, cores?}` object scoping the library to
/// specific cores.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum LibraryEntry {
    /// Bare canonical manifest name — shorthand for a project-wide entry.
    Name(String),
    /// Unified entry: canonical name plus the core ids it's scoped to
    /// (absent `cores` = project-wide).
    Scoped {
        name: String,
        #[serde(default)]
        cores: Option<Vec<String>>,
    },
}

/// Inference backend and arena configuration.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Inference {
    /// Inference backend identifier.
    #[serde(default)]
    pub backend: Option<String>,

    /// Default tensor arena size in KiB.
    #[serde(default)]
    pub default_arena_kib: Option<u32>,
}

impl Inference {
    /// True when no backend is set (empty/absent) and no arena size is given.
    pub fn is_empty(&self) -> bool {
        self.backend.as_deref().unwrap_or_default().is_empty() && self.default_arena_kib.is_none()
    }
}

/// IoT connectivity feature toggles.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Iot {
    /// Wi-Fi enabled.
    #[serde(default)]
    pub wifi: Option<bool>,

    /// MQTT enabled.
    #[serde(default)]
    pub mqtt: Option<bool>,

    /// Bluetooth Low Energy enabled.
    #[serde(default)]
    pub ble: Option<bool>,

    /// TLS enabled.
    #[serde(default)]
    pub tls: Option<bool>,
}

impl Iot {
    /// True when any connectivity toggle is explicitly set to `true`.
    pub fn any_enabled(&self) -> bool {
        matches!(self.wifi, Some(true))
            || matches!(self.mqtt, Some(true))
            || matches!(self.ble, Some(true))
            || matches!(self.tls, Some(true))
    }
}

/// Diagnostics and logging configuration.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Diagnostics {
    /// Capture last-error reporting.
    #[serde(default)]
    pub last_error: Option<bool>,

    /// Log verbosity level.
    #[serde(default)]
    pub log_level: Option<String>,
}

impl BoardModel {
    /// Effective schema version, defaulting to 1 when absent.
    pub fn effective_schema_version(&self) -> u32 {
        self.schema_version.unwrap_or(1)
    }
}

/// Normalize a parsed board model using the same cleanup rules as
/// TypeScript `normalizeBoardModel`.
pub fn normalize_board_model(mut model: BoardModel) -> BoardModel {
    if model.effective_schema_version() < 2 {
        if model.libraries.as_ref().is_some_and(Vec::is_empty) {
            model.libraries = None;
        }

        if model.iot.as_ref().is_some_and(|iot| !iot.any_enabled()) {
            model.iot = None;
        }

        if model.inference.as_ref().is_some_and(Inference::is_empty) {
            model.inference = None;
        }
    }

    if model.effective_schema_version() >= 2 {
        model.os = None;
    }

    model
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `schemaVersion` (camelCase, matching the SDK schema/loader) is the
    /// wire key; the bare snake_case `schema_version:` is a stale local
    /// convention the loader never wrote and must not be recognised.
    #[test]
    fn schema_version_wire_key_is_camel_case() {
        let model: BoardModel = serde_yaml::from_str("schemaVersion: 2\n").unwrap();
        assert_eq!(model.schema_version, Some(2));

        let model: BoardModel = serde_yaml::from_str("schema_version: 2\n").unwrap();
        assert_eq!(model.schema_version, None);
    }

    /// `ipc:` entries use the schema's `carve_out_kb` + required `kind`, not
    /// the old `size_kib` field.
    #[test]
    fn ipc_carve_out_uses_schema_keys() {
        let model: BoardModel = serde_yaml::from_str(
            "ipc:\n  - kind: rpmsg\n    name: alp_default_rpmsg\n    endpoints: [a, b]\n    carve_out_kb: 512\n",
        )
        .unwrap();
        let entry = &model.ipc.unwrap()[0];
        assert_eq!(entry.kind, "rpmsg");
        assert_eq!(entry.carve_out_kb, 512);
        assert_eq!(entry.cacheable, None);
        assert_eq!(entry.address, None);
    }

    /// Top-level `libraries:` accepts a bare name OR a `{name, cores?}` object.
    #[test]
    fn libraries_accepts_bare_name_or_scoped_object() {
        let model: BoardModel = serde_yaml::from_str(
            "libraries:\n  - mbedtls\n  - name: cmsis-dsp\n    cores: [m33_sm]\n",
        )
        .unwrap();
        let libs = model.libraries.unwrap();
        assert_eq!(libs[0], LibraryEntry::Name("mbedtls".to_string()));
        assert_eq!(
            libs[1],
            LibraryEntry::Scoped {
                name: "cmsis-dsp".to_string(),
                cores: Some(vec!["m33_sm".to_string()]),
            }
        );
    }
}
