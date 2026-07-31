// SPDX-License-Identifier: Apache-2.0
//! Issue #217: `tan bootstrap` reported every path it derived from
//! `--sdk-root` in whatever spelling the flag came in with, so the README
//! Quickstart's own `--sdk-root ./alp-sdk` produced `data.workspaceDir: "."`,
//! `data.venvDir: "./.venv"` and an `export ZEPHYR_BASE="./zephyr"` line under
//! a heading that reads "Add to your shell profile" -- a profile is sourced
//! from `$HOME`, where none of those resolve. `project.root` in the SAME
//! envelope was already absolute, so one run described one directory two ways
//! and a consumer had no way to tell which fields it still had to resolve.
//!
//! Subprocess tests, not unit tests -- `bootstrap::run` reads the real process
//! cwd, which is exactly the input under test here, and a fresh process per
//! case also keeps `sdk_report`'s thread-local out of it (same reasoning as
//! `envelope_sdk_report.rs`).
//!
//! Every case drives `--print-env`, which short-circuits before the
//! prerequisite gate and before any venv/west/pip phase: it resolves the SDK,
//! loads the workspace facts, reports, and touches nothing on disk. That is
//! the whole surface #217 is about, and it makes these hermetic -- no python,
//! no network, no toolchain.

use std::path::{Path, PathBuf};
use std::process::Command;

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` by the CLI's auto-discovery -- same reasoning as
/// `envelope_sdk_report.rs`'s `fresh_dir`.
fn fresh_workspace(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-abs217-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let workspace = parent.join("workspace");
    std::fs::create_dir_all(&workspace).expect("create workspace dir");
    workspace
}

/// `scripts/alp_project.py` is the only marker SDK resolution probes, so an
/// empty file is a complete stand-in for a checkout here. No
/// `metadata/bootstrap.json` -- bootstrap falls back to its documented
/// constants, a supported path that changes nothing this file asserts.
fn make_sdk_root(dir: &Path) {
    std::fs::create_dir_all(dir.join("scripts")).expect("create scripts dir");
    std::fs::write(dir.join("scripts").join("alp_project.py"), "").expect("write loader marker");
}

/// Run `tan bootstrap --sdk-root <flag> --print-env` in `cwd`, returning the
/// parsed JSON envelope and the plain-text `--print-env` block from two runs
/// of the same command: JSON mode reports the envelope, text mode prints the
/// block a customer is told to paste, and #217 is about both.
///
/// Both come off **stdout**. Text output in tan goes to stderr so `--format
/// json` owns stdout outright, and `--print-env` is the single documented
/// exception (tan-cli#227): it is not a report ABOUT a run, it is a shell
/// fragment meant to be captured, and on stderr both `eval "$(...)"` and
/// `> env.sh` silently produced nothing. This helper therefore doubles as
/// #227's regression test -- see the empty-stderr assertion.
fn run_print_env(cwd: &Path, home: &Path, sdk_root_flag: &str) -> (serde_json::Value, String) {
    let invoke = |json: bool| {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_tan"));
        if json {
            cmd.args(["--format", "json"]);
        }
        cmd.args(["bootstrap", "--sdk-root", sdk_root_flag, "--print-env"])
            .current_dir(cwd)
            // Isolate the machine-global `~/.alp/sdk-default` pointer: a
            // developer's real one would otherwise answer instead of the flag.
            .env("HOME", home)
            .env("USERPROFILE", home)
            // A real ZEPHYR_BASE in the developer's environment sends bootstrap
            // down the workspace-adoption branch, repointing workspaceDir at a
            // tree this test never created.
            .env_remove("ZEPHYR_BASE");
        let out = cmd.output().expect("failed to spawn tan");
        assert!(
            out.status.success(),
            "tan bootstrap --print-env failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        // tan-cli#227: nothing on stderr in EITHER mode. A `--print-env` run
        // installs nothing and reports nothing, so a byte here means the block
        // is leaking back onto the stream where a pipe cannot see it.
        assert!(
            out.stderr.is_empty(),
            "tan bootstrap --print-env wrote {} byte(s) to stderr -- #227: this block must be \
             on stdout, where `eval \"$(...)\"` and `> env.sh` can capture it. stderr was:\n{}",
            out.stderr.len(),
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).expect("stdout is utf-8")
    };

    let envelope = serde_json::from_str(&invoke(true)).expect("stdout is a JSON envelope");
    (envelope, invoke(false))
}

/// The four `data` paths and the envelope's own `sdk.root` all come back
/// absolute when the flag was relative -- and `sdk.root` agrees with
/// `data.sdkRoot`, which is the half that made the old envelope
/// self-contradictory (a relative `sdk.root` beside an absolute
/// `project.root`).
#[test]
fn a_relative_sdk_root_flag_still_reports_absolute_paths() {
    let workspace = fresh_workspace("relflag");
    make_sdk_root(&workspace.join("alp-sdk"));
    let home = workspace.join("home");
    std::fs::create_dir_all(&home).expect("create home");

    let (envelope, text) = run_print_env(&workspace, &home, "./alp-sdk");

    let data = &envelope["data"];
    for key in ["sdkRoot", "workspaceDir", "venvDir", "zephyrBase"] {
        let value = data[key].as_str().unwrap_or_else(|| panic!("data.{key}"));
        assert!(
            Path::new(value).is_absolute(),
            "data.{key} is {value:?} -- #217: derived from a relative --sdk-root and reported \
             relative, so a consumer reading it from any other directory resolves nothing"
        );
    }

    // Compared with separators normalized, NOT byte-for-byte: the two fields
    // deliberately differ in separator style on Windows, and an equality
    // assertion here fails there for a reason that has nothing to do with
    // #217 (it did, on this test's first CI run).
    //
    // - `sdk.root` is posix-normalized by `sdk_report`, which exists so the
    //   emitted value is platform-identical whichever resolver recorded it --
    //   the same rule `tan_core::project::to_posix` enforces for
    //   `project.root`.
    // - `data.sdkRoot` goes through `native()` with its three siblings,
    //   because a consumer comparing `sdkRoot` against `workspaceDir` by
    //   prefix or dirname needs one separator among THOSE four.
    //
    // What #217 is about is that they name the same directory, which is what
    // this checks. Separator style is a separate, already-settled decision.
    let posix = |s: &str| s.replace('\\', "/");
    assert_eq!(
        envelope["sdk"]["root"].as_str().map(posix),
        data["sdkRoot"].as_str().map(posix),
        "sdk.root and data.sdkRoot must name the same checkout"
    );

    // The block a customer is told to paste into a shell profile. A profile is
    // sourced from $HOME (or dot-sourced from a PowerShell profile), so a
    // relative value in it is silently wrong.
    //
    // BOTH spellings, because `render_env_lines` emits shell on POSIX and
    // PowerShell on Windows -- matching only `export ` made this loop iterate
    // zero times on windows-latest, which is what the vacuity guard below
    // caught on the first CI run:
    //   POSIX:   export ZEPHYR_BASE="/abs/workspace/zephyr"
    //   Windows: $env:ZEPHYR_BASE = "C:\abs\workspace\zephyr"
    let mut checked_an_export = false;
    for line in text.lines() {
        let Some(rest) = line
            .strip_prefix("export ZEPHYR_BASE=")
            .or_else(|| line.strip_prefix("$env:ZEPHYR_BASE = "))
        else {
            continue;
        };
        checked_an_export = true;
        let value = rest.trim_matches('"');
        assert!(
            Path::new(value).is_absolute(),
            "--print-env emitted {line:?} -- relative, so pasting it into a shell profile \
             exports a path that does not exist from the profile's own directory"
        );
    }
    assert!(
        checked_an_export,
        "--print-env printed no ZEPHYR_BASE line in either the POSIX or the PowerShell \
         spelling, so the assertion above checked nothing -- a silently vacuous test is worse \
         than a missing one. Block was:\n{text}"
    );
}

/// An already-absolute flag is reported back UNCHANGED -- the fix must not
/// canonicalize, or a customer whose checkout is reached through a symlink
/// (`/tmp/...` on macOS, resolving to `/private/tmp/...`) is handed a path
/// they never typed.
#[test]
fn an_absolute_sdk_root_flag_is_reported_verbatim() {
    let workspace = fresh_workspace("absflag");
    let sdk = workspace.join("alp-sdk");
    make_sdk_root(&sdk);
    let home = workspace.join("home");
    std::fs::create_dir_all(&home).expect("create home");

    let (envelope, _) = run_print_env(&workspace, &home, &sdk.to_string_lossy());

    assert_eq!(
        envelope["data"]["sdkRoot"].as_str(),
        Some(sdk.to_string_lossy().as_ref()),
        "an absolute --sdk-root must survive verbatim -- no canonicalization, no symlink rewrite"
    );
}
