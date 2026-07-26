// SPDX-License-Identifier: Apache-2.0
//! Debug `doctor` domain — a port of the TypeScript `@alp-sdk/core/debug`
//! doctor logic (`buildDoctorReport`, `serverChoicesForTarget`, and the
//! runtime-capability/workspace-context adapters).
//!
//! All logic here is pure: runtime capabilities and `board.yaml` existence are
//! injected as predicates, so doctor parity with the TS implementation is
//! guaranteed by deterministic unit tests rather than by the (machine-
//! dependent) golden-fixture harness.

mod context;
mod doctor;
mod inspect;
mod target;
mod trace;

pub use context::{
    DebugRuntimeCapabilities, DebugWorkspaceContext, DebuggerExtensionsState,
    collect_runtime_capabilities_from_commands, create_debug_workspace_context,
};
pub use doctor::{
    DoctorCheck, DoctorReport, DoctorStatus, DoctorSummary, append_doctor_check,
    build_doctor_report, prepend_doctor_checks, unique_next_steps,
};
pub use inspect::{DebugResolvedValue, DebugValueSource, collect_resolved_values};
pub use target::{
    DebugServerKind, DebugTargetKind, is_server_supported_for_target, parse_server_kind,
    parse_target_kind, server_choices_for_target,
};
pub use trace::{DebugGenerationTraceDecision, DebugTraceOutcome};
