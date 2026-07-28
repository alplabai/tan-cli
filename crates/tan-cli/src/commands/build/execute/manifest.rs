// SPDX-License-Identifier: Apache-2.0
//! Post-build output seam for the `--native` executor: write the
//! `system-manifest.yaml` the downstream `flash`/`size`/`image` contract reads,
//! and resolve the real on-disk `zephyr.elf` a slice produced.

use std::path::Path;

use tan_core::ProjectContext;
use tan_core::build_plan::BuildPlan;
use tan_core::system_manifest::{
    SliceRunResult, overlay_run_results_raw, parse_system_manifest_raw,
    serialize_system_manifest_raw,
};

use super::super::workspace::invoke_sdk_emit;
use super::SliceResult;

/// Outcome of [`write_post_build_manifest`] — the two independent, IN-MEMORY
/// signals `tan run` decides host-vs-hardware and flash-safety from (see
/// `tan_core::run`'s module doc and `commands::run`). Neither field is ever
/// derived by re-reading the on-disk file afterward.
pub(super) struct PostBuildManifest {
    /// `None` when the on-disk file write succeeded; `Some(reason)` describes
    /// why it didn't. Never flips the build's own exit code (see the
    /// function doc) — only gates `tan run --flash` downstream.
    pub(super) write_failed_reason: Option<String>,
    /// Whether THIS run's freshly emitted SDK system-manifest projection
    /// declares a `native_sim` slice — independent of `write_failed_reason`:
    /// the emit and the subsequent file write are two separate steps, and a
    /// failure in the latter (a Windows sharing violation, a
    /// `create_dir_all` failure) must not erase a target this run's build
    /// already established. `None` only when the emit itself failed or its
    /// result didn't parse — no signal at all, not "not native_sim".
    pub(super) native_sim_target: Option<bool>,
}

/// Write `<build_root>/system-manifest.yaml` after a `--native` run: fetch the
/// plan-time projection from the SDK (`--emit system-manifest`), overlay this
/// run's per-slice status (identity mapping — both use `ok`/`failed`/`skipped`),
/// serialize, and write under the plan's build root. Best-effort in the sense
/// that a failure here never flips the build's own exit code (the slices
/// already ran; this is a post-build convenience write) — but it is NOT
/// silent: every failure branch reports `write_failed_reason` so the caller
/// can print it (text mode) AND fold it into the JSON envelope as a warning
/// `Issue`. Previously this only ever `eprintln!`'d when `warn_enabled` (i.e.
/// never in `--format json`, the mode the alp-sdk-vscode extension always
/// uses) — so a failed rewrite left the PREVIOUS run's manifest on disk,
/// indistinguishable from a fresh one, and `tan flash` would silently program
/// stale firmware after an `ok:true` build. Reverting this back to a bare
/// `eprintln!` + `return` reopens that hole.
///
/// `native_sim_target` is derived from the SDK emit BEFORE the file write is
/// even attempted, so it survives a write failure intact — this is the fix
/// that lets `tan run` decide from THIS build's own outcome instead of
/// re-reading (possibly stale) disk state afterward.
pub(super) fn write_post_build_manifest(
    context: &ProjectContext,
    plan: &BuildPlan,
    base: &str,
    results: &[SliceResult],
) -> PostBuildManifest {
    // Confine the write under the build tree (mirrors materialise_plan). The
    // old `is_absolute() || has ParentDir` guard misses Windows-rooted/
    // drive-relative shapes (`\out`, `C:out`) that `join` still escapes the
    // base for — use the shared shape guard instead of re-deriving it.
    let rel = Path::new(&plan.build_root);
    if !tan_core::is_plain_relative(rel) {
        return PostBuildManifest {
            write_failed_reason: Some(format!("unsafe build_root `{}`", plan.build_root)),
            native_sim_target: None,
        };
    }

    let yaml = match invoke_sdk_emit(
        context,
        "system-manifest",
        "build.manifest-unavailable",
        &[],
        &[],
    ) {
        Ok(y) => y,
        Err((_, msg)) => {
            return PostBuildManifest {
                write_failed_reason: Some(msg),
                native_sim_target: None,
            };
        }
    };
    // THIS run's own target signal — computed from the emit, independent of
    // whether the file write below then succeeds.
    let native_sim_target = native_sim_target_from_yaml(&yaml);

    let overlay: Vec<SliceRunResult> = results
        .iter()
        // Carry the real artefact/build_dir/reason the run resolved; None
        // preserves the plan-time value (e.g. for a skipped or non-zephyr
        // slice). `r.reason` is the executor's "no command" / "{tool} not
        // found in PATH; ..." text — threaded through so a skipped slice
        // explains itself in the written system-manifest.yaml instead of
        // silently dropping to a bare status.
        .map(|r| {
            (
                r.core_id.clone(),
                r.status.clone(),
                r.output_artefact.clone(),
                r.build_dir.clone(),
                r.reason.clone(),
            )
        })
        .collect();
    // Raw trio only (system_manifest.rs:240-266): the typed
    // parse_system_manifest/serialize_system_manifest round trip silently
    // deletes any field the typed SystemManifest struct doesn't model — an
    // rpmsg IpcLink's shm_addr/shm_size carve-out, hw_info.eeprom, anything a
    // newer SDK adds — from the file `flash`/`image`/`renode` read back next.
    let out = match rewrite_manifest_yaml(&yaml, &overlay) {
        Ok(s) => s,
        Err(e) => {
            return PostBuildManifest {
                write_failed_reason: Some(e),
                native_sim_target,
            };
        }
    };

    let dest = Path::new(base).join(rel).join("system-manifest.yaml");
    if let Some(parent) = dest.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            return PostBuildManifest {
                write_failed_reason: Some(e.to_string()),
                native_sim_target,
            };
        }
    }
    if let Err(e) = std::fs::write(&dest, out) {
        return PostBuildManifest {
            write_failed_reason: Some(e.to_string()),
            native_sim_target,
        };
    }
    PostBuildManifest {
        write_failed_reason: None,
        native_sim_target,
    }
}

/// Whether THIS run's freshly emitted system-manifest YAML declares a
/// `native_sim` slice — pure, reusing `tan_core::run::native_sim_slice` on a
/// parse of the emit result. `None` only when `yaml` doesn't parse (no signal
/// at all); a manifest with no `native_sim` slice is `Some(false)`, not `None`.
fn native_sim_target_from_yaml(yaml: &str) -> Option<bool> {
    tan_core::system_manifest::parse_system_manifest(yaml)
        .ok()
        .map(|m| tan_core::run::native_sim_slice(&m).is_some())
}

/// The pure parse-overlay-serialize step of the post-build rewrite, split out
/// of `write_post_build_manifest` so it's testable without a live SDK checkout
/// (the surrounding function's `invoke_sdk_emit` shells out to python). MUST
/// stay on the raw trio (`parse_system_manifest_raw` /
/// `overlay_run_results_raw` / `serialize_system_manifest_raw`) — the typed
/// pair is documented in system_manifest.rs as forbidden on this exact write
/// path because it silently deletes any field the typed `SystemManifest`
/// doesn't model.
fn rewrite_manifest_yaml(yaml: &str, overlay: &[SliceRunResult]) -> Result<String, String> {
    let mut manifest = parse_system_manifest_raw(yaml).map_err(|e| e.to_string())?;
    overlay_run_results_raw(&mut manifest, overlay);
    serialize_system_manifest_raw(&manifest)
}

/// After a slice builds `ok`, resolve the real `zephyr.elf` west produced and
/// its build dir. West's default output is a nested `build/` under the slice's
/// run cwd, so the elf lands at `<cwd>/build/zephyr/zephyr.elf`. Returned
/// ABSOLUTE so every consumer (`size`/`renode`/`flash`/`image`) uses the paths
/// verbatim without re-anchoring under its own build_root. `(None, None)` when
/// the slice's own command redirects the build dir (`-d`/`--build-dir`) — west
/// then wrote somewhere other than the default nested path, so the default
/// location can't be trusted and the manifest keeps its plan-time values.
///
/// Deliberately NOT mtime-gated. An earlier version required the elf's mtime
/// `>=` the moment the command was spawned, meaning to reject a stale elf left
/// by an earlier run — but `west build` is ninja-driven: a repeat `tan build`
/// with nothing to do prints "no work to do" and leaves `zephyr.elf`'s mtime
/// at run N-1, same for an unchanged slice in a multicore build, same on a
/// coarse-mtime filesystem. That gate then dropped the resolved artefact on
/// every such no-op/incremental rebuild, and per commit 7c0137d the consumers
/// (size/image/renode/flash — all reading `<core>-<os>/zephyr/zephyr.elf`, no
/// nested `build/`) reported the elf missing after a build that actually
/// succeeded: exactly the defect that commit fixed. `is_file()` at the
/// default path is sufficient on its own: only west writes there, for THIS
/// slice's cwd, whether or not this particular invocation relinked.
///
/// ponytail: only `-d`/`--build-dir` (west's own build-dir flags) are
/// detected; a plan command redirecting the build dir some other way isn't
/// matched — falls back to the safe `(None, None)`, same as a real override.
pub(super) fn resolve_zephyr_artefact(
    slice_cwd: &Path,
    cmd_args: &[String],
) -> (Option<String>, Option<String>) {
    if build_dir_overridden(cmd_args) {
        return (None, None);
    }
    let west_build = slice_cwd.join("build");
    let elf = west_build.join("zephyr").join("zephyr.elf");
    if elf.is_file() {
        (abs_string(&elf), abs_string(&west_build))
    } else {
        (None, None)
    }
}

/// True when the slice's argv redirects west's build dir away from the
/// default nested `<cwd>/build` (`-d <dir>`, `--build-dir <dir>`, or
/// `--build-dir=<dir>`) — the one case `resolve_zephyr_artefact`'s default
/// path can't follow. `pub(super)` — also consulted by the executor's
/// sdk-switch-pristine guard (see `sdk_stamp_path`), which refuses to touch a
/// build dir west didn't put at the default nested path for the same reason.
pub(super) fn build_dir_overridden(cmd_args: &[String]) -> bool {
    cmd_args
        .iter()
        .any(|a| a == "-d" || a == "--build-dir" || a.starts_with("--build-dir="))
}

/// Path of the tan-owned SDK-identity stamp (issue #52): lives INSIDE west's
/// own nested build dir (`<cwd>/build`, not `<cwd>` itself), so wiping that
/// dir on a mismatch retires the stamp atomically — it can never outlive or
/// misdescribe a build dir that no longer exists.
pub(super) fn sdk_stamp_path(slice_cwd: &Path) -> std::path::PathBuf {
    slice_cwd.join("build").join(".tan-sdk-root")
}

/// The SDK identity key recorded in the stamp file, if any (`None` covers
/// both "never stamped" and "unreadable" — both read as "no signal", which
/// `sdk_stamp_action` treats as a mismatch on an already-configured dir).
/// The value is whatever the executor wrote via [`write_sdk_stamp`] — the
/// bare `--sdk-root` path on an older stamp, or `tan_core::plan_exec::
/// sdk_stamp_key`'s `<root>@<sdkCommit>` form once the plan carries a commit
/// — this function does not itself parse or care which.
pub(super) fn read_sdk_stamp(slice_cwd: &Path) -> Option<String> {
    std::fs::read_to_string(sdk_stamp_path(slice_cwd))
        .ok()
        .map(|s| s.trim().to_string())
}

/// Write the stamp BEFORE the tool is spawned (see the executor call site):
/// a mid-configure failure still stamped correctly, since the dir really was
/// configured against `key` regardless of whether the build finishes.
/// `key` is `tan_core::plan_exec::sdk_stamp_key`'s output — the resolved SDK
/// root, plus `@<sdkCommit>` when the plan carries one — not necessarily a
/// bare path. Best-effort — a write failure here is not fatal to the build;
/// it just means the next run may treat this dir as unstamped (fails toward
/// a spurious rebuild, not toward trusting a stale one).
pub(super) fn write_sdk_stamp(slice_cwd: &Path, key: &str) -> std::io::Result<()> {
    let path = sdk_stamp_path(slice_cwd);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, key)
}

/// Whether west has ever configured this slice's build dir (`<cwd>/build/
/// CMakeCache.txt`) — the "was this dir ever configured" signal
/// `sdk_stamp_action` needs before it treats a missing stamp as stale. Present
/// at the top of the nested dir in both plain and `--sysbuild` builds (the
/// sysbuild superbuild project has its own top-level cache; only per-image
/// caches nest further), so this holds for every slice `tan` emits today.
pub(super) fn cmake_cache_configured(slice_cwd: &Path) -> bool {
    slice_cwd.join("build").join("CMakeCache.txt").is_file()
}

/// Whether this slice's build dir shows that Zephyr's CMake boilerplate was
/// actually loaded — the signal behind the `os: zephyr` guard (tan-cli #97).
///
/// The reported defect: a project whose `CMakeLists.txt` never calls
/// `find_package(Zephyr ...)` still configures and links fine under `west
/// build -b <board>` (CMake only emits a *dev* warning about the missing
/// `project()` call), so a core declared `os: zephyr` produced a host x86-64
/// executable and the executor reported it `[+] ok`. The board name is never
/// even validated, because nothing loaded the code that would validate it.
///
/// Two signals, either of which proves the boilerplate ran; both are
/// generated build artefacts, NOT log text (a configure-log grep for
/// `ZephyrConfig.cmake` breaks the moment CMake rewords a line, and
/// log-scraping gates are a failure class this repo has been burned by):
///
/// 1. A `ZEPHYR_BASE:` entry in the build dir's own `CMakeCache.txt`. This is
///    the primary signal and the closest thing to the fact being asserted —
///    `find_package(Zephyr)` is what caches it, a plain host configure never
///    does (verified against a real `<slice>/build/CMakeCache.txt`, which
///    carries `ZEPHYR_BASE:PATH=…` on line 42, versus a baremetal one, whose
///    complete 339-line cache has no `ZEPHYR_BASE` line at all).
/// 2. A `zephyr/` subdirectory under the build dir — Zephyr's boilerplate
///    binary dir. Kept as an OR fallback rather than the primary signal so
///    the guard can only ever fail a build it is SURE about: a real Zephyr
///    tree that somehow shipped no readable `CMakeCache.txt` still passes.
///    Do NOT promote this to the primary signal: the verified real Zephyr
///    slice dir above carried `ZEPHYR_BASE:` and had NO `zephyr/` directory,
///    so signal 1 is the one doing the work.
///
/// Both signals are also checked one level down, because `--sysbuild` is a
/// live path here (`-DSB_CONF_FILE=…/zephyr/sysbuild/v2n/sysbuild.conf` for
/// V2N) and its superbuild nests the real per-image Zephyr builds one
/// directory deeper. An earlier revision of this function asserted that a
/// sysbuild top-level cache carries `ZEPHYR_BASE:` too; that is NOT
/// established. Zephyr's `share/sysbuild/CMakeLists.txt` declares
/// `project(sysbuild_toplevel LANGUAGES)` and calls
/// `find_package(Sysbuild …)`, not `find_package(Zephyr)` — and the only
/// `set(ZEPHYR_BASE … CACHE)` in the tree is in `cmake/modules/unittest.cmake`.
/// Without the nested look, a legitimate V2N sysbuild slice would have no
/// top-level `ZEPHYR_BASE:` and no top-level `zephyr/`, and this guard would
/// fail a correct build. One level is enough (sysbuild nests per-image, not
/// recursively) and keeps the cost bounded.
///
/// Callers must skip this check when `build_dir_overridden` — west then wrote
/// somewhere this can't see, same refusal `resolve_zephyr_artefact` already
/// makes.
pub(super) fn zephyr_boilerplate_loaded(slice_cwd: &Path) -> bool {
    let build = slice_cwd.join("build");
    if dir_shows_zephyr(&build) {
        return true;
    }
    // sysbuild superbuild: the real Zephyr builds are one level down.
    std::fs::read_dir(&build).is_ok_and(|entries| {
        entries
            .flatten()
            .any(|e| e.file_type().is_ok_and(|t| t.is_dir()) && dir_shows_zephyr(&e.path()))
    })
}

/// The two per-directory signals behind [`zephyr_boilerplate_loaded`].
fn dir_shows_zephyr(dir: &Path) -> bool {
    std::fs::read_to_string(dir.join("CMakeCache.txt"))
        .is_ok_and(|c| c.lines().any(|l| l.starts_with("ZEPHYR_BASE:")))
        || dir.join("zephyr").is_dir()
}

/// Absolute, lossy-string form of a path (no filesystem round-trip beyond
/// `std::path::absolute`; falls back to the path as-is if that fails).
fn abs_string(p: &Path) -> Option<String> {
    Some(
        std::path::absolute(p)
            .unwrap_or_else(|_| p.to_path_buf())
            .to_string_lossy()
            .into_owned(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;
    use tan_core::build_plan::parse_build_plan;

    fn no_sdk_context() -> ProjectContext {
        ProjectContext {
            workspace_root: None,
            sdk_root: None,
            board_yaml_path: None,
            west_cwd: None,
            python_binary: "python3".to_string(),
        }
    }

    fn sample_plan(build_root: &str) -> BuildPlan {
        parse_build_plan(&format!(
            r#"{{
              "schemaVersion": 1, "boardYaml": "b", "sku": "S", "buildRoot": "{build_root}",
              "slices": [],
              "sharedArtefacts": []
            }}"#
        ))
        .unwrap()
    }

    fn unique_temp_dir(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("{tag}-{}", std::process::id()))
    }

    #[test]
    fn write_post_build_manifest_reports_unsafe_build_root_instead_of_silent_noop() {
        // Regression: this used to be a bare `eprintln!` + `return` gated on
        // `warn_enabled` (i.e. never surfaced in `--format json`, the mode the
        // extension always uses). The caller needs `Some(reason)` to fold into
        // the envelope as a warning Issue — otherwise a stale manifest from a
        // previous run looks identical to a fresh one.
        let plan = sample_plan("../escape");
        let outcome = write_post_build_manifest(&no_sdk_context(), &plan, "/base", &[]);
        assert!(
            outcome.write_failed_reason.is_some(),
            "expected a reported reason, got None"
        );
        assert!(
            outcome
                .write_failed_reason
                .unwrap()
                .contains("unsafe build_root")
        );
        // The emit never even ran — no target signal at all, not "not native_sim".
        assert_eq!(outcome.native_sim_target, None);
    }

    #[test]
    fn write_post_build_manifest_reports_reason_when_sdk_unresolved() {
        // Every failure branch (not just the path guard) must return Some,
        // and — since the emit itself is what failed here — no target signal.
        let plan = sample_plan("build");
        let outcome = write_post_build_manifest(&no_sdk_context(), &plan, "/base", &[]);
        assert!(outcome.write_failed_reason.is_some());
        assert_eq!(outcome.native_sim_target, None);
    }

    #[test]
    fn native_sim_target_from_yaml_true_for_a_native_sim_manifest() {
        let yaml = "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n  \
                     os: zephyr\n  board: native_sim\n  status: ok\nipc: []\nhelper_mcus: \
                     []\nboot_order: []\n";
        assert_eq!(native_sim_target_from_yaml(yaml), Some(true));
    }

    #[test]
    fn native_sim_target_from_yaml_false_for_a_hardware_manifest() {
        let yaml = "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: m55_hp\n  os: \
                     zephyr\n  board: alp_e1m_aen701_m55_hp\n  status: ok\nipc: \
                     []\nhelper_mcus: []\nboot_order: []\n";
        assert_eq!(native_sim_target_from_yaml(yaml), Some(false));
    }

    #[test]
    fn native_sim_target_from_yaml_none_for_unparsable_yaml() {
        // No signal at all — must not be conflated with `Some(false)`
        // (hardware), which is exactly the shape that would let an unknown
        // target slip through to `RunAction::Flash` in `decide_run_action`.
        assert_eq!(native_sim_target_from_yaml("not: [valid"), None);
    }

    #[test]
    fn rewrite_manifest_yaml_preserves_additive_fields_the_typed_serializer_drops() {
        // Regression: system_manifest.rs forbids parse_system_manifest +
        // serialize_system_manifest on this exact write path because the typed
        // `SystemManifest` struct has no field for an rpmsg IpcLink's shm_addr/
        // shm_size carve-out or hw_info.eeprom — re-serializing it silently
        // deletes them from system-manifest.yaml on every successful build.
        // This exercises the actual sequence `write_post_build_manifest` runs
        // (rewrite_manifest_yaml) and fails if it's ever switched back to the
        // typed pair (parse_system_manifest/serialize_system_manifest).
        let yaml = r#"
schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-AEN701
  eeprom:
    magic: 0xA5
slices:
- core_id: m55_hp
  os: zephyr
  status: pending
ipc:
- name: rpmsg0
  kind: rpmsg
  endpoints: [a55, m33]
  shm_addr: 0x48000000
  shm_size: 0x100000
helper_mcus: []
boot_order: []
"#;
        let overlay = vec![(
            "m55_hp".to_string(),
            "ok".to_string(),
            Some("build/m55_hp-zephyr/build/zephyr/zephyr.elf".to_string()),
            None,
            None,
        )];

        let out = rewrite_manifest_yaml(yaml, &overlay).expect("rewrite must succeed");

        assert!(out.contains("shm_addr"), "dropped ipc shm_addr: {out}");
        assert!(out.contains("shm_size"), "dropped ipc shm_size: {out}");
        assert!(out.contains("eeprom"), "dropped hw_info.eeprom: {out}");
        // The overlay itself still lands.
        assert!(out.contains("status: ok"), "overlay did not land: {out}");
        assert!(
            out.contains("build/m55_hp-zephyr/build/zephyr/zephyr.elf"),
            "overlay artefact did not land: {out}"
        );
    }

    #[test]
    fn rewrite_manifest_yaml_threads_the_skip_reason_into_the_manifest() {
        // L22 regression: `write_post_build_manifest`'s overlay tuple used to
        // drop `r.reason` entirely, so a skipped slice's "no command" / "west
        // not found in PATH; ..." explanation never reached
        // system-manifest.yaml even though `Slice.reason` and the schema
        // field both exist for it.
        let yaml = r#"
schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_he
  os: zephyr
  status: pending
ipc: []
helper_mcus: []
boot_order: []
"#;
        let overlay = vec![(
            "m55_he".to_string(),
            "skipped".to_string(),
            None,
            None,
            Some("west not found in PATH; this is normal on non-Zephyr hosts".to_string()),
        )];

        let out = rewrite_manifest_yaml(yaml, &overlay).expect("rewrite must succeed");

        assert!(
            out.contains("status: skipped"),
            "overlay did not land: {out}"
        );
        assert!(
            out.contains("west not found in PATH; this is normal on non-Zephyr hosts"),
            "reason did not land: {out}"
        );
    }

    #[test]
    fn rewrite_manifest_yaml_clears_a_stale_output_artefact_when_the_executor_says_so() {
        // MINOR 4 of the #89 review: `overlay_run_results_raw` treats a
        // `None` `output_artefact` as "preserve the plan-time/previous-run
        // value" — correct for a slice this run never touched, but WRONG for
        // one the executor knows never dispatched this run (a demoted slice,
        // tan-cli #89). Left as `None`, a Zephyr-only tokened plan on a
        // toolchain-less host would write `status: skipped` next to the
        // PREVIOUS run's `zephyr.elf` path — `tan size`/`tan debug-config`
        // read `output_artefact` with no status check of their own. The
        // executor's fix is to overlay `Some(String::new())` instead of
        // `None`; this pins that an explicit empty string actually clears
        // the stale value rather than being (mis)treated as "preserve".
        let yaml = r#"
schema_version: 1
generated_by: scripts/alp_orchestrate.py
hw_info:
  sku: E1M-V2N101
slices:
- core_id: m33_sm
  os: zephyr
  output_artefact: build/m33_sm-zephyr/build/zephyr/zephyr.elf
  status: ok
ipc: []
helper_mcus: []
boot_order: []
"#;
        let overlay = vec![(
            "m33_sm".to_string(),
            "skipped".to_string(),
            Some(String::new()),
            None,
            Some(
                "plan field `slices[0].env.ZEPHYR_SDK_INSTALL_DIR` names ${TOOLCHAIN_ROOT}, \
                  but no toolchain install is detectable on this host"
                    .to_string(),
            ),
        )];

        let out = rewrite_manifest_yaml(yaml, &overlay).expect("rewrite must succeed");

        assert!(
            out.contains("status: skipped"),
            "overlay did not land: {out}"
        );
        assert!(
            !out.contains("build/m33_sm-zephyr/build/zephyr/zephyr.elf"),
            "the previous run's elf path must not survive a demoted slice's overlay: {out}"
        );
    }

    #[test]
    fn resolve_zephyr_artefact_accepts_an_elf_left_by_a_no_op_incremental_rebuild() {
        // Regression: an earlier version gated on `mtime >= not_before`
        // (captured right before the command was spawned) to reject a stale
        // elf from an earlier run. But `west build` is ninja-driven: a repeat
        // `tan build` with nothing to rebuild leaves `zephyr.elf`'s mtime at
        // run N-1 — the mtime gate then dropped a perfectly valid artefact on
        // every no-op/incremental rebuild. Simulate that by backdating the
        // elf's mtime well before "now" and asserting it's still resolved.
        let cwd = unique_temp_dir("alp-resolve-no-op-rebuild");
        let _ = std::fs::remove_dir_all(&cwd);
        let elf_dir = cwd.join("build").join("zephyr");
        std::fs::create_dir_all(&elf_dir).unwrap();
        let elf_path = elf_dir.join("zephyr.elf");
        std::fs::write(&elf_path, b"unchanged-since-last-run").unwrap();
        let backdated = SystemTime::now() - std::time::Duration::from_secs(3600);
        // `set_modified` needs a writable handle on Windows — a read-only
        // `File::open` fails with `PermissionDenied`.
        std::fs::OpenOptions::new()
            .write(true)
            .open(&elf_path)
            .unwrap()
            .set_modified(backdated)
            .unwrap();

        let (artefact, build_dir) = resolve_zephyr_artefact(&cwd, &[]);
        assert!(
            artefact.is_some(),
            "an unchanged (old-mtime) elf from a no-op rebuild must still be reported"
        );
        assert!(build_dir.is_some());

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn resolve_zephyr_artefact_ignores_the_default_path_when_build_dir_is_overridden() {
        // A slice command that redirects the build dir (`-d ../out`) means
        // west did NOT write to `<cwd>/build/...` — even if an elf happens to
        // sit there (e.g. left by an earlier, differently-configured run), it
        // must not be reported as this run's artefact.
        let cwd = unique_temp_dir("alp-resolve-overridden");
        let _ = std::fs::remove_dir_all(&cwd);
        let elf_dir = cwd.join("build").join("zephyr");
        std::fs::create_dir_all(&elf_dir).unwrap();
        std::fs::write(elf_dir.join("zephyr.elf"), b"unrelated").unwrap();

        let args = vec!["-d".to_string(), "../out".to_string()];
        let (artefact, build_dir) = resolve_zephyr_artefact(&cwd, &args);
        assert_eq!(artefact, None, "overridden build dir must not be trusted");
        assert_eq!(build_dir, None);

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn resolve_zephyr_artefact_accepts_the_default_path_when_no_override_present() {
        let cwd = unique_temp_dir("alp-resolve-default");
        let _ = std::fs::remove_dir_all(&cwd);
        let elf_dir = cwd.join("build").join("zephyr");
        std::fs::create_dir_all(&elf_dir).unwrap();
        std::fs::write(elf_dir.join("zephyr.elf"), b"fresh").unwrap();

        let (artefact, build_dir) = resolve_zephyr_artefact(&cwd, &[]);
        assert!(artefact.is_some());
        assert!(build_dir.is_some());

        std::fs::remove_dir_all(&cwd).ok();
    }

    /// The producer half of a cross-module assumption. `resolve_zephyr_artefact`
    /// is tan's ONLY writer of a slice's `output_artefact` (alp-sdk is
    /// planner/emit-only and never writes it), and it names `zephyr.elf` for
    /// EVERY zephyr slice — native_sim included, where the runnable is the
    /// sibling `zephyr.exe`. `tan run` and `tan debug-config` both depend on
    /// that: each resolves the host binary by swapping the filename
    /// (`tan_core::run::native_sim_exe_beside`). #83 shipped a `program`
    /// pointing at the ELF precisely because its test fixture asserted a
    /// `.exe` manifest this function cannot produce, and nothing on this side
    /// contradicted it. So pin it HERE, where the string is actually decided:
    /// add a native_sim `.exe` branch above and this fails, instead of two
    /// consumer fixtures quietly going stale.
    #[test]
    fn resolve_zephyr_artefact_names_the_elf_even_for_a_native_sim_slice() {
        let cwd = unique_temp_dir("alp-resolve-native-sim");
        let _ = std::fs::remove_dir_all(&cwd);
        let out_dir = cwd.join("build").join("zephyr");
        std::fs::create_dir_all(&out_dir).unwrap();
        // What a real native_sim build leaves behind: BOTH files, side by side.
        std::fs::write(out_dir.join("zephyr.elf"), b"elf").unwrap();
        std::fs::write(out_dir.join("zephyr.exe"), b"exe").unwrap();

        let (artefact, _) = resolve_zephyr_artefact(&cwd, &[]);
        let artefact = artefact.expect("a built slice resolves an artefact");
        assert!(
            artefact.ends_with("zephyr.elf"),
            "the manifest must record the ELF even when a runnable .exe sits \
             beside it — consumers resolve the host binary from it: {artefact}"
        );

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn sdk_stamp_round_trips_through_write_and_read() {
        let cwd = unique_temp_dir("alp-sdk-stamp-roundtrip");
        let _ = std::fs::remove_dir_all(&cwd);

        assert_eq!(read_sdk_stamp(&cwd), None, "nothing written yet");
        write_sdk_stamp(&cwd, "/sdk/v0.13.0").unwrap();
        assert_eq!(read_sdk_stamp(&cwd), Some("/sdk/v0.13.0".to_string()));

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn sdk_stamp_lives_inside_the_nested_build_dir_so_wiping_it_retires_the_stamp() {
        // The load-bearing property: the stamp can never outlive or
        // misdescribe a build dir that no longer exists, because it sits
        // INSIDE the exact directory the mismatch wipe removes.
        let cwd = unique_temp_dir("alp-sdk-stamp-retires-with-wipe");
        let _ = std::fs::remove_dir_all(&cwd);
        write_sdk_stamp(&cwd, "/sdk/v0.11.0").unwrap();
        assert!(sdk_stamp_path(&cwd).starts_with(cwd.join("build")));

        std::fs::remove_dir_all(cwd.join("build")).unwrap();
        assert_eq!(
            read_sdk_stamp(&cwd),
            None,
            "stamp must not survive the wipe"
        );

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn cmake_cache_configured_reflects_whether_west_ever_configured_the_dir() {
        let cwd = unique_temp_dir("alp-cmake-cache-configured");
        let _ = std::fs::remove_dir_all(&cwd);
        std::fs::create_dir_all(cwd.join("build")).unwrap();

        assert!(!cmake_cache_configured(&cwd), "no CMakeCache.txt yet");
        std::fs::write(cwd.join("build").join("CMakeCache.txt"), "").unwrap();
        assert!(cmake_cache_configured(&cwd));

        std::fs::remove_dir_all(&cwd).ok();
    }
}
