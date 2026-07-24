// SPDX-License-Identifier: Apache-2.0
//! Vendored `alp-sdk --emit scaffold` output for the wizard templates that map
//! cleanly onto an SDK scaffold-catalog id (alp-sdk#864).
//!
//! `tan init` is deliberately SDK-free — it never shells the SDK — so the
//! bytes below are captured once at vendor time (see `vendored/MANIFEST.md`
//! for the exact source commit + template x SKU matrix) and baked into the
//! binary via `include_str!`. This is the single source of the scaffold's
//! build-integration conventions (CMake wiring, `EXTRA_CONF_FILE` layering,
//! the `--core` flag) for a mapped template — no Rust generator re-derives
//! them, so they cannot drift from the SDK the way the retired
//! `gen_zephyr_project_files` did (the cross-core Kconfig leak that is #864's
//! reason to exist: its CMakeLists.txt never passed `--core` to
//! `--emit zephyr-conf`). `tests/parity/scaffold_byte_parity.py` re-runs the
//! SDK emit and asserts these bytes haven't drifted from an unvendored
//! change.
//!
//! `WizardTemplateId::ZephyrApp` (-> SDK `minimal`),
//! `WizardTemplateId::SensorStarter` (-> SDK `sensor`), and
//! `WizardTemplateId::EdgeAiStarter` (-> SDK `edge-ai`) are mapped today; see
//! the manifest for which tan templates were left on their existing
//! hand-written generator and why. `edge-ai` is the first HETEROGENEOUS
//! (multi-core) vendored template: its `board.yaml` carries a companion core
//! (`os: "off"`) alongside the app core, see `vendored_app_core_key` below.

use super::example_catalog::retarget_board_yaml_som;
use crate::wizard::models::WizardTemplateId;

/// One vendored file: its path relative to the project root, and its exact
/// (LF, byte-for-byte) content as emitted by `alp_project.py --emit scaffold`.
type VendoredFile = (&'static str, &'static str);

/// A mapped template's two SoM-family vendored trees: `(alif_ensemble, renesas_v2n)`.
type FamilyTrees = (&'static [VendoredFile], &'static [VendoredFile]);

/// Build a vendored file table for `$template`/`$sku` from an explicit list
/// of paths relative to that tree, each read via `include_str!` (baked into
/// the binary at compile time). File-list-aware rather than a fixed six-file
/// shape — `edge-ai` vendors eight files (the extra `src/cold_chain.c` +
/// `src/cold_chain.h`), `minimal`/`sensor` vendor six.
macro_rules! vendored_tree {
    ($template:literal, $sku:literal, [$($file:literal),+ $(,)?]) => {
        &[
            $((
                $file,
                include_str!(concat!("../vendored/", $template, "/", $sku, "/", $file)),
            )),+
        ]
    };
}

/// Vendored `minimal` scaffold for the Alif Ensemble family (captured for
/// `E1M-AEN801`, the SDK catalog's declared representative AEN SKU).
const MINIMAL_AEN: &[VendoredFile] = vendored_tree!(
    "minimal",
    "E1M-AEN801",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/main.c",
        "testcase.yaml",
    ]
);

/// Vendored `minimal` scaffold for the Renesas RZ/V2N family (captured for
/// `E1M-V2N101`, the SDK catalog's declared representative V2N SKU).
const MINIMAL_V2N: &[VendoredFile] = vendored_tree!(
    "minimal",
    "E1M-V2N101",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/main.c",
        "testcase.yaml",
    ]
);

/// Vendored `sensor` scaffold (SDK's real TMP112 `<alp/chips/tmp112.h>`
/// i2c-master app) for the Alif Ensemble family.
const SENSOR_AEN: &[VendoredFile] = vendored_tree!(
    "sensor",
    "E1M-AEN801",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/main.c",
        "testcase.yaml",
    ]
);

/// Vendored `sensor` scaffold for the Renesas RZ/V2N family.
const SENSOR_V2N: &[VendoredFile] = vendored_tree!(
    "sensor",
    "E1M-V2N101",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/main.c",
        "testcase.yaml",
    ]
);

/// Vendored `edge-ai` scaffold (SDK's real BME280 cold-chain-monitor app --
/// a companion Cortex-A cluster (`a32_cluster`, `os: "off"`) alongside the
/// Cortex-M app core running TFLite-Micro inference) for the Alif Ensemble
/// family. Eight files -- `src/cold_chain.c`/`src/cold_chain.h` on top of the
/// six every other vendored template ships.
const EDGE_AI_AEN: &[VendoredFile] = vendored_tree!(
    "edge-ai",
    "E1M-AEN801",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/cold_chain.c",
        "src/cold_chain.h",
        "src/main.c",
        "testcase.yaml",
    ]
);

/// Vendored `edge-ai` scaffold for the Renesas RZ/V2N family: companion
/// `a55_cluster` (`os: "off"`), app core `m33_sm` -- targets the DEEPX DX-M1
/// path when `som.sku` is flipped, per its own `board.yaml` comment.
const EDGE_AI_V2N: &[VendoredFile] = vendored_tree!(
    "edge-ai",
    "E1M-V2N101",
    [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/cold_chain.c",
        "src/cold_chain.h",
        "src/main.c",
        "testcase.yaml",
    ]
);

const MINIMAL: FamilyTrees = (MINIMAL_AEN, MINIMAL_V2N);
const SENSOR: FamilyTrees = (SENSOR_AEN, SENSOR_V2N);
const EDGE_AI: FamilyTrees = (EDGE_AI_AEN, EDGE_AI_V2N);

/// Pick the vendored family bucket for `sku` out of `trees`. Mirrors
/// `app_core_for_sku`'s own family split: V2N/V2M -> the Renesas tree,
/// everything else (including E1M-NX9* and any SKU the SDK catalog doesn't
/// cover) defaults to the Alif Ensemble tree — the SDK catalog has no
/// NXP-family scaffold at all today (flagged in `vendored/MANIFEST.md`);
/// `tan validate` re-checks the real SoM once an SDK resolves, same caveat
/// `app_core_for_sku` already documents.
fn family_bucket(trees: FamilyTrees, sku: &str) -> &'static [VendoredFile] {
    if sku.starts_with("E1M-V2N") || sku.starts_with("E1M-V2M") {
        trees.1
    } else {
        trees.0
    }
}

/// Read the vendored `cores:` block's APP-core key out of a vendored
/// board.yaml (e.g. `"m33_sm"`) — the core whose block owns an `app:` key,
/// NOT simply the first core listed under `cores:`. A single-core scaffold
/// (`minimal`/`sensor`) has only one candidate, so this stays correct for
/// them; a HETEROGENEOUS scaffold (`edge-ai`) lists its companion core FIRST
/// (`a55_cluster:`/`a32_cluster:`, `os: "off"`, no `app:`) and the real app
/// core SECOND (`m33_sm`/`m55_hp`, the one with `app: ./src`) — trusting
/// position would return the companion. This scans every `cores:` child,
/// tracking the current `  <core>:` key, and returns whichever one owns a
/// `    app:` line. Reads it from the vendored CONTENT rather than trusting
/// `app_core_for_sku` — a vendored scaffold's `cores:` topology does not
/// change with `--sku` (the SDK only substitutes `som.sku:`/`preset:`, see
/// `vendored/MANIFEST.md`), so for a V2N SKU it stays whatever the tree's own
/// vendored SKU declares, not a value `app_core_for_sku` would guess.
fn vendored_app_core_key(board_yaml: &str) -> Option<&str> {
    let mut in_cores = false;
    let mut current_core: Option<&str> = None;
    for line in board_yaml.lines() {
        if !in_cores {
            in_cores = line == "cores:";
            continue;
        }
        if !line.is_empty() && !line.starts_with(' ') {
            break; // The next top-level key ends the cores: block.
        }
        if let Some(core) = line
            .strip_prefix("  ")
            .filter(|rest| !rest.starts_with(' '))
            .and_then(|rest| rest.strip_suffix(':'))
        {
            current_core = Some(core); // A `  <core>:` entry key line.
        } else if line.starts_with("    app:") {
            if let Some(core) = current_core {
                return Some(core);
            }
        }
    }
    None
}

/// Every `(core id, os)` pair declared in a vendored board.yaml's `cores:`
/// block, in file order — the app core AND any pre-declared companions (e.g.
/// edge-ai's `a55_cluster`/`a32_cluster`). `os` is that core's own
/// `    os: <value>` child line verbatim (quotes and all, e.g. `"off"`), or
/// `"zephyr"` when absent (every vendored app core omits `os:` — it's
/// implicitly Zephyr, the scaffold's only buildable runtime).
fn vendored_core_ids(board_yaml: &str) -> Vec<(&str, &str)> {
    let mut in_cores = false;
    let mut ids = Vec::new();
    for line in board_yaml.lines() {
        if !in_cores {
            in_cores = line == "cores:";
            continue;
        }
        if !line.is_empty() && !line.starts_with(' ') {
            break; // The next top-level key ends the cores: block.
        }
        if let Some(core) = line
            .strip_prefix("  ")
            .filter(|rest| !rest.starts_with(' '))
            .and_then(|rest| rest.strip_suffix(':'))
        {
            ids.push((core, "zephyr")); // A `  <core>:` entry key line.
        } else if let Some(os) = line.strip_prefix("    os: ") {
            if let Some(last) = ids.last_mut() {
                last.1 = os;
            }
        }
    }
    ids
}

/// Splice `--cores` companions (and a default RPMsg channel to the first
/// active one) into a vendored board.yaml, after the sole app-core entry
/// already inside its `cores:` block. A no-op when `cores` is empty. Mirrors
/// the retired `gen_board_yaml`'s companion-core loop, but inserts into the
/// vendored `cores:` block instead of building the whole file from scratch.
/// Skips any id already declared in the vendored `cores:` block (not just the
/// app core) so this can NEVER emit a duplicate mapping key — belt-and-
/// suspenders: the user-facing rejection is `init/mod.rs`'s upfront guard
/// (`vendored_core_ids_for`), this is the last line of defense for a path
/// that reaches here without going through it.
fn splice_companion_cores(board_yaml: &str, cores: &[(String, String)]) -> String {
    if cores.is_empty() {
        return board_yaml.to_string();
    }
    let Some(app_core) = vendored_app_core_key(board_yaml) else {
        return board_yaml.to_string();
    };
    let app_core = app_core.to_string();
    let existing_ids: Vec<&str> = vendored_core_ids(board_yaml)
        .into_iter()
        .map(|(id, _)| id)
        .collect();

    let mut companion_lines = String::new();
    for (id, os) in cores {
        if *id == app_core || existing_ids.contains(&id.as_str()) {
            continue;
        }
        companion_lines.push_str(&format!("  {id}:\n"));
        companion_lines.push_str(&format!("    os: {os}\n"));
        if os == "yocto" {
            companion_lines.push_str("    image: alp-image-edge\n");
        }
    }
    if companion_lines.is_empty() {
        return board_yaml.to_string();
    }

    // Insert right before the next top-level key (or EOF) that follows the
    // `cores:` block, so the new entries land as `cores:` siblings.
    let mut out = String::new();
    let mut in_cores = false;
    let mut inserted = false;
    for line in board_yaml.lines() {
        let is_top_level = !line.is_empty() && !line.starts_with([' ', '\t']);
        if is_top_level {
            if in_cores && !inserted {
                out.push_str(&companion_lines);
                inserted = true;
            }
            in_cores = line == "cores:";
        }
        out.push_str(line);
        out.push('\n');
    }
    if in_cores && !inserted {
        out.push_str(&companion_lines);
        inserted = true;
    }

    if inserted {
        if let Some((companion, _)) = cores
            .iter()
            .find(|(id, os)| *id != app_core && os != "off" && !existing_ids.contains(&id.as_str()))
        {
            out.push_str("\nipc:\n");
            out.push_str("  - kind: rpmsg\n");
            out.push_str("    name: alp_default_rpmsg\n");
            out.push_str(&format!("    endpoints: [{app_core}, {companion}]\n"));
            out.push_str("    carve_out_kb: 512\n");
        }
    }
    out
}

/// `trees`' real app-core id for `sku` — its family bucket's own
/// `board.yaml` `cores:` key, read from `trees` (not guessed) so a re-vendor
/// that changes it fails the matching unit test instead of silently
/// drifting from what `vendored_scaffold_files` actually plans.
fn vendored_app_core_in(trees: FamilyTrees, sku: &str) -> &'static str {
    let board_yaml = family_bucket(trees, sku)
        .iter()
        .find(|(path, _)| *path == "board.yaml")
        .map(|(_, content)| *content)
        .expect("every vendored tree has a board.yaml entry");
    vendored_app_core_key(board_yaml)
        .expect("every vendored board.yaml has a non-empty cores: block")
}

/// The vendored `minimal` scaffold's real app-core id for `sku`. `tan init`'s
/// upfront `--cores` validation (`commands/init/resolve.rs`) uses this
/// instead of `app_core_for_sku` for the `zephyr-app` template, so the
/// CLI-level check always agrees with what `vendored_minimal_files` actually
/// plans — the two independently derived the wrong core (alp-sdk#864's
/// `m55_hp`-for-V2N bug) until now.
pub fn vendored_app_core_for_sku(sku: &str) -> &'static str {
    vendored_app_core_in(MINIMAL, sku)
}

/// The vendored `sensor` scaffold's real app-core id for `sku` — same
/// derivation as `vendored_app_core_for_sku`, off the `sensor` tree instead
/// of `minimal`. Values happen to match `minimal`'s today (`E1M-AEN801` ->
/// `m55_hp`, `E1M-V2N101` -> `m33_sm`) but are read from `sensor`'s own
/// vendored `board.yaml` so a future `sensor`-only re-vendor can't silently
/// disagree with what `vendored_sensor_files` plans.
pub fn vendored_sensor_app_core_for_sku(sku: &str) -> &'static str {
    vendored_app_core_in(SENSOR, sku)
}

/// The vendored `edge-ai` scaffold's real app-core id for `sku` — same
/// derivation as `vendored_app_core_for_sku`, off the `edge-ai` tree instead
/// of `minimal`. `edge-ai` is heterogeneous: its `board.yaml` lists the
/// companion core (`a55_cluster`/`a32_cluster`, `os: "off"`) BEFORE the app
/// core, so this depends on `vendored_app_core_key`'s `app:`-owner scan, not
/// positional "first child".
pub fn vendored_edge_ai_app_core_for_sku(sku: &str) -> &'static str {
    vendored_app_core_in(EDGE_AI, sku)
}

/// `trees`' declared `(core id, os)` pairs for `sku` — app core plus any
/// pre-declared companions, read from `trees`' own vendored `board.yaml`.
fn vendored_core_ids_in(trees: FamilyTrees, sku: &str) -> Vec<(String, String)> {
    let board_yaml = family_bucket(trees, sku)
        .iter()
        .find(|(path, _)| *path == "board.yaml")
        .map(|(_, content)| *content)
        .expect("every vendored tree has a board.yaml entry");
    vendored_core_ids(board_yaml)
        .into_iter()
        .map(|(id, os)| (id.to_string(), os.to_string()))
        .collect()
}

/// The vendored template's declared `(core id, os)` pairs for `sku` — app
/// core plus any pre-declared companion (e.g. edge-ai's
/// `a55_cluster`/`a32_cluster`, `os: "off"`). `tan init`'s upfront `--cores`
/// guard (`commands/init/mod.rs`) uses this to reject a `--cores` id that
/// collides with a companion the SCAFFOLD already ships — appending a second
/// entry for it would emit a duplicate `cores:` mapping key (rejected by
/// `tan validate`/serde_yaml; the SDK Python loader takes last-wins). A
/// non-vendored (hand-generated) template's board.yaml only ever starts with
/// its app core — no pre-declared companion for `--cores` to collide with —
/// so this returns empty for it.
pub fn vendored_core_ids_for(template_id: WizardTemplateId, sku: &str) -> Vec<(String, String)> {
    let trees = match template_id {
        WizardTemplateId::ZephyrApp => MINIMAL,
        WizardTemplateId::SensorStarter => SENSOR,
        WizardTemplateId::EdgeAiStarter => EDGE_AI,
        _ => return Vec::new(),
    };
    vendored_core_ids_in(trees, sku)
}

/// Plan a mapped template's files from its vendored SDK scaffold tree: pick
/// the SKU family's vendored tree, retarget `board.yaml`'s `som.sku:` line
/// onto the requested `sku` (a byte-exact no-op when `sku` is the tree's own
/// vendored SKU), and splice in any `--cores` companions.
fn vendored_scaffold_files(
    trees: FamilyTrees,
    sku: &str,
    cores: &[(String, String)],
) -> Vec<(String, String)> {
    family_bucket(trees, sku)
        .iter()
        .map(|(path, content)| {
            let content = if *path == "board.yaml" {
                splice_companion_cores(&retarget_board_yaml_som(content, sku), cores)
            } else {
                (*content).to_string()
            };
            (path.to_string(), content)
        })
        .collect()
}

/// Plan the `zephyr-app` template's files from the vendored SDK `minimal` scaffold.
pub(super) fn vendored_minimal_files(
    sku: &str,
    cores: &[(String, String)],
) -> Vec<(String, String)> {
    vendored_scaffold_files(MINIMAL, sku, cores)
}

/// Plan the `sensor-starter` template's files from the vendored SDK `sensor` scaffold.
pub(super) fn vendored_sensor_files(
    sku: &str,
    cores: &[(String, String)],
) -> Vec<(String, String)> {
    vendored_scaffold_files(SENSOR, sku, cores)
}

/// Plan the `edge-ai-starter` template's files from the vendored SDK
/// `edge-ai` scaffold.
pub(super) fn vendored_edge_ai_files(
    sku: &str,
    cores: &[(String, String)],
) -> Vec<(String, String)> {
    vendored_scaffold_files(EDGE_AI, sku, cores)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_aen_sku_is_a_byte_exact_passthrough() {
        let files = vendored_minimal_files("E1M-AEN801", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(board.as_str(), MINIMAL_AEN[2].1);
    }

    #[test]
    fn canonical_v2n_sku_is_a_byte_exact_passthrough() {
        let files = vendored_minimal_files("E1M-V2N101", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(board.as_str(), MINIMAL_V2N[2].1);
    }

    #[test]
    fn non_canonical_sku_retargets_only_the_sku_line() {
        let files = vendored_minimal_files("E1M-AEN701", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("sku: E1M-AEN701"));
        assert!(!board.contains("E1M-AEN801"));
        // Everything else (the family's preset/topology) is untouched.
        assert!(board.contains("preset: e1m-evk"));
    }

    #[test]
    fn v2n_family_sku_picks_the_v2n_tree() {
        let files = vendored_minimal_files("E1M-V2M101", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("sku: E1M-V2M101"));
        assert!(board.contains("preset: e1m-x-evk"));
    }

    #[test]
    fn unrecognized_family_defaults_to_the_aen_tree() {
        let files = vendored_minimal_files("E1M-NX9101", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("preset: e1m-evk"));
    }

    #[test]
    fn companion_cores_are_spliced_into_the_cores_block() {
        let cores = [
            ("m55_hp".to_string(), "zephyr".to_string()),
            ("a32_cluster".to_string(), "yocto".to_string()),
        ];
        let files = vendored_minimal_files("E1M-AEN801", &cores);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("  a32_cluster:\n    os: yocto\n    image: alp-image-edge\n"));
        assert!(board.contains("ipc:"));
        assert!(board.contains("endpoints: [m55_hp, a32_cluster]"));
        // Companion lands inside the `cores:` block, before `diagnostics:`.
        let cores_at = board.find("cores:").unwrap();
        let companion_at = board.find("a32_cluster:").unwrap();
        let diagnostics_at = board.find("diagnostics:").unwrap();
        assert!(cores_at < companion_at && companion_at < diagnostics_at);
    }

    #[test]
    fn off_companion_is_never_an_ipc_endpoint() {
        let cores = [
            ("m55_hp".to_string(), "zephyr".to_string()),
            ("m55_he".to_string(), "off".to_string()),
        ];
        let files = vendored_minimal_files("E1M-AEN801", &cores);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("  m55_he:\n    os: off\n"));
        assert!(!board.contains("ipc:"));
    }

    #[test]
    fn empty_cores_is_a_byte_exact_passthrough() {
        let files = vendored_minimal_files("E1M-AEN801", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(board.as_str(), MINIMAL_AEN[2].1);
    }

    #[test]
    fn non_board_yaml_files_are_untouched_by_retargeting() {
        // vendored_minimal_files only rewrites board.yaml (SKU retarget +
        // --cores splice); every other file passes through byte-identical
        // from its own family's vendored tree. CMakeLists.txt's `--core` flag
        // is per-family (alp-sdk#877 fix), so compare against the SAME family
        // (MINIMAL_V2N), not across families.
        let files = vendored_minimal_files("E1M-V2N101", &[]);
        let cmake = &files.iter().find(|(p, _)| p == "CMakeLists.txt").unwrap().1;
        assert_eq!(cmake.as_str(), MINIMAL_V2N[0].1);
        assert!(cmake.contains("EXTRA_CONF_FILE"));
        assert!(cmake.contains("--core m33_sm"));
    }

    #[test]
    fn vendored_app_core_matches_each_familys_board_yaml() {
        // The ground truth `commands/init/resolve.rs`'s upfront --cores check
        // must agree with for the zephyr-app template (alp-sdk#864/#877).
        assert_eq!(vendored_app_core_for_sku("E1M-V2N101"), "m33_sm");
        assert_eq!(vendored_app_core_for_sku("E1M-V2M101"), "m33_sm");
        assert_eq!(vendored_app_core_for_sku("E1M-AEN801"), "m55_hp");
        assert_eq!(vendored_app_core_for_sku("E1M-NX9101"), "m55_hp");
    }

    #[test]
    fn canonical_sensor_skus_are_a_byte_exact_passthrough() {
        let aen = vendored_sensor_files("E1M-AEN801", &[]);
        let aen_board = &aen.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(aen_board.as_str(), SENSOR_AEN[2].1);

        let v2n = vendored_sensor_files("E1M-V2N101", &[]);
        let v2n_board = &v2n.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(v2n_board.as_str(), SENSOR_V2N[2].1);
    }

    #[test]
    fn sensor_family_bucket_picks_the_right_tree_and_retargets_sku() {
        let files = vendored_sensor_files("E1M-V2M101", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("sku: E1M-V2M101"));
        assert!(board.contains("preset: e1m-x-evk"));

        let files = vendored_sensor_files("E1M-NX9101", &[]);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("preset: e1m-evk"));
    }

    #[test]
    fn vendored_sensor_app_core_matches_each_familys_board_yaml() {
        // The ground truth `commands/init/resolve.rs`'s upfront --cores check
        // must agree with for the sensor-starter template.
        assert_eq!(vendored_sensor_app_core_for_sku("E1M-V2N101"), "m33_sm");
        assert_eq!(vendored_sensor_app_core_for_sku("E1M-V2M101"), "m33_sm");
        assert_eq!(vendored_sensor_app_core_for_sku("E1M-AEN801"), "m55_hp");
        assert_eq!(vendored_sensor_app_core_for_sku("E1M-NX9101"), "m55_hp");
    }

    #[test]
    fn canonical_edge_ai_skus_are_a_byte_exact_passthrough() {
        let aen = vendored_edge_ai_files("E1M-AEN801", &[]);
        let aen_board = &aen.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(aen_board.as_str(), EDGE_AI_AEN[2].1);

        let v2n = vendored_edge_ai_files("E1M-V2N101", &[]);
        let v2n_board = &v2n.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(v2n_board.as_str(), EDGE_AI_V2N[2].1);
    }

    #[test]
    fn vendored_edge_ai_app_core_matches_each_familys_board_yaml() {
        // The ground truth `commands/init/resolve.rs`'s upfront --cores check
        // must agree with for the edge-ai-starter template -- and this is the
        // FIRST vendored template where the naive "first core listed" guess
        // (a55_cluster/a32_cluster, the companion) would have been wrong.
        assert_eq!(vendored_edge_ai_app_core_for_sku("E1M-V2N101"), "m33_sm");
        assert_eq!(vendored_edge_ai_app_core_for_sku("E1M-V2M101"), "m33_sm");
        assert_eq!(vendored_edge_ai_app_core_for_sku("E1M-AEN801"), "m55_hp");
        assert_eq!(vendored_edge_ai_app_core_for_sku("E1M-NX9101"), "m55_hp");
    }

    #[test]
    fn vendored_app_core_key_finds_the_app_core_not_the_first_listed_core() {
        // Regression guard for generalization B: edge-ai's V2N board.yaml
        // lists its companion `a55_cluster:` (`os: "off"`, no `app:`) BEFORE
        // the real app core `m33_sm:` (`app: ./src`). The old "first indented
        // child under cores:" heuristic would have returned `a55_cluster`.
        let board_yaml = EDGE_AI_V2N
            .iter()
            .find(|(path, _)| *path == "board.yaml")
            .map(|(_, content)| *content)
            .expect("edge-ai V2N tree has a board.yaml");
        assert_eq!(vendored_app_core_key(board_yaml), Some("m33_sm"));
        assert_ne!(vendored_app_core_key(board_yaml), Some("a55_cluster"));
    }

    #[test]
    fn edge_ai_cores_splice_targets_the_real_app_core_not_the_companion() {
        // With the old "first listed core" heuristic, splicing --cores onto
        // edge-ai's V2N tree would have keyed the IPC endpoint off the
        // companion `a55_cluster` instead of the real app core `m33_sm`.
        let cores = [("audio_dsp".to_string(), "zephyr".to_string())];
        let files = vendored_edge_ai_files("E1M-V2N101", &cores);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert!(board.contains("  audio_dsp:\n    os: zephyr\n"));
        assert!(board.contains("endpoints: [m33_sm, audio_dsp]"));
        // The vendored companion (already `os: "off"`) is untouched, not
        // mistaken for the app core.
        assert!(board.contains("  a55_cluster:\n    os: \"off\"\n"));
    }

    #[test]
    fn edge_ai_cores_matching_the_pre_declared_companion_does_not_duplicate_it() {
        // Defense-in-depth (`init/mod.rs`'s upfront guard is the user-facing
        // rejection): even reached directly, splice_companion_cores must
        // never emit a SECOND `a55_cluster:` mapping key for a companion the
        // vendored tree already ships -- the pre-existing entry (already
        // `os: "off"`) is left untouched rather than duplicated or silently
        // overridden with the caller's requested `os`.
        let cores = [("a55_cluster".to_string(), "yocto".to_string())];
        let files = vendored_edge_ai_files("E1M-V2N101", &cores);
        let board = &files.iter().find(|(p, _)| p == "board.yaml").unwrap().1;
        assert_eq!(board.matches("a55_cluster:").count(), 1);
        assert!(board.contains("  a55_cluster:\n    os: \"off\"\n"));
        assert!(!board.contains("ipc:"));
    }

    #[test]
    fn vendored_core_ids_for_edge_ai_includes_the_pre_declared_companion() {
        let ids = vendored_core_ids_for(WizardTemplateId::EdgeAiStarter, "E1M-V2N101");
        assert!(ids.contains(&("a55_cluster".to_string(), "\"off\"".to_string())));
        assert!(ids.contains(&("m33_sm".to_string(), "zephyr".to_string())));
    }

    #[test]
    fn vendored_core_ids_for_a_hand_generated_template_is_empty() {
        // No hand-generated board.yaml ever pre-declares a companion -- the
        // --cores collision guard has nothing to check for these templates.
        assert!(vendored_core_ids_for(WizardTemplateId::MinimalApp, "E1M-V2N101").is_empty());
    }
}
