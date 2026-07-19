// SPDX-License-Identifier: Apache-2.0
//! Probe / build-tool backend plan-builders: `swd_probe` (J-Link / OpenOCD /
//! pyOCD), `zephyr_west_flash`, `baremetal_cmake_flash`, and the shape-complete
//! `cc3501e_usb_bootloader`. Each maps an entry's `flash_args` to a `FlashPlan`
//! argv with no IO; `which` is injected where a backend probes PATH.

use std::path::Path;

use super::args::{fa_bool, fa_bool_opt, fa_int, fa_int_opt, fa_str};
use super::registry::{FlashInputs, FlashPlan};

const DEFAULT_BASE: &str = "0x08000000";
const DEFAULT_JLINK_DEVICE: &str = "GD32G553MEY7TR";
const DEFAULT_JLINK_SPEED: i64 = 4000;
const JLINK_BINARIES: [&str; 2] = ["JLinkExe", "JLink"];

/// The J-Link Commander script: reset/halt, load (`loadbin`+base for `.bin`,
/// else `loadfile`), optional reset-and-go, quit-close. Pure.
pub fn jlink_commander_script(artefact: &Path, base: &str, do_reset: bool) -> String {
    let path = artefact.to_string_lossy();
    let mut lines = vec!["r".to_string(), "halt".to_string()];
    if artefact
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("bin"))
        .unwrap_or(false)
    {
        lines.push(format!("loadbin {path}, {base}"));
    } else {
        lines.push(format!("loadfile {path}"));
    }
    if do_reset {
        lines.push("r".to_string());
        lines.push("g".to_string());
    }
    lines.push("qc".to_string());
    format!("{}\n", lines.join("\n"))
}

/// `swd_probe`: J-Link (primary) / OpenOCD / pyOCD. `which` is injected.
pub fn plan_swd_probe(
    inp: &FlashInputs,
    which: impl Fn(&str) -> bool,
) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let base = fa_str(fa, "base").unwrap_or_else(|| DEFAULT_BASE.to_string());
    let do_reset = fa_bool(fa, "reset", true);
    let force_pyocd = fa_bool(fa, "use_pyocd", false);
    let force_openocd = fa_bool(fa, "use_openocd", false);
    let core = inp.core_id;
    let artefact = inp.artefact.to_string_lossy().into_owned();

    // ---- J-Link (primary; best GD32G5 flash support) ----
    let jlink = if force_pyocd || force_openocd {
        None
    } else {
        JLINK_BINARIES.into_iter().find(|n| which(n))
    };
    if let Some(jlink) = jlink {
        let device = fa_str(fa, "jlink_device").unwrap_or_else(|| DEFAULT_JLINK_DEVICE.to_string());
        let speed = fa_int(fa, "jlink_speed", DEFAULT_JLINK_SPEED).to_string();
        let script = jlink_commander_script(inp.artefact, &base, do_reset);
        let argv = vec![
            jlink.to_string(),
            "-device".to_string(),
            device.clone(),
            "-if".to_string(),
            "SWD".to_string(),
            "-speed".to_string(),
            speed,
            "-AutoConnect".to_string(),
            "1".to_string(),
            "-ExitOnError".to_string(),
            "1".to_string(),
            "-NoGui".to_string(),
            "1".to_string(),
            "-CommanderScript".to_string(),
        ];
        return Ok(FlashPlan {
            argv,
            ok_message: format!(
                "swd_probe[{core}]: GD32G553 flashed via J-Link ({device}) @ {base}"
            ),
            planning_only: false,
            jlink_script: Some(script),
        });
    }

    // ---- OpenOCD / pyOCD (need interface + target) ----
    let interface = fa_str(fa, "interface").unwrap_or_default();
    let target = fa_str(fa, "target").unwrap_or_default();
    if interface.is_empty() || target.is_empty() {
        return Err(
            "swd_probe: flash_args.interface and flash_args.target are required for the \
             openocd/pyocd path (e.g. interface=cmsis-dap, target=gd32g553) -- or install \
             SEGGER J-Link for the primary path."
                .to_string(),
        );
    }
    let openocd = !force_pyocd && which("openocd");
    let pyocd = which("pyocd");
    let argv = if openocd {
        let mut program = format!("program {artefact} verify");
        if do_reset {
            program.push_str(" reset");
        }
        program.push_str(&format!(" exit {base}"));
        vec![
            "openocd".to_string(),
            "-f".to_string(),
            format!("interface/{interface}.cfg"),
            "-f".to_string(),
            format!("target/{target}.cfg"),
            "-c".to_string(),
            program,
        ]
    } else if pyocd {
        vec![
            "pyocd".to_string(),
            "flash".to_string(),
            "--target".to_string(),
            target,
            "--base-address".to_string(),
            base.clone(),
            artefact,
        ]
    } else {
        return Err(
            "swd_probe: no flash tool found -- install SEGGER J-Link (preferred), or \
             `openocd`, or `pyocd`."
                .to_string(),
        );
    };
    Ok(FlashPlan {
        argv,
        ok_message: format!("swd_probe[{core}]: GD32G553 flashed @ {base}"),
        planning_only: false,
        jlink_script: None,
    })
}

/// `zephyr_west_flash`: `west flash --build-dir <d> --runner <r> [--erase]
/// [--hex-file <h>]`. `runner` is required.
pub fn plan_zephyr_west_flash(inp: &FlashInputs) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let runner = fa_str(fa, "runner").ok_or_else(|| {
        "zephyr_west_flash: flash_args.runner is required (e.g. openocd, jlink, pyocd, nrfjprog)."
            .to_string()
    })?;
    let build_dir = fa_str(fa, "build_dir").unwrap_or_else(|| zephyr_build_dir(inp.artefact));
    let mut argv = vec![
        "west".to_string(),
        "flash".to_string(),
        "--build-dir".to_string(),
        build_dir,
        "--runner".to_string(),
        runner.clone(),
    ];
    if fa_bool(fa, "erase", false) {
        argv.push("--erase".to_string());
    }
    if let Some(hex) = fa_str(fa, "hex_file") {
        argv.push("--hex-file".to_string());
        argv.push(hex);
    }
    Ok(FlashPlan {
        argv,
        ok_message: format!(
            "zephyr_west_flash[{}]: programmed via {runner}",
            inp.core_id
        ),
        planning_only: false,
        jlink_script: None,
    })
}

/// The Zephyr build dir: `flash_args.build_dir` when set, else derived from the
/// artefact — `parent.parent` for a `zephyr.*` basename, else `parent`.
fn zephyr_build_dir(artefact: &Path) -> String {
    let is_zephyr = matches!(
        artefact.file_name().and_then(|n| n.to_str()),
        Some("zephyr.elf" | "zephyr.bin" | "zephyr.hex" | "zephyr.uf2")
    );
    let dir = if is_zephyr {
        artefact.parent().and_then(Path::parent)
    } else {
        artefact.parent()
    };
    dir.unwrap_or_else(|| Path::new(""))
        .to_string_lossy()
        .into_owned()
}

/// `baremetal_cmake_flash`: `cmake --build <d> --target <t> [--config <c>] [-j N]`.
pub fn plan_baremetal_cmake_flash(inp: &FlashInputs) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let build_dir = fa_str(fa, "build_dir").unwrap_or_else(|| {
        inp.artefact
            .parent()
            .unwrap_or_else(|| Path::new(""))
            .to_string_lossy()
            .into_owned()
    });
    let target = fa_str(fa, "target").unwrap_or_else(|| "flash".to_string());
    let mut argv = vec![
        "cmake".to_string(),
        "--build".to_string(),
        build_dir,
        "--target".to_string(),
        target.clone(),
    ];
    if let Some(config) = fa_str(fa, "config") {
        argv.push("--config".to_string());
        argv.push(config);
    }
    if let Some(jobs) = fa_int_opt(fa, "jobs") {
        argv.push("-j".to_string());
        argv.push(jobs.to_string());
    }
    Ok(FlashPlan {
        argv,
        ok_message: format!(
            "baremetal_cmake_flash[{}]: target `{target}` ok",
            inp.core_id
        ),
        planning_only: false,
        jlink_script: None,
    })
}

/// `cc3501e_usb_bootloader`: shape-complete but the vendor CLI is not public.
/// Dry-run returns the provisional argv; the real (non-dry) path is a graceful
/// failure. `device` + `mode` (`otp_program`|`ram_load`) are required.
pub fn plan_cc3501e_usb_bootloader(inp: &FlashInputs) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let device = fa_str(fa, "device").unwrap_or_default();
    let mode = fa_str(fa, "mode").unwrap_or_default();
    if device.is_empty() || mode.is_empty() {
        return Err(
            "cc3501e_usb_bootloader: flash_args.device and flash_args.mode are required \
                    (mode: otp_program | ram_load)."
                .to_string(),
        );
    }
    if mode != "otp_program" && mode != "ram_load" {
        return Err(format!(
            "cc3501e_usb_bootloader: unknown mode '{mode}' -- expected otp_program | ram_load."
        ));
    }
    let speed = fa_int(fa, "speed", 921600);
    let verify = fa_bool_opt(fa, "verify").unwrap_or(mode == "otp_program");
    // Provisional tool name (preferred candidate); the real path never spawns.
    let mut argv = vec![
        "cc3501e-flasher".to_string(),
        "--device".to_string(),
        device,
        "--mode".to_string(),
        mode,
        "--speed".to_string(),
        speed.to_string(),
        "--image".to_string(),
        inp.artefact.to_string_lossy().into_owned(),
    ];
    if verify {
        argv.push("--verify".to_string());
    }
    if inp.dry_run {
        Ok(FlashPlan {
            argv,
            ok_message: format!(
                "cc3501e_usb_bootloader[{}]: CLI shape provisional (dry-run)",
                inp.core_id
            ),
            planning_only: false,
            jlink_script: None,
        })
    } else {
        Err(
            "cc3501e-flasher CLI is not yet public; this backend lands when the upstream tool \
             stabilises. Use the bench warm-program recipe in docs/cc3501e-production.md for now."
                .to_string(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_yaml::Value;

    fn inputs<'a>(artefact: &'a Path, fa: &'a Value, dry_run: bool) -> FlashInputs<'a> {
        FlashInputs {
            artefact,
            flash_args: fa,
            core_id: "core",
            sku: "E1M-V2N101",
            dry_run,
            force_confirm: false,
        }
    }

    fn yaml_val(s: &str) -> Value {
        serde_yaml::from_str(s).unwrap()
    }

    #[test]
    fn jlink_script_bin_vs_elf_and_reset() {
        let bin = jlink_commander_script(Path::new("/f/app.bin"), "0x08000000", true);
        assert!(bin.contains("loadbin /f/app.bin, 0x08000000"));
        assert!(bin.trim_end().ends_with("qc"));
        // reset=true appends r + g before qc.
        let lines: Vec<&str> = bin.lines().collect();
        assert_eq!(
            lines,
            vec![
                "r",
                "halt",
                "loadbin /f/app.bin, 0x08000000",
                "r",
                "g",
                "qc"
            ]
        );
        let elf = jlink_commander_script(Path::new("/f/app.elf"), "0x0", false);
        assert!(elf.contains("loadfile /f/app.elf"));
        assert!(!elf.contains("loadbin"));
        assert_eq!(
            elf.lines().collect::<Vec<_>>(),
            vec!["r", "halt", "loadfile /f/app.elf", "qc"]
        );
    }

    #[test]
    fn swd_jlink_argv_defaults() {
        let fa = Value::Null;
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap();
        assert_eq!(plan.argv[0], "JLinkExe");
        for flag in [
            "-device",
            "-if",
            "SWD",
            "-speed",
            "-AutoConnect",
            "-ExitOnError",
            "-NoGui",
            "-CommanderScript",
        ] {
            assert!(plan.argv.iter().any(|a| a == flag), "missing {flag}");
        }
        // defaults: device + speed.
        let dev_i = plan.argv.iter().position(|a| a == "-device").unwrap();
        assert_eq!(plan.argv[dev_i + 1], "GD32G553MEY7TR");
        let sp_i = plan.argv.iter().position(|a| a == "-speed").unwrap();
        assert_eq!(plan.argv[sp_i + 1], "4000");
        assert!(plan.jlink_script.is_some());
    }

    #[test]
    fn swd_use_pyocd_skips_jlink_and_needs_interface_target() {
        let fa = yaml_val("use_pyocd: true");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        // even with JLinkExe present, use_pyocd forces the openocd/pyocd path,
        // which errors without interface + target.
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("interface") && err.contains("target"));
    }

    #[test]
    fn zephyr_runner_required_and_build_dir_fallback() {
        let art = Path::new("/b/m33_sm-zephyr/zephyr/zephyr.elf");
        let no_runner = Value::Null;
        let inp = inputs(art, &no_runner, false);
        assert!(
            plan_zephyr_west_flash(&inp)
                .unwrap_err()
                .contains("runner is required")
        );

        let fa = yaml_val("runner: openocd\nerase: true\nhex_file: signed.hex");
        let inp = inputs(art, &fa, false);
        let plan = plan_zephyr_west_flash(&inp).unwrap();
        // build_dir fallback = parent.parent for zephyr.elf.
        let bd_i = plan.argv.iter().position(|a| a == "--build-dir").unwrap();
        assert!(
            plan.argv[bd_i + 1]
                .replace('\\', "/")
                .ends_with("/b/m33_sm-zephyr")
        );
        assert!(plan.argv.contains(&"--erase".to_string()));
        assert!(plan.argv.contains(&"--hex-file".to_string()));
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "--runner" && w[1] == "openocd")
        );
    }

    #[test]
    fn cmake_defaults_and_extras() {
        let art = Path::new("/b/build/app.elf");
        let fa = Value::Null;
        let inp = inputs(art, &fa, false);
        let plan = plan_baremetal_cmake_flash(&inp).unwrap();
        // default target = flash.
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "--target" && w[1] == "flash")
        );
        let fa = yaml_val("target: program\nconfig: Release\njobs: 4");
        let inp = inputs(art, &fa, false);
        let plan = plan_baremetal_cmake_flash(&inp).unwrap();
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "--target" && w[1] == "program")
        );
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "--config" && w[1] == "Release")
        );
        assert!(plan.argv.windows(2).any(|w| w[0] == "-j" && w[1] == "4"));
    }

    #[test]
    fn cc3501e_validation_and_dry_vs_real() {
        let art = Path::new("/b/coproc.bin");
        // device + mode required.
        let inp = inputs(art, &Value::Null, true);
        assert!(
            plan_cc3501e_usb_bootloader(&inp)
                .unwrap_err()
                .contains("required")
        );
        // unknown mode.
        let fa = yaml_val("device: /dev/ttyACM0\nmode: bogus");
        let inp = inputs(art, &fa, true);
        assert!(
            plan_cc3501e_usb_bootloader(&inp)
                .unwrap_err()
                .contains("unknown mode")
        );
        // dry-run otp_program -> ok, verify default true.
        let fa = yaml_val("device: /dev/ttyACM0\nmode: otp_program");
        let inp = inputs(art, &fa, true);
        let plan = plan_cc3501e_usb_bootloader(&inp).unwrap();
        assert_eq!(plan.argv[0], "cc3501e-flasher");
        assert!(plan.argv.contains(&"--verify".to_string()));
        // ram_load default verify false.
        let fa = yaml_val("device: /dev/ttyACM0\nmode: ram_load");
        let inp = inputs(art, &fa, true);
        let plan = plan_cc3501e_usb_bootloader(&inp).unwrap();
        assert!(!plan.argv.contains(&"--verify".to_string()));
        // real (non-dry) path -> not-yet-public Err.
        let inp = inputs(art, &fa, false);
        assert!(
            plan_cc3501e_usb_bootloader(&inp)
                .unwrap_err()
                .contains("not yet public")
        );
    }
}
