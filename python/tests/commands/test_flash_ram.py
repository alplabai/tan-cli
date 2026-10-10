# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1313: `tan flash --ram` -- AEN Flow C (J-Link ITCM RAM-run + RAM console).

No hardware: `_spawn_jlink` is a stub J-Link, and the ELF is a tiny fixture built
here with `struct`."""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from tan.commands import flash_cmd, flash_ram
from tan.core import ram_run
from tan.core.jlink_probe import JLinkProbe

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

CONSOLE_ADDR = 0x20001000
SERIAL = "000999000001"

#: What `tan flash --ram` journals for a clean load, as `J-Link>` prints it with a
#: command script: every command echoed, `loadbin` answering O.K., the memory-map banner
#: after `go`.
CLEAN_LOAD = (
    "Found Cortex-M55 r1p0, Little endian.\n"
    "J-Link>halt\nJ-Link>loadbin /tmp/tan-ram-x/image.bin 0x0\nDownloading file...\nO.K.\n"
    "J-Link>setpc 0x100\nJ-Link>go\nMemory map 'after startup completion point' is active\n"
    "Script processing completed.\n"
)
ITCM_WORDS = (0x20003000, 0x00000101, 0x00000105, 0x00000109)
OTHER_WORDS = (0xDEADBEEF, 0x0, 0x0, 0x0)


def _connect_banner(ap_addr="0x00300000", ap=3):
    """The `connect` banner the real transcript carries (bench round 7b)."""
    return (
        "DPv3 detected\n"
        f"AP[0]: AHB-AP (IDR: 0x84770001, ADDR: 0x00000000)\n"
        f"AP[{ap}] (APAddr {ap_addr}): AHB-AP (IDR: 0x34770008)\n"
        f"AP[{ap}]: Core found\n"
        "CPUID register: 0x411FD220\n"
        "Found Cortex-M55 r1p0, Little endian.\n"
    )


def _mem32(addr, words):
    return f"{addr:08X} = " + " ".join(f"{w:08X}" for w in words) + "\n"


def core_check_out(*, ap_addr="0x00300000", local=ITCM_WORDS, he=ITCM_WORDS):
    """A `mem32` core-check session: the banner, then the two dumps (a `None` window
    prints `Could not read memory.` and no dump line). The HP window is never read."""
    out = _connect_banner(ap_addr)
    for addr, words in ((0x0, local), (0x58000000, he)):
        out += _mem32(addr, words) if words is not None else "Could not read memory.\n"
    return out + "Script processing completed.\n"


#: A clean load transcript carries the same `connect` banner (AP, CPUID) the check does.
CLEAN_LOAD = _connect_banner() + CLEAN_LOAD.replace("Found Cortex-M55 r1p0, Little endian.\n", "", 1)

#: E8 HE apertures as the SoC metadata gives them (SRAM4_M55_HE_ITCM / SRAM5_M55_HE_DTCM
#: 256 KiB, SRAM0 4096 KiB).
E8_BANKS = [
    ("SRAM0", 4096.0), ("SRAM1", 4096.0), ("SRAM2_M55_HP_ITCM", 256.0),
    ("SRAM3_M55_HP_DTCM", 1024.0), ("SRAM4_M55_HE_ITCM", 256.0), ("SRAM5_M55_HE_DTCM", 256.0),
]


def make_elf(*, base=0x0, entry=0x101, filesz=64, symbols=None, extra_segments=()):
    """A little-endian ELF32 with LOAD segment(s) and a symbol table."""
    symbols = symbols or {}
    strtab = b"\0"
    syms = [struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)]
    for name, (addr, size) in symbols.items():
        syms.append(struct.pack("<IIIBBH", len(strtab), addr, size, 0x11, 0, 1))
        strtab += name.encode() + b"\0"
    symtab = b"".join(syms)
    segments = [(base, filesz), *extra_segments]
    phoff = 52
    sym_off = phoff + 32 * len(segments)
    str_off = sym_off + len(symtab)
    shoff = str_off + len(strtab)
    ehdr = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9) + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, entry, phoff, shoff, 0, 52, 32, len(segments), 40, 3, 0
    )
    phdrs = b"".join(struct.pack("<IIIIIIII", 1, 0, p, p, n, n, 5, 4) for p, n in segments)
    shdrs = (
        struct.pack("<IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        + struct.pack("<IIIIIIIIII", 0, 2, 0, 0, sym_off, len(symtab), 2, 0, 4, 16)
        + struct.pack("<IIIIIIIIII", 0, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)
    )
    return ehdr + phdrs + symtab + strtab + shdrs


def make_bin(size=64, sp=0x20003000, reset=0x101):
    return struct.pack("<II", sp, reset) + bytes(size - 8)


class FakeJlink:
    """A stub J-Link: answers ShowEmuList, the DPIDR preflight, the load session and
    the `mem8` read. Records every script it was handed, in order."""

    def __init__(self, monkeypatch, *, console=b"hello\r\nRESULT PASS\n", dpidr="0x4C013477",
                 load_out=None, emulators=(), read_rc=0, check_out=None):
        self.scripts: list[str] = []
        self.exes: list[object] = []
        self.console, self.dpidr = console, dpidr
        self.load_out = load_out if load_out is not None else CLEAN_LOAD
        self.emulators, self.read_rc = list(emulators), read_rc
        self.check_out = check_out if check_out is not None else core_check_out()
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def kind(self, script):
        if "ShowEmuList" in script:
            return "list"
        if "loadbin" in script:
            return "load"
        if "mem32" in script:
            return "check"
        if "mem8" in script:
            return "read"
        return "preflight"

    def _spawn(self, argv, script, capture, timeout, venv_bin=None, workspace=None,
               executable=None, **kw):
        self.scripts.append(script)
        self.exes.append(executable)
        kind = self.kind(script)
        if kind == "list":
            out = "\n".join(
                f"J-Link[{i}]: Connection: USB, Serial number: {sn}, ProductName: J-Link"
                for i, sn in enumerate(self.emulators)
            )
            return flash_cmd._Outcome(success=True, stdout=out, returncode=0)
        if kind == "preflight":
            return flash_cmd._Outcome(
                success=True, stdout=f"Found SW-DP with ID {self.dpidr}\n", returncode=0
            )
        if kind == "check":
            return flash_cmd._Outcome(success=True, stdout=self.check_out, returncode=0)
        if kind == "read":
            lines, addr = [], CONSOLE_ADDR
            data = self.console
            for i in range(0, len(data), 16):
                chunk = data[i : i + 16]
                lines.append(f"{addr + i:08X} = " + " ".join(f"{b:02X}" for b in chunk))
            full = bytes(self.console) + bytes(0x40 - len(self.console))
            lines = [
                f"{CONSOLE_ADDR + i:08X} = " + " ".join(f"{b:02X}" for b in full[i : i + 16])
                for i in range(0, 0x40, 16)
            ]
            return flash_cmd._Outcome(
                success=self.read_rc == 0,
                stdout="\n".join([*lines, "Script processing completed."]),
                returncode=self.read_rc,
            )
        return flash_cmd._Outcome(success=True, stdout=self.load_out, returncode=0)


_REAL_LOAD_APERTURES = flash_ram._load_apertures


def _manifest(args="{jlink_flash_device: PART}", extra_slices=""):
    return (
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        f"- {{core_id: m55_he, os: zephyr, output_artefact: zephyr.elf, status: ok,\n"
        f"   flash_method: zephyr_west_flash, flash_args: {args}}}\n{extra_slices}"
        "helper_mcus: []\nboot_order: []\n"
    )


def _setup(tmp_path, monkeypatch, *, elf=None, binary=None, manifest=None, artefact="zephyr"):
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / f"{artefact}.elf").write_bytes(
        elf if elf is not None else make_elf(symbols={"ram_console_buf": (CONSOLE_ADDR, 0x40)})
    )
    (build / f"{artefact}.bin").write_bytes(binary if binary is not None else make_bin())
    (tmp_path / "sdk" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (build / "system-manifest.yaml").write_text(
        (manifest or _manifest()).replace("zephyr.elf", f"{artefact}.elf"),
        encoding="utf-8", newline="",
    )
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    stub = tools / "JLinkExe"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.delenv("ALP_FLASH_REQUIRE_DPIDR", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(flash_cmd.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        flash_ram, "_load_apertures",
        lambda _ctx, core: ram_run.apertures_for(core, E8_BANKS),
    )
    return str(stub)


def _run(tmp_path, **kw):
    kw.setdefault("confirm_flag", True)  # --ram is confirm-gated (bench round 7)
    kw.setdefault("ram", True)
    kw.setdefault("ram_console", False)
    kw.setdefault("ram_wait", 0.0)
    kw.setdefault("core", "m55_he")
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"), board_yaml=None,
        helper=None, dry_run=kw.pop("dry_run", False), skip_missing_tools=False, capture=True,
        cwd=str(tmp_path), **kw,
    )


def _codes(issues):
    return [i.code for i in issues]


# ── the pure logic ──────────────────────────────────────────────────────────


def test_the_elf_reader_finds_segments_and_the_console_symbol():
    elf = ram_run.parse_elf(make_elf(base=0x58000000, symbols={"ram_console_buf": (0x20000100, 0x800)}))
    assert [(s.paddr, s.filesz) for s in elf.segments] == [(0x58000000, 64)]
    assert elf.symbols["ram_console_buf"] == (0x20000100, 0x800)
    for bad in (b"", b"not an elf", make_elf()[:30], b"\x7fELF\x02\x01\x01" + bytes(60)):
        with pytest.raises(ram_run.RamRunError):
            ram_run.parse_elf(bad)


def test_the_load_base_is_the_lowest_nonzero_filesz_segment_not_the_first():
    """A zero-FileSiz .bss LOAD in DTCM listed first must not become the load base."""
    elf = ram_run.parse_elf(make_elf(base=0x0, filesz=64, extra_segments=((0x20000228, 0),)))
    elf = ram_run.ElfImage(
        elf.entry, (ram_run.Segment(0x20000228, 0x20000228, 0, 0x100), *elf.segments), elf.symbols
    )
    image = ram_run.plan_ram_image(elf, make_bin())
    assert image.base == 0x0 and image.entry == 0x100 and image.initial_sp == 0x20003000


@pytest.mark.parametrize("base", [0x80010000, 0x80000000, 0x20000000, 0x40000000])
def test_an_mram_or_implausibly_linked_image_is_refused(base):
    elf = ram_run.parse_elf(make_elf(base=base, entry=base | 1))
    with pytest.raises(ram_run.RamRunError) as raised:
        ram_run.plan_ram_image(elf, make_bin(reset=(base | 1)))
    assert raised.value.code == ram_run.CODE_NOT_RAM_LINKED


@pytest.mark.parametrize("base", [0x0, 0x50000000, 0x58000000, 0x02000000])
def test_itcm_and_sram_bases_are_accepted(base):
    elf = ram_run.parse_elf(make_elf(base=base, entry=base | 1))
    assert ram_run.plan_ram_image(elf, make_bin(reset=base | 1)).base == base


def test_a_vector_table_that_disagrees_with_the_elf_is_refused():
    elf = ram_run.parse_elf(make_elf(entry=0x101))
    with pytest.raises(ram_run.RamRunError, match="disagree"):
        ram_run.plan_ram_image(elf, make_bin(reset=0x201))
    with pytest.raises(ram_run.RamRunError, match="not a RAM address"):
        ram_run.plan_ram_image(elf, make_bin(sp=0x80000000))
    with pytest.raises(ram_run.RamRunError, match="different builds"):
        ram_run.plan_ram_image(elf, make_bin(size=32))


def test_the_scripts_are_the_proven_recipe_and_hold_no_mram_address():
    image = ram_run.plan_ram_image(ram_run.parse_elf(make_elf()), make_bin())
    pre = ram_run.preamble(SERIAL, 4000, "Cortex-M55")
    load = ram_run.load_script(pre, "/t/image.bin", image).splitlines()
    assert load == [
        f"SelectEmuBySN {SERIAL}", "si SWD", "speed 4000", "device Cortex-M55", "connect",
        "halt", "loadbin /t/image.bin 0x0", "setpc 0x100", "go", "exit",
    ]
    read = ram_run.read_script(pre, 0x20001000, 0x10000 + 8).splitlines()
    assert read[-1] == "exit"
    assert "mem8 0x20001000, 0x10000" in read and "mem8 0x20011000, 0x8" in read
    assert not any(w.startswith("0x8") and len(w) == 10 for line in load for w in line.split())


def test_the_console_decoder_matches_the_scripts_awk():
    assert ram_run.decode_console(b"ab\r\ncd\x00\x00\x00\x00\x00tail") == "ab\n\ncd"
    assert ram_run.decode_console(b"x\x01\x7fy\x00\x00z") == "xyz"  # <= 4 NULs are skipped


def test_an_incomplete_mem8_dump_is_an_error_not_silence():
    with pytest.raises(ram_run.RamRunError, match="incomplete"):
        ram_run.read_back("20001000 = 41 42\nScript processing completed.", 0x20001000, 8)


def test_the_script_builders_refuse_free_form_values():
    for serial in ("1\nerase", "a b", "x;y"):
        with pytest.raises(ram_run.RamRunError):
            ram_run.preamble(serial, 4000, "Cortex-M55")
    with pytest.raises(ram_run.RamRunError):
        ram_run.preamble(None, 4000, "Cortex-M55\nerase")
    with pytest.raises(ram_run.RamRunError):
        ram_run.preamble(None, "4000\nerase", "Cortex-M55")  # type: ignore[arg-type]
    image = ram_run.plan_ram_image(ram_run.parse_elf(make_elf()), make_bin())
    with pytest.raises(ram_run.RamRunError):
        ram_run.load_script(["connect"], "/t/x.bin\nerase", image)


# ── the command ─────────────────────────────────────────────────────────────


def test_a_ram_run_loads_runs_and_reports_the_console(tmp_path, monkeypatch):
    stub = _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, lines, _s = _run(tmp_path, ram_console=True)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert entry["method"] == "ram_run" and entry["status"] == "ok"
    console = entry["ramConsole"]
    assert console["selected"] == "ram" and console["symbol"] == "ram_console_buf"
    assert console["address"] == "0x20001000" and console["size"] == 0x40
    assert console["text"] == "hello\n\nRESULT PASS\n"
    assert console["bytesRead"] == 0x40
    assert entry["ram"]["loadAddress"] == "0x00000000" and entry["ram"]["writesMram"] is False
    assert entry["ram"]["entry"] == "0x00000100" and entry["ram"]["initialSp"] == "0x20003000"
    assert entry["jlink"]["binary"] == stub
    assert [jl.kind(s) for s in jl.scripts] == ["check", "load", "read"]
    load = next(x for x in jl.scripts if jl.kind(x) == "load").splitlines()
    assert "halt" in load and "setpc 0x100" in load and "go" in load
    # The ONLY path in the script is tan's staged copy, never the project artefact.
    loadbin = next(l for l in load if l.startswith("loadbin "))
    assert "/tan-ram-" in loadbin and loadbin.endswith("/image.bin 0x0")
    assert str(tmp_path / "build") not in loadbin
    assert not Path(entry["ram"]["stagedImage"]).exists()  # removed with the entry
    assert all(e == stub for e in jl.exes)
    assert not any("0x8" in w[:3] for s in jl.scripts for w in s.split())  # no MRAM address
    assert Path(entry["jlink"]["transcriptPath"]).name.startswith("ram_run-m55_he-")


def test_a_uart_console_build_is_ok_and_says_so(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, elf=make_elf())  # no ram_console_buf
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, ram_console=True)
    assert rc == 0
    entry = data["entries"][0]
    assert entry["ramConsole"]["selected"] == "uart" and "text" not in entry["ramConsole"]
    assert "UART console" in entry["message"]
    warn = [i for i in issues if i.code == "flash.ram-console-symbol-missing"]
    assert warn and warn[0].severity == "warning"
    assert [jl.kind(s) for s in jl.scripts] == ["check", "load"]  # nothing to read


def test_without_ram_console_nothing_is_read(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 0 and [jl.kind(s) for s in jl.scripts] == ["check", "load"]
    assert "text" not in data["entries"][0]["ramConsole"]
    assert _codes(issues) == [
        "flash.ram-debugger-detach-clears-trcena", "flash.dpidr-preflight-unarmed",
    ]


def test_an_mram_linked_image_is_refused_before_any_spawn(tmp_path, monkeypatch):
    _setup(
        tmp_path, monkeypatch,
        elf=make_elf(base=0x80010000, entry=0x80010101),
        binary=make_bin(reset=0x80010101),
    )
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == []
    assert _codes(issues) == ["flash.ram-image-not-ram-linked"]
    assert "MRAM-linked" in data["entries"][0]["message"]


def test_dry_run_shows_the_whole_plan_and_spawns_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, dry_run=True, ram_console=True)
    assert rc == 0 and jl.scripts == []
    plan = data["entries"][0]["plan"]
    assert plan["jlinkScript"][0] == "exec DisableAutoUpdateFW"
    assert "setpc 0x100" in plan["jlinkScript"] and "go" in plan["jlinkScript"]
    assert any(l.startswith("mem8 0x20001000, 0x40") for l in plan["consoleReadScript"])
    assert data["entries"][0]["ramConsole"]["address"] == "0x20001000"


def test_the_dpidr_preflight_refuses_the_wrong_board_before_loading(tmp_path, monkeypatch):
    _setup(
        tmp_path, monkeypatch,
        manifest=_manifest("{jlink_flash_device: PART, expect_dpidr: '0x4C013477', jlink_device: Cortex-M55}"),
    )
    jl = FakeJlink(monkeypatch, dpidr="0x0BE12477")
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1
    assert [jl.kind(s) for s in jl.scripts] == ["preflight"]  # never reached `loadbin`
    assert "0x0BE12477" in data["entries"][0]["message"]
    # And the right board goes through, reporting the DPIDR it read.
    jl = FakeJlink(monkeypatch, dpidr="0x4C013477")
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 0 and [jl.kind(s) for s in jl.scripts] == ["preflight", "check", "load"]
    assert data["entries"][0]["jlink"]["dpidr"] == "0x4C013477"
    assert "flash.dpidr-preflight-unarmed" not in _codes(issues)


def test_require_dpidr_refuses_an_unarmed_ram_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("ALP_FLASH_REQUIRE_DPIDR", "1")
    jl = FakeJlink(monkeypatch)
    rc, _data, _i, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == []


def test_the_probe_guard_runs_before_the_load_and_before_the_read(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, emulators=[SERIAL])
    rc, data, issues, _l, _s = _run(
        tmp_path, ram_console=True, probe_usb_path="3-4.3",
        enumerate_probes=lambda: [JLinkProbe("3-4.1", "000603000869"), JLinkProbe("3-4.3", SERIAL)],
    )
    assert rc == 0, (data, issues)
    kinds = [jl.kind(s) for s in jl.scripts]
    assert kinds == ["list", "check", "list", "load", "list", "read"]  # verified before EACH spawn
    assert all(f"SelectEmuBySN {SERIAL}" in x for x in jl.scripts if jl.kind(x) in ("check", "load"))
    assert data["entries"][0]["probe"]["usbPath"] == "3-4.3"


def test_a_shared_serial_refuses_before_any_spawn(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, emulators=[SERIAL, SERIAL])
    rc, data, issues, _l, _s = _run(
        tmp_path, probe_serial=SERIAL,
        enumerate_probes=lambda: [JLinkProbe("3-4.1", SERIAL), JLinkProbe("3-4.2", SERIAL)],
    )
    assert rc == 1 and jl.scripts == []
    assert _codes(issues) == ["flash.probe-ambiguous"]


def test_a_load_that_did_not_complete_is_flash_ram_failed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch, load_out="Cannot connect to target.\n")
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and _codes(issues) == ["flash.ram-failed"]
    FakeJlink(monkeypatch, load_out="J-Link>loadbin x 0x0\nERROR\nJ-Link>setpc 0x100\n"
              "J-Link>go\nScript processing completed.\n")
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and "loadbin did not report" in data["entries"][0]["message"]


def test_a_failed_console_read_is_reported_with_the_image_running(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch, read_rc=1)
    rc, data, issues, _l, _s = _run(tmp_path, ram_console=True)
    assert rc == 1 and _codes(issues) == ["flash.ram-failed"]
    assert "the image is running" in data["entries"][0]["message"]


def test_exactly_one_slice_must_be_selected(tmp_path, monkeypatch):
    extra = (
        "- {core_id: m55_hp, os: zephyr, output_artefact: zephyr.elf, status: ok,\n"
        "   flash_method: zephyr_west_flash, flash_args: {jlink_flash_device: PART}}\n"
    )
    _setup(tmp_path, monkeypatch, manifest=_manifest(extra_slices=extra))
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, core=None)
    assert rc == 1 and jl.scripts == [] and _codes(issues) == ["flash.ram-failed"]
    assert "--core" in issues[0].message


def test_no_trusted_jlink_refuses_a_real_run_but_not_a_dry_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / "tools" / "JLinkExe").unlink()
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == [] and "trusted location" in data["entries"][0]["message"]
    rc, data, _i, _l, _s = _run(tmp_path, dry_run=True)
    assert rc == 0 and data["entries"][0]["jlink"]["binary"] is None


# ── script-injection hardening (security scan on #1313) ─────────────────────


@pytest.mark.parametrize(
    "hostile",
    ["a\nerase", "a\r\nloadbin x 0x80010000", 'q"uote', "nul\x00byte"],
)
def test_a_hostile_artefact_path_is_refused_before_any_spawn(tmp_path, monkeypatch, hostile):
    if "\x00" in hostile:
        pytest.skip("a NUL cannot be in a POSIX file name")
    _setup(tmp_path, monkeypatch, artefact=hostile)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == [], jl.scripts
    assert _codes(issues) == ["flash.ram-failed"]


def test_a_hostile_symbol_name_never_reaches_a_script(tmp_path, monkeypatch):
    """Only the EXACT `ram_console_buf` symbol is looked up and its address/size are
    ints: a symbol named with a newline neither matches nor is interpolated."""
    _setup(
        tmp_path, monkeypatch,
        elf=make_elf(symbols={"ram_console_buf\nerase": (CONSOLE_ADDR, 0x40),
                               "ram_console_buf": (CONSOLE_ADDR, 0x40)}),
    )
    jl = FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path, ram_console=True)
    assert rc == 0
    assert not any("erase" in s for s in jl.scripts)


def test_a_hostile_serial_or_device_is_refused_before_any_spawn(tmp_path, monkeypatch):
    for args in ("{jlink_flash_device: PART, jlink_serial: \"1\\nerase\"}",
                 "{jlink_flash_device: PART, jlink_device: \"Cortex-M55\\nerase\"}"):
        _setup(tmp_path, monkeypatch, manifest=_manifest(args))
        jl = FakeJlink(monkeypatch)
        rc, data, issues, _l, _s = _run(tmp_path)
        assert rc == 1 and jl.scripts == [], (args, jl.scripts)


def test_a_symlinked_project_path_still_loads_only_the_staged_copy(tmp_path, monkeypatch):
    """Even a perfectly valid project path is never put in the script."""
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path)
    assert rc == 0
    load = next(x for x in jl.scripts if jl.kind(x) == "load")
    assert str(tmp_path) not in load or "tan-ram-" in load
    assert "zephyr.bin" not in load


# ── review round (#1349): core, apertures, confirm, strict transcript ───────


def test_ram_run_needs_confirm_like_any_write(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, confirm_flag=False)
    assert rc == 1 and jl.scripts == []  # exit non-zero, nothing spawned
    entry = data["entries"][0]
    assert entry["status"] == "planned" and "--confirm was not given" in entry["message"]
    assert "AIRCR.SYSRESETREQ" in entry["message"] and "Secure Enclave" in entry["message"]
    assert "flash.confirm-required" in _codes(issues)
    assert "flash.nothing-flashed" in _codes(issues)
    # --dry-run is unaffected; the manifest key arms it too.
    rc, data, _i, _l, _s = _run(tmp_path, confirm_flag=False, dry_run=True)
    assert rc == 0 and data["entries"][0]["status"] == "ok"
    _setup(tmp_path, monkeypatch, manifest=_manifest("{jlink_flash_device: PART, confirm: true}"))
    jl = FakeJlink(monkeypatch)
    rc, _d, _i, _l, _s = _run(tmp_path, confirm_flag=False)
    assert rc == 0 and [jl.kind(s) for s in jl.scripts] == ["check", "load"]


def test_only_the_he_core_is_ram_run(tmp_path, monkeypatch):
    manifest = _manifest().replace("m55_he", "m55_hp")
    _setup(tmp_path, monkeypatch, manifest=manifest)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, core="m55_hp")
    assert rc == 1 and jl.scripts == []
    assert _codes(issues) == ["flash.ram-core-unsupported"]
    assert "HP core" in data["entries"][0]["message"]
    rc, _d, issues, _l, _s = _run(tmp_path, core="m55_hp", dry_run=True)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-unsupported"]


def test_an_image_linked_for_the_other_cores_itcm_alias_is_a_core_mismatch(tmp_path, monkeypatch):
    base = 0x50000000
    _setup(
        tmp_path, monkeypatch,
        elf=make_elf(base=base, entry=base | 1), binary=make_bin(reset=base | 1),
    )
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == [] and _codes(issues) == ["flash.ram-core-mismatch"]
    assert "M55_HP" in data["entries"][0]["message"]


def test_the_apertures_come_from_the_soc_banks_and_bound_the_image():
    apertures = ram_run.apertures_for("m55_he", E8_BANKS)
    assert apertures.code == ((0x0, 256 * 1024), (0x58000000, 256 * 1024), (0x02000000, 4096 * 1024))
    assert apertures.data == ((0x20000000, 256 * 1024), (0x02000000, 4096 * 1024))
    assert ram_run.apertures_for("m55_he", [("SRAM0", 4096.0)]) is None  # no TCM banks: refuse
    # Sizes follow the core's OWN banks (HP's DTCM is 1 MiB); whether HP may run at all is
    # `check_core`'s decision, not this function's.
    assert ram_run.apertures_for("m55_hp", E8_BANKS).data[0] == (0x20000000, 1024 * 1024)
    big = make_bin(size=300 * 1024)
    elf = ram_run.parse_elf(make_elf(filesz=300 * 1024))
    with pytest.raises(ram_run.RamRunError, match="does not fit"):
        ram_run.plan_ram_image(elf, big, core_id="m55_he", apertures=apertures)
    ok = ram_run.plan_ram_image(
        ram_run.parse_elf(make_elf(filesz=256 * 1024)), make_bin(size=256 * 1024),
        core_id="m55_he", apertures=apertures,
    )
    assert ok.size == 256 * 1024


def test_the_console_buffer_must_lie_in_dtcm_or_sram(tmp_path, monkeypatch):
    apertures = ram_run.apertures_for("m55_he", E8_BANKS)
    for addr, size in ((0x20040000, 0x40), (0x20000000 + 256 * 1024 - 0x10, 0x40), (0x1000, 0x40)):
        elf = ram_run.parse_elf(make_elf(symbols={"ram_console_buf": (addr, size)}))
        with pytest.raises(ram_run.RamRunError, match="outside the core's DTCM/SRAM"):
            ram_run.plan_ram_image(elf, make_bin(), core_id="m55_he", apertures=apertures)
    inside = ram_run.parse_elf(make_elf(symbols={"ram_console_buf": (0x20000100, 0x800)}))
    assert ram_run.plan_ram_image(
        inside, make_bin(), core_id="m55_he", apertures=apertures
    ).console == (0x20000100, 0x800)
    # The read is capped at 64 KiB however large the symbol claims to be.
    huge = ram_run.parse_elf(make_elf(symbols={"ram_console_buf": (0x02000000, 10_000_000)}))
    assert ram_run.plan_ram_image(
        huge, make_bin(), core_id="m55_he", apertures=apertures
    ).console == (0x02000000, 64 * 1024)


def test_without_soc_metadata_a_ram_run_refuses_rather_than_guesses(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(flash_ram, "_load_apertures", _REAL_LOAD_APERTURES)  # no metadata tree
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == [] and _codes(issues) == ["flash.ram-failed"]
    assert "refusing to guess the TCM sizes" in data["entries"][0]["message"]


def test_the_real_aperture_loader_reads_the_variant_banks(tmp_path, monkeypatch):
    import types

    from tan.commands import build_output

    (tmp_path / "metadata").mkdir()
    variant = {"order_code": "X", "alp_module_skus": ["S"],
               "sram_banks_kb": {n: k for n, k in E8_BANKS}}
    monkeypatch.setattr(
        build_output, "read_sdk_som_and_soc",
        lambda root, sku, **kw: ("alif:ensemble:e8", "X", [variant], 5.5, []),
    )
    ctx = types.SimpleNamespace(sdk_root=str(tmp_path), sku="S")
    assert flash_ram._load_apertures(ctx, "m55_he").data[0] == (0x20000000, 256 * 1024)
    monkeypatch.setattr(build_output, "read_sdk_som_and_soc", lambda *a, **k: None)
    with pytest.raises(ram_run.RamRunError, match="no readable SoM preset"):
        flash_ram._load_apertures(ctx, "m55_he")


def test_a_part_number_jlink_device_is_refused(tmp_path, monkeypatch):
    _setup(
        tmp_path, monkeypatch,
        manifest=_manifest("{jlink_flash_device: PART, jlink_device: AE822FA0E5597LS0_M55_HE}"),
    )
    jl = FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path)
    assert rc == 1 and jl.scripts == []
    assert "part-number profile" in data["entries"][0]["message"]


def test_a_transcript_that_does_not_prove_the_load_is_refused():
    clean = CLEAN_LOAD
    assert ram_run.check_session(clean, loadbin=True) is None
    # No echoes at all: the load cannot be confirmed (a stale ITCM image could boot).
    msg = ram_run.check_session("Script processing completed.\n", loadbin=True)
    assert msg and "cannot confirm the load" in msg
    # Echoed but no O.K.
    no_ok = clean.replace("O.K.\n", "")
    assert "did not report 'O.K.'" in ram_run.check_session(no_ok, loadbin=True)
    # A rejected setpc / go.
    bad_setpc = clean.replace("J-Link>setpc 0x100\n", "J-Link>setpc 0x100\nERROR: no\n")
    assert "setpc was rejected" in ram_run.check_session(bad_setpc, loadbin=True)
    bad_go = clean.replace("J-Link>go\n", "J-Link>go\nCPU could not be started\n")
    assert "go was rejected" in ram_run.check_session(bad_go, loadbin=True)
    # The read session needs no echoes.
    assert ram_run.check_session("Script processing completed.\n", loadbin=False) is None


def test_the_attached_core_is_reported_when_jlink_prints_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path)
    assert rc == 0
    attached = data["entries"][0]["jlink"]["attachedCore"]
    assert attached["apAddr"] == "0x00300000" and attached["coreFoundAp"] == 3
    assert attached["cpuid"] == "0x411FD220" and attached["found"].startswith("Found Cortex-M55")


def test_the_elf_reader_bounds_its_input():
    good = make_elf(symbols={"ram_console_buf": (0x20000000, 0x40)})
    # An Elf32_Sym entry size below 16 is not a Zephyr image.
    shoff = struct.unpack_from("<I", good, 32)[0]
    bad = bytearray(good)
    struct.pack_into("<I", bad, shoff + 40 + 36, 8)  # section 1 (symtab): sh_entsize = 8
    with pytest.raises(ram_run.RamRunError, match="entry size"):
        ram_run.parse_elf(bytes(bad))
    # Too many sections.
    many = bytearray(good)
    struct.pack_into("<H", many, 48, 5000)
    with pytest.raises(ram_run.RamRunError):
        ram_run.parse_elf(bytes(many))
    # Too many symbols.
    big = bytearray(good)
    struct.pack_into("<I", big, shoff + 40 + 20, 16 * (ram_run.MAX_SYMBOLS + 1))
    with pytest.raises(ram_run.RamRunError, match="entries"):
        ram_run.parse_elf(bytes(big))


# ── tan-cli#1354: is the probe on the HE core? ──────────────────────────────


def _check_run(tmp_path, monkeypatch, check_out, load_out=None, **kw):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, check_out=check_out, load_out=load_out)
    return jl, _run(tmp_path, **kw)


def test_the_he_ap_with_a_matching_itcm_is_confirmed(tmp_path, monkeypatch):
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out())
    assert rc == 0, (data, issues)
    check = data["entries"][0]["ram"]["coreCheck"]
    assert check["verdict"] == "he" and check["apVerdict"] == "he" and check["itcmVerdict"] == "match"
    assert check["ap"] == {
        "coreFoundAp": 3, "apAddr": "0x00300000", "cpuid": "0x411FD220",
        "found": "Found Cortex-M55 r1p0, Little endian.",
    }
    words = check["itcmWords"]
    assert set(words) == {"local0x00000000", "he0x58000000"}  # no HP window
    assert words["local0x00000000"] == words["he0x58000000"] == [f"0x{w:08X}" for w in ITCM_WORDS]
    assert check["assumeHe"] is False and "DECISIVE" in check["basis"]
    script = next(x for x in jl.scripts if jl.kind(x) == "check").splitlines()
    assert script[-3:] == ["mem32 0x0, 0x4", "mem32 0x58000000, 0x4", "exit"]
    assert "0x50000000" not in " ".join(jl.scripts)  # the HP ITCM window is NEVER read
    assert not any(w in script for w in ("halt", "go", "erase")) and "loadbin" not in " ".join(script)
    assert [jl.kind(x) for x in jl.scripts] == ["check", "load"]


def test_the_he_ap_alone_is_enough_when_the_itcm_is_silent(tmp_path, monkeypatch):
    """The AP decides; an unreadable ITCM corroboration does not veto an HE attach."""
    jl, (rc, data, _i, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out(he=None))
    assert rc == 0
    check = data["entries"][0]["ram"]["coreCheck"]
    assert check["verdict"] == "he" and check["itcmVerdict"] == "unreadable"


def test_the_itcm_alone_never_confirms_the_he(tmp_path, monkeypatch):
    """No AP evidence -> unconfirmed even when the ITCM matches the HE window."""
    out = core_check_out(ap_addr="0x00400000")
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-unconfirmed"]
    assert data["entries"][0]["ram"]["coreCheck"]["verdict"] == "unidentified"
    assert [jl.kind(x) for x in jl.scripts] == ["check"]
    out_none = "Found Cortex-M55 r1p0, Little endian.\n" + "".join(
        _mem32(a, ITCM_WORDS) for a in (0x0, 0x58000000)
    ) + "Script processing completed.\n"
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out_none)
    assert rc == 1 and data["entries"][0]["ram"]["coreCheck"]["verdict"] == "unidentified"


def test_the_hp_ap_is_refused_as_a_core_mismatch_before_any_load(tmp_path, monkeypatch):
    out = core_check_out(ap_addr="0x00200000", he=OTHER_WORDS)
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-mismatch"]
    assert [jl.kind(x) for x in jl.scripts] == ["check"]  # nothing was loaded
    assert "attached to the M55-HP" in data["entries"][0]["message"]
    assert data["entries"][0]["ram"]["coreCheck"]["verdict"] == "hp"


def test_hp_evidence_is_never_overridden_by_assume_he(tmp_path, monkeypatch):
    for out, verdict in (
        (core_check_out(ap_addr="0x00200000", he=OTHER_WORDS), "hp"),
        # An HP access port whose local ITCM nevertheless equals the HE window: still HP evidence.
        (core_check_out(ap_addr="0x00200000"), "conflict-hp"),
    ):
        jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out, assume_he=True)
        assert rc == 1 and _codes(issues) == ["flash.ram-core-mismatch"], verdict
        assert [jl.kind(x) for x in jl.scripts] == ["check"]
        assert data["entries"][0]["ram"]["coreCheck"]["verdict"] == verdict
        assert "never overrides HP evidence" in data["entries"][0]["message"]


def test_an_he_ap_contradicted_by_the_itcm_is_a_conflict_that_assume_he_cannot_override(
    tmp_path, monkeypatch
):
    out = core_check_out(he=OTHER_WORDS)  # AP says HE, local ITCM is not the HE window
    for kw in ({}, {"assume_he": True}):
        jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out, **kw)
        assert rc == 1 and _codes(issues) == ["flash.ram-core-unconfirmed"]
        assert data["entries"][0]["ram"]["coreCheck"]["verdict"] == "conflict"
        assert "not overridable" in data["entries"][0]["message"]
        assert [jl.kind(x) for x in jl.scripts] == ["check"]


@pytest.mark.parametrize(
    ("label", "out", "verdict"),
    [
        ("unplaceable-ap", core_check_out(ap_addr="0x00400000"), "unidentified"),
        ("failed-session", "Cannot connect to target.\nScript processing completed.\n", "unreadable"),
    ],
)
def test_missing_evidence_refuses_unless_assume_he(tmp_path, monkeypatch, label, out, verdict):
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-unconfirmed"], label
    assert [jl.kind(x) for x in jl.scripts] == ["check"]
    entry = data["entries"][0]
    assert entry["ram"]["coreCheck"]["verdict"] == verdict
    assert "--assume-he" in entry["message"]
    same_ap_load = CLEAN_LOAD.replace("APAddr 0x00300000", "APAddr 0x00400000")  # same AP as the check
    jl, (rc, data, issues, _l, _s) = _check_run(
        tmp_path, monkeypatch, out, load_out=same_ap_load, assume_he=True
    )
    assert rc == 0, (label, data, issues)
    assert [jl.kind(x) for x in jl.scripts] == ["check", "load"]
    assert data["entries"][0]["ram"]["coreCheck"]["assumed"] is True


def test_a_load_session_that_attached_elsewhere_fails_loudly(tmp_path, monkeypatch):
    """The load is a separate J-Link session: if its Core-found AP differs from the check's
    (or is HP) the entry fails with flash.ram-core-mismatch and BOTH attaches are reported."""
    hp_load = CLEAN_LOAD.replace("APAddr 0x00300000", "APAddr 0x00200000")
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out(), load_out=hp_load)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-mismatch"]
    assert [jl.kind(x) for x in jl.scripts] == ["check", "load"]  # it did load: the failure is after the fact
    jlink = data["entries"][0]["jlink"]
    assert jlink["attachedCore"]["apAddr"] == "0x00300000"
    assert jlink["attachedCoreAtLoad"]["apAddr"] == "0x00200000"
    assert "WRONG core" in data["entries"][0]["message"]
    # A load that attached to the same AP is silent.
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out())
    assert rc == 0 and data["entries"][0]["jlink"]["attachedCoreAtLoad"]["apAddr"] == "0x00300000"


def test_dry_run_shows_the_core_check_script_and_spawns_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path, dry_run=True)
    assert rc == 0 and jl.scripts == []
    script = data["entries"][0]["plan"]["coreCheckScript"]
    assert "mem32 0x58000000, 0x4" in script and not any("0x50000000" in l for l in script)


def test_the_mem32_parser_is_strict():
    words = ram_run.parse_mem32(_mem32(0x58000000, ITCM_WORDS), 0x58000000)
    assert words == ITCM_WORDS
    assert ram_run.parse_mem32("Could not read memory.\n", 0x0) is None
    assert ram_run.parse_mem32(_mem32(0x0, ITCM_WORDS[:3]), 0x0) is None  # short
    assert ram_run.parse_mem32(_mem32(0x4, ITCM_WORDS), 0x0) is None  # not from the address
    assert ram_run.parse_mem32("00000000 = 2000300G 00000101 00000105 00000109\n", 0x0) is None
    assert ram_run.parse_mem32("garbage 0 = 1 2 3 4\n", 0x0) is None


def test_the_decision_table():
    cv = ram_run.combine_verdicts
    assert [cv("he", "match"), cv("he", "unreadable"), cv("he", "disagree")] == ["he", "he", "conflict"]
    assert [cv("hp", "disagree"), cv("hp", "unreadable"), cv("hp", "match")] == ["hp", "hp", "conflict-hp"]
    assert [cv("unidentified", "match"), cv("multiple", "match"), cv("unidentified", "unreadable")] == ["unidentified"] * 3
    assert ram_run.OVERRIDABLE_VERDICTS == {"unidentified", "unreadable"}


def test_the_core_check_script_is_all_ints_and_read_only():
    script = ram_run.core_check_script(ram_run.preamble(SERIAL, 4000, "Cortex-M55")).splitlines()
    assert script == [
        f"SelectEmuBySN {SERIAL}", "si SWD", "speed 4000", "device Cortex-M55", "connect",
        "mem32 0x0, 0x4", "mem32 0x58000000, 0x4", "exit",
    ]


def test_assume_he_without_ram_is_a_usage_error(tmp_path):
    from typer.testing import CliRunner

    from tan.cli import app

    result = CliRunner().invoke(app, ["flash", "--assume-he", "--project", str(tmp_path)])
    assert result.exit_code != 0


def test_halt_and_reset_trouble_is_surfaced_like_flow_d(tmp_path, monkeypatch):
    """Bench round 8: a load that only worked through J-Link's fallback chain is reported."""
    trouble = CLEAN_LOAD.replace(
        "J-Link>halt\n",
        "J-Link>halt\nWARNING: CPU could not be halted\nSYSRESETREQ has confused core. "
        "Trying VECTRESET...\n",
    )
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch, load_out=trouble)
    rc, data, issues, lines, _s = _run(tmp_path)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert entry["jlink"]["resetFailures"] == [
        "CPU could not be halted", "SYSRESETREQ has confused core",
    ]
    assert "only worked through a J-Link fallback" in entry["message"]
    warn = [i for i in issues if i.code == "flash.jlink-reset-unconfirmed"]
    assert warn and warn[0].severity == "warning" and "CPU could not be halted" in warn[0].message
    # A clean load reports none.
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path)
    assert data["entries"][0]["jlink"]["resetFailures"] == []
    assert "flash.jlink-reset-unconfirmed" not in _codes(issues)


def test_every_ram_run_trouble_marker_is_detected():
    for marker in ram_run.RAM_RUN_TROUBLE_MARKERS:
        assert ram_run.trouble_markers(f"x\n{marker}\ny") == (marker,)
    assert {"CPU could not be halted", "Could not find core", "SYSRESETREQ has confused core",
            "Reset: Failed", "CPU may have not been reset"} <= set(ram_run.RAM_RUN_TROUBLE_MARKERS)
    assert ram_run.trouble_markers(CLEAN_LOAD) == ()


def test_the_check_never_reads_the_hp_window_anywhere():
    """The HP ITCM window read left the HE unhaltable on silicon (bench round 8)."""
    import inspect

    assert "0x50000000" not in ram_run.core_check_script(["connect"])
    source = inspect.getsource(ram_run.core_check) + inspect.getsource(ram_run.core_check_script)
    assert "HP_ALIAS" not in source


def _two_core_banner(first="0x00300000", second="0x00200000"):
    return (
        "DPv3 detected\n"
        f"AP[2] (APAddr {second}): AHB-AP (IDR: 0x34770008)\nAP[2]: Core found\n"
        f"AP[3] (APAddr {first}): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n"
        "CPUID register: 0x411FD220\nFound Cortex-M55 r1p0, Little endian.\n"
    )


def _out_with(banner):
    return banner + "".join(_mem32(a, ITCM_WORDS) for a in (0x0, 0x58000000)) + "Script processing completed.\n"


def test_several_core_found_aps_that_include_the_hp_are_hp_evidence(tmp_path, monkeypatch):
    out = _out_with(_two_core_banner())
    for kw in ({}, {"assume_he": True}):
        jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out, **kw)
        assert rc == 1 and _codes(issues) == ["flash.ram-core-mismatch"], kw
        assert [jl.kind(x) for x in jl.scripts] == ["check"]
        check = data["entries"][0]["ram"]["coreCheck"]
        assert check["apVerdict"] == "hp" and check["verdict"] == "conflict-hp"
        assert check["ap"]["multiple"] == ["0x00200000", "0x00300000"]


def test_several_core_found_aps_without_the_hp_are_only_unplaceable(tmp_path, monkeypatch):
    out = _out_with(_two_core_banner(first="0x00300000", second="0x00400000"))
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out)
    assert rc == 1 and _codes(issues) == ["flash.ram-core-unconfirmed"]
    assert data["entries"][0]["ram"]["coreCheck"]["verdict"] == "unidentified"
    # Only THIS case is overridable (and the load must then attach to the same APs).
    same = CLEAN_LOAD.replace(
        "AP[3] (APAddr 0x00300000): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n",
        "AP[2] (APAddr 0x00400000): AHB-AP (IDR: 0x34770008)\nAP[2]: Core found\n"
        "AP[3] (APAddr 0x00300000): AHB-AP (IDR: 0x34770008)\nAP[3]: Core found\n",
    )
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out, load_out=same, assume_he=True)
    assert rc == 0, (data, issues)


def test_trouble_in_the_check_session_is_reported_against_the_check(tmp_path, monkeypatch):
    out = core_check_out().replace(
        "Found Cortex-M55 r1p0, Little endian.\n",
        "Found Cortex-M55 r1p0, Little endian.\nWARNING: CPU could not be halted\n",
    )
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, out)
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert entry["ram"]["coreCheck"]["resetFailures"] == ["CPU could not be halted"]
    assert entry["jlink"]["resetFailures"] == ["CPU could not be halted"]
    assert "the core check reported halt/reset trouble" in entry["message"]
    assert "flash.jlink-reset-unconfirmed" in _codes(issues)


def test_a_load_banner_without_an_ap_is_noted_not_silently_skipped(tmp_path, monkeypatch):
    bare = CLEAN_LOAD.replace("AP[3] (APAddr 0x00300000): AHB-AP (IDR: 0x34770008)\n", "").replace(
        "AP[3]: Core found\n", ""
    )
    jl, (rc, data, issues, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out(), load_out=bare)
    assert rc == 0, (data, issues)
    jlink = data["entries"][0]["jlink"]
    assert jlink["attachedCoreAtLoad"] is None
    assert "not compared with the core check's" in jlink["attachedCoreAtLoadNote"]
    # A banner that DOES name the AP carries no such note.
    jl, (rc, data, _i, _l, _s) = _check_run(tmp_path, monkeypatch, core_check_out())
    assert "attachedCoreAtLoadNote" not in data["entries"][0]["jlink"]


# ── tan-cli#1372: the debugger-detach advisory (DEMCR.TRCENA) ────────────────

TRCENA_CODE = "flash.ram-debugger-detach-clears-trcena"


def _trcena(issues):
    return [i for i in issues if i.code == TRCENA_CODE]


def _assert_trcena_advisory(issues):
    found = _trcena(issues)
    assert len(found) == 1 and found[0].severity == "info"
    message = found[0].message
    assert "m55_he" in message and "DEMCR.TRCENA" in message
    assert "DWT" in message and "CYCCNT" in message and "set TRCENA again" in message


def test_a_successful_ram_run_carries_the_trcena_advisory(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch)
    rc, _data, issues, _l, _s = _run(tmp_path)
    assert rc == 0
    _assert_trcena_advisory(issues)


def test_a_ram_dry_run_carries_the_trcena_advisory(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, _data, issues, _l, _s = _run(tmp_path, dry_run=True)
    assert rc == 0 and jl.scripts == []
    _assert_trcena_advisory(issues)


def test_an_unconfirmed_ram_preview_has_no_trcena_advisory(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    _rc, _data, issues, _l, _s = _run(tmp_path, confirm_flag=False)
    assert jl.scripts == [] and _trcena(issues) == []


def test_a_refused_ram_run_has_no_trcena_advisory(tmp_path, monkeypatch):
    _setup(
        tmp_path, monkeypatch,
        elf=make_elf(base=0x80010000, entry=0x80010101), binary=make_bin(reset=0x80010101),
    )
    FakeJlink(monkeypatch)
    rc, _data, issues, _l, _s = _run(tmp_path)
    assert rc == 1 and _trcena(issues) == []


def test_the_ram_help_names_trcena_and_survives_rich_markup():
    from typer.main import get_command

    from tan.cli import app

    flash = get_command(app).get_command(None, "flash")
    help_text = next(p.help for p in flash.params if "--ram" in p.opts)
    assert "TRCENA" in help_text and "DEMCR" in help_text
    assert "[" not in help_text  # rich markup eats `[...]` in help text


# ── --watch (tan-cli#1436) ──────────────────────────────────────────────────

WATCH_DUMP = (
    "J-Link>mem32 0x42002000, 0x1\n42002000 = 00000001\n"
    "J-Link>mem32 0x42002000, 0x1\n42002000 = 00000003\n"
)


def test_a_watched_ram_run_samples_in_the_load_session_and_reports_data_watch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, load_out=CLEAN_LOAD + WATCH_DUMP)
    rc, data, issues, _l, _s = _run(
        tmp_path, ram_wait=0.1, ram_watch=("0x42002000@100",),
    )
    assert rc == 0, (data, issues)
    load = next(x for x in jl.scripts if jl.kind(x) == "load").splitlines()
    assert load[load.index("go") + 1 :] == ["mem32 0x42002000, 0x1", "Sleep 100", "mem32 0x42002000, 0x1", "exit"]
    assert [jl.kind(s) for s in jl.scripts] == ["check", "load"]  # no extra session
    assert data["watch"] == [
        {"address": "0x42002000", "words": 1, "index": 0, "elapsedMs": 0, "values": ["0x00000001"]},
        {"address": "0x42002000", "words": 1, "index": 1, "elapsedMs": 100, "values": ["0x00000003"]},
    ]
    assert "watch" not in data["entries"][0]
    assert data["entries"][0]["ram"]["watch"]["timeBasis"] == "scheduled"
    assert "flash.ram-watch-incomplete" not in _codes(issues)


def test_missing_samples_are_null_with_a_warning(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch)  # the load transcript carries no mem32 dump
    rc, data, issues, _l, _s = _run(tmp_path, ram_wait=0.0, ram_watch=("0x42002000",))
    assert rc == 0
    assert data["watch"][0]["values"] is None
    assert "flash.ram-watch-incomplete" in _codes(issues)


@pytest.mark.parametrize(
    "spec,code",
    [
        ("0x42002002", "flash.ram-watch-invalid"),
        ("0x42002000:0", "flash.ram-watch-invalid"),
        ("0x42002000:65", "flash.ram-watch-invalid"),
        ("0x42002000@5", "flash.ram-watch-invalid"),
        ("nonsense", "flash.ram-watch-invalid"),
        ("0x50000000", "flash.ram-watch-unsafe-address"),
        ("0x4FFFFFFC:2", "flash.ram-watch-unsafe-address"),
        ("0x57FFFFFC", "flash.ram-watch-unsafe-address"),
    ],
)
def test_unsafe_or_invalid_watches_are_refused_before_any_spawn(tmp_path, monkeypatch, spec, code):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, ram_watch=(spec,))
    assert rc != 0 and code in _codes(issues)
    assert jl.scripts == []


def test_the_he_window_and_neighbours_are_not_refused():
    from tan.core import ram_watch

    for ok in ("0x58000000", "0x4FFFFFFC", "0x0:4", "0x42002000:64@10"):
        ram_watch.parse_watch(ok)


def test_watch_refusals_hold_under_dry_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch)
    rc, _d, issues, _l, _s = _run(tmp_path, dry_run=True, ram_watch=("0x50000000",))
    assert rc != 0 and "flash.ram-watch-unsafe-address" in _codes(issues)
    assert jl.scripts == []


def test_a_missing_mid_sequence_dump_is_null_at_its_own_index_only():
    from tan.core import ram_watch as rw

    specs = rw.parse_watches(["0x42002000@100"])
    text = (
        "J-Link>mem32 0x42002000, 0x1\n42002000 = 00000001\n"
        "J-Link>Sleep 100\n"
        "J-Link>mem32 0x42002000, 0x1\n"
        "J-Link>Sleep 100\n"
        "J-Link>mem32 0x42002000, 0x1\n42002000 = 00000003\n"
    )
    out = rw.parse_samples(text, specs, 200)
    assert [o["values"] for o in out] == [["0x00000001"], None, ["0x00000003"]]


def test_the_load_timeout_scales_with_a_long_watch(tmp_path, monkeypatch):
    from tan.core import ram_watch as rw

    specs = rw.parse_watches(["0x42002000@100"])
    assert rw.session_timeout_s(rw.parse_watches(["0x42002000@2000"]), 3_000_000) > 3000 + 1000
    _setup(tmp_path, monkeypatch)
    seen = []
    jl = FakeJlink(monkeypatch)
    orig = jl._spawn

    def spy(argv, script, capture, timeout, *a, **kw):
        seen.append((jl.kind(script), timeout))
        return orig(argv, script, capture, timeout, *a, **kw)

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", spy)
    _run(tmp_path, ram_wait=1000.0, ram_watch=("0x42002000@1000",))
    load_timeout = next(t for k, t in seen if k == "load")
    assert load_timeout > 1000 + 1000 * 2.0


def test_no_data_at_all_has_its_own_message_and_spawn_ms(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, ram_wait=0.0, ram_watch=("0x42002000",))
    msg = next(i.message for i in issues if i.code == "flash.ram-watch-incomplete")
    assert "NO data" in msg
    assert isinstance(data["entries"][0]["ram"]["watch"]["spawnMs"], int)


def test_watch_with_ram_console_skips_the_pre_sleep_but_still_reads(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    jl = FakeJlink(monkeypatch, load_out=CLEAN_LOAD + WATCH_DUMP)
    slept = []
    monkeypatch.setattr(flash_ram.time, "sleep", lambda s: slept.append(s))
    rc, data, issues, _l, _s = _run(
        tmp_path, ram_console=True, ram_wait=0.1, ram_watch=("0x42002000@100",)
    )
    assert rc == 0, (data, issues)
    assert slept == []
    assert [jl.kind(s) for s in jl.scripts] == ["check", "load", "read"]
    assert data["entries"][0]["ramConsole"]["text"]


# ── tan-cli#1372 ask 2: the experimental hold session (TAN_FLASH_RAM_HOLD=1) ──
def _recording_timeouts(monkeypatch, jl):
    seen = []
    inner = jl._spawn

    def spy(argv, script, capture, timeout, *a, **kw):
        seen.append((jl.kind(script), timeout))
        return inner(argv, script, capture, timeout, *a, **kw)

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", spy)
    return seen


def _load_lines(jl):
    return next(x for x in jl.scripts if jl.kind(x) == "load").splitlines()


def test_the_default_load_script_has_no_sleep_and_waits_before_the_read(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("TAN_FLASH_RAM_HOLD", raising=False)
    sleeps = []
    monkeypatch.setattr(flash_cmd.time, "sleep", sleeps.append)
    jl = FakeJlink(monkeypatch)
    rc, data, _i, _l, _s = _run(tmp_path, ram_console=True, ram_wait=2.5)
    assert rc == 0
    load = _load_lines(jl)
    assert load[-3:] == ["setpc 0x100", "go", "exit"] and not any(l.startswith("Sleep") for l in load)
    assert sleeps == [2.5]
    assert data["entries"][0]["ram"]["holdSession"] is False


def test_the_hold_load_script_sleeps_after_go_and_the_read_does_not_wait(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TAN_FLASH_RAM_HOLD", "1")
    sleeps = []
    monkeypatch.setattr(flash_cmd.time, "sleep", sleeps.append)
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, ram_console=True, ram_wait=2.5)
    assert rc == 0, (data, issues)
    load = _load_lines(jl)
    assert load[-4:] == ["setpc 0x100", "go", "Sleep 2500", "exit"]
    assert not any("DEMCR" in l.upper() or "0XE000" in l.upper() for l in load)
    assert sleeps == []  # no second wait before the reread
    assert [jl.kind(s) for s in jl.scripts] == ["check", "load", "read"]
    entry = data["entries"][0]
    assert entry["ram"]["holdSession"] is True
    assert entry["ramConsole"]["text"] == "hello\n\nRESULT PASS\n"
    assert "Sleep 2500" in entry["plan"]["jlinkScript"]
    found = _trcena(issues)
    assert len(found) == 1 and "session close" in found[0].message and "after the wait" in found[0].message


def test_the_hold_session_timeout_covers_the_wait(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TAN_FLASH_RAM_HOLD", "1")
    jl = FakeJlink(monkeypatch)
    timeouts = _recording_timeouts(monkeypatch, jl)
    rc, *_ = _run(tmp_path, ram_console=True, ram_wait=2000.0)
    assert rc == 0
    (load_timeout,) = [t for k, t in timeouts if k == "load"]
    assert load_timeout >= 2000.0 + 60.0


def test_hold_needs_a_console_read_and_otherwise_stays_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TAN_FLASH_RAM_HOLD", "1")
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, ram_console=False, ram_wait=2.5)
    assert rc == 0
    assert not any(l.startswith("Sleep") for l in _load_lines(jl))
    assert data["entries"][0]["ram"]["holdSession"] is False
    assert "session close" not in _trcena(issues)[0].message


def test_hold_dry_run_shows_the_sleep_script(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TAN_FLASH_RAM_HOLD", "1")
    jl = FakeJlink(monkeypatch)
    rc, data, issues, _l, _s = _run(tmp_path, dry_run=True, ram_console=True, ram_wait=1.5)
    assert rc == 0 and jl.scripts == []
    entry = data["entries"][0]
    assert entry["ram"]["holdSession"] is True
    assert entry["plan"]["jlinkScript"][-3:] == ["go", "Sleep 1500", "exit"]
    assert "session close" in _trcena(issues)[0].message
