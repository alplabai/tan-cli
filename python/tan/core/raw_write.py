# SPDX-License-Identifier: Apache-2.0
"""Pure facts for `tan flash --raw <file>@<addr>` (tan-cli#1446): a byte-exact,
sector-granular MRAM write for bench backup/restore.

Flow D writes a built image plus a signed ATOC. A bench that backed MRAM up with
`savebin` needs the saved sectors put back EXACTLY, and nothing else: no signing, no
address derivation, no reset. Everything here is IO-free; the J-Link session lives in
`tan.commands.flash_raw`.

**tan never invents an address.** Each `--raw` names its own address on the command
line; nothing is read from an `app-package-map.txt`, a manifest or a file name. That is
the whole ATOC/STOC rule: the Secure Enclave table lives in the last MRAM sectors, and a
write there is only ever one the user asked for by address.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tan.core.flash_plan import commander_path, validate_commander_path
from tan.core.flow_d_report import SECTOR_BYTES, hex_addr, sector_span

CODE_INVALID = "flash.raw-invalid"
CODE_RESERVATION = "flash.raw-reservation-required"
CODE_FAILED = "flash.raw-failed"


class RawError(ValueError):
    """A `--raw` input tan refuses before any spawn. `code` is the registered issue code."""

    code = CODE_INVALID


@dataclass(frozen=True)
class RawSpec:
    path: str
    address: int
    size: int = 0


def parse_raw(spec: str) -> RawSpec:
    """`<file>@<addr>` -> `RawSpec`. The LAST `@` splits, so a path may contain one; the
    address must be an explicit hex literal (`0x80010000`)."""
    path, sep, addr = spec.rpartition("@")
    if not sep or not path or not addr:
        raise RawError(f"--raw {spec!r} is not <file>@<address> (e.g. he_slot0.bin@0x80010000)")
    if not addr.lower().startswith("0x"):
        raise RawError(f"--raw {spec!r}: the address must be an explicit hex literal like 0x80010000")
    try:
        value = int(addr, 16)
    except ValueError:
        raise RawError(f"--raw {spec!r}: {addr!r} is not a hex address") from None
    validate_commander_path(path, "the --raw file path")
    return RawSpec(path=path, address=value)


def validate_ranges(
    specs: Sequence[RawSpec], mram_bytes: int, base: int
) -> None:
    """Refuse every unaligned, empty, out-of-MRAM or overlapping range. The loader rewrites
    whole 16 KiB sectors and fills a short tail with 0xFF, so a blob whose address or size
    is not sector-aligned would NOT be a byte-exact restore of what it names."""
    if not specs:
        raise RawError("--raw needs at least one <file>@<address>")
    end_of_mram = base + mram_bytes
    problems: list[str] = []
    for s in specs:
        where = f"{s.path}@{hex_addr(s.address)} ({s.size} B)"
        if s.size <= 0:
            problems.append(f"{where}: the file is empty")
            continue
        if s.address % SECTOR_BYTES:
            problems.append(f"{where}: the address is not {SECTOR_BYTES // 1024} KiB sector-aligned")
        if s.size % SECTOR_BYTES:
            problems.append(
                f"{where}: the size is not a whole number of {SECTOR_BYTES // 1024} KiB sectors -- "
                "the loader would fill the rest of the last sector with 0xFF, so this is not "
                "a byte-exact write; pad the saved blob to the sector boundary yourself"
            )
        if s.address < base or s.address + s.size > end_of_mram:
            problems.append(
                f"{where}: outside MRAM [{hex_addr(base)}, {hex_addr(end_of_mram)})"
            )
    ordered = sorted(specs, key=lambda s: s.address)
    for a, b in zip(ordered, ordered[1:]):
        if a.address + a.size > b.address:
            problems.append(
                f"{a.path}@{hex_addr(a.address)} and {b.path}@{hex_addr(b.address)} overlap"
            )
    if problems:
        raise RawError("; ".join(problems))


def raw_script(preamble: str, specs: Sequence[RawSpec]) -> str:
    """The write session: `preamble` (everything up to and including `connect`), a
    `loadbin` then `verifybin` per blob, `exit`. NO reset and NO `go`: the write must not
    boot, halt-resume or reset anything."""
    lines = [preamble.rstrip("\n")]
    for s in specs:
        lines.append(f"loadbin {commander_path(s.path)} {hex_addr(s.address)}")
    for s in specs:
        lines.append(f"verifybin {commander_path(s.path)} {hex_addr(s.address)}")
    lines.append("exit")
    return "\n".join(lines) + "\n"


def planned(specs: Sequence[RawSpec], sha256s: Sequence[str]) -> list[dict]:
    """Each blob as the envelope reports it: path, address, size, sha256, sector span."""
    return [
        {
            "path": s.path, "address": hex_addr(s.address), "size": s.size, "sha256": digest,
            "sectorSpan": sector_span(s.address, s.size),
        }
        for s, digest in zip(specs, sha256s)
    ]


def lease_holder(show_text: str) -> str | None:
    """The `acquired:` holder (`host/user`) in `labgrid-client -p <place> show` output,
    strictly: exactly one distinct, non-empty value across the `  acquired:` lines, else
    `None` (ambiguity is never a holder)."""
    holders = {
        line.split(":", 1)[1].strip()
        for line in show_text.splitlines()
        if line.startswith("  acquired:")
    }
    return holders.pop() if len(holders) == 1 and next(iter(holders), "x") else None


def lease_swd_path(show_text: str) -> str | None:
    """The USB path of the `swd` resource in `labgrid-client -p <place> show` output (the
    `'path': '3-4.2'` entry of the `Acquired|Matching resource 'swd'` block), or `None`.
    The `matches:` list at the top names the same resource without a path, so only the
    resource block counts."""
    import re

    inside = False
    for line in show_text.splitlines():
        if re.match(r"^(Acquired|Matching) resource 'swd'", line):
            inside = True
            continue
        if inside:
            if line and not line[0].isspace():
                return None
            found = re.search(r"'path': '([^']+)'", line)
            if found:
                return found.group(1)
    return None
