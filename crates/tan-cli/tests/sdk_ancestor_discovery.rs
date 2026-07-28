// SPDX-License-Identifier: Apache-2.0
//! Issue #101: the documented Quickstart command
//! `tan --project examples/<cat>/<name> build`, run from an alp-sdk checkout
//! root, failed preflight with `no SDK selected` because SDK discovery never
//! walked UP to the enclosing checkout.
//!
//! A subprocess, not a unit test, because the bug lives in the COMPOSITION of
//! two functions, not in either one: `cli_workspace_root` reads the real
//! process cwd and joins `--project` onto it, so the workspace root discovery
//! is handed sits levels below the checkout. A unit test on `discover_sdk_root`
//! alone can be green while the composed path stays broken — the only way to
//! exercise the join is to spawn `tan` with a real cwd, which is what the
//! extension and the customer both do.
//!
//! `tan doctor --build` is the probe: it runs the SAME
//! `probe_build_preflight(g, &context)` that `tan build`'s gate does, and emits
//! its `sdk` check into the envelope — so the assertion below is on the exact
//! readiness line the issue's logs captured as `[x] sdk no SDK selected`.

use std::path::{Path, PathBuf};
use std::process::Command;

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` by the CLI's workspace auto-discovery (same reasoning as
/// `contract.rs`'s `fresh_dir`).
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-ancestor-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// `scripts/alp_project.py` is the only marker discovery probes, so an empty
/// file is a complete stand-in for a checkout as far as this test is concerned.
fn make_sdk_root(dir: &Path) {
    std::fs::create_dir_all(dir.join("scripts")).expect("create scripts dir");
    std::fs::write(dir.join("scripts").join("alp_project.py"), "").expect("write loader marker");
}

/// Run `tan --format json --project <project> doctor --build` in `cwd` and
/// return the `sdk` readiness check from the envelope.
fn sdk_check(cwd: &Path, project: &str, home: &Path) -> serde_json::Value {
    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args([
            "--format",
            "json",
            "--project",
            project,
            "doctor",
            "--build",
        ])
        .current_dir(cwd)
        .env("SOURCE_DATE_EPOCH", "0")
        // Isolate the machine-global pointer tier: a developer's real
        // `~/.alp/sdk-default` would otherwise answer and hide the discovery
        // tier this test exists to check.
        .env("HOME", home)
        .env("USERPROFILE", home)
        .output()
        .expect("failed to spawn tan");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let envelope: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout is not JSON: {e}\n{stdout}"));
    envelope["data"]["checks"]
        .as_array()
        .unwrap_or_else(|| panic!("data.checks must be an array: {stdout}"))
        .iter()
        .find(|c| c["name"] == "sdk")
        .unwrap_or_else(|| panic!("no `sdk` check in the report: {stdout}"))
        .clone()
}

#[test]
fn a_nested_project_resolves_the_enclosing_sdk_checkout() {
    // The issue's exact layout: an SDK root, a nested
    // `examples/<cat>/<name>` project inside it, and `--project` pointing at
    // the nested path. The enclosing checkout is three levels up, and there is
    // no lateral candidate at all — `examples/peripheral-io/alp-sdk` is not
    // something an alp-sdk checkout contains — so only an ancestor walk can
    // resolve this.
    let sdk_root = fresh_dir("nested");
    let home = fresh_dir("nested-home");
    make_sdk_root(&sdk_root);
    let project = "examples/peripheral-io/gpio-button-led";
    std::fs::create_dir_all(sdk_root.join("examples/peripheral-io/gpio-button-led"))
        .expect("create nested project dir");

    let sdk = sdk_check(&sdk_root, project, &home);

    let _ = std::fs::remove_dir_all(sdk_root.parent().unwrap_or(&sdk_root));
    let _ = std::fs::remove_dir_all(home.parent().unwrap_or(&home));

    assert_eq!(
        sdk["status"], "pass",
        "the enclosing checkout must resolve with no extra flags: {sdk}"
    );
}

#[test]
fn no_sdk_anywhere_up_the_tree_still_fails_and_names_the_scoped_switch() {
    // The negative: the walk must run out at the filesystem root rather than
    // latch onto something unrelated on the way up. And when it does fail, the
    // remediation has to name the `--project`-scoped switch — a bare
    // `tan sdk switch <path>` reports success and changes nothing about a
    // `--project` invocation's outcome, which is the dead end issue #101
    // documented.
    let work_dir = fresh_dir("bare");
    let home = fresh_dir("bare-home");
    let project = "examples/peripheral-io/gpio-button-led";
    std::fs::create_dir_all(work_dir.join("examples/peripheral-io/gpio-button-led"))
        .expect("create nested project dir");

    let sdk = sdk_check(&work_dir, project, &home);

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(home.parent().unwrap_or(&home));

    assert_eq!(
        sdk["status"], "fail",
        "no SDK anywhere up the tree must stay a failure: {sdk}"
    );
    let fix = sdk["fix"].as_str().unwrap_or_default();
    assert_eq!(fix, format!("tan --project {project} sdk switch <path>"));
    assert!(
        sdk["detail"]
            .as_str()
            .unwrap_or_default()
            .contains("--sdk-root"),
        "the one flag that always works must be named: {sdk}"
    );
}
