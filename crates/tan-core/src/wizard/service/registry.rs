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
        // Files are vendored from the SDK's `minimal` scaffold-catalog entry
        // (alp-sdk#864, see wizard/vendored/MANIFEST.md), not hand-generated
        // from these fields -- libs/features/prj_conf_extras/feature_files/
        // body_line* are unread for this template (see gen_c_project_files).
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Real Zephyr app vendored from the SDK's `minimal` scaffold: find_package(Zephyr) + board.yaml -> alp.conf via EXTRA_CONF_FILE.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::SensorStarter,
        label: "Sensor starter",
        description: "West-buildable TMP112 i2c-master app wired to board.yaml via the SDK loader.",
        // Files are vendored from the SDK's `sensor` scaffold-catalog entry
        // (alp-sdk#864, see wizard/vendored/MANIFEST.md), not hand-generated
        // from these fields -- libs/features/prj_conf_extras/feature_files/
        // body_line* are unread for this template (see gen_c_project_files).
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Real Zephyr app vendored from the SDK's `sensor` scaffold: reads the TMP112 temperature sensor via <alp/chips/tmp112.h> on BRD_I2C.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::IotStarter,
        label: "IoT starter",
        description: "West-buildable Wi-Fi + MQTT/TLS telemetry app wired to board.yaml via the SDK loader (E1M-AEN801 only).",
        // Files are vendored from the SDK's `iot` scaffold-catalog entry
        // (alp-sdk#864/#903, see wizard/vendored/MANIFEST.md), not
        // hand-generated -- prj_conf_extras/feature_files/body_line* are
        // unread for this template (see gen_c_project_files). Unlike the
        // other vendored templates, `libs`/`features` are NOT blanked here:
        // `tan explain` (a fourth consumer of this registry -- explain.rs --
        // reads these two fields directly) would otherwise describe a
        // starter with no libraries/features even though this template's
        // vendored board.yaml genuinely declares `libraries: [mbedtls]` +
        // `iot: {wifi: true, tls: true}`. `mqtt: true` here even though the
        // vendored board.yaml has no separate `iot.mqtt` toggle line (MQTT is
        // inherent to this app, not board.yaml-gated): `features` feeds only
        // `tan explain`'s "Default features" summary for this template, and
        // that summary describing an MQTT/TLS telemetry app as `mqtt=false`
        // contradicts the very line above it.
        libs: &["mbedtls"],
        features: Some(WizardFeatureFlags {
            wifi: true,
            mqtt: true,
            ble: false,
            tls: true,
        }),
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Real Zephyr app vendored from the SDK's `iot` scaffold: brings up Wi-Fi via the CC3501E bridge and publishes an mqtts:// (TLS) MQTT telemetry reading on a cadence, through the portable <alp/iot.h> surface.",
            "AEN-only + preview: E1M-AEN801's CC3501E Wi-Fi transport is silicon-validated; the E1M-V2N101 Wi-Fi path does not exist yet, so --som is fixed to E1M-AEN801 for this template.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::EdgeAiStarter,
        label: "Edge AI starter",
        description: "West-buildable BME280 cold-chain-monitor app wired to board.yaml via the SDK loader.",
        // Files are vendored from the SDK's `edge-ai` scaffold-catalog entry
        // (alp-sdk#864, see wizard/vendored/MANIFEST.md), not hand-generated
        // from these fields -- libs/features/prj_conf_extras/feature_files/
        // body_line* are unread for this template (see gen_c_project_files).
        // FIRST heterogeneous (multi-core) vendored template: a companion
        // Cortex-A cluster (`os: "off"`) ships alongside the Cortex-M app core.
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Real Zephyr app vendored from the SDK's `edge-ai` scaffold: a BME280 cold-chain integrity monitor (MKT / dewpoint / excursion time) plus a TFLite-Micro anomaly score.",
            "Heterogeneous board.yaml: the app core (m33_sm/m55_hp) runs inference alongside a companion Cortex-A cluster (a55_cluster/a32_cluster, os: \"off\").",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
        ],
    },
    WizardTemplateDefinition {
        id: WizardTemplateId::BoardDiagnostics,
        label: "Board diagnostics",
        description: "West-buildable board self-test app wired to board.yaml via the SDK loader.",
        // Files are vendored from the SDK's `diagnostics` scaffold-catalog
        // entry (alp-sdk#864/#903, see wizard/vendored/MANIFEST.md), not
        // hand-generated from these fields -- libs/features/prj_conf_extras/
        // feature_files/body_line* are unread for this template (see
        // gen_c_project_files). `libs: &[]`/`features: None` are left as-is
        // (not just blanked): the SDK catalog's `diagnostics` entry genuinely
        // declares no libraries and no `iot:` toggles, so `tan explain` (a
        // fourth consumer of this registry, explain.rs) stays accurate too.
        libs: &[],
        features: None,
        prj_conf_extras: &[],
        feature_files: &[],
        body_line1: "",
        body_line2: "",
        explanation: &[
            "Real Zephyr app vendored from the SDK's `diagnostics` scaffold: reads the SoM/SoC identity, the RUN operating-point profile, and scans the on-module I2C management bus for a pass/fail bring-up report.",
            "Portable <alp/*> APIs only -- no chip driver -- so the same source runs on every E1M family; a check a backend can't service reports SKIP, not FAIL.",
            "Build with `west build -b <board>` after `export ALP_SDK_ROOT=<your alp-sdk checkout>`.",
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
