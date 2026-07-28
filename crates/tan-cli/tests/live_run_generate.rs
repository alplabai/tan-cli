// SPDX-License-Identifier: Apache-2.0
//! Live-run e2e for `tan generate`: spawns a REAL `alp_project.py` from a
//! real, pinned `alp-sdk` checkout and asserts what actually lands on disk.
//!
//! Every other `generate` test (`crates/tan-cli/src/commands/generate.rs`'s
//! own `#[cfg(test)]` module, `contract.rs`'s `generate-board-yaml-missing`
//! golden) proves tan composes the argv IT THINKS is right, against either a
//! stub `scripts/alp_project.py` (empty file) or a case that never reaches a
//! spawn at all. Nothing before this file proved alp-sdk's REAL loader
//! accepts that argv, or that files land where tan claims they will. PR #157
//! is the proof this class of gap bites: its output-directory bug (tan
//! writing `build/boards/<core>/` while `alp_project.py` strips an
//! `alp_e1m_<sku-slug>_<core>` prefix because `--output` is expected to BE
//! that directory) was pure-argv-adjacent and was caught only by a human
//! reading both sides. A live run catches it mechanically -- see
//! `zephyr_board_som_swap_does_not_collide` below, which reproduces exactly
//! the SoM-swap collision #157 fixed.
//!
//! ## Getting the pinned SDK
//!
//! `PINNED_SDK_TAG` (the SHA the parity gate holds `alp-sdk` to) lives in
//! `.github/workflows/parity.yml` as a CI env var, not a Rust constant, and
//! this file deliberately does not duplicate it a second place where it could
//! rot independently of that one. Instead: `TAN_LIVE_RUN_SDK_ROOT` names a
//! filesystem path to an alp-sdk checkout, and CI's `seam2` job (which already
//! checks alp-sdk out at exactly `PINNED_SDK_TAG` into `alp-sdk/` for its own
//! materialise/build steps) sets that env var to that same checkout -- so this
//! suite inherits the pin BY CONSTRUCTION. A developer running this locally
//! points it at any alp-sdk checkout they have (ideally also at the pin, but
//! nothing here enforces that -- these are shape/no-collision assertions
//! about how `tan` drives the loader, not a byte-parity oracle diff, which is
//! `tests/parity/seam1_field_diff.py`'s job).
//!
//! ## Skip vs. fail
//!
//! No reachable `TAN_LIVE_RUN_SDK_ROOT`: a real alp-sdk checkout plus a
//! working Python (with PyYAML + jsonschema) is a big ask for every
//! contributor's box, so this suite no-ops locally with a printed notice --
//! same self-skip shape `tests/parity/scaffold_byte_parity.py` /
//! `kconfig_fixture_parity.py` already use.
//!
//! The trap that shape has: a suite that quietly no-ops EVERYWHERE is a
//! suite that never runs, and `cargo test` reports it as a plain pass either
//! way -- there is no "skipped" verdict a human scanning CI would notice. So
//! this file additionally checks `CI` (GitHub Actions sets `CI=true` on every
//! runner, unconditionally): with `CI` set, a missing `TAN_LIVE_RUN_SDK_ROOT`
//! is a HARD PANIC, not a quiet return. CI's environment is guaranteed to
//! have the var wired (`.github/workflows/parity.yml`'s `seam2` job sets it);
//! a missing var there means that wiring itself broke, and that must be loud.
use std::path::{Path, PathBuf};
use std::process::Command;

const SDK_ROOT_ENV: &str = "TAN_LIVE_RUN_SDK_ROOT";

/// A fresh, empty scratch directory for one case, nested under its own fresh
/// parent -- mirrors `contract.rs`'s `fresh_dir` (never directly under the
/// shared system temp root: `tan`'s sibling-SDK auto-discovery probes the
/// work dir's parent for a sibling `alp-sdk/`, and a shared parent could pick
/// up another case's leftovers).
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-liverun-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// True on every GitHub Actions runner (and most other CI providers, which
/// all set this by convention) -- the signal that the environment THIS TEST
/// needs is supposed to already be there, so a skip must not happen quietly.
fn ci_environment() -> bool {
    matches!(std::env::var("CI"), Ok(v) if v == "true" || v == "1")
}

/// Resolves `TAN_LIVE_RUN_SDK_ROOT` to a checkout that actually has the
/// loader script, or decides how to react to its absence.
///
/// `None` is returned ONLY for the "not wired at all, and we are not in CI"
/// case -- a legitimate local skip. Every other absence/misconfiguration
/// (var set but the path has no `scripts/alp_project.py`; var unset AND
/// `CI` is set) is a hard panic: those are not "the environment doesn't have
/// this", they are "the environment claims to have this and is wrong",
/// which is exactly the transport-artefact-vs-real-answer distinction this
/// suite's own task write-up calls out (a first result that looks like an
/// answer can be a harness defect, not the real verdict).
fn require_live_sdk(test_name: &str) -> Option<PathBuf> {
    match std::env::var(SDK_ROOT_ENV) {
        Ok(raw) if !raw.trim().is_empty() => {
            let root = PathBuf::from(raw);
            let loader = root.join("scripts").join("alp_project.py");
            assert!(
                loader.is_file(),
                "{test_name}: {SDK_ROOT_ENV}={root:?} has no scripts/alp_project.py -- \
                 the env var IS set, so this is a misconfigured checkout, not an absent \
                 one; fix the path rather than letting the suite skip."
            );
            Some(root)
        }
        _ if ci_environment() => {
            panic!(
                "{test_name}: {SDK_ROOT_ENV} is not set, but CI=true -- this suite's CI \
                 wiring (.github/workflows/parity.yml's `seam2` job) is supposed to set it \
                 to its pinned alp-sdk checkout. A live-run e2e that can silently skip in \
                 CI is a live-run e2e that never actually runs there; failing loudly here \
                 is the whole point."
            );
        }
        _ => {
            eprintln!(
                "{test_name}: skipping -- {SDK_ROOT_ENV} is not set (no local alp-sdk \
                 checkout wired up). This is a LOCAL-ONLY no-op; CI always sets it (see \
                 require_live_sdk's panic branch)."
            );
            None
        }
    }
}

/// Spawns the real `tan` binary (`CARGO_BIN_EXE_tan`, built by cargo for this
/// integration test -- same mechanism `contract.rs` uses) with `--sdk-root
/// <sdk_root> --format json` plus `extra_args`, in `work_dir`, and parses its
/// stdout as the JSON envelope. Asserts stdout IS valid JSON and stderr is
/// empty (the `--format json` contract every other envelope test in this repo
/// holds commands to) -- a spawn/parse failure here is a harness bug, not a
/// generate-target verdict, so it panics with the raw output rather than
/// silently returning something callers would misread as `ok: false`.
fn run_tan(sdk_root: &Path, work_dir: &Path, extra_args: &[&str]) -> serde_json::Value {
    let mut args: Vec<&str> = vec!["--sdk-root"];
    let sdk_root_str = sdk_root.to_string_lossy().into_owned();
    args.push(&sdk_root_str);
    args.push("--format");
    args.push("json");
    args.extend_from_slice(extra_args);

    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(&args)
        .current_dir(work_dir)
        .output()
        .unwrap_or_else(|e| panic!("failed to spawn tan {args:?}: {e}"));

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.trim().is_empty(),
        "tan {args:?}: unexpected stderr under --format json:\n{stderr}"
    );
    serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("tan {args:?}: stdout is not valid JSON: {e}\n---\n{stdout}"))
}

/// Substrings that mean "the interpreter/module setup is broken", as opposed
/// to "the target itself produced a real error". Distinguishing the two
/// matters here specifically: a spawned `alp_project.py` that dies because
/// python is missing, a module (PyYAML/jsonschema) is not installed, or the
/// SDK checkout is absent is NOT evidence the generate TARGET is broken --
/// asserting `ok == true` and reporting a bare "target X failed" on a missing
/// interpreter would misdirect exactly the way this suite's own task
/// write-up warns against.
const ENVIRONMENT_FAILURE_SIGNATURES: &[&str] = &[
    "No module named",
    "ModuleNotFoundError",
    "os error 2",
    "cannot find the file specified",
    "is not recognized as an internal or external command",
    "python-too-old",
    "sdk-root-unresolved",
];

/// Asserts `envelope["ok"] == true`, and if it is not, panics with a message
/// that names WHICH bucket the failure is in (environment vs. target) rather
/// than a bare "expected ok, got false" -- see `ENVIRONMENT_FAILURE_SIGNATURES`.
fn assert_ok(envelope: &serde_json::Value, context: &str) {
    if envelope["ok"] == serde_json::Value::Bool(true) {
        return;
    }
    let issues = envelope["issues"].to_string();
    let bucket = if ENVIRONMENT_FAILURE_SIGNATURES
        .iter()
        .any(|sig| issues.contains(sig))
    {
        "ENVIRONMENT FAILURE (python interpreter / missing module / bad SDK root) -- \
         NOT evidence the generate target itself is broken"
    } else {
        "target failure (the generate target itself reported an error)"
    };
    panic!("{context}: tan reported ok=false -- {bucket}:\n{issues}\nfull envelope: {envelope}");
}

/// Every file written under a `--target zephyr-board` directory, relative to
/// that directory -- used to assert cross-SKU non-collision by name.
fn dir_file_names(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("read_dir {dir:?}: {e}"))
        .map(|entry| {
            entry
                .expect("dir entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    names
}

const AEN801_BOARD_YAML: &str = "\
som:\n  sku: E1M-AEN801\n\npreset: e1m-evk\nsupported_boards:\n  - e1m-evk\n  - e1m-x-evk\ncores:\n  m55_hp:\n    app: ./src\n    peripherals: []\n\ndiagnostics:\n  log_level: info\n";

fn v2_som_m33_sm_board_yaml(sku: &str) -> String {
    format!(
        "som:\n  sku: {sku}\n  hw_rev: r1\n\npreset: e1m-x-evk\ncores:\n  m33_sm:\n    \
         app: ./m33_sm\n    peripherals: [adc, pwm, i2c, gpio]\n\ndiagnostics:\n  log_level: info\n"
    )
}

/// tan-cli#116/#157: a real `--target zephyr-board` run against the pinned
/// SDK, examined for what a SUCCESSFUL envelope + on-disk result actually
/// look like -- the repo's only prior `generate` golden
/// (`generate-board-yaml-missing`) is a failure-path case, and modelling the
/// happy path on it by assumption is exactly the trap the task that produced
/// this file calls out. E1M-AEN801/m55_hp is the one AEN SKU+core pair whose
/// SoC spec declares a `zephyr_cpucluster` at the pinned tag (verified by
/// hand against `metadata/socs/alif/ensemble/e8.json`), so it is the richest
/// real success case available: alp_project.py's `gen_zephyr_board` writes a
/// full out-of-tree Zephyr board (`.dts`, `_defconfig`, `-pinctrl.dtsi`,
/// `.yaml`, `board.yml`, two `Kconfig.*` files), not just the board.yml/
/// Kconfig/soc-yaml triplet a qualified-board SKU (V2N/V2M) gets.
#[test]
fn zephyr_board_generate_matches_the_real_alp_project_output_shape() {
    let Some(sdk_root) =
        require_live_sdk("zephyr_board_generate_matches_the_real_alp_project_output_shape")
    else {
        return;
    };
    let work_dir = fresh_dir("zb-shape");
    std::fs::write(work_dir.join("board.yaml"), AEN801_BOARD_YAML).unwrap();

    let envelope = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "zephyr-board", "--core", "m55_hp"],
    );
    assert_ok(&envelope, "zephyr-board generate for E1M-AEN801/m55_hp");

    let expected_dir_name = tan_core::zephyr_board_dir_name("E1M-AEN801", "m55_hp")
        .expect("E1M-AEN801/m55_hp must resolve a board dir name");
    assert_eq!(expected_dir_name, "alp_e1m_aen801_m55_hp");

    let written = envelope["data"]["written"]
        .as_array()
        .expect("data.written must be an array");
    assert_eq!(written.len(), 1, "envelope: {envelope}");
    let written_path = written[0].as_str().unwrap().replace('\\', "/");
    assert_eq!(
        written_path,
        format!("build/boards/{expected_dir_name}"),
        "data.written[0] must name the SKU-scoped board directory, not a bare core id"
    );

    let board_dir = work_dir
        .join("build")
        .join("boards")
        .join(&expected_dir_name);
    assert!(board_dir.is_dir(), "{board_dir:?} was not created");
    let names = dir_file_names(&board_dir);
    assert!(names.contains(&"board.yml".to_string()), "{names:?}");
    assert!(
        names.contains(&format!("Kconfig.{expected_dir_name}")),
        "{names:?}"
    );
    assert!(
        names.contains(&"Kconfig.defconfig".to_string()),
        "{names:?}"
    );
    assert!(
        names.iter().any(|n| n.ends_with(".dts")),
        "no .dts file in {names:?}"
    );
    assert!(
        names.iter().any(|n| n.ends_with("_defconfig")),
        "no _defconfig file in {names:?}"
    );
    assert!(
        names.iter().any(|n| n.ends_with("-pinctrl.dtsi")),
        "no -pinctrl.dtsi file in {names:?}"
    );
    assert!(
        names.iter().any(|n| n.ends_with(".yaml")),
        "no .yaml file in {names:?}"
    );

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap());
}

/// tan-cli#116 review finding 1 / PR #157, proven against the REAL
/// `alp_project.py` instead of the pure `zephyr_board_dir_name` unit test:
/// `--target zephyr-board --core m33_sm` for E1M-V2N101, retargeted to
/// E1M-V2M101 (a real, first-class SoM-swap on the SAME PCB -- see the repo
/// memory note "V2N family is one PCB, variant-populated"), re-generated with
/// the SAME core, must land in two DIFFERENT directories with no file
/// cross-contamination. A test that only ran one SKU would have passed with
/// #157's collision bug still live -- that is the entire point of this test.
#[test]
fn zephyr_board_som_swap_does_not_collide() {
    let Some(sdk_root) = require_live_sdk("zephyr_board_som_swap_does_not_collide") else {
        return;
    };
    let work_dir = fresh_dir("zb-swap");
    std::fs::write(
        work_dir.join("board.yaml"),
        v2_som_m33_sm_board_yaml("E1M-V2N101"),
    )
    .unwrap();

    let envelope_1 = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "zephyr-board", "--core", "m33_sm"],
    );
    assert_ok(&envelope_1, "zephyr-board generate for E1M-V2N101/m33_sm");

    let dir_name_1 = tan_core::zephyr_board_dir_name("E1M-V2N101", "m33_sm").unwrap();
    assert_eq!(dir_name_1, "alp_e1m_v2n101_m33_sm");
    let dir_1 = work_dir.join("build").join("boards").join(&dir_name_1);
    assert!(dir_1.is_dir(), "{dir_1:?} missing after run 1");
    let names_1 = dir_file_names(&dir_1);
    assert!(
        names_1.iter().any(|n| n.contains("v2n101")),
        "run 1 dir has no v2n101-named file: {names_1:?}"
    );

    // Retarget the SAME project to a different SoM, same core -- the SoM-swap
    // flow the project memory note calls first-class.
    std::fs::write(
        work_dir.join("board.yaml"),
        v2_som_m33_sm_board_yaml("E1M-V2M101"),
    )
    .unwrap();
    let envelope_2 = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "zephyr-board", "--core", "m33_sm"],
    );
    assert_ok(&envelope_2, "zephyr-board generate for E1M-V2M101/m33_sm");

    let dir_name_2 = tan_core::zephyr_board_dir_name("E1M-V2M101", "m33_sm").unwrap();
    assert_eq!(dir_name_2, "alp_e1m_v2m101_m33_sm");
    assert_ne!(
        dir_name_1, dir_name_2,
        "two different SoMs sharing a core id must not resolve the same board directory"
    );
    let dir_2 = work_dir.join("build").join("boards").join(&dir_name_2);
    assert!(dir_2.is_dir(), "{dir_2:?} missing after run 2");

    // The FAILING case #157 fixed: run 1's directory + files must survive run
    // 2 untouched -- a bare-core-id directory name would have made run 2
    // overwrite run 1's files in the SAME `build/boards/m33_sm/` path.
    assert!(
        dir_1.is_dir(),
        "run 1's directory was removed/overwritten by run 2"
    );
    let names_1_after = dir_file_names(&dir_1);
    assert_eq!(
        names_1, names_1_after,
        "run 1's file set changed after run 2 -- the two SKUs collided"
    );

    // Run 2's own directory must carry ONLY v2m101-named files -- no v2n101
    // file leaked in beside it (the exact defect tan-cli#116 review finding 1
    // describes: "one board directory declaring one board, carrying two
    // boards' sources").
    let names_2 = dir_file_names(&dir_2);
    assert!(
        names_2.iter().any(|n| n.contains("v2m101")),
        "run 2 dir has no v2m101-named file: {names_2:?}"
    );
    assert!(
        !names_2.iter().any(|n| n.contains("v2n101")),
        "run 2 dir ({dir_2:?}) contains a v2n101-named file -- the two SoMs collided: {names_2:?}"
    );

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap());
}

/// tan-cli#114: `--target west-libraries` against the real loader must write
/// a file that PARSES as YAML (not merely a non-empty file -- a truncated or
/// half-written stream would still pass a bytes-not-empty check) and whose
/// real shape matches what `docs/board-config-emit.md` documents.
#[test]
fn west_libraries_target_writes_parseable_yaml() {
    let Some(sdk_root) = require_live_sdk("west_libraries_target_writes_parseable_yaml") else {
        return;
    };
    let work_dir = fresh_dir("west-libs");
    std::fs::write(work_dir.join("board.yaml"), AEN801_BOARD_YAML).unwrap();

    let envelope = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "west-libraries"],
    );
    assert_ok(&envelope, "west-libraries generate");

    let path = work_dir
        .join("build")
        .join("generated")
        .join("alp-west-libs.yml");
    assert!(path.is_file(), "{path:?} was not written");
    let text = std::fs::read_to_string(&path).unwrap();
    let parsed: serde_yaml::Value = serde_yaml::from_str(&text)
        .unwrap_or_else(|e| panic!("{path:?} is not valid YAML: {e}\n---\n{text}"));
    let zephyr_project_name = parsed["manifest"]["projects"][0]["name"]
        .as_str()
        .unwrap_or_else(|| panic!("manifest.projects[0].name missing from parsed YAML:\n{text}"));
    assert_eq!(zephyr_project_name, "zephyr");

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap());
}

/// tan-cli#113: `hw-info-h` reachable as a `tan generate --target`, writing
/// the conventional path this PR derived from `docs/board-config-emit.md` at
/// the pinned SDK tag, AND `--core` genuinely forwarded end to end -- not
/// merely accepted by argv composition and silently dropped before the
/// spawn (tan-cli#117 review finding 2's concern, now checked for this
/// target too). Proven by a REAL content difference: alp_project.py bakes
/// the forwarded core into `ALP_HW_BUILD_PRIMARY_CORE`.
#[test]
fn hw_info_h_target_is_reachable_and_forwards_core() {
    let Some(sdk_root) = require_live_sdk("hw_info_h_target_is_reachable_and_forwards_core") else {
        return;
    };
    let work_dir = fresh_dir("hw-info-h");
    std::fs::write(work_dir.join("board.yaml"), AEN801_BOARD_YAML).unwrap();
    let header_path = work_dir
        .join("build")
        .join("generated")
        .join("alp_hw_info_build.h");

    let envelope_no_core = run_tan(&sdk_root, &work_dir, &["generate", "--target", "hw-info-h"]);
    assert_ok(&envelope_no_core, "hw-info-h generate (no --core)");
    assert!(header_path.is_file(), "{header_path:?} was not written");
    let no_core_text = std::fs::read_to_string(&header_path).unwrap();
    assert!(
        no_core_text.contains("#define ALP_HW_BUILD_SOM_SKU         \"E1M-AEN801\""),
        "missing ALP_HW_BUILD_SOM_SKU macro:\n{no_core_text}"
    );

    let envelope_with_core = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "hw-info-h", "--core", "m55_hp"],
    );
    assert_ok(&envelope_with_core, "hw-info-h generate (--core m55_hp)");
    let with_core_text = std::fs::read_to_string(&header_path).unwrap();
    assert!(
        with_core_text.contains("#define ALP_HW_BUILD_PRIMARY_CORE    \"m55_hp\""),
        "--core m55_hp was not forwarded into ALP_HW_BUILD_PRIMARY_CORE:\n{with_core_text}"
    );
    assert_ne!(
        no_core_text, with_core_text,
        "hw-info-h output is identical with and without --core -- the flag is being \
         accepted by argv composition but never reaching the real alp_project.py spawn"
    );

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap());
}

/// tan-cli#157 review finding: an argument-shape mistake must exit
/// `ValidationFailure` (2) with `generate.invalid-target`, never
/// `InternalFailure` (5) -- the extension maps the two exit codes to
/// different UI treatments, so a usage error reported as an internal fault
/// is consumer-visible. Checked against the REAL compiled binary (not just
/// `resolve_generate_targets`'s in-process unit test) for both directions:
/// `--core` REFUSED for a target that never reads it (`carrier-netlist`), and
/// REQUIRED-but-omitted for `zephyr-board`.
#[test]
fn core_forwarding_refusal_is_a_validation_failure_not_internal() {
    let Some(sdk_root) =
        require_live_sdk("core_forwarding_refusal_is_a_validation_failure_not_internal")
    else {
        return;
    };
    let work_dir = fresh_dir("core-refused");
    std::fs::write(work_dir.join("board.yaml"), AEN801_BOARD_YAML).unwrap();

    // REFUSED direction: --core does nothing for carrier-netlist.
    let refused = run_tan(
        &sdk_root,
        &work_dir,
        &[
            "generate",
            "--target",
            "carrier-netlist",
            "--core",
            "m55_hp",
        ],
    );
    assert_eq!(refused["ok"], serde_json::Value::Bool(false));
    assert_eq!(refused["exitCode"], 2, "envelope: {refused}");
    assert_ne!(
        refused["exitCode"], 5,
        "an ordinary --core/--target combination mistake must not exit InternalFailure (5)"
    );
    assert_eq!(refused["issues"][0]["code"], "generate.invalid-target");

    // REQUIRED-but-omitted direction: zephyr-board with no --core at all.
    let missing_core = run_tan(
        &sdk_root,
        &work_dir,
        &["generate", "--target", "zephyr-board"],
    );
    assert_eq!(missing_core["ok"], serde_json::Value::Bool(false));
    assert_eq!(missing_core["exitCode"], 2, "envelope: {missing_core}");
    assert_ne!(missing_core["exitCode"], 5);
    assert_eq!(missing_core["issues"][0]["code"], "generate.invalid-target");

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap());
}
