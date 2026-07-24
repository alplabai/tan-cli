// SPDX-License-Identifier: Apache-2.0
//! `tan build` — the build workflow entry.
//!
//! `tan build` is native now: it consumes the SDK's `--emit build-plan`,
//! materialises the per-slice files, and runs each slice's command directly
//! (`native::native_build_outcome`) — no `west alp-build` extension command
//! involved. `tan flash`
//! and `tan renode` are likewise their own native commands (`commands::flash` /
//! `commands::renode`), as is `tan image` (`commands::image`). The per-core /
//! per-platform routing (Zephyr→`west build`, Yocto→`bitbake`, baremetal→CMake +
//! vendor toolchain) stays in the SDK's orchestrator (`alp_orchestrate.py`); the
//! CLI never re-decides the backend.
//!
//! `run` (aka `commands::build::run`) is the remaining `west`-delegating entry —
//! now used ONLY by `Command::{Migrate,Lock,Quality}` (→ `west alp-migrate` /
//! `alp-lock` / `alp-quality`, which survive ADR-0020 Phase 4). `west alp-build`
//! itself is deleted under Phase 4, so `tan build` no longer routes through `run`.
//!
//! Text mode inherits stdio so the build streams live in the caller's terminal;
//! JSON mode captures + emits a single envelope.

mod execute;
mod materialise;
mod native;
mod plan_modes;
mod preflight;
mod token_substitution;
mod workspace;

use serde::Serialize;

use super::CommandRun;
use crate::cli::{BuildArgs, GlobalArgs};

use native::native_build_outcome;
use plan_modes::{manifest_command, plan_command};

// `run` is the `west`-delegating entry (now only `migrate`/`lock`/`quality`)
// and `probe_build_preflight` is shared with `tan doctor --build`; both stay
// importable at `crate::commands::build::*` for `main.rs` / `doctor.rs`.
pub(crate) use execute::NativeBuildOutcome;
pub(crate) use preflight::probe_build_preflight;
pub use workspace::run;
// Shared with `commands::kconfig` (#35): the one `--emit` spawn path, the one
// `ZEPHYR_BASE` resolver, and the one exec-base derivation, so it drives
// `alp_orchestrate --emit kconfig --core <id>` through the SAME mechanism
// `tan build` already uses instead of re-deriving any of them.
pub(crate) use native::base_dir;
pub(crate) use workspace::{invoke_sdk_emit, resolve_zephyr_base};

/// Envelope `data` for the `west`-delegating path: the `alp-*` command run, its
/// cwd, and the forwarded args.
#[derive(Serialize)]
struct BuildData {
    /// Envelope `data` schema version (currently `"1"`).
    #[serde(rename = "schemaVersion")]
    schema_version: String,
    /// The `west alp-*` command invoked (e.g. `alp-build`).
    #[serde(rename = "westCommand")]
    west_command: String,
    /// Working directory the `west` command ran in.
    #[serde(rename = "westCwd")]
    west_cwd: String,
    /// Passthrough args forwarded verbatim after the subcommand.
    args: Vec<String>,
}

/// `tan build --native`'s full outcome (envelope + the in-memory
/// manifest-written/native_sim-target signals) — `tan run` (`commands::run`)
/// calls this INSTEAD of [`run_build`] so it decides host-vs-hardware from
/// THIS build's own result, never by re-reading `system-manifest.yaml` off
/// disk afterward (the root cause the third fix attempt at this defect
/// closes — see `tan_core::run`'s module doc). `run` always builds with
/// `--native` (never `--manifest`/`--plan`/`--materialise`), so this always
/// takes the native path — unlike [`run_build`], it doesn't branch on those
/// flags at all.
pub fn run_build_native_outcome(g: &GlobalArgs, args: &BuildArgs) -> NativeBuildOutcome {
    native_build_outcome(g, args)
}

/// `tan build` entry. `--manifest` shows the system manifest; `--plan` /
/// `--materialise` consume + show/write the plan; otherwise (and with `--native`)
/// run the CLI-native build: consume the plan, materialise its files, then run
/// each slice's command directly, so no `west alp-build` extension command is
/// needed. A thin wrapper around [`run_build_native_outcome`] for the native
/// case — kept as its own unchanged entry point (signature + behavior) since
/// `main.rs`/`doctor.rs` depend on it returning a bare `CommandRun`.
pub fn run_build(g: &GlobalArgs, args: &BuildArgs) -> CommandRun {
    if args.manifest || args.manifest_from.is_some() {
        manifest_command(g, args)
    } else if args.plan || args.plan_from.is_some() || args.materialise {
        plan_command(g, args)
    } else {
        run_build_native_outcome(g, args).run
    }
}

#[cfg(test)]
mod plan_from_materialise_e2e_tests {
    // End-to-end at the actual `main.rs` entry point (`run_build`, parsed
    // from real argv via clap) — not a direct call into
    // `token_substitution::apply_plan_token_substitution` with hand-built
    // args. A direct call is exactly how the routing bug (`--plan-from
    // --materialise` never reached the substitution pass at all, because
    // `run_build` sends any `plan_from.is_some()` to `plan_command`, never
    // `native_build_outcome`) went uncaught: it never exercised the
    // `if args.plan_from.is_some() { plan_command } ` branch this test does.
    use clap::Parser;

    use super::run_build;
    use crate::cli::{Cli, Command};

    fn sdk_root_dir(tag: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("tan-build-e2e-sdk-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        std::fs::write(dir.join("scripts").join("alp_project.py"), b"").unwrap();
        dir
    }

    fn project_dir(tag: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("tan-build-e2e-proj-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn plan_from_materialise_substitutes_tokens_via_real_cli_routing() {
        let sdk_root = sdk_root_dir("subst");
        let project = project_dir("subst");

        let plan_path = project.join("plan.json");
        let plan_json = r#"{
          "schemaVersion": 1, "planPathMode": "tokened",
          "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
          "slices": [
            { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
              "configArtefacts": [
                { "path": "build/c1/alp.conf", "contents": "SDK=${SDK_ROOT}\n" }
              ],
              "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" } }
          ],
          "sharedArtefacts": []
        }"#;
        std::fs::write(&plan_path, plan_json).unwrap();

        let cli = Cli::parse_from([
            "tan",
            "--format",
            "json",
            "--project",
            project.to_str().unwrap(),
            "--sdk-root",
            sdk_root.to_str().unwrap(),
            "build",
            "--plan-from",
            plan_path.to_str().unwrap(),
            "--materialise",
        ]);
        let Command::Build(args) = cli.command else {
            panic!("expected Command::Build");
        };

        let run = run_build(&cli.global, &args);
        assert_eq!(run.exit.code(), 0, "run: {:?}", run.json);

        let written = project.join("build/c1/alp.conf");
        let contents = std::fs::read_to_string(&written)
            .unwrap_or_else(|e| panic!("materialise did not write {}: {e}", written.display()));
        assert!(
            !contents.contains("${SDK_ROOT}"),
            "unresolved token leaked to disk: {contents}"
        );
        assert_eq!(contents, format!("SDK={}\n", sdk_root.to_str().unwrap()));

        std::fs::remove_dir_all(&project).ok();
        std::fs::remove_dir_all(&sdk_root).ok();
    }

    #[test]
    fn plan_from_materialise_refuses_an_unresolvable_tokened_plan() {
        // Same real routing, but with no `--sdk-root` and a project dir that
        // isn't itself (or near) an SDK checkout — `${SDK_ROOT}` can't
        // resolve. Must refuse and write NOTHING, never fall back to
        // materialising the literal token.
        let project = project_dir("unresolved");
        let plan_path = project.join("plan.json");
        std::fs::write(
            &plan_path,
            r#"{
              "schemaVersion": 1, "planPathMode": "tokened",
              "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
              "slices": [
                { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1",
                  "configArtefacts": [
                    { "path": "build/c1/alp.conf", "contents": "SDK=${SDK_ROOT}\n" }
                  ],
                  "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" } }
              ],
              "sharedArtefacts": []
            }"#,
        )
        .unwrap();

        let cli = Cli::parse_from([
            "tan",
            "--format",
            "json",
            "--project",
            project.to_str().unwrap(),
            "build",
            "--plan-from",
            plan_path.to_str().unwrap(),
            "--materialise",
        ]);
        let Command::Build(args) = cli.command else {
            panic!("expected Command::Build");
        };

        let run = run_build(&cli.global, &args);
        assert_ne!(run.exit.code(), 0, "run: {:?}", run.json);
        assert!(
            !project.join("build/c1/alp.conf").exists(),
            "must not materialise an unresolved tokened plan"
        );

        std::fs::remove_dir_all(&project).ok();
    }
}
