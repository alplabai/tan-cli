# SPDX-License-Identifier: Apache-2.0
r"""Pure comparison of alp-sdk's per-artifact sha256 pins (`metadata/toolchains.json`)
against the release's own `sha256.sum` (tan-cli#1496).

What is and is NOT verified, stated once (every message and doc defers to this):

* tan downloads BOTH pinned archives itself -- the SDK minimal bundle
  (tan-cli#1556; `west sdk install` used to fetch it) and the toolchain archive
  (tan-cli#1496; the SDK's `setup.sh` used to fetch it with `wget` and check
  nothing) -- and hashes each against its alp-sdk pin while it streams
  (`tan.commands.bootstrap_toolchain_fetch`). That hash is the integrity check.
* The release's own `sha256.sum` is NOT a gate (tan-cli#1559): it lives under
  the same `baseUrl` as the archives, so it adds no integrity the pin does not
  already give. It is fetched only after a hash mismatch, to tell the reader
  which kind it was ([`mismatch_diagnosis`]); an unreachable sum never blocks
  an install whose archives hashed to the pin.
* Nothing is re-hashed from disk after install. That is the residual trust.

The sum-file parser accepts a strict SUBSET of what west matches and refuses
everything else (fail-closed). West does `re.split(r"\s+", line)` -> `{t[1]: t[0]}`
with the last duplicate winning; tan accepts only a line of exactly two tokens, a
lowercase 64-hex digest and the literal name, and refuses where west is looser or
tan cannot tell what west would do: a `*name` marker or `dir/name` prefix (a
different key to west), uppercase hex (west compares against a lowercase
hexdigest), a third token, duplicates with differing hashes (west silently takes
the last), and any other line mentioning a pinned filename. West's behaviour on
lines naming no pinned file is irrelevant and ignored. **No IO here** -- the sum text arrives already read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tan.core.toolchain_provision import ToolchainArtifact, ToolchainManifest

#: The release's own checksum file, next to the archives, under the manifest's `baseUrl`.
SUM_FILENAME = "sha256.sum"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def sum_url(manifest: ToolchainManifest) -> str:
    return manifest.base_url.rstrip("/") + "/" + SUM_FILENAME


@dataclass(frozen=True)
class SumTable:
    #: literal second token -> every hash listed for it, in file order (west keeps the last).
    entries: dict[str, tuple[str, ...]]
    #: stripped lines west would mis-read or skip (not exactly `<64 lowercase hex> <name>`).
    odd_lines: tuple[str, ...]


def parse_sum_file(text: str) -> SumTable:
    """West's parse, kept faithfully, plus a record of every line it would trip on."""
    entries: dict[str, list[str]] = {}
    odd: list[str] = []
    for line in text.splitlines():
        tokens = re.split(r"\s+", line.strip())
        if tokens == [""]:
            continue
        if len(tokens) != 2 or not _HEX64.match(tokens[0]):
            odd.append(line.strip())
            continue
        entries.setdefault(tokens[1], []).append(tokens[0])
    return SumTable({k: tuple(v) for k, v in entries.items()}, tuple(odd))


@dataclass(frozen=True)
class PinFinding:
    artifact: ToolchainArtifact
    #: What the sum file lists (west's view: the last entry); `None` = no entry.
    published: str | None
    #: Set when tan refuses to guess which entry west would use.
    problem: str | None = None


def pin_findings(
    artifacts: tuple[ToolchainArtifact, ...], table: SumTable
) -> tuple[PinFinding, ...]:
    """Every artifact whose published sum is absent, ambiguous, or differs from its pin."""
    out = []
    for art in artifacts:
        name = art.filename
        hashes = table.entries.get(name, ())
        published = hashes[-1] if hashes else None
        problem = None
        if len(set(hashes)) > 1:
            problem = f"{len(hashes)} entries with different hashes"
        else:
            lookalikes = [
                k for k in table.entries
                if k != name and k.lstrip("*").replace("\\", "/").rsplit("/", 1)[-1] == name
            ]
            lookalikes += [ln for ln in table.odd_lines if name in ln]
            if lookalikes:
                problem = f"unparseable or marker/path-prefixed entry west would not match: {lookalikes[0]!r}"
        if problem is not None or published is None or published != art.sha256.strip().lower():
            out.append(PinFinding(art, published, problem))
    return tuple(out)


def mismatch_diagnosis(
    art: ToolchainArtifact, got: str | None, sum_text: str | None, why: str
) -> str:
    """Which way a pin mismatch most likely happened, read off the release's
    own sum (tan-cli#1559) -- fetched ONLY after a mismatch, never as a gate:
    the archive's hash against the pin is the whole integrity check, and the
    sum lives under the same `baseUrl`, so it can explain a refusal but never
    excuse or cause one.

    Worded as EVIDENCE, never as a verdict: the sum arrives over the same
    channel that just delivered bad bytes, so whoever rewrote the archive could
    rewrite it too. Nothing here may read as permission to move the pin.

    `got` is the downloaded sha256, or `None` when the download was refused
    for exceeding the pinned size before it could be hashed. `sum_text` is
    `None` when the sum could not be fetched (`why` says why); the refusal
    stands either way."""
    caveat = "Confirm on the release page before touching the pin."
    if sum_text is None:
        return f"The release {SUM_FILENAME} could not be fetched to explain this ({why})."
    found = pin_findings((art,), parse_sum_file(sum_text))
    if found and found[0].problem is not None:
        return f"The release {SUM_FILENAME} served here is ambiguous for this file ({found[0].problem})."
    listed = found[0].published if found else art.sha256.strip().lower()
    if listed is None:
        return f"The release {SUM_FILENAME} served here has no entry for this file."
    if listed == art.sha256.strip().lower():
        return (
            f"The release {SUM_FILENAME} served here still lists the pinned hash -- "
            f"consistent with the bytes changing in transit (a TLS-intercepting "
            f"proxy, a corrupted mirror or CDN copy). {caveat}"
        )
    if got is not None and listed == got:
        return (
            f"The release {SUM_FILENAME} served here lists the hash that was "
            f"downloaded -- consistent with upstream republishing this archive after "
            f"alp-sdk pinned it, but this sum came over the same channel. {caveat}"
        )
    return (
        f"The release {SUM_FILENAME} served here lists {listed}, which is not the pin"
        + ("" if got is None else " and not what was downloaded")
        + f". {caveat}"
    )
