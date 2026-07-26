// SPDX-License-Identifier: Apache-2.0
//! The `--native` build executor: run each plan slice's `ToolStep` sequentially,
//! folding per-slice outcomes into the envelope (JSON) or a colorful recap
//! (text). Also the consumer-mechanism env gap-filler (`ZEPHYR_BASE` /
//! `EXTRA_ZEPHYR_MODULES`).

mod env;
mod manifest;

use std::path::Path;
use std::process::Command;

use serde::Serialize;
use tan_core::ProjectContext;
use tan_core::build_plan::{BuildPlan, CONSUMER_BUILD_ROOT, PolicyAction};
use tan_core::debug::{DoctorCheck, DoctorStatus};
use tan_core::plan_exec::{SdkStampAction, assemble_slice_env, resolve_action, sdk_stamp_action};
use tan_core::preflight::preflight_summary;

use super::CommandRun;
use crate::cli::GlobalArgs;
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;

use crate::venv::{west_program, with_venv_on_path};

use super::token_substitution::SliceDemotion;
use super::workspace::resolve_zephyr_base;

/// `SliceResult.reason` for a plan slice that carries no command. The recap
/// keys its `(no command)` rendering off this exact string — keep producer and
/// renderer on the one constant.
const SKIP_REASON_NO_COMMAND: &str = "no command";

/// Per-slice outcome of a `--native` run, folded into the envelope.
#[derive(Serialize)]
struct SliceResult {
    /// The core this slice builds (e.g. `m55_hp`).
    #[serde(rename = "coreId")]
    core_id: String,
    /// Build backend for the slice (`zephyr` / `yocto` / `baremetal`).
    backend: String,
    /// Outcome: `"ok"`, `"failed"`, or `"skipped"` (no command, or the slice's
    /// tool is not on this host).
    status: String, // "ok" | "failed" | "skipped"
    /// Process exit code, when the tool actually launched.
    #[serde(skip_serializing_if = "Option::is_none")]
    rc: Option<i32>,
    /// Why the slice was skipped or failed: the missing-tool reason verbatim,
    /// or — in JSON mode, where the child's stdio was captured rather than
    /// inherited — the tail of the captured build output / the launch error,
    /// so a failed build isn't reduced to a bare rc with no diagnostic.
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
    /// Real on-disk `zephyr.elf` path after a successful build (absolute), fed
    /// into the post-build manifest so `size`/`renode`/`flash` find the artefact
    /// west actually produced. Internal plumbing — kept out of the JSON envelope.
    #[serde(skip)]
    output_artefact: Option<String>,
    /// Real on-disk build directory (west's nested `<cwd>/build`, absolute) —
    /// what `image` tars. Internal plumbing — kept out of the JSON envelope.
    #[serde(skip)]
    build_dir: Option<String>,
}

/// Envelope `data` for a `--native` build: the base dir plus each slice result.
#[derive(Serialize)]
struct BuildRunData {
    /// Envelope `data` schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// Project build-tree base the slices ran under.
    #[serde(rename = "baseDir")]
    base_dir: String,
    /// Outcome of each slice, in plan order.
    slices: Vec<SliceResult>,
}

/// The full outcome of a `--native` build run: the envelope `tan build`
/// reports, PLUS the two in-memory signals `tan run` decides host-vs-hardware
/// and flash-safety from — never by re-reading `system-manifest.yaml` off
/// disk afterward (see `tan_core::run`'s module doc for the root cause this
/// closes). `pub(crate)` because `commands::run` (a sibling of `commands::build`)
/// consumes it directly via [`crate::commands::build::run_build_native_outcome`].
pub(crate) struct NativeBuildOutcome {
    /// The build's own envelope/exit — what `tan build` (and `run_build`'s
    /// thin wrapper) return unchanged.
    pub(crate) run: CommandRun,
    /// Whether THIS run's post-build `system-manifest.yaml` FILE write
    /// actually succeeded (`manifest::PostBuildManifest::write_failed_reason
    /// .is_none()`). Gates `RunAction::Flash` for a confirmed hardware
    /// target — a failed write means `flash` would next read a stale/wrong
    /// file.
    pub(crate) manifest_written: bool,
    /// Whether THIS run's freshly emitted SDK system-manifest projection
    /// declares a `native_sim` slice — `None` only when the emit itself
    /// failed/didn't parse (no signal, not "not native_sim"). See
    /// `tan_core::run::decide_run_action`.
    pub(crate) native_sim_target: Option<bool>,
}

/// Thin `CommandRun`-only wrapper around [`execute_slices_outcome`] — kept
/// for this module's own (pre-existing) tests below, which only assert on the
/// envelope; every production caller now goes through
/// [`execute_slices_outcome`] directly (`tan build` via `native.rs`'s
/// `native_build_outcome` -> `run_build`'s `.run`, `tan run` via
/// `run_build_native_outcome`), so this has no non-test caller.
#[cfg(test)]
pub(super) fn execute_slices(
    g: &GlobalArgs,
    context: &ProjectContext,
    project: Project,
    plan: &BuildPlan,
    base: &str,
) -> CommandRun {
    // No tokened plans in this module's own tests — nothing is ever demoted.
    execute_slices_outcome(g, context, project, plan, base, &[]).run
}

/// Shared skip/fail shaping for the dispatch loop's early-exit branches
/// (unknown backend / null command / demoted slice / missing tool) — tan-cli
/// #89 review, MINOR 6: this exact shape (a colored text-mode status line,
/// plus flipping `any_failed` when the resolved policy says Fail) used to be
/// written out four times in the loop below. Each branch still builds its
/// own `Issue` (different codes/severities) and `SliceResult` — those differ
/// enough per site that folding them in too would just be a config struct
/// for four call sites, so only the part that was byte-for-byte identical is
/// factored out here.
fn report_slice_outcome(
    text_mode: bool,
    theme: &crate::style::Theme,
    core_id: &str,
    backend: &str,
    fail: bool,
    detail: &str,
    any_failed: &mut bool,
) {
    if text_mode {
        let ds = if fail {
            DoctorStatus::Fail
        } else {
            DoctorStatus::Warn
        };
        eprintln!(
            "{}",
            theme.slice_result(ds, &format!("{core_id} [{backend}] — {detail}"))
        );
    }
    if fail {
        *any_failed = true;
    }
}

/// Run each slice's `ToolStep` sequentially under `base`. Text mode streams each
/// build live (inherited stdio) with per-slice headers; JSON mode captures and
/// folds per-slice results into the envelope. Commandless slices are skipped.
/// Runs all slices (does not abort early) and exits non-zero if any failed.
pub(crate) fn execute_slices_outcome(
    g: &GlobalArgs,
    context: &ProjectContext,
    project: Project,
    plan: &BuildPlan,
    base: &str,
    demoted: &[SliceDemotion],
) -> NativeBuildOutcome {
    let base_path = Path::new(base);
    let text_mode = !g.is_json();
    let theme = crate::style::Theme::from_args(g);
    let mut results: Vec<SliceResult> = Vec::new();
    let mut any_failed = false;
    // Per-slice `build.unknown-backend` issues (skip=warning / fail=error),
    // merged into the JSON envelope below. Named per-slice — unlike
    // null_command/missing_tool, which only ride the generic
    // `build.slice-failed` issue — because the whole point of this policy is
    // to name WHICH backend string the plan sent that this CLI doesn't know.
    let mut backend_issues: Vec<Issue> = Vec::new();
    // Also folded into the JSON envelope: a warning per slice this run wiped
    // for a stale-SDK build dir (issue #52) — never silent, since the VS Code
    // extension only ever sees the envelope, not this function's `eprintln!`s.
    let mut sdk_switch_issues: Vec<Issue> = Vec::new();
    // Per-slice `build.toolchain-root-unresolved` issues (tan-cli #89): a
    // slice demoted by `apply_plan_token_substitution` because its OWN fields
    // (not a plan-level site) still name an unresolved `${TOOLCHAIN_ROOT}`.
    // Reuses the plan-fatal sibling error's code on purpose — the extension
    // needs no new vocabulary — but at `warning` (skip) or `error` (fail)
    // severity instead of always erroring the whole plan.
    let mut toolchain_issues: Vec<Issue> = Vec::new();
    let workspace_root = crate::util::cli_workspace_root(g);
    let sdk_root = crate::util::resolve_sdk_root(g, &workspace_root);
    // Normalized (absolute + `.`/`..`-collapsed) — NOT `sdk_root`'s raw
    // `to_string_lossy()`. `resolve_sdk_root` returns an explicit `--sdk-root`
    // verbatim (relative forms are a documented, supported shape), while `tan
    // sdk switch` stores an absolute, `tan_core::normalize_path`-normalized
    // pointer (the same `normalize_path(workspace_root.join(p))` idiom reused
    // here, sdk.rs:414). Comparing the two raw byte-mismatches a relative
    // `--sdk-root ../alp-sdk` against the identical absolute path `tan sdk
    // switch` already pinned for the SAME checkout — thrashing `Pristine` (a
    // full wipe) on every alternating invocation between the two forms.
    let sdk_root_str = normalized_sdk_root_str(sdk_root.as_deref(), &workspace_root);
    // Auto-manage the build env so `tan build` needs no manual
    // `source .venv/activate` / `export ZEPHYR_BASE`: derive ZEPHYR_BASE from the
    // resolved workspace and pass the alp-sdk checkout as an extra Zephyr module,
    // so `west build -b <alp-board>` finds the SDK's boards without the user
    // wiring `-DEXTRA_ZEPHYR_MODULES`. The plan's per-slice env still wins.
    let zephyr_base = resolve_zephyr_base(base, sdk_root.as_deref());

    for (idx, slice) in plan.slices.iter().enumerate() {
        let backend = slice.backend.as_str().to_string();

        // `unknown_backend` policy: Fail (default) vs Skip. `Backend::Unknown`
        // is a catch-all so ONE slice naming a backend this CLI doesn't know
        // no longer fails the WHOLE plan to parse (build_plan.rs) — but
        // nothing enforced `executionPolicy.unknownBackend` after that
        // landed, so an unrecognised backend fell straight into dispatch
        // unchecked: a net regression versus the old closed-enum
        // reject-the-plan behavior. Checked FIRST — before null-command,
        // missing-tool, AND the demoted-slice check below (tan-cli #89
        // review, MAJOR 2): an unrecognised backend is unusable regardless
        // of whether the slice happens to carry a command OR is ALSO
        // token-demoted, and `executionPolicy.unknownBackend` (default Fail,
        // the SDK default) must not be silently downgraded to
        // `missingTool`'s default-Skip just because the same slice ALSO
        // names an unresolved `${TOOLCHAIN_ROOT}` — a version/bug fact
        // outranks a host-provisioning fact, the same principle
        // `sub_field_lenient` (tan-core) already applies within one field.
        // Demoted used to be checked FIRST, ahead of this, which let that
        // exact combination exit via `missingTool`'s skip-by-default instead.
        if slice.backend.is_unknown() {
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.unknown_backend,
                PolicyAction::Fail,
            ) == PolicyAction::Fail;
            let reason = format!("unrecognised backend `{backend}`");
            report_slice_outcome(
                text_mode,
                &theme,
                &slice.core_id,
                &backend,
                fail,
                &reason,
                &mut any_failed,
            );
            backend_issues.push(Issue {
                code: "build.unknown-backend".to_string(),
                severity: if fail { "error" } else { "warning" }.to_string(),
                message: format!(
                    "slice `{}` names unrecognised backend `{backend}`{}",
                    slice.core_id,
                    if fail { "" } else { " — skipped" }
                ),
            });
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(reason),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }

        let Some(cmd) = &slice.command else {
            // `null_command` policy: Skip (default / older plan) vs Fail.
            // Checked before the demoted-slice check below for the same
            // reason as unknown-backend above: a null command is unusable
            // regardless of a co-occurring token demotion.
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.null_command,
                PolicyAction::Skip,
            ) == PolicyAction::Fail;
            let detail = if fail {
                "no command"
            } else {
                "no command, skipped"
            };
            report_slice_outcome(
                text_mode,
                &theme,
                &slice.core_id,
                &backend,
                fail,
                detail,
                &mut any_failed,
            );
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(SKIP_REASON_NO_COMMAND.to_string()),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        };

        // Demoted-slice check (tan-cli #89) — AFTER unknown-backend and
        // null-command above (MAJOR 2 of the #89 review: this used to sit
        // FIRST, ahead of both — see their comments for the regression that
        // reopened). Still BEFORE the unsafe-cwd guard/`create_dir_all`/the
        // pristine wipe/dispatch below: a demoted slice's PLAN fields still
        // carry the literal, unsubstituted `${TOOLCHAIN_ROOT}` —
        // `apply_plan_token_substitution` reported it instead of erroring
        // precisely because it has an owning slice/dispatch seam, but it did
        // NOT make the token usable. So nothing below this point may touch
        // those fields: not `create_dir_all` on a possibly token-bearing cwd,
        // not the destructive sdk-switch-pristine wipe (which would delete
        // the slice's last-good `zephyr.elf` for a build that never runs),
        // not env assembly, not dispatch.
        if let Some(d) = demoted
            .iter()
            .find(|d| d.slice_index == idx && d.core_id == slice.core_id)
        {
            // Keyed on BOTH `slice_index` AND `core_id`, not index alone,
            // and not a `debug_assert_eq!` (MINOR 3 of the #89 review: a
            // debug assertion compiles out of the shipped release binary, so
            // it gave no protection in production). A mismatch here simply
            // finds no demotion, so the slice falls through to normal
            // dispatch below — where its still-literal `${TOOLCHAIN_ROOT}`
            // fails loudly at the tool invocation — instead of silently
            // applying a skip/fail policy to the WRONG slice.
            //
            // Reuses `missingTool` rather than a new policy key: this IS the
            // condition that knob already names — "this host lacks the thing
            // this slice builds with" — whether the missing thing is a PATH
            // binary or a toolchain-root token. A new key would be the exact
            // SDK-contract drift the module doc warns against.
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.missing_tool,
                PolicyAction::Skip,
            ) == PolicyAction::Fail;
            let reason = d.reason.clone();
            report_slice_outcome(
                text_mode,
                &theme,
                &slice.core_id,
                &backend,
                fail,
                &reason,
                &mut any_failed,
            );
            toolchain_issues.push(Issue {
                code: "build.toolchain-root-unresolved".to_string(),
                severity: if fail { "error" } else { "warning" }.to_string(),
                message: format!(
                    "slice `{}` {}: {reason}",
                    slice.core_id,
                    if fail { "failed" } else { "skipped" }
                ),
            });
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(reason),
                // Explicitly cleared, NOT `None` (MINOR 4 of the #89 review):
                // `overlay_run_results_raw` treats a `None` output_artefact as
                // "preserve the plan-time/previous-run value" — a demoted
                // slice never dispatches, so leaving this `None` let a
                // Zephyr-only tokened plan on a toolchain-less host overwrite
                // `status` to skipped/failed while `system-manifest.yaml`
                // still named a PREVIOUS run's `zephyr.elf`. `tan flash`
                // already refuses any non-`ok` status before trusting
                // `output_artefact` (`plan_flash_targets`), but `tan size`/
                // `tan debug-config` read it with no status check of their
                // own — clearing it here (rather than requiring every reader
                // to add one) closes that at the one place that knows the
                // slice never built.
                output_artefact: Some(String::new()),
                build_dir: None,
            });
            continue;
        }

        if text_mode {
            eprintln!(
                "{}",
                theme.slice_start(&slice.core_id, &backend, &cmd.display())
            );
        }
        // `materialise.rs` and `manifest.rs` both refuse an absolute/`..`-escaping
        // plan path before touching disk — this executor used to accept ANY
        // `cmd.cwd` verbatim, so a plan-supplied `../../../scratch` would
        // `create_dir_all` and build outside the project, then get recorded as
        // an absolute `build_dir` in system-manifest.yaml (which `tan clean`
        // later rmtree's without a depth guard). Same shape guard, same seam.
        if !tan_core::is_plain_relative(Path::new(&cmd.cwd)) {
            if text_mode {
                eprintln!(
                    "{}",
                    theme.slice_result(
                        DoctorStatus::Fail,
                        &format!(
                            "refusing unsafe cwd `{}` (absolute or escapes the build tree)",
                            cmd.cwd
                        )
                    )
                );
            }
            any_failed = true;
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: "failed".to_string(),
                rc: None,
                reason: Some(format!("unsafe cwd `{}`", cmd.cwd)),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }
        let cwd = base_path.join(&cmd.cwd);
        // The build dir must exist before the tool runs (west/cmake build there).
        if let Err(e) = std::fs::create_dir_all(&cwd) {
            let reason = format!("cannot create build dir {}: {e}", cwd.display());
            if text_mode {
                eprintln!("{}", theme.slice_result(DoctorStatus::Fail, &reason));
            }
            any_failed = true;
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: "failed".to_string(),
                rc: None,
                reason: Some(reason),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }

        let tool = if cmd.tool == "west" {
            west_program(base, sdk_root.as_deref())
        } else {
            cmd.tool.clone()
        };

        // SDK parity (alp-sdk#114 / orchestrator.py:299): a build tool that's
        // simply not installed on this host is normal — non-Zephyr dev boxes
        // won't have `bitbake`, non-Linux boxes may lack the vendor toolchain.
        // Treat it as skipped (exit 0), not failed, and say why.
        if !crate::util::tool_available(&tool) {
            // The SDK's verbatim reason wording. "not found in PATH" assumes a
            // PATH-resolved tool; an absolute `tool` here is effectively
            // unreachable (west_program only returns venv paths it verified
            // with `is_file()`).
            let reason =
                format!("{tool} not found in PATH; this is normal on non-{backend} dev hosts");
            // `missing_tool` policy: Skip (default / older plan) vs Fail.
            let fail = resolve_action(
                plan.execution_policy.as_ref(),
                |p| p.missing_tool,
                PolicyAction::Skip,
            ) == PolicyAction::Fail;
            report_slice_outcome(
                text_mode,
                &theme,
                &slice.core_id,
                &backend,
                fail,
                &reason,
                &mut any_failed,
            );
            results.push(SliceResult {
                core_id: slice.core_id.clone(),
                backend,
                status: if fail { "failed" } else { "skipped" }.to_string(),
                rc: None,
                reason: Some(reason),
                output_artefact: None,
                build_dir: None,
            });
            continue;
        }

        // Sdk-switch-pristine guard (issue #52): a build dir west configured
        // against a PREVIOUS `--sdk-root` makes it FATAL ERROR on this one
        // ("please clean it, use --pristine, or use --build-dir") — a real
        // failure the user only sees as a bare "terminated with exit code: 1"
        // in the terminal tab title, with the actual cause buried in
        // scrollback. Detect it here and wipe west's own nested build dir
        // before the tool runs, so the configure that follows is fresh
        // instead of fatal.
        //
        // Deliberately AFTER the missing-tool skip above (not right after
        // `create_dir_all`): the wipe is destructive and the tool-availability
        // check is the only thing standing between "this slice is about to be
        // rebuilt" and "this slice is about to be skipped". Running the wipe
        // first meant a host missing `west` (or mid-switch to an SDK whose
        // venv isn't resolvable yet) got its LAST GOOD `zephyr.elf` deleted
        // for a rebuild that then never happened — the skip still reported
        // exit 0, so nothing downstream (`tan flash`/`size`) ever learned the
        // artefact it names is gone.
        //
        // Two guards (mirroring `resolve_zephyr_artefact`'s own refusal to
        // trust a build dir it cannot resolve) gate the WHOLE block —
        // detection, wipe, AND the stamp write below — so tan never touches a
        // `<cwd>/build` it cannot vouch for: an explicit `-d`/`--build-dir`
        // override (west wrote somewhere this can't know), and a cwd that
        // doesn't normalize under `CONSUMER_BUILD_ROOT` (a plan cwd of `src/`
        // would put the touch target at `<project>/src/build`, which may hold
        // files the build never created — even a harmless stamp write there
        // is an uninvited side effect outside the build tree).
        let build_dir_overridden = manifest::build_dir_overridden(&cmd.args);
        let cwd_under_build_root = Path::new(&cmd.cwd)
            .components()
            .next()
            .is_some_and(|c| c.as_os_str() == CONSUMER_BUILD_ROOT);
        if !build_dir_overridden && cwd_under_build_root {
            let cached_sdk_root = manifest::read_sdk_stamp(&cwd);
            let action = sdk_stamp_action(
                cached_sdk_root.as_deref(),
                sdk_root_str.as_deref(),
                manifest::cmake_cache_configured(&cwd),
                build_dir_overridden,
                cwd_under_build_root,
            );
            if action == SdkStampAction::Pristine {
                let new_root = sdk_root_str.as_deref().unwrap_or("?");
                let message = match &cached_sdk_root {
                    Some(old) => format!(
                        "{}: build dir was configured against SDK root `{old}`; \
                         active SDK is `{new_root}` — running pristine",
                        slice.core_id
                    ),
                    None => format!(
                        "{}: build dir predates the SDK-switch stamp (no recorded \
                         SDK root); running pristine against the active SDK `{new_root}`",
                        slice.core_id
                    ),
                };
                if text_mode {
                    eprintln!("note: {message}");
                } else {
                    sdk_switch_issues.push(Issue {
                        code: "build.sdk-switch-pristine".to_string(),
                        severity: "warning".to_string(),
                        message,
                    });
                }
                if let Err(e) = std::fs::remove_dir_all(cwd.join("build")) {
                    // Best-effort: west's own FATAL ERROR below (if the wipe
                    // didn't fully land) at least now ships with a note
                    // explaining WHY, instead of reading as opaque.
                    //
                    // This MUST reach the envelope too, not just the terminal.
                    // The extension only ever sees JSON, and the warning above
                    // has already claimed pristine is "running" — leaving the
                    // failure text-only means a locked `CMakeCache.txt` (the
                    // reachable case on Windows) hands the user the exact
                    // FATAL ERROR this feature exists to prevent, underneath a
                    // warning saying it was handled.
                    let failed = format!(
                        "{}: could not fully wipe the stale build dir: {e}",
                        slice.core_id
                    );
                    if text_mode {
                        eprintln!("note: {failed}");
                    } else {
                        sdk_switch_issues.push(Issue {
                            code: "build.sdk-switch-pristine-failed".to_string(),
                            severity: "warning".to_string(),
                            message: failed,
                        });
                    }
                }
            }
            // Re-stamp BEFORE the tool runs (not after success): a
            // mid-configure failure still stamped correctly, because the dir
            // really was configured against `sdk_root` regardless of whether
            // the build finishes — and a compile-error iteration loop keeps
            // its incremental state instead of re-pristine-ing on every
            // retry. Best-effort: a write failure here just leaves the dir
            // unstamped, which fails toward a spurious future rebuild, never
            // toward trusting a stale one.
            if let Some(root) = sdk_root_str.as_deref() {
                let _ = manifest::write_sdk_stamp(&cwd, root);
            }
        }

        // Assemble the slice subprocess env (pure, in tan-core): slice env +
        // plan `env_append_path` (append/de-dup, seeded from the inherited
        // process env) + the CLI's pre-gated consumer-mechanism gap-fillers
        // (ZEPHYR_BASE always; EXTRA_ZEPHYR_MODULES only when the plan didn't
        // carry it — "plan wins / CLI fills gaps").
        let gap_fillers = env::zephyr_env_overrides(
            zephyr_base.as_deref(),
            sdk_root.as_deref(),
            &slice.env,
            &slice.env_append_path,
            |k| std::env::var_os(k).map(|v| v.to_string_lossy().into_owned()),
        );
        let env = assemble_slice_env(
            &slice.env,
            &slice.env_append_path,
            |k| std::env::var_os(k).map(|v| v.to_string_lossy().into_owned()),
            &gap_fillers,
        );

        let mut command = Command::new(&tool);
        command
            .args(&cmd.args)
            .current_dir(&cwd)
            .envs(env.iter().map(|(k, v)| (k, v)));
        with_venv_on_path(&mut command, &tool);

        let (status, rc, reason) = if text_mode {
            match command.status() {
                Ok(s) if s.success() => ("ok", s.code(), None),
                Ok(s) => ("failed", s.code(), None),
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::NotFound {
                        eprintln!(
                            "   {tool} not found on PATH — install it to build this {backend} slice"
                        );
                    } else {
                        eprintln!("   launch error: {e}");
                    }
                    ("failed", None, None)
                }
            }
        } else {
            // `.output()` captures stdout+stderr — unlike text mode's inherited
            // stdio, JSON mode has no other way to show the failure. Losing it
            // used to leave `--format json` (the extension's only mode) with
            // nothing but a bare rc: no compiler diagnostic, no distinction
            // between "tool crashed" and "tool not executable". Fold the tail
            // of the captured output (or the launch error) into `reason`.
            match command.output() {
                Ok(o) if o.status.success() => ("ok", o.status.code(), None),
                Ok(o) => {
                    let tail = capture_output_tail(&o);
                    ("failed", o.status.code(), tail)
                }
                Err(e) => {
                    let msg = if e.kind() == std::io::ErrorKind::NotFound {
                        format!(
                            "{tool} not found on PATH — install it to build this {backend} slice"
                        )
                    } else {
                        format!("launch error: {e}")
                    };
                    ("failed", None, Some(msg))
                }
            }
        };
        if status == "failed" {
            any_failed = true;
        }
        if text_mode {
            let ds = if status == "ok" {
                DoctorStatus::Pass
            } else {
                DoctorStatus::Fail
            };
            let note = match rc {
                Some(c) => format!("{status} (rc={c})"),
                None => status.to_string(),
            };
            eprintln!("{}", theme.slice_result(ds, &note));
        }
        // On success, resolve the real on-disk artefact west produced so the
        // post-build manifest points the consumers at the elf that exists.
        let (output_artefact, build_dir) = if status == "ok" {
            manifest::resolve_zephyr_artefact(&cwd, &cmd.args)
        } else {
            (None, None)
        };
        results.push(SliceResult {
            core_id: slice.core_id.clone(),
            backend,
            status: status.to_string(),
            rc,
            reason,
            output_artefact,
            build_dir,
        });
    }

    // Output seam: write the post-build system-manifest.yaml (the contract
    // `tan flash`/`size`/`image` read) reflecting this run's per-slice status.
    // Best-effort in that a failure here never flips the build's own exit code
    // (the slices already ran) — but it must not be SILENT: a failed rewrite
    // leaves the PREVIOUS run's manifest on disk, naming that run's artefacts
    // with that run's statuses. `tan flash` is safe against this on its own
    // (`tan_core::flash::plan_flash_targets` refuses any slice whose `status`
    // isn't `ok` rather than programming a possibly-stale artefact); `tan
    // size` is NOT — its fallback re-derives `<build_dir>/zephyr/zephyr.elf`
    // when the manifest carries no artefact, and that path still holds the
    // previous run's binary. Print the reason in text mode (as before) AND
    // fold it into the JSON envelope as a warning Issue — dropping the JSON
    // half is exactly how a stale manifest used to look identical to `ok:true`.
    let manifest_outcome = manifest::write_post_build_manifest(context, plan, base, &results);
    let manifest_warning = manifest_outcome.write_failed_reason.clone();
    if let Some(reason) = &manifest_warning {
        if text_mode {
            eprintln!("note: skipped writing system-manifest.yaml — {reason}");
        }
    }

    let exit = if any_failed {
        ExitCode::RuntimeFailure
    } else {
        ExitCode::Success
    };

    let run = if g.is_json() {
        let mut issues = if any_failed {
            vec![Issue {
                code: "build.slice-failed".to_string(),
                severity: "error".to_string(),
                message: "one or more slices failed to build".to_string(),
            }]
        } else {
            Vec::new()
        };
        issues.extend(backend_issues);
        issues.extend(sdk_switch_issues);
        issues.extend(toolchain_issues);
        if let Some(reason) = manifest_warning {
            issues.push(Issue {
                code: "build.manifest-write-failed".to_string(),
                severity: "warning".to_string(),
                message: format!("system-manifest.yaml was not updated — {reason}"),
            });
        }
        let data = BuildRunData {
            schema_version: "1".to_string(),
            base_dir: base.to_string(),
            slices: results,
        };
        let json = Envelope::new("build", project, data, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        // Colorful per-slice recap: each slice as a check (ok / skipped / failed),
        // a colored summary line, and next-step hints when something failed.
        let checks: Vec<DoctorCheck> = results
            .iter()
            .map(|r| {
                let (status, detail) = match r.status.as_str() {
                    "ok" => (DoctorStatus::Pass, r.backend.clone()),
                    "skipped" => match r.reason.as_deref() {
                        Some(reason) if reason != SKIP_REASON_NO_COMMAND => {
                            (DoctorStatus::Warn, format!("{} — {reason}", r.backend))
                        }
                        _ => (DoctorStatus::Warn, format!("{} (no command)", r.backend)),
                    },
                    _ => (
                        DoctorStatus::Fail,
                        match r.rc {
                            Some(code) => format!("{} (rc={code})", r.backend),
                            None => format!("{} (did not run)", r.backend),
                        },
                    ),
                };
                DoctorCheck {
                    name: r.core_id.clone(),
                    status,
                    detail,
                    fix: None,
                }
            })
            .collect();
        let summary = preflight_summary(&checks);
        let next_steps = if summary.fail > 0 {
            vec![
                "see the streamed logs above for each failed slice".to_string(),
                "install any missing tool (west / bitbake) or fix the app's Zephyr CMakeLists"
                    .to_string(),
            ]
        } else {
            Vec::new()
        };
        let text = crate::style::render_report(g, "Build", "", &checks, &summary, &next_steps);
        CommandRun {
            exit,
            text,
            json: None,
        }
    };

    NativeBuildOutcome {
        manifest_written: manifest_outcome.write_failed_reason.is_none(),
        native_sim_target: manifest_outcome.native_sim_target,
        run,
    }
}

/// Absolute, `.`/`..`-collapsed form of `sdk_root`, anchored at `workspace_root`
/// — what `sdk_stamp_action` actually compares. An absolute `sdk_root` (`tan
/// sdk switch`'s own pointer, or an absolute `--sdk-root`) passes through
/// unchanged (`Path::join` discards the base for an absolute pushed path); a
/// relative one (a bare `--sdk-root ../alp-sdk`) is resolved against
/// `workspace_root` first, so it compares equal to the absolute form of the
/// SAME checkout instead of byte-mismatching it.
fn normalized_sdk_root_str(sdk_root: Option<&Path>, workspace_root: &Path) -> Option<String> {
    sdk_root.map(|p| {
        tan_core::normalize_path(&workspace_root.join(p))
            .to_string_lossy()
            .into_owned()
    })
}

/// The failure tail from an ALREADY-captured process `Output` (JSON mode) — a
/// pure read, no second spawn. Returns the last 4 non-empty output lines
/// joined by " | ", or an rc-only fallback when the captured streams were
/// empty. `None` only when the process actually succeeded (mirrors flash's
/// `capture_tail`, which the JSON slice-execution path lacked entirely).
fn capture_output_tail(out: &std::process::Output) -> Option<String> {
    if out.status.success() {
        return None;
    }
    let mut text = String::from_utf8_lossy(&out.stderr).into_owned();
    if text.trim().is_empty() {
        text = String::from_utf8_lossy(&out.stdout).into_owned();
    }
    let tail: Vec<&str> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    if tail.is_empty() {
        Some(format!("exited rc={}", out.status.code().unwrap_or(-1)))
    } else {
        Some(tail.join(" | "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tan_core::build_plan::parse_build_plan;

    /// A context with no SDK — `invoke_sdk_emit` errs, exercising the graceful
    /// degrade in `write_post_build_manifest` without a real planner.
    fn no_sdk_context() -> ProjectContext {
        ProjectContext {
            workspace_root: None,
            sdk_root: None,
            board_yaml_path: None,
            west_cwd: None,
            python_binary: "python3".to_string(),
        }
    }

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("{tag}-{}", std::process::id()))
    }

    #[test]
    fn normalized_sdk_root_str_treats_a_relative_and_absolute_form_as_equal() {
        // The reported thrash: `--sdk-root ../alp-sdk` (returned verbatim by
        // `resolve_sdk_root`) must stamp/compare identically to the absolute,
        // normalized pointer `tan sdk switch` already pins for the SAME
        // checkout — otherwise alternating between the two forms across
        // invocations pristines (a full wipe) every other build.
        let workspace_root = unique_temp_dir("normalize-sdk-root-ws");
        let sdk_abs = workspace_root.parent().unwrap().join("alp-sdk");

        let via_absolute = normalized_sdk_root_str(Some(&sdk_abs), &workspace_root);
        let via_relative = normalized_sdk_root_str(Some(Path::new("../alp-sdk")), &workspace_root);

        assert_eq!(via_absolute, via_relative);
    }

    #[test]
    fn native_execute_runs_commands_skips_commandless_and_reports() {
        use clap::Parser;
        // A real GlobalArgs in JSON mode (captures output, no stderr noise).
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        // A portable success command on every CI platform.
        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }},
                {{ "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2", "command": null }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices.len(), 2);
        assert_eq!(slices[0]["status"], "ok");
        assert_eq!(slices[1]["status"], "skipped");
        assert_eq!(slices[1]["reason"], "no command");
        // The build dir for the runnable slice was created.
        assert!(base.join("build/c1").is_dir());

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_refuses_a_plan_cwd_that_escapes_the_build_tree() {
        // Regression: `materialise.rs` and `manifest.rs` both refuse an
        // absolute/`..`-escaping plan path before touching disk; this executor
        // used to `base_path.join(&cmd.cwd)` and `create_dir_all` it verbatim,
        // so a plan-supplied `../../escape` would build (and later get
        // recorded as an absolute `build_dir`) OUTSIDE the project tree.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "command": { "tool": "true", "args": [], "cwd": "../../escape" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-unsafe-cwd");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        assert!(
            slices[0]["reason"].as_str().unwrap().contains("unsafe cwd"),
            "got: {slices:?}"
        );
        // Nothing was created outside the base dir.
        assert!(!base.join("../../escape").exists());

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_skips_missing_tool_and_exits_zero() {
        // SDK parity (alp-sdk#114): a slice whose build tool isn't on this host
        // (e.g. `bitbake` on a non-Yocto dev box) is skipped, not failed — the
        // whole run still exits 0.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "a32_cluster", "backend": "yocto", "buildDir": "build/a32",
              "command": { "tool": "alp-missing-tool-e114", "args": [], "cwd": "build/a32" } },
            { "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2", "command": null }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-missing-tool");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "skipped");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(
            reason.contains("not found in PATH; this is normal on non-"),
            "got: {reason}"
        );
        // The skip itself is issue-free (no error-severity issue): this
        // no-SDK test harness always fails the post-build manifest write, so
        // the only issue present is the expected `warning`-severity one — it
        // must never be `error` (which would flip `ok` to false).
        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues.iter().all(|i| i["severity"] == "warning"),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_fails_missing_tool_when_policy_says_fail() {
        // execution_policy.missingTool = "fail" flips the default skip: a slice
        // whose tool isn't on this host now FAILS the run (exit 1), instead of
        // the None-policy skip covered by the test above.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "a32_cluster", "backend": "yocto", "buildDir": "build/a32",
              "command": { "tool": "alp-missing-tool-e114", "args": [], "cwd": "build/a32" } }
          ],
          "executionPolicy": { "missingTool": "fail" },
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-missing-tool-fail");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        // The reason string the CLI shows is unchanged (only skip->fail routing).
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(
            reason.contains("not found in PATH; this is normal on non-"),
            "got: {reason}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn execute_slices_outcome_skips_a_demoted_slice_and_still_builds_the_other() {
        // tan-cli #89: a slice demoted by `apply_plan_token_substitution`
        // (its OWN fields still name an unresolved ${TOOLCHAIN_ROOT}) must
        // not cost the rest of the plan anything — the other slice builds
        // normally and the whole run stays exit 0 under the default (skip)
        // policy, exactly like a slice whose tool is simply missing.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }},
                {{ "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2",
                   "command": {{ "tool": "west", "args": ["build"], "cwd": "build/c2" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-toolchain-demoted-skip");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let demoted = vec![SliceDemotion {
            slice_index: 1,
            core_id: "c2".to_string(),
            reason: "plan field `slices[1].env.ZEPHYR_SDK_INSTALL_DIR` names ${TOOLCHAIN_ROOT}, \
                     but no toolchain install is detectable on this host — install the Zephyr \
                     SDK (`west sdk install`) or set ZEPHYR_SDK_INSTALL_DIR to an existing install"
                .to_string(),
        }];
        let outcome = execute_slices_outcome(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
            &demoted,
        );
        assert_eq!(outcome.run.exit.code(), 0);

        let env: serde_json::Value =
            serde_json::from_str(outcome.run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "ok");
        assert_eq!(slices[1]["status"], "skipped");
        let reason = slices[1]["reason"].as_str().unwrap();
        assert!(reason.contains("west sdk install"), "got: {reason}");

        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "build.toolchain-root-unresolved"
                    && i["severity"] == "warning"
                    && i["message"].as_str().unwrap().contains("c2")),
            "got: {issues:?}"
        );
        // A skip must never carry an error-severity issue (would flip `ok`).
        assert!(
            issues.iter().all(|i| i["severity"] != "error"),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn execute_slices_outcome_fails_a_demoted_slice_when_policy_says_fail() {
        // executionPolicy.missingTool = "fail" flips the default skip for a
        // demoted slice too — it reuses the SAME policy key a missing tool
        // does, so this is the same routing test as
        // `native_execute_fails_missing_tool_when_policy_says_fail`, just for
        // the #89 seam. The OTHER slice must still build (does-not-abort-
        // early is preserved) even though the whole run now exits non-zero.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }},
                {{ "coreId": "c2", "backend": "zephyr", "buildDir": "build/c2",
                   "command": {{ "tool": "west", "args": ["build"], "cwd": "build/c2" }} }}
              ],
              "executionPolicy": {{ "missingTool": "fail" }},
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-toolchain-demoted-fail");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let demoted = vec![SliceDemotion {
            slice_index: 1,
            core_id: "c2".to_string(),
            reason: "plan field `slices[1].env.ZEPHYR_SDK_INSTALL_DIR` names ${TOOLCHAIN_ROOT}, \
                     but no toolchain install is detectable on this host"
                .to_string(),
        }];
        let outcome = execute_slices_outcome(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
            &demoted,
        );
        assert_eq!(outcome.run.exit.code(), 1);

        let env: serde_json::Value =
            serde_json::from_str(outcome.run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "ok");
        assert_eq!(slices[1]["status"], "failed");

        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "build.toolchain-root-unresolved"
                    && i["severity"] == "error"
                    && i["message"].as_str().unwrap().contains("c2")),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_fails_unknown_backend_by_default() {
        // Regression: `Backend::Unknown` was added so ONE unrecognised backend
        // no longer fails the WHOLE plan to parse (build_plan.rs), but nothing
        // then enforced `executionPolicy.unknownBackend` — a slice naming an
        // unrecognised backend flowed straight into dispatch unchecked. The
        // CONSUMER contract default is Fail (unlike missingTool/nullCommand,
        // which default Skip).
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "linux_cluster", "backend": "linux", "buildDir": "build/linux" }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-unknown-backend-fail");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(reason.contains("linux"), "got: {reason}");

        // The raw backend string must be named in the envelope Issue, not
        // just the per-slice reason.
        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues.iter().any(|i| i["code"] == "build.unknown-backend"
                && i["severity"] == "error"
                && i["message"].as_str().unwrap().contains("linux")),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_skips_unknown_backend_when_policy_says_skip() {
        // executionPolicy.unknownBackend = "skip" flips the default fail: the
        // run stays exit 0 and the slice is reported skipped, not failed.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "linux_cluster", "backend": "linux", "buildDir": "build/linux" }
          ],
          "executionPolicy": { "unknownBackend": "skip" },
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-unknown-backend-skip");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "skipped");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(reason.contains("linux"), "got: {reason}");

        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues.iter().any(|i| i["code"] == "build.unknown-backend"
                && i["severity"] == "warning"
                && i["message"].as_str().unwrap().contains("linux")),
            "got: {issues:?}"
        );
        // A skip must never carry an error-severity issue (would flip `ok`).
        assert!(
            issues.iter().all(|i| i["severity"] != "error"),
            "got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_reports_failed_for_a_real_nonzero_exit() {
        // A genuinely present tool that exits nonzero is still "failed" (exit 1)
        // — the missing-tool skip path above must not swallow real failures.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "1"]"#)
        } else {
            ("sh", r#"["-c", "exit 1"]"#)
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-failing-tool");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], false);
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        assert_eq!(slices[0]["rc"], 1);

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_captures_failure_output_tail_in_json_mode() {
        // Regression: JSON mode ran the slice via `Command::output()` (which
        // CAPTURES stdout+stderr) and then threw the captured bytes away,
        // leaving `--format json` — the extension's only mode — with a bare
        // rc and zero diagnostics for a failed build. The tail of the
        // captured output must land in `reason`.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "echo boom 1>&2 && exit 1"]"#)
        } else {
            ("sh", r#"["-c", "echo boom 1>&2; exit 1"]"#)
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-captured-output");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(reason.contains("boom"), "got: {reason}");

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_reports_manifest_write_failure_as_warning_issue_in_json() {
        // Regression (CRITICAL): a failed post-build system-manifest.yaml write
        // used to be a bare `eprintln!` gated on text mode — invisible in
        // `--format json`, so `ok:true` looked identical whether or not the
        // manifest `tan flash` reads next was actually refreshed. It must now
        // surface as a `warning`-severity Issue without flipping `ok`/exit
        // (the manifest write is best-effort; the slices already succeeded).
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-manifest-warn");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        // no_sdk_context() has no `sdk_root` — `invoke_sdk_emit` inside
        // `write_post_build_manifest` always errs, exercising the failure path
        // without a real SDK checkout.
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        // The slice itself succeeded — a best-effort manifest write failure
        // must not fail the build.
        assert_eq!(run.exit.code(), 0);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(env["ok"], true);
        let issues = env["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["code"] == "build.manifest-write-failed" && i["severity"] == "warning"),
            "expected a manifest-write-failed warning issue, got: {issues:?}"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn native_execute_reports_build_dir_creation_failure_as_reason_in_json() {
        // Regression: a failed `create_dir_all` for the slice's build dir used
        // to print via `eprintln!` gated on `text_mode` only, leaving JSON
        // mode's `reason` as `None` — a bare `{"status":"failed"}` with no
        // diagnostic. The reason string must be populated in JSON mode too.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let json = r#"{
          "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "command": { "tool": "true", "args": [], "cwd": "build/c1" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let base = unique_temp_dir("alp-exec-build-dir-create-fail");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        // Pre-create a plain FILE at "build" so `create_dir_all("build/c1")`
        // fails: a path component exists and is not a directory.
        std::fs::write(base.join("build"), b"not a dir").unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );
        assert_eq!(run.exit.code(), 1);

        let env: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        let slices = env["data"]["slices"].as_array().unwrap();
        assert_eq!(slices[0]["status"], "failed");
        let reason = slices[0]["reason"].as_str().unwrap();
        assert!(reason.contains("cannot create build dir"), "got: {reason}");

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn execute_slices_outcome_threads_manifest_write_and_target_signals_out_of_the_build() {
        // Regression (root cause, third attempt): `tan run` must decide from
        // THIS build's own in-memory outcome, never from re-reading
        // `system-manifest.yaml` off disk afterward. This proves the plumbing
        // exists at all: `execute_slices_outcome` (not just `execute_slices`,
        // which discards it) surfaces `manifest_written` /
        // `native_sim_target` from the SAME `write_post_build_manifest` call
        // the build just made — `no_sdk_context()` has no `sdk_root`, so the
        // emit itself fails here, which must read as `manifest_written:
        // false` and `native_sim_target: None` (no signal), not silently
        // default to some other value.
        use clap::Parser;
        let g = crate::cli::Cli::parse_from(["alp", "--format", "json", "validate"]).global;

        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let base = unique_temp_dir("alp-exec-outcome-threading");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        let project = Project {
            root: None,
            board_yaml: None,
        };
        let outcome = execute_slices_outcome(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
            &[],
        );
        assert_eq!(outcome.run.exit.code(), 0);
        assert!(!outcome.manifest_written);
        assert_eq!(outcome.native_sim_target, None);

        std::fs::remove_dir_all(&base).ok();
    }

    /// A minimal fake SDK root: only what `resolve_sdk_root`'s
    /// `has_loader_script` marker check needs.
    fn fake_sdk_root(tag: &str) -> std::path::PathBuf {
        let d = unique_temp_dir(tag);
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("scripts")).unwrap();
        std::fs::write(d.join("scripts").join("alp_project.py"), "").unwrap();
        d
    }

    /// `GlobalArgs` with an explicit `--sdk-root`, so `sdk_root_str` resolves
    /// deterministically — unlike this module's other tests, which leave
    /// `--sdk-root` unset and rely on nothing resolving in the sandbox.
    fn global_with_sdk_root(sdk_root: &std::path::Path) -> GlobalArgs {
        use clap::Parser;
        crate::cli::Cli::parse_from([
            "alp",
            "--format",
            "json",
            "--sdk-root",
            sdk_root.to_str().unwrap(),
            "validate",
        ])
        .global
    }

    /// A one-slice plan whose command is a portable no-op (its exit status is
    /// never asserted by these tests — they only check the guard's
    /// filesystem/issue side effects). MUST actually be on PATH: the guard now
    /// runs AFTER the missing-tool skip (so a slice whose tool isn't installed
    /// is never wiped only to sit un-rebuilt), so a tool that doesn't resolve
    /// would `continue` before these tests' side effects ever land — `"true"`
    /// isn't a binary on Windows, hence the per-platform pick every other
    /// tool-spawning test in this file already uses.
    fn one_slice_plan_at(cwd: &str) -> BuildPlan {
        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0"]"#)
        } else {
            ("true", "[]")
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "{cwd}",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "{cwd}" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        parse_build_plan(&json).unwrap()
    }

    fn sdk_switch_issue_codes(json: &str) -> Vec<String> {
        let env: serde_json::Value = serde_json::from_str(json).unwrap();
        env["issues"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i["code"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn execute_slices_wipes_a_build_dir_stamped_for_a_different_sdk_root() {
        // The reported regression (issue #52): switching --sdk-root from
        // sdk_a to sdk_b must wipe m55_he's stale nested build dir (west's
        // own output, at <cwd>/build) BEFORE dispatch, instead of letting
        // west's FATAL ERROR abort the slice.
        let sdk_a = fake_sdk_root("sdkswitch-a");
        let sdk_b = fake_sdk_root("sdkswitch-b");
        let base = unique_temp_dir("exec-sdkswitch-wipe");
        let _ = std::fs::remove_dir_all(&base);
        let nested = base.join("build").join("c1").join("build");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("CMakeCache.txt"), "").unwrap();
        std::fs::write(
            nested.join(".tan-sdk-root"),
            sdk_a.to_string_lossy().as_ref(),
        )
        .unwrap();
        std::fs::write(nested.join("leftover-from-sdk-a.marker"), "x").unwrap();

        let g = global_with_sdk_root(&sdk_b);
        let plan = one_slice_plan_at("build/c1");
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );

        assert!(
            !nested.join("leftover-from-sdk-a.marker").exists(),
            "the stale build dir must be wiped"
        );
        let stamped = std::fs::read_to_string(nested.join(".tan-sdk-root")).unwrap();
        assert_eq!(stamped, sdk_b.to_string_lossy());
        let codes = sdk_switch_issue_codes(run.json.as_deref().unwrap());
        assert!(
            codes.contains(&"build.sdk-switch-pristine".to_string()),
            "{codes:?}"
        );

        std::fs::remove_dir_all(&base).ok();
        std::fs::remove_dir_all(&sdk_a).ok();
        std::fs::remove_dir_all(&sdk_b).ok();
    }

    #[test]
    fn execute_slices_keeps_a_build_dir_stamped_for_the_current_sdk_root() {
        // A build dir whose stamp already matches this run's --sdk-root is an
        // ordinary incremental rebuild, not a switch — must not be touched.
        let sdk_a = fake_sdk_root("sdkswitch-match-a");
        let base = unique_temp_dir("exec-sdkswitch-match");
        let _ = std::fs::remove_dir_all(&base);
        let nested = base.join("build").join("c1").join("build");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("CMakeCache.txt"), "").unwrap();
        std::fs::write(
            nested.join(".tan-sdk-root"),
            sdk_a.to_string_lossy().as_ref(),
        )
        .unwrap();
        std::fs::write(nested.join("incremental-state.marker"), "x").unwrap();

        let g = global_with_sdk_root(&sdk_a);
        let plan = one_slice_plan_at("build/c1");
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );

        assert!(
            nested.join("incremental-state.marker").exists(),
            "a matching stamp must not be wiped"
        );
        let codes = sdk_switch_issue_codes(run.json.as_deref().unwrap());
        assert!(
            !codes.contains(&"build.sdk-switch-pristine".to_string()),
            "{codes:?}"
        );

        std::fs::remove_dir_all(&base).ok();
        std::fs::remove_dir_all(&sdk_a).ok();
    }

    #[test]
    fn execute_slices_skips_the_wipe_when_the_build_dir_is_overridden() {
        // Guard 1: an explicit -d/--build-dir means west wrote somewhere this
        // check cannot know — mirrors resolve_zephyr_artefact's own refusal.
        let sdk_a = fake_sdk_root("sdkswitch-override-a");
        let sdk_b = fake_sdk_root("sdkswitch-override-b");
        let base = unique_temp_dir("exec-sdkswitch-override");
        let _ = std::fs::remove_dir_all(&base);
        let nested = base.join("build").join("c1").join("build");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("CMakeCache.txt"), "").unwrap();
        std::fs::write(
            nested.join(".tan-sdk-root"),
            sdk_a.to_string_lossy().as_ref(),
        )
        .unwrap();
        std::fs::write(nested.join("leftover-from-sdk-a.marker"), "x").unwrap();

        let g = global_with_sdk_root(&sdk_b);
        // Same portable-tool requirement as `one_slice_plan_at`: the `-d
        // ../elsewhere` override marker must ride along with a tool that
        // actually launches, or the missing-tool skip (which now runs first)
        // would `continue` before `build_dir_overridden` ever gets checked.
        let (tool, args) = if cfg!(windows) {
            ("cmd", r#"["/C", "exit", "0", "-d", "../elsewhere"]"#)
        } else {
            ("true", r#"["-d", "../elsewhere"]"#)
        };
        let json = format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "build",
              "slices": [
                {{ "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                   "command": {{ "tool": "{tool}", "args": {args}, "cwd": "build/c1" }} }}
              ],
              "sharedArtefacts": []
            }}"#
        );
        let plan = parse_build_plan(&json).unwrap();
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );

        assert!(
            nested.join("leftover-from-sdk-a.marker").exists(),
            "an overridden build dir must not be wiped"
        );
        let codes = sdk_switch_issue_codes(run.json.as_deref().unwrap());
        assert!(
            !codes.contains(&"build.sdk-switch-pristine".to_string()),
            "{codes:?}"
        );

        std::fs::remove_dir_all(&base).ok();
        std::fs::remove_dir_all(&sdk_a).ok();
        std::fs::remove_dir_all(&sdk_b).ok();
    }

    #[test]
    fn execute_slices_skips_the_wipe_when_cwd_is_outside_the_build_root() {
        // Guard 2: a plan cwd outside CONSUMER_BUILD_ROOT (here "src/c1")
        // would put the wipe target at <project>/src/c1/build — files the
        // build never created must never be touched.
        let sdk_a = fake_sdk_root("sdkswitch-outside-a");
        let sdk_b = fake_sdk_root("sdkswitch-outside-b");
        let base = unique_temp_dir("exec-sdkswitch-outside");
        let _ = std::fs::remove_dir_all(&base);
        let nested = base.join("src").join("c1").join("build");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(nested.join("CMakeCache.txt"), "").unwrap();
        std::fs::write(
            nested.join(".tan-sdk-root"),
            sdk_a.to_string_lossy().as_ref(),
        )
        .unwrap();
        std::fs::write(nested.join("user-owned.marker"), "x").unwrap();

        let g = global_with_sdk_root(&sdk_b);
        let plan = one_slice_plan_at("src/c1");
        let project = Project {
            root: None,
            board_yaml: None,
        };
        let run = execute_slices(
            &g,
            &no_sdk_context(),
            project,
            &plan,
            base.to_str().unwrap(),
        );

        assert!(
            nested.join("user-owned.marker").exists(),
            "a cwd outside the build root must not be wiped"
        );
        let codes = sdk_switch_issue_codes(run.json.as_deref().unwrap());
        assert!(
            !codes.contains(&"build.sdk-switch-pristine".to_string()),
            "{codes:?}"
        );

        std::fs::remove_dir_all(&base).ok();
        std::fs::remove_dir_all(&sdk_a).ok();
        std::fs::remove_dir_all(&sdk_b).ok();
    }
}
