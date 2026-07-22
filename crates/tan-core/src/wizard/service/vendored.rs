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
//! Only `WizardTemplateId::ZephyrApp` is mapped today (-> SDK `minimal`); see
//! the manifest for which tan templates were left on their existing
//! hand-written generator and why.

use super::example_catalog::retarget_board_yaml_som;

/// One vendored file: its path relative to the project root, and its exact
/// (LF, byte-for-byte) content as emitted by `alp_project.py --emit scaffold`.
type VendoredFile = (&'static str, &'static str);

macro_rules! vendored_tree {
    ($sku:literal) => {
        &[
            (
                "CMakeLists.txt",
                include_str!(concat!("../vendored/minimal/", $sku, "/CMakeLists.txt")),
            ),
            (
                "README.md",
                include_str!(concat!("../vendored/minimal/", $sku, "/README.md")),
            ),
            (
                "board.yaml",
                include_str!(concat!("../vendored/minimal/", $sku, "/board.yaml")),
            ),
            (
                "prj.conf",
                include_str!(concat!("../vendored/minimal/", $sku, "/prj.conf")),
            ),
            (
                "src/main.c",
                include_str!(concat!("../vendored/minimal/", $sku, "/src/main.c")),
            ),
            (
                "testcase.yaml",
                include_str!(concat!("../vendored/minimal/", $sku, "/testcase.yaml")),
            ),
        ]
    };
}

/// Vendored `minimal` scaffold for the Alif Ensemble family (captured for
/// `E1M-AEN801`, the SDK catalog's declared representative AEN SKU).
const MINIMAL_AEN: &[VendoredFile] = vendored_tree!("E1M-AEN801");

/// Vendored `minimal` scaffold for the Renesas RZ/V2N family (captured for
/// `E1M-V2N101`, the SDK catalog's declared representative V2N SKU).
const MINIMAL_V2N: &[VendoredFile] = vendored_tree!("E1M-V2N101");

/// Pick the vendored family bucket for `sku`. Mirrors `app_core_for_sku`'s own
/// family split: V2N/V2M -> the Renesas tree, everything else (including
/// E1M-NX9* and any SKU the SDK catalog doesn't cover) defaults to the Alif
/// Ensemble tree — the SDK catalog has no NXP-family scaffold at all today
/// (flagged in `vendored/MANIFEST.md`); `tan validate` re-checks the real SoM
/// once an SDK resolves, same caveat `app_core_for_sku` already documents.
fn family_bucket(sku: &str) -> &'static [VendoredFile] {
    if sku.starts_with("E1M-V2N") || sku.starts_with("E1M-V2M") {
        MINIMAL_V2N
    } else {
        MINIMAL_AEN
    }
}

/// Read the vendored `cores:` block's sole app-core key out of a vendored
/// board.yaml (e.g. `"m55_hp"`). Every vendored `minimal` board.yaml has
/// exactly one core entry before any `--cores` companion is spliced in, so
/// the first indented child line under the top-level `cores:` key is it. This
/// reads it from the vendored CONTENT rather than trusting `app_core_for_sku`
/// — the vendored `minimal` scaffold's `cores:` topology does not change with
/// `--sku` (the SDK only substitutes `som.sku:`/`preset:`, see
/// `vendored/MANIFEST.md`), so for a V2N SKU it stays `m55_hp`, not the
/// `m33_sm` `app_core_for_sku` would guess.
fn vendored_app_core_key(board_yaml: &str) -> Option<&str> {
    let mut after_cores = false;
    for line in board_yaml.lines() {
        if !after_cores {
            after_cores = line == "cores:";
            continue;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue; // A blank/comment line before the first core key.
        }
        return line.strip_prefix("  ")?.strip_suffix(':');
    }
    None
}

/// Splice `--cores` companions (and a default RPMsg channel to the first
/// active one) into a vendored board.yaml, after the sole app-core entry
/// already inside its `cores:` block. A no-op when `cores` is empty. Mirrors
/// the retired `gen_board_yaml`'s companion-core loop, but inserts into the
/// vendored `cores:` block instead of building the whole file from scratch.
fn splice_companion_cores(board_yaml: &str, cores: &[(String, String)]) -> String {
    if cores.is_empty() {
        return board_yaml.to_string();
    }
    let Some(app_core) = vendored_app_core_key(board_yaml) else {
        return board_yaml.to_string();
    };
    let app_core = app_core.to_string();

    let mut companion_lines = String::new();
    for (id, os) in cores {
        if *id == app_core {
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
        if let Some((companion, _)) = cores.iter().find(|(id, os)| *id != app_core && os != "off") {
            out.push_str("\nipc:\n");
            out.push_str("  - kind: rpmsg\n");
            out.push_str("    name: alp_default_rpmsg\n");
            out.push_str(&format!("    endpoints: [{app_core}, {companion}]\n"));
            out.push_str("    carve_out_kb: 512\n");
        }
    }
    out
}

/// The vendored `minimal` scaffold's real app-core id for `sku` — its family
/// bucket's own `board.yaml` `cores:` key. `tan init`'s upfront `--cores`
/// validation (`commands/init/mod.rs`) uses this instead of `app_core_for_sku`
/// for the `zephyr-app` template, so the CLI-level check always agrees with
/// what `vendored_minimal_files` actually plans — the two independently
/// derived the wrong core (alp-sdk#864's `m55_hp`-for-V2N bug) until now.
pub fn vendored_app_core_for_sku(sku: &str) -> &'static str {
    let board_yaml = family_bucket(sku)
        .iter()
        .find(|(path, _)| *path == "board.yaml")
        .map(|(_, content)| *content)
        .expect("every vendored tree has a board.yaml entry");
    vendored_app_core_key(board_yaml)
        .expect("every vendored board.yaml has a non-empty cores: block")
}

/// Plan the `zephyr-app` template's files from the vendored SDK `minimal`
/// scaffold: pick the SKU family's vendored tree, retarget `board.yaml`'s
/// `som.sku:` line onto the requested `sku` (a byte-exact no-op when `sku` is
/// the tree's own vendored SKU), and splice in any `--cores` companions.
pub(super) fn vendored_minimal_files(
    sku: &str,
    cores: &[(String, String)],
) -> Vec<(String, String)> {
    family_bucket(sku)
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
        // The ground truth `commands/init/mod.rs`'s upfront --cores check
        // must agree with for the zephyr-app template (alp-sdk#864/#877).
        assert_eq!(vendored_app_core_for_sku("E1M-V2N101"), "m33_sm");
        assert_eq!(vendored_app_core_for_sku("E1M-V2M101"), "m33_sm");
        assert_eq!(vendored_app_core_for_sku("E1M-AEN801"), "m55_hp");
        assert_eq!(vendored_app_core_for_sku("E1M-NX9101"), "m55_hp");
    }
}
