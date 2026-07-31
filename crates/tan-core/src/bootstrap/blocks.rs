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
/// native Windows prints the manifest's `manualInstallHints.windows.note`, one
/// two-space-indented line per element under its own heading — the SDK-sourced
/// fact, matching `bootstrap.ps1`'s own
/// `foreach ($line in $ManualInstallNote)`.
///
/// Issue #82, now closed: the Windows arm used to PRECEDE that note with a
/// hardcoded Arm-GNU-Toolchain / Zephyr-SDK block. alp-sdk#961 deleted
/// `bootstrap.ps1`'s own copy by moving both facts INTO the manifest note, so
/// the block survived here only while the vendored fixture predated #961. The
/// fixture is now re-vendored at alp-sdk `0ed078a6` (past #961 and #967) and
/// the note carries every fact the block did: the Arm installer URL and the
/// "tick 'Add path to environment variable'" tip verbatim (note element 4), and
/// the Zephyr-SDK `west sdk install` fact WITH its workspace locator as prose
/// (element 1 — "Run it from your west workspace's top-level directory -- the
/// alp-sdk checkout's parent directory -- after this script completes."). That
/// prose locator is the `$WorkspaceDir`-survives-as-prose case #961's PR body
/// flagged; it is why the deleted line's `{workspace_dir}` interpolation is not
/// re-added anywhere, and why this function no longer takes a `workspace_dir`.
/// Printing both duplicated the Arm URL and the PATH tip, which IS the bug #82
/// reported.
///
/// TWO things go away with the block, both accepted. First, the RESOLVED
/// workspace path (`from C:\ws`): #961 chose prose over a token upstream and
/// `bootstrap.ps1` prints no path either, so tan follows the oracle it mirrors
/// — and the resolved path is still printed in the same transcript anyway, by
/// [`next_steps_block`] from `RunPaths::token_strings()`, so the reader is not
/// left guessing. Second, a deliberate wording divergence: `bootstrap.ps1` said
/// "after this script", the deleted line said "after this STEP" because tan is
/// not a script. The manifest note ends "after this script completes." and tan
/// prints note elements verbatim, so that misnaming is back — tolerable only
/// because the same note already says "not auto-installed by bootstrap.ps1",
/// which tan was already printing verbatim, so the transcript was never free of
/// the SDK naming its own scripts.
///
/// Note element 3 likewise supersedes the deleted heading's "host tools like
/// dtc", which was wrong on Windows: alp-sdk#967 verified the Zephyr SDK's
/// native-Windows hosttools bundle ships neither `dtc` nor `gperf`.
///
/// One narrow window is degraded by the delete: an alp-sdk **`dev`** checkout
/// between #917 and #961 has a manifest whose note is the single pre-#961
/// sentence, so against it the Arm URL and PATH tip are now printed by nothing
/// at all. Customer-unreachable — `metadata/bootstrap.json` has never existed on
/// alp-sdk `origin/main` (absent from its entire history and from `v0.13.0`), so
/// every RELEASED SDK takes the [`super::fallback_facts`] path, which carries the
/// full five-element note and therefore GAINS the 7-Zip prerequisite, the
/// dtc/gperf correction and the toolchain scoping. Nobody on a release is
/// degraded; only a dev checkout in that commit range.
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
pub fn optional_libs_block(facts: &BootstrapFacts, host: HostOs) -> Vec<String> {
    if host == HostOs::Windows {
        let mut lines = vec![
            String::new(),
            "bootstrap: NOT auto-installed (manual, one-time):".to_string(),
        ];
        // One line per `manualInstallHints.windows.note` element, two-space
        // indented, exactly like `bootstrap.ps1`'s
        // `foreach ($line in $ManualInstallNote)` — the manifest-sourced fact,
        // not a hand-typed Rust copy that would desync silently. This is the
        // WHOLE Windows block now; see the doc comment above for why the
        // hardcoded Arm/Zephyr-SDK block that used to precede it is gone.
        //
        // NO blank line between the heading and the first note element: the
        // oracle at the pin is `Write-Host ""` / `Write-Info "NOT
        // auto-installed..."` / `foreach ($line in $ManualInstallNote)` with
        // nothing in between (`0ed078a6:scripts/bootstrap.ps1`). The gap that
        // used to sit here came from the deleted here-string's own leading
        // newline, so it left with the block — the POSIX arm below still emits
        // its blank because `bootstrap.sh` genuinely echoes one.
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

    // `manualInstallHints.posix.note` (tan-cli#230). alp-sdk v0.14.0 added the
    // POSIX twin of the Windows key rendered above; before it, `manualInstallHints`
    // carried only `windows`, so no Linux/macOS customer ever saw a manual-install
    // hint — the Zephyr SDK being a separate `west sdk install` included. The data
    // existed and was inert.
    //
    // Shape, heading and position all come from the oracle rather than from here
    // (`v0.14.0:scripts/bootstrap.sh`, immediately before `Bootstrap complete.`):
    //
    //     case "${OS_LABEL}" in
    //         linux|macos)
    //             echo
    //             info "NOT auto-installed (manual, one-time):"
    //             for line in "${MANUAL_INSTALL_POSIX_NOTE[@]}"; do echo "  ${line}"; done
    //             ;;
    //     esac
    //
    // So: a blank line, the same heading the Windows arm above uses, then one
    // two-space-indented line per element. It lands AFTER the optional-native-libs
    // section because the oracle prints it after (:594 vs :638).
    //
    // `Linux | MacOs` ONLY, matching that `case` exactly — NOT `HostOs::Other`,
    // and not a git-bash invocation on native Windows, which the oracle's own
    // comment calls the unsupported combo its file header already points at WSL2
    // or bootstrap.ps1. The Windows arm returned early far above, so this is
    // unreachable there anyway; the match is written out rather than assumed so
    // `Other` cannot start rendering a POSIX-specific fact by accident.
    if matches!(host, HostOs::Linux | HostOs::MacOs) {
        if let Some(manual) = facts
            .manual_install_hints
            .posix
            .as_ref()
            .filter(|m| !m.note.is_empty())
        {
            lines.push(String::new());
            lines.push("bootstrap: NOT auto-installed (manual, one-time):".to_string());
            lines.extend(manual.note.iter().map(|line| format!("  {line}")));
        }
    }
    lines
}

/// The closing "Next steps:" block: activate the venv, export the manifest's
/// `env` map, run `tan doctor`, and one ready-to-paste build command for the
/// platform's canonical example target — `tan build` on POSIX, raw `west
/// build` on native Windows (`tan build` has no board-override flag yet, so
/// it cannot select `native_sim`; see the POSIX arm's own comment).
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
    // The pinned install.sh/install.ps1 one-liner (README.md's own "Automatic"
    // section) -- NOT `cargo install --git ...` (issue #117): that built
    // unpinned HEAD and was retired repo-wide by alp-sdk#988 except here, where
    // it lived as a hardcoded copy in the Rust binary rather than in a doc or
    // script alp-sdk#988 could reach. `tan doctor` (not `--build`): since #100
    // plain doctor already folds in the build-readiness preflight (see
    // `assemble_doctor_report`'s doc comment), and bootstrap's next-steps is
    // explicitly one of the sites that fold was written to cover -- so the
    // plain form is already the right advice; only the install command needed
    // to change.
    let install_line = if is_windows {
        "  # for: irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex):"
            .to_string()
    } else {
        "  # for: curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh):"
            .to_string()
    };
    lines.extend([
        String::new(),
        "  # Sanity-check the host environment (needs tan on PATH -- see README.md".to_string(),
        install_line,
        "  tan doctor".to_string(),
        String::new(),
    ]);
    if is_windows {
        // `bootstrap.ps1` interpolates `$RepoRoot` here -- a native backslash
        // path -- and the same line spells the example as `examples\...`, so a
        // raw forward-slash `${SDK_ROOT}` would print mixed. See `print_env_block`.
        let repo_root = tokens.sdk_root.replace('/', "\\");
        lines.extend([
            "  # Or jump straight into building an example for real silicon".to_string(),
            "  # (needs the Zephyr SDK toolchain, which bootstrap does NOT install --".to_string(),
            "  #  the `tan doctor` above reports it, and names the exact install command):"
                .to_string(),
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
        // Routed through `tan build`, not a raw `west build -b native_sim/...`
        // (issue: the printed success message routed the customer around
        // tan's own README claim that it is "the single executor and the
        // user command surface"). `--sdk-root`/`--project` are ABSOLUTE
        // (`tokens.sdk_root`, not `$PWD`): the workspace-parent guard above
        // this block can have just moved the checkout to a sibling
        // `alp-workspace/alp-sdk`, so `$PWD` -- correct only when the reader
        // happens to be standing IN the checkout -- silently builds from the
        // wrong tree, or nothing at all, everywhere else. Matches the
        // Windows arm above, which already interpolates `tokens.sdk_root`
        // for the same reason.
        //
        // `tan build` has no `-b`/board-override flag (unlike raw `west
        // build`), so it cannot target `native_sim` for this example -- its
        // board.yaml (`examples/peripheral-io/uart-echo`) only declares the
        // real-silicon EVK presets, and the board comes from the SDK's own
        // plan, never a CLI override. This lands on the SAME real-hardware
        // target the Windows arm already names (`alp_e1m_aen801_m55_he/...`,
        // verified live: `tan build --plan` against this exact project
        // resolves that slice), so POSIX now needs the same Zephyr SDK
        // toolchain Windows already requires -- trading the old "no
        // hardware/toolchain needed" native_sim smoke build for a command
        // that is real and tan-routed, which native_sim currently is not.
        lines.extend([
            "  # Run the local test suite:".to_string(),
            "  bash scripts/test-all.sh".to_string(),
            String::new(),
            "  # Or jump straight into building an example for real silicon".to_string(),
            "  # (needs the Zephyr SDK toolchain, which bootstrap does NOT install --".to_string(),
            "  #  the `tan doctor` above reports it, and names the exact install command):"
                .to_string(),
            format!("  tan build --sdk-root \"{}\" \\", tokens.sdk_root),
            format!(
                "      --project \"{}/examples/peripheral-io/uart-echo\"",
                tokens.sdk_root
            ),
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

        // The nativeLibHints section is byte-pinned above. What follows it since
        // alp-sdk v0.14.0 is the `manualInstallHints.posix` block (#230), and its
        // SHAPE is pinned here rather than its content: three ~500-600 char note
        // elements retyped into this test would be exactly the hand-copy that
        // desyncs silently, which this module's own header warns about. Content
        // byte-parity is already gated where it belongs -- against the vendored
        // fixture, in `the_fallback_matches_the_real_manifest_field_for_field`
        // and `tests/parity/bootstrap_manifest_parity.py`.
        //
        // So: blank line, the SAME heading the Windows arm uses, then one
        // two-space-indented line per element in order, and nothing else. Matches
        // `v0.14.0:scripts/bootstrap.sh`'s `linux|macos` case.
        let manual_tail = |facts: &BootstrapFacts| -> Vec<String> {
            let note = &facts
                .manual_install_hints
                .posix
                .as_ref()
                .expect("the vendored manifest declares manualInstallHints.posix")
                .note;
            let mut want = vec![
                String::new(),
                "bootstrap: NOT auto-installed (manual, one-time):".to_string(),
            ];
            want.extend(note.iter().map(|l| format!("  {l}")));
            want
        };

        for (label, facts) in [
            ("manifest", manifest_facts()),
            ("fallback", fallback_facts((3, 10))),
        ] {
            let rendered = optional_libs_block(&facts, HostOs::Linux);
            let split = rendered.len() - manual_tail(&facts).len();
            assert_eq!(
                rendered[..split].join("\n"),
                expected,
                "{label} native libs"
            );
            assert_eq!(rendered[split..], manual_tail(&facts)[..], "{label} manual");
        }

        // And the two sources must render IDENTICAL bytes end to end -- the claim
        // the old single-`expected` compare made, now covering the posix block
        // too. This is what would red if `fallback_facts` gained the field with
        // different text.
        assert_eq!(
            optional_libs_block(&manifest_facts(), HostOs::Linux),
            optional_libs_block(&fallback_facts((3, 10)), HostOs::Linux)
        );
    }

    /// #230's host gate, which is `linux|macos` and NOT "anything that is not
    /// Windows". `HostOs::Other` is a host tan could not identify; handing it a
    /// hint whose every sentence is Linux/macOS-specific (`apt-get`, `brew`,
    /// `bootstrap.sh`) would be inventing a fact about a host we know nothing
    /// about. The oracle's own `case` is `linux|macos)` with no default arm, and
    /// its comment says the unsupported combos get neither section.
    #[test]
    fn posix_manual_hints_render_on_linux_and_macos_only() {
        let facts = manifest_facts();
        let heading = "bootstrap: NOT auto-installed (manual, one-time):";

        for host in [HostOs::Linux, HostOs::MacOs] {
            let block = optional_libs_block(&facts, host);
            assert!(
                block.iter().any(|l| l == heading),
                "{host:?} must render the manual-install block: {block:?}"
            );
        }

        let other = optional_libs_block(&facts, HostOs::Other);
        assert!(
            !other.iter().any(|l| l == heading),
            "an unidentified host must not be handed Linux/macOS-specific install \
             advice: {other:?}"
        );
    }

    /// An SDK predating alp-sdk v0.14.0 declares no `manualInstallHints.posix`,
    /// and must render exactly what it always did — nothing extra. The `Option`
    /// exists for this, and the tolerant read is what keeps those SDKs from
    /// becoming a hard `ValidationFailure` through auto-bootstrap.
    #[test]
    fn an_sdk_without_posix_manual_hints_renders_no_extra_block() {
        let mut legacy = manifest_facts();
        legacy.manual_install_hints.posix = None;
        let block = optional_libs_block(&legacy, HostOs::Linux);
        assert!(
            !block
                .iter()
                .any(|l| l.contains("NOT auto-installed (manual, one-time)")),
            "{block:?}"
        );
        // And it is still the full nativeLibHints section, not a truncated one.
        assert!(
            block
                .iter()
                .any(|l| l.contains("Optional native libraries unlock")),
            "{block:?}"
        );
    }

    /// Byte-parity against `bootstrap.ps1`'s manual-install section as it reads
    /// at the pin: a blank line, the heading, then IMMEDIATELY one
    /// two-space-indented line per `manualInstallHints.windows.note` element and
    /// NOTHING else. No blank between heading and first element —
    /// `0ed078a6:scripts/bootstrap.ps1` is `Write-Host ""` / `Write-Info "NOT
    /// auto-installed (manual, one-time):"` / `foreach ($line in
    /// $ManualInstallNote) { Write-Host "  $line" }` back to back.
    ///
    /// Asserted as the oracle's ALGORITHM rather than as a transcription of the
    /// note's five paragraphs: a hand-copied expected string here would BE the
    /// hardcoded duplication issue #82 removed, one layer down, and would need
    /// re-typing on every SDK wording change. The two facts the deleted block
    /// carried are checked to appear exactly ONCE, which is what fails if that
    /// block ever comes back. `nativeLibHints.windows.note` (the "Under Git
    /// Bash / MSYS2..." line) must NOT appear here — that is a different fact
    /// (bootstrap.ps1 has no heading for it at all); printing both was the
    /// alp-sdk#917-review bug this renders around.
    #[test]
    fn optional_libs_windows_renders_one_line_per_note_element() {
        for facts in [manifest_facts(), fallback_facts((3, 10))] {
            let mut expected = vec![
                String::new(),
                "bootstrap: NOT auto-installed (manual, one-time):".to_string(),
            ];
            expected.extend(
                facts
                    .manual_install_hints
                    .windows
                    .note
                    .iter()
                    .map(|line| format!("  {line}")),
            );
            let rendered = optional_libs_block(&facts, HostOs::Windows);
            assert_eq!(rendered, expected);
            let joined = rendered.join("\n");
            // Issue #82: the Arm URL and the PATH tip exactly ONCE each. The
            // deleted hardcoded block carried both, so a count of 2 is that
            // double-print regression returning. (`west sdk install` is NOT
            // checked for uniqueness: note elements 1 and 2 both name it
            // upstream, and legitimately so.)
            for once in [
                "https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads",
                "(tick 'Add path to environment variable' during install)",
            ] {
                assert_eq!(joined.matches(once).count(), 1, "{once} in:\n{joined}");
            }
            assert!(!joined.contains("Git Bash"), "{joined}");
        }
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
        let rendered = optional_libs_block(&facts, HostOs::Windows).join("\n");
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
        // No `manualInstallHints.posix`, which keeps this test on the
        // nativeLibHints arm it is about AND covers the pre-v0.14.0 SDK: an
        // absent `posix` key renders NOTHING extra, so the exhaustive compare
        // below is still four lines. That is the legacy path (#230) — the field
        // is `Option` precisely so those SDKs keep their old output.
        facts.manual_install_hints.posix = None;
        assert_eq!(
            optional_libs_block(&facts, HostOs::Linux),
            vec![
                "".to_string(),
                "bootstrap: Optional native libraries unlock the Yocto-side backends:".to_string(),
                "".to_string(),
                "  one line only".to_string(),
            ]
        );
        // The Windows arm is these THREE lines and nothing more — the
        // exhaustive compare is what proves both that the hardcoded
        // Arm/Zephyr-SDK block issue #82 removed has not crept back in ahead of
        // the note, and that no blank line survives between the heading and the
        // first element (the ps1 at the pin has none).
        facts.manual_install_hints.windows.note = vec!["one line only".to_string()];
        assert_eq!(
            optional_libs_block(&facts, HostOs::Windows),
            vec![
                "".to_string(),
                "bootstrap: NOT auto-installed (manual, one-time):".to_string(),
                "  one line only".to_string(),
            ]
        );
    }

    #[test]
    fn optional_libs_renders_the_manifest_hint_per_host() {
        let facts = manifest_facts();
        let mac = optional_libs_block(&facts, HostOs::MacOs).join("\n");
        assert!(mac.contains("  Equivalents via Homebrew:"), "{mac}");
        assert!(
            mac.contains("  mosquitto  -> alp_mqtt_* (cleartext + TLS)"),
            "{mac}"
        );
        assert!(mac.contains("  brew install mosquitto pkg-config"), "{mac}");
        // `*)` arm: no hint for an unrecognised OS.
        let other = optional_libs_block(&facts, HostOs::Other).join("\n");
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
        // Routed through `tan` (issue: the printed command sent the customer
        // around tan's own README claim to be "the single executor"), with
        // the SDK root taken from `tokens.sdk_root` rather than `$PWD` (issue:
        // `$PWD` is wrong once the workspace-parent guard has relocated the
        // checkout out from under the reader's cwd).
        assert!(
            posix.contains("  tan build --sdk-root \"/ws/alp-sdk\" \\"),
            "{posix}"
        );
        assert!(
            posix.contains("      --project \"/ws/alp-sdk/examples/peripheral-io/uart-echo\""),
            "{posix}"
        );
        assert!(!posix.contains("west build"), "{posix}");
        assert!(!posix.contains("$PWD"), "{posix}");
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

    /// #117: the retired, unpinned `cargo install --git ... --bin tan` must
    /// never reappear, and each platform must name ITS OWN install one-liner
    /// (the same two commands README.md documents) -- not the other OS's.
    #[test]
    fn next_steps_names_the_pinned_install_one_liner_not_the_retired_git_form() {
        let facts = manifest_facts();
        let tokens = Tokens {
            sdk_root: "/ws/alp-sdk",
            workspace_dir: "/ws",
        };
        let posix = next_steps_block(&facts, tokens, "/ws/.venv", "bin", false).join("\n");
        let win = next_steps_block(&facts, tokens, "C:\\ws\\.venv", "Scripts", true).join("\n");
        for rendered in [&posix, &win] {
            assert!(
                !rendered.contains("cargo install --git"),
                "retired install command resurfaced: {rendered}"
            );
        }
        assert!(
            posix.contains(
                "curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh"
            ),
            "{posix}"
        );
        assert!(!posix.contains("install.ps1"), "{posix}");
        assert!(
            win.contains(
                "irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex"
            ),
            "{win}"
        );
        assert!(!win.contains("install.sh"), "{win}");
        // The suggested next command stays plain `tan doctor`: since #100 it
        // already folds in the build-readiness preflight, and this site is one
        // of the ones that decision deliberately targets (see
        // `assemble_doctor_report`'s doc comment in tan-cli's doctor.rs).
        assert!(
            posix.contains("  tan doctor\n") || posix.ends_with("  tan doctor"),
            "{posix}"
        );
        assert!(!posix.contains("tan doctor --build"), "{posix}");
        assert!(!win.contains("tan doctor --build"), "{win}");
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
