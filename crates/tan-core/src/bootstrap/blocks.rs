// SPDX-License-Identifier: Apache-2.0
//! The text blocks `tan bootstrap` prints, one renderer per block.
//!
//! Each reproduces the corresponding section of the parity oracles — the
//! "Print-env shortcut", "Optional native libs hint" / "Manual-install hints"
//! and "Done" sections of `bootstrap.sh` and `bootstrap.ps1`. Since alp-sdk#917
//! those sections render from `metadata/bootstrap.json` rather than from
//! hardcoded text, so these take [`BootstrapFacts`] too: the env map, the venv
//! directory names and the native-lib hints are all manifest facts now, and
//! only the surrounding prose is literal here.
//!
//! They are copy-pasteable shell snippets, so they carry NO `bootstrap:` prefix
//! (unlike the progress lines) and their whitespace is load-bearing. Lines are
//! returned individually because `CommandRun::text` is printed one line at a
//! time.

use super::manifest::{BootstrapFacts, Tokens};
use super::runtime::HostOs;

/// Render the manifest's `env` map as shell-ready lines.
///
/// POSIX (`bootstrap.sh`'s `print_env_lines`) quotes the value only when it
/// looks like a path — i.e. contains `/` — which is what keeps
/// `export ZEPHYR_TOOLCHAIN_VARIANT=zephyr` unquoted while `ZEPHYR_BASE` is
/// quoted. Windows (`bootstrap.ps1`'s `Write-EnvLines`) always quotes.
///
/// Substitution happens HERE, not at load time, so a `workspace_dir` repointed
/// by workspace selection is reflected — see [`Tokens`].
///
/// One deliberate divergence from `bootstrap.ps1`: a token-substituted value is
/// separator-normalised for the host, so Windows emits `C:\dev\ws\zephyr`
/// rather than the script's mixed `C:\dev\ws/zephyr` (it interpolates the
/// manifest's forward-slash-joined value straight into a backslash path). Both
/// work as `ZEPHYR_BASE`; only one is copy-pasteable without a double-take.
/// A value with no token in it is passed through untouched.
pub fn render_env_lines(
    env: &[(String, String)],
    tokens: Tokens,
    prefix: &str,
    is_windows: bool,
) -> Vec<String> {
    env.iter()
        .map(|(key, raw)| {
            let mut value = tokens.apply(raw);
            let substituted = value != *raw;
            if is_windows {
                if substituted {
                    value = value.replace('/', "\\");
                }
                format!("{prefix}$env:{key} = \"{value}\"")
            } else if value.contains('/') {
                format!("{prefix}export {key}=\"{value}\"")
            } else {
                format!("{prefix}export {key}={value}")
            }
        })
        .collect()
}

/// `--print-env`: the venv-activation comment header plus the rendered `env`
/// map. Both scripts print exactly this and exit 0.
pub fn print_env_block(
    facts: &BootstrapFacts,
    tokens: Tokens,
    venv_bin_dir: &str,
    is_windows: bool,
) -> Vec<String> {
    let venv = &facts.venv_dir_name;
    let mut lines = if is_windows {
        // `bootstrap.ps1` interpolates `$WorkspaceDir`, a native backslash path
        // from `Resolve-Path`. Ours is the `ProjectContext` string, which is
        // forward-slash on every OS -- normalise it, or this line comes out
        // mixed (`C:/Users/dev\.venv\Scripts\Activate.ps1`), the exact thing
        // `render_env_lines` normalises a substituted value to avoid.
        let workspace = tokens.workspace_dir.replace('/', "\\");
        vec![
            "# Add to your PowerShell profile (or run before invoking the SDK):".to_string(),
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):".to_string(),
            format!("#   & \"{workspace}\\{venv}\\{venv_bin_dir}\\Activate.ps1\""),
        ]
    } else {
        let workspace = tokens.workspace_dir;
        vec![
            "# Add to your shell profile (or run before invoking the SDK):".to_string(),
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):".to_string(),
            format!("#   source \"{workspace}/{venv}/{venv_bin_dir}/activate\""),
        ]
    };
    lines.extend(render_env_lines(&facts.env, tokens, "", is_windows));
    lines
}

/// The trailing manual-install hint. POSIX prints the manifest's per-OS
/// optional-native-libs note (plus its install command, when the OS has one);
/// native Windows prints the script's literal Arm/Zephyr-SDK block followed by
/// the manifest's `manualInstallHints.windows.note` — the SDK-sourced fact,
/// matching `bootstrap.ps1`'s own `foreach ($line in $ManualInstallNote)`.
///
/// Issue #82 asked to delete the hardcoded block below as a duplicate of that
/// manifest note, on the strength of alp-sdk#961 (merged) deleting
/// `bootstrap.ps1`'s own copy the same way. Reverted: #961 didn't just delete
/// its half, it moved the Arm-toolchain URL and the "tick 'Add path to
/// environment variable'" tip INTO `manualInstallHints.windows.note` in the
/// SAME commit -- but `contract/fixtures/bootstrap/manifest.json` (and every
/// alp-sdk tag released so far, and the parity gate's `PINNED_SDK_TAG`, which
/// is past #917 but not #961) still carries the PRE-#961 note: one terse
/// sentence with neither fact. Deleting the hardcoded block against that
/// fixture doesn't remove a duplicate, it drops a required install's URL and
/// PATH tip for every SDK ref actually in use today. Re-delete only once tan
/// re-vendors the fixture (and bumps `PINNED_SDK_TAG`) to a ref at or after
/// #961 -- and re-check the wording then: #961's own PR body says the
/// `$WorkspaceDir` locator "survives as prose" in the manifest note rather
/// than a token, so a same-day dedup could leave the `west sdk install` line
/// below duplicating THAT sentence instead.
///
/// This must NOT read `nativeLibHints.windows.note` here: appending both used
/// to print the Arm/Zephyr-SDK sentence twice — once hardcoded, once from a
/// manifest note field that (pre alp-sdk#917 review) still carried the same
/// sentence. That field (the "Under Git Bash / MSYS2..." hint
/// `bootstrap.sh`'s `windows-bash)` arm prints) is parsed into
/// `BootstrapFacts::hint_windows` for round-trip fidelity but rendered by
/// NOTHING in this crate: `HostOs::detect` reads the compile-time
/// `std::env::consts::OS`, so a Windows-built `tan.exe` always takes THIS
/// branch, git-bash or not — the `else` branch below never runs on Windows.
/// `bootstrap.ps1` itself has no heading for this hint either, so the gap
/// matches the ps1 oracle tan mirrors on native Windows; there is no
/// git-bash detection here to make the `bootstrap.sh` arm reachable.
pub fn optional_libs_block(
    facts: &BootstrapFacts,
    host: HostOs,
    workspace_dir: &str,
) -> Vec<String> {
    if host == HostOs::Windows {
        let mut lines = vec![
            String::new(),
            "bootstrap: NOT auto-installed (manual, one-time):".to_string(),
            String::new(),
            "  # Arm GNU Toolchain (cross-compiles for real silicon) -- installer EXE:".to_string(),
            "  #   https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads".to_string(),
            "  #   (tick 'Add path to environment variable' during install)".to_string(),
            String::new(),
            "  # Zephyr SDK (alternative cross-toolchain + host tools like dtc):".to_string(),
            // Deliberate divergence: `bootstrap.ps1` SAID "after this script."
            // Tan is not a script, so the same sentence would misname what the
            // reader just ran; "step" is the only word changed. Past tense on
            // purpose — alp-sdk#961 deleted that here-string, so the oracle
            // this diverges from no longer exists upstream; the divergence is
            // kept because the fixture tan still reads predates #961 (see the
            // doc comment above), not because the ps1 still says it.
            format!("  #   run 'west sdk install' from {workspace_dir} after this step."),
            String::new(),
        ];
        // One line per `manualInstallHints.windows.note` element, two-space
        // indented, exactly like `bootstrap.ps1`'s
        // `foreach ($line in $ManualInstallNote)` — the manifest-sourced
        // fact, not a hand-typed Rust copy that would desync silently. See
        // the doc comment above for why the hardcoded block above stays too,
        // for now.
        lines.extend(
            facts
                .manual_install_hints
                .windows
                .note
                .iter()
                .map(|line| format!("  {line}")),
        );
        return lines;
    }

    let hint = facts.native_lib_hint(host);
    let mut lines = vec![
        String::new(),
        "bootstrap: Optional native libraries unlock the Yocto-side backends:".to_string(),
    ];
    match hint {
        Some(hint) => {
            // `bootstrap.sh`'s per-OS arm: a blank line, then
            // `for line in "${HINT_<OS>_NOTE[@]}"; do echo "  ${line}"; done`.
            lines.push(String::new());
            lines.extend(hint.note.iter().map(|line| format!("  {line}")));
            if let Some(command) = hint.command.as_deref().filter(|c| !c.is_empty()) {
                lines.push(String::new());
                lines.push(format!("  {command}"));
            }
        }
        None => lines.push("  (OS not auto-detected; see docs/testing.md)".to_string()),
    }
    lines
}

/// The closing "Next steps:" block: activate the venv, export the manifest's
/// `env` map, run `tan doctor`, and one ready-to-paste `west build` for the
/// platform's canonical example target.
pub fn next_steps_block(
    facts: &BootstrapFacts,
    tokens: Tokens,
    venv_dir: &str,
    venv_bin_dir: &str,
    is_windows: bool,
) -> Vec<String> {
    let mut lines = vec![String::new(), "Next steps:".to_string()];
    if is_windows {
        lines.push(
            "  # Activate the workspace venv (west + Zephyr/SDK deps + tan's Python backend):"
                .to_string(),
        );
        lines.push(format!("  & \"{venv_dir}\\{venv_bin_dir}\\Activate.ps1\""));
    } else {
        lines.push(
            "  # Activate the workspace venv (west + Zephyr/SDK deps live here):".to_string(),
        );
        lines.push(format!("  source \"{venv_dir}/{venv_bin_dir}/activate\""));
    }
    lines.push(String::new());
    lines.push("  # Make Zephyr reachable for builds:".to_string());
    lines.extend(render_env_lines(&facts.env, tokens, "  ", is_windows));
    lines.extend([
        String::new(),
        "  # Sanity-check the host environment (needs tan on PATH -- see README.md".to_string(),
        "  # for `cargo install --git https://github.com/alplabai/tan-cli --bin tan`):".to_string(),
        "  tan doctor".to_string(),
        String::new(),
    ]);
    if is_windows {
        // `bootstrap.ps1` interpolates `$RepoRoot` here -- a native backslash
        // path -- and the same line spells the example as `examples\...`, so a
        // raw forward-slash `${SDK_ROOT}` would print mixed. See `print_env_block`.
        let repo_root = tokens.sdk_root.replace('/', "\\");
        lines.extend([
            "  # Or jump straight into building an example for real silicon:".to_string(),
            "  west build -b alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he `".to_string(),
            format!(
                "      examples\\peripheral-io\\uart-echo -- -DEXTRA_ZEPHYR_MODULES={repo_root}"
            ),
            String::new(),
            "References:".to_string(),
            "  - docs\\cross-platform-setup.md  -- the full per-OS setup guide".to_string(),
            "  - docs\\cli.md                   -- the tan CLI verb reference".to_string(),
        ]);
    } else {
        lines.extend([
            "  # Run the local test suite:".to_string(),
            "  bash scripts/test-all.sh".to_string(),
            String::new(),
            "  # Or jump straight into building an example:".to_string(),
            "  west build -b native_sim/native/64 examples/peripheral-io/uart-echo \\".to_string(),
            "      -- -DEXTRA_ZEPHYR_MODULES=$PWD".to_string(),
            String::new(),
            "References:".to_string(),
            "  - docs/testing.md          -- full test-coverage map + how to run from scratch"
                .to_string(),
            "  - docs/test-plan.md        -- per-feature verification ledger (⏳ / 🟡 / ✅)"
                .to_string(),
        ]);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::super::manifest::{fallback_facts, parse_bootstrap_manifest};
    use super::*;

    const REAL_MANIFEST: &str =
        include_str!("../../../../contract/fixtures/bootstrap/manifest.json");

    fn manifest_facts() -> BootstrapFacts {
        parse_bootstrap_manifest(REAL_MANIFEST).expect("real manifest must parse")
    }

    /// Byte-parity against the WORKING-TREE `bootstrap.sh`'s "Print-env
    /// shortcut" section: three `printf` header lines then `print_env_lines`,
    /// which quotes a value only when it contains `/`.
    #[test]
    fn print_env_posix_matches_bootstrap_sh_verbatim() {
        let expected = "\
# Add to your shell profile (or run before invoking the SDK):
# Activate the workspace venv (west + Zephyr/SDK Python deps live here):
#   source \"/home/dev/work/.venv/bin/activate\"
export ZEPHYR_BASE=\"/home/dev/work/zephyr\"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr";
        let tokens = Tokens {
            sdk_root: "/home/dev/work/alp-sdk",
            workspace_dir: "/home/dev/work",
        };
        assert_eq!(
            print_env_block(&manifest_facts(), tokens, "bin", false).join("\n"),
            expected
        );
        // The fallback constants must render the same bytes as the manifest.
        assert_eq!(
            print_env_block(&fallback_facts((3, 10)), tokens, "bin", false).join("\n"),
            expected
        );
    }

    /// Byte-parity against the WORKING-TREE `bootstrap.ps1`'s "Print-env
    /// shortcut" section (`Write-EnvLines` always quotes), modulo the
    /// documented separator normalisation in [`render_env_lines`].
    ///
    /// The tokens are FORWARD-slash on purpose: that is what production hands
    /// in (the `ProjectContext` path), and the header line used to interpolate
    /// it raw, printing `C:/dev/work\.venv\Scripts\Activate.ps1`.
    #[test]
    fn print_env_windows_matches_bootstrap_ps1_verbatim() {
        let expected = "\
# Add to your PowerShell profile (or run before invoking the SDK):
# Activate the workspace venv (west + Zephyr/SDK Python deps live here):
#   & \"C:\\dev\\work\\.venv\\Scripts\\Activate.ps1\"
$env:ZEPHYR_BASE = \"C:\\dev\\work\\zephyr\"
$env:ZEPHYR_TOOLCHAIN_VARIANT = \"zephyr\"";
        let tokens = Tokens {
            sdk_root: "C:/dev/work/alp-sdk",
            workspace_dir: "C:/dev/work",
        };
        let lines = print_env_block(&manifest_facts(), tokens, "Scripts", true);
        assert_eq!(lines.join("\n"), expected);
        // No PATH line may carry a forward slash — a mixed-separator line is
        // the bug this guards. (The prose header's "Zephyr/SDK" is not a path.)
        for line in lines.iter().filter(|l| l.contains("C:")) {
            assert!(!line.contains('/'), "mixed separators: {line}");
        }
        assert_eq!(
            print_env_block(&fallback_facts((3, 10)), tokens, "Scripts", true).join("\n"),
            expected
        );
        // A backslash path in (what `bootstrap.ps1` itself has) is untouched.
        let native = Tokens {
            sdk_root: "C:\\dev\\work\\alp-sdk",
            workspace_dir: "C:\\dev\\work",
        };
        assert_eq!(
            print_env_block(&manifest_facts(), native, "Scripts", true).join("\n"),
            expected
        );
    }

    /// A manifest that renames the venv dir or adds an env var must reach the
    /// output without a tan release — that is the whole point of consuming it.
    #[test]
    fn a_changed_manifest_changes_the_rendered_env_block() {
        let doc = REAL_MANIFEST
            .replace("\"dirName\": \".venv\"", "\"dirName\": \".venv-4.5\"")
            .replace(
                "\"ZEPHYR_TOOLCHAIN_VARIANT\": \"zephyr\"",
                "\"ZEPHYR_TOOLCHAIN_VARIANT\": \"zephyr\", \"ZEPHYR_EXTRA\": \"${SDK_ROOT}/x\"",
            );
        let facts = parse_bootstrap_manifest(&doc).expect("edited manifest must parse");
        let tokens = Tokens {
            sdk_root: "/ws/alp-sdk",
            workspace_dir: "/ws",
        };
        let rendered = print_env_block(&facts, tokens, "bin", false).join("\n");
        assert!(
            rendered.contains("/ws/.venv-4.5/bin/activate"),
            "{rendered}"
        );
        assert!(
            rendered.contains("export ZEPHYR_EXTRA=\"/ws/alp-sdk/x\""),
            "{rendered}"
        );
    }

    /// Byte-parity against `bootstrap.sh`'s "Optional native libs hint"
    /// section for a MULTI-element note: the `info` line, a blank line, then
    /// one two-space-indented line per `nativeLibHints.linux.note` element
    /// (`for line in "${HINT_LINUX_NOTE[@]}"; do echo "  ${line}"; done`),
    /// then a blank line and the command. The `->` column stays aligned,
    /// which is the whole reason the field became an array.
    #[test]
    fn optional_libs_posix_renders_one_line_per_note_element() {
        // Leading newline, NOT a `"\` continuation: the block's first element
        // is the blank line `bootstrap.sh` echoes before the info line.
        let expected = "
bootstrap: Optional native libraries unlock the Yocto-side backends:

  libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)
  libasound2-dev    -> alp_audio_*
  libssl-dev        -> alp_hash_* / alp_aead_* / alp_random_bytes

  sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev pkg-config";
        assert_eq!(
            optional_libs_block(&manifest_facts(), HostOs::Linux, "/ws").join("\n"),
            expected
        );
        // The fallback constants must render the same bytes as the manifest.
        assert_eq!(
            optional_libs_block(&fallback_facts((3, 10)), HostOs::Linux, "/ws").join("\n"),
            expected
        );
    }

    /// Byte-parity against `bootstrap.ps1`'s `foreach ($line in
    /// $ManualInstallNote)`: the literal Arm/Zephyr-SDK block, then one
    /// indented line per `manualInstallHints.windows.note` element. See
    /// `optional_libs_block`'s doc comment for why the literal block still
    /// precedes the manifest note (issue #82 is not yet safe to finish).
    /// `nativeLibHints.windows.note` (the "Under Git Bash / MSYS2..." line)
    /// must NOT appear here — that is a different fact (bootstrap.ps1 has no
    /// heading for it at all); printing both was the alp-sdk#917-review bug
    /// this renders around.
    #[test]
    fn optional_libs_windows_renders_one_line_per_note_element() {
        let expected = "
bootstrap: NOT auto-installed (manual, one-time):

  # Arm GNU Toolchain (cross-compiles for real silicon) -- installer EXE:
  #   https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads
  #   (tick 'Add path to environment variable' during install)

  # Zephyr SDK (alternative cross-toolchain + host tools like dtc):
  #   run 'west sdk install' from C:\\ws after this step.

  The Arm GNU Toolchain and the Zephyr SDK (`west sdk install`) are separate manual, one-time \
installs on native Windows -- not auto-installed by bootstrap.ps1; native_sim / Yocto need WSL2 \
(docs/cross-platform-setup.md section 5).";
        assert_eq!(
            optional_libs_block(&manifest_facts(), HostOs::Windows, "C:\\ws").join("\n"),
            expected
        );
        assert_eq!(
            optional_libs_block(&fallback_facts((3, 10)), HostOs::Windows, "C:\\ws").join("\n"),
            expected
        );
    }

    /// The failure mode this whole fix guards against: a hand-typed Rust copy
    /// of the manual-install sentence would render the SAME text regardless of
    /// what the manifest says. Editing the manifest's
    /// `manualInstallHints.windows.note` MUST change the rendered line — proof
    /// the block reads the field rather than a literal that merely happens to
    /// match it today.
    ///
    /// Mutates the PARSED value and re-serializes, rather than `.replace()`ing
    /// the raw JSON text with a needle built from the parsed string: the note
    /// text and its JSON encoding are not always the same bytes (a quote,
    /// backslash or `\uXXXX` escape in a future note element would make a
    /// text-level needle match nothing), and a `.replace()` that then finds
    /// nothing leaves this test silently asserting nothing about the field it
    /// exists to check — worse than no test, per issue #82. Going through
    /// `serde_json::Value` sidesteps the whole escaping class: there is no
    /// "needle not found" case to guard against.
    #[test]
    fn optional_libs_windows_note_comes_from_the_manifest_not_a_rust_literal() {
        let mut doc: serde_json::Value = serde_json::from_str(REAL_MANIFEST).unwrap();
        let note = doc
            .pointer_mut("/manualInstallHints/windows/note/0")
            .expect("fixture must carry at least one manualInstallHints.windows.note element");
        let original_note = note.as_str().unwrap().to_string();
        *note = serde_json::Value::String(
            "EDITED sentence that only the manifest field carries.".to_string(),
        );
        let doc = serde_json::to_string(&doc).expect("re-serializing the edited fixture");
        let facts = parse_bootstrap_manifest(&doc).expect("edited manifest must parse");
        let rendered = optional_libs_block(&facts, HostOs::Windows, "C:\\ws").join("\n");
        assert!(
            rendered.contains("  EDITED sentence that only the manifest field carries."),
            "{rendered}"
        );
        assert!(
            !rendered.contains(&original_note),
            "the stale sentence must not survive the edit: {rendered}"
        );
    }

    /// A ONE-element note must still render exactly as a single indented line
    /// on both branches — the array shape adds lines, it does not add spacing.
    #[test]
    fn a_single_element_note_renders_as_one_indented_line() {
        let mut facts = manifest_facts();
        facts.hint_linux.note = vec!["one line only".to_string()];
        facts.hint_linux.command = None;
        assert_eq!(
            optional_libs_block(&facts, HostOs::Linux, "/ws"),
            vec![
                "".to_string(),
                "bootstrap: Optional native libraries unlock the Yocto-side backends:".to_string(),
                "".to_string(),
                "  one line only".to_string(),
            ]
        );
        facts.manual_install_hints.windows.note = vec!["one line only".to_string()];
        let win = optional_libs_block(&facts, HostOs::Windows, "C:\\ws");
        assert_eq!(win.last().unwrap(), "  one line only");
        // "here-string" was `bootstrap.ps1`'s construct, never tan's (this is a
        // Rust `vec![]`), and alp-sdk#961 deleted it upstream — name what this
        // assertion actually guards instead.
        assert_eq!(
            win[win.len() - 2],
            "",
            "the hardcoded block's trailing blank"
        );
    }

    #[test]
    fn optional_libs_renders_the_manifest_hint_per_host() {
        let facts = manifest_facts();
        let mac = optional_libs_block(&facts, HostOs::MacOs, "/ws").join("\n");
        assert!(mac.contains("  Equivalents via Homebrew:"), "{mac}");
        assert!(
            mac.contains("  mosquitto  -> alp_mqtt_* (cleartext + TLS)"),
            "{mac}"
        );
        assert!(mac.contains("  brew install mosquitto pkg-config"), "{mac}");
        // `*)` arm: no hint for an unrecognised OS.
        let other = optional_libs_block(&facts, HostOs::Other, "/ws").join("\n");
        assert!(
            other.contains("(OS not auto-detected; see docs/testing.md)"),
            "{other}"
        );
    }

    #[test]
    fn next_steps_uses_the_platform_activation_and_example_target() {
        let facts = manifest_facts();
        let posix = next_steps_block(
            &facts,
            Tokens {
                sdk_root: "/ws/alp-sdk",
                workspace_dir: "/ws",
            },
            "/ws/.venv",
            "bin",
            false,
        )
        .join("\n");
        assert!(
            posix.contains("  source \"/ws/.venv/bin/activate\""),
            "{posix}"
        );
        assert!(
            posix.contains("  export ZEPHYR_BASE=\"/ws/zephyr\""),
            "{posix}"
        );
        assert!(posix.contains("native_sim/native/64"), "{posix}");
        assert!(posix.contains("bash scripts/test-all.sh"), "{posix}");

        // FORWARD-slash tokens, as production hands in: the `west build` line
        // used to interpolate `${SDK_ROOT}` raw beside `examples\...`.
        let lines = next_steps_block(
            &facts,
            Tokens {
                sdk_root: "C:/ws/alp-sdk",
                workspace_dir: "C:/ws",
            },
            "C:\\ws\\.venv",
            "Scripts",
            true,
        );
        let win = lines.join("\n");
        assert!(
            win.contains("  & \"C:\\ws\\.venv\\Scripts\\Activate.ps1\""),
            "{win}"
        );
        assert!(
            win.contains("  $env:ZEPHYR_BASE = \"C:\\ws\\zephyr\""),
            "{win}"
        );
        assert!(
            win.contains("-DEXTRA_ZEPHYR_MODULES=C:\\ws\\alp-sdk"),
            "{win}"
        );
        // Every line that carries a PATH must be single-separator. (The block's
        // other slashes are legitimate: the README URL and the board target.)
        for line in lines
            .iter()
            .filter(|l| l.contains("C:\\ws") || l.contains("C:/ws"))
        {
            assert!(!line.contains('/'), "mixed separators: {line}");
        }
        assert!(
            win.contains("alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he"),
            "{win}"
        );
        // native_sim does not exist on native Windows — must NOT be suggested.
        assert!(!win.contains("native_sim"), "{win}");
    }

    /// The reuse path repoints WORKSPACE_DIR after `--print-env` has returned;
    /// rendering must follow it. `bootstrap.sh` re-substitutes per call for
    /// this reason, `bootstrap.ps1` binds once and prints the pre-reuse path.
    #[test]
    fn env_lines_follow_a_repointed_workspace_dir() {
        let facts = manifest_facts();
        let reused = Tokens {
            sdk_root: "/cache/alp-sdk",
            workspace_dir: "/adopted/zephyrproject",
        };
        assert_eq!(
            render_env_lines(&facts.env, reused, "  ", false),
            vec![
                "  export ZEPHYR_BASE=\"/adopted/zephyrproject/zephyr\"",
                "  export ZEPHYR_TOOLCHAIN_VARIANT=zephyr",
            ]
        );
    }
}
