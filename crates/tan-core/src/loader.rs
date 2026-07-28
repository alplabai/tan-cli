// SPDX-License-Identifier: Apache-2.0
//! Generation-target catalog — a port of the TS loader
//! `GENERATION_TARGET_CATALOG` / `listGenerationTargetSupport`. Static metadata
//! describing every `tan generate --target` output (used by `explain`, and by
//! `trace`/`support-bundle` via [`BUILD_CONFIG_EMIT_MODES`] +
//! [`create_loader_plan`]).
//!
//! tan-cli#165: this used to list only four of the (then) nine `generate`
//! targets, so `tan explain --target <mode>` silently failed to describe five
//! real targets -- including two shipped in the immediately preceding PR.
//! `generate.rs` (`crates/tan-cli/src/commands/generate.rs`) used to keep its
//! OWN, second `ALL_EMIT_MODES`/output-path table, which is exactly how the
//! two drifted apart with nothing to catch it. Fixed by unifying the
//! direction of derivation the OTHER way round from what a naive merge would
//! do: this catalog (plus [`ALL_EMIT_MODES`], now the sole copy `generate.rs`
//! reads) is the single source of every target's `emit` key + output path;
//! `generate.rs`'s `output_path_for_emit` reads [`generation_target_support`]
//! instead of hand-duplicating the file-name table. What does NOT unify:
//! `generate.rs` alone owns `CORE_SCOPABLE_TARGETS` (which targets forward
//! `--core` to `alp_project.py`) and the `zephyr-board`
//! output-directory-naming dance -- both are argv-composition concerns
//! specific to spawning the loader, not "what is this target" facts
//! `explain`/`trace` need.
//!
//! tan-cli#165 review finding 1: an earlier revision of this fix pointed
//! `trace`/`support-bundle` at [`ALL_EMIT_MODES`] too, on the theory that "the
//! set explain/trace/generate read" was one thing. It is not: `trace.rs`/
//! `cli.rs`'s own (untouched) docs describe `tan trace` as reporting "the
//! generation decisions a build would make", and `tan build` only ever
//! materialises the four per-core build-config artefacts
//! (`build_plan.rs`'s `configArtefacts`) -- never the project-level exports
//! (`carrier-netlist`, `west-libraries`, `hw-info-h`, `os-topology`,
//! `native-sim-overlay`) that make up the other five of
//! [`ALL_EMIT_MODES`]'s nine. Pointing trace's default/validation set at all
//! nine silently rescoped it to claim a build runs exports it never runs.
//! [`BUILD_CONFIG_EMIT_MODES`] restores the narrower set `generate.rs`
//! carried before #165 (its deleted comment: "none is in
//! `tan_core::ALL_EMIT_MODES` ... because those model the *build* generation
//! a slice runs") as trace/support-bundle's own constant, independent of
//! `generate`'s full discovery surface.
//!
//! `zephyr-board` (tan-cli#116) is the one target that does not fit a single
//! `output_relative_path` string at all: it writes a DIRECTORY of files,
//! named per SoM SKU + core (`alp_e1m_<sku-slug>_<core>/`), not a fixed
//! conventional path. It is still listed here (`explain --target
//! zephyr-board` must describe it), but `is_directory: true` marks its
//! `output_relative_path` as a documentary template, never a literal path to
//! join -- and it is deliberately excluded from both [`ALL_EMIT_MODES`] and
//! [`BUILD_CONFIG_EMIT_MODES`] (mirroring `generate.rs`'s own default/`--all`
//! exclusion), so `trace`/`support-bundle` never try to plan a per-SKU/core
//! path they cannot resolve without a real `board.yaml` + `--core`.

/// Static metadata for one generation/emit target (Zephyr conf, DTS overlay, etc.).
pub struct GenerationTargetSupport {
    /// Emit-mode key (e.g. `zephyr-conf`); the stable identifier used for lookup.
    pub emit: &'static str,
    /// Human-readable name shown in UI.
    pub display_name: &'static str,
    /// Output path relative to the workspace root -- a literal, joinable path
    /// for every target except `zephyr-board` (see `is_directory`).
    pub output_relative_path: &'static str,
    /// True when `output_relative_path` is a documentary template for a
    /// per-SKU/core DIRECTORY of files (currently only `zephyr-board`), not a
    /// literal path safe to join onto a workspace root.
    pub is_directory: bool,
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
        is_directory: false,
        preview_label: "Zephyr config preview",
        preview_language_id: "properties",
    },
    GenerationTargetSupport {
        emit: "dts-overlay",
        display_name: "Devicetree overlay",
        output_relative_path: "build/generated/alp.overlay",
        is_directory: false,
        preview_label: "Devicetree overlay preview",
        preview_language_id: "dts",
    },
    GenerationTargetSupport {
        emit: "cmake-args",
        display_name: "CMake args",
        output_relative_path: "build/generated/alp-cmake-args.txt",
        is_directory: false,
        preview_label: "CMake args preview",
        preview_language_id: "plaintext",
    },
    GenerationTargetSupport {
        emit: "yocto-conf",
        display_name: "Yocto config",
        output_relative_path: "build/generated/alp-yocto.conf",
        is_directory: false,
        preview_label: "Yocto config preview",
        preview_language_id: "properties",
    },
    GenerationTargetSupport {
        emit: "native-sim-overlay",
        display_name: "native_sim overlay",
        // Lives in the app's own source tree (Zephyr auto-discovers
        // boards/<board>.overlay), not build/generated/ -- see generate.rs's
        // output_path_for_emit, which this value must match verbatim.
        output_relative_path: "boards/native_sim_native_64.overlay",
        is_directory: false,
        preview_label: "native_sim overlay preview",
        preview_language_id: "dts",
    },
    GenerationTargetSupport {
        emit: "carrier-netlist",
        display_name: "Carrier netlist",
        output_relative_path: "build/generated/carrier-netlist.json",
        is_directory: false,
        preview_label: "Carrier netlist preview",
        preview_language_id: "json",
    },
    GenerationTargetSupport {
        emit: "west-libraries",
        display_name: "West libraries",
        output_relative_path: "build/generated/alp-west-libs.yml",
        is_directory: false,
        preview_label: "West libraries preview",
        preview_language_id: "yaml",
    },
    GenerationTargetSupport {
        emit: "hw-info-h",
        display_name: "Hardware info header",
        output_relative_path: "build/generated/alp_hw_info_build.h",
        is_directory: false,
        preview_label: "Hardware info header preview",
        preview_language_id: "c",
    },
    GenerationTargetSupport {
        emit: "os-topology",
        display_name: "OS topology",
        output_relative_path: "build/generated/os-topology.json",
        is_directory: false,
        preview_label: "OS topology preview",
        preview_language_id: "json",
    },
    GenerationTargetSupport {
        emit: "zephyr-board",
        display_name: "Zephyr board tree",
        // Documentary only (is_directory: true) -- the real name is
        // `alp_e1m_<sku-slug>_<core>/`, resolved per-run by
        // `zephyr_board_dir_name`. Never join this literally onto a
        // workspace root; `generate.rs`'s `output_path_for_emit` special-cases
        // this target rather than reading this field.
        output_relative_path: "build/boards/alp_e1m_<sku-slug>_<core>/",
        is_directory: true,
        preview_label: "Zephyr board tree preview",
        preview_language_id: "plaintext",
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

/// Every `generate` target defaultable by a bare `tan generate` / `--all`.
/// This is the SOLE copy `generate.rs` reads (tan-cli#165) -- `zephyr-board`
/// is deliberately excluded, since it hard-requires `--core` and writes a
/// per-SKU/core directory rather than one fixed file (see
/// `GenerationTargetSupport::is_directory`), so it cannot be defaulted the
/// way these nine can; it stays reachable only via an explicit `--target
/// zephyr-board --core <id>`.
///
/// NOT the set `tan trace`/`tan support-bundle` iterate -- see
/// [`BUILD_CONFIG_EMIT_MODES`] for that narrower, deliberately different set
/// (tan-cli#165 review finding 1).
pub const ALL_EMIT_MODES: [&str; 9] = [
    "zephyr-conf",
    "dts-overlay",
    "native-sim-overlay",
    "cmake-args",
    "yocto-conf",
    "carrier-netlist",
    "west-libraries",
    "hw-info-h",
    "os-topology",
];

/// The four per-core build-config targets a `tan build` slice actually
/// materialises (`build_plan.rs`'s `configArtefacts`, applied by
/// `commands/build/materialise.rs`) -- the set `tan trace` and `tan
/// support-bundle` enumerate by default, and validate an explicit `--target`
/// against (tan-cli#165 review finding 1). Deliberately narrower than
/// [`ALL_EMIT_MODES`]: `trace`/`support-bundle` model "the generation
/// decisions a build would make" (their own module docs, unchanged by
/// #165), not the wider `tan generate --target` discovery surface --
/// `carrier-netlist`/`native-sim-overlay`/`west-libraries`/`hw-info-h`/
/// `os-topology` are real `generate` targets `tan build` never runs.
/// Restores the distinction `generate.rs` carried before #165 (its deleted
/// comment: "none is in `tan_core::ALL_EMIT_MODES` ... because those model
/// the *build* generation a slice runs"), which pointing trace/support-bundle
/// at [`ALL_EMIT_MODES`] instead would silently collapse.
pub const BUILD_CONFIG_EMIT_MODES: [&str; 4] =
    ["zephyr-conf", "dts-overlay", "cmake-args", "yocto-conf"];

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
    fn catalog_lists_ten_targets_in_order() {
        // tan-cli#165: the catalog used to list only four of the nine (now
        // ten, with zephyr-board and os-topology) real generate targets.
        let emits: Vec<&str> = list_generation_target_support()
            .iter()
            .map(|t| t.emit)
            .collect();
        assert_eq!(
            emits,
            [
                "zephyr-conf",
                "dts-overlay",
                "cmake-args",
                "yocto-conf",
                "native-sim-overlay",
                "carrier-netlist",
                "west-libraries",
                "hw-info-h",
                "os-topology",
                "zephyr-board",
            ]
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

    /// tan-cli#165's explicit ask: "if two lists stay, add a gate that FAILS
    /// when they disagree". `ALL_EMIT_MODES` is no longer a second
    /// hand-maintained list for the file-based targets (generate.rs reads
    /// this one directly), but the catalog still carries UI metadata
    /// `ALL_EMIT_MODES` has no notion of, plus the one structural exception
    /// (`zephyr-board`) -- so this asserts the catalog's real target set is
    /// EXACTLY `ALL_EMIT_MODES` plus `zephyr-board`, no more, no less. A
    /// stale/missing catalog entry (this bug's own shape) fails this test
    /// instead of silently shipping.
    #[test]
    fn catalog_matches_all_emit_modes_plus_zephyr_board() {
        let mut catalog_emits: Vec<&str> = list_generation_target_support()
            .iter()
            .map(|t| t.emit)
            .collect();
        catalog_emits.sort_unstable();

        let mut expected: Vec<&str> = ALL_EMIT_MODES.to_vec();
        expected.push("zephyr-board");
        expected.sort_unstable();

        assert_eq!(catalog_emits, expected);
    }

    /// tan-cli#165 review finding 1: `trace`/`support-bundle`'s narrower set
    /// must never silently diverge from -- or grow past -- the wider
    /// `generate` set it is drawn from, and must stay exactly the original
    /// four build-config targets (the FAILING case this guards: a future
    /// edit that widens `BUILD_CONFIG_EMIT_MODES` to match `ALL_EMIT_MODES`
    /// again, silently re-rescoping `tan trace` beyond what a build runs).
    #[test]
    fn build_config_emit_modes_is_the_original_four_and_a_subset_of_all_emit_modes() {
        assert_eq!(
            BUILD_CONFIG_EMIT_MODES,
            ["zephyr-conf", "dts-overlay", "cmake-args", "yocto-conf"]
        );
        for mode in BUILD_CONFIG_EMIT_MODES {
            assert!(
                ALL_EMIT_MODES.contains(&mode),
                "BUILD_CONFIG_EMIT_MODES entry {mode} missing from ALL_EMIT_MODES"
            );
        }
    }

    /// tan-cli#165 review finding 3: `zephyr-board`'s documentary
    /// `output_relative_path` must name the real `alp_e1m_` prefix
    /// [`zephyr_board_dir_name`] writes, not a bare `<sku-slug>` -- the
    /// FAILING case this guards is exactly what shipped: `tan explain
    /// --target zephyr-board` pointing at a directory
    /// (`build/boards/<sku-slug>_<core>/`) that never gets created, because
    /// the real one is `build/boards/alp_e1m_<sku-slug>_<core>/`.
    #[test]
    fn zephyr_board_catalog_path_carries_the_real_alp_e1m_prefix() {
        let entry = generation_target_support("zephyr-board").expect("zephyr-board is catalogued");
        assert_eq!(
            entry.output_relative_path,
            "build/boards/alp_e1m_<sku-slug>_<core>/"
        );
        // Cross-check against the function that actually names the
        // directory, so the two can never silently re-diverge.
        let real = zephyr_board_dir_name("E1M-AEN801", "m55_hp").unwrap();
        assert!(
            entry.output_relative_path.contains("alp_e1m_"),
            "catalog template lost the alp_e1m_ prefix real names like {real:?} carry"
        );
    }

    #[test]
    fn only_zephyr_board_is_marked_directory_shaped() {
        for target in list_generation_target_support() {
            assert_eq!(
                target.is_directory,
                target.emit == "zephyr-board",
                "target={} unexpectedly is_directory={}",
                target.emit,
                target.is_directory
            );
        }
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
