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
use super::preflight::{gate_from_checks, maybe_auto_bootstrap, probe_build_preflight};
use super::token_substitution::apply_plan_token_substitution;
use super::workspace::invoke_sdk_emit;

/// The project build tree base (where `build/<core>-<os>/` lives) — the
/// directory `tan build` runs each slice's command from. `pub(crate)`
/// (widened from `pub(super)`) so `commands::kconfig` can derive the SAME
/// base for its `resolve_zephyr_base` call instead of a second copy.
pub(crate) fn base_dir(context: &ProjectContext) -> String {
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
        None => invoke_sdk_emit(context, "build-plan", "build.plan-unavailable", &[], &[])?,
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
        // Probe once; only re-probe if a bootstrap actually ran (it can change
        // the workspace/west readiness), instead of always probing twice.
        let mut checks = probe_build_preflight(g, &context);
        // `--no-auto-bootstrap` keeps `tan build` to building. The implicit
        // trigger is now a full native bootstrap on EVERY host (#49) — on
        // Windows it used to refuse instantly, and instead it can clone Zephyr
        // + the HALs into `<sdkRoot>/..` unattended, which is not something a
        // build should start without a way to say no. Default is unchanged
        // (auto-trigger stays, matching the POSIX behaviour it always had).
        if !args.no_auto_bootstrap {
            if let Some(updated) = maybe_auto_bootstrap(g, &checks) {
                context = updated;
                checks = probe_build_preflight(g, &context);
            }
        }
        if let Some(blocked) = gate_from_checks(g, checks) {
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

    // Build-plan token substitution (alp-sdk #865): a no-op unless the plan is
    // `planPathMode: "tokened"`. Runs on the in-memory plan BEFORE materialise
    // writes anything or a slice command is ever built, using `base` (the
    // SAME exec base every slice actually runs under) for the PROJECT_ROOT/
    // exec-base divergence guard.
    //
    // `toolchain` is resolved exactly ONCE here (not inside `apply_plan_token_
    // substitution`, which used to call the resolver itself — see that
    // function's doc for why that made its own unit tests host-dependent)
    // and threaded down to the executor too, so a demoted slice's advice text
    // and this run's actual dispatch decision are read off the SAME value.
    let base = base_dir(&context);
    let toolchain = crate::toolchain::resolve_toolchain_root();
    let (plan, demoted) = match apply_plan_token_substitution(g, &context, &base, &plan, &toolchain)
    {
        Ok(result) => result,
        Err((code, message)) => {
            return NativeBuildOutcome {
                run: plan_error_run(g, project, code, message, ExitCode::RuntimeFailure),
                manifest_written: false,
                native_sim_target: None,
            };
        }
    };

    // Refuse a plan whose build_root isn't the `<project>/build` that
    // flash/size/image/renode read — building into a different directory would
    // leave those separate invocations reading a stale (or missing) manifest,
    // and `tan flash` would program it onto silicon. Checked before materialise
    // writes anything. Every SDK-emitted plan uses `build` today; this makes a
    // future drift a loud error instead of a silent wrong-image flash.
    if !plan.build_root_is_consumer_default() {
        return NativeBuildOutcome {
            run: plan_error_run(
                g,
                project,
                "build.unsupported-build-root",
                format!(
                    "plan buildRoot `{}` is not `build`; tan's flash/size/image/renode read \
                     `<project>/build/system-manifest.yaml`, so building elsewhere would leave \
                     them reading a stale or missing manifest",
                    plan.build_root
                ),
                ExitCode::RuntimeFailure,
            ),
            manifest_written: false,
            native_sim_target: None,
        };
    }

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

    execute_slices_outcome(g, &context, project, &plan, &base, &demoted, args.pristine)
}
