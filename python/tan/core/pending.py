# SPDX-License-Identifier: Apache-2.0
"""The one definition of alp-sdk's `TBD` pending-placeholder sentinel (#276).

alp-sdk writes the literal string `TBD` into a manifest/preset field it has
not filled in yet -- `flash_args: TBD`, `silicon_variant: TBD`, an unfilled
`e1m_pad` -- a deliberate "don't invent a value" convention, not an error.
Every reader of that convention has to agree on what "unfilled" means, so the
rule lives here ONCE rather than being re-typed at each read site.

This module is deliberately the neutral one: it has no flash-planning
machinery (`tan.core.flash_plan`) and no image-bundle machinery
(`tan.core.image_bundle`) behind it, so a consumer that is neither -- `tan.core.size`,
and `pinmux` once it is ported -- can test for the sentinel without pulling
either domain in. `flash_plan` still owns the flash-specific decisions built
on top (`flash_args_has_tbd`'s mapping/sequence recursion, the skip-vs-fail
split between `flash_args` and an artefact path); only the base comparison
moved here.

Trimmed before comparing -- a YAML `device: "  TBD  "` is the same unfilled
field -- but deliberately NOT case-folded and NOT a substring test:
`TBD-1234-XYZ` is a plausible part number and `flash_args.build_dir:
/opt/TBDtool/x` a plausible path, and refusing either would block a
legitimate value. `tbd` lowercase is not the sentinel alp-sdk emits; widening
to it means widening the SDK's convention first, in one place, not here.
"""
from __future__ import annotations

from typing import Any

#: alp-sdk's unfilled-field sentinel.
PENDING_PLACEHOLDER = "TBD"


def is_pending_placeholder(value: Any) -> bool:
    """Whether a manifest/preset SCALAR is the SDK's unfilled-field sentinel.

    `None` and every non-string value are never pending; a string is pending
    only when it is exactly `PENDING_PLACEHOLDER` after trimming surrounding
    whitespace."""
    return isinstance(value, str) and value.strip() == PENDING_PLACEHOLDER
