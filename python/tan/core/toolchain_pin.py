# SPDX-License-Identifier: Apache-2.0
r"""Pure comparison of alp-sdk's per-artifact sha256 pins (`metadata/toolchains.json`)
against the release's own `sha256.sum` (tan-cli#1496).

What is and is NOT verified, stated once (every message and doc defers to this):

* tan downloads BOTH pinned archives itself -- the SDK minimal bundle
  (tan-cli#1556; `west sdk install` used to fetch it) and the toolchain archive
  (tan-cli#1496; the SDK's `setup.sh` used to fetch it with `wget` and check
  nothing) -- and hashes each against its alp-sdk pin while it streams
  (`tan.commands.bootstrap_toolchain_fetch`). That hash is the integrity check.
* Before downloading, tan also compares the pins with the release's own sum, so a
  pin that disagrees with what upstream publishes is refused up front with both
  hashes named, instead of surfacing as a bare download mismatch.
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


def refusal_message(findings: tuple[PinFinding, ...], url: str, *, when: str = "before") -> str:
    lines = [
        f"cannot acquire the cross toolchain: the release's {SUM_FILENAME} ({url}) "
        f"disagrees with the sha256 alp-sdk pins in metadata/toolchains.json "
        f"({when}), so the install is not trusted:"
    ]
    for f in findings:
        got = f.published if f.published is not None else "no entry for this file"
        extra = f" [{f.problem}]" if f.problem else ""
        lines.append(f"  {f.artifact.filename}: pinned {f.artifact.sha256}, published {got}{extra}")
    lines.append(
        "The upstream archive may have been replaced after alp-sdk pinned it. Do not "
        "bypass this; update the pin in alp-sdk only after the new archive is reviewed."
    )
    return "\n".join(lines)
