// SPDX-License-Identifier: Apache-2.0
//! `tan size` — natively measure each Zephyr slice's `zephyr.elf` FLASH/RAM and
//! compare it to the SoM memory budget resolved from SoC metadata. The IO/
//! subprocess side of the port; every measurement/budget/classification/render
//! *decision* is pure in `tan_core::size`. Faithful to the retired
//! `west alp-size` (`scripts/west_commands/alp_size.py`); under ADR-0020 Phase 4
//! `tan` owns this, the Python is retired.
//!
//! Divergences from the Python source, all deliberate:
//!   - the positional `app_path` folds into tan's `--project` workspace when it
//!     is `.`; a non-`.` positional still wins (parity for `tan size <app>`).
//!   - `--json` is dropped for the global `--format json`; the `alp-size/1`
//!     payload is preserved verbatim inside the standard tan Envelope `data`.
//!   - a missing alp-sdk root is NOT fatal (the Python `find_sdk_root` die);
//!     the budget just resolves `unknown`, measurement still runs. ponytail:
//!     the port spec's exit-code contract lists only manifest + over-budget as
//!     exit 1, and measurement is useful without metadata.
//!   - the middle `pyelftools` ELF-section fallback is dropped — no ELF-reader
//!     dep. ponytail: size-tool + rom/ram.json cover a real Zephyr build env;
//!     add the `object` crate only if a no-size-tool env proves common.

use std::path::{Path, PathBuf};

use tan_core::parse_som_preset;
use tan_core::size::{
    MemoryBudget, SliceSize, SocVariant, build_size_report, classify, footprint_total,
    over_budget_rows, parse_berkeley_size, render_table_lines, resolve_budget, resolve_variant,
    unknown_budget_rows,
};
use tan_core::system_manifest::{Slice, parse_system_manifest};

use super::CommandRun;
use crate::cli::{GlobalArgs, SizeArgs};
use crate::envelope::{Envelope, Issue, Project};
use crate::exit::ExitCode;
use crate::style::Theme;
use crate::util::{
    cli_workspace_root, command_on_path, resolve_cli_project_context, resolve_sdk_root,
};

/// Size tools probed on PATH, most-specific first. The Zephyr SDK ships
/// `arm-zephyr-eabi-size`; an LLVM toolchain ships `llvm-size`; host binutils
/// ships `size`. All speak the same Berkeley columns.
const SIZE_TOOLS: [&str; 3] = ["arm-zephyr-eabi-size", "llvm-size", "size"];

/// `tan size` entry. See the module docs for the faithful-plus behaviour.
pub fn run(g: &GlobalArgs, args: &SizeArgs) -> CommandRun {
    let context = resolve_cli_project_context(g);
    let project = Project {
        root: context.workspace_root.clone(),
        board_yaml: context.board_yaml_path.clone(),
    };

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));

    // App base: a non-`.` positional wins (parity); else tan's resolved
    // workspace (which already folded in `--project`).
    let app_base = if args.app_path == "." {
        context
            .west_cwd
            .clone()
            .or_else(|| context.workspace_root.clone())
            .map(PathBuf::from)
            .unwrap_or_else(|| cwd.clone())
    } else {
        cwd.join(&args.app_path)
    };

    // build_root: absolute as-is, relative against CWD (spec); default
    // `<app_base>/build`.
    let build_root = match args.build_root.as_deref() {
        Some(raw) => {
            let p = Path::new(raw);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                cwd.join(p)
            }
        }
        None => app_base.join("build"),
    };

    let sdk_root = resolve_sdk_root(g, &cli_workspace_root(g));
    let metadata_root = sdk_root.as_ref().map(|r| r.join("metadata"));

    // Load + version-guard the manifest — faithful hard error (exit 1).
    let manifest_path = build_root.join("system-manifest.yaml");
    let manifest = match std::fs::read_to_string(&manifest_path) {
        Ok(yaml) => match parse_system_manifest(&yaml) {
            Ok(m) => m,
            Err(e) => {
                return error_run(
                    g,
                    project,
                    "size.manifest-invalid",
                    &format!(
                        "{manifest_path}: {e}",
                        manifest_path = manifest_path.display()
                    ),
                );
            }
        },
        Err(e) => {
            return error_run(
                g,
                project,
                "size.manifest-unavailable",
                &format!(
                    "no system-manifest.yaml at {}; run `tan build` first ({e}).",
                    manifest_path.display()
                ),
            );
        }
    };

    let sku = args.board.clone().or_else(|| {
        let s = manifest.hw_info.sku.trim();
        (!s.is_empty()).then(|| s.to_string())
    });

    let size_bin = SIZE_TOOLS.iter().find(|t| command_on_path(t)).copied();

    let rows: Vec<SliceSize> = manifest
        .slices
        .iter()
        .map(|slice| {
            measure_slice(
                slice,
                &build_root,
                sku.as_deref(),
                metadata_root.as_deref(),
                size_bin,
            )
        })
        .collect();

    // Assemble output.
    let mut text: Vec<String> = Vec::new();
    let mut issues: Vec<Issue> = Vec::new();
    let mut exit = ExitCode::Success;

    if !g.is_json() {
        let use_color = Theme::from_args(g).color();
        text.extend(render_table_lines(&rows, use_color));
        if size_bin.is_none() {
            text.push(
                "size: no size tool on PATH (arm-zephyr-eabi-size / llvm-size / size); \
                 fell back to rom.json+ram.json."
                    .to_string(),
            );
        }
    }

    if args.fail_over_budget {
        let unknown = unknown_budget_rows(&rows);
        if !unknown.is_empty() {
            let names = join_core_ids(&unknown);
            let message = format!(
                "size: budget unknown for [{names}] — skipped by --fail-over-budget (no guess)."
            );
            if !g.is_json() {
                text.push(message.clone());
            }
            issues.push(issue("size.budget-unknown", "info", &message));
        }
        let over = over_budget_rows(&rows);
        if !over.is_empty() {
            let names = join_core_ids(&over);
            let message = format!("size: over budget: [{names}].");
            if !g.is_json() {
                text.push(message.clone());
            }
            issues.push(issue("size.over-budget", "error", &message));
            exit = ExitCode::RuntimeFailure;
        }
    }

    if g.is_json() {
        let data = build_size_report(&rows);
        let json = Envelope::new("size", project, data, issues, exit.code()).to_json();
        CommandRun {
            exit,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit,
            text,
            json: None,
        }
    }
}

/// Comma-join the core ids of a set of rows (the `[a, b]` list in the notes).
fn join_core_ids(rows: &[&SliceSize]) -> String {
    rows.iter()
        .map(|r| r.core_id.as_str())
        .collect::<Vec<_>>()
        .join(", ")
}

/// Anchor a manifest path string: absolute as-is, relative under `build_root`.
fn anchor_under(path: &str, build_root: &Path) -> PathBuf {
    let p = Path::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        build_root.join(p)
    }
}

/// Resolve a slice's build directory: the manifest `build_dir` (anchored to
/// `build_root` when relative) else the canonical `<core_id>-<os>`.
fn slice_build_dir(slice: &Slice, build_root: &Path) -> PathBuf {
    match slice.build_dir.as_deref().filter(|s| !s.is_empty()) {
        Some(bd) => {
            let p = Path::new(bd);
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                build_root.join(p)
            }
        }
        None => build_root.join(format!("{}-{}", slice.core_id, slice.os)),
    }
}

/// Measure + budget one manifest slice into a `SliceSize` row.
fn measure_slice(
    slice: &Slice,
    build_root: &Path,
    sku: Option<&str>,
    metadata_root: Option<&Path>,
    size_bin: Option<&str>,
) -> SliceSize {
    let core_id = slice.core_id.clone();
    let os = slice.os.clone();

    if os != "zephyr" {
        return SliceSize {
            core_id,
            os,
            status: "n/a".to_string(),
            flash_used: None,
            flash_total: None,
            ram_used: None,
            ram_total: None,
            source: None,
            note: Some("no Zephyr image (Yocto/baremetal)".to_string()),
            notes: Vec::new(),
        };
    }

    let build_dir = slice_build_dir(slice, build_root);
    // Prefer the real artefact the build recorded; else re-derive from build_dir.
    let elf = match slice.output_artefact.as_deref().filter(|s| !s.is_empty()) {
        Some(art) => anchor_under(art, build_root),
        None => build_dir.join("zephyr").join("zephyr.elf"),
    };
    let (sizes, source) = extract_sizes(&elf, &build_dir, size_bin);
    let budget = resolve_slice_budget(sku, &core_id, metadata_root);

    match sizes {
        None => SliceSize {
            core_id,
            os,
            status: "not-built".to_string(),
            flash_used: None,
            flash_total: budget.flash_total,
            ram_used: None,
            ram_total: budget.ram_total,
            source: None,
            note: budget.note,
            notes: vec![format!("no footprint source at {}", elf.display())],
        },
        Some((flash_used, ram_used)) => {
            let status = if budget.flash_total.is_none() && budget.ram_total.is_none() {
                "no-budget".to_string()
            } else {
                classify(
                    Some(flash_used),
                    budget.flash_total,
                    Some(ram_used),
                    budget.ram_total,
                )
                .to_string()
            };
            SliceSize {
                core_id,
                os,
                status,
                flash_used: Some(flash_used),
                flash_total: budget.flash_total,
                ram_used: Some(ram_used),
                ram_total: budget.ram_total,
                source,
                note: budget.note,
                notes: Vec::new(),
            }
        }
    }
}

/// Resolve `(flash, ram)` bytes via the best available source: the size tool
/// (only if the elf exists), else the `rom.json` + `ram.json` footprint files.
/// Returns the sizes and the source label, or `(None, None)` when neither
/// yields a measurement.
fn extract_sizes(
    elf: &Path,
    build_dir: &Path,
    size_bin: Option<&str>,
) -> (Option<(u64, u64)>, Option<String>) {
    if elf.is_file() {
        if let Some(bin) = size_bin {
            if let Some(sizes) = sizes_from_size_tool(bin, elf) {
                return (Some(sizes), Some("size-tool".to_string()));
            }
        }
    }
    let rom = std::fs::read_to_string(build_dir.join("rom.json"))
        .ok()
        .and_then(|t| footprint_total(&t));
    let ram = std::fs::read_to_string(build_dir.join("ram.json"))
        .ok()
        .and_then(|t| footprint_total(&t));
    match (rom, ram) {
        (Some(rom), Some(ram)) => (Some((rom, ram)), Some("rom/ram.json".to_string())),
        _ => (None, None),
    }
}

/// Run the size tool on `elf` and parse its Berkeley output; `None` when the
/// tool fails to spawn, exits non-zero, or its output doesn't parse.
fn sizes_from_size_tool(size_bin: &str, elf: &Path) -> Option<(u64, u64)> {
    let out = std::process::Command::new(size_bin)
        .arg(elf)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_berkeley_size(&String::from_utf8_lossy(&out.stdout))
}

/// Resolve one core's `(flash_total, ram_total)` budget from the SoM preset +
/// SoC JSON under `<sdk>/metadata`. Mirrors `alp_size.resolve_budget`: the pure
/// FLASH/RAM/note logic is in `tan_core::size`, this reads the files.
fn resolve_slice_budget(
    sku: Option<&str>,
    core_id: &str,
    metadata_root: Option<&Path>,
) -> MemoryBudget {
    let Some(sku) = sku else {
        return note_only("no SKU in manifest");
    };
    let Some(meta) = metadata_root else {
        return note_only(&format!("no SoM preset for {sku}"));
    };

    let preset_path = meta.join("e1m_modules").join(format!("{sku}.yaml"));
    if !preset_path.is_file() {
        return note_only(&format!("no SoM preset for {sku}"));
    }
    // Reuse the shared SoM parser (drops `TBD` sentinels to None, exactly what
    // variant resolution wants) instead of a second yaml reader in the CLI.
    let preset = match std::fs::read_to_string(&preset_path) {
        Ok(text) => match parse_som_preset(&text) {
            Ok(p) => p,
            Err(_) => return note_only(&format!("unreadable SoM preset for {sku}")),
        },
        Err(_) => return note_only(&format!("unreadable SoM preset for {sku}")),
    };

    let silicon_variant = preset.silicon_variant.as_deref();

    let mut variants: Vec<SocVariant> = Vec::new();
    let mut soc_flash_mb: Option<f64> = None;
    let mut soc_cores: Vec<(String, Option<f64>)> = Vec::new();

    if !preset.silicon.is_empty() {
        let parts: Vec<&str> = preset.silicon.split(':').collect();
        if parts.len() == 3 {
            let soc_path = meta
                .join("socs")
                .join(parts[0])
                .join(parts[1])
                .join(format!("{}.json", parts[2]));
            if let Ok(text) = std::fs::read_to_string(&soc_path) {
                if let Ok(soc) = serde_json::from_str::<serde_json::Value>(&text) {
                    if let Some(vs) = soc.get("variants") {
                        variants = serde_json::from_value(vs.clone()).unwrap_or_default();
                    }
                    soc_flash_mb = soc.get("soc_flash_mb").and_then(serde_json::Value::as_f64);
                    if let Some(cores) = soc.get("cores").and_then(serde_json::Value::as_array) {
                        soc_cores = cores
                            .iter()
                            .filter_map(|c| {
                                let id = c.get("id")?.as_str()?.to_string();
                                let tcm = c.get("tcm_kb").and_then(serde_json::Value::as_f64);
                                Some((id, tcm))
                            })
                            .collect();
                    }
                }
            }
        }
    }

    let variant = resolve_variant(silicon_variant, Some(sku), &variants);
    let mram_mb = variant.and_then(|v| v.mram_mb);
    let sram_banks = variant.map(SocVariant::sram_banks).unwrap_or_default();
    resolve_budget(core_id, mram_mb, soc_flash_mb, &sram_banks, &soc_cores)
}

/// A `MemoryBudget` carrying only an unresolved-budget note.
fn note_only(note: &str) -> MemoryBudget {
    MemoryBudget {
        flash_total: None,
        ram_total: None,
        note: Some(note.to_string()),
    }
}

/// Build one diagnostic issue.
fn issue(code: &str, severity: &str, message: &str) -> Issue {
    Issue {
        code: code.to_string(),
        severity: severity.to_string(),
        message: message.to_string(),
    }
}

/// A hard manifest error `CommandRun` (exit 1), text or JSON per format.
fn error_run(g: &GlobalArgs, project: Project, code: &str, message: &str) -> CommandRun {
    let issues = vec![issue(code, "error", message)];
    if g.is_json() {
        let data = build_size_report(&[]);
        let json = Envelope::new(
            "size",
            project,
            data,
            issues,
            ExitCode::RuntimeFailure.code(),
        )
        .to_json();
        CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: Vec::new(),
            json: Some(json),
        }
    } else {
        CommandRun {
            exit: ExitCode::RuntimeFailure,
            text: vec![format!("size: {message}")],
            json: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Format;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tan-size-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// A fake SDK with the loader marker + metadata for one SKU/SoC.
    fn fake_sdk(root: &Path, sku: &str, soc_flash_mb: f64) {
        std::fs::create_dir_all(root.join("scripts")).unwrap();
        std::fs::write(root.join("scripts").join("alp_project.py"), "").unwrap();
        let modules = root.join("metadata").join("e1m_modules");
        std::fs::create_dir_all(&modules).unwrap();
        std::fs::write(
            modules.join(format!("{sku}.yaml")),
            format!("sku: {sku}\nsilicon: test:fam:part\n"),
        )
        .unwrap();
        let soc_dir = root.join("metadata").join("socs").join("test").join("fam");
        std::fs::create_dir_all(&soc_dir).unwrap();
        std::fs::write(
            soc_dir.join("part.json"),
            format!(
                "{{\"soc_flash_mb\": {soc_flash_mb}, \"cores\": [{{\"id\": \"m55_hp\", \"tcm_kb\": 1280}}]}}"
            ),
        )
        .unwrap();
    }

    /// Write a one-slice manifest + its rom.json/ram.json footprint.
    fn manifest_with_footprint(build_root: &Path, sku: &str, rom: u64, ram: u64) {
        std::fs::create_dir_all(build_root).unwrap();
        std::fs::write(
            build_root.join("system-manifest.yaml"),
            format!(
                "schema_version: 1\nhw_info:\n  sku: {sku}\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
            ),
        )
        .unwrap();
        let bd = build_root.join("m55_hp-zephyr");
        std::fs::create_dir_all(&bd).unwrap();
        std::fs::write(
            bd.join("rom.json"),
            format!("{{\"symbols\":{{\"size\":{rom}}}}}"),
        )
        .unwrap();
        std::fs::write(
            bd.join("ram.json"),
            format!("{{\"symbols\":{{\"size\":{ram}}}}}"),
        )
        .unwrap();
    }

    fn global(sdk: &Path, format: Format) -> GlobalArgs {
        GlobalArgs {
            project: None,
            board_yaml: None,
            sdk_root: Some(sdk.to_string_lossy().into_owned()),
            target: None,
            all: false,
            format,
            verbose: false,
            quiet: false,
            no_color: true,
            non_interactive: false,
            ci: false,
        }
    }

    fn size_args(build_root: &Path, fail_over_budget: bool) -> SizeArgs {
        SizeArgs {
            app_path: ".".to_string(),
            build_root: Some(build_root.to_string_lossy().into_owned()),
            board: None,
            fail_over_budget,
        }
    }

    #[test]
    fn missing_manifest_exits_one() {
        let sdk = tmp("nomani-sdk");
        fake_sdk(&sdk, "E1M-TEST", 5.5);
        let build_root = tmp("nomani-build"); // exists but no manifest
        let g = global(&sdk, Format::Text);
        let run = run(&g, &size_args(&build_root, false));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        assert!(
            run.text
                .iter()
                .any(|l| l.contains("no system-manifest.yaml"))
        );
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn missing_manifest_json_carries_issue_code() {
        let sdk = tmp("nomani-json-sdk");
        fake_sdk(&sdk, "E1M-TEST", 5.5);
        let build_root = tmp("nomani-json-build");
        let g = global(&sdk, Format::Json);
        let run = run(&g, &size_args(&build_root, false));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["command"], "size");
        assert_eq!(doc["ok"], false);
        assert_eq!(doc["issues"][0]["code"], "size.manifest-unavailable");
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn measured_slice_reports_row_via_footprint_json() {
        let sdk = tmp("ok-sdk");
        fake_sdk(&sdk, "E1M-TEST", 5.5); // 5.5 MiB flash budget -> ok
        let build_root = tmp("ok-build");
        manifest_with_footprint(&build_root, "E1M-TEST", 4096, 2048);
        let g = global(&sdk, Format::Json);
        let run = run(&g, &size_args(&build_root, false));
        assert_eq!(run.exit, ExitCode::Success);
        let doc: serde_json::Value = serde_json::from_str(run.json.as_deref().unwrap()).unwrap();
        assert_eq!(doc["data"]["schema"], "alp-size/1");
        let slice = &doc["data"]["slices"][0];
        assert_eq!(slice["core_id"], "m55_hp");
        assert_eq!(slice["status"], "ok");
        assert_eq!(slice["source"], "rom/ram.json");
        assert_eq!(slice["flash"]["used"], 4096);
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn fail_over_budget_exits_one_when_over() {
        let sdk = tmp("over-sdk");
        fake_sdk(&sdk, "E1M-TEST", 0.0001); // ~104-byte flash budget -> over
        let build_root = tmp("over-build");
        manifest_with_footprint(&build_root, "E1M-TEST", 100_000, 100_000);
        let g = global(&sdk, Format::Text);
        let run = run(&g, &size_args(&build_root, true));
        assert_eq!(run.exit, ExitCode::RuntimeFailure);
        assert!(run.text.iter().any(|l| l.contains("over budget")));
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }

    #[test]
    fn fail_over_budget_skips_unknown_budget() {
        // No preset for this SKU -> budget unknown -> no-budget row, exit 0.
        let sdk = tmp("unknown-sdk");
        std::fs::create_dir_all(sdk.join("scripts")).unwrap();
        std::fs::write(sdk.join("scripts").join("alp_project.py"), "").unwrap();
        let build_root = tmp("unknown-build");
        manifest_with_footprint(&build_root, "E1M-NOPRESET", 4096, 2048);
        let g = global(&sdk, Format::Text);
        let run = run(&g, &size_args(&build_root, true));
        assert_eq!(run.exit, ExitCode::Success);
        assert!(
            run.text
                .iter()
                .any(|l| l.contains("budget unknown") && l.contains("m55_hp")),
            "{:?}",
            run.text
        );
        std::fs::remove_dir_all(&sdk).unwrap();
        std::fs::remove_dir_all(&build_root).unwrap();
    }
}
