# SPDX-License-Identifier: Apache-2.0
"""`tan.core.size`: the pure footprint model.

The numeric expectations mirror `crates/tan-core/src/size.rs`'s own unit tests
one for one, so a drift in either implementation shows up here rather than only
in a live envelope diff.
"""
import struct

import pytest

from tan.core.size import (
    WARN_FRACTION,
    MemoryBudget,
    SliceSize,
    _kib_to_bytes,
    _mb_to_bytes,
    build_size_report,
    classify,
    core_token,
    footprint_total,
    human_bytes,
    parse_berkeley_size,
    region_cell,
    region_json,
    render_table_lines,
    resolve_budget,
    resolve_variant,
    round1,
    sizes_from_elf_sections,
    sram_banks,
    unknown_budget_rows,
)


def make_elf(
    *, text=100, rodata=20, data=40, bss=200, cls=2, little=True, corrupt_size=None
) -> bytes:
    """A minimal ELF with the four allocated sections binutils' Berkeley columns
    sum. `cls=1` gives ELF32, `little=False` big-endian, `corrupt_size` patches
    every allocated section's `sh_size`."""
    endian = "<" if little else ">"
    alloc, write, execinstr = 0x2, 0x1, 0x4
    secs = [
        ("", 0, 0, 0),
        (".text", 1, alloc | execinstr, text),
        (".rodata", 1, alloc, rodata),
        (".data", 1, alloc | write, data),
        (".bss", 8, alloc | write, bss),
        (".shstrtab", 3, 0, 0),
    ]
    shstr = b"\0"
    offs = {}
    for name, *_ in secs:
        offs[name] = len(shstr) if name else 0
        if name:
            shstr += name.encode() + b"\0"
    ehsize = 64 if cls == 2 else 52
    shentsize = 64 if cls == 2 else 40
    body = b""
    placed = []
    for name, typ, flags, size in secs:
        if name == ".shstrtab":
            placed.append((name, typ, flags, len(shstr), ehsize + len(body)))
            body += shstr
        elif typ == 1:
            placed.append((name, typ, flags, size, ehsize + len(body)))
            body += b"\0" * size
        else:
            placed.append((name, typ, flags, size, ehsize + len(body)))
    shoff = ehsize + len(body)
    ident = b"\x7fELF" + bytes([cls, 1 if little else 2, 1, 0]) + b"\x00" * 8
    if cls == 2:
        header = ident + struct.pack(endian + "HHI", 1, 183, 1)
        header += struct.pack(endian + "QQQ", 0, 0, shoff)
        fmt = endian + "IIQQQQIIQQ"
    else:
        header = ident + struct.pack(endian + "HHI", 1, 40, 1)
        header += struct.pack(endian + "III", 0, 0, shoff)
        fmt = endian + "IIIIIIIIII"
    header += struct.pack(
        endian + "IHHHHHH", 0, ehsize, 0, 0, shentsize, len(placed), len(placed) - 1
    )
    table = b""
    for name, typ, flags, size, off in placed:
        if corrupt_size is not None and flags & alloc:
            size = corrupt_size
        table += struct.pack(fmt, offs[name], typ, flags, 0, off, size, 0, 0, 1, 0)
    return header + body + table


# ------------------------------------------------------------ berkeley output


def test_parse_berkeley_size_skips_the_header_and_sums_the_columns():
    out = (
        "   text\t   data\t    bss\t    dec\t    hex\tfilename\n"
        "  12345\t    678\t   9012\t  22035\t   5613\tzephyr.elf\n"
    )
    assert parse_berkeley_size(out) == (12345 + 678, 678 + 9012)


@pytest.mark.parametrize(
    "text", ["", "text data bss dec hex filename", "garbage line here", "1 2", "\n\n"]
)
def test_parse_berkeley_size_finds_no_data_row(text):
    assert parse_berkeley_size(text) is None


@pytest.mark.parametrize("row", ["-1 2 3 x", "1_0 2 3 x", "1.5 2 3 x", "٣ ٤ ٥ x"])
def test_parse_berkeley_size_rejects_what_rust_u64_parse_rejects(row):
    # Python's `int()` takes negatives, underscores and unicode digits; Rust's
    # `parse::<u64>()` takes none of them, so such a row is NOISE, not a data row.
    assert parse_berkeley_size(row) is None


def test_parse_berkeley_size_accepts_a_leading_plus_like_rust_does():
    assert parse_berkeley_size("+10 +2 +3 x") == (12, 5)


# ------------------------------------------------------------- elf sections


def test_sizes_from_elf_sections_sums_the_berkeley_columns():
    # FLASH = text+rodata+data = 160; RAM = data+bss = 240.
    assert sizes_from_elf_sections(make_elf()) == (160, 240)


def test_sizes_from_elf_sections_handles_elf32_and_big_endian():
    assert sizes_from_elf_sections(make_elf(cls=1)) == (160, 240)
    assert sizes_from_elf_sections(make_elf(little=False)) == (160, 240)
    assert sizes_from_elf_sections(make_elf(cls=1, little=False)) == (160, 240)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not an elf",
        b"\x7fELF",  # truncated before the header ends
        b"\x7fELF\x03\x01" + b"\x00" * 100,  # invalid EI_CLASS
        b"\x7fELF\x02\x09" + b"\x00" * 100,  # invalid EI_DATA
        b"MZ\x90\x00" + b"\x00" * 200,  # a PE container
    ],
)
def test_sizes_from_elf_sections_refuses_anything_that_is_not_an_elf(raw):
    # `None`, never `(0, 0)`: a fake "measured, image is empty" reads as "fits
    # easily" and is indistinguishable from a real measurement.
    assert sizes_from_elf_sections(raw) is None


def test_sizes_from_elf_sections_refuses_an_elf_with_no_allocated_section():
    assert sizes_from_elf_sections(make_elf(text=0, rodata=0, data=0, bss=0)) == (0, 0)
    # ...but an ELF whose sections are none of them SHF_ALLOC yields nothing.
    raw = bytearray(make_elf())
    e_shoff = struct.unpack_from("<Q", raw, 0x28)[0]
    e_shentsize, e_shnum = struct.unpack_from("<HH", raw, 0x3A)
    for index in range(e_shnum):
        struct.pack_into("<Q", raw, e_shoff + index * e_shentsize + 8, 0)
    assert sizes_from_elf_sections(bytes(raw)) is None


def test_sizes_from_elf_sections_refuses_a_section_table_past_the_buffer():
    raw = bytearray(make_elf())
    struct.pack_into("<Q", raw, 0x28, 1 << 40)  # e_shoff far beyond the file
    assert sizes_from_elf_sections(bytes(raw)) is None


def test_sizes_from_elf_sections_saturates_a_corrupt_sh_size():
    # `sh_size` is unvalidated and its bytes are never read here. Rust saturates
    # at u64::MAX rather than wrapping; an unbounded Python int would emit a
    # number the oracle cannot.
    flash, ram = sizes_from_elf_sections(make_elf(corrupt_size=2**64 - 16))
    assert flash == 2**64 - 1
    assert ram == 2**64 - 1


# ------------------------------------------------------------ footprint json


def test_footprint_total_reads_symbols_then_top_level():
    assert footprint_total('{"symbols":{"size":4096}}') == 4096
    assert footprint_total('{"size":2048}') == 2048


@pytest.mark.parametrize(
    "text",
    [
        '{"symbols":{"size":"x"}}',
        "{not json",
        '{"symbols":{"size":true}}',
        '{"size":-5}',
        '{"size":1.5}',
        "[]",
        "null",
        '{"symbols":[]}',
        "",
    ],
)
def test_footprint_total_is_none_for_every_shape_serde_as_u64_refuses(text):
    assert footprint_total(text) is None


# ----------------------------------------------------------------- rounding


def test_round1_rounds_the_true_binary_value_like_rust_and_cpython():
    assert round1(91.5) == 91.5
    # 0.25 is exactly representable -> ties-to-even -> 0.2; 0.35 is really
    # 0.34999... -> 0.3, not the 0.4 an (x*10).round()/10 multiply yields.
    assert round1(0.25) == 0.2
    assert round1(0.35) == 0.3


def test_region_json_pct_is_null_without_a_usable_total():
    assert region_json(915, 1000) == {"used": 915, "total": 1000, "pct": 91.5}
    assert region_json(10, 0)["pct"] is None
    assert region_json(None, 1000)["pct"] is None
    assert region_json(10, None)["pct"] is None


# --------------------------------------------------------------- classify


def test_classify_ranks_the_worst_region():
    assert classify(1100, 1000, 10, 1000) == "over"
    assert classify(900, 1000, None, None) == "warn"
    assert classify(1000, 1000, None, None) == "warn"
    assert classify(100, 1000, None, None) == "ok"


def test_classify_ignores_a_region_it_cannot_evaluate():
    # No div-by-zero and no guess -- which is why `budget_fully_known` exists.
    assert classify(10, None, None, None) == "ok"
    assert classify(10, 0, None, None) == "ok"
    assert WARN_FRACTION == 0.90


# ----------------------------------------------------------------- budget


def test_resolve_budget_prefers_mram_then_soc_flash_with_a_note():
    assert resolve_budget("m55_hp", 5.5, 4.0, [], []) == MemoryBudget(
        5_767_168, None, None
    )
    assert resolve_budget("m55_hp", None, 5.5, [], []) == MemoryBudget(
        5_767_168, None, "flash=soc_flash_mb"
    )


def test_resolve_budget_prefers_the_core_dtcm_bank_then_tcm_kb_with_a_note():
    banks = [("SRAM2_M55_HP_ITCM", 256.0), ("SRAM3_M55_HP_DTCM", 1024.0)]
    assert resolve_budget("m55_hp", 5.5, None, banks, []).ram_total == 1_048_576
    fallback = resolve_budget("m55_hp", None, None, [], [("m55_hp", 1280.0)])
    assert fallback.ram_total == 1_310_720
    assert fallback.note == "ram=core tcm_kb (ITCM+DTCM)"


def test_resolve_budget_resolves_nothing_when_metadata_says_nothing():
    assert resolve_budget("m55_hp", None, None, [], []) == MemoryBudget()


def test_resolve_budget_saturates_a_nonsense_size_to_zero_not_a_guess():
    # `Some(0)` counts as unresolved everywhere downstream, so a negative or NaN
    # `mram_mb` degrades to "no budget" rather than to "fits easily".
    assert resolve_budget("c", -4.0, None, [], []).flash_total == 0
    assert resolve_budget("c", float("nan"), None, [], []).flash_total == 0


def test_a_non_finite_size_resolves_to_no_budget_instead_of_raising():
    """tan-cli#499 defect 8. `int()` sat OUTSIDE the `try`, so `+inf` raised
    `OverflowError: cannot convert float infinity to integer` -- reachable from
    an `Infinity` or `1e400` literal in a SoC JSON, which `json.loads` resolves
    to `float('inf')`. That took the whole `tan size` run to
    `size.internal-failure`, exit 5, `data.slices` EMPTY.

    **0, not `u64::MAX` (REVIEW round).** The first version of this fix
    saturated `+inf` too, on a continuity argument. Measured end to end, that
    put a 16 EiB flash budget in the envelope at `ok:true` -- exactly the
    silent-wrong-number class the issue exists to close -- where the oracle
    answers `flash.total null` + `budget_note "unreadable SoM preset for
    E1M-AEN801"`, because serde_json refuses the `1e400` literal outright and
    the cast is never reached. There is no oracle answer for a non-finite to be
    continuous WITH, so it joins the NaN/negative arm at 0, this module's own
    unresolved value.

    The FINITE saturation is where the oracle really was measured, and is
    untouched: `"mram_mb": 1e300` gives `"flash":{"total":18446744073709551615}`
    at exit 0 on BOTH binaries."""
    u64_max = 2**64 - 1
    assert _mb_to_bytes(float("inf")) == 0
    assert _kib_to_bytes(float("inf")) == 0
    assert resolve_budget("c", float("inf"), None, [], []).flash_total == 0
    assert resolve_budget("c", None, None, [], [("c", float("inf"))]).ram_total == 0
    # The other two non-finite inputs answer "no budget" the same way.
    assert _mb_to_bytes(float("-inf")) == 0
    assert _kib_to_bytes(float("-inf")) == 0
    assert _mb_to_bytes(float("nan")) == 0
    assert _kib_to_bytes(float("nan")) == 0
    # Finite-and-huge still saturates -- measured byte-identical to the oracle.
    assert _mb_to_bytes(1e300) == u64_max
    assert _kib_to_bytes(1e300) == u64_max


def test_sram_banks_keeps_order_and_drops_non_numeric_entries():
    variant = {"sram_banks_kb": {"A": 1, "B": "x", "C": True, "D": 2.5}}
    assert sram_banks(variant) == [("A", 1.0), ("D", 2.5)]
    assert sram_banks({}) == []


def test_sram_banks_drops_an_integer_too_large_for_an_f64():
    """tan-cli#499 defect 8, second arm: guarding only `int()` is an incomplete
    fix. A 400-digit JSON integer makes the bare `float(value)` raise
    `OverflowError: int too large to convert to float`, and `sram_banks` runs
    BEFORE `resolve_budget`, so the same exit-5 collapse happened with every
    slice discarded. Dropped, not saturated, to match `_mb_to_bytes` -- its own
    `float()` raises inside a `try` and returns the unresolved 0 for the very
    same value."""
    variant = {"sram_banks_kb": {"A": 1, "HUGE": 10**400, "D": 2.5}}
    assert sram_banks(variant) == [("A", 1.0), ("D", 2.5)]
    # A non-finite FLOAT is a different input: passed through here, and resolved
    # to the unresolved 0 one level down in `_saturating_u64`.
    assert sram_banks({"sram_banks_kb": {"A": float("inf")}}) == [("A", float("inf"))]


def test_resolve_variant_forward_then_reverse_then_nothing():
    variants = [
        {"order_code": "AAA", "alp_module_skus": ["E1M-X"]},
        {"order_code": "BBB", "alp_module_skus": ["E1M-Y"]},
    ]
    assert resolve_variant("BBB", None, variants)["order_code"] == "BBB"
    # `TBD` is dropped, so the sku reverse-match answers instead.
    assert resolve_variant("TBD", "E1M-X", variants)["order_code"] == "AAA"
    # A forward value naming no variant falls THROUGH to the reverse lookup.
    assert resolve_variant("ZZZ", "E1M-Y", variants)["order_code"] == "BBB"
    assert resolve_variant(None, "E1M-Y", variants)["order_code"] == "BBB"
    assert resolve_variant("ZZZ", "E1M-Z", variants) is None
    assert resolve_variant(None, None, variants) is None


def test_resolve_variant_drops_a_padded_tbd_too():
    """#276: a padded `' TBD '` must be dropped exactly like a bare `TBD` --
    before this fix `resolve_variant` compared untrimmed, so a padded
    placeholder fell through to a forward match instead of the sku reverse
    lookup."""
    variants = [
        {"order_code": "AAA", "alp_module_skus": ["E1M-X"]},
        {"order_code": " TBD ", "alp_module_skus": ["E1M-Z"]},
    ]
    assert resolve_variant(" TBD ", "E1M-X", variants)["order_code"] == "AAA"
    assert resolve_variant("\tTBD\n", "E1M-X", variants)["order_code"] == "AAA"


def test_core_token_is_the_uppercase_core_id():
    assert core_token("m55_hp") == "M55_HP"


# ------------------------------------------------------------------ report


def zephyr_row(core, status, **overrides):
    row = SliceSize(
        core_id=core,
        os="zephyr",
        status=status,
        flash_used=1000,
        flash_total=2000,
        ram_used=500,
        ram_total=1000,
        source="size-tool",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_json_entry_key_order_and_optional_fields():
    na = SliceSize(
        core_id="a32",
        os="yocto",
        status="n/a",
        note="no Zephyr image (Yocto/baremetal)",
    )
    entry = na.to_json_entry()
    assert list(entry) == [
        "core_id",
        "os",
        "status",
        "flash",
        "ram",
        "source",
        "budget_note",
    ]
    assert entry["source"] is None

    built = zephyr_row("m55_hp", "ok").to_json_entry()
    assert "budget_note" not in built
    assert "notes" not in built

    not_built = zephyr_row(
        "m55_he", "not-built", flash_used=None, ram_used=None, source=None,
        notes=["no footprint source at /x/zephyr.elf"],
    )
    assert not_built.to_json_entry()["notes"] == ["no footprint source at /x/zephyr.elf"]


def test_report_summary_sorts_and_filters():
    report = build_size_report(
        [
            zephyr_row("m55_hp", "over", flash_used=3000),
            zephyr_row("a55", "over", flash_used=3000),
            zephyr_row("m55_he", "no-budget", flash_total=None, ram_total=None),
            # `not-built` is excluded from unknown_budget even with no budget.
            zephyr_row("m55_lp", "not-built", flash_total=None, ram_total=None),
            SliceSize(core_id="a32", os="yocto", status="n/a"),
        ]
    )
    assert report["schema"] == "alp-size/1"
    assert report["summary"]["over_budget"] == ["a55", "m55_hp"]
    assert report["summary"]["unknown_budget"] == ["m55_he"]


def test_a_half_resolved_or_zero_budget_reports_as_unknown_not_known():
    # The AND in `budget_fully_known`. With an OR, `classify` silently skipped the
    # unresolved region, the row still said `ok`, and `--fail-over-budget` never
    # flagged that the other region was never checked at all.
    half = zephyr_row("m55_hp", "ok", ram_total=None)
    assert not half.budget_fully_known()
    assert build_size_report([half])["summary"]["unknown_budget"] == ["m55_hp"]
    assert len(unknown_budget_rows([half])) == 1
    # A total that saturated to exactly 0 must count the same as unresolved --
    # `classify` already skips it, so calling it "known" reopens the same hole.
    assert not zephyr_row("a55", "ok", flash_total=0).budget_fully_known()


# ------------------------------------------------------------------ render


def test_human_bytes_and_region_cell_widths():
    assert human_bytes(None) == "?"
    assert human_bytes(512) == "512B"
    assert human_bytes(4096) == "4.0K"
    assert human_bytes(5_767_168) == "5.50M"
    assert region_cell(4096, 5_767_168) == "    4.0K/5.50M      0.1%"
    assert len(region_cell(4096, 5_767_168)) == 24
    assert region_cell(None, None) == "       ?/?           -  "
    assert region_cell(10, 0).endswith("   -  ")
    # A pct wider than its 5-column field overflows the cell rather than being
    # truncated -- which is what the shipped binary does too, and is how a wildly
    # over-budget slice stays legible instead of reading as 9615.8%.
    assert region_cell(100_000, 104) == "   97.7K/104B     96153.8%"


def test_render_table_lines_is_deterministic_and_plain_without_color():
    row = zephyr_row("m55_hp", "over", note="flash=soc_flash_mb")
    lines = render_table_lines([row], False)
    assert lines[0].startswith("CORE")
    assert "FLASH used/total" in lines[0]
    assert set(lines[1]) == {"-"}
    assert len(lines[1]) == len(lines[0])
    assert any("m55_hp" in line and "OVER" in line for line in lines)
    assert any(line.lstrip().startswith("-> flash=soc_flash_mb") for line in lines)
    assert all("\x1b" not in line for line in lines)


def test_render_table_lines_colours_the_status_when_asked():
    lines = render_table_lines([zephyr_row("c", "over")], True)
    assert "\x1b[31mOVER\x1b[0m" in lines[2]


def test_render_table_lines_shows_an_unknown_status_verbatim_and_plain():
    lines = render_table_lines([zephyr_row("c", "weird-new-status")], True)
    assert lines[2].endswith("weird-new-status")
    assert "\x1b" not in lines[2]
