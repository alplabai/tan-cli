// SPDX-License-Identifier: Apache-2.0
//! Issue #110: the envelope's `project` object never named WHICH alp-sdk tan
//! actually resolved, so a consumer (the vscode extension) had no way to
//! tell — and the naive fix, re-resolving via `resolve_sdk_tiered` just to
//! fill the envelope, would silently report a path a DIFFERENT resolver's
//! candidate set found rather than the one the command actually used.
//!
//! Subprocess tests, not unit tests — like `sdk_ancestor_discovery.rs`, the
//! resolvers here read the real process cwd/env, and each subprocess is a
//! fresh process (so the recorder's thread-local state can never leak
//! between cases; no explicit reset needed here).

use std::path::{Path, PathBuf};
use std::process::Command;

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` (or `alp-sdk-upstream/`) by the CLI's auto-discovery — same
/// reasoning as `sdk_ancestor_discovery.rs`'s `fresh_dir`.
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-sdkreport-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// `scripts/alp_project.py` is the only marker discovery probes, so an empty
/// file is a complete stand-in for a checkout as far as this test is
/// concerned.
fn make_sdk_root(dir: &Path) {
    std::fs::create_dir_all(dir.join("scripts")).expect("create scripts dir");
    std::fs::write(dir.join("scripts").join("alp_project.py"), "").expect("write loader marker");
}

/// Run `tan --format json size` in `cwd` (optionally with `--sdk-root`) and
/// return the parsed envelope.
///
/// `tan size` is the command used throughout below: it calls BOTH
/// `resolve_cli_project_context` (first, building `project`) and
/// `resolve_sdk_root` (second, for the metadata root) — the same
/// two-resolver shape `flash`/`run`/`clean` all have — so it also exercises
/// the first-writer-wins rule. No `system-manifest.yaml` is ever created
/// here; `size` errors on that (`RuntimeFailure`), which is irrelevant to
/// these assertions — both resolvers already ran and recorded before that
/// error path builds its envelope.
fn run_size(cwd: &Path, home: &Path, sdk_root_flag: Option<&Path>) -> serde_json::Value {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_tan"));
    cmd.args(["--format", "json", "size"])
        .current_dir(cwd)
        .env("SOURCE_DATE_EPOCH", "0")
        // Isolate the machine-global pointer tier: a developer's real
        // `~/.alp/sdk-default` would otherwise answer and hide the tier
        // each case exists to check.
        .env("HOME", home)
        .env("USERPROFILE", home);
    if let Some(root) = sdk_root_flag {
        cmd.arg("--sdk-root").arg(root);
    }
    let output = cmd.output().expect("failed to spawn tan");
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout is not JSON: {e}\n{stdout}"))
}

/// The posix tail a cwd-derived root must end with: `/<unique-parent>/root`.
///
/// A discovery-tier root comes from the subprocess's own cwd, and what the OS
/// hands a process as its cwd is NOT the string the test passed in — in two
/// opposite directions, both of which have failed CI here:
///
/// - macOS resolves symlinks. `std::env::temp_dir()` is under
///   `/var/folders/...`, a symlink to `/private/var/folders/...`, so the CLI
///   honestly reports the resolved form while the test's handle holds the
///   unresolved one.
/// - Windows does NOT expand 8.3 short names. The GitHub runner's `TEMP` is
///   `C:\Users\RUNNER~1\AppData\Local\Temp`, which the CLI reports verbatim
///   while `std::fs::canonicalize` would expand it to `runneradmin`.
///
/// So the full path is not predictable from here and canonicalizing is wrong
/// on one platform whichever way it is done. The parent directory name is
/// unique per process, which is what the assertion actually needs. The leading
/// `/` keeps this a real check on `sdk_report::record`'s separator
/// normalization — a backslash regression fails it.
///
/// Only the EXPECTED side is built this way; the reported side is always
/// compared unmodified.
fn expected_tail(dir: &Path) -> String {
    let parent = dir
        .parent()
        .and_then(Path::file_name)
        .expect("scratch dir always has a named parent")
        .to_string_lossy()
        .into_owned();
    let leaf = dir
        .file_name()
        .expect("scratch dir always has a name")
        .to_string_lossy()
        .into_owned();
    format!("/{parent}/{leaf}")
}

fn cleanup(dirs: &[&Path]) {
    for dir in dirs {
        let _ = std::fs::remove_dir_all(dir.parent().unwrap_or(dir));
    }
}

#[test]
fn explicit_sdk_root_flag_is_reported_verbatim() {
    let project = fresh_dir("flag-project");
    let sdk = fresh_dir("flag-sdk");
    let home = fresh_dir("flag-home");
    make_sdk_root(&sdk);

    let envelope = run_size(&project, &home, Some(&sdk));
    cleanup(&[&project, &sdk, &home]);

    assert_eq!(envelope["sdk"]["sourceTier"], "sdkRootFlag", "{envelope}");
    // Expected side built as posix from the KNOWN input; the reported side is
    // compared UNMODIFIED — `sdk_report::record` must itself normalize the
    // separator, regardless of which resolver on which platform recorded it.
    let expected_root = sdk.to_string_lossy().replace('\\', "/");
    let reported_root = envelope["sdk"]["root"].as_str().unwrap_or_default();
    assert_eq!(reported_root, expected_root, "{envelope}");
}

#[test]
fn workspace_root_self_discovery_is_reported_as_discovery() {
    let sdk = fresh_dir("self-sdk");
    let home = fresh_dir("self-home");
    make_sdk_root(&sdk);

    // No --sdk-root, no pin — cwd IS the sdk checkout, resolved purely by
    // self-discovery.
    let envelope = run_size(&sdk, &home, None);
    let tail = expected_tail(&sdk);
    cleanup(&[&sdk, &home]);

    assert_eq!(envelope["sdk"]["sourceTier"], "discovery", "{envelope}");
    // Reported side compared unmodified, as in
    // `explicit_sdk_root_flag_is_reported_verbatim`. Unlike that test this root
    // arrives via the cwd rather than an argument, so its leading component is
    // whatever the OS handed the subprocess — see `expected_tail`.
    let reported_root = envelope["sdk"]["root"].as_str().unwrap_or_default();
    assert!(
        reported_root.ends_with(&tail),
        "expected the self-discovered checkout {tail:?}, got {reported_root:?}: {envelope}"
    );
}

#[test]
fn nothing_resolved_omits_the_sdk_key_entirely() {
    let work = fresh_dir("none-project");
    let home = fresh_dir("none-home");

    let envelope = run_size(&work, &home, None);
    cleanup(&[&work, &home]);

    let obj = envelope
        .as_object()
        .expect("envelope must be a JSON object");
    assert!(
        !obj.contains_key("sdk"),
        "no SDK resolved -> `sdk` must be ABSENT (not null): {envelope}"
    );
}

#[test]
fn reported_root_is_what_the_command_actually_used_not_a_fresh_resolve_sdk_tiered_call() {
    // Asymmetric tree: `resolve_cli_project_context`'s discovery candidate
    // set is workspace-self / sibling `alp-sdk` / ancestor-walk — NONE of
    // which considers a sibling named `alp-sdk-upstream`. `resolve_sdk_root`
    // (which `tan size` also calls, second) DOES consider `alp-sdk-upstream`
    // as a lateral candidate. So the context call records nothing here, and
    // `resolve_sdk_root`'s find becomes the first (and only) writer.
    //
    // A regression that filled the envelope's `sdk` key from a fresh
    // `resolve_sdk_tiered` call instead (its discovery is
    // `tan_core::discover_workspace_sdk` — the SAME alp-sdk-upstream-blind
    // candidate set as the context call) would find nothing in this exact
    // tree, so `sdk` would come back ABSENT — which the assertions below
    // reject.
    let parent = std::env::temp_dir().join(format!("tan-sdkreport-asym-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let project = parent.join("project");
    let upstream = parent.join("alp-sdk-upstream");
    std::fs::create_dir_all(&project).expect("create project dir");
    make_sdk_root(&upstream);
    let home = fresh_dir("asym-home");

    let envelope = run_size(&project, &home, None);
    let _ = std::fs::remove_dir_all(&parent);
    cleanup(&[&home]);

    assert_eq!(envelope["sdk"]["sourceTier"], "discovery", "{envelope}");
    // Reported side compared unmodified — `sdk_report::record` normalizes the
    // separator itself, so a posix suffix must already be present verbatim.
    let reported_root = envelope["sdk"]["root"].as_str().unwrap_or_default();
    assert!(
        reported_root.ends_with("alp-sdk-upstream"),
        "expected the alp-sdk-upstream sibling (only resolve_sdk_root's own \
         discovery sees it) to be reported, got {reported_root:?}: {envelope}"
    );
}
