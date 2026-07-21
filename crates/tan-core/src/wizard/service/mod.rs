// SPDX-License-Identifier: Apache-2.0
//! Template registry, file-content generators, and planning logic.

mod c_project;
mod example_catalog;
mod host_tooling;
mod module_scaffold;
mod plan;
mod registry;

pub use c_project::{app_core_for_sku, infer_runtime_for_core_id};
pub use example_catalog::{
    example_description_from_readme, example_id_from_source_dir, example_title_from_readme,
    retarget_board_yaml_som,
};
pub use plan::{
    create_module_scaffold_plan, create_scaffold_tree_preview, create_wizard_plan,
    create_wizard_plan_with_cores, normalize_module_name,
};
pub use registry::{list_module_templates, list_wizard_templates};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wizard::models::{
        ModuleScaffoldInput, ModuleTemplateId, WizardPlan, WizardPlanInput, WizardPlannedFile,
        WizardTemplateId,
    };

    #[test]
    fn normalize_normal_name() {
        assert_eq!(normalize_module_name("my_sensor").unwrap(), "my_sensor");
    }

    #[test]
    fn normalize_name_with_spaces() {
        assert_eq!(
            normalize_module_name("My Sensor Driver").unwrap(),
            "my_sensor_driver"
        );
    }

    #[test]
    fn normalize_empty_name_errors() {
        assert!(normalize_module_name("").is_err());
        assert!(normalize_module_name("   ").is_err());
        assert!(normalize_module_name("---").is_err());
    }

    #[test]
    fn list_templates_has_seven_entries() {
        assert_eq!(list_wizard_templates().len(), 7);
    }

    #[test]
    fn zephyr_app_scaffold_is_west_buildable() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::ZephyrApp,
            project_name: "zdemo".to_string(),
            destination: ".".to_string(),
            som_sku: Some("E1M-AEN701".to_string()),
        });
        let by_path = |p: &str| {
            plan.files
                .iter()
                .find(|f| f.relative_path == p)
                .map(|f| f.content.as_str())
        };
        let cmake = by_path("CMakeLists.txt").expect("CMakeLists.txt is generated");
        assert!(cmake.contains("find_package(Zephyr REQUIRED"));
        assert!(cmake.contains("--emit zephyr-conf"));
        assert!(cmake.contains("EXTRA_CONF_FILE"));
        assert!(!cmake.contains("OVERLAY_CONFIG"));
        assert!(cmake.contains("target_sources(app PRIVATE src/main.c)"));
        // Zephyr wires target_sources directly -- no plain-CMake src/CMakeLists.txt.
        assert!(by_path("src/CMakeLists.txt").is_none());
        assert!(by_path("src/main.c").is_some());
        assert!(by_path("board.yaml").is_some());
    }

    #[test]
    fn list_module_templates_has_four_entries() {
        assert_eq!(list_module_templates().len(), 4);
    }

    #[test]
    fn wizard_plan_minimal_generates_expected_files() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::MinimalApp,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: None,
        });
        let paths: Vec<&str> = plan
            .files
            .iter()
            .map(|f| f.relative_path.as_str())
            .collect();
        assert!(paths.contains(&"board.yaml"));
        assert!(paths.contains(&"src/main.c"));
        assert!(paths.contains(&"CMakeLists.txt"));
    }

    fn board_yaml_of(plan: &WizardPlan) -> &str {
        plan.files
            .iter()
            .find(|f| f.relative_path == "board.yaml")
            .map(|f| f.content.as_str())
            .expect("board.yaml is generated")
    }

    #[test]
    fn som_sku_defaults_when_unset() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::MinimalApp,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: None,
        });
        assert!(board_yaml_of(&plan).contains("sku: E1M-AEN701"));
    }

    #[test]
    fn som_sku_overrides_board_yaml() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::SensorStarter,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: Some("E1M-V2N101".to_string()),
        });
        let board = board_yaml_of(&plan);
        assert!(board.contains("sku: E1M-V2N101"));
        assert!(!board.contains("E1M-AEN701"));
    }

    #[test]
    fn cores_scaffold_emits_companion_core_and_ipc() {
        let plan = create_wizard_plan_with_cores(
            &WizardPlanInput {
                template_id: WizardTemplateId::MinimalApp,
                project_name: String::new(),
                destination: ".".to_string(),
                som_sku: Some("E1M-V2N101".to_string()),
            },
            &[
                ("m33_sm".to_string(), "zephyr".to_string()),
                ("a55_cluster".to_string(), "yocto".to_string()),
            ],
        );
        let board = board_yaml_of(&plan);
        // App (Cortex-M) core + the Yocto companion + a default RPMsg channel.
        assert!(board.contains("  m33_sm:"));
        assert!(board.contains("  a55_cluster:"));
        assert!(board.contains("os: yocto"));
        assert!(board.contains("image: alp-image-edge"));
        assert!(board.contains("ipc:"));
        assert!(board.contains("name: alp_default_rpmsg"));
        assert!(board.contains("endpoints: [m33_sm, a55_cluster]"));
    }

    #[test]
    fn default_plan_is_single_core_no_ipc() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::MinimalApp,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: Some("E1M-V2N101".to_string()),
        });
        let board = board_yaml_of(&plan);
        assert!(!board.contains("ipc:"));
        assert!(!board.contains("a55_cluster"));
    }

    #[test]
    fn zephyr_companion_boots_stock_shim_not_the_app_dir() {
        let plan = create_wizard_plan_with_cores(
            &WizardPlanInput {
                template_id: WizardTemplateId::MinimalApp,
                project_name: String::new(),
                destination: ".".to_string(),
                som_sku: Some("E1M-AEN701".to_string()),
            },
            &[
                ("m55_hp".to_string(), "zephyr".to_string()),
                ("m55_he".to_string(), "zephyr".to_string()),
            ],
        );
        let board = board_yaml_of(&plan);
        // Only the app core builds from ./src; a Zephyr companion gets no
        // `app:` (it boots the SDK's stock shim) — two cores must never share
        // one source dir.
        assert_eq!(board.matches("app: ./src").count(), 1);
        assert!(board.contains("  m55_he:\n    os: zephyr"));
        assert!(board.contains("endpoints: [m55_hp, m55_he]"));
    }

    #[test]
    fn off_companion_is_never_an_ipc_endpoint() {
        let plan = create_wizard_plan_with_cores(
            &WizardPlanInput {
                template_id: WizardTemplateId::MinimalApp,
                project_name: String::new(),
                destination: ".".to_string(),
                som_sku: Some("E1M-AEN701".to_string()),
            },
            &[
                ("m55_hp".to_string(), "zephyr".to_string()),
                ("m55_he".to_string(), "off".to_string()),
            ],
        );
        let board = board_yaml_of(&plan);
        assert!(board.contains("  m55_he:\n    os: off"));
        // No active companion -> no default IPC channel at all.
        assert!(!board.contains("ipc:"));
    }

    #[test]
    fn runtime_inference_keys_on_silicon_class_word_starts() {
        assert_eq!(infer_runtime_for_core_id("a55_cluster"), "yocto");
        assert_eq!(infer_runtime_for_core_id("a32_cluster"), "yocto");
        assert_eq!(infer_runtime_for_core_id("m33_sm"), "zephyr");
        // 'a' not followed by a digit is NOT Cortex-A (the old starts_with('a')
        // heuristic misclassified ids like this as yocto).
        assert_eq!(infer_runtime_for_core_id("audio_dsp"), "zephyr");
    }

    #[test]
    fn board_yaml_conforms_to_v06_schema_shape() {
        // Schema-sanctioned top-level keys (board.schema.json, additionalProperties:false).
        const ALLOWED_TOP: &[&str] = &[
            "name",
            "description",
            "preset",
            "hw_rev",
            "som",
            "cores",
            "populated",
            "e1m_routes",
            "pins",
            "ipc",
            "diagnostics",
            "storage",
            "security",
            "boot",
            "ota",
            "chips",
            "features",
            "libraries",
        ];

        // One per SoM family: the app core must match the family's topology.
        let cases = [
            (WizardTemplateId::IotStarter, None, "m55_hp"),
            (
                WizardTemplateId::EdgeAiStarter,
                Some("E1M-V2N101".to_string()),
                "m33_sm",
            ),
            (
                WizardTemplateId::BoardDiagnostics,
                Some("E1M-NX9101".to_string()),
                "m33",
            ),
        ];

        for (template_id, som_sku, want_core) in cases {
            let plan = create_wizard_plan(&WizardPlanInput {
                template_id,
                project_name: String::new(),
                destination: ".".to_string(),
                som_sku,
            });
            let yaml = board_yaml_of(&plan);
            let doc: serde_yaml::Value =
                serde_yaml::from_str(yaml).expect("generated board.yaml parses as YAML");
            let map = doc.as_mapping().expect("board.yaml is a mapping");

            // Required keys present; pre-v0.6 keys gone.
            assert!(map.contains_key("som"), "missing som:\n{yaml}");
            assert!(map.contains_key("cores"), "missing cores:\n{yaml}");
            // `libraries` is a schema-sanctioned TOP-LEVEL key (board.schema.json,
            // oneOf[string, {name, cores}]); the wizard emits it there, not under
            // a core (finding H7). It is intentionally absent from this list.
            for forbidden in ["schema_version", "carrier", "os", "iot", "inference"] {
                assert!(
                    !map.contains_key(forbidden),
                    "forbidden top-level `{forbidden}`:\n{yaml}"
                );
            }
            for key in map.keys() {
                let key = key.as_str().unwrap_or_default();
                assert!(
                    ALLOWED_TOP.contains(&key),
                    "non-schema top-level key `{key}`:\n{yaml}"
                );
            }

            // cores: non-empty, contains the family's app core.
            let cores = map
                .get("cores")
                .and_then(|c| c.as_mapping())
                .expect("cores is a mapping");
            assert!(!cores.is_empty(), "cores must be non-empty:\n{yaml}");
            assert!(
                cores.contains_key(want_core),
                "expected app core `{want_core}`:\n{yaml}"
            );
        }
    }

    #[test]
    fn scaffold_preview_sorted_order() {
        let files = vec![
            WizardPlannedFile {
                relative_path: "src/main.c".to_string(),
                content: String::new(),
            },
            WizardPlannedFile {
                relative_path: "board.yaml".to_string(),
                content: String::new(),
            },
        ];
        let tree = create_scaffold_tree_preview(&files);
        assert!(tree.starts_with(".\n"));
        assert!(tree.contains("|-- board.yaml"));
        assert!(tree.contains("`-- src/main.c"));
    }

    #[test]
    fn module_scaffold_plan_normalizes_and_generates() {
        let plan = create_module_scaffold_plan(&ModuleScaffoldInput {
            template_id: ModuleTemplateId::SensorDriver,
            module_name: "My Sensor".to_string(),
            destination: ".".to_string(),
        })
        .unwrap();
        assert_eq!(plan.normalized_name, "my_sensor");
        assert_eq!(plan.files.len(), 3);
    }

    #[test]
    fn module_scaffold_invalid_name_errors() {
        let result = create_module_scaffold_plan(&ModuleScaffoldInput {
            template_id: ModuleTemplateId::DiagnosticsCheck,
            module_name: "---".to_string(),
            destination: ".".to_string(),
        });
        assert!(result.is_err());
    }

    #[test]
    fn iot_starter_has_env_example() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::IotStarter,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: None,
        });
        let paths: Vec<&str> = plan
            .files
            .iter()
            .map(|f| f.relative_path.as_str())
            .collect();
        assert!(paths.contains(&"config/iot.env.example"));
    }

    #[test]
    fn host_tooling_starter_generates_ts_files() {
        let plan = create_wizard_plan(&WizardPlanInput {
            template_id: WizardTemplateId::HostToolingStarter,
            project_name: String::new(),
            destination: ".".to_string(),
            som_sku: None,
        });
        let paths: Vec<&str> = plan
            .files
            .iter()
            .map(|f| f.relative_path.as_str())
            .collect();
        assert!(paths.contains(&"package.json"));
        assert!(paths.contains(&"src/extension.ts"));
        assert!(paths.contains(&"packages/core/src/index.ts"));
    }
}
