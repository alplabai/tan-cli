# SPDX-License-Identifier: Apache-2.0
"""The one SoM-preset `schema_version` this tan reads, and the message for a
preset it skips because it declares another.

alp-sdk#2024 moved every `metadata/e1m_modules/*.yaml` preset to
`schema_version: 2` (`som-preset-v2.schema.json`, `write_authority` required,
`npu_population` removed). tan-cli#1297 moved tan's readers with it, and every
one of them refuses a preset declaring anything else rather than guessing at a
shape it does not know. Against an SDK that predates #2024 (released alp-sdk
`v0.16.0` and older) that refusal reaches EVERY preset, so `tan presets`
listed `skus: []` and `tan size` reported every budget as `unreadable SoM
preset`, with nothing saying why. `skipped_presets_message` is the why: one
sentence, shared by both commands so they cannot word the same fact two ways.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The one `schema_version:` a SoM preset may declare.
#: `som-preset-v2.schema.json` pins it `const: 2`.
SOM_SCHEMA_VERSION = 2

#: The upstream change that introduced v2, named in the message so a reader
#: can tell "old SDK" from "broken preset".
_V2_ORIGIN = "alp-sdk#2024"


def is_supported_som_schema_version(version: object) -> bool:
    """True only for the bare integer `SOM_SCHEMA_VERSION`. `bool` is excluded
    explicitly: `True == 1` and `isinstance(True, int)` both hold in Python."""
    return isinstance(version, int) and not isinstance(version, bool) and version == SOM_SCHEMA_VERSION


def _remedy(versions: list[object]) -> str:
    ints = [v for v in versions if isinstance(v, int) and not isinstance(v, bool)]
    if ints and len(ints) == len(versions) and max(ints) < SOM_SCHEMA_VERSION:
        return (
            f"the bound alp-sdk predates som-preset v{SOM_SCHEMA_VERSION} "
            f"({_V2_ORIGIN}); point --sdk-root at an alp-sdk whose "
            f"metadata/schemas/ ships som-preset-v{SOM_SCHEMA_VERSION}.schema.json, "
            "or use a tan release that matches this SDK"
        )
    if ints and len(ints) == len(versions) and min(ints) > SOM_SCHEMA_VERSION:
        return "the bound alp-sdk is newer than this tan; upgrade tan"
    return "fix the preset's schema_version or use an alp-sdk whose presets match this tan"


def skipped_presets_message(subject: str, versions: Iterable[object]) -> str:
    """`<subject> declare(s) schema_version <v[, v...]> (this tan reads
    som-preset schema_version 2): <remedy>.`

    *subject* names what was skipped ("12 SoM presets under <dir>", "the SoM
    preset for E1M-AEN801"); *versions* is every skipped preset's declared
    value, duplicates allowed -- they are collapsed, in first-seen order, and
    an absent key reads as `None`, exactly what the preset said."""
    seen = list(versions)
    shown = ", ".join(dict.fromkeys(repr(v) for v in seen))
    return (
        f"{subject}: schema_version {shown} (this tan reads "
        f"som-preset schema_version {SOM_SCHEMA_VERSION}) -- {_remedy(seen)}."
    )
