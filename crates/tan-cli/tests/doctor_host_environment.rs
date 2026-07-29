// SPDX-License-Identifier: Apache-2.0
//! End-to-end gate for `tan doctor`'s `homePath` check — the one host-
//! environment probe whose input a test can actually control.
//!
//! A subprocess, not a unit test, for the reason `doctor_prerequisites.rs`
//! gives: the check only says anything when the host is in a state no developer
//! machine is in. A normal `USERPROFILE`/`HOME` has no space, so the `Warn` arm
//! never executes in-process, and `host_home`'s `cfg!(windows)` env-var choice
//! could be hard-coded to either name with the suite still green. Spawning
//! `tan` with a home path that DOES contain a space is what makes the branch
//! reachable, through the real argv and the real envelope.
//!
//! The other two host facts cannot be driven this way and are honest about it:
//! `zephyrSdkAvailableForHost` reads `std::env::consts::{OS, ARCH}`, which are compile-time
//! constants, and `longPaths` reads `HKLM`, which a test must not write. Their
//! decisions are unit-tested exhaustively in `tan_core::host_env` instead; what
//! is asserted HERE is only that the command emits them at all.

use std::path::PathBuf;
use std::process::Command;

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` by the CLI's workspace auto-discovery (same reasoning as
/// `contract.rs`'s `fresh_dir`).
fn fresh_dir(tag: &str, leaf: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-hostenv-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join(leaf);
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

#[test]
fn a_home_path_with_a_space_is_reported_with_the_path_itself() {
    let work_dir = fresh_dir("home-space", "root");
    // The whole point: a leaf with a space, standing in for the very common
    // Windows `C:\Users\Jane Doe` that `%USERPROFILE%` inherits from a display
    // name the user never chose.
    let home = fresh_dir("home-space-home", "Jane Doe");

    // Only the variable THIS platform's `host_home` is supposed to read, with
    // the other one removed. Setting both would pass either way, leaving the
    // `cfg!(windows)` choice free to be wrong — and reading `HOME` on native
    // Windows resolves nothing on a normal host, so the check would silently
    // report "could not resolve" for everyone.
    let (used, ignored) = if cfg!(windows) {
        ("USERPROFILE", "HOME")
    } else {
        ("HOME", "USERPROFILE")
    };
    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(["--format", "json", "doctor"])
        .current_dir(&work_dir)
        .env("SOURCE_DATE_EPOCH", "0")
        .env(used, &home)
        .env_remove(ignored)
        .output()
        .expect("failed to spawn tan");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let envelope: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout is not JSON: {e}\n{stdout}"));
    let checks = envelope["data"]["checks"]
        .as_array()
        .unwrap_or_else(|| panic!("data.checks must be an array: {stdout}"))
        .clone();

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(home.parent().unwrap_or(&home));

    let find = |name: &str| -> serde_json::Value {
        checks
            .iter()
            .find(|c| c["name"] == name)
            .unwrap_or_else(|| panic!("{name} missing from {checks:?}"))
            .clone()
    };

    let home_check = find("homePath");
    assert_eq!(home_check["status"], "warn", "{home_check}");
    // Naming the ACTUAL path is the requirement — a generic "your home has a
    // space" leaves the user to go find out which path is meant, and it is the
    // assertion that fails if `host_home` reads the wrong variable or the
    // detail drops the interpolation.
    let detail = home_check["detail"].as_str().unwrap_or_default();
    assert!(
        detail.contains(&home.to_string_lossy().to_string()),
        "the detail must name the resolved home: {detail}"
    );
    // `Warn`, never `Fail`: a two-word account name is degraded-but-usable and
    // must not be what moves `tan doctor` to exit 4.
    assert_ne!(home_check["status"], "fail", "{home_check}");
    // ... and the fix has to be in `nextSteps`, i.e. the check went through
    // `append_doctor_check` rather than a bare push.
    let fix = home_check["fix"]
        .as_str()
        .expect("a spaced home is actionable");
    let next_steps = envelope["data"]["nextSteps"]
        .as_array()
        .expect("nextSteps is an array");
    assert!(
        next_steps.iter().any(|s| s == fix),
        "fix must reach nextSteps: {next_steps:?}"
    );

    // The two compile-time-decided checks: presence only, from the command
    // itself rather than from a helper called directly.
    assert_eq!(
        find("zephyrSdkAvailableForHost")["name"],
        "zephyrSdkAvailableForHost"
    );
    assert_eq!(
        checks.iter().any(|c| c["name"] == "longPaths"),
        cfg!(windows),
        "longPaths is Windows-only: {checks:?}"
    );
}
