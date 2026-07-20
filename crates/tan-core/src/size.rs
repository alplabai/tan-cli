// SPDX-License-Identifier: Apache-2.0
//! Pure footprint model for `tan size` — the deterministic half of the native
//! port of `west alp-size` (`scripts/west_commands/alp_size.py`). Measurement
//! source selection, budget resolution, classification, and rendering all live
//! here as IO-free functions; the subprocess + filesystem wiring is in
//! `tan-cli/src/commands/size.rs`.
//!
//! FLASH = text+data (everything in the flashed image); RAM = data+bss
//! (everything live at runtime) — the Berkeley `size` model, where binutils
//! folds .rodata into text and .noinit (NOBITS) into bss.

use object::{BinaryFormat, Object, ObjectSection, SectionFlags, SectionKind};
use serde::Deserialize;
use serde_json::{Value, json};

/// ELF `sh_flags` bits used to classify a section (`SHF_WRITE` / `SHF_ALLOC`).
const SHF_WRITE: u64 = 0x1;
const SHF_ALLOC: u64 = 0x2;

/// A slice at/above this fraction of its budget is flagged `warn` even though it
/// still fits — a pre-flight "you're close" nudge.
pub const WARN_FRACTION: f64 = 0.90;

// colorama Fore/Style literals, so the color path matches the retired command's
// bytes. tan-core has no owo-colors dep; these are the same ANSI codes.
const GREEN: &str = "\x1b[32m";
const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";
const CYAN: &str = "\x1b[36m";
const RESET: &str = "\x1b[0m";

/// Parse Berkeley-format `size` output into `(flash, ram)` bytes. Columns are
/// `text data bss dec hex filename`; the first row whose first three
/// whitespace-split fields all parse as ints wins (header/noise rows skipped).
/// `FLASH = text+data`, `RAM = data+bss`. `None` when no data row is found.
pub fn parse_berkeley_size(text: &str) -> Option<(u64, u64)> {
    for line in text.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 3 {
            continue;
        }
        let (Ok(text_b), Ok(data_b), Ok(bss_b)) = (
            parts[0].parse::<u64>(),
            parts[1].parse::<u64>(),
            parts[2].parse::<u64>(),
        ) else {
            continue; // header row ("text data bss ...") or noise
        };
        return Some((text_b + data_b, data_b + bss_b));
    }
    None
}

/// Sum ELF section sizes into `(flash, ram)` bytes with Berkeley-`size`
/// semantics, straight from the section headers — the middle rung between the
/// external size tool and the `rom/ram.json` fallback, so a present `elf` is
/// measured even with no `arm-zephyr-eabi-size` on PATH.
///
/// FLASH = every allocated section that occupies the image (`SHF_ALLOC` &&
/// PROGBITS: `.text` + `.rodata` + `.data`) — binutils' `text`+`data` columns.
/// RAM = every allocated writable section, NOBITS included (`SHF_ALLOC` &&
/// `SHF_WRITE`: `.data` + `.bss` + `.noinit`) — binutils' `data`+`bss` columns.
/// So the pair matches what `arm-zephyr-eabi-size` reports for the same elf.
/// `None` when the bytes don't parse as an object file, aren't ELF, or carry no
/// allocated section (a relocatable `.o`, a stripped partial-link object, or a
/// parseable-but-wrong container like PE/Mach-O/wasm) — the caller falls
/// through to the next measurement rung instead of reporting a fake 0-byte
/// size. Handles ELF32/ELF64 and either endianness (whatever the SoM core
/// emits). Pure.
pub fn sizes_from_elf_sections(elf_bytes: &[u8]) -> Option<(u64, u64)> {
    let file = object::File::parse(elf_bytes).ok()?;
    // `object::File::parse` also accepts PE/COFF, Mach-O and wasm; those never
    // carry `SectionFlags::Elf`, so the loop below would silently see zero
    // sections and used to fall through to `Some((0, 0))` — an indistinguishable
    // "measured, image is empty" instead of "wrong file, don't trust this".
    if file.format() != BinaryFormat::Elf {
        return None;
    }
    let mut flash = 0u64;
    let mut ram = 0u64;
    let mut saw_alloc = false;
    for section in file.sections() {
        let SectionFlags::Elf { sh_flags } = section.flags() else {
            continue; // not an ELF section (shouldn't happen for a parsed ELF)
        };
        if sh_flags & SHF_ALLOC == 0 {
            continue; // non-allocated (.symtab, .debug_*, .comment, …) — neither region
        }
        saw_alloc = true;
        let size = section.size();
        // sh_size is an unvalidated header field object doesn't bounds-check
        // against file length for sections whose data is never read; a
        // corrupt/adversarial ELF can carry a huge value. Saturate instead of
        // wrapping (release, opt-level="z", no overflow-checks) or panicking
        // (debug/test, panic="abort" — an abort emits no envelope at all).
        //
        // NOBITS (.bss/.noinit) occupies RAM but not the image; PROGBITS (.text/
        // .rodata/.data) occupies both the image and, when writable, RAM.
        if section.kind() != SectionKind::UninitializedData {
            flash = flash.saturating_add(size);
        }
        if sh_flags & SHF_WRITE != 0 {
            ram = ram.saturating_add(size);
        }
    }
    // No allocated section at all (valid ELF, but nothing occupies flash/ram) —
    // report unmeasured rather than a fake 0/0 that reads as "fits easily".
    saw_alloc.then_some((flash, ram))
}

/// Total bytes from a Zephyr `rom.json` / `ram.json` footprint document. The
/// footprint root is `{"symbols": {"size": <total>, ...}}`; older layouts put
/// the total at the top level. `None` when the JSON is malformed or neither
/// shape yields an integer.
pub fn footprint_total(json_text: &str) -> Option<u64> {
    let data: Value = serde_json::from_str(json_text).ok()?;
    if let Some(size) = data.get("symbols").and_then(|s| s.get("size")) {
        if let Some(n) = size.as_u64() {
            return Some(n);
        }
    }
    data.get("size").and_then(Value::as_u64)
}

/// Round to 1 decimal to match Python `round(x, 1)` so the emitted `pct` values
/// stay byte-identical with the retired command. Formatting to one decimal place
/// rounds the TRUE binary value of `x` (both CPython's `round` and Rust's `{:.1}`
/// use correctly-rounded shortest-decimal), so the X.X5 tie cases agree — e.g.
/// 0.35 (actually 0.34999…) → 0.3, not the 0.4 a `(x*10).round()/10` multiply
/// yields. This also matches the text-cell path (`region_cell`), which already
/// format-rounds.
pub fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse::<f64>().unwrap_or(x)
}

/// A `{used, total, pct}` region object. `pct = round1(used/total*100)` when
/// `used` is present and `total` is present + non-zero, else `null`.
pub fn region_json(used: Option<u64>, total: Option<u64>) -> Value {
    let pct = match (used, total) {
        (Some(u), Some(t)) if t != 0 => Some(round1(u as f64 / t as f64 * 100.0)),
        _ => None,
    };
    json!({ "used": used, "total": total, "pct": pct })
}

/// `m55_hp` -> `M55_HP` — the token embedded in `sram_banks_kb` keys like
/// `SRAM3_M55_HP_DTCM`.
pub fn core_token(core_id: &str) -> String {
    core_id.to_uppercase()
}

/// `ok` / `warn` / `over` from the worst of the two regions. A region with a
/// missing `used`, or a `total` that is missing or zero, is ignored (no
/// div-by-zero, no guess).
pub fn classify(
    flash_used: Option<u64>,
    flash_total: Option<u64>,
    ram_used: Option<u64>,
    ram_total: Option<u64>,
) -> &'static str {
    let mut worst = "ok";
    for (used, total) in [(flash_used, flash_total), (ram_used, ram_total)] {
        let (Some(u), Some(t)) = (used, total) else {
            continue;
        };
        if t == 0 {
            continue;
        }
        let frac = u as f64 / t as f64;
        if frac > 1.0 {
            return "over";
        }
        if frac >= WARN_FRACTION {
            worst = "warn";
        }
    }
    worst
}

/// A resolved per-slice FLASH/RAM budget in bytes, plus a human note when a
/// coarser fallback was used.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct MemoryBudget {
    /// On-die program flash budget in bytes, if resolved.
    pub flash_total: Option<u64>,
    /// Per-core data RAM budget in bytes, if resolved.
    pub ram_total: Option<u64>,
    /// Human note when a coarser fallback source was used (`;`-joined).
    pub note: Option<String>,
}

/// One SoC-JSON `variants[]` entry — only the fields the budget needs. Ordering
/// of `sram_banks_kb` is preserved (serde_json `preserve_order`) so the
/// first-matching-bank rule is deterministic.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SocVariant {
    /// The variant's order code (matched against a preset's `silicon_variant`).
    #[serde(default)]
    pub order_code: Option<String>,
    /// Module SKUs this variant backs (reverse-match key).
    #[serde(default)]
    pub alp_module_skus: Vec<String>,
    /// On-die MRAM in megabytes, when declared.
    #[serde(default)]
    pub mram_mb: Option<f64>,
    /// SRAM banks keyed by name (e.g. `SRAM3_M55_HP_DTCM`), in KiB.
    #[serde(default)]
    pub sram_banks_kb: serde_json::Map<String, Value>,
}

impl SocVariant {
    /// The `sram_banks_kb` map as ordered `(name, kib)` pairs, dropping
    /// non-numeric entries.
    pub fn sram_banks(&self) -> Vec<(String, f64)> {
        self.sram_banks_kb
            .iter()
            .filter_map(|(k, v)| v.as_f64().map(|f| (k.clone(), f)))
            .collect()
    }
}

/// Resolve a SoM preset to its matching SoC-JSON variant: forward via
/// `silicon_variant == order_code` (skipping empty / `TBD`), then reverse via
/// `sku ∈ alp_module_skus`. `None` when neither path resolves.
pub fn resolve_variant<'a>(
    silicon_variant: Option<&str>,
    sku: Option<&str>,
    variants: &'a [SocVariant],
) -> Option<&'a SocVariant> {
    if let Some(declared) = silicon_variant {
        if !declared.is_empty() && declared != "TBD" {
            if let Some(v) = variants
                .iter()
                .find(|v| v.order_code.as_deref() == Some(declared))
            {
                return Some(v);
            }
            // Forward declared but not found — fall through to reverse lookup.
        }
    }
    let sku = sku?;
    variants
        .iter()
        .find(|v| v.alp_module_skus.iter().any(|s| s == sku))
}

/// The FLASH/RAM budget for one core, from values already read out of the SoM
/// preset + SoC JSON. FLASH: variant `mram_mb`, else SoC `soc_flash_mb` (with a
/// note); RAM: the `*_DTCM` bank whose name contains the core token, else the
/// core's `tcm_kb` (with a note). Any field that can't be resolved stays `None`
/// — the caller renders `unknown`, never a guessed number. Pure.
pub fn resolve_budget(
    core_id: &str,
    mram_mb: Option<f64>,
    soc_flash_mb: Option<f64>,
    sram_banks_kb: &[(String, f64)],
    soc_cores: &[(String, Option<f64>)],
) -> MemoryBudget {
    let mut notes: Vec<String> = Vec::new();

    // FLASH: on-die program flash.
    let flash_total = if let Some(mb) = mram_mb {
        Some((mb * 1024.0 * 1024.0) as u64)
    } else if let Some(mb) = soc_flash_mb {
        notes.push("flash=soc_flash_mb".to_string());
        Some((mb * 1024.0 * 1024.0) as u64)
    } else {
        None
    };

    // RAM: per-core data RAM.
    let token = core_token(core_id);
    let mut ram_total = sram_banks_kb
        .iter()
        .find(|(name, _)| name.contains(&token) && name.contains("DTCM"))
        .map(|(_, kib)| (*kib * 1024.0) as u64);
    if ram_total.is_none() {
        for (id, tcm_kb) in soc_cores {
            if id == core_id {
                if let Some(tcm) = tcm_kb {
                    ram_total = Some((*tcm * 1024.0) as u64);
                    notes.push("ram=core tcm_kb (ITCM+DTCM)".to_string());
                }
                break;
            }
        }
    }

    let note = if notes.is_empty() {
        None
    } else {
        Some(notes.join("; "))
    };
    MemoryBudget {
        flash_total,
        ram_total,
        note,
    }
}

/// One slice's measured footprint vs its resolved budget. `status` is one of
/// `ok`/`over`/`warn`/`not-built`/`n/a`/`no-budget`.
#[derive(Debug, Clone)]
pub struct SliceSize {
    /// Core id this slice targets.
    pub core_id: String,
    /// Resolved runtime (`zephyr`, `yocto`, …).
    pub os: String,
    /// Row status vocabulary term.
    pub status: String,
    /// Measured FLASH bytes, when a source yielded a measurement.
    pub flash_used: Option<u64>,
    /// FLASH budget bytes, when resolved.
    pub flash_total: Option<u64>,
    /// Measured RAM bytes, when a source yielded a measurement.
    pub ram_used: Option<u64>,
    /// RAM budget bytes, when resolved.
    pub ram_total: Option<u64>,
    /// Measurement source label (`size-tool`, `pyelftools`, `rom/ram.json`).
    pub source: Option<String>,
    /// Budget/`n/a` note surfaced as `budget_note` in JSON + the table detail.
    pub note: Option<String>,
    /// Additional notes (e.g. the missing-footprint path for a not-built slice).
    pub notes: Vec<String>,
}

impl SliceSize {
    /// True when BOTH regions' budgets resolved to a nonzero total. This used
    /// to be an OR (either region resolved), which let a half-resolved budget
    /// report as "known": `classify` silently skips whichever region has
    /// `total: None | Some(0)` (correct in isolation — no guess, no
    /// div-by-zero), so with the OR that skip was never surfaced anywhere —
    /// `--fail-over-budget` never checked the unresolved region and
    /// `summary.unknown_budget` never named the slice. A `total` of exactly
    /// `Some(0)` (a saturated `mram_mb`/`soc_flash_mb` of 0/negative/NaN, see
    /// `resolve_budget`) is treated the same as unresolved, matching what
    /// `classify` already skips.
    pub fn budget_fully_known(&self) -> bool {
        Self::region_resolved(self.flash_total) && Self::region_resolved(self.ram_total)
    }

    fn region_resolved(total: Option<u64>) -> bool {
        total.is_some_and(|t| t != 0)
    }

    /// True when this slice is over its budget.
    pub fn over_budget(&self) -> bool {
        self.status == "over"
    }

    /// This slice's `alp-size/1` JSON entry (preserve_order: core_id, os,
    /// status, flash, ram, source, then optional budget_note + notes).
    pub fn to_json_entry(&self) -> Value {
        let mut entry = json!({
            "core_id": self.core_id,
            "os": self.os,
            "status": self.status,
            "flash": region_json(self.flash_used, self.flash_total),
            "ram": region_json(self.ram_used, self.ram_total),
            "source": self.source,
        });
        if let Some(note) = self.note.as_deref().filter(|n| !n.is_empty()) {
            entry["budget_note"] = json!(note);
        }
        if !self.notes.is_empty() {
            entry["notes"] = json!(self.notes);
        }
        entry
    }
}

/// The full `alp-size/1` report payload (the `data` sub-shape wrapped verbatim
/// in the tan Envelope): `{schema, slices, summary{over_budget, unknown_budget}}`.
pub fn build_size_report(rows: &[SliceSize]) -> Value {
    let slices: Vec<Value> = rows.iter().map(SliceSize::to_json_entry).collect();

    let mut over: Vec<String> = rows
        .iter()
        .filter(|r| r.over_budget())
        .map(|r| r.core_id.clone())
        .collect();
    over.sort();

    let mut unknown: Vec<String> = rows
        .iter()
        .filter(|r| r.os == "zephyr" && r.status != "not-built" && !r.budget_fully_known())
        .map(|r| r.core_id.clone())
        .collect();
    unknown.sort();

    json!({
        "schema": "alp-size/1",
        "slices": slices,
        "summary": {
            "over_budget": over,
            "unknown_budget": unknown,
        },
    })
}

/// Slices that are over budget.
pub fn over_budget_rows(rows: &[SliceSize]) -> Vec<&SliceSize> {
    rows.iter().filter(|r| r.over_budget()).collect()
}

/// Zephyr slices that were measured but whose budget couldn't be fully
/// resolved (either region, not just both) — the ones `--fail-over-budget`
/// skips. Keyed on the resolved totals directly (`budget_fully_known`)
/// rather than `status == "no-budget"`: the caller only sets that status when
/// BOTH regions are unresolved, so a half-resolved budget (one region known,
/// the other silently skipped by `classify`) used to report as `ok`/`warn`/
/// `over` here and never surface as unknown at all.
pub fn unknown_budget_rows(rows: &[SliceSize]) -> Vec<&SliceSize> {
    rows.iter()
        .filter(|r| r.os == "zephyr" && r.status != "not-built" && !r.budget_fully_known())
        .collect()
}

/// Bytes -> a compact KiB/MiB string, or `?` for unknown.
pub fn human_bytes(n: Option<u64>) -> String {
    match n {
        None => "?".to_string(),
        Some(n) if n >= 1024 * 1024 => format!("{:.2}M", n as f64 / (1024.0 * 1024.0)),
        Some(n) if n >= 1024 => format!("{:.1}K", n as f64 / 1024.0),
        Some(n) => format!("{n}B"),
    }
}

/// A `used/total pct%` table cell.
pub fn region_cell(used: Option<u64>, total: Option<u64>) -> String {
    let pct = match (used, total) {
        (Some(u), Some(t)) if t != 0 => format!("{:5.1}%", u as f64 / t as f64 * 100.0),
        _ => "   -  ".to_string(),
    };
    format!("{:>8}/{:<8} {}", human_bytes(used), human_bytes(total), pct)
}

/// Status hue + label for the table. Unknown statuses render plain with the raw
/// status text.
fn status_hue(status: &str) -> (&'static str, &str) {
    match status {
        "ok" => (GREEN, "OK"),
        "warn" => (YELLOW, "WARN"),
        "over" => (RED, "OVER"),
        "not-built" => (YELLOW, "not built"),
        "n/a" => (CYAN, "n/a"),
        "no-budget" => (CYAN, "no budget"),
        other => ("", other),
    }
}

/// Render the per-slice table as lines: the `CORE / OS / FLASH / RAM / STATUS`
/// header, a separator, one row per slice, and a `-> <detail>` continuation for
/// any slice carrying a note. `use_color` gates the ANSI status coloring.
pub fn render_table_lines(rows: &[SliceSize], use_color: bool) -> Vec<String> {
    let head = format!(
        "{:<14} {:<10} {:<24} {:<24} STATUS",
        "CORE", "OS", "FLASH used/total", "RAM used/total"
    );
    let mut lines = vec![head.clone(), "-".repeat(head.len())];
    for r in rows {
        let (hue, label) = status_hue(&r.status);
        let status = if use_color && !hue.is_empty() {
            format!("{hue}{label}{RESET}")
        } else {
            label.to_string()
        };
        lines.push(format!(
            "{:<14} {:<10} {:<24} {:<24} {}",
            r.core_id,
            r.os,
            region_cell(r.flash_used, r.flash_total),
            region_cell(r.ram_used, r.ram_total),
            status
        ));
        let detail = r
            .note
            .as_deref()
            .or_else(|| r.notes.first().map(|s| s.as_str()));
        if let Some(detail) = detail {
            lines.push(format!("{:<14} {:<10} -> {}", "", "", detail));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn variant(json_text: &str) -> SocVariant {
        serde_json::from_str(json_text).unwrap()
    }

    #[test]
    fn parse_berkeley_size_skips_header_and_sums_columns() {
        let out = "   text\t   data\t    bss\t    dec\t    hex\tfilename\n  12345\t    678\t   9012\t  22035\t   5613\tzephyr.elf\n";
        assert_eq!(parse_berkeley_size(out), Some((12345 + 678, 678 + 9012)));
        assert_eq!(parse_berkeley_size(""), None);
        // Header-only: no numeric data row.
        assert_eq!(parse_berkeley_size("text data bss dec hex filename"), None);
        assert_eq!(parse_berkeley_size("garbage line here"), None);
    }

    #[test]
    fn sizes_from_elf_sections_sums_berkeley_columns() {
        use object::write::{Object as WObject, StandardSection};
        use object::{Architecture, BinaryFormat, Endianness};

        let mut obj = WObject::new(BinaryFormat::Elf, Architecture::Arm, Endianness::Little);
        // .text 100 (ALLOC|EXECINSTR, PROGBITS) -> FLASH only.
        let text = obj.section_id(StandardSection::Text);
        obj.append_section_data(text, &[0u8; 100], 1);
        // .rodata 20 (ALLOC, PROGBITS) -> FLASH only.
        let rodata = obj.section_id(StandardSection::ReadOnlyData);
        obj.append_section_data(rodata, &[0u8; 20], 1);
        // .data 40 (ALLOC|WRITE, PROGBITS) -> FLASH + RAM.
        let data = obj.section_id(StandardSection::Data);
        obj.append_section_data(data, &[0u8; 40], 1);
        // .bss 200 (ALLOC|WRITE, NOBITS) -> RAM only.
        let bss = obj.section_id(StandardSection::UninitializedData);
        obj.append_section_bss(bss, 200, 1);

        let bytes = obj.write().unwrap();
        // FLASH = text+rodata+data = 160 (binutils text+data column);
        // RAM = data+bss = 240 (binutils data+bss column).
        assert_eq!(sizes_from_elf_sections(&bytes), Some((160, 240)));
        // Non-object bytes -> None, never a panic.
        assert_eq!(sizes_from_elf_sections(b"not an elf"), None);
    }

    /// A parseable-but-wrong container (PE/COFF here, standing in for
    /// PE/Mach-O/wasm) used to fall through the `SectionFlags::Elf` match on
    /// every section and land on `Some((0, 0))` — an "empty but measured"
    /// result indistinguishable from a real 0-byte image. Must be `None` so
    /// the caller (`extract_sizes` in tan-cli) falls through to the next
    /// measurement rung instead of reporting a fake pass.
    #[test]
    fn sizes_from_elf_sections_rejects_non_elf_container() {
        use object::write::Object as WObject;
        use object::{Architecture, BinaryFormat, Endianness};

        let obj = WObject::new(BinaryFormat::Coff, Architecture::X86_64, Endianness::Little);
        let bytes = obj.write().unwrap();
        assert_eq!(sizes_from_elf_sections(&bytes), None);
    }

    /// A valid ELF with no `SHF_ALLOC` section at all (relocatable `.o`,
    /// stripped/partial-link object) must also be `None`, not `Some((0, 0))`
    /// — same "fake empty measurement" hole as the non-ELF case above.
    #[test]
    fn sizes_from_elf_sections_rejects_elf_with_no_allocated_sections() {
        use object::write::Object as WObject;
        use object::{Architecture, BinaryFormat, Endianness};

        let obj = WObject::new(BinaryFormat::Elf, Architecture::Arm, Endianness::Little);
        // No sections appended -> only the null section (+ shstrtab), never
        // SHF_ALLOC.
        let bytes = obj.write().unwrap();
        assert_eq!(sizes_from_elf_sections(&bytes), None);
    }

    /// A corrupt/adversarial ELF whose section-header table still parses but
    /// whose `sh_size` fields are huge must saturate, not silently wrap
    /// (release, opt-level="z", no overflow-checks) or panic (debug,
    /// `panic="abort"` — an abort emits no envelope at all). `object` bounds-
    /// checks the header *table* itself but not an individual section's
    /// declared `sh_size` for a section whose bytes are never read (true of
    /// every section here — we only call `.size()`, never `.data()`).
    #[test]
    fn sizes_from_elf_sections_saturates_instead_of_overflowing() {
        use object::write::{Object as WObject, StandardSection};
        use object::{Architecture, BinaryFormat, Endianness};

        // Aarch64 forces a 64-bit ELF class so the hand-patched header
        // offsets below (Elf64_Shdr layout) are correct.
        let mut obj = WObject::new(BinaryFormat::Elf, Architecture::Aarch64, Endianness::Little);
        let text = obj.section_id(StandardSection::Text);
        obj.append_section_data(text, &[0u8; 4], 1);
        let data = obj.section_id(StandardSection::Data);
        obj.append_section_data(data, &[0u8; 4], 1);
        let bss = obj.section_id(StandardSection::UninitializedData);
        obj.append_section_bss(bss, 4, 1);
        let mut bytes = obj.write().unwrap();

        const SHF_ALLOC: u64 = 0x2;
        const SHF_WRITE: u64 = 0x1;
        const SHF_EXECINSTR: u64 = 0x4;
        const SHT_PROGBITS: u32 = 1;
        const SHT_NOBITS: u32 = 8;
        // Large enough that summing any two of these overflows a plain u64 add.
        let huge = u64::MAX - 15;

        let e_shoff = u64::from_le_bytes(bytes[0x28..0x30].try_into().unwrap()) as usize;
        let e_shentsize = u16::from_le_bytes(bytes[0x3a..0x3c].try_into().unwrap()) as usize;
        let e_shnum = u16::from_le_bytes(bytes[0x3c..0x3e].try_into().unwrap()) as usize;
        let mut patched = 0;
        for i in 1..e_shnum {
            let off = e_shoff + i * e_shentsize;
            let sh_type = u32::from_le_bytes(bytes[off + 4..off + 8].try_into().unwrap());
            let sh_flags = u64::from_le_bytes(bytes[off + 8..off + 16].try_into().unwrap());
            let is_text = sh_type == SHT_PROGBITS
                && sh_flags & (SHF_ALLOC | SHF_EXECINSTR) == (SHF_ALLOC | SHF_EXECINSTR);
            let is_data = sh_type == SHT_PROGBITS
                && sh_flags & (SHF_ALLOC | SHF_WRITE) == (SHF_ALLOC | SHF_WRITE)
                && sh_flags & SHF_EXECINSTR == 0;
            let is_bss = sh_type == SHT_NOBITS
                && sh_flags & (SHF_ALLOC | SHF_WRITE) == (SHF_ALLOC | SHF_WRITE);
            if is_text || is_data || is_bss {
                bytes[off + 32..off + 40].copy_from_slice(&huge.to_le_bytes());
                patched += 1;
            }
        }
        assert_eq!(
            patched, 3,
            "expected to corrupt sh_size of .text/.data/.bss"
        );

        // flash = text + data (huge+huge, overflows a plain u64 add);
        // ram = data + bss (huge+huge, overflows a plain u64 add).
        let (flash, ram) = sizes_from_elf_sections(&bytes).expect("still parses as ELF");
        assert_eq!(flash, u64::MAX);
        assert_eq!(ram, u64::MAX);
    }

    #[test]
    fn footprint_total_reads_symbols_then_top_level() {
        assert_eq!(footprint_total(r#"{"symbols":{"size":4096}}"#), Some(4096));
        assert_eq!(footprint_total(r#"{"size":2048}"#), Some(2048));
        assert_eq!(footprint_total(r#"{"symbols":{"size":"x"}}"#), None);
        assert_eq!(footprint_total("{not json"), None);
    }

    #[test]
    fn classify_ranks_worst_region() {
        // over: any region fraction > 1.0
        assert_eq!(
            classify(Some(1100), Some(1000), Some(10), Some(1000)),
            "over"
        );
        // warn: 0.90 <= frac <= 1.0
        assert_eq!(classify(Some(900), Some(1000), None, None), "warn");
        assert_eq!(classify(Some(1000), Some(1000), None, None), "warn");
        // ok: below warn
        assert_eq!(classify(Some(100), Some(1000), None, None), "ok");
        // used None / total None|0 ignored
        assert_eq!(classify(Some(10), None, None, None), "ok");
        assert_eq!(classify(Some(10), Some(0), None, None), "ok");
    }

    #[test]
    fn round1_and_region_json_pct() {
        assert_eq!(round1(91.5), 91.5);
        // 915/1000*100 = 91.5 exactly.
        let r = region_json(Some(915), Some(1000));
        assert_eq!(r["pct"], json!(91.5));
        assert_eq!(r["used"], json!(915));
        assert_eq!(r["total"], json!(1000));
        // total 0 => pct null; used None => pct null.
        assert_eq!(region_json(Some(10), Some(0))["pct"], Value::Null);
        assert_eq!(region_json(None, Some(1000))["pct"], Value::Null);
        // Matches CPython round(x,1), which rounds the TRUE binary value (not a
        // scaled x10 multiply): 0.35 is actually 0.34999... -> 0.3; 0.25 -> 0.2.
        assert_eq!(round1(0.25), 0.2);
        assert_eq!(round1(0.35), 0.3);
    }

    #[test]
    fn resolve_budget_flash_and_ram_sources() {
        // mram_mb wins, no note.
        let b = resolve_budget("m55_hp", Some(5.5), Some(4.0), &[], &[]);
        assert_eq!(b.flash_total, Some(5_767_168));
        assert_eq!(b.note, None);

        // soc_flash_mb fallback carries a note.
        let b = resolve_budget("m55_hp", None, Some(5.5), &[], &[]);
        assert_eq!(b.flash_total, Some(5_767_168));
        assert_eq!(b.note.as_deref(), Some("flash=soc_flash_mb"));

        // DTCM bank matched by core token.
        let banks = vec![
            ("SRAM2_M55_HP_ITCM".to_string(), 256.0),
            ("SRAM3_M55_HP_DTCM".to_string(), 1024.0),
        ];
        let b = resolve_budget("m55_hp", Some(5.5), None, &banks, &[]);
        assert_eq!(b.ram_total, Some(1_048_576));
        assert_eq!(b.note, None);

        // tcm_kb fallback carries a note.
        let cores = vec![("m55_hp".to_string(), Some(1280.0))];
        let b = resolve_budget("m55_hp", None, None, &[], &cores);
        assert_eq!(b.ram_total, Some(1_310_720));
        assert_eq!(b.note.as_deref(), Some("ram=core tcm_kb (ITCM+DTCM)"));

        // both absent.
        let b = resolve_budget("m55_hp", None, None, &[], &[]);
        assert_eq!(b, MemoryBudget::default());
    }

    #[test]
    fn resolve_variant_forward_reverse_and_tbd() {
        let variants = vec![
            variant(r#"{"order_code":"AAA","alp_module_skus":["E1M-X"]}"#),
            variant(r#"{"order_code":"BBB","alp_module_skus":["E1M-Y"]}"#),
        ];
        // forward on order_code
        assert_eq!(
            resolve_variant(Some("BBB"), None, &variants)
                .unwrap()
                .order_code
                .as_deref(),
            Some("BBB")
        );
        // TBD ignored -> reverse via sku
        assert_eq!(
            resolve_variant(Some("TBD"), Some("E1M-X"), &variants)
                .unwrap()
                .order_code
                .as_deref(),
            Some("AAA")
        );
        // reverse via sku when no variant declared
        assert_eq!(
            resolve_variant(None, Some("E1M-Y"), &variants)
                .unwrap()
                .order_code
                .as_deref(),
            Some("BBB")
        );
        // no match
        assert!(resolve_variant(Some("ZZZ"), Some("E1M-Z"), &variants).is_none());
    }

    fn zephyr_row(core: &str, status: &str) -> SliceSize {
        SliceSize {
            core_id: core.to_string(),
            os: "zephyr".to_string(),
            status: status.to_string(),
            flash_used: Some(1000),
            flash_total: Some(2000),
            ram_used: Some(500),
            ram_total: Some(1000),
            source: Some("size-tool".to_string()),
            note: None,
            notes: Vec::new(),
        }
    }

    #[test]
    fn json_entry_key_order_and_optional_fields() {
        // n/a slice: note surfaces as budget_note; no source measurement.
        let na = SliceSize {
            core_id: "a32".to_string(),
            os: "yocto".to_string(),
            status: "n/a".to_string(),
            flash_used: None,
            flash_total: None,
            ram_used: None,
            ram_total: None,
            source: None,
            note: Some("no Zephyr image (Yocto/baremetal)".to_string()),
            notes: Vec::new(),
        };
        let entry = na.to_json_entry();
        let keys: Vec<&str> = entry
            .as_object()
            .unwrap()
            .keys()
            .map(|s| s.as_str())
            .collect();
        assert_eq!(
            keys,
            [
                "core_id",
                "os",
                "status",
                "flash",
                "ram",
                "source",
                "budget_note"
            ]
        );
        assert_eq!(
            entry["budget_note"],
            json!("no Zephyr image (Yocto/baremetal)")
        );

        // not-built slice: notes present.
        let mut nb = zephyr_row("m55_he", "not-built");
        nb.flash_used = None;
        nb.ram_used = None;
        nb.source = None;
        nb.notes = vec!["no footprint source at /x/zephyr.elf".to_string()];
        let entry = nb.to_json_entry();
        assert_eq!(
            entry["notes"],
            json!(["no footprint source at /x/zephyr.elf"])
        );

        // ok slice with a source, no optional fields.
        let entry = zephyr_row("m55_hp", "ok").to_json_entry();
        assert_eq!(entry["source"], json!("size-tool"));
        assert!(entry.get("budget_note").is_none());
        assert!(entry.get("notes").is_none());
    }

    #[test]
    fn report_summary_sorts_and_filters() {
        let mut over = zephyr_row("m55_hp", "over");
        over.flash_used = Some(3000); // over its 2000 budget
        let over_a = {
            let mut r = zephyr_row("a55", "over");
            r.flash_used = Some(3000);
            r
        };
        // no-budget zephyr slice -> unknown_budget
        let no_budget = SliceSize {
            flash_total: None,
            ram_total: None,
            status: "no-budget".to_string(),
            ..zephyr_row("m55_he", "no-budget")
        };
        // not-built excluded from unknown_budget even without a budget
        let not_built = SliceSize {
            flash_total: None,
            ram_total: None,
            status: "not-built".to_string(),
            ..zephyr_row("m55_lp", "not-built")
        };
        let report = build_size_report(&[over, over_a, no_budget, not_built]);
        assert_eq!(report["schema"], json!("alp-size/1"));
        assert_eq!(report["summary"]["over_budget"], json!(["a55", "m55_hp"]));
        assert_eq!(report["summary"]["unknown_budget"], json!(["m55_he"]));
    }

    #[test]
    fn half_resolved_or_zero_total_budget_reports_as_unknown_not_known() {
        // Only ONE region resolved (ram_total: None). The old `budget_known()`
        // OR meant this counted as "known" even though `classify` silently
        // skips the unresolved ram region — so it rendered `status: "ok"`
        // (what the caller sets when it only checks the resolved region) and
        // never showed up in `summary.unknown_budget` or `unknown_budget_rows`,
        // so `--fail-over-budget` never flagged that ram was never checked.
        let half = SliceSize {
            ram_total: None,
            status: "ok".to_string(),
            ..zephyr_row("m55_hp", "ok")
        };
        assert!(!half.budget_fully_known());
        let report = build_size_report(std::slice::from_ref(&half));
        assert_eq!(report["summary"]["unknown_budget"], json!(["m55_hp"]));
        assert_eq!(unknown_budget_rows(&[half]).len(), 1);

        // A total that saturates to exactly 0 (see `resolve_budget`'s
        // `mram_mb`/`soc_flash_mb` cast) must count the same as unresolved —
        // `classify` already skips a `total == 0` region, so treating
        // `Some(0)` as "known" here would reopen the identical hole.
        let zero_total = SliceSize {
            flash_total: Some(0),
            status: "ok".to_string(),
            ..zephyr_row("a55", "ok")
        };
        assert!(!zero_total.budget_fully_known());
    }

    #[test]
    fn render_table_lines_deterministic_and_plain() {
        let mut row = zephyr_row("m55_hp", "over");
        row.note = Some("flash=soc_flash_mb".to_string());
        let lines = render_table_lines(&[row], false);
        assert!(lines[0].starts_with("CORE"));
        assert!(lines[0].contains("FLASH used/total"));
        assert!(lines[1].chars().all(|c| c == '-'));
        assert!(
            lines
                .iter()
                .any(|l| l.contains("m55_hp") && l.contains("OVER"))
        );
        assert!(
            lines
                .iter()
                .any(|l| l.trim_start().starts_with("-> flash=soc_flash_mb"))
        );
        // color off => no ANSI escapes
        assert!(lines.iter().all(|l| !l.contains('\u{1b}')));
    }
}
