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

/// Envelope field names that are path-shaped (own a `PathBuf::to_string_lossy()`
/// value somewhere in the CLI) and so need `\`→`/` normalization. Scoped
/// on purpose: a blanket rewrite over every string leaf would also silently
/// launder a real drift in `issues[].message` or a `data` value that merely
/// happens to contain a backslash — the one normalization that could mask
/// the exact kind of change this gate exists to catch.
const PATH_KEYS: &[&str] = &[
    "root",
    "boardYaml",
    "boardYamlPath",
    "destination",
    "relativePath",
    "sdkPath",
    "sdkPinned",
    "written",
    "unchanged",
    "launchJsonPath",
];

/// The placeholder a golden spells the case's own scratch directory as.
const WORK_DIR_TOKEN: &str = "__WORKDIR__";

/// Recursively normalizes `\`→`/` on path-shaped fields only (see
/// `PATH_KEYS`) so a golden captured on one OS matches a capture on any
/// other: CI runs this suite on ubuntu/windows/macos runners, and
/// `PathBuf::to_string_lossy()` renders `./board.yaml` as `.\board.yaml` on
/// Windows. `key` is the enclosing object field name (`None` at the root);
/// an array inherits its own key so e.g. every string in `written: [...]`
/// is still recognized. Goldens are authored with forward slashes; only the
/// freshly captured side needs normalizing before the diff.
fn normalize(value: &mut serde_json::Value, key: Option<&str>, work_dir_marker: &str) {
    match value {
        serde_json::Value::String(s) => {
            if key.is_some_and(|k| PATH_KEYS.contains(&k)) {
                *s = s.replace('\\', "/");
                // Some commands reflect the ABSOLUTE working directory back
                // (`debug-config` reports `project.root` and the launch.json
                // path it would write). That value is machine-specific AND
                // pid-specific, so it can only be pinned as a token. Anchor on
                // the case's own unique scratch-dir marker rather than on the
                // harness's `work_dir` string: on macOS `$TMPDIR` is a symlink
                // (`/var/…`) that `std::env::current_dir()` resolves through
                // (`/private/var/…`), so a whole-prefix comparison would miss
                // there and only there.
                if let Some(at) = s.find(work_dir_marker) {
                    *s = format!("{WORK_DIR_TOKEN}{}", &s[at + work_dir_marker.len()..]);
                }
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                normalize(item, key, work_dir_marker);
            }
        }
        serde_json::Value::Object(map) => {
            for (k, v) in map.iter_mut() {
                normalize(v, Some(k.as_str()), work_dir_marker);
            }
        }
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

/// A fresh, empty scratch directory for one case run, nested under its own
/// fresh, uniquely named PARENT — never directly under the shared system
/// temp root. Two reasons:
///   - Deliberately never nested inside the repo checkout: `tan`'s
///     sibling-SDK auto-discovery and `init`'s create/update file diff both
///     read the current directory's contents, so running inside the
///     checkout could pick up this repo's own files.
///   - `discover_workspace_sdk` (tan-core `project.rs`) also probes the
///     work-dir's PARENT for a sibling `alp-sdk/`. If the parent were the
///     shared `$TEMP` root, a stray `alp-sdk` checkout dropped there by
///     something else would flip a golden's `sourceTier`. The extra
///     `.../tan-contract-<tag>-<pid>/root` nesting level gives every case
///     its own empty parent that nothing else can plausibly populate.
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-contract-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
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

    // Remove each dir's fresh PARENT (see `fresh_dir`), not just the `root`
    // leaf, so no empty scratch directory is left behind.
    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(home_dir.parent().unwrap_or(&home_dir));

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
    // Mirrors `fresh_dir`'s layout: `<temp>/tan-contract-<case>-<pid>/root`.
    let work_dir_marker = format!("tan-contract-{case_name}-{}/root", std::process::id());
    normalize(&mut actual, None, &work_dir_marker);
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
contract_case!(generate_board_yaml_missing, "generate-board-yaml-missing");
// One profile per `--target-kind`. The four `configuration` objects ARE the
// product of this command — alp-sdk-vscode#342 writes them into launch.json
// verbatim — so the golden pins the emitted KEY SET, not just the envelope
// wrapper: an added key (a `preLaunchTask` naming a task nothing provides) or
// a changed `program`/`executable` fails here instead of reaching a consumer.
contract_case!(
    debug_config_preview_zephyr_mcu,
    "debug-config-preview-zephyr-mcu"
);
contract_case!(
    debug_config_preview_baremetal_mcu,
    "debug-config-preview-baremetal-mcu"
);
contract_case!(
    debug_config_preview_yocto_userspace,
    "debug-config-preview-yocto-userspace"
);
contract_case!(
    debug_config_preview_native_host,
    "debug-config-preview-native-host"
);

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
