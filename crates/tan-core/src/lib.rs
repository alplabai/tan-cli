// SPDX-License-Identifier: Apache-2.0
#![doc = include_str!("../README.md")]

/// Default SoM SKU written into a scaffolded `board.yaml` when none is supplied
/// (`alp init` without `--som`). Single source of truth for the literal, shared
/// by the wizard (`wizard::service`) and the CLI's app-core guard.
///
/// Must name a SoM with a QUALIFIED board tree in alp-sdk, or the very first
/// `tan build` after `tan init` fails on a board that doesn't exist. This was
/// `E1M-AEN701` (tan-cli #97), which has only two loose overlays
/// (`zephyr/boards/alp_e1m_aen701_m55_{he,hp}.overlay`) and no board tree —
/// so its `m55_he` slice died with `No board named 'alp_e1m_aen701_m55_he'
/// found`. `E1M-AEN801` is the lead part and the only AEN SKU with BOTH
/// `zephyr/boards/alp/e1m_aen801_m55_he` and `…_m55_hp`. AEN701 remains a
/// perfectly valid `--som` value; it is just not a sane out-of-the-box default.
pub const DEFAULT_SOM_SKU: &str = "E1M-AEN801";

pub mod bootstrap;
pub mod build_plan;
pub mod build_readiness;
pub mod clean;
pub mod clock;
pub mod debug;
pub mod debug_launch;
pub mod diff;
pub mod flash;
pub mod host_env;
pub mod image_bundle;
pub mod kconfig;
pub mod loader;
pub mod model;
pub mod path_guard;
pub mod pinmux;
pub mod plan_exec;
pub mod plan_tokens;
pub mod preflight;
pub mod presets;
pub mod preview;
pub mod project;
pub mod proxy;
pub mod renode;
pub mod run;
pub mod runners;
pub mod sdk;
pub mod sdk_catalogue;
pub mod size;
pub mod system_manifest;
pub mod validate;
pub mod west_config;
pub mod wizard;

pub use build_plan::{
    BUILD_PLAN_SCHEMA_VERSION, Backend, BuildPlan, BuildPlanError, BuildSlice, ExecutionPolicy,
    GeneratedFile, PlanWarning, PolicyAction, ToolStep, parse_build_plan, summarize_plan,
};
pub use build_readiness::{
    BuildOs, BuildReadinessReport, BuildToolProbe, board_os_set, build_readiness_report,
};
pub use clean::{
    CleanAction, CleanPlan, RejectedTarget, TargetOrigin, classify as clean_classify,
    clean_targets, is_under,
};
pub use clock::format_iso8601_utc;
pub use debug::{
    DebugGenerationTraceDecision, DebugResolvedValue, DebugRuntimeCapabilities, DebugServerKind,
    DebugTargetKind, DebugTraceOutcome, DebugValueSource, DebugWorkspaceContext,
    DebuggerExtensionsState, DoctorCheck, DoctorReport, DoctorStatus, DoctorSummary,
    append_doctor_check, build_doctor_report, collect_resolved_values,
    collect_runtime_capabilities_from_commands, create_debug_workspace_context,
    is_server_supported_for_target, parse_server_kind, parse_target_kind, prepend_doctor_checks,
    server_choices_for_target, unique_next_steps,
};
pub use debug_launch::{
    LaunchJsonWritePlan, LaunchResolution, apply_launch_resolution, create_launch_draft,
    create_launch_json_write_plan, is_unresolved_placeholder, launch_preview_document,
    launch_preview_notes,
};
pub use diff::{DiffEntry, DiffKind, collect_diff_entries, prune_nulls};
pub use flash::{
    BackendKind, FlashBackendMeta, FlashInputs, FlashKind, FlashPlan, FlashTarget, ToolGate,
    backend_for, jlink_commander_script, plan_baremetal_cmake_flash, plan_flash_targets,
    plan_swd_probe, plan_xspi_flashwriter, plan_yocto_wic, plan_zephyr_west_flash, registry_keys,
    resolve_artefact_path, tool_gate,
};
pub use host_env::{HostEnvProbe, ZEPHYR_SDK_HOSTS, host_environment_checks};
pub use image_bundle::{
    BundleHelperEntry, BundleManifest, BundleSliceEntry, assemble_bundle_manifest,
    helper_artefact_rel, raw_passthrough, slice_archive_name, slice_artefact_rel,
    slice_should_bundle,
};
pub use kconfig::{
    KCONFIG_SCHEMA_VERSION, KconfigData, KconfigError, KconfigSymbol, parse_kconfig,
    resolve_default_kconfig_core,
};
pub use loader::{
    ALL_EMIT_MODES, GenerationTargetSupport, LoaderPlan, create_loader_plan,
    generation_target_support, list_generation_target_support, zephyr_board_dir_name,
};
pub use model::{BoardModel, normalize_board_model};
pub use path_guard::{is_plain_relative, is_unsafe_removal_target, normalize as normalize_path};
pub use pinmux::{PinmuxPad, PinmuxTable, parse_pinmux_table, pinmux_family_for_sku};
pub use preflight::{
    PreflightInput, build_preflight_checks, preflight_blocked, preflight_next_steps,
    preflight_summary,
};
pub use presets::{PresetCatalogueDefaults, empty_preset_catalogue};
pub use preview::{EffectiveConfigPreviewPayload, create_effective_config_preview_payload};
pub use project::{
    ProjectContext, ProjectResolutionInput, ProjectSettings, discover_workspace_sdk,
    nearest_ancestor_sdk, resolve_project_context,
};
pub use proxy::select_https_proxy;
pub use renode::{
    FAMILY_TOKEN_TO_PLATFORM, RenodeError, build_renode_argv, elf_vector_table_base,
    platform_files_for_sku, platform_stem_for_sku, renode_rejected_argv, select_sku, sku_family,
    soc_family_token, zephyr_elf_from_manifest,
};
// `NATIVE_SIM_EXE` is deliberately NOT re-exported: it is `pub(crate)` in
// `run`, so `native_sim_exe_beside{,_path}` are the only ways to name the
// native_sim runnable. A third caller hand-rolling `parent().join(…)` is the
// drift that put an unlaunchable `zephyr.elf` in `debug-config`'s `program`.
pub use run::{
    NATIVE_SIM_BOARD, RunAction, decide_run_action, native_sim_exe_beside,
    native_sim_exe_beside_path, native_sim_slice,
};
pub use sdk::{
    GITHUB_RELEASES_URL, SdkReadinessReport, SdkReadinessState, SdkRelease, SdkSourceTier,
    check_sdk_readiness, describe_network_error, parse_remote_sdk_releases, resolve_active_sdk,
    resolve_global_default_sdk, resolve_sdk_source_tier, resolve_sdk_version_root,
};
pub use sdk_catalogue::{
    AcceleratorAvail, BoardPreset, ChipChoice, ChipDef, ChipKconfig, I2cDevice, MemorySpec,
    PadRoute, SdkCatalogue, SocCore, SocSpec, SomPreset, TopologyCore, accelerator_availability,
    boards_for_som, chip_defaults, chip_family_for_sku, chips_for_som, core_ids_for_som,
    effective_chip_choices, effective_populated, parse_board_preset, parse_chip_def,
    parse_soc_spec, parse_som_preset,
};
pub use size::{
    MemoryBudget, SliceSize, SocVariant, WARN_FRACTION, build_size_report, classify, core_token,
    footprint_total, human_bytes, over_budget_rows, parse_berkeley_size, region_cell, region_json,
    render_table_lines, resolve_budget, resolve_variant, round1, unknown_budget_rows,
};
pub use validate::{
    Outcome, ParseError, Severity, ValidationIssue, ValidationResult, ValidatorExecution,
    analyze_validation_result, classify_validation_outcome, parse_board_model,
    validate_board_yaml_local,
};
pub use west_config::{
    ManifestReconcile, describe_reconcile_failure, get_manifest_path, set_manifest_path,
    workspace_needs_bootstrap,
};
