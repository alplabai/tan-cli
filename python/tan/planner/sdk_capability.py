# Copyright (c) 2026 Alp Lab AB
# SPDX-License-Identifier: Apache-2.0
"""
A general "does the BOUND alp-sdk checkout carry capability X" floor
(tan-cli#591).

`tan/planner/zephyr_board.py` hand-ports alp-sdk's `scripts/gen_zephyr_board.py`
generator, which grows a REQUIRED fact from time to time (the SE-owned `atoc`
memory-map region, alp-sdk#1289; the SoC JSON's `zephyr_peripherals_dtsi` key,
alp-sdk#1352). When such a fact is absent, there are two entirely different
causes that used to produce ONE message blaming the customer's own metadata:

  1. the bound alp-sdk checkout PREDATES the requirement -- no released
     alp-sdk has it yet, and no SoM/SoC authoring fixes it;
  2. the checkout is new enough, but THIS SoM/SoC/board's own metadata is
     genuinely incomplete.

This module answers (1) directly, from the checkout's own facts, rather than
inferring it from the shape of one board's metadata (the imprecise heuristic
`_aen_missing_region_message` used to apply to `atoc` alone, before this
module existed -- see its removal in the same change that added this file).
A caller still owns its OWN case-(2) message; `require_capability` only rules
case (1) out or in.

Deliberately keyed on a CAPABILITY, never a bare alp-sdk version number: the
planner mirror (`tan/planner/`) moves per-capability as alp-sdk gains ported
requirements one at a time, not on a release cadence, so a version-number
floor would need renegotiating on every unrelated alp-sdk release and would
say nothing true about a checkout built from an arbitrary commit. A
capability's *probe* instead reads a fact that ships with the checkout itself
-- a JSON-Schema property, or an entry in alp-sdk's own
`metadata/quality-tasks-v1.json` gate registry (the same registry
`adding-a-ci-gate`'s four-site lockstep keeps in sync with `scripts/check_*.py`
on the alp-sdk side) -- so the answer is correct for ANY checkout, release or
`dev`, without tan knowing which alp-sdk version introduced it.

Every probe reads ONLY under `metadata_root` (never `<sdk_root>/scripts/`):
`metadata/` is the one directory `tan/planner/` already has a resolved root
for (ADR-0017 -- the generators relocated into tan, the facts did not), and
restricting probes to it means a probe can be exercised against a plain copy
of `metadata/`, the same fixture shape
`tests/planner/test_zephyr_board_metadata_facts.py`'s `_MutatedMetadata`
already builds for every other refusal in this generator.

Adding the NEXT ported requirement's floor is: one `SdkCapability` entry
below (an id, the alp-sdk issue, a description, and a probe), plus one
`require_capability(metadata_root, THE_CAPABILITY)` call at the site that
already raises the authoring-gap message for that fact -- not a second
bespoke heuristic.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SdkTooOldError(Exception):
    """The BOUND alp-sdk checkout itself predates a required capability --
    distinct from a `ZephyrBoardEmitError` authoring gap, which means the
    checkout knows the fact but THIS SoM/SoC/board's own metadata omits it.

    Not a `ZephyrBoardEmitError` subclass on purpose: `zephyr_board.py`'s own
    exception funnels (both `tan generate`'s `_emit_one_in_process` and every
    `pytest.raises` in its test module) catch broadly enough that the
    distinction survives in the message via `type(err).__name__` -- exactly
    how `tan generate`'s in-process engine already reports every planner
    exception (`generate_cmd.py::_emit_one_in_process`) -- without requiring
    a new top-level issue code in `generate_cmd.py`, which is out of scope
    for this fix (tan-cli#591 stays inside `tan/planner/`).
    """

    def __init__(self, capability: "SdkCapability") -> None:
        self.capability = capability
        super().__init__(
            f"this alp-sdk predates {capability.description} "
            f"({capability.issue}); {capability.emit_description} needs a "
            "checkout containing it -- upgrade alp-sdk, or pin tan to a "
            "release that predates the requirement"
        )


@dataclass(frozen=True)
class SdkCapability:
    """One ported requirement's checkable floor.

    *probe* answers True iff *metadata_root* shows the bound alp-sdk
    checkout knows the concept AT ALL -- independent of whether any
    particular SoM/SoC/board actually used it. `id` and `issue` are for
    diagnostics and tests, not compared against anything; `description` and
    `emit_description` compose directly into `SdkTooOldError`'s message.
    """

    id: str
    issue: str
    description: str
    probe: Callable[[Path], bool]
    emit_description: str = "the AEN board emit"


def _read_json(path: Path) -> Any | None:
    """`None` on anything short of a parsed JSON value -- absent, unreadable,
    or malformed are all "this probe can't confirm the capability", not a
    crash. A probe answering `False` when it can't tell is the safe
    direction: it falls through to the ordinary authoring-gap message
    instead of manufacturing an SDK-floor refusal from a read error."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def schema_declares_property(schema_relpath: str, prop: str) -> Callable[[Path], bool]:
    """Probe factory: True iff `metadata/schemas/<schema_relpath>` declares
    *prop* as a top-level JSON-Schema property.

    A property that exists only from some alp-sdk commit onward is a
    reliable, checkout-only marker for "this checkout is new enough to know
    the concept": schemas ship with every checkout, release or `dev` alike,
    so the probe needs nothing beyond the already-resolved `metadata_root`.
    """

    def _probe(metadata_root: Path) -> bool:
        spec = _read_json(metadata_root / "schemas" / schema_relpath)
        if not isinstance(spec, dict):
            return False
        return prop in (spec.get("properties") or {})

    return _probe


def quality_task_declared(task_id: str) -> Callable[[Path], bool]:
    """Probe factory: True iff `metadata/quality-tasks-v1.json` lists a task
    with id *task_id* -- alp-sdk's own registry of which `check_*.py` gates
    exist (the same registry `adding-a-ci-gate`'s four-site lockstep keeps
    current on the alp-sdk side). A requirement that shipped its own gate is
    checkable through the registry it already updated, rather than inventing
    a second marker file for tan to track.
    """

    def _probe(metadata_root: Path) -> bool:
        doc = _read_json(metadata_root / "quality-tasks-v1.json")
        if not isinstance(doc, dict):
            return False
        tasks = doc.get("tasks")
        if not isinstance(tasks, list):
            return False
        return any(isinstance(t, dict) and t.get("id") == task_id for t in tasks)

    return _probe


# ---------------------------------------------------------------------
# Registry -- one entry per ported requirement.
# ---------------------------------------------------------------------

AEN_ATOC_RESERVATION = SdkCapability(
    id="aen.atoc_reservation",
    issue="alp-sdk#1289",
    description="the SE-owned ATOC reservation",
    probe=quality_task_declared("atoc-reservation"),
)

AEN_ZEPHYR_PERIPHERALS_DTSI = SdkCapability(
    id="aen.zephyr_peripherals_dtsi",
    issue="alp-sdk#1352",
    description="the per-SoC `zephyr_peripherals_dtsi` overlay reference",
    probe=schema_declares_property(
        "soc-spec-v1.schema.json", "zephyr_peripherals_dtsi"
    ),
)


def require_capability(metadata_root: Path, capability: SdkCapability) -> None:
    """No-op when the bound checkout carries *capability*; raises
    `SdkTooOldError` when it doesn't.

    Answers ONLY "does the checkout know the concept" -- a caller still owns
    its own authoring-gap message for "the checkout knows it, but this
    SoM/SoC/board's metadata doesn't declare it".
    """
    if not capability.probe(metadata_root):
        raise SdkTooOldError(capability)
