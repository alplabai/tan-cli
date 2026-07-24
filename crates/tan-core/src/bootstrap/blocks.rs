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
    let workspace = tokens.workspace_dir;
    let venv = &facts.venv_dir_name;
    let mut lines = if is_windows {
        vec![
            "# Add to your PowerShell profile (or run before invoking the SDK):".to_string(),
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):".to_string(),
            format!("#   & \"{workspace}\\{venv}\\{venv_bin_dir}\\Activate.ps1\""),
        ]
    } else {
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
/// the manifest's Windows note.
pub fn optional_libs_block(
    facts: &BootstrapFacts,
    host: HostOs,
    workspace_dir: &str,
) -> Vec<String> {
    let hint = facts.native_lib_hint(host);
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
            format!("  #   run 'west sdk install' from {workspace_dir} after this step."),
            String::new(),
        ];
        if let Some(hint) = hint {
            lines.push(format!("  {}", hint.note));
        }
        return lines;
    }

    let mut lines = vec![
        String::new(),
        "bootstrap: Optional native libraries unlock the Yocto-side backends:".to_string(),
    ];
    match hint {
        Some(hint) => {
            lines.push(String::new());
            lines.push(format!("  {}", hint.note));
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
    let repo_root = tokens.sdk_root;
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
    #[test]
    fn print_env_windows_matches_bootstrap_ps1_verbatim() {
        let expected = "\
# Add to your PowerShell profile (or run before invoking the SDK):
# Activate the workspace venv (west + Zephyr/SDK Python deps live here):
#   & \"C:\\dev\\work\\.venv\\Scripts\\Activate.ps1\"
$env:ZEPHYR_BASE = \"C:\\dev\\work\\zephyr\"
$env:ZEPHYR_TOOLCHAIN_VARIANT = \"zephyr\"";
        let tokens = Tokens {
            sdk_root: "C:\\dev\\work\\alp-sdk",
            workspace_dir: "C:\\dev\\work",
        };
        assert_eq!(
            print_env_block(&manifest_facts(), tokens, "Scripts", true).join("\n"),
            expected
        );
        assert_eq!(
            print_env_block(&fallback_facts((3, 10)), tokens, "Scripts", true).join("\n"),
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

    #[test]
    fn optional_libs_renders_the_manifest_hint_per_host() {
        let facts = manifest_facts();
        let linux = optional_libs_block(&facts, HostOs::Linux, "/ws").join("\n");
        assert!(
            linux.contains("Optional native libraries unlock"),
            "{linux}"
        );
        assert!(
            linux.contains(
                "  sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev pkg-config"
            ),
            "{linux}"
        );
        let mac = optional_libs_block(&facts, HostOs::MacOs, "/ws").join("\n");
        assert!(mac.contains("  brew install mosquitto pkg-config"), "{mac}");
        // The Windows hint has `command: null` -> note only, no blank command line.
        let win = optional_libs_block(&facts, HostOs::Windows, "C:\\ws").join("\n");
        assert!(
            win.contains("NOT auto-installed (manual, one-time)"),
            "{win}"
        );
        assert!(win.contains("run 'west sdk install' from C:\\ws"), "{win}");
        assert!(win.contains("the canonical use is WSL2 + Ubuntu"), "{win}");
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

        let win = next_steps_block(
            &facts,
            Tokens {
                sdk_root: "C:\\ws\\alp-sdk",
                workspace_dir: "C:\\ws",
            },
            "C:\\ws\\.venv",
            "Scripts",
            true,
        )
        .join("\n");
        assert!(
            win.contains("  & \"C:\\ws\\.venv\\Scripts\\Activate.ps1\""),
            "{win}"
        );
        assert!(
            win.contains("  $env:ZEPHYR_BASE = \"C:\\ws\\zephyr\""),
            "{win}"
        );
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
