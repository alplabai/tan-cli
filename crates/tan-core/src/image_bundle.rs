// SPDX-License-Identifier: Apache-2.0
//! Pure model + string builders for `tan image` — the flashable-image bundle
//! projection. The IO (tar/gzip/sha256/copy/mkdir) lives in the CLI
//! (`commands::image`); this module owns every *decision* that doesn't touch the
//! filesystem so it stays unit-testable: the bundle-manifest shape + key order,
//! the OS-independent forward-slash artefact paths, and the ok-slice gate.
//!
//! Faithful to the retired `west alp-image` / `scripts/.../alp_image.py`, with
//! two deliberate contract fixes flagged in the port spec:
//!   - the helper entry carries `chip` (the real schema field) instead of the
//!     perpetually-`null` `role` (a Python bug — `role` is in neither the schema
//!     nor the manifest model, so `hm.get('role')` always yielded `null`);
//!   - artefact paths are built as forward-slash strings here rather than from a
//!     `PathBuf`, so the machine contract never leaks a Windows `\` separator.

use std::path::Path;

use serde::Serialize;

use crate::path_guard::is_plain_relative;

/// Bundle-manifest `schema_version` (matches the system-manifest v1 line).
pub const BUNDLE_SCHEMA_VERSION: u32 = 1;
/// `generated_by` value — tan-branded (Python emitted `west alp-image`).
pub const GENERATED_BY: &str = "tan image";
/// The single input document read from the build root.
pub const MANIFEST_FILE: &str = "system-manifest.yaml";
/// Bundle root directory name (under the build root).
pub const BUNDLE_DIR: &str = "image-bundle";
/// Per-slice archive subdirectory name.
pub const SLICES_DIR: &str = "slices";
/// Helper-MCU firmware subdirectory name.
pub const HELPERS_DIR: &str = "helper-mcus";
/// Output bundle-manifest filename.
pub const BUNDLE_MANIFEST: &str = "bundle-manifest.json";

/// One bundled slice: its archive path + integrity metadata.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BundleSliceEntry {
    /// Core this slice targets (e.g. `m55_hp`).
    pub core_id: String,
    /// Resolved runtime (e.g. `zephyr`).
    pub os: String,
    /// Forward-slash relative path to the archive (`slices/<core>-<os>.tar.gz`).
    pub artefact: String,
    /// Lowercase-hex SHA-256 of the archive bytes.
    pub sha256: String,
    /// Archive size in bytes.
    pub size: u64,
}

/// One bundled helper-MCU firmware: its copied path + integrity metadata.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BundleHelperEntry {
    /// Helper name from the manifest (`null` only if the manifest omitted it).
    pub name: Option<String>,
    /// Helper chip id — the deliberate replacement for the null `role` bug.
    pub chip: Option<String>,
    /// Forward-slash relative path (`helper-mcus/<basename>`).
    pub artefact: String,
    /// Lowercase-hex SHA-256 of the copied firmware bytes.
    pub sha256: String,
    /// Firmware size in bytes.
    pub size: u64,
}

/// The written `bundle-manifest.json`. Field order IS the on-disk key order
/// (serde_json preserve_order): schema_version, generated_by, hw_info, slices,
/// helper_mcus, boot_order.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BundleManifest {
    /// Bundle schema major.
    pub schema_version: u32,
    /// Emitting tool id (`tan image`).
    pub generated_by: String,
    /// The manifest `hw_info` object, carried verbatim (raw `serde_yaml::Value`
    /// so additive keys like `eeprom` survive — a typed `HwInfo` would drop them).
    pub hw_info: serde_yaml::Value,
    /// Bundled slices (empty when none built).
    pub slices: Vec<BundleSliceEntry>,
    /// Bundled helper-MCU firmwares (empty when none present).
    pub helper_mcus: Vec<BundleHelperEntry>,
    /// The manifest `boot_order` list, carried verbatim (opaque).
    pub boot_order: Vec<serde_yaml::Value>,
}

/// Archive filename for a slice: `<core_id>-<os>.tar.gz`.
///
/// `None` when `core_id`/`os` aren't safe to fold into a filename that will
/// be `.join()`ed onto the bundle's `slices/` dir. Both fields come straight
/// off system-manifest.yaml — the parser is a deliberately tolerant reader,
/// not a validator — and this used to `format!` them in unchecked: a
/// `core_id` of `../../../../home/u/x` or (Windows) `C:/x` escaped
/// `image-bundle/slices/` entirely, so the caller's `remove_file` +
/// `File::create` could unlink/overwrite an arbitrary `*.tar.gz` outside the
/// build tree. The guard lives here rather than at the call site because
/// `slice_artefact_rel` below needs the exact same check for the manifest's
/// `artefact` string — a per-call-site guard is how this class of bug keeps
/// recurring in this codebase.
pub fn slice_archive_name(core_id: &str, os: &str) -> Option<String> {
    if !is_plain_relative(Path::new(core_id)) || !is_plain_relative(Path::new(os)) {
        return None;
    }
    Some(format!("{core_id}-{os}.tar.gz"))
}

/// Forward-slash relative artefact path for a slice: `slices/<core_id>-<os>.tar.gz`.
/// `None` for the same unsafe `core_id`/`os` shapes `slice_archive_name` rejects.
pub fn slice_artefact_rel(core_id: &str, os: &str) -> Option<String> {
    slice_archive_name(core_id, os).map(|name| format!("{SLICES_DIR}/{name}"))
}

/// Forward-slash relative artefact path for a helper firmware: `helper-mcus/<basename>`.
pub fn helper_artefact_rel(basename: &str) -> String {
    format!("{HELPERS_DIR}/{basename}")
}

/// A slice is bundled iff it built successfully (`status == "ok"`).
pub fn slice_should_bundle(status: &str) -> bool {
    status == "ok"
}

/// Assemble the bundle manifest from the collected entries + verbatim passthrough
/// fields. Pure: no IO.
pub fn assemble_bundle_manifest(
    hw_info: serde_yaml::Value,
    boot_order: Vec<serde_yaml::Value>,
    slices: Vec<BundleSliceEntry>,
    helper_mcus: Vec<BundleHelperEntry>,
) -> BundleManifest {
    BundleManifest {
        schema_version: BUNDLE_SCHEMA_VERSION,
        generated_by: GENERATED_BY.to_string(),
        hw_info,
        slices,
        helper_mcus,
        boot_order,
    }
}

/// Extract the manifest's `hw_info` (default `{}`) + `boot_order` (default `[]`)
/// as raw `serde_yaml::Value`s straight off the YAML bytes — bypassing the typed
/// `SystemManifest` so additive schema keys pass through unchanged. Never fails:
/// unparseable/non-mapping input yields the empty defaults. Pure: no IO.
pub fn raw_passthrough(yaml: &str) -> (serde_yaml::Value, Vec<serde_yaml::Value>) {
    let root: serde_yaml::Value = serde_yaml::from_str(yaml).unwrap_or(serde_yaml::Value::Null);
    let hw_info = root
        .get("hw_info")
        .cloned()
        .unwrap_or_else(|| serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
    let boot_order = root
        .get("boot_order")
        .and_then(|v| v.as_sequence().cloned())
        .unwrap_or_default();
    (hw_info, boot_order)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slice_archive_name_joins_core_and_os() {
        assert_eq!(
            slice_archive_name("m55_hp", "zephyr"),
            Some("m55_hp-zephyr.tar.gz".to_string())
        );
    }

    #[test]
    fn slice_artefact_rel_is_forward_slash() {
        assert_eq!(
            slice_artefact_rel("m55_hp", "zephyr"),
            Some("slices/m55_hp-zephyr.tar.gz".to_string())
        );
        // No backslash ever, even conceptually on Windows.
        assert!(
            !slice_artefact_rel("m55_hp", "zephyr")
                .unwrap()
                .contains('\\')
        );
    }

    #[test]
    fn slice_archive_name_rejects_path_escapes() {
        // `..` traversal.
        assert_eq!(slice_archive_name("../../../../home/u/x", "zephyr"), None);
        assert_eq!(slice_archive_name("m55_hp", "../escape"), None);
        // POSIX-absolute (also rejected on Windows, unlike raw `is_absolute()`).
        assert_eq!(slice_archive_name("/etc/passwd", "zephyr"), None);
        // Same guard reaches the artefact-path builder.
        assert_eq!(slice_artefact_rel("../escape", "zephyr"), None);
        // Ordinary identifiers still pass.
        assert!(slice_archive_name("m55_hp", "zephyr").is_some());
    }

    #[cfg(windows)]
    #[test]
    fn slice_archive_name_rejects_windows_rooted_and_drive_relative() {
        // `\x` and `C:x` are NOT `is_absolute()` and carry no `ParentDir` —
        // exactly the shapes the old `is_absolute() || ParentDir` guard
        // pattern let through while still escaping a `.join()`ed base.
        assert_eq!(slice_archive_name(r"\windows\system32", "zephyr"), None);
        assert_eq!(slice_archive_name("C:foo", "zephyr"), None);
    }

    #[test]
    fn helper_artefact_rel_is_forward_slash() {
        assert_eq!(
            helper_artefact_rel("gd32_bridge.bin"),
            "helper-mcus/gd32_bridge.bin"
        );
    }

    #[test]
    fn only_ok_slices_bundle() {
        assert!(slice_should_bundle("ok"));
        for s in ["pending", "failed", "skipped", ""] {
            assert!(!slice_should_bundle(s), "status {s:?} must not bundle");
        }
    }

    #[test]
    fn manifest_serializes_in_exact_key_order() {
        let m = assemble_bundle_manifest(
            serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
            Vec::new(),
            Vec::new(),
            Vec::new(),
        );
        let json = serde_json::to_string(&m).unwrap();
        assert_eq!(
            json,
            r#"{"schema_version":1,"generated_by":"tan image","hw_info":{},"slices":[],"helper_mcus":[],"boot_order":[]}"#
        );
    }

    #[test]
    fn empty_collections_serialize_as_present_arrays() {
        let m = assemble_bundle_manifest(
            serde_yaml::Value::Mapping(serde_yaml::Mapping::new()),
            Vec::new(),
            Vec::new(),
            Vec::new(),
        );
        let json = serde_json::to_string(&m).unwrap();
        assert!(json.contains(r#""slices":[]"#));
        assert!(json.contains(r#""helper_mcus":[]"#));
    }

    #[test]
    fn hw_info_passthrough_keeps_eeprom_and_unknown_keys() {
        // The typed HwInfo drops `eeprom` + additive keys; raw passthrough must not.
        let yaml = "\
schema_version: 1
hw_info:
  sku: E1M-AEN701
  eeprom:
    magic: 0xA1
  future_key: keep-me
boot_order:
- m55_hp
- m55_he
";
        let (hw_info, boot_order) = raw_passthrough(yaml);
        let m = assemble_bundle_manifest(hw_info, boot_order, Vec::new(), Vec::new());
        let json = serde_json::to_string(&m).unwrap();
        assert!(json.contains("eeprom"), "{json}");
        assert!(json.contains("future_key"), "{json}");
        assert!(
            json.contains(r#""boot_order":["m55_hp","m55_he"]"#),
            "{json}"
        );
    }

    #[test]
    fn raw_passthrough_defaults_on_missing_or_bad_input() {
        let (hw, bo) = raw_passthrough("");
        assert_eq!(hw, serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
        assert!(bo.is_empty());
        let (hw2, bo2) = raw_passthrough("schema_version: 1\nslices: []\n");
        assert_eq!(hw2, serde_yaml::Value::Mapping(serde_yaml::Mapping::new()));
        assert!(bo2.is_empty());
    }

    #[test]
    fn helper_entry_with_null_name_serializes_name_null() {
        let e = BundleHelperEntry {
            name: None,
            chip: Some("cc3501e".to_string()),
            artefact: helper_artefact_rel("fw.bin"),
            sha256: "deadbeef".to_string(),
            size: 4,
        };
        let json = serde_json::to_string(&e).unwrap();
        // Locks the chosen field name (`chip`, not `role`) + the null-name shape.
        assert_eq!(
            json,
            r#"{"name":null,"chip":"cc3501e","artefact":"helper-mcus/fw.bin","sha256":"deadbeef","size":4}"#
        );
    }
}
