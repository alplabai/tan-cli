// SPDX-License-Identifier: Apache-2.0
//! Materialise a build plan: write every artefact the plan carries under `base`,
//! refusing absolute or `..`-escaping paths. The SDK guarantees these contents
//! match what `west alp-build` would write, so materialising cannot drift.

use std::path::{Component, Path};

use tan_core::build_plan::BuildPlan;

/// Write every artefact the plan carries under `base`. Idempotent byte-writes;
/// refuses absolute or `..`-escaping artefact paths (defensive — we only write
/// inside the project's build tree). The SDK guarantees these contents match
/// what `west alp-build` would write, so materialising cannot drift.
pub(super) fn materialise_plan(
    plan: &BuildPlan,
    base: &Path,
) -> Result<Vec<String>, MaterialiseError> {
    let mut written = Vec::new();
    for f in plan.all_artefacts() {
        let rel = Path::new(&f.path);
        if rel.is_absolute() || rel.components().any(|c| matches!(c, Component::ParentDir)) {
            return Err(MaterialiseError::UnsafePath(f.path.clone()));
        }
        let dest = base.join(rel);
        if let Some(parent) = dest.parent() {
            std::fs::create_dir_all(parent).map_err(|e| MaterialiseError::Io(f.path.clone(), e))?;
        }
        std::fs::write(&dest, &f.contents).map_err(|e| MaterialiseError::Io(f.path.clone(), e))?;
        written.push(f.path.clone());
    }
    Ok(written)
}

/// Failure modes of `materialise_plan`.
#[derive(Debug)]
pub(super) enum MaterialiseError {
    /// Artefact path was absolute or contained `..` (rejected before writing).
    UnsafePath(String),
    /// I/O error while creating dirs or writing the artefact at this path.
    Io(String, std::io::Error),
}

impl MaterialiseError {
    /// Human-readable, single-line description of the failure for the envelope.
    pub(super) fn message(&self) -> String {
        match self {
            MaterialiseError::UnsafePath(p) => {
                format!("refusing to write unsafe artefact path `{p}` (absolute or contains `..`)")
            }
            MaterialiseError::Io(p, e) => format!("failed to write `{p}`: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tan_core::build_plan::parse_build_plan;

    const SAMPLE_PLAN: &str = r#"{
      "schemaVersion": 1,
      "boardYaml": "/p/board.yaml",
      "sku": "E1M-AEN701",
      "buildRoot": "build",
      "slices": [
        { "coreId": "m55_hp", "backend": "zephyr", "buildDir": "build/m55_hp-zephyr",
          "configArtefacts": [{ "path": "build/m55_hp-zephyr/alp.conf", "contents": "CONFIG_GPIO=y\n" }],
          "command": { "tool": "west", "args": ["build"], "cwd": "build/m55_hp-zephyr" } }
      ],
      "sharedArtefacts": [{ "path": "build/generated/alp/system_ipc.h", "contents": "/* ipc */\n" }]
    }"#;

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("{tag}-{}", std::process::id()))
    }

    #[test]
    fn materialise_writes_all_artefacts_and_creates_dirs() {
        let plan = parse_build_plan(SAMPLE_PLAN).unwrap();
        let base = unique_temp_dir("alp-mat-ok");
        let _ = std::fs::remove_dir_all(&base);

        let written = materialise_plan(&plan, &base).expect("materialise should succeed");
        assert_eq!(written.len(), plan.all_artefacts().len());

        // Nested parent dirs were created, and contents byte-match the plan.
        let shared = base.join("build/generated/alp/system_ipc.h");
        assert_eq!(std::fs::read_to_string(&shared).unwrap(), "/* ipc */\n");
        let conf = base.join("build/m55_hp-zephyr/alp.conf");
        assert_eq!(std::fs::read_to_string(&conf).unwrap(), "CONFIG_GPIO=y\n");

        // Idempotent: a second write succeeds (byte-overwrite).
        materialise_plan(&plan, &base).expect("re-materialise should succeed");

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn materialise_refuses_path_traversal() {
        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [],
          "sharedArtefacts": [{ "path": "../escape.txt", "contents": "x" }]
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-mat-unsafe");
        let _ = std::fs::remove_dir_all(&base);

        let err = materialise_plan(&plan, &base).expect_err("must refuse `..`");
        assert!(err.message().contains("unsafe"), "got: {}", err.message());
        // Nothing escaped above base.
        assert!(!base.join("../escape.txt").exists());

        std::fs::remove_dir_all(&base).ok();
    }
}
