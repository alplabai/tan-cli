// SPDX-License-Identifier: Apache-2.0
//! Storage-target backend plan-builders: `yocto_wic_to_sd_or_emmc` / `yocto_wic`
//! (bmaptool / dd to a raw block device, with a decompress→dd pipeline for
//! compressed images) and `xspi_flashwriter` (Renesas Flash Writer over SCIF,
//! HW-gated). Both are planning-only until the confirm gate is armed.

use std::path::Path;

use super::args::{fa_bool, fa_int, fa_str};
use super::registry::{FlashInputs, FlashPlan};

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
}
