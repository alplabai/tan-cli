// SPDX-License-Identifier: Apache-2.0
//! `tan-cli`'s IO/glue side of build-plan token substitution (alp-sdk #865,
//! "hermetic build plans"): resolves tan's already-resolved SDK root /
//! project root / planner python, drives the pure
//! [`tan_core::plan_tokens`] pass, and runs the one guard that pass can't do
//! itself — comparing a plan's `sdkCommit` against the resolved SDK
//! checkout's actual `git` HEAD (a subprocess call, so it belongs here, not
//! in `tan-core`).

use std::path::Path;
use std::process::Command;

use tan_core::ProjectContext;
use tan_core::build_plan::BuildPlan;
use tan_core::plan_tokens::{
    PLAN_PATH_MODE_TOKENED, PlanTokenError, TokenValues, project_root_diverges_from_exec_base,
    sdk_commit_mismatches, substitute_plan_tokens,
};

use crate::cli::GlobalArgs;

use super::workspace::resolved_planner_python;

/// A slice demoted by `tan_core::plan_tokens::substitute_plan_tokens` (tan-cli
/// #89): its fields still name a literal `${TOOLCHAIN_ROOT}` because this host
/// has none resolved, but the demotion has an owning slice AND an owning
/// dispatch seam (`executionPolicy.missingTool`) to route to, so it did not
/// fail the whole plan. `reason` is built HERE, not in `tan-core`, because
/// only this file has the resolved [`crate::toolchain::ToolchainRoot`] value
/// needed to phrase host-specific advice ("install one" vs "choose between
/// several") — `tan-core::plan_tokens` knows only that the token is unresolved,
/// not why.
#[derive(Debug)]
pub(super) struct SliceDemotion {
    /// Index into `plan.slices` — mirrors `tan_core::plan_tokens::DemotedSlice`
    /// so the executor's dispatch loop (index-keyed) can attribute this back
    /// to the right slice.
    pub(super) slice_index: usize,
    /// Carried alongside the index so the executor can key its lookup on
    /// BOTH — see `DemotedSlice`'s own doc for why an index-only signal is
    /// fragile if `plan.slices` is ever reordered/filtered between here and
    /// dispatch.
    pub(super) core_id: String,
    /// The full host-specific sentence the executor/materialise surface
    /// verbatim: which field, which token, and why it's unresolved on THIS
    /// host — the same advice text `build.toolchain-root-unresolved`'s
    /// plan-fatal sibling error already gives, just attached to a skip/fail
    /// instead of a hard refusal.
    pub(super) reason: String,
}

/// Apply the build-plan token-substitution pass to `plan` before materialise
/// writes anything or a slice command runs — called from EVERY route that
/// materialises or executes a plan (`native::native_build_outcome` and
/// `plan_modes::plan_command`'s `--materialise` branch), not just the
/// default native build: a plan-shaped bug doesn't care which flag reached
/// it. A no-op unless `plan.plan_path_mode` is present (every plan the SDK
/// emits today has none, so this returns `plan.clone()` unchanged); a
/// present-but-unrecognised mode is a hard error, never a half-applied guess.
/// `exec_base` is the executor's actual base dir (`native::base_dir` —
/// `west_cwd || workspace_root`); the caller resolves it once and passes it
/// in so this never re-derives a second, possibly different, value.
///
/// `toolchain` is likewise resolved ONCE by the caller (`native_build_outcome`
/// / `plan_command`, both via `crate::toolchain::resolve_toolchain_root()`)
/// and handed in, rather than this function calling the resolver itself: a
/// previous version called `resolve_toolchain_root()` internally, which made
/// this function's OWN unit tests silently host-dependent — they assumed
/// `NotFound` but actually got whatever `ZEPHYR_SDK_INSTALL_DIR`/`/opt`/`$HOME`
/// happen to resolve to on the machine running `cargo test`, passing on a
/// clean CI box and failing on any developer machine with a real Zephyr SDK
/// installed. Threading it as a parameter lets a test inject a fixed
/// `ToolchainRoot` and stay deterministic regardless of ambient host state.
pub(super) fn apply_plan_token_substitution(
    g: &GlobalArgs,
    context: &ProjectContext,
    exec_base: &str,
    plan: &BuildPlan,
    toolchain: &crate::toolchain::ToolchainRoot,
) -> Result<(BuildPlan, Vec<SliceDemotion>), (&'static str, String)> {
    match plan.plan_path_mode.as_deref() {
        None => return Ok((plan.clone(), Vec::new())),
        Some(PLAN_PATH_MODE_TOKENED) => {}
        Some(other) => {
            return Err((
                "build.plan-invalid",
                format!(
                    "unknown planPathMode `{other}` (only \"tokened\" is defined) — refusing \
                     rather than silently treating it as legacy or applying token substitution"
                ),
            ));
        }
    }

    // Guard: PROJECT_ROOT (board.yaml's directory) vs the executor's actual
    // base dir. They coincide only in the default config (board.yaml at the
    // workspace root, no west_cwd override) — a tokened plan substituting
    // `${PROJECT_ROOT}` from one while slices actually run under the other
    // would silently build against the wrong tree.
    let project_root = context
        .board_yaml_path
        .as_deref()
        .and_then(parent_dir)
        .ok_or((
            "build.plan-invalid",
            "a `planPathMode: tokened` plan needs a resolved board.yaml to derive \
             ${PROJECT_ROOT} from — pass `--board-yaml <PATH>` or run from a project."
                .to_string(),
        ))?;
    if project_root_diverges_from_exec_base(&project_root, exec_base) {
        return Err((
            "build.project-root-mismatch",
            format!(
                "plan is `planPathMode: tokened`, but ${{PROJECT_ROOT}} (`{project_root}`, the \
                 board.yaml directory) differs from where tan actually runs each slice's \
                 command (`{exec_base}`) — refusing to build against the wrong tree. This only \
                 happens with a non-default `west_cwd`/`--project` split; align them, or drop \
                 `planPathMode` from the plan."
            ),
        ));
    }

    // ONE resolved sdk_root for the whole substitution — never re-resolved
    // per-slice — the same value `execute_slices_outcome` already resolves
    // for ZEPHYR_BASE/env-gap filling (`crate::util::resolve_sdk_root`). A
    // tokened plan NEEDS a real ${SDK_ROOT} value: degrading an unresolved
    // sdk_root to `""` would substitute `${SDK_ROOT}/scripts` into the bare
    // relative path `/scripts`, sailing straight past the leftover-token
    // guard (there's no token left to catch) — refuse instead.
    let Some(sdk_root) = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g))
    else {
        return Err((
            "build.sdk-root-unresolved",
            "plan is `planPathMode: tokened` (needs ${SDK_ROOT} substituted with a real path), \
             but no alp-sdk checkout resolved — pass `--sdk-root <PATH>`, pin one with `tan sdk \
             switch`, or run from a project near an alp-sdk checkout."
                .to_string(),
        ));
    };
    let sdk_root_str = sdk_root.to_string_lossy().into_owned();

    // The two-SDK split-brain guard: whenever the plan carries a `sdkCommit`
    // — the live emit and a captured `--plan-from` file alike — a mismatch
    // against the resolved SDK checkout's actual HEAD means tan is about to
    // build against a DIFFERENT SDK checkout/SHA than the plan was emitted
    // from.
    if let Some(plan_commit) = plan.sdk_commit.as_deref() {
        let resolved_commit = git_short_head(&sdk_root).unwrap_or_default();
        if sdk_commit_mismatches(plan_commit, &resolved_commit) {
            return Err((
                "build.sdk-commit-mismatch",
                format!(
                    "plan was emitted from alp-sdk commit `{plan_commit}`, but the resolved SDK \
                     checkout is at `{resolved_commit}` — building against a different SDK \
                     checkout than the plan was captured from can silently produce the wrong \
                     image. Re-emit the plan from the current SDK checkout, or point \
                     `--sdk-root` at the checkout the plan names."
                ),
            ));
        }
    }

    // ${TOOLCHAIN_ROOT} (ADR 0021): resolved LAZILY by the caller — an
    // unresolvable toolchain root arrives here as `ToolchainRoot::NotFound`/
    // `Ambiguous`, not refused, so the plans the SDK emits today (which name
    // no `${TOOLCHAIN_ROOT}`) keep building on a host with no detectable
    // toolchain install. The refusal fires inside the pass, only when a plan
    // actually uses the token.
    let toolchain_root = toolchain.path().map(|p| p.to_string_lossy().into_owned());

    let python = resolved_planner_python(context);
    let values = TokenValues {
        sdk_root: &sdk_root_str,
        project_root: &project_root,
        python: &python,
        toolchain_root: toolchain_root.as_deref(),
    };
    let (plan, demoted) = substitute_plan_tokens(plan, &values).map_err(|e| match e {
        PlanTokenError::LeftoverToken { field, token } => (
            "build.plan-token-unresolved",
            format!(
                "plan is `planPathMode: tokened` but field `{field}` still names the literal \
                 token `{token}` after substitution — an SDK-side token this CLI does not \
                 resolve (only ${{SDK_ROOT}}, ${{PROJECT_ROOT}}, ${{PYTHON}}, \
                 ${{TOOLCHAIN_ROOT}} are known). Upgrade tan, or check the plan for a bug."
            ),
        ),
        // Distinct from the above on purpose: tan KNOWS this token, so
        // "upgrade tan" is the wrong advice — the host is missing (or has
        // several of) the thing it names. Only reachable for a PLAN-LEVEL
        // field now (boardYaml / sharedArtefacts[]) — a slice-confined
        // occurrence is reported as a `DemotedSlice` instead (tan-cli #89),
        // mapped to `SliceDemotion` below rather than erroring here.
        PlanTokenError::UnresolvedToolchainRoot { field } => (
            "build.toolchain-root-unresolved",
            format!(
                "plan field `{field}` names ${{TOOLCHAIN_ROOT}}, but {}. tan refuses rather than \
                 substituting an empty path, which would silently build against the host root.",
                describe_unresolved(toolchain)
            ),
        ),
        PlanTokenError::UnknownPlanPathMode(mode) => (
            "build.plan-invalid",
            format!("unknown planPathMode `{mode}` (only \"tokened\" is defined)"),
        ),
    })?;

    // `toolchain` is the caller's ONE resolved value for this whole pass
    // (see the function doc) — reused here for the demotion's advice text
    // instead of a second resolution, so a demoted slice's message and a
    // plan-fatal `UnresolvedToolchainRoot`'s message (above) never disagree
    // about why.
    let demoted = demoted
        .into_iter()
        .map(|d| {
            let reason = format!(
                "plan field `{}` names ${{TOOLCHAIN_ROOT}}, but {}",
                d.field,
                describe_unresolved(toolchain)
            );
            SliceDemotion {
                slice_index: d.slice_index,
                core_id: d.core_id,
                reason,
            }
        })
        .collect();
    Ok((plan, demoted))
}

/// Why [`crate::toolchain::resolve_toolchain_root`] produced no single path,
/// phrased to complete the sentence "…names ${TOOLCHAIN_ROOT}, but {this}."
/// The two causes need opposite fixes — install one vs. choose between the
/// ones already installed — so they must not share a message.
fn describe_unresolved(toolchain: &crate::toolchain::ToolchainRoot) -> String {
    match toolchain {
        crate::toolchain::ToolchainRoot::Ambiguous(candidates) => {
            let list = candidates
                .iter()
                .map(|p| p.to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "this host has several toolchain installs and no `ZEPHYR_SDK_INSTALL_DIR` to \
                 choose between them ({list}) — set `ZEPHYR_SDK_INSTALL_DIR` to the one this \
                 build should use"
            )
        }
        // `Resolved` cannot reach here (a resolved root is substituted), but
        // it shares the "nothing usable" advice rather than being unreachable!().
        crate::toolchain::ToolchainRoot::NotFound
        | crate::toolchain::ToolchainRoot::Resolved(_) => {
            "no toolchain install is detectable on this host — install the Zephyr SDK (`west sdk \
             install`) or set `ZEPHYR_SDK_INSTALL_DIR` to an existing install"
                .to_string()
        }
    }
}

/// Parent directory of `board_yaml_path`, forward-slash (matches
/// `ProjectContext`'s own `to_posix` convention, `project.rs`).
fn parent_dir(board_yaml_path: &str) -> Option<String> {
    Path::new(board_yaml_path)
        .parent()
        .map(|p| p.to_string_lossy().replace('\\', "/"))
}

/// `git rev-parse --short HEAD` in `sdk_root`. `None` when `git` is missing,
/// `sdk_root` isn't a git checkout, or the command otherwise fails — all
/// "no signal" to the caller (`sdk_commit_mismatches` never flags an absent
/// commit as a mismatch), not a hard failure: an SDK checkout with no `.git`
/// (a release tarball) is a normal, supported setup.
fn git_short_head(sdk_root: &Path) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(sdk_root)
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let head = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!head.is_empty()).then_some(head)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tan_core::build_plan::parse_build_plan;

    fn context(board_yaml_path: &str, sdk_root: &str) -> ProjectContext {
        ProjectContext {
            workspace_root: Some("/work/proj".to_string()),
            sdk_root: Some(sdk_root.to_string()),
            board_yaml_path: Some(board_yaml_path.to_string()),
            west_cwd: Some("/work/proj".to_string()),
            python_binary: "python3".to_string(),
        }
    }

    /// A real temp dir marked as an SDK root (`scripts/alp_project.py`), so
    /// `--sdk-root <dir>` makes `crate::util::resolve_sdk_root` deterministically
    /// resolve to it (`resolve_sdk_root` requires the loader marker to exist on
    /// disk — it's not just a string match).
    fn sdk_root_dir(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("tan-tok-sdk-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("alp_project.py"), b"").unwrap();
        dir
    }

    fn global() -> GlobalArgs {
        use clap::Parser;
        crate::cli::Cli::parse_from(["tan", "--format", "json", "validate"]).global
    }

    fn global_with_sdk_root(sdk_root: &std::path::Path) -> GlobalArgs {
        use clap::Parser;
        crate::cli::Cli::parse_from([
            "tan",
            "--format",
            "json",
            "--sdk-root",
            &sdk_root.to_string_lossy(),
            "validate",
        ])
        .global
    }

    /// A fixed, host-independent `ToolchainRoot` for every test below that
    /// doesn't itself exercise toolchain resolution: `apply_plan_token_
    /// substitution` used to call `crate::toolchain::resolve_toolchain_root()`
    /// internally, which made these tests depend on whether THIS machine
    /// happens to have a Zephyr SDK install (passing on a clean CI box,
    /// failing on any developer machine with `ZEPHYR_SDK_INSTALL_DIR` set or
    /// a `zephyr-sdk-*` under `$HOME`/`/opt`). Threading the value in as a
    /// parameter (this helper) is what makes these tests deterministic.
    fn no_toolchain() -> crate::toolchain::ToolchainRoot {
        crate::toolchain::ToolchainRoot::NotFound
    }

    const LEGACY_PLAN: &str = r#"{
      "schemaVersion": 1, "boardYaml": "/work/proj/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": []
    }"#;

    #[test]
    fn legacy_plan_is_untouched_no_op() {
        let plan = parse_build_plan(LEGACY_PLAN).unwrap();
        let ctx = context("/work/proj/board.yaml", "/opt/alp-sdk");
        let (out, demoted) =
            apply_plan_token_substitution(&global(), &ctx, "/work/proj", &plan, &no_toolchain())
                .expect("legacy plan must not error");
        assert_eq!(out, plan);
        assert!(demoted.is_empty());
    }

    #[test]
    fn unknown_plan_path_mode_is_refused_before_any_guard_runs() {
        // A `sdk_root`/`board.yaml` that would ALSO fail the divergence guard
        // — proving the unknown-mode check short-circuits first, rather than
        // half-executing the tokened-plan guards for a mode that isn't
        // "tokened" (and reporting a misleading "planPathMode: tokened" error).
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened-v2",
          "boardYaml": "/work/proj/examples/foo/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let ctx = context("/work/proj/examples/foo/board.yaml", "/opt/alp-sdk");
        let err =
            apply_plan_token_substitution(&global(), &ctx, "/work/proj", &plan, &no_toolchain())
                .expect_err("unknown mode must be refused");
        assert_eq!(err.0, "build.plan-invalid");
        assert!(err.1.contains("tokened-v2"), "got: {}", err.1);
    }

    #[test]
    fn project_root_mismatch_is_refused() {
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        // board.yaml lives nested under the workspace root, but the exec base
        // stays the workspace root itself — a real PROJECT_ROOT/exec-base split.
        let ctx = context("/work/proj/examples/foo/board.yaml", "/opt/alp-sdk");
        let err =
            apply_plan_token_substitution(&global(), &ctx, "/work/proj", &plan, &no_toolchain())
                .expect_err("divergence must be refused");
        assert_eq!(err.0, "build.project-root-mismatch");
        assert!(err.1.contains("examples/foo"), "got: {}", err.1);
    }

    #[test]
    fn unresolved_sdk_root_is_refused_not_substituted_empty() {
        // Regression: a tokened plan with no resolvable sdk_root used to
        // degrade `${SDK_ROOT}` to `""`, e.g. turning `${SDK_ROOT}/scripts`
        // into the bare `/scripts` — sailing right past the leftover-token
        // guard. Must refuse instead.
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
              "env": { "ALP_SDK_ROOT": "${SDK_ROOT}/scripts" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let ctx = context("/work/proj/board.yaml", "/opt/alp-sdk");
        // No `--sdk-root` and a cwd that isn't an SDK checkout -> unresolved.
        let err =
            apply_plan_token_substitution(&global(), &ctx, "/work/proj", &plan, &no_toolchain())
                .expect_err("unresolved sdk_root must be refused");
        assert_eq!(err.0, "build.sdk-root-unresolved");
    }

    #[test]
    fn tokened_plan_with_matching_project_root_substitutes() {
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
              "env": { "ALP_SDK_ROOT": "${SDK_ROOT}" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let sdk_root = sdk_root_dir("substitutes");
        let ctx = context("/work/proj/board.yaml", &sdk_root.to_string_lossy());
        let (out, demoted) = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            "/work/proj",
            &plan,
            &no_toolchain(),
        )
        .expect("matching project root must substitute");
        assert_eq!(out.board_yaml, "/work/proj/board.yaml");
        assert_eq!(
            out.slices[0].env.get("ALP_SDK_ROOT").map(String::as_str),
            Some(sdk_root.to_string_lossy().into_owned().as_str())
        );
        assert!(demoted.is_empty());

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn slice_confined_toolchain_root_is_demoted_not_a_hard_error() {
        // tan-cli #89: an unresolved ${TOOLCHAIN_ROOT} confined to one
        // slice's own field must not fail the whole substitution pass — it
        // comes back as a `SliceDemotion`, carrying the same host-specific
        // advice `describe_unresolved` gives the plan-fatal sibling error,
        // for the executor to route through `executionPolicy.missingTool` at
        // dispatch instead of erroring here.
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "m33_sm", "backend": "zephyr", "buildDir": "build/c1",
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
              "env": { "ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}" } }
          ],
          "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let sdk_root = sdk_root_dir("slice-demotion");
        let ctx = context("/work/proj/board.yaml", &sdk_root.to_string_lossy());
        // Inject `NotFound` explicitly (see `no_toolchain`'s doc) instead of
        // relying on this machine having no real Zephyr SDK install.
        let (out, demoted) = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            "/work/proj",
            &plan,
            &no_toolchain(),
        )
        .expect("a slice-confined unresolved toolchain root must not fail the plan");

        assert_eq!(demoted.len(), 1, "expected exactly one demotion");
        let d = &demoted[0];
        assert_eq!(d.slice_index, 0);
        assert_eq!(d.core_id, "m33_sm");
        // `field` was folded into `reason` (and the standalone field dropped
        // — it was read only by this assertion, per MINOR 5 of the #89
        // review): assert on `reason` instead.
        assert!(
            d.reason.contains("slices[0].env.ZEPHYR_SDK_INSTALL_DIR"),
            "got: {}",
            d.reason
        );
        assert!(
            d.reason.contains("west sdk install") || d.reason.contains("ZEPHYR_SDK_INSTALL_DIR"),
            "got: {}",
            d.reason
        );
        // The literal token survives in the (never-dispatched) output plan —
        // fenced by the executor's index check, not substituted blank.
        assert_eq!(
            out.slices[0]
                .env
                .get("ZEPHYR_SDK_INSTALL_DIR")
                .map(String::as_str),
            Some("${TOOLCHAIN_ROOT}")
        );

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn unresolved_toolchain_root_advice_differs_by_cause() {
        use crate::toolchain::ToolchainRoot;

        // Nothing installed: tell them to install one.
        let none = describe_unresolved(&ToolchainRoot::NotFound);
        assert!(none.contains("west sdk install"), "got: {none}");

        // Several installed: tell them to choose, and name the candidates —
        // "install the Zephyr SDK" would be actively wrong advice here.
        let many = describe_unresolved(&ToolchainRoot::Ambiguous(vec![
            std::path::PathBuf::from("/opt/zephyr-sdk-0.16.5"),
            std::path::PathBuf::from("/opt/zephyr-sdk-1.0.1"),
        ]));
        assert!(many.contains("ZEPHYR_SDK_INSTALL_DIR"), "got: {many}");
        assert!(many.contains("/opt/zephyr-sdk-0.16.5"), "got: {many}");
        assert!(many.contains("/opt/zephyr-sdk-1.0.1"), "got: {many}");
        assert!(!many.contains("west sdk install"), "got: {many}");
    }

    #[test]
    fn leftover_token_after_substitution_is_refused() {
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${UNKNOWN}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let sdk_root = sdk_root_dir("leftover-token");
        let ctx = context("/work/proj/board.yaml", &sdk_root.to_string_lossy());
        let err = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            "/work/proj",
            &plan,
            &no_toolchain(),
        )
        .expect_err("leftover token must be refused");
        assert_eq!(err.0, "build.plan-token-unresolved");
        assert!(err.1.contains("${UNKNOWN}"), "got: {}", err.1);

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn missing_git_head_is_no_signal_not_a_hard_error() {
        // A resolved SDK root that is NOT a git checkout at all (no `.git`) —
        // the sdkCommit guard must treat "could not resolve HEAD" as no
        // signal (an SDK release tarball is a normal, supported setup), not
        // fail the build outright.
        let sdk_root = sdk_root_dir("nogit");

        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened", "sdkCommit": "deadbee",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let ctx = context("/work/proj/board.yaml", &sdk_root.to_string_lossy());
        let result = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            "/work/proj",
            &plan,
            &no_toolchain(),
        );
        assert!(result.is_ok(), "got: {result:?}");

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn sdk_commit_mismatch_is_refused_even_without_plan_from() {
        // Regression: the split-brain guard used to fire ONLY when
        // `args.plan_from` was set — but `sdkCommit` is a top-level field the
        // SDK's LIVE emit can carry too, so a live-emit mismatch used to sail
        // through unchecked. Must fire whenever `plan.sdkCommit` is present,
        // regardless of how the plan was acquired.
        let sdk_root = sdk_root_dir("commit-mismatch-live");
        let git = |args: &[&str]| {
            std::process::Command::new("git")
                .arg("-C")
                .arg(&sdk_root)
                .args(args)
                .output()
                .expect("git must be on PATH for this test")
        };
        assert!(git(&["init", "-q"]).status.success());
        assert!(
            git(&[
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "x"
            ])
            .status
            .success()
        );
        let head = String::from_utf8_lossy(&git(&["rev-parse", "--short", "HEAD"]).stdout)
            .trim()
            .to_string();

        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened", "sdkCommit": "0000000",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        assert_ne!(plan.sdk_commit.as_deref(), Some(head.as_str()));
        let ctx = context("/work/proj/board.yaml", &sdk_root.to_string_lossy());
        // No `--plan-from` anywhere in this call — this IS the "live emit"
        // shape the dead-code bug missed.
        let err = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            "/work/proj",
            &plan,
            &no_toolchain(),
        )
        .expect_err("sdkCommit mismatch must be refused regardless of plan_from");
        assert_eq!(err.0, "build.sdk-commit-mismatch");
        assert!(err.1.contains("0000000"), "got: {}", err.1);

        std::fs::remove_dir_all(&sdk_root).ok();
    }
}
