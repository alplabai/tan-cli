// SPDX-License-Identifier: Apache-2.0
//! `tan-cli`'s IO/glue side of build-plan token substitution (alp-sdk #865,
//! "hermetic build plans"): resolves tan's already-resolved SDK root /
//! project root / planner python, drives the pure
//! [`tan_core::plan_tokens`] pass, and runs the one guard that pass can't do
//! itself — comparing a `--plan-from` plan's `sdkCommit` against the resolved
//! SDK checkout's actual `git` HEAD (a subprocess call, so it belongs here,
//! not in `tan-core`).

use std::path::Path;
use std::process::Command;

use tan_core::ProjectContext;
use tan_core::build_plan::BuildPlan;
use tan_core::plan_tokens::{
    PlanTokenError, TokenValues, project_root_diverges_from_exec_base, sdk_commit_mismatches,
    substitute_plan_tokens,
};

use crate::cli::{BuildArgs, GlobalArgs};

use super::workspace::resolved_planner_python;

/// Apply the build-plan token-substitution pass to `plan` before materialise
/// writes anything or a slice command runs: a no-op unless `plan` carries
/// `planPathMode: "tokened"` (every plan the SDK emits today does not, so
/// this returns `plan.clone()` unchanged). `exec_base` is the executor's
/// actual base dir (`native::base_dir` — `west_cwd || workspace_root`); the
/// caller resolves it once and passes it in so this never re-derives a
/// second, possibly different, value.
pub(super) fn apply_plan_token_substitution(
    g: &GlobalArgs,
    context: &ProjectContext,
    args: &BuildArgs,
    exec_base: &str,
    plan: &BuildPlan,
) -> Result<BuildPlan, (&'static str, String)> {
    if plan.plan_path_mode.is_none() {
        return Ok(plan.clone());
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
    // for ZEPHYR_BASE/env-gap filling (`crate::util::resolve_sdk_root`).
    let sdk_root = crate::util::resolve_sdk_root(g, &crate::util::cli_workspace_root(g));
    let sdk_root_str = sdk_root
        .as_deref()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();

    // The two-SDK split-brain guard: when running from a captured plan file,
    // a `sdkCommit` mismatch against the resolved SDK checkout's actual HEAD
    // means the plan was emitted from a DIFFERENT SDK checkout/SHA than tan
    // is about to build against.
    if let Some(plan_from) = &args.plan_from {
        if let Some(plan_commit) = plan.sdk_commit.as_deref() {
            let resolved_commit = sdk_root
                .as_deref()
                .and_then(git_short_head)
                .unwrap_or_default();
            if sdk_commit_mismatches(plan_commit, &resolved_commit) {
                return Err((
                    "build.sdk-commit-mismatch",
                    format!(
                        "`--plan-from {plan_from}` was emitted from alp-sdk commit \
                         `{plan_commit}`, but the resolved SDK checkout is at \
                         `{resolved_commit}` — building against a different SDK checkout than \
                         the plan was captured from can silently produce the wrong image. \
                         Re-emit the plan from the current SDK checkout, or point `--sdk-root` \
                         at the checkout the plan names."
                    ),
                ));
            }
        }
    }

    let python = resolved_planner_python(context);
    let values = TokenValues {
        sdk_root: &sdk_root_str,
        project_root: &project_root,
        python: &python,
    };
    substitute_plan_tokens(plan, &values).map_err(|e| {
        let PlanTokenError::LeftoverToken { field, token } = e;
        (
            "build.plan-token-unresolved",
            format!(
                "plan is `planPathMode: tokened` but field `{field}` still names the literal \
                 token `{token}` after substitution — an SDK-side token this CLI does not \
                 resolve (only ${{SDK_ROOT}}, ${{PROJECT_ROOT}}, ${{PYTHON}} are known). Upgrade \
                 tan, or check the plan for a bug."
            ),
        )
    })
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

    fn args(plan_from: Option<&str>) -> BuildArgs {
        BuildArgs {
            plan: false,
            plan_from: plan_from.map(str::to_string),
            materialise: false,
            native: true,
            manifest: false,
            manifest_from: None,
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

    const LEGACY_PLAN: &str = r#"{
      "schemaVersion": 1, "boardYaml": "/work/proj/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": []
    }"#;

    #[test]
    fn legacy_plan_is_untouched_no_op() {
        let plan = parse_build_plan(LEGACY_PLAN).unwrap();
        let ctx = context("/work/proj/board.yaml", "/opt/alp-sdk");
        let out = apply_plan_token_substitution(&global(), &ctx, &args(None), "/work/proj", &plan)
            .expect("legacy plan must not error");
        assert_eq!(out, plan);
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
        let err = apply_plan_token_substitution(&global(), &ctx, &args(None), "/work/proj", &plan)
            .expect_err("divergence must be refused");
        assert_eq!(err.0, "build.project-root-mismatch");
        assert!(err.1.contains("examples/foo"), "got: {}", err.1);
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
        let out = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            &args(None),
            "/work/proj",
            &plan,
        )
        .expect("matching project root must substitute");
        assert_eq!(out.board_yaml, "/work/proj/board.yaml");
        assert_eq!(
            out.slices[0].env.get("ALP_SDK_ROOT").map(String::as_str),
            Some(sdk_root.to_string_lossy().into_owned().as_str())
        );

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn leftover_token_after_substitution_is_refused() {
        let json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${UNKNOWN}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [], "sharedArtefacts": []
        }"#;
        let plan = parse_build_plan(json).unwrap();
        let ctx = context("/work/proj/board.yaml", "/opt/alp-sdk");
        let err = apply_plan_token_substitution(&global(), &ctx, &args(None), "/work/proj", &plan)
            .expect_err("leftover token must be refused");
        assert_eq!(err.0, "build.plan-token-unresolved");
        assert!(err.1.contains("${UNKNOWN}"), "got: {}", err.1);
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
            &args(Some("plan.json")),
            "/work/proj",
            &plan,
        );
        assert!(result.is_ok(), "got: {result:?}");

        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn sdk_commit_mismatch_on_plan_from_is_refused() {
        // The plan claims a different SDK commit than the resolved checkout's
        // real HEAD — the two-SDK split-brain guard must refuse the build.
        let sdk_root = sdk_root_dir("commit-mismatch");
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
        let err = apply_plan_token_substitution(
            &global_with_sdk_root(&sdk_root),
            &ctx,
            &args(Some("plan.json")),
            "/work/proj",
            &plan,
        )
        .expect_err("sdkCommit mismatch on --plan-from must be refused");
        assert_eq!(err.0, "build.sdk-commit-mismatch");
        assert!(err.1.contains("0000000"), "got: {}", err.1);

        std::fs::remove_dir_all(&sdk_root).ok();
    }
}
