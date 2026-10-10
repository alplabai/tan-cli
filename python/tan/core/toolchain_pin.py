# SPDX-License-Identifier: Apache-2.0
"""Pure comparison of alp-sdk's per-artifact sha256 pins (`metadata/toolchains.json`)
against the release's own `sha256.sum` (tan-cli#1496).

`west sdk install` downloads each archive into a private temp directory and
deletes it, so tan never holds the archive bytes. West itself only checks the
archive against the release's `sha256.sum`, which an upstream that swaps an
archive and republishes the sum passes cleanly. tan therefore fetches that small
sum file BEFORE the install (`tan.commands.bootstrap_toolchain_pin`) and refuses
when any pinned artifact's entry differs from alp-sdk's pin. No second archive
download. **No IO here** -- the sum text arrives already read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tan.core.toolchain_provision import ToolchainArtifact, ToolchainManifest

#: The file west reads, next to the archives, under the manifest's `baseUrl`.
SUM_FILENAME = "sha256.sum"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def sum_url(manifest: ToolchainManifest) -> str:
    return manifest.base_url.rstrip("/") + "/" + SUM_FILENAME


def parse_sum_file(text: str) -> dict[str, str]:
    """`sha256sum`-format text -> `{basename: lowercase hex}`. Lines that are not
    `<64 hex> [*| ]<name>` are ignored; a duplicated name keeps the LAST entry."""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not _HEX64.match(parts[0]):
            continue
        name = parts[1].strip().lstrip("*").replace("\\", "/").rsplit("/", 1)[-1]
        if name:
            entries[name] = parts[0].lower()
    return entries


@dataclass(frozen=True)
class PinFinding:
    artifact: ToolchainArtifact
    #: `None` = the sum file has no entry for this filename.
    published: str | None


def pin_findings(
    artifacts: tuple[ToolchainArtifact, ...], sums: dict[str, str]
) -> tuple[PinFinding, ...]:
    """Every artifact whose published sum is absent or differs from its pin."""
    out = []
    for art in artifacts:
        published = sums.get(art.filename)
        if published is None or published != art.sha256.strip().lower():
            out.append(PinFinding(art, published))
    return tuple(out)


def refusal_message(findings: tuple[PinFinding, ...], url: str) -> str:
    lines = [
        f"cannot acquire the cross toolchain: the release's {SUM_FILENAME} ({url}) "
        f"disagrees with the sha256 alp-sdk pins in metadata/toolchains.json, so "
        f"nothing was downloaded:"
    ]
    for f in findings:
        got = f.published if f.published is not None else "no entry for this file"
        lines.append(f"  {f.artifact.filename}: pinned {f.artifact.sha256}, published {got}")
    lines.append(
        "The upstream archive may have been replaced after alp-sdk pinned it. Do not "
        "bypass this; update the pin in alp-sdk only after the new archive is reviewed."
    )
    return "\n".join(lines)
