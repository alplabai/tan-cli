# SPDX-License-Identifier: Apache-2.0
"""Pure pre-flight logic for `tan renode` -- the deterministic half of the
native port of the retired `west alp-renode`.

Port of `crates/tan-core/src/renode/mod.rs` (the PLAIN, non-`--sim-mode` half
only -- see `tan.commands.renode_cmd`'s module docstring for why `--sim-mode`
is out of scope for this port). No IO: SoM SKU -> Renode platform-descriptor
mapping, single-Zephyr-slice selection, the headless Renode argv, and the two
console-line classifiers all live here so they are testable without a Renode
install. The subprocess + filesystem wiring is in `tan.commands.renode_cmd`.
"""
from __future__ import annotations

from typing import Any

#: SoM-family token -> Renode platform-descriptor stem under
#: `metadata/renode/<stem>.repl` + `<stem>.resc`. Mirrors the Rust
#: `FAMILY_TOKEN_TO_PLATFORM` -- the ONLY two wired stems today. The stems ARE
#: on-disk filenames (contract): keep them verbatim.
FAMILY_TOKEN_TO_PLATFORM: tuple[tuple[str, str], ...] = (
    ("alif_ensemble", "alif_ensemble_e8"),
    ("renesas_rzv2n", "renesas_rzv2n"),
)

#: SoM-family directory name -> soc-family token. Mirrors `west_libs._SOC_
#: FAMILY_TOKEN`.
_SOC_FAMILY_TOKEN = {
    "aen": "alif_ensemble",
    "v2n": "renesas_rzv2n",
    "v2n-m1": "renesas_rzv2n",
    "imx93": "nxp_imx9",
}

#: The E1M SKU-prefix token -> SoM family. Mirrors `alp_project_loader.
#: _sku_family`'s `^E1M-(AEN|V2N|V2M|NX9)` regex.
_SKU_TOKEN_TO_FAMILY = (
    ("AEN", "aen"),
    ("V2N", "v2n"),
    ("V2M", "v2n-m1"),
    ("NX9", "imx93"),
)

#: The SKU prefix stripped by `sku_family` before matching a family token.
#: Deliberately just the bare "E1M-" prefix (no trailing SKU digits).
#:
#: NOTE: this constant, together with `_SKU_TOKEN_TO_FAMILY`/
#: `_SOC_FAMILY_TOKEN`/`FAMILY_TOKEN_TO_PLATFORM` above, is SoM-SKU-family
#: branching living in tan -- the same I-26 category `tests/gates/
#: test_no_new_hardware_facts.py` already records as DEBT for `scaffold.py`
#: ("tan picks a template tree by SKU FAMILY, which is the vendor branching
#: I-26 forbids"). It is a faithful port of the oracle
#: (`crates/tan-core/src/renode/mod.rs`), inherited rather than invented, and
#: retires when the Renode SKU -> descriptor mapping is declared in alp-sdk
#: metadata instead.
_SKU_PREFIX = "E1M-"


class RenodeError(Exception):
    """Why a `tan renode` pre-flight failed. Every instance maps to
    `ExitCode.RUNTIME_FAILURE` (faithful to the retired Python's `log.die` =
    exit 1 for all of these); the CLI adds the one new `schema_version != 1`
    guard separately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _debug_list(items: list[str]) -> str:
    """Rust's `Vec<String>` `{:?}` spelling -- double-quoted, comma-separated,
    used only inside a message string."""
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _debug_option_str(value: str | None) -> str:
    """Rust's `Option<String>` `{:?}` spelling."""
    return f'Some("{value}")' if value is not None else "None"


def sku_family(sku: str) -> str:
    """SKU -> SoM family directory name. Mirrors `alp_project_loader.
    _sku_family`: `^E1M-(AEN|V2N|V2M|NX9)` -> `aen`/`v2n`/`v2n-m1`/`imx93`."""
    unrecognised = RenodeError(f"unrecognised SoM SKU pattern: {sku}")
    if not sku.startswith(_SKU_PREFIX):
        raise unrecognised
    rest = sku[len(_SKU_PREFIX) :]
    for token, family in _SKU_TOKEN_TO_FAMILY:
        if rest.startswith(token):
            return family
    raise unrecognised


def soc_family_token(family: str) -> str | None:
    """SoM family -> soc-family token."""
    return _SOC_FAMILY_TOKEN.get(family)


def _wired_tokens() -> list[str]:
    """The wired source tokens (keys of `FAMILY_TOKEN_TO_PLATFORM`), sorted --
    mirrors Rust's `sorted(_FAMILY_TOKEN_TO_PLATFORM)` for the error hint."""
    return sorted(token for token, _stem in FAMILY_TOKEN_TO_PLATFORM)


def platform_stem_for_sku(sku: str) -> str:
    """Full SKU -> Renode platform stem chain: SKU -> family -> token -> stem.
    Raises when the family has no wired descriptor yet."""
    family = sku_family(sku)
    token = soc_family_token(family)
    stem = None
    if token is not None:
        stem = next((s for t, s in FAMILY_TOKEN_TO_PLATFORM if t == token), None)
    if stem is not None:
        return stem
    raise RenodeError(
        f"no Renode platform descriptor for SoM family '{family}' "
        f"(token={_debug_option_str(token)}) of SKU {sku}; wired families: "
        f"{_debug_list(_wired_tokens())}. Add metadata/renode/<stem>.repl + "
        ".resc and a FAMILY_TOKEN_TO_PLATFORM entry to extend coverage."
    )


def platform_files_for_sku(sku: str, sdk_root: str) -> tuple[str, str]:
    """`(repl_path, resc_path)` under `<sdk_root>/metadata/renode/` for the
    SKU's family. No existence check -- the caller validates.

    A STRING join (`os.path.join`), not `pathlib`: matches the rest of the
    manifest-consuming surface (`tan.core.system_manifest`), which keeps a
    caller's own path style rather than re-rendering it.
    """
    import os  # noqa: PLC0415 -- keeps this module import-light at module scope

    stem = platform_stem_for_sku(sku)
    base = os.path.join(sdk_root, "metadata", "renode")
    return os.path.join(base, f"{stem}.repl"), os.path.join(base, f"{stem}.resc")


def select_sku(manifest: Any, board_override: str | None) -> str:
    """The SKU to drive descriptor selection: `--board` override, else the
    manifest's non-empty `hw_info.sku`, else a refusal.

    `manifest` is a `tan.core.system_manifest.SystemManifest`.
    """
    if board_override is not None:
        board = board_override.strip()
        if board:
            return board
    sku = (manifest.sku or "").strip()
    if not sku:
        raise RenodeError(
            "could not determine SoM SKU: manifest has no hw_info.sku and "
            "--board was not given."
        )
    return sku


def zephyr_elf_from_manifest(manifest: Any, build_root: str, core: str | None) -> str:
    """Resolve the single runnable Zephyr slice's `zephyr.elf` from the
    manifest.

    Filters `os == "zephyr"`; a slice is runnable when its `status` is not in
    `{blocked, skipped}`; the pool is the runnable set, or the full zephyr set
    when none are runnable (a single blocked zephyr slice still boots). Raises
    when the pool is empty, or has more than one entry and `core` was not
    given to disambiguate.

    `core` narrows the zephyr set to that one core BEFORE the runnable filter
    (so a multicore project can boot one slice instead of being refused
    outright), keeping the runnable-or-all fallback so an explicitly named
    blocked/skipped slice still boots exactly like a lone one does.

    A post-build `output_artefact` is PREFERRED when present -- used as-is
    when absolute, joined to `build_root` when relative. Otherwise the
    `build_dir` is used as-is when absolute, joined to `build_root` when
    relative, falling back to `build_root/<core_id>-<os>` when absent; the ELF
    is then `<that>/zephyr/zephyr.elf`.
    """
    import os  # noqa: PLC0415

    all_zephyr = [s for s in manifest.slices if s.get("os") == "zephyr"]
    if core is not None:
        zephyr = [s for s in all_zephyr if s.get("core_id") == core]
        if not zephyr:
            raise RenodeError(
                f"system-manifest.yaml has no zephyr slice for core '{core}'; "
                f"its zephyr cores are {_debug_list([s.get('core_id', '') for s in all_zephyr])}."
            )
    else:
        zephyr = all_zephyr

    runnable = [s for s in zephyr if s.get("status") not in ("blocked", "skipped")]
    pool = runnable if runnable else zephyr
    if not pool:
        raise RenodeError("system-manifest.yaml has no os: zephyr slice to boot in Renode.")
    if len(pool) > 1:
        cores = [s.get("core_id", "") for s in pool]
        raise RenodeError(
            f"system-manifest.yaml has {len(cores)} zephyr slices "
            f"(cores {_debug_list(cores)}); the Renode smoke boots a "
            "single-Zephyr-slice system. Pick one with `--core <CORE_ID>`. "
            "Booting several slices together (dual-OS) is a separate target."
        )

    s = pool[0]
    artefact = s.get("output_artefact") or ""
    if artefact:
        return artefact if os.path.isabs(artefact) else os.path.join(build_root, artefact)

    build_dir = s.get("build_dir") or ""
    if build_dir:
        base = build_dir if os.path.isabs(build_dir) else os.path.join(build_root, build_dir)
    else:
        base = os.path.join(build_root, f"{s.get('core_id', '')}-{s.get('os', '')}")
    return os.path.join(base, "zephyr", "zephyr.elf")


def build_renode_argv(
    renode_bin: str, repl: str, resc: str, elf: str, vtor: int | None
) -> list[str]:
    """The headless Renode command line. Injects the `.resc`'s `$repl` /
    `$elf` variables by value and includes the script. This ordering + flags
    is a machine contract -- VERBATIM: `--console --disable-xwt --plain` then
    `-e $repl=@... -e $elf=@... [-e $vtor=0x...] -e i @...`.

    `--hide-monitor` is deliberately never added: Renode 1.16.1 rejects it
    alongside `--console` ("--hide-monitor and --console cannot be set at the
    same time"), and it is redundant anyway -- `--disable-xwt` already implies
    it.

    `vtor` is assigned UNQUOTED on purpose: a quoted `$vtor="0x..."` reaches
    the script as a string and `cpu VectorTableOffset $vtor` dies with "Cannot
    convert type 'string' to 'uint'". Injected before the include so the
    script can read it.
    """
    argv = [
        renode_bin,
        "--console",
        "--disable-xwt",
        "--plain",
        "-e",
        f"$repl=@{repl}",
        "-e",
        f"$elf=@{elf}",
    ]
    if vtor is not None:
        # `0x` lowercase prefix, 8 uppercase hex digits, zero-padded -- Rust's
        # `{:#010X}`. Python's own `{:#010X}` format spec emits an UPPERCASE
        # `0X` prefix instead, which is why this is built by hand.
        argv.append("-e")
        argv.append(f"$vtor=0x{vtor & 0xFFFFFFFF:08X}")
    argv.append("-e")
    argv.append(f"i @{resc}")
    return argv


def renode_rejected_argv(line: str) -> bool:
    """Whether a console line is Renode telling us it REFUSED the command
    line -- either the complaint itself or the usage page it dumps
    afterwards.

    This exists because argv rejection is otherwise indistinguishable from a
    clean smoke: Renode prints its usage and exits 0, so the run reports
    success while nothing was ever simulated.
    """
    stripped = line.lstrip()
    return stripped.startswith("usage: renode") or "cannot be set at the same time" in line


def renode_cpu_halted(line: str) -> bool:
    """Whether a console line is Renode reporting that the CPU halted on its
    FIRST instruction fetch -- the firmware never ran a single instruction.

    Without `--expect`, a Renode that boots, halts the CPU, and then shuts
    down cleanly exits 0 just like a healthy smoke does (issue #64) -- neither
    the exit status nor an argv-rejection latch trips.
    """
    return "CPU was halted" in line or "PC does not lay in memory" in line


def elf_vector_table_base(elf: bytes) -> int | None:
    """The base address of the LOAD segment holding `elf`'s entry point --
    the image's real vector table, and the value Renode's `cpu
    VectorTableOffset` needs. `None` when `elf` isn't a 32-bit little-endian
    ELF, or no LOAD segment covers the entry (a malformed or unexpected
    image; the caller then injects nothing and Renode keeps its own guess).

    Renode guesses that offset from the LOWEST `vaddr` it sees, which is
    wrong for an MRAM-linked Zephyr image: its `.data` init segment has
    `vaddr` in DTCM (where it RUNS) but `paddr` in MRAM (where it is
    STORED), so Renode reads SP/PC out of an address nothing was ever loaded
    to, gets zeros, and halts the CPU before one instruction executes.
    Keying on the entry point instead of the lowest address is what makes
    this correct for BOTH shapes -- a RAM-run image's entry lands in its DTCM
    segment, an MRAM-linked one's in its MRAM segment.

    Physical (`p_paddr`), not virtual: the load address is where the bytes
    actually are, which is exactly what the vector-table fetch reads.

    Containment alone only proves the entry point falls inside this
    segment's runtime footprint -- it does NOT prove the vector table is the
    FIRST thing in the segment. So this also reads the table's own second
    word -- the reset vector -- and requires it to equal `e_entry` (Thumb bit
    cleared on both sides) before trusting the guess. `None` when the word
    isn't even in the file (`p_filesz < 8`) or doesn't match -- no answer
    beats a wrong one.
    """
    import struct  # noqa: PLC0415

    # e_ident: 0x7F 'E' 'L' 'F', class 1 (32-bit), data 1 (little-endian).
    if len(elf) < 52 or elf[0:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        return None

    def u32_at(off: int) -> int | None:
        if off < 0 or off + 4 > len(elf):
            return None
        return struct.unpack_from("<I", elf, off)[0]

    def u16_at(off: int) -> int | None:
        if off < 0 or off + 2 > len(elf):
            return None
        return struct.unpack_from("<H", elf, off)[0]

    entry_raw = u32_at(0x18)
    phoff = u32_at(0x1C)
    phentsize = u16_at(0x2A)
    phnum = u16_at(0x2C)
    if entry_raw is None or phoff is None or phentsize is None or phnum is None:
        return None
    # Thumb entry points carry bit 0 set; the address itself is even.
    entry = entry_raw & ~1

    # A real Elf32_Phdr is 32 bytes. A corrupt file with a smaller stride but
    # a still-valid e_ident makes every read below land inside the NEXT
    # phdr's fields instead of this one's, so a garbage file would otherwise
    # produce a plausible-looking wrong base instead of None.
    if phentsize < 32:
        return None

    for i in range(phnum):
        off = phoff + i * phentsize
        # Elf32_Phdr: type, offset, vaddr, paddr, filesz, memsz, ...
        p_type = u32_at(off)
        if p_type is None:
            return None
        if p_type != 1:  # PT_LOAD only
            continue
        p_offset = u32_at(off + 4)
        vaddr = u32_at(off + 8)
        paddr = u32_at(off + 12)
        filesz = u32_at(off + 16)
        memsz = u32_at(off + 20)
        if None in (p_offset, vaddr, paddr, filesz, memsz):
            return None
        if vaddr <= entry < vaddr + memsz:
            if filesz < 8:
                # The table's own second word isn't even in the file for
                # THIS segment -- keep scanning rather than abort: a
                # bss-only PT_LOAD whose memsz footprint happens to cover
                # the entry must not give up before a later, real code
                # segment gets examined.
                continue
            word1 = u32_at(p_offset + 4)
            if word1 is None:
                return None
            if (word1 & ~1) != entry:
                continue  # this segment starts with something else
            return paddr
    return None
