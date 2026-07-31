#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Filesystem roots for the planner package -- the bottom leaf.

`REPO`, `METADATA_ROOT`, and the schema paths. A near-dependency-free leaf so
any sibling -- topology, the loader, the carve-out / partition resolvers -- can
import a path root directly instead of reaching back into the package __init__
(which would cycle). Extracting these is the #285 paths seam that lets
topology.py drop its lazy `BOARD_SCHEMA` import.

RELOCATED (was alp-sdk `scripts/alp_orchestrate/paths.py`): the roots used to be
derived from this file's own location -- three parents up landed on the SDK
checkout. Inside `tan` that walk lands on the `tan` package, so the root is now
an explicit binding resolved by `tan.planner_root.sdk_root()`, which raises if
the package was imported before a root was bound. `REPO` still means *the
alp-sdk checkout*, never tan's own tree: `metadata/**`, `firmware/`,
`zephyr/sysbuild/` and the git commit all come from the SDK (ADR-0017 -- the
generators relocated, the facts did not).
"""

from __future__ import annotations

from tan.planner_root import sdk_root

# Evaluated at import time, and deliberately so: siblings take
# `metadata_root: Path = METADATA_ROOT` as a *default argument*, which binds
# here too. `sdk_root()` raising on an unbound import is what keeps that from
# becoming a silently-wrong metadata tree.
REPO = sdk_root()
METADATA_ROOT = REPO / "metadata"
BOARD_SCHEMA = METADATA_ROOT / "schemas" / "board.schema.json"
BOARD_PRESET_SCHEMA = METADATA_ROOT / "schemas" / "board-preset.schema.json"
