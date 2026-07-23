// SPDX-License-Identifier: Apache-2.0
//! Golden-envelope drift gate: pins the JSON wire format the vscode
//! extension parses (`{command,ok,exitCode,project,data,issues}`, the
//! exit-code contract in `src/exit.rs`) against real `tan` subprocess output.
//!
//! Fixtures live at `contract/envelopes/<case>/` (repo root) — see
//! `contract/README.md` for the shape and how to regenerate a golden after a
//! deliberate envelope change. This crate has no lib target, so this suite
//! spawns the compiled binary directly via `CARGO_BIN_EXE_tan` (cargo builds
//! it before running integration tests in the same package) rather than
//! calling command handlers in-process — that also exercises the exact
//! argv-parsing + stdout-framing path the extension actually shells out to.

use std::path::{Path, PathBuf};
use std::process::Command;

/// Root of the golden fixture tree.
fn fixtures_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contract/envelopes")
}

/// Recursively normalizes every JSON string leaf so a golden captured on one
/// OS matches a capture on any other: CI runs this suite on ubuntu/windows/
/// macos runners, and `PathBuf::to_string_lossy()` renders `./board.yaml` as
/// `.\board.yaml` on Windows. Goldens are authored with forward slashes; only
/// the freshly captured side needs normalizing before the diff.
fn normalize(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(s) => *s = s.replace('\\', "/"),
        serde_json::Value::Array(items) => items.iter_mut().for_each(normalize),
        serde_json::Value::Object(map) => map.values_mut().for_each(normalize),
        serde_json::Value::Null | serde_json::Value::Bool(_) | serde_json::Value::Number(_) => {}
    }
}

/// Copies every fixture input (currently just `board.yaml`, when a case
/// needs one) from `case_dir` into the isolated `work_dir` the subprocess
/// runs in — skips the harness's own case-metadata files.
fn copy_fixture_inputs(case_dir: &Path, work_dir: &Path) {
    for entry in std::fs::read_dir(case_dir).expect("read case dir") {
        let entry = entry.expect("dir entry");
        let name = entry.file_name();
        if matches!(
            name.to_string_lossy().as_ref(),
            "args.txt" | "expected.json" | "expected.exit"
        ) {
            continue;
        }
        if entry.file_type().expect("file type").is_file() {
            std::fs::copy(entry.path(), work_dir.join(&name)).expect("copy fixture input");
        }
    }
}

/// A fresh, empty scratch directory for one case run. Deliberately never
/// nested inside the repo checkout: `tan`'s sibling-SDK auto-discovery and
/// `init`'s create/update file diff both read the current directory's
/// contents, so running inside the checkout could pick up this repo's own
/// files and make the golden depend on where it happens to be checked out.
fn fresh_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("tan-contract-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// Runs one golden case end to end: spawn `tan` with the case's argv in an
/// isolated cwd and an isolated HOME/USERPROFILE (so a developer's real
/// `~/.alp/sdk-default` can never leak into `sdk current`'s answer), then
/// diff the normalized envelope and the exit code against the committed
/// golden.
fn run_case(case_name: &str) {
    let case_dir = fixtures_root().join(case_name);
    assert!(
        case_dir.is_dir(),
        "no fixture directory for case '{case_name}' at {}",
        case_dir.display()
    );

    let args_raw = std::fs::read_to_string(case_dir.join("args.txt"))
        .unwrap_or_else(|e| panic!("{case_name}: missing args.txt: {e}"));
    let args: Vec<&str> = args_raw
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();

    let expected_json = std::fs::read_to_string(case_dir.join("expected.json"))
        .unwrap_or_else(|e| panic!("{case_name}: missing expected.json: {e}"));
    let expected_exit: i32 = std::fs::read_to_string(case_dir.join("expected.exit"))
        .unwrap_or_else(|e| panic!("{case_name}: missing expected.exit: {e}"))
        .trim()
        .parse()
        .unwrap_or_else(|e| panic!("{case_name}: expected.exit is not an integer: {e}"));

    let work_dir = fresh_dir(case_name);
    let home_dir = fresh_dir(&format!("{case_name}-home"));
    copy_fixture_inputs(&case_dir, &work_dir);

    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(&args)
        .current_dir(&work_dir)
        // Deterministic capture: honored by `crate::util::generated_at_iso`
        // for any command that timestamps its output.
        .env("SOURCE_DATE_EPOCH", "0")
        // Isolate the SDK-pointer tiers (`~/.alp/sdk-default`) from whatever
        // the machine running this suite actually has configured.
        .env("HOME", &home_dir)
        .env("USERPROFILE", &home_dir)
        .output()
        .unwrap_or_else(|e| panic!("{case_name}: failed to spawn tan: {e}"));

    let _ = std::fs::remove_dir_all(&work_dir);
    let _ = std::fs::remove_dir_all(&home_dir);

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.trim().is_empty(),
        "{case_name}: unexpected stderr under --format json:\n{stderr}"
    );

    assert_eq!(
        output.status.code(),
        Some(expected_exit),
        "{case_name}: exit code mismatch"
    );

    let mut actual: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("{case_name}: stdout is not valid JSON: {e}\n---\n{stdout}"));
    normalize(&mut actual);
    let expected: serde_json::Value = serde_json::from_str(expected_json.trim())
        .unwrap_or_else(|e| panic!("{case_name}: expected.json is not valid JSON: {e}"));

    assert_eq!(
        actual, expected,
        "{case_name}: envelope drifted from the committed golden — if this is a \
         deliberate contract change, regenerate the fixture (see contract/README.md), \
         don't just fix the assertion"
    );
}

macro_rules! contract_case {
    ($fn_name:ident, $case:literal) => {
        #[test]
        fn $fn_name() {
            run_case($case);
        }
    };
}

contract_case!(init_preview_minimal_app, "init-preview-minimal-app");
contract_case!(init_invalid_template, "init-invalid-template");
contract_case!(validate_offline_clean, "validate-offline-clean");
contract_case!(
    validate_offline_schema_violation,
    "validate-offline-schema-violation"
);
contract_case!(sdk_current_no_sdk, "sdk-current-no-sdk");
contract_case!(sdk_unknown_subcommand, "sdk-unknown-subcommand");

/// Not a golden-diff case: `tan --version`'s first stdout line is its own
/// small contract (the vscode extension parses it to gate feature
/// availability by CLI version), but a literal golden would need editing on
/// every version bump. Pin the FORMAT (`tan MAJOR.MINOR.PATCH`) instead.
#[test]
fn version_first_line_matches_contract() {
    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .arg("--version")
        .output()
        .expect("failed to spawn tan --version");
    assert!(output.status.success(), "tan --version must exit 0");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let first_line = stdout.lines().next().unwrap_or_default();
    let mut parts = first_line.split_whitespace();

    assert_eq!(
        parts.next(),
        Some("tan"),
        "tan --version first token must be 'tan', got: {first_line:?}"
    );
    let version = parts.next().unwrap_or_default();
    let segments: Vec<&str> = version.split('.').collect();
    assert_eq!(
        segments.len(),
        3,
        "tan --version must read 'tan MAJOR.MINOR.PATCH', got: {first_line:?}"
    );
    assert!(
        segments
            .iter()
            .all(|seg| !seg.is_empty() && seg.chars().all(|c| c.is_ascii_digit())),
        "tan --version segments must be numeric, got: {first_line:?}"
    );
}
