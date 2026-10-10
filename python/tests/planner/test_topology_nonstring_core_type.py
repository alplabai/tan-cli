# SPDX-License-Identifier: Apache-2.0
"""`core_os_topology`'s `soc_types` read (tan-cli#957): the identical
unguarded `c.get("type", "")` `presets_cmd.core_type_lookup` carried, in the
build/`--emit os-topology` path this time.

Same requirement as `tests/planner/test_topology_metadata_root_override.py`
(the sibling this file's fixture is copied from): `tan.planner.topology`
cannot be imported before `bind_sdk_root(<checkout>)` has run, so this needs
a real `ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT` and skips, loudly, without one --
never a silent pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as test_topology_metadata_root_override.py)"
)


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    import tan.planner as planner_pkg

    return planner_pkg


@pytest.mark.parametrize(
    "type_value",
    [7, ["cortex-a55"], {"a": 1}, True, None, 0, []],
    ids=["int", "list", "dict", "bool", "null", "zero", "emptylist"],
)
def test_core_os_topology_normalises_a_nonstring_core_type_never_raises(
    planner, type_value
):
    """A schema-invalid `soc_spec` (the shape `soc-spec-v1.schema.json`'s own
    `"type": {"type": "string"}` forbids, but nothing stops a hand-authored,
    mid-`porting-a-new-som`, or corrupted SoC JSON from producing) must not
    raise `AttributeError` out of `_runtime_class`/`_default_os_from_core_type`'s
    `(core_type or "").lower()`, and must not write the raw non-string value
    onto the emitted `core_type` field either.

    Mutation-proven: reverting the `soc_types` dict comprehension's `c["type"]
    if isinstance(c.get("type"), str) else ""` to the bare `c.get("type", "")`
    it replaced turns every truthy case in this parametrize RED
    (`AttributeError`) and the `core_type`/`runtime_class` assertions below RED
    for the falsy cases too (`None`/`0`/`[]` would leak onto the wire instead
    of normalising to `""`). Restoring it turns all seven GREEN -- verified
    by hand while writing this test.
    """
    from tan.planner.models import BoardProject, Slice
    from tan.planner.topology import core_os_topology

    project = BoardProject(
        sku="E1M-TEST",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={"a55": Slice(core_id="a55", os="yocto")},
        ipc=[],
        soc_spec={"cores": [{"id": "a55", "type": type_value}]},
        som_preset={},
        board_preset=None,
    )

    result = core_os_topology(project)
    rows = {row["core_id"]: row for row in result["cores"]}
    assert rows["a55"]["core_type"] == ""
    assert rows["a55"]["runtime_class"] == "other"
    assert rows["a55"]["default_os"] == "off"
    assert rows["a55"]["allowed_os"] == []
