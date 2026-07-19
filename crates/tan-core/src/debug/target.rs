// SPDX-License-Identifier: Apache-2.0
//! Target/server kinds and their `--target-kind`/`--server` parsers plus the
//! target→server support matrix.

use serde::Serialize;

/// Class of debug target a project produces; drives which servers and checks apply.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum DebugTargetKind {
    /// Zephyr RTOS firmware on an MCU.
    ZephyrMcu,
    /// Bare-metal firmware on an MCU.
    BaremetalMcu,
    /// A userspace process on a Yocto/Linux image.
    YoctoUserspace,
    /// A native executable on the host machine.
    NativeHost,
}

impl DebugTargetKind {
    /// The kebab-case wire string for this kind (matches the serde representation).
    pub fn as_str(self) -> &'static str {
        match self {
            DebugTargetKind::ZephyrMcu => "zephyr-mcu",
            DebugTargetKind::BaremetalMcu => "baremetal-mcu",
            DebugTargetKind::YoctoUserspace => "yocto-userspace",
            DebugTargetKind::NativeHost => "native-host",
        }
    }
}

/// Debug-server / GDB backend used to attach to a target.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DebugServerKind {
    /// SEGGER J-Link GDB server.
    Jlink,
    /// OpenOCD.
    Openocd,
    /// pyOCD.
    Pyocd,
    /// A remote `gdbserver` (Yocto userspace).
    Gdbserver,
    /// No server (native-host debugging).
    None,
}

impl DebugServerKind {
    /// The lowercase wire string for this kind (matches the serde representation).
    pub fn as_str(self) -> &'static str {
        match self {
            DebugServerKind::Jlink => "jlink",
            DebugServerKind::Openocd => "openocd",
            DebugServerKind::Pyocd => "pyocd",
            DebugServerKind::Gdbserver => "gdbserver",
            DebugServerKind::None => "none",
        }
    }
}

/// Parse `--target-kind`. Mirrors TS `parseTargetKind`: an absent/empty value
/// defaults to `native-host`; an unknown value is an error.
pub fn parse_target_kind(raw: Option<&str>) -> Result<DebugTargetKind, String> {
    match raw.unwrap_or("") {
        "" | "native-host" => Ok(DebugTargetKind::NativeHost),
        "zephyr-mcu" => Ok(DebugTargetKind::ZephyrMcu),
        "baremetal-mcu" => Ok(DebugTargetKind::BaremetalMcu),
        "yocto-userspace" => Ok(DebugTargetKind::YoctoUserspace),
        other => Err(format!(
            "Unsupported --target-kind '{other}'. Allowed values: zephyr-mcu, baremetal-mcu, yocto-userspace, native-host."
        )),
    }
}

/// Parse `--server`. Mirrors TS `parseServerKind`: absent/empty defaults to
/// `none`; an unknown value is an error.
pub fn parse_server_kind(raw: Option<&str>) -> Result<DebugServerKind, String> {
    match raw.unwrap_or("") {
        "" | "none" => Ok(DebugServerKind::None),
        "jlink" => Ok(DebugServerKind::Jlink),
        "openocd" => Ok(DebugServerKind::Openocd),
        "pyocd" => Ok(DebugServerKind::Pyocd),
        "gdbserver" => Ok(DebugServerKind::Gdbserver),
        other => Err(format!(
            "Unsupported --server '{other}'. Allowed values: jlink, openocd, pyocd, gdbserver, none."
        )),
    }
}

/// The debug servers valid for a given target kind.
pub fn server_choices_for_target(target: DebugTargetKind) -> &'static [DebugServerKind] {
    match target {
        DebugTargetKind::ZephyrMcu | DebugTargetKind::BaremetalMcu => &[
            DebugServerKind::Jlink,
            DebugServerKind::Openocd,
            DebugServerKind::Pyocd,
        ],
        DebugTargetKind::YoctoUserspace => &[DebugServerKind::Gdbserver],
        DebugTargetKind::NativeHost => &[DebugServerKind::None],
    }
}

/// Whether `server` is among the valid choices for `target`.
pub fn is_server_supported_for_target(target: DebugTargetKind, server: DebugServerKind) -> bool {
    server_choices_for_target(target).contains(&server)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_defaults_match_ts() {
        assert_eq!(
            parse_target_kind(None).unwrap(),
            DebugTargetKind::NativeHost
        );
        assert_eq!(
            parse_target_kind(Some("")).unwrap(),
            DebugTargetKind::NativeHost
        );
        assert_eq!(parse_server_kind(None).unwrap(), DebugServerKind::None);
        assert_eq!(
            parse_target_kind(Some("zephyr-mcu")).unwrap(),
            DebugTargetKind::ZephyrMcu
        );
    }

    #[test]
    fn parse_unknown_values_error_with_ts_message() {
        let err = parse_target_kind(Some("bogus")).unwrap_err();
        assert!(err.contains("Unsupported --target-kind 'bogus'"));
        assert!(err.contains("zephyr-mcu, baremetal-mcu, yocto-userspace, native-host"));
        let err = parse_server_kind(Some("bogus")).unwrap_err();
        assert!(err.contains("Unsupported --server 'bogus'"));
    }

    #[test]
    fn server_support_matrix() {
        assert!(is_server_supported_for_target(
            DebugTargetKind::ZephyrMcu,
            DebugServerKind::Jlink
        ));
        assert!(!is_server_supported_for_target(
            DebugTargetKind::NativeHost,
            DebugServerKind::Jlink
        ));
        assert!(is_server_supported_for_target(
            DebugTargetKind::YoctoUserspace,
            DebugServerKind::Gdbserver
        ));
    }
}
