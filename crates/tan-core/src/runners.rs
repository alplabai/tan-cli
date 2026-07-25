// SPDX-License-Identifier: Apache-2.0
//! Pure parse + lookup for `<build_dir>/zephyr/runners.yaml` — the file Zephyr's
//! CMake writes beside every build and `west flash` / `west debug` read to learn
//! which probe tools apply and how to invoke them.
//!
//! It is the one place a build states its OWN device name, GDB, and OpenOCD
//! paths, which is why `debug-config` resolves a launch configuration from it
//! instead of emitting `<resolved-device>` placeholders the user must fill in by
//! hand (#66).
//!
//! Tolerant like `system_manifest`: unknown keys are ignored and a document
//! missing every field parses to an empty config rather than failing. This is
//! Zephyr's output, not ours — a reshaped file must degrade to "nothing
//! resolved", never block a debug session.

use serde::Deserialize;

/// A build's runner configuration, flattened to what a launch config needs.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct RunnersConfig {
    /// Runner ids the board registered, in `runners.yaml` order.
    pub runners: Vec<String>,
    /// `flash-runner`: the board's default for programming.
    pub flash_runner: Option<String>,
    /// `debug-runner`: the board's default for attaching.
    pub debug_runner: Option<String>,
    /// `config.board_dir` — the board directory inside the SDK.
    pub board_dir: Option<String>,
    /// `config.gdb` — the toolchain GDB the build was made against.
    pub gdb: Option<String>,
    /// `config.openocd` — the OpenOCD binary the build resolved.
    pub openocd: Option<String>,
    /// `config.openocd_search` — OpenOCD script search directories.
    pub openocd_search: Vec<String>,
    /// `args.<runner>` — each runner's argv, verbatim.
    pub args: std::collections::BTreeMap<String, Vec<String>>,
}

#[derive(Deserialize, Default)]
struct RawRunners {
    #[serde(default)]
    runners: Vec<String>,
    #[serde(default, rename = "flash-runner")]
    flash_runner: Option<String>,
    #[serde(default, rename = "debug-runner")]
    debug_runner: Option<String>,
    #[serde(default)]
    config: RawConfig,
    #[serde(default)]
    args: std::collections::BTreeMap<String, Vec<String>>,
}

#[derive(Deserialize, Default)]
struct RawConfig {
    #[serde(default)]
    board_dir: Option<String>,
    #[serde(default)]
    gdb: Option<String>,
    #[serde(default)]
    openocd: Option<String>,
    #[serde(default)]
    openocd_search: Vec<String>,
}

/// Drop `Some("")` — an empty string in the file means "absent", not a path.
fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty())
}

/// Parse a `runners.yaml` document. Pure: no IO.
///
/// `Err` only when the YAML itself is unparseable; every missing field is
/// simply absent from the result.
pub fn parse_runners_config(text: &str) -> Result<RunnersConfig, String> {
    let raw: RawRunners =
        serde_yaml::from_str(text).map_err(|e| format!("runners.yaml is not valid YAML: {e}"))?;
    Ok(RunnersConfig {
        runners: raw.runners,
        flash_runner: non_empty(raw.flash_runner),
        debug_runner: non_empty(raw.debug_runner),
        board_dir: non_empty(raw.config.board_dir),
        gdb: non_empty(raw.config.gdb),
        openocd: non_empty(raw.config.openocd),
        openocd_search: raw.config.openocd_search,
        args: raw.args,
    })
}

/// Every value a runner's argv gives for `flag`, in either form west emits:
/// `--device=Cortex-M55` (inline) or `--config <path>` (separate token).
///
/// A list, not a single value: OpenOCD takes more than one `--config`, and
/// cortex-debug's `configFiles` is an array — collapsing to the first would
/// silently drop a board's second config file and produce a session that
/// attaches to half a target.
pub fn runner_arg_values(runners: &RunnersConfig, runner: &str, flag: &str) -> Vec<String> {
    let Some(argv) = runners.args.get(runner) else {
        return Vec::new();
    };
    let inline_prefix = format!("{flag}=");
    let mut values = Vec::new();
    let mut i = 0;
    while i < argv.len() {
        let arg = &argv[i];
        if let Some(value) = arg.strip_prefix(&inline_prefix) {
            if !value.is_empty() {
                values.push(value.to_string());
            }
        } else if arg == flag {
            // The next token is the value only when it isn't another flag — a
            // trailing `--config` with nothing after it must not swallow the
            // following option as if it were a path.
            if let Some(next) = argv.get(i + 1) {
                if !next.starts_with('-') {
                    values.push(next.clone());
                    i += 1;
                }
            }
        }
        i += 1;
    }
    values
}

/// The first value for `flag`, for the single-valued options (`--device`,
/// `--target`). `None` when the runner or the flag is absent.
pub fn runner_arg_value(runners: &RunnersConfig, runner: &str, flag: &str) -> Option<String> {
    runner_arg_values(runners, runner, flag).into_iter().next()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The real shape, from an E1M-AEN801 `m55_he` build.
    const AEN801: &str = r#"
runners:
- alif_flash
- jlink
flash-runner: alif_flash
debug-runner: jlink
config:
  board_dir: /sdk/zephyr/boards/alp/e1m_aen801_m55_he
  elf_file: zephyr.elf
  gdb: /zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb-py
  openocd: /zephyr-sdk-1.0.1/hosttools/usr/bin/openocd
  openocd_search:
    - /zephyr-sdk-1.0.1/hosttools/opt/openocd/share/openocd/scripts
args:
  alif_flash:
    - --device=AE822FA0E5597LS0_HE
  jlink:
    - --dt-flash=y
    - --device=Cortex-M55
    - --speed=4000
"#;

    #[test]
    fn parses_the_real_aen801_shape() {
        let c = parse_runners_config(AEN801).unwrap();
        assert_eq!(c.runners, vec!["alif_flash", "jlink"]);
        assert_eq!(c.flash_runner.as_deref(), Some("alif_flash"));
        assert_eq!(c.debug_runner.as_deref(), Some("jlink"));
        assert_eq!(
            c.gdb.as_deref(),
            Some("/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb-py")
        );
        assert_eq!(
            c.openocd.as_deref(),
            Some("/zephyr-sdk-1.0.1/hosttools/usr/bin/openocd")
        );
        assert_eq!(
            c.openocd_search,
            vec!["/zephyr-sdk-1.0.1/hosttools/opt/openocd/share/openocd/scripts"]
        );
        assert_eq!(
            c.board_dir.as_deref(),
            Some("/sdk/zephyr/boards/alp/e1m_aen801_m55_he")
        );
    }

    #[test]
    fn reads_the_inline_flag_form() {
        let c = parse_runners_config(AEN801).unwrap();
        assert_eq!(
            runner_arg_value(&c, "jlink", "--device").as_deref(),
            Some("Cortex-M55")
        );
        assert_eq!(
            runner_arg_value(&c, "alif_flash", "--device").as_deref(),
            Some("AE822FA0E5597LS0_HE")
        );
        // An absent runner and an absent flag both answer None, not a panic.
        assert_eq!(runner_arg_value(&c, "pyocd", "--target"), None);
        assert_eq!(runner_arg_value(&c, "jlink", "--nope"), None);
    }

    #[test]
    fn reads_the_split_flag_form_and_collects_every_value() {
        // OpenOCD's `--config` repeats; a board with two config files must
        // yield both, since cortex-debug's `configFiles` is a list.
        let text = r#"
runners: [openocd]
args:
  openocd:
    - --config
    - /boards/alp/board.cfg
    - --config
    - /boards/alp/extra.cfg
    - --cmd-pre-init
    - "adapter speed 4000"
"#;
        let c = parse_runners_config(text).unwrap();
        assert_eq!(
            runner_arg_values(&c, "openocd", "--config"),
            vec!["/boards/alp/board.cfg", "/boards/alp/extra.cfg"]
        );
    }

    #[test]
    fn a_dangling_flag_does_not_swallow_the_next_option() {
        // `--config` with no path must not consume `--speed` as if it were one.
        let text = "runners: [openocd]\nargs:\n  openocd:\n    - --config\n    - --speed=4000\n";
        let c = parse_runners_config(text).unwrap();
        assert!(runner_arg_values(&c, "openocd", "--config").is_empty());
    }

    #[test]
    fn a_reshaped_or_empty_file_degrades_instead_of_failing() {
        // Zephyr owns this file; an unknown-key or missing-section version must
        // resolve to "nothing known", never an error that blocks debugging.
        let c = parse_runners_config("runners: []\nsomething_new: 42\n").unwrap();
        assert_eq!(c, RunnersConfig::default());
        assert!(parse_runners_config("{}").is_ok());
        // Only unparseable YAML is an error.
        assert!(parse_runners_config("runners: [oops\n").is_err());
    }
}
