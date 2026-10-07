# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1371: the MRAM write path refuses an ELF that is not MRAM-linked.

The mirror of `flash.ram-image-not-ram-linked`: an ITCM-linked (`p_paddr` 0x0)
image written to MRAM slot0 is wrong, whatever the manifest says (a hand-edited or
stale manifest, a build whose manifest write failed). No hardware: every spawn is a
stub, and the ELF is the tiny `struct` fixture `test_flash_ram` builds."""
from __future__ import annotations

import os

import pytest

from tan.commands import flash_cmd, flash_mram_guard
from tan.core import mram_link
from tests.commands.test_flash_command import _flow_d_run
from tests.commands.test_flash_ram import make_elf

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

MRAM_BASE = 0x80000000
CODE = "flash.mram-image-not-mram-linked"

#: Flow D with the ATOC supplied, and Flow D with NO ATOC (tan signs it via SETOOLS).
ATOC_GIVEN = None  # `_flow_d_run`'s default flash_args
AUTO_SIGN = (
    '{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000", '
    "confirm: true, atoc_unqueryable: true}"
)


# ── the pure check ──────────────────────────────────────────────────────────


def test_an_itcm_linked_elf_is_refused_with_its_base_named():
    message = mram_link.mram_link_refusal(make_elf(base=0x0), MRAM_BASE)
    assert message is not None
    assert "0x0" in message and "0x80000000" in message and "ITCM" in message


@pytest.mark.parametrize("base", [0x80000000, 0x80010000])
def test_an_mram_linked_elf_is_accepted(base):
    assert mram_link.mram_link_refusal(make_elf(base=base, entry=base | 1), MRAM_BASE) is None


def test_the_lowest_segment_with_file_content_decides_not_the_first():
    """A zero-FileSiz .bss LOAD at 0x20000000 must not make an MRAM image look low."""
    elf = make_elf(base=0x80010000, entry=0x80010001, extra_segments=((0x20000000, 0),))
    assert mram_link.mram_link_refusal(elf, MRAM_BASE) is None


def test_a_file_that_is_not_an_elf_is_not_verifiable_and_is_left_alone():
    assert mram_link.mram_link_refusal(b"\x00" * 64, MRAM_BASE) is None


def test_an_unresolved_aperture_base_refuses_rather_than_guessing():
    message = mram_link.mram_link_refusal(make_elf(base=0x80010000), None)
    assert message is not None and "soc_flash_base" in message


# ── the write path ──────────────────────────────────────────────────────────


def _run(tmp_path, monkeypatch, *, elf, base=MRAM_BASE, flash_args=ATOC_GIVEN):
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "a.elf").write_bytes(elf)
    monkeypatch.setattr(flash_mram_guard, "soc_flash_base", lambda _ctx: base)
    spawned: list = []
    jlink: list = []
    monkeypatch.setattr(
        flash_cmd, "_spawn_jlink",
        lambda *a, **k: jlink.append((a, k)) or flash_cmd._Outcome(success=True, stdout=""),
    )
    kwargs = {} if flash_args is ATOC_GIVEN else {"flash_args": flash_args}
    result = _flow_d_run(tmp_path, monkeypatch, spawned=spawned, **kwargs)
    return result, spawned, jlink


@pytest.mark.parametrize("flash_args", [ATOC_GIVEN, AUTO_SIGN], ids=["atoc-given", "auto-sign"])
def test_an_itcm_linked_image_is_refused_before_any_spawn(tmp_path, monkeypatch, flash_args):
    (rc, data, issues, _l, _s), spawned, jlink = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), flash_args=flash_args
    )
    assert rc == 1
    assert [i.code for i in issues] == [CODE]
    assert "0x0" in issues[0].message and "0x80000000" in issues[0].message
    assert "alif_mram_jlink[m55_hp]" in issues[0].message
    assert spawned == [] and jlink == []  # no app-gen-toc, no J-Link, not even a listing
    assert data["entries"][0]["status"] == "failed"


def test_the_refusal_holds_under_dry_run_too(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "a.elf").write_bytes(make_elf(base=0x0))
    monkeypatch.setattr(flash_mram_guard, "soc_flash_base", lambda _ctx: MRAM_BASE)
    rc, _d, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch, dry_run=True)
    assert rc == 1 and [i.code for i in issues] == [CODE]


def test_a_correctly_linked_image_is_not_refused(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), _sp, _jl = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x80010000, entry=0x80010001)
    )
    assert CODE not in [i.code for i in issues]
    assert rc == 0


def test_an_elf_with_no_resolvable_aperture_base_is_refused(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), spawned, _jl = _run(
        tmp_path, monkeypatch, elf=make_elf(base=0x80010000), base=None
    )
    assert rc == 1 and [i.code for i in issues] == [CODE]
    assert "soc_flash_base" in issues[0].message and spawned == []


def test_a_bare_bin_with_no_elf_beside_it_is_not_checked(tmp_path, monkeypatch):
    """A raw .bin carries no link address; nothing to check, nothing refused."""
    monkeypatch.setattr(flash_mram_guard, "soc_flash_base", lambda _ctx: MRAM_BASE)
    rc, _d, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch)
    assert CODE not in [i.code for i in issues] and rc == 0


# ── Flow A: `zephyr_west_flash` with the Alif runner (tan-cli#1371, Flow A half) ──
#
# Flow A burns MRAM slot0 over the SE-UART when `west flash` falls back to the board
# default (`alif_flash`). Unlike Flow D, an unresolved `soc_flash_base` SKIPS here:
# `zephyr_west_flash` also serves non-MRAM boards, so "no MRAM aperture known" is not
# evidence of a problem.

_WEST_MANIFEST = """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m55_he, os: zephyr, output_artefact: zephyr.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: FLASH_ARGS}
helper_mcus: []
boot_order: []
"""


def _west_run(tmp_path, monkeypatch, *, elf, base=MRAM_BASE, flash_args="{}", dry_run=False):
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / "zephyr.elf").write_bytes(elf)
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (build / "system-manifest.yaml").write_text(
        _WEST_MANIFEST.replace("FLASH_ARGS", flash_args), encoding="utf-8", newline=""
    )
    tools = tmp_path / "faketools"
    tools.mkdir(exist_ok=True)
    (tools / "west").write_text("", encoding="utf-8")
    os.chmod(tools / "west", 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_mram_guard, "soc_flash_base", lambda _ctx: base)
    spawned: list = []
    monkeypatch.setattr(
        flash_cmd, "_spawn",
        lambda *a, **k: spawned.append(a) or flash_cmd._Outcome(success=True, stdout=""),
    )
    result = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=dry_run,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
    )
    return result, spawned


@pytest.mark.parametrize(
    ("flash_args", "dry_run"),
    [("{}", False), ("{runner: alif_flash}", False), ("{}", True)],
    ids=["no-runner", "alif_flash", "dry-run"],
)
def test_flow_a_refuses_an_itcm_linked_image_before_west_spawns(
    tmp_path, monkeypatch, flash_args, dry_run
):
    (rc, data, issues, _l, _s), spawned = _west_run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), flash_args=flash_args, dry_run=dry_run
    )
    assert rc == 1 and [i.code for i in issues] == [CODE]
    assert "zephyr_west_flash[m55_he]" in issues[0].message
    assert "0x0" in issues[0].message and "0x80000000" in issues[0].message
    assert spawned == [] and data["entries"][0]["status"] == "failed"


def test_flow_a_accepts_an_mram_linked_image(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), spawned = _west_run(
        tmp_path, monkeypatch, elf=make_elf(base=0x80010000, entry=0x80010001)
    )
    assert rc == 0 and CODE not in [i.code for i in issues] and spawned


def test_flow_a_skips_when_the_aperture_base_is_unresolved(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), spawned = _west_run(tmp_path, monkeypatch, elf=make_elf(base=0x0), base=None)
    assert rc == 0 and CODE not in [i.code for i in issues] and spawned


def test_flow_a_does_not_check_an_explicit_other_runner(tmp_path, monkeypatch):
    (rc, _d, issues, _l, _s), spawned = _west_run(
        tmp_path, monkeypatch, elf=make_elf(base=0x0), flash_args="{runner: jlink}"
    )
    assert rc == 0 and CODE not in [i.code for i in issues] and spawned
