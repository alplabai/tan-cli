// SPDX-License-Identifier: Apache-2.0
//! Generation-target catalog — a port of the TS loader
//! `GENERATION_TARGET_CATALOG` / `listGenerationTargetSupport`. Static metadata
//! describing the four emit targets (used by `explain`, and later `generate`).

/// Static metadata for one generation/emit target (Zephyr conf, DTS overlay, etc.).
pub struct GenerationTargetSupport {
    /// Emit-mode key (e.g. `zephyr-conf`); the stable identifier used for lookup.
    pub emit: &'static str,
    /// Human-readable name shown in UI.
    pub display_name: &'static str,
    /// Output path relative to the workspace root.
    pub output_relative_path: &'static str,
    /// Label for a preview pane of the generated output.
    pub preview_label: &'static str,
    /// Editor/VS Code language id for syntax-highlighting the preview.
    pub preview_language_id: &'static str,
}

static GENERATION_TARGET_CATALOG: &[GenerationTargetSupport] = &[
    GenerationTargetSupport {
        emit: "zephyr-conf",
        display_name: "Zephyr config",
        output_relative_path: "build/generated/alp.conf",
        preview_label: "Zephyr config preview",
        preview_language_id: "properties",
    },
    GenerationTargetSupport {
        emit: "dts-overlay",
        display_name: "Devicetree overlay",
        output_relative_path: "build/generated/alp.overlay",
        preview_label: "Devicetree overlay preview",
        preview_language_id: "dts",
    },
    GenerationTargetSupport {
        emit: "cmake-args",
        display_name: "CMake args",
        output_relative_path: "build/generated/alp-cmake-args.txt",
        preview_label: "CMake args preview",
        preview_language_id: "plaintext",
    },
    GenerationTargetSupport {
        emit: "yocto-conf",
        display_name: "Yocto config",
        output_relative_path: "build/generated/alp-yocto.conf",
        preview_label: "Yocto config preview",
        preview_language_id: "properties",
    },
];

/// Returns the full catalog of generation targets, in fixed catalog order.
pub fn list_generation_target_support() -> &'static [GenerationTargetSupport] {
    GENERATION_TARGET_CATALOG
}

/// `som.sku` + core id -> the SDK's `--emit zephyr-board` directory name.
/// Mirrors `gen_zephyr_board._board_dir_name` verbatim: `E1M-AEN801` +
/// `m55_hp` -> `alp_e1m_aen801_m55_hp`. `alp_project.py` keys every generated
/// file under exactly this name and strips it before joining onto `--output`
/// (`docs/porting-new-som.md` Step 7 pairs `--output
/// build/boards/alp_e1m_aen901_m55_hp/` with `--board-root build/boards`), so
/// `--output` must BE this directory -- a bare core id collides across SoMs
/// that share a core id (tan-cli#116 review).
///
/// `None` when `sku` does not start with `E1M-` (mirrors the SDK's own
/// `unrecognised SKU prefix` guard).
pub fn zephyr_board_dir_name(sku: &str, core_id: &str) -> Option<String> {
    let slug = sku.strip_prefix("E1M-")?.to_lowercase();
    Some(format!("alp_e1m_{slug}_{core_id}"))
}

/// Looks up a target by its `emit` key; `None` if no target matches.
pub fn generation_target_support(emit: &str) -> Option<&'static GenerationTargetSupport> {
    GENERATION_TARGET_CATALOG.iter().find(|t| t.emit == emit)
}

/// The four emit modes, in catalog order (mirrors TS `ALL_EMIT_MODES`).
pub const ALL_EMIT_MODES: [&str; 4] = ["zephyr-conf", "dts-overlay", "cmake-args", "yocto-conf"];

/// The output path + command line a loader run would use (mirror of TS
/// `createLoaderPlan`, limited to the fields `trace`/`support-bundle` surface).
/// `emit` must be a valid target; paths are joined as given (callers pass
/// resolved roots).
pub struct LoaderPlan {
    /// Absolute output path (workspace root joined with the target's relative path).
    pub output_path: String,
    /// Full python invocation that would run the loader script.
    pub command_line: String,
}

/// Builds the `LoaderPlan` for `target`: resolves the output path under
/// `workspace_root` and formats the `alp_project.py` command line.
pub fn create_loader_plan(
    workspace_root: &str,
    sdk_root: &str,
    board_yaml_path: &str,
    python_binary: &str,
    target: &GenerationTargetSupport,
) -> LoaderPlan {
    let output_path = std::path::Path::new(workspace_root)
        .join(target.output_relative_path)
        .to_string_lossy()
        .to_string();
    let script_path = std::path::Path::new(sdk_root)
        .join("scripts")
        .join("alp_project.py")
        .to_string_lossy()
        .to_string();
    let command_line = format!(
        "{python_binary} {script_path} --input {board_yaml_path} --emit {} --output {output_path}",
        target.emit
    );
    LoaderPlan {
        output_path,
        command_line,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_lists_four_targets_in_order() {
        let emits: Vec<&str> = list_generation_target_support()
            .iter()
            .map(|t| t.emit)
            .collect();
        assert_eq!(
            emits,
            ["zephyr-conf", "dts-overlay", "cmake-args", "yocto-conf"]
        );
    }

    #[test]
    fn lookup_by_emit() {
        assert_eq!(
            generation_target_support("cmake-args").map(|t| t.display_name),
            Some("CMake args")
        );
        assert!(generation_target_support("bogus").is_none());
    }

    #[test]
    fn zephyr_board_dir_name_matches_the_sdk_convention() {
        assert_eq!(
            zephyr_board_dir_name("E1M-AEN801", "m55_hp").as_deref(),
            Some("alp_e1m_aen801_m55_hp")
        );
        // Two different SKUs sharing a core id must never collide: the
        // FAILING case this whole function exists to prevent (tan-cli#116
        // review finding 1).
        assert_ne!(
            zephyr_board_dir_name("E1M-AEN801", "m55_hp"),
            zephyr_board_dir_name("E1M-AEN901", "m55_hp"),
        );
    }

    #[test]
    fn zephyr_board_dir_name_rejects_a_non_e1m_sku() {
        assert_eq!(zephyr_board_dir_name("BOGUS-SKU", "m55_hp"), None);
    }
}
