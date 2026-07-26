// SPDX-License-Identifier: Apache-2.0
//! Wiring gate for `tan doctor --build`'s `missingPrerequisites[].command` —
//! the one field alp-sdk-vscode's `runToolchainFix` puts behind a Fix button
//! (ADR 0021 Lane 1 P0a), and the only thing that turns a missing `ninja` from
//! `failed to launch (exit code: 1)` into something a user can press.
//!
//! A subprocess, not a unit test, because the field only says anything when a
//! tool is actually ABSENT: on a normal developer host `cmake` and `ninja` are
//! installed, so `missingPrerequisites` comes back as at most `[{west, null}]`
//! — whose `command` is `null` on every platform — and the host resolution that
//! picks WHICH `prerequisites.install.<os>` map the report reads could be
//! hard-coded with the suite still green. Spawning `tan` with a `PATH` that
//! resolves nothing is what makes the absent-tool branch reachable at all, and
//! it exercises the real argv + envelope path the extension shells.
//!
//! No SDK is resolvable from the scratch dir, so the commands asserted below come
//! from tan's fallback facts — which `tan_core::bootstrap::manifest`'s
//! `the_fallback_matches_the_real_manifest_field_for_field` pins byte-equal to the
//! vendored `metadata/bootstrap.json`. That is the case issue #90 called out as
//! the reason not to delete tan's hardcoded table: every SDK a customer can
//! install today has no `metadata/bootstrap.json` at all, and this test is what
//! proves those installs did not lose their install commands with the table.

use std::path::PathBuf;
use std::process::Command;

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` by the CLI's workspace auto-discovery (same reasoning as
/// `contract.rs`'s `fresh_dir`).
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-prereq-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

#[test]
fn an_absent_tool_carries_this_platforms_install_command() {
    let work_dir = fresh_dir("doctor-build");
    let empty = fresh_dir("empty-path");

    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(["--format", "json", "doctor", "--build"])
        .current_dir(&work_dir)
        // The whole point: an empty directory as the entire PATH, so `west`,
        // `cmake` and `ninja` are all reported absent regardless of what this
        // machine has installed.
        .env("PATH", &empty)
        .env("SOURCE_DATE_EPOCH", "0")
        .env("HOME", &work_dir)
        .env("USERPROFILE", &work_dir)
        .output()
        .expect("failed to spawn tan");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let envelope: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout is not JSON: {e}\n{stdout}"));
    let missing = envelope["data"]["missingPrerequisites"]
        .as_array()
        .unwrap_or_else(|| panic!("missingPrerequisites must name the absent tools: {stdout}"))
        .clone();

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(empty.parent().unwrap_or(&empty));

    let command_for = |tool: &str| -> serde_json::Value {
        missing
            .iter()
            .find(|m| m["tool"] == tool)
            .unwrap_or_else(|| panic!("{tool} must be reported missing: {missing:?}"))["command"]
            .clone()
    };

    // Each host gets ITS OWN package manager's command, from
    // `prerequisites.install.<os>` — never another host's, which is what losing
    // the host resolution would silently do (a `winget` line behind a Fix button
    // on Linux is worse than no button at all).
    //
    // `ninja` is Windows-only: `prerequisites.posix` does not list it, so neither
    // POSIX install map carries one and the field stays `null` there even though
    // `--build` probes the tool on every platform.
    if cfg!(windows) {
        assert_eq!(command_for("cmake"), "winget install -e --id Kitware.CMake");
        assert_eq!(
            command_for("ninja"),
            "winget install -e --id Ninja-build.Ninja"
        );
    } else if cfg!(target_os = "linux") {
        assert_eq!(command_for("cmake"), "sudo apt-get install -y cmake");
        assert_eq!(command_for("ninja"), serde_json::Value::Null);
    } else if cfg!(target_os = "macos") {
        assert_eq!(command_for("cmake"), "brew install cmake");
        assert_eq!(command_for("ninja"), serde_json::Value::Null);
    } else {
        // A POSIX host the manifest does not serve: every command `null`, which
        // is what every POSIX host reported before alp-sdk#959.
        assert_eq!(command_for("cmake"), serde_json::Value::Null);
        assert_eq!(command_for("ninja"), serde_json::Value::Null);
    }
    // `west` is installed into the workspace venv by `tan bootstrap`, not by a
    // package manager, so no OS's install map lists one — and an invented one
    // would be worse than `null` in a field rendered as a button.
    assert_eq!(command_for("west"), serde_json::Value::Null);
}
