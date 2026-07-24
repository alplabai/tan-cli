// SPDX-License-Identifier: Apache-2.0
//! The verbatim text blocks `tan bootstrap` prints, one renderer per block.
//!
//! Each is a byte-for-byte reproduction of the corresponding heredoc /
//! here-string in the parity oracles — `bootstrap.sh:94-100, 266-299, 305-329`
//! and `bootstrap.ps1:75-81, 278-291, 297-318`. They are copy-pasteable shell
//! snippets, so they carry NO `bootstrap:` prefix (unlike the progress lines)
//! and their whitespace is load-bearing. Lines are returned individually
//! because `CommandRun::text` is printed one line at a time.

use super::runtime::HostOs;

/// `--print-env`: the two `export`/`$env:` lines plus the venv-activation
/// comment header. Printed and exited BEFORE any prerequisite check or venv
/// work, exactly as both scripts short-circuit.
pub fn print_env_block(workspace_dir: &str, is_windows: bool) -> Vec<String> {
    if is_windows {
        vec![
            "# Add to your PowerShell profile (or run before invoking the SDK):".to_string(),
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):".to_string(),
            format!("#   & \"{workspace_dir}\\.venv\\Scripts\\Activate.ps1\""),
            format!("$env:ZEPHYR_BASE = \"{workspace_dir}\\zephyr\""),
            "$env:ZEPHYR_TOOLCHAIN_VARIANT = \"zephyr\"".to_string(),
        ]
    } else {
        vec![
            "# Add to your shell profile (or run before invoking the SDK):".to_string(),
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):".to_string(),
            format!("#   source \"{workspace_dir}/.venv/bin/activate\""),
            format!("export ZEPHYR_BASE=\"{workspace_dir}/zephyr\""),
            "export ZEPHYR_TOOLCHAIN_VARIANT=zephyr".to_string(),
        ]
    }
}

/// The trailing manual-install hint. POSIX hosts get the optional-native-libs
/// block that unlocks the Yocto-side backends (apt on Linux, brew on macOS);
/// native Windows gets the "NOT auto-installed" toolchain block instead.
pub fn optional_libs_block(host: HostOs, workspace_dir: &str) -> Vec<String> {
    match host {
        HostOs::Windows => vec![
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
            "  # native_sim / Yocto: not available on native Windows -- use WSL2".to_string(),
            "  #   (docs/cross-platform-setup.md section 5) and scripts/bootstrap.sh there."
                .to_string(),
        ],
        HostOs::Linux => {
            let mut lines = optional_libs_header();
            lines.extend([
                String::new(),
                "  # libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)".to_string(),
                "  # libasound2-dev    -> alp_audio_*".to_string(),
                "  # libssl-dev        -> alp_hash_* / alp_aead_* / alp_random_bytes".to_string(),
                String::new(),
                "  sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev pkg-config"
                    .to_string(),
            ]);
            lines
        }
        HostOs::MacOs => {
            let mut lines = optional_libs_header();
            lines.extend([
                String::new(),
                "  # Equivalents via Homebrew:".to_string(),
                "  brew install mosquitto pkg-config".to_string(),
                "  # Note: macOS uses CoreAudio rather than ALSA, so the Yocto audio".to_string(),
                "  # backend doesn't apply on macOS hosts.  OpenSSL ships with macOS.".to_string(),
            ]);
            lines
        }
        HostOs::Other => {
            let mut lines = optional_libs_header();
            lines.push("  (OS not auto-detected; see docs/testing.md)".to_string());
            lines
        }
    }
}

/// The blank line + `info` header both POSIX branches share (bootstrap.sh:266-267).
fn optional_libs_header() -> Vec<String> {
    vec![
        String::new(),
        "bootstrap: Optional native libraries unlock the Yocto-side backends:".to_string(),
    ]
}

/// The closing "Next steps:" block: activate the venv, export `ZEPHYR_BASE`,
/// run `tan doctor`, and one ready-to-paste `west build` for the platform's
/// canonical example target.
pub fn next_steps_block(
    workspace_dir: &str,
    venv_dir: &str,
    repo_root: &str,
    is_windows: bool,
) -> Vec<String> {
    if is_windows {
        vec![
            String::new(),
            "Next steps:".to_string(),
            "  # Activate the workspace venv (west + Zephyr/SDK deps + tan's Python backend):"
                .to_string(),
            format!("  & \"{venv_dir}\\Scripts\\Activate.ps1\""),
            String::new(),
            "  # Make Zephyr reachable for builds:".to_string(),
            format!("  $env:ZEPHYR_BASE = \"{workspace_dir}\\zephyr\""),
            "  $env:ZEPHYR_TOOLCHAIN_VARIANT = \"zephyr\"".to_string(),
            String::new(),
            "  # Sanity-check the host environment (needs tan on PATH -- see README.md".to_string(),
            "  # for `cargo install --git https://github.com/alplabai/tan-cli --bin tan`):"
                .to_string(),
            "  tan doctor".to_string(),
            String::new(),
            "  # Or jump straight into building an example for real silicon:".to_string(),
            "  west build -b alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he `".to_string(),
            format!(
                "      examples\\peripheral-io\\uart-echo -- -DEXTRA_ZEPHYR_MODULES={repo_root}"
            ),
            String::new(),
            "References:".to_string(),
            "  - docs\\cross-platform-setup.md  -- the full per-OS setup guide".to_string(),
            "  - docs\\cli.md                   -- the tan CLI verb reference".to_string(),
        ]
    } else {
        vec![
            String::new(),
            "Next steps:".to_string(),
            "  # Activate the workspace venv (west + Zephyr/SDK deps live here):".to_string(),
            format!("  source \"{venv_dir}/bin/activate\""),
            String::new(),
            "  # Make Zephyr reachable for builds:".to_string(),
            format!("  export ZEPHYR_BASE=\"{workspace_dir}/zephyr\""),
            "  export ZEPHYR_TOOLCHAIN_VARIANT=zephyr".to_string(),
            String::new(),
            "  # Sanity-check the host environment (needs tan on PATH -- see README.md".to_string(),
            "  # for `cargo install --git https://github.com/alplabai/tan-cli --bin tan`):"
                .to_string(),
            "  tan doctor".to_string(),
            String::new(),
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
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Byte-parity against `bootstrap.sh:94-100`'s heredoc. If this test has to
    /// change, the SDK script changed and the port must be re-synced.
    #[test]
    fn print_env_posix_matches_bootstrap_sh_verbatim() {
        let expected = "\
# Add to your shell profile (or run before invoking the SDK):
# Activate the workspace venv (west + Zephyr/SDK Python deps live here):
#   source \"/home/dev/work/.venv/bin/activate\"
export ZEPHYR_BASE=\"/home/dev/work/zephyr\"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr";
        assert_eq!(
            print_env_block("/home/dev/work", false).join("\n"),
            expected
        );
    }

    /// Byte-parity against `bootstrap.ps1:75-81`'s here-string. The backticks
    /// there escape `$env:` for PowerShell, so the RENDERED text is a bare
    /// `$env:ZEPHYR_BASE = "..."` — that rendered form is the contract.
    #[test]
    fn print_env_windows_matches_bootstrap_ps1_verbatim() {
        let expected = "\
# Add to your PowerShell profile (or run before invoking the SDK):
# Activate the workspace venv (west + Zephyr/SDK Python deps live here):
#   & \"C:\\dev\\work\\.venv\\Scripts\\Activate.ps1\"
$env:ZEPHYR_BASE = \"C:\\dev\\work\\zephyr\"
$env:ZEPHYR_TOOLCHAIN_VARIANT = \"zephyr\"";
        assert_eq!(print_env_block("C:\\dev\\work", true).join("\n"), expected);
    }

    #[test]
    fn optional_libs_picks_the_per_host_block() {
        let linux = optional_libs_block(HostOs::Linux, "/ws").join("\n");
        assert!(
            linux.contains("sudo apt-get install -y libmosquitto-dev"),
            "{linux}"
        );
        let mac = optional_libs_block(HostOs::MacOs, "/ws").join("\n");
        assert!(mac.contains("brew install mosquitto pkg-config"), "{mac}");
        let win = optional_libs_block(HostOs::Windows, "C:\\ws").join("\n");
        assert!(
            win.contains("NOT auto-installed (manual, one-time)"),
            "{win}"
        );
        assert!(win.contains("run 'west sdk install' from C:\\ws"), "{win}");
        let other = optional_libs_block(HostOs::Other, "/ws").join("\n");
        assert!(
            other.contains("(OS not auto-detected; see docs/testing.md)"),
            "{other}"
        );
    }

    #[test]
    fn next_steps_uses_the_platform_activation_and_example_target() {
        let posix = next_steps_block("/ws", "/ws/.venv", "/ws/alp-sdk", false).join("\n");
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

        let win = next_steps_block("C:\\ws", "C:\\ws\\.venv", "C:\\ws\\alp-sdk", true).join("\n");
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
}
