#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Duplicate-key-rejecting YAML/JSON loaders (alp-sdk issue #1127).

RELOCATED (was alp-sdk `scripts/strict_loaders.py`, a top-level module outside
`scripts/alp_orchestrate/`, hand-ported the same way `zephyr_board.py` /
`project_loader.py` were -- named in `HAND_PORT_SOURCES` (not `PINNED_HASHES`,
since it has no `scripts/alp_orchestrate/` counterpart to pin by filename), but
its staleness hash lives in its OWN `STRICT_LOADERS_HASH` /
`STRICT_LOADERS_PINNED_SDK_COMMIT` pair, not `HAND_PORT_HASHES` -- see
`test_planner_relocation_freshness.py`'s STRICT_LOADERS_* comment for why).

`yaml.safe_load` and `json.loads` silently keep only the LAST value of a
duplicated mapping key -- `yaml.safe_load("som: a\\nsom: b\\n")` returns
`{'som': 'b'}` with no error or warning. In a hand-edited board.yaml,
SoM preset, or chip/SoC manifest, a duplicated key is invisible in a
diff and silently drops hardware configuration. `strict_yaml_load()`
and `strict_json_loads()` are the one place `tan/planner/loader.py`'s
`_load_yaml` / `_load_json` route through instead of the raw stdlib
loaders, so a board.yaml `tan build` accepts is the same board.yaml
`tan validate` (which shells the SDK validator, itself routed through
alp-sdk's own copy of this module) accepts.

Zero dependencies beyond PyYAML on purpose, same rationale as alp-sdk's
copy: this is a leaf every metadata-ingestion boundary can import without
creating a cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class DuplicateKeyError(ValueError):
    """Raised when a YAML/JSON mapping repeats a key."""


def _no_duplicates_mapping_constructor(
    loader: yaml.SafeLoader, node: yaml.MappingNode
) -> dict[str, Any]:
    # Duplicate-key detection must run on the node's OWN explicit pairs
    # BEFORE `flatten_mapping()` splices in `<<: *anchor` merge-key
    # pairs -- otherwise a spec-legal merge override (the anchor
    # supplies a default, a sibling explicit key overrides it) reads as
    # the same key twice and gets rejected as a duplicate, when the
    # explicit value should just win, exactly like the stdlib loader
    # does. Scanning only the node's own pairs also leaves two merge
    # SOURCES quietly disagreeing on a key to the normal YAML
    # first-wins merge semantics, not a hard error -- this loader's job
    # is to catch a literal duplicate key typed twice in one mapping.
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            mark = key_node.start_mark
            raise DuplicateKeyError(
                f"duplicate key {key!r} at line {mark.line + 1}, "
                f"column {mark.column + 1}"
            )
        seen.add(key)

    loader.flatten_mapping(node)
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


class _StrictLoader(yaml.SafeLoader):
    pass


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_mapping_constructor
)


def strict_yaml_load(text: str, source: str | Path = "<string>") -> Any:
    """`yaml.safe_load`, but raise `DuplicateKeyError` on a repeated mapping key."""
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except DuplicateKeyError as e:
        raise DuplicateKeyError(f"{source}: {e}") from e


def _no_duplicates_object_pairs_hook(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise DuplicateKeyError(f"duplicate key {key!r}")
        mapping[key] = value
    return mapping


def strict_json_loads(text: str, source: str | Path = "<string>") -> Any:
    """`json.loads`, but raise `DuplicateKeyError` on a repeated object key."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates_object_pairs_hook)
    except DuplicateKeyError as e:
        raise DuplicateKeyError(f"{source}: {e}") from e
