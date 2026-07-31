# SPDX-License-Identifier: Apache-2.0
"""`tan.core.renode_plan` unit tests -- ported from `crates/tan-core/src/
renode/mod.rs`'s own `#[cfg(test)]` module, which is the oracle for every
case here."""
from __future__ import annotations

import struct

import pytest

from tan.core.renode_plan import (
    RenodeError,
    build_renode_argv,
    elf_vector_table_base,
    platform_files_for_sku,
    platform_stem_for_sku,
    renode_cpu_halted,
    renode_rejected_argv,
    select_sku,
    sku_family,
    soc_family_token,
    zephyr_elf_from_manifest,
)
from tan.core.system_manifest import parse_system_manifest


def test_platform_stem_for_the_wired_skus():
    assert platform_stem_for_sku("E1M-AEN801") == "alif_ensemble_e8"
    assert platform_stem_for_sku("E1M-V2N101") == "renesas_rzv2n"
    # V2M reuses the V2N descriptor via family v2n-m1.
    assert platform_stem_for_sku("E1M-V2M101") == "renesas_rzv2n"


def test_nx9_has_no_descriptor_yet():
    with pytest.raises(RenodeError) as excinfo:
        platform_stem_for_sku("E1M-NX901")
    msg = str(excinfo.value)
    assert "imx93" in msg
    assert "alif_ensemble" in msg
    assert "renesas_rzv2n" in msg


def test_bogus_sku_is_unrecognised():
    with pytest.raises(RenodeError, match="unrecognised SoM SKU pattern: BOGUS"):
        platform_stem_for_sku("BOGUS")


def test_sku_family_and_token_maps():
    assert sku_family("E1M-AEN801") == "aen"
    assert sku_family("E1M-V2N101") == "v2n"
    assert sku_family("E1M-V2M101") == "v2n-m1"
    assert sku_family("E1M-NX901") == "imx93"
    assert soc_family_token("aen") == "alif_ensemble"
    assert soc_family_token("v2n") == "renesas_rzv2n"
    assert soc_family_token("v2n-m1") == "renesas_rzv2n"
    assert soc_family_token("imx93") == "nxp_imx9"


def test_platform_files_paths():
    repl, resc = platform_files_for_sku("E1M-AEN801", "/sdk")
    assert repl.replace("\\", "/") == "/sdk/metadata/renode/alif_ensemble_e8.repl"
    assert resc.replace("\\", "/") == "/sdk/metadata/renode/alif_ensemble_e8.resc"


def _manifest_with(slices: list[tuple[str, str, str]]):
    """A manifest with the given zephyr slices, each `(core_id, status,
    build_dir)`; `build_dir` empty -> the slice omits the key."""
    yaml = "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n"
    for core, status, bd in slices:
        yaml += f"- core_id: {core}\n  os: zephyr\n  status: {status}\n"
        if bd:
            yaml += f"  build_dir: {bd}\n"
    return parse_system_manifest(yaml)


def test_relative_build_dir_joins_build_root():
    m = _manifest_with([("m55_hp", "pending", "m55_hp-zephyr")])
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/p/build/m55_hp-zephyr/zephyr/zephyr.elf"


def test_output_artefact_is_preferred_over_build_dir():
    m = parse_system_manifest(
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  status: ok\n  "
        "build_dir: m55_hp-zephyr\n  "
        "output_artefact: m55_hp-zephyr/build/zephyr/zephyr.elf\n"
    )
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/p/build/m55_hp-zephyr/build/zephyr/zephyr.elf"


def test_absolute_output_artefact_used_verbatim():
    m = parse_system_manifest(
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN801\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  status: ok\n  "
        "output_artefact: /abs/out/build/zephyr/zephyr.elf\n"
    )
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/abs/out/build/zephyr/zephyr.elf"


def test_absolute_build_dir_used_verbatim():
    m = _manifest_with([("m55_hp", "pending", "/abs/out")])
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/abs/out/zephyr/zephyr.elf"


def test_absent_build_dir_falls_back_to_core_os_stem():
    m = _manifest_with([("m55_hp", "pending", "")])
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/p/build/m55_hp-zephyr/zephyr/zephyr.elf"


def test_blocked_slice_ignored_boots_the_runnable_one():
    m = _manifest_with([("m55_he", "blocked", ""), ("m55_hp", "pending", "")])
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/p/build/m55_hp-zephyr/zephyr/zephyr.elf"


def test_core_picks_one_slice_out_of_a_multi_slice_manifest():
    m = _manifest_with([("m55_hp", "pending", ""), ("m55_he", "pending", "")])
    assert (
        zephyr_elf_from_manifest(m, "/p/build", "m55_he").replace("\\", "/")
        == "/p/build/m55_he-zephyr/zephyr/zephyr.elf"
    )
    assert (
        zephyr_elf_from_manifest(m, "/p/build", "m55_hp").replace("\\", "/")
        == "/p/build/m55_hp-zephyr/zephyr/zephyr.elf"
    )


def test_core_naming_a_blocked_slice_still_boots_it():
    m = _manifest_with([("m55_he", "blocked", ""), ("m55_hp", "pending", "")])
    assert (
        zephyr_elf_from_manifest(m, "/p/build", "m55_he").replace("\\", "/")
        == "/p/build/m55_he-zephyr/zephyr/zephyr.elf"
    )


def test_unknown_core_errors_and_lists_the_zephyr_cores():
    m = _manifest_with([("m55_hp", "pending", ""), ("m55_he", "pending", "")])
    with pytest.raises(RenodeError) as excinfo:
        zephyr_elf_from_manifest(m, "/p/build", "a32_cluster")
    msg = str(excinfo.value)
    assert "a32_cluster" in msg
    assert "m55_hp" in msg
    assert "m55_he" in msg


def test_zero_zephyr_slices_errors():
    m = parse_system_manifest("schema_version: 1\nslices: []\n")
    with pytest.raises(RenodeError, match="no os: zephyr slice"):
        zephyr_elf_from_manifest(m, "/p/build", None)


def test_two_runnable_zephyr_slices_error_lists_cores():
    m = _manifest_with([("m55_hp", "pending", ""), ("m55_he", "pending", "")])
    with pytest.raises(RenodeError) as excinfo:
        zephyr_elf_from_manifest(m, "/p/build", None)
    msg = str(excinfo.value)
    assert "m55_hp" in msg
    assert "m55_he" in msg


def test_all_blocked_falls_back_and_boots_the_blocked_slice():
    m = _manifest_with([("m55_hp", "blocked", "")])
    elf = zephyr_elf_from_manifest(m, "/p/build", None)
    assert elf.replace("\\", "/") == "/p/build/m55_hp-zephyr/zephyr/zephyr.elf"

    m2 = _manifest_with([("m55_hp", "blocked", ""), ("m55_he", "skipped", "")])
    with pytest.raises(RenodeError):
        zephyr_elf_from_manifest(m2, "/p/build", None)


def test_build_renode_argv_is_the_exact_contract():
    argv = build_renode_argv("renode", "/m/x.repl", "/m/x.resc", "/b/zephyr.elf", None)
    assert argv == [
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
    assert len(argv) == 10
    assert "--hide-monitor" not in argv


def test_argv_injects_vtor_unquoted_before_the_include():
    argv = build_renode_argv("renode", "/m/x.repl", "/m/x.resc", "/b/zephyr.elf", 0x80000000)
    vtor_at = argv.index("$vtor=0x80000000")
    include_at = argv.index("i @/m/x.resc")
    assert vtor_at < include_at
    assert not any('"' in a for a in argv)

    plain = build_renode_argv("renode", "/m/x.repl", "/m/x.resc", "/b/zephyr.elf", None)
    assert not any(a.startswith("$vtor=") for a in plain)


def test_renode_argv_rejection_is_recognised_from_its_own_wording():
    assert renode_rejected_argv("--hide-monitor and --console cannot be set at the same time")
    assert renode_rejected_argv("usage: renode [options] [file-to-include / snapshot]")
    assert not renode_rejected_argv("renode: booting /b/zephyr.elf on alif_ensemble_e8.repl")
    assert not renode_rejected_argv("*** Booting Zephyr OS ***")


def test_renode_cpu_halt_is_recognised_from_its_own_wording():
    assert renode_cpu_halted(
        "16:23:19.7011 [ERROR] cpu: PC does not lay in memory or PC and SP are equal "
        "to zero. CPU was halted."
    )
    assert renode_cpu_halted("CPU was halted")
    assert renode_cpu_halted("PC does not lay in memory")
    assert not renode_cpu_halted("renode: booting /b/zephyr.elf on alif_ensemble_e8.repl")
    assert not renode_cpu_halted("*** Booting Zephyr OS ***")


def test_select_sku_prefers_override_then_manifest():
    m = _manifest_with([("m55_hp", "pending", "")])  # hw_info.sku = E1M-AEN801
    assert select_sku(m, "E1M-V2N101") == "E1M-V2N101"
    assert select_sku(m, None) == "E1M-AEN801"
    assert select_sku(m, "  ") == "E1M-AEN801"

    bare = parse_system_manifest("schema_version: 1\nslices: []\n")
    with pytest.raises(RenodeError, match="could not determine SoM SKU"):
        select_sku(bare, None)


# ── elf_vector_table_base ───────────────────────────────────────────────────


def _synthetic_elf(
    entry: int, segs: list[tuple[int, int, int]], bad_reset_vector: int | None = None
) -> bytes:
    """A minimal Elf32 LE header + program headers, enough for
    `elf_vector_table_base`. `segs` is `(vaddr, paddr, memsz)`; each segment
    gets real file content (2 words, `p_filesz = 8`)."""
    phoff = 52
    phentsize = 32
    phdrs_end = phoff + len(segs) * phentsize
    e = bytearray(phdrs_end)
    e[0:4] = b"\x7fELF"
    e[4] = 1
    e[5] = 1
    struct.pack_into("<I", e, 0x18, entry)
    struct.pack_into("<I", e, 0x1C, phoff)
    struct.pack_into("<H", e, 0x2A, phentsize)
    struct.pack_into("<H", e, 0x2C, len(segs))

    word1 = bad_reset_vector if bad_reset_vector is not None else entry
    for i, (vaddr, paddr, memsz) in enumerate(segs):
        off = phoff + i * phentsize
        content_off = phdrs_end + i * 8
        struct.pack_into("<I", e, off, 1)  # PT_LOAD
        struct.pack_into("<I", e, off + 4, content_off)  # p_offset
        struct.pack_into("<I", e, off + 8, vaddr)
        struct.pack_into("<I", e, off + 12, paddr)
        struct.pack_into("<I", e, off + 16, 8)  # p_filesz
        struct.pack_into("<I", e, off + 20, memsz)
        e += struct.pack("<I", 0)  # word0: initial SP (unread)
        e += struct.pack("<I", word1)  # word1: reset vector
    return bytes(e)


def test_vector_table_base_is_the_entry_segments_load_address():
    """The entry-bearing segment must have DISTINCT vaddr/paddr, or a `return
    paddr` -> `return vaddr` mutation is invisible: every other fixture
    segment here has `vaddr == paddr`, so this is the one that actually
    exercises the paddr-vs-vaddr distinction `elf_vector_table_base` exists
    for (an MRAM-linked image's `.data` segment RUNS at a DTCM vaddr but is
    STORED at an MRAM paddr)."""
    elf = _synthetic_elf(
        0x20000201,  # Thumb bit set; masked entry 0x20000200
        [
            (0x20000000, 0x80000000, 99144),
        ],
    )
    assert elf_vector_table_base(elf) == 0x80000000


def test_vector_table_base_handles_a_ram_run_image_too():
    elf = _synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)])
    assert elf_vector_table_base(elf) == 0x20000000


def test_vector_table_base_is_none_on_junk_or_an_unmapped_entry():
    assert elf_vector_table_base(b"not an elf at all") is None
    elf = _synthetic_elf(0x90000000, [(0x20000000, 0x20000000, 8192)])
    assert elf_vector_table_base(elf) is None


def test_vector_table_base_is_none_when_the_second_word_is_not_the_reset_vector():
    elf = _synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)], bad_reset_vector=0x20009999)
    assert elf_vector_table_base(elf) is None


def test_vector_table_base_is_none_when_the_second_word_is_not_in_the_file():
    elf = bytearray(_synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)]))
    filesz_off = 52 + 16
    struct.pack_into("<I", elf, filesz_off, 0)
    assert elf_vector_table_base(bytes(elf)) is None


def test_vector_table_base_entry_exactly_at_vaddr_is_in_bounds():
    elf = _synthetic_elf(0x20000000, [(0x20000000, 0x20000000, 8192)])
    assert elf_vector_table_base(elf) == 0x20000000


def test_vector_table_base_entry_exactly_at_vaddr_plus_memsz_is_out_of_bounds():
    elf = _synthetic_elf(0x20002000, [(0x20000000, 0x20000000, 8192)])
    assert elf_vector_table_base(elf) is None


def test_vector_table_base_rejects_elf64():
    elf = bytearray(_synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)]))
    elf[4] = 2
    assert elf_vector_table_base(bytes(elf)) is None


def test_vector_table_base_rejects_big_endian():
    elf = bytearray(_synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)]))
    elf[5] = 2
    assert elf_vector_table_base(bytes(elf)) is None


def test_vector_table_base_rejects_a_too_small_phentsize():
    elf = bytearray(_synthetic_elf(0x20000201, [(0x20000000, 0x20000000, 8192)]))
    struct.pack_into("<H", elf, 0x2A, 16)
    assert elf_vector_table_base(bytes(elf)) is None
