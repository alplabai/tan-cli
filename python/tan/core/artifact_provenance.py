# SPDX-License-Identifier: Apache-2.0
"""`metadata/bootstrap.json`'s `artifactProvenance` block, normalised --
pure, no IO (same contract as `tan.core.bootstrap`, which imports this).

alp-sdk v0.16.0 (alplabai/alp-sdk#1574) added a top-level `artifactProvenance`
map to the manifest tan already reads for `missingPrerequisites[].command`:

    "artifactProvenance": {
      "cmake": {"tier": "A", "source": "https://cmake.org/",
                "sizeBytes": null, "licence": "BSD-3-Clause"},
      ...
    }

Its keys are the `prerequisites.{posix,macos,windows}` tool vocabulary (plus
`7zip`) -- the SAME names `missingPrerequisites[].tool` already carries -- so
carrying the block onto those entries is a join on an identity that already
exists, not a new one (tan-cli#1066).

**`source` on the wire becomes `sourceUrl`.** alp-sdk spells the upstream
project page `source`; the envelope deliberately does not. The consumer that
asked for this (alp-sdk-vscode#467's dependency-consent screen) already
defines a `source` column as *what will actually run* -- the install command,
verbatim -- and the artifact a user actually installs is a brew bottle or an
apt package, not the tarball behind that page. One name for two meanings would
misinform the security reader the screen exists for, so tan-cli#1066 renames
it at the seam it crosses. `licence` keeps alp-sdk's own (British) spelling,
because that one is the same fact under the same name.

**Absence is `null`, never an omitted key and never a default.** `xz` and
`7zip` genuinely declare no licence upstream and every entry's `sizeBytes` is
`null` today; an SDK predating the block (v0.16.0-rc1 and earlier) reports
`null` for all four. "Not reported" is a true answer and an invented one is
not -- and a key set that is stable regardless of the SDK in front of it is
what lets `contract/doctor-data-keys.json` declare one shape rather than
"these four keys, sometimes".

**Malformed input DEGRADES, it does not refuse.** This is the one place this
module deliberately does NOT reuse the curated-error register the sibling
readers use (`planner.template._require_mapping_doc` / `_require_field`, PR
tan-cli#1073/#1082, which raise a named `TemplateError`). Those guard a
document whose content the command cannot proceed without. This one is
advisory display metadata hanging off `tan doctor` -- whose whole job is to
report on a broken environment, and which already treats a missing or
malformed `bootstrap.json` as a warning with a documented fallback rather than
a refusal (`doctor_cmd._load_manifest`'s own docstring: "a doctor that cannot
start because the thing it diagnoses is broken is the failure mode it exists
to prevent"). Refusing here would let one upstream typo in a field nobody
builds with take out the diagnosis of every OTHER thing wrong with the host.
So every malformed shape -- a non-mapping block, a non-mapping entry, a
missing or wrongly-typed field -- collapses to the SAME `null` an absent SDK
key yields, per FIELD rather than per block: a `cmake` entry whose `sizeBytes`
is the string `"12"` still reports its real `tier`/`licence`/`sourceUrl`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The manifest's own key for the block, and for the one field renamed on the
#: wire. Named rather than inlined so the rename has exactly one spelling.
BLOCK_KEY = "artifactProvenance"
SDK_SOURCE_KEY = "source"


@dataclass(frozen=True)
class ArtifactProvenance:
    """One tool's provenance, already normalised to the wire's own shape.

    Every field defaults to `None` so `ArtifactProvenance()` IS the "not
    reported" value -- there is no second spelling of absence to keep in sync,
    and a caller with no table at hand (`UNKNOWN` below) produces exactly what
    a caller holding a table with no entry for its tool does.
    """

    tier: str | None = None
    licence: str | None = None
    source_url: str | None = None
    size_bytes: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        """The four keys as `missingPrerequisites[]` carries them. Always all
        four, always in this order -- see the module docstring on absence."""
        return {
            "tier": self.tier,
            "licence": self.licence,
            "sourceUrl": self.source_url,
            "sizeBytes": self.size_bytes,
        }


#: The "nothing reported" provenance: an SDK predating `artifactProvenance`, a
#: tool with no entry in it (`west`, `zephyrSdk`, `setools`, `jlink` and
#: `python3-venv` all have none today), or an entry too malformed to read.
UNKNOWN = ArtifactProvenance()


def _text(value: Any) -> str | None:
    """A string field, or `None` for anything else -- including a number, a
    `null` alp-sdk wrote deliberately (`xz`/`7zip`'s `licence`), and a nested
    object. No `str()` coercion: rendering `{'a': 1}` as a licence in a consent
    screen would be an invented fact, which is the one thing this must not do."""
    return value if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    """A byte count, or `None`. `bool` is excluded explicitly for the reason
    the sibling readers exclude it (`parse_bootstrap_manifest`, `_load_manifest`):
    `True == 1` in Python, so `"sizeBytes": true` would otherwise report a size
    of one byte. A float is rejected rather than truncated -- alp-sdk emits
    `null` or an integer, and a truncation here would be tan inventing a
    precision the manifest never claimed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def entry_from_doc(doc: Any) -> ArtifactProvenance:
    """One `artifactProvenance[<tool>]` value, normalised. Anything that is not
    a mapping (a bare string, a list, `null`) yields `UNKNOWN` rather than
    raising -- see the module docstring."""
    if not isinstance(doc, dict):
        return UNKNOWN
    return ArtifactProvenance(
        tier=_text(doc.get("tier")),
        licence=_text(doc.get("licence")),
        source_url=_text(doc.get(SDK_SOURCE_KEY)),
        size_bytes=_count(doc.get("sizeBytes")),
    )


def parse_table(raw: Any) -> dict[str, ArtifactProvenance]:
    """The whole `artifactProvenance` block, normalised to `tool ->
    ArtifactProvenance`.

    `{}` for an absent block, a block that is not a mapping, or one whose every
    entry is unreadable -- all four of which are the same fact downstream
    ("nothing to report"), and none of which is an error here. A non-string key
    is dropped: `missingPrerequisites[].tool` is a string, so a key that could
    never match one carries no information and keeping it would only widen this
    return type for a lookup that cannot hit.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        tool: entry_from_doc(doc) for tool, doc in raw.items() if isinstance(tool, str)
    }


def for_tool(table: dict[str, ArtifactProvenance] | None, tool: str) -> ArtifactProvenance:
    """`table`'s entry for `tool`, or `UNKNOWN`. Takes `None` for a caller that
    has no table at all (every pre-tan-cli#1066 call site, and every unit test
    of the refusal builders), so "no table" and "no entry" cannot diverge."""
    if not table:
        return UNKNOWN
    return table.get(tool, UNKNOWN)
