// SPDX-License-Identifier: Apache-2.0
//! Post-build output seam for the `--native` executor: write the
//! `system-manifest.yaml` the downstream `flash`/`size`/`image` contract reads,
//! and resolve the real on-disk `zephyr.elf` a slice produced.

use std::path::{Component, Path};

use tan_core::ProjectContext;
use tan_core::build_plan::BuildPlan;
use tan_core::system_manifest::{
    overlay_run_results, parse_system_manifest, serialize_system_manifest,
};

use super::super::workspace::invoke_sdk_emit;
use super::SliceResult;

/// Write `<build_root>/system-manifest.yaml` after a `--native` run: fetch the
/// plan-time projection from the SDK (`--emit system-manifest`), overlay this
/// run's per-slice status (identity mapping — both use `ok`/`failed`/`skipped`),
/// serialize, and write under the plan's build root. Best-effort: the manifest
/// is a post-build convenience, so any failure (no SDK, emit error, unsafe
/// build_root, unwritable dir) warns (text mode only) and returns — it never
/// changes the build's exit result.
pub(super) fn write_post_build_manifest(
    context: &ProjectContext,
    plan: &BuildPlan,
    base: &str,
    results: &[SliceResult],
    warn_enabled: bool,
) {
    let warn = |msg: String| {
        if warn_enabled {
            eprintln!("note: skipped writing system-manifest.yaml — {msg}");
        }
    };

    // Confine the write under the build tree (mirrors materialise_plan): a plan
    // build_root that's absolute or escapes via `..` is refused.
    let rel = Path::new(&plan.build_root);
    if rel.is_absolute() || rel.components().any(|c| matches!(c, Component::ParentDir)) {
        warn(format!("unsafe build_root `{}`", plan.build_root));
        return;
    }

    let yaml = match invoke_sdk_emit(context, "system-manifest", "build.manifest-unavailable") {
        Ok(y) => y,
        Err((_, msg)) => return warn(msg),
    };
    let mut manifest = match parse_system_manifest(&yaml) {
        Ok(m) => m,
        Err(e) => return warn(e.to_string()),
    };

    let overlay: Vec<(String, String, Option<String>, Option<String>)> = results
        .iter()
        // Carry the real artefact/build_dir the run resolved; None preserves the
        // plan-time value (e.g. for a skipped or non-zephyr slice).
        .map(|r| {
            (
                r.core_id.clone(),
                r.status.clone(),
                r.output_artefact.clone(),
                r.build_dir.clone(),
            )
        })
        .collect();
    overlay_run_results(&mut manifest, &overlay);

    let out = match serialize_system_manifest(&manifest) {
        Ok(s) => s,
        Err(e) => return warn(e),
    };

    let dest = Path::new(base).join(rel).join("system-manifest.yaml");
    if let Some(parent) = dest.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            return warn(e.to_string());
        }
    }
    if let Err(e) = std::fs::write(&dest, out) {
        warn(e.to_string());
    }
}

/// After a slice builds `ok`, resolve the real `zephyr.elf` west produced and
/// its build dir. West's default output is a nested `build/` under the slice's
/// run cwd, so the elf lands at `<cwd>/build/zephyr/zephyr.elf`. Returned
/// ABSOLUTE so every consumer (`size`/`renode`/`flash`/`image`) uses the paths
/// verbatim without re-anchoring under its own build_root. `(None, None)` when
/// no elf is there — a non-Zephyr backend, or a build that wrote elsewhere — so
/// the manifest keeps its plan-time values.
///
/// ponytail: assumes west's default nested `build/` dir; a slice that passes
/// `-d <other>` isn't matched here — the `is_file` gate then falls back to the
/// plan-time paths (no regression), upgrade to parse `-d` from the argv if a
/// custom build dir ever ships in a plan command.
pub(super) fn resolve_zephyr_artefact(slice_cwd: &Path) -> (Option<String>, Option<String>) {
    let west_build = slice_cwd.join("build");
    let elf = west_build.join("zephyr").join("zephyr.elf");
    if elf.is_file() {
        (abs_string(&elf), abs_string(&west_build))
    } else {
        (None, None)
    }
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
