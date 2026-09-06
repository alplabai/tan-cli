# SPDX-License-Identifier: Apache-2.0
"""`ospi_memories:`/`hyperram:` `on_module:` scaffold lines for `tan new-som`
(tan-cli#1220).

Split into its own module rather than grown inside `new_som_cmd.py`: that
file already sits at its `tests/gates/module_size_budget.d/` ceiling
(`MODULE_CAP`), and PR #1222 set the precedent for this exact situation --
`model_cmd.py` at its own ceiling got a new `tan/core/model_list.py` for the
actual engine, keeping the command file's own growth to dispatch/glue. This
module is the `_render_preset` sibling: pure string rendering, no IO, no
click/typer.

Scoped to Alif Ensemble parts only. The caller (`_render_preset`) already
splits every `soc_ref` into `vendor_slug, family_slug, part_slug` for its own
`metadata/socs/...` path -- `is_alif_ensemble` reads that SAME triple rather
than adding a second vendor/family discrimination mechanism (tan-cli#1220's
own instruction: reuse how `new_som_cmd.py` already tells vendors/families
apart, don't invent a new one). The scope itself is not arbitrary: the
schema's own `on_module.ospi_memories` description reads "(AEN family)"
(`metadata/schemas/som-preset-v1.schema.json`), and every shipped
`E1M-AEN*.yaml` preset that populates these fields (301/401/501/601/701/801)
uses the identical CS0=NOR/CS1=HyperRAM split on one shared octal OSPI0
controller.

Every value below is either that shared Ensemble-silicon convention
(`chip_select`, `interface`) or an explicit placeholder -- never an invented
MPN or capacity (ADR-0017, invariant I-26). Two of the schema's fields have
no `"TBD"` string escape hatch the way `chip`/`capacity_mbit` do:
`chip_select` is a bare `"type": "integer"` (no `oneOf` alternative), and
`role`/`interface` must match a lowercase-slug pattern (`^[a-z][a-z0-9_]*$`,
which an uppercase `TBD` fails). The convention value fills `chip_select`/
`interface`; the schema-legal lowercase literal `tbd` fills `role`, matching
`inference.preferred_backend`'s own `tbd` placeholder convention next door in
`_render_preset`.

`chip_select`'s only bound in `metadata/schemas/som-preset-v1.schema.json` is
`"minimum": 0` -- there is no schema maximum, and no `scripts/check_*.py`
gate in the pinned alp-sdk checkout mentions `chip_select` at all (measured:
`grep -rn chip_select scripts/` finds nothing). "Bounded to 0 or 1" is a
description of the real OSPI0 controller's two physical chip-selects, not an
enforced ceiling; nothing stops a preset from declaring `chip_select: 2` and
still passing every automated check in this checkout.

`assembled:` (optional on both `$defs`, and also has no `"TBD"` variant) is
left out of the rendered block entirely -- the schema's own default when
omitted is `true` (populated), which this scaffold has no basis to assert OR
deny for a module whose BOM does not exist yet. Matches how `_render_preset`
already treats every other schema-optional block it cannot safely guess
(`capabilities:`, `pad_routes:`, `helper_firmware:`): a comment, not a
guessed value.
"""
from __future__ import annotations

#: The one (vendor, family) pair `ospi_memories:`/`hyperram:` are scoped to
#: -- see the module docstring for why.
_ALIF_ENSEMBLE = ("alif", "ensemble")


def is_alif_ensemble(vendor_slug: str, family_slug: str) -> bool:
    """True when a `soc_ref`'s parsed `vendor_slug`/`family_slug` name an
    Alif Ensemble part -- the one scope `render_ospi_memories_and_hyperram`
    is meant to be called under."""
    return (vendor_slug, family_slug) == _ALIF_ENSEMBLE


def render_ospi_memories_and_hyperram() -> list[str]:
    """The `ospi_memories:`/`hyperram:` `on_module:` sub-block, as 2-space-
    indented lines ready to append inside `_render_preset`'s `on_module:`
    section (append after `eeprom:`, before the `i2c_devices:` comment
    block -- the same relative order every shipped `E1M-AEN*.yaml` preset
    that carries both keys uses). See the module docstring for what each
    field's placeholder is and why.
    """
    lines: list[str] = []
    a = lines.append
    a("  # OSPI0 dual-chip-select external memory (Ensemble-family")
    a("  # convention): NOR flash on CS0, HyperRAM on CS1, sharing one")
    a("  # shared octal bus.  Delete the whole ospi_memories:/hyperram:")
    a("  # pair if this module populates neither; add an ospi1: row")
    a("  # alongside ospi0: for a second, independent OSPI controller.")
    a("  ospi_memories:")
    a("    ospi0:")
    a("      chip:           TBD   # storage-part MPN (datasheet/BOM), or TBD")
    a("      capacity_mbit:  TBD   # Mbit, from the storage-part datasheet")
    a("      chip_select:    0     # OSPI0 CS0 (Ensemble-family convention)")
    a("      role:           tbd   # what it's for, e.g. app_storage")
    a("  # HyperRAM sharing the OSPI0 octal bus with the flash above,")
    a("  # separated only by chip-select -- delete if not populated.")
    a("  hyperram:")
    a("    chip:           TBD     # HyperRAM MPN (datasheet/BOM), or TBD")
    a("    capacity_mbit:  TBD     # Mbit, from the HyperRAM datasheet")
    a("    interface:      ospi0   # controller instance (Ensemble convention)")
    a("    chip_select:    1       # OSPI0 CS1 (Ensemble-family convention)")
    return lines
