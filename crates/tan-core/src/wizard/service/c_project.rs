// SPDX-License-Identifier: Apache-2.0
//! C-project (plain-CMake + Zephyr) file-content generators.

use super::vendored::{vendored_minimal_files, vendored_sensor_files};
use crate::wizard::models::{WizardPlannedFile, WizardTemplateDefinition, WizardTemplateId};

pub(super) fn gen_c_project_files(
    def: &WizardTemplateDefinition,
    som_sku: Option<&str>,
    cores: &[(String, String)],
) -> Vec<WizardPlannedFile> {
    // zephyr-app and sensor-starter are vendored from the SDK's `minimal`/
    // `sensor` scaffold-catalog entries (alp-sdk#864) instead of
    // hand-generated -- see `wizard/vendored/MANIFEST.md`. These are the only
    // templates mapped onto a vendored tree today; every other template below
    // still hand-generates its files (no clean 1:1 SDK catalog equivalent --
    // see the manifest).
    let vendored_files = match def.id {
        WizardTemplateId::ZephyrApp => {
            let sku = som_sku.unwrap_or(crate::DEFAULT_SOM_SKU);
            Some(vendored_minimal_files(sku, cores))
        }
        WizardTemplateId::SensorStarter => {
            let sku = som_sku.unwrap_or(crate::DEFAULT_SOM_SKU);
            Some(vendored_sensor_files(sku, cores))
        }
        _ => None,
    };
    if let Some(vendored_files) = vendored_files {
        return vendored_files
            .into_iter()
            .map(|(relative_path, content)| WizardPlannedFile {
                relative_path,
                content,
            })
            .collect();
    }

    let mut files = vec![
        WizardPlannedFile {
            relative_path: "board.yaml".to_string(),
            content: gen_board_yaml(def, som_sku, cores),
        },
        WizardPlannedFile {
            relative_path: "README.md".to_string(),
            content: gen_readme(def, som_sku),
        },
        WizardPlannedFile {
            relative_path: "prj.conf".to_string(),
            content: gen_prj_conf(def),
        },
        WizardPlannedFile {
            relative_path: "CMakeLists.txt".to_string(),
            content: gen_root_cmake().to_string(),
        },
        WizardPlannedFile {
            relative_path: "src/CMakeLists.txt".to_string(),
            content: gen_src_cmake(def),
        },
        WizardPlannedFile {
            relative_path: "include/app/app.h".to_string(),
            content: gen_app_header().to_string(),
        },
        WizardPlannedFile {
            relative_path: "src/main.c".to_string(),
            content: gen_main_c(def),
        },
    ];

    for spec in def.feature_files {
        files.push(WizardPlannedFile {
            relative_path: spec.path.to_string(),
            content: gen_feature_file(spec.unit_name, spec.todo_line),
        });
    }

    if def.id == WizardTemplateId::IotStarter {
        files.push(WizardPlannedFile {
            relative_path: "config/iot.env.example".to_string(),
            content: gen_iot_env_example().to_string(),
        });
    }

    files
}

/// Canonical Zephyr app-core id for a SoM family, taken from the SoM preset's
/// `topology:`. `tan init` is SDK-free, so this maps by SKU prefix; the value is
/// re-checked against the SoM catalogue by `tan validate` once an SDK resolves.
pub fn app_core_for_sku(sku: &str) -> &'static str {
    if sku.starts_with("E1M-V2N") || sku.starts_with("E1M-V2M") {
        "m33_sm" // Renesas RZ/V2N, RZ/V2M
    } else if sku.starts_with("E1M-NX9") {
        "m33" // NXP
    } else {
        "m55_hp" // Alif Ensemble (E1M-AEN*) + default
    }
}

/// Best-effort runtime for a core id when the topology gives no `board:` /
/// `machine:` hint: a Cortex-A-looking id (`a<digit>` at a word start, e.g.
/// `a55_cluster`) runs Linux (yocto); everything else defaults to Zephyr.
/// Single owner of this heuristic — `tan presets` and `tan init` both use it.
/// KEEP IN SYNC with the IDE configurator's `coreSiliconClass`
/// (`packages/alp-webview/src/features/configurator/ConfiguratorView.tsx`): same
/// `a<digit>`/`m<digit>` word-start test. The one intentional difference is the
/// fallback — here an unrecognized id defaults to `zephyr` (a concrete runtime
/// must be picked), whereas the configurator returns `unknown` to offer all OS
/// options. Covered by the `infer_runtime_*` unit tests below.
pub fn infer_runtime_for_core_id(id: &str) -> &'static str {
    let lower = id.to_lowercase();
    let bytes = lower.as_bytes();
    let mut word_start = true;
    for (i, &b) in bytes.iter().enumerate() {
        if word_start && b == b'a' && bytes.get(i + 1).is_some_and(u8::is_ascii_digit) {
            return "yocto";
        }
        word_start = b == b'_' || b == b'-';
    }
    "zephyr"
}

/// Emit a board.yaml conforming to the SDK board schema (v0.6+): `som` + `cores`
/// are the only required top-level keys; population/OS is per-core. Template
/// connectivity/inference tuning lives under the app core's `core_entry`, but
/// `libraries` is a project-wide, top-level key (`core_entry` only allows
/// `extra_libraries`, additionalProperties: false) mapping each library to the
/// cores that use it; project-wide diagnostics is a sanctioned top-level key.
fn gen_board_yaml(
    def: &WizardTemplateDefinition,
    som_sku: Option<&str>,
    cores: &[(String, String)],
) -> String {
    let sku = som_sku.unwrap_or(crate::DEFAULT_SOM_SKU);
    let core = app_core_for_sku(sku);

    let mut s = String::new();
    s.push_str("# Generated by `tan init`.\n");
    s.push_str("# board.yaml describes hardware: the SoM SKU + per-core app map.\n");
    s.push_str("# Validate it with `tan validate` once an SDK is resolved.\n\n");
    s.push_str("som:\n");
    s.push_str(&format!("  sku: {sku}\n"));
    s.push_str("cores:\n");
    s.push_str(&format!("  {core}:\n"));
    s.push_str("    os: zephyr\n");
    s.push_str("    app: ./src\n");

    if let Some(f) = &def.features {
        s.push_str("    iot:\n");
        s.push_str(&format!("      wifi: {}\n", f.wifi));
        s.push_str(&format!("      mqtt: {}\n", f.mqtt));
        s.push_str(&format!("      ble: {}\n", f.ble));
        s.push_str(&format!("      tls: {}\n", f.tls));
    }

    if def.id == WizardTemplateId::EdgeAiStarter {
        // Backend is silicon-determined (SoM `capabilities:`); only the
        // app-level arena budget is a board.yaml knob.
        s.push_str("    inference:\n");
        s.push_str("      default_arena_kib: 256\n");
    }

    // Companion cores (heterogeneous `--cores`): emit each core other than the
    // app core. A `yocto` (Cortex-A) core boots the stock Linux image; a
    // Zephyr/baremetal companion gets NO `app:` — it boots the SDK's stock
    // shim, since the canonical layout names each app folder after its core ID
    // and the scaffold creates only `./src` (the app core's). With no
    // `--cores` this loop is empty (single-core default).
    for (id, os) in cores {
        if id == core {
            continue;
        }
        s.push_str(&format!("  {id}:\n"));
        s.push_str(&format!("    os: {os}\n"));
        if os == "yocto" {
            s.push_str("    image: alp-image-edge\n");
        }
    }

    if !def.libs.is_empty() {
        s.push_str("\nlibraries:\n");
        for lib in def.libs {
            s.push_str(&format!("  - name: {lib}\n"));
            s.push_str(&format!("    cores: [{core}]\n"));
        }
    }

    // A default RPMsg channel links the app core to its first ACTIVE companion
    // (ipc endpoints must all have `os != off` per the board schema).
    if let Some((companion, _)) = cores.iter().find(|(id, os)| id != core && os != "off") {
        s.push_str("\nipc:\n");
        s.push_str("  - kind: rpmsg\n");
        s.push_str("    name: alp_default_rpmsg\n");
        s.push_str(&format!("    endpoints: [{core}, {companion}]\n"));
        s.push_str("    carve_out_kb: 512\n");
    }

    if def.id == WizardTemplateId::BoardDiagnostics {
        s.push_str("diagnostics:\n");
        s.push_str("  last_error: true\n");
        s.push_str("  log_level: debug\n");
    }

    s
}

fn gen_readme(def: &WizardTemplateDefinition, som_sku: Option<&str>) -> String {
    let sku = som_sku.unwrap_or(crate::DEFAULT_SOM_SKU);
    let mut s = String::new();
    s.push_str("# ALP Starter Project\n\n");
    s.push_str(&format!("Template: {}\n", def.id.as_str()));
    s.push_str(&format!("SoM: {sku}\n"));
    s.push_str(&format!("App core: {} (Zephyr)\n\n", app_core_for_sku(sku)));
    s.push_str("## Generated Starter Notes\n\n");
    for line in def.explanation {
        s.push_str(&format!("- {line}\n"));
    }
    s.push_str("\n## Next Steps\n\n");
    s.push_str("- Run Alp: Validate board.yaml.\n");
    s.push_str("- Run Alp: Generate all to produce derived outputs under build/generated/.\n");
    s.push_str("- Extend source files under src/features/ for your target behavior.\n\n");
    s.push_str("This workspace was generated by Alp: New Project Wizard.\n");
    s.push_str("Use Alp commands to validate, generate, and build outputs.\n");
    s
}

fn gen_prj_conf(def: &WizardTemplateDefinition) -> String {
    let mut s = String::new();
    s.push_str("CONFIG_ASSERT=y\n");
    s.push_str("CONFIG_NEWLIB_LIBC=y\n");
    for line in def.prj_conf_extras {
        s.push_str(line);
        s.push('\n');
    }
    s
}

fn gen_root_cmake() -> &'static str {
    "cmake_minimum_required(VERSION 3.20)\n\
     project(alp_starter C)\n\
     \n\
     add_subdirectory(src)\n"
}

fn gen_src_cmake(def: &WizardTemplateDefinition) -> String {
    let mut s = String::from("set(ALP_APP_SOURCES\n  main.c\n");
    for spec in def.feature_files {
        let rel = spec.path.strip_prefix("src/").unwrap_or(spec.path);
        s.push_str(&format!("  {rel}\n"));
    }
    s.push_str(")\n\nadd_executable(alp_app ${ALP_APP_SOURCES})\n");
    s.push_str("target_include_directories(alp_app PRIVATE ../include)\n");
    s
}

fn gen_app_header() -> &'static str {
    "// SPDX-License-Identifier: Apache-2.0\n\
     \n\
     #ifndef ALP_APP_APP_H\n\
     #define ALP_APP_APP_H\n\
     \n\
     int alp_app_init(void);\n\
     int alp_app_run(void);\n\
     \n\
     #endif /* ALP_APP_APP_H */\n"
}

fn gen_main_c(def: &WizardTemplateDefinition) -> String {
    let mut s = String::new();
    s.push_str("// SPDX-License-Identifier: Apache-2.0\n\n");
    s.push_str("#include \"app/app.h\"\n");
    s.push_str("#include <stdio.h>\n\n");
    s.push_str("int alp_app_init(void) {\n");
    s.push_str("  // TODO: initialize app-level services.\n");
    s.push_str("  return 0;\n");
    s.push_str("}\n\n");
    s.push_str("int alp_app_run(void) {\n");
    s.push_str("  // TODO: execute one app cycle.\n");
    s.push_str("  return 0;\n");
    s.push_str("}\n\n");
    s.push_str("int main(void) {\n");
    s.push_str("  if (alp_app_init() != 0) {\n");
    s.push_str("    puts(\"alp_app_init failed\");\n");
    s.push_str("    return 1;\n");
    s.push_str("  }\n\n");
    s.push_str("  if (alp_app_run() != 0) {\n");
    s.push_str("    puts(\"alp_app_run failed\");\n");
    s.push_str("    return 1;\n");
    s.push_str("  }\n\n");
    s.push_str(&format!("  puts(\"{}\");\n", def.body_line1));
    s.push_str(&format!("  puts(\"{}\");\n", def.body_line2));
    s.push_str("  return 0;\n");
    s.push_str("}\n");
    s
}

fn gen_feature_file(unit_name: &str, todo_line: &str) -> String {
    format!(
        "// SPDX-License-Identifier: Apache-2.0\n\
         \n\
         #include <stdio.h>\n\
         \n\
         int {unit_name}_step(void) {{\n\
         \x20\x20// {todo_line}\n\
         \x20\x20return 0;\n\
         }}\n"
    )
}

fn gen_iot_env_example() -> &'static str {
    "# Copy to iot.env and provide real values.\n\
     WIFI_SSID=<ssid>\n\
     WIFI_PASSWORD=<password>\n\
     MQTT_ENDPOINT=<host>\n\
     MQTT_PORT=8883\n"
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wizard::models::WizardTemplateId;
    use crate::wizard::service::registry::list_wizard_templates;

    /// `libraries:` must be a top-level key (list of `{name, cores}`), never
    /// nested under `cores.<core>` -- `core_entry` in the board schema only
    /// allows `extra_libraries` and forbids unknown keys.
    #[test]
    fn board_yaml_libraries_are_top_level_not_under_core_entry() {
        // sensor-starter is now vendored (see `wizard/vendored/MANIFEST.md`),
        // so this hand-generator regression check picks another hand-generated
        // template that still declares `libs`.
        let def = list_wizard_templates()
            .into_iter()
            .find(|d| d.id == WizardTemplateId::BoardDiagnostics)
            .expect("board-diagnostics template registered");
        let files = gen_c_project_files(def, None, &[]);
        let board_yaml = &files
            .iter()
            .find(|f| f.relative_path == "board.yaml")
            .expect("board.yaml planned")
            .content;

        assert!(
            board_yaml.contains("\nlibraries:\n  - name: fmt\n"),
            "expected top-level `libraries:` entry, got:\n{board_yaml}"
        );
        assert!(
            !board_yaml.contains("    libraries:"),
            "libraries must not be nested under cores.<core>, got:\n{board_yaml}"
        );
    }
}
