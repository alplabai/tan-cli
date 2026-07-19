// SPDX-License-Identifier: Apache-2.0
//! Pure planning for `tan flash` — the decision + argv-building half of the
//! native port of `west alp-flash` (`scripts/west_commands/alp_flash.py` +
//! `scripts/flash_backends/*`), retiring the west extension under ADR-0020
//! Phase 4. Every string, argv, filter, and per-backend command shape lives here
//! and is unit-tested with no IO; the subprocess/filesystem/temp-file half lives
//! in `tan-cli`'s `commands/flash.rs`.
//!
//! The flow mirrors `alp_flash.dispatch` + `_flash_entry`: walk the manifest's
//! `boot_order` (or the sorted slice `core_id`s when empty), map each step to its
//! slice, append the helper MCUs after, then dispatch each entry's `flash_method`
//! to a backend plan-builder. `flash_args` sub-keys are read tolerantly — a
//! non-mapping value (the AEN701 helper's `flash_args: TBD` string) reads as an
//! empty map.

use std::path::{Path, PathBuf};

use serde_yaml::Value;

use crate::system_manifest::SystemManifest;

/// Whether an entry is a per-core image slice or an on-module helper MCU.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlashKind {
    /// A per-core image slice (keyed by `core_id`).
    Slice,
    /// An on-module helper MCU (keyed by `name`).
    Helper,
}

impl FlashKind {
    /// The human/JSON label (`slice` | `helper`).
    pub fn as_str(self) -> &'static str {
        match self {
            FlashKind::Slice => "slice",
            FlashKind::Helper => "helper",
        }
    }
}

/// One manifest entry selected for flashing, in dispatch order.
#[derive(Debug, Clone, PartialEq)]
pub struct FlashTarget {
    /// Slice vs. helper.
    pub kind: FlashKind,
    /// `core_id` (slice) or `name` (helper).
    pub id: String,
    /// The `flash_method` string, when present.
    pub flash_method: Option<String>,
    /// The raw `flash_args` value (kept as-is; non-mapping reads as empty).
    pub flash_args: Value,
    /// Slice artefact path (`output_artefact`).
    pub output_artefact: Option<String>,
    /// Helper firmware path (`firmware_path`).
    pub firmware_path: Option<String>,
}

/// Build the ordered flash target list + any `boot_order` warnings, mirroring
/// `alp_flash.dispatch`'s step-building. Pure.
///
/// - Empty `boot_order`: one step per slice `core_id`, sorted ascending.
/// - Non-empty `boot_order`: walked in order; a step referencing a `core_id`
///   not in `slices` is dropped and surfaced as a warning string.
/// - Helpers always come AFTER all slices.
/// - `core = Some` flashes only that slice and skips every helper.
/// - `helper = Some` skips every slice and flashes only that helper.
pub fn plan_flash_targets(
    m: &SystemManifest,
    core: Option<&str>,
    helper: Option<&str>,
) -> (Vec<FlashTarget>, Vec<String>) {
    let mut targets = Vec::new();
    let mut warnings = Vec::new();

    // Slice lookup by non-empty core_id (drop empties, matching the Python dict
    // comprehension guard).
    let find_slice = |cid: &str| {
        m.slices
            .iter()
            .find(|s| !s.core_id.is_empty() && s.core_id == cid)
    };

    // Steps: boot_order when present, else the sorted slice core_ids.
    let steps: Vec<String> = if m.boot_order.is_empty() {
        let mut ids: Vec<String> = m
            .slices
            .iter()
            .filter(|s| !s.core_id.is_empty())
            .map(|s| s.core_id.clone())
            .collect();
        ids.sort();
        ids
    } else {
        m.boot_order
            .iter()
            .filter_map(|step| {
                step.as_mapping()?
                    .iter()
                    .find(|(k, _)| k.as_str() == Some("core"))
                    .and_then(|(_, v)| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
            })
            .collect()
    };

    // ---- slices (skipped entirely when --helper is set) ----
    if helper.is_none() {
        for cid in &steps {
            if let Some(sel) = core {
                if cid != sel {
                    continue;
                }
            }
            match find_slice(cid) {
                Some(s) => targets.push(FlashTarget {
                    kind: FlashKind::Slice,
                    id: s.core_id.clone(),
                    flash_method: s.flash_method.clone(),
                    flash_args: s.flash_args.clone().unwrap_or(Value::Null),
                    output_artefact: s.output_artefact.clone(),
                    firmware_path: None,
                }),
                None => warnings.push(format!(
                    "flash: boot_order references core '{cid}' not in slices; skipping"
                )),
            }
        }
    }

    // ---- helper MCUs (skipped entirely when --core is set) ----
    if core.is_none() {
        for h in &m.helper_mcus {
            if h.name.is_empty() {
                continue;
            }
            if let Some(sel) = helper {
                if h.name != sel {
                    continue;
                }
            }
            targets.push(FlashTarget {
                kind: FlashKind::Helper,
                id: h.name.clone(),
                flash_method: h.flash_method.clone(),
                flash_args: h.flash_args.clone().unwrap_or(Value::Null),
                output_artefact: None,
                firmware_path: h.firmware_path.clone(),
            });
        }
    }

    (targets, warnings)
}

/// Resolve a manifest artefact string to a path. Absolute strings pass through;
/// relative strings try `build_root/artefact` first, then `sdk_root/artefact`
/// when the build candidate is not a real file — mirroring
/// `alp_flash._flash_entry`. `is_file` is injected to keep this pure.
pub fn resolve_artefact_path(
    artefact: &str,
    build_root: &Path,
    sdk_root: Option<&Path>,
    is_file: impl Fn(&Path) -> bool,
) -> PathBuf {
    let p = Path::new(artefact);
    if p.is_absolute() {
        return p.to_path_buf();
    }
    let cand_build = build_root.join(artefact);
    match sdk_root {
        None => cand_build,
        Some(sdk) => {
            let cand_sdk = sdk.join(artefact);
            if is_file(&cand_build) {
                cand_build
            } else if is_file(&cand_sdk) {
                cand_sdk
            } else {
                cand_build
            }
        }
    }
}

/// Outcome of the required-tool gate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolGate {
    /// At least one required tool is present (or the gate is bypassed).
    Proceed,
    /// No required tool present, but `--skip-missing-tools` downgrades to a skip.
    Skip(String),
    /// No required tool present and no skip flag — a hard failure.
    Fail(String),
}

/// The required-tool gate: a backend is usable when AT LEAST ONE of `requires`
/// is on PATH. Bypassed entirely under `--dry-run`, and for backends with an
/// empty `requires` (e.g. `xspi_flashwriter`). `which` is injected. Pure.
pub fn tool_gate(
    requires: &[&str],
    dry_run: bool,
    skip_missing: bool,
    kind: &str,
    id: &str,
    method: &str,
    which: impl Fn(&str) -> bool,
) -> ToolGate {
    if dry_run || requires.is_empty() {
        return ToolGate::Proceed;
    }
    if requires.iter().any(|t| which(t)) {
        return ToolGate::Proceed;
    }
    let msg = format!(
        "flash: {kind} '{id}' backend '{method}' needs one of {requires:?} on PATH; none found."
    );
    if skip_missing {
        ToolGate::Skip(format!("{msg} (skipped via --skip-missing-tools)"))
    } else {
        ToolGate::Fail(msg)
    }
}

// ---------------------------------------------------------------------------
// flash_args typed accessors (tolerant: a non-mapping value reads as empty)
// ---------------------------------------------------------------------------

fn fa_get<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.as_mapping()?
        .iter()
        .find(|(key, _)| key.as_str() == Some(k))
        .map(|(_, val)| val)
}

/// A non-empty string sub-key; `None` when absent, empty, or non-string.
fn fa_str(v: &Value, k: &str) -> Option<String> {
    fa_get(v, k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// A bool sub-key with a default when absent/non-bool.
fn fa_bool(v: &Value, k: &str, default: bool) -> bool {
    fa_get(v, k).and_then(Value::as_bool).unwrap_or(default)
}

/// A bool sub-key, `None` when absent (distinguishes present-false from absent).
fn fa_bool_opt(v: &Value, k: &str) -> Option<bool> {
    fa_get(v, k).and_then(Value::as_bool)
}

/// An int sub-key with `or`-semantics: absent OR zero yields the default.
fn fa_int(v: &Value, k: &str, default: i64) -> i64 {
    fa_get(v, k)
        .and_then(Value::as_i64)
        .filter(|n| *n != 0)
        .unwrap_or(default)
}

/// A truthy int sub-key; `None` when absent or zero.
fn fa_int_opt(v: &Value, k: &str) -> Option<i64> {
    fa_get(v, k).and_then(Value::as_i64).filter(|n| *n != 0)
}

// ---------------------------------------------------------------------------
// Backend registry
// ---------------------------------------------------------------------------

/// Which backend implementation a `flash_method` resolves to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendKind {
    /// `swd_probe` — J-Link / OpenOCD / pyOCD external probe.
    Swd,
    /// `zephyr_west_flash` — `west flash`.
    Zephyr,
    /// `baremetal_cmake_flash` — `cmake --build --target`.
    Cmake,
    /// `cc3501e_usb_bootloader` — CC3501E USB-CDC bootloader (shape-complete).
    Cc3501e,
    /// `yocto_wic_to_sd_or_emmc` / `yocto_wic` — bmaptool / dd to a block device.
    YoctoWic,
    /// `xspi_flashwriter` — Renesas Flash Writer over SCIF (HW-gated).
    Xspi,
}

/// A registered backend: the tool-gate `requires` list + which builder to call.
#[derive(Debug, Clone, Copy)]
pub struct FlashBackendMeta {
    /// Executables the gate checks (at least one must be on PATH).
    pub requires: &'static [&'static str],
    /// The builder to dispatch to.
    pub kind: BackendKind,
}

/// Resolve a `flash_method` string to its backend metadata, or `None`.
pub fn backend_for(method: &str) -> Option<FlashBackendMeta> {
    let meta = match method {
        "swd_probe" => FlashBackendMeta {
            requires: &["JLinkExe", "JLink", "openocd", "pyocd"],
            kind: BackendKind::Swd,
        },
        "zephyr_west_flash" => FlashBackendMeta {
            requires: &["west"],
            kind: BackendKind::Zephyr,
        },
        "baremetal_cmake_flash" => FlashBackendMeta {
            requires: &["cmake"],
            kind: BackendKind::Cmake,
        },
        "cc3501e_usb_bootloader" => FlashBackendMeta {
            requires: &["cc3501e-flasher", "cc3501e-tool"],
            kind: BackendKind::Cc3501e,
        },
        "yocto_wic_to_sd_or_emmc" | "yocto_wic" => FlashBackendMeta {
            requires: &["bmaptool", "dd"],
            kind: BackendKind::YoctoWic,
        },
        "xspi_flashwriter" => FlashBackendMeta {
            requires: &[],
            kind: BackendKind::Xspi,
        },
        _ => return None,
    };
    Some(meta)
}

/// The registered method names, sorted — for the "Available: …" error when a
/// `flash_method` has no backend.
pub fn registry_keys() -> Vec<&'static str> {
    let mut keys = vec![
        "baremetal_cmake_flash",
        "cc3501e_usb_bootloader",
        "swd_probe",
        "xspi_flashwriter",
        "yocto_wic",
        "yocto_wic_to_sd_or_emmc",
        "zephyr_west_flash",
    ];
    keys.sort_unstable();
    keys
}

// ---------------------------------------------------------------------------
// Per-backend plan builders
// ---------------------------------------------------------------------------

/// Everything a backend plan-builder consumes. Injected by the CLI layer.
pub struct FlashInputs<'a> {
    /// Resolved artefact path.
    pub artefact: &'a Path,
    /// The entry's `flash_args`.
    pub flash_args: &'a Value,
    /// The slice/helper id (log lines).
    pub core_id: &'a str,
    /// SoM SKU.
    pub sku: &'a str,
    /// `--dry-run`.
    pub dry_run: bool,
    /// The env half of the confirm gate (`ALP_FLASH_FORCE=1`). The per-entry
    /// `flash_args.confirm` is OR-ed in by the yocto/xspi plan builders, so the
    /// effective gate is `flash_args.confirm OR ALP_FLASH_FORCE=1`.
    pub force_confirm: bool,
}

/// A built flash plan: the argv, the success message, whether it is planning-
/// only (never spawns real device IO), and — for the J-Link path — the
/// Commander script the CLI must materialise to a temp file.
#[derive(Debug, Clone, PartialEq)]
pub struct FlashPlan {
    /// The command to run (a `"|"` token marks a decompress→dd pipeline).
    pub argv: Vec<String>,
    /// Message on a clean spawn (rc 0).
    pub ok_message: String,
    /// Plan-only: print "would run …" and do not spawn (dry-run gate is separate).
    pub planning_only: bool,
    /// J-Link Commander script content; the CLI writes it to a temp file and
    /// appends its path as the final `-CommanderScript` arg.
    pub jlink_script: Option<String>,
}

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

/// `yocto_wic_to_sd_or_emmc` / `yocto_wic`: bmaptool (preferred) or dd to a raw
/// `/dev/` block device. Compressed images pipe `gunzip`/`xz` into `dd`.
/// Planning-only unless confirmed. `which` is injected.
pub fn plan_yocto_wic(
    inp: &FlashInputs,
    which: impl Fn(&str) -> bool,
) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let target = fa_str(fa, "target")
        .ok_or_else(|| "yocto_wic: flash_args.target is required (e.g. /dev/sdb)".to_string())?;
    if !target.starts_with("/dev/") {
        return Err(format!(
            "yocto_wic: refusing target '{target}' -- must start with /dev/ to avoid clobbering a \
             regular file. Set flash_args.target to a real block device."
        ));
    }
    let artefact = inp.artefact.to_string_lossy().into_owned();
    let compress = fa_str(fa, "compress").or_else(|| match artefact_suffix(inp.artefact) {
        Some("gz") => Some("gz".to_string()),
        Some("xz") => Some("xz".to_string()),
        _ => None,
    });
    // Python (yocto_wic.py:146): confirm = bool(flash_args.confirm) OR ALP_FLASH_FORCE.
    // force_confirm carries the env half; fold the per-entry flash_args.confirm here.
    let confirm = inp.force_confirm || fa_bool(fa, "confirm", false);
    let planning_only = inp.dry_run || !confirm;

    let bmaptool = which("bmaptool");
    let dd = which("dd");
    let argv = if bmaptool || (planning_only && !dd) {
        vec![
            "bmaptool".to_string(),
            "copy".to_string(),
            artefact,
            target.clone(),
        ]
    } else if dd {
        let bs = fa_str(fa, "bs").unwrap_or_else(|| "4M".to_string());
        let dd_cmd = vec![
            "dd".to_string(),
            format!("of={target}"),
            format!("bs={bs}"),
            "conv=fsync".to_string(),
            "status=progress".to_string(),
        ];
        match compress.as_deref() {
            Some("gz") => {
                let dcmp = if which("gunzip") {
                    vec!["gunzip".to_string(), "-c".to_string(), artefact]
                } else if which("gzip") {
                    vec!["gzip".to_string(), "-dc".to_string(), artefact]
                } else {
                    return Err(
                        "yocto_wic: compressed .wic.gz fallback needs `gunzip` or `gzip` \
                                on PATH."
                            .to_string(),
                    );
                };
                pipeline(dcmp, dd_cmd)
            }
            Some("xz") => {
                if !which("xz") {
                    return Err(
                        "yocto_wic: compressed .wic.xz fallback needs `xz` on PATH.".to_string()
                    );
                }
                let dcmp = vec!["xz".to_string(), "-dc".to_string(), artefact];
                pipeline(dcmp, dd_cmd)
            }
            _ => vec![
                "dd".to_string(),
                format!("if={artefact}"),
                format!("of={target}"),
                format!("bs={bs}"),
                "conv=fsync".to_string(),
                "status=progress".to_string(),
            ],
        }
    } else {
        return Err(
            "yocto_wic: neither `bmaptool` nor `dd` is on PATH; install bmaptool \
                    (preferred -- sparse aware) via `apt install bmap-tools` or run on a host \
                    with coreutils."
                .to_string(),
        );
    };

    Ok(FlashPlan {
        argv,
        ok_message: format!("yocto_wic[{}]: programmed {target}", inp.core_id),
        planning_only,
        jlink_script: None,
    })
}

/// Join a decompress argv and a dd argv with the `"|"` pipeline marker the CLI
/// splits on to wire stdout→stdin.
fn pipeline(mut decompress: Vec<String>, dd: Vec<String>) -> Vec<String> {
    decompress.push("|".to_string());
    decompress.extend(dd);
    decompress
}

/// The lowercased final path extension (`gz`, `xz`, `wic`, …).
fn artefact_suffix(artefact: &Path) -> Option<&str> {
    artefact.extension().and_then(|e| e.to_str())
}

/// `xspi_flashwriter`: Renesas Flash Writer over SCIF. Planning-only unless
/// confirmed; the confirmed real write is HW-gated and fails today.
/// `flash_partition` must be `mtd0` or `mtd1`.
pub fn plan_xspi_flashwriter(inp: &FlashInputs) -> Result<FlashPlan, String> {
    let fa = inp.flash_args;
    let partition = fa_str(fa, "flash_partition").unwrap_or_default();
    if partition != "mtd0" && partition != "mtd1" {
        return Err(
            "xspi_flashwriter: flash_args.flash_partition must be 'mtd0' (bl2) or 'mtd1' (fip)"
                .to_string(),
        );
    }
    let port = fa_str(fa, "port").unwrap_or_else(|| "<port>".to_string());
    let writer = fa_str(fa, "flash_writer").unwrap_or_else(|| "<flash_writer.mot>".to_string());
    let baud = fa_int(fa, "baud", 115200);
    let artefact_name = inp
        .artefact
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let plan = vec![
        "flash-writer-scif".to_string(),
        format!("port={port}"),
        format!("writer={writer}"),
        format!("baud={baud}"),
        format!("partition={partition}"),
        format!("artefact={artefact_name}"),
    ];

    // Python (xspi_flashwriter.py:61): confirm = bool(flash_args.confirm) OR ALP_FLASH_FORCE.
    let confirm = inp.force_confirm || fa_bool(inp.flash_args, "confirm", false);
    let planning_only = inp.dry_run || !confirm;
    if planning_only {
        let why = if inp.dry_run {
            "dry-run"
        } else {
            "flash_args.confirm is false"
        };
        Ok(FlashPlan {
            argv: plan,
            ok_message: format!(
                "xspi_flashwriter[{}]: would write {artefact_name} -> xSPI {partition} via Flash \
                 Writer on {port} ({why})",
                inp.core_id
            ),
            planning_only: true,
            jlink_script: None,
        })
    } else {
        Err(
            "xspi_flashwriter: the real SCIF write is HW-gated and not yet validated on silicon \
             (bench shelved). Run with --dry-run; see docs/provisioning.md."
                .to_string(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    fn manifest(yaml: &str) -> SystemManifest {
        parse_system_manifest(yaml).expect("valid manifest")
    }

    const MULTI: &str = r#"
schema_version: 1
hw_info:
  sku: E1M-V2N101
slices:
- core_id: m33_sm
  os: zephyr
  output_artefact: build/m33_sm-zephyr/zephyr/zephyr.elf
  flash_method: zephyr_west_flash
  flash_args:
    runner: openocd
- core_id: a55_cluster
  os: yocto
  output_artefact: build/a55.wic
  flash_method: yocto_wic_to_sd_or_emmc
  flash_args:
    target: /dev/sdb
helper_mcus:
- name: gd32_bridge
  chip: gd32g553
  firmware_path: firmware/gd32.bin
  flash_method: swd_probe
  flash_args: {}
boot_order: []
"#;

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
    fn empty_boot_order_sorts_core_ids_helpers_last() {
        let m = manifest(MULTI);
        let (targets, warnings) = plan_flash_targets(&m, None, None);
        assert!(warnings.is_empty());
        let ids: Vec<&str> = targets.iter().map(|t| t.id.as_str()).collect();
        // a55_cluster < m33_sm ascending; helper last.
        assert_eq!(ids, vec!["a55_cluster", "m33_sm", "gd32_bridge"]);
        assert_eq!(targets[2].kind, FlashKind::Helper);
    }

    #[test]
    fn boot_order_is_walked_in_order_unknown_core_warns() {
        let yaml = MULTI.replace(
            "boot_order: []",
            "boot_order:\n- core: m33_sm\n- core: ghost\n- core: a55_cluster",
        );
        let m = manifest(&yaml);
        let (targets, warnings) = plan_flash_targets(&m, None, None);
        let ids: Vec<&str> = targets.iter().map(|t| t.id.as_str()).collect();
        // boot_order order preserved; ghost dropped; helper appended after.
        assert_eq!(ids, vec!["m33_sm", "a55_cluster", "gd32_bridge"]);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("ghost") && warnings[0].contains("not in slices"));
    }

    #[test]
    fn core_filter_selects_only_that_slice_no_helpers() {
        let m = manifest(MULTI);
        let (targets, _) = plan_flash_targets(&m, Some("m33_sm"), None);
        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].id, "m33_sm");
        assert_eq!(targets[0].kind, FlashKind::Slice);
    }

    #[test]
    fn helper_filter_selects_only_that_helper_no_slices() {
        let m = manifest(MULTI);
        let (targets, _) = plan_flash_targets(&m, None, Some("gd32_bridge"));
        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].id, "gd32_bridge");
        assert_eq!(targets[0].kind, FlashKind::Helper);
    }

    #[test]
    fn core_matches_nothing_yields_empty() {
        let m = manifest(MULTI);
        let (targets, warnings) = plan_flash_targets(&m, Some("nope"), None);
        assert!(targets.is_empty());
        assert!(warnings.is_empty());
    }

    #[test]
    fn resolve_artefact_prefers_build_then_sdk_then_falls_back() {
        let build = Path::new("/work/build");
        let sdk = Path::new("/sdk");
        // build candidate is a file -> pick it.
        let got = resolve_artefact_path("out.elf", build, Some(sdk), |p| {
            p == Path::new("/work/build/out.elf")
        });
        assert_eq!(got, PathBuf::from("/work/build/out.elf"));
        // build missing, sdk file exists -> pick sdk.
        let got = resolve_artefact_path("fw.bin", build, Some(sdk), |p| {
            p == Path::new("/sdk/fw.bin")
        });
        assert_eq!(got, PathBuf::from("/sdk/fw.bin"));
        // neither is a file -> fall back to build candidate.
        let got = resolve_artefact_path("x.bin", build, Some(sdk), |_| false);
        assert_eq!(got, PathBuf::from("/work/build/x.bin"));
        // absolute passes through unchanged.
        let got = resolve_artefact_path("/abs/y.elf", build, Some(sdk), |_| false);
        assert_eq!(got, PathBuf::from("/abs/y.elf"));
    }

    #[test]
    fn tool_gate_semantics() {
        let reqs = ["west"];
        // at least one present -> proceed.
        assert_eq!(
            tool_gate(
                &reqs,
                false,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| true
            ),
            ToolGate::Proceed
        );
        // none present + skip_missing -> Skip with the flag phrase.
        match tool_gate(
            &reqs,
            false,
            true,
            "slice",
            "m",
            "zephyr_west_flash",
            |_| false,
        ) {
            ToolGate::Skip(msg) => assert!(msg.contains("skipped via --skip-missing-tools")),
            other => panic!("expected Skip, got {other:?}"),
        }
        // none present + no skip -> Fail.
        assert!(matches!(
            tool_gate(
                &reqs,
                false,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| false
            ),
            ToolGate::Fail(_)
        ));
        // dry-run bypasses regardless.
        assert_eq!(
            tool_gate(
                &reqs,
                true,
                false,
                "slice",
                "m",
                "zephyr_west_flash",
                |_| false
            ),
            ToolGate::Proceed
        );
        // empty requires never trips.
        assert_eq!(
            tool_gate(&[], false, false, "helper", "x", "xspi_flashwriter", |_| {
                false
            }),
            ToolGate::Proceed
        );
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

    #[test]
    fn yocto_target_required_and_dev_guard() {
        let art = Path::new("/b/core.wic");
        let inp = inputs(art, &Value::Null, true);
        assert!(
            plan_yocto_wic(&inp, |_| true)
                .unwrap_err()
                .contains("target is required")
        );
        let fa = yaml_val("target: /tmp/spill.img");
        let inp = inputs(art, &fa, true);
        assert!(
            plan_yocto_wic(&inp, |_| true)
                .unwrap_err()
                .contains("must start with /dev/")
        );
    }

    #[test]
    fn yocto_bmaptool_preferred_dd_fallback_and_planning_only() {
        let art = Path::new("/b/core.wic");
        let fa = yaml_val("target: /dev/sdb");
        let inp = inputs(art, &fa, false); // not dry, not confirmed -> planning_only
        // bmaptool present -> bmaptool copy.
        let plan = plan_yocto_wic(&inp, |t| t == "bmaptool").unwrap();
        assert_eq!(
            plan.argv,
            vec!["bmaptool", "copy", "/b/core.wic", "/dev/sdb"]
        );
        assert!(plan.planning_only);
        // only dd present -> dd fallback with bs=4M conv=fsync status=progress.
        let plan = plan_yocto_wic(&inp, |t| t == "dd").unwrap();
        assert_eq!(
            plan.argv,
            vec![
                "dd",
                "if=/b/core.wic",
                "of=/dev/sdb",
                "bs=4M",
                "conv=fsync",
                "status=progress"
            ]
        );
    }

    #[test]
    fn yocto_compressed_gz_pipes_into_dd() {
        let art = Path::new("/b/core.wic.gz");
        let fa = yaml_val("target: /dev/sdb");
        let inp = inputs(art, &fa, false);
        // dd + gunzip present (no bmaptool) -> gunzip -c | dd.
        let plan = plan_yocto_wic(&inp, |t| t == "dd" || t == "gunzip").unwrap();
        assert!(plan.argv.contains(&"|".to_string()));
        assert_eq!(plan.argv[0], "gunzip");
        assert!(
            plan.argv
                .windows(2)
                .any(|w| w[0] == "-c" && w[1] == "/b/core.wic.gz")
        );
    }

    #[test]
    fn confirm_via_flash_args_arms_real_write_without_env() {
        // flash_args.confirm:true must open the yocto/xspi gate even when the env
        // half (force_confirm) is false — Python folds both per entry.
        let art = Path::new("/b/core.wic");
        let fa = yaml_val("target: /dev/sdb\nconfirm: true");
        let inp = inputs(art, &fa, false); // dry_run=false, force_confirm defaults false
        assert!(
            !plan_yocto_wic(&inp, |t| t == "dd").unwrap().planning_only,
            "flash_args.confirm:true must arm a real dd write, not planning-only"
        );
        // Absent confirm + no env stays planning-only (would-run, not run).
        let fa_none = yaml_val("target: /dev/sdb");
        let inp_none = inputs(art, &fa_none, false);
        assert!(
            plan_yocto_wic(&inp_none, |t| t == "dd")
                .unwrap()
                .planning_only
        );
        // xspi: confirm via flash_args reaches the confirmed (HW-gated) path too.
        let fa_x = yaml_val("flash_partition: mtd1\nport: COM24\nconfirm: true");
        let inp_x = inputs(Path::new("/b/fip.bin"), &fa_x, false);
        assert!(
            plan_xspi_flashwriter(&inp_x)
                .unwrap_err()
                .contains("HW-gated")
        );
    }

    #[test]
    fn xspi_partition_required_and_hw_gated_when_confirmed() {
        let art = Path::new("/b/fip.bin");
        let inp = inputs(art, &Value::Null, true);
        assert!(plan_xspi_flashwriter(&inp).unwrap_err().contains("mtd0"));
        // valid partition, planning-only (not confirmed).
        let fa = yaml_val("flash_partition: mtd1\nport: COM24");
        let inp = inputs(art, &fa, true);
        let plan = plan_xspi_flashwriter(&inp).unwrap();
        assert_eq!(plan.argv[0], "flash-writer-scif");
        assert!(plan.argv.iter().any(|a| a == "partition=mtd1"));
        assert!(plan.argv.iter().any(|a| a == "baud=115200"));
        assert!(plan.planning_only);
        // confirmed real path -> HW-gated Err.
        let mut inp = inputs(art, &fa, false);
        inp.force_confirm = true;
        assert!(
            plan_xspi_flashwriter(&inp)
                .unwrap_err()
                .contains("HW-gated")
        );
    }

    #[test]
    fn non_mapping_flash_args_read_as_empty() {
        // The AEN701 helper's flash_args is the string "TBD".
        let fa = Value::String("TBD".to_string());
        assert_eq!(fa_str(&fa, "runner"), None);
        assert!(!fa_bool(&fa, "erase", false));
        assert_eq!(fa_int(&fa, "speed", 921600), 921600);
    }

    #[test]
    fn registry_keys_are_sorted_and_complete() {
        let keys = registry_keys();
        assert_eq!(keys.len(), 7);
        let mut sorted = keys.clone();
        sorted.sort_unstable();
        assert_eq!(keys, sorted);
        assert!(backend_for("swd_probe").is_some());
        assert!(backend_for("yocto_wic").is_some());
        assert!(backend_for("nope").is_none());
    }
}
