# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1371: Flow D refuses an ELF whose load addresses sit below the app's MRAM slot.

Checked only for the shapes tan itself controls: the mramxip `loadbin` at
`flash_args.slot0_load_address` and the SETOOLS auto-sign (`mramAddress`). A supplied
ATOC may legitimately carry an ITCM load entry and is NOT checked. Flow A (`west flash`
on the `alif_flash` runner) is not checked either: that runner refuses a bad image
itself and supports images linked at the ITCM global alias. No hardware: every spawn is
a stub."""
from __future__ import annotations

import os
import struct
from types import SimpleNamespace

import pytest

from tan.commands import flash_cmd, flash_mram_guard
from tan.core import mram_link
from tests.commands.test_flash_command import _flow_d_run
from tests.commands.test_flash_ram import make_elf

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SLOT0 = 0x80010000
SOC_FLASH_BASE = 0x80000000
CODE = "flash.mram-image-not-mram-linked"

#: `_flow_d_run`'s default: mramxip shape (`slot0_load_address`) with a supplied ATOC.
SLOT0_AND_ATOC = None
#: No ATOC: tan signs one via SETOOLS (which needs `slot0_load_address`).
AUTO_SIGN = (
    '{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000", '
    "confirm: true, atoc_unqueryable: true}"
)
#: Single-ATOC shape: an operator-supplied ATOC, no slot0 -- not tan's to check.
SUPPLIED_ATOC_ONLY = (
    '{jlink_flash_device: PART_PROFILE, atoc: atoc.bin, atoc_address: "0x8057F5B0", '
    "confirm: true, atoc_unqueryable: true}"
)


def mini_elf(segments=((0x0, 0x0, 64),), *, ident_class=1, ident_data=1, phnum=None,
             shoff=0, shnum=0, cut=None):
    """A little-endian ELF32 with ONLY program headers: `(vaddr, paddr, filesz)` each."""
    phnum = len(segments) if phnum is None else phnum
    ehdr = b"\x7fELF" + bytes([ident_class, ident_data, 1]) + bytes(9) + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, 0x101, 52, shoff, 0, 52, 32, phnum, 40, shnum, 0
    )
    phdrs = b"".join(struct.pack("<IIIIIIII", 1, 0, v, p, n, n, 5, 4) for v, p, n in segments)
    data = ehdr + phdrs
    return data if cut is None else data[:cut]


# ── the pure check ──────────────────────────────────────────────────────────


def test_an_image_loaded_below_the_floor_is_refused_with_both_addresses():
    message = mram_link.mram_link_refusal(make_elf(base=0x0), SLOT0, "slot0_load_address")
    assert message is not None
    assert "0x0" in message and "0x80010000" in message and "slot0_load_address" in message


@pytest.mark.parametrize("base", [SLOT0, SLOT0 + 0x400])
def test_an_image_loaded_at_or_above_the_floor_is_accepted(base):
    elf = make_elf(base=base, entry=base | 1)
    assert mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address") is None


def test_between_soc_flash_base_and_slot0_is_still_below_the_floor():
    elf = make_elf(base=SOC_FLASH_BASE)
    assert mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address") is not None


def test_the_lowest_segment_with_file_content_decides_not_the_first():
    """A zero-FileSiz .bss LOAD at 0x20000000 must not make an MRAM image look low."""
    elf = make_elf(base=SLOT0, entry=SLOT0 | 1, extra_segments=((0x20000000, 0),))
    assert mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address") is None


def test_the_load_address_decides_not_the_run_address():
    """An image LOADed into MRAM (p_paddr >= slot0) that RUNS from ITCM (p_vaddr 0x0)."""
    elf = mini_elf([(0x0, SLOT0, 64)])
    assert mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address") is None


def test_the_symbol_table_is_never_read():
    """A hostile section table must neither refuse nor skip: program headers only."""
    elf = mini_elf([(SLOT0, SLOT0, 64)], shoff=1, shnum=5000)
    assert mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address") is None


@pytest.mark.parametrize(
    ("elf", "why"),
    [
        (mini_elf(ident_class=2), "ELF32"),
        (mini_elf(ident_data=2), "little-endian"),
        (mini_elf(cut=30), "truncated"),
        (mini_elf(phnum=5), "past the end"),
        (mini_elf([(0x0, 0x0, 0)]), "no LOAD segment with file content"),
    ],
    ids=["elf64", "big-endian", "truncated-header", "phnum-past-eof", "no-loadable-segment"],
)
def test_an_elf_that_cannot_be_read_is_refused_not_skipped(elf, why):
    message = mram_link.mram_link_refusal(elf, SLOT0, "slot0_load_address")
    assert message is not None
    assert "could not be parsed" in message and why in message


# ── the real SoC lookup (no monkeypatching) ─────────────────────────────────


def _metadata(tmp_path, *, preset="schema_version: 2\nsilicon: acme:fam:part\n",
              soc='{"soc_flash_base": 2147483648}', write_preset=True, write_soc=True):
    meta = tmp_path / "sdk" / "metadata"
    (meta / "e1m_modules").mkdir(parents=True)
    if write_preset:
        (meta / "e1m_modules" / "S.yaml").write_text(preset, encoding="utf-8")
    if write_soc:
        (meta / "socs" / "acme" / "fam").mkdir(parents=True)
        (meta / "socs" / "acme" / "fam" / "part.json").write_text(soc, encoding="utf-8")
    return SimpleNamespace(sdk_root=str(tmp_path / "sdk"), sku="S")


def test_soc_flash_base_is_read_from_the_soc_json_the_preset_names(tmp_path):
    assert flash_mram_guard.soc_flash_base(_metadata(tmp_path)) == 0x80000000


@pytest.mark.parametrize(
    ("kwargs", "step"),
    [
        ({"write_preset": False}, "SoM preset"),
        ({"preset": "schema_version: 1\nsilicon: acme:fam:part\n"}, "schema_version"),
        ({"write_soc": False}, "SoC JSON"),
        ({"soc": "{}"}, "soc_flash_base"),
    ],
    ids=["preset-missing", "schema-unsupported", "soc-json-missing", "key-missing"],
)
def test_an_unresolved_base_names_the_step_that_failed(tmp_path, kwargs, step):
    ctx = _metadata(tmp_path, **kwargs)
    with pytest.raises(flash_mram_guard.ApertureUnresolved) as raised:
        flash_mram_guard.soc_flash_base(ctx)
    assert step in str(raised.value)


def test_with_no_slot0_the_floor_is_the_soc_flash_base(tmp_path):
    ctx = _metadata(tmp_path)
    (tmp_path / "a.elf").write_bytes(make_elf(base=0x0))
    message = flash_mram_guard.mram_link_guard(str(tmp_path / "a.elf"), "m55_he", ctx, slot0=None)
    assert message is not None and "0x80000000" in message and "soc_flash_base" in message


def test_with_no_slot0_and_no_metadata_the_guard_refuses_and_says_why(tmp_path):
    ctx = _metadata(tmp_path, write_preset=False)
    (tmp_path / "a.elf").write_bytes(make_elf(base=SLOT0))
    message = flash_mram_guard.mram_link_guard(str(tmp_path / "a.elf"), "m55_he", ctx, slot0=None)
    assert message is not None and "SoM preset" in message


# ── the pairing ─────────────────────────────────────────────────────────────


def test_find_elf_returns_the_bytes_of_the_artefact_or_its_same_stem_elf(tmp_path):
    (tmp_path / "a.elf").write_bytes(make_elf(base=SLOT0))
    (tmp_path / "a.bin").write_bytes(b"\x00" * 8)
    assert flash_mram_guard.find_elf(str(tmp_path / "a.elf")) == make_elf(base=SLOT0)
    assert flash_mram_guard.find_elf(str(tmp_path / "a.bin")) == make_elf(base=SLOT0)
    assert flash_mram_guard.find_elf(str(tmp_path / "missing.bin")) is None
    (tmp_path / "b.bin").write_bytes(b"\x00" * 8)
    assert flash_mram_guard.find_elf(str(tmp_path / "b.bin")) is None


# ── the write path ──────────────────────────────────────────────────────────


def _run(tmp_path, monkeypatch, *, elf, flash_args=SLOT0_AND_ATOC, dry_run=False):
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "a.elf").write_bytes(elf)
    jlink: list = []
    monkeypatch.setattr(
        flash_cmd, "_spawn_jlink",
        lambda *a, **k: jlink.append((a, k)) or flash_cmd._Outcome(success=True, stdout=""),
    )
    spawned: list = []
    kwargs = {} if flash_args is SLOT0_AND_ATOC else {"flash_args": flash_args}
    result = _flow_d_run(tmp_path, monkeypatch, spawned=spawned, dry_run=dry_run, **kwargs)
    return result, spawned, jlink


@pytest.mark.parametrize("flash_args", [SLOT0_AND_ATOC, AUTO_SIGN], ids=["slot0+atoc", "auto-sign"])
def test_an_itcm_linked_image_is_refused_before_any_spawn(tmp_path, monkeypatch, flash_args):
    (rc, data, issues, _l, _s), spawned, jlink = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), flash_args=flash_args
    )
    assert rc == 1
    assert [i.code for i in issues] == [CODE]
    assert "0x0" in issues[0].message and "0x80010000" in issues[0].message
    assert "alif_mram_jlink[m55_hp]" in issues[0].message
    assert spawned == [] and jlink == []  # no app-gen-toc, no J-Link, not even a listing
    assert data["entries"][0]["status"] == "failed"


def test_the_refusal_holds_under_dry_run_too(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), _sp, _jl = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), dry_run=True
    )
    assert rc == 1 and [i.code for i in issues] == [CODE]


def test_an_image_below_slot0_but_inside_the_aperture_is_refused(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), _sp, _jl = _run(tmp_path, monkeypatch, elf=make_elf(base=0x80000000))
    assert rc == 1 and [i.code for i in issues] == [CODE]


def test_a_correctly_linked_image_is_not_refused(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), _sp, _jl = _run(
        tmp_path, monkeypatch, elf=make_elf(base=SLOT0, entry=SLOT0 | 1)
    )
    assert CODE not in [i.code for i in issues] and rc == 0


def test_an_image_loaded_into_mram_but_run_from_itcm_is_not_refused(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), _sp, _jl = _run(tmp_path, monkeypatch, elf=mini_elf([(0x0, SLOT0, 64)]))
    assert CODE not in [i.code for i in issues] and rc == 0


def test_a_supplied_atoc_with_no_slot0_is_not_checked(tmp_path, monkeypatch):
    """The ATOC may carry a legitimate ITCM load entry; it is the operator's, not tan's."""
    (rc, _d, issues, _l, _s), _sp, _jl = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), flash_args=SUPPLIED_ATOC_ONLY
    )
    assert CODE not in [i.code for i in issues] and rc == 0


def test_an_unparseable_elf_is_refused_on_flow_d(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), spawned, jlink = _run(
        tmp_path, monkeypatch, elf=mini_elf(ident_class=2)
    )
    assert rc == 1 and [i.code for i in issues] == [CODE]
    assert "could not be parsed" in issues[0].message and spawned == [] and jlink == []


def test_a_bare_bin_with_no_elf_beside_it_is_not_checked(tmp_path, monkeypatch):
    rc, _d, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch)
    assert CODE not in [i.code for i in issues] and rc == 0

