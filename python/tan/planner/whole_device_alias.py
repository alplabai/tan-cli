#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Shared "extent == aperture exactly" whole-device-alias predicate (alp-sdk#2073).

A `memory_map:` region whose resolved `[base, base + size)` extent equals
a SoC's declared on-die MRAM aperture exactly (e.g. `mram_main`, once its
`base` stops being the `"TBD"` sentinel) is the device itself, not a
partition inside it -- `atoc`/`mcuboot`/`he_slot0`/`hp_slot0`/`reserved`/
`storage` all subdivide the SAME window `mram_main` aliases.

Two independent callers need this exact predicate and must never drift on
it: `aperture.py`'s `classify_region()` / `is_partition_inside_aperture()`
(the aperture-tiling / flash-class checks, alp-sdk#1365 split B), and
`zephyr_board.py`'s `_aen_check_map_overlaps()` (the disjoint-slot0
overlap check, alp-sdk#2073).

RELOCATION NOTE -- this module keeps its own file for a DIFFERENT reason
than upstream's. Upstream `scripts/whole_device_alias.py` is flat and
package-free because `gen_zephyr_board.py` lives OUTSIDE the
`alp_orchestrate` package, so importing the predicate from
`alp_orchestrate.aperture` would drag in the whole orchestrator
(jsonschema, alp_project, alp_cli) and add a package-to-generator import
loop with `loader.py` / `secure.py`'s existing deferred import going the
other way. NEITHER hazard exists here: `aperture.py` and `zephyr_board.py`
are both inside `tan.planner`, so a plain `from .whole_device_alias import
...` costs nothing and closes no loop. What DOES carry over is the reason
the predicate is shared at all -- one spelling, so the callers cannot
drift, which is the entire point of alp-sdk#2073.

That is also why this is NOT inlined the way `sentinels.is_tbd` was
relocated into `zephyr_board.py::_is_tbd`. That inline stays within ONE
module (six call sites, all inside `zephyr_board.py`); this predicate is
needed by TWO, so inlining it would mean two copies in two files.
And `is_tbd` is the warning here, not the model: tan already carries a
SECOND, independent copy of that same predicate at `slugs.py:108`
(imported by `kconfig.py`), so the relocated spelling did not stay single
even inside tan. Keeping exactly one spelling of THIS predicate is the
whole point of alp-sdk#2073.

Upstream's docstring also records that `check_atoc_reservation.py` still
hand-writes this same comparison instead of importing it. That gap is
alp-sdk's alone -- tan relocates no counterpart of that gate, so the
three call sites named above (`aperture.py`'s two, `zephyr_board.py`'s
one) are the whole surface here.
"""

from __future__ import annotations


def is_whole_device_alias(
    ext: tuple[int, int], aperture: tuple[int, int],
) -> bool:
    """True when *ext* equals *aperture* exactly -- the whole-device alias
    case, not a partition inside the device.

    `classify_region()` and `is_partition_inside_aperture()` both test
    this FIRST, before their subset-containment check, because the
    alias's own extent also satisfies `lo >= full_lo and hi <= full_hi`
    -- order matters, not just the predicate. Comparing BOTH edges is
    the whole point: a region merely flush with the aperture's low edge
    (e.g. `mcuboot`, same `base` as `mram_main`, far smaller `size_kib`)
    must NOT match here, or a genuine overlap at that edge would be
    silently excluded from the overlap comparison instead of refused.
    """
    lo, hi = ext
    full_lo, full_hi = aperture
    return lo == full_lo and hi == full_hi
