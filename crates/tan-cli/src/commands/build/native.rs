// SPDX-License-Identifier: Apache-2.0
//! `tan build --native` (the default) — acquire the SDK build plan, materialise
//! its files, then hand off to the executor. Also holds the shared plan
//! acquisition + base-dir helpers.

use std::path::Path;

use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, parse_build_plan};

use crate::cli::{BuildArgs, GlobalArgs};
use crate::envelope::Project;
use crate::exit::ExitCode;
use crate::util::resolve_cli_project_context;

use super::execute::{NativeBuildOutcome, execute_slices_outcome};
use super::materialise::materialise_plan;
use super::plan_modes::plan_error_run;
use super::preflight::{maybe_auto_bootstrap, preflight_gate};
use super::workspace::invoke_sdk_emit;

/// The project build tree base (where `build/<core>-<os>/` lives) — the same
/// place `west alp-build` would run.
pub(super) fn base_dir(context: &ProjectContext) -> String {
    context
        .west_cwd
        .clone()
        .or_else(|| context.workspace_root.clone())
        .unwrap_or_else(|| ".".to_string())
}

/// Acquire + parse the build plan: from `--plan-from <FILE>` if given, else by
/// invoking the SDK's `--emit build-plan` (ADR 0014). Schema-version guarded.
pub(super) fn acquire_plan(
    context: &ProjectContext,
    args: &BuildArgs,
) -> Result<BuildPlan, (&'static str, String)> {
    let json = match &args.plan_from {
        Some(path) => std::fs::read_to_string(path).map_err(|e| {
            (
                "build.plan-unavailable",
                format!("failed to read plan file `{path}`: {e}"),
            )
        })?,
        None => invoke_sdk_emit(context, "build-plan", "build.plan-unavailable")?,
    };
    parse_build_plan(&json).map_err(|e| ("build.plan-invalid", e.to_string()))
}

/// `tan build --native` — consume the plan, materialise its files, then run each
/// slice's command sequentially. (Per ADR 0014 the conf→build wiring "C4" is
/// still settling on the SDK side; we run whatever command the emit gives, so a
/// build before C4 lands may not yet apply the per-slice config.)
///
/// Returns the full [`NativeBuildOutcome`] (envelope + the in-memory
/// manifest-written/native_sim-target signals) — `run_build`'s `.run` thin
/// wrapper (`build/mod.rs`) is what `tan build` itself uses, but `tan run`
/// (`commands::run::run`, via `run_build_native_outcome`) needs the rest to
/// decide host-vs-hardware without re-reading `system-manifest.yaml` off disk
/// afterward. Every early-return here (no plan / materialise failure)
/// happened before a build could establish anything, so it reports
/// `manifest_written: false, native_sim_target: None` — harmless in practice
/// since `decide_run_action` short-circuits on `build_ok == false` first.
pub(super) fn native_build_outcome(g: &GlobalArgs, args: &BuildArgs) -> NativeBuildOutcome {
    let mut context = resolve_cli_project_context(g);

    // Order-independent pre-flight (text mode only). Before blocking on a missing
    // prerequisite, collapse the flow: if the SDK and board.yaml are present but
    // no Zephyr workspace is resolved, bootstrap one on demand so `tan build`
    // alone gets from a fresh checkout to a build. Then, if anything still blocks
    // (no SDK / board.yaml, or a bootstrap that didn't produce a workspace), show
    // a colorful, actionable readiness report and stop — instead of proceeding to
    // a raw west/CMake error. JSON mode keeps its stable envelope; the same
    // errors surface from acquire_plan.
    if !g.is_json() {
        if let Some(updated) = maybe_auto_bootstrap(g, &context) {
            context = updated;
        }
        if let Some(blocked) = preflight_gate(g, &context) {
            return NativeBuildOutcome {
                run: blocked,
                manifest_written: false,
                native_sim_target: None,
            };
        }
    }

    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let plan = match acquire_plan(&context, args) {
        Ok(plan) => plan,
        Err((code, message)) => {
            return NativeBuildOutcome {
                run: plan_error_run(g, project, code, message, ExitCode::RuntimeFailure),
                manifest_written: false,
                native_sim_target: None,
            };
        }
    };

    let base = base_dir(&context);
    if let Err(e) = materialise_plan(&plan, Path::new(&base)) {
        return NativeBuildOutcome {
            run: plan_error_run(
                g,
                project,
                "build.materialise-failed",
                e.message(),
                ExitCode::WriteFailure,
            ),
            manifest_written: false,
            native_sim_target: None,
        };
    }

    execute_slices_outcome(g, &context, project, &plan, &base)
}
