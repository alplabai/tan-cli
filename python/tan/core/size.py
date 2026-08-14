# SPDX-License-Identifier: Apache-2.0
"""Pure footprint model for `tan size` -- port of `crates/tan-core/src/size.rs`.

Measurement-source selection, budget resolution, classification and rendering
are all IO-free here; the subprocess + filesystem wiring lives in
`tan.commands.size_cmd`.

FLASH = text+data (everything in the flashed image); RAM = data+bss (everything
live at runtime) -- the Berkeley `size` model, where binutils folds `.rodata`
into `text` and `.noinit` (NOBITS) into `bss`.

Nothing here knows a hardware fact. `resolve_budget` is handed numbers a caller
read out of alp-sdk `metadata/`; no SKU, address or bank name is spelled in this
file (I-26).
"""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from tan.core.pending import is_pending_placeholder

#: ELF `sh_flags` bits used to classify a section.
SHF_WRITE = 0x1
SHF_ALLOC = 0x2

#: ELF `sh_type` for a section that occupies memory but not the image.
SHT_NOBITS = 8

#: A slice at/above this fraction of its budget is `warn` even though it still
#: fits -- a pre-flight "you're close" nudge.
WARN_FRACTION = 0.90

#: `u64::MAX`. A corrupt/adversarial `sh_size` saturates here rather than
#: reporting an arbitrarily large Python int the oracle could never emit.
_U64_MAX = 2**64 - 1

# colorama Fore/Style literals, so the color path matches the retired command's
# bytes.
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"

#: Rust's `str::parse::<u64>()`: optional `+`, digits only. Python's `int()` is
#: laxer (it takes `-5`, `1_000`, surrounding whitespace, unicode digits), so a
#: `size` output row the oracle skips as noise must be skipped here too.
_U64_RE = re.compile(r"\+?[0-9]+\Z")


def _u64(token: str) -> int | None:
    if not _U64_RE.match(token):
        return None
    value = int(token)
    return value if value <= _U64_MAX else None


def parse_berkeley_size(text: str) -> tuple[int, int] | None:
    """Parse Berkeley-format `size` output into `(flash, ram)` bytes. Columns are
    `text data bss dec hex filename`; the first row whose first three
    whitespace-split fields all parse as u64 wins (header/noise rows skipped).
    `None` when no data row is found."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        text_b, data_b, bss_b = (_u64(p) for p in parts[:3])
        if text_b is None or data_b is None or bss_b is None:
            continue  # header row ("text data bss ...") or noise
        return text_b + data_b, data_b + bss_b
    return None


def sizes_from_elf_sections(elf_bytes: bytes) -> tuple[int, int] | None:
    """Sum ELF section sizes into `(flash, ram)` bytes with Berkeley-`size`
    semantics, straight from the section headers -- the middle rung between the
    external size tool and the `rom/ram.json` fallback, so a present elf is
    measured even with no `arm-zephyr-eabi-size` on PATH.

    FLASH = every `SHF_ALLOC` section that occupies the image (not NOBITS):
    `.text` + `.rodata` + `.data`, i.e. binutils' `text`+`data` columns.
    RAM = every `SHF_ALLOC | SHF_WRITE` section, NOBITS included: `.data` +
    `.bss` + `.noinit`, i.e. binutils' `data`+`bss` columns.

    `None` -- so the caller falls through to the next rung instead of reporting
    a fake 0-byte size -- when the bytes are not ELF (a PE/Mach-O/wasm container
    parses in other readers and used to yield `(0, 0)` in the oracle), when the
    header table does not fit in the file, or when NO allocated section exists
    (a relocatable `.o`, a stripped partial link). Handles ELF32/ELF64 and
    either endianness, whatever the SoM core emits. Pure.
    """
    if len(elf_bytes) < 64 or elf_bytes[:4] != b"\x7fELF":
        return None
    ei_class, ei_data = elf_bytes[4], elf_bytes[5]
    if ei_class not in (1, 2) or ei_data not in (1, 2):
        return None
    endian = "<" if ei_data == 1 else ">"
    if ei_class == 2:  # ELF64
        e_shoff = struct.unpack_from(endian + "Q", elf_bytes, 0x28)[0]
        e_shentsize, e_shnum = struct.unpack_from(endian + "HH", elf_bytes, 0x3A)
        shdr = endian + "IIQQQQIIQQ"
        type_at, flags_at, size_at = 1, 2, 5
    else:  # ELF32
        e_shoff = struct.unpack_from(endian + "I", elf_bytes, 0x20)[0]
        e_shentsize, e_shnum = struct.unpack_from(endian + "HH", elf_bytes, 0x2E)
        shdr = endian + "IIIIIIIIII"
        type_at, flags_at, size_at = 1, 2, 5
    entry_size = struct.calcsize(shdr)
    if e_shentsize < entry_size or e_shnum == 0:
        return None
    # The header TABLE is bounds-checked (a truncated/adversarial e_shoff/e_shnum
    # must not read past the buffer); an individual `sh_size` is NOT -- it is an
    # unvalidated field, and its bytes are never read here, only its declared
    # length, so it is saturated instead.
    if e_shoff + e_shnum * e_shentsize > len(elf_bytes):
        return None

    flash = 0
    ram = 0
    saw_alloc = False
    for index in range(e_shnum):
        fields = struct.unpack_from(shdr, elf_bytes, e_shoff + index * e_shentsize)
        sh_type, sh_flags, sh_size = (
            fields[type_at],
            fields[flags_at],
            fields[size_at],
        )
        if not sh_flags & SHF_ALLOC:
            continue  # .symtab, .debug_*, .comment -- neither region
        saw_alloc = True
        if sh_type != SHT_NOBITS:
            flash = min(flash + sh_size, _U64_MAX)
        if sh_flags & SHF_WRITE:
            ram = min(ram + sh_size, _U64_MAX)
    return (flash, ram) if saw_alloc else None


def footprint_total(json_text: str) -> int | None:
    """Total bytes from a Zephyr `rom.json` / `ram.json` footprint document. The
    footprint root is `{"symbols": {"size": <total>}}`; older layouts put the
    total at the top level. `None` when the JSON is malformed or neither shape
    yields a non-negative integer.

    `bool` is excluded explicitly: it is an `int` subclass in Python, and
    serde_json's `as_u64()` never accepts a JSON boolean.
    """
    try:
        data = json.loads(json_text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    symbols = data.get("symbols")
    if isinstance(symbols, dict):
        size = _as_u64(symbols.get("size"))
        if size is not None:
            return size
    return _as_u64(data.get("size"))


def _as_u64(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _U64_MAX else None


def round1(x: float) -> float:
    """Round to 1 decimal the way both CPython's `round` and Rust's `{:.1}` do:
    on the TRUE binary value, so the X.X5 tie cases agree -- 0.35 is actually
    0.34999... and gives 0.3, not the 0.4 a `(x*10).round()/10` multiply
    yields. Formatting-then-parsing rather than `round()` keeps this identical
    to the text-cell path, which already format-rounds."""
    try:
        return float(f"{x:.1f}")
    except (OverflowError, ValueError):
        return x


def region_json(used: int | None, total: int | None) -> dict[str, Any]:
    """A `{used, total, pct}` region object. `pct` is `null` unless `used` is
    present and `total` is present AND non-zero (no div-by-zero, no guess)."""
    pct = None
    if used is not None and total:
        pct = round1(used / total * 100.0)
    return {"used": used, "total": total, "pct": pct}


def core_token(core_id: str) -> str:
    """`m55_hp` -> `M55_HP` -- the token embedded in `sram_banks_kb` keys."""
    return core_id.upper()


def classify(
    flash_used: int | None,
    flash_total: int | None,
    ram_used: int | None,
    ram_total: int | None,
) -> str:
    """`ok` / `warn` / `over` from the worst of the two regions. A region with a
    missing `used`, or a `total` that is missing or zero, is IGNORED -- no
    div-by-zero and no guess. That silent skip is why `budget_fully_known`
    exists: it is the only thing that surfaces a half-resolved budget."""
    worst = "ok"
    for used, total in ((flash_used, flash_total), (ram_used, ram_total)):
        if used is None or not total:
            continue
        frac = used / total
        if frac > 1.0:
            return "over"
        if frac >= WARN_FRACTION:
            worst = "warn"
    return worst


@dataclass
class MemoryBudget:
    """A resolved per-slice FLASH/RAM budget in bytes, plus a human note when a
    coarser fallback source was used."""

    flash_total: int | None = None
    ram_total: int | None = None
    note: str | None = None


def budget_note_only(note: str) -> MemoryBudget:
    return MemoryBudget(None, None, note)


def _saturating_u64(value: float) -> int:
    """Rust's `as u64` cast for a FINITE input -- truncate toward zero, 0 at or
    below zero, `u64::MAX` above it -- and 0, never a number, for a NON-FINITE
    one.

    That cast used to sit outside its caller's `try` (tan-cli#499 defect 8), so
    an infinite `mram_mb` / `soc_flash_mb` / `tcm_kb` / `sram_banks_kb` entry --
    which `json.loads` produces for an `Infinity` literal or an overflowing
    `1e400` in a SoC JSON -- collapsed the whole `tan size` run into
    `size.internal-failure` at exit 5, with `data.slices` EMPTY (every other
    slice's measurement discarded) and `project` null. Python's `int()` refuses
    `inf` (`OverflowError: cannot convert float infinity to integer`) where
    Rust's cast does not, so the two cases have to be separated by hand.

    **Where the boundary sits, and why it is NOT "saturate everything"
    (tan-cli#499 REVIEW).** The first version of this fix answered `+inf` with
    `u64::MAX`, arguing continuity across the f64 range. Measured, that
    manufactures a confidently WRONG budget at `ok:true`, which is the exact
    failure class this issue exists to close:

        # SoC JSON with "mram_mb": 1e400
        this branch, before:  exit 0 ok:true  flash.total 18446744073709551615
        tan 0.4.1:            exit 0 ok:true  flash.total null,
                              budget_note "unreadable SoM preset for E1M-AEN801"

    16 EiB of flash, stated as fact, on a manifest a customer could then read as
    "plenty of room". So the saturation is BOUNDED to the inputs where the
    oracle demonstrably saturates -- the finite ones:

        # SoC JSON with "mram_mb": 1e300   (finite; serde_json reads it)
        this branch:  exit 0  flash.total 18446744073709551615
        tan 0.4.1:    exit 0  flash.total 18446744073709551615   <- byte-identical

    A NON-finite value is not a point on that curve: serde_json refuses `1e400`
    / `Infinity` / `NaN` outright, so there is no oracle answer to be continuous
    WITH. Those join the negative/NaN arm at 0 -- this module's own "unresolved"
    value (`_region_resolved` treats 0 as unresolved, `classify` skips it,
    `budget_fully_known()` is False for it), so the envelope reports `pct: null`
    and no budget rather than a fabricated maximum. A residual divergence from
    the oracle stays (`total: 0` vs `total: null` plus a note); it is upstream
    of this file and NOT fixed here -- see the PR's "Not fixed" section.
    """
    if value != value or value <= 0:  # NaN, -inf, and every non-positive
        return 0
    if value == float("inf"):
        # NOT `_U64_MAX`: see the docstring. An input the oracle's own reader
        # refuses is answered "no budget", not "the largest budget there is".
        return 0
    return min(int(value), _U64_MAX)


def _mb_to_bytes(mb: float) -> int:
    """Megabytes -> bytes with Rust's `as u64` cast semantics: truncate toward
    zero, and saturate a negative or NaN input to 0. A `Some(0)` total counts as
    unresolved everywhere downstream (`region_resolved`), so a nonsense
    `mram_mb` degrades to "no budget" rather than to "fits easily"."""
    try:
        value = float(mb) * 1024.0 * 1024.0
    except (TypeError, ValueError, OverflowError):
        return 0
    return _saturating_u64(value)


def _kib_to_bytes(kib: float) -> int:
    try:
        value = float(kib) * 1024.0
    except (TypeError, ValueError, OverflowError):
        return 0
    return _saturating_u64(value)


def sram_banks(variant: dict) -> list[tuple[str, float]]:
    """A SoC-JSON variant's `sram_banks_kb` as ordered `(name, kib)` pairs,
    dropping non-numeric and unrepresentable entries. Insertion order is the
    wire order, so the first-matching-bank rule in [`resolve_budget`] is
    deterministic.

    A JSON integer too large for an f64 is DROPPED, not crashed on
    (tan-cli#499 defect 8): `float(10**400)` raises `OverflowError: int too
    large to convert to float`, which took the whole `tan size` run to
    `size.internal-failure` at exit 5. Dropping matches what `_mb_to_bytes`
    already does with the identical value -- its own `float()` raises inside a
    `try` and it returns the unresolved 0 -- so both regions answer "no budget"
    for an unrepresentable integer rather than one crashing and one not. A
    non-finite FLOAT is passed through and reaches the same answer one level
    down: [`_saturating_u64`] resolves it to 0, not to `u64::MAX`."""
    raw = variant.get("sram_banks_kb")
    if not isinstance(raw, dict):
        return []
    out: list[tuple[str, float]] = []
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            kib = float(value)
        except OverflowError:
            continue
        out.append((str(name), kib))
    return out


def resolve_variant(
    silicon_variant: str | None, sku: str | None, variants: list[dict]
) -> dict | None:
    """Resolve a SoM preset to its matching SoC-JSON variant: forward via
    `silicon_variant == order_code` (skipping empty / `TBD`, trimmed -- #276),
    then reverse via `sku in alp_module_skus`. `None` when neither path
    resolves. A forward value that names no variant falls THROUGH to the
    reverse lookup."""
    if silicon_variant and not is_pending_placeholder(silicon_variant):
        for variant in variants:
            if variant.get("order_code") == silicon_variant:
                return variant
    if sku is None:
        return None
    for variant in variants:
        skus = variant.get("alp_module_skus")
        if isinstance(skus, list) and any(s == sku for s in skus):
            return variant
    return None


def slot0_bytes_for_core(
    core_id: str, memory_map: list[dict] | None
) -> int | None:
    """This core's OWN slot0 window from the SoM preset's `memory_map`, in
    bytes -- `None` when the preset declares none for it (tan-cli#747).

    Since alp-sdk#1445/#1069 a dual-M55 AEN SoM gives each core a disjoint
    slot0 (`he_slot0` at 0x80010000, `hp_slot0` at 0x802b0000, 2688 KiB
    each), and only that window is linkable by that core. The whole-MRAM
    figure is the sum of every partition INCLUDING the other core's, so it
    is nobody's budget.

    Matched on the `<role>_slot0` name, role being the core id's last
    segment -- the same `core_id.split("_")[-1]` the SDK's own
    `gen_zephyr_board` uses to pick a role's window, not a second spelling
    that could drift from it. `accessible_from`, when the entry declares
    one, must name this core: a window some other core owns is not a budget
    for this one, and silently accepting it would reintroduce exactly the
    cross-core confusion #1069 exists to prevent.
    """
    role = core_id.rsplit("_", 1)[-1]
    if not role:
        return None
    want = f"{role}_slot0"
    for entry in memory_map or []:
        if not isinstance(entry, dict) or entry.get("name") != want:
            continue
        accessible = entry.get("accessible_from")
        if isinstance(accessible, list) and accessible and core_id not in accessible:
            continue
        size_kib = entry.get("size_kib")
        # bool is an int in Python; a `size_kib: true` is malformed, not 1 KiB.
        if isinstance(size_kib, bool) or not isinstance(size_kib, (int, float)):
            continue
        if size_kib <= 0 or not math.isfinite(float(size_kib)):
            continue
        return int(size_kib) * 1024
    return None


def resolve_budget(
    core_id: str,
    mram_mb: float | None,
    soc_flash_mb: float | None,
    sram_banks_kb: list[tuple[str, float]],
    soc_cores: list[tuple[str, float | None]],
    memory_map: list[dict] | None = None,
) -> MemoryBudget:
    """The FLASH/RAM budget for one core, from values a caller already read out
    of the SoM preset + SoC JSON. FLASH: this core's own `<role>_slot0` window
    from the SoM `memory_map`, else variant `mram_mb`, else SoC
    `soc_flash_mb` (with a note). RAM: the `*_DTCM` bank whose name contains the
    core token, else the core's `tcm_kb` (with a note). Anything unresolvable
    stays `None` -- the caller renders `unknown`, never a guessed number.

    FLASH prefers the per-core window for the reason the RAM half below has
    always resolved per core: a shared total is not a budget. Measured before
    the fix, `E1M-AEN801` reported BOTH M55s against 5767168 B (the whole
    5.5 MB MRAM) while each image links into 2752512 B, so utilisation read
    2.0% where the linker said 4.22%, and `over_budget` could never fire --
    an image would have to exceed the whole part, which it cannot, because
    mcuboot + both slot0s + reserved + storage + atoc already fill it
    (tan-cli#747).

    `memory_map` defaults to `None` so every existing caller keeps today's
    behaviour exactly; a SoM that declares no per-role window (single-M55
    parts, non-AEN families, presets predating alp-sdk#1445) still falls
    through to `mram_mb` with no note, byte-identical to before."""
    notes: list[str] = []

    slot0_total = slot0_bytes_for_core(core_id, memory_map)
    if slot0_total is not None:
        flash_total: int | None = slot0_total
    elif mram_mb is not None:
        flash_total = _mb_to_bytes(mram_mb)
    elif soc_flash_mb is not None:
        notes.append("flash=soc_flash_mb")
        flash_total = _mb_to_bytes(soc_flash_mb)
    else:
        flash_total = None

    token = core_token(core_id)
    ram_total: int | None = None
    for name, kib in sram_banks_kb:
        if token in name and "DTCM" in name:
            ram_total = _kib_to_bytes(kib)
            break
    if ram_total is None:
        for core, tcm_kb in soc_cores:
            if core == core_id:
                if tcm_kb is not None:
                    ram_total = _kib_to_bytes(tcm_kb)
                    notes.append("ram=core tcm_kb (ITCM+DTCM)")
                break

    return MemoryBudget(flash_total, ram_total, "; ".join(notes) or None)


@dataclass
class SliceSize:
    """One slice's measured footprint vs its resolved budget. `status` is one of
    `ok`/`over`/`warn`/`not-built`/`n/a`/`no-budget`."""

    core_id: str
    os: str
    status: str
    flash_used: int | None = None
    flash_total: int | None = None
    ram_used: int | None = None
    ram_total: int | None = None
    source: str | None = None
    note: str | None = None
    notes: list[str] = field(default_factory=list)

    def budget_fully_known(self) -> bool:
        """True only when BOTH regions resolved to a nonzero total.

        Deliberately an AND, not an OR: `classify` silently skips whichever
        region has no usable `total` (correct in isolation), so with an OR a
        half-resolved budget reported as `ok` and never surfaced -- neither
        `--fail-over-budget` nor `summary.unknown_budget` named the slice.
        A total of exactly `0` counts as unresolved for the same reason
        `classify` already skips it.
        """
        return _region_resolved(self.flash_total) and _region_resolved(self.ram_total)

    def over_budget(self) -> bool:
        return self.status == "over"

    def to_json_entry(self) -> dict[str, Any]:
        """This slice's `alp-size/1` JSON entry. Key order IS the wire order:
        core_id, os, status, flash, ram, source, then optional budget_note +
        notes."""
        entry: dict[str, Any] = {
            "core_id": self.core_id,
            "os": self.os,
            "status": self.status,
            "flash": region_json(self.flash_used, self.flash_total),
            "ram": region_json(self.ram_used, self.ram_total),
            "source": self.source,
        }
        if self.note:
            entry["budget_note"] = self.note
        if self.notes:
            entry["notes"] = list(self.notes)
        return entry


def _region_resolved(total: int | None) -> bool:
    return total is not None and total != 0


def build_size_report(rows: list[SliceSize]) -> dict[str, Any]:
    """The full `alp-size/1` payload wrapped verbatim in the tan Envelope's
    `data`: `{schema, slices, summary{over_budget, unknown_budget}}`."""
    return {
        "schema": "alp-size/1",
        "slices": [row.to_json_entry() for row in rows],
        "summary": {
            "over_budget": sorted(r.core_id for r in rows if r.over_budget()),
            "unknown_budget": sorted(r.core_id for r in unknown_budget_rows(rows)),
        },
    }


def over_budget_rows(rows: list[SliceSize]) -> list[SliceSize]:
    return [r for r in rows if r.over_budget()]


def unknown_budget_rows(rows: list[SliceSize]) -> list[SliceSize]:
    """Zephyr slices that were measured but whose budget could not be fully
    resolved -- the ones `--fail-over-budget` skips. Keyed on the resolved
    totals (`budget_fully_known`) rather than on `status == "no-budget"`, which
    the caller only sets when BOTH regions are unresolved."""
    return [
        r
        for r in rows
        if r.os == "zephyr" and r.status != "not-built" and not r.budget_fully_known()
    ]


def human_bytes(n: int | None) -> str:
    """Bytes -> a compact KiB/MiB string, or `?` for unknown."""
    if n is None:
        return "?"
    if n >= 1024 * 1024:
        return f"{n / (1024.0 * 1024.0):.2f}M"
    if n >= 1024:
        return f"{n / 1024.0:.1f}K"
    return f"{n}B"


def region_cell(used: int | None, total: int | None) -> str:
    """A `used/total pct%` table cell, exactly 24 columns wide for the values
    this command produces."""
    pct = f"{used / total * 100.0:5.1f}%" if used is not None and total else "   -  "
    return f"{human_bytes(used):>8}/{human_bytes(total):<8} {pct}"


def _status_hue(status: str) -> tuple[str, str]:
    """Status hue + label. An unknown status renders plain, with its raw text."""
    return {
        "ok": (_GREEN, "OK"),
        "warn": (_YELLOW, "WARN"),
        "over": (_RED, "OVER"),
        "not-built": (_YELLOW, "not built"),
        "n/a": (_CYAN, "n/a"),
        "no-budget": (_CYAN, "no budget"),
    }.get(status, ("", status))


def render_table_lines(rows: list[SliceSize], use_color: bool) -> list[str]:
    """The per-slice table: header, separator, one row per slice, and a
    `-> <detail>` continuation for any slice carrying a note."""
    head = (
        f"{'CORE':<14} {'OS':<10} {'FLASH used/total':<24} "
        f"{'RAM used/total':<24} STATUS"
    )
    lines = [head, "-" * len(head)]
    for row in rows:
        hue, label = _status_hue(row.status)
        status = f"{hue}{label}{_RESET}" if use_color and hue else label
        lines.append(
            f"{row.core_id:<14} {row.os:<10} "
            f"{region_cell(row.flash_used, row.flash_total):<24} "
            f"{region_cell(row.ram_used, row.ram_total):<24} {status}"
        )
        detail = row.note or (row.notes[0] if row.notes else None)
        if detail is not None:
            lines.append(f"{'':<14} {'':<10} -> {detail}")
    return lines
