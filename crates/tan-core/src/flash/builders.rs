// SPDX-License-Identifier: Apache-2.0
//! Probe / build-tool backend plan-builders: `swd_probe` (J-Link / OpenOCD /
//! pyOCD), `zephyr_west_flash`, and `baremetal_cmake_flash`. Each maps an
//! entry's `flash_args` to a `FlashPlan` argv with no IO; `which` is injected
//! where a backend probes PATH.

use std::path::Path;

use serde_yaml::Value;

use super::args::{
    PENDING_PLACEHOLDER, fa_bool_checked, fa_int_checked, fa_str, is_pending_placeholder,
};
use super::registry::{FlashInputs, FlashPlan};

const DEFAULT_BASE: &str = "0x08000000";
const DEFAULT_JLINK_DEVICE: &str = "GD32G553MEY7TR";
const DEFAULT_JLINK_SPEED: i64 = 4000;
const JLINK_BINARIES: [&str; 2] = ["JLinkExe", "JLink"];

/// Raw `flash_args` sub-key lookup (mapping only). Duplicated from
/// `args::fa_get` -- which is private to `args.rs` -- because
/// `fa_str_checked` below needs the raw `Value`, not one of the typed
/// projections `args.rs` exposes.
fn fa_raw<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.as_mapping()?
        .iter()
        .find(|(key, _)| key.as_str() == Some(k))
        .map(|(_, val)| val)
}

/// Strict `flash_args` string accessor for fields where silently falling
/// back to a baked-in default is dangerous (a flash base address; an
/// OpenOCD interface/target name that gets interpolated into a spawned
/// command). `fa_str` (args.rs) treats ANY non-string value -- including
/// the bare YAML Integer an unquoted `base: 0x08000000` resolves to -- as
/// "absent", so the caller silently substitutes DEFAULT_BASE and programs
/// real silicon at the wrong address with no warning. This accessor
/// instead returns `Ok(None)` only when the key is genuinely absent/null/
/// empty (caller may apply its default); round-trips a bare numeric scalar
/// back into a string (hex for an address field, decimal otherwise); and
/// hard-errors on any other shape (bool/mapping/sequence) instead of
/// defaulting.
///
/// It also hard-errors on the pending placeholder (#222), which is the one
/// place this family and the tolerant [`fa_str`] deliberately disagree: there
/// `TBD` reads as absent because the callers' defaults are safe, here it must
/// not, because "the default is dangerous" is this accessor's whole premise.
fn fa_str_checked(v: &Value, k: &str, as_hex_address: bool) -> Result<Option<String>, String> {
    let Some(raw) = fa_raw(v, k) else {
        return Ok(None);
    };
    if raw.is_null() {
        return Ok(None);
    }
    if let Some(s) = raw.as_str() {
        // #222: the pending placeholder is an ERROR here, not "absent". This
        // accessor exists precisely for the fields where a silent fallback to a
        // baked-in default is dangerous, so reading an unfilled `jlink_device`
        // as absent would flash with `DEFAULT_JLINK_DEVICE` and an unfilled
        // `base` would program at `DEFAULT_BASE` — while the manifest was
        // saying "not yet known". The tolerant `fa_str` reads it as absent
        // because its callers' defaults are safe; the strict `_checked` family
        // (`fa_bool_checked`/`fa_int_checked` already do this) refuses.
        if is_pending_placeholder(s) {
            return Err(format!(
                "flash_args.{k} is still the {PENDING_PLACEHOLDER} placeholder -- the hardware \
                 configuration for this field has not been filled in yet, and this plans a real \
                 flash write. Fill it in in the SDK manifest."
            ));
        }
        return Ok((!s.is_empty()).then(|| s.to_string()));
    }
    if let Some(n) = raw.as_u64() {
        return Ok(Some(if as_hex_address {
            format!("0x{n:08X}")
        } else {
            n.to_string()
        }));
    }
    if let Some(n) = raw.as_i64() {
        // `n as u64` sign-extends a negative value (`-8` -> a cast) into
        // `0xFFFFFFFFFFFFFFF8`, which `validate_address` (a pure charset
        // check) then ACCEPTS as a plausible-looking hex address and the
        // J-Link/OpenOCD command interpolates verbatim -- a typo'd negative
        // `base` must hard-error, never invent an address.
        if n < 0 {
            return Err(format!(
                "flash_args.{k} = {n} is negative; refusing to interpret it as an \
                 address/count -- this plans a real flash write."
            ));
        }
        return Ok(Some(if as_hex_address {
            format!("0x{n:08X}")
        } else {
            n.to_string()
        }));
    }
    Err(format!(
        "flash_args.{k} must be a quoted string (got {raw:?}); refusing to silently fall back \
         to a default -- this plans a real flash write."
    ))
}

/// Reject anything that is not a plain identifier, or a `/`-separated path of
/// plain identifier segments: no `..`, no absolute/rooted/drive-relative
/// forms, no whitespace, no Tcl/shell metacharacters. `interface`/`target`
/// are interpolated verbatim into an OpenOCD `-f <name>.cfg` path and a `-c`
/// Tcl command string (see `plan_swd_probe` below); an unrestricted value is
/// a path-traversal + Tcl-injection primitive into a process that is
/// routinely run with device-flashing privileges.
///
/// OpenOCD ships interface configs in subdirectories (e.g.
/// `interface: ftdi/olimex-arm-usb-ocd-h`), so a single-segment-only guard
/// hard-errored every real FTDI-adapter config. `is_plain_relative` (the
/// shared path-shape guard) accepts exactly the multi-segment-but-not-
/// escaping shape this needs; the per-segment charset check on top of it
/// still closes the Tcl/shell-metacharacter hole `is_plain_relative` alone
/// doesn't -- it only rejects `..`/absolute/rooted forms, not `;`/`[`/`]`/
/// newlines inside an otherwise-plain path component.
fn validate_identifier(s: &str, field: &str) -> Result<(), String> {
    let ok = crate::path_guard::is_plain_relative(Path::new(s))
        && s.split('/').all(|seg| {
            !seg.is_empty()
                && seg
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        });
    if ok {
        Ok(())
    } else {
        Err(format!(
            "flash_args.{field} = {s:?} is not a plain identifier or '/'-separated path of \
             plain identifiers (letters, digits, '-', '_' per segment) -- refusing to \
             interpolate it into a spawned command / OpenOCD Tcl script."
        ))
    }
}

/// Validate a flash base address is purely hex/decimal digits (with an
/// optional `0x`/`0X` prefix). `base` is interpolated verbatim into a
/// J-Link Commander script line (`jlink_commander_script`) and an OpenOCD
/// `-c` Tcl command string (`plan_swd_probe`) -- both line/command-oriented
/// interpreters, so a newline (or `;`, `[`, `]`, ...) inside `base` is a
/// command-injection primitive that runs arbitrary extra commands against
/// whatever silicon is attached.
fn validate_address(s: &str, field: &str) -> Result<(), String> {
    let digits = s
        .strip_prefix("0x")
        .or_else(|| s.strip_prefix("0X"))
        .unwrap_or(s);
    if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_hexdigit()) {
        Ok(())
    } else {
        Err(format!(
            "flash_args.{field} = {s:?} is not a plain hex/decimal address -- refusing to \
             interpolate it into a J-Link/OpenOCD command."
        ))
    }
}

/// Whether an artefact is a raw binary (needs an explicit load address), as
/// opposed to ELF/HEX which carry their own. Shared by the J-Link script
/// (`loadbin`+base vs `loadfile`) and the OpenOCD/pyOCD base-address
/// handling in `plan_swd_probe` -- passing a load offset for a non-`.bin`
/// artefact shifts every section by that offset and writes outside the
/// intended flash region.
fn is_raw_bin(artefact: &Path) -> bool {
    artefact
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("bin"))
        .unwrap_or(false)
}

/// The J-Link Commander script: reset/halt, load (`loadbin`+base for `.bin`,
/// else `loadfile`), optional reset-and-go, quit-close. Pure.
pub fn jlink_commander_script(artefact: &Path, base: &str, do_reset: bool) -> String {
    let path = artefact.to_string_lossy();
    let mut lines = vec!["r".to_string(), "halt".to_string()];
    if is_raw_bin(artefact) {
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
    // `fa_str_checked`, not `fa_str`: see its doc comment -- a bare YAML
    // Integer (an unquoted `base: 0x08000000`) must not silently read as
    // "absent" and fall back to DEFAULT_BASE. `validate_address` then
    // closes the newline-injection hole into the J-Link script / OpenOCD
    // `-c` string.
    let base = match fa_str_checked(fa, "base", true)? {
        Some(b) => {
            validate_address(&b, "base")?;
            b
        }
        None => DEFAULT_BASE.to_string(),
    };
    // `fa_bool_checked`, not `fa_bool`: a quoted `reset: "false"` reads as
    // "absent" via `Value::as_bool` too, so `fa_bool` would silently apply
    // the default `true` -- the OPPOSITE of what was written -- and reset
    // the target when the manifest asked it not to.
    let do_reset = fa_bool_checked(fa, "reset")?.unwrap_or(true);
    // `fa_bool_checked`, not a tolerant `fa_bool`: a quoted `use_pyocd:
    // "true"` / `use_openocd: "true"` reads as "absent" via `Value::as_bool`
    // too, so a tolerant reader would silently apply the `false` default and
    // route the flash through the J-Link path when the manifest demanded a
    // DIFFERENT probe tool entirely.
    let force_pyocd = fa_bool_checked(fa, "use_pyocd")?.unwrap_or(false);
    let force_openocd = fa_bool_checked(fa, "use_openocd")?.unwrap_or(false);
    let core = inp.core_id;
    let artefact = inp.artefact.to_string_lossy().into_owned();
    let is_bin = is_raw_bin(inp.artefact);

    // ---- J-Link (primary; best GD32G5 flash support) ----
    // `--dry-run` is documented (cli.rs, `tool_gate` in mod.rs) to bypass
    // the required-tool PATH gate entirely; without the `inp.dry_run ||`
    // bypass here this inner `which()` probe hard-failed a dry run on any
    // box without J-Link/OpenOCD/pyOCD installed, making `tan flash
    // --dry-run` host-dependent instead of a pure preview.
    let jlink = if force_pyocd || force_openocd {
        None
    } else if inp.dry_run {
        Some(JLINK_BINARIES[0])
    } else {
        JLINK_BINARIES.into_iter().find(|n| which(n))
    };
    if let Some(jlink) = jlink {
        let device = fa_str_checked(fa, "jlink_device", false)?
            .unwrap_or_else(|| DEFAULT_JLINK_DEVICE.to_string());
        // `fa_int_checked`, not `fa_int`: a quoted `jlink_speed: "8000"`
        // reads as "absent" via `Value::as_i64` too, so `fa_int` would
        // silently apply the DEFAULT_JLINK_SPEED default with no warning.
        let speed = fa_int_checked(fa, "jlink_speed")?
            .unwrap_or(DEFAULT_JLINK_SPEED)
            .to_string();
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
    let interface = fa_str_checked(fa, "interface", false)?.unwrap_or_default();
    let target = fa_str_checked(fa, "target", false)?.unwrap_or_default();
    if interface.is_empty() || target.is_empty() {
        return Err(
            "swd_probe: flash_args.interface and flash_args.target are required for the \
             openocd/pyocd path (e.g. interface=cmsis-dap, target=gd32g553) -- or install \
             SEGGER J-Link for the primary path."
                .to_string(),
        );
    }
    // `interface`/`target` are interpolated verbatim into an OpenOCD
    // `-f <name>.cfg` path and a `-c` Tcl command string below; an
    // unrestricted charset is a path-traversal + Tcl-injection primitive
    // into a process that runs with device-flashing privileges.
    validate_identifier(&interface, "interface")?;
    validate_identifier(&target, "target")?;
    // Same `--dry-run` bypass as the J-Link probe above.
    let openocd = !force_pyocd && (inp.dry_run || which("openocd"));
    let pyocd = !force_openocd && (inp.dry_run || which("pyocd"));
    let argv = if openocd {
        let mut program = format!("program {artefact} verify");
        if do_reset {
            program.push_str(" reset");
        }
        // `base` is a load OFFSET, meaningful only for a raw `.bin`; ELF/HEX
        // carry their own addresses and OpenOCD's `program` proc adds a
        // trailing address to them, so passing it unconditionally (as
        // before) shifted every ELF/HEX section by `base`.
        if is_bin {
            program.push_str(&format!(" exit {base}"));
        } else {
            program.push_str(" exit");
        }
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
        let mut argv = vec![
            "pyocd".to_string(),
            "flash".to_string(),
            "--target".to_string(),
            target,
        ];
        // pyOCD's --base-address is documented binary-only; passing it for
        // an ELF/HEX is meaningless at best and a wrong-address write at
        // worst (the format already embeds its own load addresses).
        if is_bin {
            argv.push("--base-address".to_string());
            argv.push(base.clone());
        }
        argv.push(artefact);
        argv
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

/// `zephyr_west_flash`: `west flash --build-dir <d> [--runner <r>] [--erase]
/// [--hex-file <h>]`. `runner` is OPTIONAL — when absent, `--runner` is omitted
/// and `west flash` falls back to the board.cmake default runner (e.g. AEN's
/// `alif_flash`). Mirrors the SDK `zephyr_west_flash` backend contract.
pub fn plan_zephyr_west_flash(inp: &FlashInputs) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let runner = fa_str(fa, "runner");
    let build_dir = fa_str(fa, "build_dir").unwrap_or_else(|| zephyr_build_dir(inp.artefact));
    let mut argv = vec![
        "west".to_string(),
        "flash".to_string(),
        "--build-dir".to_string(),
        build_dir,
    ];
    if let Some(runner) = &runner {
        argv.push("--runner".to_string());
        argv.push(runner.clone());
    }
    // `fa_bool_checked`, not a tolerant `fa_bool`: a quoted `erase: "true"`
    // reads as "absent" via `Value::as_bool` too, so a tolerant reader would
    // silently apply the `false` default and drop `--erase` from the `west
    // flash` argv -- the device gets flashed WITHOUT the erase the manifest
    // asked for.
    if fa_bool_checked(fa, "erase")?.unwrap_or(false) {
        argv.push("--erase".to_string());
    }
    if let Some(hex) = fa_str(fa, "hex_file") {
        argv.push("--hex-file".to_string());
        argv.push(hex);
    }
    Ok(FlashPlan {
        argv,
        ok_message: format!(
            "zephyr_west_flash[{}]: programmed via {}",
            inp.core_id,
            runner.as_deref().unwrap_or("board-default runner")
        ),
        planning_only: false,
        jlink_script: None,
    })
}

/// The Zephyr build dir: `flash_args.build_dir` when set, else derived from
/// the artefact — `parent.parent` when the artefact sits directly in a
/// `zephyr/` subdirectory, else `parent`.
///
/// Previously this checked the artefact's BASENAME against a fixed
/// `zephyr.{elf,bin,hex,uf2}` allowlist, which missed MCUboot-signed
/// (`zephyr.signed.hex`) and sysbuild (`merged.hex`) outputs -- both of
/// which still land in `<build_dir>/zephyr/`, just under a different name.
/// Those artefacts fell through to the `else` arm, landing one directory
/// too deep, and `west flash --build-dir <that>` failed with no
/// CMakeCache.txt there. Checking the PARENT DIRECTORY NAME instead covers
/// every artefact name Zephyr (or a signing/merge step) can produce there.
fn zephyr_build_dir(artefact: &Path) -> String {
    let in_zephyr_subdir = artefact
        .parent()
        .and_then(Path::file_name)
        .and_then(|n| n.to_str())
        .map(|n| n.eq_ignore_ascii_case("zephyr"))
        .unwrap_or(false);
    let dir = if in_zephyr_subdir {
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
    // `fa_int_checked`, not a tolerant `fa_int_opt`: a quoted `jobs: "4"`
    // reads as "absent" via `Value::as_i64` too, so a tolerant reader would
    // silently drop `-j N` with no warning, leaving the build tool's own
    // default parallelism instead of what the manifest asked for.
    if let Some(jobs) = fa_int_checked(fa, "jobs")? {
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
    fn zephyr_runner_optional_and_build_dir_fallback() {
        let art = Path::new("/b/m33_sm-zephyr/zephyr/zephyr.elf");
        // runner absent -> succeeds, omits --runner, defers to board default.
        let no_runner = Value::Null;
        let inp = inputs(art, &no_runner, false);
        let plan = plan_zephyr_west_flash(&inp).unwrap();
        assert!(!plan.argv.contains(&"--runner".to_string()));
        assert!(plan.argv.contains(&"flash".to_string()));

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
    fn base_numeric_yaml_scalar_is_accepted_not_silently_defaulted() {
        // Unquoted `base: 0x20000000` resolves to a YAML Integer, not a
        // String. `fa_str` would read that as "absent" and the caller would
        // silently fall back to DEFAULT_BASE (0x08000000) -- a wrong-address
        // flash write on real silicon with no warning.
        let fa = yaml_val("base: 0x20000000");
        assert!(
            matches!(fa_raw(&fa, "base"), Some(Value::Number(_))),
            "sanity: unquoted base must parse as a YAML Number"
        );
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap();
        assert!(
            plan.ok_message.contains("0x20000000"),
            "expected the requested base, got: {}",
            plan.ok_message
        );
        assert!(!plan.ok_message.contains(DEFAULT_BASE));
    }

    #[test]
    fn base_with_embedded_newline_is_rejected_not_interpolated() {
        // A newline in `base` would inject an extra line into the J-Link
        // Commander script / an extra Tcl command into OpenOCD's `-c`
        // string -- must hard-error instead of running it against hardware.
        let fa = yaml_val("base: \"0x08000000\\nerase\"");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("base"));
    }

    #[test]
    fn base_negative_int_is_rejected_not_sign_extended() {
        // Regression: `n as u64` used to sign-extend -8 into
        // 0xFFFFFFFFFFFFFFF8, which `validate_address` then ACCEPTED as a
        // plausible-looking address -- a typo'd negative base must hard-error,
        // never silently invent an address to flash.
        let fa = yaml_val("base: -8");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("base") && err.contains("negative"));
        assert!(!err.contains("FFFFFFFF"));
    }

    #[test]
    fn dry_run_bypasses_missing_tool_gate_for_jlink_and_openocd() {
        // `--dry-run` is documented to bypass the required-tool PATH gate
        // (mod.rs `tool_gate`); plan_swd_probe's own internal `which()`
        // probe used to ignore that and hard-fail whenever no probe tool
        // was actually installed.
        let art = Path::new("/b/fw.bin");
        let fa = Value::Null;
        let inp = inputs(art, &fa, true);
        let plan = plan_swd_probe(&inp, |_| false).unwrap();
        assert_eq!(plan.argv[0], "JLinkExe");

        let fa = yaml_val("use_openocd: true\ninterface: cmsis-dap\ntarget: gd32g553");
        let inp = inputs(art, &fa, true);
        let plan = plan_swd_probe(&inp, |_| false).unwrap();
        assert_eq!(plan.argv[0], "openocd");
    }

    #[test]
    fn openocd_omits_base_offset_for_non_bin_artefact() {
        let fa = yaml_val("use_openocd: true\ninterface: cmsis-dap\ntarget: gd32g553");

        let art = Path::new("/b/app.elf");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "openocd").unwrap();
        let c_i = plan.argv.iter().position(|a| a == "-c").unwrap();
        assert!(
            plan.argv[c_i + 1].ends_with(" exit"),
            "elf must not carry a base offset: {}",
            plan.argv[c_i + 1]
        );

        let art_bin = Path::new("/b/app.bin");
        let inp = inputs(art_bin, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "openocd").unwrap();
        let c_i = plan.argv.iter().position(|a| a == "-c").unwrap();
        assert!(plan.argv[c_i + 1].contains("exit 0x08000000"));
    }

    #[test]
    fn openocd_rejects_unsafe_interface_and_target() {
        let art = Path::new("/b/fw.bin");
        let fa = yaml_val("use_openocd: true\ninterface: \"../../../tmp/evil\"\ntarget: gd32g553");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "openocd").unwrap_err();
        assert!(err.contains("interface"));
    }

    #[test]
    fn openocd_accepts_subdirectory_interface_config() {
        // OpenOCD ships interface configs in subdirectories, e.g. the real
        // `interface/ftdi/olimex-arm-usb-ocd-h.cfg` -- a single-segment-only
        // guard hard-errored every FTDI-adapter manifest.
        let art = Path::new("/b/fw.bin");
        let fa =
            yaml_val("use_openocd: true\ninterface: ftdi/olimex-arm-usb-ocd-h\ntarget: gd32g553");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "openocd").unwrap();
        let f_i = plan
            .argv
            .iter()
            .position(|a| a == "interface/ftdi/olimex-arm-usb-ocd-h.cfg");
        assert!(f_i.is_some(), "got argv: {:?}", plan.argv);
    }

    #[test]
    fn openocd_still_rejects_metacharacters_inside_a_path_segment() {
        // `is_plain_relative` alone would accept this (no `..`, not
        // absolute) -- the per-segment charset check must still close the
        // Tcl/shell-injection hole.
        let art = Path::new("/b/fw.bin");
        let fa = yaml_val("use_openocd: true\ninterface: \"ftdi/oli;rm -rf /\"\ntarget: gd32g553");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "openocd").unwrap_err();
        assert!(err.contains("interface"));
    }

    #[test]
    fn reset_quoted_string_is_rejected_not_silently_true() {
        // Regression: `reset: "false"` (quoted) used to read as absent via
        // `Value::as_bool`, so `fa_bool` silently applied the `true`
        // default -- resetting the target when the manifest asked it not to.
        let fa = yaml_val("reset: \"false\"");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("reset"));

        // a bare (unquoted) bool still works.
        let fa = yaml_val("reset: false");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap();
        assert_eq!(
            plan.jlink_script.unwrap().lines().last().unwrap(),
            "qc",
            "reset: false must not append the r/g reset lines"
        );
    }

    #[test]
    fn jlink_speed_quoted_string_is_rejected_not_silently_defaulted() {
        // Regression: `jlink_speed: "8000"` (quoted) used to read as absent
        // via `Value::as_i64`, so `fa_int` silently applied DEFAULT_JLINK_SPEED
        // (4000) with no warning.
        let fa = yaml_val("jlink_speed: \"8000\"");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("jlink_speed"));

        // a bare (unquoted) int still works and is honoured, not defaulted.
        let fa = yaml_val("jlink_speed: 9600");
        let inp = inputs(art, &fa, false);
        let plan = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap();
        let sp_i = plan.argv.iter().position(|a| a == "-speed").unwrap();
        assert_eq!(plan.argv[sp_i + 1], "9600");
    }

    #[test]
    fn zephyr_build_dir_covers_signed_and_merged_artefact_names() {
        // Both still land at <build_dir>/zephyr/<name>, just not under one
        // of the fixed zephyr.{elf,bin,hex,uf2} basenames the old basename
        // allowlist required -- the old code landed one directory too deep
        // and `west flash --build-dir` failed with no CMakeCache.txt there.
        use std::path::PathBuf;
        for name in ["zephyr.signed.hex", "merged.hex"] {
            let art = PathBuf::from(format!("/b/proj-zephyr/zephyr/{name}"));
            let fa = Value::Null;
            let inp = inputs(art.as_path(), &fa, false);
            let plan = plan_zephyr_west_flash(&inp).unwrap();
            let bd_i = plan.argv.iter().position(|a| a == "--build-dir").unwrap();
            assert!(
                plan.argv[bd_i + 1]
                    .replace('\\', "/")
                    .ends_with("/b/proj-zephyr"),
                "{name}: got {}",
                plan.argv[bd_i + 1]
            );
        }
    }

    #[test]
    fn erase_quoted_string_is_rejected_not_silently_dropped() {
        // Regression: `erase: "true"` (quoted) used to read as absent via
        // `Value::as_bool`, so a tolerant `fa_bool` silently applied the
        // `false` default -- flashing WITHOUT the erase the manifest asked
        // for, with no warning.
        let fa = yaml_val("erase: \"true\"");
        let art = Path::new("/b/m33-zephyr/zephyr/zephyr.elf");
        let inp = inputs(art, &fa, false);
        let err = plan_zephyr_west_flash(&inp).unwrap_err();
        assert!(err.contains("erase"));

        // a bare (unquoted) bool still works and is honoured.
        let fa = yaml_val("erase: true");
        let inp = inputs(art, &fa, false);
        let plan = plan_zephyr_west_flash(&inp).unwrap();
        assert!(plan.argv.contains(&"--erase".to_string()));
    }

    #[test]
    fn use_openocd_and_use_pyocd_quoted_string_is_rejected_not_silently_false() {
        // Regression: `use_openocd: "true"` / `use_pyocd: "true"` (quoted)
        // used to read as absent via `Value::as_bool`, so a tolerant
        // `fa_bool` silently applied the `false` default -- routing the
        // flash through the J-Link path when the manifest demanded a
        // DIFFERENT probe tool entirely.
        let art = Path::new("/b/fw.bin");
        for key in ["use_openocd", "use_pyocd"] {
            let fa = yaml_val(&format!("{key}: \"true\""));
            let inp = inputs(art, &fa, false);
            let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
            assert!(err.contains(key), "{key}: got {err}");
        }

        // a bare (unquoted) use_openocd: true still forces the openocd/pyocd
        // path (errors here only because interface/target are absent).
        let fa = yaml_val("use_openocd: true");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "JLinkExe").unwrap_err();
        assert!(err.contains("interface") && err.contains("target"));
    }

    #[test]
    fn use_openocd_true_hard_errors_when_only_pyocd_present() {
        // Regression: `pyocd` was computed without a `!force_openocd` guard
        // (unlike `openocd`, which has `!force_pyocd`), so `use_openocd:
        // true` on a host with pyocd present but openocd absent silently
        // fell through to the pyocd branch and flashed via the WRONG probe
        // tool instead of hard-erroring like the symmetric `use_pyocd: true`
        // case does when pyocd is missing.
        let fa = yaml_val("use_openocd: true\ninterface: cmsis-dap\ntarget: gd32g553");
        let art = Path::new("/b/fw.bin");
        let inp = inputs(art, &fa, false);
        let err = plan_swd_probe(&inp, |t| t == "pyocd").unwrap_err();
        assert!(err.contains("no flash tool found"), "got {err}");
    }

    #[test]
    fn cmake_jobs_quoted_string_is_rejected_not_silently_omitted() {
        // Regression: `jobs: "4"` (quoted) used to read as absent via
        // `Value::as_i64`, so a tolerant `fa_int_opt` silently dropped
        // `-j N` with no warning.
        let art = Path::new("/b/build/app.elf");
        let fa = yaml_val("jobs: \"4\"");
        let inp = inputs(art, &fa, false);
        let err = plan_baremetal_cmake_flash(&inp).unwrap_err();
        assert!(err.contains("jobs"));

        // a bare (unquoted) int still works and is honoured.
        let fa = yaml_val("jobs: 4");
        let inp = inputs(art, &fa, false);
        let plan = plan_baremetal_cmake_flash(&inp).unwrap();
        assert!(plan.argv.windows(2).any(|w| w[0] == "-j" && w[1] == "4"));
    }

    /// tan-cli#222, the half the `fa_str` fix does not reach. `plan_swd_probe`
    /// reads FOUR fields through `fa_str_checked`, a second accessor whose
    /// string branch carried the same empty-only filter — so `TBD` survived it
    /// and a real flasher was planned against the literal placeholder.
    ///
    /// `TBD` is an ERROR here, not "absent", because that is what the strict
    /// accessor is for: these are the fields whose doc comment says a silent
    /// fallback to a baked-in default is dangerous. Reading `jlink_device: TBD`
    /// as absent would flash a GD32G553 with `DEFAULT_JLINK_DEVICE`, and
    /// `base: TBD` as absent would program real silicon at `DEFAULT_BASE` —
    /// both while the manifest was saying "not yet known". The sibling
    /// `fa_bool_checked`/`fa_int_checked` already hard-error on a present `TBD`;
    /// this makes the string member of the family agree with them.
    #[test]
    fn a_pending_placeholder_never_reaches_a_spawned_probe_command() {
        let art = Path::new("/b/build/app.bin");

        // The starkest one: no validator stands behind `jlink_device`, so the
        // literal went straight into `JLinkExe -device TBD` with
        // `planning_only: false`.
        let fa = yaml_val("jlink_device: TBD");
        let err = plan_swd_probe(&inputs(art, &fa, true), |_| true)
            .expect_err("TBD is not a J-Link device name");
        assert!(
            err.contains("jlink_device"),
            "must name the unfilled field: {err}"
        );

        // `interface`/`target` are worse than unvalidated: `validate_identifier`
        // ACCEPTS `TBD` (plain alphanumeric), and being non-empty it also
        // suppressed the "are required" refusal — so the run planned
        // `openocd -f interface/TBD.cfg -f target/TBD.cfg`.
        for key in ["interface", "target"] {
            let fa = yaml_val(&format!("{key}: TBD\nuse_openocd: true"));
            let err = plan_swd_probe(&inputs(art, &fa, true), |_| true)
                .expect_err("TBD is not an OpenOCD config name");
            assert!(err.contains(key), "must name the unfilled field: {err}");
            assert!(
                !err.contains("TBD.cfg"),
                "must not report the placeholder as an attempted config path: {err}"
            );
        }

        // `base` was already refused, but by `validate_address` happening to
        // reject a non-hex charset rather than by the accessor. Pin that it is
        // now the unfilled-field error, naming the field to fill in.
        let fa = yaml_val("base: TBD");
        let err = plan_swd_probe(&inputs(art, &fa, true), |_| true)
            .expect_err("TBD is not a flash base address");
        assert!(err.contains("base"), "must name the unfilled field: {err}");

        // Trimmed, like every other reader of the sentinel.
        let fa = yaml_val("jlink_device: ' TBD '");
        assert!(plan_swd_probe(&inputs(art, &fa, true), |_| true).is_err());

        // A value that merely CONTAINS the letters is still a value: over-
        // matching here would refuse real J-Link device names.
        let fa = yaml_val("jlink_device: TBD-1");
        let plan =
            plan_swd_probe(&inputs(art, &fa, true), |_| true).expect("TBD-1 is a real device name");
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "-device" && w[1] == "TBD-1")
        );
    }
}
