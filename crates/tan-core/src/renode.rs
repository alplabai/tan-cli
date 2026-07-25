// SPDX-License-Identifier: Apache-2.0
//! Pure pre-flight logic for `tan renode` — the deterministic half of the native
//! port of `west alp-renode` (`scripts/west_commands/alp_renode.py`), retired
//! under ADR-0020 Phase 4. No IO: SKU→platform-descriptor mapping, single-Zephyr-
//! slice selection, and the headless Renode argv all live here as IO-free
//! functions; the subprocess + filesystem wiring is in `tan-cli`'s
//! `commands/renode.rs`.
//!
//! SCOPE: the PLAIN (non-sim) headless smoke only. The Python `--sim-mode` studio
//! gateway (sockets / RamConsole bridge, issue #674) is deferred; none of its
//! helpers live here yet.

use std::path::{Path, PathBuf};

use crate::system_manifest::SystemManifest;

/// SoM-family token → Renode platform-descriptor stem under
/// `metadata/renode/<stem>.repl` + `<stem>.resc`. Mirrors
/// `alp_renode._FAMILY_TOKEN_TO_PLATFORM` — the ONLY two wired stems today. The
/// stems ARE on-disk filenames (contract): keep them verbatim.
pub const FAMILY_TOKEN_TO_PLATFORM: &[(&str, &str)] = &[
    ("alif_ensemble", "alif_ensemble_e8"),
    ("renesas_rzv2n", "renesas_rzv2n"),
];

/// Why a `tan renode` pre-flight failed. Every variant maps to
/// `ExitCode::RuntimeFailure` (faithful to the Python `log.die` = exit 1 for all
/// of these); the CLI adds the one new `schema_version != 1` guard separately.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RenodeError {
    /// SKU didn't match `^E1M-(AEN|V2N|V2M|NX9)` (mirrors `_sku_family`'s
    /// `ValueError`).
    #[error("unrecognised SoM SKU pattern: {sku}")]
    UnrecognisedSku {
        /// The offending SKU string.
        sku: String,
    },
    /// The SKU's family resolves, but no Renode descriptor is wired for it yet
    /// (e.g. `imx93`/`nxp_imx9`).
    #[error(
        "no Renode platform descriptor for SoM family '{family}' (token={token:?}) \
         of SKU {sku}; wired families: {wired:?}. Add metadata/renode/<stem>.repl \
         + .resc and a FAMILY_TOKEN_TO_PLATFORM entry to extend coverage."
    )]
    NoDescriptorForFamily {
        /// The resolved family (e.g. `imx93`).
        family: String,
        /// The soc-family token, when one exists (e.g. `nxp_imx9`).
        token: Option<String>,
        /// The originating SKU.
        sku: String,
        /// The wired source tokens (sorted), for the "extend coverage" hint.
        wired: Vec<String>,
    },
    /// The manifest carries no `os: zephyr` slice to boot.
    #[error("system-manifest.yaml has no os: zephyr slice to boot in Renode.")]
    NoZephyrSlice,
    /// More than one runnable Zephyr slice and no `--core` to disambiguate —
    /// the smoke boots a single one.
    #[error(
        "system-manifest.yaml has {} zephyr slices (cores {cores:?}); the Renode \
         smoke boots a single-Zephyr-slice system. Pick one with `--core <CORE_ID>`. \
         Booting several slices together (dual-OS) is a separate target.",
        cores.len()
    )]
    MultipleZephyrSlices {
        /// The core ids of the competing zephyr slices.
        cores: Vec<String>,
    },
    /// `--core` named a core the manifest carries no Zephyr slice for.
    #[error(
        "system-manifest.yaml has no zephyr slice for core '{core}'; \
         its zephyr cores are {cores:?}."
    )]
    UnknownCore {
        /// The `--core` value that matched nothing.
        core: String,
        /// The zephyr core ids the manifest does carry.
        cores: Vec<String>,
    },
    /// Neither `hw_info.sku` nor `--board` gave a SKU.
    #[error("could not determine SoM SKU: manifest has no hw_info.sku and --board was not given.")]
    SkuUnresolved,
}

/// SKU → SoM family directory name. Mirrors `alp_project_loader._sku_family`:
/// `^E1M-(AEN|V2N|V2M|NX9)` → `aen`/`v2n`/`v2n-m1`/`imx93`. No regex dep — a
/// bare `E1M-` prefix + token match is the same match at string start.
pub fn sku_family(sku: &str) -> Result<&'static str, RenodeError> {
    let unrecognised = || RenodeError::UnrecognisedSku {
        sku: sku.to_string(),
    };
    let rest = sku.strip_prefix("E1M-").ok_or_else(unrecognised)?;
    for (token, family) in [
        ("AEN", "aen"),
        ("V2N", "v2n"),
        ("V2M", "v2n-m1"),
        ("NX9", "imx93"),
    ] {
        if rest.starts_with(token) {
            return Ok(family);
        }
    }
    Err(unrecognised())
}

/// SoM family → soc-family token. Mirrors `west_libs._SOC_FAMILY_TOKEN`.
pub fn soc_family_token(family: &str) -> Option<&'static str> {
    match family {
        "aen" => Some("alif_ensemble"),
        "v2n" => Some("renesas_rzv2n"),
        "v2n-m1" => Some("renesas_rzv2n"),
        "imx93" => Some("nxp_imx9"),
        _ => None,
    }
}

/// The wired source tokens (keys of [`FAMILY_TOKEN_TO_PLATFORM`]), sorted —
/// mirrors Python's `sorted(_FAMILY_TOKEN_TO_PLATFORM)` for the error hint.
fn wired_tokens() -> Vec<String> {
    let mut v: Vec<String> = FAMILY_TOKEN_TO_PLATFORM
        .iter()
        .map(|(tok, _)| tok.to_string())
        .collect();
    v.sort();
    v
}

/// Full SKU → Renode platform stem chain: SKU → family → token → stem. Errors
/// `NoDescriptorForFamily` when the family has no wired descriptor yet.
pub fn platform_stem_for_sku(sku: &str) -> Result<String, RenodeError> {
    let family = sku_family(sku)?;
    let token = soc_family_token(family);
    let stem = token.and_then(|t| {
        FAMILY_TOKEN_TO_PLATFORM
            .iter()
            .find(|(tok, _)| *tok == t)
            .map(|(_, stem)| *stem)
    });
    match stem {
        Some(s) => Ok(s.to_string()),
        None => Err(RenodeError::NoDescriptorForFamily {
            family: family.to_string(),
            token: token.map(str::to_string),
            sku: sku.to_string(),
            wired: wired_tokens(),
        }),
    }
}

/// `(repl_path, resc_path)` under `<sdk_root>/metadata/renode/` for the SKU's
/// family. No existence check — the caller validates.
pub fn platform_files_for_sku(
    sku: &str,
    sdk_root: &Path,
) -> Result<(PathBuf, PathBuf), RenodeError> {
    let stem = platform_stem_for_sku(sku)?;
    let base = sdk_root.join("metadata").join("renode");
    Ok((
        base.join(format!("{stem}.repl")),
        base.join(format!("{stem}.resc")),
    ))
}

/// The SKU to drive descriptor selection: `--board` override, else the
/// manifest's non-empty `hw_info.sku`, else `SkuUnresolved`.
pub fn select_sku(
    manifest: &SystemManifest,
    board_override: Option<&str>,
) -> Result<String, RenodeError> {
    if let Some(board) = board_override {
        let board = board.trim();
        if !board.is_empty() {
            return Ok(board.to_string());
        }
    }
    let sku = manifest.hw_info.sku.trim();
    if sku.is_empty() {
        Err(RenodeError::SkuUnresolved)
    } else {
        Ok(sku.to_string())
    }
}

/// Resolve the single runnable Zephyr slice's `zephyr.elf` from the manifest.
/// Filters `os == "zephyr"`; a slice is runnable when its `status` is NOT in
/// `{blocked, skipped}`; the pool is the runnable set, or the full zephyr set
/// when none are runnable (a single blocked zephyr slice still boots). Errors
/// `NoZephyrSlice` (empty) / `MultipleZephyrSlices` (>1, no `core`).
///
/// `core` is the caller's `--core <CORE_ID>` choice. When given, the zephyr set
/// is narrowed to that one core BEFORE the runnable filter, so a multicore
/// project (an AEN801's `m55_hp` + `m55_he`) can boot one slice in the smoke
/// instead of being refused outright. The narrowing keeps the runnable-or-all
/// fallback, so an explicitly named blocked/skipped slice still boots exactly
/// like a lone one does — the smoke touches no hardware, so a stale artefact
/// costs a failed simulation, not a mis-flashed board. `UnknownCore` when the
/// name matches no zephyr slice.
///
/// A post-build `output_artefact` (the real elf the build wrote) is PREFERRED
/// when present — used as-is when absolute, joined to `build_root` when
/// relative. Otherwise the `build_dir` is used as-is when absolute, joined to
/// `build_root` when relative, falling back to `build_root/<core_id>-<os>` when
/// absent; the ELF is then `<that>/zephyr/zephyr.elf`.
pub fn zephyr_elf_from_manifest(
    manifest: &SystemManifest,
    build_root: &Path,
    core: Option<&str>,
) -> Result<PathBuf, RenodeError> {
    let all_zephyr: Vec<&crate::system_manifest::Slice> = manifest
        .slices
        .iter()
        .filter(|s| s.os == "zephyr")
        .collect();
    let zephyr: Vec<&crate::system_manifest::Slice> = match core {
        Some(id) => {
            let picked: Vec<&crate::system_manifest::Slice> = all_zephyr
                .iter()
                .copied()
                .filter(|s| s.core_id == id)
                .collect();
            if picked.is_empty() {
                return Err(RenodeError::UnknownCore {
                    core: id.to_string(),
                    cores: all_zephyr.iter().map(|s| s.core_id.clone()).collect(),
                });
            }
            picked
        }
        None => all_zephyr,
    };
    let runnable: Vec<&crate::system_manifest::Slice> = zephyr
        .iter()
        .copied()
        .filter(|s| s.status != "blocked" && s.status != "skipped")
        .collect();
    let pool: &[&crate::system_manifest::Slice] = if runnable.is_empty() {
        &zephyr
    } else {
        &runnable
    };
    if pool.is_empty() {
        return Err(RenodeError::NoZephyrSlice);
    }
    if pool.len() > 1 {
        return Err(RenodeError::MultipleZephyrSlices {
            cores: pool.iter().map(|s| s.core_id.clone()).collect(),
        });
    }
    let s = pool[0];
    // Prefer the real artefact the build recorded, over re-deriving from build_dir.
    if let Some(artefact) = s.output_artefact.as_deref().filter(|a| !a.is_empty()) {
        let p = Path::new(artefact);
        return Ok(if p.is_absolute() {
            p.to_path_buf()
        } else {
            build_root.join(p)
        });
    }
    let build_dir = match s.build_dir.as_deref().filter(|b| !b.is_empty()) {
        Some(bd) => {
            let p = Path::new(bd);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                build_root.join(p)
            }
        }
        None => build_root.join(format!("{}-{}", s.core_id, s.os)),
    };
    Ok(build_dir.join("zephyr").join("zephyr.elf"))
}

/// The headless Renode command line. Injects the `.resc`'s `$repl` / `$elf`
/// variables by value and includes the script. This ordering + flags is a
/// machine contract — VERBATIM: `--console --disable-xwt --plain` then
/// `-e $repl=@… -e $elf=@… -e i @…`.
///
/// `--hide-monitor` used to sit between `--disable-xwt` and `--plain`, ported
/// straight from the Python command. Renode 1.16.1 REJECTS that combination —
/// "--hide-monitor and --console cannot be set at the same time" — printing its
/// usage page and exiting, so nothing ever booted. It was redundant anyway:
/// Renode's own `--disable-xwt` help reads "It automatically sets HideMonitor".
pub fn build_renode_argv(
    renode_bin: &str,
    repl: &Path,
    resc: &Path,
    elf: &Path,
    vtor: Option<u32>,
) -> Vec<String> {
    let mut argv = vec![
        renode_bin.to_string(),
        "--console".to_string(),
        "--disable-xwt".to_string(),
        "--plain".to_string(),
        "-e".to_string(),
        format!("$repl=@{}", repl.display()),
        "-e".to_string(),
        format!("$elf=@{}", elf.display()),
    ];
    // Assigned UNQUOTED on purpose: a quoted `$vtor="0x…"` reaches the script
    // as a string and `cpu VectorTableOffset $vtor` dies with "Cannot convert
    // type 'string' to 'uint'". Injected before the include so the script can
    // read it; a descriptor that ignores `$vtor` is unaffected (an unused
    // monitor variable), which is what keeps this safe to ship ahead of
    // alp-sdk#947 wiring `cpu VectorTableOffset $vtor` into the .resc.
    if let Some(base) = vtor {
        argv.push("-e".to_string());
        argv.push(format!("$vtor={base:#010X}"));
    }
    argv.push("-e".to_string());
    argv.push(format!("i @{}", resc.display()));
    argv
}

/// The base address of the LOAD segment holding `elf`'s entry point — the
/// image's real vector table, and the value Renode's `cpu VectorTableOffset`
/// needs. `None` when `elf` isn't a 32-bit little-endian ELF, or no LOAD
/// segment covers the entry (a malformed or unexpected image; the caller then
/// injects nothing and Renode keeps its own guess).
///
/// Renode guesses that offset from the LOWEST `vaddr` it sees, which is wrong
/// for an MRAM-linked Zephyr image: its `.data` init segment has `vaddr`
/// 0x20000000 (where it RUNS, in DTCM) but `paddr` 0x80018348 (where it is
/// STORED, in MRAM), so Renode reads SP/PC out of an address nothing was ever
/// loaded to, gets zeros, and halts the CPU before one instruction executes
/// (alp-sdk#947). Keying on the entry point instead of the lowest address is
/// what makes this correct for BOTH shapes — a RAM-run image's entry lands in
/// its DTCM segment, an MRAM-linked one's in its MRAM segment.
///
/// Physical (`p_paddr`), not virtual: the load address is where the bytes
/// actually are, which is exactly what the vector-table fetch reads.
///
/// Containment alone only proves the entry point falls inside this segment's
/// runtime footprint — it does NOT prove the vector table is the FIRST thing
/// in the segment. An allocated `.note.gnu.build-id` ahead of `_vector_table`,
/// or a padded/offset link, would satisfy containment and still hand back a
/// confident wrong address. So this also reads the table's own second word —
/// the reset vector, the Thumb bit cleared must equal `e_entry` with the
/// Thumb bit cleared — before trusting the guess. That word lives at file
/// offset `p_offset + 4`; the delta is kept in the `paddr` domain (not
/// `vaddr`) because `paddr` is the address actually being verified and
/// returned, and the two diverge for exactly the MRAM-linked shape above.
/// `None` when the word isn't even in the file (`p_filesz < 8`, e.g. a
/// bss-only LOAD segment) or doesn't match — no answer beats a wrong one.
pub fn elf_vector_table_base(elf: &[u8]) -> Option<u32> {
    // e_ident: 0x7F 'E' 'L' 'F', class 1 (32-bit), data 1 (little-endian).
    if elf.len() < 52 || &elf[0..4] != b"\x7FELF" || elf[4] != 1 || elf[5] != 1 {
        return None;
    }
    let u32_at = |off: usize| -> Option<u32> {
        elf.get(off..off + 4)
            .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    };
    let u16_at = |off: usize| -> Option<u16> {
        elf.get(off..off + 2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
    };
    // Thumb entry points carry bit 0 set; the address itself is even.
    let entry = u32_at(0x18)? & !1;
    let phoff = u32_at(0x1C)? as usize;
    let phentsize = u16_at(0x2A)? as usize;
    let phnum = u16_at(0x2C)? as usize;
    // A real Elf32_Phdr is 32 bytes. A corrupt file with a smaller stride (8,
    // 16, ...) but a still-valid e_ident makes every read below land inside
    // the NEXT phdr's fields instead of this one's, so a garbage file would
    // otherwise produce a plausible-looking wrong base instead of None.
    // (phentsize == 0 is benign on its own — every iteration just re-reads
    // phdr 0 — but the guard covers it too.)
    if phentsize < 32 {
        return None;
    }

    for i in 0..phnum {
        let off = phoff.checked_add(i.checked_mul(phentsize)?)?;
        // Elf32_Phdr: type, offset, vaddr, paddr, filesz, memsz, ...
        if u32_at(off)? != 1 {
            continue; // PT_LOAD only
        }
        let p_offset = u32_at(off + 4)? as usize;
        let vaddr = u32_at(off + 8)?;
        let paddr = u32_at(off + 12)?;
        let filesz = u32_at(off + 16)?;
        let memsz = u32_at(off + 20)?;
        if entry >= vaddr && entry < vaddr.checked_add(memsz)? {
            // The table's own second word isn't even in the file — nothing
            // to verify against, so refuse rather than guess.
            if filesz < 8 {
                return None;
            }
            let word1 = u32_at(p_offset + 4)?;
            if word1 & !1 != entry {
                return None; // segment starts with something else — not our table
            }
            return Some(paddr);
        }
    }
    None
}

/// Whether a console line is Renode telling us it REFUSED the command line —
/// either the complaint itself or the usage page it dumps afterwards.
///
/// This exists because argv rejection is otherwise indistinguishable from a
/// clean smoke: Renode prints its usage and exits **0**, so the run reported
/// success while nothing was ever simulated. A smoke test that cannot fail is
/// worse than no smoke test, and the failure mode is silent by construction —
/// the next incompatible flag would pass just as quietly. Matched on Renode's
/// own wording rather than an exit code, since the exit code carries no signal
/// here.
pub fn renode_rejected_argv(line: &str) -> bool {
    let line = line.trim_start();
    line.starts_with("usage: renode") || line.contains("cannot be set at the same time")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_manifest::parse_system_manifest;

    #[test]
    fn platform_stem_for_the_wired_skus() {
        assert_eq!(
            platform_stem_for_sku("E1M-AEN801").unwrap(),
            "alif_ensemble_e8"
        );
        assert_eq!(
            platform_stem_for_sku("E1M-V2N101").unwrap(),
            "renesas_rzv2n"
        );
        // V2M reuses the V2N descriptor via family v2n-m1.
        assert_eq!(
            platform_stem_for_sku("E1M-V2M101").unwrap(),
            "renesas_rzv2n"
        );
    }

    #[test]
    fn nx9_has_no_descriptor_yet() {
        let err = platform_stem_for_sku("E1M-NX901").unwrap_err();
        match err {
            RenodeError::NoDescriptorForFamily {
                family,
                token,
                wired,
                ..
            } => {
                assert_eq!(family, "imx93");
                assert_eq!(token.as_deref(), Some("nxp_imx9"));
                assert_eq!(wired, vec!["alif_ensemble", "renesas_rzv2n"]);
            }
            other => panic!("expected NoDescriptorForFamily, got {other:?}"),
        }
        // The message names the wired families.
        let msg = platform_stem_for_sku("E1M-NX901").unwrap_err().to_string();
        assert!(msg.contains("alif_ensemble"), "{msg}");
        assert!(msg.contains("renesas_rzv2n"), "{msg}");
    }

    #[test]
    fn bogus_sku_is_unrecognised() {
        assert_eq!(
            platform_stem_for_sku("BOGUS").unwrap_err(),
            RenodeError::UnrecognisedSku {
                sku: "BOGUS".to_string()
            }
        );
    }

    #[test]
    fn sku_family_and_token_maps() {
        assert_eq!(sku_family("E1M-AEN801").unwrap(), "aen");
        assert_eq!(sku_family("E1M-V2N101").unwrap(), "v2n");
        assert_eq!(sku_family("E1M-V2M101").unwrap(), "v2n-m1");
        assert_eq!(sku_family("E1M-NX901").unwrap(), "imx93");
        assert_eq!(soc_family_token("aen"), Some("alif_ensemble"));
        assert_eq!(soc_family_token("v2n"), Some("renesas_rzv2n"));
        assert_eq!(soc_family_token("v2n-m1"), Some("renesas_rzv2n"));
        assert_eq!(soc_family_token("imx93"), Some("nxp_imx9"));
    }

    #[test]
    fn platform_files_paths() {
        let (repl, resc) = platform_files_for_sku("E1M-AEN801", Path::new("/sdk")).unwrap();
        assert_eq!(
            repl,
            PathBuf::from("/sdk/metadata/renode/alif_ensemble_e8.repl")
        );
        assert_eq!(
            resc,
            PathBuf::from("/sdk/metadata/renode/alif_ensemble_e8.resc")
        );
    }

    /// A manifest with the given zephyr slices, each `(core_id, status,
    /// build_dir)`; `build_dir` empty → the slice omits the key.
    fn manifest_with(slices: &[(&str, &str, &str)]) -> SystemManifest {
        let mut yaml = String::from("schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n");
        for (core, status, bd) in slices {
            yaml.push_str(&format!(
                "- core_id: {core}\n  os: zephyr\n  status: {status}\n"
            ));
            if !bd.is_empty() {
                yaml.push_str(&format!("  build_dir: {bd}\n"));
            }
        }
        parse_system_manifest(&yaml).expect("valid test manifest")
    }

    #[test]
    fn relative_build_dir_joins_build_root() {
        let m = manifest_with(&[("m55_hp", "pending", "m55_hp-zephyr")]);
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(
            elf,
            PathBuf::from("/p/build/m55_hp-zephyr/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn output_artefact_is_preferred_over_build_dir() {
        // A post-build manifest carrying the real elf: it wins over re-deriving
        // `<build_dir>/zephyr/zephyr.elf`. Relative anchors to build_root.
        let m = parse_system_manifest(
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n\
             - core_id: m55_hp\n  os: zephyr\n  status: ok\n  \
             build_dir: m55_hp-zephyr\n  \
             output_artefact: m55_hp-zephyr/build/zephyr/zephyr.elf\n",
        )
        .unwrap();
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(
            elf,
            PathBuf::from("/p/build/m55_hp-zephyr/build/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn absolute_output_artefact_used_verbatim() {
        let m = parse_system_manifest(
            "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n\
             - core_id: m55_hp\n  os: zephyr\n  status: ok\n  \
             output_artefact: /abs/out/build/zephyr/zephyr.elf\n",
        )
        .unwrap();
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(elf, PathBuf::from("/abs/out/build/zephyr/zephyr.elf"));
    }

    #[test]
    fn absolute_build_dir_used_verbatim() {
        let m = manifest_with(&[("m55_hp", "pending", "/abs/out")]);
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(elf, PathBuf::from("/abs/out/zephyr/zephyr.elf"));
    }

    #[test]
    fn absent_build_dir_falls_back_to_core_os_stem() {
        let m = manifest_with(&[("m55_hp", "pending", "")]);
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(
            elf,
            PathBuf::from("/p/build/m55_hp-zephyr/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn blocked_slice_ignored_boots_the_runnable_one() {
        let m = manifest_with(&[("m55_he", "blocked", ""), ("m55_hp", "pending", "")]);
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(
            elf,
            PathBuf::from("/p/build/m55_hp-zephyr/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn core_picks_one_slice_out_of_a_multi_slice_manifest() {
        // The AEN801 shape: two runnable Zephyr slices. Without `--core` this is
        // `MultipleZephyrSlices` (asserted below); with it, the named slice boots.
        let m = manifest_with(&[("m55_hp", "pending", ""), ("m55_he", "pending", "")]);
        assert_eq!(
            zephyr_elf_from_manifest(&m, Path::new("/p/build"), Some("m55_he")).unwrap(),
            PathBuf::from("/p/build/m55_he-zephyr/zephyr/zephyr.elf")
        );
        assert_eq!(
            zephyr_elf_from_manifest(&m, Path::new("/p/build"), Some("m55_hp")).unwrap(),
            PathBuf::from("/p/build/m55_hp-zephyr/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn core_naming_a_blocked_slice_still_boots_it() {
        // The runnable-or-all fallback survives the narrowing: an explicitly
        // named blocked slice boots exactly like a lone blocked one does. The
        // smoke touches no hardware, so a stale artefact costs a failed
        // simulation, not a mis-flashed board.
        let m = manifest_with(&[("m55_he", "blocked", ""), ("m55_hp", "pending", "")]);
        assert_eq!(
            zephyr_elf_from_manifest(&m, Path::new("/p/build"), Some("m55_he")).unwrap(),
            PathBuf::from("/p/build/m55_he-zephyr/zephyr/zephyr.elf")
        );
    }

    #[test]
    fn unknown_core_errors_and_lists_the_zephyr_cores() {
        let m = manifest_with(&[("m55_hp", "pending", ""), ("m55_he", "pending", "")]);
        match zephyr_elf_from_manifest(&m, Path::new("/p/build"), Some("a32_cluster")).unwrap_err()
        {
            RenodeError::UnknownCore { core, cores } => {
                assert_eq!(core, "a32_cluster");
                assert_eq!(cores, vec!["m55_hp", "m55_he"]);
            }
            other => panic!("expected UnknownCore, got {other:?}"),
        }
    }

    #[test]
    fn zero_zephyr_slices_errors() {
        let m = parse_system_manifest("schema_version: 1\nslices: []\n").unwrap();
        assert_eq!(
            zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap_err(),
            RenodeError::NoZephyrSlice
        );
    }

    #[test]
    fn two_runnable_zephyr_slices_error_lists_cores() {
        let m = manifest_with(&[("m55_hp", "pending", ""), ("m55_he", "pending", "")]);
        match zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap_err() {
            RenodeError::MultipleZephyrSlices { cores } => {
                assert_eq!(cores, vec!["m55_hp", "m55_he"]);
            }
            other => panic!("expected MultipleZephyrSlices, got {other:?}"),
        }
    }

    #[test]
    fn all_blocked_falls_back_and_boots_the_blocked_slice() {
        // pool = runnable OR all-zephyr; one blocked zephyr slice still boots.
        let m = manifest_with(&[("m55_hp", "blocked", "")]);
        let elf = zephyr_elf_from_manifest(&m, Path::new("/p/build"), None).unwrap();
        assert_eq!(
            elf,
            PathBuf::from("/p/build/m55_hp-zephyr/zephyr/zephyr.elf")
        );
        // …but a blocked + another blocked still errors on count.
        let m2 = manifest_with(&[("m55_hp", "blocked", ""), ("m55_he", "skipped", "")]);
        assert!(matches!(
            zephyr_elf_from_manifest(&m2, Path::new("/p/build"), None).unwrap_err(),
            RenodeError::MultipleZephyrSlices { .. }
        ));
    }

    #[test]
    fn build_renode_argv_is_the_exact_contract() {
        let argv = build_renode_argv(
            "renode",
            Path::new("/m/x.repl"),
            Path::new("/m/x.resc"),
            Path::new("/b/zephyr.elf"),
            None,
        );
        assert_eq!(
            argv,
            vec![
                "renode",
                "--console",
                "--disable-xwt",
                "--plain",
                "-e",
                "$repl=@/m/x.repl",
                "-e",
                "$elf=@/b/zephyr.elf",
                "-e",
                "i @/m/x.resc",
            ]
        );
        assert_eq!(argv.len(), 10);
        // Renode 1.16.1 refuses `--hide-monitor` alongside `--console`, and
        // `--disable-xwt` already implies it. Keep it out.
        assert!(!argv.iter().any(|a| a == "--hide-monitor"));
    }

    /// A minimal Elf32 LE header + program headers, enough for
    /// `elf_vector_table_base`. `segs` is `(vaddr, paddr, memsz)`; each
    /// segment gets real file content (2 words, `p_filesz = 8`) so the
    /// second-word verification has something to read. Word 1 (the reset
    /// vector) of the segment containing `entry` defaults to `entry` itself —
    /// a well-formed image whose vector table really is first in that
    /// segment — unless `bad_reset_vector` overrides it, faking an image
    /// where something else (e.g. a build-id note) sits there instead.
    fn synthetic_elf(
        entry: u32,
        segs: &[(u32, u32, u32)],
        bad_reset_vector: Option<u32>,
    ) -> Vec<u8> {
        let phoff = 52u32;
        let phentsize = 32u16;
        let phdrs_end = phoff as usize + segs.len() * phentsize as usize;
        let mut e = vec![0u8; phdrs_end];
        e[0..4].copy_from_slice(b"\x7FELF");
        e[4] = 1; // 32-bit
        e[5] = 1; // little-endian
        e[0x18..0x1C].copy_from_slice(&entry.to_le_bytes());
        e[0x1C..0x20].copy_from_slice(&phoff.to_le_bytes());
        e[0x2A..0x2C].copy_from_slice(&phentsize.to_le_bytes());
        e[0x2C..0x2E].copy_from_slice(&(segs.len() as u16).to_le_bytes());

        let masked_entry = entry & !1;
        for (i, (vaddr, paddr, memsz)) in segs.iter().enumerate() {
            let off = phoff as usize + i * phentsize as usize;
            let content_off = (phdrs_end + i * 8) as u32; // 2 words (8 bytes) per segment
            e[off..off + 4].copy_from_slice(&1u32.to_le_bytes()); // PT_LOAD
            e[off + 4..off + 8].copy_from_slice(&content_off.to_le_bytes()); // p_offset
            e[off + 8..off + 12].copy_from_slice(&vaddr.to_le_bytes());
            e[off + 12..off + 16].copy_from_slice(&paddr.to_le_bytes());
            e[off + 16..off + 20].copy_from_slice(&8u32.to_le_bytes()); // p_filesz
            e[off + 20..off + 24].copy_from_slice(&memsz.to_le_bytes());

            let is_entry_segment =
                masked_entry >= *vaddr && masked_entry < vaddr.wrapping_add(*memsz);
            let word1 = if is_entry_segment {
                bad_reset_vector.unwrap_or(masked_entry)
            } else {
                0
            };
            e.extend_from_slice(&0u32.to_le_bytes()); // word0: initial SP (unread by the check)
            e.extend_from_slice(&word1.to_le_bytes()); // word1: reset vector
        }
        e
    }

    #[test]
    fn vector_table_base_is_the_entry_segments_load_address() {
        // The real E1M-AEN801 m55_he shape (alp-sdk#947): the .data init
        // segment RUNS at 0x20000000 but is STORED at 0x80018348, so the lowest
        // vaddr — what Renode guesses from — points at memory nothing was
        // loaded to. Keying on the entry's segment gives 0x80000000.
        let elf = synthetic_elf(
            0x8000_5A51, // Thumb bit set
            &[
                (0x2000_0130, 0x2000_0130, 16664),
                (0x8000_0000, 0x8000_0000, 99144),
                (0x2000_0000, 0x8001_8348, 304),
                (0x8001_8478, 0x8001_8478, 4),
            ],
            None,
        );
        assert_eq!(elf_vector_table_base(&elf), Some(0x8000_0000));
    }

    #[test]
    fn vector_table_base_handles_a_ram_run_image_too() {
        // The hello-world shape the descriptor was written for: everything in
        // DTCM. The same rule must keep working, or fixing #947 would break it.
        let elf = synthetic_elf(0x2000_0201, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        assert_eq!(elf_vector_table_base(&elf), Some(0x2000_0000));
    }

    #[test]
    fn vector_table_base_is_none_on_junk_or_an_unmapped_entry() {
        assert_eq!(elf_vector_table_base(b"not an elf at all"), None);
        // Entry outside every LOAD segment -> no answer, rather than a wrong one.
        let elf = synthetic_elf(0x9000_0000, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_is_none_when_the_second_word_is_not_the_reset_vector() {
        // Bounds match (entry falls inside the segment) but the segment's own
        // second word does NOT equal e_entry — e.g. an allocated
        // .note.gnu.build-id sits ahead of the real _vector_table. This is
        // exactly the confident-wrong-address case #65's review flagged:
        // containment alone must not be trusted.
        let elf = synthetic_elf(
            0x2000_0201,
            &[(0x2000_0000, 0x2000_0000, 8192)],
            Some(0x2000_9999),
        );
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_is_none_when_the_second_word_is_not_in_the_file() {
        // p_filesz < 8: the table's second word isn't stored in the file at
        // all (a bss-only LOAD segment), so there's nothing to verify against.
        let mut elf = synthetic_elf(0x2000_0201, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        let filesz_off = 52 + 16; // the sole phdr's p_filesz field
        elf[filesz_off..filesz_off + 4].copy_from_slice(&0u32.to_le_bytes());
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_entry_exactly_at_vaddr_is_in_bounds() {
        // Pins the `>=` lower-bound check: entry == vaddr must still match.
        let elf = synthetic_elf(0x2000_0000, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        assert_eq!(elf_vector_table_base(&elf), Some(0x2000_0000));
    }

    #[test]
    fn vector_table_base_entry_exactly_at_vaddr_plus_memsz_is_out_of_bounds() {
        // Pins the `<` upper-bound check: entry == vaddr + memsz must NOT match
        // (the segment's footprint ends one byte before it).
        let elf = synthetic_elf(0x2000_2000, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_rejects_elf64() {
        let mut elf = synthetic_elf(0x2000_0201, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        elf[4] = 2; // ELFCLASS64
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_rejects_big_endian() {
        let mut elf = synthetic_elf(0x2000_0201, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        elf[5] = 2; // ELFDATA2MSB
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn vector_table_base_rejects_a_too_small_phentsize() {
        // A corrupt e_phentsize (8 or 16) would otherwise make the loop read
        // vaddr/paddr/memsz out of the neighbouring phdr's fields.
        let mut elf = synthetic_elf(0x2000_0201, &[(0x2000_0000, 0x2000_0000, 8192)], None);
        elf[0x2A..0x2C].copy_from_slice(&16u16.to_le_bytes());
        assert_eq!(elf_vector_table_base(&elf), None);
    }

    #[test]
    fn argv_injects_vtor_unquoted_before_the_include() {
        // Unquoted: a quoted value reaches the script as a string and
        // `cpu VectorTableOffset $vtor` dies with "Cannot convert type
        // 'string' to 'uint'" (verified against Renode 1.16.1).
        let argv = build_renode_argv(
            "renode",
            Path::new("/m/x.repl"),
            Path::new("/m/x.resc"),
            Path::new("/b/zephyr.elf"),
            Some(0x8000_0000),
        );
        let vtor_at = argv.iter().position(|a| a == "$vtor=0x80000000").unwrap();
        let include_at = argv.iter().position(|a| a == "i @/m/x.resc").unwrap();
        assert!(vtor_at < include_at, "the script must see $vtor: {argv:?}");
        assert!(!argv.iter().any(|a| a.contains('"')));
        // No offset resolved -> nothing injected, argv byte-identical to before.
        let plain = build_renode_argv(
            "renode",
            Path::new("/m/x.repl"),
            Path::new("/m/x.resc"),
            Path::new("/b/zephyr.elf"),
            None,
        );
        assert!(!plain.iter().any(|a| a.starts_with("$vtor=")));
    }

    #[test]
    fn renode_argv_rejection_is_recognised_from_its_own_wording() {
        // Both halves of what Renode 1.16.1.15992 printed instead of booting.
        assert!(renode_rejected_argv(
            "--hide-monitor and --console cannot be set at the same time"
        ));
        assert!(renode_rejected_argv(
            "usage: renode [options] [file-to-include / snapshot]"
        ));
        // A normal boot line must never trip it — that would turn a good smoke
        // into a false failure, the mirror of the bug this guards.
        assert!(!renode_rejected_argv(
            "renode: booting /b/zephyr.elf on alif_ensemble_e8.repl"
        ));
        assert!(!renode_rejected_argv("*** Booting Zephyr OS ***"));
    }

    #[test]
    fn select_sku_prefers_override_then_manifest() {
        let m = manifest_with(&[("m55_hp", "pending", "")]); // hw_info.sku = E1M-AEN801
        assert_eq!(select_sku(&m, Some("E1M-V2N101")).unwrap(), "E1M-V2N101");
        assert_eq!(select_sku(&m, None).unwrap(), "E1M-AEN801");
        assert_eq!(select_sku(&m, Some("  ")).unwrap(), "E1M-AEN801");

        let bare = parse_system_manifest("schema_version: 1\nslices: []\n").unwrap();
        assert_eq!(
            select_sku(&bare, None).unwrap_err(),
            RenodeError::SkuUnresolved
        );
    }
}
