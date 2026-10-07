# SPDX-License-Identifier: Apache-2.0
"""Pure facts about a Flow D write, for the envelope (tan-cli#1321, #1318).

Two halves, both IO-free:

**What a J-Link transcript says (tan-cli#1321).** `verifybin` compares the file
against J-Link's flash CACHE, not the chip, so "verified" overstated what was
proven (alp-sdk#2233: on this bench only a fresh-session read after a cold cycle
proves a write). The envelope reports `verification: "cache-verified"`, the SW-DP
ID the transcript actually carried, and every reset-failure marker in it
(tan-cli#522: `Failed to halt CPU` was already caught; `Reset: Failed` and
`CPU may have not been reset` are the same condition worded by other J-Link
builds).

**What a write would do (tan-cli#1318).** Every write as `{address, size,
sectorSpan}` with 16 KiB sectors -- the AEN MRAM loader rewrites whole sectors
and fills the remainder with 0xFF, so a write's footprint is wider than its
size, and the operator needs it before arming.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from tan.core.dp_id import _dp_id_value
from tan.core.flash_plan import FlashPlanError, commander_path, validate_commander_path

#: The MRAM loader's erase/rewrite granule on the AEN parts (tan-cli#1318).
SECTOR_BYTES = 16 * 1024

#: What `verifybin` proves, in the one spelling the envelope and message use.
VERIFICATION_CACHE = "cache-verified"
VERIFICATION_READBACK = "readback-verified"
#: Stated once, beside the field, so a consumer is not left reading
#: "verified" as "the chip holds these bytes".
VERIFICATION_NOTE = (
    "verifybin compares the image against J-Link's flash cache, not the chip; a "
    "fresh-session read after a cold power cycle is what proves a write "
    "(alp-sdk#2233). --readback adds a fresh-session read-back + sha256 compare."
)

#: J-Link Commander phrasing that means the PIN reset (`RSetType 2` / `r` / `g`)
#: did not land. Substring-matched against the captured transcript; the exit
#: code cannot tell these runs apart (JLinkExe still exits 0).
RESET_FAILURE_MARKERS = (
    "Failed to halt CPU",
    "CPU is not halted",
    "Reset: Failed",
    "CPU may have not been reset",
)


def reset_failures(transcript: str) -> tuple[str, ...]:
    """Every reset-failure marker present in `transcript`, in marker order."""
    return tuple(m for m in RESET_FAILURE_MARKERS if m in transcript)


def dpidr_in(transcript: str) -> str | None:
    """The SW-DP ID the transcript reports (`Found SW-DP with ID 0x...` /
    `DPIDR: 0x...`), verbatim, or `None` when it names none."""
    return _dp_id_value(transcript)


def transcript_tail(transcript: str, lines: int = 40) -> list[str]:
    """The last `lines` non-empty lines of `transcript`."""
    kept = [line.rstrip() for line in transcript.splitlines() if line.strip()]
    return kept[-lines:]


def sector_span(address: int, size: int, sector: int = SECTOR_BYTES) -> dict[str, Any]:
    """The sectors a write of `size` bytes at `address` touches: the first
    sector's base address, the last sector's END (exclusive), the count and the
    byte footprint (count * sector). A zero-size write touches nothing."""
    if size <= 0:
        return {"first": hex_addr(address - address % sector), "end": hex_addr(address - address % sector),
                "count": 0, "bytes": 0, "sectorBytes": sector}
    first = address - address % sector
    last = (address + size - 1) - (address + size - 1) % sector
    count = (last - first) // sector + 1
    return {
        "first": hex_addr(first),
        "end": hex_addr(last + sector),
        "count": count,
        "bytes": count * sector,
        "sectorBytes": sector,
    }


def hex_addr(value: int) -> str:
    return f"0x{value:08X}"


def planned_write(name: str, address: str, size: int | None, path: str | None) -> dict[str, Any]:
    """One planned write as the envelope reports it. `size` is `None` when the
    file is not readable yet (the dry-run of a hand-written manifest)."""
    entry: dict[str, Any] = {"name": name, "address": address, "path": path, "size": size}
    if size is not None:
        entry["sectorSpan"] = sector_span(int(address, 16), size)
    return entry


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def readback_script(
    jlink_script: str,
    regions: Sequence[tuple[str, int, str]],
) -> str:
    """The Commander script of a FRESH read-back session: the same preamble the
    write used (everything up to and including `connect`, so the same probe,
    interface, speed and part-number device), then one `savebin <file>, <addr>,
    <size>` per region and `exit`. `regions` is `(address_hex, size, dest_path)`.
    Raises `ValueError` if `jlink_script` has no `connect` line."""
    out: list[str] = []
    for line in jlink_script.splitlines():
        out.append(line)
        if line.strip().lower() == "connect":
            break
    else:
        raise ValueError("the write script has no `connect` line to build a read-back from")
    for address, size, dest in regions:
        validate_commander_path(dest, "the read-back destination path")
        out.append(f"savebin {commander_path(dest)} {address} 0x{size:X}")
    out.append("exit")
    return "\n".join(out) + "\n"


# ── sector overlap (tan-cli#1343 review) ────────────────────────────────────

CODE_SECTOR_OVERLAP = "flash.write-sector-overlap"


class SectorOverlapError(FlashPlanError):
    """Two writes (or a write and a resident entry that will not be rewritten)
    share a 16 KiB sector. `code` is the registered issue code."""

    code = CODE_SECTOR_OVERLAP


def _interval(address: int, size: int | None) -> tuple[int, int]:
    """`[first, end)` of the sectors touched by `size` bytes at `address`; an
    unknown size counts as one sector (the least it can touch)."""
    first = address - address % SECTOR_BYTES
    last = address + max((size or 1), 1) - 1
    return first, last - last % SECTOR_BYTES + SECTOR_BYTES


def find_overlaps(
    writes: Sequence[dict[str, Any]],
    resident: Sequence[tuple[str, int | None, int | None]] = (),
) -> list[str]:
    """Human descriptions of every sector overlap among `writes`
    (`{name, address, size}` dicts) and between a write and a `resident`
    `(name, address, size)` region (entries with no address are skipped: tan
    cannot place what it was not told). The loader rewrites WHOLE sectors, so a
    write whose tail reaches the next write's first sector would erase the
    neighbour's head."""
    spans = [
        (w["name"], *_interval(int(w["address"], 16), w.get("size")))
        for w in writes
        if w.get("address")
    ]
    out: list[str] = []
    for i, (a_name, a_lo, a_hi) in enumerate(spans):
        for b_name, b_lo, b_hi in spans[i + 1 :]:
            if a_lo < b_hi and b_lo < a_hi:
                lo, hi = max(a_lo, b_lo), min(a_hi, b_hi)
                out.append(
                    f"the {a_name} write and the {b_name} write share the 16 KiB sector(s) "
                    f"{hex_addr(lo)}-{hex_addr(hi)}"
                )
    for r_name, r_addr, r_size in resident:
        if r_addr is None:
            continue
        r_lo, r_hi = _interval(r_addr, r_size)
        for w_name, w_lo, w_hi in spans:
            if w_lo < r_hi and r_lo < w_hi:
                out.append(
                    f"the {w_name} write's sectors {hex_addr(w_lo)}-{hex_addr(w_hi)} cover the "
                    f"resident entry {r_name} at {hex_addr(r_addr)}, which this ATOC does not rewrite"
                )
    return out
