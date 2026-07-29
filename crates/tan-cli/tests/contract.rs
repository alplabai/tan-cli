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

/// Root of `contract/` — the golden envelopes plus the frozen issue-code
/// registry (`issue-codes.json`) the release workflow publishes alongside them.
fn contract_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contract")
}

/// Root of the golden fixture tree.
fn fixtures_root() -> PathBuf {
    contract_root().join("envelopes")
}

/// Repo root, for reading the sources a frozen issue code is emitted from.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
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

/// Copies every fixture input from `case_dir` into the isolated `work_dir`
/// the subprocess runs in — skips the harness's own case-metadata files.
///
/// Directories are copied RECURSIVELY, which is what lets a case ship a
/// synthetic `sdk/` checkout (`scripts/alp_project.py` + `metadata/…` +
/// `examples/…`) and pass `--sdk-root ./sdk`. That relative argv keeps the
/// "no absolute paths in argv" rule intact: the reflected `data.sdkRoot`
/// comes out as the literal `./sdk` on every platform.
fn copy_fixture_inputs(case_dir: &Path, work_dir: &Path) {
    copy_tree(case_dir, work_dir, true);
}

/// `is_case_dir` scopes the case-metadata skip to the TOP level only: a
/// fixture `sdk/` subtree that happened to contain a file called `args.txt`
/// must be copied, not silently dropped.
fn copy_tree(case_dir: &Path, work_dir: &Path, is_case_dir: bool) {
    for entry in std::fs::read_dir(case_dir).expect("read case dir") {
        let entry = entry.expect("dir entry");
        let name = entry.file_name();
        if is_case_dir
            && matches!(
                name.to_string_lossy().as_ref(),
                "args.txt" | "expected.json" | "expected.exit"
            )
        {
            continue;
        }
        if entry.file_type().expect("file type").is_dir() {
            let nested = work_dir.join(&name);
            std::fs::create_dir_all(&nested).expect("create fixture subdir");
            copy_tree(&entry.path(), &nested, false);
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
// The three New Project wizard families (tan-cli#106). Every `data` field
// alp-sdk-vscode reads out of them is behind a `?? []`, so a rename degrades
// SILENTLY on the consumer side — these goldens are the only thing that turns
// one red.
//
// `presets-heterogeneous-som` is the issue's worked example made executable:
// its fixture SoM has an `a55` (yocto) and an `m33` (zephyr) core, so a rename
// of `data.soms` or `data.soms[].cores` fails here instead of quietly
// scaffolding a multi-core part as single-core with no IPC. The no-SDK case is
// its pair, pinning the `presets.sdk-root-unresolved` warning on the wire —
// the code the wizard needs to even KNOW it fell back.
contract_case!(presets_no_sdk, "presets-no-sdk");
contract_case!(presets_heterogeneous_som, "presets-heterogeneous-som");
// `data.available.projectTemplates` — the wizard's starter list.
contract_case!(explain_overview, "explain-overview");
// `data.examples[].sourceDir` — what `tan init --from-example <sourceDir>` is
// handed back, so a rename breaks scaffolding from an SDK example.
contract_case!(examples_catalog, "examples-catalog");
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
/// every version bump. Pin the FORMAT (`tan MAJOR.MINOR.PATCH[-PRERELEASE]`)
/// instead.
///
/// The optional pre-release suffix is part of the contract, not a loophole.
/// This test used to require all three segments to be strictly numeric, which
/// made `tan 0.4.0-rc1` fail `must be numeric` — so tan could not cut a release
/// candidate at all without reddening its own contract suite. That was a
/// producer-only restriction: the consumer had ALREADY been built for it, on
/// purpose. `alp-sdk-vscode/src/alpCli/service.ts` matches the line with
/// `/^tan \d+\.\d+\.\d+/` (no `$`, so a suffix is fine), `parseTanVersion`
/// deliberately KEEPS the suffix rather than dropping it, and `cliSkew`
/// implements SemVer §11 so `0.4.0-rc1` sorts strictly BELOW `0.4.0` — which is
/// what stops an rc from passing as the finished release. Found by cutting
/// v0.4.0-rc1.
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

    // Split the SemVer core from an optional pre-release. `split_once('-')` and
    // not `rsplit_once`: the pre-release itself may contain `-`
    // (`0.4.0-rc.1-hotfix` is legal SemVer), and the FIRST `-` is always the
    // separator because the core is digits and dots only.
    let (core, pre) = match version.split_once('-') {
        Some((core, pre)) => (core, Some(pre)),
        None => (version, None),
    };

    let segments: Vec<&str> = core.split('.').collect();
    assert_eq!(
        segments.len(),
        3,
        "tan --version must read 'tan MAJOR.MINOR.PATCH[-PRERELEASE]', got: {first_line:?}"
    );
    assert!(
        segments
            .iter()
            .all(|seg| !seg.is_empty() && seg.chars().all(|c| c.is_ascii_digit())),
        "tan --version MAJOR.MINOR.PATCH segments must be numeric, got: {first_line:?}"
    );

    // A pre-release is allowed, but only in the charset the consumer's regex
    // accepts (`(-[0-9A-Za-z.-]+)?`). Anything outside it parses to `null`
    // there, and a `null` version makes every skew check report "unknown" —
    // which every caller treats as "stay quiet". So a malformed suffix does not
    // warn the user, it silently disables the warnings, and only this assertion
    // would say so.
    if let Some(pre) = pre {
        assert!(
            !pre.is_empty()
                && pre
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-'),
            "tan --version pre-release must match the consumer's [0-9A-Za-z.-]+ \
             charset, got: {first_line:?}"
        );
    }
}

/// Strips whole-line `//` comments so a code-literal search cannot be
/// satisfied by prose ABOUT the code. Inline trailing comments and doc
/// comments both start a line here in practice; a literal inside a string in
/// a commented-out block would still count, which is the acceptable residue.
fn code_lines(source: &str) -> String {
    source
        .lines()
        .filter(|line| !line.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The frozen `issues[].code` strings alp-sdk-vscode matches with `===`
/// (tan-cli#106), gated against `contract/issue-codes.json`.
///
/// WHY a source-literal assertion and not a golden envelope: of the five
/// codes, only `presets.sdk-root-unresolved` is reachable from a hermetic,
/// host-independent subprocess (it has a golden — `presets-no-sdk` — and
/// that golden is the stronger gate). The `bootstrap.*` codes are not:
/// `yocto-host` fires only on a non-Linux host, so a golden would be inert on
/// the ubuntu CI leg; `prerequisites-missing` fires only when a required tool
/// is absent from PATH. A gate that cannot run on the platform CI runs it on
/// is exactly the inert-gate failure this exists to avoid.
///
/// WHAT THIS DOES NOT PROVE, stated plainly: that the code still REACHES the
/// wire. It proves the spelling still exists at the emission site (and, for
/// the retired code, that nothing has re-used it). A refactor that deletes
/// the whole refusal branch and leaves the string behind passes here.
/// `crates/tan-cli/src/commands/bootstrap/mod.rs`'s own unit tests cover the
/// emission; this covers the spelling.
#[test]
fn frozen_issue_codes() {
    let registry_path = contract_root().join("issue-codes.json");
    let registry: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(&registry_path).expect("read contract/issue-codes.json"),
    )
    .expect("contract/issue-codes.json is not valid JSON");

    let codes = registry["issueCodes"]
        .as_array()
        .expect("issue-codes.json: `issueCodes` must be an array");
    assert!(
        !codes.is_empty(),
        "issue-codes.json: the registry is empty — an empty registry would make \
         this gate pass while freezing nothing"
    );

    // Every `.rs` under `crates/`, read once: the retired-code check has to
    // search the whole tree, and the frozen ones index into the same map.
    let mut sources: Vec<(PathBuf, String)> = Vec::new();
    collect_rust_sources(&repo_root().join("crates"), &mut sources);
    assert!(
        sources.len() > 50,
        "expected to walk the whole crates/ tree, found only {} files — the walk \
         is broken, and a broken walk makes the retired-code check vacuous",
        sources.len()
    );

    for entry in codes {
        let code = entry["code"].as_str().expect("issueCodes[].code");
        let status = entry["status"].as_str().expect("issueCodes[].status");
        match status {
            "frozen" => {
                let rel = entry["emittedBy"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{code}: a frozen code needs `emittedBy`"));
                let literal = entry["literal"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{code}: a frozen code needs `literal`"));
                let path = repo_root().join(rel);
                let source = std::fs::read_to_string(&path)
                    .unwrap_or_else(|e| panic!("{code}: cannot read {rel}: {e}"));
                assert!(
                    code_lines(&source).contains(literal),
                    "FROZEN ISSUE CODE `{code}` is gone: {rel} no longer contains \
                     {literal:?} outside comments.\n\
                     alp-sdk-vscode matches this code with `===` and that match FAILS \
                     OPEN — the extension will not error, log or warn, it will silently \
                     skip the check. If this rename is deliberate: bump the CLI \
                     MAJOR/MINOR, update contract/issue-codes.json + CHANGELOG.md, and \
                     open the matching alp-sdk-vscode issue. Do NOT loosen the consumer \
                     to a prefix match."
                );
            }
            "reserved" => {
                // Pre-consumer: the spelling exists at the emission site (kept
                // honest against source, same as `frozen`) but nothing matches
                // it with `===` yet, so unlike `frozen` a rename here costs
                // nothing on the wire -- enforced by requiring `consumer`
                // stays "none" while the status does.
                let consumer = entry["consumer"].as_str().unwrap_or_default();
                assert!(
                    consumer == "none" || consumer.starts_with("none "),
                    "{code}: status `reserved` requires `consumer: \"none\"` -- a code \
                     with a real consumer must be `frozen` instead, or it can be \
                     silently renamed out from under that consumer"
                );
                let rel = entry["emittedBy"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{code}: a reserved code needs `emittedBy`"));
                let literal = entry["literal"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{code}: a reserved code needs `literal`"));
                let path = repo_root().join(rel);
                let source = std::fs::read_to_string(&path)
                    .unwrap_or_else(|e| panic!("{code}: cannot read {rel}: {e}"));
                assert!(
                    code_lines(&source).contains(literal),
                    "RESERVED ISSUE CODE `{code}` is gone: {rel} no longer contains \
                     {literal:?} outside comments. No consumer matches it yet, so \
                     dropping or renaming it is not a breaking wire change -- but the \
                     registry entry is now stale. Update contract/issue-codes.json to \
                     match, or restore the emission."
                );
            }
            "retired" => {
                // A retired code is not emitted any more, but the consumer branch
                // that matches it is permanent back-compat for old pinned binaries.
                // The invariant left to enforce is that nothing RE-USES the
                // spelling for a different verdict.
                let suffix = code.rsplit('.').next().unwrap_or(code);
                let needle = format!("\"{suffix}\"");
                for (path, source) in &sources {
                    assert!(
                        !code_lines(source).contains(&needle),
                        "RETIRED ISSUE CODE `{code}` was re-introduced in {}. \
                         alp-sdk-vscode still maps this exact string to \"this tan is \
                         too old to bootstrap on Windows\" for anyone pinned to a \
                         v0.3.0-or-earlier binary; re-using the spelling for anything \
                         else would show that message for an unrelated verdict. Pick a \
                         different code.",
                        path.display()
                    );
                }
            }
            other => panic!("{code}: unknown status {other:?} (expected frozen|reserved|retired)"),
        }
    }
}

/// Recursively collects `(path, contents)` for every `.rs` file under `dir`,
/// skipping `target/`.
fn collect_rust_sources(dir: &Path, out: &mut Vec<(PathBuf, String)>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if path.file_name().is_some_and(|n| n == "target") {
                continue;
            }
            collect_rust_sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            // Deliberately NOT a `&& let` chain: let-chains stabilised in Rust
            // 1.88 and this workspace pins `rust-version = "1.86"`, so the msrv
            // job rejects them with E0658 even though a current toolchain
            // compiles them without complaint. Local `cargo test` cannot catch
            // that -- only the msrv leg can.
            if let Ok(text) = std::fs::read_to_string(&path) {
                out.push((path, text));
            }
        }
    }
}

/// `tan doctor --build`'s `data` KEY SET, which alp-sdk-vscode's toolchain
/// panel reads with silently-degrading fallbacks (`src/toolchain.ts`).
///
/// A key-set assertion rather than a golden envelope because doctor's VALUES
/// are host facts — which tools are on PATH, whether a Zephyr workspace
/// exists, the registry's long-path setting. The key names are not
/// host-dependent, and they are the whole contract on this side: rename
/// `nextSteps`, `summary.fail`, `checks[].status`, or the `workspace` check
/// and the panel falls back to its own in-process probes with no error.
///
/// `--build` specifically: that is the invocation `buildToolchainReportViaCli`
/// makes, and it is the report that carries the `workspace` check. Plain
/// `tan doctor` emits a DIFFERENT check vocabulary (`workspaceRoot`, `lldb`,
/// …) which no consumer matches by name and which is deliberately NOT frozen
/// here.
#[test]
fn doctor_build_data_keys_the_extension_reads() {
    let work_dir = fresh_dir("doctor-build-keys");
    let home_dir = fresh_dir("doctor-build-keys-home");

    let output = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(["doctor", "--build", "--format", "json"])
        .current_dir(&work_dir)
        .env("SOURCE_DATE_EPOCH", "0")
        .env("HOME", &home_dir)
        .env("USERPROFILE", &home_dir)
        .output()
        .expect("failed to spawn tan doctor --build");

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(home_dir.parent().unwrap_or(&home_dir));

    let stdout = String::from_utf8_lossy(&output.stdout);
    let envelope: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("doctor --build stdout is not JSON: {e}\n---\n{stdout}"));
    let data = &envelope["data"];

    // `data.summary.{pass,warn,fail}` — `missingRequired` is computed from
    // `summary.fail`; a missing key reads as "nothing failed".
    for key in ["pass", "warn", "fail"] {
        assert!(
            data["summary"][key].is_number(),
            "doctor --build: data.summary.{key} must be a number (toolchain.ts \
             counts missingRequired off it) — got {}\n---\n{stdout}",
            data["summary"][key]
        );
    }

    // `data.nextSteps` — rendered verbatim as the panel's fix list.
    assert!(
        data["nextSteps"].is_array(),
        "doctor --build: data.nextSteps must be an array\n---\n{stdout}"
    );

    // `data.checks[].name` / `.status` — `status` is indexed into a
    // pass|warn|fail lookup table on the consumer side, so a fourth value or a
    // renamed field silently yields `undefined` for the whole row.
    let checks = data["checks"]
        .as_array()
        .unwrap_or_else(|| panic!("doctor --build: data.checks must be an array\n---\n{stdout}"));
    assert!(
        !checks.is_empty(),
        "doctor --build: data.checks is empty, so the per-check assertions below \
         would be vacuous\n---\n{stdout}"
    );
    for check in checks {
        assert!(
            check["name"].is_string(),
            "doctor --build: every data.checks[] entry needs a string `name` — got \
             {check}"
        );
        assert!(
            matches!(check["status"].as_str(), Some("pass" | "warn" | "fail")),
            "doctor --build: data.checks[].status must be pass|warn|fail (toolchain.ts \
             indexes CLI_STATUS with it) — got {check}"
        );
    }

    // The one check name matched as a LITERAL on the consumer side
    // (`c.name === "workspace"`, toolchain.ts:249 and :284): it gates the
    // "Bootstrap the Zephyr workspace" fix offer, so a rename removes the
    // offer with no error anywhere.
    assert!(
        checks
            .iter()
            .any(|c| c["name"].as_str() == Some("workspace")),
        "doctor --build: no check named \"workspace\". alp-sdk-vscode matches that \
         exact name to decide whether `--fix` can bootstrap a Zephyr workspace; \
         renamed, the offer silently disappears.\n---\n{stdout}"
    );
}

/// tan-cli#112: `--build` and `--build --fix` must stay ACCEPTED CLI
/// arguments, permanently -- both alp-sdk-vscode call sites
/// (`toolchain.ts`'s Toolchain Doctor panel and its "Bootstrap now" fix)
/// hardcode literal `["doctor", "--build"]` / `["doctor", "--build", "--fix"]`
/// argv with no fallback if the flag stops parsing.
///
/// The FAILING case this guards: if `--build` is removed from `DoctorArgs` (or
/// renamed), clap refuses the argv before `doctor::run` ever executes, and
/// `tan` itself catches that as a `cli.parse-error` issue with `command:
/// "cli"` (verified: `tan doctor --unknown-flag` on this build answers
/// `{"command":"cli","exitCode":2,...}`, not a doctor envelope at all). So
/// `command == "doctor"` is the one assertion that can only pass if the flag
/// was accepted and dispatch actually reached the doctor handler -- unlike an
/// exit-code check, it does not depend on which build tools happen to be on
/// this host (a never-bootstrapped project legitimately answers exitCode 4,
/// not 0, on every host tested).
#[test]
fn doctor_build_and_build_fix_stay_accepted_cli_arguments() {
    let work_dir = fresh_dir("doctor-build-shim");
    let home_dir = fresh_dir("doctor-build-shim-home");

    for argv in [
        vec!["doctor", "--build", "--format", "json"],
        vec!["doctor", "--build", "--fix", "--format", "json"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_tan"))
            .args(&argv)
            .current_dir(&work_dir)
            .env("SOURCE_DATE_EPOCH", "0")
            .env("HOME", &home_dir)
            .env("USERPROFILE", &home_dir)
            .output()
            .unwrap_or_else(|e| panic!("failed to spawn tan {}: {e}", argv.join(" ")));

        let stdout = String::from_utf8_lossy(&output.stdout);
        let envelope: serde_json::Value = serde_json::from_str(stdout.trim()).unwrap_or_else(|e| {
            panic!(
                "tan {}: stdout is not JSON -- the flag was likely rejected before \
                 reaching doctor::run: {e}\n---stdout---\n{stdout}\n---stderr---\n{}",
                argv.join(" "),
                String::from_utf8_lossy(&output.stderr)
            )
        });
        assert_eq!(
            envelope["command"],
            "doctor",
            "tan {}: envelope.command must be \"doctor\" (a rejected/unknown \
             \"--build\" answers \"cli\" with a cli.parse-error issue instead) \
             ---\n{stdout}",
            argv.join(" ")
        );
        assert_ne!(
            envelope["exitCode"].as_i64(),
            Some(2),
            "tan {}: exitCode 2 is the parse-error/usage-error code (see exit.rs); \
             doctor's own paths never return it, so seeing it here means the \
             argv was refused before dispatch ---\n{stdout}",
            argv.join(" ")
        );
    }

    let _ = std::fs::remove_dir_all(work_dir.parent().unwrap_or(&work_dir));
    let _ = std::fs::remove_dir_all(home_dir.parent().unwrap_or(&home_dir));
}
