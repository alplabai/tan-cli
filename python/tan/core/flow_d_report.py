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
import re
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

#: What a matching read-back proves: a FRESH J-Link session read the same bytes. Still
#: not a cold-cycle proof (alp-sdk#2233).
VERIFICATION_READBACK_NOTE = (
    "a fresh J-Link session read the written regions back and their sha256 matches the "
    "source files; this is stronger than the flash cache but is not a cold-power-cycle "
    "proof (alp-sdk#2233)."
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


#: Core-debug DHCSR (0xE000EDF0) bits (tan-cli#1453). S_RESET_ST and S_RETIRE_ST are
#: sticky but CLEAR ON READ, and J-Link reads DHCSR itself (a failed halt, the probe's
#: own `connect`) before tan does -- so only S_RESET_ST (a reset happened since the last
#: read, with no lockup) counts as proof that THIS reset booted. S_SLEEP / S_RETIRE_ST
#: alone are also what the OLD image idling looks like, so they prove only that a core is
#: running, never that the reset took.
DHCSR_ADDRESS = "0xE000EDF0"
DHCSR_S_HALT = 1 << 17
DHCSR_S_SLEEP = 1 << 18
DHCSR_S_LOCKUP = 1 << 19
DHCSR_S_RETIRE_ST = 1 << 24
DHCSR_S_RESET_ST = 1 << 25
_DHCSR_RAN = DHCSR_S_SLEEP | DHCSR_S_RETIRE_ST | DHCSR_S_RESET_ST

#: Probe-transcript phrasing that means the probe session itself reset or tried to halt
#: the core, which makes its DHCSR read evidence of that session, not of the flash's reset.
PROBE_TROUBLE_MARKERS = ("Reset:",)

#: DWT_PCSR, the program-counter sample register: a NON-halting witness of where a RUNNING
#: core is executing (tan-cli#1453 review). DHCSR's S_RESET_ST is cleared by J-Link's own
#: reads and will usually be gone, so this is the second witness. It needs the DWT unit
#: present (Cortex-M55 has it) and readable while the core runs; a halted or sleeping core
#: returns 0xFFFFFFFF, which is no sample at all.
DWT_PCSR_ADDRESS = "0xE000101C"
PCSR_NO_SAMPLE = 0xFFFFFFFF
PCSR_SAMPLES = 3


def nohalt_probe_script(jlink_script: str) -> str:
    """A fresh, read-only, NON-halting session: the write's preamble up to and including
    `connect`, one `mem32` of DHCSR, `exit` (tan-cli#1453). Raises `ValueError` without a
    `connect` line."""
    out: list[str] = []
    for line in jlink_script.splitlines():
        out.append(line)
        if line.strip().lower() == "connect":
            break
    else:
        raise ValueError("the write script has no `connect` line to build a probe from")
    out.append(f"mem32 {DHCSR_ADDRESS} 1")
    for _ in range(PCSR_SAMPLES):
        out += [f"mem32 {DWT_PCSR_ADDRESS} 1", "Sleep 5"]
    out.append("exit")
    return "\n".join(out) + "\n"


def dhcsr_in(transcript: str) -> int | None:
    """The DHCSR word a `mem32 0xE000EDF0 1` printed (`E000EDF0 = 03050001`), or `None`."""
    match = re.search(r"E000EDF0\s*=\s*([0-9A-Fa-f]{8})", transcript)
    return int(match.group(1), 16) if match else None


def dhcsr_confirms_reset(value: int | None) -> bool:
    """True only when `value` shows a reset since the last read (S_RESET_ST) on a core
    that is neither halted nor locked up."""
    return (
        value is not None and bool(value & DHCSR_S_RESET_ST)
        and not value & (DHCSR_S_HALT | DHCSR_S_LOCKUP)
    )


def dhcsr_core_running(value: int | None) -> bool:
    """True when `value` shows a running core (retired an instruction, slept, or reset)
    that is not halted or locked up. This does NOT say the reset took."""
    return (
        value is not None and bool(value & _DHCSR_RAN)
        and not value & (DHCSR_S_HALT | DHCSR_S_LOCKUP)
    )


def pcsr_samples(transcript: str) -> list[int]:
    """Every PC sample a `mem32 0xE000101C 1` printed, in order (0xFFFFFFFF included)."""
    return [int(m, 16) for m in re.findall(r"E000101C\s*=\s*([0-9A-Fa-f]{8})", transcript)]


def pcsr_in_ranges(samples: Sequence[int], ranges: Sequence[tuple[int, int]]) -> bool:
    """True when at least one real sample exists and EVERY real sample lies inside
    `ranges` (`[start, end)` of the image tan just flashed). 0xFFFFFFFF is no evidence; a
    sample outside the ranges (old image, loader, ROM) vetoes. The old image is not known to
    tan, so an old image linked into the same range is not excluded -- that residual is why
    S_RESET_ST stays the stronger witness."""
    real = [x for x in samples if x != PCSR_NO_SAMPLE]
    return bool(real) and bool(ranges) and all(
        any(lo <= x < hi for lo, hi in ranges) for x in real
    )


def probe_trouble(transcript: str) -> tuple[str, ...]:
    """Reset/halt markers in a boot-probe transcript (it must have done neither)."""
    return tuple(m for m in (*PROBE_TROUBLE_MARKERS, *RESET_FAILURE_MARKERS) if m in transcript)


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


def reset_tail(jlink_script: str) -> list[str]:
    """The write script's reset/run tail -- from its first `RSetType` line up to (not
    including) the final `exit` -- so a read-back ends the same way the write did and
    the app is not left halted. `["g"]` when the script has no `RSetType`."""
    lines = [line for line in jlink_script.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("rsettype"):
            end = len(lines) - 1 if lines[-1].strip().lower() == "exit" else len(lines)
            return lines[i:end]
    return ["g"]


#: What an in-session read-back proves (tan-cli#1458): the chip's bytes were read back after a
#: halt, in the write's own session, before the reset -- NOT a fresh session, so J-Link's
#: flash cache may sit between the read and the cells.
VERIFICATION_INSESSION_NOTE = (
    "the written regions were read back with savebin in the SAME J-Link session as the write, "
    "after a halt and before the PIN reset (a separate session would let the app run in "
    "between); their sha256 matches the source files. Stronger than verifybin's cache compare "
    "but not a fresh-session or cold-power-cycle proof (alp-sdk#2233)."
)

#: J-Link Commander phrasing for "no debug access to the target right now" -- a gated
#: debug domain (the app is in STOP/WFI) as much as a bad cable (tan-cli#1450).
UNREACHABLE_MARKERS = (
    "Could not read memory",
    "Cannot read memory",
    "Cannot connect to target",
    "Could not connect to target",
    "Connecting to target failed",
)


def target_unreachable(transcript: str) -> bool:
    """Whether `transcript` says the target could not be reached for a memory read."""
    return any(m in transcript for m in UNREACHABLE_MARKERS)


def combined_script(jlink_script: str, regions: Sequence[tuple[str, int, str]]) -> str:
    """The write session WITH the read-back inside it (tan-cli#1458, bench): everything up to
    the first `RSetType` (connect, loadbin, verifybin), then `h`, one `savebin` per region,
    the write's own reset/run tail, `exit`. There is NO `exit` between the write and the read:
    J-Link's `exit` resumes the core even after `h`, so a separate read-back session let the
    app run on stale state before it connected. `regions` is `(address_hex, size, dest)`.
    Raises `ValueError` without a `RSetType` tail to keep (nothing to run after the read)."""
    lines = [line for line in jlink_script.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("rsettype"):
            break
    else:
        raise ValueError("the write script has no reset tail to run after the read-back")
    tail = reset_tail(jlink_script)
    out = [*lines[:i], "h"]
    for address, size, dest in regions:
        validate_commander_path(dest, "the read-back destination path")
        out.append(f"savebin {commander_path(dest)} {address} 0x{size:X}")
    return "\n".join([*out, *tail, "exit"]) + "\n"


def readback_script(
    jlink_script: str,
    regions: Sequence[tuple[str, int, str]],
) -> str:
    """The Commander script of a FRESH read-back session: the same preamble the
    write used (everything up to and including `connect`, so the same probe,
    interface, speed and part-number device), then one `savebin <file>, <addr>,
    <size>` per region and `exit`. `regions` is `(address_hex, size, dest_path)`.
    The session ends at `exit` WITHOUT the write's reset/run tail (a raw sector write must
    not reset or run anything, tan-cli#1446; Flow D reads inside its write session instead,
    see [`combined_script`]).
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
                    f"resident entry {r_name} at {hex_addr(r_addr)}, and no write here replaces it "
                    "(a write starting at exactly that address does)"
                )
    return out
