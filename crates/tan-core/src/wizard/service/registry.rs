// SPDX-License-Identifier: Apache-2.0
//! Static wizard/module template registry.

use crate::wizard::models::{
    FeatureFileSpec, ModuleTemplateDefinition, ModuleTemplateId, WizardFeatureFlags,
    WizardTemplateDefinition, WizardTemplateId,
};

// ---------------------------------------------------------------------------
// Static template registry
// ---------------------------------------------------------------------------

pub(super) static TEMPLATE_DEFINITIONS: &[WizardTemplateDefinition] = &[
    WizardTemplateDefinition {
        id: WizardTemplateId::MinimalApp,
        label: "Minimal app",
        description: "Smallest baseline project with a simple main loop.",
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[FeatureFileSpec {
            path: "src/features/app_bootstrap.c",
            unit_name: "app_bootstrap",
            todo_line: "TODO: register app services and initialize runtime modules.",
        }],
        body_line1: "ALP minimal starter boot",
        body_line2: "TODO: add your application logic",
        explanation: &[
            "Minimal template keeps generated code intentionally small and neutral.",
            "Use this baseline when you want full control over feature bring-up order.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::ZephyrApp,
        label: "Zephyr app",
        description: "West-buildable Zephyr application wired to board.yaml via the SDK loader.",
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "Alp SDK Zephyr app starting",
        body_line2: "add your application logic",
        explanation: &[
            "Real Zephyr app: find_package(Zephyr) + board.yaml -> alp.conf via OVERLAY_CONFIG.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::SensorStarter,
        label: "Sensor starter",
        description: "Sensor polling skeleton with diagnostics-friendly logging.",
        libs: &["fmt"],
        features: None,
        prj_conf_extras: &["CONFIG_SENSOR=y", "CONFIG_I2C=y"],
        feature_files: &[FeatureFileSpec {
            path: "src/features/sensor_pipeline.c",
            unit_name: "sensor_pipeline",
            todo_line: "TODO: initialize bus, read sensors, normalize values for app flow.",
        }],
        body_line1: "ALP sensor starter boot",
        body_line2: "TODO: initialize sensor bus and polling loop",
        explanation: &[
            "src/main.c includes a sensor-oriented TODO path for bus init and polling.",
            "Use this when your first milestone is sensor bring-up and deterministic sampling.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::IotStarter,
        label: "IoT starter",
        description: "Connectivity-oriented starter with Wi-Fi and MQTT defaults.",
        libs: &["mbedtls", "fmt"],
        features: Some(WizardFeatureFlags {
            wifi: true,
            mqtt: true,
            ble: false,
            tls: true,
        }),
        prj_conf_extras: &[
            "CONFIG_NETWORKING=y",
            "CONFIG_NET_IPV4=y",
            "CONFIG_MQTT_LIB=y",
            "CONFIG_MBEDTLS=y",
        ],
        feature_files: &[FeatureFileSpec {
            path: "src/features/connectivity_pipeline.c",
            unit_name: "connectivity_pipeline",
            todo_line: "TODO: bring up Wi-Fi, establish MQTT session, and publish telemetry.",
        }],
        body_line1: "ALP IoT starter boot",
        body_line2: "TODO: connect Wi-Fi and start MQTT session",
        explanation: &[
            "Starter defaults include Wi-Fi, MQTT, and TLS-friendly settings in board.yaml.",
            "src/main.c highlights connectivity boot and MQTT session bring-up steps.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::EdgeAiStarter,
        label: "Edge AI starter",
        description: "Inference-first starter with arena sizing and backend hints.",
        libs: &["cmsis_dsp", "etl"],
        features: None,
        prj_conf_extras: &["CONFIG_CMSIS_DSP=y", "CONFIG_CBPRINTF_FP_SUPPORT=y"],
        feature_files: &[FeatureFileSpec {
            path: "src/features/inference_pipeline.c",
            unit_name: "inference_pipeline",
            todo_line: "TODO: map input tensors, execute inference, and decode model output.",
        }],
        body_line1: "ALP edge AI starter boot",
        body_line2: "TODO: load model and run inference loop",
        explanation: &[
            "board.yaml includes an inference block with arena defaults for initial runs.",
            "src/main.c points to model-load and inference-loop integration work.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::BoardDiagnostics,
        label: "Board diagnostics",
        description: "Bring-up oriented starter for board and peripheral checks.",
        libs: &["fmt", "doctest"],
        features: None,
        prj_conf_extras: &["CONFIG_LOG=y", "LOG_MODE_DEFERRED=y", "LOG_DEFAULT_LEVEL=4"],
        feature_files: &[FeatureFileSpec {
            path: "src/features/diagnostics_checks.c",
            unit_name: "diagnostics_checks",
            todo_line: "TODO: run board bring-up checks and report failing subsystems.",
        }],
        body_line1: "ALP board diagnostics starter boot",
        body_line2: "TODO: run bring-up checks and report failures",
        explanation: &[
            "Template enables diagnostics-friendly defaults for bring-up and fault tracking.",
            "src/main.c is oriented toward check-list style board validation routines.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::HostToolingStarter,
        label: "Host tooling starter",
        description: "Monorepo scaffold for a host-side ALP tool: shared core package, standalone CLI, and VS Code extension surface.",
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Scaffolds a monorepo with packages/core (shared domain), packages/cli (standalone npm CLI), and root src/ (VS Code extension).",
            "Follows the one-core-many-surfaces principle: validation, generation, and scaffolding logic lives in the core package.",
            "File generation for this template is planned and not yet available.",
        ],
    },
];

pub(super) static MODULE_TEMPLATE_DEFINITIONS: &[ModuleTemplateDefinition] = &[
    ModuleTemplateDefinition {
        id: ModuleTemplateId::SensorDriver,
        label: "Sensor driver module",
        description: "Adds a source/header pair for sensor acquisition logic.",
        function_prefix: "alp_sensor",
        explanation: &[
            "Use {nm}_run to place sensor polling and conversion logic.",
            "Keep hardware-specific register access isolated from upper-level app flow.",
        ],
    },
    ModuleTemplateDefinition {
        id: ModuleTemplateId::ConnectivityService,
        label: "Connectivity service module",
        description: "Adds module skeleton for network/session orchestration.",
        function_prefix: "alp_conn",
        explanation: &[
            "Use {nm}_init for stack/session initialization.",
            "Keep retry/backoff and transport health checks localized in this module.",
        ],
    },
    ModuleTemplateDefinition {
        id: ModuleTemplateId::InferenceStage,
        label: "Inference stage module",
        description: "Adds module skeleton for model pre/post processing path.",
        function_prefix: "alp_infer",
        explanation: &[
            "Use {nm}_run to host pre-process, infer, and post-process calls.",
            "Keep model IO shaping and feature extraction close to this module boundary.",
        ],
    },
    ModuleTemplateDefinition {
        id: ModuleTemplateId::DiagnosticsCheck,
        label: "Diagnostics check module",
        description: "Adds bring-up and runtime health-check module scaffold.",
        function_prefix: "alp_diag",
        explanation: &[
            "Use {nm}_run for periodic health checks and error probes.",
            "Keep board bring-up assertions and diagnostics output in this module.",
        ],
    },
];

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// All registered project-wizard templates, in registry order.
pub fn list_wizard_templates() -> Vec<&'static WizardTemplateDefinition> {
    TEMPLATE_DEFINITIONS.iter().collect()
}

/// All registered module-scaffold templates, in registry order.
pub fn list_module_templates() -> Vec<&'static ModuleTemplateDefinition> {
    MODULE_TEMPLATE_DEFINITIONS.iter().collect()
}
